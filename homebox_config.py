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


def display_kitty_image(path: str) -> None:
    """Write a kitty graphics protocol image to stdout."""
    import base64
    import sys

    with open(path, "rb") as f:
        raw = f.read()
    b64 = base64.standard_b64encode(raw).decode()
    chunks = [b64[i : i + 4096] for i in range(0, len(b64), 4096)]
    buf = bytearray()
    for i, chunk in enumerate(chunks):
        more = 0 if i == len(chunks) - 1 else 1
        if i == 0:
            buf += f"\033_Ga=T,f=100,m={more};{chunk}\033\\".encode()
        else:
            buf += f"\033_Gm={more};{chunk}\033\\".encode()
    sys.stdout.buffer.write(buf)
    sys.stdout.buffer.flush()


def display_image(path: str) -> None:
    """Display image according to config (kitty / external / none)."""
    import subprocess

    cfg = get_config()["display"]
    viewer = cfg["image_viewer"]

    if viewer == "kitty":
        if is_kitty_supported():
            display_kitty_image(path)
        else:
            # Fallback: external viewer
            viewer = "external"

    if viewer == "external":
        cmd = cfg["external_viewer_cmd"]
        subprocess.Popen([cmd, path])


# ---------------------------------------------------------------------------
# Webcam capture
# ---------------------------------------------------------------------------

def capture_webcam(device: int = 0) -> str | None:
    """Open webcam, show live preview, let user capture a frame.

    Runs synchronously (call from a background thread or suspended TUI).
    Returns path to saved JPEG, or None if cancelled.
    """
    import tempfile
    try:
        import cv2
    except ImportError:
        print("\n[Error] opencv not available. Install cv2 to use webcam capture.\n")
        input("Press Enter to continue...")
        return None

    cap = cv2.VideoCapture(device)
    if not cap.isOpened():
        print(f"\n[Error] Cannot open webcam device {device}.\n")
        input("Press Enter to continue...")
        return None

    print("\n  Webcam preview — SPACE/Enter: capture | q/Esc: cancel\n")
    captured = None
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        display = frame.copy()
        cv2.putText(
            display,
            "SPACE/Enter: capture | q/Esc: cancel",
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 0),
            2,
        )
        cv2.imshow("HomeBox — Capture Photo", display)
        key = cv2.waitKey(30) & 0xFF
        if key in (32, 13):  # space or enter
            captured = frame
            break
        elif key in (ord("q"), 27):  # q or escape
            break

    cap.release()
    cv2.destroyAllWindows()

    if captured is None:
        return None

    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    cv2.imwrite(tmp.name, captured)
    tmp.close()
    return tmp.name


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
