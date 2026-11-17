"""Per-board tag resolution: make a typed tag valid for the board being queried.

A tag list written for one board rarely fits another. ``1girl`` is the everyday
spelling on danbooru and its clones, but a Moebooru board (konachan, yande.re,
sakugabooru) does not serve it at all — the search returns zero posts — while
Zerochan redirects it to ``Solo`` and writes multi-word names with spaces
rather than underscores. Sending the raw list produces silent ``found=0``.

Resolution runs in tiers, cheapest and most certain first:

1. the tag is already valid on the board (its own tag API lists it exactly);
2. a cross-engine table maps it to the name this engine uses;
3. the board's alias API names a canonical tag for it;
4. the closest tag with the highest count wins, if it is close enough.

Whatever cannot be resolved is reported rather than silently dropped, and every
answer is cached per ``(provider, input tag)`` so a repeat run costs nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus, unquote

import aiohttp

from ..config import default_db_path
from ..filters import canonical
from .store import SITE_TAG_API

LOG = logging.getLogger(__name__)

#: Exact-name lookup, ``{tag}`` substituted from the input.
#:
#: The prefix search in ``SITE_TAG_API`` cannot decide whether a tag exists:
#: Moebooru's ``?name=`` is a *substring* match (asking for ``long_hair`` also
#: returns ``absurdly_long_hair``) and a Gelbooru board's ``name_pattern`` is
#: unordered, so the exact row can sit past the result limit. These endpoints
#: answer with the tag that matches the name exactly.
SITE_TAG_EXACT: dict[str, str] = {
    "rule34": "https://api.rule34.xxx/index.php?page=dapi&s=tag&q=index&name={tag}",
    "gelbooru": "https://gelbooru.com/index.php?page=dapi&s=tag&q=index&name={tag}",
    "safebooru": "https://safebooru.org/index.php?page=dapi&s=tag&q=index&name={tag}",
    "xbooru": "https://xbooru.com/index.php?page=dapi&s=tag&q=index&name={tag}",
    "tbib": "https://tbib.org/index.php?page=dapi&s=tag&q=index&name={tag}",
    "konachan": "https://konachan.com/tag.json?name={tag}&limit=100",
    "konachan.net": "https://konachan.net/tag.json?name={tag}&limit=100",
    "yandere": "https://yande.re/tag.json?name={tag}&limit=100",
    "sakugabooru": "https://www.sakugabooru.com/tag.json?name={tag}&limit=100",
    "e621": "https://e621.net/tags.json?search%5Bname%5D={tag}&limit=5",
    "e926": "https://e926.net/tags.json?search%5Bname%5D={tag}&limit=5",
    "danbooru": "https://danbooru.donmai.us/tags.json?search%5Bname%5D={tag}&limit=5",
}

#: HTTP redirects, which zerochan uses to name a tag's canonical spelling.
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Boards whose exact lookup answers in dapi XML rather than JSON.
_XML_EXACT = frozenset({"rule34", "gelbooru", "safebooru", "xbooru", "tbib"})

#: Boards whose tag API is itself behind an API key. Measured: gelbooru answers
#: the tag endpoint with HTTP 401 and rule34 with a "Missing authentication"
#: document when the key is absent, so the tag would look nonexistent.
_AUTH_TAG_API = frozenset({"rule34", "gelbooru"})

#: How long a resolved tag stays good. Boards rename tags rarely, so a week is
#: ample and saves a request per tag on every run.
CACHE_TTL = 7 * 24 * 3600.0

#: Prefixes that carry no search meaning and must survive resolution untouched.
_META_PREFIXES = ("rating:", "order:", "status:", "score:", "sort:", "user:", "id:")

#: Cross-engine correspondences, keyed by the *canonical* danbooru-ish spelling.
#: Only entries that were confirmed against a live board belong here: a wrong
#: mapping is worse than none, because it turns a visible empty result into a
#: wrong one.
CROSS_ENGINE: dict[str, dict[str, str]] = {
    # Danbooru's 1girl/1boy counting tags have no Moebooru equivalent; a Moebooru
    # board serves "1girl" as a dead tag. Zerochan spells it Solo / Two Girls,
    # and e621 records the alias to female/male in its own alias table.
    "1girl": {"zerochan": "Solo", "e621": "female", "e926": "female"},
    "solo": {"zerochan": "Solo"},
    "1boy": {"zerochan": "Solo", "e621": "male", "e926": "male"},
    "2girls": {"zerochan": "Two Girls"},
    "3girls": {"zerochan": "Three Girls"},
    "multiple_girls": {"zerochan": "Multiple Girls"},
    "1girls": {"zerochan": "Solo"},
}

#: Per-board alias lookup, ``{tag}`` substituted from the input.
#:
#: A Moebooru board cannot be asked for its whole alias table — the listing
#: caps at 20 arbitrary rows and rate-limits past that — so its endpoint is not
#: listed here; the cross-engine table and the closest-tag tier cover it. The
#: boards below answer a *targeted* "is this an alias, and of what?" query.
_ALIAS_URLS: dict[str, str] = {
    "danbooru": "https://danbooru.donmai.us/tag_aliases.json?search%5Bantecedent_name%5D={tag}&limit=5",
    "e621": "https://e621.net/tag_aliases.json?search%5Bantecedent_name%5D={tag}&limit=5",
    "e926": "https://e926.net/tag_aliases.json?search%5Bantecedent_name%5D={tag}&limit=5",
}

#: Similarity below which the closest tag is too different to be worth using.
MATCH_CUTOFF = 0.72


def _db_path() -> Path:
    return default_db_path()


def _is_meta(tag: str) -> bool:
    return tag.lower().startswith(_META_PREFIXES)


def _normalized(name: str) -> str:
    """A tag name with only the differences boards disagree about removed.

    Spaces become underscores, because that is the one spelling difference every
    engine tolerates. Applying ``canonical()`` here would be wrong: it also
    strips a leading ``_`` or ``#``, yet Moebooru really does store ``_long_hair``
    and ``#cat`` as tags distinct from ``long_hair`` and ``cat``. Collapsing them
    would report an exact match for a tag the board does not have — the very
    failure this resolver exists to prevent.
    """
    return name.strip().lower().replace(" ", "_")


def _similar(a: str, b: str) -> float:
    """Similarity in 0..1, ignoring the underscores boards disagree about."""
    left = canonical(a).replace("_", "")
    right = canonical(b).replace("_", "")
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    # A prefix relationship (``long`` vs ``long_hair``) is a strong hint.
    if left.startswith(right) or right.startswith(left):
        return 0.85
    # Cheap edit-distance-free score: shared prefix plus length ratio.
    shared = 0
    for x, y in zip(left, right, strict=False):
        if x != y:
            break
        shared += 1
    ratio = min(len(left), len(right)) / max(len(left), len(right))
    return min(0.8, (shared / max(len(left), len(right))) * 1.5 + ratio * 0.3)


class Unknown:
    """The board could not be asked — which is *not* "the board has no such tag".

    The distinction matters more than it looks. A tag API behind an absent API
    key answers 401, and treating that as "absent" both hides the tag from the
    search and caches the lie for a week. An unresolvable answer must leave the
    tag alone instead.
    """


_UNKNOWN = Unknown()


@dataclass(slots=True)
class ResolvedTag:
    """One input tag and what the board will actually be searched for."""

    original: str
    resolved: str = ""
    #: board | cross-engine | alias | closest | unchanged | missing
    how: str = "unchanged"
    candidates: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.resolved)

    @property
    def unknown(self) -> bool:
        """True when the board could not be asked, so nothing was concluded."""
        return self.how == "unknown"


@dataclass(slots=True)
class Resolution:
    """The whole tag list, ready to query, plus what could not be mapped."""

    tags: str
    items: list[ResolvedTag] = field(default_factory=list)

    @property
    def missing(self) -> list[ResolvedTag]:
        """Input tags this board does not know (excluding ones never asked about)."""
        return [item for item in self.items if not item.ok and not item.unknown]

    @property
    def changed(self) -> list[ResolvedTag]:
        return [item for item in self.items if item.ok and item.resolved != item.original]

    def summary_lines(self) -> list[str]:
        """Human lines for the run summary: what moved, what was dropped."""
        lines: list[str] = []
        for item in self.changed:
            lines.append(f"   {item.original} -> {item.resolved}  ({item.how})")
        for item in self.missing:
            near = ", ".join(item.candidates[:3])
            hint = f" (closest: {near})" if near else ""
            lines.append(f"   {item.original}: not on this board{hint}")
        return lines


class TagCache:
    """Resolved tags, kept in the download database with a TTL.

    The same SQLite file the downloads live in is used on purpose: it already
    exists, is already the tool's durable state, and needs no second location to
    document or clean up.
    """

    def __init__(self, path: Path | str | None = None, *, ttl: float = CACHE_TTL) -> None:
        self._path = Path(path) if path else _db_path()
        self._ttl = ttl
        self._db: object | None = None
        self._ready = False

    async def _connect(self):
        import aiosqlite

        if self._db is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(str(self._path))
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS tag_resolutions (
                    provider    TEXT NOT NULL,
                    input_tag   TEXT NOT NULL,
                    resolved    TEXT NOT NULL,
                    how         TEXT NOT NULL,
                    resolved_at REAL NOT NULL,
                    PRIMARY KEY (provider, input_tag)
                )
                """
            )
            await self._db.commit()
        return self._db

    async def get(self, provider: str, tag: str) -> tuple[str, str] | None:
        """Cached answer for this pair, if it is still fresh."""
        db = await self._connect()
        cursor = await db.execute(
            "SELECT resolved, how, resolved_at FROM tag_resolutions WHERE provider = ? AND input_tag = ?",
            (provider, _normalized(tag)),
        )
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        if row is None:
            return None
        resolved, how, resolved_at = row
        if time.time() - float(resolved_at) > self._ttl:
            return None
        return str(resolved), str(how)

    async def put(self, provider: str, tag: str, resolved: str, how: str) -> None:
        db = await self._connect()
        await db.execute(
            """
            INSERT INTO tag_resolutions (provider, input_tag, resolved, how, resolved_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(provider, input_tag)
            DO UPDATE SET resolved = excluded.resolved,
                          how = excluded.how,
                          resolved_at = excluded.resolved_at
            """,
            (provider, _normalized(tag), resolved, how, time.time()),
        )
        await db.commit()

    async def put_missing(self, provider: str, tag: str) -> None:
        """Remember that a tag has no answer, so it is not re-queried every run."""
        await self.put(provider, tag, "", "missing")

    async def clear(self) -> int:
        db = await self._connect()
        cursor = await db.execute("DELETE FROM tag_resolutions")
        await db.commit()
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None


