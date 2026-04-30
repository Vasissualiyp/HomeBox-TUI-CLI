#!/usr/bin/env python3
"""HomeBox TUI — vim-mode inventory browser.

Requires EMAIL, PASSWORD, URL environment variables.

━━━ Pane switching ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  shift+H   move left
  shift+L   move right
  shift+J   move down
  shift+K   move up

━━━ Sidebar (locations tree) ━━━━━━━━━━━━━━━━━━━━━━━
  j / k     move cursor down / up
  h         collapse node
  l         expand node
  G         jump to bottom
  g g       jump to top
  ctrl+d    half-page down
  ctrl+u    half-page up
  shift+J   jump to tags pane

━━━ Tags list ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  j / k     move cursor
  Enter     toggle tag selection (accumulates filter)
  G / g g   jump to bottom / top
  ctrl+d    half-page down
  ctrl+u    half-page up
  shift+K   jump back to locations tree

━━━ Items table ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  j / k     move cursor down / up
  g g       jump to top
  G         jump to bottom
  ctrl+d    half-page down
  ctrl+u    half-page up
  Enter     show full detail in right panel
  v         view item image (suspends TUI)

━━━ Global actions ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  a         add item
  A         add location
  x         delete selected item  (confirmation required)
  X         delete sidebar-selected location
  u         upload image to selected item
  B         bulk index (webcam capture flow)
  r         refresh
  /         focus search
  q         quit
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
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
    Tree,
)
from textual.message import Message

from homebox_api import HomeBoxClient, HomeBoxError
from homebox_config import _kitty_wrap, _kitty_delete_all, is_kitty_supported


# ---------------------------------------------------------------------------
# Shared CSS snippets
# ---------------------------------------------------------------------------

_DIALOG_CSS = """
.modal-bg { align: center middle; }
#dialog {
    background: $surface;
    border: solid $primary;
    padding: 1 2;
    width: 64;
    height: auto;
    max-height: 90%;
}
#dialog-title { text-style: bold; color: $accent; margin-bottom: 1; }
.field-label { color: $text-muted; margin-top: 1; }
#buttons { align: right middle; margin-top: 2; height: 3; }
#buttons Button { margin-left: 1; }
"""

_CONFIRM_CSS = """
.modal-bg { align: center middle; }
#dialog {
    background: $surface;
    border: solid $error;
    padding: 1 2;
    width: 52;
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
# Vim-aware ListView (tags list)
# ---------------------------------------------------------------------------

class VimListView(ListView):
    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("G", "vim_end", show=False),
        Binding("ctrl+d", "half_page_down", show=False),
        Binding("ctrl+u", "half_page_up", show=False),
    ]

    def action_vim_end(self) -> None:
        for _ in range(200):
            self.action_cursor_down()

    def action_half_page_down(self) -> None:
        for _ in range(10):
            self.action_cursor_down()

    def action_half_page_up(self) -> None:
        for _ in range(10):
            self.action_cursor_up()

    def action_vim_top(self) -> None:
        for _ in range(200):
            self.action_cursor_up()


# ---------------------------------------------------------------------------
# Location tree with vim keys + h/l collapse/expand
# ---------------------------------------------------------------------------

class LocationTree(Tree):
    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("h", "collapse_node", show=False),
        Binding("l", "expand_node", show=False),
        Binding("G", "vim_end", show=False),
        Binding("ctrl+d", "half_page_down", show=False),
        Binding("ctrl+u", "half_page_up", show=False),
    ]

    def action_expand_node(self) -> None:
        node = self.cursor_node
        if node is not None:
            node.expand()

    def action_collapse_node(self) -> None:
        node = self.cursor_node
        if node is not None:
            node.collapse()

    def action_vim_end(self) -> None:
        for _ in range(200):
            self.action_cursor_down()

    def action_half_page_down(self) -> None:
        for _ in range(10):
            self.action_cursor_down()

    def action_half_page_up(self) -> None:
        for _ in range(10):
            self.action_cursor_up()

    def action_vim_top(self) -> None:
        for _ in range(200):
            self.action_cursor_up()

    def populate(self, tree_data: list[dict]) -> None:
        """Rebuild tree from /locations/tree API response."""
        self.clear()
        self.root.add_leaf("All locations", data=None)
        for loc in tree_data:
            self._build_loc_node(self.root, loc)
        self.root.expand()

    def _build_loc_node(self, parent, loc: dict) -> None:
        children = loc.get("children") or []
        if children:
            node = parent.add(loc["name"], data=loc["id"], expand=False)
            for child in children:
                self._build_loc_node(node, child)
        else:
            parent.add_leaf(loc["name"], data=loc["id"])


