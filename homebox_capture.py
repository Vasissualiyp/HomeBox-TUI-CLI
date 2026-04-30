#!/usr/bin/env python3
"""Standalone webcam capture helper — run as a subprocess.

Usage: python homebox_capture.py <device_index> <result_file>

Captures a frame from the webcam with live kitty-protocol preview.
On success, writes the path to the saved JPEG into *result_file*.
On cancel, does not create *result_file*.
"""

import os
import sys
import select
import tempfile
import termios
import tty
import base64
import io

# ---------------------------------------------------------------------------
# Kitty helpers (self-contained, no imports from homebox_*)
# ---------------------------------------------------------------------------

_IN_TMUX = bool(os.environ.get("TMUX"))
_KITTY = bool(
    os.environ.get("KITTY_WINDOW_ID")
    or os.environ.get("TERM") == "xterm-kitty"
    or os.environ.get("TERM_PROGRAM") in ("WezTerm",)
)
_PREVIEW_ID = 99


def _wrap(data: bytes) -> bytes:
    if not _IN_TMUX:
        return data
    escaped = data.replace(b"\x1b", b"\x1b\x1b")
    return b"\x1bPtmux;" + escaped + b"\x1b\\"


def _kitty_show(frame_bytes: bytes) -> None:
    """Delete previous preview + display new frame via kitty protocol."""
    from PIL import Image

    img = Image.open(io.BytesIO(frame_bytes))
    img.thumbnail((640, 480), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    png = buf.getvalue()
    b64 = base64.standard_b64encode(png).decode()

    out = bytearray()
    # Delete old preview
    out += _wrap(f"\033_Ga=d,d=i,i={_PREVIEW_ID},q=2;\033\\".encode())
    # Transmit + place new preview
    chunks = [b64[i:i + 4096] for i in range(0, len(b64), 4096)]
    kitty_buf = bytearray()
    for i, chunk in enumerate(chunks):
        more = 0 if i == len(chunks) - 1 else 1
        if i == 0:
            kitty_buf += f"\033_Ga=T,f=100,q=2,i={_PREVIEW_ID},m={more};{chunk}\033\\".encode()
        else:
            kitty_buf += f"\033_Gm={more};{chunk}\033\\".encode()
    out += _wrap(bytes(kitty_buf))
    os.write(1, bytes(out))


def _kitty_clear() -> None:
    """Remove the preview image."""
    try:
        os.write(1, _wrap(f"\033_Ga=d,d=i,i={_PREVIEW_ID},q=2;\033\\".encode()))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main capture loop
# ---------------------------------------------------------------------------

def main() -> None:
    device = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    result_file = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        import cv2
    except ImportError:
        print("\r\n  [Error] opencv not available.\r\n")
        return

    cap = cv2.VideoCapture(device)
    if not cap.isOpened():
        print(f"\r\n  [Error] Cannot open webcam device {device}.\r\n")
        return

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    tty.setcbreak(fd)

    print("\r\n  Live webcam — Enter/Space: capture | q: cancel\r\n")
    captured_frame = None
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("\r\n  [Error] Lost webcam feed.\r\n")
                break

            if _KITTY:
                _, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
                _kitty_show(jpg.tobytes())

            # Non-blocking key check (~3 FPS)
            if select.select([sys.stdin], [], [], 0.3)[0]:
                key = os.read(fd, 1)
                if key == b"\x1b":
                    # Drain escape sequence (mouse events etc.)
                    while select.select([sys.stdin], [], [], 0.01)[0]:
                        os.read(fd, 64)
                    continue
                if key in (b"\n", b"\r", b" "):
                    captured_frame = frame
                    break
                elif key == b"q":
                    break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        if _KITTY:
            _kitty_clear()
        termios.tcflush(fd, termios.TCIFLUSH)

    cap.release()

    if captured_frame is not None and result_file:
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        cv2.imwrite(tmp.name, captured_frame)
        tmp.close()
        with open(result_file, "w") as f:
            f.write(tmp.name)


if __name__ == "__main__":
    main()
