"""Bulk item indexing screen for HomeBox TUI.

Flow:
  1. Choose location
  2. Capture photos (webcam) one by one
  3. For each photo: fill in name / description / quantity
     — navigate back, skip, rotate, retake, view image
  4. Confirm the whole batch → submit to API
"""

from __future__ import annotations

import asyncio
import pathlib
from dataclasses import dataclass, field
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.screen import Screen
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
)

from textual.events import Key
from textual.message import Message

from homebox_api import HomeBoxClient, HomeBoxError
from homebox_config import (
    capture_webcam,
    display_image,
    get_config,
    image_info,
    rotate_image_cw,
)


# ---------------------------------------------------------------------------
# Custom Input that exits on Escape
# ---------------------------------------------------------------------------


class VimInput(Input):
    """Input that posts an ExitInsert message when Escape is pressed."""

    class ExitInsert(Message):
        """Fired when user presses Escape in this input."""

    async def _on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.post_message(self.ExitInsert())
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class PendingItem:
    image_path: str
    name: str = ""
    description: str = ""
    quantity: int = 1
    skip: bool = False
    tag_ids: list[str] = field(default_factory=list)
    tag_names: list[str] = field(default_factory=list)

    def is_ready(self) -> bool:
        return bool(self.name.strip())


# ---------------------------------------------------------------------------
# Panels (composed into ContentSwitcher)
# ---------------------------------------------------------------------------


def _detect_webcams() -> list[tuple[str, int]]:
    """Return list of (label, device_index) for available webcams."""
    import glob
    devices = sorted(glob.glob("/dev/video*"))
    if devices:
        return [(dev, int(dev.replace("/dev/video", ""))) for dev in devices]
    # Fallback: probe indices 0-3
    found = []
    try:
        import cv2
        for i in range(4):
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                found.append((f"Camera {i}", i))
            cap.release()
    except Exception:
        pass
    return found if found else [("Camera 0 (default)", 0)]


class ChooseLocPanel(Vertical):
    """Phase 1: pick a location and optionally a webcam."""

    DEFAULT_CSS = """
    ChooseLocPanel {
        padding: 2 4;
    }
    ChooseLocPanel Label { margin-bottom: 1; }
    ChooseLocPanel #title { text-style: bold; color: $accent; margin-bottom: 2; }
    ChooseLocPanel #body-row { height: 1fr; }
    ChooseLocPanel #form-col { width: 1fr; }
    ChooseLocPanel #webcam-row { height: auto; margin-top: 1; }
    ChooseLocPanel #webcam-toggle { height: 1; margin-bottom: 1; }
    ChooseLocPanel #sel-webcam { display: none; }
    ChooseLocPanel #preview-col {
        width: 40;
        border-left: solid $primary-darken-2;
        padding: 0 1;
        display: none;
    }
    ChooseLocPanel #preview-label { color: $text-muted; height: 1; margin-bottom: 1; }
    ChooseLocPanel #btn-row { height: 3; align: right middle; margin-top: 2; }
    ChooseLocPanel #btn-row Button { margin-left: 1; }
    """

    def __init__(self, locations: list[dict], default_device: int = 0, **kwargs) -> None:
        super().__init__(**kwargs)
        self._locations = locations
        self._default_device = default_device
        self._webcams = _detect_webcams()

    def compose(self) -> ComposeResult:
        from homebox_tui import KittyImageWidget
        yield Label("Bulk Index — Choose Location", id="title")
        with Horizontal(id="body-row"):
            with Vertical(id="form-col"):
                yield Label("Location for all captured items:")
                yield Select(
                    [(loc["name"], loc["id"]) for loc in self._locations],
                    prompt="Select location…",
                    id="sel-location",
                )
                with Vertical(id="webcam-row"):
                    yield Button("▶ Choose webcam device", id="webcam-toggle", variant="default")
                    options = []
                    for label, idx in self._webcams:
                        marker = " ✓" if idx == self._default_device else ""
                        options.append((f"{label}{marker}", idx))
                    if not options:
                        options = [("Camera 0 (default)", 0)]
                    yield Select(options, value=self._default_device, id="sel-webcam")
            with Vertical(id="preview-col"):
                yield Static("Webcam preview", id="preview-label")
                yield KittyImageWidget(id="cam-preview-img")
        with Horizontal(id="btn-row"):
            yield Button("Cancel", id="btn-cancel")
            yield Button("Start Capture →", variant="primary", id="btn-start")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "webcam-toggle":
            sel = self.query_one("#sel-webcam", Select)
            preview_col = self.query_one("#preview-col")
            if sel.display:
                sel.display = False
                preview_col.display = False
                event.button.label = "▶ Choose webcam device"
            else:
                sel.display = True
                preview_col.display = True
                event.button.label = "▼ Choose webcam device"
                # Show preview for currently selected device
                self._refresh_preview()
            event.stop()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "sel-webcam" and event.value is not Select.BLANK:
            self._refresh_preview()

    def _refresh_preview(self) -> None:
        device = self.selected_device
        self.query_one("#preview-label", Static).update(
            f"[dim]Grabbing frame from /dev/video{device}…[/dim]"
        )
        self.run_worker(self._grab_preview(device), exclusive=True, group="cam-preview")

    async def _grab_preview(self, device: int) -> None:
        import asyncio
        jpeg = await asyncio.get_event_loop().run_in_executor(
            None, _grab_single_frame, device
        )
        from homebox_tui import KittyImageWidget
        label = self.query_one("#preview-label", Static)
        img_w = self.query_one("#cam-preview-img", KittyImageWidget)
        if jpeg:
            label.update(f"/dev/video{device}")
            img_w.set_image(jpeg)
        else:
            label.update(f"[red]Cannot open /dev/video{device}[/red]")
            img_w.set_image(None)

    @property
    def selected_device(self) -> int:
        sel = self.query_one("#sel-webcam", Select)
        v = sel.value
        return int(v) if v is not Select.BLANK else self._default_device


