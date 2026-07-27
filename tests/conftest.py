"""Shared fixtures.

Every test runs against a throwaway ``REMOTELY_HOME`` so a test can never read
or clobber a real user's hosts, vault or themes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from remotely import config
from remotely.models import Credential, Host
from remotely.store import HostStore
from remotely.themes import ThemeRegistry
from remotely.vault import Vault

PASSCODE = "correct horse battery staple"


@pytest.fixture(autouse=True)
def remotely_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "remotely-home"
    monkeypatch.setenv(config.ENV_HOME, str(root))
    config.ensure_layout()
    return root


@pytest.fixture
def store() -> HostStore:
    return HostStore()


@pytest.fixture
def vault() -> Vault:
    vault = Vault()
    vault.initialise(PASSCODE)
    return vault


@pytest.fixture
def themes() -> ThemeRegistry:
    return ThemeRegistry()


def make_host(name: str = "web-1", **overrides) -> Host:
    defaults = dict(
        name=name,
        hostname="10.0.0.5",
        username="deploy",
        port=22,
        group="Production",
        tags=["web", "critical"],
        theme="prod",
    )
    defaults.update(overrides)
    return Host(**defaults)


def make_credential(name: str = "ldap", **overrides) -> Credential:
    defaults = dict(name=name, kind="password", password="s3cret")
    defaults.update(overrides)
    return Credential(**defaults)
