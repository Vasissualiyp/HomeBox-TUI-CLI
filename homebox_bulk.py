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

from homebox_api import HomeBoxClient, HomeBoxError
from homebox_config import (
    capture_webcam,
    display_image,
    get_config,
    image_info,
    rotate_image_cw,
)


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

    def is_ready(self) -> bool:
        return bool(self.name.strip())


# ---------------------------------------------------------------------------
# Panels (composed into ContentSwitcher)
# ---------------------------------------------------------------------------


class ChooseLocPanel(Vertical):
    """Phase 1: pick a location."""

    DEFAULT_CSS = """
    ChooseLocPanel {
        align: center middle;
        padding: 2 4;
    }
    ChooseLocPanel Label { margin-bottom: 1; }
    ChooseLocPanel #title { text-style: bold; color: $accent; margin-bottom: 2; }
    ChooseLocPanel #btn-row { height: 3; align: right middle; margin-top: 2; }
    ChooseLocPanel #btn-row Button { margin-left: 1; }
    """

    def __init__(self, locations: list[dict], **kwargs) -> None:
        super().__init__(**kwargs)
        self._locations = locations

    def compose(self) -> ComposeResult:
        yield Label("Bulk Index — Choose Location", id="title")
        yield Label("Location for all captured items:")
        yield Select(
            [(loc["name"], loc["id"]) for loc in self._locations],
            prompt="Select location…",
            id="sel-location",
        )
        with Horizontal(id="btn-row"):
            yield Button("Cancel", id="btn-cancel")
            yield Button("Start Capture →", variant="primary", id="btn-start")


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
        width: 36;
        border-left: solid $primary-darken-2;
        padding: 0 1;
    }
    #image-info-box { height: 1fr; }
    .field-label { color: $text-muted; margin-top: 1; }
    #review-actions { height: 3; margin-top: 1; }
    #review-actions Button { margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="review-header"):
            yield Static("", id="review-title")
            yield Static("", id="review-status")
        with Horizontal(id="review-body"):
            with Vertical(id="review-form"):
                yield Label("Name *", classes="field-label")
                yield Input(placeholder="Item name", id="inp-name")
                yield Label("Description", classes="field-label")
                yield Input(placeholder="Optional", id="inp-desc")
                yield Label("Quantity", classes="field-label")
                yield Input(value="1", id="inp-qty")
            with Vertical(id="review-image-panel"):
                yield Label("Image", classes="field-label")
                yield Static("—", id="image-info-box")
        with Horizontal(id="review-actions"):
            yield Button("[c] Capture new", id="btn-capture")
            yield Button("[b] Back", id="btn-back")
            yield Button("[s] Skip", id="btn-skip")
            yield Button("[v] View", id="btn-view")
            yield Button("[r] Rotate CW", id="btn-rotate")
            yield Button("[R] Retake", id="btn-retake")
            yield Button("Next → [Enter]", variant="primary", id="btn-next")
            yield Button("[f] Finish", id="btn-finish")

    def load_item(self, item: PendingItem, index: int, total: int) -> None:
        self.query_one("#review-title", Static).update(
            f"[bold]Item {index + 1} of {total}[/bold]"
        )
        self.query_one("#review-status", Static).update(
            f"  {total - index - 1} remaining"
        )
        self.query_one("#inp-name", Input).value = item.name
        self.query_one("#inp-desc", Input).value = item.description
        self.query_one("#inp-qty", Input).value = str(item.quantity)
        p = pathlib.Path(item.image_path)
        try:
            info = image_info(item.image_path)
        except Exception:
            info = "?"
        self.query_one("#image-info-box", Static).update(
            f"[bold]{p.name}[/bold]\n{info}\n\n"
            "[dim]v[/dim] view  [dim]r[/dim] rotate CW  [dim]R[/dim] retake"
        )

    def get_form_data(self) -> dict:
        try:
            qty = max(1, int(self.query_one("#inp-qty", Input).value or "1"))
        except ValueError:
            qty = 1
        return {
            "name": self.query_one("#inp-name", Input).value.strip(),
            "description": self.query_one("#inp-desc", Input).value.strip(),
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
        table.add_columns("", "Name", "Description", "Qty", "Image")
        for item in items:
            status = "⊘" if item.skip else "✓"
            table.add_row(
                status,
                item.name or "(unnamed)" if not item.skip else "(skipped)",
                item.description[:40] if not item.skip else "",
                str(item.quantity) if not item.skip else "",
                pathlib.Path(item.image_path).name,
            )


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------


class BulkIndexScreen(Screen):
    """Full-screen bulk item indexing flow."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=True),
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

    # --- Layout ---

    def compose(self) -> ComposeResult:
        yield Header()
        with ContentSwitcher(initial="choose-loc", id="switcher"):
            yield ChooseLocPanel(self._locations, id="choose-loc")
            yield ReviewPanel(id="review")
            yield ConfirmPanel(id="confirm")
        yield Footer()

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
            self._skip_current()
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

    def on_key(self, event) -> None:
        if self.query_one("#switcher", ContentSwitcher).current != "review":
            return
        # Only fire when not in an Input
        if isinstance(self.focused, Input):
            return
        key = event.key
        if key == "c":
            self._do_capture(); event.stop()
        elif key == "b":
            self._save_and_advance(-1); event.stop()
        elif key == "s":
            self._skip_current(); event.stop()
        elif key == "v":
            self._view_current(); event.stop()
        elif key == "r":
            self._rotate_current(); event.stop()
        elif key == "R":
            self._do_retake(); event.stop()
        elif key == "f":
            self._goto_confirm(); event.stop()
        elif key == "enter":
            self._save_and_advance(+1); event.stop()

    # --- Phase 1: choose location ---

    def _start_capture(self) -> None:
        sel = self.query_one("#sel-location", Select)
        if sel.value is Select.BLANK:
            self.notify("Please select a location first", severity="warning")
            return
        self._location_id = str(sel.value)
        loc = next((l for l in self._locations if l["id"] == self._location_id), None)
        self._location_name = loc["name"] if loc else self._location_id
        self.sub_title = f"Location: {self._location_name}"
        # Capture first photo immediately
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

    def _skip_current(self) -> None:
        if not self._items:
            return
        self._items[self._idx].skip = True
        self._save_and_advance(+1)

    def _do_capture(self, after_start: bool = False) -> None:
        """Suspend TUI, open webcam, capture a frame."""
        self.run_worker(self._capture_thread, thread=True, exclusive=True, group="capture")
        if after_start:
            self._pending_goto_review = True

    _pending_goto_review: bool = False

    def _capture_thread(self) -> None:
        """Runs in background thread — suspends TUI for webcam."""
        with self.app.suspend():
            path = capture_webcam(self._cfg["webcam"]["device_index"])
        self.call_from_thread(self._on_capture_done, path)

    def _on_capture_done(self, path: str | None) -> None:
        if path is None:
            self.notify("Capture cancelled", severity="warning")
            if not self._items:
                # Nothing captured, go back to choose-loc
                self.query_one("#switcher", ContentSwitcher).current = "choose-loc"
            return
        item = PendingItem(image_path=path)
        self._items.append(item)
        self._idx = len(self._items) - 1
        self._goto_review()
        self._pending_goto_review = False

    def _do_retake(self) -> None:
        """Replace current item's image with a new capture."""
        if not self._items:
            return
        self._save_current_form()
        old_path = self._items[self._idx].image_path
        self.run_worker(self._retake_thread(old_path), thread=False, group="capture")

    async def _retake_thread(self, old_path: str) -> None:
        import asyncio

        def _blocking():
            with self.app.suspend():
                return capture_webcam(self._cfg["webcam"]["device_index"])

        path = await asyncio.get_event_loop().run_in_executor(None, _blocking)
        if path:
            # Remove old temp file
            try:
                pathlib.Path(old_path).unlink(missing_ok=True)
            except Exception:
                pass
            self._items[self._idx].image_path = path
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
            # Suspend TUI and display via kitty or fallback
            self.run_worker(self._view_thread(path), thread=False, group="view")

    async def _view_thread(self, path: str) -> None:
        import asyncio

        def _blocking():
            with self.app.suspend():
                from homebox_config import display_kitty_image, is_kitty_supported
                if is_kitty_supported():
                    print(f"\n  {pathlib.Path(path).name}\n")
                    display_kitty_image(path)
                else:
                    print(f"\n  Image: {path}\n")
                print("\nPress Enter to return to HomeBox...")
                input()

        await asyncio.get_event_loop().run_in_executor(None, _blocking)

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

    async def _submit_all(self) -> None:
        active = [i for i in self._items if not i.skip]
        self.notify(f"Submitting {len(active)} items…")
        errors = 0
        for item in active:
            try:
                created = await self._client.create_item({
                    "name": item.name,
                    "description": item.description,
                    "quantity": item.quantity,
                    "locationId": self._location_id,
                })
                await self._client.upload_item_image(created["id"], item.image_path)
            except HomeBoxError as e:
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
