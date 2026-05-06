#!/usr/bin/env python3
"""Standalone webcam capture helper — run as a subprocess.

Usage: python homebox_capture.py <device_index> <result_dir>

Captures frames from the webcam with live kitty-protocol preview.
Loops until user presses 'q' — each Enter/Space saves a JPEG to *result_dir*.
"""

import os
import sys
import select
import time
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
    _tty_write((msg + "\n").encode())


# ---------------------------------------------------------------------------
# Kitty helpers — send JPEG directly (no PIL), much lower latency
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


_PREVIEW_W = 320
_PREVIEW_H = 240


def _kitty_show(frame) -> None:
    """Send a video frame via kitty protocol as PNG (f=100).

    Uses OpenCV for resize + PNG encode at compression level 1 (fast).
    320×240 PNG level-1 ≈ 30–60 KB — small enough for snappy terminal transfer.
    """
    import cv2

    # Resize with OpenCV (fast bilinear)
    h, w = frame.shape[:2]
    scale = min(_PREVIEW_W / w, _PREVIEW_H / h, 1.0)
    sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
    small = cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_LINEAR)

    # Encode as PNG at compression level 1 (fastest, ~2–4× smaller than raw)
    _, png = cv2.imencode(".png", small, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    b64 = base64.standard_b64encode(png.tobytes()).decode()
    chunks = [b64[i:i + 4096] for i in range(0, len(b64), 4096)]

    out = bytearray()
    # Delete old preview
    out += _wrap(f"\033_Ga=d,d=i,i={_PREVIEW_ID},q=2;\033\\".encode())
    # Move cursor to image area
    out += _wrap(f"\033[{_IMG_START_ROW};1H".encode())
    # Transmit + place: f=100 = PNG (dimensions embedded in PNG header)
    kitty_buf = bytearray()
    for i, chunk in enumerate(chunks):
        more = 0 if i == len(chunks) - 1 else 1
        if i == 0:
            kitty_buf += (
                f"\033_Ga=T,f=100,q=2,i={_PREVIEW_ID},m={more};{chunk}\033\\"
            ).encode()
        else:
            kitty_buf += f"\033_Gm={more};{chunk}\033\\".encode()
    out += _wrap(bytes(kitty_buf))
    _tty_write(bytes(out))


def _kitty_clear() -> None:
    try:
        _tty_write(_wrap(f"\033_Ga=d,d=i,i={_PREVIEW_ID},q=2;\033\\".encode()))
    except Exception:
        pass


def _draw_header(count: int) -> None:
    _tty_write(_wrap(b"\033[H\033[2J"))  # home + clear screen
    _tty_print("")
    _tty_print("  Live webcam — Enter/Space: capture | q: done")
    if count > 0:
        _tty_print(f"  Photos captured so far: {count}")
    _tty_print("")


# ---------------------------------------------------------------------------
# Main capture loop
# ---------------------------------------------------------------------------

FRAME_INTERVAL = 1.0   # seconds between preview updates (1 FPS)
KEY_POLL       = 0.05  # seconds between key checks (20 polls/sec → ≤50ms latency)


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
    # Minimize internal buffer so cap.read() always returns the latest frame
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    # Open /dev/tty for reading — stdin may be redirected by parent TUI
    try:
        tty_in_fd = os.open("/dev/tty", os.O_RDONLY)
    except OSError:
        tty_in_fd = sys.stdin.fileno()
    tty_in = os.fdopen(tty_in_fd, "rb", buffering=0, closefd=False)

    old_settings = termios.tcgetattr(tty_in_fd)
    tty.setcbreak(tty_in_fd)

    captured_paths: list[str] = []
    _draw_header(0)

    # Pre-read the webcam to warm it up (first frame is often slow)
    cap.read()

    last_frame_time = 0.0

    try:
        while True:
            # --- Key check (fast, non-blocking) ---
            if select.select([tty_in], [], [], KEY_POLL)[0]:
                key = os.read(tty_in_fd, 1)
                if key == b"\x1b":
                    while select.select([tty_in], [], [], 0.01)[0]:
                        os.read(tty_in_fd, 64)
                    continue
                if key in (b"\n", b"\r", b" "):
                    # Capture current frame immediately
                    ret, frame = cap.read()
                    if ret and result_dir:
                        tmp = tempfile.NamedTemporaryFile(
                            suffix=".jpg", delete=False, dir=result_dir
                        )
                        cv2.imwrite(tmp.name, frame)
                        tmp.close()
                        captured_paths.append(tmp.name)
                    _draw_header(len(captured_paths))
                    last_frame_time = 0.0  # force preview refresh
                    continue
                elif key == b"q":
                    break

            # --- Frame update (throttled to FRAME_INTERVAL) ---
            now = time.monotonic()
            if now - last_frame_time >= FRAME_INTERVAL:
                ret, frame = cap.read()
                if not ret:
                    _tty_print("\r\n  [Error] Lost webcam feed.\r\n")
                    break
                if _KITTY:
                    _kitty_show(frame)
                last_frame_time = now

    finally:
        termios.tcsetattr(tty_in_fd, termios.TCSADRAIN, old_settings)
        if _KITTY:
            _kitty_clear()
        termios.tcflush(tty_in_fd, termios.TCIFLUSH)
        # Release in a daemon thread — cap.release() can block 2–4s on USB webcams.
        # The subprocess exits right after, so the OS closes the fd anyway.
        import threading
        threading.Thread(target=cap.release, daemon=True).start()

    if captured_paths:
        _tty_print(f"\r\n  ✓ {len(captured_paths)} photo(s) captured!\r\n")
    else:
        _tty_print("\r\n  No photos captured.\r\n")


if __name__ == "__main__":
    main()
