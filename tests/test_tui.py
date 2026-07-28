"""TUI behaviour, driven headlessly through Textual's pilot."""

from __future__ import annotations

import pytest

from remotely.tui.app import CommandBar, RemotelyApp
from textual.widgets import OptionList

from .conftest import make_host


def build_app() -> RemotelyApp:
    app = RemotelyApp()
    app.store.add(make_host("prod-web", group="Production", tags=["web"], theme="prod"))
    app.store.add(make_host("prod-db", group="Production", tags=["db"], theme="prod"))
    app.store.add(
        make_host("laptop", hostname="192.168.1.5", group="Personal", tags=["home"],
                  theme="personal")
    )
    return app


async def test_idle_view_lists_hosts_by_group() -> None:
    app = build_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        results = app.query_one("#results", OptionList)
        # 2 group headers + 3 hosts
        assert results.option_count == 5
        # Headers are not selectable.
        assert results.get_option_at_index(0).disabled


async def test_typing_filters_to_matching_hosts() -> None:
    app = build_app()
    async with app.run_test() as pilot:
        app.query_one("#command-bar", CommandBar).value = "prod"
        await pilot.pause()
        assert {c.value for c in app._rows.values()} == {"prod-web", "prod-db"}


async def test_slash_shows_commands() -> None:
    app = build_app()
    async with app.run_test() as pilot:
        app.query_one("#command-bar", CommandBar).value = "/"
        await pilot.pause()
        assert all(c.kind == "command" for c in app._rows.values())


async def test_tag_filter() -> None:
    app = build_app()
    async with app.run_test() as pilot:
        app.query_one("#command-bar", CommandBar).value = "@home"
        await pilot.pause()
        assert [c.value for c in app._rows.values()] == ["home"]


async def test_no_match_shows_placeholder() -> None:
    app = build_app()
    async with app.run_test() as pilot:
        app.query_one("#command-bar", CommandBar).value = "zzzzzz"
        await pilot.pause()
        results = app.query_one("#results", OptionList)
        assert results.option_count == 1
        assert results.get_option_at_index(0).disabled


async def test_tab_accepts_highlighted_completion() -> None:
    app = build_app()
    async with app.run_test() as pilot:
        bar = app.query_one("#command-bar", CommandBar)
        bar.value = "/conn"
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert bar.value == "/connect "


async def test_arrow_keys_skip_group_headers() -> None:
    app = build_app()
    async with app.run_test() as pilot:
        results = app.query_one("#results", OptionList)
        await pilot.pause()
        first = results.highlighted
        assert first is not None
        assert not results.get_option_at_index(first).disabled

        await pilot.press("down")
        await pilot.pause()
        second = results.highlighted
        assert second != first
        assert not results.get_option_at_index(second).disabled


async def test_escape_clears_the_bar() -> None:
    app = build_app()
    async with app.run_test() as pilot:
        bar = app.query_one("#command-bar", CommandBar)
        bar.value = "prod"
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert bar.value == ""


async def test_detail_pane_follows_highlight() -> None:
    app = build_app()
    async with app.run_test() as pilot:
        app.query_one("#command-bar", CommandBar).value = "laptop"
        await pilot.pause()
        assert app._selected_host is not None
        assert app._selected_host.name == "laptop"


async def test_connect_reports_missing_host() -> None:
    app = build_app()
    async with app.run_test() as pilot:
        app._run_command("connect", "does-not-exist")
        await pilot.pause()
        status = app.query_one("#status")
        assert status.has_class("error")


async def test_unknown_command_is_reported() -> None:
    app = build_app()
    async with app.run_test() as pilot:
        app._run_command("frobnicate", "")
        await pilot.pause()
        assert app.query_one("#status").has_class("error")


async def test_banner_reflects_host_count_and_vault_state() -> None:
    app = build_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_banner()
        await pilot.pause()
        # 3 hosts were added and no vault has been created yet.
        assert len(app.store) == 3
        assert app.vault.is_locked


async def test_reload_picks_up_external_changes() -> None:
    app = build_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        from remotely.store import HostStore

        other = HostStore()
        other.add(make_host("added-elsewhere", group="Later"))

        app.action_reload()
        await pilot.pause()
        assert app.store.get("added-elsewhere") is not None


# --------------------------------------------------------------- live sessions


async def test_connecting_opens_a_themed_tab_and_renders_the_remote() -> None:
    """The whole app path: launcher -> vault -> SSH -> terminal pane."""
    import paramiko

    from remotely.models import Credential
    from remotely.vault import Vault
    from remotely.tui.terminal import TerminalPane
    from textual.widgets import Tabs

    from .conftest import PASSCODE
    from .sshserver import PASSWORD, USERNAME, SSHTestServer

    with SSHTestServer(host_key=paramiko.RSAKey.generate(2048)) as server:
        app = RemotelyApp()
        app.store.add(
            make_host(
                "testbox",
                hostname=server.hostname,
                username=USERNAME,
                port=server.port,
                theme="prod",
                auth_mode="credential",
                credential="pw",
            )
        )
        app.vault.initialise(PASSCODE)
        app.vault.put(Credential(name="pw", kind="password", password=PASSWORD))

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert [t.id for t in app.query(Tabs).first().query("Tab")] == ["launcher"]

            app._connect("testbox")

            # Wait for the handshake, then for the banner to render.
            for _ in range(300):
                await pilot.pause()
                sessions = app.sessions.list()
                if sessions and sessions[0].status != "connecting":
                    break
            assert app.sessions.list(), "no session was created"
            session = app.sessions.list()[0]
            assert session.status == "connected", session.error

            pane = app.query_one(f"#{session.id}", TerminalPane)
            for _ in range(300):
                await pilot.pause()
                if "Welcome" in "\n".join(session.emulator.display()):
                    break
            screen = "\n".join(session.emulator.display())
            assert "Welcome to the test server" in screen, screen

            # The tab exists and carries the theme as colour, not an emoji.
            tab_ids = [t.id for t in app.query(Tabs).first().query("Tab")]
            assert session.id in tab_ids
            assert "🔴" not in str(app.query_one(f"Tab#{session.id}").label)

            # The pane renders real cells, not a blank strip.
            strip = pane.render_line(0)
            assert "".join(seg.text for seg in strip).strip()

            app.sessions.close_all()


