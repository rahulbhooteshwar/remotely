"""Modal screens: forms, prompts and browsers."""

from __future__ import annotations

from typing import Iterable, Sequence

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    OptionList,
    Select,
    Static,
)
from textual.widgets.option_list import Option

from ..models import Credential, Host, UNGROUPED
from ..sessions import SessionTab
from ..themes import Theme

AGENT_CHOICE = "__agent__"


def _field(label: str, widget) -> Vertical:
    """A labelled form row."""
    box = Vertical(Label(label, classes="field-label"), widget, classes="field")
    return box


class PasscodeScreen(ModalScreen[str | None]):
    """Ask for the vault passcode, optionally confirming it for a new vault."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, *, creating: bool = False, message: str = "") -> None:
        super().__init__()
        self.creating = creating
        self.message = message

    def compose(self) -> ComposeResult:
        title = "Create vault passcode" if self.creating else "Unlock vault"
        with Vertical(classes="modal modal-narrow"):
            yield Label(title, classes="modal-title")
            body = self.message or (
                "This passcode encrypts every stored credential. "
                "There is no recovery if you lose it."
                if self.creating
                else "Enter your vault passcode."
            )
            yield Static(body, classes="modal-help")
            yield Input(password=True, placeholder="Passcode", id="passcode")
            if self.creating:
                yield Input(password=True, placeholder="Confirm passcode", id="confirm")
            yield Static("", id="passcode-error", classes="error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("OK", variant="primary", id="ok")

    def on_mount(self) -> None:
        self.query_one("#passcode", Input).focus()

    def _error(self, text: str) -> None:
        self.query_one("#passcode-error", Static).update(text)

    def _submit(self) -> None:
        value = self.query_one("#passcode", Input).value
        if not value:
            self._error("Passcode must not be empty.")
            return
        if self.creating:
            confirm = self.query_one("#confirm", Input).value
            if value != confirm:
                self._error("Passcodes do not match.")
                return
        self.dismiss(value)

    @on(Input.Submitted)
    def _on_submit(self) -> None:
        self._submit()

    @on(Button.Pressed, "#ok")
    def _on_ok(self) -> None:
        self._submit()

    @on(Button.Pressed, "#cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no confirmation."""

    BINDINGS = [Binding("escape", "dismiss_false", "Cancel")]

    def __init__(self, question: str, *, detail: str = "", confirm_label: str = "Confirm") -> None:
        super().__init__()
        self.question = question
        self.detail = detail
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal modal-narrow"):
            yield Label(self.question, classes="modal-title")
            if self.detail:
                yield Static(self.detail, classes="modal-help")
            with Horizontal(classes="modal-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm_label, variant="error", id="confirm")

    def on_mount(self) -> None:
        self.query_one("#confirm", Button).focus()

    @on(Button.Pressed, "#confirm")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def _no(self) -> None:
        self.dismiss(False)

    def action_dismiss_false(self) -> None:
        self.dismiss(False)


class TextPromptScreen(ModalScreen[str | None]):
    """Single line prompt, used for paths and names."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, title: str, *, placeholder: str = "", value: str = "", help: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.initial = value
        self.help_text = help

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal modal-narrow"):
            yield Label(self.title_text, classes="modal-title")
            if self.help_text:
                yield Static(self.help_text, classes="modal-help")
            yield Input(value=self.initial, placeholder=self.placeholder, id="value")
            with Horizontal(classes="modal-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("OK", variant="primary", id="ok")

    def on_mount(self) -> None:
        self.query_one("#value", Input).focus()

    def _submit(self) -> None:
        value = self.query_one("#value", Input).value.strip()
        self.dismiss(value or None)

    @on(Input.Submitted)
    def _on_submit(self) -> None:
        self._submit()

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self._submit()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class HostFormScreen(ModalScreen[Host | None]):
    """Add or edit a host."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(
        self,
        host: Host | None = None,
        *,
        themes: Sequence[str] = (),
        credentials: Sequence[str] = (),
        groups: Sequence[str] = (),
        vault_locked: bool = False,
    ) -> None:
        super().__init__()
        self.original = host
        self.theme_names = list(themes) or ["personal"]
        self.credential_names = list(credentials)
        self.known_groups = list(groups)
        self.vault_locked = vault_locked

    def compose(self) -> ComposeResult:
        host = self.original
        with Vertical(classes="modal modal-wide"):
            yield Label("Edit host" if host else "New host", classes="modal-title")
            with VerticalScroll(classes="form-scroll"):
                yield _field("Name", Input(value=host.name if host else "", id="name"))
                yield _field(
                    "Hostname", Input(value=host.hostname if host else "", id="hostname")
                )
                with Horizontal(classes="field-row"):
                    yield _field(
                        "Username", Input(value=host.username if host else "", id="username")
                    )
                    yield _field(
                        "Port",
                        Input(value=str(host.port) if host else "22", id="port", type="integer"),
                    )
                with Horizontal(classes="field-row"):
                    yield _field(
                        "Group",
                        Input(
                            value=host.group if host else "",
                            placeholder=UNGROUPED,
                            id="group",
                        ),
                    )
                    yield _field(
                        "Theme",
                        Select(
                            [(name, name) for name in self.theme_names],
                            value=(
                                host.theme
                                if host and host.theme in self.theme_names
                                else self.theme_names[0]
                            ),
                            allow_blank=False,
                            id="theme",
                        ),
                    )
                yield _field(
                    "Tags (comma separated)",
                    Input(value=", ".join(host.tags) if host else "", id="tags"),
                )
                auth_options: list[tuple[str, str]] = [("ssh agent / default keys", AGENT_CHOICE)]
                auth_options += [(f"credential: {n}", n) for n in self.credential_names]
                current_auth = AGENT_CHOICE
                if host and host.auth_mode == "credential" and host.credential:
                    if host.credential in self.credential_names:
                        current_auth = host.credential
                    else:
                        # Vault locked or credential deleted: keep the reference
                        # visible instead of silently downgrading to agent auth.
                        auth_options.append((f"credential: {host.credential}", host.credential))
                        current_auth = host.credential
                yield _field(
                    "Authentication",
                    Select(auth_options, value=current_auth, allow_blank=False, id="auth"),
                )
                if self.vault_locked:
                    yield Static(
                        "Vault is locked, so existing credentials are not listed.",
                        classes="modal-help",
                    )
                ssh_options = ""
                if host and host.ssh_options is not None:
                    ssh_options = ", ".join(host.ssh_options)
                yield _field(
                    "SSH options (comma separated, blank = defaults)",
                    Input(
                        value=ssh_options,
                        placeholder="StrictHostKeyChecking=accept-new",
                        id="ssh_options",
                    ),
                )
                yield _field(
                    "Description", Input(value=host.description if host else "", id="description")
                )
            yield Static("", id="form-error", classes="error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    def on_mount(self) -> None:
        self.query_one("#name", Input).focus()

    def _collect(self) -> Host:
        def value_of(widget_id: str) -> str:
            return self.query_one(f"#{widget_id}", Input).value.strip()

        raw_port = value_of("port") or "22"
        try:
            port = int(raw_port)
        except ValueError:
            port = -1

        tags = [t.strip() for t in value_of("tags").split(",") if t.strip()]

        raw_options = self.query_one("#ssh_options", Input).value.strip()
        # Blank means "not configured" -> defaults apply. A single "-" is the
        # escape hatch for "explicitly no options".
        if raw_options == "-":
            ssh_options: list[str] | None = []
        elif raw_options:
            ssh_options = [o.strip() for o in raw_options.split(",") if o.strip()]
        else:
            ssh_options = None

        auth_value = str(self.query_one("#auth", Select).value)
        if auth_value == AGENT_CHOICE:
            auth_mode = "agent"
            credential = None
        else:
            auth_mode = "credential"
            credential = auth_value

        return Host(
            name=value_of("name"),
            hostname=value_of("hostname"),
            username=value_of("username"),
            port=port,
            group=value_of("group") or UNGROUPED,
            tags=tags,
            theme=str(self.query_one("#theme", Select).value),
            auth_mode=auth_mode,  # type: ignore[arg-type]
            credential=credential,
            ssh_options=ssh_options,
            description=value_of("description"),
        )

    def action_save(self) -> None:
        host = self._collect()
        problems = host.validate()
        if problems:
            self.query_one("#form-error", Static).update(" ".join(problems))
            return
        self.dismiss(host)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class CredentialFormScreen(ModalScreen[Credential | None]):
    """Add or edit a vault credential."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, credential: Credential | None = None) -> None:
        super().__init__()
        self.original = credential

    def compose(self) -> ComposeResult:
        cred = self.original
        with Vertical(classes="modal modal-wide"):
            yield Label("Edit credential" if cred else "New credential", classes="modal-title")
            with VerticalScroll(classes="form-scroll"):
                yield _field("Name", Input(value=cred.name if cred else "", id="name"))
                yield _field(
                    "Kind",
                    Select(
                        [("password", "password"), ("ssh key", "key")],
                        value=cred.kind if cred else "password",
                        allow_blank=False,
                        id="kind",
                    ),
                )
                yield _field(
                    "Username (optional, overrides the host's)",
                    Input(value=(cred.username or "") if cred else "", id="username"),
                )
                yield _field(
                    "Password",
                    Input(
                        value=(cred.password or "") if cred else "",
                        password=True,
                        id="password",
                    ),
                )
                yield _field(
                    "Key path",
                    Input(
                        value=(cred.key_path or "") if cred else "",
                        placeholder="~/.ssh/id_ed25519",
                        id="key_path",
                    ),
                )
                yield _field(
                    "Key passphrase",
                    Input(
                        value=(cred.key_passphrase or "") if cred else "",
                        password=True,
                        id="key_passphrase",
                    ),
                )
                yield _field(
                    "Description",
                    Input(value=cred.description if cred else "", id="description"),
                )
            yield Static("", id="cred-error", classes="error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    def on_mount(self) -> None:
        self.query_one("#name", Input).focus()

    def action_save(self) -> None:
        def value_of(widget_id: str) -> str:
            return self.query_one(f"#{widget_id}", Input).value

        credential = Credential(
            name=value_of("name").strip(),
            kind=str(self.query_one("#kind", Select).value),  # type: ignore[arg-type]
            username=value_of("username").strip() or None,
            password=value_of("password") or None,
            key_path=value_of("key_path").strip() or None,
            key_passphrase=value_of("key_passphrase") or None,
            description=value_of("description").strip(),
        )
        problems = credential.validate()
        if problems:
            self.query_one("#cred-error", Static).update(" ".join(problems))
            return
        self.dismiss(credential)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class ListPickerScreen(ModalScreen[str | None]):
    """Generic list browser returning the chosen option id."""

    BINDINGS = [Binding("escape", "dismiss_none", "Close")]

    def __init__(
        self,
        title: str,
        options: Iterable[Option],
        *,
        help_text: str = "",
        empty_text: str = "Nothing here yet.",
        extra_buttons: Sequence[tuple[str, str]] = (),
    ) -> None:
        super().__init__()
        self.title_text = title
        self.options = list(options)
        self.help_text = help_text
        self.empty_text = empty_text
        self.extra_buttons = list(extra_buttons)

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal modal-wide"):
            yield Label(self.title_text, classes="modal-title")
            if self.help_text:
                yield Static(self.help_text, classes="modal-help")
            if self.options:
                yield OptionList(*self.options, id="picker")
            else:
                yield Static(self.empty_text, classes="modal-help empty")
            with Horizontal(classes="modal-buttons"):
                yield Button("Close", id="cancel")
                for button_id, label in self.extra_buttons:
                    yield Button(label, id=button_id)

    def on_mount(self) -> None:
        if self.options:
            self.query_one("#picker", OptionList).focus()

    @on(OptionList.OptionSelected, "#picker")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.dismiss(event.option.id)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id:
            self.dismiss(f"!{event.button.id}")

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Key and command reference."""

    BINDINGS = [Binding("escape,f1,q", "dismiss_none", "Close")]

    HELP = """\
[b]Command bar[/b]
  type anything    fuzzy search hosts by name, hostname, user, group or tag
  /                commands
  @                filter by tag
  #                filter by group

[b]Keys[/b]
  enter            launch the highlighted host
  tab              accept the highlighted completion
  up / down        move through results
  ctrl+n           new host
  ctrl+e           edit highlighted host
  ctrl+d           delete highlighted host
  ctrl+t           themes
  ctrl+k           vault
  ctrl+l           open session tabs
  ctrl+r           reload config from disk
  f1               this help
  ctrl+q           quit Remotely (sessions keep running)

[b]Commands[/b]
  /connect <host>  launch a host
  /add             add a host
  /edit <host>     edit a host
  /delete <host>   delete a host
  /sessions        list open tabs
  /themes          browse and clone themes
  /vault           manage credentials
  /lock            lock the vault
  /export <path>   export hosts    (add --secrets to include credentials)
  /import <path>   import hosts    (add --replace to overwrite everything)
  /help            this help
  /quit            exit

[b]Tabs[/b]
  Sessions open as tmux windows in the 'remotely' session.
  ctrl+b n / p     next / previous tab
  ctrl+b 0         back to Remotely
  ctrl+b d         detach, leaving every session running
"""

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal modal-wide"):
            yield Label("Help", classes="modal-title")
            with VerticalScroll(classes="form-scroll"):
                yield Static(self.HELP, id="help-body")
            with Horizontal(classes="modal-buttons"):
                yield Button("Close", variant="primary", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed, "#cancel")
    def _close(self) -> None:
        self.dismiss(None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


def theme_options(themes: Sequence[Theme]) -> list[Option]:
    """Build picker rows for the theme browser."""
    rows: list[Option] = []
    for theme in themes:
        origin = "bundled" if theme.builtin else "user"
        swatch = f"[{theme.accent}]{theme.icon} ████[/]"
        rows.append(
            Option(
                f"{swatch}  [b]{theme.name}[/b]  [dim]{origin}[/dim]\n"
                f"       [dim]{theme.description or theme.display_label}[/dim]",
                id=theme.name,
            )
        )
    return rows


def credential_options(credentials: Sequence[Credential], usage: dict[str, int]) -> list[Option]:
    """Build picker rows for the vault browser."""
    rows: list[Option] = []
    for cred in credentials:
        count = usage.get(cred.name.lower(), 0)
        used = f"{count} host{'s' if count != 1 else ''}"
        detail = cred.key_path if cred.kind == "key" else (cred.username or "")
        rows.append(
            Option(
                f"[b]{cred.name}[/b]  [dim]{cred.kind}[/dim]\n"
                f"    [dim]{detail}{'  ' if detail else ''}{used}[/dim]",
                id=cred.name,
            )
        )
    return rows


def session_options(tabs: Sequence[SessionTab]) -> list[Option]:
    """Build picker rows for the open-tabs browser."""
    return [
        Option(
            f"{'▶ ' if tab.active else '  '}[b]{tab.host}[/b]  [dim]tab {tab.index}"
            f"{'  ' + tab.theme if tab.theme else ''}[/dim]",
            id=tab.window_id,
        )
        for tab in tabs
    ]
