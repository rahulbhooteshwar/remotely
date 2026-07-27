"""SSH transports.

Two implementations behind one interface:

* :class:`ParamikoTransport` speaks SSH in-process. Nothing needs to be
  installed, and a password goes straight into the handshake - it never touches
  the filesystem, an environment variable, or another process's argv.
* :class:`SystemSSHTransport` shells out to the host's ``ssh`` inside a local
  PTY. Opt-in per host, for the cases Paramiko does not cover (ProxyCommand,
  GSSAPI, exotic certificate setups).

Both expose a byte stream plus resize and close, which is all the terminal
layer needs to drive them.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import config
from .models import Credential, Host

DEFAULT_TERM = "xterm-256color"
CONNECT_TIMEOUT = 20.0
KEEPALIVE_SECONDS = 30


class TransportError(RuntimeError):
    """Connection or authentication failure, phrased for the status line."""


class AuthFailed(TransportError):
    """Credentials were rejected."""


class HostKeyRejected(TransportError):
    """The server's host key did not match a previously recorded one."""


class Transport(Protocol):
    """What the terminal layer needs from a session."""

    def connect(self, cols: int, rows: int) -> None: ...
    def read(self, size: int = 65536) -> bytes: ...
    def write(self, data: bytes) -> None: ...
    def resize(self, cols: int, rows: int) -> None: ...
    def close(self) -> None: ...
    @property
    def closed(self) -> bool: ...
    @property
    def exit_status(self) -> int | None: ...


@dataclass(slots=True)
class ConnectionSpec:
    """Everything needed to open one session, resolved from host + credential."""

    hostname: str
    username: str
    port: int = 22
    password: str | None = None
    key_path: str | None = None
    key_passphrase: str | None = None
    allow_agent: bool = True
    strict_host_keys: bool = False
    connect_timeout: float = CONNECT_TIMEOUT
    keepalive: int = KEEPALIVE_SECONDS
    term: str = DEFAULT_TERM
    #: Raw ``-o`` options, only meaningful for the system-ssh transport.
    ssh_options: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def target(self) -> str:
        return f"{self.username}@{self.hostname}"


#: ssh -o options we can honour natively. Anything else only applies to the
#: system-ssh transport, and the UI says so rather than silently ignoring it.
TRANSLATED_OPTIONS = {
    "connecttimeout",
    "serveraliveinterval",
    "stricthostkeychecking",
    "preferredauthentications",
    "pubkeyauthentication",
    "passwordauthentication",
    "identitiesonly",
}


def build_spec(host: Host, credential: Credential | None) -> ConnectionSpec:
    """Resolve a host and its credential into a connection spec."""
    username = host.username
    if credential is not None and credential.username:
        username = credential.username
    if not username:
        raise TransportError(f"Host {host.name!r} has no username.")
    if not host.hostname:
        raise TransportError(f"Host {host.name!r} has no hostname.")

    spec = ConnectionSpec(
        hostname=host.hostname,
        username=username,
        port=host.port,
        ssh_options=list(host.ssh_options or []),
    )

    if credential is not None:
        if credential.kind == "key":
            if not credential.key_path:
                raise TransportError(f"Credential {credential.name!r} has no key path.")
            key_path = Path(credential.key_path).expanduser()
            if not key_path.exists():
                spec.notes.append(f"Key file not found: {key_path}")
            spec.key_path = str(key_path)
            spec.key_passphrase = credential.key_passphrase or None
            spec.allow_agent = False
        else:
            if not credential.password:
                raise TransportError(
                    f"Credential {credential.name!r} has no password stored."
                )
            spec.password = credential.password
            spec.allow_agent = False

    for option in spec.ssh_options:
        name, _, value = option.partition("=")
        key = name.strip().lower()
        value = value.strip()
        if key == "connecttimeout":
            try:
                spec.connect_timeout = float(value)
            except ValueError:
                pass
        elif key == "serveraliveinterval":
            try:
                spec.keepalive = int(value)
            except ValueError:
                pass
        elif key == "stricthostkeychecking":
            spec.strict_host_keys = value.lower() in ("yes", "true")
        elif key not in TRANSLATED_OPTIONS:
            spec.notes.append(f"ssh option {option!r} applies only to system ssh")

    return spec


def known_hosts_path() -> Path:
    return config.home() / "known_hosts"


