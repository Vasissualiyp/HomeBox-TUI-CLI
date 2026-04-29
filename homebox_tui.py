#!/usr/bin/env python3
"""HomeBox TUI — vim-mode inventory browser with add/delete/image-upload.

Requires EMAIL, PASSWORD, URL environment variables.

Vim keys (when items table focused):
  j / k        move down / up
  g g          go to top
  G            go to bottom
  ctrl+d / u   half-page down / up

Actions:
  a            add item
  A            add location
  x            delete selected item  (with confirmation)
  X            delete selected location from sidebar  (with confirmation)
  u            upload image to selected item
  /            focus search bar
  r            refresh
  q            quit
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.screen import Screen, ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
)
from textual.message import Message

from homebox_api import HomeBoxClient, HomeBoxError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(value, default="") -> str:
    return default if value is None else str(value)

_DIALOG_CSS = """
.modal-screen { align: center middle; }
#dialog {
    background: $surface;
    border: solid $primary;
    padding: 1 2;
    width: 64;
    height: auto;
    max-height: 90%;
}
#dialog-title {
    text-style: bold;
    color: $accent;
    margin-bottom: 1;
}
.field-label { color: $text-muted; margin-top: 1; }
#buttons { align: right middle; margin-top: 2; height: 3; }
#buttons Button { margin-left: 1; }
"""

_CONFIRM_CSS = """
.modal-screen { align: center middle; }
#dialog {
    background: $surface;
    border: solid $error;
    padding: 1 2;
    width: 50;
    height: auto;
}
#confirm-msg { margin-bottom: 1; }
#buttons { align: right middle; margin-top: 1; height: 3; }
#buttons Button { margin-left: 1; }
"""


# ---------------------------------------------------------------------------
# Vim-aware DataTable
# ---------------------------------------------------------------------------

class VimDataTable(DataTable):
    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("G", "scroll_bottom", show=False),
        Binding("ctrl+d", "half_page_down", show=False),
        Binding("ctrl+u", "half_page_up", show=False),
    ]

    def action_half_page_down(self) -> None:
        for _ in range(10):
            self.action_cursor_down()

    def action_half_page_up(self) -> None:
        for _ in range(10):
            self.action_cursor_up()


# ---------------------------------------------------------------------------
# Detail panel
# ---------------------------------------------------------------------------

class ItemDetail(Static):
    DEFAULT_CSS = """
    ItemDetail {
        height: 100%;
        padding: 1 2;
        border-left: solid $primary-darken-2;
        overflow-y: auto;
    }
    """

    def show_item(self, item: dict | None) -> None:
        if item is None:
            self.update("")
            return

        lines: list[str] = []
        lines.append(f"[bold accent]{item.get('name', 'Unknown')}[/bold accent]\n")

        def row(key: str, val):
            if val not in (None, "", [], {}):
                lines.append(f"[dim]{key}:[/dim] {val}")

        row("Location", (item.get("location") or {}).get("name"))
        row("Tags", ", ".join(t["name"] for t in (item.get("tags") or [])))
        row("Quantity", item.get("quantity"))
        row("Asset ID", item.get("assetId"))
        row("Serial #", item.get("serialNumber"))
        row("Manufacturer", item.get("manufacturer"))
        row("Model", item.get("modelNumber"))
        row("Purchase Price", item.get("purchasePrice"))
        row("Purchase From", item.get("purchaseFrom"))
        pt = item.get("purchaseTime", "") or ""
        row("Purchase Date", pt[:10] if pt else None)
        row("Insured", item.get("insured"))
        we = item.get("warrantyExpires", "") or ""
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
    pass


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
    Sidebar ListView { height: auto; border: none; }
    Sidebar ListItem { padding: 0 1; }
    """

    class FilterSelected(Message):
        def __init__(self, kind: str, filter_id: str | None, name: str) -> None:
            super().__init__()
            self.kind = kind
            self.filter_id = filter_id
            self.name = name

    def __init__(self, locations: list[dict], tags: list[dict]) -> None:
        super().__init__()
        self._locations = locations
        self._tags = tags

    def compose(self) -> ComposeResult:
        yield Label("Locations")
        yield SidebarList(
            ListItem(Label("All")),
            *[ListItem(Label(loc["name"]), id=f"loc-{loc['id']}") for loc in self._locations],
            id="loc-list",
        )
        yield Label("Tags")
        yield SidebarList(
            ListItem(Label("All")),
            *[ListItem(Label(tg["name"]), id=f"tag-{tg['id']}") for tg in self._tags],
            id="tag-list",
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        text = str(event.item.query_one(Label).renderable)
        if event.list_view.id == "loc-list":
            fid = item_id.removeprefix("loc-") if item_id else None
            self.post_message(self.FilterSelected("location", fid, text))
        else:
            fid = item_id.removeprefix("tag-") if item_id else None
            self.post_message(self.FilterSelected("tag", fid, text))


# ---------------------------------------------------------------------------
# Modal: Add Item
# ---------------------------------------------------------------------------

class AddItemScreen(ModalScreen):
    CSS = _DIALOG_CSS
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, locations: list[dict]) -> None:
        super().__init__()
        self._locations = locations

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="modal-screen"):
            yield Label("Add Item", id="dialog-title")
            yield Label("Name *", classes="field-label")
            yield Input(placeholder="Item name", id="inp-name")
            yield Label("Description", classes="field-label")
            yield Input(placeholder="Optional", id="inp-desc")
            yield Label("Quantity", classes="field-label")
            yield Input(value="1", id="inp-qty")
            yield Label("Location *", classes="field-label")
            yield Select(
                [(loc["name"], loc["id"]) for loc in self._locations],
                prompt="Select location…",
                id="sel-location",
            )
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="btn-cancel")
                yield Button("Add Item", variant="primary", id="btn-submit")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-submit":
            self._submit()

    def _submit(self) -> None:
        name = self.query_one("#inp-name", Input).value.strip()
        if not name:
            self.notify("Name is required", severity="error")
            return
        sel = self.query_one("#sel-location", Select)
        if sel.value is Select.BLANK:
            self.notify("Location is required", severity="error")
            return
        try:
            qty = max(1, int(self.query_one("#inp-qty", Input).value or "1"))
        except ValueError:
            qty = 1
        self.dismiss({
            "name": name,
            "description": self.query_one("#inp-desc", Input).value.strip(),
            "quantity": qty,
            "locationId": sel.value,
        })


