"""Configuration management for HomeBox CLI/TUI.

Config file: ~/.config/homebox-cli/config.toml
"""

from __future__ import annotations

import sys
import pathlib
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore
    except ImportError:
        import tomli as tomllib  # type: ignore

CONFIG_PATH = pathlib.Path.home() / ".config" / "homebox-cli" / "config.toml"

_DEFAULTS: dict[str, Any] = {
    "display": {
        # "kitty"    — use kitty graphics protocol (in-terminal inline)
        # "external" — open in external program
        # "none"     — show path/info only
        "image_viewer": "kitty",
        "external_viewer_cmd": "xdg-open",
    },
    "webcam": {
        "device_index": 0,
    },
}


def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {s: dict(v) for s, v in _DEFAULTS.items()}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            user = tomllib.load(f)
        for section, vals in user.items():
            if section in cfg and isinstance(vals, dict):
                cfg[section].update(vals)
            else:
                cfg[section] = vals
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for section, vals in cfg.items():
        lines.append(f"[{section}]")
        for key, val in vals.items():
            if isinstance(val, bool):
                lines.append(f"{key} = {'true' if val else 'false'}")
            elif isinstance(val, str):
                escaped = val.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{key} = "{escaped}"')
            else:
                lines.append(f"{key} = {val}")
        lines.append("")
    with open(CONFIG_PATH, "w") as f:
        f.write("\n".join(lines))


_cache: dict[str, Any] | None = None


def get_config() -> dict[str, Any]:
    global _cache
    if _cache is None:
        _cache = load_config()
    return _cache


def reload_config() -> dict[str, Any]:
    global _cache
    _cache = load_config()
    return _cache


# ---------------------------------------------------------------------------
# Image display helpers
# ---------------------------------------------------------------------------

def is_kitty_supported() -> bool:
    """Detect whether the running terminal supports the kitty graphics protocol."""
    import os
    return bool(
        os.environ.get("KITTY_WINDOW_ID")
        or os.environ.get("TERM") == "xterm-kitty"
        or os.environ.get("TERM_PROGRAM") in ("WezTerm",)
    )


def _kitty_wrap(data: bytes) -> bytes:
    """Wrap kitty APC data in tmux DCS passthrough when needed."""
    import os
    if not os.environ.get("TMUX"):
        return data
    escaped = data.replace(b"\x1b", b"\x1b\x1b")
    return b"\x1bPtmux;" + escaped + b"\x1b\\"


def _kitty_write(data: bytes) -> None:
    """Write kitty graphics bytes, with tmux passthrough if needed."""
    import os
    os.write(1, _kitty_wrap(data))


def _kitty_delete_all() -> None:
    """Delete all kitty images from the terminal."""
    try:
        _kitty_write(b"\033_Ga=d,q=2;\033\\")
    except Exception:
        pass


def _kitty_delete_id(image_id: int) -> None:
    """Delete a specific kitty image by ID."""
    try:
        _kitty_write(f"\033_Ga=d,d=i,i={image_id},q=2;\033\\".encode())
    except Exception:
        pass


def display_kitty_bytes(raw: bytes, image_id: int = 0) -> None:
    """Display raw image bytes (PNG/JPEG) via kitty graphics protocol.

    If *image_id* > 0, the image is assigned that ID so it can be
    replaced or deleted later.
    """
    import base64, io
    from PIL import Image

    # Resize to fit terminal (approximate 80 cols × 24 rows)
    img = Image.open(io.BytesIO(raw))
    img.thumbnail((640, 480), Image.LANCZOS)
    png_buf = io.BytesIO()
    img.save(png_buf, format="PNG", optimize=True)
    png_bytes = png_buf.getvalue()

    id_part = f",i={image_id}" if image_id else ""

    b64 = base64.standard_b64encode(png_bytes).decode()
    chunks = [b64[i : i + 4096] for i in range(0, len(b64), 4096)]
    buf = bytearray()
    for i, chunk in enumerate(chunks):
        more = 0 if i == len(chunks) - 1 else 1
        if i == 0:
            buf += f"\033_Ga=T,f=100,q=2{id_part},m={more};{chunk}\033\\".encode()
        else:
            buf += f"\033_Gm={more};{chunk}\033\\".encode()
    _kitty_write(buf)


def display_kitty_image(path: str) -> None:
    """Write a kitty graphics protocol image to stdout."""
    with open(path, "rb") as f:
        raw = f.read()
    display_kitty_bytes(raw)


def display_image(path: str) -> None:
    """Display image according to config (kitty / external / none)."""
    import subprocess

    cfg = get_config()["display"]
    viewer = cfg["image_viewer"]

    if viewer == "kitty":
        if is_kitty_supported():
            display_kitty_image(path)
        else:
            viewer = "external"

    if viewer == "external":
        cmd = cfg["external_viewer_cmd"]
        subprocess.Popen([cmd, path])


# ---------------------------------------------------------------------------
# Webcam capture
# ---------------------------------------------------------------------------

def capture_webcam(device: int = 0) -> str | None:
    """Capture a frame from the webcam with optional live kitty preview.

    Runs the actual capture in a **subprocess** so that all terminal I/O
    (setcbreak, kitty escapes, select) is fully isolated from Textual's
    driver threads.  This prevents the deadlocks that occur when writing
    to fd 1 inside ``app.suspend()``.

    Returns path to saved JPEG, or None if cancelled.
    """
    import os, sys, subprocess, tempfile

    result_file = tempfile.mktemp(suffix=".path")
    try:
        subprocess.run(
            [sys.executable, "-u", os.path.join(os.path.dirname(__file__), "homebox_capture.py"),
             str(device), result_file],
        )
    except Exception:
        return None

    if os.path.exists(result_file):
        with open(result_file) as f:
            path = f.read().strip()
        os.unlink(result_file)
        return path if path and os.path.exists(path) else None
    return None


# ---------------------------------------------------------------------------
# Image rotation
# ---------------------------------------------------------------------------

def rotate_image_cw(path: str) -> None:
    """Rotate image 90° clockwise in-place."""
    from PIL import Image

    with Image.open(path) as img:
        rotated = img.rotate(-90, expand=True)
        rotated.save(path)


def image_info(path: str) -> str:
    """Return a one-liner with image dimensions and file size."""
    import os
    from PIL import Image

    size = os.path.getsize(path)
    try:
        with Image.open(path) as img:
            w, h = img.size
        return f"{w}×{h}  {size // 1024} KB"
    except Exception:
        return f"{size // 1024} KB"