class TagResolver:
    """Resolve tags against whichever board is about to be queried."""

    def __init__(
        self,
        settings: object | None = None,
        *,
        cache: TagCache | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._settings = settings
        self._cache = cache or TagCache()
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            proxy = getattr(self._settings, "proxy", None) or None
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                proxy=proxy,
                headers={"User-Agent": "bdl-tag-resolver/1.0", "Accept": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        await self._cache.close()

    def _auth_params(self, provider: str) -> str:
        """The query suffix a keyed tag API needs, or an empty string.

        Without it gelbooru answers 401 and rule34 a "Missing authentication"
        document, and every tag would be reported as absent from the board.
        """
        if provider not in _AUTH_TAG_API:
            return ""
        creds = getattr(self._settings, "credentials_for", None)
        if creds is None:
            return ""
        try:
            pairs = creds(provider) or {}
        # A settings shim that does not know this board has no key to give.
        except Exception:
            return ""
        return "".join(f"&{quote_plus(str(k))}={quote_plus(str(v))}" for k, v in pairs.items() if v)

    async def _get_json(self, url: str) -> object | Unknown | None:
        """The decoded body, ``None`` for a body that is not JSON, ``Unknown``
        when the board never gave a usable answer at all."""
        try:
            session = await self._http()
            async with session.get(url) as response:
                if response.status != 200:
                    LOG.debug("tag API %s answered HTTP %d", url, response.status)
                    return _UNKNOWN
                text = await response.text()
        except (TimeoutError, aiohttp.ClientError) as exc:
            LOG.debug("tag API %s failed: %s", url, exc)
            return _UNKNOWN
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    async def exact_tag(self, provider: str, tag: str) -> str | None:
        """The board's own name for *tag*, if it holds that tag at all.

        This is what decides whether a tag is valid. A prefix search cannot do
        it: Moebooru answers ``?name=`` with every tag *containing* the string
        and a Gelbooru board's ``name_pattern`` comes back unordered, so the
        exact row may never be in the window. Here the query asks for the name
        itself and only an exact hit is accepted.
        """
        if provider == "zerochan":
            return await self._zerochan_tag(tag)
        template = SITE_TAG_EXACT.get(provider)
        if not template:
            return None
        url = template.format(tag=quote_plus(_normalized(tag))) + self._auth_params(provider)
        if provider in _XML_EXACT:
            return await self._exact_from_xml(url, tag)
        payload = await self._get_json(url)
        if isinstance(payload, Unknown):
            return payload
        if not isinstance(payload, list):
            return None
        wanted = _normalized(tag)
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("tag") or item.get("tag_name")
            if name and _normalized(str(name)) == wanted:
                return str(name).strip().lower()
        return None

    async def _zerochan_tag(self, tag: str) -> str | None:
        """Zerochan's tag API is its redirect.

        Asking for a tag answers one of three ways, and each is a fact:
        ``301`` with a ``Location`` naming the canonical spelling (underscores
        are wrong there — multi-word tags use spaces), ``500`` for a tag that
        exists as typed, and ``404`` for one the board does not have.
        """
        session = await self._http()
        path = quote_plus(tag.strip().replace("_", " "))
        url = f"https://www.zerochan.net/{path}?json&l=1"
        try:
            async with session.get(url, allow_redirects=False) as response:
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("Location", "")
                    target = location.rsplit("/", 1)[-1]
                    return unquote(target).replace("+", " ").strip().lower() or None
                if response.status in (200, 500):
                    return tag.strip().lower()
        except (TimeoutError, aiohttp.ClientError) as exc:
            LOG.debug("zerochan tag lookup failed for %s: %s", tag, exc)
            return _UNKNOWN
        return None  # 404: zerochan does not have this tag

    async def _exact_from_xml(self, url: str, tag: str) -> str | None:
        """The same exact check for a dapi board, whose answer is XML.

        Two shapes are in use: gelbooru nests ``<name>`` as a child element,
        while xbooru and safebooru put it in a ``name`` attribute.
        """
        import xml.etree.ElementTree as ET

        try:
            session = await self._http()
            async with session.get(url) as response:
                if response.status != 200:
                    LOG.debug("tag API %s answered HTTP %d", url, response.status)
                    return _UNKNOWN
                text = await response.text()
        except (TimeoutError, aiohttp.ClientError):
            return _UNKNOWN
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return None
        wanted = _normalized(tag)
        for item in root.iter("tag"):
            name = item.get("name") or item.findtext("name") or ""
            if _normalized(str(name)) == wanted:
                return str(name).strip().lower()
        return None

    async def board_tags(self, provider: str, tag: str) -> list[tuple[str, int]]:
        """Prefix search on the board's own tag API: [(name, count), ...].

        Used to offer candidates when an exact match does not exist, so a typo
        can be pointed at the nearest real tag.
        """
        pattern = SITE_TAG_API.get(provider)
        if not pattern:
            return []
        url = pattern.format(prefix=quote_plus(tag.replace(" ", "_"))) + self._auth_params(provider)
        payload = await self._get_json(url)
        if not isinstance(payload, list):
            return []
        found: list[tuple[str, int]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("tag") or item.get("tag_name")
            if not name:
                continue
            count = item.get("count") or item.get("post_count") or 0
            try:
                found.append((str(name).strip().lower(), int(count)))
            except (TypeError, ValueError):
                found.append((str(name).strip().lower(), 0))
        return found

    async def _alias_of(self, provider: str, tag: str) -> str | None:
        """The canonical tag this board lists *tag* as an alias of, if any.

        Fetched per tag rather than per run: the only boards that answer this
        are the ones whose alias table cannot be dumped, and the answer is
        cached in SQLite for a week anyway. A board that never sees the tag
        twice pays one request; one that does, pays none.
        """
        template = _ALIAS_URLS.get(provider)
        if not template:
            return None
        payload = await self._get_json(template.format(tag=quote_plus(_normalized(tag))))
        if not isinstance(payload, list):
            return None
        wanted = _normalized(tag)
        for item in payload:
            if not isinstance(item, dict):
                continue
            antecedent = item.get("antecedent_name")
            consequent = item.get("consequent_name")
            if not antecedent or not consequent:
                continue
            if _normalized(str(antecedent)) == wanted:
                return str(consequent).strip().lower()
        return None

    async def resolve_tag(self, provider: str, tag: str) -> ResolvedTag:
        """One tag, resolved for one board, cache first."""
        clean = tag.strip()
        if not clean or _is_meta(clean):
            return ResolvedTag(original=clean, resolved=clean, how="unchanged")

        key = _normalized(clean)
        cached = await self._cache.get(provider, key)
        if cached is not None:
            resolved, how = cached
            return ResolvedTag(original=clean, resolved=resolved, how=how)

        answer = await self._resolve_uncached(provider, clean)
        # The unknown case is tested first on purpose. Its answer keeps the tag
        # as typed so the search still uses it, which makes ``.ok`` true; taking
        # that for a successful resolution would cache the failure for a week.
        if answer.unknown:
            LOG.debug("could not ask %s about %r; leaving it as typed", provider, clean)
        elif answer.ok:
            await self._cache.put(provider, key, answer.resolved, answer.how)
        else:
            await self._cache.put_missing(provider, key)
        return answer

    async def _resolve_uncached(self, provider: str, tag: str) -> ResolvedTag:
        variants = {tag.strip().lower(), _normalized(tag)}

        # 1. the board holds this tag exactly (its own name for it wins).
        exact = await self.exact_tag(provider, tag)
        if isinstance(exact, Unknown):
            # The board could not be asked. Leave the tag exactly as typed and
            # conclude nothing: an unreachable tag API is not evidence of an
            # absent tag.
            return ResolvedTag(original=tag, resolved=tag, how="unknown")
        if exact:
            return ResolvedTag(original=tag, resolved=exact, how="board")

        # 2. the cross-engine table.
        for variant in variants:
            mapped = CROSS_ENGINE.get(variant, {}).get(provider)
            if mapped:
                return ResolvedTag(original=tag, resolved=mapped, how="cross-engine")

        # 3. the board's own alias table.
        alias = await self._alias_of(provider, tag)
        if alias:
            return ResolvedTag(original=tag, resolved=alias, how="alias")

        # 4. the closest tag with the highest count, if it is close enough.
        board = await self.board_tags(provider, tag)
        ranked = sorted(board, key=lambda pair: pair[1], reverse=True)
        candidates = tuple(name for name, _count in ranked[:5])
        best, best_score = "", 0.0
        for name, _count in ranked:
            score = _similar(tag, name)
            if score > best_score:
                best, best_score = name, score
        if best and best_score >= MATCH_CUTOFF:
            return ResolvedTag(original=tag, resolved=best, how="closest", candidates=candidates)

        return ResolvedTag(original=tag, resolved="", how="missing", candidates=candidates)

    async def resolve(self, provider: str, tags: str) -> Resolution:
        """A whole tag string, resolved tag by tag, order preserved."""
        items: list[ResolvedTag] = []
        for chunk in tags.split():
            items.append(await self.resolve_tag(provider, chunk))
        kept = [item.resolved for item in items if item.ok]
        return Resolution(tags=" ".join(kept), items=items)


async def resolve_for_providers(
    providers: list[str],
    tags: str,
    *,
    settings: object | None = None,
    cache: TagCache | None = None,
) -> dict[str, Resolution]:
    """Resolve the same tag string for several boards at once.

    Boards are queried concurrently, because one board's tag API should not
    delay another's — the whole point is to reach every source on the first try.
    """
    resolver = TagResolver(settings, cache=cache)
    try:
        results = await asyncio.gather(
            *(resolver.resolve(provider, tags) for provider in providers),
            return_exceptions=True,
        )
    finally:
        await resolver.close()

    out: dict[str, Resolution] = {}
    for provider, result in zip(providers, results, strict=True):
        if isinstance(result, BaseException):
            LOG.warning("tag resolution failed for %s: %s", provider, result)
            out[provider] = Resolution(tags=tags)
        else:
            out[provider] = result
    return out
