"""Terminal emulation and key encoding."""

from __future__ import annotations

import pytest

from remotely.terminal import (
    KEY_SEQUENCES,
    TerminalEmulator,
    encode_key,
    resolve_color,
)


@pytest.fixture
def term() -> TerminalEmulator:
    return TerminalEmulator(40, 6)


# --------------------------------------------------------------------- output


def test_plain_text_is_displayed(term: TerminalEmulator) -> None:
    term.feed(b"hello world")
    assert term.display()[0].startswith("hello world")


def test_newlines_advance_rows(term: TerminalEmulator) -> None:
    term.feed(b"one\r\ntwo\r\nthree")
    display = term.display()
    assert display[0].strip() == "one"
    assert display[1].strip() == "two"
    assert display[2].strip() == "three"


def test_colour_is_captured(term: TerminalEmulator) -> None:
    term.feed(b"\x1b[31mR")
    cell = term.line(0)[0]
    assert cell.char == "R"
    assert cell.fg == "#cc5555"


def test_bold_and_underline(term: TerminalEmulator) -> None:
    term.feed(b"\x1b[1;4mX")
    cell = term.line(0)[0]
    assert cell.bold is True
    assert cell.underline is True


def test_256_and_true_colour(term: TerminalEmulator) -> None:
    term.feed(b"\x1b[38;5;208mA\x1b[38;2;255;0;255mB")
    line = term.line(0)
    assert line[0].fg == "#ff8700"
    assert line[1].fg == "#ff00ff"


def test_background_colour(term: TerminalEmulator) -> None:
    term.feed(b"\x1b[44mB")
    assert term.line(0)[0].bg == "#5588cc"


def test_clear_screen(term: TerminalEmulator) -> None:
    term.feed(b"junk\x1b[2J\x1b[H")
    assert term.display()[0].strip() == ""


def test_carriage_return_overwrites(term: TerminalEmulator) -> None:
    term.feed(b"aaaa\rbb")
    assert term.display()[0].startswith("bbaa")


def test_backspace(term: TerminalEmulator) -> None:
    term.feed(b"abc\x08 ")
    assert term.display()[0].startswith("ab")


def test_cursor_position_tracked(term: TerminalEmulator) -> None:
    term.feed(b"abc")
    assert term.cursor == (3, 0)
    term.feed(b"\r\n")
    assert term.cursor[1] == 1


def test_hidden_cursor_reports_offscreen(term: TerminalEmulator) -> None:
    term.feed(b"\x1b[?25l")
    assert term.cursor == (-1, -1)


def test_line_is_padded_to_width(term: TerminalEmulator) -> None:
    term.feed(b"hi")
    assert len(term.line(0)) == term.cols


def test_resize_changes_geometry(term: TerminalEmulator) -> None:
    term.resize(100, 30)
    assert (term.cols, term.rows) == (100, 30)
    assert len(term.line(0)) == 100


def test_resize_refuses_degenerate_sizes(term: TerminalEmulator) -> None:
    term.resize(0, 0)
    assert term.cols >= 2 and term.rows >= 2


def test_scrolling_keeps_latest_content(term: TerminalEmulator) -> None:
    for i in range(20):
        term.feed(f"line{i}\r\n".encode())
    assert "line19" in "\n".join(term.display())


def test_title_from_osc(term: TerminalEmulator) -> None:
    term.feed(b"\x1b]0;my-remote-host\x07")
    assert term.title == "my-remote-host"


def test_dirty_rows_reported_then_cleared(term: TerminalEmulator) -> None:
    term.feed(b"x")
    assert term.take_dirty()
    assert term.take_dirty() == set()


def test_alternate_screen_is_handled(term: TerminalEmulator) -> None:
    """Full-screen apps switch buffers; the emulator must not fall over."""
    term.feed(b"normal\r\n")
    term.feed(b"\x1b[?1049h")  # enter alternate screen
    term.feed(b"fullscreen app")
    assert "fullscreen app" in "".join(term.display())
    term.feed(b"\x1b[?1049l")  # leave it
    assert term.cols and term.rows


# --------------------------------------------------------------------- colours


@pytest.mark.parametrize(
    "value,expected",
    [
        ("red", "#cc5555"),
        ("brightred", "#ff7b72"),
        ("ff8700", "#ff8700"),
        ("default", None),
        (None, None),
        ("", None),
    ],
)
def test_resolve_color(value, expected) -> None:
    assert resolve_color(value) == expected


def test_resolve_color_default_override() -> None:
    assert resolve_color("default", default="#ffffff") == "#ffffff"


# ---------------------------------------------------------------- key encoding


@pytest.mark.parametrize(
    "key,expected",
    [
        ("enter", b"\r"),
        ("tab", b"\t"),
        ("backspace", b"\x7f"),
        ("escape", b"\x1b"),
        ("up", b"\x1b[A"),
        ("down", b"\x1b[B"),
        ("right", b"\x1b[C"),
        ("left", b"\x1b[D"),
        ("home", b"\x1b[H"),
        ("delete", b"\x1b[3~"),
        ("f5", b"\x1b[15~"),
    ],
)
def test_special_keys(key: str, expected: bytes) -> None:
    assert encode_key(key) == expected


@pytest.mark.parametrize(
    "key,expected",
    [
        ("ctrl+c", b"\x03"),
        ("ctrl+d", b"\x04"),
        ("ctrl+a", b"\x01"),
        ("ctrl+z", b"\x1a"),
    ],
)
def test_control_keys(key: str, expected: bytes) -> None:
    """ctrl+c must reach the remote, not be swallowed as an app shortcut."""
    assert encode_key(key) == expected


def test_alt_prefixes_escape() -> None:
    assert encode_key("alt+b") == b"\x1bb"


def test_printable_character() -> None:
    assert encode_key("a", "a") == b"a"
    assert encode_key("question_mark", "?") == b"?"


def test_unicode_character() -> None:
    assert encode_key("é", "é") == "é".encode("utf-8")


def test_unknown_key_returns_none() -> None:
    """Unmapped keys fall through so app bindings still work."""
    assert encode_key("f24") is None
    assert encode_key("ctrl+shift+alt+f9") is None


def test_every_mapped_sequence_is_bytes() -> None:
    assert all(isinstance(v, bytes) and v for v in KEY_SEQUENCES.values())
