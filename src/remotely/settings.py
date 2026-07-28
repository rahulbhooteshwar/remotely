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
    #: Textual's application theme, chosen from the command palette. Empty
    #: means "whatever Textual defaults to". Not a toggle, so it is absent
    #: from LABELS and does not appear in the /settings list.
    app_theme: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Settings":
        """Build from stored JSON, coercing each field to its declared type.

        Per-field rather than blanket ``bool()``: the settings file is not all
        switches any more, and coercing a theme name to a bool would silently
        throw it away.
        """
        values: dict[str, Any] = {}
        for field in fields(cls):
            if field.name not in raw:
                continue
            value = raw[field.name]
            if field.type in (bool, "bool"):
                values[field.name] = bool(value)
            elif field.type in (str, "str"):
                values[field.name] = str(value) if value is not None else ""
            else:
                values[field.name] = value
        return cls(**values)

    def toggle(self, name: str) -> bool:
        """Flip a boolean preference and return its new value."""
        if not hasattr(self, name) or not isinstance(getattr(self, name), bool):
            raise KeyError(name)
        new = not getattr(self, name)
        setattr(self, name, new)
        return new


#: Order and wording used by the ``/settings`` list. Booleans only - anything
#: set elsewhere (the app theme comes from the command palette) stays out.
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