# ---------------------------------------------------------------------------
# Modal: Add Location
# ---------------------------------------------------------------------------

class AddLocationScreen(ModalScreen):
    CSS = _DIALOG_CSS
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, locations: list[dict]) -> None:
        super().__init__()
        self._locations = locations

    def compose(self) -> ComposeResult:
        parent_opts = [("(none — top level)", "__none__")] + [
            (loc["name"], loc["id"]) for loc in self._locations
        ]
        with Vertical(id="dialog", classes="modal-screen"):
            yield Label("Add Location", id="dialog-title")
            yield Label("Name *", classes="field-label")
            yield Input(placeholder="Location name", id="inp-name")
            yield Label("Description", classes="field-label")
            yield Input(placeholder="Optional", id="inp-desc")
            yield Label("Parent location (optional)", classes="field-label")
            yield Select(parent_opts, value="__none__", id="sel-parent")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="btn-cancel")
                yield Button("Add Location", variant="primary", id="btn-submit")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-submit":
            self._submit()

    def _submit(self) -> None:
        name = self.query_one("#inp-name", Input).value.strip()
        if not name:
            self.notify("Name is required", severity="error")
            return
        parent_val = self.query_one("#sel-parent", Select).value
        parent_id = None if (parent_val in ("__none__", Select.BLANK)) else parent_val
        self.dismiss({
            "name": name,
            "description": self.query_one("#inp-desc", Input).value.strip(),
            "parent_id": parent_id,
        })


# ---------------------------------------------------------------------------
# Modal: Confirm Delete
# ---------------------------------------------------------------------------

class ConfirmDeleteScreen(ModalScreen):
    CSS = _CONFIRM_CSS
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, subject: str, kind: str = "item") -> None:
        super().__init__()
        self._subject = subject
        self._kind = kind

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="modal-screen"):
            yield Static(
                f"Delete {self._kind} [bold]{self._subject}[/bold]?\nThis cannot be undone.",
                id="confirm-msg",
            )
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="btn-cancel")
                yield Button("Delete", variant="error", id="btn-delete")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-delete")


# ---------------------------------------------------------------------------
# Modal: Upload Image
# ---------------------------------------------------------------------------

class ImageUploadScreen(ModalScreen):
    CSS = _DIALOG_CSS
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, item_name: str) -> None:
        super().__init__()
        self._item_name = item_name

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="modal-screen"):
            yield Label(f"Upload image for [bold]{self._item_name}[/bold]", id="dialog-title")
            yield Label("File path", classes="field-label")
            yield Input(placeholder="/path/to/image.png", id="inp-path")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="btn-cancel")
                yield Button("Upload", variant="primary", id="btn-upload")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        path_str = self.query_one("#inp-path", Input).value.strip()
        if not path_str:
            self.notify("Path is required", severity="error")
            return
        p = pathlib.Path(path_str).expanduser().resolve()
        if not p.exists():
            self.notify(f"File not found: {p}", severity="error")
            return
        self.dismiss(str(p))


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------

