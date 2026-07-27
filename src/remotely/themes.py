"""File-based theming for launched sessions.

A theme is a TOML file. Themes are discovered from two places, later shadowing
earlier by ``name``:

1. themes bundled with the package (``prod``, ``non-prod``, ``personal``)
2. ``~/.remotely/themes/*.toml``

Dropping a new ``.toml`` into the user directory is the whole install process -
there is no registry to update. A user file reusing a bundled name overrides it,
so the defaults can be retuned without editing package files.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from . import config

BUNDLED_PACKAGE = "remotely.themes_data"
FALLBACK_THEME = "personal"


class ThemeError(RuntimeError):
    """Raised when a theme file is unreadable or malformed."""


@dataclass(slots=True)
class Theme:
    """Presentation for one session tab.

    ``tmux`` maps onto tmux window options, ``remote`` shapes the prompt inside
    the SSH session, and ``colors`` drives how the tab is rendered in the TUI.
    """

    name: str
    label: str = ""
    description: str = ""
    source: Path | None = None
    builtin: bool = False
    colors: dict[str, str] = field(default_factory=dict)
    tmux: dict[str, str] = field(default_factory=dict)
    tab: dict[str, str] = field(default_factory=dict)
    remote: dict[str, Any] = field(default_factory=dict)

    @property
    def display_label(self) -> str:
        return self.label or self.name

    @property
    def accent(self) -> str:
        return self.colors.get("accent", "#7aa2f7")

    @property
    def icon(self) -> str:
        return self.tab.get("icon", "•")

    @property
    def ascii_icon(self) -> str:
        """Marker used where the locale cannot represent the real icon."""
        return self.tab.get("ascii_icon", "*")

    def tab_title(self, host_name: str, *, unicode_ok: bool = True) -> str:
        """Render the tmux window name for a host under this theme.

        tmux stores the name verbatim, but a client running under a non-UTF-8
        locale renders each byte of a multibyte icon as ``_``, so the tab shows
        up as "__ web-01". Falling back to an ASCII marker keeps it legible on
        such terminals.
        """
        icon = self.icon if unicode_ok else self.ascii_icon
        template = self.tab.get("format", "{icon} {host}")
        try:
            return template.format(icon=icon, host=host_name, theme=self.name)
        except (KeyError, IndexError):
            return f"{icon} {host_name}"

    def tmux_window_options(self) -> dict[str, str]:
        """Theme keys translated to real tmux window options.

        Unknown keys are passed through untouched so a theme file can reach any
        tmux window option without this module needing to know about it.
        """
        mapping = {
            "window_style": "window-style",
            "window_active_style": "window-active-style",
            "pane_border_style": "pane-border-style",
            "pane_active_border_style": "pane-active-border-style",
            "window_status_style": "window-status-style",
            "window_status_current_style": "window-status-current-style",
        }
        options: dict[str, str] = {}
        for key, value in self.tmux.items():
            if not isinstance(value, str):
                continue
            options[mapping.get(key, key.replace("_", "-"))] = value
        return options

    def remote_prompt_command(self) -> str | None:
        """A shell snippet to colour the remote prompt, or ``None``.

        Sent into the session after ssh connects. Best-effort by nature: it
        assumes a POSIX-ish shell and silently does nothing if the remote is
        something exotic.
        """
        colour = self.remote.get("prompt_color")
        if not colour:
            return None
        banner = self.remote.get("banner")
        label = f"[{banner}] " if banner else ""
        # \[..\] wrappers keep readline's column maths correct.
        ps1 = f'\\[\\e[{colour}m\\]{label}\\u@\\h\\[\\e[0m\\]:\\w\\$ '
        return f"export PS1='{ps1}'"

    @classmethod
    def from_dict(
        cls, raw: dict[str, Any], *, name: str, source: Path | None, builtin: bool
    ) -> "Theme":
        def section(key: str) -> dict[str, Any]:
            value = raw.get(key, {})
            return dict(value) if isinstance(value, dict) else {}

        return cls(
            name=str(raw.get("name", name)).strip() or name,
            label=str(raw.get("label", "")).strip(),
            description=str(raw.get("description", "")).strip(),
            source=source,
            builtin=builtin,
            colors={k: str(v) for k, v in section("colors").items()},
            tmux={k: str(v) for k, v in section("tmux").items()},
            tab={k: str(v) for k, v in section("tab").items()},
            remote=section("remote"),
        )


def _load_file(path: Path, *, builtin: bool) -> Theme:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ThemeError(f"Could not read theme {path}: {exc}") from exc
    return Theme.from_dict(raw, name=path.stem, source=path, builtin=builtin)


class ThemeRegistry:
    """Discovers and caches themes from the bundled and user directories."""

    def __init__(self, user_dir: Path | None = None) -> None:
        self.user_dir = user_dir or config.themes_dir()
        self._themes: dict[str, Theme] = {}
        self._errors: list[str] = []
        self.reload()

    def reload(self) -> None:
        themes: dict[str, Theme] = {}
        errors: list[str] = []

        for path in self._bundled_paths():
            try:
                theme = _load_file(path, builtin=True)
                themes[theme.name.lower()] = theme
            except ThemeError as exc:
                errors.append(str(exc))

        if self.user_dir.is_dir():
            for path in sorted(self.user_dir.glob("*.toml")):
                try:
                    theme = _load_file(path, builtin=False)
                    themes[theme.name.lower()] = theme
                except ThemeError as exc:
                    errors.append(str(exc))

        self._themes = themes
        self._errors = errors

    @staticmethod
    def _bundled_paths() -> list[Path]:
        try:
            root = resources.files(BUNDLED_PACKAGE)
        except (ModuleNotFoundError, TypeError):
            return []
        paths: list[Path] = []
        for entry in root.iterdir():
            if entry.name.endswith(".toml"):
                # Bundled themes are plain files on disk in every supported
                # install mode (wheel, editable, uv tool), so a direct Path is
                # safe and keeps the loader uniform.
                paths.append(Path(str(entry)))
        return sorted(paths)

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    def __len__(self) -> int:
        return len(self._themes)

    def names(self) -> list[str]:
        return sorted(self._themes, key=lambda n: (not self._themes[n].builtin, n))

    def list(self) -> list[Theme]:
        return [self._themes[n] for n in self.names()]

    def get(self, name: str | None) -> Theme:
        """Look up a theme, falling back to ``personal`` then to a bare default."""
        if name:
            found = self._themes.get(name.strip().lower())
            if found is not None:
                return found
        fallback = self._themes.get(FALLBACK_THEME)
        if fallback is not None:
            return fallback
        if self._themes:
            return self._themes[self.names()[0]]
        return Theme(name=FALLBACK_THEME, label="Personal")

    def exists(self, name: str) -> bool:
        return name.strip().lower() in self._themes

    def copy_to_user_dir(self, name: str, new_name: str | None = None) -> Path:
        """Clone a theme into the user directory so it can be edited."""
        theme = self.get(name)
        if theme.source is None:
            raise ThemeError(f"Theme {name!r} has no source file to copy.")
        target_name = (new_name or f"{theme.name}-custom").strip()
        self.user_dir.mkdir(parents=True, exist_ok=True)
        target = self.user_dir / f"{target_name}.toml"
        if target.exists():
            raise ThemeError(f"{target} already exists.")
        body = theme.source.read_text(encoding="utf-8")
        # Rewrite the name key so the clone registers under its own identity.
        lines = body.splitlines()
        for index, line in enumerate(lines):
            if line.strip().startswith("name ="):
                lines[index] = f'name = "{target_name}"'
                break
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.reload()
        return target
