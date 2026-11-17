"""Provider HTTP client: one session, retries, cached responses."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp

from .. import USER_AGENT, XBOORU_HEADERS
from ..media import NSFW_RATINGS, SAFE_RATINGS, SUGGESTIVE_RATINGS
from ..models import Post
from ..net.doh import DoHResolver
from .base import MOEBOORU_MAX_TAGS, Provider, ProviderError, QueryPlan

LOG = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

#: How many leading characters a broken JSON body may carry before the real
#: object starts. Zerochan opens its payload with a stray brace.
_JSON_SLACK = 8

#: Statuses that move a request elsewhere. They are followed by hand for a
#: board that drops the query string on the way (see ``_redirect_target``).
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Guard for the hand-rolled redirect walk, so a board answering with a loop
#: cannot hold a source open until the request timeout.
MAX_REDIRECTS = 5

#: Every spelling a board may use for the three rating categories, so a
#: ``rating:`` filter dropped from a query still matches whatever comes back.
_RATING_ALIASES: dict[str, frozenset[str]] = {
    **{alias: SAFE_RATINGS for alias in ("safe", "s", "general", "g")},
    **{alias: SUGGESTIVE_RATINGS for alias in ("questionable", "q", "sensitive")},
    **{alias: NSFW_RATINGS for alias in ("explicit", "e")},
}


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider: str
    posts: tuple[Post, ...]
    exhausted: bool


class ProviderClient:
    """Async client for every source at once."""

    def __init__(self, settings: Any, *, max_concurrency: int = 6) -> None:
        self._settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._resolver = DoHResolver(proxy=getattr(settings, "proxy", None))
        self._cache: dict[tuple[str, str, int, int], tuple[float, ProviderResult]] = {}

    # -- lifecycle ---------------------------------------------------------
    async def open(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        # Hard timeouts everywhere so no socket can hang forever.
        timeout = aiohttp.ClientTimeout(total=25, connect=5, sock_read=20)
        connector = aiohttp.TCPConnector(
            limit=16, ttl_dns_cache=300, enable_cleanup_closed=True, resolver=self._resolver
        )
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            proxy=self._settings.proxy or None,
        )

    @staticmethod
    def _headers_for(provider: Provider) -> dict[str, str]:
        """Per-board request headers.

        Xbooru's DAPI needs no key, but it sits behind Cloudflare, which is
        quicker to challenge a client that announces itself as a script than
        one that arrives with a browser's full header set.
        """
        if provider.key == "xbooru":
            return dict(XBOORU_HEADERS)
        return provider.request_headers()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        await self._resolver.close()

    async def __aenter__(self) -> ProviderClient:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- cache -------------------------------------------------------------
    def _cache_get(self, key: tuple[str, str, int, int, str]) -> ProviderResult | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, result = entry
        if expires_at < time.monotonic():
            self._cache.pop(key, None)
            return None
        return result

    def _cache_put(self, key: tuple[str, str, int, int, str], result: ProviderResult) -> None:
        if self._settings.cache_ttl <= 0:
            return
        if len(self._cache) >= self._settings.max_cache_entries:
            oldest = min(self._cache, key=lambda item: self._cache[item][0])
            self._cache.pop(oldest, None)
        self._cache[key] = (time.monotonic() + self._settings.cache_ttl, result)

    def clear_cache(self) -> None:
        self._cache.clear()

    # -- api ---------------------------------------------------------------
    async def search(self, provider_key: str, tags: str, page: int, limit: int, order: str = "") -> ProviderResult:
        from . import PROVIDERS  # lazy: this package may still be initialising

        provider = PROVIDERS.get(provider_key)
        if provider is None:
            raise ProviderError(f"Unknown source: {provider_key}")
        return await self.search_provider(provider, provider_key, tags, page, limit, order)

    async def search_provider(
        self,
        provider: Provider,
        provider_key: str,
        tags: str,
        page: int,
        limit: int,
        order: str = "",
    ) -> ProviderResult:
        """One board query, including the weekly-top fallback.

        Shared by both client classes on purpose: the fallback was once written
        directly into ``search`` and silently lost, because ``CliProviderClient``
        overrides that method and never called it.
        """
        cache_key = (provider_key, tags, page, limit, order)
        cached = self._cache_get(cache_key)
        if cached is not None:
            LOG.debug("cache hit %s: %s p%s", provider_key, tags, page)
            return cached

        creds = self._settings.credentials_for(provider_key)
        # The plan decides both what the request says and whether the board is
        # the one doing the ranking; the two must not drift apart.
        plan = provider.query_plan(tags, order)
        if plan.dropped_tags:
            LOG.warning(
                "%s: %d tag(s) left out — the board rejects a query with more than %d tags",
                provider.key,
                plan.dropped_tags,
                MOEBOORU_MAX_TAGS,
            )
        # When the ranking happens locally, only the first page is worth asking
        # for: score order is not a property the board can page through, so a
        # second page would only add posts that outrank the first one's.
        if plan.single_page:
            page = 1

        url = provider.build_url(tags, page, limit, creds, order)
        # A bare "top" on a Moebooru board is best answered by its own weekly
        # listing: it is a real endpoint, but it takes no tags and ignores
        # paging, so only an untagged first page can use it.
        use_popular = (
            page == 1
            and order == "top"
            and not tags.strip()
            and provider.kind == "moebooru"
            and bool(provider.popular_endpoint)
        )
        if use_popular:
            url = provider.popular_url(creds)
        payload = await self._fetch_json(url, provider)
        raw_items = provider.raw_items(payload)
        posts = tuple(provider.parse(payload))
        if plan.rating_filter:
            posts = tuple(post for post in posts if _rating_allowed(post.rating, plan.rating_filter))
        if not posts and order == "top" and page == 1:
            # Two ways a week's ranking comes back empty, and each has an answer
            # the other already holds: the board's own listing cannot be reached
            # or is empty early in the week, or the tags are ones this board
            # does not serve at all (``1girl`` lives on danbooru, not on a
            # Moebooru board). Either way, showing nothing for "best of the
            # week" is the ``found=0`` this replaces.
            posts = await self._top_fallback(provider, plan, creds, from_popular=use_popular)
        if plan.client_sort == "score":
            posts = tuple(sorted(posts, key=_score_key))
        result = ProviderResult(
            provider=provider_key,
            posts=posts,
            # A locally sorted page is the whole answer, so it counts as the
            # end of the listing however full it looks.
            exhausted=plan.single_page or len(raw_items) < limit,
        )
        self._cache_put(cache_key, result)
        return result

    async def _top_fallback(
        self,
        provider: Provider,
        plan: QueryPlan,
        creds: dict[str, str],
        *,
        from_popular: bool,
    ) -> tuple[Post, ...]:
        """The other way to answer a ``top`` request that came back empty.

        A ``top`` query has two possible sources: the board's own weekly listing
        and a score-ordered search. Whichever one just produced nothing, the
        other is tried, so a tagless request whose listing is empty still gets
        ranked posts, and a tag the board does not index still gets the week's
        best work instead of an empty page.
        """
        if from_popular:
            LOG.info("%s: the weekly listing is empty; falling back to a score search", provider.key)
            try:
                payload = await self._fetch_json(
                    provider.build_url(plan.tags, 1, 0, creds, "top"), provider
                )
            except ProviderError:
                return ()
        else:
            if not provider.popular_endpoint:
                return ()
            LOG.info(
                "%s: no post carries %s; falling back to the board's weekly listing",
                provider.key,
                plan.tags or "the query",
            )
            try:
                payload = await self._fetch_json(provider.popular_url(creds), provider)
            except ProviderError:
                return ()
        posts = tuple(provider.parse(payload))
        if plan.rating_filter:
            posts = tuple(post for post in posts if _rating_allowed(post.rating, plan.rating_filter))
        return posts

    async def _fetch_json(self, url: str, provider: Provider) -> object:
        # Reopen on our own: a network failure or suspend may have closed the session.
        await self.open()

        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                # The semaphore covers the request only. Sleeping inside it
                # would let one source's 429 hold up every other source.
                async with self._semaphore:
                    text = await self._read(url, provider)
            except _ThrottledError as exc:
                last_error = exc
                LOG.warning("%s: 429, pausing %.1f s", provider.key, exc.pause)
                await asyncio.sleep(exc.pause)
                continue
            except _RetryableError as exc:
                last_error = exc
                await asyncio.sleep(_backoff(attempt))
                continue
            except (TimeoutError, aiohttp.ClientError) as exc:
                last_error = exc
                LOG.warning("%s: network error (%s), attempt %d/%d", provider.key, exc, attempt + 1, MAX_ATTEMPTS)
                await asyncio.sleep(_backoff(attempt))
                continue

            stripped = text.strip()
            if not stripped:
                return []
            payload = _loads_lenient(stripped)
            if payload is not None:
                return payload
            if stripped.startswith("<"):
                # A board refusing the request in its own words (an XML error
                # document) is handed to ``raw_items``, which reports the
                # reason the board gave instead of blaming an anti-bot page.
                return stripped
            snippet = stripped[:120].replace("\n", " ")
            raise ProviderError(
                f"{provider.title}: response is not JSON ({snippet!r}). An anti-bot page is the likely cause."
            )

        raise ProviderError(f"{provider.title}: source unavailable ({last_error})")

    async def _read(self, url: str, provider: Provider) -> str:
        """GET one URL, following redirects by hand and keeping the query.

        Boards redirect a tag to its canonical spelling and drop the query on
        the way: Zerochan answers ``/1girl?json`` with ``301 /Solo``, so letting
        the HTTP layer follow it lands on an HTML page with the ``?json`` (and
        the paging) gone. The original query is carried over to the target
        instead, so the caller still gets the format it asked for.
        """
        assert self._session is not None
        headers = self._headers_for(provider)
        target = url
        for _ in range(MAX_REDIRECTS):
            async with self._session.get(target, headers=headers, allow_redirects=False) as response:
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        raise ProviderError(f"{provider.title}: HTTP {response.status} without a Location header")
                    target = _redirect_target(target, location)
                    LOG.debug("%s: redirect to %s", provider.key, target)
                    continue
                return await _response_text(response, provider)
        raise ProviderError(f"{provider.title}: too many redirects (last: {target})")


class _RetryableError(RuntimeError):
    pass


class _ThrottledError(RuntimeError):
    """A 429 that must be waited out before the next attempt."""

    def __init__(self, pause: float) -> None:
        super().__init__(f"throttled, retry in {pause:.1f} s")
        self.pause = pause


async def _response_text(response: aiohttp.ClientResponse, provider: Provider) -> str:
    """Body of an accepted response, or the error the board's status implies."""
    status = response.status
    if status == 429:
        raise _ThrottledError(_retry_after(response.headers.get("Retry-After")))
    if status in provider.extra_ok_statuses or status < 400:
        return await response.text()
    if status >= 500:
        raise _RetryableError(f"HTTP {status}")
    if status in (401, 403):
        raise ProviderError(_denied_message(provider, status))
    raise ProviderError(_status_message(provider, status))