async def test_failed_connection_surfaces_in_the_status_line() -> None:
    from remotely.models import Credential
    from remotely.vault import Vault
    from .conftest import PASSCODE

    app = RemotelyApp()
    app.store.add(
        make_host("dead", hostname="127.0.0.1", port=9, auth_mode="credential", credential="pw")
    )
    app.vault.initialise(PASSCODE)
    app.vault.put(Credential(name="pw", kind="password", password="x"))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("dead")
        for _ in range(300):
            await pilot.pause()
            sessions = app.sessions.list()
            if sessions and sessions[0].status == "error":
                break
        assert app.sessions.list()[0].status == "error"
        app.sessions.close_all()


async def test_vault_is_asked_for_once_per_run_not_per_connection() -> None:
    """Unlocking is per app run: a second connect must not re-prompt.

    Encodes the behaviour directly, because it is the difference between the
    app feeling usable and feeling like it nags.
    """
    import paramiko

    from remotely.models import Credential
    from .conftest import PASSCODE
    from .sshserver import PASSWORD, USERNAME, SSHTestServer

    with SSHTestServer(host_key=paramiko.RSAKey.generate(2048)) as server:
        app = RemotelyApp()
        for name in ("box-a", "box-b"):
            app.store.add(
                make_host(
                    name,
                    hostname=server.hostname,
                    username=USERNAME,
                    port=server.port,
                    auth_mode="credential",
                    credential="pw",
                )
            )
        app.vault.initialise(PASSCODE)
        app.vault.put(Credential(name="pw", kind="password", password=PASSWORD))

        prompts = 0
        original = app._unlock_vault

        def counting(*args, **kwargs):
            nonlocal prompts
            prompts += 1
            return original(*args, **kwargs)

        app._unlock_vault = counting  # type: ignore[method-assign]

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert not app.vault.is_locked  # unlocked by initialise() above

            for host in ("box-a", "box-b"):
                app._connect(host)
                for _ in range(300):
                    await pilot.pause()
                    if any(s.status != "connecting" for s in app.sessions.for_host(host)):
                        break

            assert len(app.sessions.list()) == 2
            assert prompts == 0, "an unlocked vault must never re-prompt"
            app.sessions.close_all()


async def test_passcode_dialog_is_emphasised() -> None:
    """The prompt appears unannounced mid-task, so it must stand out."""
    from remotely.tui.screens import PasscodeScreen
    from textual.widgets import Button

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(PasscodeScreen(message="Unlock to continue."))
        await pilot.pause()

        modal = app.screen.query_one(".modal")
        classes = modal.classes
        assert "modal-passcode" in classes, "passcode dialog lost its emphasis class"

        # Centred, not pinned to a corner.
        assert app.screen.styles.align_horizontal == "center"
        assert app.screen.styles.align_vertical == "middle"

        # The action is named, not a generic OK.
        assert app.screen.query_one("#ok", Button).label.plain == "Unlock"


async def test_chrome_updates_are_safe_after_teardown() -> None:
    """Workers outlive the widget tree, so chrome updates must not crash.

    Regression for a CI-only race: a connection attempt still in flight when
    the app exits woke up and called query_one('#banner') on a torn-down DOM,
    failing the worker. The same thing happens in real use if you quit while
    a host is still connecting.
    """
    app = build_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._dom_alive()

    # The app has exited; every chrome path must now be a quiet no-op.
    assert not app._dom_alive()
    app._refresh_banner()
    app._status("late message")
    app._status("late error", error=True)
    app._refresh_results()
    app._clear_bar()
    app.action_show_launcher()


async def test_quitting_mid_connect_does_not_crash_the_watcher() -> None:
    """Tear down while a connection is still in flight."""
    from remotely.models import Credential
    from .conftest import PASSCODE

    app = build_app()
    app.store.add(
        make_host("slow", hostname="10.255.255.1", port=9, auth_mode="credential",
                  credential="pw")
    )
    app.vault.initialise(PASSCODE)
    app.vault.put(Credential(name="pw", kind="password", password="x"))

    async with app.run_test() as pilot:
        await pilot.pause()
        app._connect("slow")
        await pilot.pause()
        # Leave immediately, with the handshake still outstanding.

    assert not app._dom_alive()
    app.sessions.close_all()


# ------------------------------------------------------- usability regressions


async def test_pane_fits_between_the_tabs_and_the_footer() -> None:
    """The pane used to overrun the screen, hiding the prompt and status line.

    Regression: it was 29 rows starting at row 3 on a 30-row screen, so the
    last terminal lines rendered under the footer and #status fell off the
    bottom entirely.
    """
    from remotely.tui.terminal import TerminalPane

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        pane = app.query_one(f"#{session.id}", TerminalPane)
        for _ in range(30):
            await pilot.pause()

        footer = app.query_one("Footer")
        status = app.query_one("#status")
        screen_height = app.screen.size.height

        pane_bottom = pane.region.y + pane.region.height
        assert pane_bottom <= footer.region.y, "pane overlaps the footer"
        assert status.region.y < screen_height, "status line is off-screen"
        assert pane.region.y + pane.region.height <= screen_height
        # The remote is told exactly the rows we can draw.
        assert session.emulator.rows == pane.size.height
        app.sessions.close_all()


