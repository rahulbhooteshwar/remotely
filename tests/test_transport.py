"""SSH transport, exercised against a real Paramiko SSH server.

Nothing here is mocked: a genuine key exchange, authentication and channel are
performed. A bundled SSH client is only trustworthy if it is tested this way.
"""

from __future__ import annotations

import paramiko
import pytest

from remotely import config
from remotely.models import Credential, Host
from remotely.transport import (
    AuthFailed,
    ParamikoTransport,
    SystemSSHTransport,
    TransportError,
    build_spec,
    create_transport,
    known_hosts_path,
)

from .sshserver import PASSWORD, USERNAME, SSHTestServer, wait_for


@pytest.fixture(scope="module")
def host_key() -> paramiko.PKey:
    # Key generation is slow; one per module is plenty.
    return paramiko.RSAKey.generate(2048)


@pytest.fixture
def server(host_key: paramiko.PKey):
    with SSHTestServer(host_key=host_key) as running:
        yield running


def host_for(server: SSHTestServer, **overrides) -> Host:
    defaults = dict(
        name="testbox",
        hostname=server.hostname,
        username=USERNAME,
        port=server.port,
        auth_mode="credential",
        credential="pw",
    )
    defaults.update(overrides)
    return Host(**defaults)


def password_credential(password: str = PASSWORD) -> Credential:
    return Credential(name="pw", kind="password", password=password)


def read_until(transport, needle: bytes, timeout: float = 10.0) -> bytes:
    """Accumulate output until ``needle`` shows up."""
    buffer = bytearray()

    def got_it() -> bool:
        buffer.extend(transport.read())
        return needle in bytes(buffer)

    assert wait_for(got_it, timeout), (
        f"never saw {needle!r}; got {bytes(buffer)!r}"
    )
    return bytes(buffer)


# ------------------------------------------------------------------- connecting


def test_password_auth_connects(server: SSHTestServer) -> None:
    transport = ParamikoTransport(build_spec(host_for(server), password_credential()))
    transport.connect(80, 24)
    try:
        assert not transport.closed
        assert b"Welcome to the test server" in read_until(transport, b"$ ")
    finally:
        transport.close()


def test_wrong_password_is_rejected(server: SSHTestServer) -> None:
    transport = ParamikoTransport(
        build_spec(host_for(server), password_credential("wrong"))
    )
    with pytest.raises(AuthFailed):
        transport.connect(80, 24)


def test_unknown_host_fails_clearly() -> None:
    host = Host(
        name="nope",
        hostname="127.0.0.1",
        username="x",
        port=9,  # discard port, nothing listening
        auth_mode="credential",
        credential="pw",
    )
    transport = ParamikoTransport(build_spec(host, password_credential()))
    with pytest.raises(TransportError):
        transport.connect(80, 24)


def test_key_auth_connects(tmp_path, host_key: paramiko.PKey) -> None:
    client_key = paramiko.RSAKey.generate(2048)
    key_file = tmp_path / "id_rsa"
    client_key.write_private_key_file(str(key_file))

    with SSHTestServer(
        password=None, authorized_key=client_key, host_key=host_key
    ) as server:
        credential = Credential(name="k", kind="key", key_path=str(key_file))
        transport = ParamikoTransport(build_spec(host_for(server), credential))
        transport.connect(80, 24)
        try:
            assert b"Welcome" in read_until(transport, b"$ ")
        finally:
            transport.close()


# ------------------------------------------------------------------ interaction


def test_roundtrip_command(server: SSHTestServer) -> None:
    transport = ParamikoTransport(build_spec(host_for(server), password_credential()))
    transport.connect(80, 24)
    try:
        read_until(transport, b"$ ")
        transport.write(b"whoami\r")
        assert USERNAME.encode() in read_until(transport, USERNAME.encode())
    finally:
        transport.close()


def test_pty_size_is_sent(server: SSHTestServer) -> None:
    transport = ParamikoTransport(build_spec(host_for(server), password_credential()))
    transport.connect(120, 40)
    try:
        read_until(transport, b"$ ")
        assert server.server is not None
        assert server.server.pty_size == (120, 40)

        transport.resize(100, 30)
        assert wait_for(lambda: server.server.pty_size == (100, 30))
    finally:
        transport.close()


def test_close_ends_the_session(server: SSHTestServer) -> None:
    transport = ParamikoTransport(build_spec(host_for(server), password_credential()))
    transport.connect(80, 24)
    read_until(transport, b"$ ")
    transport.close()
    assert transport.closed


