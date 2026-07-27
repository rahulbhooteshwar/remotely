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


class TerminalPane(Widget, can_focus=True):
    """A live terminal for one session."""

    DEFAULT_CSS = """
    TerminalPane {
        height: 1fr;
        width: 1fr;
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

    # ---------------------------------------------------------------- keyboard

    async def _on_key(self, event: events.Key) -> None:
        session = self.session
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
        session.write(data)

    # ---------------------------------------------------------------- rendering

    def render_line(self, y: int) -> Strip:
        emulator = self.session.emulator
        if y >= emulator.rows:
            return Strip.blank(self.size.width)

        cells = emulator.line(y)
        cursor_x, cursor_y = emulator.cursor
        show_cursor = self.has_focus and cursor_y == y

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
