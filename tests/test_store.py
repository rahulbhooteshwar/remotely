"""Host store: CRUD, grouping, persistence."""

from __future__ import annotations

import json

import pytest

from remotely import config
from remotely.models import Host, UNGROUPED
from remotely.store import HostStore, StoreError

from .conftest import make_host


def test_add_and_reload(store: HostStore) -> None:
    store.add(make_host())
    assert len(store) == 1

    reloaded = HostStore()
    host = reloaded.require("web-1")
    assert host.hostname == "10.0.0.5"
    assert host.tags == ["web", "critical"]


def test_hosts_file_contains_no_secrets(store: HostStore) -> None:
    store.add(make_host(auth_mode="credential", credential="ldap"))
    raw = config.hosts_file().read_text()
    assert "ldap" in raw  # the reference is fine
    assert "password" not in raw


def test_duplicate_name_rejected(store: HostStore) -> None:
    store.add(make_host())
    with pytest.raises(StoreError, match="already exists"):
        store.add(make_host())


def test_name_lookup_is_case_insensitive(store: HostStore) -> None:
    store.add(make_host("Web-1"))
    assert store.get("web-1") is not None
    assert store.get("WEB-1") is not None


def test_update_can_rename(store: HostStore) -> None:
    store.add(make_host())
    store.update("web-1", make_host("web-2"))
    assert store.get("web-1") is None
    assert store.require("web-2").name == "web-2"


def test_update_rejects_rename_onto_existing(store: HostStore) -> None:
    store.add(make_host("a"))
    store.add(make_host("b"))
    with pytest.raises(StoreError, match="already exists"):
        store.update("a", make_host("b"))


def test_update_in_place_keeps_same_name(store: HostStore) -> None:
    store.add(make_host())
    store.update("web-1", make_host("web-1", port=2222))
    assert store.require("web-1").port == 2222


def test_delete(store: HostStore) -> None:
    store.add(make_host())
    store.delete("web-1")
    assert len(store) == 0
    with pytest.raises(StoreError):
        store.delete("web-1")


def test_groups_sorted_with_ungrouped_last(store: HostStore) -> None:
    store.add(make_host("z-host", group="Zebra"))
    store.add(make_host("a-host", group="Alpha"))
    store.add(make_host("loose", group=""))
    assert list(store.groups()) == ["Alpha", "Zebra", UNGROUPED]


def test_hosts_within_a_group_are_sorted(store: HostStore) -> None:
    store.add(make_host("beta", group="G"))
    store.add(make_host("alpha", group="G"))
    assert [h.name for h in store.groups()["G"]] == ["alpha", "beta"]


def test_tags_are_deduplicated_and_sorted(store: HostStore) -> None:
    store.add(make_host("a", tags=["web", "db"]))
    store.add(make_host("b", tags=["web", "cache"]))
    assert store.tags() == ["cache", "db", "web"]


def test_using_credential(store: HostStore) -> None:
    store.add(make_host("a", auth_mode="credential", credential="ldap"))
    store.add(make_host("b", auth_mode="credential", credential="LDAP"))
    store.add(make_host("c"))
    assert {h.name for h in store.using_credential("ldap")} == {"a", "b"}


def test_validation_rejects_bad_hosts(store: HostStore) -> None:
    with pytest.raises(StoreError):
        store.add(Host(name="", hostname="h", username="u"))
    with pytest.raises(StoreError):
        store.add(Host(name="n", hostname="", username="u"))
    with pytest.raises(StoreError):
        store.add(Host(name="n", hostname="h", username="u", port=0))


def test_credential_auth_requires_a_credential(store: HostStore) -> None:
    with pytest.raises(StoreError, match="credential"):
        store.add(make_host(auth_mode="credential", credential=None))


def test_agent_mode_clears_stale_credential() -> None:
    host = Host(name="n", hostname="h", username="u", auth_mode="agent", credential="ldap")
    assert host.credential is None


def test_ssh_options_none_and_empty_are_distinct(store: HostStore) -> None:
    store.add(make_host("defaults", ssh_options=None))
    store.add(make_host("explicit-none", ssh_options=[]))
    reloaded = HostStore()
    assert reloaded.require("defaults").ssh_options is None
    assert reloaded.require("explicit-none").ssh_options == []


def test_corrupt_config_raises_clearly() -> None:
    config.hosts_file().write_text("{not json")
    with pytest.raises(StoreError, match="not valid JSON"):
        HostStore()


def test_bare_list_config_is_accepted() -> None:
    config.hosts_file().write_text(
        json.dumps([{"name": "x", "hostname": "h", "username": "u"}])
    )
    assert HostStore().require("x").hostname == "h"


def test_save_is_atomic_and_leaves_no_temp_files(store: HostStore) -> None:
    store.add(make_host())
    leftovers = [p.name for p in config.home().iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
