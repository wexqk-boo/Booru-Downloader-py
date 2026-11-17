"""Provider-level tests: query planning, parsing and error messages.

Nothing here touches the network: every request is either built as a URL or
answered by a stub client, so the suite stays offline.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from bdl.config import credential_prefix, load_credentials, load_dotenv
from bdl.models import Post
from bdl.providers import CREDENTIAL_PREFIXES, PROVIDERS, ProviderError, resolve_sources
from bdl.providers.base import MOEBOORU_MAX_TAGS, ZEROCHAN_EXTENSIONS
from bdl.providers.client import ProviderClient, _loads_lenient, _rating_allowed, _status_message


# -- registry ---------------------------------------------------------------
def test_the_new_boards_are_registered() -> None:
    assert {"sakugabooru", "zerochan"} <= set(PROVIDERS)
    assert PROVIDERS["sakugabooru"].kind == "moebooru"
    assert PROVIDERS["zerochan"].kind == "zerochan"
    # Realbooru was withdrawn: its DAPI is disabled server-side, so it could
    # only ever answer with an error.
    assert "realbooru" not in PROVIDERS


def test_boards_without_keys_do_not_claim_to_need_them() -> None:
    for key in ("xbooru", "safebooru", "zerochan"):
        provider = PROVIDERS[key]
        assert not provider.key_required
        assert provider.auth_style == "none"


def test_all_still_resolves_every_board() -> None:
    assert len(resolve_sources("all")) == len(PROVIDERS)
    assert resolve_sources("zerochan,safebooru") == [PROVIDERS["zerochan"], PROVIDERS["safebooru"]]


# -- credentials ------------------------------------------------------------
def _no_ambient_env(monkeypatch) -> None:
    """Credentials resolve from the environment first, so clear the ambient set.

    Without this a developer with a real ``.env`` sourced into their shell would
    see these tests fail for reasons that have nothing to do with the code.
    """
    for key in list(os.environ):
        if key.endswith(("_API_KEY", "_USER_ID", "_LOGIN")):
            monkeypatch.delenv(key, raising=False)


def test_a_dotted_board_name_reads_an_underscored_env_key(tmp_path, monkeypatch) -> None:
    """`konachan.net` must not look for `KONACHAN.NET_API_KEY`.

    A dot in the variable name is not something anyone writes in a .env file,
    so the board was queried anonymously while the configured key sat unused.
    """
    _no_ambient_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("KONACHAN_NET_API_KEY=secret\nKONACHAN_NET_LOGIN=alice\n", encoding="utf-8")
    credentials = load_credentials(env)
    assert credentials["konachan.net"].api_key == "secret"
    assert credentials["konachan.net"].login == "alice"
    # A dotted prefix is never derived from the key itself.
    assert credential_prefix("konachan.net") == "KONACHAN_NET"
    assert CREDENTIAL_PREFIXES["konachan.net"] == "KONACHAN_NET"
    # Boards without an alias keep the plain upper-cased name.
    assert credential_prefix("sakugabooru") == "SAKUGABOORU"


def test_sakugabooru_credentials_come_from_its_own_prefix(tmp_path, monkeypatch) -> None:
    _no_ambient_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("SAKUGABOORU_API_KEY=k\nSAKUGABOORU_LOGIN=alice\n", encoding="utf-8")
    credentials = load_credentials(env)
    assert credentials["sakugabooru"].api_key == "k"
    assert credentials["sakugabooru"].login == "alice"


def test_dotenv_parsing_keeps_values_intact(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# comment\nexport ZEROCHAN_API_KEY='quoted'\nSAKUGABOORU_LOGIN=bob  # trailing\n",
        encoding="utf-8",
    )
    parsed = load_dotenv(env)
    assert parsed["ZEROCHAN_API_KEY"] == "quoted"
    assert parsed["SAKUGABOORU_LOGIN"] == "bob"


# -- zerochan ---------------------------------------------------------------
def test_zerochan_joins_tags_with_commas_in_the_path() -> None:
    """A space or an underscore in the path is a redirect or a 404."""
    provider = PROVIDERS["zerochan"]
    assert provider.build_url("cat", 1, 24, {}) == "https://www.zerochan.net/cat?json&p=1&l=24"
    assert (
        provider.build_url("hatsune miku cat", 2, 24, {})
        == "https://www.zerochan.net/hatsune,miku,cat?json&p=2&l=24"
    )
    # No tags at all: the parameter form on the bare host.
    assert provider.build_url("", 1, 24, {}) == "https://www.zerochan.net?json&p=1&l=24"
    # A "top" search ranks by favourites.
    assert provider.build_url("cat", 1, 24, {}, "top").endswith("&s=fav")


def test_zerochan_declares_that_500_is_a_normal_answer() -> None:
    """The listing really is served as an HTTP 500."""
    assert 500 in PROVIDERS["zerochan"].extra_ok_statuses
    headers = PROVIDERS["zerochan"].request_headers()
    assert "bdl/" in headers["User-Agent"]
    assert "zerochan" in headers["Referer"]


def test_zerochan_parses_a_listing_without_a_file_url() -> None:
    """The listing carries no `full`, so the media URL has to be assembled."""
    provider = PROVIDERS["zerochan"]
    payload = {
        "items": [
            {
                "id": 4595000,
                "width": 1920,
                "height": 1080,
                "thumbnail": "https://static.zerochan.net/thumb.jpg",
                "tag": "Hatsune Miku",
                "tags": ["Vocaloid", "Hatsune Miku"],
            }
        ]
    }
    (post,) = provider.parse(payload)
    assert post.id == "4595000"
    assert post.source == "zerochan"
    assert post.rating == "safe"  # zerochan has no rating scale
    assert post.width == 1920 and post.height == 1080
    # Every extension is offered, because the listing never says which one.
    assert post.file_url == f"https://static.zerochan.net/.full.4595000.{ZEROCHAN_EXTENSIONS[0]}"
    assert len(post.alternate_urls) == len(ZEROCHAN_EXTENSIONS) + len(ZEROCHAN_EXTENSIONS) - 1
    assert any(url.endswith(".png") for url in post.alternate_urls)
    # The headline tag is not duplicated when the tag list already has it.
    assert post.tags == ("Vocaloid", "Hatsune Miku")


def test_zerochan_prefers_an_explicit_full_url() -> None:
    """A detail-style answer carries `full`; it must win over the guess."""
    provider = PROVIDERS["zerochan"]
    payload = {"items": [{"id": 7, "full": "https://static.zerochan.net/x.full.7.webp", "tags": []}]}
    (post,) = provider.parse(payload)
    assert post.file_url == "https://static.zerochan.net/x.full.7.webp"
    assert post.file_url not in post.alternate_urls


def test_zerochan_survives_the_stray_brace_it_sends() -> None:
    """A valid listing is not valid JSON: it opens with an extra `{`."""
    body = '{\n{\n "items": [ {"id": 1, "width": 2, "height": 3} ]\n}\n<html>trailing</html>'
    parsed = _loads_lenient(body)
    assert parsed is not None
    assert parsed["items"][0]["id"] == 1
    # A plainly broken body still fails, so the anti-bot message is reachable.
    assert _loads_lenient("<html>Just a moment...</html>") is None
    assert _loads_lenient(json.dumps({"items": []})) == {"items": []}


# -- moebooru ---------------------------------------------------------------
@pytest.mark.parametrize("board", ["konachan", "konachan.net", "yandere", "sakugabooru"])
def test_moebooru_never_sends_more_tags_than_the_board_allows(board: str) -> None:
    """More than six whitespace-separated tags is an outright board error."""
    provider = PROVIDERS[board]
    tags = " ".join(f"tag{i}" for i in range(10))
    plan = provider.query_plan(tags, "top")
    assert len(plan.tags.split()) <= MOEBOORU_MAX_TAGS
    assert plan.dropped_tags == 10 - MOEBOORU_MAX_TAGS
    # The plan's own budget is what the URL carries.
    url = provider.build_url(tags, 1, 50, {}, "top")
    assert len(url.split("tags=")[1].split("&")[0].split("+")) <= MOEBOORU_MAX_TAGS


def test_moebooru_keeps_the_order_tag_only_when_a_slot_is_free() -> None:
    provider = PROVIDERS["konachan"]
    roomy = provider.query_plan("miku solo", "top")
    assert roomy.tags == "miku solo order:score"
    assert roomy.board_sorted and not roomy.client_sort

    full = provider.query_plan("a b c d e f", "top")
    assert full.tags == "a b c d e f"  # the six slots belong to the user
    assert full.client_sort == "score" and full.single_page
    assert not full.board_sorted


def test_moebooru_moves_rating_into_a_local_filter() -> None:
    """`rating:` costs a tag slot, so it is enforced after the response."""
    provider = PROVIDERS["konachan"]
    plan = provider.query_plan("miku solo rating:safe", "top")
    assert "rating:" not in plan.tags
    assert plan.rating_filter == frozenset({"safe"})
    # A query with room left keeps the sorting tag alongside the local filter.
    assert plan.tags == "miku solo order:score"


def test_moebooru_rating_filter_matches_every_spelling() -> None:
    assert _rating_allowed("s", frozenset({"safe"}))
    assert _rating_allowed("safe", frozenset({"s"}))
    assert _rating_allowed("q", frozenset({"questionable"}))
    assert _rating_allowed("e", frozenset({"explicit"}))
    assert not _rating_allowed("e", frozenset({"safe"}))
    assert not _rating_allowed(None, frozenset({"safe"}))


def test_moebooru_plain_query_is_passed_through_untouched() -> None:
    """Nothing is rewritten when there is nothing to rewrite."""
    provider = PROVIDERS["konachan"]
    plan = provider.query_plan("miku solo", "")
    assert plan.tags == "miku solo"
    assert not plan.rating_filter and not plan.dropped_tags
    assert "order%3Ascore" not in provider.build_url("miku solo", 1, 50, {})


def test_moebooru_sakugabooru_has_a_weekly_listing() -> None:
    """Sakugabooru is a Moebooru board, so it serves the same weekly page."""
    assert PROVIDERS["sakugabooru"].popular_endpoint.endswith("popular_recent.json?period=1w")
    assert PROVIDERS["sakugabooru"].endpoint == "https://www.sakugabooru.com/post.json"


# -- keyless gelbooru error reporting ----------------------------------------
def test_a_keyless_gelbooru_board_sends_no_credentials() -> None:
    provider = PROVIDERS["safebooru"]
    assert provider.endpoint.endswith("page=dapi&s=post&q=index&json=1")
    url = provider.build_url("cat", 1, 50, {})
    assert "api_key" not in url and "user_id" not in url


def test_a_keyless_404_does_not_talk_about_api_keys() -> None:
    """The message item 2 is about: boards without keys must not be blamed."""
    message = _status_message(PROVIDERS["safebooru"], 404)
    assert "API" not in message
    assert "404" in message

    for key in ("xbooru", "safebooru", "zerochan"):
        assert "API" not in _status_message(PROVIDERS[key], 404)

    # A board that really authenticates keeps the useful hint.
    assert "RULE34_API_KEY" in _status_message(PROVIDERS["rule34"], 404)
    assert "RULE34_API_KEY" in _status_message(PROVIDERS["rule34"], 422)
    # ...and a keyless board does not get it on any status.
    assert "API" not in _status_message(PROVIDERS["safebooru"], 400)


def test_a_board_error_object_becomes_its_own_reason() -> None:
    """A non-JSON body carrying a reason is reported as that reason."""
    payload = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<response success="false" reason="Search error: API limited due to abuse."/>'
    )
    with pytest.raises(ProviderError) as excinfo:
        PROVIDERS["safebooru"].raw_items(payload)
    assert "API limited due to abuse" in str(excinfo.value)


def test_gelbooru_order_meta_tag_is_no_longer_sent() -> None:
    """`order:score` on a gelbooru board returns an empty body, not results."""
    for key in ("xbooru", "safebooru", "gelbooru"):
        url = PROVIDERS[key].build_url("cat", 1, 50, {}, "top")
        assert "order" not in url.split("tags=")[1].split("&")[0]
        assert "sort=score" in url


def _stub_session(body: str, status: int = 200):
    """A session double good enough for one `_fetch_json` round trip."""

    class _FakeResponse:
        def __init__(self) -> None:
            self.status = status
            self.headers: dict[str, str] = {}

        async def text(self) -> str:
            return body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> bool:
            return False

    class _FakeSession:
        closed = False

        def get(self, _url, headers=None, allow_redirects=True):
            return _FakeResponse()

    return _FakeSession()


def test_a_non_json_http_body_reaches_the_boards_own_error_message() -> None:
    """A board's XML error document must not be blamed on an anti-bot page.

    The boards answer a broken or throttled query with HTTP 200 and an XML
    reason; that body has to travel to ``raw_items`` so the reader sees what
    the board actually said.
    """
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<response success="false" reason="Search error: API offline"/>'
    )

    async def scenario() -> str:
        client = ProviderClient(_Settings())  # type: ignore[arg-type]
        client._session = _stub_session(body)  # type: ignore[assignment]
        payload = await client._fetch_json("https://example.invalid", PROVIDERS["safebooru"])
        # The raw XML is handed on, so the reason can be extracted downstream.
        assert isinstance(payload, str)
        with pytest.raises(ProviderError) as excinfo:
            PROVIDERS["safebooru"].raw_items(payload)
        return str(excinfo.value)

    message = asyncio.run(scenario())
    assert "API offline" in message
    assert "anti-bot" not in message


def test_an_html_page_without_a_reason_shows_its_opening_text() -> None:
    """A challenge page carries no reason, so the raw opening text is shown."""

    async def scenario() -> str:
        client = ProviderClient(_Settings())  # type: ignore[arg-type]
        client._session = _stub_session("<html><head><title>Just a moment…</title></head></html>")  # type: ignore[assignment]
        payload = await client._fetch_json("https://example.invalid", PROVIDERS["xbooru"])
        with pytest.raises(ProviderError) as excinfo:
            PROVIDERS["xbooru"].raw_items(payload)
        return str(excinfo.value)

    message = asyncio.run(scenario())
    # Nothing is invented about credentials for a board that needs none.
    assert "Just a moment" in message
    assert "API" not in message


# -- client plumbing --------------------------------------------------------
class _Settings:
    cache_ttl = 0.0
    max_cache_entries = 8

    def credentials_for(self, key: str) -> dict[str, str]:
        return {}


def _client(handler, urls: list[str]):
    class _Client(ProviderClient):
        async def _fetch_json(self, url: str, provider) -> object:  # type: ignore[override]
            urls.append(url)
            return handler(len(urls))

    return _Client(_Settings())  # type: ignore[arg-type]


def test_a_capped_moebooru_search_sorts_and_stops_after_one_page() -> None:
    """With the order tag dropped, only the first page is worth fetching."""
    pages: list[int] = []

    def handler(call: int):
        pages.append(call)
        return [
            {"id": call * 10 + i, "tags": "miku", "file_url": f"/i/{call}{i}.png", "score": i}
            for i in range(3)
        ]

    async def scenario() -> tuple[list[int], bool]:
        urls: list[str] = []
        client = _client(handler, urls)
        provider = PROVIDERS["konachan"]
        result = await client.search_provider(provider, "konachan", "a b c d e f", 1, 10, "top")
        return [post.score for post in result.posts], result.exhausted

    scores, exhausted = asyncio.run(scenario())
    assert scores == [2, 1, 0]  # re-ranked locally, best first
    assert exhausted is True  # a locally sorted page is the whole answer
    assert pages == [1]  # no second request


def test_a_local_rating_filter_drops_the_posts_the_board_could_not() -> None:
    def handler(_call: int):
        return [
            {"id": 1, "tags": "miku", "file_url": "/a.png", "score": 5, "rating": "s"},
            {"id": 2, "tags": "miku", "file_url": "/b.png", "score": 9, "rating": "e"},
        ]

    async def scenario() -> list[str]:
        urls: list[str] = []
        client = _client(handler, urls)
        result = await client.search_provider(PROVIDERS["konachan"], "konachan", "miku rating:safe", 1, 10, "top")
        return [post.id for post in result.posts]

    assert asyncio.run(scenario()) == ["1"]


def test_the_post_model_carries_fallback_urls() -> None:
    post = Post(id="1", source="zerochan", file_url="a.jpg", alternate_urls=("a.png",))
    assert post.alternate_urls == ("a.png",)
    # The default keeps the old single-URL behaviour for every other board.
    assert Post(id="2", source="gelbooru", file_url="b.jpg").alternate_urls == ()
