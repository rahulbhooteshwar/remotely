"""The terminal widget.

Draws a :class:`~remotely.sessions.Session` and forwards keystrokes to it.

Rendering goes through ``render_line`` rather than ``render`` so Textual only
repaints the rows that changed, and incoming bytes are drained on a timer
instead of on arrival - a chatty remote command would otherwise trigger a
repaint per packet.
"""

from __future__ import annotations

import time

from rich.segment import Segment
from rich.spinner import SPINNERS
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

#: Rich's "dots" spinner, taken from Rich itself so it stays in step with it.
_DOTS = SPINNERS["dots"]
SPINNER_FRAMES: str = _DOTS["frames"]
SPINNER_INTERVAL: float = _DOTS["interval"] / 1000


def spinner_frame(elapsed: float) -> str:
    """The dots frame for a given elapsed time.

    Driven by the clock rather than the repaint counter, so the spin rate stays
    Rich's 80ms regardless of how often the pane happens to redraw.
    """
    if not SPINNER_FRAMES:
        return ""
    index = int(elapsed / SPINNER_INTERVAL) % len(SPINNER_FRAMES)
    return SPINNER_FRAMES[index]


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

    class RetryRequested(Message):
        """The user asked to reconnect this session."""

        def __init__(self, session: Session) -> None:
            super().__init__()
            self.session = session

    def __init__(self, session: Session, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session = session
        self._announced_close = False
        self._last_status = ""
        self._started = time.monotonic()
        # pyte reports "default" for any cell the remote never explicitly
        # coloured, and that resolves to bg=None. Textual does not backfill a
        # None segment background from the widget's CSS background for
        # content lines, so leaving it None means no colour code is emitted
        # at all and the real terminal we are running inside shows its own
        # default through the gap - not the theme's. Resolving it here is
        # what makes the theme actually paint the whole pane.
        pane_styles = session.theme.pane_styles()
        self._bg_default = pane_styles["background"]
        self._fg_default = pane_styles["color"]
        self._blank_style = Style(bgcolor=self._bg_default, color=self._fg_default)
        self._selection_cache: Style | None = None
        #: Rows the retry button occupies, so a click can be mapped back to it.
        self._retry_rows: set[int] = set()

    # ------------------------------------------------------------------ setup

    def on_mount(self) -> None:
        self.set_interval(DRAIN_INTERVAL, self._drain)
        self.focus()

    def on_show(self) -> None:
        # Re-sync on tab switch: the pane may have been resized while hidden.
        self._sync_size()
        self.focus()

    def restart(self) -> None:
        """Re-arm the pane after its session has been reconnected.

        The close announcement fires once per session; without resetting it a
        retried session that dies again would never report the second death.
        """
        self._announced_close = False
        self._last_status = ""
        self._started = time.monotonic()
        self._retry_rows.clear()
        self._sync_size()
        self.refresh()
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
            changed = True

        if session.status != self._last_status:
            self._last_status = session.status
            changed = True

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

    def _on_click(self, event: events.Click) -> None:
        """Clicking the Retry button reconnects, as well as pressing r."""
        if self.is_dead and event.y in self._retry_rows:
            event.prevent_default()
            event.stop()
            self.post_message(self.RetryRequested(self.session))

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
            # Nothing to type at, so the pane's own keys take over.
            if self.is_dead and event.key in ("r", "R", "enter"):
                event.prevent_default()
                event.stop()
                self.post_message(self.RetryRequested(session))
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

    # ------------------------------------------------------------------ paste

    def _on_paste(self, event: events.Paste) -> None:
        """Send pasted text to the remote.

        Without this the pane simply dropped it: Textual turns a bracketed
        paste into a Paste event rather than a run of key presses, so nothing
        in _on_key ever saw it and paste did nothing at all.
        """
        session = self.session
        if not session.is_live or not event.text:
            return
        event.stop()
        event.prevent_default()

        # A terminal submits a line with CR; LF would move down without
        # returning, so a pasted newline has to be translated.
        text = event.text.replace("\r\n", "\r").replace("\n", "\r")
        data = text.encode("utf-8", "replace")

        # Wrap only when the remote asked for it. Bracketing unconditionally
        # would leave the markers as literal junk in anything that has not
        # enabled the mode, and not wrapping when it has costs the protection
        # against a multi-line paste running itself line by line.
        if session.emulator.bracketed_paste:
            data = BRACKET_START + data + BRACKET_END

        if not session.emulator.at_live_edge:
            session.emulator.scroll_to_live()
        session.write(data)

    # --------------------------------------------------------------- selection

    def _selection_style(self) -> Style:
        """Highlight for selected cells: the theme's selection background only.

        Deliberately drops the component style's foreground. Textual's default
        resolves to the same colour as its background (#064273 on #064273), so
        applying both would paint the selection as an unreadable solid block.
        Keeping each cell's own foreground is also what a terminal does.
        """
        if self._selection_cache is None:
            try:
                component = self.screen.get_component_rich_style("screen--selection")
                self._selection_cache = Style(bgcolor=component.bgcolor)
            except Exception:
                self._selection_cache = Style(bgcolor="#264f78")
        return self._selection_cache

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

        # A blank pane while connecting tells the user nothing; show the state
        # where they are already looking.
        if session.status == "connecting":
            return self._render_notice(y)

        emulator = session.emulator
        if y >= emulator.rows:
            return Strip.blank(self.size.width, self._blank_style)

        if self.is_dead:
            overlay = self._overlay_strip(y)
            if overlay is not None:
                return overlay

        return Strip(self._terminal_segments(y), emulator.cols).adjust_cell_length(
            self.size.width, self._blank_style
        )

    def _terminal_segments(self, y: int) -> list[Segment]:
        """One row of the emulator as styled segments."""
        session = self.session
        emulator = session.emulator
        cells = emulator.line(y)
        cursor_x, cursor_y = emulator.cursor
        # A cursor drawn over scrolled-back history is misleading.
        show_cursor = self.has_focus and cursor_y == y and emulator.at_live_edge

        # Textual paints drag-selection inside Visual.to_strips, which only runs
        # for widgets rendering via render()/Visual. Painting via render_line
        # bypasses it entirely, so the screen tracks the selection and
        # get_selection() returns the right text while nothing on screen ever
        # changes - indistinguishable from selection being broken. Highlight it
        # here instead.
        select_from, select_to = -1, -1
        selection = self.text_selection
        if selection is not None:
            span = selection.get_span(y)
            if span is not None:
                select_from, select_to = span
                if select_to == -1:
                    select_to = len(cells)

        segments: list[Segment] = []
        run: list[str] = []
        run_style: Style | None = None
        run_start = 0

        def flush(end: int) -> None:
            if not run:
                return
            # The compositor maps a click back to content coordinates by
            # reading an "offset" from each segment's style meta. Without it
            # get_widget_and_offset_at returns None and the screen refuses to
            # begin a selection - so drag-to-select silently does nothing.
            meta = Style.from_meta({"offset": (run_start, y)})
            segments.append(Segment("".join(run), (run_style or Style()) + meta))

        for x, cell in enumerate(cells):
            style = _style_for(cell, self._bg_default, self._fg_default)
            if select_from <= x < select_to:
                style = style + self._selection_style()
            if show_cursor and x == cursor_x:
                style = style + _REVERSE
            if run_style is not None and style == run_style:
                run.append(cell.char)
                continue
            flush(x)
            run = [cell.char]
            run_style = style
            run_start = x

        flush(len(cells))
        return segments

    # ----------------------------------------------------------------- overlay

    @property
    def is_dead(self) -> bool:
        """Whether the connection is gone and the pane should offer a retry."""
        return self.session.status in ("error", "closed")

    def overlay_lines(self) -> list[tuple[str, str]]:
        """Rows of the disconnected overlay, as ``(text, role)`` pairs.

        Covers both cases the user hits: a handshake that never succeeded, and
        a live session whose pipe broke underneath them.
        """
        session = self.session
        rows: list[tuple[str, str]] = []

        if session.status == "error":
            rows.append(("Could not connect", "title"))
            rows.append(("", "blank"))
            rows.append((session.error or "Unknown error.", "body"))
        elif session.ended_cleanly:
            rows.append(("Session ended", "title"))
            rows.append(("", "blank"))
            rows.append((f"{session.host_name} closed the connection.", "body"))
        else:
            rows.append(("Connection lost", "title"))
            rows.append(("", "blank"))
            detail = session.error or "The connection to the host was dropped."
            rows.append((detail, "body"))
            status = session.exit_status
            # Negative means "no status reported" (paramiko's default), which
            # is noise rather than information.
            if status is not None and status > 0:
                rows.append((f"Remote exited with status {status}.", "body"))

        if session.target_hint:
            rows.append((session.target_hint, "dim"))
        for hint in session.failure_hints():
            rows.append((hint, "dim"))

        rows.append(("", "blank"))
        rows.append(("[ Retry ]", "button"))
        rows.append(("r retry     ctrl+w launcher     ctrl+shift+w close", "dim"))
        return rows

    def _overlay_geometry(self) -> tuple[int, int, int, int, list[tuple[str, str]]]:
        """``(top, left, width, height, rows)`` for the centred overlay box."""
        rows = self.overlay_lines()
        content_width = max((len(text) for text, _ in rows), default=0)
        width = min(max(content_width + 4, 28), max(self.size.width, 4))
        height = len(rows) + 2  # a blank line of padding top and bottom
        top = max(0, (self.size.height - height) // 2)
        left = max(0, (self.size.width - width) // 2)
        return top, left, width, height, rows

    def _overlay_strip(self, y: int) -> Strip | None:
        """The overlay row for ``y``, or None when the box does not cover it.

        Composited over the terminal content rather than replacing the screen,
        so whatever the session last printed stays readable around it.
        """
        top, left, width, height, rows = self._overlay_geometry()
        if not (top <= y < top + height):
            self._retry_rows.discard(y)
            return None

        index = y - top - 1  # first and last rows of the box are padding
        if index < 0 or index >= len(rows):
            text, role = "", "blank"
        else:
            text, role = rows[index]

        if role == "button":
            self._retry_rows.add(y)
        else:
            self._retry_rows.discard(y)

        theme = self.session.theme
        panel = Style(bgcolor=_OVERLAY_BG)
        styles = {
            "title": panel + Style(color=theme.accent, bold=True),
            "body": panel + Style(color="#e6edf3"),
            "dim": panel + Style(color="#93a1b5"),
            "button": panel + Style(color=_OVERLAY_BG, bgcolor=theme.accent, bold=True),
            "blank": panel,
        }
        style = styles.get(role, panel)

        inner = width - 2
        centred = text.center(inner)[:inner] if text else " " * inner
        # The button reads as a control, so only its label is inverted.
        if role == "button":
            pad = (inner - len(text)) // 2
            box = [
                Segment(" " * (pad + 1), panel),
                Segment(text, style),
                Segment(" " * (inner - pad - len(text) + 1), panel),
            ]
        else:
            box = [Segment(" ", panel), Segment(centred, style), Segment(" ", panel)]

        # Keep the live terminal visible to the left and right of the box.
        row = self._terminal_segments(y)
        left_part = Strip(row, self.session.emulator.cols).crop(0, left)
        right_part = Strip(row, self.session.emulator.cols).crop(
            min(left + width, self.size.width), self.size.width
        )
        return Strip(
            [*left_part, *box, *right_part]
        ).adjust_cell_length(self.size.width, self._blank_style)

    def notice_lines(self) -> list[tuple[str, Style]]:
        """The connecting / failed message, as (text, style) rows."""
        session = self.session
        accent = Style(color=session.theme.accent, bold=True)
        dim = Style(color="#8b98b0")
        bad = Style(color="#ff7b72", bold=True)

        if session.status == "connecting":
            frame = spinner_frame(time.monotonic() - self._started)
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
            return Strip.blank(width, self._blank_style)
        text, style = lines[index]
        if not text:
            return Strip.blank(width, self._blank_style)
        pad = max(0, (width - len(text)) // 2)
        bg = Style(bgcolor=self._bg_default)
        return Strip(
            [Segment(" " * pad, bg), Segment(text, bg + style)]
        ).adjust_cell_length(width, self._blank_style)


#: Markers a terminal wraps pasted text in when the remote enables DECSET 2004.
BRACKET_START = b"\x1b[200~"
BRACKET_END = b"\x1b[201~"

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

#: Overlay panel background. Deliberately not theme-derived: it must read as a
#: panel floating above the session, whatever colour that session is.
_OVERLAY_BG = "#161b22"

_REVERSE = Style(reverse=True)
_STYLE_CACHE: dict[tuple, Style] = {}


def _style_for(cell: Cell, bg_default: str, fg_default: str) -> Style:
    """Cached Rich style for a cell.

    A full screen is a few thousand cells redrawn many times a second, but only
    a handful of distinct styles, so caching turns most of the work into a dict
    lookup. ``bg_default``/``fg_default`` are the theme's colours, used
    whenever pyte reports a cell as "default" (``cell.bg``/``cell.fg`` is
    ``None``) - see the comment in ``TerminalPane.__init__`` for why that
    can't be left unset.
    """
    key = (
        cell.fg,
        cell.bg,
        cell.bold,
        cell.italic,
        cell.underline,
        cell.reverse,
        bg_default,
        fg_default,
    )
    style = _STYLE_CACHE.get(key)
    if style is None:
        style = Style(
            color=cell.fg or fg_default,
            bgcolor=cell.bg or bg_default,
            bold=cell.bold,
            italic=cell.italic,
            underline=cell.underline,
            reverse=cell.reverse,
        )
        _STYLE_CACHE[key] = style
    return style
