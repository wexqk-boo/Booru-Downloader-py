"""Unit tests for bdl: no network (sanitizing, parsing, DB, sidecar)."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bdl.db import Database
from bdl.downloader import Downloader, rating_bucket
from bdl.filters import (
    AVATAR_ASPECT,
    WALLPAPER_ASPECT,
    aspect_matches,
    aspect_ratio,
    parse_aspect_range,
)
from bdl.meta import ext_for_file_url, filename_for, slugify, write_sidecar
from bdl.models import Post
from bdl.providers import PROVIDERS, ProviderError, resolve_sources


# File names
def test_slugify() -> None:
    assert slugify("hatsune miku!!") == "hatsune_miku"
    assert slugify("a---b") == "a_b"
    assert slugify("  ") == "untagged"
    assert slugify("日本語タグ") == "untagged"  # non-ASCII → empty → untagged
    assert slugify("x" * 100) == "x" * 48


def test_ext_for_file_url() -> None:
    assert ext_for_file_url("https://img.example.com/img/1234.jpg") == ".jpg"
    assert ext_for_file_url("https://img.example.com/a/b.PNG") == ".png"
    assert ext_for_file_url("https://x/page?f=1") == ""
    assert ext_for_file_url("https://x/f") == ""


def test_filename_for() -> None:
    post = Post(id="5", source="xbooru", file_url="https://x/5.png", tags=("hatsune miku", "vocaloid"))
    assert filename_for(post, ".png") == "xbooru_5_hatsune_miku.png"

    bare = Post(id="7", source="gelbooru", file_url="https://g/7.jpg")
    assert filename_for(bare, ".jpg") == "gelbooru_7.jpg"


# Sources
def test_yandere_build_url_moebooru_no_auth() -> None:
    provider = PROVIDERS["yandere"]
    url = provider.build_url("hatsune_miku", 3, 10, {})
    assert provider.endpoint in url
    assert "page=3" in url
    assert "limit=10" in url
    assert "login=" not in url
    assert "api_key=" not in url


def test_yandere_build_url_adds_credentials() -> None:
    provider = PROVIDERS["yandere"]
    url = provider.build_url("miku", 1, 5, {"login": "alice", "api_key": "KEY"})
    assert "login=alice" in url
    assert "api_key=KEY" in url


def test_yandere_parse_moebooru() -> None:
    provider = PROVIDERS["yandere"]
    payload = [
        {
            "id": 5,
            "file_url": "https://yande.re/image/x.jpg",
            "preview_url": "/preview/x.jpg",
            "tags": "hatsune_miku vocaloid",
            "score": 7,
            "width": 100,
            "height": 200,
        }
    ]
    post = provider.parse(payload)[0]
    assert post.uid == "yandere:5"
    assert post.file_url == "https://yande.re/image/x.jpg"
    assert post.preview_url == "https://yande.re/preview/x.jpg"
    assert post.tags == ("hatsune_miku", "vocaloid")
    assert post.score == 7


def test_konachan_registered_from_bot() -> None:
    assert PROVIDERS["konachan"].kind == "moebooru"
    assert PROVIDERS["konachan"].endpoint == "https://konachan.com/post.json"


def test_tbib_legacy_parse() -> None:
    provider = PROVIDERS["tbib"]
    assert provider.kind == "gelbooru_legacy"
    payload = [{"id": 1, "directory": 1835, "image": "abc.png", "tags": "stockings", "width": 10, "height": 20}]
    post = provider.parse(payload)[0]
    assert post.file_url == "https://tbib.org/images/1835/abc.png"
    assert post.uid == "tbib:1"


def test_e621_build_url() -> None:
    provider = PROVIDERS["e621"]
    url = provider.build_url("miku", 1, 100, {})
    assert url == "https://e621.net/posts.json?tags=miku&limit=100"
    top = provider.build_url("miku", 1, 100, {}, order="top")
    assert "order%3Ascore" in top


def test_e621_parse_payload() -> None:
    provider = PROVIDERS["e621"]
    payload = {
        "posts": [
            {
                "id": 6355443,
                "rating": "e",
                "file": {
                    "width": 1920,
                    "height": 1080,
                    "ext": "jpg",
                    "url": "https://static1.e621.net/data/ab/cd/abcd1234.jpg",
                },
                "preview": {"url": "https://static1.e621.net/preview/ab/cd/abcd1234.jpg"},
                "tags": {
                    "general": ["vocaloid"],
                    "character": ["hatsune_miku"],
                    "artist": [],
                },
                "score": {"total": 439},
            }
        ]
    }
    post = provider.parse(payload)[0]
    assert post.uid == "e621:6355443"
    assert post.file_url == "https://static1.e621.net/data/ab/cd/abcd1234.jpg"
    assert post.preview_url == "https://static1.e621.net/preview/ab/cd/abcd1234.jpg"
    assert post.tags == ("vocaloid", "hatsune_miku")
    assert post.rating == "e"
    assert post.score == 439
    assert post.width == 1920


def test_danbooru_build_url() -> None:
    provider = PROVIDERS["danbooru"]
    url = provider.build_url("miku", 1, 100, {})
    assert url == "https://danbooru.donmai.us/posts.json?tags=miku&limit=100&page=1"
    assert provider.build_url("miku", 2, 20, {}).endswith("page=2")
    assert "search%5Border%5D=score" in provider.build_url("miku", 1, 100, {}, order="top")


def test_danbooru_parse_payload() -> None:
    provider = PROVIDERS["danbooru"]
    payload = [
        {
            "id": 7,
            "rating": "s",
            "tag_string": "vocaloid hatsune_miku",
            "file_url": "https://cdn.donmai.us/original/ab/cd/abcd.jpg",
            "preview_file_url": "https://cdn.donmai.us/preview/ab/cd/abcd.jpg",
            "image_width": 1920,
            "image_height": 1080,
            "file_size": 12345,
            "score": 12,
        },
        # A banned post carries no media and must be dropped, not crash.
        {"id": 8, "rating": "s", "tag_string": "cat"},
    ]
    posts = provider.parse(payload)
    assert len(posts) == 1
    post = posts[0]
    assert post.uid == "danbooru:7"
    assert post.preview_url == "https://cdn.donmai.us/preview/ab/cd/abcd.jpg"
    assert post.tags == ("vocaloid", "hatsune_miku")
    assert post.width == 1920
    assert post.score == 12


def test_danbooru_media_asset_fallback() -> None:
    provider = PROVIDERS["danbooru"]
    payload = [
        {
            "id": 9,
            "rating": "g",
            "tag_string": "scenery",
            "media_asset": {
                "image_width": 800,
                "image_height": 600,
                "preview_url": "https://cdn.donmai.us/preview/xy.jpg",
                "variants": [
                    {"type": "sample", "url": "https://cdn.donmai.us/sample/xy.jpg"},
                    {"type": "original", "url": "/original/xy.jpg"},
                ],
            },
        }
    ]
    post = provider.parse(payload)[0]
    assert post.file_url == "https://danbooru.donmai.us/original/xy.jpg"
    assert post.preview_url == "https://cdn.donmai.us/preview/xy.jpg"
    assert post.width == 800


def test_resolve_sources() -> None:
    assert [p.key for p in resolve_sources("xbooru,yandere,tbib,e621")] == ["xbooru", "yandere", "tbib", "e621"]
    assert [p.key for p in resolve_sources("all")] == list(PROVIDERS)
    assert [p.key for p in resolve_sources("danbooru,safebooru")] == ["danbooru", "safebooru"]
    assert [p.key for p in resolve_sources("rule34")] == ["rule34"]
    with pytest.raises(ProviderError):
        resolve_sources("not-a-board")
    with pytest.raises(ProviderError):
        resolve_sources("")


# DB
def test_db_roundtrip(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(tmp_path / "t.sqlite3")
        await db.open()
        try:
            post = Post(id="1", source="xbooru", file_url="https://x/1.png", tags=("a",), rating="safe")
            assert not await db.has(post.uid)

            await db.record_download(post, "/tmp/x.png", 10, ".png", None)
            assert await db.has(post.uid)

            summary = await db.summary()
            assert summary["totals"]["files"] == 1
            assert summary["sources"][0]["source"] == "xbooru"

            run_id = await db.start_run("xbooru", "miku")
            assert isinstance(run_id, int)
            await db.finish_run(run_id, found=1, filtered=0, downloaded=1, skipped=0, failed=0)
            summary2 = await db.summary()
            assert summary2["runs"][0]["downloaded"] == 1

            await db.record_failure(post, "тестовая ошибка")
            summary3 = await db.summary()
            assert summary3["failures"][0]["count"] == 1
        finally:
            await db.close()

    asyncio.run(scenario())


# Sidecar
def test_sidecar_write(tmp_path) -> None:
    post = Post(
        id="9",
        source="rule34",
        file_url="https://r/9.jpg",
        tags=("x",),
        rating="explicit",
        width=1,
        height=2,
        score=3,
    )
    target = tmp_path / "rule34_9_x.jpg.json"
    write_sidecar(target, post, 123.0, "abc")
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["uid"] == "rule34:9"
    assert data["sha256"] == "abc"
    assert data["tags"] == ["x"]
    assert data["rating"] == "explicit"
    assert data["width"] == 1


# Subfolders: safe/nsfw, video, sidecar
def test_rating_bucket() -> None:
    from bdl.downloader import rating_bucket

    assert rating_bucket("safe") == "safe"
    assert rating_bucket("s") == "safe"
    assert rating_bucket("general") == "safe"
    assert rating_bucket("SAFE") == "safe"
    assert rating_bucket("questionable") == "suggestive"
    assert rating_bucket("explicit") == "nsfw"
    assert rating_bucket("q") == "suggestive"
    assert rating_bucket("e") == "nsfw"
    assert rating_bucket("sensitive") == "suggestive"
    assert rating_bucket(None) == "nsfw"


def test_media_paths_buckets(tmp_path) -> None:
    from bdl.downloader import media_paths

    safe_img = Post(id="1", source="xbooru", file_url="https://x/1.png", rating="safe")
    safe_video = Post(id="2", source="xbooru", file_url="https://x/2.mp4", rating="s")
    nsfw_img = Post(id="3", source="xbooru", file_url="https://x/3.jpg", rating="explicit")
    nsfw_video = Post(id="4", source="xbooru", file_url="https://x/4.webm", rating="explicit")

    assert media_paths(tmp_path, "xbooru", safe_img, ".png") == (
        tmp_path / "xbooru" / "safe",
        tmp_path / "xbooru" / "safe" / "json",
    )
    assert media_paths(tmp_path, "xbooru", safe_video, ".mp4") == (
        tmp_path / "xbooru" / "safe" / "videos",
        tmp_path / "xbooru" / "safe" / "json",
    )
    assert media_paths(tmp_path, "xbooru", nsfw_img, ".jpg") == (
        tmp_path / "xbooru" / "nsfw",
        tmp_path / "xbooru" / "nsfw" / "json",
    )
    assert media_paths(tmp_path, "xbooru", nsfw_video, ".webm") == (
        tmp_path / "xbooru" / "nsfw" / "videos",
        tmp_path / "xbooru" / "nsfw" / "json",
    )
    suggestive = Post(id="5", source="xbooru", file_url="https://x/5.jpg", rating="questionable")
    assert media_paths(tmp_path, "xbooru", suggestive, ".jpg") == (
        tmp_path / "xbooru" / "suggestive",
        tmp_path / "xbooru" / "suggestive" / "json",
    )


def test_fmt_bytes_and_blocks() -> None:
    from bdl.downloader import _blocks, _fmt_bytes

    assert _fmt_bytes(512) == "512.0 B"
    assert _fmt_bytes(1536) == "1.5 KiB"
    assert _fmt_bytes(5 * 1024 * 1024) == "5.0 MiB"
    assert _blocks(50).startswith("████████")
    assert len(_blocks(0)) == 16


def test_progress_counts_are_posts_not_bytes() -> None:
    """The aggregate row shows "44 / 50", never a byte figure for post counts."""
    from rich.text import Text

    from bdl.downloader import TransferProgress

    bar = TransferProgress()
    bar.start("Gelbooru", 50)
    bar._progress.update(bar._agg_task, completed=44)
    line = next(iter(bar._progress.get_renderables()))
    rendered = line.plain if isinstance(line, Text) else str(line)

    assert "44 / 50" in rendered
    assert "44.0 B" not in rendered


def test_progress_row_leaves_when_the_file_finishes() -> None:
    """A completed file is removed from the display, not left behind."""
    from bdl.downloader import TransferProgress

    bar = TransferProgress()
    bar.start("Gelbooru", 4)
    task_id = bar.begin_file("done.png", 1024)
    bar.chunk(task_id, 1024)
    assert len(bar._progress.tasks) == 2

    bar.finish_file(task_id)
    assert [task.id for task in bar._progress.tasks] == [bar._agg_task]
    # A late chunk for a removed row must not raise or resurrect it.
    bar.chunk(task_id, 512)
    assert len(bar._progress.tasks) == 1


def test_progress_line_fits_the_terminal() -> None:
    """Rows are clipped, so a repaint never wraps and scrolls the terminal."""
    from bdl.downloader import TransferProgress

    bar = TransferProgress()
    bar.start("Gelbooru", 12)
    task_id = bar.begin_file("x" * 120 + ".jpg", 4096)
    bar.chunk(task_id, 2048)
    frame = next(iter(bar._progress.get_renderables()))
    lines = frame.plain.split("\n")

    assert len(lines) == 2, "one line per visible task"
    for line in lines:
        assert len(line) <= max(24, bar._progress._width())


def test_media_subdir() -> None:
    from bdl.downloader import media_subdir

    assert media_subdir(".jpg") == ""
    assert media_subdir(".png") == ""
    assert media_subdir(".jpeg") == ""
    assert media_subdir(".mp4") == "videos"
    assert media_subdir(".WEBM") == "videos"
    assert media_subdir(".mov") == "videos"
    assert media_subdir(".mkv") == "videos"


def test_sidecar_goes_to_json_dir(tmp_path) -> None:
    """Sidecar is written to json/, not next to the media."""
    from bdl.downloader import SIDECAR_DIR
    from bdl.meta import write_sidecar

    post = Post(id="1", source="xbooru", file_url="https://x/1.png", tags=("a",), rating="safe")
    target = tmp_path / SIDECAR_DIR / "xbooru_1_a.png.json"
    write_sidecar(target, post, 123.0, None)
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8"))["uid"] == "xbooru:1"


# Blacklist
def test_blacklist_filters_posts() -> None:
    from bdl.downloader import Downloader

    downloader = Downloader(object(), out_root=Path("/tmp"), blacklist={"loli", "guro"})
    ok_post = Post(id="1", source="x", file_url="https://x/1.jpg", tags=("miku",))
    bad_post = Post(id="2", source="x", file_url="https://x/2.jpg", tags=("miku", "Loli"))
    assert downloader.passes_filters(ok_post)
    assert not downloader.passes_filters(bad_post)


def test_blacklist_file_roundtrip(tmp_path) -> None:
    from bdl.blacklist import add_tag, load_blacklist, remove_tag, save_blacklist

    path = tmp_path / "blacklist.txt"
    assert load_blacklist(path) == set()

    save_blacklist({"Loli", "guro"}, path)
    assert load_blacklist(path) == {"loli", "guro"}

    assert add_tag("scat", path) == 1
    assert add_tag("Scat", path) == 0  # already there, same case
    assert load_blacklist(path) == {"loli", "guro", "scat"}

    assert remove_tag("loli", path) == 1
    assert load_blacklist(path) == {"guro", "scat"}
    assert remove_tag("nonexistent", path) == 0


def test_blacklist_family_expansion(tmp_path) -> None:
    from bdl.blacklist import add_tag, load_blacklist, remove_tag

    path = tmp_path / "bl-family.txt"

    added = add_tag("ai", path)
    tags = load_blacklist(path)
    assert added >= 5
    assert "ai_generated" in tags
    assert "midjourney" in tags

    removed = remove_tag("ai", path)
    assert removed == added
    assert load_blacklist(path) == set()


def test_family_preview_lists_members() -> None:
    from bdl.tags.families import family_preview

    preview = family_preview("ai")
    assert preview.startswith("(+ ")
    assert "ai_generated" in preview
    assert "more)" in preview

    # A short family is listed in full, without a trailing count.
    assert family_preview("fart") == "(+ farting, flatulence, gass, toot)"

    # A tag that owns no sub-tags gets no note at all.
    assert family_preview("hatsune_miku") == ""


# Output root resolution
def test_default_out_dir_env_then_config_then_pictures(tmp_path, monkeypatch) -> None:
    from bdl import config

    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.delenv("BDL_OUT", raising=False)
    monkeypatch.delenv("BOORU_DL_OUT", raising=False)
    assert config.default_out_dir() == Path.home() / "Pictures" / "booru"

    config.set_setting("out", str(tmp_path / "lib"))
    assert config.default_out_dir() == tmp_path / "lib"

    # ~ is expanded, and the environment still has the last word.
    config.set_setting("out", "~/booru-alt")
    assert config.default_out_dir() == Path.home() / "booru-alt"
    monkeypatch.setenv("BDL_OUT", str(tmp_path / "from-env"))
    assert config.default_out_dir() == tmp_path / "from-env"


# Tag hints
def test_local_hints_prefix() -> None:
    from bdl.tags.commands import local_hints

    hints = local_hints("hatsune", 5)
    assert hints and hints[0] == "hatsune_miku"
    assert len(hints) <= 5
    assert local_hints("") == []


def test_local_hints_slang() -> None:
    from bdl.tags.catalog import RU_SLANG
    from bdl.tags.commands import local_hints

    # slang: a dictionary key is substituted with its canonical value
    if not RU_SLANG:
        return
    key, value = next(iter(RU_SLANG.items()))
    assert value in local_hints(key, 10)


def test_popular_tags_diverse() -> None:
    from bdl.tags.commands import popular_tags

    popular = asyncio.run(popular_tags(20))
    assert len(popular) == 20
    assert len(set(popular)) == 20  # no duplicates
    assert all(tag for tag in popular)


def test_tags_db_parse_hints_popular(tmp_path) -> None:
    import random

    random.seed(42)  # determinism: exclude instability caused by random
    from bdl.tags.store import db_hints, db_popular, parse_csv

    csv_path = tmp_path / "danbooru-test.csv"
    csv_path.write_text(
        '1girl,0,6008644,"1girls,sole_female"\n'
        'highres,5,5256195,"high_res,hires"\n'
        'solo,0,5000954,"female_solo"\n'
        'breasts,0,3439214,"boobs,oppai"\n'
        "cat_ears,0,100,\n",
        encoding="utf-8",
    )
    data = parse_csv(csv_path)
    assert len(data.names) == 5
    assert data.aliases["boobs"] == "breasts"

    assert "highres" in db_hints(data, "hi", 10)
    assert "breasts" in db_hints(data, "boo", 10)  # via alias
    assert db_hints(data, "zzz", 10) == []

    popular = db_popular(data, 3)
    assert len(popular) == 3, f"ожидалось 3, получено {len(popular)}: {popular}"
    assert len(set(popular)) == 3


def test_prune_stale(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(tmp_path / "p.sqlite3")
        await db.open()
        try:
            real = tmp_path / "real.jpg"
            real.write_bytes(b"x")
            keep = Post(id="1", source="x", file_url="https://x/1.jpg")
            gone = Post(id="2", source="y", file_url="https://y/2.png")
            await db.record_download(keep, str(real), 1, ".jpg", None)
            await db.record_download(gone, str(tmp_path / "gone.png"), 1, ".png", None)

            dry = await db.prune_stale(dry_run=True)
            assert dry["total"] == 1 and dry["y"] == 1
            assert await db.has(gone.uid)  # dry_run deletes nothing

            result = await db.prune_stale()
            assert result["total"] == 1
            assert not await db.has(gone.uid)
            assert await db.has(keep.uid)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_prune_stale_respects_scope(tmp_path) -> None:
    """A run scoped to a throwaway --out must not forget the real library."""

    async def scenario() -> None:
        db = Database(tmp_path / "scope.sqlite3")
        await db.open()
        try:
            library = tmp_path / "library"
            scratch = tmp_path / "scratch"
            library.mkdir()
            scratch.mkdir()
            inside = Post(id="1", source="x", file_url="https://x/1.jpg")
            outside = Post(id="2", source="y", file_url="https://y/2.png")
            gone_outside = Post(id="3", source="y", file_url="https://y/3.png")
            await db.record_download(inside, str(library / "gone_a.jpg"), 1, ".jpg", None)
            await db.record_download(outside, str(scratch / "gone_b.png"), 1, ".png", None)
            await db.record_download(gone_outside, str(library / "gone_c.png"), 1, ".png", None)

            # Scoped to scratch: only its own missing row is forgotten.
            scoped = await db.prune_stale(under=scratch)
            assert scoped["total"] == 1
            assert not await db.has(outside.uid)
            assert await db.has(gone_outside.uid)  # outside the scope — untouched
            assert await db.has(inside.uid)

            # --all sees everything again.
            everything = await db.prune_stale()
            assert everything["total"] == 2
            assert not await db.has(inside.uid)
            assert not await db.has(gone_outside.uid)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_collect_posts_stops_at_limit() -> None:
    class FakeClient:
        def __init__(self, all_posts: list[Post]) -> None:
            self._posts = all_posts
            self.calls = 0

        async def search(self, provider_key: str, tags: str, page: int, limit: int, order: str = ""):
            self.calls += 1
            start = (page - 1) * limit
            chunk = self._posts[start : start + limit]
            exhausted = start + limit >= len(self._posts)
            return SimpleNamespace(posts=tuple(chunk), exhausted=exhausted)

    posts = [Post(id=str(i), source="x", file_url=f"https://x/{i}.png") for i in range(30)]
    client = FakeClient(posts)
    got = asyncio.run(Downloader.collect_posts(client, "x", "t", limit=10, per_page=10))
    assert len(got) == 10
    assert len({p.uid for p in got}) == 10
    assert client.calls == 1


def test_collect_posts_dedup_across_pages() -> None:
    class DupClient:
        def __init__(self) -> None:
            self.calls = 0

        async def search(self, provider_key: str, tags: str, page: int, limit: int, order: str = ""):
            self.calls += 1
            # The same five posts on every page.
            chunk = tuple(Post(id=str(i), source="x", file_url=f"https://x/{i}.png") for i in range(5))
            return SimpleNamespace(posts=chunk, exhausted=False)

    client = DupClient()
    got = asyncio.run(Downloader.collect_posts(client, "x", "t", limit=50, per_page=5))
    # the first page yielded 5 fresh posts, a duplicate on the second — stop
    assert len(got) == 5
    assert len({p.uid for p in got}) == 5
    assert client.calls == 2


# Aspect ratios (wallpapers / avatars / --aspect-ratio)
def _post(width: int | None, height: int | None, uid: str = "1") -> Post:
    return Post(id=uid, source="x", file_url=f"https://x/{uid}.png", width=width, height=height)


def test_aspect_ratio_value() -> None:
    assert aspect_ratio(_post(1920, 1080)) == pytest.approx(1.7778, abs=1e-4)
    assert aspect_ratio(_post(512, 512)) == 1.0
    assert aspect_ratio(_post(None, 1080)) is None
    assert aspect_ratio(_post(1920, None)) is None
    assert aspect_ratio(_post(0, 1080)) is None  # 0 — size unknown, do not divide


def test_wallpaper_aspect_selects_16_9_and_16_10() -> None:
    low, high = WALLPAPER_ASPECT
    assert aspect_matches(_post(1920, 1080), low, high)  # 16:9
    assert aspect_matches(_post(1920, 1200), low, high)  # 16:10
    assert aspect_matches(_post(2560, 1440), low, high)  # 16:9, larger
    # vertical and square — not wallpaper
    assert not aspect_matches(_post(1080, 1920), low, high)
    assert not aspect_matches(_post(512, 512), low, high)
    # almost 16:9, but too narrow (1.4) and too wide (2.0)
    assert not aspect_matches(_post(1400, 1000), low, high)
    assert not aspect_matches(_post(2000, 1000), low, high)


def test_avatar_aspect_selects_square_and_portrait() -> None:
    low, high = AVATAR_ASPECT
    assert aspect_matches(_post(512, 512), low, high)  # 1:1
    assert aspect_matches(_post(600, 900), low, high)  # 2:3
    assert aspect_matches(_post(750, 1000), low, high)  # 3:4
    assert aspect_matches(_post(500, 480), low, high)  # slightly wider than square — ok
    # widescreen wallpapers — not avatars
    assert not aspect_matches(_post(1920, 1080), low, high)
    assert not aspect_matches(_post(1000, 400), low, high)  # 2.5 — too elongated
    assert not aspect_matches(_post(400, 1000), low, high)  # 0.4 — too tall


def test_aspect_matches_without_dimensions() -> None:
    # Without width/height selection is impossible — the post does not pass (we do not crash).
    assert not aspect_matches(_post(None, None), *WALLPAPER_ASPECT)


def test_parse_aspect_range_formats() -> None:
    assert parse_aspect_range("1.5:1.8") == (1.5, 1.8)
    assert parse_aspect_range("1,5:1,8") == (1.5, 1.8)  # decimal comma
    assert parse_aspect_range("1.5,1.8") == (1.5, 1.8)  # comma as separator
    assert parse_aspect_range("1.5/1.8") == (1.5, 1.8)  # slash
    assert parse_aspect_range("1.8:1.5") == (1.5, 1.8)  # inverted — we normalize
    assert parse_aspect_range(" 1.5 : 1.8 ") == (1.5, 1.8)  # spaces


@pytest.mark.parametrize(
    "bad",
    ["", "abc", "1.5", "0:2", "-1:2", ":", "1:abc"],
)
def test_parse_aspect_range_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_aspect_range(bad)


def test_parse_aspect_range_understands_width_height_shorthand() -> None:
    """«16:9» is the ratio 1.78, not the window 9..16 (otherwise nothing would match)."""
    low, high = parse_aspect_range("16:9")
    assert low < 16 / 9 < high
    assert aspect_matches(_post(1920, 1080), low, high)
    assert aspect_matches(_post(1600, 900), low, high)
    assert not aspect_matches(_post(1920, 1200), low, high)  # 16:10 is slightly narrower
    assert not aspect_matches(_post(1080, 1920), low, high)

    low, high = parse_aspect_range("3:4")
    assert low < 3 / 4 < high
    assert aspect_matches(_post(750, 1000), low, high)
    assert not aspect_matches(_post(1920, 1080), low, high)


def test_downloader_aspect_filter_in_passes_filters() -> None:
    """passes_filters really does drop posts with the wrong aspect ratio."""
    downloader = Downloader(
        None,  # type: ignore[arg-type]  # the filter does not touch the DB
        out_root=Path("/tmp/bdl-aspect"),
        aspect_range=WALLPAPER_ASPECT,
    )
    assert downloader.passes_filters(_post(1920, 1080))
    assert not downloader.passes_filters(_post(512, 512))

    off = Downloader(None, out_root=Path("/tmp/bdl-aspect"))  # type: ignore[arg-type]
    assert off.passes_filters(_post(512, 512))  # without the filter everything passes


def test_downloader_keeps_posts_without_dimensions_by_default() -> None:
    """A board that omits width/height must not silently lose its posts."""
    downloader = Downloader(
        None,  # type: ignore[arg-type]
        out_root=Path("/tmp/bdl-aspect"),
        aspect_range=WALLPAPER_ASPECT,
    )
    assert downloader.aspect_verdict(_post(None, None)) is None
    assert downloader.passes_filters(_post(None, 1080))  # kept by default
    assert downloader.passes_filters(_post(1920, 1080))
    assert not downloader.passes_filters(_post(512, 512))  # a known bad ratio still goes


def test_downloader_aspect_unknown_skip_drops_them() -> None:
    downloader = Downloader(
        None,  # type: ignore[arg-type]
        out_root=Path("/tmp/bdl-aspect"),
        aspect_range=WALLPAPER_ASPECT,
        aspect_unknown="skip",
    )
    assert not downloader.passes_filters(_post(None, None))
    assert not downloader.passes_filters(_post(900, None))
    assert downloader.passes_filters(_post(1920, 1080))  # a real match still survives


def test_aspect_verdict_is_true_without_a_filter() -> None:
    downloader = Downloader(None, out_root=Path("/tmp/bdl-aspect"))  # type: ignore[arg-type]
    assert downloader.aspect_verdict(_post(None, None)) is True


def test_rating_bucket_used_by_media_paths() -> None:
    assert rating_bucket("safe") == "safe"
    assert rating_bucket("general") == "safe"
    assert rating_bucket("explicit") == "nsfw"
    assert rating_bucket("questionable") == "suggestive"
    assert rating_bucket(None) == "nsfw"  # unknown rating — into nsfw, we do not lose it


# Triple dedup (_already_have)
def test_already_have_level1_uid_in_db(tmp_path) -> None:
    """Level 1: uid is already in the DB — the file may not even exist."""

    async def scenario() -> None:
        db = Database(tmp_path / "d1.sqlite3")
        await db.open()
        try:
            post = Post(id="1", source="x", file_url="https://x/1.png")
            await db.record_download(post, str(tmp_path / "missing.png"), 1, ".png", None)
            dl = Downloader(db, out_root=tmp_path)
            assert await dl._already_have(post, tmp_path / "missing.png")
        finally:
            await db.close()

    asyncio.run(scenario())


def test_already_have_level2_file_on_disk(tmp_path) -> None:
    """Level 2: the file is in place, even though there is no DB record."""

    async def scenario() -> None:
        db = Database(tmp_path / "d2.sqlite3")
        await db.open()
        try:
            post = Post(id="2", source="x", file_url="https://x/2.png")
            on_disk = tmp_path / "2.png"
            on_disk.write_bytes(b"data")
            dl = Downloader(db, out_root=tmp_path)
            assert await dl._already_have(post, on_disk)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_already_have_force_ignores_everything(tmp_path) -> None:
    """--force disables all three levels: we download again."""

    async def scenario() -> None:
        db = Database(tmp_path / "d3.sqlite3")
        await db.open()
        try:
            post = Post(id="3", source="x", file_url="https://x/3.png")
            on_disk = tmp_path / "3.png"
            on_disk.write_bytes(b"data")
            await db.record_download(post, str(on_disk), 4, ".png", None)
            dl = Downloader(db, out_root=tmp_path, force=True)
            assert not await dl._already_have(post, on_disk)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_run_batch_counts_posts_already_in_db_as_skipped(tmp_path) -> None:
    """A rerun must report skips, not look like an empty tag."""

    class FakeClient:
        async def search(self, provider_key: str, tags: str, page: int, limit: int, order: str = ""):
            chunk = (Post(id="7", source="x", file_url="https://x/7.png"),) if page == 1 else ()
            return SimpleNamespace(posts=chunk, exhausted=page == 1)

    async def scenario() -> None:
        db = Database(tmp_path / "d4.sqlite3")
        await db.open()
        try:
            post = Post(id="7", source="x", file_url="https://x/7.png")
            await db.record_download(post, str(tmp_path / "7.png"), 4, ".png", None)
            dl = Downloader(db, out_root=tmp_path, progress=False)
            provider = SimpleNamespace(key="x", title="X")
            stats = await dl.run_batch(FakeClient(), provider, "t", limit=5, per_page=5)
            assert stats.reported == 1
            assert stats.skipped == 1
            assert stats.downloaded == 0
        finally:
            await db.close()

    asyncio.run(scenario())


def test_run_batch_force_keeps_known_posts_in_the_queue(tmp_path) -> None:
    """--force must survive the uid pre-filter, not just _already_have."""

    reached: list[Post] = []

    class FakeClient:
        async def search(self, provider_key: str, tags: str, page: int, limit: int, order: str = ""):
            chunk = (Post(id="7", source="x", file_url="https://x/7.png"),) if page == 1 else ()
            return SimpleNamespace(posts=chunk, exhausted=page == 1)

    async def scenario() -> None:
        db = Database(tmp_path / "d6.sqlite3")
        await db.open()
        try:
            post = Post(id="7", source="x", file_url="https://x/7.png")
            on_disk = tmp_path / "7.png"
            on_disk.write_bytes(b"data")
            await db.record_download(post, str(on_disk), 4, ".png", None)

            dl = Downloader(db, out_root=tmp_path, progress=False, force=True)

            # Stub the transfer: this test is about the queue, not the network.
            async def fake_download(provider, candidate):
                reached.append(candidate)
                return "downloaded", None, None

            dl.download_post = fake_download  # type: ignore[method-assign]
            provider = SimpleNamespace(key="x", title="X")
            stats = await dl.run_batch(FakeClient(), provider, "t", limit=5, per_page=5)
            assert len(reached) == 1
            assert stats.skipped == 0
            assert stats.downloaded == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_run_batch_reports_nothing_for_an_empty_tag(tmp_path) -> None:
    """Nothing came back at all — that is the case worth warning about."""

    class EmptyClient:
        async def search(self, provider_key: str, tags: str, page: int, limit: int, order: str = ""):
            return SimpleNamespace(posts=(), exhausted=True)

    async def scenario() -> None:
        db = Database(tmp_path / "d5.sqlite3")
        await db.open()
        try:
            dl = Downloader(db, out_root=tmp_path, progress=False)
            provider = SimpleNamespace(key="x", title="X")
            stats = await dl.run_batch(EmptyClient(), provider, "nope", limit=5, per_page=5)
            assert stats.reported == 0
            assert stats.skipped == 0
            assert stats.found == 0
        finally:
            await db.close()

    asyncio.run(scenario())


def test_already_have_checksum_detects_changed_file(tmp_path) -> None:
    """--checksum: the hash did not match — there is a record, but we download again."""

    async def scenario() -> None:
        db = Database(tmp_path / "d4.sqlite3")
        await db.open()
        try:
            path = tmp_path / "4.png"
            path.write_bytes(b"correct-data")
            import hashlib

            good = hashlib.sha256(b"correct-data").hexdigest()
            post = Post(id="4", source="x", file_url="https://x/4.png")
            await db.record_download(post, str(path), 12, ".png", good)

            dl = Downloader(db, out_root=tmp_path, checksum=True)
            assert await dl._already_have(post, path)  # hash matched — skip

            path.write_bytes(b"TAMPERED!!!")  # the file got corrupted
            assert not await dl._already_have(post, path)  # hash is different — we download
        finally:
            await db.close()

    asyncio.run(scenario())


def test_already_have_checksum_detects_duplicate_by_hash(tmp_path) -> None:
    """--checksum: the same content under a different uid — caught as a duplicate."""

    async def scenario() -> None:
        db = Database(tmp_path / "d5.sqlite3")
        await db.open()
        try:
            import hashlib

            payload = b"same-picture"
            digest = hashlib.sha256(payload).hexdigest()
            first = Post(id="a", source="x", file_url="https://x/a.png")
            await db.record_download(first, str(tmp_path / "a.png"), len(payload), ".png", digest)

            # Different uid, but the same file at the target path.
            second = Post(id="b", source="y", file_url="https://y/b.png")
            dup_path = tmp_path / "b.png"
            dup_path.write_bytes(payload)

            dl = Downloader(db, out_root=tmp_path, checksum=True)
            assert await dl._already_have(second, dup_path)

            # Without --checksum we do not search by hash: remove the file from the target path,
            # so that level 2 does not trigger, and the post is considered new again.
            dup_path.unlink()
            off = Downloader(db, out_root=tmp_path)
            assert not await off._already_have(second, dup_path)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_db_sha256_helpers(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(tmp_path / "d6.sqlite3")
        await db.open()
        try:
            post = Post(id="7", source="x", file_url="https://x/7.png")
            assert await db.sha256_of(post.uid) is None  # no record
            assert await db.file_and_hash(post.uid) == (None, None)
            assert not await db.has_sha256("deadbeef")

            await db.record_download(post, "/tmp/7.png", 1, ".png", "deadbeef")
            assert await db.sha256_of(post.uid) == "deadbeef"
            assert await db.file_and_hash(post.uid) == ("/tmp/7.png", "deadbeef")
            assert await db.has_sha256("deadbeef")
            # exclude_uid hides the record itself — leaving "no duplicate".
            assert not await db.has_sha256("deadbeef", exclude_uid=post.uid)
        finally:
            await db.close()

    asyncio.run(scenario())


# Typo correction (rapidfuzz) and perceptual dedup
def test_correct_typos_fixes_a_misspelled_tag() -> None:
    from bdl.tags.suggest import correct_typos

    fixed, changes = correct_typos("mku")
    assert fixed == "miku"
    assert changes == [("mku", "miku")]


def test_correct_typos_leaves_real_tags_alone() -> None:
    from bdl.tags.suggest import correct_typos

    # Both are genuine tags, so they must survive untouched: "sakura" is a tag
    # in its own right and "1girl" is shorthand, not a typo for "1girls".
    assert correct_typos("miku sakura 1girl") == ("miku sakura 1girl", [])
    assert correct_typos("") == ("", [])


def test_correct_typos_keeps_a_negated_tag_negated() -> None:
    from bdl.tags.suggest import correct_typos

    fixed, changes = correct_typos("-mku")
    assert fixed == "-miku"
    assert changes == [("mku", "miku")]


def test_perceptual_distance_and_duplicate_threshold() -> None:
    from bdl.perceptual import DUPLICATE_DISTANCE, distance, is_duplicate

    assert distance("0000000000000000", "0000000000000000") == 0
    assert distance("0000000000000000", "000000000000000f") == 4
    assert distance("ffffffffffffffff", "fffffffffffffff0") == 4
    # A handful of flipped bits is a re-encode, not a different picture.
    assert is_duplicate("0000000000000000", "000000000000000f")
    # Half the bits flipped is a different picture.
    assert not is_duplicate("0000000000000000", "00000000ffffffff")
    assert distance("not-hex", "0000000000000000") is None
    assert DUPLICATE_DISTANCE == 8


def test_phash_of_file_survives_a_resize(tmp_path) -> None:
    Image = pytest.importorskip("PIL.Image")
    from bdl.perceptual import is_duplicate, phash_of_file

    original = tmp_path / "a.png"
    Image.new("RGB", (96, 96), (200, 30, 90)).save(original)
    resized = tmp_path / "b.png"
    Image.open(original).resize((24, 24)).save(resized)

    first = phash_of_file(original)
    second = phash_of_file(resized)
    assert first is not None and second is not None
    assert is_duplicate(first, second)


def test_phash_of_a_broken_file_is_none(tmp_path) -> None:
    from bdl.perceptual import phash_of_file

    broken = tmp_path / "broken.png"
    broken.write_bytes(b"this is not an image")
    # Dedup is an optimisation: an undecodable file must not raise.
    assert phash_of_file(broken) is None


def test_db_records_and_lists_phashes(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(tmp_path / "ph.sqlite3")
        await db.open()
        try:
            post = Post(id="1", source="x", file_url="https://x/1.png")
            await db.record_download(post, "/tmp/1.png", 10, ".png", "hash", "abcdef0123456789")
            assert await db.all_phashes() == [(post.uid, "abcdef0123456789")]
        finally:
            await db.close()

    asyncio.run(scenario())


def test_db_migrates_an_old_table_without_phash(tmp_path) -> None:
    import sqlite3

    async def scenario() -> None:
        path = tmp_path / "old.sqlite3"
        # A library written before perceptual dedup existed: no phash column.
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE downloads (uid TEXT PRIMARY KEY, source TEXT, post_id TEXT, file_path TEXT,"
            " file_size INTEGER, ext TEXT, tags TEXT, rating TEXT, width INTEGER, height INTEGER,"
            " score INTEGER, file_url TEXT, sha256 TEXT, downloaded_at REAL)"
        )
        conn.execute("INSERT INTO downloads VALUES ('x:1','x','1','/tmp/1.png',10,'.png','[]','safe',1,1,0,'u',NULL,0)")
        conn.commit()
        conn.close()

        db = Database(path)
        await db.open()
        try:
            # The migration adds the column; the old row's phash stays NULL and
            # is simply not offered as dedup evidence.
            assert await db.all_phashes() == []
            post = Post(id="2", source="x", file_url="https://x/2.png")
            await db.record_download(post, "/tmp/2.png", 10, ".png", None, "ffff000000000000")
            assert (post.uid, "ffff000000000000") in await db.all_phashes()
        finally:
            await db.close()

    asyncio.run(scenario())


def test_a_repost_from_another_board_is_a_duplicate(tmp_path) -> None:
    """The same picture under a different uid is caught by its pHash."""

    async def scenario() -> None:
        db = Database(tmp_path / "dup.sqlite3")
        await db.open()
        try:
            first = Post(id="1", source="gelbooru", file_url="https://g/1.png")
            await db.record_download(first, "/tmp/1.png", 10, ".png", None, "abcdef0123456789")
            downloader = Downloader(db, out_root=tmp_path)
            await downloader._load_phashes()

            # Same art, reposted to another board under a new id: no uid match,
            # no sha256 (not checked), but the perceptual hash agrees.
            assert downloader._is_duplicate_picture("abcdef0123456789")
            assert downloader._is_duplicate_picture("abcdef012345678f")  # 4 bits differ
            assert not downloader._is_duplicate_picture("0000000000000000")
            # The post's own earlier row never blocks it: --force exists to
            # re-fetch a picture the user already has.
            assert not downloader._is_duplicate_picture("abcdef0123456789", first.uid)
            # A different post with the same art is still a repost.
            assert downloader._is_duplicate_picture("abcdef0123456789", "gelbooru:2")
        finally:
            await db.close()

    asyncio.run(scenario())


def test_dedup_can_be_switched_off(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(tmp_path / "nodup.sqlite3")
        await db.open()
        try:
            post = Post(id="1", source="x", file_url="https://x/1.png")
            await db.record_download(post, "/tmp/1.png", 10, ".png", None, "abcdef0123456789")
            downloader = Downloader(db, out_root=tmp_path, dedup=False)
            await downloader._load_phashes()
            assert not downloader._is_duplicate_picture("abcdef0123456789")
        finally:
            await db.close()

    asyncio.run(scenario())


def test_xbooru_gets_a_browser_user_agent() -> None:
    from bdl import XBOORU_USER_AGENT
    from bdl.providers.client import ProviderClient

    headers = ProviderClient._headers_for(PROVIDERS["xbooru"])
    assert headers["User-Agent"] == XBOORU_USER_AGENT
    assert "Firefox" in headers["User-Agent"]
    # Cloudflare weighs the whole header set, not just the agent.
    assert headers["Referer"].startswith("https://xbooru.com/")
    assert headers["Accept"].startswith("text/html")
    assert headers["Accept-Language"] == "en-US,en;q=0.5"
    # Other boards keep the honest tool agent.
    assert ProviderClient._headers_for(PROVIDERS["gelbooru"]) == {}


def test_rejection_message_does_not_invent_an_api_key() -> None:
    from bdl.providers.client import _denied_message

    # Xbooru has no API keys at all, so its 403 must not mention one.
    plain = _denied_message(PROVIDERS["xbooru"], 403)
    assert "API_KEY" not in plain
    assert "bot filter" in plain
    # Gelbooru does take a key, so there the hint is right.
    keyed = _denied_message(PROVIDERS["gelbooru"], 401)
    assert "GELBOORU_API_KEY" in keyed


def test_preview_detection_uses_the_kitty_environment() -> None:
    from bdl.preview import in_kitty

    assert in_kitty({"KITTY_WINDOW_ID": "1"})
    assert in_kitty({"TERM": "xterm-kitty"})
    assert not in_kitty({"TERM": "dumb"})
    assert not in_kitty({})


def test_preview_thumbnail_is_png_bytes(tmp_path) -> None:
    Image = pytest.importorskip("PIL.Image")
    from bdl.preview import _thumbnail

    path = tmp_path / "shot.png"
    Image.new("RGB", (400, 200), (10, 200, 30)).save(path)
    payload = _thumbnail(path, rows=6)
    assert payload is not None
    assert payload.startswith(b"\x89PNG")
    # Tiles are fitted inside the box, never stretched past it.
    tile = Image.open(io.BytesIO(payload))
    assert tile.height <= 6 * 16
    assert tile.width <= 208


def test_preview_skips_a_file_that_is_not_an_image(tmp_path) -> None:
    from bdl.preview import _thumbnail

    broken = tmp_path / "not.png"
    broken.write_bytes(b"nope")
    assert _thumbnail(broken, rows=4) is None


def test_preview_gallery_is_a_grid_not_a_column(tmp_path) -> None:
    """A batch of eight pictures becomes two rows of three, not eight rows."""
    Image = pytest.importorskip("PIL.Image")

    from bdl import preview

    paths = []
    for index in range(8):
        path = tmp_path / f"{index}.png"
        Image.new("RGB", (900, 300), (index * 20, 40, 90)).save(path)
        paths.append(path)

    strips: list[tuple[int, int]] = []

    def fake_transmit(payload: bytes, rows: int, columns: int) -> bool:
        strips.append((rows, columns))
        return True

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(preview, "available", lambda: True)
    monkeypatch.setattr(preview, "_transmit", fake_transmit)
    try:
        drawn = preview.show_many(paths)
    finally:
        monkeypatch.undo()

    assert drawn == preview.MAX_TILE_ROWS == 2
    # One strip per gallery row, each three tiles wide and inside the box.
    assert len(strips) == 2
    for rows, columns in strips:
        assert rows == preview.PREVIEW_ROWS
        assert 1 <= columns <= 200


def test_preview_columns_keep_the_picture_shape() -> None:
    from bdl.preview import columns_for

    # A 3:1 strip scaled to 8 rows is 48 cells wide: 8 rows * 2 (cells are
    # twice as tall as wide) * 3 (the aspect ratio) / 1.
    assert columns_for(1200, 400, 8) == 48
    assert columns_for(0, 400, 8) == 1
    assert columns_for(400, 400, 0) == 1


def test_gauge_splits_the_bar_into_full_and_empty_cells() -> None:
    from bdl.downloader import _gauge

    filled, empty = _gauge(50, 10)
    assert (len(filled), len(empty)) == (5, 5)
    assert set(filled) == {"█"} and set(empty) == {"━"}
    # Clamped at both ends: a task can report more bytes than it announced.
    assert _gauge(120, 8) == ("█" * 8, "")
    assert _gauge(-5, 8) == ("", "━" * 8)


def test_progress_file_row_has_a_gauge_and_a_spinner() -> None:
    from rich.text import Text

    from bdl.downloader import TransferProgress

    bar = TransferProgress()
    bar.start("Gelbooru", 4)
    task_id = bar.begin_file("miku.png", 1024)
    bar.chunk(task_id, 512)
    lines = next(iter(bar._progress.get_renderables())).plain.split("\n")
    row = lines[1]
    assert "█" in row and "━" in row
    assert "50%" in row
    # The gauge sits next to the percentage, not pushed to the far right.
    assert "50% │" in row
    assert "512.0 B /    1.0 KiB" in row
    assert isinstance(bar._progress.get_renderables(), object)  # still one Text
    assert isinstance(next(iter(bar._progress.get_renderables())), Text)


def test_progress_rows_share_one_column_layout() -> None:
    """Every row lines up, whatever the numbers happen to be.

    This is the regression guard for the "staircase": a four-percent bar and a
    sixty-six-percent bar must start and end at the same cells.
    """

    from bdl.downloader import TransferProgress

    bar = TransferProgress()
    bar.start("Gelbooru", 2)
    small = bar.begin_file("a.png", 245 * 1024)
    large = bar.begin_file("b.png", 12 * 1024 * 1024)
    bar.chunk(small, 10 * 1024)  # ~4%
    bar.chunk(large, 8 * 1024 * 1024)  # ~66%
    bar._progress.update(small, speed=900 * 1024)  # 4-char speed
    bar._progress.update(large, speed=1.2 * 1024 * 1024)  # 9-char speed
    rows = next(iter(bar._progress.get_renderables())).plain.split("\n")[1:]

    def columns(row: str) -> list[int]:
        return [row.index(mark) for mark in ("[", "]", "│", "│", "│")]

    assert len(rows) == 2
    assert columns(rows[0]) == columns(rows[1])
    # Different percentages and different speeds, identical geometry.
    assert "4%" in rows[0] and "67%" in rows[1]
    assert len({len(row) for row in rows}) == 1


# Concurrency: config, flags and the adaptive pool
def test_concurrency_setting_round_trips_through_config(tmp_path, monkeypatch) -> None:
    """`bdl config set concurrency 16` must survive into the next run."""
    from bdl import config

    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.delenv("BDL_CONCURRENCY", raising=False)
    assert config.default_concurrency() == config.DEFAULT_CONCURRENCY == 8

    config.set_setting("concurrency", "16")
    assert config.get_setting("concurrency") == "16"
    assert config.default_concurrency() == 16
    # Persisted, not just cached: read the file back from disk.
    assert json.loads((tmp_path / "config.json").read_text())["concurrency"] == "16"


def test_concurrency_environment_wins_and_garbage_is_ignored(tmp_path, monkeypatch) -> None:
    from bdl import config

    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.json")
    config.set_setting("concurrency", "16")
    monkeypatch.setenv("BDL_CONCURRENCY", "24")
    assert config.default_concurrency() == 24

    # A value nobody can parse must not break a download.
    config.set_setting("concurrency", "banana")
    monkeypatch.delenv("BDL_CONCURRENCY", raising=False)
    assert config.default_concurrency() == config.DEFAULT_CONCURRENCY
    # Nor may it escape the safe range.
    assert config.clamp_concurrency(10_000) == config.MAX_CONCURRENCY
    assert config.clamp_concurrency(0) == 1


def test_turbo_flag_sets_twenty_transfers(tmp_path, monkeypatch) -> None:
    from bdl import config
    from bdl.cli import build_parser, resolve_concurrency

    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.delenv("BDL_CONCURRENCY", raising=False)
    parser = build_parser()

    plain = parser.parse_args(["dl", "-s", "gelbooru", "-t", "cat"])
    assert resolve_concurrency(plain) == 8

    turbo = parser.parse_args(["dl", "-s", "gelbooru", "-t", "cat", "--turbo"])
    assert resolve_concurrency(turbo) == config.TURBO_CONCURRENCY == 20

    explicit = parser.parse_args(["dl", "-s", "gelbooru", "-t", "cat", "--concurrency", "12"])
    assert resolve_concurrency(explicit) == 12

    # A saved preference is what a bare run uses.
    config.set_setting("concurrency", "16")
    assert resolve_concurrency(parser.parse_args(["dl", "-s", "gelbooru", "-t", "cat"])) == 16

    # --turbo is the sharper instruction and wins, but the user is told.
    both = parser.parse_args(["dl", "-s", "gelbooru", "-t", "cat", "--concurrency", "4", "--turbo"])
    assert resolve_concurrency(both) == 20


def test_adaptive_pool_starts_at_the_requested_size() -> None:
    from bdl.downloader import AdaptivePool

    pool = AdaptivePool(8, ceiling=20, floor=4)
    assert pool.limit == 8
    assert pool.in_flight == 0
    assert AdaptivePool.host_key("https://img4.gelbooru.com/x/y.jpg") == "img4.gelbooru.com"


def test_adaptive_pool_grows_and_shrinks_within_its_bounds() -> None:
    from bdl.downloader import AdaptivePool

    async def scenario() -> None:
        pool = AdaptivePool(8, ceiling=20, floor=4)
        await pool.set_limit(20)
        assert pool.limit == 20
        # The ceiling holds even if something asks for more.
        await pool.set_limit(500)
        assert pool.limit == 20
        await pool.set_limit(1)
        assert pool.limit == 4  # the floor keeps a usable trickle

    asyncio.run(scenario())


def test_adaptive_pool_penalises_only_the_host_that_failed() -> None:
    """A throttled board must not slow down a healthy one."""
    from bdl.downloader import AdaptivePool

    async def scenario() -> None:
        pool = AdaptivePool(8, ceiling=20, floor=4)
        await pool.acquire("busy.example")
        pool.penalise("busy.example", seconds=30)
        assert pool.penalised("busy.example")
        # Halved, not dropped to one: a single 429 usually means "slower".
        assert pool.host_cap("busy.example") == 4
        assert pool.host_cap("calm.example") == 8
        await pool.release("busy.example")
        assert pool.in_flight == 0

    asyncio.run(scenario())


def test_adaptive_pool_cooldown_expires_and_relaxes() -> None:
    from bdl.downloader import AdaptivePool

    async def scenario() -> None:
        pool = AdaptivePool(8, ceiling=20, floor=4)
        pool.penalise("slow.example", seconds=0.0)
        assert not pool.penalised("slow.example")
        pool.penalise("slow.example", seconds=30)
        assert pool.relax("slow.example") is False  # still cooling down

    asyncio.run(scenario())


def test_adaptive_pool_blocks_a_penalised_host_beyond_its_cap() -> None:
    """The per-host cap is enforced, not just reported."""
    from bdl.downloader import AdaptivePool

    async def scenario() -> None:
        pool = AdaptivePool(4, ceiling=20, floor=2)
        pool.penalise("tight.example", seconds=30)  # cap 4 -> 2
        await pool.acquire("tight.example")
        await pool.acquire("tight.example")
        assert pool.host_cap("tight.example") == 2
        third = asyncio.create_task(pool.acquire("tight.example"))
        await asyncio.sleep(0.05)
        assert not third.done()  # the cap holds it back
        await pool.release("tight.example")
        await asyncio.wait_for(third, timeout=1.0)
        assert pool.in_flight == 2
        for _ in range(2):
            await pool.release("tight.example")

    asyncio.run(scenario())


def test_adaptive_pool_releases_a_place_on_every_exit_path() -> None:
    """An exception inside the guarded block must not leak a slot."""
    from bdl.downloader import AdaptivePool, _PoolGuard

    async def scenario() -> None:
        pool = AdaptivePool(2, ceiling=4, floor=1)
        try:
            async with _PoolGuard(pool, "host.example"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert pool.in_flight == 0
        # Which means the pool is still usable afterwards.
        async with _PoolGuard(pool, "host.example"):
            assert pool.in_flight == 1
        assert pool.in_flight == 0

    asyncio.run(scenario())


def test_downloader_adapts_the_pool_to_the_measured_speed() -> None:
    """Hot line -> grow; cold line -> shrink; failures -> back off."""
    from bdl.downloader import SCALE_DOWN_SPEED, SCALE_UP_SPEED, Downloader

    downloader = Downloader(None, out_root=Path("/tmp/bdl-adaptive"), concurrency=8)  # type: ignore[arg-type]

    async def scenario() -> None:
        assert downloader._pool.limit == 8
        # A gigabit line saturates the requested pool, so the ramp opens up.
        await downloader._adapt_pool(SCALE_UP_SPEED * 1.5, failures=0, previous_failures=0)
        assert downloader._pool.limit == 10
        # It climbs towards the ceiling but stops there.
        for _ in range(10):
            await downloader._adapt_pool(SCALE_UP_SPEED * 2, failures=0, previous_failures=0)
        assert downloader._pool.limit == downloader._pool_ceiling() <= 20
        # The line goes quiet: the pool gives places back.
        await downloader._adapt_pool(SCALE_DOWN_SPEED / 2, failures=0, previous_failures=0)
        assert downloader._pool.limit < downloader._pool_ceiling()

    asyncio.run(scenario())


def test_downloader_never_adapts_when_adaptive_is_off() -> None:
    from bdl.downloader import SCALE_UP_SPEED, Downloader

    downloader = Downloader(  # type: ignore[arg-type]
        None, out_root=Path("/tmp/bdl-adaptive"), concurrency=4, adaptive=False
    )

    async def scenario() -> None:
        assert downloader._pool.limit == 4
        await downloader._adapt_pool(SCALE_UP_SPEED * 10, failures=0, previous_failures=0)
        assert downloader._pool.limit == 4
        assert downloader._pool_ceiling() == 4

    asyncio.run(scenario())


def test_downloader_shrinks_the_pool_when_a_transfer_fails() -> None:
    """A failing window walks the pool back, but not below the requested size."""
    from bdl.downloader import Downloader

    downloader = Downloader(None, out_root=Path("/tmp/bdl-adaptive"), concurrency=12)  # type: ignore[arg-type]

    async def scenario() -> None:
        await downloader._pool.set_limit(18)  # as if the ramp had opened up
        await downloader._adapt_pool(speed=0.0, failures=1, previous_failures=0)
        assert downloader._pool.limit == 16
        # However bad it gets, the size the user asked for is the floor.
        for _ in range(20):
            await downloader._adapt_pool(speed=0.0, failures=1, previous_failures=0)
        assert downloader._pool.limit == 12

    asyncio.run(scenario())


# Moebooru "top": order:score is a tag, and the weekly listing covers no tags
def test_moebooru_top_sorts_by_score_through_a_tag() -> None:
    """`order=score` as a query argument does nothing on Moebooru.

    The board only honours `order:score` inside the tag list, so a top query
    must put it there — with or without the user's own tags.
    """
    provider = PROVIDERS["konachan"]
    url = provider.build_url("hatsune_miku", 1, 50, {}, order="top")
    assert "order%3Ascore" in url
    assert "order=score" not in url  # the argument form is silently ignored
    assert "date%3A" not in url  # Moebooru has no usable date filter

    without_tags = provider.build_url("", 1, 50, {}, order="top")
    assert "tags=order%3Ascore" in without_tags

    # A plain (non-top) query must not smuggle the ordering in.
    assert "order" not in provider.build_url("hatsune_miku", 1, 50, {})


def test_moebooru_popular_endpoint_is_the_bare_top() -> None:
    """Boards advertise their own weekly listing for an untagged top."""
    konachan = PROVIDERS["konachan"]
    assert konachan.popular_endpoint == "https://konachan.com/post/popular_recent.json?period=1w"
    assert PROVIDERS["yandere"].popular_endpoint.endswith("/post/popular_recent.json?period=1w")
    assert konachan.popular_url({}) == konachan.popular_endpoint
    # Keys, when configured, are appended to the listing as well.
    signed = konachan.popular_url({"login": "alice", "api_key": "KEY"})
    assert "login=alice" in signed and "api_key=KEY" in signed
    # Non-Moebooru boards are not touched by any of this.
    assert PROVIDERS["gelbooru"].popular_endpoint == ""


def test_bare_top_uses_the_popular_listing_only_without_tags() -> None:
    """The listing takes no tags and ignores paging, so it fits page 1 only."""
    from bdl.providers.client import ProviderClient

    urls: list[str] = []

    class _Settings:
        cache_ttl = 0.0
        max_cache_entries = 8

        def credentials_for(self, key: str) -> dict[str, str]:
            return {}

    class _Client(ProviderClient):
        async def _fetch_json(self, url: str, provider) -> object:  # type: ignore[override]
            urls.append(url)
            return []

    async def scenario() -> None:
        client = _Client(_Settings())  # type: ignore[arg-type]
        konachan = PROVIDERS["konachan"]
        await client.search_provider(konachan, "konachan", "", 1, 10, "top")
        await client.search_provider(konachan, "konachan", "miku", 1, 10, "top")
        await client.search_provider(konachan, "konachan", "", 2, 10, "top")

    asyncio.run(scenario())
    assert urls[0].startswith("https://konachan.com/post/popular_recent.json")
    assert "order%3Ascore" in urls[1]
    assert "popular_recent" not in urls[2]  # page 2 needs the real search
    assert "order%3Ascore" in urls[2]


def test_missing_result_from_the_popular_listing_falls_back() -> None:
    """An empty weekly listing must not end the run with nothing."""
    from bdl.providers.client import ProviderClient

    urls: list[str] = []
    calls = {"n": 0}

    class _Settings:
        cache_ttl = 0.0
        max_cache_entries = 8

        def credentials_for(self, key: str) -> dict[str, str]:
            return {}

    class _Client(ProviderClient):
        async def _fetch_json(self, url: str, provider) -> object:  # type: ignore[override]
            urls.append(url)
            calls["n"] += 1
            if calls["n"] == 1:
                return []  # the weekly listing is empty
            return [
                {
                    "id": 1,
                    "tags": "miku",
                    "file_url": "/image/a.png",
                    "width": 100,
                    "height": 100,
                    "score": 5,
                }
            ]

    async def scenario() -> int:
        client = _Client(_Settings())  # type: ignore[arg-type]
        result = await client.search_provider(PROVIDERS["konachan"], "konachan", "", 1, 10, "top")
        return len(result.posts)

    assert asyncio.run(scenario()) == 1
    assert urls[0].startswith("https://konachan.com/post/popular_recent.json")
    assert "order%3Ascore" in urls[1]


# Console streams: a non-blocking stdout must not eat the summary
def test_restore_blocking_survives_odd_streams(monkeypatch) -> None:
    """`os.set_blocking` is best-effort: a captured stream has no fd to set."""
    import io

    from bdl import stdio

    class _NoFileno(io.StringIO):
        def fileno(self) -> int:
            raise io.UnsupportedOperation("no descriptor")

    monkeypatch.setattr(stdio.sys, "stdout", _NoFileno())
    monkeypatch.setattr(stdio.sys, "stderr", _NoFileno())
    stdio.restore_blocking()  # must not raise


def test_restore_blocking_sets_the_flag_on_a_real_stream(tmp_path) -> None:
    import os

    from bdl import stdio

    with open(tmp_path / "stream.txt", "w") as handle:
        stdio.restore_blocking()
        # A regular file is always blocking; the point is that the call is made
        # and the stream is flushed rather than raising.
        assert os.get_blocking(handle.fileno()) is True
