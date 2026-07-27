"""A real SSH server for tests.

Paramiko can act as a server, so the client path gets exercised against an
actual SSH handshake rather than a mock: real key exchange, real
authentication, a real channel carrying real bytes. That is the only way to
have any confidence in a bundled SSH client.

The shell is deliberately tiny - it echoes what you type and answers a couple
of commands - because the point is the transport, not the shell.
"""

from __future__ import annotations

import socket
import threading
import time

import paramiko

USERNAME = "tester"
PASSWORD = "s3cret-pass"
BANNER = "Welcome to the test server\r\n"
PROMPT = "$ "


class _Server(paramiko.ServerInterface):
    def __init__(self, password: str | None, public_key: paramiko.PKey | None) -> None:
        self.password = password
        self.public_key = public_key
        self.shell_requested = threading.Event()
        self.pty_size: tuple[int, int] | None = None

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def get_allowed_auths(self, username: str) -> str:
        allowed = []
        if self.password is not None:
            allowed.append("password")
        if self.public_key is not None:
            allowed.append("publickey")
        return ",".join(allowed) or "none"

    def check_auth_password(self, username: str, password: str) -> int:
        if self.password is not None and username == USERNAME and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        if self.public_key is not None and key == self.public_key:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_shell_request(self, channel) -> bool:
        self.shell_requested.set()
        return True

    def check_channel_pty_request(
        self, channel, term, width, height, pixelwidth, pixelheight, modes
    ) -> bool:
        self.pty_size = (width, height)
        return True

    def check_channel_window_change_request(
        self, channel, width, height, pixelwidth, pixelheight
    ) -> bool:
        self.pty_size = (width, height)
        return True


class SSHTestServer:
    """A one-connection-at-a-time SSH server on an ephemeral port."""

    def __init__(
        self,
        *,
        password: str | None = PASSWORD,
        authorized_key: paramiko.PKey | None = None,
        host_key: paramiko.PKey | None = None,
    ) -> None:
        self.password = password
        self.authorized_key = authorized_key
        self.host_key = host_key or paramiko.RSAKey.generate(2048)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(5)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.server: _Server | None = None
        self.received = bytearray()

    @property
    def hostname(self) -> str:
        return "127.0.0.1"

    def start(self) -> "SSHTestServer":
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                self._socket.settimeout(0.5)
                client, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._session, args=(client,), daemon=True).start()

    def _session(self, client: socket.socket) -> None:
        transport = paramiko.Transport(client)
        transport.add_server_key(self.host_key)
        server = _Server(self.password, self.authorized_key)
        self.server = server
        try:
            transport.start_server(server=server)
        except Exception:
            return

        channel = transport.accept(10)
        if channel is None:
            return
        if not server.shell_requested.wait(10):
            channel.close()
            return

        channel.send(BANNER)
        channel.send(PROMPT)
        line = bytearray()
        try:
            while not self._stop.is_set() and not channel.closed:
                data = channel.recv(1024)
                if not data:
                    break
                self.received.extend(data)
                for byte in data:
                    char = bytes([byte])
                    if char in (b"\r", b"\n"):
                        channel.send("\r\n")
                        self._run(channel, bytes(line).decode("utf-8", "replace"))
                        line.clear()
                        channel.send(PROMPT)
                    elif char == b"\x03":  # ctrl+c
                        channel.send("^C\r\n" + PROMPT)
                        line.clear()
                    elif char == b"\x04":  # ctrl+d
                        channel.send("logout\r\n")
                        channel.send_exit_status(0)
                        channel.close()
                        return
                    elif char == b"\x7f":  # backspace
                        if line:
                            line.pop()
                            channel.send("\b \b")
                    else:
                        line.extend(char)
                        channel.send(char)
        except Exception:
            pass
        finally:
            try:
                channel.close()
            except Exception:
                pass

    @staticmethod
    def _run(channel, command: str) -> None:
        command = command.strip()
        if command == "whoami":
            channel.send(f"{USERNAME}\r\n")
        elif command == "colour":
            channel.send("\x1b[31mRED\x1b[0m \x1b[1;32mBOLDGREEN\x1b[0m\r\n")
        elif command == "size":
            channel.send("SIZE\r\n")
        elif command == "exit":
            channel.send_exit_status(0)
            channel.close()
        elif command:
            channel.send(f"you said: {command}\r\n")

    def stop(self) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self) -> "SSHTestServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    """Poll until ``predicate`` is true. Returns whether it became true."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False
