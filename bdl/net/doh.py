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


class DoHResolver(AbstractResolver):
    """Cloudflare DoH resolver with a cache and a fallback to system DNS."""

    def __init__(self, doh_url: str = DOH_URL) -> None:
        self._doh_url = doh_url
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        return self._session

    async def resolve(self, host: str, port: int = 0, family: int = 0) -> list[dict[str, Any]]:
        cached = self._cache.get(host)
        if cached and cached[0] > time.monotonic():
            return self._adapt(cached[1], host, port, family)

        async with self._lock:
            cached = self._cache.get(host)
            if cached and cached[0] > time.monotonic():
                return self._adapt(cached[1], host, port, family)

            addresses = await self._query(host)
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
                    return []
                data = json.loads(await response.text())
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as exc:
            LOG.warning("DoH is unavailable: %s", exc)
            return []

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
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
