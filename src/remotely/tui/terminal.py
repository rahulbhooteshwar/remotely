"""The terminal widget.

Draws a :class:`~remotely.sessions.Session` and forwards keystrokes to it.

Rendering goes through ``render_line`` rather than ``render`` so Textual only
repaints the rows that changed, and incoming bytes are drained on a timer
instead of on arrival - a chatty remote command would otherwise trigger a
repaint per packet.
"""

from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from ..sessions import Session
from ..terminal import Cell, encode_key

#: ~30fps. Fast enough to feel immediate, slow enough that a flood of output
#: costs a bounded number of repaints.
DRAIN_INTERVAL = 1 / 30

#: Paging steps per wheel notch. pyte pages by a fraction of the screen, so one
#: step already moves a few lines.
WHEEL_STEPS = 1
PAGE_STEPS = 8


class TerminalPane(Widget, can_focus=True):
    """A live terminal for one session."""

    # Textual's selection machinery needs this plus get_selection() below;
    # without both, a session's output cannot be selected or copied at all.
    ALLOW_SELECT = True

    DEFAULT_CSS = """
    TerminalPane {
        height: 100%;
        width: 100%;
        overflow: hidden;
    }
    """

    class Closed(Message):
        """The session ended."""

        def __init__(self, session: Session) -> None:
            super().__init__()
            self.session = session

    class TitleChanged(Message):
        """The remote set a new terminal title."""

        def __init__(self, session: Session, title: str) -> None:
            super().__init__()
            self.session = session
            self.title = title

    def __init__(self, session: Session, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session = session
        self._last_title = ""
        self._announced_close = False
        self._last_status = ""
        self._spinner = 0

    # ------------------------------------------------------------------ setup

    def on_mount(self) -> None:
        self.set_interval(DRAIN_INTERVAL, self._drain)
        self.focus()

    def on_show(self) -> None:
        # Re-sync on tab switch: the pane may have been resized while hidden.
        self._sync_size()
        self.focus()

    # ----------------------------------------------------------------- pumping

    def _drain(self) -> None:
        session = self.session
        data = session.drain()
        changed = False

        if data:
            session.emulator.feed(data)
            changed = True

        # While connecting, keep repainting so the spinner turns and the user
        # can see something is actually happening.
        if session.status == "connecting":
            self._spinner += 1
            changed = True

        if session.status != self._last_status:
            self._last_status = session.status
            changed = True

        title = session.emulator.title
        if title and title != self._last_title:
            self._last_title = title
            self.post_message(self.TitleChanged(session, title))

        if not session.is_live and not self._announced_close:
            self._announced_close = True
            self.post_message(self.Closed(session))
            changed = True

        if changed:
            self.refresh()

    # ----------------------------------------------------------------- sizing

    def _sync_size(self) -> None:
        cols = max(self.size.width, 2)
        rows = max(self.size.height, 2)
        emulator = self.session.emulator
        if (cols, rows) != (emulator.cols, emulator.rows):
            self.session.resize(cols, rows)
            self.refresh()

    def on_resize(self, event: events.Resize) -> None:
        self._sync_size()

    # -------------------------------------------------------------- scrollback

    def _scroll(self, steps: int) -> None:
        emulator = self.session.emulator
        moved = emulator.scroll_up(steps) if steps > 0 else emulator.scroll_down(-steps)
        if moved:
            self.refresh()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        self._scroll(WHEEL_STEPS)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        self._scroll(-WHEEL_STEPS)

    # ---------------------------------------------------------------- keyboard

    async def _on_key(self, event: events.Key) -> None:
        session = self.session

        # Scrollback is shift-prefixed so plain page keys still reach the
        # remote, where a pager or editor expects them.
        if event.key == "shift+pageup":
            event.prevent_default(); event.stop(); self._scroll(PAGE_STEPS); return
        if event.key == "shift+pagedown":
            event.prevent_default(); event.stop(); self._scroll(-PAGE_STEPS); return
        if event.key == "shift+end":
            event.prevent_default(); event.stop()
            session.emulator.scroll_to_live(); self.refresh(); return

        if not session.is_live:
            return

        # Leave the app's own escape hatches alone so the user is never trapped
        # inside a session with no way back to the launcher.
        if event.key in RESERVED_KEYS:
            return

        data = encode_key(event.key, event.character)
        if data is None:
            return
        event.prevent_default()
        event.stop()

        # Typing means you want to be where the action is.
        if not session.emulator.at_live_edge:
            session.emulator.scroll_to_live()
        session.write(data)

    # --------------------------------------------------------------- selection

    def get_selection(self, selection):
        """Text under a drag-selection, so it can be copied.

        The default implementation reads ``_render()``, which returns nothing
        useful for a widget that paints via ``render_line``. Rebuilding the
        visible screen as plain text gives Textual something to slice.
        """
        try:
            text = "\n".join(self.session.emulator.display())
        except Exception:
            return None
        return selection.extract(text), "\n"

    # ---------------------------------------------------------------- rendering

    def render_line(self, y: int) -> Strip:
        session = self.session

        # A blank pane while connecting or after a failure tells the user
        # nothing; show the state where they are already looking.
        if session.status in ("connecting", "error"):
            return self._render_notice(y)

        emulator = session.emulator
        if y >= emulator.rows:
            return Strip.blank(self.size.width)

        cells = emulator.line(y)
        cursor_x, cursor_y = emulator.cursor
        # A cursor drawn over scrolled-back history is misleading.
        show_cursor = self.has_focus and cursor_y == y and emulator.at_live_edge

        segments: list[Segment] = []
        run: list[str] = []
        run_style: Style | None = None

        for x, cell in enumerate(cells):
            style = _style_for(cell)
            if show_cursor and x == cursor_x:
                style = style + Style(reverse=True)
            if run_style is not None and style == run_style:
                run.append(cell.char)
                continue
            if run:
                segments.append(Segment("".join(run), run_style))
            run = [cell.char]
            run_style = style

        if run:
            segments.append(Segment("".join(run), run_style))

        return Strip(segments, emulator.cols).adjust_cell_length(self.size.width)

    def notice_lines(self) -> list[tuple[str, Style]]:
        """The connecting / failed message, as (text, style) rows."""
        session = self.session
        accent = Style(color=session.theme.accent, bold=True)
        dim = Style(color="#8b98b0")
        bad = Style(color="#ff7b72", bold=True)

        if session.status == "connecting":
            frame = "|/-\\"[self._spinner // 6 % 4]
            return [
                (f"{frame}  Connecting to {session.host_name}", accent),
                ("", dim),
                (session.target_hint, dim),
                ("", dim),
                ("ctrl+w returns to the launcher", dim),
            ]

        lines: list[tuple[str, Style]] = [
            (f"Could not connect to {session.host_name}", bad),
            ("", dim),
            (session.error or "Unknown error.", Style(color="#e6edf3")),
            ("", dim),
            (session.target_hint, dim),
        ]
        for hint in session.failure_hints():
            lines.append((hint, dim))
        lines += [("", dim), ("ctrl+w launcher     ctrl+e edit host     ctrl+k vault", dim)]
        return lines

    def _render_notice(self, y: int) -> Strip:
        lines = self.notice_lines()
        width = self.size.width
        top = max(0, (self.size.height - len(lines)) // 2)
        index = y - top
        if index < 0 or index >= len(lines):
            return Strip.blank(width)
        text, style = lines[index]
        if not text:
            return Strip.blank(width)
        pad = max(0, (width - len(text)) // 2)
        return Strip([Segment(" " * pad), Segment(text, style)]).adjust_cell_length(width)


#: Keys the terminal never swallows, so the launcher is always reachable.
RESERVED_KEYS = frozenset(
    {
        "ctrl+w",  # back to the launcher
        "ctrl+q",  # quit
        "f1",  # help
        "ctrl+pageup",
        "ctrl+pagedown",
    }
)

_STYLE_CACHE: dict[tuple, Style] = {}


def _style_for(cell: Cell) -> Style:
    """Cached Rich style for a cell.

    A full screen is a few thousand cells redrawn many times a second, but only
    a handful of distinct styles, so caching turns most of the work into a dict
    lookup.
    """
    key = (cell.fg, cell.bg, cell.bold, cell.italic, cell.underline, cell.reverse)
    style = _STYLE_CACHE.get(key)
    if style is None:
        style = Style(
            color=cell.fg,
            bgcolor=cell.bg,
            bold=cell.bold,
            italic=cell.italic,
            underline=cell.underline,
            reverse=cell.reverse,
        )
        _STYLE_CACHE[key] = style
    return style
