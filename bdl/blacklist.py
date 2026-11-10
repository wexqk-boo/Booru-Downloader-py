"""Tag blacklist: persistent file and management helpers.

Default file: ``~/.config/bdl/blacklist.txt`` (one tag per line, ``#`` starts a
comment). Posts matching any of these tags are skipped during download, and a
base tag such as ``ai`` also pulls in its whole family.
"""

from __future__ import annotations

from pathlib import Path

from .config import xdg_config_home
from .tags.families import expand_blacklist_tags


def default_blacklist_path() -> Path:
    return xdg_config_home() / "bdl" / "blacklist.txt"


def load_blacklist(path: Path | str | None = None) -> set[str]:
    """Read the tag list in lowercase; a missing file yields an empty set."""
    file_path = Path(path).expanduser() if path else default_blacklist_path()
    if not file_path.is_file():
        return set()
    tags: set[str] = set()
    for line in file_path.read_text(encoding="utf-8").splitlines():
        tag = line.strip().lower()
        if tag and not tag.startswith("#"):
            tags.add(tag)
    return tags


def save_blacklist(tags: set[str], path: Path | str | None = None) -> Path:
    """Write the sorted tag list and return the file path."""
    file_path = Path(path).expanduser() if path else default_blacklist_path()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{tag}\n" for tag in sorted(tags)]
    file_path.write_text("".join(lines), encoding="utf-8")
    return file_path


def expand_tag(tag: str) -> set[str]:
    """Tag family: the tag itself plus related sub-tags (ai/furry/guro/...)."""
    clean = tag.strip().lower()
    if not clean:
        return set()
    expanded = set(expand_blacklist_tags(clean))
    expanded.add(clean)
    return expanded


def add_tag(tag: str, path: Path | str | None = None) -> int:
    """Add a tag and its family; return how many entries were added."""
    expanded = expand_tag(tag)
    if not expanded:
        return 0
    tags = load_blacklist(path)
    before = len(tags)
    tags |= expanded
    if len(tags) != before:
        save_blacklist(tags, path)
    return len(tags) - before


def remove_tag(tag: str, path: Path | str | None = None) -> int:
    """Remove a tag and its family if present; return how many entries were removed."""
    clean = tag.strip().lower()
    tags = load_blacklist(path)
    to_remove = expand_tag(clean) & tags
    if not to_remove:
        return 0
    tags -= to_remove
    save_blacklist(tags, path)
    return len(to_remove)
