"""Theme discovery, overriding and pane styling."""

from __future__ import annotations

from remotely import config
from remotely.themes import Theme, ThemeRegistry

BUNDLED = {"prod", "non-prod", "personal"}


def test_bundled_themes_are_discovered(themes: ThemeRegistry) -> None:
    assert BUNDLED.issubset(set(themes.names()))
    assert themes.errors == []


def test_bundled_themes_are_complete(themes: ThemeRegistry) -> None:
    for name in BUNDLED:
        theme = themes.get(name)
        assert theme.description
        assert theme.accent.startswith("#")
        assert theme.icon
        assert theme.pane_styles()


def test_user_theme_is_picked_up() -> None:
    (config.themes_dir() / "staging.toml").write_text(
        'name = "staging"\n[colors]\naccent = "#5f87d7"\n'
    )
    registry = ThemeRegistry()
    assert "staging" in registry.names()
    assert registry.get("staging").accent == "#5f87d7"
    assert registry.get("staging").builtin is False


def test_user_theme_overrides_bundled_by_name() -> None:
    (config.themes_dir() / "prod.toml").write_text(
        'name = "prod"\n[colors]\naccent = "#00ff00"\n'
    )
    registry = ThemeRegistry()
    theme = registry.get("prod")
    assert theme.accent == "#00ff00"
    assert theme.builtin is False
    # Overriding must not duplicate the entry.
    assert registry.names().count("prod") == 1


def test_unknown_theme_falls_back_to_personal(themes: ThemeRegistry) -> None:
    assert themes.get("does-not-exist").name == "personal"
    assert themes.get(None).name == "personal"


def test_malformed_theme_is_reported_not_fatal() -> None:
    (config.themes_dir() / "broken.toml").write_text("name = = =")
    registry = ThemeRegistry()
    assert BUNDLED.issubset(set(registry.names()))
    assert any("broken.toml" in error for error in registry.errors)


def test_pane_styles_come_from_colors() -> None:
    theme = Theme(
        name="t",
        colors={"background": "#101010", "foreground": "#eeeeee", "accent": "#ff0000"},
    )
    styles = theme.pane_styles()
    assert styles["background"] == "#101010"
    assert styles["color"] == "#eeeeee"
    assert styles["border_color"] == "#ff0000"


def test_terminal_section_overrides_colors() -> None:
    theme = Theme(
        name="t",
        colors={"background": "#101010", "accent": "#ff0000"},
        terminal={"background": "#000000", "border": "#00ff00"},
    )
    styles = theme.pane_styles()
    assert styles["background"] == "#000000"
    assert styles["border_color"] == "#00ff00"


def test_pane_styles_have_sane_defaults() -> None:
    styles = Theme(name="bare").pane_styles()
    assert all(value.startswith("#") for value in styles.values())


def test_bundled_themes_define_distinct_pane_colours(themes: ThemeRegistry) -> None:
    """Telling prod from non-prod at a glance is the whole point."""
    backgrounds = {themes.get(n).pane_styles()["background"] for n in BUNDLED}
    assert len(backgrounds) == len(BUNDLED)


def test_tab_title_uses_format(themes: ThemeRegistry) -> None:
    assert themes.get("prod").tab_title("web-1") == "🔴 web-1"


def test_tab_title_survives_bad_template() -> None:
    theme = Theme(name="t", tab={"format": "{nope}"})
    assert "web-1" in theme.tab_title("web-1")


def test_tab_title_falls_back_to_ascii(themes: ThemeRegistry) -> None:
    # A terminal that cannot render the icon should still get a usable marker.
    assert themes.get("prod").tab_title("web-1", unicode_ok=False) == "[!] web-1"
    assert themes.get("non-prod").tab_title("web-1", unicode_ok=False) == "[~] web-1"


def test_every_bundled_theme_has_an_ascii_icon(themes: ThemeRegistry) -> None:
    for name in BUNDLED:
        theme = themes.get(name)
        title = theme.tab_title("h", unicode_ok=False)
        assert title.isascii(), f"{name} produced non-ascii fallback {title!r}"


def test_ascii_icon_defaults_when_absent() -> None:
    assert Theme(name="t").tab_title("h", unicode_ok=False) == "* h"


def test_remote_prompt_command_included_when_configured(themes: ThemeRegistry) -> None:
    command = themes.get("prod").remote_prompt_command()
    assert command is not None and "PS1" in command and "PROD" in command


def test_remote_prompt_absent_without_colour() -> None:
    assert Theme(name="t").remote_prompt_command() is None


def test_clone_creates_editable_copy(themes: ThemeRegistry) -> None:
    path = themes.copy_to_user_dir("prod", "prod-eu")
    assert path.exists()
    assert path.parent == config.themes_dir()
    assert 'name = "prod-eu"' in path.read_text()

    reloaded = ThemeRegistry()
    assert "prod-eu" in reloaded.names()
    assert reloaded.get("prod-eu").builtin is False
