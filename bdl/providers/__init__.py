"""Sources for the CLI — a self-contained registry.

Gelbooru, rule34, xbooru, safebooru, danbooru, konachan and yandere are the
base sources. Tbib, e621, e926 and konachan.net were added separately.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from ..models import Post
from .base import Provider, ProviderError, _absolute, _to_int
from .client import ProviderClient, ProviderResult

# base sources (gelbooru family + moebooru)
BOT_PROVIDERS: dict[str, Provider] = {
    "rule34": Provider(
        key="rule34",
        key_required=True,
        short="Rule34",
        title="Rule 34 💜",
        base_url="https://rule34.xxx",
        endpoint="https://api.rule34.xxx/index.php?page=dapi&s=post&q=index&json=1",
        kind="gelbooru",
        auth_style="user_id",
    ),
    "gelbooru": Provider(
        key="gelbooru",
        key_required=True,
        short="Gelbooru",
        title="Gelbooru 💙",
        base_url="https://gelbooru.com",
        endpoint="https://gelbooru.com/index.php?page=dapi&s=post&q=index&json=1",
        kind="gelbooru",
        auth_style="user_id",
    ),
    "xbooru": Provider(
        key="xbooru",
        short="Xbooru",
        title="Xbooru 🖤",
        base_url="https://xbooru.com",
        endpoint="https://xbooru.com/index.php?page=dapi&s=post&q=index&json=1",
        kind="gelbooru",
        auth_style="none",
    ),
    "konachan": Provider(
        key="konachan",
        short="Konachan",
        title="Konachan 🎐",
        base_url="https://konachan.com",
        endpoint="https://konachan.com/post.json",
        kind="moebooru",
        auth_style="login",
        date_filter=True,
    ),
    "yandere": Provider(
        key="yandere",
        short="Yande.re",
        title="Yande.re 🌸",
        base_url="https://yande.re",
        endpoint="https://yande.re/post.json",
        kind="moebooru",
        auth_style="login",
        date_filter=True,
    ),
}

TBIB = Provider(
    key="tbib",
    short="Tbib",
    title="Tbib 🐾",
    base_url="https://tbib.org",
    endpoint="https://tbib.org/index.php?page=dapi&s=post&q=index&json=1",
    kind="gelbooru_legacy",
    auth_style="none",
)


class E621Provider(Provider):
    """e621.net — the API returns ``{"posts": [...]}`` with nested fields.

    Anonymous requests only ever get the newest page (pagination does not work),
    so ``page`` is ignored and the per-request limit can go up to 320. e621
    ratings are letters — ``s``/``q``/``e`` (safe/questionable/explicit) — keep
    that in mind with ``--rating-include``.
    """

    def __init__(
        self,
        *,
        key: str = "e621",
        short: str = "e621",
        title: str = "e621 🦊",
        base_url: str = "https://e621.net",
        endpoint: str = "https://e621.net/posts.json",
    ) -> None:
        super().__init__(
            key=key,
            short=short,
            title=title,
            base_url=base_url,
            endpoint=endpoint,
            kind="gelbooru",
            auth_style="none",
        )

    def build_url(self, tags: str, page: int, limit: int, creds: dict[str, str], order: str = "") -> str:
        query_tags = tags
        if order == "top":
            query_tags = f"{tags} order:score" if tags else "order:score"
        return f"{self.endpoint}?tags={quote_plus(query_tags)}&limit={limit}"

    def raw_items(self, payload: object) -> list[object]:
        if isinstance(payload, dict):
            raw = payload.get("posts")
            if isinstance(raw, list):
                return raw
        return super().raw_items(payload)

    def parse(self, payload: object) -> list[Post]:
        posts: list[Post] = []
        for item in self.raw_items(payload):
            if not isinstance(item, dict):
                continue
            file_info = item.get("file")
            if not isinstance(file_info, dict):
                file_info = {}
            file_url = file_info.get("url") or item.get("file_url")
            if not file_url:
                continue

            preview = item.get("preview")
            preview_url = preview.get("url") if isinstance(preview, dict) else item.get("preview_url")

            tags_obj = item.get("tags")
            if isinstance(tags_obj, dict):
                flat_tags: list[str] = []
                for group in ("general", "species", "character", "copyright", "artist", "meta", "lore"):
                    values = tags_obj.get(group)
                    if isinstance(values, list):
                        flat_tags.extend(str(value) for value in values)
                tag_tuple = tuple(flat_tags)
            else:
                tag_tuple = tuple(str(tags_obj or "").split())

            score = item.get("score")
            if isinstance(score, dict):
                score = score.get("total")

            posts.append(
                Post(
                    id=str(item.get("id", "") or ""),
                    source=self.key,
                    file_url=_absolute(self.base_url, file_url),
                    preview_url=_absolute(self.base_url, preview_url),
                    tags=tag_tuple,
                    rating=(str(item.get("rating") or "") or None),
                    width=_to_int(file_info.get("width")),
                    height=_to_int(file_info.get("height")),
                    score=_to_int(score),
                    file_size=_to_int(file_info.get("size")),
                )
            )
        return posts


PROVIDERS: dict[str, Provider] = {
    name: BOT_PROVIDERS[name] for name in ("gelbooru", "rule34", "xbooru", "konachan", "yandere")
}
PROVIDERS["safebooru"] = Provider(
    key="safebooru",
    short="Safebooru",
    title="Safebooru 🌤",
    base_url="https://safebooru.org",
    endpoint="https://safebooru.org/index.php?page=dapi&s=post&q=index&json=1",
    kind="gelbooru",
    auth_style="none",
)
PROVIDERS["danbooru"] = Provider(
    key="danbooru",
    short="Danbooru",
    title="Danbooru 🍡",
    base_url="https://danbooru.donmai.us",
    endpoint="https://danbooru.donmai.us/posts.json",
    kind="danbooru",
    # Anonymous search works; a login only raises the per-page limit.
    auth_style="login",
)
PROVIDERS["tbib"] = TBIB
PROVIDERS["e621"] = E621Provider()
PROVIDERS["e926"] = E621Provider(
    key="e926",
    short="e926",
    title="e926 🐾",
    base_url="https://e926.net",
    endpoint="https://e926.net/posts.json",
)
PROVIDERS["konachan.net"] = Provider(
    key="konachan.net",
    short="Konachan.net",
    title="Konachan.net 🌼",
    base_url="https://konachan.net",
    endpoint="https://konachan.net/post.json",
    kind="moebooru",
    auth_style="login",
)

ALL = "all"


def resolve_sources(raw: str) -> list[Provider]:
    """Turn ``"all"`` or ``"a,b"`` into a list of providers, without repeats."""
    keys = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
    if not keys:
        raise ProviderError("No sources specified")
    if ALL in keys:
        return list(PROVIDERS.values())
    providers: list[Provider] = []
    seen: set[str] = set()
    for key in keys:
        provider = PROVIDERS.get(key)
        if provider is None:
            available = ", ".join([*PROVIDERS, ALL])
            raise ProviderError(f"Unknown source: {key!r} (available: {available})")
        # A repeated source (`gelbooru,gelbooru`) would download posts twice.
        if key not in seen:
            seen.add(key)
            providers.append(provider)
    return providers


class CliProviderClient(ProviderClient):
    """``ProviderClient`` that uses the local CLI registry."""

    def __init__(self, settings, providers: dict[str, Provider] | None = None, *, max_concurrency: int = 6) -> None:
        super().__init__(settings, max_concurrency=max_concurrency)
        self._providers = dict(providers or PROVIDERS)

    async def search(self, provider_key: str, tags: str, page: int, limit: int, order: str = "") -> ProviderResult:
        provider = self._providers.get(provider_key)
        if provider is None:
            raise ProviderError(f"Unknown source: {provider_key}")
        return await self.search_provider(provider, provider_key, tags, page, limit, order)


__all__ = ["ALL", "PROVIDERS", "TBIB", "CliProviderClient", "E621Provider", "resolve_sources"]
