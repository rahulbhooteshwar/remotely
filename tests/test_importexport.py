"""Import and export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from remotely.importexport import TransferError, export_hosts, import_hosts
from remotely.store import HostStore
from remotely.vault import Vault

from .conftest import make_credential, make_host


def test_export_excludes_secrets_by_default(
    store: HostStore, vault: Vault, tmp_path: Path
) -> None:
    store.add(make_host())
    vault.put(make_credential(password="do-not-leak"))

    path, wrote_secrets = export_hosts(store, tmp_path / "out.json", vault=vault)
    assert wrote_secrets is False
    body = path.read_text()
    assert "do-not-leak" not in body
    # The credential shell still travels so an import knows what to ask for.
    assert "ldap" in body


def test_export_with_secrets_is_opt_in_and_locked_down(
    store: HostStore, vault: Vault, tmp_path: Path
) -> None:
    store.add(make_host())
    vault.put(make_credential(password="travels-with-me"))

    path, wrote_secrets = export_hosts(
        store, tmp_path / "full.json", vault=vault, include_secrets=True
    )
    assert wrote_secrets is True
    assert "travels-with-me" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_export_with_secrets_requires_unlocked_vault(
    store: HostStore, vault: Vault, tmp_path: Path
) -> None:
    vault.lock()
    with pytest.raises(TransferError, match="Unlock"):
        export_hosts(store, tmp_path / "x.json", vault=vault, include_secrets=True)


def test_roundtrip(store: HostStore, tmp_path: Path) -> None:
    store.add(make_host("a", group="G1"))
    store.add(make_host("b", group="G2", ssh_options=[]))
    path, _ = export_hosts(store, tmp_path / "hosts.json")

    store.replace_all([])
    result = import_hosts(store, path)

    assert result.total == 2
    assert store.require("a").group == "G1"
    assert store.require("b").ssh_options == []


def test_import_skips_existing_by_default(store: HostStore, tmp_path: Path) -> None:
    store.add(make_host("a", port=22))
    path, _ = export_hosts(store, tmp_path / "hosts.json")
    store.update("a", make_host("a", port=2222))

    result = import_hosts(store, path)
    assert result.skipped == ["a"]
    assert store.require("a").port == 2222  # untouched


def test_import_overwrite(store: HostStore, tmp_path: Path) -> None:
    store.add(make_host("a", port=22))
    path, _ = export_hosts(store, tmp_path / "hosts.json")
    store.update("a", make_host("a", port=2222))

    result = import_hosts(store, path, overwrite=True)
    assert result.updated == ["a"]
    assert store.require("a").port == 22


def test_import_replace_discards_everything_else(store: HostStore, tmp_path: Path) -> None:
    store.add(make_host("keep"))
    path, _ = export_hosts(store, tmp_path / "hosts.json")
    store.add(make_host("extra"))

    import_hosts(store, path, replace=True)
    assert [h.name for h in store] == ["keep"]


def test_import_credentials_when_present(
    store: HostStore, vault: Vault, tmp_path: Path
) -> None:
    vault.put(make_credential(password="shared"))
    path, _ = export_hosts(store, tmp_path / "full.json", vault=vault, include_secrets=True)

    vault.delete("ldap")
    result = import_hosts(store, path, vault=vault)
    assert result.credentials == ["ldap"]
    assert vault.require("ldap").password == "shared"


def test_redacted_credentials_are_not_imported(
    store: HostStore, vault: Vault, tmp_path: Path
) -> None:
    vault.put(make_credential(password="secret"))
    path, _ = export_hosts(store, tmp_path / "redacted.json", vault=vault)

    vault.delete("ldap")
    result = import_hosts(store, path, vault=vault)
    assert result.credentials == []
    assert vault.get("ldap") is None


def test_import_rejects_invalid_host(store: HostStore, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"hosts": [{"name": "x", "hostname": "", "username": "u"}]}))
    with pytest.raises(TransferError):
        import_hosts(store, bad)


def test_import_rejects_missing_file(store: HostStore, tmp_path: Path) -> None:
    with pytest.raises(TransferError, match="does not exist"):
        import_hosts(store, tmp_path / "nope.json")


def test_import_rejects_garbage(store: HostStore, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(TransferError, match="not valid JSON"):
        import_hosts(store, bad)


def test_import_accepts_bare_list(store: HostStore, tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text(json.dumps([{"name": "x", "hostname": "h", "username": "u"}]))
    assert import_hosts(store, path).total == 1
