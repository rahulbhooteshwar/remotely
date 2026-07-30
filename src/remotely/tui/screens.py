"""Modal screens: forms, prompts and browsers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Iterable, Sequence

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    OptionList,
    Select,
    Static,
)
from textual.widgets.button import ButtonVariant
from textual.widgets.option_list import Option

from ..models import Credential, Host, UNGROUPED
from ..themes import Theme

if TYPE_CHECKING:  # avoids a cycle: sessions imports themes, screens imports both
    from ..sessions import Session

AGENT_CHOICE = "__agent__"
#: Sentinels for "make me a new vault credential from the fields below". They
#: cannot collide with a real credential name because the vault rejects an
#: empty name and these are not valid ones a user would type.
NEW_PASSWORD_CHOICE = "__new_password__"
NEW_KEY_CHOICE = "__new_key__"
NEW_CHOICES = (NEW_PASSWORD_CHOICE, NEW_KEY_CHOICE)


@dataclass(slots=True)
class PickerResult:
    """A choice made in :class:`ListPickerScreen`.

    ``action`` is empty when a row itself was activated, otherwise it is the id
    of the button that was pressed. ``selection`` is the row the result applies
    to: the activated one, or - when a button fired - whichever row was
    highlighted at the time. That second case is what lets a dialog offer a
    button that acts on "the selected thing".
    """

    action: str = ""
    selection: str | None = None


@dataclass(slots=True)
class HostFormResult:
    """What :class:`HostFormScreen` hands back.

    The form cannot write to the vault itself - that may need an unlock prompt,
    which only the app can push - so a request to create a credential comes back
    as a draft. ``credential.name`` may be blank, meaning "you pick one".
    """

    host: Host
    credential: Credential | None = None


def _field(label: str, widget) -> Vertical:
    """A labelled form row."""
    box = Vertical(Label(label, classes="field-label"), widget, classes="field")
    return box


class RevealToggle(Static):
    """Unmasks the secret field beside it.

    Words rather than an eye glyph: the obvious pictograms are emoji, and a
    terminal that will not render one shows a replacement box and gets its
    width wrong, which would shift the field it sits next to.
    """

    SHOW = "show"
    HIDE = "hide"

    def __init__(self, field: Input) -> None:
        super().__init__(self.SHOW, classes="reveal-toggle")
        self.field = field
        self.tooltip = "Show or hide this secret"

    def _on_click(self, event) -> None:
        event.stop()
        event.prevent_default()
        self.toggle()

    def toggle(self) -> None:
        self.field.password = not self.field.password
        self.update(self.SHOW if self.field.password else self.HIDE)


def _secret_field(label: str, field: Input) -> Vertical:
    """A masked field plus its reveal control.

    Typing a password you cannot read is how a wrong one gets saved, and the
    only way to find out was to fail a connection - so every masked field in
    the host and credential forms goes through here.
    """
    return Vertical(
        Label(label, classes="field-label"),
        Horizontal(field, RevealToggle(field), classes="secret-row"),
        classes="field",
    )


class RevealedInput(Input):
    """An input in a collapsible section that scrolls itself into view.

    Its section is shown by toggling ``display``, and the layout that gives the
    widget a region only lands afterwards - so nothing at the moment of the
    toggle can scroll to it. ``scroll_to_widget`` against an empty region
    returns False and does nothing, and a chain of ``call_after_refresh`` hops
    is no help either: they all drain in one batch, before the layout runs.

    Textual posts ``Show`` once the region is real, which is exactly when the
    scroll can work. It does not bubble, so the widget has to handle its own.
    """

    #: Set by the form when the user picked the mode this field belongs to, so
    #: a Show from any other cause - the dialog simply opening - does not yank
    #: the view away from wherever the user was.
    reveal_on_show = False

    def _on_show(self, event: events.Show) -> None:
        if not self.reveal_on_show:
            return
        self.reveal_on_show = False
        self.scroll_visible(animate=False)


def _button(label: str, *, id: str, variant: ButtonVariant = "default") -> Button:
    """A dialog button, one row tall instead of three.

    ``compact`` is what does the shrinking, and it has to be the widget flag
    rather than a rule in app.tcss: Textual's default button is three rows
    because ``-style-default`` draws ``tall`` top and bottom borders, and its
    hover and focus rules redraw them at higher specificity - so a border
    override in our stylesheet would let the button spring back to three rows
    the moment the pointer touched it. The compact flag sets
    ``border: none !important``, which holds in every state. Horizontal padding
    is untouched; only the wasted rows go.

    Every dialog button goes through here so a new one cannot quietly be the
    odd tall one out.
    """
    return Button(label, variant=variant, id=id, compact=True)


class CompactOnSmall:
    """Trims dialog chrome when the terminal is too short to spare it.

    A dialog's fixed furniture - padding, the title's gap, the space above the
    buttons - costs the same 11 rows whether the terminal has 60 or 14. On a
    short one (a large terminal font gives you exactly that) it swallows the
    form, leaving a scroll viewport one row tall: technically scrollable,
    useless in practice. Below the threshold the chrome collapses and the
    dialog uses the full height, which the fields get instead.

    Textual dispatches on_mount/on_resize down the MRO, so screens mixing this
    in keep their own handlers.
    """

    #: Terminal height at or below which the chrome is not affordable.
    COMPACT_BELOW = 26

    def _sync_compact(self) -> None:
        try:
            modal = self.query_one(".modal")
        except Exception:
            return
        modal.set_class(self.app.size.height < self.COMPACT_BELOW, "compact")

    def on_mount(self) -> None:
        self._sync_compact()

    def on_resize(self, event) -> None:
        self._sync_compact()


class PasscodeScreen(ModalScreen[str | None]):
    """Ask for the vault passcode, optionally confirming it for a new vault."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, *, creating: bool = False, message: str = "") -> None:
        super().__init__()
        self.creating = creating
        self.message = message

    def compose(self) -> ComposeResult:
        title = "🔒  Create vault passcode" if self.creating else "🔒  Vault locked"
        with Vertical(classes="modal modal-narrow modal-passcode"):
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
            if not self.creating:
                # Say this once, here, so it does not feel like it will keep
                # asking on every connection.
                yield Static(
                    "Asked once per run. Stays unlocked until you quit or /lock.",
                    classes="modal-footnote",
                )
            with Horizontal(classes="modal-buttons"):
                yield _button("Cancel", id="cancel")
                yield _button(
                    "Unlock" if not self.creating else "Create",
                    id="ok",
                    variant="primary",
                )

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
                yield _button("Cancel", id="cancel")
                yield _button(self.confirm_label, id="confirm", variant="error")

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
                yield _button("Cancel", id="cancel")
                yield _button("OK", id="ok", variant="primary")

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


