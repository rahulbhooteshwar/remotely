"""Command bar parsing, fuzzy ranking and completion."""

from __future__ import annotations

import pytest

from remotely.completion import (
    COMMANDS,
    CompletionEngine,
    find_command,
    fuzzy_score,
    parse,
    rank,
)

from .conftest import make_host

HOSTS = [
    make_host("prod-web-01", hostname="10.0.0.1", group="Production", tags=["web", "critical"]),
    make_host("prod-db-01", hostname="10.0.0.2", group="Production", tags=["db", "critical"]),
    make_host("staging-web", hostname="10.1.0.1", group="Staging", tags=["web"]),
    make_host("laptop", hostname="192.168.1.9", username="rb", group="Personal", tags=["home"]),
]


@pytest.fixture
def engine() -> CompletionEngine:
    return CompletionEngine(
        hosts=lambda: HOSTS,
        tags=lambda: ["critical", "db", "home", "web"],
        groups=lambda: ["Personal", "Production", "Staging"],
        themes=lambda: ["non-prod", "personal", "prod"],
    )


# ------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "text,mode,query",
    [
        ("", "host", ""),
        ("prod", "host", "prod"),
        ("/", "command", ""),
        ("/con", "command", "con"),
        ("@web", "tag", "web"),
        ("#Prod", "group", "Prod"),
    ],
)
def test_parse_modes(text: str, mode: str, query: str) -> None:
    parsed = parse(text)
    assert parsed.mode == mode
    assert parsed.query == query


def test_parse_command_with_argument() -> None:
    parsed = parse("/connect prod-web")
    assert parsed.mode == "command_arg"
    assert parsed.command is not None and parsed.command.name == "connect"
    assert parsed.query == "prod-web"


def test_parse_argless_command_with_trailing_text_stays_on_command() -> None:
    # /add takes no argument, so typing past it should not silently swallow it.
    parsed = parse("/add something")
    assert parsed.mode == "command"


def test_aliases_resolve() -> None:
    assert find_command("open").name == "connect"
    assert find_command("rm").name == "delete"
    assert find_command("q").name == "quit"
    assert find_command("nope") is None


# ------------------------------------------------------------------- scoring


def test_exact_beats_prefix_beats_substring() -> None:
    exact = fuzzy_score("web", "web")
    prefix = fuzzy_score("web", "web-server")
    substring = fuzzy_score("web", "my-web-thing")
    assert exact > prefix > substring


def test_word_boundary_beats_mid_word() -> None:
    boundary = fuzzy_score("db", "prod-db-01")
    middle = fuzzy_score("db", "adbc")
    assert boundary > middle


def test_subsequence_matches_but_ranks_low() -> None:
    score = fuzzy_score("pw1", "prod-web-01")
    assert score is not None
    assert score < fuzzy_score("prod", "prod-web-01")


def test_non_match_returns_none() -> None:
    assert fuzzy_score("zzz", "prod-web-01") is None


def test_shorter_candidates_win_ties() -> None:
    assert fuzzy_score("web", "web") > fuzzy_score("web", "webbbbbbbbbb")


def test_empty_query_matches_everything() -> None:
    assert fuzzy_score("", "anything") is not None


def test_rank_sorts_and_filters() -> None:
    ranked = rank("we", ["web", "well", "xyz"])
    assert [name for name, _ in ranked][:2] == ["web", "well"]
    assert "xyz" not in [name for name, _ in ranked]


# ---------------------------------------------------------------- completion


def test_host_search_matches_name(engine: CompletionEngine) -> None:
    names = [c.value for c in engine.complete("prod")]
    assert "prod-web-01" in names and "prod-db-01" in names
    assert "laptop" not in names


def test_host_search_matches_hostname(engine: CompletionEngine) -> None:
    assert "laptop" in [c.value for c in engine.complete("192.168")]


def test_host_search_matches_tag_and_group(engine: CompletionEngine) -> None:
    assert "staging-web" in [c.value for c in engine.complete("Staging")]
    assert "prod-db-01" in [c.value for c in engine.complete("db")]


def test_name_match_outranks_blob_match(engine: CompletionEngine) -> None:
    # "web" is in staging-web's name and in prod-web-01's name; both beat a host
    # that only matches through its tags.
    results = [c.value for c in engine.complete("web")]
    assert results[0] in ("staging-web", "prod-web-01")


def test_slash_lists_every_command(engine: CompletionEngine) -> None:
    values = {c.value for c in engine.complete("/")}
    assert values == {c.name for c in COMMANDS}


def test_command_completion_inserts_trailing_space_for_arg_commands(
    engine: CompletionEngine,
) -> None:
    connect = next(c for c in engine.complete("/conn") if c.value == "connect")
    assert connect.insert_text == "/connect "
    add = next(c for c in engine.complete("/add") if c.value == "add")
    assert add.insert_text == "/add"


def test_command_argument_completes_hosts(engine: CompletionEngine) -> None:
    completions = engine.complete("/connect prod")
    assert completions
    assert all(c.kind == "host" for c in completions)
    assert completions[0].insert_text.startswith("/connect ")


def test_command_argument_completes_themes(engine: CompletionEngine) -> None:
    # "pro" is a substring of non-prod and a subsequence of personal, so all
    # three match; what matters is that the exact prefix ranks first.
    completions = engine.complete("/themes pro")
    assert completions[0].value == "prod"
    assert completions[0].insert_text == "/themes prod"
    assert all(c.kind == "theme" for c in completions)


def test_tag_completion(engine: CompletionEngine) -> None:
    completions = engine.complete("@cr")
    assert [c.value for c in completions] == ["critical"]
    assert completions[0].insert_text == "@critical"
    assert "2 hosts" in completions[0].description


def test_group_completion(engine: CompletionEngine) -> None:
    completions = engine.complete("#Prod")
    assert [c.value for c in completions] == ["Production"]
    assert "2 hosts" in completions[0].description


def test_resolve_target_hosts_by_tag(engine: CompletionEngine) -> None:
    hosts = engine.resolve_target_hosts("@critical")
    assert {h.name for h in hosts} == {"prod-web-01", "prod-db-01"}


def test_resolve_target_hosts_by_group(engine: CompletionEngine) -> None:
    hosts = engine.resolve_target_hosts("#Staging")
    assert [h.name for h in hosts] == ["staging-web"]


def test_limit_is_respected() -> None:
    many = [make_host(f"host-{i:03d}") for i in range(100)]
    engine = CompletionEngine(hosts=lambda: many, limit=5)
    assert len(engine.complete("host")) == 5
