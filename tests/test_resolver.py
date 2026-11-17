"""The per-board tag resolver: mapping, reporting and the SQLite cache.

The resolver's whole job is to turn what a person typed into what a particular
board will accept, and to say plainly when it cannot. The tests here keep that
promise honest: a tag the board does not have must be reported, a board that
could not be reached must not be mistaken for one that has no such tag, and an
answer must be cached per board so the next run does not ask again.

Everything runs against a throwaway cache in ``tmp_path`` and a stub session:
no network, and nothing under the user's real config or picture folders.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdl.tags import resolver as R


class _Response:
    def __init__(self, body: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


class _Session:
    """Answers each URL from a table, recording what was asked for."""

    closed = False

    def __init__(self, responses: dict[str, _Response] | None = None, default: _Response | None = None) -> None:
        self.responses = responses or {}
        self.default = default or _Response("[]")
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs) -> _Response:
        self.urls.append(url)
        for fragment, response in self.responses.items():
            if fragment in url:
                return response
        return self.default


class _Settings:
    def __init__(self, creds: dict[str, dict[str, str]] | None = None, proxy: str = "") -> None:
        self.proxy = proxy
        self._creds = creds or {}

    def credentials_for(self, provider: str) -> dict[str, str]:
        return self._creds.get(provider, {})


def _run(coro):
    """Run one coroutine to completion (the suite drives asyncio directly)."""
    import asyncio

    return asyncio.run(coro)


def _resolver(session: _Session, tmp_path: Path, settings: _Settings | None = None) -> R.TagResolver:
    resolver = R.TagResolver(settings or _Settings(), cache=R.TagCache(tmp_path / "tags.sqlite3"))
    resolver._session = session  # inject the double; _http() returns it as-is
    return resolver


@pytest.fixture(autouse=True)
def _no_ambient_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROXY_URL", raising=False)


# --------------------------------------------------------------------------- #
# Exact lookup: what decides whether a tag is valid at all
# --------------------------------------------------------------------------- #


def test_exact_lookup_accepts_only_the_exact_name(tmp_path: Path) -> None:
    """A prefix API answers with every tag *containing* the string.

    Moebooru's ``?name=`` is a substring match, so `long_hair` comes back with
    `_long_hair` and `absurdly_long_hair` first. Accepting the first row would
    silently search for a different tag.
    """
    body = json.dumps(
        [
            {"name": "_long_hair", "count": 900},
            {"name": "absurdly_long_hair", "count": 12},
            {"name": "long_hair", "count": 155949},
        ]
    )
    session = _Session(default=_Response(body))
    resolver = _resolver(session, tmp_path)

    result = _run(resolver.exact_tag("konachan", "long_hair"))
    assert result == "long_hair"
    assert _run(resolver.exact_tag("konachan", "zzznope")) is None


def test_exact_lookup_returns_unknown_when_the_board_cannot_be_asked(tmp_path: Path) -> None:
    """HTTP 401 is not evidence that the tag is absent."""
    session = _Session(default=_Response("", status=401))
    resolver = _resolver(session, tmp_path)
    assert _run(resolver.exact_tag("gelbooru", "1girl")) is R._UNKNOWN


def test_xml_tag_apis_need_credentials(tmp_path: Path) -> None:
    """gelbooru answers its tag endpoint with 401 and rule34 with an error doc.

    Without the key every tag looks absent, so the resolver has to send it.
    """
    settings = _Settings({"gelbooru": {"api_key": "KEY", "user_id": "42"}})
    xml = '<tags count="1"><tag><name>1girl</name><count>9982562</count></tag></tags>'
    session = _Session(default=_Response(xml))
    resolver = _resolver(session, tmp_path, settings)

    assert _run(resolver.exact_tag("gelbooru", "1girl")) == "1girl"
    assert "api_key=KEY" in session.urls[0]
    assert "user_id=42" in session.urls[0]


def test_an_unkeyed_board_is_asked_without_credentials(tmp_path: Path) -> None:
    session = _Session(default=_Response('[{"name": "cat", "count": 5}]'))
    resolver = _resolver(session, tmp_path, _Settings({"gelbooru": {"api_key": "KEY"}}))
    _run(resolver.exact_tag("safebooru", "cat"))
    assert "api_key" not in session.urls[0]


def test_xml_exact_reads_both_attribute_and_element_forms(tmp_path: Path) -> None:
    """xbooru puts the tag in attributes, gelbooru in child elements."""
    attributes = '<tags><tag type="0" count="341419" name="1girl" id="1"/></tags>'
    elements = "<tags><tag><id>1</id><name>1girl</name><count>5</count></tag></tags>"
    for body in (attributes, elements):
        session = _Session(default=_Response(body))
        resolver = _resolver(session, tmp_path / body[:3].replace("<", "a").replace(">", "b"))
        assert _run(resolver.exact_tag("safebooru", "1girl")) == "1girl"


# --------------------------------------------------------------------------- #
# The tiers, in order
# --------------------------------------------------------------------------- #


def test_cross_engine_maps_1girl_to_zerochan_solo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tag the board does not use is answered by the mapping table."""
    session = _Session(default=_Response("", status=404))
    resolver = _resolver(session, tmp_path)

    async def no_such_tag(_tag: str):
        return None

    monkeypatch.setattr(resolver, "_zerochan_tag", no_such_tag)
    item = _run(resolver.resolve_tag("zerochan", "1girl"))
    assert (item.resolved, item.how) == ("Solo", "cross-engine")