class HostFormScreen(CompactOnSmall, ModalScreen[HostFormResult | None]):
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
        #: The auth choice already reflected on screen. Set in compose, then
        #: used to tell a real change from the Changed message Textual posts
        #: when the Select first mounts.
        self._last_auth = ""

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
                # Order matters: the two "create one now" entries lead, because
                # typing a password here is the common case for a new host, and
                # agent auth trails because it needs nothing from this form.
                auth_options: list[tuple[str, str]] = [
                    ("password based (saved in vault)", NEW_PASSWORD_CHOICE),
                    ("ssh key based (saved in vault)", NEW_KEY_CHOICE),
                ]
                auth_options += [(f"credential: {n}", n) for n in self.credential_names]
                # A new host starts on password auth, the overwhelmingly common
                # case. Editing never second-guesses what the host already uses:
                # an agent-auth host opens on agent, not on a blank password
                # field that would look like something needed filling in.
                current_auth = AGENT_CHOICE if host else NEW_PASSWORD_CHOICE
                if host and host.auth_mode == "credential" and host.credential:
                    if host.credential in self.credential_names:
                        current_auth = host.credential
                    else:
                        # Vault locked or credential deleted: keep the reference
                        # visible instead of silently downgrading to agent auth.
                        auth_options.append((f"credential: {host.credential}", host.credential))
                        current_auth = host.credential
                auth_options.append(("ssh agent / default keys", AGENT_CHOICE))
                self._last_auth = current_auth
                yield _field(
                    "Authentication",
                    Select(auth_options, value=current_auth, allow_blank=False, id="auth"),
                )
                if self.vault_locked:
                    yield Static(
                        "Vault is locked, so existing credentials are not listed. "
                        "You can still add a new one - saving will ask for the "
                        "passcode.",
                        classes="modal-help",
                    )
                # Always mounted, shown only for the choice they belong to.
                # Toggling display beats mounting on demand: mount() is async,
                # so a form saved in the same tick as the change would read
                # fields that do not exist yet.
                with Vertical(id="new-password-fields"):
                    yield _secret_field(
                        "Password", RevealedInput(password=True, id="new_password")
                    )
                with Vertical(id="new-key-fields"):
                    yield _field(
                        "Key path",
                        RevealedInput(placeholder="~/.ssh/id_ed25519", id="new_key_path"),
                    )
                    yield _secret_field(
                        "Key passphrase (optional)",
                        Input(password=True, id="new_key_passphrase"),
                    )
                with Vertical(id="new-cred-name-fields"):
                    yield _field(
                        "Save credential as (optional)",
                        Input(
                            placeholder="reuse it for other hosts under this name",
                            id="new_cred_name",
                        ),
                    )
                    yield Static(
                        "Leave this blank and a default name is generated for you.",
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
                yield Checkbox(
                    "Use the system ssh binary for this host",
                    value=host.use_system_ssh if host else False,
                    id="use_system_ssh",
                )
                yield Static(
                    "The built-in client needs nothing installed and is the "
                    "default. Turn this on only for setups it cannot express "
                    "(ProxyCommand, GSSAPI) - it requires ssh on PATH and "
                    "cannot use a stored password.",
                    classes="modal-help",
                )
                yield _field(
                    "Description", Input(value=host.description if host else "", id="description")
                )
            yield Static("", id="form-error", classes="error")
            with Horizontal(classes="modal-buttons"):
                yield _button("Cancel", id="cancel")
                yield _button("Save", id="save", variant="primary")

    def on_mount(self) -> None:
        self._sync_auth_fields()
        self.query_one("#name", Input).focus()

    # ------------------------------------------------------------- new credential

    def _auth_choice(self) -> str:
        return str(self.query_one("#auth", Select).value)

    def _sync_auth_fields(self) -> None:
        """Show only the fields the selected authentication needs."""
        choice = self._auth_choice()
        self.query_one("#new-password-fields").display = choice == NEW_PASSWORD_CHOICE
        self.query_one("#new-key-fields").display = choice == NEW_KEY_CHOICE
        self.query_one("#new-cred-name-fields").display = choice in NEW_CHOICES

    @on(Select.Changed, "#auth")
    def _auth_changed(self) -> None:
        self._sync_auth_fields()
        choice = self._auth_choice()
        previous, self._last_auth = self._last_auth, choice
        if choice == previous:
            # Textual posts Changed when the Select mounts, echoing the value
            # we gave it. Now that a new host opens on password auth, treating
            # that echo as a user action would snatch the cursor out of the
            # Name field before the form had even been touched.
            return
        # The revealed field sits well below the fold on a full-length form, so
        # picking "password based" would otherwise look like it did nothing.
        # The field scrolls itself into view once it has a region (see
        # RevealedInput); focus can be given straight away.
        selector = {
            NEW_PASSWORD_CHOICE: "#new_password",
            NEW_KEY_CHOICE: "#new_key_path",
        }.get(choice)
        if selector is None:
            return
        try:
            field = self.query_one(selector, RevealedInput)
        except NoMatches:  # pragma: no cover - both fields are always composed
            return
        field.reveal_on_show = True
        field.focus()

    def _draft_credential(self) -> Credential | None:
        """The vault entry to create, or ``None`` when auth needs no new one.

        The name is left as typed - blank included - because uniquifying it
        needs the vault's contents, which this screen deliberately has no
        access to.
        """
        choice = self._auth_choice()
        if choice not in NEW_CHOICES:
            return None

        def value_of(widget_id: str) -> str:
            return self.query_one(f"#{widget_id}", Input).value.strip()

        name = value_of("new_cred_name")
        if choice == NEW_PASSWORD_CHOICE:
            # Not stripped: a password's leading or trailing space is part of it.
            password = self.query_one("#new_password", Input).value
            return Credential(name=name, kind="password", password=password or None)
        return Credential(
            name=name,
            kind="key",
            key_path=value_of("new_key_path") or None,
            key_passphrase=value_of("new_key_passphrase") or None,
        )

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

        auth_value = self._auth_choice()
        if auth_value == AGENT_CHOICE or auth_value in NEW_CHOICES:
            # A requested-but-unwritten credential leaves here as agent auth: it
            # does not exist yet and its final name is the app's to decide, so
            # the host is rebound only once the vault has taken the secret. That
            # way a failed unlock cannot leave a host pointing at nothing.
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
            use_system_ssh=bool(self.query_one("#use_system_ssh", Checkbox).value),
            description=value_of("description"),
        )

    def action_save(self) -> None:
        host = self._collect()
        draft = self._draft_credential()
        problems = host.validate()
        if draft is not None:
            # A blank name is legal here (the app fills one in), so validate a
            # stand-in that has one and let the real check be about the secret.
            problems += replace(draft, name=draft.name or "placeholder").validate()
        if problems:
            self.query_one("#form-error", Static).update(" ".join(problems))
            return
        self.dismiss(HostFormResult(host=host, credential=draft))

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class CredentialFormScreen(CompactOnSmall, ModalScreen[Credential | None]):
    """Add or edit a vault credential."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(
        self,
        credential: Credential | None = None,
        *,
        existing_names: Sequence[str] = (),
    ) -> None:
        super().__init__()
        self.original = credential
        self.existing_names = list(existing_names)

    def _name_conflict(self, name: str) -> str:
        """Complaint about ``name`` already being taken, or "" if it is free.

        Checked here rather than left to the vault because `Vault.put` is a
        blind upsert: saving over an existing name would silently replace
        someone else's secret and re-point every host that shares it.
        """
        taken = {n.strip().lower() for n in self.existing_names}
        original = self.original.name.strip().lower() if self.original else ""
        key = name.strip().lower()
        if key == original or key not in taken:
            return ""
        return (
            f"A credential named '{name.strip()}' already exists. Choose a "
            "different name, or delete the existing one first."
        )

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
                yield _secret_field(
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
                yield _secret_field(
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
                yield _button("Cancel", id="cancel")
                yield _button("Save", id="save", variant="primary")

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
        conflict = self._name_conflict(credential.name)
        if conflict:
            problems.append(conflict)
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


class ListPickerScreen(ModalScreen[PickerResult | None]):
    """Generic list browser returning the chosen option, or a button press."""

    BINDINGS = [Binding("escape", "dismiss_none", "Close")]

    def __init__(
        self,
        title: str,
        options: Iterable[Option],
        *,
        help_text: str = "",
        empty_text: str = "Nothing here yet.",
        extra_buttons: Sequence[tuple[str, str]] = (),
        dismiss_label: str = "Ok",
    ) -> None:
        super().__init__()
        self.title_text = title
        self.options = list(options)
        self.help_text = help_text
        self.empty_text = empty_text
        self.extra_buttons = list(extra_buttons)
        self.dismiss_label = dismiss_label

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
                for button_id, label in self.extra_buttons:
                    yield _button(label, id=button_id)
                # Last, so the buttons that act on the list come first and the
                # way out sits where the eye finishes. It is also named for what
                # it does - leave the dialog - because in a dialog full of
                # closable things "Close" reads as "close the highlighted one".
                yield _button(self.dismiss_label, id="cancel")

    def on_mount(self) -> None:
        if self.options:
            self.query_one("#picker", OptionList).focus()

    def _highlighted(self) -> str | None:
        """Id of the row under the cursor, for buttons that act on a row."""
        try:
            picker = self.query_one("#picker", OptionList)
        except NoMatches:  # the empty-list placeholder is showing instead
            return None
        index = picker.highlighted
        if index is None:
            return None
        try:
            return picker.get_option_at_index(index).id
        except IndexError:  # the list shrank under us
            return None

    @on(OptionList.OptionSelected, "#picker")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.dismiss(PickerResult(selection=event.option.id))

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id:
            self.dismiss(
                PickerResult(action=event.button.id, selection=self._highlighted())
            )

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class HostDetailScreen(CompactOnSmall, ModalScreen[None]):
    """Everything known about one host.

    This used to be a permanent right-hand column. It earned its space only
    when you were looking at it, so it moved behind the tile's details icon and
    the list got the width back.
    """

    BINDINGS = [Binding("escape,q", "dismiss_none", "Close")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal modal-wide"):
            yield Label(self.title_text, classes="modal-title")
            with VerticalScroll(classes="form-scroll"):
                yield Static(self.body, id="detail-body")
            with Horizontal(classes="modal-buttons"):
                yield _button("Close", id="cancel", variant="primary")

    @on(Button.Pressed, "#cancel")
    def _close(self) -> None:
        self.dismiss(None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class HelpScreen(CompactOnSmall, ModalScreen[None]):
    """Key and command reference."""

    BINDINGS = [Binding("escape,f1,q", "dismiss_none", "Close")]

    HELP = """\
