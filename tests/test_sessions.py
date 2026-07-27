"""tmux backend, exercised against a real tmux server on a private socket.

These are skipped when tmux is unavailable rather than mocked away, because the
whole point of the backend is the behaviour of the real thing.
"""

from __future__ import annotations

import os
import shutil
import uuid

import pytest

from remotely.sessions import (
    HOST_TAG,
    SessionTab,
    TmuxBackend,
    TmuxError,
    ensure_utf8_locale,
    format_tab_lines,
    hold_on_failure,
    locale_supports_unicode,
)
from remotely.ssh import build_plan
from remotely.themes import ThemeRegistry

from .conftest import make_host

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture
def backend():
    socket = f"remotely-test-{uuid.uuid4().hex[:8]}"
    backend = TmuxBackend(session_name="remotely-test", socket_name=socket)
    yield backend
    try:
        backend.server.kill()
    except Exception:
        pass


def test_ensure_session_creates_it(backend: TmuxBackend) -> None:
    assert not backend.session_exists()
    backend.ensure_session("sleep 60")
    assert backend.session_exists()


def test_ensure_session_is_idempotent(backend: TmuxBackend) -> None:
    first = backend.ensure_session("sleep 60")
    second = backend.ensure_session("sleep 60")
    assert first.session_name == second.session_name
    assert len(backend.get_session().windows) == 1


def test_open_tab_creates_a_window(backend: TmuxBackend, themes: ThemeRegistry) -> None:
    backend.ensure_session("sleep 60")
    host = make_host("prod-web", theme="prod")
    plan = build_plan(host, None, ssh_binary="sleep")
    plan.argv = ["sleep", "60"]

    tab = backend.open_tab(host, plan, themes.get("prod"))
    assert tab.host == "prod-web"
    assert tab.theme == "prod"
    assert "prod-web" in tab.name
    assert len(backend.get_session().windows) == 2


def test_theme_is_applied_to_the_window(backend: TmuxBackend, themes: ThemeRegistry) -> None:
    backend.ensure_session("sleep 60")
    host = make_host("prod-web", theme="prod")
    plan = build_plan(host, None)
    plan.argv = ["sleep", "60"]

    tab = backend.open_tab(host, plan, themes.get("prod"))
    window = next(
        w for w in backend.get_session().windows if str(w.window_id) == tab.window_id
    )
    style = window.show_option("window-style")
    assert style == themes.get("prod").tmux["window_style"]


def test_environment_reaches_the_session(backend: TmuxBackend, themes: ThemeRegistry, tmp_path) -> None:
    backend.ensure_session("sleep 60")
    marker = tmp_path / "env.txt"
    host = make_host("envtest")
    plan = build_plan(host, None)
    plan.argv = ["sh", "-c", f"printf %s \"$REMOTELY_ASKPASS_FILE\" > {marker}; sleep 30"]
    plan.env = {"REMOTELY_ASKPASS_FILE": "/tmp/expected-path"}

    backend.open_tab(host, plan, themes.get("personal"))

    import time

    for _ in range(50):
        if marker.exists() and marker.read_text():
            break
        time.sleep(0.1)
    assert marker.read_text() == "/tmp/expected-path"


def test_list_tabs_only_reports_our_windows(
    backend: TmuxBackend, themes: ThemeRegistry
) -> None:
    session = backend.ensure_session("sleep 60")
    session.new_window(window_name="user-window", attach=False, window_shell="sleep 60")

    host = make_host("ours")
    plan = build_plan(host, None)
    plan.argv = ["sleep", "60"]
    backend.open_tab(host, plan, themes.get("personal"))

    tabs = backend.list_tabs()
    assert [tab.host for tab in tabs] == ["ours"]


def test_close_tab(backend: TmuxBackend, themes: ThemeRegistry) -> None:
    backend.ensure_session("sleep 60")
    host = make_host("closeme")
    plan = build_plan(host, None)
    plan.argv = ["sleep", "60"]

    tab = backend.open_tab(host, plan, themes.get("personal"))
    assert len(backend.list_tabs()) == 1

    backend.close_tab(tab.window_id)
    assert backend.list_tabs() == []