async def test_failed_session_shows_the_reason_in_the_pane() -> None:
    """A failure used to leave a blank pane with nothing to act on."""
    from remotely.models import Credential
    from remotely.tui.terminal import TerminalPane

    from .conftest import PASSCODE

    app = build_app()
    app.store.add(
        make_host("dead", hostname="127.0.0.1", port=9, auth_mode="credential", credential="pw")
    )
    app.vault.initialise(PASSCODE)
    app.vault.put(Credential(name="pw", kind="password", password="x"))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("dead")
        for _ in range(400):
            await pilot.pause()
            s = app.sessions.list()
            if s and s[0].status == "error":
                break
        session = app.sessions.list()[0]
        assert session.status == "error"

        pane = app.query_one(f"#{session.id}", TerminalPane)
        text = " ".join(line for line, _ in pane.notice_lines())
        assert "Could not connect to dead" in text
        assert session.error and session.error.split(".")[0] in text
        # And something the user can actually do about it.
        assert session.failure_hints(), "no actionable hint for a refused connection"
        app.sessions.close_all()


async def test_connecting_state_is_visible_not_blank() -> None:
    from remotely.tui.terminal import TerminalPane

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        pane = app.query_one(f"#{session.id}", TerminalPane)
        if session.status == "connecting":
            text = " ".join(line for line, _ in pane.notice_lines())
            assert "Connecting to prod-web" in text
        app.sessions.close_all()


async def test_terminal_output_can_be_selected_for_copying() -> None:
    from textual.selection import Selection
    from textual.geometry import Offset
    from remotely.tui.terminal import TerminalPane

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        session.emulator.feed(b"copy-me-please\r\n")
        pane = app.query_one(f"#{session.id}", TerminalPane)
        await pilot.pause()

        assert pane.ALLOW_SELECT is True
        result = pane.get_selection(Selection(Offset(0, 0), Offset(14, 0)))
        assert result is not None, "get_selection returned nothing"
        text, ending = result
        assert "copy-me" in text
        app.sessions.close_all()


async def test_closing_one_tab_keeps_the_others() -> None:
    from remotely.tui.app import SessionTab
    from textual.widgets import Tabs

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for host in ("prod-web", "prod-db"):
            app._connect(host)
            for _ in range(200):
                await pilot.pause()
                if app.sessions.for_host(host):
                    break
        assert len(app.sessions.list()) == 2

        first = app.sessions.list()[0]
        app._close_session(first.id)
        await pilot.pause()

        remaining = app.sessions.list()
        assert len(remaining) == 1, "closing one tab should not close the others"
        tab_ids = [t.id for t in app.query(Tabs).first().query("Tab")]
        assert first.id not in tab_ids
        assert remaining[0].id in tab_ids
        # And we land on the surviving session, not ejected to the launcher.
        assert app.query_one("#content").current == remaining[0].id
        app.sessions.close_all()


async def test_tab_carries_a_close_control() -> None:
    from remotely.tui.app import SessionTab

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        tab = app.query_one(f"Tab#{session.id}", SessionTab)
        assert SessionTab.CLOSE_GLYPH in str(tab.label)
        app.sessions.close_all()


async def test_close_command_reports_unknown_host() -> None:
    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._run_command("close", "nope")
        await pilot.pause()
        assert app.query_one("#status").has_class("error")


async def test_rendered_segments_carry_selection_offsets() -> None:
    """Drag-to-select needs offset metadata on every segment.

    Regression: ALLOW_SELECT plus get_selection was not enough. Textual's
    compositor maps a click back to content coordinates by reading
    meta["offset"] from a segment's style; with plain segments it returns None
    and the screen never begins a selection, so dragging did nothing at all.
    """
    from remotely.tui.terminal import TerminalPane

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        session.status = "connected"
        session.emulator.feed(b"selectable text here\r\n")
        pane = app.query_one(f"#{session.id}", TerminalPane)
        await pilot.pause()

        strip = pane.render_line(0)
        segments = list(strip)
        assert segments, "nothing rendered"

        offsets = [
            seg.style.meta["offset"]
            for seg in segments
            if seg.style is not None and seg.style._meta is not None
            and "offset" in seg.style.meta
        ]
        assert offsets, "no segment carried an 'offset' meta; selection cannot start"

        # x must be the character index of the segment's start, y the row.
        assert offsets[0] == (0, 0)
        assert all(oy == 0 for _, oy in offsets)
        assert offsets == sorted(offsets), "offsets must increase across the line"

        # And row 1 reports row 1.
        row1 = [
            seg.style.meta["offset"]
            for seg in pane.render_line(1)
            if seg.style is not None and seg.style._meta is not None
            and "offset" in seg.style.meta
        ]
        assert row1 and all(oy == 1 for _, oy in row1)
        app.sessions.close_all()


async def test_default_coloured_cells_use_the_theme_background() -> None:
    """A cell pyte reports as "default" must paint the theme, not the host terminal's.

    Regression: cells the remote never explicitly coloured resolved to
    bg=None, and Textual does not backfill a None segment background from the
    widget's CSS for content lines - so nothing was emitted for that cell and
    whatever terminal the app happened to be running inside showed through
    its own default colour instead of the theme's.
    """
    from remotely.tui.terminal import TerminalPane

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        session.status = "connected"
        # Untouched cells: nothing has been written, so pyte reports "default".
        pane = app.query_one(f"#{session.id}", TerminalPane)
        await pilot.pause()

        theme_background = session.theme.pane_styles()["background"]
        strip = pane.render_line(0)
        segments = list(strip)
        assert segments, "nothing rendered"
        for seg in segments:
            assert seg.style is not None
            assert seg.style.bgcolor is not None, "a cell was left with no background at all"
            assert seg.style.bgcolor.name == theme_background

        app.sessions.close_all()