[b]Launcher[/b]
  Hosts are grouped into boxes, two rows each: name and target, then tags.
  Click a tile to connect. The icons on its right edge do the rest:
    \u29c9  copy the host name       \u270e  edit the host
    \u276f  copy user@host           \u25c9  view full details

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
  ctrl+e           edit host    (this tab's host, or the highlighted one)
  ctrl+d           delete host  (this tab's host, or the highlighted one)
  ctrl+t           themes
  ctrl+k           vault
  ctrl+l           open sessions
  ctrl+r           reload config from disk
  f1               this help
  ctrl+q           quit Remotely and close every session

[b]Inside a session[/b]
  ctrl+w           back to the launcher
  ctrl+shift+w     close this tab (or click the x on the tab)
  ctrl+pageup      previous tab
  ctrl+pagedown    next tab
  shift+pageup     scroll back through output
  shift+pagedown   scroll forward
  shift+end        jump back to the live edge
  r                retry, when a session has disconnected
  mouse wheel      scroll
  drag             select text - copied to the clipboard on release
  Every other key goes to the remote shell, including ctrl+c and ctrl+d.

[b]Commands[/b]
  /connect <host>  launch a host
  /add             add a host
  /edit <host>     edit a host
  /delete <host>   delete a host
  /sessions        list open sessions
  /close [host]    close this tab, or a host's sessions
  /themes          browse and clone themes
  /vault           manage credentials
  /settings        change preferences (confirmation prompts, etc.)
  /lock            lock the vault
  /export <path>   export hosts    (add --secrets to include credentials)
  /import <path>   import hosts    (add --replace to overwrite everything)
                   paths complete as you type and need not exist yet;
                   run either with no path to be prompted
  /help            this help
  /quit            exit

[b]About sessions[/b]
  Sessions run inside Remotely using its own SSH client, so nothing needs
  to be installed. They close when you quit - there is no detach.
"""

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal modal-wide"):
            yield Label("Help", classes="modal-title")
            with VerticalScroll(classes="form-scroll"):
                yield Static(self.HELP, id="help-body")
            with Horizontal(classes="modal-buttons"):
                yield _button("Close", id="cancel", variant="primary")

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


class CredentialRow(Horizontal):
    """One vault credential, with its own delete control.

    Built from widgets rather than an OptionList row for the same reason the
    launcher tiles are: an OptionList row is drawn text, so it has nowhere to
    put a button. Here the delete glyph is a widget, and Textual does the
    layout - the clickable cells cannot drift away from the drawn ones.
    """

    can_focus = True

    class Chosen(Message):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

    class DeleteRequested(Message):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

    def __init__(
        self, credential: Credential, used_by: int, *, alternate: bool = False
    ) -> None:
        super().__init__(classes="cred-row" + (" alt" if alternate else ""))
        self.credential = credential
        self.used_by = used_by

    def compose(self) -> ComposeResult:
        cred = self.credential
        detail = cred.key_path if cred.kind == "key" else (cred.username or "")
        used = (
            f"used by {self.used_by} host{'s' if self.used_by != 1 else ''}"
            if self.used_by
            else "unused"
        )
        with Vertical(classes="cred-text"):
            yield Static(
                f"[b]{cred.name}[/b]  [dim]{cred.kind}[/dim]", classes="cred-line"
            )
            yield Static(
                f"{detail}{'  ' if detail else ''}{used}",
                classes="cred-line cred-detail",
            )
        yield CredentialDeleteIcon(cred.name)

    def _on_click(self, event) -> None:
        # The delete icon stops its own clicks, so anything here is the row.
        event.stop()
        self.focus()
        self.post_message(self.Chosen(self.credential.name))

    def _on_key(self, event) -> None:
        # Handled on the row, not the screen, so the buttons keep their own
        # enter behaviour instead of fighting a screen-wide binding.
        if event.key == "enter":
            event.stop()
            self.post_message(self.Chosen(self.credential.name))
        elif event.key in ("delete", "backspace"):
            event.stop()
            self.post_message(self.DeleteRequested(self.credential.name))


class CredentialDeleteIcon(Static):
    """The delete control at a credential row's right edge."""

    def __init__(self, name: str) -> None:
        super().__init__("✕", classes="cred-icon")
        self.credential_name = name
        self.tooltip = "Delete credential"

    def _on_click(self, event) -> None:
        # Stop it here or the row beneath opens the edit form as well - the
        # click would both delete and edit, which is nobody's intent.
        event.stop()
        event.prevent_default()
        self.post_message(CredentialRow.DeleteRequested(self.credential_name))


class CredentialListScreen(CompactOnSmall, ModalScreen[PickerResult | None]):
    """The vault browser: pick one to edit, or delete it in place."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Close"),
        Binding("up", "move(-1)", "Up", show=False),
        Binding("down", "move(1)", "Down", show=False),
    ]

    def __init__(
        self, credentials: Sequence[Credential], usage: dict[str, int]
    ) -> None:
        super().__init__()
        self.credentials = list(credentials)
        self.usage = usage

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal modal-wide"):
            yield Label("Credentials", classes="modal-title")
            yield Static(
                "One credential can be shared by many hosts. Select to edit, "
                "or use ✕ to delete an unused one.",
                classes="modal-help",
            )
            with VerticalScroll(classes="form-scroll"):
                if not self.credentials:
                    yield Static("No credentials yet.", classes="modal-help empty")
                for index, cred in enumerate(self.credentials):
                    yield CredentialRow(
                        cred,
                        self.usage.get(cred.name.lower(), 0),
                        alternate=bool(index % 2),
                    )
            with Horizontal(classes="modal-buttons"):
                yield _button("New", id="new")
                yield _button("Lock vault", id="lock")
                yield _button("Ok", id="cancel")

    def on_mount(self) -> None:
        rows = list(self.query(CredentialRow))
        if rows:
            rows[0].focus()

    def action_move(self, delta: int) -> None:
        rows = list(self.query(CredentialRow))
        if not rows:
            return
        try:
            index = rows.index(self.focused)  # type: ignore[arg-type]
        except ValueError:  # focus is on a button, or nowhere
            index = -1 if delta > 0 else 0
        rows[(index + delta) % len(rows)].focus()

    @on(CredentialRow.Chosen)
    def _chosen(self, event: CredentialRow.Chosen) -> None:
        self.dismiss(PickerResult(selection=event.name))

    @on(CredentialRow.DeleteRequested)
    def _delete(self, event: CredentialRow.DeleteRequested) -> None:
        self.dismiss(PickerResult(action="delete", selection=event.name))

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id:
            self.dismiss(PickerResult(action=event.button.id))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


def session_options(sessions: Sequence["Session"]) -> list[Option]:
    """Build picker rows for the open-sessions browser."""
    marks = {
        "connecting": ("[yellow]…[/yellow]", "connecting"),
        "connected": ("[green]●[/green]", "connected"),
        "closed": ("[dim]○[/dim]", "closed"),
        "error": ("[red]✗[/red]", "failed"),
    }
    rows: list[Option] = []
    for session in sessions:
        mark, label = marks.get(session.status, ("•", session.status))
        detail = session.error if session.status == "error" else label
        rows.append(
            Option(
                f"{mark} [b]{session.host_name}[/b]  [dim]{session.theme.name}[/dim]\n"
                f"    [dim]{detail}[/dim]",
                id=session.id,
            )
        )
    return rows
