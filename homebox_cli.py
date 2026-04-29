#!/usr/bin/env python3
"""HomeBox read-only CLI.

Requires EMAIL, PASSWORD, URL environment variables.
"""

import sys
import click
from rich.console import Console
from rich.table import Table
from rich import box

from homebox_api import HomeBoxClient, HomeBoxError, run_client

console = Console()


def err(msg: str):
    console.print(f"[red]Error:[/red] {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """HomeBox read-only CLI."""


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------

@cli.group()
def items():
    """Manage items."""


@items.command("list")
@click.option("--search", "-s", default=None, help="Search query")
@click.option("--location", "-l", default=None, help="Filter by location ID")
@click.option("--tag", "-t", default=None, help="Filter by tag ID")
@click.option("--page", "-p", default=1, show_default=True, help="Page number")
@click.option("--page-size", "-n", default=50, show_default=True, help="Items per page")
def items_list(search, location, tag, page, page_size):
    """List items."""
    try:
        result = run_client(
            lambda c: c.get_items(
                q=search,
                page=page,
                page_size=page_size,
                locations=[location] if location else None,
                tags=[tag] if tag else None,
            )
        )
    except HomeBoxError as e:
        err(str(e))

    rows = result.get("items", [])
    total = result.get("total", len(rows))

    table = Table(
        title=f"Items (page {page}, {len(rows)}/{total} shown)",
        box=box.ROUNDED,
        show_lines=False,
    )
    table.add_column("Name", style="bold cyan", no_wrap=True)
    table.add_column("Location", style="green")
    table.add_column("Tags", style="yellow")
    table.add_column("Qty", justify="right")
    table.add_column("ID", style="dim")

    for item in rows:
        loc = (item.get("location") or {}).get("name", "")
        tags_str = ", ".join(t["name"] for t in (item.get("tags") or []))
        qty = str(item.get("quantity", ""))
        table.add_row(item.get("name", ""), loc, tags_str, qty, item.get("id", ""))

    console.print(table)


@items.command("get")
@click.argument("item_id")
def items_get(item_id):
    """Get details for a single item."""
    try:
        item = run_client(lambda c: c.get_item(item_id))
    except HomeBoxError as e:
        err(str(e))

    _print_item_detail(item)


def _print_item_detail(item: dict):
    table = Table(box=box.SIMPLE, show_header=False, title=f"[bold]{item.get('name', 'Item')}[/bold]")
    table.add_column("Field", style="bold dim", no_wrap=True)
    table.add_column("Value")

    def row(label, value):
        if value not in (None, "", [], {}):
            table.add_row(label, str(value))

    row("ID", item.get("id"))
    row("Name", item.get("name"))
    row("Description", item.get("description"))
    row("Asset ID", item.get("assetId"))
    row("Quantity", item.get("quantity"))
    row("Location", (item.get("location") or {}).get("name"))
    tags = ", ".join(t["name"] for t in (item.get("tags") or []))
    row("Tags", tags)
    row("Manufacturer", item.get("manufacturer"))
    row("Model", item.get("modelNumber"))
    row("Serial Number", item.get("serialNumber"))
    row("Purchase Price", item.get("purchasePrice"))
    row("Purchase From", item.get("purchaseFrom"))
    row("Purchase Date", item.get("purchaseTime", "")[:10] if item.get("purchaseTime") else None)
    row("Insured", item.get("insured"))
    row("Warranty Expires", item.get("warrantyExpires", "")[:10] if item.get("warrantyExpires") else None)
    row("Lifetime Warranty", item.get("lifetimeWarranty"))
    row("Warranty Details", item.get("warrantyDetails"))
    row("Notes", item.get("notes"))
    row("Archived", item.get("archived"))

    fields = item.get("fields") or []
    for f in fields:
        val = f.get("textValue") or f.get("numberValue") or f.get("booleanValue")
        row(f"  {f.get('name', 'Field')}", val)

    console.print(table)


# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------

@cli.group()
def locations():
    """Manage locations."""


@locations.command("list")
def locations_list():
    """List all locations."""
    try:
        locs = run_client(lambda c: c.get_locations())
    except HomeBoxError as e:
        err(str(e))

    table = Table(title="Locations", box=box.ROUNDED)
    table.add_column("Name", style="bold cyan")
    table.add_column("Description")
    table.add_column("Items", justify="right", style="green")
    table.add_column("ID", style="dim")

    for loc in locs:
        table.add_row(
            loc.get("name", ""),
            loc.get("description", ""),
            str(loc.get("itemCount", "")),
            loc.get("id", ""),
        )

    console.print(table)


@locations.command("get")
@click.argument("location_id")
def locations_get(location_id):
    """Get details for a location."""
    try:
        loc = run_client(lambda c: c.get_location(location_id))
    except HomeBoxError as e:
        err(str(e))

    table = Table(box=box.SIMPLE, show_header=False, title=f"[bold]{loc.get('name', 'Location')}[/bold]")
    table.add_column("Field", style="bold dim")
    table.add_column("Value")
    table.add_row("ID", loc.get("id", ""))
    table.add_row("Name", loc.get("name", ""))
    table.add_row("Description", loc.get("description", ""))
    table.add_row("Item Count", str(loc.get("itemCount", "")))
    console.print(table)


# ---------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------

@cli.group()
def tags():
    """Manage tags."""


@tags.command("list")
def tags_list():
    """List all tags."""
    try:
        tgs = run_client(lambda c: c.get_tags())
    except HomeBoxError as e:
        err(str(e))

    table = Table(title="Tags", box=box.ROUNDED)
    table.add_column("Name", style="bold cyan")
    table.add_column("Description")
    table.add_column("ID", style="dim")

    for tg in tgs:
        table.add_row(
            tg.get("name", ""),
            tg.get("description", ""),
            tg.get("id", ""),
        )

    console.print(table)


@tags.command("get")
@click.argument("tag_id")
def tags_get(tag_id):
    """Get details for a tag."""
    try:
        tg = run_client(lambda c: c.get_tag(tag_id))
    except HomeBoxError as e:
        err(str(e))

    table = Table(box=box.SIMPLE, show_header=False, title=f"[bold]{tg.get('name', 'Tag')}[/bold]")
    table.add_column("Field", style="bold dim")
    table.add_column("Value")
    table.add_row("ID", tg.get("id", ""))
    table.add_row("Name", tg.get("name", ""))
    table.add_row("Description", tg.get("description", ""))
    console.print(table)


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

@cli.command()
def stats():
    """Show group statistics."""
    try:
        data = run_client(lambda c: c.get_stats())
    except HomeBoxError as e:
        err(str(e))

    table = Table(title="Statistics", box=box.ROUNDED, show_header=False)
    table.add_column("Metric", style="bold cyan")
    table.add_column("Value", justify="right", style="green")

    for key, val in data.items():
        if isinstance(val, (str, int, float, bool)):
            table.add_row(key, str(val))

    console.print(table)


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------

@cli.command()
def whoami():
    """Show current user info."""
    try:
        user = run_client(lambda c: c.get_self())
    except HomeBoxError as e:
        err(str(e))

    table = Table(title="Current User", box=box.ROUNDED, show_header=False)
    table.add_column("Field", style="bold dim")
    table.add_column("Value")
    for key, val in user.items():
        if isinstance(val, (str, int, float, bool)):
            table.add_row(key, str(val))
    console.print(table)


if __name__ == "__main__":
    cli()
