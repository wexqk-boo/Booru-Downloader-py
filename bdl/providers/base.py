"""Provider base class and the parsers for the API shapes.

Shapes:
- gelbooru  — `file_url` + `preview_url` (rule34, gelbooru, xbooru, safebooru);
- gelbooru_legacy — `directory` + `image`, the URL is assembled (tbib);
- moebooru  — `file_url`, pages are 1-based (konachan, yande.re, sakugabooru);
- danbooru  — nested `file`/`preview` objects, `file_url` as a fallback;
- zerochan  — a JSON envelope with `items`, and no `full` field in the listing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, quote_plus

from ..models import Post

#: Moebooru boards reject a query outright past this many whitespace separated
#: tags: ``{"success": false, "reason": "Error: You cannot search for more than
#: 6 tags at a time"}``. Only *plain* tags are checked — meta tags such as
#: ``rating:safe`` or the ``order:score`` ranking tag travel the same way and
#: each one costs a slot as well, which is why they are counted here.
MOEBOORU_MAX_TAGS = 6

#: Zerochan never reports the file extension of a post, so the downloader walks
#: these candidates in order. Images are served as jpg/png/webp/gif only.
ZEROCHAN_EXTENSIONS: tuple[str, ...] = ("jpg", "png", "webp", "gif")

_ZEROCHAN_STATIC = "https://static.zerochan.net"

#: The boards answer a failed search with HTTP 200 and this envelope.
_XML_ERROR_RE = re.compile(r"<response[^>]*reason=\"([^\"]*)\"", re.IGNORECASE)


class ProviderError(RuntimeError):
    """The source is unreachable or returned an unexpected response."""


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """How one search turns into a request, plus where its sorting happens.

    Building the URL and deciding who sorts the result must agree — a plan that
    silently dropped an ``order:score`` tag while the caller still expected the
    board to rank by score would hand back an unsorted first page — so both read
    the same value instead of each re-deriving it.
    """

    tags: str = ""
    sort_param: str = ""
    #: The board itself returns the page in score order.
    board_sorted: bool = False
    #: The caller has to re-rank the page itself: ``"score"``, or ``""``.
    client_sort: str = ""
    #: Ratings the caller has to enforce itself, because a ``rating:`` tag was
    #: dropped from the query. Empty means the board filtered as asked.
    rating_filter: frozenset[str] = frozenset()
    #: Client-side ranking only makes sense over a single page — score order is
    #: not something the board can page through — so the caller stops after one.
    single_page: bool = False
    #: Tags left out of the request because the board would have rejected the
    #: query outright; reported to the user instead of silently vanishing.
    dropped_tags: int = 0


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


def _xml_error(payload: str) -> str | None:
    """The board's own message when it answers 200 with an XML error envelope."""
    match = _XML_ERROR_RE.search(payload)
    return match.group(1).strip() if match else None


def _drop_meta_tags(tags: str) -> tuple[str, frozenset[str]]:
    """Remove the meta tags that cost a Moebooru search a slot.

    Rating and ordering are applied locally instead, because Moebooru counts
    every one of them against its six-tag budget together with the user's own
    tags — the last tag typed was regularly the one that tipped the query over.
    Returns the remaining tags and the ratings that were asked for, so the
    caller can still honour them after the request comes back.
    """
    kept: list[str] = []
    ratings: set[str] = set()
    for tag in tags.split():
        lowered = tag.lower()
        if lowered.startswith("rating:"):
            rating = lowered.partition(":")[2].strip()
            if rating:
                ratings.add(rating)
            continue
        if lowered.startswith(("order:", "status:")):
            continue
        kept.append(tag)
    return " ".join(kept), frozenset(ratings)


def _score_key(post: Post) -> tuple[int, str]:
    """Sort by score, best first; posts without a score sink to the bottom."""
    return (-(post.score or 0), post.uid)


