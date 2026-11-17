# bdl

[![PyPI version](https://img.shields.io/pypi/v/booru-dl?color=blue)](https://pypi.org/project/booru-dl/)
[![Python versions](https://img.shields.io/pypi/pyversions/booru-dl)](https://pypi.org/project/booru-dl/)

Async batch downloader and sync tool for booru boards (Gelbooru, Rule34,
Xbooru, Safebooru, Danbooru, Konachan, Yande.re, Tbib, e621, e926).

<p align="center">
  <img src="https://raw.githubusercontent.com/wexqk-boo/Booru-Downloader-py/main/assets/banner.png" alt="booru-dl preview" width="650">
</p>

## Features

- **Async streaming downloads** — `asyncio` + `aiohttp`, files streamed to disk
  in chunks, concurrency bounded by a resizable pool.
- **Adaptive concurrency** — the pool starts at what you asked for (8 by
  default), grows towards 20 while the line is genuinely delivering (above
  ~20 MiB/s) and eases back when it is not. A board that answers `429 Too Many
  Requests` or times out gets its own smaller cap, so one throttled host cannot
  drag the healthy ones down. `--no-adaptive` pins the pool to a fixed size.
- **Four-level deduplication** — `uid` in SQLite, the file already present on
  disk, an optional sha256 check, and a perceptual pHash so the same artwork
  reposted to another board under a new id is skipped too. The first three
  levels are free; the pHash has to see the bytes, so a repost is downloaded,
  recognised and then deleted instead of being kept.
- **Aspect-ratio filtering** — `--wallpapers`, `--avatars`, or a custom
  `--aspect-ratio MIN:MAX`, applied after the API response so nothing extra is
  downloaded. Boards that omit image dimensions are kept by default; pass
  `--aspect-unknown skip` to drop them instead.
- **Tag-family blacklist** — a root tag pulls in its related sub-tags from
  `~/.config/bdl/blacklist.json`, plus a per-run `--blacklist` override. The tags
  bdl ships with are built in and cannot be edited away; the file holds only
  your own additions.
  Matching is component-wise, so `ai` also blocks `ai_artwork` and
  `ai_generated_v2`, but not `airplane`.
- **AI-art filter** — a built-in list of generator tags (Stable Diffusion,
  NovelAI, NAI, Midjourney, DALL·E, ComfyUI and the `ai_*` family) is skipped
  by default, because the boards apply it inconsistently. `--no-ai-filter`
  keeps those posts.
- **DNS-over-HTTPS and proxy support** — a built-in `DoHResolver` (Cloudflare)
  works around local DNS blocks on image CDNs, and an upstream proxy can be set.
- **uvloop when available** — the event loop switches to uvloop on every
  platform but Windows, which speeds up a batch of many small transfers.
- **Typo tolerance** — a tag that is not in the dictionary is matched against
  the popular tags and corrected before the query goes out (`mku` → `miku`).
- **Image previews** — inside Kitty, finished pictures are drawn once the batch
  ends as a compact gallery of thumbnail rows rather than a column running off
  the screen; `--no-preview` turns them off.
- **Interactive wizard** — `bdl` on a terminal (or `bdl tui`) walks through the
  download: boards with checkboxes, tags with live tag suggestions, limit, sort
  order, rating, geometry and an optional list of tags to exclude, then prints a
  summary before starting. The interface ships in Russian (default) and English;
  `questionary` supplies the arrow-key menus and `rich` the section headers.
- **Machine-readable output** — `--json` on `search` and `status`.

## Installation

Requires Python 3.11+. The repository is **Booru-Downloader-py**, the package is
published as **`booru-dl`**, and the console command it installs is **`bdl`**.

```bash
pip install booru-dl                  # or: pip install 'booru-dl[dev]' for tests/ruff
```

From a checkout:

```bash
git clone https://github.com/wexqk-boo/Booru-Downloader-py.git
cd Booru-Downloader-py
python3 -m venv .venv
./.venv/bin/pip install -e .          # or: pip install -e '.[dev]' for tests/ruff
```

The package installs a single entry point: `bdl`.

To put it on `PATH`:

```bash
./install.sh          # symlink the entry point into ~/.local/bin
./install.sh --copy   # copy it instead
./install.sh --remove # uninstall
```

Run `bdl` on a terminal to open the wizard. Without a terminal it prints usage
and exits `2`, so scripts never block on a prompt. If an old install left a
fish function named `bdl`, it shadows the binary — remove it
(`rm ~/.config/fish/functions/bdl.fish`) and run `functions -e bdl` once;
`./install.sh --remove` cleans that up together with the stale helper.

`rapidfuzz` powers tag suggestions and typo correction; without it the code
falls back to the standard-library `difflib`. `imagehash` and `pillow` enable
perceptual dedup, and `uvloop` speeds up the event loop everywhere but Windows.

## Usage

```bash
# Download every source, tag "miku", wallpaper aspect ratio only
bdl dl -s all -t "miku" --wallpapers

# Preview results without downloading
bdl search -s gelbooru -t "school_uniform" --limit 20

# Download statistics from the SQLite state
bdl status

# Daemon-style sync: repeated passes, already-downloaded posts skipped
bdl sync -s all -t "rating:general" --interval 900

# Blacklist management
bdl blacklist list                  # saved tags and built-in tags, separately
bdl blacklist add loli scat         # a family such as `ai` expands on its own
bdl blacklist remove loli
bdl blacklist clear                 # the user's tags only; built-ins stay
```

Other subcommands: `tui` (interactive wizard), `prune-stale` (forget DB rows
whose files are gone), `migrate-layout` (move an older library into the current
folder layout), `config` (`get` / `set` / `show`), and `tags` (tag
suggestions).

`prune-stale` only inspects paths under the current output root, so a run
against a temporary `--out` cannot forget the real library. Use `--all` to
check every recorded path, or `--out DIR` to pick a different scope.

### Migrating an older library

`bdl migrate-layout` re-sorts a library written by an older version — the
`<out_root>/<source>/<rating>/` tree — into the current layout. Each image is
classified by its real aspect ratio (read with Pillow), every film is moved to
the shared `videos/` root and sidecars to `json/`, and the recorded SQLite
paths are rewritten to follow the files. Emptied source folders are removed.

```bash
bdl migrate-layout --dry-run    # print the plan, change nothing
bdl migrate-layout              # perform it
```

`--dry-run` is safe to run at any time and reports exactly the moves a real run
would make. Pair it with `--json` for a machine-readable summary, or
`--keep-empty` to leave the old source folders in place. A move whose
destination already exists is reported as a conflict rather than overwriting
it, so nothing is lost.

### Wizard

```bash
bdl tui     # or bare `bdl` on a terminal, which reaches the same wizard
```

Rating choices are Safe / Suggestive / NSFW / All (`--safe`, `--suggestive`,
`--nsfw` on the command line), and geometry offers Wallpapers, Avatars, a
custom `MIN:MAX` range, or no filter. `MIN:MAX` accepts `1.5:1.8`, `1,5:1,8`,
`1.5/1.8` and width:height shorthands such as `16:9` or `3:4`.

The whole wizard is five screens and takes well under a minute: boards, the tag
query, limit and geometry, an optional line of tags to exclude, then the
summary. It never asks about concurrency — it starts from the saved setting (or
8) and the downloader tunes the pool to the measured speed from there. The
JSON/sha256/re-download switches stay off unless you pass them to `dl` yourself.

The wizard asks for the output folder only until one is known: the first answer
is saved to the config and later runs reuse it silently, unless `BDL_OUT` is
set, which wins outright and is never written back. The exclusion step is a
single comma-separated field — a root tag such as `ai` pulls in its whole family,
an empty answer excludes nothing. Tag fields complete from the local Danbooru
database as you type, and the run ends with a summary before the final
confirmation.

Common flags for `dl` / `sync`: `--limit`, `--per-page`, `--out`,
`--concurrency`, `--turbo`, `--no-adaptive`, `--sidecar`, `--checksum`,
`--force`, `--min-score`, `--rating-include`, `--max-size-mb`, `--blacklist`,
`--no-ai-filter`, `--no-tag-resolve`, `--no-progress`, `--no-preview`,
`--no-notify`, `--timeout`.

### Tag resolution

Boards disagree about tags: `1girl` is spelled `Solo` on Zerochan and `female`
on e621, an underscore is wrong where a space is right, and a border tag may be
one of several aliases. Before each search bdl maps the tags you typed onto
every selected board, using that board's own tag and alias API plus a small
cross-engine table, and logs what it did:

```
tag mapping for zerochan:
   1girl -> Solo  (board)
tag mapping for yandere:
   zzznope: not on this board
```

A tag a board does not have is reported, never silently dropped, and a board
whose tag API is unreachable leaves the tag exactly as typed rather than
hiding it. Answers are cached per board in `downloads.sqlite3` for seven days.
Pass `--no-tag-resolve` to search with the tags exactly as typed.

### The two blacklists

bdl excludes two separate things. The **built-in** list is the generator tags
(`ai`, `stable_diffusion`, `novelai`, …) and the like; it ships with the
program, is always in effect, and cannot be edited away by accident. Your own
exclusions live in `~/.config/bdl/blacklist.json` and are the only thing ever
written. `bdl blacklist list` shows both under their own heading, and
`bdl blacklist clear` empties only yours.

In the wizard the exclusion step is a menu: use the saved list, add tags (and
choose whether to keep them), remove tags from the saved list, or view the full
list and come back to the same step.

Concurrency, in order of precedence:

- `--turbo` — 20 parallel transfers, for fat (1–5 Gbit/s) pipes.
- `--concurrency N` — an explicit pool size; clamped to 1–64.
- `BDL_CONCURRENCY` — per-invocation override from the environment.
- `bdl config set concurrency 16` — a saved default; `bdl config get
  concurrency` prints what a run would actually use, and an unset setting
  answers `8`.

### Top of the week

`--order top` (the wizard's "Top of the week") means "sort by score", and each
board family spells that differently:

- Moebooru (Konachan, Yande.re, Sakugabooru) sorts through a *tag*: the request
  carries `order:score`. An `order=score` query argument is silently ignored by
  those boards, and their `date:>=` filter returns nothing, so a tagged top is
  honestly over all time. These boards reject a query of more than six tags, so
  `order:score` — and a `rating:` filter, which would cost another slot — is
  only sent while there is room. Past that the six best tags go out alone and
  the result is ranked by score locally (one page only, with a warning naming
  how many tags were left out).
- For an untagged top those boards use their own weekly listing
  (`/post/popular_recent.json?period=1w`) — the closest thing Moebooru has to
  "best of the week". It takes no tags and ignores paging, so it is only used
  for the first page with an empty query, with a score search as the fallback.
- A tagged top that comes back empty falls back to the board's weekly listing,
  and an empty weekly listing falls back to a score search. Some tags a board
  still lists in its own tag API are not served through search at all
  (`1girl` on Konachan returns zero posts while `long_hair` returns hundreds),
  so without the fallback "top of the week" would report `found=0` for a query
  the board plainly has material for. The fallback applies to `top` only — a
  `newest` search is never quietly served a ranking — and never to deep pages,
  because the listing cannot be paged.
- Gelbooru-family boards use `sort=score:desc`; Danbooru sends
  `search[order]=score`; Zerochan uses `s=fav`.

## Directory layout

Files are written as `<out_root>/<wallpapers|avatars>/<bucket>/<filename>`, and
every video goes to its own shared `<out_root>/videos/<bucket>/` root rather
than into a shape folder. Sidecar JSON is collected in a `json/` folder beside
the media it describes:

```
<out_root>/
├── wallpapers/      # aspect ratio 1.5–1.8 (16:9, 16:10)
│   └── safe/        # ratings: safe, s, general, g
│       ├── <file>.jpg
│       └── json/    # <file>.jpg.json sidecars (with --sidecar)
├── avatars/         # aspect ratio 0.6–1.05 (square, portrait)
│   └── suggestive/  # ratings: questionable, q, sensitive
│       └── json/
├── videos/          # every mp4, webm, mov, avi, mkv, flv, m4v, wmv lands here
│   └── safe/
│       ├── <file>.mp4
│       └── json/
└── nsfw/            # shapes fitting neither window keep the rating here
```

The shape folders come first, then the rating bucket; a file fitting neither
aspect window stays directly under its rating at the root. A video is never
judged by its shape, so a 16:9 clip is filed under `videos/`, not
`wallpapers/`. The default `out_root` comes from `BDL_OUT`, then the `out` key
in the config file, then `~/Pictures/booru`. Unknown or missing ratings land in
`nsfw/`.

## Configuration

- Config file: `~/.config/bdl/config.json`
- Download state (SQLite): `~/.local/share/bdl/downloads.sqlite3`
- Tag blacklist: `~/.config/bdl/blacklist.json` (`bdl blacklist list|add|remove|clear`)
- Tag database cache: `~/.cache/bdl/`
- Log: `~/.local/state/bdl/bdl.log`

Environment variables:

- `BDL_OUT` — override the output root (`BOORU_DL_OUT` still works).
- `BDL_LOG_FILE` — duplicate logs to a file (`BOORU_DL_LOG_FILE` still works).
- `BDL_LANG` — wizard language, `ru` (default) or `en`. Also settable through
  `bdl config set lang en`.
- `BDL_CONCURRENCY` — parallel transfers for this run; beats the saved setting.
- `PROXY_URL` — route requests through a proxy.

### Board blocks

Xbooru's DAPI needs no API key, but Cloudflare fronts the site and sometimes
challenges an IP with HTTP 403. Requests to Xbooru therefore carry a full set of
browser headers. If the IP is still challenged, the affected files are counted
as `blocked` instead of filling the screen with warnings, and the reason is
reported once in the batch summary at the end. Setting `PROXY_URL` is the usual
way out.

Zerochan needs a `User-Agent` naming the project (its [API
rules](https://www.zerochan.net/api) ban anonymous clients), so those requests
carry bdl's own. Its listing endpoint answers HTTP 500 even on success, and the
body has a stray brace before the JSON, so the client treats 500 as expected
there and parses the body leniently. Its tag spellings are redirects
(`/1girl` answers `301` to `/Solo`), and the redirect drops the query string, so
the client follows redirects by hand and carries `?json` and the pagination
parameters onto the target — following them automatically would fetch an HTML
page and fail to parse. Zerochan also does not say which extension
a picture has: bdl tries `jpg`, then `png`, `webp`, `gif` against
`static.zerochan.net`, using the same fallback for the media download.

Realbooru is **not** supported: the site disabled its DAPI server-side, so the
board could only ever answer with an error, and a source that cannot download
anything does not belong in the list.

Sakugabooru is a Moebooru board; `SAKUGABOORU_API_KEY` and
`SAKUGABOORU_LOGIN` are optional and only widen what an anonymous request may
see. Because the key is optional, a search that simply finds nothing says so
instead of blaming credentials.

- `bdl config set out ~/Pictures/booru` — default output root for every command
  that takes `--out` (`BDL_OUT` still wins over it).
- `bdl config set concurrency 16` — default number of parallel transfers
  (`--turbo`, `--concurrency` and `BDL_CONCURRENCY` still win over it).

## Development

```bash
./.venv/bin/python -m pytest tests -q    # unit tests, no network
./.venv/bin/ruff check bdl tests
```

## License

MIT
