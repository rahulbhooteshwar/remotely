"""Host persistence.

Hosts are keyed by ``name`` (case-insensitively) and written atomically so an
interrupted save cannot truncate the file.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable, Iterator

from . import config
from .models import Host, UNGROUPED

SCHEMA_VERSION = 1


class StoreError(RuntimeError):
    """Raised for duplicate names, missing hosts, and unreadable config."""


def atomic_write(path: Path, payload: str, mode: int = 0o600) -> None:
    """Write ``payload`` to ``path`` via a temp file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class HostStore:
    """CRUD over ``hosts.json``."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.hosts_file()
        self._hosts: list[Host] = []
        self.load()

    # ---------------------------------------------------------------- loading

    def load(self) -> None:
        if not self.path.exists():
            self._hosts = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"{self.path} is not valid JSON: {exc}") from exc
        entries = raw.get("hosts", []) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            raise StoreError(f"{self.path} does not contain a list of hosts.")
        hosts: list[Host] = []
        for entry in entries:
            if isinstance(entry, dict):
                host = Host.from_dict(entry)
                if host.name:
                    hosts.append(host)
        self._hosts = hosts

    def save(self) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "hosts": [h.to_dict() for h in self._hosts],
        }
        atomic_write(self.path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    # ---------------------------------------------------------------- reading

    def __len__(self) -> int:
        return len(self._hosts)

    def __iter__(self) -> Iterator[Host]:
        return iter(self._hosts)

    @property
    def hosts(self) -> list[Host]:
        return list(self._hosts)

    def get(self, name: str) -> Host | None:
        key = name.strip().lower()
        for host in self._hosts:
            if host.name.lower() == key:
                return host
        return None

    def require(self, name: str) -> Host:
        host = self.get(name)
        if host is None:
            raise StoreError(f"No host named {name!r}.")
        return host

    def groups(self) -> dict[str, list[Host]]:
        """Hosts bucketed by group, groups sorted with Ungrouped last."""
        buckets: dict[str, list[Host]] = {}
        for host in self._hosts:
            buckets.setdefault(host.group or UNGROUPED, []).append(host)
        for hosts in buckets.values():
            hosts.sort(key=lambda h: h.name.lower())
        return {
            name: buckets[name]
            for name in sorted(buckets, key=lambda g: (g == UNGROUPED, g.lower()))
        }

    def tags(self) -> list[str]:
        seen = {tag for host in self._hosts for tag in host.tags}
        return sorted(seen, key=str.lower)

    def group_names(self) -> list[str]:
        return list(self.groups().keys())

    def using_credential(self, credential: str) -> list[Host]:
        key = credential.strip().lower()
        return [h for h in self._hosts if (h.credential or "").lower() == key]

    # ---------------------------------------------------------------- writing

    def add(self, host: Host) -> Host:
        problems = host.validate()
        if problems:
            raise StoreError(" ".join(problems))
        if self.get(host.name) is not None:
            raise StoreError(f"A host named {host.name!r} already exists.")
        self._hosts.append(host)
        self.save()
        return host

    def update(self, original_name: str, host: Host) -> Host:
        problems = host.validate()
        if problems:
            raise StoreError(" ".join(problems))
        key = original_name.strip().lower()
        for index, existing in enumerate(self._hosts):
            if existing.name.lower() == key:
                renamed = host.name.lower() != key
                if renamed and self.get(host.name) is not None:
                    raise StoreError(f"A host named {host.name!r} already exists.")
                self._hosts[index] = host
                self.save()
                return host
        raise StoreError(f"No host named {original_name!r}.")

    def delete(self, name: str) -> Host:
        key = name.strip().lower()
        for index, existing in enumerate(self._hosts):
            if existing.name.lower() == key:
                removed = self._hosts.pop(index)
                self.save()
                return removed
        raise StoreError(f"No host named {name!r}.")

    def replace_all(self, hosts: Iterable[Host]) -> None:
        self._hosts = list(hosts)
        self.save()