def _grab_single_frame(device: int) -> bytes | None:
    """Grab one JPEG frame from a webcam (blocking, runs in executor)."""
    try:
        import cv2
        cap = cv2.VideoCapture(device)
        if not cap.isOpened():
            return None
        # Warm-up read (first frame is often garbage)
        cap.read()
        ret, frame = cap.read()
        cap.release()
        if ret:
            _, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            return jpg.tobytes()
    except Exception:
        pass
    return None


class ReviewPanel(Vertical):
    """Phase 2: review / fill in each photo."""

    DEFAULT_CSS = """
    ReviewPanel { padding: 1 2; }
    #review-header { height: 3; }
    #review-title { text-style: bold; color: $accent; }
    #review-status { color: $text-muted; }
    #review-body { height: 1fr; }
    #review-form { width: 1fr; padding-right: 2; }
    #review-image-panel {
        width: 52;
        border-left: solid $primary-darken-2;
        padding: 0 1;
    }
    #review-kitty-img { height: 16; width: 100%; }
    #image-info-box { height: auto; }
    .field-label { color: $text-muted; margin-top: 1; }
    #skip-indicator { color: $warning; height: auto; margin-top: 1; }
    #tag-display { height: auto; margin-top: 1; }
    #review-shortcuts { height: 1; color: $text-muted; dock: bottom; background: $surface-darken-1; padding: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="review-header"):
            yield Static("", id="review-title")
            yield Static("", id="review-status")
        with Horizontal(id="review-body"):
            with Vertical(id="review-form"):
                yield Label("\[n]ame *", classes="field-label")
                yield VimInput(placeholder="Item name", id="inp-name")
                yield Label("\[d]escription", classes="field-label")
                yield VimInput(placeholder="Optional", id="inp-desc")
                yield Label("\[Q]uantity", classes="field-label")
                yield VimInput(value="1", id="inp-qty")
                yield Label("\[t]ags", classes="field-label")
                yield Static("[dim](none)[/dim]", id="tag-display")
                yield Static("", id="skip-indicator")
            with Vertical(id="review-image-panel"):
                from homebox_tui import KittyImageWidget
                yield KittyImageWidget(id="review-kitty-img")
                yield Static("—", id="image-info-box")
        yield Static(
            " \[b]ack  \[n]ame \[d]esc \[Q]ty \[t]ags \[i]nsert  |"
            "  \[s]kip \[v]iew \[r]otate \[R]etake \[c]apture  |"
            "  Enter:next  \[f]inish",
            id="review-shortcuts",
        )

    def load_item(self, item: PendingItem, index: int, total: int) -> None:
        self.query_one("#review-title", Static).update(
            f"[bold]Item {index + 1} of {total}[/bold]"
        )
        self.query_one("#review-status", Static).update(
            f"  {total - index - 1} remaining"
        )
        self.query_one("#inp-name", VimInput).value = item.name
        self.query_one("#inp-desc", VimInput).value = item.description
        self.query_one("#inp-qty", VimInput).value = str(item.quantity)
        # Tags
        tag_w = self.query_one("#tag-display", Static)
        if item.tag_names:
            tag_w.update(", ".join(item.tag_names))
        else:
            tag_w.update("[dim](none)[/dim]")
        # Skip indicator
        skip_w = self.query_one("#skip-indicator", Static)
        if item.skip:
            skip_w.update("[bold yellow]⊘ SKIPPED[/bold yellow]  (press [bold]s[/bold] to unskip)")
        else:
            skip_w.update("")
        p = pathlib.Path(item.image_path)
        try:
            info = image_info(item.image_path)
        except Exception:
            info = "?"
        self.query_one("#image-info-box", Static).update(
            f"[bold]{p.name}[/bold]\n{info}"
        )
        # Show image via kitty protocol
        try:
            from homebox_tui import KittyImageWidget
            with open(item.image_path, "rb") as f:
                img_bytes = f.read()
            self.query_one("#review-kitty-img", KittyImageWidget).set_image(img_bytes)
        except Exception:
            pass

    def get_form_data(self) -> dict:
        try:
            qty = max(1, int(self.query_one("#inp-qty", VimInput).value or "1"))
        except ValueError:
            qty = 1
        return {
            "name": self.query_one("#inp-name", VimInput).value.strip(),
            "description": self.query_one("#inp-desc", VimInput).value.strip(),
            "quantity": qty,
        }


class ConfirmPanel(Vertical):
    """Phase 3: summary + confirm."""

    DEFAULT_CSS = """
    ConfirmPanel { padding: 1 2; }
    #confirm-title { text-style: bold; color: $accent; margin-bottom: 1; }
    #confirm-table { height: 1fr; }
    #confirm-actions { height: 3; margin-top: 1; align: right middle; }
    #confirm-actions Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="confirm-title")
        yield DataTable(id="confirm-table", cursor_type="row")
        with Horizontal(id="confirm-actions"):
            yield Button("← Back to Review", id="btn-back-confirm")
            yield Button("Add All Items", variant="primary", id="btn-add-all")

    def load_items(self, items: list[PendingItem], location_name: str) -> None:
        active = [i for i in items if not i.skip]
        skipped = [i for i in items if i.skip]
        self.query_one("#confirm-title", Static).update(
            f"[bold]Add {len(active)} item(s) to '{location_name}'[/bold]"
            + (f"  ({len(skipped)} skipped)" if skipped else "")
        )
        table = self.query_one("#confirm-table", DataTable)
        table.clear(columns=True)
        table.add_columns("", "Name", "Description", "Qty", "Tags", "Image")
        for item in items:
            status = "⊘" if item.skip else "✓"
            tags = ", ".join(item.tag_names) if not item.skip else ""
            table.add_row(
                status,
                item.name or "(unnamed)" if not item.skip else "(skipped)",
                item.description[:40] if not item.skip else "",
                str(item.quantity) if not item.skip else "",
                tags,
                pathlib.Path(item.image_path).name,
            )