def test_cross_engine_maps_1girl_to_e621_female(tmp_path: Path) -> None:
    session = _Session(default=_Response("[]"))
    resolver = _resolver(session, tmp_path)
    item = _run(resolver.resolve_tag("e621", "1girl"))
    assert (item.resolved, item.how) == ("female", "cross-engine")


def test_meta_tags_are_passed_through_untouched(tmp_path: Path) -> None:
    session = _Session(default=_Response("[]"))
    resolver = _resolver(session, tmp_path)
    for tag in ("rating:safe", "order:score", "score:>10"):
        item = _run(resolver.resolve_tag("konachan", tag))
        assert (item.resolved, item.how) == (tag, "unchanged")
    assert session.urls == []  # a meta tag is never asked about


def test_a_tag_the_board_lacks_is_reported_not_dropped(tmp_path: Path) -> None:
    session = _Session(default=_Response("[]"))
    resolver = _resolver(session, tmp_path)
    item = _run(resolver.resolve_tag("yandere", "zzznope"))
    assert item.resolved == ""
    assert item.how == "missing"
    assert not item.ok


def test_an_unreachable_board_leaves_the_tag_as_typed(tmp_path: Path) -> None:
    """The tag must reach the search even if the tag API did not answer."""
    session = _Session(default=_Response("", status=500))
    resolver = _resolver(session, tmp_path)
    item = _run(resolver.resolve_tag("konachan", "cat"))
    assert (item.resolved, item.how) == ("cat", "unknown")
    assert item.unknown


# --------------------------------------------------------------------------- #
# resolve() over a whole query
# --------------------------------------------------------------------------- #


def test_resolve_keeps_reports_and_rebuilds_the_tag_string(tmp_path: Path) -> None:
    body = json.dumps([{"name": "solo", "count": 10}])
    session = _Session(default=_Response(body))
    resolver = _resolver(session, tmp_path)

    resolution = _run(resolver.resolve("e621", "1girl solo zzznope"))
    # 1girl is not an e621 tag, so the mapping table answers "female"; solo is
    # the board's own; zzznope is dropped and reported.
    assert resolution.tags == "female solo"
    assert [item.original for item in resolution.items] == ["1girl", "solo", "zzznope"]
    kinds = {item.original: item.how for item in resolution.items}
    assert kinds["1girl"] == "cross-engine"
    assert kinds["solo"] == "board"
    assert kinds["zzznope"] == "missing"
    # The summary must name what could not be mapped.
    text = "\n".join(resolution.summary_lines())
    assert "zzznope" in text


