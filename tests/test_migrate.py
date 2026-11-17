"""Tests for the `bdl migrate-layout` command and its planner.

Everything runs inside pytest's temporary directory (under /tmp): the real
library is never opened, and the assertions are about files on disk plus the
rows that follow them in SQLite.
"""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from pathlib import Path

import pytest

from bdl import migrate
from bdl.db import Database
from bdl.models import Post


def _image(path: Path, width: int, height: int) -> Path:
    """Write a real PNG of the given size, so classification reads pixels."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), (10, 20, 30)).save(path)
    return path


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    """An old-style library with one wallpaper and one avatar per source."""
    root = tmp_path / "booru"
    # gelbooru: a 16:9 wallpaper and a square avatar.
    _image(root / "gelbooru" / "safe" / "gelbooru_1_wide.jpg", 1920, 1080)
    _image(root / "gelbooru" / "safe" / "gelbooru_2_square.png", 500, 500)
    # rule34: an unusual shape that fits neither window.
    _image(root / "rule34" / "nsfw" / "rule34_3_tall.jpg", 800, 1600)
    (root / "rule34" / "nsfw" / "rule34_3_tall.jpg.json").write_text("{}", encoding="utf-8")
    # A film needs no classification: every film goes to the shared root.
    (root / "gelbooru" / "safe" / "videos").mkdir(parents=True, exist_ok=True)
    (root / "gelbooru" / "safe" / "videos" / "gelbooru_9_clip.mp4").write_bytes(b"video")
    # A sidecar that describes nothing is left alone.
    (root / "gelbooru" / "safe" / "orphan.jpg.json").write_text("{}", encoding="utf-8")
    return root


# -- classification ---------------------------------------------------------
def test_classify_matches_the_filter_windows() -> None:
    assert migrate.classify(16 / 9) == "wallpapers"
    assert migrate.classify(1.0) == "avatars"
    assert migrate.classify(0.5) == ""  # neither window
    assert migrate.classify(None) == ""


def test_image_aspect_reads_the_real_pixels(tmp_path: Path) -> None:
    path = _image(tmp_path / "a.png", 1600, 900)
    assert migrate.image_aspect(path) == pytest.approx(1600 / 900)
    # A file that is not an image is simply unknown, not an error.
    broken = tmp_path / "b.png"
    broken.write_bytes(b"not an image")
    assert migrate.image_aspect(broken) is None
    assert migrate.image_aspect(tmp_path / "missing.png") is None


# -- planning ---------------------------------------------------------------
def test_plan_sorts_media_into_shape_folders(library: Path) -> None:
    plan = migrate.build_plan(library)
    targets = {move.source_path.name: move.target_path for move in plan.moves}

    assert targets["gelbooru_1_wide.jpg"] == library / "wallpapers" / "safe" / "gelbooru_1_wide.jpg"
    assert targets["gelbooru_2_square.png"] == library / "avatars" / "safe" / "gelbooru_2_square.png"
    # An unusual shape keeps its rating folder at the root of the new layout.
    assert targets["rule34_3_tall.jpg"] == library / "nsfw" / "rule34_3_tall.jpg"
    # A film lands in the shared videos root, exactly where a fresh download
    # of the same file would be placed.
    assert targets["gelbooru_9_clip.mp4"] == library / "videos" / "safe" / "gelbooru_9_clip.mp4"


def test_a_sidecar_follows_the_media_file_it_describes(library: Path) -> None:
    plan = migrate.build_plan(library)
    sidecar = next(move for move in plan.moves if move.source_path.name == "rule34_3_tall.jpg.json")
    assert sidecar.target_path == library / "nsfw" / "json" / "rule34_3_tall.jpg.json"
    assert sidecar.reason == "sidecar"
    # A sidecar naming no real file is reported, not moved.
    assert any(path.name == "orphan.jpg.json" for path, _reason in plan.skipped)


def test_a_wide_film_is_not_filed_as_a_wallpaper(library: Path) -> None:
    """A 16:9 clip would look like a wallpaper by ratio; it must not be one."""
    plan = migrate.build_plan(library)
    video = next(move for move in plan.moves if move.source_path.suffix == ".mp4")
    assert video.target_path == library / "videos" / "safe" / "gelbooru_9_clip.mp4"
    assert "wallpapers" not in str(video.target_path)


def test_dry_run_reports_exactly_what_a_real_run_would_do(library: Path) -> None:
    plan = migrate.build_plan(library)
    result = migrate.apply_plan(plan, dry_run=True)
    assert result.moved_count == plan.total
    assert not result.failed
    # Nothing on disk has changed: the old tree is untouched.
    assert (library / "gelbooru" / "safe" / "gelbooru_1_wide.jpg").is_file()
    assert not (library / "wallpapers").exists()


def test_applying_moves_the_files_and_removes_the_old_folders(library: Path) -> None:
    plan = migrate.build_plan(library)
    result = migrate.apply_plan(plan)
    assert result.moved_count == plan.total
    assert not result.failed

    assert (library / "wallpapers" / "safe" / "gelbooru_1_wide.jpg").is_file()
    assert (library / "avatars" / "safe" / "gelbooru_2_square.png").is_file()
    assert (library / "nsfw" / "json" / "rule34_3_tall.jpg.json").is_file()

    migrate.prune_empty_dirs(library)
    # rule34 held nothing but migrated files, so the whole tree is gone.
    assert not (library / "rule34").exists()
    # gelbooru keeps the empty videos folder's parents only as far as the
    # unmoved orphan sidecar still needs them — nothing else survives.
    assert (library / "gelbooru" / "safe" / "orphan.jpg.json").is_file()
    assert not (library / "gelbooru" / "safe" / "videos").exists()
    assert not (library / "gelbooru" / "safe" / "gelbooru_1_wide.jpg").exists()


def test_migration_is_idempotent(library: Path) -> None:
    """A second run has nothing left to move.

    The fixture deliberately contains a sidecar whose media file does not
    exist; that one is reported every time rather than guessed at, which is
    why the assertion counts moves rather than skips.
    """
    migrate.apply_plan(migrate.build_plan(library))
    migrate.prune_empty_dirs(library)
    again = migrate.build_plan(library)
    assert again.total == 0
    assert [path.name for path, _reason in again.skipped] == ["orphan.jpg.json"]


def test_a_taken_destination_is_reported_instead_of_overwriting(library: Path) -> None:
    """Two files with the same name must not silently replace one another."""
    clash_src = library / "rule34" / "safe" / "gelbooru_1_wide.jpg"
    clash_src.parent.mkdir(parents=True, exist_ok=True)
    _image(clash_src, 1920, 1080)
    (library / "wallpapers" / "safe").mkdir(parents=True, exist_ok=True)
    (library / "wallpapers" / "safe" / "gelbooru_1_wide.jpg").write_bytes(b"already here")

    plan = migrate.build_plan(library)
    result = migrate.apply_plan(plan)
    assert result.failed
    assert any("already taken" in reason for _move, reason in result.failed)
    # Both originals survive: the moved one and the pre-existing destination.
    assert clash_src.is_file()
    assert (library / "wallpapers" / "safe" / "gelbooru_1_wide.jpg").read_bytes() == b"already here"


def test_temporary_files_are_left_behind(library: Path) -> None:
    part = library / "gelbooru" / "safe" / ".gelbooru_1_wide.jpg.part"
    part.write_bytes(b"partial")
    plan = migrate.build_plan(library)
    assert all(move.source_path != part for move in plan.moves)
    assert any(path == part for path, _reason in plan.skipped)
    # A dotfile must not survive as a reason to keep the old tree forever.
    migrate.apply_plan(plan)
    migrate.prune_empty_dirs(library)
    assert part.is_file()


def test_prune_reports_the_same_folders_it_would_delete(library: Path) -> None:
    migrate.apply_plan(migrate.build_plan(library))
    # The emptied folders are named by a dry prune...
    empty = migrate.prune_empty_dirs(library, dry_run=True)
    assert any(path.name == "videos" for path in empty)
    assert (library / "gelbooru" / "safe" / "videos").exists()  # ...without touching them.
    # ...and a real prune removes them.
    migrate.prune_empty_dirs(library)
    assert not (library / "gelbooru" / "safe" / "videos").exists()
    assert not (library / "rule34").exists()


def test_an_empty_root_is_not_an_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    plan = migrate.build_plan(missing)
    assert plan.total == 0
    assert migrate.apply_plan(plan).moved_count == 0
    assert migrate.old_source_dirs(missing) == []


# -- the command ------------------------------------------------------------
def _run_migrate(args: Namespace) -> int:
    from bdl.cli import cmd_migrate_layout

    return asyncio.run(cmd_migrate_layout(args))


def _args(root: Path, db_path: Path, **over) -> Namespace:
    base = {
        "out": str(root),
        "db": str(db_path),
        "dry_run": False,
        "json": False,
        "keep_empty": False,
    }
    base.update(over)
    return Namespace(**base)


def _seed_db(db_path: Path, root: Path, rows: list[tuple[str, str, int | None, int | None]]) -> None:
    """Record rows the way a download would, optionally with a shape."""

    async def seed() -> None:
        db = Database(db_path)
        await db.open()
        try:
            for uid, rel, width, height in rows:
                post = Post(id=uid.split(":")[1], source=uid.split(":")[0], file_url="x", rating="safe")
                await db.record_download(post, str(root / rel), 10, Path(rel).suffix, None, None)
                if width is not None:
                    await db.conn.execute(
                        "UPDATE downloads SET width = ?, height = ? WHERE uid = ?", (width, height, uid)
                    )
            await db.conn.commit()
        finally:
            await db.close()

    asyncio.run(seed())


def test_the_command_moves_files_and_rewrites_recorded_paths(library: Path, tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "downloads.sqlite3"
    _seed_db(
        db_path,
        library,
        [
            ("gelbooru:1", "gelbooru/safe/gelbooru_1_wide.jpg", 1920, 1080),
            # 16:9 in the database, and still a film: the recorded shape must
            # not be allowed to file it under wallpapers/.
            ("gelbooru:9", "gelbooru/safe/videos/gelbooru_9_clip.mp4", 1920, 1080),
        ],
    )

    code = _run_migrate(_args(library, db_path))
    assert code == 0
    out = capsys.readouterr().out
    assert "moved" in out

    async def read_paths() -> dict[str, str]:
        db = Database(db_path)
        await db.open()
        try:
            rows = await db._fetchall("SELECT uid, file_path FROM downloads")
            return dict(rows)
        finally:
            await db.close()

    paths = asyncio.run(read_paths())
    assert paths["gelbooru:1"] == str(library / "wallpapers" / "safe" / "gelbooru_1_wide.jpg")
    # The film went to the videos root, not into the wallpaper folder its
    # 16:9 shape would otherwise have suggested.
    assert paths["gelbooru:9"] == str(library / "videos" / "safe" / "gelbooru_9_clip.mp4")


def test_the_command_dry_run_changes_nothing(library: Path, tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "downloads.sqlite3"
    _seed_db(db_path, library, [("gelbooru:1", "gelbooru/safe/gelbooru_1_wide.jpg", 1920, 1080)])

    code = _run_migrate(_args(library, db_path, dry_run=True, json=True))
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["dry_run"] is True
    assert summary["planned"] == summary["moved"] > 0
    assert summary["db_rows_updated"] == 0
    # Files and rows are exactly where they were.
    assert (library / "gelbooru" / "safe" / "gelbooru_1_wide.jpg").is_file()

    async def read_path() -> str:
        db = Database(db_path)
        await db.open()
        try:
            rows = await db._fetchall("SELECT file_path FROM downloads")
            return rows[0][0]
        finally:
            await db.close()

    assert asyncio.run(read_path()) == str(library / "gelbooru" / "safe" / "gelbooru_1_wide.jpg")


def test_the_command_refuses_a_missing_root(tmp_path: Path, capsys) -> None:
    code = _run_migrate(_args(tmp_path / "nope", tmp_path / "d.sqlite3"))
    assert code == 2
    assert "not a directory" in capsys.readouterr().err


def test_keep_empty_leaves_the_source_folders(library: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "d.sqlite3"
    code = _run_migrate(_args(library, db_path, keep_empty=True, json=True))
    assert code == 0
    assert (library / "gelbooru").is_dir()
