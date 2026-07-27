"""Host and credential import/export.

Exports are plain JSON. Secrets are excluded unless explicitly asked for, and an
export that does contain them is written ``0600`` and reports that fact back to
the caller so the UI can warn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Credential, Host
from .store import HostStore, atomic_write
from .vault import Vault

EXPORT_VERSION = 1


class TransferError(RuntimeError):
    """Raised for unreadable or malformed transfer files."""


@dataclass(slots=True)
class ImportResult:
    added: list[str]
    updated: list[str]
    skipped: list[str]
    credentials: list[str]

    @property
    def total(self) -> int:
        return len(self.added) + len(self.updated)

    def summary(self) -> str:
        bits = [f"{len(self.added)} added", f"{len(self.updated)} updated"]
        if self.skipped:
            bits.append(f"{len(self.skipped)} skipped")
        if self.credentials:
            bits.append(f"{len(self.credentials)} credentials")
        return ", ".join(bits)


def export_hosts(
    store: HostStore,
    path: Path,
    *,
    vault: Vault | None = None,
    include_secrets: bool = False,
) -> tuple[Path, bool]:
    """Write hosts to ``path``. Returns the path and whether secrets went with it."""
    payload: dict[str, Any] = {
        "version": EXPORT_VERSION,
        "kind": "remotely-export",
        "hosts": [host.to_dict() for host in store],
    }

    secrets_written = False
    if include_secrets:
        if vault is None or vault.is_locked:
            raise TransferError("Unlock the vault before exporting secrets.")
        payload["credentials"] = [cred.to_dict() for cred in vault.list()]
        secrets_written = bool(payload["credentials"])
    elif vault is not None and not vault.is_locked:
        # Ship the credential shells so an import knows what to ask for.
        payload["credentials"] = [cred.redacted() for cred in vault.list()]

    target = path.expanduser()
    atomic_write(
        target,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        mode=0o600 if secrets_written else 0o644,
    )
    return target, secrets_written


def _load(path: Path) -> dict[str, Any]:
    target = path.expanduser()
    if not target.exists():
        raise TransferError(f"{target} does not exist.")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TransferError(f"{target} is not valid JSON: {exc}") from exc
    if isinstance(raw, list):
        return {"hosts": raw}
    if not isinstance(raw, dict):
        raise TransferError(f"{target} does not contain an object or a list.")
    return raw


def import_hosts(
    store: HostStore,
    path: Path,
    *,
    vault: Vault | None = None,
    replace: bool = False,
    overwrite: bool = False,
) -> ImportResult:
    """Merge hosts from ``path`` into ``store``.

    ``replace`` swaps the whole host list. Otherwise existing names are skipped
    unless ``overwrite`` is set.
    """
    raw = _load(path)
    entries = raw.get("hosts")
    if not isinstance(entries, list):
        raise TransferError("No 'hosts' list found in the file.")

    incoming: list[Host] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        host = Host.from_dict(entry)
        problems = host.validate()
        if problems:
            raise TransferError(f"Host {host.name or '<unnamed>'!r}: {' '.join(problems)}")
        incoming.append(host)

    added: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []

    if replace:
        store.replace_all(incoming)
        added = [h.name for h in incoming]
    else:
        for host in incoming:
            existing = store.get(host.name)
            if existing is None:
                store.add(host)
                added.append(host.name)
            elif overwrite:
                store.update(existing.name, host)
                updated.append(host.name)
            else:
                skipped.append(host.name)

    imported_credentials: list[str] = []
    credential_entries = raw.get("credentials")
    if isinstance(credential_entries, list) and vault is not None and not vault.is_locked:
        for entry in credential_entries:
            if not isinstance(entry, dict):
                continue
            # Redacted exports carry no secret material; nothing to import.
            if not entry.get("password") and not entry.get("key_path"):
                continue
            credential = Credential.from_dict(entry)
            if credential.validate():
                continue
            if vault.get(credential.name) is not None and not overwrite:
                continue
            vault.put(credential)
            imported_credentials.append(credential.name)

    return ImportResult(added, updated, skipped, imported_credentials)
