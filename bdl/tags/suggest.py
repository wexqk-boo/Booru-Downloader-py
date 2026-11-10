"""Tag autocomplete.

Hybrid: a local dictionary (TAG_LIBRARY + slang + popular tags) with an
optional board API call (moebooru /tag.json?name=), falling back to the local
index when the network or API stays silent.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from ..filters import canonical
from .catalog import ALIASES, RU_SLANG, TAG_LIBRARY

LOG = logging.getLogger(__name__)

# Local index: canonical tags, deduplicated.
_LOCAL_INDEX: frozenset[str] = frozenset(
    {canonical(tag) for tag in ALIASES.values()}
    | {canonical(tag) for tag in RU_SLANG.values()}
    | {canonical(tag) for group in TAG_LIBRARY.values() for tag in group.values()}
)

_MOEBoorU_TAG_API = "https://yande.re/tag.json?name={prefix}&limit=10"

_session: aiohttp.ClientSession | None = None


async def get_session() -> aiohttp.ClientSession:
    """Shared ClientSession for autocomplete (connection pooling)."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4))
    return _session


async def close_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def _local_hints(prefix: str, limit: int = 10) -> list[str]:
    prefix = canonical(prefix)
    if not prefix:
        return []
    return sorted(tag for tag in _LOCAL_INDEX if tag.startswith(prefix))[:limit]


async def _remote_hints(prefix: str, limit: int = 10) -> list[str]:
    """Query moebooru tag.json (yande.re); works without auth."""
    try:
        session = await get_session()
        async with session.get(
            _MOEBoorU_TAG_API.format(prefix=prefix), headers={"User-Agent": "booru-bot"}
        ) as response:
            if response.status != 200:
                return []
            data = await response.json()
        return [item.get("name", "") for item in data if item.get("name")][:limit]
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        LOG.debug("remote autocomplete unavailable: %s", exc)
        return []


def _fuzzy_fix_blocking(tags: str, cutoff: float = 0.6) -> str:
    """Synchronous part of fuzzy_fix (call via asyncio.to_thread)."""
    out: list[str] = []
    try:
        from rapidfuzz import fuzz as rf_fuzz
        from rapidfuzz import process as rf_process

        _HAS_RAPIDFUZZ = True
    except ImportError:
        _HAS_RAPIDFUZZ = False

    for token in tags.split():
        if token.startswith("-"):
            out.append(token)
            continue
        if _HAS_RAPIDFUZZ:
            match = rf_process.extractOne(token, _LOCAL_INDEX, scorer=rf_fuzz.ratio, score_cutoff=cutoff * 100)
            out.append(match[0] if match else token)
            continue
        # Fallback: difflib.
        from difflib import SequenceMatcher

        best, best_ratio = token, cutoff
        for cand in _LOCAL_INDEX:
            ratio = SequenceMatcher(None, token, cand).ratio()
            if ratio > best_ratio:
                best, best_ratio = cand, ratio
        out.append(best)
    return " ".join(out)


async def fuzzy_fix(tags: str, cutoff: float = 0.6) -> str:
    """Fix typos; runs in a worker thread so it never stalls the event loop."""
    return await asyncio.to_thread(_fuzzy_fix_blocking, tags, cutoff)


async def suggest_tags(prefix: str, limit: int = 10) -> list[str]:
    """Return suggestions: local plus remote, deduplicated."""
    prefix = prefix.strip().lower().lstrip("#")
    if not prefix:
        return []
    local = _local_hints(prefix, limit)
    if len(local) >= limit:
        return local[:limit]
    remote = await _remote_hints(prefix, limit)
    merged: list[str] = []
    seen: set[str] = set()
    for tag in [*local, *remote]:
        if tag not in seen:
            seen.add(tag)
            merged.append(tag)
    return merged[:limit]


def prewarm_autocomplete() -> None:
    """Warm the cache; calling this at startup is optional."""
    return