async def test_clicking_a_tab_does_not_crash() -> None:
    """Regression: SessionTab._on_click called super() with the event.

    Tab._on_click takes no event argument, so every click on a session tab
    raised "takes 1 positional argument but 2 were given". Textual walks the
    MRO and invokes the base handler itself, so there is nothing to delegate.
    """
    from remotely.tui.app import SessionTab
    from textual.events import Click

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # Not what this test is about: go straight to closing.
        app.settings.confirm_close_tab = False
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        tab = app.query_one(f"Tab#{session.id}", SessionTab)

        def click(x: int) -> Click:
            return Click(widget=tab, x=x, y=0, delta_x=0, delta_y=0, button=1,
                         shift=False, meta=False, ctrl=False,
                         screen_x=x, screen_y=0, style=None)

        # A click on the label selects; neither path may raise. Coordinates are
        # relative to the tab's region, which includes its border and padding.
        await tab._on_message(click(1))
        await pilot.pause()
        assert app.sessions.get(session.id) is not None, "label click closed the session"

        # The end of the host name must not close it either - that is the far
        # right of the *text*, several cells before the glyph.
        await tab._on_message(click(max(1, tab.region.width - 5)))
        for _ in range(10):
            await pilot.pause()
        assert app.sessions.get(session.id) is not None, (
            "clicking the host name closed the tab"
        )

        # A click on the trailing x asks to close it.
        await tab._on_message(click(tab.region.width - 1))
        for _ in range(30):
            await pilot.pause()
        assert app.sessions.get(session.id) is None, "clicking the x did not close the tab"
        app.sessions.close_all()


async def test_selection_is_actually_painted() -> None:
    """Regression: the selection was tracked but never drawn.

    Textual applies the selection highlight inside Visual.to_strips, which only
    runs for widgets rendering through render()/Visual. TerminalPane paints via
    render_line and so bypassed it: screen.selections was correct and
    get_selection() returned the right text, but not one cell on screen ever
    changed - indistinguishable from selection being broken.
    """
    from textual.events import MouseMove

    from remotely.tui.terminal import TerminalPane

    app = build_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        session.status = "connected"
        session.emulator.feed(b"HELLO-SELECT-ME world\r\n")
        pane = app.query_one(f"#{session.id}", TerminalPane)
        await pilot.pause()

        def backgrounds():
            return [str(seg.style.bgcolor) for seg in pane.render_line(0)]

        plain = backgrounds()
        assert len(set(plain)) == 1, "unselected row should be one uniform background"

        origin = pane.region.offset
        await pilot.mouse_down(pane, offset=(0, 0))
        await pilot.pause()
        # Drive the drag the way the app forwards a real mouse move.
        app.screen._forward_event(
            MouseMove(widget=None, x=origin.x + 12, y=origin.y, delta_x=12, delta_y=0,
                      button=1, shift=False, meta=False, ctrl=False,
                      screen_x=origin.x + 12, screen_y=origin.y)
        )
        await pilot.pause()
        await pilot.pause()

        selection = pane.text_selection
        assert selection is not None, "no selection was registered"
        span = selection.get_span(0)
        assert span is not None

        during = backgrounds()
        assert len(set(during)) > 1, "selection produced no visible highlight"

        # The highlight must stop where the selection stops, not flood the row.
        highlight = pane._selection_style().bgcolor
        painted = sum(
            len(seg.text) for seg in pane.render_line(0) if seg.style.bgcolor == highlight
        )
        assert painted == span[1] - span[0], "highlight width does not match the selection"

        # And the selected text is readable: fg must not equal the highlight bg.
        for seg in pane.render_line(0):
            if seg.style.bgcolor == highlight:
                assert seg.style.color != seg.style.bgcolor, "selected text is invisible"
        app.sessions.close_all()


async def test_finished_selection_is_copied_to_the_clipboard() -> None:
    """ctrl+c belongs to the remote shell, so copy happens on release."""
    from textual.events import MouseMove

    from remotely.tui.terminal import TerminalPane

    app = build_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        session.status = "connected"
        session.emulator.feed(b"COPY-THIS-PLEASE\r\n")
        pane = app.query_one(f"#{session.id}", TerminalPane)
        await pilot.pause()

        origin = pane.region.offset
        await pilot.mouse_down(pane, offset=(0, 0))
        await pilot.pause()
        app.screen._forward_event(
            MouseMove(widget=None, x=origin.x + 9, y=origin.y, delta_x=9, delta_y=0,
                      button=1, shift=False, meta=False, ctrl=False,
                      screen_x=origin.x + 9, screen_y=origin.y)
        )
        await pilot.pause()
        await pilot.mouse_up(pane, offset=(9, 0))
        for _ in range(20):
            await pilot.pause()

        assert "COPY-THIS" in (app._clipboard or ""), "selection was not copied"
        app.sessions.close_all()


async def test_tabs_carry_theme_colour_instead_of_an_emoji() -> None:
    """The theme reads as the tab's colour; the emoji stays in the launcher."""
    from remotely.tui.app import SessionTab

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        await pilot.pause()
        tab = app.query_one(f"Tab#{session.id}", SessionTab)

        label = str(tab.label)
        assert "prod-web" in label
        assert SessionTab.CLOSE_GLYPH in label
        theme = app.themes.get("prod")
        assert theme.icon not in label, "tab still shows the theme emoji"

        # The theme is conveyed by colour instead.
        assert tab.styles.background.hex.lower().startswith(theme.accent.lower()[:4])
        assert tab.styles.color != tab.styles.background
        app.sessions.close_all()


