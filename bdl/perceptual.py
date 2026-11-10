"""Perceptual hashing, for spotting the same artwork on two boards.

The same file is reposted across boards under different post ids, so neither
the uid nor the sha256 catches it: the bytes differ (re-encode, resize, strip
metadata) while the picture does not. A 64-bit pHash survives those edits, so a
small Hamming distance means "this is the same art".
"""

from __future__ import annotations

import logging
from pathlib import Path

LOG = logging.getLogger(__name__)

#: Two hashes this close are the same picture. 8 bits out of 64 tolerates a
#: re-encode or a modest resize while staying well clear of unrelated images.
DUPLICATE_DISTANCE = 8


def phash_of_file(path: Path) -> str | None:
    """The pHash of an image as a hex string, or None when it cannot be read.

    Videos, corrupt downloads and formats Pillow cannot open simply return
    None: dedup is an optimisation, never a reason to fail a download.
    """
    try:
        import imagehash
        from PIL import Image

        with Image.open(path) as image:
            image.load()
            return str(imagehash.phash(image))
    except Exception as exc:  # a decoder failure only means "no hash"
        LOG.debug("no perceptual hash for %s: %s", path, exc)
        return None


def distance(left: str, right: str) -> int | None:
    """Hamming distance between two hex pHashes, or None if either is unusable."""
    try:
        return bin(int(left, 16) ^ int(right, 16)).count("1")
    except (TypeError, ValueError):
        return None


def is_duplicate(left: str, right: str, limit: int = DUPLICATE_DISTANCE) -> bool:
    """Whether two hashes describe the same picture."""
    gap = distance(left, right)
    return gap is not None and gap <= limit