class ParamikoTransport:
    """In-process SSH. The default, and the reason nothing has to be installed."""

    def __init__(self, spec: ConnectionSpec) -> None:
        self.spec = spec
        self._client = None
        self._channel = None
        self._exit_status: int | None = None

    def connect(self, cols: int, rows: int) -> None:
        import paramiko

        client = paramiko.SSHClient()

        # Trust what the user already trusts, read-only...
        system_known = Path.home() / ".ssh" / "known_hosts"
        if system_known.exists():
            try:
                client.load_system_host_keys(str(system_known))
            except Exception:
                pass

        # ...then our own store, which is the one new keys get written back to.
        own_known = known_hosts_path()
        try:
            own_known.parent.mkdir(parents=True, exist_ok=True)
            own_known.touch(mode=0o600, exist_ok=True)
            client.load_host_keys(str(own_known))
        except Exception:
            pass

        if self.spec.strict_host_keys:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            # Equivalent of StrictHostKeyChecking=accept-new: learn unknown
            # hosts, but a *changed* key still raises.
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        pkey = None
        if self.spec.key_path:
            pkey = self._load_key(self.spec.key_path, self.spec.key_passphrase)

        try:
            client.connect(
                hostname=self.spec.hostname,
                port=self.spec.port,
                username=self.spec.username,
                password=self.spec.password,
                pkey=pkey,
                key_filename=None,
                timeout=self.spec.connect_timeout,
                allow_agent=self.spec.allow_agent,
                look_for_keys=self.spec.allow_agent,
                auth_timeout=self.spec.connect_timeout,
            )
        except Exception as exc:  # paramiko raises a wide family here
            raise self._translate(exc) from exc

        try:
            transport = client.get_transport()
            if transport is not None and self.spec.keepalive:
                transport.set_keepalive(self.spec.keepalive)
            channel = client.invoke_shell(
                term=self.spec.term, width=cols, height=rows
            )
            channel.settimeout(0.0)
        except Exception as exc:
            client.close()
            raise TransportError(f"Could not open a shell: {exc}") from exc

        # Persist newly learned host keys so the next connection is verified.
        try:
            known = known_hosts_path()
            known.parent.mkdir(parents=True, exist_ok=True)
            client.save_host_keys(str(known))
            os.chmod(known, 0o600)
        except OSError:
            pass

        self._client = client
        self._channel = channel

    @staticmethod
    def _load_key(path: str, passphrase: str | None):
        import paramiko

        errors: list[str] = []
        # Key type is not knowable from the file name, so try each loader.
        # Resolved by name because paramiko drops key types over time - 5.0
        # removed DSSKey - and a missing one must not break the others.
        loaders = [
            getattr(paramiko, name, None)
            for name in ("Ed25519Key", "ECDSAKey", "RSAKey", "DSSKey")
        ]
        for loader in [candidate for candidate in loaders if candidate is not None]:
            try:
                return loader.from_private_key_file(path, password=passphrase)
            except paramiko.PasswordRequiredException as exc:
                raise AuthFailed(
                    f"Key {path} needs a passphrase; add one to the credential."
                ) from exc
            except paramiko.SSHException as exc:
                errors.append(f"{loader.__name__}: {exc}")
            except OSError as exc:
                raise TransportError(f"Cannot read key {path}: {exc}") from exc
        raise TransportError(f"Unsupported or corrupt key {path} ({'; '.join(errors)})")

    @staticmethod
    def _translate(exc: Exception) -> TransportError:
        import paramiko
        import socket

        if isinstance(exc, paramiko.AuthenticationException):
            return AuthFailed("Authentication failed - check the credential.")
        if isinstance(exc, paramiko.BadHostKeyException):
            return HostKeyRejected(
                "Host key does not match the one on record. If this is expected, "
                f"remove the entry from {known_hosts_path()}."
            )
        if isinstance(exc, socket.timeout):
            return TransportError("Connection timed out.")
        if isinstance(exc, socket.gaierror):
            return TransportError("Host could not be resolved.")
        if isinstance(exc, ConnectionRefusedError):
            return TransportError("Connection refused.")
        if isinstance(exc, OSError) and exc.errno is not None:
            return TransportError(f"Network error: {exc.strerror or exc}")
        return TransportError(str(exc) or exc.__class__.__name__)

    def read(self, size: int = 65536) -> bytes:
        channel = self._channel
        if channel is None:
            return b""
        import socket

        try:
            if channel.recv_ready():
                return channel.recv(size)
            if channel.exit_status_ready() and not channel.recv_ready():
                self._exit_status = channel.recv_exit_status()
                return b""
        except socket.timeout:
            return b""
        except Exception:
            return b""
        return b""

    def write(self, data: bytes) -> None:
        channel = self._channel
        if channel is not None and not channel.closed:
            try:
                channel.sendall(data)
            except Exception:
                pass

    def resize(self, cols: int, rows: int) -> None:
        channel = self._channel
        if channel is not None and not channel.closed:
            try:
                channel.resize_pty(width=cols, height=rows)
            except Exception:
                pass

    def close(self) -> None:
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:
                pass
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass

    @property
    def closed(self) -> bool:
        channel = self._channel
        if channel is None:
            return True
        return channel.closed or channel.eof_received

    @property
    def exit_status(self) -> int | None:
        return self._exit_status


