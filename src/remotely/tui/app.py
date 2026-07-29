"""The Remotely TUI.

A single command bar drives everything, and SSH sessions open as tabs inside
this app rather than in an external multiplexer. Tab 0 is always the launcher,
so there is a way back from any session.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from textual import on, work
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import ContentSwitcher, Footer, Input, Static, Tabs, Tab
from textual.widgets.option_list import Option

from .. import __version__, config
from ..completion import Completion, CompletionEngine, find_command, parse
from ..importexport import TransferError, export_hosts, import_hosts
from ..models import Credential, Host
from .. import settings as settings_module
from ..sessions import Session, SessionManager
from ..store import HostStore, StoreError
from ..themes import Theme, ThemeRegistry
from ..transport import TransportError, system_ssh_available
from ..vault import (
    InvalidPasscode,
    Vault,
    VaultError,
    VaultLocked,
    default_credential_name,
    unique_credential_name,
)
from .screens import (
    ConfirmScreen,
    CredentialFormScreen,
    CredentialListScreen,
    HelpScreen,
    HostDetailScreen,
    HostFormResult,
    HostFormScreen,
    ListPickerScreen,
    PasscodeScreen,
    TextPromptScreen,
    session_options,
    theme_options,
)
from .results import ACTIONS, CompletionRow, GroupBox, HostTile
from .terminal import TerminalPane

LAUNCHER_TAB = "launcher"


class SessionTab(Tab):
    """A session tab carrying its own close control.

    Textual's Tab has no close affordance, and a keyboard-only route is easy to
    miss, so the label ends in a visible x and a click landing on it closes the
    session instead of selecting it.

    The theme shows up as the tab's own colour rather than an emoji: a filled
    accent-coloured button reads at a glance and, unlike a glyph, cannot be
    mangled by a terminal that will not render the emoji. The icon still marks
    hosts in the launcher, where it sits in a text list.
    """

    CLOSE_GLYPH = "✕"

    class CloseRequested(Message):
        def __init__(self, tab_id: str) -> None:
            super().__init__()
            self.tab_id = tab_id

    def __init__(self, title: str, *, theme: Theme, id: str) -> None:
        super().__init__(f"{title}  {self.CLOSE_GLYPH}", id=id)
        self._title = title
        self.theme_colors = theme

    def on_mount(self) -> None:
        # Accent on the theme's own terminal background: bright enough to tell
        # prod from the rest instantly, dark text so the label stays readable.
        dark = self.theme_colors.pane_styles()["background"]
        self.styles.background = self.theme_colors.accent
        self.styles.color = dark
        self.styles.text_style = "bold"
        # Edges, so two tabs on the same theme do not merge into one block of
        # colour. A Tab is one row tall, so only the vertical edges are
        # available - which is all that is needed to tell them apart.
        self.styles.border_left = ("outer", dark)
        self.styles.border_right = ("outer", dark)

    def set_title(self, title: str) -> None:
        self._title = title
        self.label = f"{title}  {self.CLOSE_GLYPH}"

    def _on_click(self, event) -> None:
        # The glyph plus its trailing padding occupies the last few cells.
        #
        # Measure against `region`, not `size`: event.x is relative to the
        # widget's region, which includes the border and padding, while `size`
        # is the content box alone. Using `size` here put the hot zone several
        # cells too far left, so clicking the end of the host name closed the
        # tab - and the border added later made that worse.
        #
        # Never call super() here: Tab._on_click takes no event argument, so
        # passing one is a TypeError. Textual already walks the MRO and invokes
        # the base handler itself; prevent_default() breaks that walk, which is
        # precisely how a hit on the x suppresses "select this tab".
        if self.id and event.x >= max(0, self.region.width - 3):
            event.prevent_default()
            event.stop()
            self.post_message(self.CloseRequested(self.id))


class SessionTabs(Tabs):
    """The tab row, with scrolling that does not depend on clicking a tab.

    Textual's Tabs only scrolls when the active tab changes or the widget is
    resized, so with more tabs than fit, the ones off the end are reachable
    only by activating something - which is the thing you cannot see well
    enough to click. This adds the two ways people actually try: the wheel
    anywhere over the row, and holding the pointer near either edge.
    """

    #: Cells from an edge that count as "on the edge" for hover scrolling.
    EDGE = 2
    #: Cells moved per wheel notch.
    WHEEL_STEP = 6
    #: Cells per tick while hovering an edge, and how often that ticks.
    HOVER_STEP = 2
    HOVER_INTERVAL = 0.06

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._edge_timer = None
        self._edge_direction = 0

    def _viewport(self):
        try:
            return self.query_one("#tabs-scroll")
        except NoMatches:
            return None

    def _shift(self, cells: int) -> bool:
        """Scroll the row by ``cells``; True if there was anywhere to go.

        Works from ``scroll_target_x`` rather than the current offset: the
        offset settles a frame later, so back-to-back ticks would read a stale
        value, conclude the end had been reached and stop the scroll after a
        single step.
        """
        viewport = self._viewport()
        if viewport is None:
            return False
        limit = viewport.max_scroll_x
        current = viewport.scroll_target_x
        target = max(0, min(limit, current + cells))
        if target == current:
            return False
        # force=True is required: #tabs-scroll is `overflow: hidden`, so Textual
        # refuses to scroll it otherwise. Tabs' own _scroll_active_tab passes
        # the same flag for the same reason.
        viewport.scroll_to(x=target, animate=False, force=True)
        return True

    # ------------------------------------------------------------------ wheel

    # A terminal reports a plain wheel as vertical even when the content is a
    # row, so vertical notches have to drive horizontal movement here or the
    # wheel would do nothing at all over the tab bar.
    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self._shift(self.WHEEL_STEP):
            event.stop()
            event.prevent_default()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self._shift(-self.WHEEL_STEP):
            event.stop()
            event.prevent_default()

    def _on_mouse_scroll_right(self, event: events.MouseScrollRight) -> None:
        if self._shift(self.WHEEL_STEP):
            event.stop()
            event.prevent_default()

    def _on_mouse_scroll_left(self, event: events.MouseScrollLeft) -> None:
        if self._shift(-self.WHEEL_STEP):
            event.stop()
            event.prevent_default()

    # ------------------------------------------------------------ edge hover

    def _on_mouse_move(self, event: events.MouseMove) -> None:
        width = self.size.width
        if width <= 0:
            return
        if event.x <= self.EDGE:
            self._start_edge_scroll(-self.HOVER_STEP)
        elif event.x >= width - 1 - self.EDGE:
            self._start_edge_scroll(self.HOVER_STEP)
        else:
            self._stop_edge_scroll()

    def _on_leave(self, event: events.Leave) -> None:
        self._stop_edge_scroll()

    def _start_edge_scroll(self, direction: int) -> None:
        if self._edge_direction == direction and self._edge_timer is not None:
            return
        self._stop_edge_scroll()
        self._edge_direction = direction
        self._edge_timer = self.set_interval(
            self.HOVER_INTERVAL, lambda: self._edge_tick(direction)
        )

    def _edge_tick(self, direction: int) -> None:
        # Stop once the end is reached rather than ticking forever.
        if not self._shift(direction):
            self._stop_edge_scroll()

    def _stop_edge_scroll(self) -> None:
        if self._edge_timer is not None:
            self._edge_timer.stop()
            self._edge_timer = None
        self._edge_direction = 0

    def on_unmount(self) -> None:
        self._stop_edge_scroll()


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
        Binding("ctrl+w", "show_launcher", "Launcher", priority=True),
        Binding("ctrl+shift+w", "close_tab", "Close tab", priority=True),
        Binding("ctrl+pagedown", "next_tab", "Next tab", priority=True, show=False),
        Binding("ctrl+pageup", "prev_tab", "Prev tab", priority=True, show=False),
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
        sessions: SessionManager | None = None,
    ) -> None:
        super().__init__()
        config.ensure_layout()
        self.store = store or HostStore()
        self.vault = vault or Vault()
        self.themes = themes or ThemeRegistry()
        self.sessions = sessions or SessionManager()
        self.settings = settings_module.load()
        self.engine = CompletionEngine(
            hosts=lambda: self.store.hosts,
            tags=lambda: self.store.tags(),
            groups=lambda: self.store.group_names(),
            themes=lambda: self.themes.names(),
        )
        self._rows: dict[str, Completion] = {}
        self._nav: list[str] = []
        self._nav_index: int | None = None
        self._generation = 0
        self._theme_ready = False
        self._selected_host: Host | None = None

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            yield Static("", id="banner")
            yield SessionTabs(Tab("Launcher", id=LAUNCHER_TAB), id="tabs")
            with ContentSwitcher(initial=LAUNCHER_TAB, id="content"):
                with Vertical(id=LAUNCHER_TAB):
                    yield CommandBar(
                        placeholder=(
                            "Search hosts, or / for commands, @ for tags, # for groups"
                        ),
                        id="command-bar",
                    )
                    # One full-width column. Details used to live in a
                    # permanent right-hand pane; it now opens from a tile.
                    with VerticalScroll(id="body"):
                        yield Vertical(id="results")
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._restore_app_theme()
        self.query_one("#command-bar", CommandBar).focus()
        self._refresh_banner()
        self._refresh_results()
        self._warn_about_environment()

    # ------------------------------------------------------------------ chrome

    def _dom_alive(self) -> bool:
        """Whether the widget tree is still there to update.

        Background workers outlive the widget tree on shutdown - a connection
        attempt can still be in flight when the user quits - so anything that
        touches chrome from a worker has to tolerate it being torn down
        underneath. Without this, quitting mid-connect crashes the worker.
        """
        if not self.is_running:
            return False
        try:
            self.query_one("#root")
        except NoMatches:
            return False
        return True

    # --------------------------------------------------------------- app theme

    def _restore_app_theme(self) -> None:
        """Re-apply the theme picked from the command palette last time.

        Textual's `theme` is an in-memory reactive - it has no idea our config
        directory exists - so a choice made in the palette lasted only until
        the app closed.
        """
        wanted = self.settings.app_theme
        if wanted and wanted != self.theme:
            # A theme can vanish between versions, or the file can be edited by
            # hand. Falling back to the default beats refusing to start.
            if wanted in self.available_themes:
                self.theme = wanted
            else:
                self.settings.app_theme = ""
        self._theme_ready = True

    def watch_theme(self, theme: str) -> None:
        """Persist the palette's choice as soon as it is made."""
        if not getattr(self, "_theme_ready", False):
            return  # still starting up; nothing the user chose yet
        if theme == self.settings.app_theme:
            return
        self.settings.app_theme = theme
        try:
            settings_module.save(self.settings)
        except OSError:
            # A theme is not worth failing a keystroke over.
            pass

    def _refresh_banner(self) -> None:
        if not self._dom_alive():
            return
        lock = "[red]locked[/red]" if self.vault.is_locked else "[green]unlocked[/green]"
        if not self.vault.exists():
            lock = "[dim]not created[/dim]"
        count = len(self.store)
        live = len(self.sessions.live())
        self.query_one("#banner", Static).update(
            f"[b]Remotely[/b] [dim]v{__version__}[/dim]   "
            f"{count} host{'s' if count != 1 else ''}   "
            f"vault {lock}   "
            f"{live} session{'s' if live != 1 else ''}"
        )

    def active_session(self) -> Session | None:
        """The session on the current tab, or None on the launcher."""
        if not self._dom_alive():
            return None
        try:
            current = self._tabs().active
        except Exception:
            return None
        if not current or current == LAUNCHER_TAB:
            return None
        return self.sessions.get(current)

    def _context_host(self) -> Host | None:
        """The host a shortcut should act on, given where the user is.

        On a session tab the launcher's highlighted row is off screen, so acting
        on it edits or deletes something the user cannot see. The host behind
        the tab they are looking at is the only sane target there.
        """
        session = self.active_session()
        if session is not None:
            host = self.store.get(session.host_name)
            if host is not None:
                return host
        return self._selected_host

    def _status(self, message: str, *, error: bool = False) -> None:
        if not self._dom_alive():
            return
        widget = self.query_one("#status", Static)
        widget.set_class(error, "error")
        widget.update(message)

    def _warn_about_environment(self) -> None:
        """Only genuine problems. The binary carries its own SSH client, so
        there is nothing to check for the default path."""
        problems: list[str] = list(self.themes.errors)
        needs_system_ssh = [h.name for h in self.store if h.use_system_ssh]
        if needs_system_ssh and not system_ssh_available():
            names = ", ".join(needs_system_ssh[:3])
            problems.append(
                f"{len(needs_system_ssh)} host(s) are set to use system ssh "
                f"({names}) but ssh is not installed."
            )
        if problems:
            self._status("  ".join(problems), error=True)

    # ----------------------------------------------------------------- results

    def _results(self) -> Vertical:
        return self.query_one("#results", Vertical)

    def _refresh_results(self) -> None:
        if not self._dom_alive():
            return
        text = self.query_one("#command-bar", CommandBar).value
        container = self._results()
        container.remove_children()

        self._generation += 1
        self._rows = {}
        self._nav: list[str] = []

        if not text.strip():
            widgets = self._grouped_host_widgets()
        else:
            widgets = self._completion_widgets(self.engine.complete(text))

        if not widgets:
            container.mount(Static(self._empty_note(text), classes="empty-note"))
            self._nav_index = None
            self._selected_host = None
            return

        for widget in widgets:
            container.mount(widget)
        self._nav_index = None
        # Set the state now so ctrl+e has a target immediately, then paint the
        # highlight once the mounts have landed - mount() is asynchronous, so
        # querying for the widget on this pass finds nothing and the first row
        # would sit unhighlighted until the user pressed a key.
        self._highlight(0)
        self.call_after_refresh(self._highlight, 0)

    def _empty_note(self, text: str) -> str:
        """What to say when nothing matched.

        "No matches" is right for a search and wrong for /export, where the
        argument is a path you are about to create - there is nothing to match
        and the message read as though the command were broken.
        """
        parsed = parse(text)
        if parsed.mode == "command_arg" and parsed.command is not None:
            if parsed.command.arg_type == "path":
                return (
                    "[dim]Type a path and press enter — it does not have to exist yet.\n"
                    f"Example:  [/dim]/{parsed.command.name} ~/remotely-hosts.json"
                    + ("  [dim]--secrets[/dim]" if parsed.command.name == "export" else "")
                    + "\n[dim]Or run [/dim]/"
                    + parsed.command.name
                    + "[dim] with no path to be prompted.[/dim]"
                )
        return "[dim]No matches[/dim]"

    def _grouped_host_widgets(self) -> list:
        groups = self.store.groups()
        if not groups:
            return [
                Static(
                    "[dim]No hosts yet. Press ctrl+n or type /add to create one.[/dim]",
                    classes="empty-note",
                )
            ]
        boxes = []
        for group, hosts in groups.items():
            tiles = []
            for index, host in enumerate(hosts):
                row_id = f"host:{group}:{index}:{host.name}"
                self._rows[row_id] = CompletionEngine._host_completion(host)
                self._nav.append(row_id)
                tiles.append(self._make_tile(host, row_id, index))
            boxes.append(GroupBox(group, *tiles))
        return boxes

    def _make_tile(self, host: Host, row_id: str, index: int) -> HostTile:
        tile = HostTile(
            host,
            self.themes.get(host.theme),
            open_sessions=len(self.sessions.for_host(host.name)),
            alternate=bool(index % 2),
        )
        tile.id = self._widget_id(row_id)
        return tile

    def _completion_widgets(self, completions) -> list:
        """Search results: hosts stay tiles, everything else is a plain row.

        Hosts are hoisted above the rest, so the navigation order is built the
        same way - arrow keys have to walk the list you can see, not the order
        the engine happened to return.
        """
        tiles: list[HostTile] = []
        host_ids: list[str] = []
        rows: list = []
        other_ids: list[str] = []

        for index, completion in enumerate(completions):
            row_id = f"{completion.kind}:{index}:{completion.value}"
            if completion.kind == "host":
                host = self.store.get(completion.value)
                if host is None:
                    continue
                self._rows[row_id] = completion
                host_ids.append(row_id)
                tiles.append(self._make_tile(host, row_id, len(tiles)))
            else:
                self._rows[row_id] = completion
                other_ids.append(row_id)
                row = CompletionRow(row_id, self._render_completion(completion))
                row.id = self._widget_id(row_id)
                rows.append(row)

        self._nav.extend(host_ids + other_ids)
        widgets: list = []
        if tiles:
            widgets.append(GroupBox("Matches", *tiles))
        widgets.extend(rows)
        return widgets

    def _widget_id(self, row_id: str) -> str:
        """A DOM-safe, refresh-unique id for a row key.

        The generation counter is load-bearing. remove_children() is
        asynchronous, exactly like mount(), so the outgoing widgets are still
        registered when the replacements go in. Typing "#Pe" straight after
        "#P" regenerates the same row key, and Textual rejected the duplicate
        id with DuplicateIds - crashing the app mid-keystroke. Numbering each
        refresh means an id can never collide with one on its way out.
        """
        safe = "".join(c if c.isalnum() else "-" for c in row_id)
        return f"row-{self._generation}-{safe}"

    def _render_completion(self, completion: Completion) -> str:
        icons = {"command": "\u203a", "tag": "@", "group": "#", "theme": "\u25c8", "path": "\u2026"}
        icon = icons.get(completion.kind, "\u2022")
        return (
            f"[dim]{icon}[/dim] [b]{completion.label}[/b]  "
            f"[dim]{completion.description}[/dim]"
        )

    # ------------------------------------------------------------- navigation

    def _nav_widget(self, row_id: str):
        try:
            return self.query_one(f"#{self._widget_id(row_id)}")
        except NoMatches:
            return None

    def _highlight(self, index: int | None) -> None:
        """Move the highlight, which is what enter and ctrl+e act on."""
        for row_id in self._nav:
            widget = self._nav_widget(row_id)
            if widget is not None:
                widget.remove_class("-highlight")

        if index is None or not self._nav:
            self._nav_index = None
            self._selected_host = None
            return

        index = max(0, min(len(self._nav) - 1, index))
        self._nav_index = index
        row_id = self._nav[index]
        widget = self._nav_widget(row_id)
        if widget is not None:
            widget.add_class("-highlight")
            widget.scroll_visible(animate=False)

        completion = self._rows.get(row_id)
        self._selected_host = (
            self.store.get(completion.value)
            if completion is not None and completion.kind == "host"
            else None
        )

    def _highlighted_completion(self) -> Completion | None:
        if self._nav_index is None or self._nav_index >= len(self._nav):
            return None
        return self._rows.get(self._nav[self._nav_index])

    def _host_detail_body(self, host: Host) -> str:
        """The long-form description shown by the details icon."""
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

        open_count = len(self.sessions.for_host(host.name))
        lines = [
            f"[dim]target[/dim]   {host.target}",
            f"[dim]port[/dim]     {host.port}",
            f"[dim]group[/dim]    {host.group}",
            f"[dim]tags[/dim]     {' '.join('@' + t for t in host.tags) or '[dim]none[/dim]'}",
            f"[dim]theme[/dim]    [{theme.accent}]{theme.icon} {theme.name}[/]",
            f"[dim]auth[/dim]     {auth}",
            f"[dim]client[/dim]   {'system ssh' if host.use_system_ssh else 'built-in'}",
        ]
        if open_count:
            lines.append(f"[dim]open[/dim]     {open_count} session(s)")
        lines += ["", "[dim]ssh options[/dim]", options_text]

        credential_kind = None
        if host.auth_mode == "credential" and host.credential and not self.vault.is_locked:
            cred = self.vault.get(host.credential)
            credential_kind = cred.kind if cred else None
        for warning in host.warnings(credential_kind=credential_kind):
            lines += ["", f"[yellow]{warning}[/yellow]"]

        if host.description:
            lines += ["", f"[dim]{host.description}[/dim]"]
        return "\n".join(lines)

    # ------------------------------------------------------------- input events

    @on(Input.Changed, "#command-bar")
    def _on_changed(self) -> None:
        self._refresh_results()

    @on(CommandBar.Navigate)
    def _on_navigate(self, event: CommandBar.Navigate) -> None:
        if not self._nav:
            return
        current = self._nav_index if self._nav_index is not None else -1
        self._highlight(current + event.delta)

    @on(HostTile.Launch)
    def _on_tile_launch(self, event: HostTile.Launch) -> None:
        self._connect(event.host_name)

    @on(HostTile.Action)
    def _on_tile_action(self, event: HostTile.Action) -> None:
        self._tile_action(event.host_name, event.action)

    @on(CompletionRow.Chosen)
    def _on_row_chosen(self, event: CompletionRow.Chosen) -> None:
        completion = self._rows.get(event.row_id)
        if completion is not None:
            self._activate(completion)

    def _tile_action(self, host_name: str, action: str) -> None:
        host = self.store.get(host_name)
        if host is None:
            self._status(f"No host named {host_name!r}.", error=True)
            return
        if action == "copy-name":
            self.copy_to_clipboard(host.name)
            self._status(f"Copied [b]{host.name}[/b] to the clipboard.")
        elif action == "copy-target":
            target = f"{host.username}@{host.hostname}" if host.username else host.hostname
            self.copy_to_clipboard(target)
            self._status(f"Copied [b]{target}[/b] to the clipboard.")
        elif action == "edit":
            self._edit_host(host.name)
        elif action == "details":
            self.push_screen(
                HostDetailScreen(host.name, self._host_detail_body(host))
            )

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

    @on(Input.Submitted, "#command-bar")
    def _on_submitted(self) -> None:
        text = self.query_one("#command-bar", CommandBar).value
        parsed = parse(text)

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

    # -------------------------------------------------------------- dispatching

    def _run_command(self, name: str, argument: str) -> None:
        argument = argument.strip()
        actions = {
            "connect": lambda: self._connect(argument),
            "add": self.action_new_host,
            "edit": lambda: self._edit_host(argument),
            "delete": lambda: self._delete_host(argument),
            "sessions": self.action_sessions,
            "close": lambda: self._close_by_name(argument),
            "themes": self.action_themes,
            "vault": self.action_vault,
            "settings": self.action_settings,
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
        if not self._dom_alive():
            return
        self.query_one("#command-bar", CommandBar).value = ""

    # ------------------------------------------------------------------- vault

    @work
    async def _unlock_vault(self, reason: str = "") -> bool:
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

    # ------------------------------------------------------------------ tabs

    def _tabs(self) -> SessionTabs:
        return self.query_one("#tabs", SessionTabs)

    def _switch_to(self, tab_id: str) -> None:
        self.query_one("#content", ContentSwitcher).current = tab_id
        self._sync_banner_visibility(tab_id)
        tabs = self._tabs()
        if tabs.active != tab_id:
            tabs.active = tab_id

    def action_show_launcher(self) -> None:
        if not self._dom_alive():
            return
        self._switch_to(LAUNCHER_TAB)
        self.query_one("#command-bar", CommandBar).focus()

    def action_next_tab(self) -> None:
        self._tabs().action_next_tab()

    def action_prev_tab(self) -> None:
        self._tabs().action_previous_tab()

    @on(Tabs.TabActivated, "#tabs")
    def _on_tab_activated(self, event: Tabs.TabActivated) -> None:
        tab_id = event.tab.id
        if not tab_id:
            return
        self.query_one("#content", ContentSwitcher).current = tab_id
        self._sync_banner_visibility(tab_id)
        if tab_id == LAUNCHER_TAB:
            self.query_one("#command-bar", CommandBar).focus()
        else:
            try:
                self.query_one(f"#{tab_id}", TerminalPane).focus()
            except Exception:
                pass

    def _sync_banner_visibility(self, tab_id: str) -> None:
        """The banner is launcher chrome; a session wants the row for output."""
        if not self._dom_alive():
            return
        try:
            self.query_one("#banner", Static).display = tab_id == LAUNCHER_TAB
        except NoMatches:
            pass

    @on(events.TextSelected)
    def _on_text_selected(self, event: events.TextSelected) -> None:
        """Copy a finished drag-selection straight to the clipboard.

        Textual binds copy to ctrl+c / super+c, but ctrl+c has to reach the
        remote shell - taking it would break interrupting a command, which
        matters far more. Copying on release is what a terminal does anyway,
        and it leaves no keystroke to discover.
        """
        if not self._dom_alive():
            return
        try:
            text = self.screen.get_selected_text()
        except Exception:
            return
        if not text or not text.strip():
            return
        self.copy_to_clipboard(text)
        lines = text.count("\n") + 1
        unit = "line" if lines == 1 else "lines"
        self._status(f"Copied {lines} {unit} to the clipboard.")

    @on(TerminalPane.Closed)
    def _on_pane_closed(self, event: TerminalPane.Closed) -> None:
        session = event.session
        status = session.exit_status
        detail = f" (exit {status})" if status not in (None, 0) else ""
        if session.error:
            self._status(f"[b]{session.host_name}[/b]: {session.error}", error=True)
        else:
            self._status(f"Session [b]{session.host_name}[/b] ended{detail}.")
        self._refresh_banner()
        self._refresh_results()

    # A tab is deliberately NOT retitled from the remote's OSC title. Shells set
    # that to whatever they like - commonly "user@host", sometimes the running
    # command - so tabs drifted into showing different things depending on which
    # box you connected to. The tab names the host record you launched, always.

    @on(TerminalPane.RetryRequested)
    def _on_retry_requested(self, event: TerminalPane.RetryRequested) -> None:
        self._retry_session(event.session.id)

    @work
    async def _retry_session(self, session_id: str) -> None:
        """Reconnect a dead session in its existing tab."""
        session = self.sessions.get(session_id)
        if session is None:
            return
        if session.is_live:
            return  # already back; a double press must not tear it down

        host = self.store.get(session.host_name)
        if host is None:
            self._status(
                f"{session.host_name} is no longer in your hosts.", error=True
            )
            return

        credential = await self._credential_for(host)
        if credential is False:  # cancelled or unavailable; message already shown
            return

        try:
            pane = self.query_one(f"#{session.id}", TerminalPane)
        except NoMatches:
            return
        cols = max(pane.size.width, 20)
        rows = max(pane.size.height, 10)

        try:
            self.sessions.reopen(
                session, host, credential, cols=cols, rows=rows
            )
        except TransportError as exc:
            self._status(str(exc), error=True)
            return

        pane.restart()
        self._refresh_banner()
        self._refresh_results()
        self._watch_connection(session)

    async def _credential_for(self, host: Host) -> Credential | None | bool:
        """The host's credential, or False when it could not be obtained.

        False rather than None because None is the legitimate answer for hosts
        that use an agent or a bare key.
        """
        if not (host.auth_mode == "credential" and host.credential):
            return None
        if not await self._ensure_vault(f"Unlock the vault to connect to {host.name}."):
            self._status("Cancelled: vault stayed locked.", error=True)
            return False
        try:
            return self.vault.require(host.credential)
        except VaultError as exc:
            self._status(str(exc), error=True)
            return False

    @on(SessionTab.CloseRequested)
    def _on_tab_close_clicked(self, event: SessionTab.CloseRequested) -> None:
        self._request_close(event.tab_id)

    def action_close_tab(self) -> None:
        """Close the session on the current tab."""
        current = self._tabs().active
        if not current or current == LAUNCHER_TAB:
            self._status("No session tab is selected.")
            return
        self._request_close(current)

    @work
    async def _request_close(self, session_id: str) -> None:
        await self._close_with_confirm(session_id)

    async def _close_with_confirm(self, session_id: str) -> bool:
        """Close a tab, confirming first when the preference asks for it.

        A dead session has nothing left to lose, so it closes straight away
        regardless - confirming there would be pure nagging.

        Awaitable rather than a worker of its own so the sessions dialog can
        close a tab and then carry on with its own loop.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return False
        if self.settings.confirm_close_tab and session.is_live:
            confirmed = await self.push_screen_wait(
                ConfirmScreen(
                    f"Close the session to {session.host_name}?",
                    detail="The connection will be dropped. "
                    "Turn this prompt off in /settings.",
                    confirm_label="Close",
                )
            )
            if not confirmed:
                return False
        self._close_session(session_id)
        return True

    # --------------------------------------------------------------- connecting

    @work
    async def _connect(self, host_name: str) -> None:
        host = self.store.get(host_name)
        if host is None:
            self._status(f"No host named {host_name!r}.", error=True)
            return

        credential = await self._credential_for(host)
        if credential is False:  # cancelled or unavailable; already reported
            return

        theme = self.themes.get(host.theme)
        body = self.query_one("#body")
        cols = max(body.size.width - 2, 20)
        rows = max(body.size.height - 2, 10)

        try:
            session = self.sessions.open(host, credential, theme, cols=cols, rows=rows)
        except TransportError as exc:
            self._status(str(exc), error=True)
            return

        pane = TerminalPane(session, id=session.id)
        styles = theme.pane_styles()
        self.query_one("#content", ContentSwitcher).mount(pane)
        pane.styles.background = styles["background"]
        pane.styles.color = styles["color"]

        self._tabs().add_tab(
            SessionTab(session.host_name, theme=theme, id=session.id)
        )
        self._switch_to(session.id)

        self._status(f"Connecting to [b]{host.name}[/b]…")
        self._clear_bar()
        self._refresh_banner()
        self._watch_connection(session)

    @work
    async def _watch_connection(self, session: Session) -> None:
        """Report the outcome of the handshake without blocking the UI."""
        import asyncio

        for _ in range(600):  # up to ~60s, generous for slow bastions
            if session.status != "connecting":
                break
            if not self.is_running:
                return  # quit while connecting; nothing left to report to
            await asyncio.sleep(0.1)

        if session.status == "error":
            # Stay on the failed tab: the pane shows the error and what to do
            # about it. Bouncing back to the launcher hides both.
            self._status(
                f"[b]{session.host_name}[/b]: {session.error}  "
                f"[dim](ctrl+shift+w closes this tab)[/dim]",
                error=True,
            )
        elif session.status == "connected":
            # No "Connected to X" chatter: the tab and the live pane already say
            # so. Notes are warnings (options the built-in client cannot honour,
            # for instance) and would be lost silently, so those still show.
            if session.notes:
                self._status(f"[b]{session.host_name}[/b]: {' '.join(session.notes)}")
            else:
                self._status("")
        self._refresh_banner()
        self._refresh_results()

    def _close_by_name(self, name: str) -> None:
        """/close with no argument closes the current tab."""
        if not name:
            self.action_close_tab()
            return
        matches = self.sessions.for_host(name)
        if not matches:
            self._status(f"No open session for {name!r}.", error=True)
            return
        for session in matches:
            self._request_close(session.id)

    def _close_session(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        host_name = session.host_name
        session.close()
        self.sessions.remove(session_id)
        try:
            self._tabs().remove_tab(session_id)
        except Exception:
            pass
        try:
            self.query_one(f"#{session_id}", TerminalPane).remove()
        except Exception:
            pass
        # Land on whatever tab is left rather than always the launcher, so
        # closing one of several sessions does not eject you from the others.
        remaining = self.sessions.list()
        if remaining:
            self._switch_to(remaining[-1].id)
        else:
            self.action_show_launcher()
        self._refresh_banner()
        self._refresh_results()
        self._status(f"Closed session to [b]{host_name}[/b].")

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

    async def _apply_form_result(self, result: HostFormResult) -> Host | None:
        """Store the form's new credential, if any, and bind the host to it.

        Returns the host to save, or ``None`` when the credential could not be
        stored - in which case nothing at all is written, so the host never ends
        up referring to a credential the vault does not have.
        """
        draft = result.credential
        if draft is None:
            return result.host
        if not await self._ensure_vault("Unlock the vault to save this credential."):
            self._status("Cancelled: vault stayed locked.", error=True)
            return None
        if draft.name:
            if self.vault.get(draft.name) is not None:
                self._status(
                    f"A credential named [b]{draft.name}[/b] already exists. "
                    "Pick another name, or select it from the list.",
                    error=True,
                )
                return None
            name = draft.name
        else:
            name = unique_credential_name(
                default_credential_name(result.host.name), self.vault.names()
            )
        try:
            self.vault.put(replace(draft, name=name))
        except VaultError as exc:
            self._status(str(exc), error=True)
            return None
        return replace(result.host, auth_mode="credential", credential=name)

    @staticmethod
    def _credential_note(result: HostFormResult, saved: Host) -> str:
        """Trailer naming the credential a save just created, if it created one.

        Said as part of the host's own status line rather than on its own: the
        add/update message lands straight after and would wipe it.
        """
        if result.credential is None or not saved.credential:
            return ""
        return f" Credential [b]{saved.credential}[/b] saved to the vault."

    @work
    async def action_new_host(self) -> None:
        result = await self.push_screen_wait(
            HostFormScreen(
                themes=self.themes.names(),
                credentials=[] if self.vault.is_locked else self.vault.names(),
                groups=self.store.group_names(),
                vault_locked=self.vault.is_locked,
            )
        )
        if result is None:
            return
        host = await self._apply_form_result(result)
        if host is None:
            return
        try:
            self.store.add(host)
        except StoreError as exc:
            self._status(str(exc), error=True)
            return
        self._refresh_banner()
        self._refresh_results()
        self._status(f"Added [b]{host.name}[/b].{self._credential_note(result, host)}")

    @work
    async def action_edit_host(self) -> None:
        target = self._context_host()
        await self._edit_host_async(target.name if target else "")

    def _edit_host(self, name: str) -> None:
        self._edit_host_worker(name)

    @work
    async def _edit_host_worker(self, name: str) -> None:
        await self._edit_host_async(name)

    async def _edit_host_async(self, name: str) -> None:
        host = self.store.get(name) if name else self._context_host()
        if host is None:
            self._status("Select a host to edit first.", error=True)
            return
        result = await self.push_screen_wait(
            HostFormScreen(
                host,
                themes=self.themes.names(),
                credentials=[] if self.vault.is_locked else self.vault.names(),
                groups=self.store.group_names(),
                vault_locked=self.vault.is_locked,
            )
        )
        if result is None:
            return
        updated = await self._apply_form_result(result)
        if updated is None:
            return
        try:
            self.store.update(host.name, updated)
        except StoreError as exc:
            self._status(str(exc), error=True)
            return
        self._refresh_banner()
        self._refresh_results()
        self._status(
            f"Updated [b]{updated.name}[/b].{self._credential_note(result, updated)}"
        )

    @work
    async def action_delete_host(self) -> None:
        target = self._context_host()
        await self._delete_host_async(target.name if target else "")

    def _delete_host(self, name: str) -> None:
        self._delete_host_worker(name)

    @work
    async def _delete_host_worker(self, name: str) -> None:
        await self._delete_host_async(name)

    async def _delete_host_async(self, name: str) -> None:
        host = self.store.get(name) if name else self._context_host()
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
            if choice.action == "reload":
                self.themes.reload()
                self._status("Themes reloaded.")
                continue
            selected = choice.selection
            if selected is None:
                continue
            name = await self.push_screen_wait(
                TextPromptScreen(
                    f"Clone '{selected}' as",
                    value=f"{selected}-custom",
                    help=f"Creates a new .toml in {config.themes_dir()}.",
                )
            )
            if name is None:
                continue
            try:
                path = self.themes.copy_to_user_dir(selected, name)
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
                CredentialListScreen(credentials, usage)
            )
            if choice is None:
                return
            if choice.action == "lock":
                self._lock_vault()
                return
            if choice.action == "new":
                created = await self.push_screen_wait(
                    CredentialFormScreen(existing_names=self.vault.names())
                )
                if created is not None:
                    try:
                        self.vault.put(created)
                        self._status(f"Saved credential [b]{created.name}[/b].")
                    except VaultError as exc:
                        self._status(str(exc), error=True)
                continue
            if choice.selection is None:
                continue
            if choice.action == "delete":
                if not await self._delete_credential(choice.selection):
                    continue
                continue
            await self._edit_credential(choice.selection)

    async def _delete_credential(self, name: str) -> bool:
        """Delete a credential, refusing while any host still points at it.

        Refusing rather than cascading is deliberate: the hosts would otherwise
        be left referring to a name that no longer exists, and that only
        surfaces later, at connect time.
        """
        users = self.store.using_credential(name)
        if users:
            listed = ", ".join(h.name for h in users[:4])
            if len(users) > 4:
                listed += f", and {len(users) - 4} more"
            edit = await self.push_screen_wait(
                ConfirmScreen(
                    f"'{name}' is used by {len(users)} "
                    f"host{'s' if len(users) != 1 else ''}",
                    detail=f"{listed}. Deleting it would leave "
                    f"{'them' if len(users) != 1 else 'it'} unable to connect. "
                    "Edit it instead - every host follows the change - or point "
                    "those hosts elsewhere first.",
                    confirm_label="Edit instead",
                )
            )
            if edit:
                await self._edit_credential(name)
            return False

        confirmed = await self.push_screen_wait(
            ConfirmScreen(
                f"Delete credential '{name}'?",
                detail="No host uses it. The stored secret is gone for good.",
                confirm_label="Delete",
            )
        )
        if not confirmed:
            return False
        try:
            self.vault.delete(name)
        except VaultError as exc:
            self._status(str(exc), error=True)
            return False
        self._status(f"Deleted credential [b]{name}[/b].")
        return True

    async def _edit_credential(self, name: str) -> None:
        existing = self.vault.get(name)
        if existing is None:
            return
        edited = await self.push_screen_wait(
            CredentialFormScreen(existing, existing_names=self.vault.names())
        )
        if edited is None:
            return
        renamed = edited.name.lower() != existing.name.lower()
        try:
            if renamed:
                self.vault.delete(existing.name)
            self.vault.put(edited)
        except VaultError as exc:
            self._status(str(exc), error=True)
            return
        note = ""
        if renamed:
            # Only after the vault write succeeded: hosts must never be moved
            # onto a name the vault does not actually have.
            moved = self.store.rebind_credential(existing.name, edited.name)
            if moved:
                note = f" Repointed {moved} host{'s' if moved != 1 else ''}."
                self._refresh_results()
        self._status(f"Saved credential [b]{edited.name}[/b].{note}")

    @work
    async def action_sessions(self) -> None:
        # Loops, because closing one tab is rarely the only thing you came to
        # do: the dialog reopens on the remaining sessions instead of making
        # you summon it again per tab.
        while True:
            sessions = self.sessions.list()
            if not sessions:
                self._status("No open sessions.")
                return
            choice = await self.push_screen_wait(
                ListPickerScreen(
                    "Open sessions",
                    session_options(sessions),
                    help_text=(
                        "Select a session to switch to it, "
                        "or close tabs without leaving here."
                    ),
                    empty_text="No open sessions.",
                    extra_buttons=[
                        ("closeone", "Close Selected Tab"),
                        ("closeall", "Close All Tabs"),
                    ],
                )
            )
            if choice is None:
                return
            if choice.action == "closeall":
                if await self._close_all_sessions(sessions):
                    return
                continue
            if choice.action == "closeone":
                if choice.selection is None:
                    self._status("Highlight a session to close it.", error=True)
                    continue
                await self._close_with_confirm(choice.selection)
                continue
            if choice.selection:
                self._switch_to(choice.selection)
            return

    async def _close_all_sessions(self, sessions: list[Session]) -> bool:
        """Close every session, confirming once. False if the user backed out."""
        live = [s for s in sessions if s.is_live]
        # One prompt for the batch, not one per session.
        if self.settings.confirm_close_tab and live:
            confirmed = await self.push_screen_wait(
                ConfirmScreen(
                    f"Close all {len(sessions)} sessions?",
                    detail=f"{len(live)} are still connected. "
                    "Turn this prompt off in /settings.",
                    confirm_label="Close all",
                )
            )
            if not confirmed:
                return False
        for session in list(sessions):
            self._close_session(session.id)
        self._status("Closed all sessions.")
        return True

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
                self.store, Path(raw_path), vault=self.vault, include_secrets=include_secrets
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

    # ------------------------------------------------------------------ closing

    @work
    async def action_quit(self) -> None:
        """Quit, confirming first when sessions would be lost.

        Sessions live in this process, so quitting drops every one of them -
        which is exactly why this is worth asking about.
        """
        live = self.sessions.live()
        if self.settings.confirm_quit and live:
            names = ", ".join(sorted({s.host_name for s in live})[:4])
            more = "" if len(live) <= 4 else f" and {len(live) - 4} more"
            confirmed = await self.push_screen_wait(
                ConfirmScreen(
                    f"Quit and close {len(live)} open session"
                    f"{'s' if len(live) != 1 else ''}?",
                    detail=f"{names}{more}. Sessions do not survive quitting. "
                    "Turn this prompt off in /settings.",
                    confirm_label="Quit",
                )
            )
            if not confirmed:
                return
        self.sessions.close_all()
        self.exit()

    @work
    async def action_settings(self) -> None:
        """Preferences, toggled from the palette."""
        while True:
            rows = [
                Option(
                    f"[b]{label}[/b]   "
                    f"{'[green]on[/green]' if getattr(self.settings, name) else '[dim]off[/dim]'}"
                    f"\n    [dim]{help_text}[/dim]",
                    id=name,
                )
                for name, (label, help_text) in settings_module.LABELS.items()
            ]
            choice = await self.push_screen_wait(
                ListPickerScreen(
                    "Settings",
                    rows,
                    help_text=(
                        "Select a preference to toggle it. "
                        f"Stored in {config.settings_file()}."
                    ),
                )
            )
            if choice is None or choice.action or choice.selection is None:
                return
            try:
                new_value = self.settings.toggle(choice.selection)
            except KeyError:
                continue
            try:
                settings_module.save(self.settings)
            except OSError as exc:
                self._status(f"Could not save settings: {exc}", error=True)
                return
            label = settings_module.LABELS[choice.selection][0]
            self._status(f"{label}: {'on' if new_value else 'off'}.")


def run(**kwargs: Any) -> None:
    RemotelyApp(**kwargs).run()
