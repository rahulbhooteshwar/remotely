"""Session manager, end to end against a real SSH server.

These go through the whole stack the way the app does: vault -> credential ->
transport -> live SSH channel -> pyte emulator -> rendered cells.
"""

from __future__ import annotations

import paramiko
import pytest

from remotely.models import Credential, Host
from remotely.sessions import SessionError, SessionManager
from remotely.themes import ThemeRegistry
from remotely.vault import Vault

from .conftest import PASSCODE
from .sshserver import PASSWORD, USERNAME, SSHTestServer, wait_for


@pytest.fixture(scope="module")
def host_key() -> paramiko.PKey:
    return paramiko.RSAKey.generate(2048)


@pytest.fixture
def server(host_key: paramiko.PKey):
    with SSHTestServer(host_key=host_key) as running:
        yield running


@pytest.fixture
def manager() -> SessionManager:
    made = SessionManager()
    yield made
    made.close_all()


def make_host(server: SSHTestServer, **overrides) -> Host:
    defaults = dict(
        name="testbox",
        hostname=server.hostname,
        username=USERNAME,
        port=server.port,
        theme="prod",
        auth_mode="credential",
        credential="pw",
    )
    defaults.update(overrides)
    return Host(**defaults)


def credential() -> Credential:
    return Credential(name="pw", kind="password", password=PASSWORD)


def connect(manager, server, themes, **overrides):
    """Open a session and wait for the handshake to finish."""
    host = make_host(server, **overrides)
    session = manager.open(host, credential(), themes.get(host.theme), cols=80, rows=24)
    assert wait_for(lambda: session.status != "connecting", timeout=15), "never connected"
    return session


def pump(session, needle: str, timeout: float = 10.0) -> str:
    """Drain the transport into the emulator until ``needle`` appears."""

    def check() -> bool:
        session.emulator.feed(session.drain())
        return needle in "\n".join(session.emulator.display())

    ok = wait_for(check, timeout)
    screen = "\n".join(session.emulator.display())
    assert ok, f"never saw {needle!r} on screen:\n{screen}"
    return screen


# ------------------------------------------------------------------- lifecycle


def test_session_connects_and_renders(
    manager: SessionManager, server: SSHTestServer, themes: ThemeRegistry
) -> None:
    session = connect(manager, server, themes)
    assert session.status == "connected"
    assert "Welcome to the test server" in pump(session, "Welcome")


def test_session_is_registered(
    manager: SessionManager, server: SSHTestServer, themes: ThemeRegistry
) -> None:
    session = connect(manager, server, themes)
    assert manager.get(session.id) is session
    assert len(manager) == 1
    assert [s.host_name for s in manager.live()] == ["testbox"]
    assert manager.for_host("TESTBOX") == [session]


def test_bad_password_reports_error_without_raising(
    manager: SessionManager, server: SSHTestServer, themes: ThemeRegistry
) -> None:
    """A failed connection must surface on the session, not crash the app."""
    host = make_host(server)
    bad = Credential(name="pw", kind="password", password="nope")
    session = manager.open(host, bad, themes.get("prod"), cols=80, rows=24)

    assert wait_for(lambda: session.status == "error", timeout=15)
    assert session.error and "uthentication" in session.error
    assert not session.is_live


def test_typing_reaches_the_remote(
    manager: SessionManager, server: SSHTestServer, themes: ThemeRegistry
) -> None:
    session = connect(manager, server, themes)
    pump(session, "$")
    session.write(b"whoami\r")
    assert USERNAME in pump(session, USERNAME)


def test_colour_survives_the_round_trip(
    manager: SessionManager, server: SSHTestServer, themes: ThemeRegistry
) -> None:
    session = connect(manager, server, themes)
    pump(session, "$")
    session.write(b"colour\r")
    pump(session, "BOLDGREEN")

    # Find the styled cell rather than assuming a position.
    reds = [
        cell
        for row in range(session.emulator.rows)
        for cell in session.emulator.line(row)
        if cell.fg == "#cc5555"
    ]
    assert reds, "no red cells found"
    greens = [
        cell
        for row in range(session.emulator.rows)
        for cell in session.emulator.line(row)
        if cell.bold and cell.fg == "#55aa55"
    ]
    assert greens, "no bold green cells found"


def test_resize_propagates_to_remote_pty(
    manager: SessionManager, server: SSHTestServer, themes: ThemeRegistry
) -> None:
    session = connect(manager, server, themes)
    pump(session, "$")
    session.resize(120, 40)
    assert session.emulator.cols == 120
    assert wait_for(lambda: server.server.pty_size == (120, 40))


def test_remote_logout_closes_the_session(
    manager: SessionManager, server: SSHTestServer, themes: ThemeRegistry
) -> None:
    session = connect(manager, server, themes)
    pump(session, "$")
    session.write(b"\x04")  # ctrl+d
    assert wait_for(lambda: not session.is_live, timeout=10)
    assert session.status == "closed"


def test_close_stops_the_session(
    manager: SessionManager, server: SSHTestServer, themes: ThemeRegistry
) -> None:
    session = connect(manager, server, themes)
    pump(session, "$")
    manager.close(session.id)
    assert session.status == "closed"
    assert manager.live() == []


def test_close_unknown_session_raises(manager: SessionManager) -> None:
    with pytest.raises(SessionError):
        manager.close("nope")


def test_remove_deregisters(
    manager: SessionManager, server: SSHTestServer, themes: ThemeRegistry
) -> None:
    session = connect(manager, server, themes)
    manager.close(session.id)
    manager.remove(session.id)
    assert manager.get(session.id) is None
    assert len(manager) == 0


def test_multiple_concurrent_sessions(
    manager: SessionManager, server: SSHTestServer, themes: ThemeRegistry
) -> None:
    first = connect(manager, server, themes)
    second = connect(manager, server, themes, theme="personal")

    assert first.id != second.id
    assert len(manager.live()) == 2
    assert {s.theme.name for s in manager.list()} == {"prod", "personal"}

    pump(first, "$")
    pump(second, "$")
    first.write(b"whoami\r")
    assert USERNAME in pump(first, USERNAME)


def test_close_all(
    manager: SessionManager, server: SSHTestServer, themes: ThemeRegistry
) -> None:
    connect(manager, server, themes)
    connect(manager, server, themes)
    manager.close_all()
    assert manager.live() == []


# ---------------------------------------------------------------------- theming


def test_session_title_uses_theme(
    manager: SessionManager, server: SSHTestServer, themes: ThemeRegistry
) -> None:
    session = connect(manager, server, themes)
    assert session.title == "🔴 testbox"


def test_vault_backed_credential_reaches_the_server(
    manager: SessionManager, server: SSHTestServer, themes: ThemeRegistry
) -> None:
    """The full path: encrypted at rest, decrypted, authenticated, connected."""
    vault = Vault()
    vault.initialise(PASSCODE)
    vault.put(Credential(name="pw", kind="password", password=PASSWORD))
    vault.lock()

    reopened = Vault()
    reopened.unlock(PASSCODE)
    stored = reopened.require("pw")

    host = make_host(server)
    session = manager.open(host, stored, themes.get("prod"), cols=80, rows=24)
    assert wait_for(lambda: session.status != "connecting", timeout=15)
    assert session.status == "connected", session.error
    assert "Welcome" in pump(session, "Welcome")
