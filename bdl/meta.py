"""File names and JSON sidecars next to the art."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

from .models import Post

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MULTI_SEP = re.compile(r"[_-]{2,}")

_EXT_FROM_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",
    "video/webm": ".webm",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
}


def slugify(text: str, max_len: int = 48) -> str:
    """Turn an arbitrary tag into a safe ASCII file name."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    slug = _UNSAFE.sub("_", text)
    slug = _MULTI_SEP.sub("_", slug)
    return slug.strip("_-")[:max_len].rstrip("_-") or "untagged"


def ext_for_file_url(url: str) -> str:
    """Extension from the URL path (e.g. .jpg), or an empty string."""
    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    if len(ext) <= 1 or len(ext) > 8 or not ext[1:].isalnum():
        return ""
    return ext


def ext_from_content_type(content_type: str, fallback: str = "") -> str:
    """Extension from Content-Type, used as a fallback."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return _EXT_FROM_CONTENT_TYPE.get(ct, fallback)


def filename_for(post: Post, ext: str) -> str:
    """File name: {source}_{id}_{first_tag}{ext}."""
    slug = slugify(post.tags[0] if post.tags else "")
    stem = f"{post.source}_{post.id}"
    if slug != "untagged":
        stem += f"_{slug}"
    return f"{stem}{ext}"


def write_sidecar(json_path: Path, post: Post, downloaded_at: float, sha256: str | None) -> None:
    """Write metadata JSON atomically, creating parent directories as needed."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "uid": post.uid,
        "source": post.source,
        "post_id": post.id,
        "file_url": post.file_url,
        "tags": list(post.tags),
        "rating": post.rating,
        "width": post.width,
        "height": post.height,
        "score": post.score,
        "downloaded_at": downloaded_at,
    }
    if sha256:
        data["sha256"] = sha256
    tmp = json_path.with_suffix(json_path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, json_path)
