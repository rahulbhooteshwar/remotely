"""tmux session backend.

Tabs are tmux windows. Remotely runs as window 0 of a session (``remotely`` by
default) and every SSH connection becomes a sibling window, which is what makes
tabs work identically on macOS, Linux, BSD and WSL without a terminal emulator
in the loop.

Windows created by Remotely are tagged with the tmux user option
``@remotely_host``. That tag is the only source of truth for "is this one of
ours", so a window the user opened by hand is never touched.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any, Iterable

from .models import Host
from .ssh import LaunchPlan
from .themes import Theme

DEFAULT_SESSION = "remotely"
HOST_TAG = "@remotely_host"
THEME_TAG = "@remotely_theme"
UI_TAG = "@remotely_ui"


class TmuxError(RuntimeError):
    """Raised when tmux is missing or refuses a command."""


@dataclass(slots=True)
class SessionTab:
    """An open SSH tab."""

    window_id: str
    index: str
    name: str
    host: str
    theme: str
    active: bool = False


def hold_on_failure(command: str) -> str:
    """Wrap a command so a failed session stays visible instead of vanishing.

    This is ``remain-on-exit failed`` expressed portably. The tmux option
    itself is a *window* option, so it cannot be set on the session and
    inherited, and setting it globally would change the user's whole server -
    unacceptable when we are a guest in their session. Worse, a command that
    dies instantly (ssh missing, DNS failure) takes the window with it before
    the window id can even be read back, turning a useful error into an
    internal one.

    Blocking on ``read`` keeps the pane alive with the exit code on screen. A
    clean exit still closes the tab, which is what people expect after typing
    ``exit`` on the far end.
    """
    return (
        f"{command}; __rc=$?; "
        'if [ "$__rc" -ne 0 ]; then '
        "printf '\\n\\033[31m[remotely]\\033[0m session ended with exit code %s\\n' \"$__rc\"; "
        "printf '\\033[2mpress enter to close this tab\\033[0m'; "
        "read __ignored; "
        "fi"
    )


def _available_locales() -> set[str]:
    """Locale names this system knows about, normalised for comparison."""
    try:
        result = subprocess.run(
            ["locale", "-a"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return {
        line.strip().lower().replace("-", "")
        for line in result.stdout.splitlines()
        if line.strip()
    }


def ensure_utf8_locale() -> bool:
    """Guarantee a UTF-8 ``LC_CTYPE`` for everything we hand to tmux.

    Not cosmetic. Under a non-UTF-8 locale tmux emits format output that
    libtmux cannot parse, and session creation fails outright with an opaque
    ``zip() argument 2 is shorter than argument 1``. Terminal programs
    routinely correct this for themselves; we do the same, touching only
    ``LC_CTYPE`` so a user's ``LC_MESSAGES``, currency and date settings are
    left alone.

    Returns whether a UTF-8 locale is now in effect.
    """
    if locale_supports_unicode():
        return True
    available = _available_locales()
    for candidate in ("C.UTF-8", "en_US.UTF-8", "UTF-8"):
        if not available or candidate.lower().replace("-", "") in available:
            os.environ["LC_CTYPE"] = candidate
            return True
    return False


def locale_supports_unicode() -> bool:
    """Whether this process's locale can render multibyte characters.

    Window names are stored verbatim by the tmux server; it is the *client*
    locale that decides whether an icon displays or comes out as ``__``. Read
    at call time rather than cached so tests can vary the environment.
    """
    for variable in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = os.environ.get(variable)
        if value:
            lowered = value.lower()
            return "utf-8" in lowered or "utf8" in lowered
    return False


def inside_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def current_session_name() -> str | None:
    """Name of the tmux session we are running inside, if any."""
    if not inside_tmux():
        return None
    try:
        import libtmux

        server = libtmux.Server()
        pane_id = os.environ.get("TMUX_PANE")
        if pane_id:
            for session in server.sessions:
                for window in session.windows:
                    for pane in window.panes:
                        if pane.pane_id == pane_id:
                            return session.session_name
    except Exception:
        return None
    return None


class TmuxBackend:
    """Thin, defensive wrapper over :mod:`libtmux`."""

    def __init__(self, session_name: str = DEFAULT_SESSION, socket_name: str | None = None):
        self.session_name = session_name
        self.socket_name = socket_name
        self._server: Any = None

    # ------------------------------------------------------------------ server

    @property
    def server(self) -> Any:
        if self._server is None:
            # Every tmux interaction funnels through here, so this is the one
            # place that has to guarantee a parseable locale.
            ensure_utf8_locale()
            try:
                import libtmux
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise TmuxError("libtmux is not installed.") from exc
            kwargs = {"socket_name": self.socket_name} if self.socket_name else {}
            self._server = libtmux.Server(**kwargs)
        return self._server

    def is_available(self) -> bool:
        """Whether a tmux binary exists and can be spoken to."""
        try:
            self.server.raise_if_dead()
            return True
        except Exception:
            # A dead server is fine - we will start one. A missing binary is not.
            import shutil

            return shutil.which("tmux") is not None

    # ----------------------------------------------------------------- session

    def session_exists(self) -> bool:
        try:
            return bool(self.server.has_session(self.session_name))
        except Exception:
            return False

    def get_session(self) -> Any:
        for session in self.server.sessions:
            if session.session_name == self.session_name:
                return session
        raise TmuxError(f"tmux session {self.session_name!r} does not exist.")

    def ensure_session(self, ui_command: str, start_directory: str | None = None) -> Any:
        """Return the Remotely session, creating it with the UI in window 0."""
        if self.session_exists():
            return self.get_session()
        try:
            session = self.server.new_session(
                session_name=self.session_name,
                attach=False,
                window_name="remotely",
                window_command=ui_command,
                start_directory=start_directory,
            )
        except Exception as exc:
            raise TmuxError(f"Could not create tmux session: {exc}") from exc

        try:
            window = session.windows[0]
            _set_option(window, UI_TAG, "1")
            # Keep the UI window pinned at index 0 and out of the way of tabs.
            _set_option(window, "automatic-rename", "off")
        except Exception:
            pass
        self._apply_session_chrome(session)
        return session

    def _apply_session_chrome(self, session: Any) -> None:
        """Base status-bar look for the Remotely session."""
        options = {
            "status": "on",
            "status-position": "top",
            "status-justify": "left",
            "status-style": "bg=#11151c,fg=#8b98b0",
            "status-left": "#[bg=#7aa2f7,fg=#11151c,bold] remotely #[default] ",
            "status-left-length": "20",
            "status-right": "#[fg=#5f6b80]%H:%M ",
            "window-status-format": " #I #W ",
            "window-status-current-format": " #I #W ",
            "mouse": "on",
            "base-index": "0",
        }
        for key, value in options.items():
            try:
                session.set_option(key, value)
            except Exception:
                continue

    # ------------------------------------------------------------------- tabs

    def open_tab(
        self,
        host: Host,
        plan: LaunchPlan,
        theme: Theme,
        *,
        session: Any = None,
        select: bool = True,
    ) -> SessionTab:
        """Launch ``plan`` in a new themed tmux window."""
        target = session if session is not None else self.get_session()
        title = theme.tab_title(host.name, unicode_ok=locale_supports_unicode())

        try:
            window = target.new_window(
                window_name=title,
                attach=False,
                window_shell=hold_on_failure(plan.command),
                environment=plan.env or None,
            )
        except Exception as exc:
            raise TmuxError(
                f"Could not open a tab for {host.name!r}: {exc}. "
                f"Check that '{plan.argv[0]}' exists and is executable."
            ) from exc

        _set_option(window, "automatic-rename", "off")
        _set_option(window, HOST_TAG, host.name)
        _set_option(window, THEME_TAG, theme.name)
        for key, value in theme.tmux_window_options().items():
            _set_option(window, key, value)

        if theme.remote.get("inject_prompt"):
            command = theme.remote_prompt_command()
            if command:
                _send_keys_later(window, command)

        if select:
            try:
                window.select()
            except Exception:
                pass

        return SessionTab(
            window_id=str(window.window_id),
            index=str(window.window_index),
            name=title,
            host=host.name,
            theme=theme.name,
            active=select,
        )

    def list_tabs(self, session: Any = None) -> list[SessionTab]:
        """Open Remotely tabs, newest last. Windows we did not create are ignored."""
        try:
            target = session if session is not None else self.get_session()
        except TmuxError:
            return []

        tabs: list[SessionTab] = []
        for window in target.windows:
            host = _get_option(window, HOST_TAG)
            if not host:
                continue
            tabs.append(
                SessionTab(
                    window_id=str(window.window_id),
                    index=str(window.window_index),
                    name=str(window.window_name),
                    host=host,
                    theme=_get_option(window, THEME_TAG) or "",
                    active=bool(getattr(window, "window_active", "0") == "1"),
                )
            )
        tabs.sort(key=lambda t: int(t.index) if t.index.isdigit() else 0)
        return tabs

    def _find_window(self, window_id: str, session: Any = None) -> Any:
        target = session if session is not None else self.get_session()
        for window in target.windows:
            if str(window.window_id) == window_id:
                return window
        raise TmuxError(f"No open tab with id {window_id!r}.")

    def focus_tab(self, window_id: str, session: Any = None) -> None:
        self._find_window(window_id, session).select()

    def close_tab(self, window_id: str, session: Any = None) -> None:
        window = self._find_window(window_id, session)
        try:
            window.kill()
        except Exception as exc:
            raise TmuxError(f"Could not close tab: {exc}") from exc

    def attach_command(self) -> list[str]:
        """argv that attaches the current terminal to the Remotely session."""
        argv = ["tmux"]
        if self.socket_name:
            argv += ["-L", self.socket_name]
        if locale_supports_unicode():
            # -u tells tmux the terminal is UTF-8 capable. Only meaningful when
            # the locale already agrees; forcing it otherwise mangles output.
            argv.append("-u")
        argv += ["attach-session", "-t", self.session_name]
        return argv


def _window_method(window: Any, modern: str, legacy: str) -> Any:
    """Prefer the current libtmux method name, fall back only if it is absent.

    The legacy names still exist but are deprecated, so reaching for them on a
    merely-unset option would emit a warning on every lookup.
    """
    return getattr(window, modern, None) or getattr(window, legacy, None)


def _set_option(window: Any, key: str, value: str) -> bool:
    """Set a tmux window option. Unknown keys fail quietly so a theme file
    naming an option this tmux build lacks cannot break a launch."""
    setter = _window_method(window, "set_option", "set_window_option")
    if setter is None:
        return False
    try:
        setter(key, value)
        return True
    except Exception:
        return False


def _get_option(window: Any, key: str) -> str | None:
    """Read a tmux window option, or ``None`` when it is not set."""
    getter = _window_method(window, "show_option", "show_window_option")
    if getter is None:
        return None
    try:
        value = getter(key)
    except Exception:
        return None
    if isinstance(value, dict):
        value = value.get(key)
    if value in (None, ""):
        return None
    return str(value)


def _send_keys_later(window: Any, command: str) -> None:
    """Best-effort prompt injection once the remote shell is likely up."""
    try:
        pane = window.panes[0]
    except (AttributeError, IndexError):
        return
    try:
        # run-shell -b -d defers without blocking the caller.
        window.cmd(
            "run-shell",
            "-b",
            "-d",
            "2",
            f"tmux send-keys -t {shlex.quote(str(pane.pane_id))} {shlex.quote(command)} Enter",
        )
    except Exception:
        pass


def format_tab_lines(tabs: Iterable[SessionTab]) -> list[str]:
    """Human readable one-liners for the sessions view."""
    return [
        f"{'*' if tab.active else ' '} [{tab.index}] {tab.host}"
        + (f"  ({tab.theme})" if tab.theme else "")
        for tab in tabs
    ]
