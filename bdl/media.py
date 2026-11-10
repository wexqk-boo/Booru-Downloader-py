"""Media classification: file kind, rating bucket and on-disk layout."""

from __future__ import annotations

from pathlib import Path

from .models import Post

SIDECAR_DIR = "json"

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


def media_subdir(ext: str) -> str:
    """Subfolder for a file with this extension: ``videos`` or the bucket root."""
    return "videos" if ext.lower() in VIDEO_EXTENSIONS else ""


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


def media_paths(out_root: Path, source: str, post: Post, ext: str) -> tuple[Path, Path]:
    """Return the (media directory, sidecar directory) for a post."""
    bucket_dir = out_root / source / rating_bucket(post.rating)
    subdir = media_subdir(ext)
    return (bucket_dir / subdir if subdir else bucket_dir), bucket_dir / SIDECAR_DIR
