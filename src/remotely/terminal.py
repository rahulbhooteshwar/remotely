"""Terminal emulation.

Wraps :mod:`pyte` so a byte stream from a transport becomes something Textual
can draw, and turns Textual key events back into the bytes a remote shell
expects. Keeping this free of Textual widget code means the escape-sequence
handling and key encoding can be tested without an app running.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pyte

DEFAULT_COLS = 80
DEFAULT_ROWS = 24
SCROLLBACK = 5000
#: Fraction of a screen that one paging step moves. pyte's default of 0.5 makes
#: a single wheel notch jump half the view; this makes it feel like scrolling.
SCROLL_RATIO = 0.125

#: pyte reports the eight base colours by name; everything else arrives as a
#: hex string or an xterm index we can pass through.
_NAMED_COLORS = {
    "black": "#1c1c1c",
    "red": "#cc5555",
    "green": "#55aa55",
    "brown": "#aa8855",
    "yellow": "#d7af5f",
    "blue": "#5588cc",
    "magenta": "#aa55aa",
    "cyan": "#55aaaa",
    "white": "#d0d0d0",
    "brightblack": "#5f5f5f",
    "brightred": "#ff7b72",
    "brightgreen": "#7ee787",
    "brightyellow": "#ffd785",
    "brightblue": "#79c0ff",
    "brightmagenta": "#d2a8ff",
    "brightcyan": "#76e3ea",
    "brightwhite": "#ffffff",
}


def resolve_color(value: str | None, *, default: str | None = None) -> str | None:
    """Turn a pyte colour token into something Rich understands."""
    if not value or value == "default":
        return default
    lowered = value.lower()
    if lowered in _NAMED_COLORS:
        return _NAMED_COLORS[lowered]
    if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
        return f"#{value}"
    if value.isdigit():
        index = int(value)
        if 0 <= index <= 255:
            return f"color({index})"
    return default


@dataclass(slots=True)
class Cell:
    """One rendered character and its resolved styling."""

    char: str
    fg: str | None
    bg: str | None
    bold: bool
    italic: bool
    underline: bool
    reverse: bool


class _Screen(pyte.HistoryScreen):
    """A HistoryScreen that tolerates private CSI sequences.

    pyte dispatches a private sequence (``CSI ? ... m``) to the same handler as
    the public one but with ``private=True``, and its own
    ``select_graphic_rendition`` does not accept that keyword - so a remote
    emitting one raised TypeError straight out of ``feed`` and took the whole
    application down. A private SGR is not a standard SGR; ignoring it is the
    correct reading as well as the safe one.
    """

    def select_graphic_rendition(self, *attrs: int, **kwargs: object) -> None:
        if kwargs.get("private"):
            return
        super().select_graphic_rendition(*attrs)


class TerminalEmulator:
    """A pyte screen fed by raw bytes.

    ``HistoryScreen`` gives scrollback; the alternate-screen mode that
    full-screen apps use is handled by pyte itself, so vim and htop behave.
    """

    def __init__(self, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS) -> None:
        self.cols = max(cols, 2)
        self.rows = max(rows, 2)
        self.screen = _Screen(
            self.cols, self.rows, history=SCROLLBACK, ratio=SCROLL_RATIO
        )
        self.stream = pyte.ByteStream(self.screen)
        self._title = ""
        #: Malformed or unsupported sequences seen, kept for --doctor and
        #: tests rather than shown to the user mid-session.
        self.feed_errors = 0
        self.last_feed_error: str | None = None

    # ------------------------------------------------------------------ input

    def feed(self, data: bytes) -> None:
        """Push transport output into the emulator.

        Never raises. Whatever arrives down the wire is untrusted input, and a
        sequence this emulator mishandles has to degrade to a rendering glitch
        in one pane - not take the application, and every other session in it,
        down with it. pyte recovers its parser on the next feed, so swallowing
        the error here costs at most the sequence that caused it.
        """
        if not data:
            return
        try:
            self.stream.feed(data)
        except Exception as exc:  # noqa: BLE001 - deliberately broad
            self.feed_errors += 1
            self.last_feed_error = f"{type(exc).__name__}: {exc}"

    # -------------------------------------------------------------- scrollback

    def scroll_up(self, steps: int = 1) -> bool:
        """Move back through history. Returns whether anything moved."""
        before = self.scrollback_offset
        for _ in range(max(1, steps)):
            try:
                self.screen.prev_page()
            except Exception:
                break
        return self.scrollback_offset != before

    def scroll_down(self, steps: int = 1) -> bool:
        before = self.scrollback_offset
        for _ in range(max(1, steps)):
            try:
                self.screen.next_page()
            except Exception:
                break
        return self.scrollback_offset != before

    def scroll_to_live(self) -> None:
        """Jump back to the bottom, where new output lands."""
        guard = 0
        while self.scrollback_offset > 0 and guard < 1000:
            if not self.scroll_down(1):
                break
            guard += 1

    @property
    def scrollback_offset(self) -> int:
        """How far back from live we are, in pyte's paging units."""
        history = getattr(self.screen, "history", None)
        if history is None:
            return 0
        return max(0, history.size - history.position)

    @property
    def bracketed_paste(self) -> bool:
        """Whether the remote asked for pasted text to be bracketed.

        Shells and editors turn this on (DECSET 2004) so they can tell typing
        from pasting - which is what stops a multi-line paste executing itself
        line by line. pyte stores private modes shifted by five bits.
        """
        return (2004 << 5) in self.screen.mode

    @property
    def at_live_edge(self) -> bool:
        return self.scrollback_offset == 0

    def resize(self, cols: int, rows: int) -> None:
        cols = max(int(cols), 2)
        rows = max(int(rows), 2)
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        self.screen.resize(rows, cols)

    # ----------------------------------------------------------------- output

    @property
    def title(self) -> str:
        """The title the remote set via OSC, if any."""
        return getattr(self.screen, "title", "") or self._title

    @property
    def cursor(self) -> tuple[int, int]:
        """Cursor as ``(x, y)``; ``(-1, -1)`` when hidden."""
        cursor = self.screen.cursor
        if cursor.hidden:
            return (-1, -1)
        return (cursor.x, cursor.y)

    def line(self, y: int) -> list[Cell]:
        """One row of the visible screen, already colour-resolved."""
        buffer = self.screen.buffer
        row = buffer[y] if y in buffer else {}
        cells: list[Cell] = []
        for x in range(self.cols):
            char = row[x] if x in row else None
            if char is None:
                cells.append(Cell(" ", None, None, False, False, False, False))
                continue
            cells.append(
                Cell(
                    char=char.data or " ",
                    fg=resolve_color(char.fg),
                    bg=resolve_color(char.bg),
                    bold=bool(char.bold),
                    italic=bool(char.italics),
                    underline=bool(char.underscore),
                    reverse=bool(char.reverse),
                )
            )
        return cells

    def lines(self) -> Iterable[list[Cell]]:
        for y in range(self.rows):
            yield self.line(y)

    def display(self) -> list[str]:
        """Plain text of the visible screen, used by tests and the tab preview."""
        return list(self.screen.display)

    def take_dirty(self) -> set[int]:
        """Rows changed since the last call."""
        dirty = set(self.screen.dirty)
        self.screen.dirty.clear()
        return dirty


