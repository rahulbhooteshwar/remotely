"""The Remotely TUI.

A single command bar drives everything. The list underneath is both the search
result and the completion dropdown, so there is only ever one thing to look at
and one thing to press enter on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import __version__, config
from ..completion import COMMANDS, Completion, CompletionEngine, find_command, parse
from ..importexport import TransferError, export_hosts, import_hosts
from ..models import Credential, Host
from ..sessions import SessionTab, TmuxBackend, TmuxError, current_session_name, inside_tmux
from ..ssh import SSHError, askpass_force_supported, build_plan
from ..store import HostStore, StoreError
from ..themes import ThemeRegistry
from ..vault import InvalidPasscode, Vault, VaultError, VaultLocked
from .screens import (
    ConfirmScreen,
    CredentialFormScreen,
    HelpScreen,
    HostFormScreen,
    ListPickerScreen,
    PasscodeScreen,
    TextPromptScreen,
    credential_options,
    session_options,
    theme_options,
)


class CommandBar(Input):
    """The one input. Steals navigation keys so the list can be driven from here."""

    class Navigate(Message):
        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    class Accept(Message):
        """Tab pressed: take the highlighted completion into the input."""

    class Cancel(Message):
        """Escape pressed."""

    async def _on_key(self, event) -> None:
        key = event.key
        if key in ("up", "down"):
            event.prevent_default()
            event.stop()
            self.post_message(self.Navigate(-1 if key == "up" else 1))
            return
        if key in ("pageup", "pagedown"):
            event.prevent_default()
            event.stop()
            self.post_message(self.Navigate(-8 if key == "pageup" else 8))
            return
        if key == "tab":
            event.prevent_default()
            event.stop()
            self.post_message(self.Accept())
            return
        if key == "escape":
            event.prevent_default()
            event.stop()
            self.post_message(self.Cancel())
            return
        await super()._on_key(event)


class RemotelyApp(App[None]):
    """Main application."""

    CSS_PATH = "app.tcss"
    TITLE = "Remotely"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f1", "help", "Help", priority=True),
        Binding("ctrl+n", "new_host", "New", priority=True),
        Binding("ctrl+e", "edit_host", "Edit", priority=True),
        Binding("ctrl+d", "delete_host", "Delete", priority=True),
        Binding("ctrl+t", "themes", "Themes", priority=True),
        Binding("ctrl+k", "vault", "Vault", priority=True),
        Binding("ctrl+l", "sessions", "Tabs", priority=True),
        Binding("ctrl+r", "reload", "Reload", priority=True),
    ]

    def __init__(
        self,
        *,
        store: HostStore | None = None,
        vault: Vault | None = None,
        themes: ThemeRegistry | None = None,
        backend: TmuxBackend | None = None,
    ) -> None:
        super().__init__()
        config.ensure_layout()
        self.store = store or HostStore()
        self.vault = vault or Vault()
        self.themes = themes or ThemeRegistry()
        # When already inside a tmux session, put tabs there rather than
        # hijacking the user into a second session.
        existing = current_session_name()
        self.backend = backend or TmuxBackend(session_name=existing or "remotely")
        self.engine = CompletionEngine(
            hosts=lambda: self.store.hosts,
            tags=lambda: self.store.tags(),
            groups=lambda: self.store.group_names(),
            themes=lambda: self.themes.names(),
        )
        self._rows: dict[str, Completion] = {}
        self._selected_host: Host | None = None

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            yield Static("", id="banner")
            yield CommandBar(
                placeholder="Search hosts, or / for commands, @ for tags, # for groups",
                id="command-bar",
            )
            with Horizontal(id="body"):
                yield OptionList(id="results")
                with VerticalScroll(id="detail-pane"):
                    yield Static("", id="detail")
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#command-bar", CommandBar).focus()
        self._refresh_banner()
        self._refresh_results()
        self._warn_about_environment()

    # ------------------------------------------------------------------ chrome

    def _refresh_banner(self) -> None:
        lock = "[red]locked[/red]" if self.vault.is_locked else "[green]unlocked[/green]"
        if not self.vault.exists():
            lock = "[dim]not created[/dim]"
        count = len(self.store)
        self.query_one("#banner", Static).update(
            f"[b]Remotely[/b] [dim]v{__version__}[/dim]   "
            f"{count} host{'s' if count != 1 else ''}   "
            f"vault {lock}   "
            f"session [b]{self.backend.session_name}[/b]"
        )

    def _status(self, message: str, *, error: bool = False) -> None:
        widget = self.query_one("#status", Static)
        widget.set_class(error, "error")
        widget.update(message)

    def _warn_about_environment(self) -> None:
        problems: list[str] = []
        if not self.backend.is_available():
            problems.append("tmux was not found - tabs are unavailable.")
        elif not inside_tmux():
            problems.append("Not running inside tmux; launch with 'remotely' for tabs.")
        if not askpass_force_supported():
            problems.append(
                "OpenSSH < 8.4: stored passwords cannot be auto-supplied, ssh will prompt."
            )
        for error in self.themes.errors:
            problems.append(error)
        if problems:
            self._status("  ".join(problems), error=True)

    # ----------------------------------------------------------------- results

    def _refresh_results(self) -> None:
        text = self.query_one("#command-bar", CommandBar).value
        results = self.query_one("#results", OptionList)
        results.clear_options()
        self._rows = {}

        options: list[Option] = []
        if not text.strip():
            options = self._grouped_host_options()
        else:
            for index, completion in enumerate(self.engine.complete(text)):
                option_id = f"{completion.kind}:{index}:{completion.value}"
                self._rows[option_id] = completion
                options.append(Option(self._render_completion(completion), id=option_id))

        if options:
            results.add_options(options)
            self._highlight_first_selectable()
        else:
            results.add_options([Option("[dim]No matches[/dim]", disabled=True)])
            self._update_detail(None)

    def _grouped_host_options(self) -> list[Option]:
        """The idle view: every host, bucketed by group."""
        options: list[Option] = []
        groups = self.store.groups()
        if not groups:
            return [
                Option(
                    "[dim]No hosts yet. Press ctrl+n or type /add to create one.[/dim]",
                    disabled=True,
                )
            ]
        for group, hosts in groups.items():
            options.append(Option(f"[b]{group}[/b] [dim]({len(hosts)})[/dim]", disabled=True))
            for index, host in enumerate(hosts):
                completion = CompletionEngine._host_completion(host)
                option_id = f"host:{group}:{index}:{host.name}"
                self._rows[option_id] = completion
                options.append(Option(self._render_completion(completion, indent=True), id=option_id))
        return options

    def _render_completion(self, completion: Completion, *, indent: bool = False) -> str:
        pad = "  " if indent else ""
        if completion.kind == "host":
            host = self.store.get(completion.value)
            theme = self.themes.get(host.theme if host else None)
            marker = f"[{theme.accent}]{theme.icon}[/]"
            return f"{pad}{marker} [b]{completion.label}[/b]  [dim]{completion.description}[/dim]"
        icons = {"command": "›", "tag": "@", "group": "#", "theme": "◈", "path": "…"}
        icon = icons.get(completion.kind, "•")
        return f"{pad}[dim]{icon}[/dim] [b]{completion.label}[/b]  [dim]{completion.description}[/dim]"

    def _highlight_first_selectable(self) -> None:
        results = self.query_one("#results", OptionList)
        for index in range(results.option_count):
            option = results.get_option_at_index(index)
            if not option.disabled:
                results.highlighted = index
                return
        results.highlighted = None

    def _highlighted_completion(self) -> Completion | None:
        results = self.query_one("#results", OptionList)
        index = results.highlighted
        if index is None:
            return None
        try:
            option = results.get_option_at_index(index)
        except Exception:
            return None
        if option.id is None:
            return None
        return self._rows.get(option.id)

    def _update_detail(self, completion: Completion | None) -> None:
        detail = self.query_one("#detail", Static)
        if completion is None:
            detail.update("")
            self._selected_host = None
            return

        if completion.kind != "host":
            self._selected_host = None
            body = f"[b]{completion.label}[/b]\n\n{completion.description}"
            if completion.kind == "command":
                command = find_command(completion.value)
                if command is not None:
                    body += f"\n\n[dim]usage:[/dim] {command.usage}"
            detail.update(body)
            return

        host = self.store.get(completion.value)
        self._selected_host = host
        if host is None:
            detail.update("")
            return

        theme = self.themes.get(host.theme)
        if host.auth_mode == "credential":
            auth = f"credential [b]{host.credential}[/b]"
            if self.vault.is_locked:
                auth += "  [dim](vault locked)[/dim]"
        else:
            auth = "ssh agent / default keys"

        options = host.ssh_options
        if options is None:
            options_text = "[dim]defaults[/dim]"
        elif not options:
            options_text = "[dim]none[/dim]"
        else:
            options_text = "\n".join(f"  -o {o}" for o in options)

        lines = [
            f"[b]{host.name}[/b]",
            "",
            f"[dim]target[/dim]   {host.target}",
            f"[dim]port[/dim]     {host.port}",
            f"[dim]group[/dim]    {host.group}",
            f"[dim]tags[/dim]     {' '.join('@' + t for t in host.tags) or '[dim]none[/dim]'}",
            f"[dim]theme[/dim]    [{theme.accent}]{theme.icon} {theme.name}[/]",
            f"[dim]auth[/dim]     {auth}",
            "",
            "[dim]ssh options[/dim]",
            options_text,
        ]
        if host.description:
            lines += ["", f"[dim]{host.description}[/dim]"]
        detail.update("\n".join(lines))

    # ------------------------------------------------------------- input events

    @on(Input.Changed, "#command-bar")
    def _on_changed(self) -> None:
        self._refresh_results()

    @on(OptionList.OptionHighlighted, "#results")
    def _on_highlight(self, event: OptionList.OptionHighlighted) -> None:
        option_id = event.option.id
        self._update_detail(self._rows.get(option_id) if option_id else None)

    @on(CommandBar.Navigate)
    def _on_navigate(self, event: CommandBar.Navigate) -> None:
        results = self.query_one("#results", OptionList)
        if results.option_count == 0:
            return
        current = results.highlighted if results.highlighted is not None else -1
        step = 1 if event.delta > 0 else -1
        target = current
        for _ in range(abs(event.delta)):
            probe = target
            while True:
                probe += step
                if probe < 0 or probe >= results.option_count:
                    probe = None
                    break
                if not results.get_option_at_index(probe).disabled:
                    break
            if probe is None:
                break
            target = probe
        if target != current:
            results.highlighted = target
            results.scroll_to_highlight()

    @on(CommandBar.Accept)
    def _on_accept(self) -> None:
        completion = self._highlighted_completion()
        if completion is None:
            return
        bar = self.query_one("#command-bar", CommandBar)
        bar.value = completion.insert_text
        bar.cursor_position = len(bar.value)

    @on(CommandBar.Cancel)
    def _on_cancel(self) -> None:
        bar = self.query_one("#command-bar", CommandBar)
        if bar.value:
            bar.value = ""
        self._status("")

    @on(OptionList.OptionSelected, "#results")
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id
        completion = self._rows.get(option_id) if option_id else None
        if completion is not None:
            self._activate(completion)

    @on(Input.Submitted, "#command-bar")
    def _on_submitted(self) -> None:
        text = self.query_one("#command-bar", CommandBar).value
        parsed = parse(text)

        # An explicit "/command arg" runs as typed rather than acting on whatever
        # happens to be highlighted.
        if parsed.mode == "command_arg" and parsed.command is not None and parsed.query:
            self._run_command(parsed.command.name, parsed.query)
            return
        if parsed.mode == "command" and parsed.command is not None:
            if parsed.command.arg_type == "none":
                self._run_command(parsed.command.name, "")
                return

        completion = self._highlighted_completion()
        if completion is not None:
            self._activate(completion)

    def _activate(self, completion: Completion) -> None:
        if completion.kind == "host":
            self._connect(completion.value)
            return
        if completion.kind == "command":
            command = find_command(completion.value)
            if command is None:
                return
            if command.arg_type == "none":
                self._run_command(command.name, "")
            else:
                bar = self.query_one("#command-bar", CommandBar)
                bar.value = completion.insert_text
                bar.cursor_position = len(bar.value)
            return
        if completion.kind in ("tag", "group"):
            bar = self.query_one("#command-bar", CommandBar)
            bar.value = completion.insert_text
            bar.cursor_position = len(bar.value)
            return
        if completion.kind == "theme":
            self._status(f"Theme {completion.value}")

    # -------------------------------------------------------------- dispatching

    def _run_command(self, name: str, argument: str) -> None:
        argument = argument.strip()
        actions = {
            "connect": lambda: self._connect(argument),
            "add": self.action_new_host,
            "edit": lambda: self._edit_host(argument),
            "delete": lambda: self._delete_host(argument),
            "sessions": self.action_sessions,
            "themes": self.action_themes,
            "vault": self.action_vault,
            "lock": self._lock_vault,
            "export": lambda: self._export(argument),
            "import": lambda: self._import(argument),
            "help": self.action_help,
            "quit": self.action_quit,
        }
        action = actions.get(name)
        if action is None:
            self._status(f"Unknown command /{name}", error=True)
            return
        self._clear_bar()
        action()

    def _clear_bar(self) -> None:
        bar = self.query_one("#command-bar", CommandBar)
        bar.value = ""

    # ------------------------------------------------------------------- vault

    @work
    async def _unlock_vault(self, reason: str = "") -> bool:
        """Prompt for the passcode, creating the vault on first use."""
        if not self.vault.is_locked:
            return True
        creating = not self.vault.exists()
        while True:
            passcode = await self.push_screen_wait(
                PasscodeScreen(creating=creating, message=reason)
            )
            if passcode is None:
                return False
            try:
                if creating:
                    self.vault.initialise(passcode)
                else:
                    self.vault.unlock(passcode)
            except InvalidPasscode:
                reason = "Incorrect passcode. Try again."
                continue
            except VaultError as exc:
                self._status(str(exc), error=True)
                return False
            self._refresh_banner()
            self._status("Vault unlocked.")
            return True

    async def _ensure_vault(self, reason: str = "") -> bool:
        if not self.vault.is_locked:
            return True
        return await self._unlock_vault(reason).wait()

    def _lock_vault(self) -> None:
        self.vault.lock()
        self._refresh_banner()
        self._status("Vault locked.")

    # --------------------------------------------------------------- connecting

    @work
    async def _connect(self, host_name: str) -> None:
        host = self.store.get(host_name)
        if host is None:
            self._status(f"No host named {host_name!r}.", error=True)
            return

        credential: Credential | None = None
        if host.auth_mode == "credential" and host.credential:
            if not await self._ensure_vault(f"Unlock the vault to connect to {host.name}."):
                self._status("Cancelled: vault stayed locked.", error=True)
                return
            try:
                credential = self.vault.require(host.credential)
            except VaultError as exc:
                self._status(str(exc), error=True)
                return

        try:
            plan = build_plan(host, credential)
        except SSHError as exc:
            self._status(str(exc), error=True)
            return

        theme = self.themes.get(host.theme)
        try:
            session = None
            if not self.backend.session_exists():
                # Running the TUI outside the managed session (dev mode); make a
                # home for the tab rather than failing.
                session = self.backend.ensure_session("sleep 86400")
            tab = self.backend.open_tab(host, plan, theme, session=session)
        except TmuxError as exc:
            self._status(str(exc), error=True)
            return

        notes = f"  [dim]{' '.join(plan.notes)}[/dim]" if plan.notes else ""
        self._status(f"Launched [b]{host.name}[/b] in tab {tab.index} ({theme.name}).{notes}")
        self._clear_bar()
        self._refresh_results()

    # ------------------------------------------------------------------ actions

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_reload(self) -> None:
        try:
            self.store.load()
        except StoreError as exc:
            self._status(str(exc), error=True)
            return
        self.themes.reload()
        self._refresh_banner()
        self._refresh_results()
        self._status("Reloaded configuration from disk.")

    @work
    async def action_new_host(self) -> None:
        host = await self.push_screen_wait(
            HostFormScreen(
                themes=self.themes.names(),
                credentials=[] if self.vault.is_locked else self.vault.names(),
                groups=self.store.group_names(),
                vault_locked=self.vault.is_locked,
            )
        )
        if host is None:
            return
        try:
            self.store.add(host)
        except StoreError as exc:
            self._status(str(exc), error=True)
            return
        self._refresh_banner()
        self._refresh_results()
        self._status(f"Added [b]{host.name}[/b].")

    @work
    async def action_edit_host(self) -> None:
        await self._edit_host_async(self._selected_host.name if self._selected_host else "")

    def _edit_host(self, name: str) -> None:
        self._edit_host_worker(name)

    @work
    async def _edit_host_worker(self, name: str) -> None:
        await self._edit_host_async(name)

    async def _edit_host_async(self, name: str) -> None:
        host = self.store.get(name) if name else self._selected_host
        if host is None:
            self._status("Select a host to edit first.", error=True)
            return
        updated = await self.push_screen_wait(
            HostFormScreen(
                host,
                themes=self.themes.names(),
                credentials=[] if self.vault.is_locked else self.vault.names(),
                groups=self.store.group_names(),
                vault_locked=self.vault.is_locked,
            )
        )
        if updated is None:
            return
        try:
            self.store.update(host.name, updated)
        except StoreError as exc:
            self._status(str(exc), error=True)
            return
        self._refresh_banner()
        self._refresh_results()
        self._status(f"Updated [b]{updated.name}[/b].")

    @work
    async def action_delete_host(self) -> None:
        await self._delete_host_async(self._selected_host.name if self._selected_host else "")

    def _delete_host(self, name: str) -> None:
        self._delete_host_worker(name)

    @work
    async def _delete_host_worker(self, name: str) -> None:
        await self._delete_host_async(name)

    async def _delete_host_async(self, name: str) -> None:
        host = self.store.get(name) if name else self._selected_host
        if host is None:
            self._status("Select a host to delete first.", error=True)
            return
        confirmed = await self.push_screen_wait(
            ConfirmScreen(
                f"Delete host {host.name}?",
                detail=f"{host.target}:{host.port}. This cannot be undone.",
                confirm_label="Delete",
            )
        )
        if not confirmed:
            return
        try:
            self.store.delete(host.name)
        except StoreError as exc:
            self._status(str(exc), error=True)
            return
        self._selected_host = None
        self._refresh_banner()
        self._refresh_results()
        self._status(f"Deleted [b]{host.name}[/b].")

    @work
    async def action_themes(self) -> None:
        while True:
            themes = self.themes.list()
            choice = await self.push_screen_wait(
                ListPickerScreen(
                    "Themes",
                    theme_options(themes),
                    help_text=(
                        f"Bundled themes plus anything in {config.themes_dir()}. "
                        "Select one to clone it for editing."
                    ),
                    extra_buttons=[("reload", "Reload")],
                )
            )
            if choice is None:
                return
            if choice == "!reload":
                self.themes.reload()
                self._status("Themes reloaded.")
                continue
            name = await self.push_screen_wait(
                TextPromptScreen(
                    f"Clone '{choice}' as",
                    value=f"{choice}-custom",
                    help=f"Creates a new .toml in {config.themes_dir()}.",
                )
            )
            if name is None:
                continue
            try:
                path = self.themes.copy_to_user_dir(choice, name)
            except Exception as exc:
                self._status(str(exc), error=True)
                continue
            self._status(f"Wrote {path}. Edit it and press ctrl+r to reload.")
            return

    @work
    async def action_vault(self) -> None:
        if not await self._ensure_vault("Unlock the vault to manage credentials."):
            return
        while True:
            credentials = self.vault.list()
            usage = {
                cred.name.lower(): len(self.store.using_credential(cred.name))
                for cred in credentials
            }
            choice = await self.push_screen_wait(
                ListPickerScreen(
                    "Credentials",
                    credential_options(credentials, usage),
                    help_text=(
                        "One credential can be shared by many hosts. "
                        "Select to edit, or add a new one."
                    ),
                    empty_text="No credentials yet.",
                    extra_buttons=[("new", "New"), ("lock", "Lock vault")],
                )
            )
            if choice is None:
                return
            if choice == "!lock":
                self._lock_vault()
                return
            if choice == "!new":
                created = await self.push_screen_wait(CredentialFormScreen())
                if created is not None:
                    try:
                        self.vault.put(created)
                        self._status(f"Saved credential [b]{created.name}[/b].")
                    except VaultError as exc:
                        self._status(str(exc), error=True)
                continue

            existing = self.vault.get(choice)
            if existing is None:
                continue
            edited = await self.push_screen_wait(CredentialFormScreen(existing))
            if edited is None:
                continue
            try:
                if edited.name.lower() != existing.name.lower():
                    self.vault.delete(existing.name)
                self.vault.put(edited)
                self._status(f"Saved credential [b]{edited.name}[/b].")
            except VaultError as exc:
                self._status(str(exc), error=True)

    @work
    async def action_sessions(self) -> None:
        try:
            tabs: list[SessionTab] = self.backend.list_tabs()
        except TmuxError as exc:
            self._status(str(exc), error=True)
            return
        if not tabs:
            self._status("No open session tabs.")
            return
        choice = await self.push_screen_wait(
            ListPickerScreen(
                "Open tabs",
                session_options(tabs),
                help_text="Select a tab to switch to it.",
                empty_text="No open session tabs.",
            )
        )
        if choice is None or choice.startswith("!"):
            return
        try:
            self.backend.focus_tab(choice)
        except TmuxError as exc:
            self._status(str(exc), error=True)

    # ---------------------------------------------------------- import / export

    @work
    async def _export(self, argument: str) -> None:
        parts = argument.split()
        include_secrets = "--secrets" in parts
        path_parts = [p for p in parts if not p.startswith("--")]
        raw_path = path_parts[0] if path_parts else None
        if raw_path is None:
            raw_path = await self.push_screen_wait(
                TextPromptScreen(
                    "Export hosts to",
                    value=str(Path.home() / "remotely-hosts.json"),
                    help="Add --secrets to the command to include credentials.",
                )
            )
            if raw_path is None:
                return

        if include_secrets and not await self._ensure_vault("Unlock the vault to export secrets."):
            return

        try:
            path, wrote_secrets = export_hosts(
                self.store,
                Path(raw_path),
                vault=self.vault,
                include_secrets=include_secrets,
            )
        except (TransferError, VaultLocked, OSError) as exc:
            self._status(str(exc), error=True)
            return

        if wrote_secrets:
            self._status(
                f"Exported {len(self.store)} hosts and credentials to {path} "
                f"[b]in plaintext[/b] (mode 0600). Handle carefully.",
                error=True,
            )
        else:
            self._status(f"Exported {len(self.store)} hosts to {path} (no secrets).")

    @work
    async def _import(self, argument: str) -> None:
        parts = argument.split()
        replace = "--replace" in parts
        overwrite = "--overwrite" in parts or replace
        path_parts = [p for p in parts if not p.startswith("--")]
        raw_path = path_parts[0] if path_parts else None
        if raw_path is None:
            raw_path = await self.push_screen_wait(
                TextPromptScreen(
                    "Import hosts from",
                    placeholder=str(Path.home() / "remotely-hosts.json"),
                    help="Add --replace to swap the whole host list.",
                )
            )
            if raw_path is None:
                return

        if replace:
            confirmed = await self.push_screen_wait(
                ConfirmScreen(
                    "Replace every saved host?",
                    detail=f"All {len(self.store)} current hosts will be discarded.",
                    confirm_label="Replace",
                )
            )
            if not confirmed:
                return

        try:
            result = import_hosts(
                self.store,
                Path(raw_path),
                vault=None if self.vault.is_locked else self.vault,
                replace=replace,
                overwrite=overwrite,
            )
        except (TransferError, StoreError, OSError) as exc:
            self._status(str(exc), error=True)
            return

        self._refresh_banner()
        self._refresh_results()
        self._status(f"Import complete: {result.summary()}.")


def run(**kwargs: Any) -> None:
    RemotelyApp(**kwargs).run()
