#!/usr/bin/env python3
"""HomeBox read-only TUI.

Requires EMAIL, PASSWORD, URL environment variables.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)
from textual.screen import Screen
from textual.message import Message

from homebox_api import HomeBoxClient, HomeBoxError


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _fmt(value, default="") -> str:
    if value is None:
        return default
    return str(value)


# ---------------------------------------------------------------------------
# Detail panel
# ---------------------------------------------------------------------------

class ItemDetail(Static):
    """Displays details for the selected item."""

    DEFAULT_CSS = """
    ItemDetail {
        height: 100%;
        padding: 1 2;
        border-left: solid $primary-darken-2;
        overflow-y: auto;
    }
    """

    def show_item(self, item: dict | None):
        if item is None:
            self.update("")
            return

        lines: list[str] = []
        lines.append(f"[bold accent]{item.get('name', 'Unknown')}[/bold accent]\n")

        def row(key: str, val):
            if val not in (None, "", [], {}):
                lines.append(f"[dim]{key}:[/dim] {val}")

        loc = (item.get("location") or {}).get("name")
        row("Location", loc)
        tags = ", ".join(t["name"] for t in (item.get("tags") or []))
        row("Tags", tags)
        row("Quantity", item.get("quantity"))
        row("Asset ID", item.get("assetId"))
        row("Serial #", item.get("serialNumber"))
        row("Manufacturer", item.get("manufacturer"))
        row("Model", item.get("modelNumber"))
        row("Purchase Price", item.get("purchasePrice"))
        row("Purchase From", item.get("purchaseFrom"))
        pt = item.get("purchaseTime", "")
        row("Purchase Date", pt[:10] if pt else None)
        row("Insured", item.get("insured"))
        we = item.get("warrantyExpires", "")
        row("Warranty Expires", we[:10] if we else None)
        row("Lifetime Warranty", item.get("lifetimeWarranty"))
        row("Warranty Details", item.get("warrantyDetails"))
        row("Archived", item.get("archived"))

        desc = item.get("description")
        if desc:
            lines.append(f"\n[dim]Description:[/dim]\n{desc}")

        notes = item.get("notes")
        if notes:
            lines.append(f"\n[dim]Notes:[/dim]\n{notes}")

        for f in (item.get("fields") or []):
            val = f.get("textValue") or f.get("numberValue") or f.get("booleanValue")
            if val not in (None, ""):
                row(f"  {f.get('name', 'Field')}", val)

        self.update("\n".join(lines))


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

class SidebarList(ListView):
    """A labeled list for sidebar sections."""


class Sidebar(Vertical):
    DEFAULT_CSS = """
    Sidebar {
        width: 22;
        border-right: solid $primary-darken-2;
    }
    Sidebar Label {
        text-style: bold;
        color: $accent;
        padding: 1 1 0 1;
    }
    Sidebar ListView {
        height: auto;
        border: none;
    }
    Sidebar ListItem {
        padding: 0 1;
    }
    """

    class FilterSelected(Message):
        def __init__(self, kind: str, filter_id: str | None, name: str) -> None:
            super().__init__()
            self.kind = kind           # "location" or "tag"
            self.filter_id = filter_id  # None means "All"
            self.name = name

    def __init__(self, locations: list[dict], tags: list[dict]):
        super().__init__()
        self._locations = locations
        self._tags = tags

    def compose(self) -> ComposeResult:
        yield Label("Locations")
        loc_items = [ListItem(Label("All"))] + [
            ListItem(Label(loc["name"]), id=f"loc-{loc['id']}")
            for loc in self._locations
        ]
        yield SidebarList(*loc_items, id="loc-list")

        yield Label("Tags")
        tag_items = [ListItem(Label("All"))] + [
            ListItem(Label(tg["name"]), id=f"tag-{tg['id']}")
            for tg in self._tags
        ]
        yield SidebarList(*tag_items, id="tag-list")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        display_text = str(event.item.query_one(Label).renderable)

        if event.list_view.id == "loc-list":
            if item_id is None:
                self.post_message(self.FilterSelected("location", None, "All"))
            else:
                self.post_message(self.FilterSelected("location", item_id.removeprefix("loc-"), display_text))
        else:
            if item_id is None:
                self.post_message(self.FilterSelected("tag", None, "All"))
            else:
                self.post_message(self.FilterSelected("tag", item_id.removeprefix("tag-"), display_text))


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------

class MainScreen(Screen):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("/", "focus_search", "Search"),
        Binding("escape", "clear_search", "Clear", show=False),
    ]

    DEFAULT_CSS = """
    MainScreen {
        layout: vertical;
    }
    #search-bar {
        height: 3;
        padding: 0 1;
        dock: top;
    }
    #main-body {
        layout: horizontal;
        height: 1fr;
    }
    #items-panel {
        layout: vertical;
        width: 1fr;
    }
    #items-table {
        height: 1fr;
    }
    #detail-panel {
        width: 40;
        height: 100%;
    }
    #status-bar {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        dock: bottom;
    }
    """

    def __init__(self, client: HomeBoxClient):
        super().__init__()
        self._client = client
        self._all_items: list[dict] = []
        self._filtered_items: list[dict] = []
        self._location_filter: str | None = None
        self._tag_filter: str | None = None
        self._search_query: str = ""
        self._locations: list[dict] = []
        self._tags: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="Search items... (press / to focus)", id="search-bar")
        with Horizontal(id="main-body"):
            yield Sidebar(self._locations, self._tags)
            with Vertical(id="items-panel"):
                yield DataTable(id="items-table", cursor_type="row")
            yield ItemDetail(id="detail-panel")
        yield Static("Loading...", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#items-table", DataTable)
        table.add_columns("Name", "Location", "Tags", "Qty")
        self.run_worker(self._load_all(), exclusive=True, group="loader")

    async def _load_all(self) -> None:
        status = self.query_one("#status-bar", Static)
        status.update("Loading data...")
        try:
            self._locations, self._tags, self._all_items = await asyncio.gather(
                self._client.get_locations(),
                self._client.get_tags(),
                self._client.get_all_items(),
            )
        except HomeBoxError as e:
            status.update(f"[red]Error: {e}[/red]")
            return

        # Rebuild sidebar with real data
        sidebar = self.query_one(Sidebar)
        await sidebar.remove()
        new_sidebar = Sidebar(self._locations, self._tags)
        await self.query_one("#main-body", Horizontal).mount(new_sidebar, before=self.query_one("#items-panel"))

        self._filtered_items = list(self._all_items)
        self._rebuild_table()
        status.update(f"{len(self._all_items)} items loaded")

    def _rebuild_table(self) -> None:
        table = self.query_one("#items-table", DataTable)
        table.clear()
        for item in self._filtered_items:
            loc = (item.get("location") or {}).get("name", "")
            tags = ", ".join(t["name"] for t in (item.get("tags") or []))
            qty = _fmt(item.get("quantity"))
            table.add_row(
                item.get("name", ""),
                loc,
                tags,
                qty,
                key=item.get("id"),
            )
        self.query_one("#detail-panel", ItemDetail).show_item(None)

    def _apply_filters(self) -> None:
        result = self._all_items
        if self._location_filter:
            result = [
                i for i in result
                if (i.get("location") or {}).get("id") == self._location_filter
            ]
        if self._tag_filter:
            result = [
                i for i in result
                if any(t["id"] == self._tag_filter for t in (i.get("tags") or []))
            ]
        if self._search_query:
            q = self._search_query.lower()
            result = [
                i for i in result
                if q in (i.get("name") or "").lower()
                or q in (i.get("description") or "").lower()
            ]
        self._filtered_items = result
        self._rebuild_table()
        self.query_one("#status-bar", Static).update(
            f"{len(self._filtered_items)} of {len(self._all_items)} items"
        )

    def on_sidebar_filter_selected(self, event: Sidebar.FilterSelected) -> None:
        if event.kind == "location":
            self._location_filter = event.filter_id
        else:
            self._tag_filter = event.filter_id
        self._apply_filters()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._search_query = event.value
        self._apply_filters()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        item_id = event.row_key.value
        if item_id:
            self.run_worker(self._fetch_and_show(item_id), exclusive=True, group="detail")

    async def _fetch_and_show(self, item_id: str) -> None:
        try:
            item = await self._client.get_item(item_id)
            self.query_one("#detail-panel", ItemDetail).show_item(item)
        except HomeBoxError as e:
            self.query_one("#status-bar", Static).update(f"[red]Error fetching item: {e}[/red]")

    def action_refresh(self) -> None:
        self._location_filter = None
        self._tag_filter = None
        self._search_query = ""
        self.query_one("#search-bar", Input).value = ""
        self.run_worker(self._load_all(), exclusive=True, group="loader")

    def action_focus_search(self) -> None:
        self.query_one("#search-bar", Input).focus()

    def action_clear_search(self) -> None:
        self.query_one("#search-bar", Input).value = ""
        self.query_one("#items-table", DataTable).focus()

    def action_quit(self) -> None:
        self.app.exit()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class HomeBoxApp(App):
    TITLE = "HomeBox"
    SUB_TITLE = "read-only inventory browser"

    CSS = """
    Screen {
        background: $surface;
    }
    """

    def __init__(self):
        super().__init__()
        self._client: HomeBoxClient | None = None

    async def on_mount(self) -> None:
        try:
            self._client = HomeBoxClient()
            self._client._client = __import__("httpx").AsyncClient(timeout=30.0)
            await self._client.login()
        except HomeBoxError as e:
            self.exit(message=f"Login failed: {e}")
            return
        await self.push_screen(MainScreen(self._client))

    async def on_unmount(self) -> None:
        if self._client and self._client._client:
            await self._client._client.aclose()


if __name__ == "__main__":
    app = HomeBoxApp()
    app.run()
