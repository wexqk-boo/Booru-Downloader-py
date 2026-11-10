"""SQLite store over aiosqlite: download state and run history.

Acts as the source of truth for syncing: dedup by ``uid = source:id``,
run history, accumulated failures.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from .models import Post


def _is_under(path: Path, root: Path) -> bool:
    """Whether ``path`` lies inside ``root`` (both are resolved first)."""
    try:
        Path(path).resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    uid          TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    post_id      TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    file_size    INTEGER,
    ext          TEXT,
    tags         TEXT NOT NULL DEFAULT '[]',
    rating       TEXT,
    width        INTEGER,
    height       INTEGER,
    score        INTEGER,
    file_url     TEXT NOT NULL,
    sha256       TEXT,
    phash        TEXT,
    downloaded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_downloads_source ON downloads(source);

CREATE TABLE IF NOT EXISTS failures (
    uid        TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    post_id    TEXT NOT NULL,
    error      TEXT NOT NULL,
    failed_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failures_source ON failures(source);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    finished_at REAL,
    source     TEXT NOT NULL,
    tags       TEXT NOT NULL,
    found      INTEGER NOT NULL DEFAULT 0,
    filtered   INTEGER NOT NULL DEFAULT 0,
    downloaded INTEGER NOT NULL DEFAULT 0,
    skipped    INTEGER NOT NULL DEFAULT 0,
    failed     INTEGER NOT NULL DEFAULT 0
);
"""

RUN_COLUMNS = (
    "id",
    "started_at",
    "finished_at",
    "source",
    "tags",
    "found",
    "filtered",
    "downloaded",
    "skipped",
    "failed",
)