class SystemSSHTransport:
    """Runs the host's ``ssh`` in a local PTY.

    Opt-in per host, for setups Paramiko cannot express. Passwords are not
    supported here - that is exactly the dependency we removed - so this path is
    for key, agent or externally-managed auth.
    """

    def __init__(self, spec: ConnectionSpec, ssh_binary: str = "ssh") -> None:
        self.spec = spec
        self.ssh_binary = ssh_binary
        self._pid: int | None = None
        self._fd: int | None = None
        self._exit_status: int | None = None

    def argv(self) -> list[str]:
        argv = [self.ssh_binary, "-p", str(self.spec.port), "-tt"]
        options = list(self.spec.ssh_options)
        if not any(o.lower().startswith("stricthostkeychecking") for o in options):
            options.append("StrictHostKeyChecking=accept-new")
        if self.spec.keepalive and not any(
            o.lower().startswith("serveraliveinterval") for o in options
        ):
            options.append(f"ServerAliveInterval={self.spec.keepalive}")
        for option in options:
            argv += ["-o", option]
        if self.spec.key_path:
            argv += ["-i", self.spec.key_path, "-o", "IdentitiesOnly=yes"]
        argv.append(self.spec.target)
        return argv

    @property
    def command(self) -> str:
        return shlex.join(self.argv())

    def connect(self, cols: int, rows: int) -> None:
        # Report the misconfiguration before the missing binary: when both are
        # wrong, the combination is the actionable problem, not the absence of
        # a dependency the user was trying to avoid in the first place.
        if self.spec.password:
            raise TransportError(
                "System ssh cannot be given a stored password. Use the built-in "
                "client, or switch this host to key or agent authentication."
            )
        if shutil.which(self.ssh_binary) is None:
            raise TransportError(
                f"'{self.ssh_binary}' is not installed. Turn off 'use system ssh' "
                "for this host to use the built-in SSH client instead."
            )

        import pty
        import fcntl
        import struct
        import termios

        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                env = dict(os.environ)
                env["TERM"] = self.spec.term
                os.execvpe(self.ssh_binary, self.argv(), env)
            except Exception:
                os._exit(127)

        # parent
        try:
            fcntl.ioctl(
                fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
            )
        except OSError:
            pass
        os.set_blocking(fd, False)
        self._pid = pid
        self._fd = fd

    def read(self, size: int = 65536) -> bytes:
        if self._fd is None:
            return b""
        try:
            return os.read(self._fd, size)
        except BlockingIOError:
            return b""
        except OSError:
            self._reap()
            return b""

    def write(self, data: bytes) -> None:
        if self._fd is None:
            return
        try:
            os.write(self._fd, data)
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        if self._fd is None:
            return
        import fcntl
        import struct
        import termios

        try:
            fcntl.ioctl(
                self._fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
            )
        except OSError:
            pass

    def _reap(self) -> None:
        if self._pid is None:
            return
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
            if pid:
                self._exit_status = os.waitstatus_to_exitcode(status)
                self._pid = None
        except ChildProcessError:
            self._pid = None
        except OSError:
            pass

    def close(self) -> None:
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGHUP)
            except OSError:
                pass
            self._reap()
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    @property
    def closed(self) -> bool:
        if self._fd is None:
            return True
        self._reap()
        return self._pid is None

    @property
    def exit_status(self) -> int | None:
        return self._exit_status


def create_transport(host: Host, credential: Credential | None) -> Transport:
    """Pick a transport for a host, honouring its ``use_system_ssh`` flag."""
    spec = build_spec(host, credential)
    if getattr(host, "use_system_ssh", False):
        return SystemSSHTransport(spec)
    return ParamikoTransport(spec)


def system_ssh_available() -> bool:
    return shutil.which("ssh") is not None
