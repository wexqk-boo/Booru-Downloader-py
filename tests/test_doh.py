"""Tests for the DNS-over-HTTPS resolver: caching, fallback and back-off.

No real network traffic: the aiohttp session is replaced by a double, so the
timing and the failure bookkeeping are exercised deterministically.
"""

from __future__ import annotations

import time

import aiohttp
import pytest

from bdl.net.doh import COOLDOWN, FAILURE_THRESHOLD, DoHResolver


class _DeadSession:
    """A session whose every request fails, as a blackholed DoH does."""

    closed = False

    def get(self, *_args, **_kwargs):
        raise aiohttp.ClientError("connection dropped")

    async def close(self) -> None:
        self.closed = True


class _AnsweringSession:
    """A session returning a valid Cloudflare DoH JSON document."""

    closed = False

    def __init__(self, ip: str) -> None:
        self._ip = ip

    async def close(self) -> None:
        self.closed = True

    def get(self, *_args, **_kwargs):
        ip = self._ip

        class _Response:
            status = 200

            async def text(self) -> str:
                return f'{{"Answer": [{{"type": 1, "data": "{ip}"}}]}}' 

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc) -> bool:
                return False

        return _Response()


@pytest.mark.asyncio
async def test_a_successful_lookup_is_cached(monkeypatch) -> None:
    resolver = DoHResolver()
    resolver._session = _AnsweringSession("203.0.113.7")  # type: ignore[assignment]
    records = await resolver.resolve("konachan.com", 443)
    assert records[0]["host"] == "203.0.113.7"
    assert records[0]["hostname"] == "konachan.com"
    assert records[0]["port"] == 443

    # A second lookup is answered from the cache, with no new DoH round trip:
    # swapping in a session that would fail proves the query never went out.
    resolver._session = _DeadSession()  # type: ignore[assignment]
    again = await resolver.resolve("konachan.com", 443)
    assert [record["host"] for record in again] == ["203.0.113.7"]
    await resolver.close()


@pytest.mark.asyncio
async def test_it_falls_back_to_system_dns_when_doh_is_empty(monkeypatch) -> None:
    resolver = DoHResolver()
    resolver._session = _DeadSession()  # type: ignore[assignment]

    async def system(_host: str) -> list[str]:
        return ["198.51.100.9"]

    monkeypatch.setattr(resolver, "_system", staticmethod(system))
    records = await resolver.resolve("example.net", 443)
    assert [record["host"] for record in records] == ["198.51.100.9"]
    await resolver.close()


@pytest.mark.asyncio
async def test_repeated_failures_open_a_cooldown(monkeypatch) -> None:
    """A blackholed DoH must not cost every hostname its own timeout.

    On a filtered network the packets are dropped, so each query waits out the
    full timeout; without a cooldown a 12-source run pays that once per host,
    one after another, which is what made the boards look like they timed out
    early.
    """
    resolver = DoHResolver()
    resolver._session = _DeadSession()  # type: ignore[assignment]
    queries = 0
    real_query = resolver._query

    async def counted_query(host: str) -> list[str]:
        nonlocal queries
        queries += 1
        return await real_query(host)

    async def system(_host: str) -> list[str]:
        return ["198.51.100.9"]

    monkeypatch.setattr(resolver, "_query", counted_query)
    monkeypatch.setattr(resolver, "_system", staticmethod(system))

    for index in range(FAILURE_THRESHOLD):
        await resolver.resolve(f"host-{index}.example.net", 443)
    assert queries == FAILURE_THRESHOLD

    # Every later lookup is served by system DNS without touching DoH again.
    for index in range(20):
        await resolver.resolve(f"later-{index}.example.net", 443)
    assert queries == FAILURE_THRESHOLD
    assert resolver._retry_at > time.monotonic()
    assert resolver._in_cooldown()

    await resolver.close()


@pytest.mark.asyncio
async def test_a_later_success_clears_the_cooldown(monkeypatch) -> None:
    resolver = DoHResolver()
    resolver._session = _DeadSession()  # type: ignore[assignment]

    async def system(_host: str) -> list[str]:
        return ["198.51.100.9"]

    monkeypatch.setattr(resolver, "_system", staticmethod(system))

    for index in range(FAILURE_THRESHOLD):
        await resolver.resolve(f"down-{index}.example.net", 443)
    assert resolver._in_cooldown()

    # The cooldown window has passed, so DoH is tried again and now answers.
    resolver._retry_at = 0.0
    resolver._session = _AnsweringSession("203.0.113.5")  # type: ignore[assignment]
    records = await resolver.resolve("recovered.example.net", 443)
    assert [record["host"] for record in records] == ["203.0.113.5"]
    assert resolver._failures == 0
    assert not resolver._in_cooldown()

    await resolver.close()


def test_the_cooldown_is_bounded() -> None:
    """The window is a handful of minutes, not the rest of the session."""
    assert 30 <= COOLDOWN <= 600
