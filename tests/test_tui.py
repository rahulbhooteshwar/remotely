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

            # The tab exists and carries the theme marker.
            tab_ids = [t.id for t in app.query(Tabs).first().query("Tab")]
            assert session.id in tab_ids
            assert "🔴" in session.title

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
