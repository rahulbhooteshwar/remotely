"""User preferences.

Kept separate from ``hosts.json`` so a corrupt or unrecognised preference can
never take the host list down with it: anything unreadable falls back to
defaults rather than raising.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from . import config
from .store import atomic_write


@dataclass(slots=True)
class Settings:
    """Everything the user can change from ``/settings``."""

    #: Ask before quitting while sessions are still open.
    confirm_quit: bool = True
    #: Ask before closing a tab whose session is still live.
    confirm_close_tab: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Settings":
        known = {f.name: f for f in fields(cls)}
        values: dict[str, Any] = {}
        for name, field in known.items():
            if name in raw:
                values[name] = bool(raw[name])
        return cls(**values)

    def toggle(self, name: str) -> bool:
        """Flip a preference and return its new value."""
        if not hasattr(self, name):
            raise KeyError(name)
        new = not getattr(self, name)
        setattr(self, name, new)
        return new


#: Order and wording used by the ``/settings`` list.
LABELS: dict[str, tuple[str, str]] = {
    "confirm_quit": (
        "Confirm before quitting",
        "Ask for confirmation when quitting with sessions still open.",
    ),
    "confirm_close_tab": (
        "Confirm before closing a tab",
        "Ask for confirmation when closing a tab whose session is still live.",
    ),
}


def load(path: Path | None = None) -> Settings:
    """Read preferences, falling back to defaults on anything unreadable."""
    target = path or config.settings_file()
    if not target.exists():
        return Settings()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    return Settings.from_dict(raw)


def save(settings: Settings, path: Path | None = None) -> None:
    target = path or config.settings_file()
    atomic_write(target, json.dumps(settings.to_dict(), indent=2) + "\n", mode=0o600)
