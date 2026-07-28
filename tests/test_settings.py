"""Preferences: defaults, persistence, and tolerance of a bad file."""

from __future__ import annotations

import json

from remotely import config, settings as settings_module
from remotely.settings import LABELS, Settings


def test_defaults_are_protective() -> None:
    """A destructive action should ask by default."""
    fresh = Settings()
    assert fresh.confirm_quit is True
    assert fresh.confirm_close_tab is True


def test_missing_file_yields_defaults() -> None:
    assert not config.settings_file().exists()
    assert settings_module.load() == Settings()


def test_roundtrip() -> None:
    saved = Settings(confirm_quit=False, confirm_close_tab=True)
    settings_module.save(saved)
    assert settings_module.load() == saved


def test_saved_file_is_owner_only() -> None:
    settings_module.save(Settings())
    assert config.settings_file().stat().st_mode & 0o777 == 0o600


def test_corrupt_file_falls_back_instead_of_raising() -> None:
    """A broken preference file must never stop the app starting."""
    config.settings_file().write_text("{not json")
    assert settings_module.load() == Settings()


def test_non_object_file_falls_back() -> None:
    config.settings_file().write_text("[1, 2, 3]")
    assert settings_module.load() == Settings()


def test_unknown_keys_are_ignored() -> None:
    """An older binary reading a newer file must not crash."""
    config.settings_file().write_text(
        json.dumps({"confirm_quit": False, "future_option": "hello"})
    )
    loaded = settings_module.load()
    assert loaded.confirm_quit is False
    assert loaded.confirm_close_tab is True


def test_partial_file_keeps_defaults_for_the_rest() -> None:
    config.settings_file().write_text(json.dumps({"confirm_close_tab": False}))
    loaded = settings_module.load()
    assert loaded.confirm_close_tab is False
    assert loaded.confirm_quit is True


def test_toggle_flips_and_returns_the_new_value() -> None:
    s = Settings()
    assert s.toggle("confirm_quit") is False
    assert s.confirm_quit is False
    assert s.toggle("confirm_quit") is True


def test_toggle_rejects_an_unknown_name() -> None:
    import pytest

    with pytest.raises(KeyError):
        Settings().toggle("nope")


def test_every_field_has_a_label() -> None:
    """The /settings list is built from LABELS; a field missing one is invisible."""
    from dataclasses import fields

    names = {f.name for f in fields(Settings)}
    assert names == set(LABELS), "LABELS and Settings fields are out of step"
    for label, help_text in LABELS.values():
        assert label and help_text
