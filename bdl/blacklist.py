"""Tag blacklist: the built-in list, the user's saved list, and helpers.

Posts matching any excluded tag are skipped during download. Two lists exist,
and keeping them apart is the point:

* the **built-in** list is what the program ships with (generator tags, snuff,
  and the like). It is a Python constant, so it cannot be edited into a bad
  state and updates with the program.
* the **user's** list is what the person added, stored in
  ``~/.config/bdl/blacklist.json`` as ``{"tags": [...]}``. Only this one is
  ever written to, so a program update can never delete the user's choices and
  a user can never accidentally break the built-in protection.

The file is JSON, and written atomically: a crash mid-write must not leave a
half-written list that silently drops half the exclusions.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .config import xdg_config_home
from .filters import AI_ART_TAGS
from .tags.families import expand_blacklist_tags

#: Exclusions that ship with bdl. Kept as data so they can be listed and
#: reported separately from the user's own entries.
BUILTIN_TAGS: frozenset[str] = frozenset(AI_ART_TAGS)


def default_blacklist_path() -> Path:
    """The user's saved list. JSON since the built-in list was split out."""
    return xdg_config_home() / "bdl" / "blacklist.json"


def legacy_blacklist_path() -> Path:
    """Where the list used to live, read once so nobody loses their tags."""
    return xdg_config_home() / "bdl" / "blacklist.txt"


def _clean(raw: list[object]) -> set[str]:
    """Normalise a stored list and drop anything that is actually built in."""
    return {
        str(tag).strip().lower()
        for tag in raw
        if str(tag).strip() and str(tag).strip().lower() not in BUILTIN_TAGS
    }


def load_user_blacklist(path: Path | str | None = None) -> set[str]:
    """The user's saved tags; a missing or broken file yields an empty set.

    Built-in tags are filtered out on the way in as well as on the way out, so
    a file written by an older version cannot make them look user-owned: they
    are never counted as the user's entries and ``clear`` never reports
    removing them.
    """
    file_path = Path(path).expanduser() if path else default_blacklist_path()
    if file_path.is_file():
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            raw = payload.get("tags")
            if isinstance(raw, list):
                return _clean(raw)
        elif isinstance(payload, list):  # a hand-written bare array
            return _clean(payload)
        # An unreadable file falls through to the legacy format below.

    # Migrate a pre-JSON list rather than silently ignoring it.
    legacy = legacy_blacklist_path()
    if file_path == default_blacklist_path() and legacy.is_file():
        try:
            tags = {
                line.strip().lower()
                for line in legacy.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            }
        except OSError:
            return set()
        tags = {tag for tag in tags if tag not in BUILTIN_TAGS}
        if tags:
            save_user_blacklist(tags, file_path)
        return tags
    return set()


def save_user_blacklist(tags: set[str], path: Path | str | None = None) -> Path:
    """Write the sorted tags atomically and return the file path.

    The write goes to a temporary file in the same directory and is then moved
    into place, so a reader never sees a partial document and an interrupted
    write cannot destroy the previous list.
    """
    file_path = Path(path).expanduser() if path else default_blacklist_path()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"tags": sorted(tags)}, ensure_ascii=False, indent=2) + "\n"
    handle, tmp_name = tempfile.mkstemp(dir=str(file_path.parent), prefix=".blacklist-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, file_path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return file_path


def load_blacklist(path: Path | str | None = None) -> set[str]:
    """Everything to exclude: the built-in list plus the user's saved tags.

    This is what the downloader is handed, so a caller that only wants the
    user's own entries should use :func:`load_user_blacklist` instead.
    """
    return set(BUILTIN_TAGS) | load_user_blacklist(path)


def save_blacklist(tags: set[str], path: Path | str | None = None) -> Path:
    """Save the user's list, minus anything that is actually built in.

    The built-in tags are filtered out on the way in: they need no storage, and
    writing them would freeze them at the version that saved them.
    """
    return save_user_blacklist({tag for tag in tags if tag not in BUILTIN_TAGS}, path)


def expand_tag(tag: str) -> set[str]:
    """Tag family: the tag itself plus related sub-tags (ai/furry/guro/...).

    Anything that is already built in is dropped: those tags are excluded
    regardless, and counting them as the user's entries would make ``add`` and
    ``remove`` disagree about how many tags they moved.
    """
    clean = tag.strip().lower()
    if not clean:
        return set()
    expanded = set(expand_blacklist_tags(clean))
    expanded.add(clean)
    return {item for item in expanded if item not in BUILTIN_TAGS}


def add_tag(tag: str, path: Path | str | None = None) -> int:
    """Add a tag and its family; return how many entries were added."""
    expanded = expand_tag(tag)
    if not expanded:
        return 0
    tags = load_user_blacklist(path)
    before = len(tags)
    tags |= expanded
    if len(tags) != before:
        save_user_blacklist(tags, path)
    return len(tags) - before


def add_tags(tags: list[str], path: Path | str | None = None) -> tuple[int, list[str]]:
    """Add several tags at once; return how many were new and which they were."""
    existing = load_user_blacklist(path)
    before = set(existing)
    added: list[str] = []
    for tag in tags:
        expanded = expand_tag(tag)
        new = expanded - existing
        if new:
            added.append(tag.strip().lower())
        existing |= expanded
    if existing != before:
        save_user_blacklist(existing, path)
    return len(existing) - len(before), added


def remove_tag(tag: str, path: Path | str | None = None) -> int:
    """Remove a tag and its family if present; return how many entries were removed."""
    clean = tag.strip().lower()
    tags = load_user_blacklist(path)
    to_remove = expand_tag(clean) & tags
    if not to_remove:
        return 0
    tags -= to_remove
    save_user_blacklist(tags, path)
    return len(to_remove)


def remove_tags(tags: list[str], path: Path | str | None = None) -> tuple[int, list[str]]:
    """Remove several tags at once; return how many went and which were found."""
    existing = load_user_blacklist(path)
    removed: list[str] = []
    for tag in tags:
        to_remove = expand_tag(tag) & existing
        if to_remove:
            removed.append(tag.strip().lower())
            existing -= to_remove
    if removed:
        save_user_blacklist(existing, path)
    return len(removed), removed


def clear_user_blacklist(path: Path | str | None = None) -> int:
    """Forget every user tag. The built-in list is untouched, always."""
    tags = load_user_blacklist(path)
    if tags:
        save_user_blacklist(set(), path)
    return len(tags)


def split_blacklist(tags: set[str]) -> tuple[set[str], set[str]]:
    """Divide a combined set into ``(user tags, built-in tags)``."""
    return {tag for tag in tags if tag not in BUILTIN_TAGS}, {tag for tag in tags if tag in BUILTIN_TAGS}
