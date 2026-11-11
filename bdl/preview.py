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

from .stdio import restore_blocking

LOG = logging.getLogger(__name__)

#: Rows of text one row of thumbnails is allowed to occupy.
PREVIEW_ROWS = 8

#: Thumbnails side by side in a row. Three keeps each tile readable without
#: pushing the strip past a normal terminal width.
PER_ROW = 3

#: Rows of thumbnails in the gallery. A batch may download a hundred pictures;
#: the gallery stays a small block at the bottom instead of a full-screen roll.
MAX_TILE_ROWS = 2

#: Pixels a single tile is scaled into, and the gap between tiles.
TILE_WIDTH = 208
_CELL_PIXELS = 16
_TILE_GAP = 8
_BACKGROUND = (0, 0, 0, 0)

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


def _thumbnail(path: Path, rows: int) -> bytes | None:
    """Return PNG bytes for one tile, or None when it cannot be built."""
    try:
        from PIL import Image
    except ImportError:
        LOG.debug("Pillow is missing; skipping preview")
        return None
    try:
        image = Image.open(path)
        image.load()
        # Fit the picture inside one tile's box instead of stretching it to
        # fill: previews exist to recognise a picture, and a squashed one is
        # harder to recognise than a small one.
        box_height = max(1, rows * _CELL_PIXELS)
        scale = min(TILE_WIDTH / image.width, box_height / image.height)
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        image = image.convert("RGBA").resize(size, Image.LANCZOS)
        if image.width <= 1 or image.height <= 1:
            return None
        from io import BytesIO

        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except (OSError, ValueError) as exc:
        LOG.debug("could not build a preview for %s: %s", path, exc)
        return None


def _strip(paths: list[Path], rows: int) -> tuple[bytes, int, int] | None:
    """Compose a row of thumbnails into one PNG.

    Returns the PNG bytes together with its pixel size, which the caller needs
    to work out how many cells the row should occupy. One combined image means
    one graphics payload for the whole row: sending a separate escape sequence
    per tile makes Kitty lay them out in a column and scroll the terminal,
    which is exactly what a preview should not do.
    """
    if not paths:
        return None
    try:
        from PIL import Image
    except ImportError:
        LOG.debug("Pillow is missing; skipping preview")
        return None

    tiles: list[Image.Image] = []
    for path in paths:
        payload = _thumbnail(path, rows)
        if payload is None:
            continue
        from io import BytesIO

        tiles.append(Image.open(BytesIO(payload)))
    if not tiles:
        return None

    height = max(tile.height for tile in tiles)
    width = sum(tile.width for tile in tiles) + _TILE_GAP * (len(tiles) - 1)
    strip = Image.new("RGBA", (width, height), _BACKGROUND)
    offset = 0
    for tile in tiles:
        # Bottom-align the tiles so a short picture does not float.
        strip.paste(tile, (offset, height - tile.height), tile)
        offset += tile.width + _TILE_GAP
    from io import BytesIO

    buffer = BytesIO()
    strip.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), strip.width, strip.height


def show_many(paths: list[Path], rows: int = PREVIEW_ROWS, per_row: int = PER_ROW) -> int:
    """Draw *paths* as a compact gallery of thumbnail rows.

    At most ``MAX_TILE_ROWS`` rows are drawn, so a large batch leaves a neat
    block at the bottom of the screen instead of a column running off it.
    Returns how many rows were drawn.
    """
    if not available():
        return 0
    restore_blocking()
    drawn = 0
    for start in range(0, len(paths), per_row):
        if drawn >= MAX_TILE_ROWS:
            break
        group = [path for path in paths[start : start + per_row] if path.is_file()]
        composed = _strip(group, rows)
        if composed is None:
            continue
        payload, width, height = composed
        columns = columns_for(width, height, rows)
        if _transmit(payload, rows, columns):
            drawn += 1
            continue
        # The protocol was refused (an older Kitty, or a multiplexer in
        # between); the icat kitten exists for exactly that case.
        if _icat(payload, rows, columns):
            drawn += 1
    # A few hundred kilobytes of escape codes is exactly the write that lands
    # on a non-blocking stream half-way through.
    restore_blocking()
    return drawn


def show(path: Path, rows: int = PREVIEW_ROWS) -> bool:
    """Draw *path* as a single thumbnail. Returns True when something was drawn."""
    return show_many([path], rows) > 0


def _transmit(payload: bytes, rows: int, columns: int) -> bool:
    """Send PNG bytes straight to the terminal with the graphics protocol.

    ``f=100`` means the data is PNG and ``a=T`` places it at the cursor, so
    nothing has to be deleted afterwards. The placement box is expressed in
    cells; Kitty fits the picture inside it without distorting it.
    """
    encoded = base64.b64encode(payload).decode("ascii")
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


def _icat(payload: bytes, rows: int, columns: int) -> bool:
    """Older Kitty builds may still be reached through the icat kitten."""
    kitten = shutil.which("kitty")
    if kitten is None:
        return False
    import subprocess
    import tempfile

    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            handle.write(payload)
            temporary = handle.name
        try:
            subprocess.run(
                [kitten, "+kitten", "icat", "--place", f"{columns}x{rows}", temporary],
                check=False,
                stdout=sys.stdout,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        finally:
            Path(temporary).unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        LOG.debug("icat preview failed: %s", exc)
        return False
    sys.stdout.write("\n")
    return True


def columns_for(width: int, height: int, rows: int) -> int:
    """Cells a picture of *width*×*height* pixels needs to occupy *rows* rows.

    A terminal cell is about twice as tall as it is wide, so if the picture is
    given *rows* rows it has ``height / rows`` pixels per row, and a column is
    half of that. Getting this right is what keeps the tile from being scaled
    down into a corner of an oversized box.
    """
    if width <= 0 or height <= 0 or rows <= 0:
        return 1
    return max(1, min(200, round(width * rows * 2 / height)))