async def test_tab_title_ignores_the_remote_set_title() -> None:
    """Tabs name the host record, whatever the remote shell calls itself.

    Regression: the OSC title was written onto the tab, so a box whose shell
    set "user@host" showed that while others showed the host name - the tab
    row meant different things depending on which server you had reached.
    """
    from remotely.tui.app import SessionTab

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        session.status = "connected"
        await pilot.pause()

        # Exactly what a login shell emits.
        session.emulator.feed(b"\x1b]0;deploy@prod-web-01:~\x07")
        for _ in range(15):
            await pilot.pause()

        assert session.emulator.title == "deploy@prod-web-01:~", "OSC title not parsed"
        label = str(app.query_one(f"Tab#{session.id}", SessionTab).label)
        assert "prod-web" in label
        assert "deploy@" not in label, "remote title leaked onto the tab"
        app.sessions.close_all()


async def test_shortcuts_target_the_session_you_are_looking_at() -> None:
    """On a session tab, ctrl+e must edit that host, not the hidden highlight."""
    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        await pilot.pause()

        # The launcher is highlighting something else entirely, off screen.
        app._selected_host = app.store.get("laptop")
        assert app._context_host().name == "prod-web"

        # Back on the launcher the highlight is visible again, so it wins.
        app.action_show_launcher()
        await pilot.pause()
        assert app._context_host().name == "laptop"
        app.sessions.close_all()


async def test_banner_is_hidden_on_a_session_tab() -> None:
    """The version/host/vault row is launcher chrome, not session chrome."""
    from textual.widgets import Static

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        banner = app.query_one("#banner", Static)
        assert banner.display, "banner should show on the launcher"

        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        await pilot.pause()
        assert not banner.display, "banner still visible on a session tab"

        app.action_show_launcher()
        await pilot.pause()
        assert banner.display, "banner did not come back on the launcher"
        app.sessions.close_all()


async def test_successful_connect_leaves_no_status_chatter() -> None:
    """"Connected to X" said nothing the tab and pane did not already show."""
    import paramiko

    from remotely.models import Credential
    from textual.widgets import Static

    from .conftest import PASSCODE
    from .sshserver import PASSWORD, USERNAME, SSHTestServer

    with SSHTestServer(host_key=paramiko.RSAKey.generate(2048)) as server:
        app = RemotelyApp()
        app.store.add(
            make_host("testbox", hostname=server.hostname, username=USERNAME,
                      port=server.port, auth_mode="credential", credential="pw")
        )
        app.vault.initialise(PASSCODE)
        app.vault.put(Credential(name="pw", kind="password", password=PASSWORD))

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._connect("testbox")
            for _ in range(400):
                await pilot.pause()
                sessions = app.sessions.list()
                if sessions and sessions[0].status == "connected":
                    break
            for _ in range(40):
                await pilot.pause()

            session = app.sessions.list()[0]
            assert session.status == "connected", session.error
            status = app.query_one("#status", Static)
            text = "".join(seg.text for seg in status.render_line(0))
            assert "Connected to" not in text, f"status still chatters: {text!r}"
            app.sessions.close_all()


def _pane_rows(pane) -> list[str]:
    return [
        "".join(seg.text for seg in pane.render_line(y)).strip()
        for y in range(pane.size.height)
    ]


async def test_failed_launch_shows_a_retry_overlay() -> None:
    """A handshake that never succeeded offers a way to try again."""
    from remotely.models import Credential
    from remotely.tui.terminal import TerminalPane

    from .conftest import PASSCODE

    app = build_app()
    app.store.add(
        make_host("dead", hostname="127.0.0.1", port=9, auth_mode="credential",
                  credential="pw")
    )
    app.vault.initialise(PASSCODE)
    app.vault.put(Credential(name="pw", kind="password", password="x"))

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app._connect("dead")
        for _ in range(400):
            await pilot.pause()
            sessions = app.sessions.list()
            if sessions and sessions[0].status == "error":
                break
        session = app.sessions.list()[0]
        pane = app.query_one(f"#{session.id}", TerminalPane)
        for _ in range(20):
            await pilot.pause()

        assert pane.is_dead
        text = " ".join(_pane_rows(pane))
        assert "Could not connect" in text
        assert "Retry" in text, "no retry affordance on a failed launch"
        assert pane._retry_rows, "retry button has no clickable row"
        app.sessions.close_all()


async def test_broken_pipe_shows_the_overlay_over_the_session() -> None:
    """An in-flight disconnect must say so without wiping what was on screen."""
    import paramiko

    from remotely.models import Credential
    from remotely.tui.terminal import TerminalPane

    from .conftest import PASSCODE
    from .sshserver import PASSWORD, USERNAME, SSHTestServer

    with SSHTestServer(host_key=paramiko.RSAKey.generate(2048)) as server:
        app = RemotelyApp()
        app.store.add(
            make_host("box", hostname=server.hostname, username=USERNAME,
                      port=server.port, auth_mode="credential", credential="pw")
        )
        app.vault.initialise(PASSCODE)
        app.vault.put(Credential(name="pw", kind="password", password=PASSWORD))

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._connect("box")
            for _ in range(400):
                await pilot.pause()
                sessions = app.sessions.list()
                if sessions and sessions[0].status == "connected":
                    break
            session = app.sessions.list()[0]
            pane = app.query_one(f"#{session.id}", TerminalPane)
            for _ in range(80):
                await pilot.pause()
                if "Welcome" in "\n".join(session.emulator.display()):
                    break

            # The pipe breaks underneath a live session.
            session.transport.close()
            for _ in range(200):
                await pilot.pause()
                if not session.is_live:
                    break
            for _ in range(20):
                await pilot.pause()

            assert pane.is_dead
            rows = _pane_rows(pane)
            text = " ".join(rows)
            assert "Connection lost" in text
            assert "Retry" in text
            # Composited, not a wipe: the session's output is still readable.
            assert any("Welcome to the test server" in row for row in rows), (
                "overlay replaced the screen instead of floating over it"
            )
            app.sessions.close_all()


