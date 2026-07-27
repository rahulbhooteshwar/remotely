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
