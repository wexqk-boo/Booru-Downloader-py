"""Tag autocomplete.

Hybrid: a local dictionary (TAG_LIBRARY + slang + popular tags) with an
optional board API call (moebooru /tag.json?name=), falling back to the local
index when the network or API stays silent.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from .. import USER_AGENT
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
        async with session.get(_MOEBoorU_TAG_API.format(prefix=prefix), headers={"User-Agent": USER_AGENT}) as response:
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


# Popular board tags, loaded once. Big enough to catch real typos, small
# enough that a scan stays instant.
_POPULAR_LIMIT = 20000
_corrections: tuple[int, set[str]] | None = None


def _correction_pool() -> set[str]:
    """Tag names worth correcting towards: the dictionary plus popular tags.

    The board CSV is 170k names, and comparing every typo against all of them
    pulls in obscure tags — ``sakura`` matched ``sakurea`` (197 posts) instead
    of staying put. The popular slice keeps suggestions plausible.
    """
    global _corrections
    pool = {
        canonical(tag)
        for tag in (
            *ALIASES,
            *ALIASES.values(),
            *RU_SLANG,
            *RU_SLANG.values(),
            *(tag for group in TAG_LIBRARY.values() for tag in group.values()),
        )
    }
    try:
        from .store import db_path, parse_csv

        path = db_path()
        if not path.is_file():
            return pool
        key = path.stat().st_mtime_ns
        if _corrections is not None and _corrections[0] == key:
            return pool | _corrections[1]
        data = parse_csv(path)
        ranked = sorted(zip(data.names, data.counts, strict=True), key=lambda pair: pair[1], reverse=True)
        popular = {name for name, _ in ranked[:_POPULAR_LIMIT]}
        _corrections = (key, popular)
        pool |= popular
    except (OSError, ValueError, KeyError) as exc:
        LOG.debug("no tag list for typo correction: %s", exc)
    return pool


def correct_typos(tags: str, cutoff: int = 82) -> tuple[str, list[tuple[str, str]]]:
    """Nudge misspelled tags towards real ones: ``mku`` becomes ``miku``.

    Only tokens that are not tags themselves are touched, so ``1girl`` and
    every bare shorthand stay exactly as typed. Returns the new tag string and
    the substitutions that were made, for the caller to log.
    """
    tokens = tags.split()
    if not tokens:
        return tags, []
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        return tags, []

    pool = _correction_pool()
    fixed: list[str] = []
    changes: list[tuple[str, str]] = []
    for token in tokens:
        negated = token.startswith("-")
        bare = canonical(token.lstrip("-"))
        if not bare or bare in pool:
            fixed.append(token)
            continue
        match = process.extractOne(bare, pool, scorer=fuzz.ratio, score_cutoff=cutoff)
        if match is None or match[0] == bare:
            fixed.append(token)
            continue
        replacement = match[0]
        changes.append((bare, replacement))
        fixed.append("-" + replacement if negated else replacement)
    return " ".join(fixed), changes


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