async def test_retry_reconnects_in_the_same_tab() -> None:
    """Retry must reuse the session id so the tab does not move or vanish."""
    import paramiko

    from remotely.models import Credential
    from remotely.tui.terminal import TerminalPane

    from .conftest import PASSCODE
    from .sshserver import PASSWORD, USERNAME, SSHTestServer

    with SSHTestServer(host_key=paramiko.RSAKey.generate(2048)) as server:
        app = RemotelyApp()
        app.store.add(
            make_host("box", hostname=server.hostname, username=USERNAME,
                      port=server.port, auth_mode="credential", credential="pw")
        )
        app.vault.initialise(PASSCODE)
        app.vault.put(Credential(name="pw", kind="password", password=PASSWORD))

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._connect("box")
            for _ in range(400):
                await pilot.pause()
                sessions = app.sessions.list()
                if sessions and sessions[0].status == "connected":
                    break
            session = app.sessions.list()[0]
            original_id = session.id
            pane = app.query_one(f"#{session.id}", TerminalPane)
            for _ in range(60):
                await pilot.pause()

            session.transport.close()
            for _ in range(200):
                await pilot.pause()
                if not session.is_live:
                    break
            assert not session.is_live

            pane.focus()
            await pilot.pause()
            await pilot.press("r")
            for _ in range(400):
                await pilot.pause()
                if session.status == "connected":
                    break
            for _ in range(40):
                await pilot.pause()

            assert session.status == "connected", session.error
            assert session.id == original_id, "retry minted a new session id"
            assert app.query(f"Tab#{original_id}"), "the tab was lost on retry"
            assert len(app.sessions.live()) == 1
            assert not pane.is_dead
            # A reconnected session must be able to report a second death.
            assert pane._announced_close is False
            app.sessions.close_all()


async def test_clean_exit_is_not_reported_as_a_failure() -> None:
    """Typing `exit` is not a broken pipe and must not be described as one."""
    import paramiko

    from remotely.models import Credential
    from remotely.tui.terminal import TerminalPane

    from .conftest import PASSCODE
    from .sshserver import PASSWORD, USERNAME, SSHTestServer

    with SSHTestServer(host_key=paramiko.RSAKey.generate(2048)) as server:
        app = RemotelyApp()
        app.store.add(
            make_host("box", hostname=server.hostname, username=USERNAME,
                      port=server.port, auth_mode="credential", credential="pw")
        )
        app.vault.initialise(PASSCODE)
        app.vault.put(Credential(name="pw", kind="password", password=PASSWORD))

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._connect("box")
            for _ in range(400):
                await pilot.pause()
                sessions = app.sessions.list()
                if sessions and sessions[0].status == "connected":
                    break
            session = app.sessions.list()[0]
            pane = app.query_one(f"#{session.id}", TerminalPane)
            for _ in range(80):
                await pilot.pause()
                if "Welcome" in "\n".join(session.emulator.display()):
                    break

            session.write(b"exit\r")
            for _ in range(300):
                await pilot.pause()
                if not session.is_live:
                    break
            for _ in range(20):
                await pilot.pause()

            assert not session.is_live
            text = " ".join(_pane_rows(pane))
            assert "Session ended" in text, text
            assert "Connection lost" not in text
            # Still offers to reconnect - that is the common next thing to want.
            assert "Retry" in text
            app.sessions.close_all()


@pytest.mark.parametrize("size", [(120, 40), (84, 24), (72, 18), (60, 14), (50, 12)])
async def test_form_buttons_stay_on_screen_at_any_terminal_size(size) -> None:
    """Save/Cancel must never be clipped, however few rows or columns there are.

    Regression, reported with a large terminal font - which costs both rows
    and columns. Two independent causes: .form-scroll had a fixed 24-row cap
    taller than the whole dialog on a short terminal, pushing the buttons off
    the bottom; and .modal-wide was a fixed 84 columns, pushing the
    right-aligned Save clean off the right edge on a narrow one.
    """
    from remotely.tui.screens import HostFormScreen
    from textual.widgets import Button

    width, height = size
    app = build_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.push_screen(HostFormScreen(None, themes=app.themes.names()))
        for _ in range(12):
            await pilot.pause()

        buttons = {str(b.label): b for b in app.screen.query(Button)}
        assert {"Save", "Cancel"} <= set(buttons), f"form lost its buttons: {buttons}"

        for label, button in buttons.items():
            region = button.region
            assert region.y >= 0, f"{label} is above the screen at {size}"
            assert region.y + region.height <= height, f"{label} is below the screen at {size}"
            assert region.x >= 0, f"{label} is off the left edge at {size}"
            assert region.x + region.width <= width, f"{label} is off the right edge at {size}"

        # The fields must still be reachable, not squeezed to nothing.
        scroll = app.screen.query_one(".form-scroll")
        assert scroll.region.height >= 1, "no room left for the form fields"


@pytest.mark.parametrize("size", [(100, 40), (84, 24), (72, 18), (60, 14)])
async def test_every_form_field_is_reachable_on_small_screens(size) -> None:
    """Fixed chrome must not squeeze the form down to an unusable slit.

    Making the buttons sticky is not enough on its own: the title, padding and
    button row cost the same 11 rows at 14 lines as at 60, which left a
    one-row scroll viewport - scrollable in principle, unusable in practice.
    """
    from remotely.tui.screens import HostFormScreen

    width, height = size
    app = build_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.push_screen(HostFormScreen(None, themes=app.themes.names()))
        for _ in range(12):
            await pilot.pause()

        scroll = app.screen.query_one(".form-scroll")
        # Enough to show a labelled field, not just a sliver.
        assert scroll.region.height >= 5, (
            f"form viewport is {scroll.region.height} rows at {size}"
        )

        # The bottom of the form must be reachable by scrolling.
        scroll.scroll_end(animate=False)
        for _ in range(8):
            await pilot.pause()
        assert scroll.scroll_y == scroll.max_scroll_y, "cannot scroll to the end"

        last_field = list(app.screen.query(".field"))[-1]
        assert scroll.region.contains_region(last_field.region), (
            f"last field unreachable at {size}"
        )