def test_missing_excludes_tags_that_were_never_answered_for(tmp_path: Path) -> None:
    session = _Session(default=_Response("", status=500))
    resolver = _resolver(session, tmp_path)
    resolution = _run(resolver.resolve("konachan", "cat"))
    assert resolution.missing == []
    assert resolution.items[0].unknown


# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #


def test_a_resolved_tag_is_cached_per_board(tmp_path: Path) -> None:
    body = json.dumps([{"name": "cat", "count": 5}])
    session = _Session(default=_Response(body))
    cache = R.TagCache(tmp_path / "tags.sqlite3")
    resolver = R.TagResolver(_Settings(), cache=cache)
    resolver._session = session

    first = _run(resolver.resolve_tag("konachan", "cat"))
    assert first.resolved == "cat"
    asked = len(session.urls)

    second = _run(resolver.resolve_tag("konachan", "cat"))
    assert second.resolved == "cat"
    assert len(session.urls) == asked  # answered from the cache


def test_a_dead_tag_is_cached_so_it_is_not_asked_twice(tmp_path: Path) -> None:
    session = _Session(default=_Response("[]"))
    cache = R.TagCache(tmp_path / "tags.sqlite3")
    resolver = R.TagResolver(_Settings(), cache=cache)
    resolver._session = session

    _run(resolver.resolve_tag("yandere", "zzznope"))
    asked = len(session.urls)
    _run(resolver.resolve_tag("yandere", "zzznope"))
    assert len(session.urls) == asked


def test_an_unreachable_board_is_not_cached(tmp_path: Path) -> None:
    """Caching a 500 would keep the tag broken for a week."""
    session = _Session(default=_Response("", status=500))
    cache = R.TagCache(tmp_path / "tags.sqlite3")
    resolver = R.TagResolver(_Settings(), cache=cache)
    resolver._session = session

    _run(resolver.resolve_tag("konachan", "cat"))
    assert _run(cache.get("konachan", "cat")) is None

    # Once the board answers, the tag resolves normally.
    session.default = _Response('[{"name": "cat", "count": 5}]')
    assert _run(resolver.resolve_tag("konachan", "cat")).resolved == "cat"


def test_the_cache_honours_its_ttl(tmp_path: Path) -> None:
    cache = R.TagCache(tmp_path / "tags.sqlite3", ttl=-1.0)  # already expired
    _run(cache.put("konachan", "cat", "cat", "board"))
    assert _run(cache.get("konachan", "cat")) is None


def test_cache_clear_forgets_everything(tmp_path: Path) -> None:
    cache = R.TagCache(tmp_path / "tags.sqlite3")
    _run(cache.put("konachan", "cat", "cat", "board"))
    _run(cache.clear())
    assert _run(cache.get("konachan", "cat")) is None


def test_resolve_for_providers_answers_each_board_separately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fan-out asks each board for itself, in its own terms.

    ``resolve_for_providers`` builds its own resolvers, so the stub is patched
    onto the class rather than an instance — otherwise the test would reach the
    real boards.
    """
    body = json.dumps([{"name": "cat", "count": 5}])
    session = _Session(default=_Response(body))
    cache = R.TagCache(tmp_path / "tags.sqlite3")

    async def stub_http(_self):
        return session

    monkeypatch.setattr(R.TagResolver, "_http", stub_http)

    result = _run(R.resolve_for_providers(["konachan", "zerochan"], "cat", settings=_Settings(), cache=cache))

    assert set(result) == {"konachan", "zerochan"}
    assert result["konachan"].items[0].how == "board"
    # Zerochan answers its tag API with a redirect the stub does not emulate, so
    # its tag reads as missing rather than as a board hit.
    assert result["zerochan"].items[0].how in {"missing", "board"}


def test_resolve_for_providers_maps_through_the_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session(default=_Response("[]"))

    async def stub_http(_self):
        return session

    monkeypatch.setattr(R.TagResolver, "_http", stub_http)
    cache = R.TagCache(tmp_path / "tags.sqlite3")

    result = _run(R.resolve_for_providers(["e621", "e926"], "1girl", settings=_Settings(), cache=cache))

    assert result["e621"].items[0].resolved == "female"
    assert result["e926"].items[0].resolved == "female"
