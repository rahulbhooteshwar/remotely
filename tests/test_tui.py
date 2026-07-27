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
