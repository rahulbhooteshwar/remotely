"""Domain models.

A ``Host`` never carries a secret. Anything sensitive is referenced by name and
resolved through the vault at launch time, which is what makes ``hosts.json``
safe to export, diff, or commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

AuthMode = Literal["credential", "agent"]
CredentialKind = Literal["password", "key"]

UNGROUPED = "Ungrouped"

#: SSH ``-o`` options applied when a host does not define its own. Mirrors the
#: intent of the original Connectify defaults but stays auth-agnostic because
#: the vault, not the host, decides how we authenticate.
DEFAULT_SSH_OPTIONS: list[str] = [
    "StrictHostKeyChecking=accept-new",
    "ServerAliveInterval=30",
    "ServerAliveCountMax=6",
]


def _clean_str(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


@dataclass(slots=True)
class Host:
    """A saved SSH destination."""

    name: str
    hostname: str
    username: str
    port: int = 22
    group: str = UNGROUPED
    tags: list[str] = field(default_factory=list)
    theme: str = "personal"
    auth_mode: AuthMode = "agent"
    #: Name of the vault credential to authenticate with. ``None`` means rely on
    #: the ssh agent / default key discovery.
    credential: str | None = None
    #: ``None`` means "not configured, use defaults". An empty list explicitly
    #: means "no extra options" - the same distinction Connectify draws.
    ssh_options: list[str] | None = None
    #: Shell out to the host's ``ssh`` instead of the built-in client. Opt-in,
    #: for setups the bundled client cannot express (ProxyCommand, GSSAPI).
    #: Requires ssh to be installed and rules out stored passwords.
    use_system_ssh: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        self.hostname = self.hostname.strip()
        self.username = self.username.strip()
        self.group = self.group.strip() or UNGROUPED
        self.tags = [t.strip() for t in self.tags if str(t).strip()]
        if self.auth_mode != "credential":
            self.credential = None

    @property
    def target(self) -> str:
        return f"{self.username}@{self.hostname}"

    @property
    def search_blob(self) -> str:
        """Lowercased haystack used by the fuzzy matcher."""
        parts = [self.name, self.hostname, self.username, self.group, *self.tags]
        return " ".join(parts).lower()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Host":
        opts = raw.get("ssh_options")
        auth_mode = raw.get("auth_mode", "agent")
        if auth_mode not in ("credential", "agent"):
            auth_mode = "agent"
        try:
            port = int(raw.get("port", 22) or 22)
        except (TypeError, ValueError):
            port = 22
        return cls(
            name=_clean_str(raw.get("name")),
            hostname=_clean_str(raw.get("hostname")),
            username=_clean_str(raw.get("username")),
            port=port,
            group=_clean_str(raw.get("group"), UNGROUPED) or UNGROUPED,
            tags=[str(t) for t in raw.get("tags") or []],
            theme=_clean_str(raw.get("theme"), "personal") or "personal",
            auth_mode=auth_mode,  # type: ignore[arg-type]
            credential=raw.get("credential") or None,
            ssh_options=list(opts) if isinstance(opts, list) else None,
            use_system_ssh=bool(raw.get("use_system_ssh", False)),
            description=_clean_str(raw.get("description")),
        )

    def validate(self) -> list[str]:
        """Return a list of human readable problems, empty when the host is ok."""
        problems: list[str] = []
        if not self.name:
            problems.append("Name is required.")
        if not self.hostname:
            problems.append("Hostname is required.")
        if not self.username:
            problems.append("Username is required.")
        if not (0 < self.port < 65536):
            problems.append("Port must be between 1 and 65535.")
        if self.auth_mode == "credential" and not self.credential:
            problems.append("Pick a credential or switch auth to 'agent'.")
        return problems

    def warnings(self, *, credential_kind: str | None = None) -> list[str]:
        """Non-blocking advice shown next to the host, not enforced."""
        notes: list[str] = []
        if self.use_system_ssh and credential_kind == "password":
            notes.append(
                "System ssh cannot receive a stored password; this host will "
                "prompt. Turn off 'use system ssh' to use the built-in client."
            )
        return notes


@dataclass(slots=True)
class Credential:
    """A secret held in the vault.

    One credential can be shared by any number of hosts, which is how "my LDAP
    login" is modelled: a single named entry that many hosts point at.
    """

    name: str
    kind: CredentialKind = "password"
    #: Optional. When set it overrides the host's username at launch time.
    username: str | None = None
    password: str | None = None
    key_path: str | None = None
    key_passphrase: str | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Credential":
        kind = raw.get("kind", "password")
        if kind not in ("password", "key"):
            kind = "password"
        return cls(
            name=_clean_str(raw.get("name")),
            kind=kind,  # type: ignore[arg-type]
            username=raw.get("username") or None,
            password=raw.get("password") or None,
            key_path=raw.get("key_path") or None,
            key_passphrase=raw.get("key_passphrase") or None,
            description=_clean_str(raw.get("description")),
        )

    def redacted(self) -> dict[str, Any]:
        """Metadata safe to render on screen or write to an export."""
        return {
            "name": self.name,
            "kind": self.kind,
            "username": self.username,
            "key_path": self.key_path,
            "description": self.description,
            "has_password": bool(self.password),
            "has_passphrase": bool(self.key_passphrase),
        }

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.name:
            problems.append("Credential name is required.")
        if self.kind == "password" and not self.password:
            problems.append("Password is required for a password credential.")
        if self.kind == "key" and not self.key_path:
            problems.append("Key path is required for a key credential.")
        return problems