# ---------------------------------------------------------------------------
# Tag picker modal
# ---------------------------------------------------------------------------


class TagPickerScreen(Screen):
    """Modal for picking tags. Vim-style navigation: j/k, gg/G, Ctrl-u/d, / to search."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss_or_clear", "Cancel"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "go_top", "Top", show=False),
        Binding("shift+g", "go_bottom", "Bottom", show=False),
        Binding("ctrl+d", "half_down", "½ Down", show=False),
        Binding("ctrl+u", "half_up", "½ Up", show=False),
        Binding("slash", "start_search", "Search", show=False),
    ]

    DEFAULT_CSS = """
    TagPickerScreen { align: center middle; }
    #tag-dialog {
        background: $surface;
        border: solid $primary;
        padding: 1 2;
        width: 50;
        height: auto;
        max-height: 80%;
    }
    #tag-dialog-title { text-style: bold; color: $accent; margin-bottom: 1; }
    #tag-list { height: auto; max-height: 16; }
    .tag-item { padding: 0 1; }
    .tag-item.selected { color: $success; }
    #tag-search-row { height: 3; margin-top: 1; display: none; }
    #tag-search-row Input { width: 1fr; }
    #new-tag-row { height: 3; margin-top: 1; }
    #new-tag-row Input { width: 1fr; }
    #new-tag-row Button { margin-left: 1; }
    #tag-btn-row { height: 3; margin-top: 1; align: right middle; }
    #tag-btn-row Button { margin-left: 1; }
    #tag-hints { color: $text-muted; height: 1; }
    """

    def __init__(
        self,
        all_tags: list[dict],
        selected_ids: list[str],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._all_tags = list(all_tags)
        self._selected: set[str] = set(selected_ids)
        self._filter: str = ""
        self._searching: bool = False
        self._g_pressed: bool = False
        self._gen: int = 0  # generation counter for unique IDs

    def compose(self) -> ComposeResult:
        from textual.widgets import ListView, ListItem
        with Vertical(id="tag-dialog"):
            yield Static("Select Tags  (Enter: toggle | /: search | +: new)", id="tag-dialog-title")
            lv = ListView(id="tag-list")
            yield lv
            with Horizontal(id="tag-search-row"):
                yield Input(placeholder="Filter tags…", id="inp-tag-search")
            with Horizontal(id="new-tag-row"):
                yield Input(placeholder="New tag name…", id="inp-new-tag")
                yield Button("+", id="btn-add-tag", variant="primary")
            yield Static("j/k:move  gg/G:top/bot  Ctrl-u/d:½page  Enter:toggle  Esc:done", id="tag-hints")
            with Horizontal(id="tag-btn-row"):
                yield Button("Done", variant="primary", id="btn-tag-done")

    def on_mount(self) -> None:
        self._rebuild_list()
        self.query_one("#tag-list").focus()

    @property
    def _filtered_tags(self) -> list[dict]:
        if not self._filter:
            return self._all_tags
        f = self._filter.lower()
        return [t for t in self._all_tags if f in t["name"].lower()]

    def _rebuild_list(self) -> None:
        from textual.widgets import ListView, ListItem
        lv = self.query_one("#tag-list", ListView)
        # Store the tag data on the ListView itself to avoid widget ID issues
        tags = self._filtered_tags
        children = list(lv.children)
        # Update existing items, add/remove as needed
        for i, tag in enumerate(tags):
            tid = tag["id"]
            name = tag["name"]
            marker = "✓ " if tid in self._selected else "  "
            text = f"{marker}{name}"
            if i < len(children):
                # Update existing item
                children[i]._tag_id = tid
                children[i]._tag_name = name
                children[i].query_one(Static).update(text)
            else:
                self._gen += 1
                item = ListItem(Static(text), id=f"tg{self._gen}-{i}")
                item._tag_id = tid
                item._tag_name = name
                lv.append(item)
        # Remove excess items (in reverse to avoid index shift)
        for i in range(len(children) - 1, len(tags) - 1, -1):
            if i >= 0 and i < len(children):
                children[i].remove()

    def _is_list_focused(self) -> bool:
        from textual.widgets import ListView
        focused = self.app.focused
        return isinstance(focused, ListView) or (focused is not None and focused.id == "tag-list")

    def on_list_view_selected(self, event) -> None:
        item = event.item
        tid = getattr(item, "_tag_id", None)
        if tid is None:
            return
        if tid in self._selected:
            self._selected.discard(tid)
        else:
            self._selected.add(tid)
        # Remember cursor position
        from textual.widgets import ListView
        lv = self.query_one("#tag-list", ListView)
        idx = lv.index
        self._rebuild_list()
        lv.index = min(idx, len(self._filtered_tags) - 1)

    # --- Vim navigation ---

    def action_cursor_down(self) -> None:
        self._g_pressed = False
        if not self._is_list_focused():
            return
        from textual.widgets import ListView
        lv = self.query_one("#tag-list", ListView)
        if lv.index is not None and lv.index < len(self._filtered_tags) - 1:
            lv.index += 1

    def action_cursor_up(self) -> None:
        self._g_pressed = False
        if not self._is_list_focused():
            return
        from textual.widgets import ListView
        lv = self.query_one("#tag-list", ListView)
        if lv.index is not None and lv.index > 0:
            lv.index -= 1

    def action_go_top(self) -> None:
        if not self._is_list_focused():
            self._g_pressed = False
            return
        if not self._g_pressed:
            self._g_pressed = True
            return
        self._g_pressed = False
        from textual.widgets import ListView
        lv = self.query_one("#tag-list", ListView)
        if len(self._filtered_tags):
            lv.index = 0

    def action_go_bottom(self) -> None:
        self._g_pressed = False
        if not self._is_list_focused():
            return
        from textual.widgets import ListView
        lv = self.query_one("#tag-list", ListView)
        n = len(self._filtered_tags)
        if n:
            lv.index = n - 1

    def action_half_down(self) -> None:
        self._g_pressed = False
        if not self._is_list_focused():
            return
        from textual.widgets import ListView
        lv = self.query_one("#tag-list", ListView)
        n = len(self._filtered_tags)
        if lv.index is not None:
            lv.index = min(lv.index + 8, n - 1)

    def action_half_up(self) -> None:
        self._g_pressed = False
        if not self._is_list_focused():
            return
        from textual.widgets import ListView
        lv = self.query_one("#tag-list", ListView)
        if lv.index is not None:
            lv.index = max(lv.index - 8, 0)

    def action_start_search(self) -> None:
        self._g_pressed = False
        if not self._is_list_focused():
            return
        self._searching = True
        row = self.query_one("#tag-search-row")
        row.display = True
        inp = self.query_one("#inp-tag-search", Input)
        inp.value = ""
        inp.focus()

    def _end_search(self) -> None:
        self._searching = False
        self.query_one("#tag-search-row").display = False
        self.query_one("#tag-list").focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "inp-tag-search":
            self._filter = event.input.value.strip()
            self._rebuild_list()

    def action_dismiss_or_clear(self) -> None:
        self._g_pressed = False
        if self._searching:
            self._filter = ""
            self._rebuild_list()
            self._end_search()
        elif isinstance(self.app.focused, Input):
            self.query_one("#tag-list").focus()
        else:
            self._finish()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "inp-tag-search":
            self._end_search()
        elif event.input.id == "inp-new-tag":
            self._add_new_tag()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-add-tag":
            self._add_new_tag()
        elif event.button.id == "btn-tag-done":
            self._finish()

    def _add_new_tag(self) -> None:
        name = self.query_one("#inp-new-tag", Input).value.strip()
        if not name:
            return
        # Avoid duplicates
        for t in self._all_tags:
            if t["name"].lower() == name.lower():
                self._selected.add(t["id"])
                self.query_one("#inp-new-tag", Input).value = ""
                self._rebuild_list()
                return
        new_tag = {"id": f"__new__{name}", "name": name}
        self._all_tags.append(new_tag)
        self._selected.add(new_tag["id"])
        self.query_one("#inp-new-tag", Input).value = ""
        self._rebuild_list()

    def _finish(self) -> None:
        result_ids = list(self._selected)
        result_names = []
        for tag in self._all_tags:
            if tag["id"] in self._selected:
                result_names.append(tag["name"])
        self.dismiss({"ids": result_ids, "names": result_names, "all_tags": self._all_tags})


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------


class BulkIndexScreen(Screen):
    """Full-screen bulk item indexing flow."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "maybe_cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    BulkIndexScreen { background: $surface; }
    ContentSwitcher { height: 1fr; }
    """

    def __init__(self, client: HomeBoxClient, locations: list[dict], **kwargs) -> None:
        super().__init__(**kwargs)
        self._client = client
        self._locations = locations
        self._location_id: str = ""
        self._location_name: str = ""
        self._items: list[PendingItem] = []
        self._idx: int = 0
        self._cfg = get_config()
        self._all_tags: list[dict] = []
        self._device: int = self._cfg["webcam"]["device_index"]

    # --- Layout ---

    def compose(self) -> ComposeResult:
        yield Header()
        with ContentSwitcher(initial="choose-loc", id="switcher"):
            yield ChooseLocPanel(self._locations, default_device=self._device, id="choose-loc")
            yield ReviewPanel(id="review")
            yield ConfirmPanel(id="confirm")
        yield Footer()

    async def on_mount(self) -> None:
        try:
            self._all_tags = await self._client.get_tags()
        except Exception:
            self._all_tags = []

    # --- Escape handling ---

    def action_maybe_cancel(self) -> None:
        """Context-aware Escape: cancel on choose-loc/confirm, no-op on review (VimInput handles it)."""
        current = self.query_one("#switcher", ContentSwitcher).current
        if current == "choose-loc":
            self.dismiss(None)
        elif current == "confirm":
            # Go back to review
            self.query_one("#switcher", ContentSwitcher).current = "review"
            self._refresh_review()
        # On "review" screen: do nothing — let VimInput handle Escape

    # --- Button routing ---

    def on_button_pressed(self, event: Button.Pressed) -> None:  # noqa: C901
        bid = event.button.id
        switcher = self.query_one("#switcher", ContentSwitcher)
        current = switcher.current

        # --- Choose location ---
        if bid == "btn-cancel":
            self.action_cancel()
        elif bid == "btn-start" and current == "choose-loc":
            self._start_capture()

        # --- Review ---
        elif bid == "btn-next" and current == "review":
            self._save_and_advance(+1)
        elif bid == "btn-back" and current == "review":
            self._save_and_advance(-1)
        elif bid == "btn-skip" and current == "review":
            self._toggle_skip()
        elif bid == "btn-capture" and current == "review":
            self._do_capture()
        elif bid == "btn-retake" and current == "review":
            self._do_retake()
        elif bid == "btn-rotate" and current == "review":
            self._rotate_current()
        elif bid == "btn-view" and current == "review":
            self._view_current()
        elif bid == "btn-finish" and current == "review":
            self._goto_confirm()

        # --- Confirm ---
        elif bid == "btn-back-confirm" and current == "confirm":
            self._goto_review()
        elif bid == "btn-add-all" and current == "confirm":
            self.run_worker(self._submit_all(), group="submit")

    # --- Keyboard bindings in review phase ---

    def _focus_input(self, input_id: str) -> None:
        """Focus an input field (enter insert mode)."""
        inp = self.query_one(f"#{input_id}", VimInput)
        inp.focus()

    def on_vim_input_exit_insert(self, event: VimInput.ExitInsert) -> None:
        """Handle Escape from any VimInput — exit insert mode."""
        self.set_focus(None)

    def on_key(self, event) -> None:
        if self.query_one("#switcher", ContentSwitcher).current != "review":
            return

        # Don't intercept keys when an input is focused
        if isinstance(self.app.focused, (Input, VimInput)):
            return

        key = event.key

        # --- Normal mode shortcuts ---
        if key == "n":
            self._focus_input("inp-name"); event.stop()
        elif key == "d":
            self._focus_input("inp-desc"); event.stop()
        elif key == "Q":
            self._focus_input("inp-qty"); event.stop()
        elif key == "i":
            self._focus_input("inp-name"); event.stop()
        elif key == "t":
            self._open_tag_picker(); event.stop()
        elif key == "c":
            self._do_capture(); event.stop()
        elif key == "b":
            self._save_and_advance(-1); event.stop()
        elif key == "s":
            self._toggle_skip(); event.stop()
        elif key == "v":
            self._view_current(); event.stop()
        elif key == "r":
            self._rotate_current(); event.stop()
        elif key == "R":
            self._do_retake(); event.stop()
        elif key == "f":
            self._goto_confirm(); event.stop()
        elif key in ("enter", "j"):
            self._save_and_advance(+1); event.stop()
        elif key == "k":
            self._save_and_advance(-1); event.stop()

    # --- Phase 1: choose location ---

    def _start_capture(self) -> None:
        panel = self.query_one(ChooseLocPanel)
        if panel.query_one("#sel-location", Select).value is Select.BLANK:
            self.notify("Please select a location first", severity="warning")
            return
        self._location_id = str(panel.query_one("#sel-location", Select).value)
        loc = next((l for l in self._locations if l["id"] == self._location_id), None)
        self._location_name = loc["name"] if loc else self._location_id
        self._device = panel.selected_device
        self.sub_title = f"Location: {self._location_name}  |  Device: /dev/video{self._device}"
        self._do_capture(after_start=True)

    # --- Phase 2: review ---

    def _goto_review(self) -> None:
        if not self._items:
            self.notify("No photos captured yet", severity="warning")
            return
        self.query_one("#switcher", ContentSwitcher).current = "review"
        self._refresh_review()

    def _refresh_review(self) -> None:
        if not self._items:
            return
        panel = self.query_one(ReviewPanel)
        panel.load_item(self._items[self._idx], self._idx, len(self._items))

    def _save_current_form(self) -> None:
        if not self._items:
            return
        data = self.query_one(ReviewPanel).get_form_data()
        item = self._items[self._idx]
        item.name = data["name"]
        item.description = data["description"]
        item.quantity = data["quantity"]

    def _save_and_advance(self, direction: int) -> None:
        if not self._items:
            return
        self._save_current_form()
        new_idx = self._idx + direction
        if new_idx < 0:
            self.notify("Already at the first item", severity="warning")
            return
        if new_idx >= len(self._items):
            self._goto_confirm()
            return
        self._idx = new_idx
        self._refresh_review()

    def _toggle_skip(self) -> None:
        if not self._items:
            return
        item = self._items[self._idx]
        item.skip = not item.skip
        if item.skip:
            self.notify("Marked as skipped")
        else:
            self.notify("Unskipped")
        self._refresh_review()

    def _open_tag_picker(self) -> None:
        if not self._items:
            return
        self._save_current_form()
        item = self._items[self._idx]
        self.app.push_screen(
            TagPickerScreen(self._all_tags, item.tag_ids),
            self._on_tag_pick_done,
        )

    def _on_tag_pick_done(self, result: dict | None) -> None:
        if result is None or not self._items:
            return
        item = self._items[self._idx]
        item.tag_ids = result["ids"]
        item.tag_names = result["names"]
        # Update master tag list with any newly created tags
        self._all_tags = result["all_tags"]
        self._refresh_review()

    def _do_capture(self, after_start: bool = False) -> None:
        """Suspend TUI, open webcam, capture photos in a loop."""
        if after_start:
            self._pending_goto_review = True

        device = self._device

        with self.app.suspend():
            try:
                paths = capture_webcam(device)
            except Exception:
                paths = []

        self._on_capture_done(paths)

    _pending_goto_review: bool = False

    def _on_capture_done(self, paths: list[str]) -> None:
        if not paths:
            self.notify("No photos captured", severity="warning")
            if not self._items:
                self.query_one("#switcher", ContentSwitcher).current = "choose-loc"
            return
        for p in paths:
            self._items.append(PendingItem(image_path=p))
        self._idx = 0
        self.notify(f"{len(paths)} photo(s) captured — review each item below")
        self._goto_review()
        self._pending_goto_review = False

    def _do_retake(self) -> None:
        """Replace current item's image with a new capture."""
        if not self._items:
            return
        self._save_current_form()
        old_path = self._items[self._idx].image_path
        device = self._device

        with self.app.suspend():
            try:
                paths = capture_webcam(device)
            except Exception:
                paths = []

        if paths:
            try:
                pathlib.Path(old_path).unlink(missing_ok=True)
            except Exception:
                pass
            self._items[self._idx].image_path = paths[0]
            for p in paths[1:]:
                try:
                    pathlib.Path(p).unlink(missing_ok=True)
                except Exception:
                    pass
            self._refresh_review()
            self.notify("Photo replaced")
        else:
            self.notify("Retake cancelled", severity="warning")

    def _rotate_current(self) -> None:
        if not self._items:
            return
        path = self._items[self._idx].image_path
        try:
            rotate_image_cw(path)
            self.notify("Rotated 90° CW")
            self._refresh_review()
        except Exception as e:
            self.notify(f"Rotate failed: {e}", severity="error")

    def _view_current(self) -> None:
        if not self._items:
            return
        path = self._items[self._idx].image_path
        cfg = self._cfg["display"]
        viewer = cfg["image_viewer"]

        if viewer == "external":
            import subprocess
            subprocess.Popen([cfg["external_viewer_cmd"], path])
        else:
            with self.app.suspend():
                from homebox_config import display_kitty_image, is_kitty_supported
                if is_kitty_supported():
                    print(f"\n  {pathlib.Path(path).name}\n")
                    display_kitty_image(path)
                else:
                    print(f"\n  Image: {path}\n")
                print("\nPress Enter to return to HomeBox...")
                input()

    # --- Phase 3: confirm & submit ---

    def _goto_confirm(self) -> None:
        if not self._items:
            self.notify("No photos captured yet", severity="warning")
            return
        # Save current form if in review
        if self.query_one("#switcher", ContentSwitcher).current == "review":
            self._save_current_form()
        active = [i for i in self._items if not i.skip]
        if not active:
            self.notify("All items are skipped — nothing to add", severity="warning")
            return
        unnamed = [i for i in active if not i.is_ready()]
        if unnamed:
            self.notify(
                f"{len(unnamed)} item(s) have no name — fill in names or skip them",
                severity="warning",
            )
            return
        panel = self.query_one(ConfirmPanel)
        panel.load_items(self._items, self._location_name)
        self.query_one("#switcher", ContentSwitcher).current = "confirm"

    async def _resolve_tag_id(self, tag_id: str) -> str | None:
        """Resolve a tag ID — create via API if it's a placeholder __new__ ID."""
        if tag_id.startswith("__new__"):
            name = tag_id[len("__new__"):]
            try:
                created = await self._client.create_tag(name)
                return created["id"]
            except Exception:
                return None
        return tag_id

    async def _submit_all(self) -> None:
        active = [i for i in self._items if not i.skip]
        self.notify(f"Submitting {len(active)} items…")

        # Resolve all placeholder tag IDs first (create new tags)
        tag_cache: dict[str, str] = {}  # placeholder → real ID
        for item in active:
            for tid in item.tag_ids:
                if tid.startswith("__new__") and tid not in tag_cache:
                    real = await self._resolve_tag_id(tid)
                    if real:
                        tag_cache[tid] = real

        errors = 0
        for item in active:
            try:
                created = await self._client.create_item({
                    "name": item.name,
                    "description": item.description,
                    "quantity": item.quantity,
                    "locationId": self._location_id,
                })
                item_id = created["id"]
                await self._client.upload_item_image(item_id, item.image_path)
                # Assign tags if any
                if item.tag_ids:
                    real_ids = [tag_cache.get(t, t) for t in item.tag_ids]
                    real_ids = [t for t in real_ids if t]  # filter None
                    if real_ids:
                        # Fetch full item, set labelIds, send only update-safe fields
                        full = await self._client.get_item(item_id)
                        update = {
                            "id": item_id,
                            "name": full.get("name", item.name),
                            "description": full.get("description", item.description),
                            "quantity": full.get("quantity", item.quantity),
                            "locationId": self._location_id,
                            "labelIds": real_ids,
                        }
                        await self._client.update_item(item_id, update)
            except (HomeBoxError, Exception) as e:
                self.notify(f"Error adding '{item.name}': {e}", severity="error")
                errors += 1

        # Clean up temp files
        for item in self._items:
            try:
                pathlib.Path(item.image_path).unlink(missing_ok=True)
            except Exception:
                pass

        if errors == 0:
            self.notify(f"Added {len(active)} items successfully!", severity="information")
        else:
            self.notify(f"Done with {errors} error(s)", severity="warning")

        self.dismiss(len(active) - errors)

    # --- Cancel ---

    def action_cancel(self) -> None:
        # Clean up temp files
        for item in self._items:
            try:
                pathlib.Path(item.image_path).unlink(missing_ok=True)
            except Exception:
                pass
        self.dismiss(0)
