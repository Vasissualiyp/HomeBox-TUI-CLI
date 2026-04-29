#!/usr/bin/env python3
"""Test kitty image rendering, with tmux DCS passthrough support."""
import os, base64, io
from PIL import Image

# Create a simple 100x100 red square
img = Image.new("RGB", (100, 100), (255, 0, 0))
buf = io.BytesIO()
img.save(buf, format="PNG")
png_bytes = buf.getvalue()

IN_TMUX = bool(os.environ.get("TMUX"))

def wrap(data: bytes) -> bytes:
    """Wrap in tmux DCS passthrough if inside tmux."""
    if not IN_TMUX:
        return data
    # Double every ESC inside the payload
    escaped = data.replace(b"\x1b", b"\x1b\x1b")
    return b"\x1bPtmux;" + escaped + b"\x1b\\"

b64 = base64.standard_b64encode(png_bytes).decode()

# Transmit + place image (single chunk, small image)
kitty_cmd = f"\033_Ga=T,f=100,q=2;{b64}\033\\".encode()
os.write(1, wrap(kitty_cmd))

print()
print("If you see a red square above, kitty graphics work!")
print(f"IN_TMUX={IN_TMUX}")
print(f"TERM={os.environ.get('TERM')}")
print(f"TERM_PROGRAM={os.environ.get('TERM_PROGRAM')}")
