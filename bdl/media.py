"""Media classification: file kind, rating bucket and on-disk layout."""

from __future__ import annotations

from pathlib import Path

from .models import Post

SIDECAR_DIR = "json"

#: Every film lives under this one root folder, whatever its shape: aspect
#: ratio is a still-image notion, and a 16:9 clip buried in ``wallpapers/`` is
#: not where anyone looks for footage.
VIDEO_DIR = "videos"

VIDEO_EXTENSIONS = frozenset({".mp4", ".webm", ".mov", ".avi", ".mkv", ".flv", ".m4v", ".wmv"})
ANIMATION_EXTENSIONS = frozenset({".gif"})

# Board rating vocabularies overlap but disagree on spelling, so each bucket
# lists every alias in the wild. "s" is safe on e621 but sensitive on Danbooru;
# we follow e621 because that is where the single-letter form comes from.
SAFE_RATINGS = frozenset({"safe", "s", "general", "g"})
SUGGESTIVE_RATINGS = frozenset({"questionable", "q", "sensitive"})
NSFW_RATINGS = frozenset({"explicit", "e"})

RATING_BUCKETS: dict[str, frozenset[str]] = {
    "safe": SAFE_RATINGS,
    "suggestive": SUGGESTIVE_RATINGS,
    "nsfw": NSFW_RATINGS,
}

BUCKETS = tuple(RATING_BUCKETS)


def rating_bucket(rating: str | None) -> str:
    """Map a board rating onto one of the three output folders.

    Unknown or missing ratings land in ``nsfw``: hiding adult content in a
    folder named "safe" would be worse than the opposite mistake.
    """
    if rating is None:
        return "nsfw"
    value = rating.strip().lower()
    for bucket, values in RATING_BUCKETS.items():
        if value in values:
            return bucket
    return "nsfw"


def is_video_extension(ext: str) -> bool:
    """True when this extension names a film rather than a still."""
    return ext.lower() in VIDEO_EXTENSIONS


def media_subdir(ext: str) -> str:
    """``videos`` for a film, else ``""``.

    Films used to sit in a ``videos/`` subfolder of the rating bucket; they now
    get a root folder of their own (see :func:`layout_paths`). The name is kept
    because it doubles as the marker for "this is a film".
    """
    return VIDEO_DIR if is_video_extension(ext) else ""


def _strip_query(url: str) -> str:
    return url.lower().split("?", 1)[0].split("#", 1)[0]


def is_video(url: str) -> bool:
    return _strip_query(url).endswith(tuple(VIDEO_EXTENSIONS))


def is_animation(url: str) -> bool:
    return _strip_query(url).endswith(tuple(ANIMATION_EXTENSIONS))


def ratings_for(category: str | None) -> frozenset[str] | None:
    """Rating aliases selected by a category name, or None for "everything"."""
    if not category:
        return None
    return RATING_BUCKETS.get(category.strip().lower())


def bucket_of(post: Post) -> str:
    return rating_bucket(post.rating)


def media_kind(post: Post, aspect_range: tuple[float, float] | None = None, ext: str = "") -> str:
    """Classify a post into wallpapers, avatars, or root bucket.

    A film is never a wallpaper or an avatar: its shape decides nothing, so it
    reports no kind and the layout puts it under ``videos/`` instead. Pass the
    settled *ext* when it is known; otherwise the post's own URL is inspected.
    """
    if is_video_extension(ext) or (not ext and post.file_url and is_video(post.file_url)):
        return ""
    if aspect_range == (1.5, 1.8):
        return "wallpapers"
    if aspect_range == (0.6, 1.05):
        return "avatars"
    if post.width and post.height and post.height > 0:
        ratio = post.width / post.height
        if 1.5 <= ratio <= 1.8:
            return "wallpapers"
        if 0.6 <= ratio <= 1.05:
            return "avatars"
    return ""


def layout_paths(
    out_root: Path,
    rating: str | None,
    ext: str,
    *,
    kind: str = "",
) -> tuple[Path, Path]:
    """The (media directory, sidecar directory) for a rating and a file kind.

    This is the single definition of the on-disk layout: the downloader and the
    ``migrate-layout`` command both go through it, so a migration can never
    disagree with where a fresh download would have put the same file.

    A film ignores ``kind`` entirely and lands in ``<out_root>/videos/<rating>/``
    with its sidecars in the ``json/`` folder beside it; only stills are sorted
    by shape.
    """
    if is_video_extension(ext):
        bucket_dir = out_root / VIDEO_DIR / rating_bucket(rating)
        return bucket_dir, bucket_dir / SIDECAR_DIR
    base = out_root / kind if kind else out_root
    bucket_dir = base / rating_bucket(rating)
    return bucket_dir, bucket_dir / SIDECAR_DIR


def media_paths(
    out_root: Path,
    source: str,
    post: Post,
    ext: str,
    *,
    kind: str = "",
) -> tuple[Path, Path]:
    """Return the (media directory, sidecar directory) for a post."""
    del source
    return layout_paths(out_root, post.rating, ext, kind=kind)
