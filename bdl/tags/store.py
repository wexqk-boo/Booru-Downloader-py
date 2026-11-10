"""Local Danbooru tag database (danbooru.csv from a1111-sd-webui-tagcomplete).

Rows look like ``name,count,category,"aliases"``. The CSV is cached in
``~/.local/share/bdl/danbooru-tags.csv`` and refreshed once per ``REFRESH_TTL``;
without network we fall back to the bundled dictionary.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import pickle
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

import aiohttp

from .. import USER_AGENT
from ..config import xdg_cache_home, xdg_data_home

LOG = logging.getLogger(__name__)

DB_URL = "https://raw.githubusercontent.com/DominikDoom/a1111-sd-webui-tagcomplete/main/tags/danbooru.csv"
REFRESH_TTL = 7 * 24 * 3600  # 7 days
DOWNLOAD_TIMEOUT = 90

CACHE_DIR = xdg_cache_home() / "bdl"

_db_cache: tuple[float, TagData] | None = None


@dataclass(slots=True)
class TagData:
    names: list[str]
    counts: list[int]
    aliases: dict[str, str]


def db_path() -> Path:
    return xdg_data_home() / "bdl" / "danbooru-tags.csv"


async def _download(path: Path, session: aiohttp.ClientSession) -> bool:
    """Download the CSV via aiohttp; True on success."""
    try:
        async with session.get(DB_URL, headers={"User-Agent": USER_AGENT}) as resp:
            if resp.status != 200:
                LOG.warning("Tag database: HTTP %s", resp.status)
                return False

            # Buffer chunks on the event loop, flush to disk from a thread
            # (aiohttp streams cannot be consumed in a worker).
            flush_at = 2 * 1024 * 1024
            buffer = bytearray()
            with open(path, "wb") as fh:
                async for chunk in resp.content.iter_chunked(1 << 16):
                    buffer.extend(chunk)
                    if len(buffer) >= flush_at:
                        await asyncio.to_thread(fh.write, bytes(buffer))
                        buffer.clear()
                if buffer:
                    await asyncio.to_thread(fh.write, bytes(buffer))
        LOG.info("Danbooru tag database downloaded: %s", path)
        return True
    except (TimeoutError, aiohttp.ClientError, OSError) as exc:
        LOG.warning("Failed to download the tag database: %s", exc)
        return False


def parse_csv(path: Path) -> TagData:
    names: list[str] = []
    counts: list[int] = []
    aliases: dict[str, str] = {}
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.reader(fh):
            if not row or not row[0].strip():
                continue
            name = row[0].strip().lower()
            count = 0
            if len(row) > 2 and row[2].strip().lstrip("-").isdigit():
                count = int(row[2])
            names.append(name)
            counts.append(count)
            if len(row) > 3 and row[3]:
                for alias in row[3].split(","):
                    alias = alias.strip().lower()
                    if alias:
                        aliases.setdefault(alias, name)
    return TagData(names=names, counts=counts, aliases=aliases)


async def ensure_db(force: bool = False) -> Path | None:
    """Make sure the database is present and fresh; returns its path or None."""
    path = db_path()
    stale = False
    if path.is_file():
        age = time.time() - path.stat().st_mtime
        stale = age > REFRESH_TTL
    if force or not path.is_file() or stale:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)) as session:
            ok = await _download(tmp, session)
        if ok:
            tmp.replace(path)
        elif not path.is_file():
            return None
        else:
            tmp.unlink(missing_ok=True)
    return path if path.is_file() else None


async def load_db(refresh: bool = False) -> TagData | None:
    """Load the database (cached); None if unavailable."""
    global _db_cache
    now = time.monotonic()
    if _db_cache is not None and now - _db_cache[0] < 60 and not refresh:
        return _db_cache[1]
    path = await ensure_db(force=refresh) if refresh else await ensure_db()
    if path is None:
        return None
    data = await _load_or_dump(path)
    _db_cache = (now, data)
    return data


def _pickle_path(path: Path) -> Path:
    return path.with_suffix(".pkl")


def _dump_data(path: Path) -> TagData | None:
    """Parse the CSV and write the binary cache next to it; returns TagData."""
    data = parse_csv(path)
    try:
        with open(_pickle_path(path), "wb") as fh:
            pickle.dump(data, fh, protocol=4)
    except OSError:
        pass
    return data


async def _load_or_dump(path: Path) -> TagData | None:
    """Load from .pkl when the CSV is unchanged, otherwise parse and re-dump."""
    pkl = _pickle_path(path)
    try:
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        if pkl.is_file():
            with open(pkl, "rb") as fh:
                cached = pickle.load(fh)
            # Migration: older caches stored a bare TagData.
            if isinstance(cached, tuple) and len(cached) == 2:
                cached_key, data = cached
            else:
                cached_key, data = None, cached
            if cached_key == key and isinstance(data, TagData):
                return data
    except (OSError, EOFError, pickle.UnpicklingError, ValueError, AttributeError, ImportError):
        # ImportError/AttributeError mean the cache was written by an older
        # release whose module paths no longer exist; re-parsing is cheap
        # compared to asking the user to clear it by hand.
        pass
    return await asyncio.to_thread(_dump_data, path)


def db_hints(data: TagData, prefix: str, limit: int = 10) -> list[str]:
    """Name and alias matches, ordered by popularity."""
    if not prefix:
        return []
    seen: dict[str, int] = {}
    for name in data.names:
        if name.startswith(prefix) and name not in seen:
            seen[name] = 0
    for alias, name in data.aliases.items():
        if alias.startswith(prefix) and name not in seen:
            seen[name] = 0
    if not seen:
        return []
    # Popularity comes from counts[name]; missing entries rank last.
    count_index = {name: count for name, count in zip(data.names, data.counts, strict=True)}
    ranked = sorted(seen, key=lambda name: count_index.get(name, 0), reverse=True)
    return ranked[:limit]


def db_popular(data: TagData, limit: int = 25) -> list[str]:
    """Random sample weighted by popularity (post count).

    Always returns exactly ``limit`` unique tags: a sample without replacement
    drawn from the most popular candidates.
    """
    import random

    if not data.names:
        return []
    limit = min(limit, len(data.names))
    # Descending by count, sampled from a wider pool than the limit.
    ordered = sorted(zip(data.names, data.counts, strict=True), key=lambda pair: pair[1], reverse=True)
    top = [name for name, _ in ordered]
    return random.sample(top, limit)


# --- Native board tag lists (live prefix search) ---
# {prefix} is substituted from user input. Gelbooru-family boards use a
# percent wildcard and answer with dapi XML.
SITE_TAG_API: dict[str, str] = {
    "rule34": "https://api.rule34.xxx/index.php?page=dapi&s=tag&q=index&name_pattern={prefix}%&orderby=count&limit=25",
    "gelbooru": "https://gelbooru.com/index.php?page=dapi&s=tag&q=index&name_pattern={prefix}%&orderby=count&limit=25",
    "safebooru": "https://safebooru.org/index.php?page=dapi&s=tag&q=index&name_pattern={prefix}%&orderby=count&limit=25",
    "xbooru": "https://xbooru.com/index.php?page=dapi&s=tag&q=index&name_pattern={prefix}%&orderby=count&limit=25",
    "tbib": "https://tbib.org/index.php?page=dapi&s=tag&q=index&name_pattern={prefix}%&orderby=count&limit=25",
    # moebooru
    "konachan": "https://konachan.com/tag.json?name={prefix}*&order=count&limit=25",
    "konachan.net": "https://konachan.net/tag.json?name={prefix}*&order=count&limit=25",
    "yandere": "https://yande.re/tag.json?name={prefix}*&order=count&limit=25",
    # e621/e926
    "e621": "https://e621.net/tags.json?search[name_matches]={prefix}*&search[order]=count&limit=25",
    "e926": "https://e926.net/tags.json?search[name_matches]={prefix}*&search[order]=count&limit=25",
    "danbooru": "https://danbooru.donmai.us/tags.json?search[name_matches]={prefix}*&search[order]=count&limit=25",
}

# Sources that need credentials from .env
_SITE_TAG_AUTH = frozenset({"rule34", "gelbooru"})


def _parse_site_tags_xml(text: str) -> list[str]:
    """Parse dapi XML (<tags><tag><name>...</name>...)."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    names: list[str] = []
    for item in root.iter("tag"):
        name = item.findtext("name")
        if name:
            names.append(name.strip().lower())
    return names


