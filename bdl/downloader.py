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
from dataclasses import dataclass, field
from pathlib import Path

import aiofiles
import aiohttp
from rich.progress import Progress, ProgressColumn, Task, TextColumn, TimeElapsedColumn
from rich.text import Text

from .db import Database
from .filters import aspect_state
from .media import SIDECAR_DIR, VIDEO_EXTENSIONS, media_paths, media_subdir, rating_bucket
from .meta import ext_for_file_url, filename_for, write_sidecar
from .models import Post
from .net.doh import DoHResolver
from .providers import Provider, ProviderClient

LOG = logging.getLogger(__name__)

USER_AGENT = "bdl/2.0 (+aiohttp)"
CHUNK_SIZE = 64 * 1024
BUFFER_SIZE = 2 * 1024 * 1024
MAX_ATTEMPTS = 3
BATCH_SIZE = 20

DOWNLOADED = "downloaded"
SKIPPED = "skipped"
FAILED = "failed"

# Re-exported so callers that used the old layout module keep working.
VIDEO_EXTS = VIDEO_EXTENSIONS
__all__ = [
    "BATCH_SIZE",
    "DOWNLOADED",
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
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


class _BlocksColumn(ProgressColumn):
    """Bar that still renders when the total size is unknown."""

    def render(self, task: Task) -> Text:
        if not task.total:
            return Text(_fmt_bytes(task.completed))
        pct = task.completed / task.total * 100
        return Text(f"{_blocks(pct)} {pct:>3.0f}% {_fmt_bytes(task.completed)}/{_fmt_bytes(task.total)}")


class _SpeedColumn(ProgressColumn):
    def render(self, task: Task) -> Text:
        return Text(_fmt_speed(float(task.fields.get("speed", 0.0))), style="cyan")


class _EtaColumn(ProgressColumn):
    def render(self, task: Task) -> Text:
        total, speed = task.total, float(task.fields.get("speed", 0.0))
        if not total or speed <= 0:
            return Text("")
        return Text(f"ETA {_fmt_duration((total - task.completed) / speed)}", style="dim")


class TransferProgress:
    """Aggregate line plus one row per active file.

    Finished rows linger briefly so their final percentage is readable before
    they disappear.
    """

    HOLD_FINISHED = 0.7

    def __init__(self) -> None:
        self._progress = Progress(
            TextColumn("[progress.description]{task.description}", justify="left"),
            _BlocksColumn(),
            _SpeedColumn(),
            _EtaColumn(),
            TimeElapsedColumn(),
        )
        self._agg_task: int | None = None
        self._total_bytes = 0
        self._speed = 0.0
        self._planned_bytes = 0
        self._w_start = time.monotonic()
        self._w_bytes = 0
        self._hide_loop: asyncio.Task | None = None
        self._pending_hides: list[tuple[int, float]] = []

    def start(self, label: str, total: int) -> None:
        self._progress.start()
        self._agg_task = self._progress.add_task(label, total=total)
        self._hide_loop = asyncio.create_task(self._hide_finished())

    async def _hide_finished(self) -> None:
        try:
            while True:
                now = time.monotonic()
                due = [entry for entry in self._pending_hides if entry[1] <= now]
                if due:
                    self._pending_hides = [entry for entry in self._pending_hides if entry not in due]
                    for task_id, _ in due:
                        if 0 <= task_id < len(self._progress.tasks):
                            self._progress.update(task_id, visible=False)
                await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            pass

    def set_planned(self, total_bytes: int) -> None:
        self._planned_bytes = max(0, total_bytes)

    def update_aggregate(self, processed: int, downloaded: int, skipped: int, failed: int) -> None:
        now = time.monotonic()
        if (window := now - self._w_start) >= 0.4:
            instant = self._w_bytes / window
            self._speed = instant if self._speed == 0 else self._speed * 0.6 + instant * 0.4
            self._w_start = now
            self._w_bytes = 0
        counts = f"[green]+{downloaded}[/] [dim]={skipped}[/] [red]x{failed}[/]"
        eta = ""
        if self._planned_bytes > self._total_bytes and self._speed > 0:
            eta = f" ETA {_fmt_duration((self._planned_bytes - self._total_bytes) / self._speed)}"
        description = f"[cyan]{_fmt_bytes(self._total_bytes)}[/] {_fmt_speed(self._speed)}{eta}  {counts}"
        self._progress.update(self._agg_task, completed=processed, description=description)  # type: ignore[arg-type]

    def add_bytes(self, n: int) -> None:
        self._total_bytes += n
        self._w_bytes += n

    def begin_file(self, name: str, content_length: int | None) -> int:
        short = name if len(name) <= 42 else name[:39] + "…"
        now = time.monotonic()
        return self._progress.add_task(short, total=content_length, speed=0.0, w_start=now, w_bytes=0)

    def chunk(self, task_id: int, nbytes: int) -> None:
        # The row may already be hidden by finish_file; rich would shift ids.
        if not (0 <= task_id < len(self._progress.tasks)):
            return
        now = time.monotonic()
        task = self._progress.tasks[task_id]
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
        # Rows are hidden rather than removed: removal renumbers the tasks.
        if not (0 <= task_id < len(self._progress.tasks)):
            return
        task = self._progress.tasks[task_id]
        suffix = f" [green]{task.completed / task.total * 100:>3.0f}%[/]" if task.total else ""
        self._progress.update(task_id, description=f"[green]ok[/] {task.description}{suffix}")
        self._pending_hides.append((task_id, time.monotonic() + self.HOLD_FINISHED))

    def stop(self) -> None:
        if self._hide_loop is not None:
            self._hide_loop.cancel()
            self._hide_loop = None
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
                    LOG.warning("recorded file is gone, re-downloading: %s", path)
                elif await asyncio.to_thread(self._sha256_of_file, path) == recorded_hash:
                    return True
                else:
                    LOG.warning("checksum mismatch, re-downloading: %s", path)
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

        # Some boards reject hotlinks without a matching Referer.
        headers = {"Referer": f"{provider.base_url.rstrip('/')}/"}
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with self._sem, self._session.get(post.file_url, headers=headers) as resp:  # type: ignore[union-attr]
                    if resp.status == 429:
                        pause = _retry_after(resp.headers.get("Retry-After"))
                        LOG.warning("%s: rate limited, sleeping %.1fs", post.file_url, pause)
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
                    progress_task = (
                        bar.begin_file(f"{filename}", resp.content_length) if bar is not None else None
                    )
                    digest = hashlib.sha256() if self._checksum else None
                    try:
                        size = await self._stream_to_file(
                            resp, tmp_path, digest, bar, progress_task
                        )
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

                    # Rename only after a complete, validated transfer so a
                    # half-written file never appears under its real name.
                    await asyncio.to_thread(os.replace, tmp_path, final_path)
                    sha256 = digest.hexdigest() if digest else None
                    if self._sidecar:
                        await asyncio.to_thread(
                            write_sidecar, sidecar_dir / f"{filename}.json", post, time.time(), sha256
                        )
                    return DOWNLOADED, None, self._metadata_row(post, final_path, size, filename, sha256)
            except (_RetryableError, TimeoutError, aiohttp.ClientError) as exc:
                last_error = exc
                LOG.warning(
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
        post: Post, final_path: Path, size: int, filename: str, sha256: str | None
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

        # Smallest first: the API already reports sizes, so no HEAD requests.
        if len(todo) > 3 and any(post.file_size is not None for post in todo):
            todo.sort(key=lambda post: post.file_size or 0)
            if self._bar is not None:
                self._bar.set_planned(sum(post.file_size or 0 for post in todo))

        processed = downloaded_n = skipped_n = failed_n = 0
        batch_meta: list[tuple] = []

        async def worker(post: Post) -> tuple[str, str | None, tuple | None]:
            nonlocal processed, downloaded_n, skipped_n, failed_n
            status, error, meta = await self.download_post(provider, post)
            processed += 1
            if status == DOWNLOADED:
                downloaded_n += 1
                if meta is not None:
                    batch_meta.append(meta)
                    if len(batch_meta) >= BATCH_SIZE:
                        # Snapshot before awaiting: other workers append while
                        # the write is in flight and clear() would drop them.
                        await self._flush(batch_meta)
            elif status == SKIPPED:
                skipped_n += 1
            else:
                failed_n += 1
            if self._bar is not None:
                self._bar.update_aggregate(processed, downloaded_n, skipped_n, failed_n)
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
            else:
                stats.failed += 1
                stats.failures.append((post, error or "unknown"))
                LOG.warning("failed %s: %s", post.uid, error)
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
