"""Encrypted credential vault.

Replaces the macOS Keychain dependency with a portable, app-managed store: one
file, one passcode, AES-256-GCM over a scrypt-derived key.

File format is a JSON envelope so the KDF parameters stay readable (and
upgradable) while the body remains opaque::

    {"version": 1, "kdf": {"name": "scrypt", "n": 32768, ...},
     "salt": "<b64>", "nonce": "<b64>", "ciphertext": "<b64>"}

The ciphertext authenticates the envelope header via AAD, so tampering with the
KDF parameters fails the tag check rather than silently weakening the vault.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import config
from .models import Credential
from .store import atomic_write

VAULT_VERSION = 1
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32

# ~64 MB and roughly 100 ms on a modern laptop. Interactive-login territory:
# high enough to make offline guessing expensive, low enough that unlocking the
# TUI does not feel stalled.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1


class VaultError(RuntimeError):
    """Base class for vault problems."""


class VaultLocked(VaultError):
    """Raised when an operation needs a passcode that has not been supplied."""


class InvalidPasscode(VaultError):
    """Raised when decryption fails the authentication tag check."""


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


@dataclass(frozen=True, slots=True)
class KdfParams:
    name: str = "scrypt"
    n: int = SCRYPT_N
    r: int = SCRYPT_R
    p: int = SCRYPT_P

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "n": self.n, "r": self.r, "p": self.p}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "KdfParams":
        if raw.get("name", "scrypt") != "scrypt":
            raise VaultError(f"Unsupported KDF {raw.get('name')!r}.")
        return cls(n=int(raw["n"]), r=int(raw["r"]), p=int(raw["p"]))

    def derive(self, passcode: str, salt: bytes) -> bytes:
        kdf = Scrypt(salt=salt, length=KEY_BYTES, n=self.n, r=self.r, p=self.p)
        return kdf.derive(passcode.encode("utf-8"))


class Vault:
    """Named credentials behind a single passcode.

    Instances start locked. Call :meth:`unlock` (existing vault) or
    :meth:`initialise` (first run) before touching credentials.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.vault_file()
        self._key: bytes | None = None
        self._kdf = KdfParams()
        self._salt: bytes | None = None
        self._credentials: dict[str, Credential] = {}

    # --------------------------------------------------------------- lifecycle

    def exists(self) -> bool:
        return self.path.exists()

    @property
    def is_locked(self) -> bool:
        return self._key is None

    def initialise(self, passcode: str) -> None:
        """Create a brand new empty vault protected by ``passcode``."""
        if self.exists():
            raise VaultError(f"A vault already exists at {self.path}.")
        if not passcode:
            raise VaultError("Passcode must not be empty.")
        self._kdf = KdfParams()
        self._salt = secrets.token_bytes(SALT_BYTES)
        self._key = self._kdf.derive(passcode, self._salt)
        self._credentials = {}
        self._flush()

    def unlock(self, passcode: str) -> None:
        """Decrypt the vault into memory. Raises :class:`InvalidPasscode`."""
        if not self.exists():
            raise VaultError(f"No vault at {self.path}. Create one first.")
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise VaultError(f"Vault file is corrupt: {exc}") from exc

        if envelope.get("version") != VAULT_VERSION:
            raise VaultError(f"Unsupported vault version {envelope.get('version')!r}.")

        kdf = KdfParams.from_dict(envelope["kdf"])
        salt = _b64d(envelope["salt"])
        nonce = _b64d(envelope["nonce"])
        ciphertext = _b64d(envelope["ciphertext"])

        key = kdf.derive(passcode, salt)
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, self._aad(envelope))
        except InvalidTag as exc:
            raise InvalidPasscode("Incorrect passcode.") from exc

        body = json.loads(plaintext.decode("utf-8"))
        self._kdf = kdf
        self._salt = salt
        self._key = key
        self._credentials = {
            cred.name.lower(): cred
            for cred in (Credential.from_dict(c) for c in body.get("credentials", []))
            if cred.name
        }

    def lock(self) -> None:
        """Drop the derived key and all plaintext secrets from memory."""
        self._key = None
        self._credentials = {}

    def change_passcode(self, current: str, new: str) -> None:
        if not new:
            raise VaultError("New passcode must not be empty.")
        self.unlock(current)
        self._kdf = KdfParams()
        self._salt = secrets.token_bytes(SALT_BYTES)
        self._key = self._kdf.derive(new, self._salt)
        self._flush()

    # ---------------------------------------------------------------- internals

    @staticmethod
    def _aad(envelope: dict[str, Any]) -> bytes:
        """Additional authenticated data: the non-secret header fields."""
        header = {
            "version": envelope["version"],
            "kdf": envelope["kdf"],
            "salt": envelope["salt"],
        }
        return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _require_key(self) -> bytes:
        if self._key is None or self._salt is None:
            raise VaultLocked("Vault is locked. Unlock it with your passcode first.")
        return self._key

    def _flush(self) -> None:
        key = self._require_key()
        assert self._salt is not None
        body = json.dumps(
            {"credentials": [c.to_dict() for c in self._credentials.values()]},
            ensure_ascii=False,
        ).encode("utf-8")

        nonce = os.urandom(NONCE_BYTES)
        envelope: dict[str, Any] = {
            "version": VAULT_VERSION,
            "kdf": self._kdf.to_dict(),
            "salt": _b64e(self._salt),
        }
        ciphertext = AESGCM(key).encrypt(nonce, body, self._aad(envelope))
        envelope["nonce"] = _b64e(nonce)
        envelope["ciphertext"] = _b64e(ciphertext)
        atomic_write(self.path, json.dumps(envelope, indent=2) + "\n", mode=0o600)

    # -------------------------------------------------------------- credentials

    def __len__(self) -> int:
        return len(self._credentials)

    def __iter__(self) -> Iterator[Credential]:
        return iter(self.list())

    def list(self) -> list[Credential]:
        self._require_key()
        return sorted(self._credentials.values(), key=lambda c: c.name.lower())

    def names(self) -> list[str]:
        return [c.name for c in self.list()]

    def get(self, name: str) -> Credential | None:
        self._require_key()
        return self._credentials.get(name.strip().lower())

    def require(self, name: str) -> Credential:
        cred = self.get(name)
        if cred is None:
            raise VaultError(f"No credential named {name!r}.")
        return cred

    def put(self, credential: Credential) -> Credential:
        """Insert or replace a credential by name."""
        self._require_key()
        problems = credential.validate()
        if problems:
            raise VaultError(" ".join(problems))
        self._credentials[credential.name.strip().lower()] = credential
        self._flush()
        return credential

    def rename(self, old: str, new: str) -> Credential:
        self._require_key()
        cred = self.require(old)
        if self.get(new) is not None and old.strip().lower() != new.strip().lower():
            raise VaultError(f"A credential named {new!r} already exists.")
        del self._credentials[old.strip().lower()]
        cred.name = new.strip()
        self._credentials[cred.name.lower()] = cred
        self._flush()
        return cred

    def delete(self, name: str) -> Credential:
        self._require_key()
        key = name.strip().lower()
        if key not in self._credentials:
            raise VaultError(f"No credential named {name!r}.")
        removed = self._credentials.pop(key)
        self._flush()
        return removed