@dataclass(frozen=True, slots=True)
class Provider:
    key: str
    title: str
    base_url: str
    endpoint: str
    short: str = ""
    kind: str = "gelbooru"  # gelbooru | gelbooru_legacy | moebooru | danbooru | zerochan
    auth_style: str = "user_id"  # user_id | login | none
    key_required: bool = False
    page_param: str = "pid"
    page_base: int = 0
    #: Moebooru boards expose the week's best work at their own endpoint. It
    #: takes no tags and ignores paging, so it is only useful for a bare "top".
    popular_endpoint: str = ""
    #: Extra HTTP status the board uses for a normal answer. Zerochan replies
    #: 500 to a perfectly good listing, so a bare status check would call every
    #: request a failure.
    extra_ok_statuses: frozenset[int] = frozenset()
    #: Request headers this board needs beyond the client's defaults.
    headers: tuple[tuple[str, str], ...] = ()

    @property
    def label(self) -> str:
        return self.short or self.key

    def popular_url(self, creds: dict[str, str]) -> str:
        """URL of the board's own "best of the last week" listing, if it has one."""
        url = self.popular_endpoint
        api_key = creds.get("api_key", "").strip()
        login = creds.get("login", "").strip()
        if api_key and login:
            separator = "&" if "?" in url else "?"
            url += f"{separator}login={quote_plus(login)}&api_key={quote_plus(api_key)}"
        return url

    def request_headers(self) -> dict[str, str]:
        """Headers every request to this board should carry."""
        return dict(self.headers)

    # -- URL ----------------------------------------------------------------
    def query_plan(self, tags: str, order: str = "") -> QueryPlan:
        """Decide what the request says and who ends up doing the ranking.

        Only the first page carries any weight on the boards this class cannot
        sort through the request itself; both facts travel together in the plan
        so the caller never has to re-derive one from the other.
        """
        if order != "top":
            if self.kind == "moebooru":
                # Even without a "top", rating: tags cost a slot, and more than
                # six plain tags is an outright error, so the same local
                # handling applies; the tags the user typed are never silently
                # dropped without being counted.
                plain, ratings = _drop_meta_tags(tags)
                kept = plain.split()[:MOEBOORU_MAX_TAGS]
                dropped = max(0, len(plain.split()) - MOEBOORU_MAX_TAGS)
                if ratings or dropped:
                    return QueryPlan(
                        tags=" ".join(kept),
                        rating_filter=ratings,
                        dropped_tags=dropped,
                    )
            return QueryPlan(tags=tags)

        if self.kind == "moebooru":
            # Moebooru sorts by score through a *tag* — ``order:score`` — and
            # silently ignores an ``order=`` query argument (the same query
            # returns unsorted posts with either). It has no usable ``date:>=``
            # filter, so a rolling week just returns nothing; the honest top is
            # over all time, and ``popular_endpoint`` covers the weekly case.
            #
            # That tag costs one of the six slots, and so does ``rating:``, so
            # both are resolved locally: with them stripped the user never has
            # to count tags against a limit they cannot see.
            plain, ratings = _drop_meta_tags(tags)
            if len(plain.split()) < MOEBOORU_MAX_TAGS:
                return QueryPlan(
                    tags=f"{plain} order:score".strip(),
                    board_sorted=True,
                    rating_filter=ratings,
                )
            # A full house of plain tags: the six slots belong to the user, so
            # the ranking happens here over the single page the board allows.
            # Anything past the sixth tag is dropped rather than sent, because
            # the board answers a seven-tag query with an error object instead
            # of results — a partial search beats no search.
            words = plain.split()
            kept = words[:MOEBOORU_MAX_TAGS]
            return QueryPlan(
                tags=" ".join(kept),
                client_sort="score",
                rating_filter=ratings,
                single_page=True,
                dropped_tags=max(0, len(words) - MOEBOORU_MAX_TAGS),
            )

        if self.kind == "zerochan":
            # Zerochan ranks by popularity with ``s=fav``; there is no rating
            # scale on the site at all.
            return QueryPlan(tags=tags, sort_param="&s=fav", board_sorted=True)

        if self.kind == "danbooru":
            return QueryPlan(tags=tags, sort_param="&search%5Border%5D=score", board_sorted=True)

        return QueryPlan(tags=tags, sort_param="&sort=score:desc", board_sorted=True)

    def build_url(self, tags: str, page: int, limit: int, creds: dict[str, str], order: str = "") -> str:
        plan = self.query_plan(tags, order)

        if self.kind == "moebooru":
            url = f"{self.endpoint}?tags={quote_plus(plan.tags)}&page={max(page, 1)}&limit={limit}{plan.sort_param}"
            api_key = creds.get("api_key", "").strip()
            login = creds.get("login", "").strip()
            if api_key and login:
                url += f"&login={quote_plus(login)}&api_key={quote_plus(api_key)}"
            return url

        if self.kind == "zerochan":
            # Zerochan puts the query in the *path*, not a parameter, and joins
            # several tags with a comma — a space there is a 301 and an
            # underscore is a 404, so ``a,b`` is the only form that searches.
            query = ",".join(plan.tags.split())
            base = f"{self.endpoint}/{quote(query, safe=',')}" if query else self.endpoint
            return f"{base}?json&p={max(page, 1)}&l={limit}{plan.sort_param}"

        if self.kind == "danbooru":
            # Danbooru pages are 1-based and it spells the sort as a search
            # parameter rather than a plain query argument.
            return f"{self.endpoint}?tags={quote_plus(plan.tags)}&limit={limit}&page={max(page, 1)}{plan.sort_param}"

        offset = max(page - 1 + self.page_base, 0)
        url = f"{self.endpoint}&tags={quote_plus(plan.tags)}&{self.page_param}={offset}&limit={limit}{plan.sort_param}"
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
    #: List-valued envelope keys, in the order they are looked for. Boards in
    #: the same family disagree: gelbooru uses ``post``, e621 ``posts`` and
    #: zerochan ``items``.
    _ITEMS_FIELDS: tuple[str, ...] = ("post", "posts", "items")

    def raw_items(self, payload: Any) -> list[Any]:
        """Raw items of a response, before broken posts are dropped."""
        if isinstance(payload, str):
            # Boards answer 200 with a JSON or XML string carrying the error
            # text — an abusive-paging notice, an offline API, a bad tag.
            raise ProviderError(f"{self.title}: {_xml_error(payload) or payload[:200]}")
        if isinstance(payload, dict):
            for field in self._ITEMS_FIELDS:
                value = payload.get(field)
                if isinstance(value, list):
                    return value
            # A single-record answer is still one post.
            return [payload]
        if isinstance(payload, list):
            return payload
        raise ProviderError(f"{self.title}: unexpected response format (expected a list of posts)")

    def parse(self, payload: Any) -> list[Post]:
        raw = self.raw_items(payload)
        if self.kind == "zerochan":
            return self._parse_zerochan(raw)
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

    def _parse_zerochan(self, raw: list[Any]) -> list[Post]:
        """Zerochan, whose listing never says what the file is called.

        The listing endpoint reports only ``id``/``width``/``height``/``tags``
        — there is no ``file_url`` and no extension — so the media URL is
        assembled against the static host and every plausible extension is
        offered as a fallback for the downloader to walk. The detail endpoint
        (``/<id>?json``) does carry a ``full`` field, but fetching it would cost
        one request per post, which is not a trade this client wants to make.
        """
        posts: list[Post] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            post_id = str(item.get("id") or "")
            if not post_id:
                continue
            # `primary` (detail endpoint) and `tag` (listing) are the same
            # value under two names: the picture's headline tag.
            primary = str(item.get("primary") or item.get("tag") or "").strip()
            tags = item.get("tags")
            tag_values = tuple(str(tag).strip() for tag in tags if str(tag).strip()) if isinstance(tags, list) else ()
            if primary and not any(tag.lower() == primary.lower() for tag in tag_values):
                tag_values = (primary, *tag_values)

            explicit = _absolute(self.base_url, item.get("full") or item.get("file_url"))
            candidates: list[str] = []
            if explicit:
                candidates.append(explicit)
            # Gallery-dl's naming, which the static host accepts verbatim and
            # which does not depend on the headline tag being spelled right.
            candidates.extend(f"{_ZEROCHAN_STATIC}/.full.{post_id}.{ext}" for ext in ZEROCHAN_EXTENSIONS)
            # An old detail-style answer spells the tag out; keep it as a last
            # resort for the rare post whose id-only name is not served.
            if primary:
                slug = primary.replace(" ", ".")
                candidates.extend(f"{_ZEROCHAN_STATIC}/{slug}.full.{post_id}.{ext}" for ext in ZEROCHAN_EXTENSIONS)

            deduped = tuple(dict.fromkeys(candidates))
            posts.append(
                Post(
                    id=post_id,
                    source=self.key,
                    file_url=deduped[0],
                    preview_url=_absolute(self.base_url, item.get("thumbnail") or item.get("medium")),
                    tags=tag_values,
                    # Zerochan has no rating scale at all; the site is SFW.
                    rating="safe",
                    width=_to_int(item.get("width")),
                    height=_to_int(item.get("height")),
                    score=_to_int(item.get("fav") or item.get("score")),
                    file_size=_to_int(item.get("size")),
                    alternate_urls=deduped[1:],
                )
            )
        return posts

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