class MainScreen(Screen):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("/", "focus_search", "Search"),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("a", "add_item", "Add Item"),
        Binding("A", "add_location", "Add Loc"),
        Binding("x", "delete_item", "Del Item"),
        Binding("X", "delete_location", "Del Loc"),
        Binding("u", "upload_image", "Upload Img"),
    ]

    DEFAULT_CSS = """
    #search-bar { height: 3; padding: 0 1; dock: top; }
    #main-body { layout: horizontal; height: 1fr; }
    #items-panel { layout: vertical; width: 1fr; }
    #items-table { height: 1fr; }
    #detail-panel { width: 40; height: 100%; }
    #status-bar { height: 1; padding: 0 1; color: $text-muted; dock: bottom; }
    """

    def __init__(self, client: HomeBoxClient) -> None:
        super().__init__()
        self._client = client
        self._all_items: list[dict] = []
        self._filtered_items: list[dict] = []
        self._location_filter: str | None = None
        self._tag_filter: str | None = None
        self._search_query: str = ""
        self._locations: list[dict] = []
        self._tags: list[dict] = []
        self._cursor_item: dict | None = None
        self._cursor_location: str | None = None  # sidebar-highlighted location id
        self._last_key_was_g = False

    # --- Layout ---

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="Search items… (press / to focus)", id="search-bar")
        with Horizontal(id="main-body"):
            yield Sidebar([], [])
            with Vertical(id="items-panel"):
                yield VimDataTable(id="items-table", cursor_type="row")
            yield ItemDetail(id="detail-panel")
        yield Static("Loading…", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#items-table", VimDataTable).add_columns("Name", "Location", "Tags", "Qty")
        self.run_worker(self._load_all(), exclusive=True, group="loader")

    # --- Data loading ---

    async def _load_all(self) -> None:
        self._set_status("Loading…")
        try:
            self._locations, self._tags, self._all_items = await asyncio.gather(
                self._client.get_locations(),
                self._client.get_tags(),
                self._client.get_all_items(),
            )
        except HomeBoxError as e:
            self._set_status(f"[red]Error: {e}[/red]")
            return
        await self._rebuild_sidebar()
        self._filtered_items = list(self._all_items)
        self._rebuild_table()
        self._set_status(f"{len(self._all_items)} items loaded")

    async def _rebuild_sidebar(self) -> None:
        old = self.query_one(Sidebar)
        await old.remove()
        new = Sidebar(self._locations, self._tags)
        await self.query_one("#main-body", Horizontal).mount(new, before=self.query_one("#items-panel"))

    def _rebuild_table(self) -> None:
        table = self.query_one("#items-table", VimDataTable)
        table.clear()
        for item in self._filtered_items:
            loc = (item.get("location") or {}).get("name", "")
            tags = ", ".join(t["name"] for t in (item.get("tags") or []))
            table.add_row(item.get("name", ""), loc, tags, _fmt(item.get("quantity")), key=item.get("id"))
        self.query_one("#detail-panel", ItemDetail).show_item(None)
        self._cursor_item = None

    def _apply_filters(self) -> None:
        result = self._all_items
        if self._location_filter:
            result = [i for i in result if (i.get("location") or {}).get("id") == self._location_filter]
        if self._tag_filter:
            result = [i for i in result if any(t["id"] == self._tag_filter for t in (i.get("tags") or []))]
        if self._search_query:
            q = self._search_query.lower()
            result = [i for i in result if q in (i.get("name") or "").lower() or q in (i.get("description") or "").lower()]
        self._filtered_items = result
        self._rebuild_table()
        self._set_status(f"{len(self._filtered_items)} of {len(self._all_items)} items")

    def _set_status(self, msg: str) -> None:
        self.query_one("#status-bar", Static).update(msg)

    # --- Events ---

    def on_sidebar_filter_selected(self, event: Sidebar.FilterSelected) -> None:
        if event.kind == "location":
            self._location_filter = event.filter_id
            self._cursor_location = event.filter_id
        else:
            self._tag_filter = event.filter_id
        self._apply_filters()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-bar":
            self._search_query = event.value
            self._apply_filters()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key:
            item_id = event.row_key.value
            self._cursor_item = next((i for i in self._filtered_items if i.get("id") == item_id), None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key:
            self.run_worker(self._fetch_detail(event.row_key.value), exclusive=True, group="detail")

    async def _fetch_detail(self, item_id: str) -> None:
        try:
            item = await self._client.get_item(item_id)
            self.query_one("#detail-panel", ItemDetail).show_item(item)
        except HomeBoxError as e:
            self._set_status(f"[red]Error: {e}[/red]")

    # --- Vim gg chord ---

    def on_key(self, event) -> None:
        table = self.query_one("#items-table", VimDataTable)
        if table.has_focus and event.key == "g":
            if self._last_key_was_g:
                table.action_scroll_top()
                self._last_key_was_g = False
                event.prevent_default()
                event.stop()
            else:
                self._last_key_was_g = True
                event.prevent_default()
                event.stop()
            return
        self._last_key_was_g = False

    # --- Screen actions ---

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
        self.query_one("#items-table", VimDataTable).focus()

    def action_quit(self) -> None:
        self.app.exit()

    def _focused_is_input(self) -> bool:
        return isinstance(self.app.focused, Input)

    # --- Add item ---

    def action_add_item(self) -> None:
        if self._focused_is_input():
            return
        if not self._locations:
            self.notify("No locations loaded yet", severity="warning")
            return
        self.app.push_screen(AddItemScreen(self._locations), self._on_add_item)

    def _on_add_item(self, result: dict | None) -> None:
        if result:
            self.run_worker(self._do_create_item(result), group="mutate")

    async def _do_create_item(self, payload: dict) -> None:
        try:
            item = await self._client.create_item(payload)
            self.notify(f"Created item: {item['name']}")
            self.run_worker(self._load_all(), exclusive=True, group="loader")
        except HomeBoxError as e:
            self.notify(f"Error: {e}", severity="error")

    # --- Add location ---

    def action_add_location(self) -> None:
        if self._focused_is_input():
            return
        self.app.push_screen(AddLocationScreen(self._locations), self._on_add_location)

    def _on_add_location(self, result: dict | None) -> None:
        if result:
            self.run_worker(self._do_create_location(result), group="mutate")

    async def _do_create_location(self, result: dict) -> None:
        try:
            loc = await self._client.create_location(
                name=result["name"],
                description=result["description"],
                parent_id=result["parent_id"],
            )
            self.notify(f"Created location: {loc['name']}")
            self.run_worker(self._load_all(), exclusive=True, group="loader")
        except HomeBoxError as e:
            self.notify(f"Error: {e}", severity="error")

    # --- Delete item ---

    def action_delete_item(self) -> None:
        if self._focused_is_input() or not self._cursor_item:
            return
        name = self._cursor_item.get("name", "?")
        item_id = self._cursor_item.get("id")
        self.app.push_screen(
            ConfirmDeleteScreen(name, "item"),
            lambda confirmed: self.run_worker(self._do_delete_item(item_id), group="mutate") if confirmed else None,
        )

    async def _do_delete_item(self, item_id: str) -> None:
        try:
            await self._client.delete_item(item_id)
            self.notify("Item deleted")
            self.run_worker(self._load_all(), exclusive=True, group="loader")
        except HomeBoxError as e:
            self.notify(f"Error: {e}", severity="error")

    # --- Delete location ---

    def action_delete_location(self) -> None:
        if self._focused_is_input() or not self._cursor_location:
            return
        loc = next((l for l in self._locations if l["id"] == self._cursor_location), None)
        if not loc:
            self.notify("Select a location in the sidebar first", severity="warning")
            return
        self.app.push_screen(
            ConfirmDeleteScreen(loc["name"], "location"),
            lambda confirmed: self.run_worker(self._do_delete_location(loc["id"]), group="mutate") if confirmed else None,
        )

    async def _do_delete_location(self, location_id: str) -> None:
        try:
            await self._client.delete_location(location_id)
            self.notify("Location deleted")
            self._cursor_location = None
            self.run_worker(self._load_all(), exclusive=True, group="loader")
        except HomeBoxError as e:
            self.notify(f"Error: {e}", severity="error")

    # --- Upload image ---

    def action_upload_image(self) -> None:
        if self._focused_is_input() or not self._cursor_item:
            self.notify("Select an item first", severity="warning")
            return
        name = self._cursor_item.get("name", "?")
        item_id = self._cursor_item.get("id")
        self.app.push_screen(
            ImageUploadScreen(name),
            lambda path: self.run_worker(self._do_upload(item_id, path), group="mutate") if path else None,
        )

    async def _do_upload(self, item_id: str, file_path: str) -> None:
        self._set_status(f"Uploading {pathlib.Path(file_path).name}…")
        try:
            await self._client.upload_item_image(item_id, file_path)
            self.notify("Image uploaded successfully")
            self._set_status("")
        except HomeBoxError as e:
            self.notify(f"Upload error: {e}", severity="error")
            self._set_status("")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class HomeBoxApp(App):
    TITLE = "HomeBox"
    SUB_TITLE = "inventory browser"

    def __init__(self) -> None:
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
    HomeBoxApp().run()
