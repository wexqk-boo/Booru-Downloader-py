"""Thumbnails for downloaded pictures, using the Kitty graphics protocol.

Only Kitty and a few compatible terminals can show pixels inside a terminal
cell grid, so everything here is a no-op elsewhere. A failure to draw must
never disturb a running batch: the gallery is a nicety, the library is not.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import sys
from pathlib import Path

LOG = logging.getLogger(__name__)

#: Rows of text a thumbnail is allowed to occupy.
PREVIEW_ROWS = 12

#: Escape sequence size limit; images are resized down before they get here.
_CHUNK = 4096


def in_kitty(env: dict[str, str] | None = None) -> bool:
    """True when the current terminal speaks the Kitty graphics protocol.

    ``KITTY_WINDOW_ID`` is the reliable signal; the terminfo name is kept as a
    fallback for terminals that impersonate Kitty over ssh.
    """
    environ = os.environ if env is None else env
    if environ.get("KITTY_WINDOW_ID"):
        return True
    return environ.get("TERM", "") == "xterm-kitty"


def available() -> bool:
    """True when a preview can actually be drawn right now.

    Only Kitty can render the graphics protocol, and the icat fallback is
    Kitty's too, so anything else is skipped rather than emitting escape
    sequences the terminal would print as garbage.
    """
    return sys.stdout.isatty() and in_kitty()


def _resize(path: Path, rows: int) -> bytes | None:
    """Return PNG bytes for a thumbnail, or None when it cannot be built."""
    try:
        from PIL import Image
    except ImportError:
        LOG.debug("Pillow is missing; skipping preview")
        return None
    try:
        image = Image.open(path)
        image.load()
        # Terminal cells are about twice as tall as they are wide.
        height = max(1, rows * 16)
        width = max(1, round(image.width * height / image.height))
        image = image.convert("RGBA").resize((min(width, 1200), height), Image.LANCZOS)
        if image.width <= 1 or image.height <= 1:
            return None
        from io import BytesIO

        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except (OSError, ValueError) as exc:
        LOG.debug("could not build a preview for %s: %s", path, exc)
        return None


def show(path: Path, rows: int = PREVIEW_ROWS) -> bool:
    """Draw *path* as a thumbnail. Returns True when something was drawn."""
    if not available() or not path.is_file():
        return False
    payload = _resize(path, rows)
    if not payload:
        return False
    if _transmit(payload, rows):
        return True
    # The protocol was refused (an older Kitty, or a multiplexer in between);
    # the icat kitten is the fallback for exactly that case.
    return _icat(path, rows)


def _transmit(payload: bytes, rows: int) -> bool:
    """Send PNG bytes straight to the terminal with the graphics protocol.

    ``f=100`` means the data is PNG and ``a=T`` places it at the cursor in
    ``c`` columns by ``r`` rows, so nothing has to be deleted afterwards.
    """
    encoded = base64.b64encode(payload).decode("ascii")
    columns = max(4, min(40, round(rows * 2.4)))
    out = sys.stdout
    try:
        for index in range(0, len(encoded), _CHUNK):
            chunk = encoded[index : index + _CHUNK]
            more = 1 if index + _CHUNK < len(encoded) else 0
            if index == 0:
                out.write(f"\x1b_Ga=T,f=100,c={columns},r={rows},m={more};{chunk}\x1b\\")
            else:
                out.write(f"\x1b_Gm={more};{chunk}\x1b\\")
        out.write("\n")
        out.flush()
    except (OSError, ValueError) as exc:
        LOG.debug("preview transmit failed: %s", exc)
        return False
    return True


def _icat(path: Path, rows: int) -> bool:
    """Older Kitty builds may still be reached through the icat kitten."""
    kitten = shutil.which("kitty")
    if kitten is None:
        return False
    import subprocess

    columns = max(4, min(40, round(rows * 2.4)))
    try:
        subprocess.run(
            [kitten, "+kitten", "icat", "--place", f"{columns}x{rows}", str(path)],
            check=False,
            stdout=sys.stdout,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.debug("icat preview failed: %s", exc)
        return False
    return True