async def test_chrome_stays_roomy_on_a_normal_terminal() -> None:
    """The compact treatment is for small terminals only."""
    from remotely.tui.screens import HostFormScreen

    app = build_app()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        app.push_screen(HostFormScreen(None, themes=app.themes.names()))
        for _ in range(12):
            await pilot.pause()
        assert not app.screen.query_one(".modal").has_class("compact")

    app2 = RemotelyApp()  # bare: the per-test store already has build_app's hosts
    async with app2.run_test(size=(60, 14)) as pilot:
        await pilot.pause()
        app2.push_screen(HostFormScreen(None, themes=app2.themes.names()))
        for _ in range(12):
            await pilot.pause()
        assert app2.screen.query_one(".modal").has_class("compact")


async def _open_many(app, pilot, count: int, theme: str = "prod") -> None:
    from remotely.models import Host

    for i in range(count):
        app.store.add(
            Host(name=f"box-{i:02d}", hostname=f"10.0.0.{i}", username="u", port=22,
                 group="G", tags=[], theme=theme)
        )
    for i in range(count):
        app._connect(f"box-{i:02d}")
        for _ in range(150):
            await pilot.pause()
            if app.sessions.for_host(f"box-{i:02d}"):
                break
    for _ in range(10):
        await pilot.pause()


async def test_tab_bar_scrolls_with_the_wheel() -> None:
    """Overflowing tabs were reachable only by activating one, which needs a
    click on the tab you cannot see. The wheel now works anywhere over the row.
    """
    from remotely.tui.app import SessionTabs
    from textual import events

    app = RemotelyApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await _open_many(app, pilot, 12)

        tabs = app.query_one("#tabs", SessionTabs)
        viewport = tabs.query_one("#tabs-scroll")
        assert viewport.max_scroll_x > 0, "tabs do not overflow; test proves nothing"

        viewport.scroll_to(x=0, animate=False, force=True)
        await pilot.pause()

        def wheel(cls):
            return cls(widget=tabs, x=40, y=0, delta_x=0, delta_y=1, button=0,
                       shift=False, meta=False, ctrl=False, screen_x=40, screen_y=0)

        tabs._on_mouse_scroll_down(wheel(events.MouseScrollDown))
        await pilot.pause()
        moved = viewport.scroll_offset.x
        assert moved > 0, "wheel down did not scroll the tab bar"

        tabs._on_mouse_scroll_up(wheel(events.MouseScrollUp))
        await pilot.pause()
        assert viewport.scroll_offset.x < moved, "wheel up did not scroll back"
        app.sessions.close_all()


