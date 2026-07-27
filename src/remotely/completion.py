"""Completion engine for the command bar.

One input field does everything, the way Claude Code's prompt does. What you
type decides what gets completed:

    /...    a command, then that command's argument
    @...    a tag
    #...    a group
    else    fuzzy search across every saved host

This module is deliberately free of Textual imports so the behaviour can be
tested without standing up an app.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Literal, Sequence

from .models import Host

Kind = Literal["command", "host", "tag", "group", "theme", "path"]
ArgType = Literal["none", "host", "theme", "path"]


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    summary: str
    arg_type: ArgType = "none"
    arg_label: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def usage(self) -> str:
        return f"/{self.name} {self.arg_label}".strip()


COMMANDS: tuple[Command, ...] = (
    Command("connect", "Launch a host in a new tab", "host", "<host>", ("open", "go")),
    Command("add", "Add a new host"),
    Command("edit", "Edit a host", "host", "<host>"),
    Command("delete", "Delete a host", "host", "<host>", ("rm",)),
    Command("sessions", "List and switch to open tabs", aliases=("tabs",)),
    Command("themes", "Browse, preview and clone themes", "theme", "[theme]"),
    Command("vault", "Manage stored credentials", aliases=("creds",)),
    Command("lock", "Lock the credential vault"),
    Command("export", "Export hosts to a file", "path", "<path>"),
    Command("import", "Import hosts from a file", "path", "<path>"),
    Command("help", "Show help"),
    Command("quit", "Exit Remotely", aliases=("exit", "q")),
)

_BY_NAME: dict[str, Command] = {}
for _command in COMMANDS:
    _BY_NAME[_command.name] = _command
    for _alias in _command.aliases:
        _BY_NAME[_alias] = _command


def find_command(name: str) -> Command | None:
    return _BY_NAME.get(name.strip().lower())


@dataclass(frozen=True, slots=True)
class Completion:
    """One row in the suggestion list."""

    value: str
    label: str
    kind: Kind
    description: str = ""
    #: Text that replaces the whole input when accepted.
    insert: str = ""
    score: int = 0
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def insert_text(self) -> str:
        return self.insert or self.value


# --------------------------------------------------------------------- scoring


def fuzzy_score(query: str, candidate: str) -> int | None:
    """Score ``candidate`` against ``query``; ``None`` when it does not match.

    Ranks exact > prefix > word-boundary > substring > subsequence, then prefers
    shorter candidates so "web" ranks "web" above "web-backup-replica".
    """
    if not query:
        return 1
    query = query.lower()
    candidate_lower = candidate.lower()

    if query == candidate_lower:
        base = 1000
    elif candidate_lower.startswith(query):
        base = 900
    elif any(
        part.startswith(query)
        for part in candidate_lower.replace("_", " ").replace("-", " ").replace(".", " ").split()
    ):
        base = 800
    elif query in candidate_lower:
        base = 700
    else:
        gaps = _subsequence_gaps(query, candidate_lower)
        if gaps is None:
            return None
        base = max(300, 600 - gaps * 10)

    return base - min(len(candidate_lower), 90)


def _subsequence_gaps(query: str, candidate: str) -> int | None:
    """Number of skipped characters when matching query as a subsequence."""
    position = 0
    gaps = 0
    for char in query:
        found = candidate.find(char, position)
        if found == -1:
            return None
        gaps += found - position
        position = found + 1
    return gaps


def rank(
    query: str,
    items: Iterable[str],
    *,
    limit: int | None = None,
) -> list[tuple[str, int]]:
    """Score and sort ``items``, dropping non-matches."""
    scored: list[tuple[str, int]] = []
    for item in items:
        score = fuzzy_score(query, item)
        if score is not None:
            scored.append((item, score))
    scored.sort(key=lambda pair: (-pair[1], pair[0].lower()))
    return scored[:limit] if limit else scored


# ------------------------------------------------------------------- the engine


@dataclass(slots=True)
class ParsedInput:
    """What the raw input text currently means."""

    mode: Literal["host", "command", "command_arg", "tag", "group"]
    query: str
    command: Command | None = None
    raw: str = ""


def parse(text: str) -> ParsedInput:
    """Classify the input without mutating it."""
    raw = text
    stripped = text.lstrip()

    if stripped.startswith("/"):
        body = stripped[1:]
        if " " in body:
            name, _, argument = body.partition(" ")
            command = find_command(name)
            if command is not None and command.arg_type != "none":
                return ParsedInput("command_arg", argument.lstrip(), command, raw)
            # Unknown command or one that takes no argument: keep completing the
            # command name so a typo is still recoverable.
            return ParsedInput("command", name, command, raw)
        return ParsedInput("command", body, find_command(body), raw)

    if stripped.startswith("@"):
        return ParsedInput("tag", stripped[1:], None, raw)

    if stripped.startswith("#"):
        return ParsedInput("group", stripped[1:], None, raw)

    return ParsedInput("host", stripped, None, raw)


class CompletionEngine:
    """Turns input text into ranked suggestions.

    Sources are supplied as callables so the engine stays decoupled from the
    store, vault and theme registry and can be driven by fixtures in tests.
    """

    def __init__(
        self,
        hosts: Callable[[], Sequence[Host]],
        tags: Callable[[], Sequence[str]] | None = None,
        groups: Callable[[], Sequence[str]] | None = None,
        themes: Callable[[], Sequence[str]] | None = None,
        limit: int = 40,
    ) -> None:
        self._hosts = hosts
        self._tags = tags or (lambda: [])
        self._groups = groups or (lambda: [])
        self._themes = themes or (lambda: [])
        self.limit = limit

    # ------------------------------------------------------------- host lookup

    def matching_hosts(self, query: str) -> list[Host]:
        """Hosts matching ``query`` across name, hostname, user, group and tags."""
        results: list[tuple[Host, int]] = []
        for host in self._hosts():
            score = fuzzy_score(query, host.name)
            if score is None:
                # Fall back to the wider haystack, but rank it below name hits.
                blob_score = fuzzy_score(query, host.search_blob)
                score = None if blob_score is None else blob_score - 200
            if score is not None:
                results.append((host, score))
        results.sort(key=lambda pair: (-pair[1], pair[0].name.lower()))
        return [host for host, _ in results]

    def hosts_with_tag(self, tag: str) -> list[Host]:
        key = tag.strip().lower()
        return [h for h in self._hosts() if any(t.lower() == key for t in h.tags)]

    def hosts_in_group(self, group: str) -> list[Host]:
        key = group.strip().lower()
        return [h for h in self._hosts() if h.group.lower() == key]

    # -------------------------------------------------------------- completion

    def complete(self, text: str) -> list[Completion]:
        parsed = parse(text)
        if parsed.mode == "command":
            return self._complete_command(parsed)
        if parsed.mode == "command_arg":
            return self._complete_command_arg(parsed)
        if parsed.mode == "tag":
            return self._complete_tag(parsed)
        if parsed.mode == "group":
            return self._complete_group(parsed)
        return self._complete_host(parsed)

    def _complete_command(self, parsed: ParsedInput) -> list[Completion]:
        out: list[Completion] = []
        for command in COMMANDS:
            score = fuzzy_score(parsed.query, command.name)
            if score is None:
                score = next(
                    (
                        s - 50
                        for s in (fuzzy_score(parsed.query, a) for a in command.aliases)
                        if s is not None
                    ),
                    None,
                )
            if score is None:
                continue
            insert = f"/{command.name}" + (" " if command.arg_type != "none" else "")
            out.append(
                Completion(
                    value=command.name,
                    label=command.usage,
                    kind="command",
                    description=command.summary,
                    insert=insert,
                    score=score,
                )
            )
        out.sort(key=lambda c: (-c.score, c.value))
        return out[: self.limit]

    def _complete_command_arg(self, parsed: ParsedInput) -> list[Completion]:
        command = parsed.command
        assert command is not None
        if command.arg_type == "host":
            return [
                self._host_completion(host, insert=f"/{command.name} {host.name}")
                for host in self.matching_hosts(parsed.query)[: self.limit]
            ]
        if command.arg_type == "theme":
            return [
                Completion(
                    value=name,
                    label=name,
                    kind="theme",
                    description="Theme",
                    insert=f"/{command.name} {name}",
                    score=score,
                )
                for name, score in rank(parsed.query, self._themes(), limit=self.limit)
            ]
        return []

    def _complete_tag(self, parsed: ParsedInput) -> list[Completion]:
        out: list[Completion] = []
        for tag, score in rank(parsed.query, self._tags(), limit=self.limit):
            count = len(self.hosts_with_tag(tag))
            out.append(
                Completion(
                    value=tag,
                    label=f"@{tag}",
                    kind="tag",
                    description=f"{count} host{'s' if count != 1 else ''}",
                    insert=f"@{tag}",
                    score=score,
                )
            )
        return out

    def _complete_group(self, parsed: ParsedInput) -> list[Completion]:
        out: list[Completion] = []
        for group, score in rank(parsed.query, self._groups(), limit=self.limit):
            count = len(self.hosts_in_group(group))
            out.append(
                Completion(
                    value=group,
                    label=f"#{group}",
                    kind="group",
                    description=f"{count} host{'s' if count != 1 else ''}",
                    insert=f"#{group}",
                    score=score,
                )
            )
        return out

    def _complete_host(self, parsed: ParsedInput) -> list[Completion]:
        return [
            self._host_completion(host)
            for host in self.matching_hosts(parsed.query)[: self.limit]
        ]

    @staticmethod
    def _host_completion(host: Host, insert: str | None = None) -> Completion:
        bits = [f"{host.target}:{host.port}"]
        if host.tags:
            bits.append(" ".join(f"@{t}" for t in host.tags))
        return Completion(
            value=host.name,
            label=host.name,
            kind="host",
            description="  ".join(bits),
            insert=insert or host.name,
            score=0,
            meta={"group": host.group, "theme": host.theme},
        )

    # ------------------------------------------------------------------ actions

    def resolve_target_hosts(self, text: str) -> list[Host]:
        """Hosts the current input refers to, for the results pane."""
        parsed = parse(text)
        if parsed.mode == "tag":
            return sorted(self.hosts_with_tag(parsed.query), key=lambda h: h.name.lower())
        if parsed.mode == "group":
            return sorted(self.hosts_in_group(parsed.query), key=lambda h: h.name.lower())
        if parsed.mode == "command_arg" and parsed.command and parsed.command.arg_type == "host":
            return self.matching_hosts(parsed.query)
        if parsed.mode == "host":
            return self.matching_hosts(parsed.query)
        return []
