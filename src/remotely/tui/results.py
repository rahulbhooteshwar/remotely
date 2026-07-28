"""Launcher result widgets.

The host list is built from real widgets rather than an OptionList so that a
row can have clickable regions: the tile launches, the icons at its right edge
each do something else. Hit-testing by column would have worked, but letting
each icon be its own widget means Textual does the layout and the geometry
cannot drift out of step with the drawing - the class of bug that made the tab
close control fire on the wrong cells.

Nothing here talks to the store or the vault; widgets post messages and the app
decides what they mean.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Static

from ..models import Host
from ..themes import Theme

#: Actions offered on every host tile, in the order they are drawn.
#: Single-width BMP glyphs, deliberately not emoji: a terminal that cannot
#: render an emoji shows a replacement box and, worse, gets the width wrong,
#: which would shift every icon beside it.
ACTIONS: tuple[tuple[str, str, str], ...] = (
    ("copy-name", "⧉", "Copy host name"),
    ("copy-target", "❯", "Copy user@host"),
    ("edit", "✎", "Edit host"),
    ("details", "◉", "View details"),
)

#: Cells each icon occupies, including its padding.
ICON_WIDTH = 3


class IconButton(Static):
    """One clickable action at the right edge of a tile."""

    def __init__(self, host_name: str, action: str, glyph: str, tooltip: str) -> None:
        super().__init__(glyph, classes="tile-icon")
        self.host_name = host_name
        self.action = action
        self.tooltip = tooltip

    def _on_click(self, event) -> None:
        # Stop it here or the tile beneath would also see the click and launch
        # a session, which is the opposite of what the icon is for.
        event.stop()
        event.prevent_default()
        self.post_message(HostTile.Action(self.host_name, self.action))


class HostTile(Vertical):
    """One host: two rows of detail, plus its actions.

    Clicking anywhere except the icons launches the host, which is the common
    case and so gets the large target.
    """

    class Launch(Message):
        def __init__(self, host_name: str) -> None:
            super().__init__()
            self.host_name = host_name

    class Action(Message):
        def __init__(self, host_name: str, action: str) -> None:
            super().__init__()
            self.host_name = host_name
            self.action = action

    def __init__(
        self,
        host: Host,
        theme: Theme,
        *,
        open_sessions: int = 0,
        alternate: bool = False,
    ) -> None:
        super().__init__(classes="host-tile" + (" alt" if alternate else ""))
        self.host = host
        self.host_theme = theme
        self.open_sessions = open_sessions

    def compose(self) -> ComposeResult:
        host = self.host
        live = (
            f"  [{self.host_theme.accent}]({self.open_sessions} open)[/]"
            if self.open_sessions
            else ""
        )
        target = f"{host.username}@{host.hostname}:{host.port}" if host.username else (
            f"{host.hostname}:{host.port}"
        )
        tags = "  ".join(f"@{tag}" for tag in host.tags) or "[dim]no tags[/dim]"

        with Horizontal(classes="tile-body"):
            with Vertical(classes="tile-text"):
                yield Static(
                    f"[b]{host.name}[/b]{live}   [dim]{target}[/dim]",
                    classes="tile-line",
                )
                yield Static(f"[dim]{tags}[/dim]", classes="tile-line")
            with Horizontal(classes="tile-actions"):
                for action, glyph, tooltip in ACTIONS:
                    yield IconButton(host.name, action, glyph, tooltip)

    def on_mount(self) -> None:
        # The theme reads as a stripe down the left edge, so a production box
        # is obvious in a long list without relying on an emoji.
        self.styles.border_left = ("thick", self.host_theme.accent)

    def _on_click(self, event) -> None:
        # Icons stop their own clicks, so anything arriving here is the tile.
        event.stop()
        self.post_message(self.Launch(self.host.name))


class GroupBox(Vertical):
    """A titled, bordered box holding one group's tiles."""

    def __init__(self, group: str, *tiles: HostTile) -> None:
        super().__init__(*tiles, classes="group-box")
        count = len(tiles)
        self.border_title = f" {group} "
        self.border_subtitle = f" {count} host{'s' if count != 1 else ''} "


class CompletionRow(Static):
    """A single-line result for anything that is not a host.

    Commands, tags and groups have nothing to show on a second row and are not
    launchable, so they stay a plain row rather than pretending to be a tile.
    """

    class Chosen(Message):
        def __init__(self, row_id: str) -> None:
            super().__init__()
            self.row_id = row_id

    def __init__(self, row_id: str, markup: str) -> None:
        super().__init__(markup, classes="completion-row")
        self.row_id = row_id

    def _on_click(self, event) -> None:
        event.stop()
        self.post_message(self.Chosen(self.row_id))
