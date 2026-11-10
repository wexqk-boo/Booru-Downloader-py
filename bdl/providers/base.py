"""Provider base class and the parsers for the API shapes.

Shapes:
- gelbooru  — `file_url` + `preview_url` (rule34, gelbooru, xbooru, safebooru);
- gelbooru_legacy — `directory` + `image`, the URL is assembled (tbib);
- moebooru  — `file_url`, pages are 1-based (konachan, yande.re);
- danbooru  — nested `file`/`preview` objects, `file_url` as a fallback.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

from ..models import Post


class ProviderError(RuntimeError):
    """The source is unreachable or returned an unexpected response."""


def _top_tags(tags: str) -> str:
    """Restrict the query to the last seven days."""
    since = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    return f"{tags} date:>={since}" if tags else f"date:>={since}"


def _absolute(base: str, value: str | None) -> str | None:
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("/"):
        return base.rstrip("/") + value
    if value.startswith(("http://", "https://")):
        return value
    return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class Provider:
    key: str
    title: str
    base_url: str
    endpoint: str
    short: str = ""
    kind: str = "gelbooru"  # gelbooru | gelbooru_legacy | moebooru
    auth_style: str = "user_id"  # user_id | login | none
    key_required: bool = False
    page_param: str = "pid"
    page_base: int = 0
    date_filter: bool = False  # whether the API accepts date: filters in tags

    @property
    def label(self) -> str:
        return self.short or self.key

    # -- URL ----------------------------------------------------------------
    def build_url(self, tags: str, page: int, limit: int, creds: dict[str, str], order: str = "") -> str:
        sort_param = ""
        if order == "top":
            if self.date_filter:
                tags = _top_tags(tags)
            sort_param = "&order=score" if self.kind == "moebooru" else "&sort=score:desc"

        if self.kind == "moebooru":
            url = f"{self.endpoint}?tags={quote_plus(tags)}&page={max(page, 1)}&limit={limit}{sort_param}"
            api_key = creds.get("api_key", "").strip()
            login = creds.get("login", "").strip()
            if api_key and login:
                url += f"&login={quote_plus(login)}&api_key={quote_plus(api_key)}"
            return url

        if self.kind == "danbooru":
            # Danbooru pages are 1-based and it spells the sort as a search
            # parameter rather than a plain query argument.
            order_param = "&search%5Border%5D=score" if order == "top" else ""
            return f"{self.endpoint}?tags={quote_plus(tags)}&limit={limit}&page={max(page, 1)}{order_param}"

        offset = max(page - 1 + self.page_base, 0)
        url = f"{self.endpoint}&tags={quote_plus(tags)}&{self.page_param}={offset}&limit={limit}{sort_param}"
        api_key = creds.get("api_key", "").strip()
        if not api_key or self.auth_style == "none":
            return url
        if self.auth_style == "user_id":
            user_id = creds.get("user_id", "").strip()
            if user_id:
                url += f"&api_key={quote_plus(api_key)}&user_id={quote_plus(user_id)}"
            else:
                url += f"&api_key={quote_plus(api_key)}"
        else:
            login = creds.get("login", "").strip()
            if login:
                url += f"&login={quote_plus(login)}&api_key={quote_plus(api_key)}"
        return url

    # -- parsing ------------------------------------------------------------
    def raw_items(self, payload: Any) -> list[Any]:
        """Raw items of a response, before broken posts are dropped."""
        if isinstance(payload, str):
            # boards answer 200 with a JSON string carrying the error text
            raise ProviderError(f"{self.title}: {payload[:200]}")
        raw = payload.get("post", []) if isinstance(payload, dict) else payload
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            raise ProviderError(f"{self.title}: unexpected response format (expected a list of posts)")
        return raw

    def parse(self, payload: Any) -> list[Post]:
        raw = self.raw_items(payload)
        posts: list[Post] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if self.kind == "gelbooru_legacy":
                post = self._parse_legacy(item)
            elif self.kind == "moebooru":
                post = self._parse_moebooru(item)
            elif self.kind == "danbooru":
                post = self._parse_danbooru(item)
            else:
                post = self._parse_gelbooru(item)
            if post is not None:
                posts.append(post)
        return posts

    def _parse_gelbooru(self, item: dict[str, Any]) -> Post | None:
        file_url = (
            _absolute(self.base_url, item.get("file_url"))
            or _absolute(self.base_url, item.get("sample_url"))
            or _absolute(self.base_url, item.get("image"))
        )
        if not file_url:
            return None
        return Post(
            id=str(item.get("id", "") or file_url),
            source=self.key,
            file_url=file_url,
            preview_url=_absolute(self.base_url, item.get("preview_url")),
            tags=tuple(str(item.get("tags") or "").split()),
            rating=(str(item.get("rating") or "") or None),
            width=_to_int(item.get("width")),
            height=_to_int(item.get("height")),
            score=_to_int(item.get("score")),
            file_size=_to_int(item.get("file_size") or item.get("size")),
        )

    def _parse_legacy(self, item: dict[str, Any]) -> Post | None:
        directory = item.get("directory")
        image = item.get("image")
        if not directory or not image:
            return None
        file_url = f"{self.base_url.rstrip('/')}/images/{directory}/{image}"
        return Post(
            id=str(item.get("id", "") or file_url),
            source=self.key,
            file_url=file_url,
            preview_url=None,
            tags=tuple(str(item.get("tags") or "").split()),
            rating=(str(item.get("rating") or "") or None),
            width=_to_int(item.get("width")),
            height=_to_int(item.get("height")),
            score=_to_int(item.get("score")),
            file_size=_to_int(item.get("file_size") or item.get("size")),
        )

    def _parse_moebooru(self, item: dict[str, Any]) -> Post | None:
        file_url = _absolute(self.base_url, item.get("file_url"))
        if not file_url:
            return None
        return Post(
            id=str(item.get("id", "") or file_url),
            source=self.key,
            file_url=file_url,
            preview_url=_absolute(self.base_url, item.get("preview_url")),
            tags=tuple(str(item.get("tags") or "").split()),
            rating=(str(item.get("rating") or "") or None),
            width=_to_int(item.get("width")),
            height=_to_int(item.get("height")),
            score=_to_int(item.get("score")),
            file_size=_to_int(item.get("file_size") or item.get("size")),
        )

    def _parse_danbooru(self, item: dict[str, Any]) -> Post | None:
        """Danbooru: flat ``file_url`` first, then the ``media_asset`` variants."""
        asset = item.get("media_asset")
        if not isinstance(asset, dict):
            asset = {}

        file_url = _absolute(self.base_url, item.get("file_url"))
        if not file_url:
            for variant in asset.get("variants") or []:
                if isinstance(variant, dict) and variant.get("type") == "original":
                    file_url = _absolute(self.base_url, variant.get("url"))
                    break
        # A banned or still-processing post has no media at all.
        if not file_url:
            return None

        preview_url = _absolute(self.base_url, item.get("preview_file_url"))
        if not preview_url:
            preview_url = _absolute(self.base_url, asset.get("preview_url"))

        file_url_lower = file_url.lower()
        if file_url_lower.endswith((".mp4", ".webm")):
            preview_url = _absolute(self.base_url, item.get("large_file_url")) or preview_url

        return Post(
            id=str(item.get("id", "") or file_url),
            source=self.key,
            file_url=file_url,
            preview_url=preview_url,
            tags=tuple(str(item.get("tag_string") or "").split()),
            rating=(str(item.get("rating") or "") or None),
            width=_to_int(item.get("image_width") or asset.get("image_width")),
            height=_to_int(item.get("image_height") or asset.get("image_height")),
            score=_to_int(item.get("score")),
            file_size=_to_int(item.get("file_size")),
        )
