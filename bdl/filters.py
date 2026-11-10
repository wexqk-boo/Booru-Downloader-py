"""Tag sanitising and aspect-ratio selection.

Boards canonicalise tags to lowercase with underscores, so every comparison
here happens on the canonical form. Matching is deliberately fuzzy on
composites: ``loli_focus`` and ``girl_loli`` both hit ``loli``. Exact matching
used to let those through.
"""

from __future__ import annotations

import re

from .models import Post

MAX_TAGS = 24
MAX_QUERY_LENGTH = 300

_CANONICAL_RE = re.compile(r"[^\w]+", re.UNICODE)
_ALLOWED_TAG_RE = re.compile(r"^-?[\w:().'!?&+\-]+$", re.UNICODE)
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)

# Content filtered regardless of the user's own blacklist. Kept short on
# purpose: this runs on every query and is not meant to be a taste filter.
FILTERED_TAGS: frozenset[str] = frozenset({"scat", "necrophilia", "bestiality", "snuff"})

# Sample the middle of the "portrait" range: 2:3 is 0.667, 3:4 is 0.75, and we
# allow a little slack past square so near-1:1 crops still count as avatars.
WALLPAPER_ASPECT: tuple[float, float] = (1.5, 1.8)
AVATAR_ASPECT: tuple[float, float] = (0.6, 1.05)
#: How far a width:height shorthand is widened, so 16:9 does not mean 1.7778 exactly.
ASPECT_TOLERANCE = 0.06


class QueryRejected(ValueError):
    """The query failed validation and must never reach a board."""


def canonical(tag: str) -> str:
    return _CANONICAL_RE.sub("_", tag.strip().lower()).strip("_")


def split_tags(tags: str) -> list[str]:
    return [chunk for chunk in tags.replace(",", " ").split() if chunk]


def tag_hits(tag: str, patterns: frozenset[str]) -> bool:
    """True when *tag* is a pattern, or a composite containing it."""
    if tag in patterns:
        return True
    return any(tag.startswith(pattern + "_") or tag.endswith("_" + pattern) for pattern in patterns)


def aspect_ratio(post: Post) -> float | None:
    """width / height, or None when the board did not report dimensions."""
    if not post.width or not post.height:
        return None
    return post.width / post.height


def aspect_matches(post: Post, low: float, high: float) -> bool:
    """Whether the post fits the ratio window. Posts without sizes never do."""
    ratio = aspect_ratio(post)
    return ratio is not None and low <= ratio <= high


def aspect_state(post: Post, low: float, high: float) -> bool | None:
    """Match result: True/False, or None when the board omitted dimensions."""
    ratio = aspect_ratio(post)
    if ratio is None:
        return None
    return low <= ratio <= high


def _is_float(text: str) -> bool:
    try:
        float(text.strip().replace(",", "."))
    except ValueError:
        return False
    return True


def parse_aspect_range(value: str) -> tuple[float, float]:
    """Parse ``MIN:MAX`` in any of the spellings people actually type.

    Accepts ``1.5:1.8``, ``1.5/1.8``, ``1,5:1,8`` and reversed bounds (``1.8:1.5``
    clearly describes the same window). A notation whose parts are both >= 2 is
    read as a width:height pair — ``16:9`` means the ratio 1.78, not the window
    9..16 — and is widened by :data:`ASPECT_TOLERANCE`, because an exact ratio
    would reject every real image.
    """
    text = value.strip().replace("…", ":").replace("—", ":").replace("–", ":")
    parts: list[str] = []
    for sep in (":", "/", ";"):
        if sep in text:
            parts = text.split(sep, 1)
            break
    else:
        # A lone comma is ambiguous: it separates bounds in "1.5,1.8" but is a
        # decimal mark in "1,5". Treat it as a separator only if both sides
        # parse as numbers on their own.
        if text.count(",") == 1:
            left, _, right = text.partition(",")
            if _is_float(left) and _is_float(right):
                parts = [left, right]
        if not parts and "," in text:
            parts = [text.replace(",", ".")]

    if len(parts) != 2:
        raise ValueError(f"expected MIN:MAX, got {value!r}")

    try:
        first, second = (float(part.strip().replace(",", ".")) for part in parts)
    except ValueError:
        raise ValueError(f"{value!r} does not contain two numbers") from None
    if first <= 0 or second <= 0:
        raise ValueError("aspect ratios must be positive")
    if first >= 2:
        # Width:height shorthand ("16:9", "3:4"): one ratio, not a window.
        ratio = first / second
        return ratio * (1 - ASPECT_TOLERANCE), ratio * (1 + ASPECT_TOLERANCE)
    low, high = (first, second) if first <= second else (second, first)
    return low, high


def dedupe_posts(posts: list[Post]) -> list[Post]:
    """Drop repeats by uid, keeping the first occurrence."""
    seen: set[str] = set()
    unique: list[Post] = []
    for post in posts:
        if post.uid not in seen:
            seen.add(post.uid)
            unique.append(post)
    return unique


def normalize_tags(query: str) -> str:
    """Turn free-form user input into a tag string the boards accept.

    Russian slang and aliases are resolved here, so ``ножки`` becomes a real
    tag. Anything left in Cyrillic afterwards is an error: no board indexes it.
    """
    cleaned = " ".join(query.strip().lower().split())
    if not cleaned:
        return ""
    if len(cleaned) > MAX_QUERY_LENGTH:
        raise QueryRejected(f"Query too long (max {MAX_QUERY_LENGTH} characters)")
    if (tokens := split_tags(cleaned)) and len(tokens) > MAX_TAGS:
        raise QueryRejected(f"Too many tags (max {MAX_TAGS})")
    if not tokens:
        return ""

    from .tags.catalog import ALIASES, RU_SLANG

    negated = cleaned.startswith("-")
    phrase = canonical(cleaned[1:] if negated else cleaned)
    # A whole multi-word alias wins over token-by-token translation:
    # "кошачьи уши" is cat_ears, not "cat" + "ears".
    if phrase_alias := ALIASES.get(phrase) or RU_SLANG.get(phrase):
        tokens = ["-" + phrase_alias if negated else phrase_alias]
    else:
        tokens = [_translate(token) for token in tokens]

    if leftover := [token for token in tokens if _CYRILLIC_RE.search(token.lstrip("-"))]:
        raise QueryRejected(f"Boards are indexed in Latin script; could not translate {leftover[0]!r}")
    for token in tokens:
        if not _ALLOWED_TAG_RE.match(token):
            raise QueryRejected(f"Invalid tag: {token!r}")

    # Negated tags express what the user does *not* want, so they must not
    # trip the content filter.
    positive = {canonical(token) for token in tokens if not token.startswith("-")}
    if any(tag_hits(tag, FILTERED_TAGS) for tag in positive):
        raise QueryRejected("Query rejected: this content is filtered")

    return " ".join(tokens)


def _translate(token: str) -> str:
    from .tags.catalog import ALIASES, RU_SLANG

    negated = token.startswith("-")
    key = canonical(token.lstrip("-"))
    resolved = ALIASES.get(key) or RU_SLANG.get(key) or token
    return "-" + resolved if negated and resolved != token else resolved