# ---------------------------------------------------------------------------
# Kitty inline image widget (unicode-placeholder approach)
# ---------------------------------------------------------------------------

class KittyImageWidget(Static):
    """Displays an image via kitty graphics protocol (direct placement).

    After every Textual render cycle that clears the widget's cells,
    ``call_after_refresh`` repaints the kitty image on top.
    Supports tmux via DCS passthrough wrapping.
    """

    DEFAULT_CSS = """
    KittyImageWidget {
        height: 12;
        width: 100%;
        content-align: center middle;
    }
    """

    _pane_offset: tuple[int, int] | None = None  # class-level cache

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._png_bytes: bytes | None = None
        self._raw_bytes: bytes | None = None  # original for 'v' key viewing

    # ------------------------------------------------------------------
    # tmux helpers
    # ------------------------------------------------------------------

    @classmethod
    def _get_pane_offset(cls) -> tuple[int, int]:
        """Return (top_row, left_col) of current tmux pane in terminal space."""
        import os
        if cls._pane_offset is not None:
            return cls._pane_offset
        if not os.environ.get("TMUX"):
            cls._pane_offset = (0, 0)
            return cls._pane_offset
        import subprocess
        try:
            r = subprocess.run(
                ["tmux", "display-message", "-p", "#{pane_top},#{pane_left}"],
                capture_output=True, text=True, timeout=1,
            )
            parts = r.stdout.strip().split(",")
            cls._pane_offset = (int(parts[0]), int(parts[1]))
        except Exception:
            cls._pane_offset = (0, 0)
        return cls._pane_offset

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_image(self, image_bytes: bytes | None) -> None:
        self._raw_bytes = image_bytes
        self._png_bytes = None

        # Clear old kitty image
        if is_kitty_supported():
            _kitty_delete_all()

        if image_bytes is None or not is_kitty_supported():
            if image_bytes and not is_kitty_supported():
                self.update("[dim]image attached — press [bold]v[/bold] to view[/dim]")
            else:
                self.update("[dim](no image)[/dim]")
            return

        # Invalidate cached tmux pane offset (might have changed)
        KittyImageWidget._pane_offset = None

        try:
            import io
            from PIL import Image

            cols = max(1, self.size.width)
            rows = max(1, self.size.height)

            img = Image.open(io.BytesIO(image_bytes))
            img.thumbnail((cols * 8, rows * 16), Image.LANCZOS)
            png_buf = io.BytesIO()
            img.save(png_buf, format="PNG", optimize=True)
            self._png_bytes = png_buf.getvalue()
        except Exception as e:
            self.update(f"[dim red]img err: {e}[/dim red]")
            return

        self.update("")  # blank; kitty image painted on top
        self.call_after_refresh(self._paint_kitty)

    # ------------------------------------------------------------------
    # Rendering — repaint kitty image after every Textual render
    # ------------------------------------------------------------------

    def render(self):
        result = super().render()
        if self._png_bytes is not None:
            self.call_after_refresh(self._paint_kitty)
        return result

    def _paint_kitty(self) -> None:
        if self._png_bytes is None:
            return
        try:
            import os, base64
            region = self.content_region

            pane_top, pane_left = self._get_pane_offset()
            row = region.y + 1 + pane_top
            col = region.x + 1 + pane_left
            cols = max(1, region.width)
            rows = max(1, region.height)

            b64 = base64.standard_b64encode(self._png_bytes).decode()
            chunks = [b64[i:i + 4096] for i in range(0, len(b64), 4096)]

            # Build cursor positioning + kitty commands together
            # (both must reach kitty, so both go inside DCS passthrough)
            kitty_buf = bytearray()
            kitty_buf += f"\033[{row};{col}H".encode()
            for i, chunk in enumerate(chunks):
                more = 0 if i == len(chunks) - 1 else 1
                if i == 0:
                    kitty_buf += (
                        f"\033_Ga=T,f=100,q=2,"
                        f"c={cols},r={rows},"
                        f"m={more};{chunk}\033\\"
                    ).encode()
                else:
                    kitty_buf += f"\033_Gm={more};{chunk}\033\\".encode()

            out = bytearray()
            out += _kitty_wrap(b"\033_Ga=d,q=2;\033\\")  # delete old
            out += _kitty_wrap(bytes(kitty_buf))           # position + place
            os.write(1, bytes(out))
        except Exception:
            pass



# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

class Sidebar(Vertical):
    DEFAULT_CSS = """
    Sidebar {
        width: 24;
        border-right: solid $primary-darken-2;
    }
    Sidebar .section-label {
        text-style: bold;
        color: $accent;
        padding: 0 1;
        height: 1;
    }
    Sidebar LocationTree {
        height: 1fr;
        border: none;
        padding: 0;
        overflow-x: hidden;
    }
    Sidebar .tag-section { height: 14; }
    Sidebar VimListView { height: 1fr; border: none; overflow-y: auto; }
    Sidebar ListItem { padding: 0 1; }
    """

    class FilterSelected(Message):
        def __init__(self, kind: str, filter_id: str | None, name: str) -> None:
            super().__init__()
            self.kind = kind           # "location"
            self.filter_id = filter_id  # None = show all
            self.name = name

    class TagFilterChanged(Message):
        def __init__(self, hover_id: str | None, selected_ids: frozenset) -> None:
            super().__init__()
            self.hover_id = hover_id
            self.selected_ids = selected_ids

    def __init__(self, location_tree: list[dict], tags: list[dict], **kwargs) -> None:
        super().__init__(**kwargs)
        self._location_tree = location_tree
        self._tags = tags
        self._selected_tag_ids: set[str] = set()
        self._hover_tag_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Label("Locations", classes="section-label")
        yield LocationTree("Locations", id="loc-tree")
        with Vertical(classes="tag-section"):
            yield Label("Tags", classes="section-label")
            yield VimListView(
                ListItem(Label("All tags"), id="tag-all"),
                *[ListItem(Label(tg["name"]), id=f"tag-{tg['id']}") for tg in self._tags],
                id="tag-list",
            )

    def on_mount(self) -> None:
        self.query_one(LocationTree).populate(self._location_tree)

    # Location tree events

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        loc_id = event.node.data  # None for "All locations"
        name = str(event.node.label)
        self.post_message(self.FilterSelected("location", loc_id, name))

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        node = event.node
        if node.children:
            node.toggle()

    # Tag list events

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "tag-list":
            return
        item = event.item
        if item is None:
            return
        item_id = item.id or ""
        if item_id == "tag-all" or not item_id.startswith("tag-"):
            self._hover_tag_id = None
        else:
            self._hover_tag_id = item_id.removeprefix("tag-")
        self.post_message(self.TagFilterChanged(
            self._hover_tag_id,
            frozenset(self._selected_tag_ids),
        ))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "tag-list":
            return
        item = event.item
        if item is None:
            return
        item_id = item.id or ""
        if item_id == "tag-all" or not item_id.startswith("tag-"):
            # Clear all selections
            self._selected_tag_ids.clear()
            self._refresh_tag_labels()
        else:
            tag_id = item_id.removeprefix("tag-")
            if tag_id in self._selected_tag_ids:
                self._selected_tag_ids.discard(tag_id)
            else:
                self._selected_tag_ids.add(tag_id)
            self._refresh_one_tag_label(tag_id)
        self.post_message(self.TagFilterChanged(
            self._hover_tag_id,
            frozenset(self._selected_tag_ids),
        ))

    def _refresh_one_tag_label(self, tag_id: str) -> None:
        tag_name = next((t["name"] for t in self._tags if t["id"] == tag_id), tag_id)
        try:
            item = self.query_one(f"#tag-{tag_id}", ListItem)
            label = item.query_one(Label)
            if tag_id in self._selected_tag_ids:
                label.update(f"✓ {tag_name}")
            else:
                label.update(tag_name)
        except Exception:
            pass

    def _refresh_tag_labels(self) -> None:
        for tg in self._tags:
            tag_id = tg["id"]
            tag_name = tg["name"]
            try:
                item = self.query_one(f"#tag-{tag_id}", ListItem)
                label = item.query_one(Label)
                if tag_id in self._selected_tag_ids:
                    label.update(f"✓ {tag_name}")
                else:
                    label.update(tag_name)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Detail panel
# ---------------------------------------------------------------------------

