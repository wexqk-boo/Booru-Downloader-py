"""Tag suggestions: local dictionary + slang + aliases + remote fallback.

Combines hints from the local catalog (``.catalog``: dictionary, slang,
aliases) with a live lookup: moebooru ``/tag.json`` (yande.re), falling back
to prefix search on the board API.
An empty fragment returns a random sample of popular tags per category.
"""

from __future__ import annotations

import random

from ..filters import canonical
from .catalog import ALIASES, RU_SLANG, TAG_LIBRARY

_LOCAL_ALL: frozenset[str] = frozenset(
    set(ALIASES.values()) | set(RU_SLANG.values()) | {tag for group in TAG_LIBRARY.values() for tag in group.values()}
)
_CATEGORIES: tuple[tuple[str, ...], ...] = tuple(tuple(group.values()) for group in TAG_LIBRARY.values() if group)


def local_hints(prefix: str, limit: int = 10) -> list[str]:
    """Exact hints from the local dictionary and the synced Danbooru database."""
    p = canonical(prefix).strip().lstrip("#")
    if not p:
        return []
    hints = sorted(tag for tag in _LOCAL_ALL if tag.startswith(p))
    if len(hints) >= limit:
        return hints[:limit]
    # Slang/aliases are keyed by the trigger; expose the canonical value.
    extra = sorted(value for key, value in {**ALIASES, **RU_SLANG}.items() if key.startswith(p))
    merged: list[str] = []
    seen: set[str] = set()
    for tag in [*hints, *extra]:
        if tag not in seen:
            seen.add(tag)
            merged.append(tag)
    return merged[:limit]


async def local_hints_async(prefix: str, limit: int = 10) -> list[str]:
    """Exact hints, including the async Danbooru database."""
    merged = local_hints(prefix, limit)
    p = canonical(prefix).strip().lstrip("#")
    if not p or len(merged) >= limit:
        return merged
    try:
        from .store import db_hints, load_db

        data = await load_db()
        if data is not None:
            seen = set(merged)
            for tag in db_hints(data, p, limit):
                if tag not in seen:
                    seen.add(tag)
                    merged.append(tag)
                if len(merged) >= limit:
                    break
    except (OSError, RuntimeError):
        pass
    return merged[:limit]


async def popular_tags(limit: int = 25) -> list[str]:
    """Random sample: Danbooru database (weighted) or the local dictionary."""
    try:
        from .store import db_popular, load_db

        data = await load_db()
        if data is not None:
            return db_popular(data, limit)
    except (OSError, RuntimeError):
        pass
    picks: list[str] = []
    for group in _CATEGORIES:
        tag = random.choice(group)
        if tag not in picks:
            picks.append(tag)
    need = limit - len(picks)
    if need > 0:
        rest = [tag for tag in _LOCAL_ALL if tag not in picks]
        picks.extend(random.sample(rest, min(need, len(rest))))
    return picks[:limit]
