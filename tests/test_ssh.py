"""SSH command construction and secret staging."""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path

import pytest

from remotely import config
from remotely.models import Credential
from remotely.ssh import (
    SSHError,
    build_plan,
    merge_options,
    resolve_ssh_options,
    stage_secret,
    sweep_stale_secrets,
)

from .conftest import make_credential, make_host


def test_plan_has_target_and_port() -> None:
    plan = build_plan(make_host(), None)
    assert plan.argv[0] == "ssh"
    assert "-p" in plan.argv and "22" in plan.argv
    assert plan.argv[-1] == "deploy@10.0.0.5"


def test_agent_auth_stages_no_secret() -> None:
    plan = build_plan(make_host(), None)
    assert plan.secret_file is None
    assert plan.env == {}


def test_password_never_appears_in_argv() -> None:
    plan = build_plan(make_host(), make_credential(password="do-not-leak"))
    assert "do-not-leak" not in " ".join(plan.argv)
    assert "do-not-leak" not in plan.command
    assert "do-not-leak" not in " ".join(plan.env.values())


def test_password_is_staged_for_askpass() -> None:
    plan = build_plan(make_host(), make_credential(password="s3cret"))
    assert plan.secret_file is not None
    assert plan.secret_file.read_text() == "s3cret"
    assert plan.env["SSH_ASKPASS_REQUIRE"] == "force"
    assert plan.env["REMOTELY_ASKPASS_FILE"] == str(plan.secret_file)
    assert plan.env["SSH_ASKPASS"]


def test_staged_secret_is_owner_readable_only() -> None:
    path = stage_secret("top secret")
    assert path.stat().st_mode & 0o777 == 0o600
    assert config.run_dir().stat().st_mode & 0o777 == 0o700


def test_askpass_helper_consumes_the_secret_once() -> None:
    path = stage_secret("one-shot")
    os.environ["REMOTELY_ASKPASS_FILE"] = str(path)
    try:
        from remotely import askpass

        assert askpass.main() == 0
        assert not path.exists()
        # A replay finds nothing and must fail rather than emit an empty secret.
        assert askpass.main() == 1
    finally:
        os.environ.pop("REMOTELY_ASKPASS_FILE", None)


def test_sweep_removes_only_stale_secrets() -> None:
    fresh = stage_secret("fresh")
    stale = stage_secret("stale")
    old = time.time() - 10_000
    os.utime(stale, (old, old))

    removed = sweep_stale_secrets()
    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


def test_key_credential_adds_identity_file(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("PRIVATE KEY")
    plan = build_plan(
        make_host(), Credential(name="k", kind="key", key_path=str(key))
    )
    assert "-i" in plan.argv
    assert str(key) in plan.argv
    assert plan.secret_file is None  # no passphrase, nothing to stage


def test_key_passphrase_is_staged(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("PRIVATE KEY")
    plan = build_plan(
        make_host(),
        Credential(name="k", kind="key", key_path=str(key), key_passphrase="pp"),
    )
    assert plan.secret_file is not None
    assert plan.secret_file.read_text() == "pp"


def test_missing_key_file_is_reported_not_fatal(tmp_path: Path) -> None:
    plan = build_plan(
        make_host(), Credential(name="k", kind="key", key_path=str(tmp_path / "nope"))
    )
    assert any("not found" in note for note in plan.notes)


def test_password_credential_without_password_is_rejected() -> None:
    cred = Credential(name="broken", kind="password", password=None)
    with pytest.raises(SSHError, match="no password"):
        build_plan(make_host(), cred)


def test_credential_username_overrides_host() -> None:
    plan = build_plan(make_host(), make_credential(username="svc-account"))
    assert plan.argv[-1] == "svc-account@10.0.0.5"


def test_default_options_applied_when_unset() -> None:
    options = resolve_ssh_options(make_host(ssh_options=None), None)
    assert any(o.startswith("StrictHostKeyChecking") for o in options)


def test_empty_option_list_means_no_options() -> None:
    assert resolve_ssh_options(make_host(ssh_options=[]), None) == []


def test_password_auth_disables_pubkey() -> None:
    options = resolve_ssh_options(make_host(ssh_options=None), make_credential())
    assert "PubkeyAuthentication=no" in options


def test_host_options_win_over_auth_derived() -> None:
    host = make_host(ssh_options=["PubkeyAuthentication=yes"])
    options = resolve_ssh_options(host, make_credential())
    assert "PubkeyAuthentication=yes" in options
    assert "PubkeyAuthentication=no" not in options


def test_merge_options_dedupes_by_key() -> None:
    merged = merge_options(["A=1", "B=1"], ["A=2"])
    assert merged == ["A=2", "B=1"]


def test_command_is_shell_quoted() -> None:
    # The whole argument is quoted as one token, which is what protects the
    # command when tmux hands it to a shell.
    plan = build_plan(make_host(hostname="host with space"), None)
    assert "'deploy@host with space'" in plan.command
    assert shlex.split(plan.command)[-1] == "deploy@host with space"