class ItemDetail(Vertical):
    DEFAULT_CSS = """
    ItemDetail {
        height: 100%;
        border-left: solid $primary-darken-2;
    }
    ItemDetail KittyImageWidget { height: 12; }
    ItemDetail #detail-text {
        height: 1fr;
        overflow-y: auto;
        padding: 1 2;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._current_img_bytes: bytes | None = None

    def compose(self) -> ComposeResult:
        yield KittyImageWidget(id="detail-image")
        yield Static("[dim]Select an item to see details[/dim]", id="detail-text")

    def show_item(self, item: dict | None, img_bytes: bytes | None = None) -> None:
        self._current_img_bytes = img_bytes
        self.query_one("#detail-image", KittyImageWidget).set_image(img_bytes)

        text = self.query_one("#detail-text", Static)
        if item is None:
            text.update("[dim]Select an item to see details[/dim]")
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
        pt = item.get("purchaseTime") or ""
        row("Purchase Date", pt[:10] if pt else None)
        row("Insured", item.get("insured"))
        we = item.get("warrantyExpires") or ""
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
        # Attachments summary
        attachments = item.get("attachments") or []
        photos = [a for a in attachments if a.get("type") == "photo"]
        if photos:
            lines.append(f"\n[dim]Photos:[/dim] {len(photos)}")
            if img_bytes is None:
                lines.append("[dim](press [bold]v[/bold] to view)[/dim]")

        text.update("\n".join(lines))


# ---------------------------------------------------------------------------
# Modal: Add Item
# ---------------------------------------------------------------------------

class AddItemScreen(ModalScreen):
    CSS = _DIALOG_CSS
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, locations: list[dict], **kwargs) -> None:
        super().__init__(**kwargs)
        self._locations = locations

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="modal-bg"):
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
        else:
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

    def __init__(self, locations: list[dict], **kwargs) -> None:
        super().__init__(**kwargs)
        self._locations = locations

    def compose(self) -> ComposeResult:
        opts = [("(none — top level)", "__none__")] + [
            (loc["name"], loc["id"]) for loc in self._locations
        ]
        with Vertical(id="dialog", classes="modal-bg"):
            yield Label("Add Location", id="dialog-title")
            yield Label("Name *", classes="field-label")
            yield Input(placeholder="Location name", id="inp-name")
            yield Label("Description", classes="field-label")
            yield Input(placeholder="Optional", id="inp-desc")
            yield Label("Parent location (optional)", classes="field-label")
            yield Select(opts, value="__none__", id="sel-parent")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="btn-cancel")
                yield Button("Add Location", variant="primary", id="btn-submit")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        else:
            self._submit()

    def _submit(self) -> None:
        name = self.query_one("#inp-name", Input).value.strip()
        if not name:
            self.notify("Name is required", severity="error")
            return
        parent_val = self.query_one("#sel-parent", Select).value
        parent_id = None if parent_val in ("__none__", Select.BLANK) else str(parent_val)
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

    def __init__(self, subject: str, kind: str = "item", **kwargs) -> None:
        super().__init__(**kwargs)
        self._subject = subject
        self._kind = kind

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="modal-bg"):
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

    def __init__(self, item_name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._item_name = item_name

    def compose(self) -> ComposeResult:
        import pathlib
        with Vertical(id="dialog", classes="modal-bg"):
            yield Label(f"Upload image for [bold]{self._item_name}[/bold]", id="dialog-title")
            yield Label("File path", classes="field-label")
            yield Input(
                value=str(pathlib.Path.cwd()),
                placeholder="/path/to/image.png",
                id="inp-path",
            )
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="btn-cancel")
                yield Button("Upload", variant="primary", id="btn-upload")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        import pathlib
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
        # Pane switching (shift+hjkl) — position-aware
        Binding("H", "pane_left", "← Left", show=False),
        Binding("L", "pane_right", "→ Right", show=False),
        Binding("J", "pane_down", "↓ Down", show=False),
        Binding("K", "pane_up", "↑ Up", show=False),
        # Mutations
        Binding("a", "add_item", "Add Item"),
        Binding("A", "add_location", "Add Loc"),
        Binding("x", "delete_item", "Del Item"),
        Binding("X", "delete_location", "Del Loc"),
        Binding("u", "upload_image", "Upload Img"),
        Binding("B", "bulk_index", "Bulk Index"),
        Binding("v", "view_image", "View Image", show=False),
    ]

    DEFAULT_CSS = """
    MainScreen { layout: vertical; }
    #search-bar { height: 3; padding: 0 1; dock: top; }
    #main-body { layout: horizontal; height: 1fr; }
    #items-panel { layout: vertical; width: 1fr; }
    #items-table { height: 1fr; }
    #detail-panel { width: 38; height: 100%; }
    #status-bar { height: 1; padding: 0 1; color: $text-muted; dock: bottom; }
    """

    def __init__(self, client: HomeBoxClient, **kwargs) -> None:
        super().__init__(**kwargs)
        self._client = client
        self._all_items: list[dict] = []
        self._filtered_items: list[dict] = []
        self._location_filter: str | None = None
        self._hover_tag_id: str | None = None
        self._selected_tag_ids: set[str] = set()
        self._search_query: str = ""
        self._location_tree: list[dict] = []
        self._locations_flat: list[dict] = []
        self._tags: list[dict] = []
        self._cursor_item: dict | None = None
        self._cursor_location_id: str | None = None
        self._last_key_was_g = False

    # --- Compose ---

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="Search items… (press / to focus)", id="search-bar")
        with Horizontal(id="main-body"):
            yield Sidebar([], [], id="sidebar")
            with Vertical(id="items-panel"):
                yield VimDataTable(id="items-table", cursor_type="row")
            yield ItemDetail(id="detail-panel")
        yield Static("Loading…", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#items-table", VimDataTable).add_columns(
            "Name", "Location", "Tags", "Qty"
        )
        self.run_worker(self._load_all(), exclusive=True, group="loader")

    # --- Data loading ---

    async def _load_all(self) -> None:
        self._set_status("Loading…")
        try:
            self._location_tree, self._tags, self._all_items = await __import__("asyncio").gather(
                self._client.get_location_tree(),
                self._client.get_tags(),
                self._client.get_all_items(),
            )
        except HomeBoxError as e:
            self._set_status(f"[red]Error: {e}[/red]")
            return

        self._locations_flat = _flatten_tree(self._location_tree)
        await self._rebuild_sidebar()
        self._filtered_items = list(self._all_items)
        self._rebuild_table()
        self._set_status(f"{len(self._all_items)} items loaded")

    async def _rebuild_sidebar(self) -> None:
        old = self.query_one("#sidebar", Sidebar)
        await old.remove()
        new = Sidebar(self._location_tree, self._tags, id="sidebar")
        await self.query_one("#main-body", Horizontal).mount(
            new, before=self.query_one("#items-panel")
        )

    def _rebuild_table(self) -> None:
        table = self.query_one("#items-table", VimDataTable)
        table.clear()
        for item in self._filtered_items:
            loc = (item.get("location") or {}).get("name", "")
            tags = ", ".join(t["name"] for t in (item.get("tags") or []))
            table.add_row(
                item.get("name", ""),
                loc,
                tags,
                str(item.get("quantity", "")),
                key=item.get("id"),
            )
        self.query_one("#detail-panel", ItemDetail).show_item(None)
        self._cursor_item = None

    def _apply_filters(self) -> None:
        result = self._all_items
        if self._location_filter:
            result = [
                i for i in result
                if (i.get("location") or {}).get("id") == self._location_filter
            ]
        # All selected tags + hover tag must each be present (AND logic)
        active_tags = set(self._selected_tag_ids)
        if self._hover_tag_id:
            active_tags.add(self._hover_tag_id)
        for tag_id in active_tags:
            result = [
                i for i in result
                if any(t["id"] == tag_id for t in (i.get("tags") or []))
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
        self._set_status(f"{len(self._filtered_items)} of {len(self._all_items)} items")

    def _set_status(self, msg: str) -> None:
        self.query_one("#status-bar", Static).update(msg)

    # --- Events ---

    def on_sidebar_filter_selected(self, event: Sidebar.FilterSelected) -> None:
        if event.kind == "location":
            self._location_filter = event.filter_id
            self._cursor_location_id = event.filter_id
        self._apply_filters()

    def on_sidebar_tag_filter_changed(self, event: Sidebar.TagFilterChanged) -> None:
        self._hover_tag_id = event.hover_id
        self._selected_tag_ids = set(event.selected_ids)
        self._apply_filters()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-bar":
            self._search_query = event.value
            self._apply_filters()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key:
            item_id = event.row_key.value
            self._cursor_item = next(
                (i for i in self._filtered_items if i.get("id") == item_id), None
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key:
            self.run_worker(self._fetch_detail(event.row_key.value), exclusive=True, group="detail")

    async def _fetch_detail(self, item_id: str) -> None:
        try:
            item = await self._client.get_item(item_id)
            # Try to fetch first photo attachment
            attachments = item.get("attachments") or []
            photo = next((a for a in attachments if a.get("type") == "photo"), None)
            img_bytes: bytes | None = None
            if photo:
                try:
                    img_bytes = await self._client.get_attachment(item_id, photo["id"])
                except Exception:
                    pass
            self.query_one("#detail-panel", ItemDetail).show_item(item, img_bytes)
        except HomeBoxError as e:
            self._set_status(f"[red]Error: {e}[/red]")

    # --- Vim gg chord (handles DataTable, LocationTree, VimListView) ---

    def on_key(self, event) -> None:
        if event.key != "g":
            self._last_key_was_g = False
            return

        focused = self.app.focused
        if self._last_key_was_g:
            self._last_key_was_g = False
            event.prevent_default()
            event.stop()
            if isinstance(focused, VimDataTable):
                focused.action_scroll_top()
            elif isinstance(focused, LocationTree):
                focused.action_vim_top()
            elif isinstance(focused, VimListView):
                focused.action_vim_top()
        else:
            self._last_key_was_g = True
            event.prevent_default()
            event.stop()

    # --- Position-aware pane focus actions ---
    #
    # Layout (left→right, top→bottom):
    #   [search-bar          ]  (top, full width)
    #   [loc-tree | items    | detail]
    #   [tag-list | items    | detail]

    def _focused_pane(self) -> str:
        """Return a label for which pane currently has focus."""
        focused = self.app.focused
        if isinstance(focused, LocationTree):
            return "loc"
        if isinstance(focused, VimListView):
            return "tag"
        if isinstance(focused, VimDataTable):
            return "table"
        if isinstance(focused, ItemDetail):
            return "detail"
        if isinstance(focused, Input) and getattr(focused, "id", None) == "search-bar":
            return "search"
        return "other"

    def action_pane_left(self) -> None:  # H
        pane = self._focused_pane()
        if pane == "detail":
            self.query_one("#items-table", VimDataTable).focus()
        elif pane == "table":
            self.query_one("#loc-tree", LocationTree).focus()
        # loc / tag / search: already leftmost, ignore

    def action_pane_right(self) -> None:  # L
        pane = self._focused_pane()
        if pane in ("loc", "tag"):
            self.query_one("#items-table", VimDataTable).focus()
        elif pane == "table":
            self.query_one("#detail-panel", ItemDetail).focus()
        # detail / search: already rightmost or special, ignore

    def action_pane_down(self) -> None:  # J
        pane = self._focused_pane()
        if pane == "loc":
            self.query_one("#tag-list", VimListView).focus()
        elif pane == "search":
            self.query_one("#items-table", VimDataTable).focus()
        # tag: already bottommost sidebar; table/detail: no below pane

    def action_pane_up(self) -> None:  # K
        pane = self._focused_pane()
        if pane == "tag":
            self.query_one("#loc-tree", LocationTree).focus()
        elif pane == "table":
            self.query_one("#search-bar", Input).focus()
        # loc: already topmost sidebar; search/detail: no above pane

    # Keep old names for compatibility if any bindings land here
    def action_focus_search(self) -> None:
        self.query_one("#search-bar", Input).focus()

    def action_clear_search(self) -> None:
        self.query_one("#search-bar", Input).value = ""
        self.query_one("#items-table", VimDataTable).focus()

    # --- Refresh / quit ---

    def action_refresh(self) -> None:
        self._location_filter = None
        self._hover_tag_id = None
        self._selected_tag_ids = set()
        self._search_query = ""
        self.query_one("#search-bar", Input).value = ""
        self.run_worker(self._load_all(), exclusive=True, group="loader")

    def action_quit(self) -> None:
        self.app.exit()

    # --- Guard: skip mutations when typing in search ---

    def _in_input(self) -> bool:
        return isinstance(self.app.focused, Input)

    # --- View image ---

    def action_view_image(self) -> None:
        if self._in_input():
            return
        detail = self.query_one("#detail-panel", ItemDetail)
        img_bytes = detail._current_img_bytes
        if img_bytes is None:
            self.notify("No image for this item", severity="warning")
            return
        self.run_worker(self._do_view_image(img_bytes), group="view")

    async def _do_view_image(self, img_bytes: bytes) -> None:
        import tempfile, os, asyncio
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(img_bytes)
            tmp = f.name

        def _show():
            from homebox_config import display_image, is_kitty_supported
            display_image(tmp)
            if is_kitty_supported():
                input("\nPress Enter to return to HomeBox…")

        try:
            with self.app.suspend():
                await asyncio.get_event_loop().run_in_executor(None, _show)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # --- Add item ---

    def action_add_item(self) -> None:
        if self._in_input():
            return
        if not self._locations_flat:
            self.notify("No locations loaded yet", severity="warning")
            return
        self.app.push_screen(AddItemScreen(self._locations_flat), self._on_add_item)

    def _on_add_item(self, result: dict | None) -> None:
        if result:
            self.run_worker(self._do_create_item(result), group="mutate")

    async def _do_create_item(self, payload: dict) -> None:
        try:
            item = await self._client.create_item(payload)
            self.notify(f"Created: {item['name']}")
            self.run_worker(self._load_all(), exclusive=True, group="loader")
        except HomeBoxError as e:
            self.notify(f"Error: {e}", severity="error")

    # --- Add location ---

    def action_add_location(self) -> None:
        if self._in_input():
            return
        self.app.push_screen(AddLocationScreen(self._locations_flat), self._on_add_location)

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
        if self._in_input() or not self._cursor_item:
            return
        name = self._cursor_item.get("name", "?")
        item_id = self._cursor_item.get("id")
        self.app.push_screen(
            ConfirmDeleteScreen(name, "item"),
            lambda ok: self.run_worker(self._do_delete_item(item_id), group="mutate") if ok else None,
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
        if self._in_input() or not self._cursor_location_id:
            self.notify("Highlight a location in the sidebar first", severity="warning")
            return
        loc = next((l for l in self._locations_flat if l["id"] == self._cursor_location_id), None)
        if not loc:
            return
        self.app.push_screen(
            ConfirmDeleteScreen(loc["name"], "location"),
            lambda ok: self.run_worker(self._do_delete_location(loc["id"]), group="mutate") if ok else None,
        )

    async def _do_delete_location(self, location_id: str) -> None:
        try:
            await self._client.delete_location(location_id)
            self.notify("Location deleted")
            self._cursor_location_id = None
            self.run_worker(self._load_all(), exclusive=True, group="loader")
        except HomeBoxError as e:
            self.notify(f"Error: {e}", severity="error")

    # --- Upload image ---

    def action_upload_image(self) -> None:
        if self._in_input() or not self._cursor_item:
            self.notify("Select an item first", severity="warning")
            return
        name = self._cursor_item.get("name", "?")
        item_id = self._cursor_item.get("id")
        self.app.push_screen(
            ImageUploadScreen(name),
            lambda path: self.run_worker(self._do_upload(item_id, path), group="mutate") if path else None,
        )

    async def _do_upload(self, item_id: str, file_path: str) -> None:
        import pathlib
        self._set_status(f"Uploading {pathlib.Path(file_path).name}…")
        try:
            await self._client.upload_item_image(item_id, file_path)
            self.notify("Image uploaded")
            self._set_status("")
        except HomeBoxError as e:
            self.notify(f"Upload error: {e}", severity="error")
            self._set_status("")

    # --- Bulk index ---

    def action_bulk_index(self) -> None:
        if self._in_input():
            return
        if not self._locations_flat:
            self.notify("No locations loaded yet", severity="warning")
            return
        from homebox_bulk import BulkIndexScreen
        self.app.push_screen(
            BulkIndexScreen(self._client, self._locations_flat),
            self._on_bulk_done,
        )

    def _on_bulk_done(self, added: int) -> None:
        if added and added > 0:
            self.run_worker(self._load_all(), exclusive=True, group="loader")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flatten_tree(tree: list[dict], result: list | None = None) -> list[dict]:
    """Convert nested location tree to flat list (for Select widgets)."""
    if result is None:
        result = []
    for node in tree:
        result.append({"id": node["id"], "name": node["name"]})
        if node.get("children"):
            _flatten_tree(node["children"], result)
    return result


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
