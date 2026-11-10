"""Data models: a post from a booru."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Post:
    id: str
    source: str
    file_url: str
    preview_url: str | None = None
    tags: tuple[str, ...] = ()
    rating: str | None = None
    width: int | None = None
    height: int | None = None
    score: int | None = None
    file_size: int | None = None

    @property
    def tag_string(self) -> str:
        return " ".join(self.tags)

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.id}"