def test_remote_logout_marks_closed(server: SSHTestServer) -> None:
    transport = ParamikoTransport(build_spec(host_for(server), password_credential()))
    transport.connect(80, 24)
    try:
        read_until(transport, b"$ ")
        transport.write(b"\x04")  # ctrl+d
        assert wait_for(lambda: bool(transport.read()) or transport.closed)
        assert wait_for(lambda: transport.closed, timeout=5)
    finally:
        transport.close()


# ------------------------------------------------------------------- host keys


def test_host_key_is_recorded(server: SSHTestServer) -> None:
    transport = ParamikoTransport(build_spec(host_for(server), password_credential()))
    transport.connect(80, 24)
    transport.close()

    recorded = known_hosts_path()
    assert recorded.exists()
    assert recorded.stat().st_mode & 0o777 == 0o600
    assert str(server.port) in recorded.read_text() or server.hostname in recorded.read_text()


def test_changed_host_key_is_refused(server: SSHTestServer, host_key) -> None:
    """A different key on the same host:port must not be silently accepted."""
    transport = ParamikoTransport(build_spec(host_for(server), password_credential()))
    transport.connect(80, 24)
    transport.close()

    # Same port, brand new host key: this is what a MITM looks like.
    impostor_key = paramiko.RSAKey.generate(2048)
    server.stop()
    with SSHTestServer(host_key=impostor_key) as impostor:
        spec = build_spec(host_for(impostor), password_credential())
        # Rewrite the recorded entry to point at the impostor's port so the
        # mismatch is on a known host rather than an unknown one.
        recorded = known_hosts_path()
        text = recorded.read_text().replace(f"]:{server.port}", f"]:{impostor.port}")
        recorded.write_text(text)

        with pytest.raises(TransportError):
            ParamikoTransport(spec).connect(80, 24)


# ------------------------------------------------------------------------ spec


def test_spec_prefers_credential_username(server: SSHTestServer) -> None:
    credential = Credential(name="pw", kind="password", password="x", username="override")
    spec = build_spec(host_for(server), credential)
    assert spec.username == "override"
    assert spec.target.startswith("override@")


def test_spec_translates_known_ssh_options() -> None:
    host = Host(
        name="h",
        hostname="example.com",
        username="u",
        ssh_options=["ConnectTimeout=5", "ServerAliveInterval=15", "StrictHostKeyChecking=yes"],
    )
    spec = build_spec(host, None)
    assert spec.connect_timeout == 5.0
    assert spec.keepalive == 15
    assert spec.strict_host_keys is True


def test_spec_flags_untranslatable_options() -> None:
    host = Host(
        name="h", hostname="example.com", username="u", ssh_options=["ProxyCommand=nc %h %p"]
    )
    spec = build_spec(host, None)
    assert any("system ssh" in note for note in spec.notes)


def test_missing_password_is_rejected_before_connecting() -> None:
    host = Host(name="h", hostname="e.com", username="u", auth_mode="credential", credential="c")
    with pytest.raises(TransportError, match="no password"):
        build_spec(host, Credential(name="c", kind="password", password=None))


def test_agent_mode_needs_no_credential() -> None:
    spec = build_spec(Host(name="h", hostname="e.com", username="u"), None)
    assert spec.allow_agent is True
    assert spec.password is None


# ------------------------------------------------------------------ transport choice


def test_default_transport_is_built_in() -> None:
    host = Host(name="h", hostname="e.com", username="u")
    assert isinstance(create_transport(host, None), ParamikoTransport)


def test_opt_in_selects_system_ssh() -> None:
    host = Host(name="h", hostname="e.com", username="u", use_system_ssh=True)
    assert isinstance(create_transport(host, None), SystemSSHTransport)


def test_system_ssh_argv_shape() -> None:
    host = Host(name="h", hostname="e.com", username="u", port=2222, use_system_ssh=True)
    transport = create_transport(host, None)
    argv = transport.argv()
    assert argv[0] == "ssh"
    assert "-p" in argv and "2222" in argv
    assert argv[-1] == "u@e.com"
    assert any("StrictHostKeyChecking" in a for a in argv)


def test_system_ssh_refuses_stored_password() -> None:
    """The whole point of the built-in client is not needing this path."""
    host = Host(
        name="h",
        hostname="e.com",
        username="u",
        use_system_ssh=True,
        auth_mode="credential",
        credential="pw",
    )
    transport = create_transport(host, password_credential())
    with pytest.raises(TransportError, match="cannot be given a stored password"):
        transport.connect(80, 24)
