"""Credential vault: encryption, passcode handling, CRUD."""

from __future__ import annotations

import json

import pytest

from remotely import config
from remotely.models import Credential
from remotely.vault import InvalidPasscode, Vault, VaultError, VaultLocked

from .conftest import PASSCODE, make_credential


def test_initialise_creates_encrypted_file(vault: Vault) -> None:
    assert vault.exists()
    assert not vault.is_locked
    envelope = json.loads(config.vault_file().read_text())
    assert envelope["version"] == 1
    assert envelope["kdf"]["name"] == "scrypt"
    assert "ciphertext" in envelope and "nonce" in envelope


def test_vault_file_is_owner_only(vault: Vault) -> None:
    vault.put(make_credential())
    mode = config.vault_file().stat().st_mode & 0o777
    assert mode == 0o600


def test_secret_is_not_recoverable_from_disk(vault: Vault) -> None:
    vault.put(make_credential(password="hunter2-unmistakable"))
    blob = config.vault_file().read_bytes()
    assert b"hunter2-unmistakable" not in blob


def test_roundtrip_after_relock(vault: Vault) -> None:
    vault.put(make_credential(username="svc", description="Shared LDAP login"))
    vault.lock()
    assert vault.is_locked

    reopened = Vault()
    reopened.unlock(PASSCODE)
    cred = reopened.require("ldap")
    assert cred.password == "s3cret"
    assert cred.username == "svc"
    assert cred.description == "Shared LDAP login"


def test_wrong_passcode_rejected(vault: Vault) -> None:
    vault.put(make_credential())
    other = Vault()
    with pytest.raises(InvalidPasscode):
        other.unlock("not the passcode")


def test_locked_vault_refuses_access(vault: Vault) -> None:
    vault.put(make_credential())
    vault.lock()
    with pytest.raises(VaultLocked):
        vault.list()
    with pytest.raises(VaultLocked):
        vault.put(make_credential("other"))


def test_tampering_with_kdf_params_is_detected(vault: Vault) -> None:
    vault.put(make_credential())
    path = config.vault_file()
    envelope = json.loads(path.read_text())
    # Weakening the KDF must not yield a readable vault: the header is
    # authenticated, so the tag check fails before any decryption happens.
    envelope["kdf"]["n"] = 1024
    path.write_text(json.dumps(envelope))

    with pytest.raises((InvalidPasscode, VaultError)):
        Vault().unlock(PASSCODE)


def test_change_passcode(vault: Vault) -> None:
    vault.put(make_credential())
    vault.change_passcode(PASSCODE, "a whole new passcode")

    with pytest.raises(InvalidPasscode):
        Vault().unlock(PASSCODE)

    reopened = Vault()
    reopened.unlock("a whole new passcode")
    assert reopened.require("ldap").password == "s3cret"


def test_change_passcode_requires_the_current_one(vault: Vault) -> None:
    with pytest.raises(InvalidPasscode):
        vault.change_passcode("wrong", "new")


def test_rename_and_delete(vault: Vault) -> None:
    vault.put(make_credential())
    vault.rename("ldap", "corp-ldap")
    assert vault.get("ldap") is None
    assert vault.require("corp-ldap").password == "s3cret"

    vault.delete("corp-ldap")
    assert vault.names() == []


def test_lookup_is_case_insensitive(vault: Vault) -> None:
    vault.put(make_credential("LDAP"))
    assert vault.get("ldap") is not None
    assert vault.get("LdAp") is not None


def test_put_rejects_incomplete_credential(vault: Vault) -> None:
    with pytest.raises(VaultError):
        vault.put(Credential(name="empty", kind="password", password=None))
    with pytest.raises(VaultError):
        vault.put(Credential(name="nokey", kind="key", key_path=None))


def test_key_credential_roundtrip(vault: Vault) -> None:
    vault.put(
        Credential(
            name="deploy-key",
            kind="key",
            key_path="~/.ssh/id_ed25519",
            key_passphrase="pp",
        )
    )
    vault.lock()
    reopened = Vault()
    reopened.unlock(PASSCODE)
    cred = reopened.require("deploy-key")
    assert cred.kind == "key"
    assert cred.key_passphrase == "pp"


def test_cannot_initialise_over_existing_vault(vault: Vault) -> None:
    with pytest.raises(VaultError):
        Vault().initialise("another")


def test_redacted_omits_secrets() -> None:
    cred = make_credential(username="svc")
    redacted = cred.redacted()
    assert redacted["has_password"] is True
    assert "password" not in redacted
    assert redacted["username"] == "svc"
