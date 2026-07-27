"""SSH command construction and secret staging.

Secrets never appear on the command line. They are staged in a one-shot 0600
file and collected by :mod:`remotely.askpass`, which OpenSSH invokes through
``SSH_ASKPASS``. ``SSH_ASKPASS_REQUIRE=force`` is what makes this work while a
TTY is attached - without it ssh ignores the helper and prompts directly.
"""

from __future__ import annotations

import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .models import Credential, DEFAULT_SSH_OPTIONS, Host

ASKPASS_PREFIX = "askpass-"
#: Staged secrets are consumed within a second or two; anything older is debris
#: from a crash and gets swept on the next launch.
STALE_SECONDS = 300


class SSHError(RuntimeError):
    """Raised when a session cannot be prepared."""


@dataclass(slots=True)
class LaunchPlan:
    """Everything needed to start one session, ready for the tmux backend."""

    argv: list[str]
    env: dict[str, str]
    secret_file: Path | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def command(self) -> str:
        """The argv as a single shell-safe string."""
        return shlex.join(self.argv)


def sweep_stale_secrets(now: float | None = None) -> int:
    """Delete abandoned askpass files. Returns how many were removed."""
    run_dir = config.run_dir()
    if not run_dir.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - STALE_SECONDS
    removed = 0
    for path in run_dir.glob(f"{ASKPASS_PREFIX}*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def stage_secret(secret: str) -> Path:
    """Write ``secret`` to a fresh 0600 file and return its path."""
    config.ensure_layout()
    sweep_stale_secrets()
    name = f"{ASKPASS_PREFIX}{int(time.time())}-{secrets.token_hex(8)}"
    path = config.run_dir() / name
    # Create with the right mode from the start; never widen then narrow.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secret)
    return path


def find_askpass_helper() -> str | None:
    """Locate the ``remotely-askpass`` executable.

    Installed entry point first; when running from a source checkout there is no
    console script, so fall back to invoking the module through the current
    interpreter via a tiny generated shim.
    """
    found = shutil.which("remotely-askpass")
    if found:
        return found

    shim = config.run_dir() / "askpass-shim.sh"
    try:
        config.ensure_layout()
        body = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m remotely.askpass "$@"\n'
        if not shim.exists() or shim.read_text(encoding="utf-8") != body:
            shim.write_text(body, encoding="utf-8")
        os.chmod(shim, 0o700)
        return str(shim)
    except OSError:
        return None


def _option_key(option: str) -> str:
    """``PreferredAuthentications=password`` -> ``preferredauthentications``."""
    return option.split("=", 1)[0].strip().lower()


def merge_options(base: list[str], overrides: list[str]) -> list[str]:
    """Merge ssh ``-o`` options, with ``overrides`` winning per option key."""
    merged: dict[str, str] = {}
    for option in [*base, *overrides]:
        merged[_option_key(option)] = option
    return list(merged.values())


def auth_options(credential: Credential | None) -> list[str]:
    """Options implied by the chosen authentication method."""
    if credential is None:
        return []
    if credential.kind == "password":
        # Stop ssh burning its attempt budget on agent keys the server will
        # reject before it ever gets to the password we actually have.
        return [
            "PreferredAuthentications=password,keyboard-interactive",
            "PubkeyAuthentication=no",
        ]
    return [
        "PreferredAuthentications=publickey",
        "IdentitiesOnly=yes",
    ]


def resolve_ssh_options(host: Host, credential: Credential | None) -> list[str]:
    """Final ``-o`` list for a host.

    ``host.ssh_options is None`` means "not configured" and takes the defaults;
    an empty list explicitly means "no extra options" and is honoured as such.
    Auth-derived options are applied underneath, so an explicit host option of
    the same key always wins.
    """
    configured = DEFAULT_SSH_OPTIONS if host.ssh_options is None else host.ssh_options
    return merge_options(auth_options(credential), list(configured))


def build_plan(
    host: Host,
    credential: Credential | None = None,
    *,
    ssh_binary: str = "ssh",
) -> LaunchPlan:
    """Assemble the argv and environment for one SSH session."""
    if not host.hostname:
        raise SSHError(f"Host {host.name!r} has no hostname.")

    username = host.username
    if credential is not None and credential.username:
        username = credential.username
    if not username:
        raise SSHError(f"Host {host.name!r} has no username.")

    argv: list[str] = [ssh_binary, "-p", str(host.port)]
    for option in resolve_ssh_options(host, credential):
        argv += ["-o", option]

    notes: list[str] = []
    secret: str | None = None

    if credential is not None:
        if credential.kind == "key":
            if not credential.key_path:
                raise SSHError(f"Credential {credential.name!r} has no key path.")
            key_path = Path(credential.key_path).expanduser()
            if not key_path.exists():
                notes.append(f"Key file not found: {key_path}")
            argv += ["-i", str(key_path)]
            secret = credential.key_passphrase or None
        else:
            secret = credential.password or None
            if not secret:
                raise SSHError(f"Credential {credential.name!r} has no password stored.")

    argv.append(f"{username}@{host.hostname}")

    env: dict[str, str] = {}
    secret_file: Path | None = None
    if secret:
        helper = find_askpass_helper()
        if helper is None:
            raise SSHError(
                "Could not locate the remotely-askpass helper, so the stored "
                "secret cannot be delivered. ssh will prompt instead."
            )
        secret_file = stage_secret(secret)
        env["SSH_ASKPASS"] = helper
        env["REMOTELY_ASKPASS_FILE"] = str(secret_file)
        # Required for OpenSSH >= 8.4 to consult the helper while a TTY exists.
        env["SSH_ASKPASS_REQUIRE"] = "force"
        # Older OpenSSH only reaches for the helper when DISPLAY is set; a dummy
        # value is harmless because the helper never opens a window.
        env.setdefault("DISPLAY", os.environ.get("DISPLAY", ":0"))

    return LaunchPlan(argv=argv, env=env, secret_file=secret_file, notes=notes)


def openssh_version() -> tuple[int, int] | None:
    """Return the local OpenSSH version, or ``None`` if it cannot be read."""
    try:
        result = subprocess.run(
            ["ssh", "-V"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # ssh -V writes to stderr, e.g. "OpenSSH_9.6p1, OpenSSL 3.0.13".
    blob = (result.stderr or result.stdout or "").strip()
    if not blob.startswith("OpenSSH_"):
        return None
    token = blob[len("OpenSSH_") :].split(",", 1)[0]
    digits = ""
    for char in token:
        if char.isdigit() or char == ".":
            digits += char
        else:
            break
    parts = digits.split(".")
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None


def askpass_force_supported() -> bool:
    """Whether ``SSH_ASKPASS_REQUIRE=force`` is available (OpenSSH >= 8.4)."""
    version = openssh_version()
    if version is None:
        return True  # Assume modern; the failure mode is a manual prompt.
    return version >= (8, 4)
