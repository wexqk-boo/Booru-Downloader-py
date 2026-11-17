"""Migrate an old library into the current on-disk layout.

The old layout nested every file under its source::

    <out_root>/<source>/<rating>/[videos/]/<file>
    <out_root>/<source>/<rating>/json/<file>.json

The current one sorts by *shape* instead, because that is what the user
actually browses, and keeps every film together::

    <out_root>/<wallpapers|avatars>/<rating>/<file>
    <out_root>/<wallpapers|avatars>/<rating>/json/<file>.json
    <out_root>/videos/<rating>/<file>
    <out_root>/videos/<rating>/json/<file>.json

Classification of a still is by aspect ratio, read from the image itself with
Pillow — the database is not trusted for it, because a row written before the
folder existed can carry stale or missing dimensions. A film goes to ``videos/``
regardless of its shape, so nothing about it has to be measured or guessed.
Sidecars always follow the media file they describe, into that file's ``json/``
folder.

The module is free of database and CLI concerns: it moves files and reports
what it did, and the caller writes the new paths back.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .media import VIDEO_DIR, layout_paths, media_subdir

LOG = logging.getLogger(__name__)

#: Folders the new layout owns at the top level. A directory named like one of
#: these is already migrated and is left alone. The rating buckets and ``videos``
#: are included because the current layout also writes ``<out_root>/<rating>/``
#: for a file whose shape fits neither window — without them a second run would
#: mistake that folder for an old source tree.
LAYOUT_DIRS = frozenset({"wallpapers", "avatars", "videos", "safe", "suggestive", "nsfw"})

#: Ratio windows for "wallpaper" and "avatar", matching ``bdl.filters``. Kept as
#: plain numbers so this module imports without the filter stack.
WALLPAPER_RANGE: tuple[float, float] = (1.5, 1.8)
AVATAR_RANGE: tuple[float, float] = (0.6, 1.05)

@dataclass(slots=True)
class Move:
    """One file that the migration wants to relocate."""

    source_path: Path
    target_path: Path
    kind: str
    reason: str = ""

    @property
    def changed(self) -> bool:
        return self.source_path != self.target_path


@dataclass(slots=True)
class MigrationPlan:
    """What a migration would do, before anything on disk is touched."""

    moves: list[Move] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    conflicts: list[tuple[Path, Path]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.moves)

    def counts(self) -> dict[str, int]:
        """Moves per destination bucket, for the summary line."""
        counts: dict[str, int] = {}
        for move in self.moves:
            counts[move.kind] = counts.get(move.kind, 0) + 1
        return dict(sorted(counts.items()))


def image_aspect(path: Path) -> float | None:
    """width / height read from the file itself, or None when unreadable.

    Pillow is imported lazily: every command that does not classify images must
    keep working on a machine where it is missing.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - depends on the environment
        LOG.warning("Pillow is not installed; images cannot be classified by shape")
        return None
    try:
        with Image.open(path) as handle:
            width, height = handle.size
    except (OSError, ValueError):
        return None
    if not width or not height:
        return None
    return width / height


def classify(aspect: float | None) -> str:
    """Map an aspect ratio onto ``wallpapers``/``avatars``/"" (the root bucket)."""
    if aspect is None:
        return ""
    if WALLPAPER_RANGE[0] <= aspect <= WALLPAPER_RANGE[1]:
        return "wallpapers"
    if AVATAR_RANGE[0] <= aspect <= AVATAR_RANGE[1]:
        return "avatars"
    return ""


def _source_parts(path: Path, out_root: Path) -> tuple[str, str] | None:
    """``(source_dir, rating_dir)`` for a file in the old layout, else None."""
    try:
        parts = path.relative_to(out_root).parts
    except ValueError:
        return None
    if len(parts) < 3:
        return None
    source_dir, rating_dir = parts[0], parts[1]
    if source_dir in LAYOUT_DIRS:
        # Already in the current layout — there is nothing to migrate.
        return None
    return source_dir, rating_dir


def _kind_for(path: Path, rating_dir: str) -> str:
    """The destination bucket for a media file.

    A film has no shape to classify — it belongs in the ``videos`` root, which
    :func:`~bdl.media.layout_paths` derives from the extension alone. The name
    is returned rather than an empty string so the summary counts it under
    ``videos`` instead of lumping it in with the unclassifiable shapes.
    """
    del rating_dir
    if media_subdir(path.suffix):
        return VIDEO_DIR
    return classify(image_aspect(path))


def _sidecar_owner(path: Path) -> Path | None:
    """The media file a ``<name>.json`` sidecar belongs to, if any.

    Sidecars are named after the file they describe with ``.json`` appended, so
    ``123.jpg.json`` describes ``123.jpg``.
    """
    if path.suffix != ".json":
        return None
    media_name = path.name[: -len(".json")]
    if not media_name:
        return None
    candidate = path.with_name(media_name)
    return candidate if candidate.is_file() else None