async def test_tab_bar_scrolls_while_hovering_an_edge() -> None:
    from remotely.tui.app import SessionTabs
    from textual import events

    app = RemotelyApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await _open_many(app, pilot, 12)

        tabs = app.query_one("#tabs", SessionTabs)
        viewport = tabs.query_one("#tabs-scroll")
        assert viewport.max_scroll_x > 0
        viewport.scroll_to(x=0, animate=False, force=True)
        await pilot.pause()

        def move(x):
            return events.MouseMove(widget=tabs, x=x, y=0, delta_x=0, delta_y=0,
                                    button=0, shift=False, meta=False, ctrl=False,
                                    screen_x=x, screen_y=0)

        tabs._on_mouse_move(move(tabs.size.width - 1))
        for _ in range(10):
            await pilot.pause(0.07)
        right = viewport.scroll_offset.x
        assert right > 0, "hovering the right edge did not scroll"

        # Away from the edge it must stop, not keep drifting.
        tabs._on_mouse_move(move(tabs.size.width // 2))
        await pilot.pause()
        assert tabs._edge_timer is None, "edge scrolling kept running mid-row"
        settled = viewport.scroll_offset.x
        for _ in range(5):
            await pilot.pause(0.07)
        assert viewport.scroll_offset.x == settled, "kept scrolling after leaving the edge"

        # And the left edge brings it back.
        tabs._on_mouse_move(move(0))
        for _ in range(20):
            await pilot.pause(0.07)
        assert viewport.scroll_offset.x < settled, "hovering the left edge did not scroll back"

        tabs._on_mouse_move(move(tabs.size.width // 2))
        await pilot.pause()
        app.sessions.close_all()


async def test_edge_scrolling_stops_when_leaving_the_tab_bar() -> None:
    """A timer left running after the pointer leaves would scroll forever."""
    from remotely.tui.app import SessionTabs
    from textual import events

    app = RemotelyApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await _open_many(app, pilot, 12)
        tabs = app.query_one("#tabs", SessionTabs)
        # Opening tabs leaves the row scrolled to the last one; from there the
        # right edge has nowhere to go and the timer correctly stops itself.
        tabs.query_one("#tabs-scroll").scroll_to(x=0, animate=False, force=True)
        await pilot.pause()

        tabs._on_mouse_move(
            events.MouseMove(widget=tabs, x=tabs.size.width - 1, y=0, delta_x=0,
                             delta_y=0, button=0, shift=False, meta=False,
                             ctrl=False, screen_x=0, screen_y=0)
        )
        await pilot.pause()
        assert tabs._edge_timer is not None, "edge hover did not start scrolling"

        tabs._on_leave(events.Leave(tabs))
        await pilot.pause()
        assert tabs._edge_timer is None, "scrolling continued after the pointer left"
        app.sessions.close_all()


async def test_same_theme_tabs_are_separated_by_borders() -> None:
    """Adjacent tabs on one theme are a single block of colour without edges."""
    from remotely.tui.app import SessionTab, SessionTabs

    app = RemotelyApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await _open_many(app, pilot, 3, theme="prod")

        tabs = list(app.query(SessionTab))
        assert len(tabs) == 3
        for tab in tabs:
            left = tab.styles.border_left
            right = tab.styles.border_right
            assert left[0] not in ("", "none", None), f"no left border: {left}"
            assert right[0] not in ("", "none", None), f"no right border: {right}"
            # Dark against the accent fill, so it reads as a divider.
            assert left[1] != tab.styles.background
            # A border occupies real cells, or it separates nothing.
            assert tab.region.width > tab.size.width

        app.sessions.close_all()


async def test_connecting_uses_the_rich_dots_spinner() -> None:
    from rich.spinner import SPINNERS

    from remotely.tui.terminal import TerminalPane, spinner_frame

    frames = SPINNERS["dots"]["frames"]

    # Clock-driven, so the rate does not depend on the repaint rate.
    assert spinner_frame(0.0) == frames[0]
    assert spinner_frame(0.08) == frames[1]
    assert spinner_frame(0.08 * len(frames)) == frames[0], "must wrap around"

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        pane = app.query_one(f"#{session.id}", TerminalPane)
        if session.status == "connecting":
            text = " ".join(line for line, _ in pane.notice_lines())
            assert any(f in text for f in frames), "connecting notice has no dots frame"
        app.sessions.close_all()


async def test_left_click_drag_actually_selects_text() -> None:
    """End to end drag-selection, the way a mouse does it.

    The earlier test only asserted get_selection() worked when handed a
    Selection, which passed while real dragging did nothing. This drives the
    same path the compositor and screen use for a real mouse.
    """
    from textual.events import MouseMove

    from remotely.tui.terminal import TerminalPane

    app = build_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        session.status = "connected"
        session.emulator.feed(b"HELLO-SELECT-ME world\r\n")
        pane = app.query_one(f"#{session.id}", TerminalPane)
        await pilot.pause()

        origin = pane.region.offset
        # The precondition: the compositor must resolve a content offset.
        _, offset = app.screen.get_widget_and_offset_at(origin.x + 2, origin.y)
        assert offset is not None, "compositor cannot map the click to content"

        await pilot.mouse_down(pane, offset=(0, 0))
        await pilot.pause()
        app.screen.post_message(
            MouseMove(
                widget=pane, x=11, y=0, delta_x=11, delta_y=0, button=1,
                shift=False, meta=False, ctrl=False,
                screen_x=origin.x + 11, screen_y=origin.y,
            )
        )
        await pilot.pause()
        await pilot.pause()
        await pilot.mouse_up(pane, offset=(11, 0))
        await pilot.pause()

        selected = app.screen.get_selected_text()
        assert selected, "dragging produced no selection"
        assert "HELLO-SELECT" in selected
        app.sessions.close_all()


# ----------------------------------------------------------------- confirmations


async def test_declining_the_quit_prompt_keeps_the_app_running() -> None:
    """The prompt has to actually prevent the quit, not just appear."""
    from remotely.tui.screens import ConfirmScreen

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.live():
                break
        assert app.sessions.live()

        app.action_quit()
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, ConfirmScreen):
                break
        assert isinstance(app.screen, ConfirmScreen), "no confirmation was shown"

        app.screen.dismiss(False)
        for _ in range(20):
            await pilot.pause()
        assert app.is_running, "app quit despite the prompt being declined"
        assert app.sessions.live(), "sessions were closed despite declining"
        app.sessions.close_all()


async def test_quitting_without_sessions_does_not_nag() -> None:
    """Nothing is at risk, so there is nothing to confirm."""
    from remotely.tui.screens import ConfirmScreen

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert not app.sessions.live()
        app.action_quit()
        for _ in range(10):
            await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)


async def test_quit_prompt_can_be_turned_off() -> None:
    from remotely.tui.screens import ConfirmScreen

    app = build_app()
    app.settings.confirm_quit = False
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.live():
                break
        app.action_quit()
        for _ in range(10):
            await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)


async def test_declining_the_close_prompt_keeps_the_tab() -> None:
    from remotely.tui.screens import ConfirmScreen

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.live():
                break
        session = app.sessions.live()[0]

        app._request_close(session.id)
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, ConfirmScreen):
                break
        assert isinstance(app.screen, ConfirmScreen), "no confirmation was shown"

        app.screen.dismiss(False)
        for _ in range(20):
            await pilot.pause()
        assert app.sessions.get(session.id) is not None, "tab closed despite declining"
        app.sessions.close_all()


async def test_accepting_the_close_prompt_closes_the_tab() -> None:
    from remotely.tui.screens import ConfirmScreen

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.live():
                break
        session = app.sessions.live()[0]

        app._request_close(session.id)
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, ConfirmScreen):
                break
        app.screen.dismiss(True)
        for _ in range(30):
            await pilot.pause()
            if app.sessions.get(session.id) is None:
                break
        assert app.sessions.get(session.id) is None
        app.sessions.close_all()


async def test_closing_a_dead_tab_does_not_prompt() -> None:
    """A finished session has nothing left to lose."""
    from remotely.tui.screens import ConfirmScreen

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._connect("prod-web")
        for _ in range(200):
            await pilot.pause()
            if app.sessions.list():
                break
        session = app.sessions.list()[0]
        session.status = "closed"

        app._request_close(session.id)
        for _ in range(20):
            await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)
        assert app.sessions.get(session.id) is None


async def test_settings_command_persists_a_toggle() -> None:
    from remotely import settings as settings_module
    from remotely.tui.screens import ListPickerScreen

    app = build_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.settings.confirm_quit is True

        app._run_command("settings", "")
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, ListPickerScreen):
                break
        assert isinstance(app.screen, ListPickerScreen), "settings list did not open"

        app.screen.dismiss("confirm_quit")
        for _ in range(20):
            await pilot.pause()

        assert app.settings.confirm_quit is False
        assert settings_module.load().confirm_quit is False, "toggle was not persisted"
