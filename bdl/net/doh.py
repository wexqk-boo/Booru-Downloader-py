"""DNS-over-HTTPS resolver for aiohttp.

The local resolver on this machine blocks the booru CDNs (``img4.gelbooru.com``,
``api-cdn.rule34.xxx``), so media downloads failed. DoH sidesteps that: the name
is resolved through Cloudflare, the connection goes to the returned IP, while
TLS/SNI keep the original hostname.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from typing import Any

import aiohttp
from aiohttp.abc import AbstractResolver

LOG = logging.getLogger(__name__)

DOH_URL = "https://cloudflare-dns.com/dns-query"
CACHE_TTL = 300.0
#: A resolver that answers is worth 8 s of patience; one that is blackholed is
#: not, because the answer is the same and every host waits for it.
QUERY_TIMEOUT = 5.0
#: After this many failures in a row the resolver stops asking for a while.
#: A filtered network drops DoH packets rather than refusing them, so without
#: this every hostname in a 12-source run pays the full timeout, one after
#: another, and the run looks like "the timeouts fire too early".
FAILURE_THRESHOLD = 3
COOLDOWN = 120.0


class DoHResolver(AbstractResolver):
    """Cloudflare DoH resolver with a cache and a fallback to system DNS."""

    def __init__(self, doh_url: str = DOH_URL, proxy: str | None = None) -> None:
        self._doh_url = doh_url
        #: Honour ``PROXY_URL`` here too: when the boards are only reachable
        #: through a proxy, cloudflare-dns.com usually is as well, and asking
        #: for it directly just burns the timeout before every single resolve.
        self._proxy = proxy or None
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        #: Consecutive DoH failures and the time we may ask again. Guarded by
        #: the event loop only: every reader runs on the same loop.
        self._failures = 0
        self._retry_at = 0.0

    def _in_cooldown(self) -> bool:
        return self._failures >= FAILURE_THRESHOLD and time.monotonic() < self._retry_at

    def _note_result(self, ok: bool) -> None:
        if ok:
            self._failures = 0
            return
        self._failures += 1
        if self._failures == FAILURE_THRESHOLD:
            self._retry_at = time.monotonic() + COOLDOWN
            LOG.warning(
                "DoH failed %d times in a row; skipping it for %.0f s and using system DNS",
                self._failures,
                COOLDOWN,
            )
        elif self._failures > FAILURE_THRESHOLD:
            self._retry_at = time.monotonic() + COOLDOWN

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=QUERY_TIMEOUT),
                proxy=self._proxy,
            )
        return self._session

    async def resolve(self, host: str, port: int = 0, family: int = 0) -> list[dict[str, Any]]:
        cached = self._cache.get(host)
        if cached and cached[0] > time.monotonic():
            return self._adapt(cached[1], host, port, family)

        async with self._lock:
            cached = self._cache.get(host)
            if cached and cached[0] > time.monotonic():
                return self._adapt(cached[1], host, port, family)

            addresses = [] if self._in_cooldown() else await self._query(host)
            if not addresses:
                LOG.info("DoH has no record for %s, falling back to system DNS", host)
                addresses = await self._system(host)

            records = [
                {"hostname": host, "host": ip, "port": port, "family": socket.AF_INET, "proto": 0, "flags": 0}
                for ip in addresses
            ]
            if records:
                self._cache[host] = (time.monotonic() + CACHE_TTL, records)
            return self._adapt(records, host, port, family)

    async def _query(self, host: str) -> list[str]:
        params = {"name": host, "type": "A"}
        headers = {"accept": "application/dns-json"}
        try:
            session = await self._get_session()
            async with session.get(self._doh_url, params=params, headers=headers) as response:
                if response.status != 200:
                    LOG.warning("DoH returned HTTP %s", response.status)
                    self._note_result(False)
                    return []
                data = json.loads(await response.text())
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as exc:
            LOG.warning("DoH is unavailable: %s", exc)
            self._note_result(False)
            return []

        self._note_result(True)
        return [answer["data"] for answer in data.get("Answer", []) if answer.get("type") == 1 and answer.get("data")]

    @staticmethod
    async def _system(host: str) -> list[str]:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, family=socket.AF_INET)
        except OSError:
            return []
        return sorted({info[4][0] for info in infos})

    @staticmethod
    def _adapt(records: list[dict[str, Any]], host: str, port: int, family: int) -> list[dict[str, Any]]:
        # Always report the address's real family, never the requested one:
        # an IPv4 address passed into an IPv6 socket fails to connect.
        adapted: list[dict[str, Any]] = []
        for record in records:
            ip = record.get("host", "")
            actual = socket.AF_INET6 if ":" in ip else socket.AF_INET
            adapted.append({**record, "hostname": host, "port": port, "family": actual})
        return adapted

    async def close(self) -> None:
        self._cache.clear()
        self._failures = 0
        self._retry_at = 0.0
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