def _targets(out_root: Path, rating_dir: str, kind: str, name: str, ext: str) -> tuple[Path, Path]:
    """``(media target, sidecar target)`` for a file's *name* and extension."""
    media_dir, sidecar_dir = layout_paths(out_root, rating_dir, ext, kind=kind)
    return media_dir / name, sidecar_dir / f"{name}.json"


def build_plan(out_root: Path) -> MigrationPlan:
    """Walk every old ``<out_root>/<source>/`` tree and plan the moves.

    Nothing is written and no image is opened twice: this is both the dry run
    and the first half of a real one.
    """
    plan = MigrationPlan()
    if not out_root.is_dir():
        return plan
    root = Path(out_root)
    for source_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        if source_dir.name in LAYOUT_DIRS:
            continue
        for path in sorted(source_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.name.startswith("."):
                # In-flight ``.part`` files and other dotfiles are not ours.
                plan.skipped.append((path, "temporary file"))
                continue
            _plan_one(plan, path, root)
    _flag_conflicts(plan)
    return plan


def _plan_one(plan: MigrationPlan, path: Path, root: Path) -> None:
    """Add the move for a single file, or record why it is skipped."""
    located = _source_parts(path, root)
    if located is None:
        plan.skipped.append((path, "not inside an old source folder"))
        return
    _, rating_dir = located

    if path.suffix == ".json":
        owner = _sidecar_owner(path)
        if owner is None:
            plan.skipped.append((path, "sidecar without its media file"))
            return
        kind = _kind_for(owner, rating_dir)
        _, sidecar_target = _targets(root, rating_dir, kind, owner.name, owner.suffix)
        plan.moves.append(Move(path, sidecar_target, kind or "root", reason="sidecar"))
        return

    kind = _kind_for(path, rating_dir)
    media_target, _ = _targets(root, rating_dir, kind, path.name, path.suffix)
    plan.moves.append(Move(path, media_target, kind or "root", reason="media"))


def _flag_conflicts(plan: MigrationPlan) -> None:
    """Record moves whose destination is already taken.

    Two files with the same name in the same bucket would silently overwrite one
    another; the caller reports these instead of losing a picture.
    """
    seen: dict[Path, Path] = {}
    for move in plan.moves:
        if not move.changed:
            continue
        clash = seen.get(move.target_path)
        if move.target_path.exists() or (clash is not None and clash != move.source_path):
            plan.conflicts.append((move.source_path, move.target_path))
        else:
            seen[move.target_path] = move.source_path


@dataclass(slots=True)
class ApplyResult:
    """What a migration actually did."""

    moved: list[Move] = field(default_factory=list)
    failed: list[tuple[Move, str]] = field(default_factory=list)

    @property
    def moved_count(self) -> int:
        return len(self.moved)


def apply_plan(plan: MigrationPlan, *, dry_run: bool = False) -> ApplyResult:
    """Move every planned file, reporting exactly what happened.

    With ``dry_run`` nothing is touched, but the returned result lists the same
    moves a real run would perform — the caller needs the destinations either
    way, because the database rows have to follow the files.
    """
    result = ApplyResult()
    conflicting = {source for source, _ in plan.conflicts}
    for move in plan.moves:
        if not move.changed:
            continue
        if move.source_path in conflicting:
            result.failed.append((move, "destination already taken"))
            LOG.warning("skipping %s: %s is already taken", move.source_path, move.target_path)
            continue
        if dry_run:
            result.moved.append(move)
            continue
        try:
            move.target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(move.source_path), str(move.target_path))
        except (OSError, shutil.Error) as exc:
            result.failed.append((move, str(exc)))
            LOG.warning("could not move %s: %s", move.source_path, exc)
            continue
        result.moved.append(move)
    return result


def old_source_dirs(out_root: Path) -> list[Path]:
    """Every old ``<out_root>/<source>/`` directory, migrated or not."""
    if not out_root.is_dir():
        return []
    return sorted(path for path in out_root.iterdir() if path.is_dir() and path.name not in LAYOUT_DIRS)


def prune_empty_dirs(out_root: Path, *, dry_run: bool = False) -> list[Path]:
    """Delete the old source folders once their files have moved out.

    Deepest first, so a tree of now-empty folders collapses in one pass and the
    source directory itself goes last.
    """
    removed: list[Path] = []
    root = Path(out_root)
    for source_dir in old_source_dirs(root):
        nested = [path for path in sorted(source_dir.rglob("*"), reverse=True) if path.is_dir()]
        for path in [*nested, source_dir]:
            if any(path.iterdir()):
                continue
            removed.append(path)
            if dry_run:
                continue
            try:
                path.rmdir()
            except OSError as exc:
                LOG.debug("could not remove %s: %s", path, exc)
    return removed
