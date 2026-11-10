"""Command line interface: search, dl, sync, status, blacklist, tags, tui."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json as jsonlib
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import aiohttp

from . import __version__
from .blacklist import add_tag, load_blacklist, remove_tag
from .config import (
    ClientSettingsShim,
    default_db_path,
    default_out_dir,
    env_first,
    load_credentials,
    load_proxy,
    xdg_state_home,
)
from .db import Database
from .downloader import BatchStats, Downloader
from .filters import AVATAR_ASPECT, WALLPAPER_ASPECT, parse_aspect_range
from .media import NSFW_RATINGS, SAFE_RATINGS, SUGGESTIVE_RATINGS
from .models import Post
from .notifier import notify
from .providers import ALL, PROVIDERS, CliProviderClient, ProviderError, resolve_sources

LOG = logging.getLogger("bdl")
SOURCE_CHOICES = [*sorted(PROVIDERS), ALL]

CATEGORY_RATINGS = {
    "safe": SAFE_RATINGS,
    "suggestive": SUGGESTIVE_RATINGS,
    "nsfw": NSFW_RATINGS,
}


def _default_log_file() -> Path:
    raw = env_first("BDL_LOG_FILE", "BOORU_DL_LOG_FILE")
    if raw:
        return Path(raw).expanduser()
    return xdg_state_home() / "bdl" / "bdl.log"


def _setup_logging(verbose: bool, log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite path (default: ~/.local/share/bdl/downloads.sqlite3)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=".env file with API keys (default: next to the package)",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="log file (default: ~/.local/state/bdl/bdl.log, or $BOORU_DL_LOG_FILE)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def _add_batch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=100, help="posts to fetch per source")
    parser.add_argument("--per-page", type=int, default=50, help="posts per API request")
    parser.add_argument("--out", type=Path, default=None, help="output root (one subfolder per source)")
    parser.add_argument("--concurrency", type=int, default=3, help="parallel downloads")
    parser.add_argument("--order", choices=["", "top"], default="", help="'top' = best of the week")
    parser.add_argument("--force", action="store_true", help="re-download files already recorded")
    parser.add_argument("--sidecar", action="store_true", help="write post metadata as JSON next to the file")
    parser.add_argument("--checksum", action="store_true", help="verify content sha256 (CPU heavy)")
    parser.add_argument("--min-score", type=int, default=None, help="skip posts scored lower")
    parser.add_argument("--max-size-mb", type=int, default=None, help="skip files larger than this")
    parser.add_argument("--timeout", type=float, default=600.0, help="per-file download timeout, seconds")

    aspect = parser.add_mutually_exclusive_group()
    aspect.add_argument(
        "--wallpapers",
        action="store_true",
        help="wide images only (aspect 1.5-1.8, i.e. 16:9 and 16:10)",
    )
    aspect.add_argument(
        "--avatars",
        "--icons",
        "--pfp",
        action="store_true",
        help="square or mildly portrait images only (1:1, 3:4, 2:3)",
    )
    parser.add_argument(
        "--aspect-ratio",
        default=None,
        metavar="MIN:MAX",
        help="custom width/height window, e.g. 1.5:1.8, 0.6:1.05 or 16:9",
    )
    parser.add_argument(
        "--aspect-unknown",
        choices=["keep", "skip"],
        default="keep",
        help="posts whose board omits width/height: keep them (default) or skip",
    )
    parser.add_argument(
        "--blacklist",
        default=None,
        help="extra tags to exclude, comma separated (on top of ~/.config/bdl/blacklist.txt)",
    )

    category = parser.add_mutually_exclusive_group()
    category.add_argument("--safe", action="store_true", help="safe ratings only (safe, s, general)")
    category.add_argument(
        "--suggestive",
        action="store_true",
        help="suggestive ratings only (questionable, q, sensitive)",
    )
    category.add_argument(
        "--nsfw",
        action="store_true",
        help="explicit ratings only (explicit, e)",
    )
    # Sits in the same group as the presets: hand-picking a rating set and
    # asking for a preset at the same time has no defined winner.
    category.add_argument(
        "--rating-include",
        default=None,
        metavar="RATINGS",
        help="allowed ratings, comma separated (safe,suggestive,questionable,explicit)",
    )
    parser.add_argument("--no-progress", action="store_true", help="disable the progress display")
    parser.add_argument(
        "--no-proxy-media",
        action="store_true",
        help="download media directly, bypassing the proxy (search still proxies)",
    )
    parser.add_argument("--no-notify", action="store_true", help="do not send desktop notifications")


def _prog_name() -> str:
    raw = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    return raw if raw in {"bdl", "booru-dl"} else "bdl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_prog_name(),
        description="Async batch downloader and sync tool for booru boards.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{parser.prog} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sp = sub.add_parser(
        "search", parents=[_common_parser()], help="search without downloading", conflict_handler="resolve"
    )
    sp.add_argument("-s", "--source", required=True, choices=SOURCE_CHOICES, help="source, or 'all'")
    sp.add_argument("-t", "--tags", required=True, help="space separated tags, quote them")
    sp.add_argument("--limit", type=int, default=50, help="posts to show per source")
    sp.add_argument("--per-page", type=int, default=50)
    sp.add_argument("--order", choices=["", "top"], default="")
    sp.add_argument("--json", action="store_true", help="machine readable output")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("dl", parents=[_common_parser()], help="download a batch", conflict_handler="resolve")
    sp.add_argument("-s", "--source", required=True, help=f"comma separated sources, or '{ALL}'")
    sp.add_argument("-t", "--tags", required=True, help="space separated tags, quote them")
    _add_batch_args(sp)
    sp.set_defaults(func=cmd_dl)

    sp = sub.add_parser(
        "sync",
        parents=[_common_parser()],
        help="periodically repeat a batch",
        conflict_handler="resolve",
    )
    sp.add_argument("-s", "--source", required=True, help=f"comma separated sources, or '{ALL}'")
    sp.add_argument("-t", "--tags", required=True, help="space separated tags, quote them")
    _add_batch_args(sp)
    sp.add_argument("--interval", type=int, default=3600, help="seconds between passes")
    sp.add_argument("--once", action="store_true", help="run a single pass and exit")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser(
        "status", parents=[_common_parser()], help="download statistics", conflict_handler="resolve"
    )
    sp.add_argument("--json", action="store_true", help="machine readable output")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser(
        "blacklist",
        parents=[_common_parser()],
        help="manage excluded tags",
        conflict_handler="resolve",
    )
    sp.add_argument("action", choices=["list", "add", "remove"], help="list, or add/remove <tag>")
    sp.add_argument("tag", nargs="?", default=None, help="tag for add/remove")
    sp.set_defaults(func=cmd_blacklist)

    sp = sub.add_parser(
        "prune-stale",
        parents=[_common_parser()],
        help="forget database rows whose files are gone",
        conflict_handler="resolve",
    )
    sp.add_argument("--dry-run", action="store_true", help="report only, do not modify")
    sp.add_argument("--all", action="store_true", help="check every recorded path, not just --out")
    sp.add_argument("--out", type=Path, default=None, help="limit the check to this output root")
    sp.add_argument("--json", action="store_true", help="machine readable output")
    sp.set_defaults(func=cmd_prune_stale)

    sp = sub.add_parser(
        "config", parents=[_common_parser()], help="CLI settings", conflict_handler="resolve"
    )
    sp.add_argument("action", choices=["get", "set", "show"], help="get <key> / set <key> <value> / show")
    sp.add_argument("key", nargs="?", default=None, help="setting name (e.g. out)")
    sp.add_argument("value", nargs="?", default=None, help="value for set")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser(
        "tags",
        parents=[_common_parser()],
        help="tag suggestions (empty prefix lists popular tags)",
        conflict_handler="resolve",
    )
    sp.add_argument("prefix", nargs="?", default=None, help="tag fragment (empty lists popular tags)")
    sp.add_argument("-n", "--num", type=int, default=25, help="how many suggestions")
    sp.add_argument("--pick", action="store_true", help="pick through fzf")
    sp.add_argument("--source", default=None, help="include tags native to a source (e.g. konachan.net)")
    sp.add_argument("--all", action="store_true", help="dump the whole local tag database")
    sp.add_argument("--update", action="store_true", help="force refresh of the tag database")
    sp.set_defaults(func=cmd_tags)

    sp = sub.add_parser(
        "tui", parents=[_common_parser()], help="interactive wizard (questionary)", conflict_handler="resolve"
    )
    _add_batch_args(sp)
    sp.set_defaults(func=cmd_tui)

    return parser


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def _label(key: str) -> str:
    provider = PROVIDERS.get(key)
    return provider.title if provider else key


def _print_failed_providers(failed: list[str]) -> None:
    """Board-level problems, listed after the summary.

    A board that is down (503), throttles or rejects the key only costs its own
    batch; the remaining sources keep downloading and the reason shows up here.
    """
    if not failed:
        return
    print("\nSources with errors (the rest of the batch is unaffected):")
    for item in failed:
        print(f"  x {item}")


def _print_summary(all_stats: dict[str, BatchStats]) -> None:
    if not all_stats:
        print("Nothing downloaded: every source failed.")
        return
    print("\n=== Batch summary ===")
    for key, stats in all_stats.items():
        print(
            f"  {_label(key)}: found={stats.found}, downloaded={stats.downloaded}, "
            f"skipped={stats.skipped}, filtered={stats.filtered}, failed={stats.failed}"
        )
        if stats.unknown_aspect:
            print(f"      kept {stats.unknown_aspect} without board dimensions")
        if stats.reported == 0:
            print("      no results — the tag may not exist on this board")
        for post, error in stats.failures[:3]:
            print(f"      x {post.uid}: {error}")
        if len(stats.failures) > 3:
            print(f"      ... and {len(stats.failures) - 3} more errors")

    downloaded = sum(stats.downloaded for stats in all_stats.values())
    skipped = sum(stats.skipped for stats in all_stats.values())
    if downloaded == 0 and skipped > 0:
        print("Nothing new: everything is already downloaded. Try other tags or --force.")


def _notify_body(all_stats: dict[str, BatchStats]) -> str:
    lines = []
    total_new = total_skip = total_fail = 0
    for key, stats in all_stats.items():
        provider = PROVIDERS.get(key)
        lines.append(
            f"{provider.short if provider else key}: +{stats.downloaded}, "
            f"skipped {stats.skipped}, failed {stats.failed}"
        )
        total_new += stats.downloaded
        total_skip += stats.skipped
        total_fail += stats.failed
    return "\n".join([f"downloaded {total_new}, skipped {total_skip}, failed {total_fail}", *lines])


def resolve_aspect_range(args: argparse.Namespace) -> tuple[float, float] | None:
    """Aspect window from ``--aspect-ratio`` / ``--wallpapers`` / ``--avatars``.

    An explicit ``--aspect-ratio`` overrides the presets. Returns None when no
    filtering was requested, and raises ValueError on a malformed range.
    """
    preset = getattr(args, "aspect_range", None)
    if preset is not None:
        return preset
    if getattr(args, "aspect_ratio", None):
        return parse_aspect_range(args.aspect_ratio)
    if getattr(args, "wallpapers", False):
        return WALLPAPER_ASPECT
    if getattr(args, "avatars", False):
        return AVATAR_ASPECT
    return None


def resolve_rating_set(args: argparse.Namespace) -> set[str] | None:
    if getattr(args, "safe", False):
        return set(SAFE_RATINGS)
    if getattr(args, "suggestive", False):
        return set(SUGGESTIVE_RATINGS)
    if getattr(args, "nsfw", False):
        return set(NSFW_RATINGS)
    raw = getattr(args, "rating_include", None)
    if raw:
        ratings = {chunk.strip().lower() for chunk in raw.split(",") if chunk.strip()}
        if not ratings:
            raise ValueError("--rating-include names no ratings")
        return ratings
    return None


def check_batch_args(args: argparse.Namespace) -> str | None:
    """Return a message when the batch flags contradict each other.

    argparse already rejects the preset combinations; this covers the values
    that only fail once parsed.
    """
    try:
        resolve_aspect_range(args)
    except ValueError as exc:
        return f"--aspect-ratio: {exc}"
    try:
        resolve_rating_set(args)
    except ValueError as exc:
        return str(exc)
    source = getattr(args, "source", None)
    if source is not None:
        try:
            resolve_sources(source)
        except ProviderError as exc:
            return str(exc)
    return None


async def _execute_batch(
    args: argparse.Namespace, db: Database
) -> tuple[dict[str, BatchStats], bool, list[str]]:
    creds = load_credentials(args.env_file)
    proxy = load_proxy(args.env_file)
    providers = resolve_sources(args.source)
    # The boards cannot filter by geometry, so width/height are checked locally.
    aspect_range = resolve_aspect_range(args)
    rating_set = resolve_rating_set(args)
    blacklist = load_blacklist()
    if args.blacklist:
        from .blacklist import expand_tag

        for chunk in args.blacklist.split(","):
            if chunk.strip():
                blacklist |= expand_tag(chunk)
    out_root = args.out or default_out_dir()
    shim = ClientSettingsShim(credentials=creds, proxy=proxy)

    # Keep the database in step with the folder: rows for deleted files go.
    # Scoped to this run's output root, so a throwaway --out cannot wipe the
    # history of the real library.
    pruned = await db.prune_stale(under=out_root)
    if pruned.get("total"):
        LOG.info("pruned %d rows for deleted files", pruned["total"])

    all_stats: dict[str, BatchStats] = {}
    all_sources_failed = False
    failed_providers: list[str] = []

    async with (
        CliProviderClient(shim, max_concurrency=4) as client,
        Downloader(
            db,
            out_root=out_root,
            concurrency=args.concurrency,
            media_timeout=args.timeout,
            proxy=("" if args.no_proxy_media else proxy),
            force=args.force,
            sidecar=args.sidecar,
            checksum=args.checksum,
            max_size_mb=args.max_size_mb,
            min_score=args.min_score,
            rating_include=rating_set,
            blacklist=blacklist,
            aspect_range=aspect_range,
            aspect_unknown=args.aspect_unknown,
            progress=not args.no_progress,
        ) as downloader,
    ):
        for provider in providers:
            run_id = await db.start_run(provider.key, args.tags)
            try:
                stats = await downloader.run_batch(
                    client,
                    provider,
                    args.tags,
                    limit=args.limit,
                    per_page=args.per_page,
                    order=args.order,
                )
            except ProviderError as exc:
                # A board can be down, throttling or rejecting the key; that
                # only costs its own batch, so record the reason and move on.
                all_sources_failed = True
                failed_providers.append(f"{provider.key} ({exc})")
                LOG.warning("%s: %s", provider.title, exc)
                await db.finish_run(run_id, found=0, filtered=0, downloaded=0, skipped=0, failed=0)
                continue
            await db.finish_run(
                run_id,
                found=stats.found,
                filtered=stats.filtered,
                downloaded=stats.downloaded,
                skipped=stats.skipped,
                failed=stats.failed,
            )
            all_stats[provider.key] = stats
    return all_stats, all_sources_failed, failed_providers


async def cmd_search(args: argparse.Namespace) -> int:
    shim = ClientSettingsShim(credentials=load_credentials(args.env_file), proxy=load_proxy(args.env_file))
    found: list[tuple[str, Post]] = []
    async with CliProviderClient(shim, max_concurrency=4) as client:
        for provider in resolve_sources(args.source):
            posts = await Downloader.collect_posts(
                client, provider.key, args.tags, args.limit, args.per_page, args.order
            )
            found.extend((provider.key, post) for post in posts)

    if args.json:
        payload = [
            {
                "source": src,
                "id": post.id,
                "uid": post.uid,
                "url": post.file_url,
                "tags": list(post.tags),
                "rating": post.rating,
                "width": post.width,
                "height": post.height,
                "score": post.score,
            }
            for src, post in found
        ]
        print(jsonlib.dumps(payload, ensure_ascii=False, indent=2))
    elif not found:
        print("No results (the tag may not exist on these boards).")
    else:
        for _, post in found:
            dims = f"{post.width}x{post.height}" if post.width and post.height else "-"
            score = post.score if post.score is not None else "-"
            print(f"{post.uid:<26} {dims:>10} r={post.rating or '-':<4} s={score}  {' '.join(post.tags[:6])}")
            print(f"    {post.file_url}")
    return 0


async def cmd_dl(args: argparse.Namespace) -> int:
    db = Database(args.db or default_db_path())
    await db.open()
    try:
        all_stats, all_sources_failed, failed_providers = await _execute_batch(args, db)
    finally:
        await db.close()

    _print_summary(all_stats)
    _print_failed_providers(failed_providers)
    if all_stats and not args.no_notify:
        await notify("bdl: batch finished", _notify_body(all_stats))
    return 1 if all_sources_failed and not all_stats else 0


async def cmd_sync(args: argparse.Namespace) -> int:
    while True:
        db = Database(args.db or default_db_path())
        await db.open()
        try:
            all_stats, all_sources_failed, failed_providers = await _execute_batch(args, db)
        finally:
            await db.close()

        _print_summary(all_stats)
        _print_failed_providers(failed_providers)
        if all_stats and not args.no_notify:
            await notify("bdl: sync", _notify_body(all_stats))
        if args.once:
            return 1 if all_sources_failed and not all_stats else 0

        LOG.info("next pass in %d s (Ctrl+C to stop)", args.interval)
        try:
            await asyncio.sleep(args.interval)
        except asyncio.CancelledError:
            return 0


async def cmd_tui(args: argparse.Namespace) -> int:
    """Ask the wizard for a batch, then run it through the normal path."""
    from .i18n import t
    from .tui import run_wizard

    try:
        answers = await run_wizard()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not answers:
        print(t("tui.cancelled"))
        return 130

    args.source = ",".join(answers["sources"])
    args.tags = answers["tags"]
    args.limit = answers["limit"]
    args.out = answers.get("out") or args.out
    args.concurrency = answers.get("concurrency") or args.concurrency
    args.order = answers.get("order") or args.order
    options = answers.get("options") or {}
    args.sidecar = bool(options.get("sidecar"))
    args.checksum = bool(options.get("checksum"))
    args.force = bool(options.get("force"))
    args.no_notify = not options.get("notify", True)
    # The wizard already resolved the geometry and wrote the blacklist file.
    args.aspect_range = answers.get("aspect")
    args.aspect_ratio = None
    args.wallpapers = args.avatars = False
    args.blacklist = None
    rating = answers.get("rating")
    args.safe = rating == "safe"
    args.suggestive = rating == "suggestive"
    args.nsfw = rating == "nsfw"
    args.rating_include = None
    return await cmd_dl(args)


async def cmd_status(args: argparse.Namespace) -> int:
    db = Database(args.db or default_db_path())
    await db.open()
    try:
        summary = await db.summary()
    finally:
        await db.close()

    if args.json:
        print(jsonlib.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print("== Files per source ==")
    if not summary["sources"]:
        print("  (empty — nothing downloaded yet)")
    for row in summary["sources"]:
        print(f"  {row['source']:<14} files={row['files']:<6} {fmt_bytes(row['bytes'])}")

    totals = summary["totals"]
    print(f"TOTAL: files={totals['files']}, {fmt_bytes(totals['bytes'])}")

    print("== Recent runs ==")
    for run in summary["runs"]:
        print(
            f"  #{run['id']} {run['source']:<12} '{run['tags'][:40]}' "
            f"found={run['found']} downloaded={run['downloaded']} "
            f"skipped={run['skipped']} failed={run['failed']}"
        )
    if summary["failures"]:
        print("== Failures per source ==")
        for failure in summary["failures"]:
            print(f"  {failure['source']}: {failure['count']} (last at {failure['last_at']})")
    return 0


async def cmd_blacklist(args: argparse.Namespace) -> int:
    tags = load_blacklist()
    if args.action == "list":
        if not tags:
            print("Blacklist is empty (file: ~/.config/bdl/blacklist.txt)")
            return 0
        _print_blacklist(tags)
        return 0

    if not args.tag:
        print("Usage: bdl blacklist add|remove <tag>", file=sys.stderr)
        return 2

    tag = args.tag.strip().lower()
    if args.action == "add":
        changed = add_tag(args.tag)
        print(f"added {changed} tags for {tag!r}" if changed else f"{tag!r} is already blacklisted")
    else:
        changed = remove_tag(args.tag)
        print(f"removed {changed} tags for {tag!r}" if changed else f"{tag!r} was not blacklisted")
    return 0


def _print_blacklist(tags: set[str]) -> None:
    """List the blacklist grouped by family, previewing what each root pulls in."""
    from .tags.families import TAG_FAMILIES, family_preview

    members: dict[str, str] = {}
    for family, extra in TAG_FAMILIES.items():
        for tag in extra:
            members.setdefault(tag, family)
    # A root tag names its own family, so `ai` belongs with `ai_generated`.
    for family in TAG_FAMILIES:
        members.setdefault(family, family)

    known = sorted(tags, key=lambda tag: (members.get(tag, tag), tag))
    width = max((len(tag) for tag in known), default=0)
    for tag in known:
        family = members.get(tag)
        if tag in TAG_FAMILIES:
            # The root is the family: show which tags it actually blocks.
            print(f"  {tag:<{width}}  {family_preview(tag)}")
        elif family:
            print(f"  {tag:<{width}}  {family}")
        else:
            print(f"  {tag}")


async def cmd_prune_stale(args: argparse.Namespace) -> int:
    db = Database(args.db or default_db_path())
    await db.open()
    try:
        # Without --all we only touch paths under the chosen output root.
        scope = None if args.all else (args.out or default_out_dir())
        result = await db.prune_stale(dry_run=args.dry_run, under=scope)
    finally:
        await db.close()

    if args.json:
        print(jsonlib.dumps(result, ensure_ascii=False, indent=2))
        return 0
    total = result.pop("total", 0)
    print(f"Prune: {'found' if args.dry_run else 'forgot'} {total} rows for deleted files")
    for source, count in sorted(result.items()):
        print(f"  {source}: {count}")
    return 0


async def cmd_config(args: argparse.Namespace) -> int:
    from .config import get_setting, load_config, set_setting

    if args.action == "show":
        values = load_config()
        if not values:
            print("No settings (file: ~/.config/bdl/config.json)")
        for key, value in sorted(values.items()):
            print(f"{key}={value}")
        return 0
    if args.action == "get":
        if not args.key:
            print("Usage: bdl config get <key>", file=sys.stderr)
            return 2
        print(get_setting(args.key))
        return 0
    if args.action == "set":
        if not args.key or args.value is None:
            print("Usage: bdl config set <key> <value>", file=sys.stderr)
            return 2
        set_setting(args.key, args.value)
        print(f"{args.key}={args.value}")
        return 0
    return 0


async def cmd_tags(args: argparse.Namespace) -> int:
    from .tags.commands import local_hints_async, popular_tags
    from .tags.store import ensure_db, fetch_site_tags
    from .tags.suggest import suggest_tags

    await ensure_db(force=args.update)

    if args.all:
        from .tags.store import load_db

        data = await load_db()
        hints = data.names if data is not None else await popular_tags(max(args.num, 100))
        for tag in hints:
            print(tag)
        return 0

    limit = max(1, min(args.num, 50))
    if args.prefix:
        # Local database first: it is not board specific and never rate limits.
        hints = await local_hints_async(args.prefix, limit)
        if not hints:
            try:
                hints = await suggest_tags(args.prefix, limit)
            finally:
                from .tags.suggest import close_session

                await close_session()
        if args.source:
            creds = load_credentials(args.env_file).get(args.source)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30), proxy=load_proxy(args.env_file) or None
            ) as session:
                site_tags = await fetch_site_tags(
                    args.source, session, args.prefix, creds.for_url() if creds else None
                )
            if site_tags:
                seen = set(hints)
                for tag in site_tags:
                    if tag.startswith(args.prefix.strip().lower()) and tag not in seen:
                        seen.add(tag)
                        hints.append(tag)
                    if len(hints) >= limit:
                        break
    else:
        hints = await popular_tags(limit)

    if not hints:
        print("No suggestions.")
        return 0

    if args.pick:
        import subprocess

        try:
            proc = subprocess.run(
                ["fzf", "--multi", "--query", args.prefix or ""],
                input="\n".join(hints) + "\n",
                text=True,
                capture_output=True,
            )
        except FileNotFoundError:
            print("fzf not found, showing the list:")
        else:
            for line in proc.stdout.splitlines():
                if line.strip():
                    print(line.strip())
            return 0

    for i, tag in enumerate(hints, start=1):
        print(f"{i:>2}. {tag}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # Bare `bdl` opens the wizard, but only on a terminal: a script that
    # forgot its subcommand should get usage, not a prompt it cannot answer.
    if not argv:
        if not sys.stdin.isatty():
            parser.print_help()
            return 2
        argv = ["tui"]
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.log_file or _default_log_file())
    problem = check_batch_args(args)
    if problem:
        print(f"{_prog_name()}: error: {problem}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # stdout was closed early (e.g. `... | head`); that is not an error.
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