def _redirect_target(current: str, location: str) -> str:
    """Where a redirect points, with the original query carried over.

    A board that redirects a tag to its canonical spelling tends to hand back a
    bare path, losing the caller's ``?json`` and paging. Anything the target
    already sets explicitly wins; everything else is inherited from the URL
    that was asked for, so a redirect cannot silently downgrade the response
    from JSON to a full HTML page.
    """
    target = urljoin(current, location)
    current_parts = urlsplit(current)
    target_parts = urlsplit(target)

    if not current_parts.query:
        return target
    if not target_parts.query:
        return urlunsplit(target_parts._replace(query=current_parts.query))

    # Both carry a query: the target's own parameters win, the rest is kept.
    kept = _query_pairs(current_parts.query)
    kept.update(_query_pairs(target_parts.query))
    merged = urlencode(list(kept.items()))
    return urlunsplit(target_parts._replace(query=merged))


def _query_pairs(query: str) -> dict[str, str]:
    """Query string as an ordered mapping; a bare flag maps to an empty value."""
    return dict(parse_qsl(query, keep_blank_values=True))


def _loads_lenient(text: str) -> Any | None:
    """Parse a board's JSON, tolerating a little junk in front of it.

    Zerochan answers with a stray opening brace before the real object (and
    trailing HTML after it), so a plain ``json.loads`` never succeeds on a
    valid listing. The first few characters are skipped one at a time and the
    value is decoded from whatever position works.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for offset in range(1, min(_JSON_SLACK, len(text))):
        if text[offset] not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(text, offset)
        except json.JSONDecodeError:
            continue
        return value
    return None


def _score_key(post: Post) -> tuple[int, str]:
    """Sort by score, best first; posts without a score sink to the bottom."""
    return (-(post.score or 0), post.uid)


def _rating_allowed(rating: str | None, wanted: frozenset[str]) -> bool:
    """Whether a post matches a ``rating:`` filter the query could not carry.

    Boards spell the same rating differently — Moebooru uses ``s``/``q``/``e``
    where gelbooru uses ``safe``/``questionable``/``explicit`` — so the filter is
    resolved through the shared rating buckets rather than compared letter by
    letter, which would drop every post on a board that spells it the other way.
    """
    if rating is None:
        return False
    lowered = rating.strip().lower()
    for want in wanted:
        bucket = _RATING_ALIASES.get(want.strip().lower())
        if bucket is None:
            if lowered == want.strip().lower():
                return True
        elif lowered in bucket:
            return True
    return False


def _status_message(provider: Provider, status: int) -> str:
    """Explain a 4xx without pretending every board wants a key.

    Xbooru, safebooru, tbib, zerochan and friends take no credentials
    at all, so answering their 404 with "Check the API keys" sends the user
    hunting for a setting that does not exist. Only a board that genuinely
    authenticates gets the key advice — and a board that does not is never even
    told about keys.
    """
    needs_key = provider.key_required or provider.auth_style in ("user_id", "login")
    if status == 404:
        # rule34 answers a bad or missing key with a 404 rather than a 401,
        # which is the one board where the key hint is worth giving.
        if needs_key:
            return (
                f"{provider.title}: HTTP 404. The board has no such page — check the tags, "
                f"and {provider.key.upper()}_API_KEY if this search needs credentials."
            )
        return f"{provider.title}: HTTP 404. The board has no such page — check the tags."
    if needs_key:
        return (
            f"{provider.title}: request failed with HTTP {status} "
            f"(credentials rejected or the query is invalid). Check {provider.key.upper()}_API_KEY in .env."
        )
    return (
        f"{provider.title}: request failed with HTTP {status} — the tags are likely invalid, "
        "or the board is throttling."
    )


def _denied_message(provider: Provider, status: int) -> str:
    """Explain a 401/403 honestly, instead of blaming a missing API key.

    A handful of boards need no key at all (xbooru, safebooru, tbib, e621), so
    telling the user to add one sends them looking for a setting that does not
    exist. What these boards do have is a bot filter.
    """
    if provider.key_required or provider.auth_style in ("user_id", "login"):
        return f"{provider.title}: authorization required (HTTP {status}). Add {provider.key.upper()}_API_KEY to .env."
    return (
        f"{provider.title}: request rejected (HTTP {status}) by the board's bot filter. "
        "Try again later or set PROXY_URL in .env."
    )


def _backoff(attempt: int) -> float:
    return min(0.4 * (2**attempt), 4.0)


def _retry_after(raw: str | None) -> float:
    try:
        return min(max(float(raw or 2.0), 0.5), 8.0)
    except (TypeError, ValueError):
        return 2.0