class Database:
    """Async wrapper around SQLite (aiosqlite)."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        path = Path(self._path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(path))
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing library untouched, so
        a new column has to be added by hand or the insert statements break.
        """
        cursor = await self._conn.execute("PRAGMA table_info(downloads)")
        try:
            columns = {row[1] for row in await cursor.fetchall()}
        finally:
            await cursor.close()
        if "phash" not in columns:
            await self._conn.execute("ALTER TABLE downloads ADD COLUMN phash TEXT")
        # Outside the branch: the schema above no longer creates this index,
        # because it would run before the column exists on an older library.
        await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_phash ON downloads(phash)")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> Database:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not open: call await db.open()")
        return self._conn

    # -- internal helpers -------------------------------------------------
    async def _fetchall(self, sql: str, params: tuple = ()) -> list[Any]:
        cursor = await self.conn.execute(sql, params)
        try:
            return await cursor.fetchall()
        finally:
            await cursor.close()

    # -- downloads --------------------------------------------------------
    async def has(self, uid: str) -> bool:
        rows = await self._fetchall("SELECT 1 FROM downloads WHERE uid = ?", (uid,))
        return bool(rows)

    async def sha256_of(self, uid: str) -> str | None:
        """Stored sha256 for the post (None if the row or the hash is missing)."""
        rows = await self._fetchall("SELECT sha256 FROM downloads WHERE uid = ?", (uid,))
        if not rows:
            return None
        return rows[0][0] or None

    async def file_and_hash(self, uid: str) -> tuple[str | None, str | None]:
        """Post path and sha256, for integrity checks under --checksum."""
        rows = await self._fetchall("SELECT file_path, sha256 FROM downloads WHERE uid = ?", (uid,))
        if not rows:
            return None, None
        return rows[0][0] or None, rows[0][1] or None

    async def has_sha256(self, sha256: str, exclude_uid: str | None = None) -> bool:
        """Whether a downloaded file has this hash (catches duplicates across uids)."""
        if exclude_uid is None:
            rows = await self._fetchall("SELECT 1 FROM downloads WHERE sha256 = ? LIMIT 1", (sha256,))
        else:
            rows = await self._fetchall(
                "SELECT 1 FROM downloads WHERE sha256 = ? AND uid != ? LIMIT 1",
                (sha256, exclude_uid),
            )
        return bool(rows)

    async def all_phashes(self) -> list[tuple[str, str]]:
        """``(uid, phash)`` for every recorded image.

        Deliberately not scoped to one board: the same artwork is reposted
        across boards under different ids, which is exactly the case the
        perceptual check exists for.
        """
        return [
            (row[0], row[1]) for row in await self._fetchall("SELECT uid, phash FROM downloads WHERE phash IS NOT NULL")
        ]

    async def record_download(
        self,
        post: Post,
        file_path: str,
        file_size: int | None,
        ext: str,
        sha256: str | None,
        phash: str | None = None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO downloads (uid, source, post_id, file_path, file_size, ext,
                                   tags, rating, width, height, score, file_url, sha256, phash, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                file_path = excluded.file_path, file_size = excluded.file_size,
                ext = excluded.ext, tags = excluded.tags, rating = excluded.rating,
                width = excluded.width, height = excluded.height, score = excluded.score,
                file_url = excluded.file_url, sha256 = excluded.sha256, phash = excluded.phash,
                downloaded_at = excluded.downloaded_at
            """,
            (
                post.uid,
                post.source,
                post.id,
                file_path,
                file_size,
                ext,
                json.dumps(list(post.tags), ensure_ascii=False),
                post.rating,
                post.width,
                post.height,
                post.score,
                post.file_url,
                sha256,
                phash,
                time.time(),
            ),
        )
        await self.conn.commit()

    async def record_download_batch(self, rows: list[tuple]) -> None:
        """Write a batch of downloads in one commit()."""
        if not rows:
            return
        await self.conn.executemany(
            """
            INSERT INTO downloads (uid, source, post_id, file_path, file_size, ext,
                                   tags, rating, width, height, score, file_url, sha256, phash, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                file_path = excluded.file_path, file_size = excluded.file_size,
                ext = excluded.ext, tags = excluded.tags, rating = excluded.rating,
                width = excluded.width, height = excluded.height, score = excluded.score,
                file_url = excluded.file_url, sha256 = excluded.sha256, phash = excluded.phash,
                downloaded_at = excluded.downloaded_at
            """,
            rows,
        )
        await self.conn.commit()

    async def record_failure_batch(self, rows: list[tuple]) -> None:
        """Write a batch of failures in one commit()."""
        if not rows:
            return
        await self.conn.executemany(
            """
            INSERT INTO failures (uid, source, post_id, error, failed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET error = excluded.error, failed_at = excluded.failed_at
            """,
            rows,
        )
        await self.conn.commit()

    async def record_failure(self, post: Post, error: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO failures (uid, source, post_id, error, failed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET error = excluded.error, failed_at = excluded.failed_at
            """,
            (post.uid, post.source, post.id, error[:500], time.time()),
        )
        await self.conn.commit()

    # -- runs -------------------------------------------------------------
    async def start_run(self, source: str, tags: str) -> int:
        cursor = await self.conn.execute(
            "INSERT INTO runs (started_at, source, tags) VALUES (?, ?, ?)",
            (time.time(), source, tags),
        )
        await self.conn.commit()
        run_id = cursor.lastrowid
        await cursor.close()
        return int(run_id)

    async def finish_run(
        self,
        run_id: int,
        *,
        found: int,
        filtered: int,
        downloaded: int,
        skipped: int,
        failed: int,
    ) -> None:
        await self.conn.execute(
            """
            UPDATE runs SET finished_at = ?, found = ?, filtered = ?, downloaded = ?, skipped = ?, failed = ?
            WHERE id = ?
            """,
            (time.time(), found, filtered, downloaded, skipped, failed, run_id),
        )
        await self.conn.commit()

    # -- summary ----------------------------------------------------------
    async def all_uids(self) -> set[str]:
        """Every uid already downloaded (used to spot fresh posts)."""
        rows = await self._fetchall("SELECT uid FROM downloads")
        return {row[0] for row in rows}

    async def prune_stale(self, dry_run: bool = False, under: Path | None = None) -> dict[str, int]:
        """Drop records whose files are gone from disk.

        Only rows pointing inside ``under`` are considered, so a run working in
        a temporary folder cannot forget the history of the real library.
        Returns {source: count, "total": N}. With dry_run it only counts.
        """
        scope = Path(under).resolve() if under is not None else None
        rows = await self._fetchall("SELECT uid, source, file_path FROM downloads")
        missing: dict[str, int] = {}
        stale: list[str] = []
        for uid, source, file_path in rows:
            if not file_path:
                continue
            path = Path(file_path)
            if scope is not None and not _is_under(path, scope):
                continue
            if not path.exists():
                missing[source] = missing.get(source, 0) + 1
                stale.append(uid)
        if stale and not dry_run:
            await self.conn.executemany("DELETE FROM downloads WHERE uid = ?", [(uid,) for uid in stale])
            await self.conn.commit()
        missing["total"] = len(stale)
        return missing

    async def summary(self, runs_limit: int = 10) -> dict[str, Any]:
        source_rows = await self._fetchall(
            """
            SELECT source, COUNT(*), COALESCE(SUM(file_size), 0), MAX(downloaded_at)
            FROM downloads GROUP BY source ORDER BY source
            """
        )
        sources = [
            {"source": key, "files": files, "bytes": size, "last_at": last_at}
            for key, files, size, last_at in source_rows
        ]
        total = await self._fetchall("SELECT COUNT(*), COALESCE(SUM(file_size), 0) FROM downloads")
        totals = {"files": total[0][0], "bytes": total[0][1]} if total else {"files": 0, "bytes": 0}

        run_rows = await self._fetchall("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (runs_limit,))
        runs = [dict(zip(RUN_COLUMNS, row, strict=True)) for row in run_rows]

        failure_rows = await self._fetchall(
            "SELECT source, COUNT(*), MAX(failed_at) FROM failures GROUP BY source ORDER BY source"
        )
        failures = [{"source": key, "count": count, "last_at": last_at} for key, count, last_at in failure_rows]
        return {"sources": sources, "totals": totals, "runs": runs, "failures": failures}
