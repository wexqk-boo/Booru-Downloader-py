"""Streaming download engine.

Files are written in 64 KiB chunks against a ~2 MiB buffer, so a stalled or
oversized transfer never holds more than a slice of the file in memory. The
HTTP session uses a DoH resolver because local DNS often refuses the boards'
CDNs (``img4.gelbooru.com``, ``api-cdn.rule34.xxx``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import aiofiles
import aiohttp
from rich import get_console
from rich.cells import cell_len, set_cell_size
from rich.console import RenderableType
from rich.progress import Progress, Task
from rich.text import Text

from . import USER_AGENT, XBOORU_USER_AGENT, preview
from .db import Database
from .filters import aspect_state
from .media import SIDECAR_DIR, VIDEO_EXTENSIONS, media_paths, media_subdir, rating_bucket
from .meta import ext_for_file_url, filename_for, write_sidecar
from .models import Post
from .net.doh import DoHResolver
from .perceptual import is_duplicate, phash_of_file
from .providers import Provider, ProviderClient

LOG = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
BUFFER_SIZE = 2 * 1024 * 1024
MAX_ATTEMPTS = 3
BATCH_SIZE = 20

DOWNLOADED = "downloaded"
SKIPPED = "skipped"
DUPLICATE = "duplicate"
FAILED = "failed"

# Re-exported so callers that used the old layout module keep working.
VIDEO_EXTS = VIDEO_EXTENSIONS
__all__ = [
    "BATCH_SIZE",
    "DOWNLOADED",
    "DUPLICATE",
    "FAILED",
    "SIDECAR_DIR",
    "SKIPPED",
    "BatchStats",
    "Downloader",
    "TransferProgress",
    "media_paths",
    "media_subdir",
    "rating_bucket",
]


def _fmt_bytes(n: int | float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def _fmt_speed(bps: float) -> str:
    return f"{_fmt_bytes(bps)}/s" if bps else "—"


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _blocks(pct: float, width: int = 16) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


# Braille spinner: one frame per rendered tick, so it animates while bytes land.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _spinner() -> str:
    return _SPINNER_FRAMES[int(time.monotonic() * 12) % len(_SPINNER_FRAMES)]


def _gauge(pct: float, width: int) -> tuple[str, str]:
    """Split a bar into its filled and empty halves, both one cell per step."""
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled, "━" * (width - filled)


def _clip(text: str, width: int) -> str:
    """Trim *text* to a terminal cell budget, with an ellipsis when cut."""
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    return set_cell_size(text, width - 1) + "…"


def _pad(text: str, width: int) -> str:
    """Pad by rendered cell width, so wide characters keep the columns aligned."""
    clipped = _clip(text, width)
    return clipped + " " * max(0, width - cell_len(clipped))


class _TransferDisplay(Progress):
    """Progress that draws every row as one hard-clipped line.

    rich's task table shares a single width budget across all rows, so a long
    aggregate line widens the description column and wraps every file row onto
    a second terminal line. Building the lines here keeps each row on exactly
    one line, and clipping guarantees the display never scrolls.
    """

    def __init__(self) -> None:
        super().__init__()
        self.total_bytes = 0
        self.speed = 0.0
        self.planned_bytes = 0

    def _width(self) -> int:
        # rich builds the initial renderable before ``self.live`` exists, so
        # fall back to the global console during construction.
        live = getattr(self, "live", None)
        console = live.console if live is not None else get_console()
        return max(24, console.width)

    def get_renderables(self) -> Iterable[RenderableType]:
        width = self._width()
        lines: list[Text] = []
        for task in self.tasks:
            if not task.visible:
                continue
            line = self._aggregate_line(task, width) if task.fields.get("aggregate") else self._file_line(task, width)
            line.truncate(width)
            lines.append(line)
        yield Text("\n").join(lines) if lines else Text("")

    def _file_line(self, task: Task, width: int) -> Text:
        total = _fmt_bytes(task.total) if task.total else "?"
        pct = min(task.completed / task.total * 100, 100) if task.total else 0.0
        bar_width = 18
        filled, empty = _gauge(pct, bar_width)
        speed = _fmt_speed(float(task.fields.get("speed", 0.0)))
        tail = f" {pct:>3.0f}%  {_fmt_bytes(task.completed)}/{total}  {speed}"
        prefix = f"{_spinner()} "
        name_width = max(12, width - cell_len(prefix) - bar_width - cell_len(tail) - 3)
        return Text.assemble(
            (prefix, "cyan"),
            (_pad(str(task.description), name_width), ""),
            "  ",
            (filled, "cyan"),
            (empty, "grey37"),
            (tail, "cyan" if pct >= 100 else ""),
        )

    def _aggregate_line(self, task: Task, width: int) -> Text:
        eta = ""
        if self.planned_bytes > self.total_bytes and self.speed > 0:
            eta = f"  ETA {_fmt_duration((self.planned_bytes - self.total_bytes) / self.speed)}"
        counter = f"{int(task.completed)} / {int(task.total or 0)}"
        gauge_width = max(8, min(24, width // 4))
        pct = min(task.completed / task.total * 100, 100) if task.total else 0.0
        filled, empty = _gauge(pct, gauge_width)
        return Text.assemble(
            ("⠿ ", "cyan"),
            (filled, "cyan"),
            (empty, "grey37"),
            "  ",
            (counter, "bold"),
            "  ",
            (_fmt_bytes(self.total_bytes), "bold cyan"),
            "  ",
            (_fmt_speed(self.speed), "cyan"),
            (eta, "dim"),
            "  ",
            (str(task.fields.get("counts", "")), "green"),
            "  ",
            (_fmt_duration(task.elapsed), "dim"),
        )


class TransferProgress:
    """One aggregate line plus a row per file that is still in flight.

    A finished file leaves the display the moment its last byte lands, so the
    terminal only ever shows active downloads and never grows a wall of
    completed rows.
    """

    def __init__(self) -> None:
        self._progress = _TransferDisplay()
        self._agg_task: int | None = None
        self._total_bytes = 0
        self._speed = 0.0
        self._planned_bytes = 0
        self._w_start = time.monotonic()
        self._w_bytes = 0
        self._console = get_console()

    def _task(self, task_id: int) -> Task | None:
        """Look a task up by id; ids survive removals of other rows."""
        for task in self._progress.tasks:
            if task.id == task_id:
                return task
        return None

    def start(self, label: str, total: int) -> None:
        self._progress.start()
        self._agg_task = self._progress.add_task(label, total=total, aggregate=True)

    def set_planned(self, total_bytes: int) -> None:
        self._planned_bytes = max(0, total_bytes)
        self._progress.planned_bytes = self._planned_bytes

    def update_aggregate(self, processed: int, downloaded: int, skipped: int, failed: int, duplicates: int = 0) -> None:
        now = time.monotonic()
        if (window := now - self._w_start) >= 0.4:
            instant = self._w_bytes / window
            self._speed = instant if self._speed == 0 else self._speed * 0.6 + instant * 0.4
            self._w_start = now
            self._w_bytes = 0
        self._progress.speed = self._speed
        counts = f"+{downloaded} ={skipped} x{failed}"
        if duplicates:
            counts += f" ~{duplicates}"
        self._progress.update(
            self._agg_task,  # type: ignore[arg-type]
            completed=processed,
            counts=counts,
        )

    def add_bytes(self, n: int) -> None:
        self._total_bytes += n
        self._w_bytes += n
        self._progress.total_bytes = self._total_bytes

    def begin_file(self, name: str, content_length: int | None) -> int:
        short = name if len(name) <= 42 else name[:39] + "…"
        now = time.monotonic()
        return self._progress.add_task(short, total=content_length, speed=0.0, w_start=now, w_bytes=0)

    def chunk(self, task_id: int, nbytes: int) -> None:
        # The row may already be gone: a finished file is removed, not hidden.
        task = self._task(task_id)
        if task is None:
            return
        now = time.monotonic()
        self._progress.update(task_id, advance=nbytes)
        window = now - float(task.fields.get("w_start", now))
        w_bytes = int(task.fields.get("w_bytes", 0)) + nbytes
        if window >= 0.4:
            instant = w_bytes / window
            prev = float(task.fields.get("speed", 0.0))
            self._progress.update(task_id, speed=prev * 0.6 + instant * 0.4, w_start=now, w_bytes=0)
        else:
            self._progress.update(task_id, w_bytes=w_bytes)
        self.add_bytes(nbytes)

    def finish_file(self, task_id: int) -> None:
        # Remove the row outright: the display keeps only work still in flight.
        if self._task(task_id) is None:
            return
        self._progress.remove_task(task_id)

    def log(self, message: str, *args: object) -> None:
        """Write a line without tearing a hole in the live display.

        A plain ``logging`` record goes straight to stderr, which the progress
        bar repaints several times a second, so the text lands in the middle of
        the bars. ``console.print`` goes through rich, which clears the live
        region first and redraws it afterwards.
        """
        self._console.print(message % args if args else message, markup=False, highlight=False, style="dim")

    def stop(self) -> None:
        self._progress.stop()


@dataclass(slots=True)
class BatchStats:
    found: int = 0
    #: Unique posts the board actually returned, downloaded or not. Lets the
    #: summary tell "this tag matches nothing" from "we already have it all".
    reported: int = 0
    filtered: int = 0
    downloaded: int = 0
    skipped: int = 0
    #: Skipped because the picture is already in the library under another id.
    duplicates: int = 0
    failed: int = 0
    unknown_aspect: int = 0
    failures: list[tuple[Post, str]] = field(default_factory=list)


class Downloader:
    def __init__(
        self,
        db: Database,
        *,
        out_root: Path,
        concurrency: int = 3,
        media_timeout: float = 600.0,
        proxy: str = "",
        force: bool = False,
        sidecar: bool = False,
        checksum: bool = False,
        max_size_mb: int | None = None,
        min_score: int | None = None,
        rating_include: set[str] | None = None,
        blacklist: set[str] | None = None,
        aspect_range: tuple[float, float] | None = None,
        aspect_unknown: str = "keep",
        progress: bool = False,
        dedup: bool = True,
        preview: bool = False,
    ) -> None:
        self._db = db
        self._out_root = out_root
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._media_timeout = media_timeout
        self._proxy = proxy or None
        self._force = force
        self._sidecar = sidecar
        self._checksum = checksum
        self._max_bytes = max_size_mb * 1024 * 1024 if max_size_mb else None
        self._min_score = min_score
        self._rating_include = rating_include
        self._blacklist = {tag.lower() for tag in (blacklist or set())}
        self._aspect_range = aspect_range
        self._aspect_unknown = aspect_unknown
        self._progress_enabled = progress
        self._dedup = dedup
        #: Draw a thumbnail in Kitty once a file lands. Only set when the user
        #: asked for it, so nothing is decoded on a normal run.
        self._preview = preview
        self._preview_paths: list[Path] = []
        #: (uid, phash) pairs loaded once per run; refreshed as files land so a
        #: duplicate inside the same batch is caught too.
        self._phashes: list[tuple[str, str]] | None = None
        self._bar: TransferProgress | None = None
        self._session: aiohttp.ClientSession | None = None
        self._resolver = DoHResolver()

    async def open(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._media_timeout, connect=10, sock_read=90),
            connector=aiohttp.TCPConnector(
                limit=32,
                limit_per_host=16,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
                resolver=self._resolver,
            ),
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            proxy=self._proxy,
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        await self._resolver.close()

    async def __aenter__(self) -> Downloader:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @staticmethod
    async def collect_posts(
        client: ProviderClient,
        provider_key: str,
        tags: str,
        limit: int,
        per_page: int,
        order: str = "",
    ) -> list[Post]:
        """Page through the API until ``limit`` unique posts are collected."""
        posts: list[Post] = []
        seen: set[str] = set()
        max_pages = max(1, (limit + per_page - 1) // per_page + 5)
        for page in range(1, max_pages + 1):
            if len(posts) >= limit:
                break
            result = await client.search(provider_key, tags, page, per_page, order)
            fresh = [post for post in result.posts if post.uid not in seen]
            if not fresh:
                # An all-duplicates page means the board wrapped around.
                break
            for post in fresh:
                if len(posts) >= limit:
                    break
                seen.add(post.uid)
                posts.append(post)
            if result.exhausted:
                break
        return posts

    def aspect_verdict(self, post: Post) -> bool | None:
        """True/False for the ratio window, None when dimensions are unknown."""
        if self._aspect_range is None:
            return True
        return aspect_state(post, *self._aspect_range)

    def passes_filters(self, post: Post) -> bool:
        score_ok = self._min_score is None or (post.score is not None and post.score >= self._min_score)
        rating_ok = self._rating_include is None or post.rating in self._rating_include
        blacklist_ok = not self._blacklist or not (self._blacklist & {tag.lower() for tag in post.tags})
        verdict = self.aspect_verdict(post)
        aspect_ok = verdict is True or (verdict is None and self._aspect_unknown == "keep")
        return score_ok and rating_ok and blacklist_ok and aspect_ok

    @staticmethod
    def _sha256_of_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest()

    async def _already_have(self, post: Post, final_path: Path) -> bool:
        """Whether this post is already on disk. Never issues a network request.

        Three levels, cheapest first: the uid is in SQLite, the target file
        exists, or — with ``--checksum`` — the content hash matches.
        """
        if self._force:
            return False

        if await self._db.has(post.uid):
            if not self._checksum:
                return True
            # With checksums the database row is a claim, not proof: the file
            # may have been edited or deleted since it was recorded.
            recorded_path, recorded_hash = await self._db.file_and_hash(post.uid)
            if recorded_path and recorded_hash:
                path = Path(recorded_path)
                if not await asyncio.to_thread(path.is_file):
                    self._warn("recorded file is gone, re-downloading: %s", path)
                elif await asyncio.to_thread(self._sha256_of_file, path) == recorded_hash:
                    return True
                else:
                    self._warn("checksum mismatch, re-downloading: %s", path)
        elif await asyncio.to_thread(final_path.is_file):
            # Present on disk but absent from the database (pruned, or a
            # different profile wrote it). Re-downloading would be wasteful.
            LOG.debug("file on disk but not in database: %s", final_path)
            return True

        if self._checksum and await asyncio.to_thread(final_path.is_file):
            digest = await asyncio.to_thread(self._sha256_of_file, final_path)
            if await self._db.has_sha256(digest, exclude_uid=post.uid):
                LOG.debug("same content already stored under another uid: %s", final_path)
                return True
        return False

    def _warn(self, message: str, *args: object) -> None:
        """A per-file warning: through the live display when one is running."""
        if self._bar is not None:
            self._bar.log(message, *args)
        else:
            LOG.warning(message, *args)

    def _is_duplicate_picture(self, phash: str, uid: str = "") -> bool:
        """Whether this pHash matches a picture already in the library.

        The hash list is loaded once per run and kept up to date locally, so
        every comparison is plain arithmetic: SQLite has no bit-distance
        operator, and a Hamming comparison in Python over a few thousand rows
        is far cheaper than a query per file.

        The post's own earlier record is skipped: with ``--force`` the picture
        is deliberately fetched a second time, and its own stored hash would
        otherwise reject the very file the user asked for.
        """
        if self._phashes is None:
            return False
        return any(is_duplicate(phash, known) for known_uid, known in self._phashes if known_uid != uid)

    async def _load_phashes(self) -> None:
        if not self._dedup or self._phashes is not None:
            return
        self._phashes = await self._db.all_phashes()

    def _remember_phash(self, uid: str, phash: str | None) -> None:
        """Add a freshly stored hash so the rest of the batch can see it."""
        if self._phashes is not None and phash:
            self._phashes.append((uid, phash))

    def collect_preview(self, path: Path) -> None:
        """Queue a finished picture for the gallery drawn after the batch.

        Drawing mid-run would fight the progress display for the same rows of
        the terminal, so previews wait until every board is done.
        """
        self._preview_paths.append(path)

    def show_previews(self) -> None:
        """Draw the queued thumbnails, when the terminal can show them."""
        paths, self._preview_paths = self._preview_paths, []
        if not paths or not preview.available():
            return
        for path in paths:
            try:
                preview.show(path)
            except (OSError, ValueError) as exc:
                LOG.debug("preview failed for %s: %s", path, exc)

    async def download_post(self, provider: Provider, post: Post) -> tuple[str, str | None, tuple | None]:
        """Download one post; returns ``(status, error, metadata_row)``."""
        ext = ext_for_file_url(post.file_url) or ".bin"
        filename = filename_for(post, ext)
        media_dir, sidecar_dir = media_paths(self._out_root, provider.key, post, ext)
        final_path = media_dir / filename
        tmp_path = media_dir / f".{filename}.part"

        if await self._already_have(post, final_path):
            return SKIPPED, None, None

        media_dir.mkdir(parents=True, exist_ok=True)
        if self._sidecar:
            sidecar_dir.mkdir(parents=True, exist_ok=True)

        # Some boards reject hotlinks without a matching Referer, and Xbooru's
        # Cloudflare edge challenges the plain tool User-Agent.
        headers = {"Referer": f"{provider.base_url.rstrip('/')}/"}
        if provider.key == "xbooru":
            headers["User-Agent"] = XBOORU_USER_AGENT
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with self._sem, self._session.get(post.file_url, headers=headers) as resp:  # type: ignore[union-attr]
                    if resp.status == 429:
                        pause = _retry_after(resp.headers.get("Retry-After"))
                        self._warn("%s: rate limited, sleeping %.1fs", post.file_url, pause)
                        await asyncio.sleep(pause)
                        continue
                    if resp.status == 404:
                        return FAILED, "404 — removed from the board", None
                    if resp.status >= 500:
                        raise _RetryableError(f"HTTP {resp.status}")
                    if resp.status >= 400:
                        return FAILED, f"HTTP {resp.status}", None

                    # Trust Content-Length to skip a file we would discard
                    # anyway; the post-transfer check still guards boards that
                    # lie about it or omit it.
                    length = resp.content_length
                    if self._max_bytes is not None and length is not None and length > self._max_bytes:
                        return FAILED, f"exceeds --max-size-mb ({length} bytes)", None

                    bar = self._bar
                    progress_task = bar.begin_file(f"{filename}", resp.content_length) if bar is not None else None
                    digest = hashlib.sha256() if self._checksum else None
                    try:
                        size = await self._stream_to_file(resp, tmp_path, digest, bar, progress_task)
                    except BaseException:
                        await asyncio.to_thread(tmp_path.unlink, missing_ok=True)
                        raise
                    finally:
                        if progress_task is not None and bar is not None:
                            bar.finish_file(progress_task)

                    if self._max_bytes is not None and size > self._max_bytes:
                        await asyncio.to_thread(tmp_path.unlink, missing_ok=True)
                        return FAILED, f"exceeds --max-size-mb ({size} bytes)", None
                    if size == 0:
                        await asyncio.to_thread(tmp_path.unlink, missing_ok=True)
                        return FAILED, "empty file", None

                    # Perceptual dedup, before the file is given its real name:
                    # the same artwork is reposted across boards under different
                    # ids, so uid and sha256 both miss it.
                    phash = await asyncio.to_thread(phash_of_file, tmp_path)
                    if phash is not None and self._is_duplicate_picture(phash, post.uid):
                        await asyncio.to_thread(tmp_path.unlink, missing_ok=True)
                        return DUPLICATE, None, None

                    # Rename only after a complete, validated transfer so a
                    # half-written file never appears under its real name.
                    await asyncio.to_thread(os.replace, tmp_path, final_path)
                    sha256 = digest.hexdigest() if digest else None
                    if self._sidecar:
                        await asyncio.to_thread(
                            write_sidecar, sidecar_dir / f"{filename}.json", post, time.time(), sha256
                        )
                    if self._preview:
                        await asyncio.to_thread(self.collect_preview, final_path)
                    return (
                        DOWNLOADED,
                        None,
                        self._metadata_row(post, final_path, size, filename, sha256, phash),
                    )
            except (_RetryableError, TimeoutError, aiohttp.ClientError) as exc:
                last_error = exc
                self._warn(
                    "%s: network error (%s), attempt %d/%d",
                    post.file_url,
                    exc,
                    attempt + 1,
                    MAX_ATTEMPTS,
                )
                await asyncio.sleep(_backoff(attempt))
        return FAILED, str(last_error) if last_error else "net-error", None

    async def _stream_to_file(
        self,
        resp: aiohttp.ClientResponse,
        tmp_path: Path,
        digest: hashlib._Hash | None,
        bar: TransferProgress | None,
        progress_task: int | None,
    ) -> int:
        """Write the response body to disk in buffered chunks."""
        written = 0
        buffer = bytearray()
        async with aiofiles.open(tmp_path, "wb") as handle:
            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                buffer.extend(chunk)
                written += len(chunk)
                if digest is not None:
                    digest.update(chunk)
                if progress_task is not None and bar is not None:
                    bar.chunk(progress_task, len(chunk))
                if len(buffer) >= BUFFER_SIZE:
                    await handle.write(bytes(buffer))
                    buffer.clear()
                if self._max_bytes is not None and written > self._max_bytes:
                    break
            if buffer:
                await handle.write(bytes(buffer))
        return written

    @staticmethod
    def _metadata_row(
        post: Post,
        final_path: Path,
        size: int,
        filename: str,
        sha256: str | None,
        phash: str | None = None,
    ) -> tuple:
        return (
            post.uid,
            post.source,
            post.id,
            str(final_path),
            size,
            Path(filename).suffix,
            json.dumps(list(post.tags), ensure_ascii=False),
            post.rating,
            post.width,
            post.height,
            post.score,
            post.file_url,
            sha256,
            phash,
            time.time(),
        )

    async def _cleanup_parts(self, provider: Provider) -> None:
        """Remove ``.part`` leftovers from interrupted runs."""
        base = self._out_root / provider.key

        def cleanup() -> None:
            if not base.is_dir():
                return
            for part in base.rglob(".*.part"):
                try:
                    part.unlink()
                except OSError:
                    LOG.debug("could not remove %s", part)

        await asyncio.to_thread(cleanup)

    async def run_batch(
        self,
        client: ProviderClient,
        provider: Provider,
        tags: str,
        *,
        limit: int,
        per_page: int,
        order: str = "",
    ) -> BatchStats:
        stats = BatchStats()
        # --force means "download it again", so the uid lookup must not filter
        # the post out before the downloader ever sees it.
        known = set() if self._force else await self._db.all_uids()
        todo: list[Post] = []
        seen: set[str] = set()
        filtered = 0
        unknown_aspect = 0
        already = 0
        max_pages = max(10, (limit + per_page - 1) // per_page + 20)
        # Page past the limit: already-downloaded posts do not count towards it.
        page = 1
        while len(todo) < limit and page <= max_pages:
            result = await client.search(provider.key, tags, page, per_page, order)
            for post in result.posts:
                if post.uid in seen:
                    continue
                seen.add(post.uid)
                if post.uid in known:
                    already += 1
                    continue
                if not self.passes_filters(post):
                    filtered += 1
                    continue
                if self.aspect_verdict(post) is None:
                    unknown_aspect += 1
                todo.append(post)
                if len(todo) >= limit:
                    break
            if result.exhausted:
                break
            page += 1
        stats.found = len(todo) + filtered
        # `seen` holds every unique post the board returned, downloaded or not,
        # which is what distinguishes an empty tag from a tag we already have.
        stats.reported = len(seen)
        # Posts dropped here never reach the downloader, so their skip has to be
        # counted now — otherwise a re-run of a finished batch looks empty.
        stats.skipped = already
        stats.filtered = filtered
        stats.unknown_aspect = unknown_aspect

        self._bar = TransferProgress() if self._progress_enabled and todo and sys.stdout.isatty() else None
        if self._bar is not None:
            self._bar.start(provider.title, len(todo))

        await self._load_phashes()

        # Smallest first: the API already reports sizes, so no HEAD requests.
        if len(todo) > 3 and any(post.file_size is not None for post in todo):
            todo.sort(key=lambda post: post.file_size or 0)
            if self._bar is not None:
                self._bar.set_planned(sum(post.file_size or 0 for post in todo))

        processed = downloaded_n = skipped_n = failed_n = duplicates_n = 0
        batch_meta: list[tuple] = []

        async def worker(post: Post) -> tuple[str, str | None, tuple | None]:
            nonlocal processed, downloaded_n, skipped_n, failed_n, duplicates_n
            status, error, meta = await self.download_post(provider, post)
            processed += 1
            if status == DOWNLOADED:
                downloaded_n += 1
                if meta is not None:
                    self._remember_phash(str(meta[0]), meta[13])
                    batch_meta.append(meta)
                    if len(batch_meta) >= BATCH_SIZE:
                        # Snapshot before awaiting: other workers append while
                        # the write is in flight and clear() would drop them.
                        await self._flush(batch_meta)
            elif status == SKIPPED:
                skipped_n += 1
            elif status == DUPLICATE:
                duplicates_n += 1
            else:
                failed_n += 1
            if self._bar is not None:
                self._bar.update_aggregate(processed, downloaded_n, skipped_n, failed_n, duplicates_n)
            return status, error, meta

        try:
            results = await asyncio.gather(*(worker(post) for post in todo))
        finally:
            if batch_meta:
                await self._flush(batch_meta)
            if self._bar is not None:
                self._bar.stop()
                self._bar = None
            # Interrupts must not leave .part files behind.
            await self._cleanup_parts(provider)

        failures_batch: list[tuple] = []
        for (status, error, _meta), post in zip(results, todo, strict=True):
            if status == DOWNLOADED:
                stats.downloaded += 1
            elif status == SKIPPED:
                stats.skipped += 1
            elif status == DUPLICATE:
                stats.skipped += 1
                stats.duplicates += 1
            else:
                stats.failed += 1
                stats.failures.append((post, error or "unknown"))
                self._warn("failed %s: %s", post.uid, error)
                row = (post.uid, post.source, post.id, (error or "unknown")[:500], time.time())
                failures_batch.append(row)
        if failures_batch:
            await self._db.record_failure_batch(failures_batch)
        return stats

    async def _flush(self, batch: list[tuple]) -> None:
        rows, batch[:] = list(batch), []
        await self._db.record_download_batch(rows)


class _RetryableError(RuntimeError):
    pass


def _backoff(attempt: int) -> float:
    return min(0.4 * (2**attempt), 4.0)


def _retry_after(raw: str | None) -> float:
    try:
        return min(max(float(raw or 2.0), 0.5), 8.0)
    except (TypeError, ValueError):
        return 2.0
