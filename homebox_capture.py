#!/usr/bin/env python3
"""Standalone webcam capture helper — run as a subprocess.

Usage: python homebox_capture.py <device_index> <result_dir>

Captures frames from the webcam with live kitty-protocol preview.
Loops until user presses 'q' — each Enter/Space saves a JPEG to *result_dir*.
"""

import os
import sys
import select
import tempfile
import termios
import tty
import base64
import io

# Use /dev/tty for output to ensure it reaches the terminal even when
# stdout might be captured by a parent TUI framework.
try:
    _TTY_FD = os.open("/dev/tty", os.O_WRONLY)
except OSError:
    _TTY_FD = 1  # fallback to stdout


def _tty_write(data: bytes) -> None:
    os.write(_TTY_FD, data)


def _tty_print(msg: str) -> None:
    """Print to /dev/tty so it's always visible."""
    _tty_write((msg + "\n").encode())

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
_IMG_START_ROW = 5  # row where the kitty image is placed


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
    # Move cursor to image area
    out += _wrap(f"\033[{_IMG_START_ROW};1H".encode())
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
    _tty_write(bytes(out))


def _kitty_clear() -> None:
    """Remove the preview image."""
    try:
        _tty_write(_wrap(f"\033_Ga=d,d=i,i={_PREVIEW_ID},q=2;\033\\".encode()))
    except Exception:
        pass


def _draw_header(count: int) -> None:
    """Draw the status header at the top of the screen."""
    _tty_write(_wrap(b"\033[H\033[2J"))  # home + clear screen
    _tty_print("")
    _tty_print("  Live webcam — Enter/Space: capture | q: done")
    if count > 0:
        _tty_print(f"  Photos captured so far: {count}")
    _tty_print("")


# ---------------------------------------------------------------------------
# Main capture loop
# ---------------------------------------------------------------------------

def main() -> None:
    device = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    result_dir = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        import cv2
    except ImportError:
        _tty_print("\r\n  [Error] opencv not available.\r\n")
        return

    cap = cv2.VideoCapture(device)
    if not cap.isOpened():
        _tty_print(f"\r\n  [Error] Cannot open webcam device {device}.\r\n")
        return

    # Open /dev/tty for reading too — stdin may be redirected by parent TUI
    try:
        tty_in_fd = os.open("/dev/tty", os.O_RDONLY)
    except OSError:
        tty_in_fd = sys.stdin.fileno()
    tty_in = os.fdopen(tty_in_fd, "rb", buffering=0, closefd=False)

    old_settings = termios.tcgetattr(tty_in_fd)
    tty.setcbreak(tty_in_fd)

    captured_paths: list[str] = []
    _draw_header(0)

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                _tty_print("\r\n  [Error] Lost webcam feed.\r\n")
                break

            if _KITTY:
                _, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
                _kitty_show(jpg.tobytes())

            # Non-blocking key check (~3 FPS)
            if select.select([tty_in], [], [], 0.3)[0]:
                key = os.read(tty_in_fd, 1)
                if key == b"\x1b":
                    # Drain escape sequence (mouse events etc.)
                    while select.select([tty_in], [], [], 0.01)[0]:
                        os.read(tty_in_fd, 64)
                    continue
                if key in (b"\n", b"\r", b" "):
                    # Save this frame
                    if result_dir:
                        tmp = tempfile.NamedTemporaryFile(
                            suffix=".jpg", delete=False, dir=result_dir
                        )
                        cv2.imwrite(tmp.name, frame)
                        tmp.close()
                        captured_paths.append(tmp.name)
                    _draw_header(len(captured_paths))
                    continue  # keep capturing
                elif key == b"q":
                    break
    finally:
        termios.tcsetattr(tty_in_fd, termios.TCSADRAIN, old_settings)
        if _KITTY:
            _kitty_clear()
        termios.tcflush(tty_in_fd, termios.TCIFLUSH)

    cap.release()

    if captured_paths:
        _tty_print(f"\r\n  ✓ {len(captured_paths)} photo(s) captured!\r\n")
    else:
        _tty_print("\r\n  No photos captured.\r\n")


if __name__ == "__main__":
    main()
