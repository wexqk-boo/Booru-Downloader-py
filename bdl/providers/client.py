"""Provider HTTP client: one session, retries, cached responses."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from .. import USER_AGENT
from ..models import Post
from ..net.doh import DoHResolver
from .base import Provider, ProviderError

LOG = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


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
        self._resolver = DoHResolver()
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

        cache_key = (provider_key, tags, page, limit, order)
        cached = self._cache_get(cache_key)
        if cached is not None:
            LOG.debug("cache hit %s: %s p%s", provider_key, tags, page)
            return cached

        url = provider.build_url(tags, page, limit, self._settings.credentials_for(provider_key), order)
        payload = await self._fetch_json(url, provider)
        raw_items = provider.raw_items(payload)
        posts = tuple(provider.parse(payload))
        result = ProviderResult(
            provider=provider_key,
            posts=posts,
            exhausted=len(raw_items) < limit,
        )
        self._cache_put(cache_key, result)
        return result

    async def _fetch_json(self, url: str, provider: Provider) -> object:
        # Reopen on our own: a network failure or suspend may have closed the session.
        await self.open()

        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            pause = 0.0
            try:
                async with self._semaphore:
                    assert self._session is not None
                    async with self._session.get(url) as response:
                        if response.status == 429:
                            # Sleep after leaving the semaphore, otherwise one
                            # source's 429 holds up every other source.
                            pause = _retry_after(response.headers.get("Retry-After"))
                            last_error = ProviderError(f"{provider.title}: too many requests (429)")
                        elif response.status >= 500:
                            raise _RetryableError(f"HTTP {response.status}")
                        elif response.status in (401, 403):
                            raise ProviderError(
                                f"{provider.title}: authorization required (HTTP {response.status}). "
                                f"Add {provider.key.upper()}_API_KEY to .env."
                            )
                        elif response.status >= 400:
                            raise ProviderError(
                                f"{provider.title}: response error HTTP {response.status}. Check the API keys."
                            )
                        else:
                            text = await response.text()
            except _RetryableError as exc:
                last_error = exc
                await asyncio.sleep(_backoff(attempt))
                continue
            except (TimeoutError, aiohttp.ClientError) as exc:
                last_error = exc
                LOG.warning("%s: network error (%s), attempt %d/%d", provider.key, exc, attempt + 1, MAX_ATTEMPTS)
                await asyncio.sleep(_backoff(attempt))
                continue

            if pause:
                LOG.warning("%s: 429, pausing %.1f s", provider.key, pause)
                await asyncio.sleep(pause)
                continue

            stripped = text.strip()
            if not stripped:
                return []
            try:
                return json.loads(stripped)
            except json.JSONDecodeError as exc:
                snippet = stripped[:120].replace("\n", " ")
                raise ProviderError(
                    f"{provider.title}: response is not JSON ({snippet!r}). An anti-bot page is the likely cause."
                ) from exc

        raise ProviderError(f"{provider.title}: source unavailable ({last_error})")


class _RetryableError(RuntimeError):
    pass


def _backoff(attempt: int) -> float:
    return min(0.4 * (2**attempt), 4.0)


def _retry_after(raw: str | None) -> float:
    try:
        return min(max(float(raw or 2.0), 0.5), 8.0)
    except (TypeError, ValueError):
        return 2.0