# --------------------------------------------------------------- key encoding

#: Textual key name -> bytes to send. Application-cursor mode is not tracked;
#: normal mode sequences work with every shell we care about and most TUIs.
KEY_SEQUENCES: dict[str, bytes] = {
    "enter": b"\r",
    "tab": b"\t",
    "shift+tab": b"\x1b[Z",
    "backspace": b"\x7f",
    "escape": b"\x1b",
    "space": b" ",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "insert": b"\x1b[2~",
    "delete": b"\x1b[3~",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
    "ctrl+up": b"\x1b[1;5A",
    "ctrl+down": b"\x1b[1;5B",
    "ctrl+right": b"\x1b[1;5C",
    "ctrl+left": b"\x1b[1;5D",
}


def encode_key(key: str, character: str | None = None) -> bytes | None:
    """Encode a Textual key event for the remote terminal.

    Returns ``None`` when the key carries no meaning for the session, so the
    caller can let it fall through to the app's own bindings.
    """
    if key in KEY_SEQUENCES:
        return KEY_SEQUENCES[key]

    if key.startswith("ctrl+") and len(key) == 6:
        letter = key[5]
        if "a" <= letter <= "z":
            return bytes([ord(letter) - ord("a") + 1])
        if letter in "@[\\]^_":
            return bytes([ord(letter) - 64])

    if key.startswith("alt+") and len(key) == 5:
        return b"\x1b" + key[4].encode("utf-8")

    if character:
        return character.encode("utf-8")

    # A single printable character arriving as the key name.
    if len(key) == 1 and key.isprintable():
        return key.encode("utf-8")

    return None