def test_instantly_failing_command_still_yields_a_visible_tab(
    backend: TmuxBackend, themes: ThemeRegistry
) -> None:
    """A dead command must leave the pane on screen, not vanish.

    Without remain-on-exit inherited at creation time the window disappears
    before it can be styled and libtmux raises on the missing window id, so the
    user sees an internal error instead of the reason ssh failed.
    """
    backend.ensure_session("sleep 60")
    host = make_host("broken")
    plan = build_plan(host, None)
    plan.argv = ["/nonexistent/ssh", "user@host"]

    tab = backend.open_tab(host, plan, themes.get("prod"))
    assert tab.host == "broken"
    assert [t.host for t in backend.list_tabs()] == ["broken"]


def test_failing_command_in_a_session_we_did_not_create(
    backend: TmuxBackend, themes: ThemeRegistry
) -> None:
    """Same guarantee when adopting the user's own tmux session."""
    backend.server.new_session(session_name="theirs", attach=False, window_command="sleep 60")
    session = next(s for s in backend.server.sessions if s.session_name == "theirs")

    host = make_host("adopted")
    plan = build_plan(host, None)
    plan.argv = ["/nonexistent/ssh", "user@host"]

    tab = backend.open_tab(host, plan, themes.get("prod"), session=session)
    assert [t.host for t in backend.list_tabs(session)] == ["adopted"]
    assert tab.index != "0"


def test_close_unknown_tab_raises(backend: TmuxBackend) -> None:
    backend.ensure_session("sleep 60")
    with pytest.raises(TmuxError):
        backend.close_tab("@999")


def test_list_tabs_without_session_is_empty(backend: TmuxBackend) -> None:
    assert backend.list_tabs() == []


def test_attach_command_targets_the_socket(backend: TmuxBackend) -> None:
    argv = backend.attach_command()
    assert argv[0] == "tmux"
    assert "-L" in argv and backend.socket_name in argv
    assert argv[-1] == "remotely-test"


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"LANG": "en_US.UTF-8"}, True),
        ({"LC_ALL": "C.utf8"}, True),
        ({"LANG": "C"}, False),
        ({"LC_ALL": "POSIX"}, False),
        ({}, False),
    ],
)
def test_locale_detection(monkeypatch: pytest.MonkeyPatch, env: dict, expected: bool) -> None:
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert locale_supports_unicode() is expected


def test_utf8_locale_is_installed_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a UTF-8 locale libtmux cannot even parse tmux's output."""
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "C")

    assert ensure_utf8_locale() is True
    assert locale_supports_unicode() is True
    assert "utf" in os.environ["LC_CTYPE"].lower()


def test_existing_utf8_locale_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "en_GB.UTF-8")

    assert ensure_utf8_locale() is True
    assert "LC_CTYPE" not in os.environ  # nothing was overridden


def test_session_creation_works_from_a_c_locale(
    backend: TmuxBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: this used to fail with an opaque zip() error."""
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "C")

    backend.ensure_session("sleep 60")
    assert backend.session_exists()


def test_no_utf8_locale_available_is_reported_not_papered_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A system with no UTF-8 locale at all cannot support tmux.

    libtmux fails to parse tmux's format output before a tab title is ever
    computed, so there is nothing to degrade gracefully to here - the honest
    behaviour is to say so, which is what --doctor surfaces.
    """
    monkeypatch.setattr("remotely.sessions._available_locales", lambda: {"c", "posix"})
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "C")

    assert ensure_utf8_locale() is False
    assert locale_supports_unicode() is False


def test_tab_title_is_ascii_when_unicode_is_unavailable(themes: ThemeRegistry) -> None:
    """The title itself still degrades, which covers terminals tmux mishandles."""
    assert themes.get("prod").tab_title("web-1", unicode_ok=False) == "[!] web-1"


def test_attach_command_only_forces_utf8_when_the_locale_agrees(
    backend: TmuxBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "C")
    assert "-u" not in backend.attach_command()

    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert "-u" in backend.attach_command()


def test_hold_on_failure_preserves_the_command() -> None:
    wrapped = hold_on_failure("ssh -p 22 user@host")
    assert wrapped.startswith("ssh -p 22 user@host;")
    assert "read" in wrapped


def test_format_tab_lines() -> None:
    tabs = [
        SessionTab("@1", "1", "n", "web", "prod", active=True),
        SessionTab("@2", "2", "n", "db", "", active=False),
    ]
    lines = format_tab_lines(tabs)
    assert lines[0].startswith("*")
    assert "prod" in lines[0]
    assert lines[1].startswith(" ")