def _parse_site_tags(payload: object) -> list[str]:
    """Extract tag names from a board JSON response (several shapes)."""
    names: list[str] = []
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                name = item.get("name") or item.get("tag") or item.get("tag_name")
                if name:
                    names.append(str(name).strip().lower())
    elif isinstance(payload, dict):
        # e621/e926: {"tags": [...]}
        tags = payload.get("tags")
        if isinstance(tags, list):
            for item in tags:
                if isinstance(item, dict) and item.get("name"):
                    names.append(str(item["name"]).strip().lower())
    return names


async def fetch_site_tags(
    provider_key: str,
    session: aiohttp.ClientSession,
    prefix: str = "",
    creds: dict[str, str] | None = None,
) -> list[str]:
    """Live tag prefix search on a board (with an on-disk cache)."""
    prefix = (prefix or "").strip().lower()
    if not prefix:
        return []
    url = SITE_TAG_API.get(provider_key)
    if not url:
        return []
    url = url.format(prefix=quote_plus(prefix))
    if provider_key in _SITE_TAG_AUTH and creds:
        for key, value in creds.items():
            if value:
                url += f"&{key}={quote_plus(value)}"

    cache = CACHE_DIR / f"site-tags-{provider_key}-{prefix[:24]}.cache"
    now = time.time()
    if cache.is_file() and now - cache.stat().st_mtime < 24 * 3600:
        try:
            with open(cache, "rb") as fh:
                return await asyncio.to_thread(pickle.load, fh)
        except (OSError, EOFError, pickle.UnpicklingError):
            pass
    try:
        async with session.get(url, headers={"User-Agent": USER_AGENT}) as resp:
            if resp.status != 200:
                LOG.warning("Native tags %s: HTTP %s", provider_key, resp.status)
                return []
            text = await resp.text()
        # dapi (gelbooru family) answers XML, the rest answer JSON.
        if text.lstrip().startswith("<"):
            names = sorted(set(_parse_site_tags_xml(text)))
        else:
            payload = json.loads(text)
            names = sorted(set(_parse_site_tags(payload)))
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(cache, "wb") as fh:
            await asyncio.to_thread(pickle.dump, names, fh, 4)
        return names
    except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as exc:
        LOG.warning("Native tags %s unavailable: %s", provider_key, exc)
        return []
