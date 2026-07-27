"""Filesystem layout for Remotely.

Everything lives under ``~/.remotely``. The location can be redirected with
``REMOTELY_HOME`` which exists mainly so tests never touch a real user's data.

    ~/.remotely/
    |-- hosts.json       host definitions (never contains secrets)
    |-- vault.enc        encrypted credential vault
    |-- settings.json    UI preferences
    |-- themes/          user themes, shadowing the bundled ones by name
    `-- run/             transient askpass helpers, mode 0700
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "REMOTELY_HOME"
DEFAULT_HOME = "~/.remotely"


def home() -> Path:
    """Root config directory, honouring ``REMOTELY_HOME``."""
    raw = os.environ.get(ENV_HOME) or DEFAULT_HOME
    return Path(raw).expanduser()


def hosts_file() -> Path:
    return home() / "hosts.json"


def vault_file() -> Path:
    return home() / "vault.enc"


def settings_file() -> Path:
    return home() / "settings.json"


def themes_dir() -> Path:
    return home() / "themes"


def run_dir() -> Path:
    """Directory for short-lived askpass helpers. Always mode 0700."""
    return home() / "run"


def ensure_layout() -> Path:
    """Create the directory tree if missing and return the root."""
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    themes_dir().mkdir(parents=True, exist_ok=True)
    rd = run_dir()
    rd.mkdir(parents=True, exist_ok=True)
    # The run directory holds plaintext secrets for a few hundred milliseconds
    # at a time, so tighten it even if the umask was permissive.
    os.chmod(rd, 0o700)
    return root
