# bdl

Async batch downloader and sync tool for booru boards (Gelbooru, Rule34,
Xbooru, Safebooru, Danbooru, Konachan, Yande.re, Tbib, e621, e926).

## Features

- **Async streaming downloads** — `asyncio` + `aiohttp`, files streamed to disk
  in chunks, concurrency bounded by `asyncio.Semaphore`.
- **Three-level deduplication** — `uid` in SQLite, the file already present on
  disk, and an optional sha256 check. Already-known posts are skipped without a
  single media request.
- **Aspect-ratio filtering** — `--wallpapers`, `--avatars`, or a custom
  `--aspect-ratio MIN:MAX`, applied after the API response so nothing extra is
  downloaded. Boards that omit image dimensions are kept by default; pass
  `--aspect-unknown skip` to drop them instead.
- **Tag-family blacklist** — a root tag pulls in its related sub-tags from
  `~/.config/bdl/blacklist.txt`, plus a per-run `--blacklist` override.
- **DNS-over-HTTPS and proxy support** — a built-in `DoHResolver` (Cloudflare)
  works around local DNS blocks on image CDNs, and an upstream proxy can be set.
- **Interactive wizard** — `bdl` on a terminal (or `bdl tui`) walks through the
  download: boards with checkboxes, tags with live tag suggestions, limit, sort
  order, rating, geometry and an optional list of tags to exclude, then prints a
  summary before starting. The interface ships in Russian (default) and English;
  `questionary` supplies the arrow-key menus and `rich` the section headers.
- **Machine-readable output** — `--json` on `search` and `status`.

## Installation

Requires Python 3.11+. The repository is **Booru-Downloader-py**; the console
command it installs is **`bdl`**.

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

Optional extras: `pip install -e '.[fast]'` pulls in `rapidfuzz` for faster tag
suggestions (falls back to the standard-library `difflib` without it).

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
bdl blacklist list
bdl blacklist add loli
```

Other subcommands: `tui` (interactive wizard), `prune-stale` (forget DB rows
whose files are gone), `config` (`get` / `set` / `show`), and `tags` (tag
suggestions).

`prune-stale` only inspects paths under the current output root, so a run
against a temporary `--out` cannot forget the real library. Use `--all` to
check every recorded path, or `--out DIR` to pick a different scope.

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
summary. It never asks about concurrency — 8 parallel transfers keep a 100 Mbit
line busy — and the JSON/sha256/re-download switches stay off unless you pass
them to `dl` yourself.

The wizard asks for the output folder only until one is known: the first answer
is saved to the config and later runs reuse it silently, unless `BDL_OUT` is
set, which wins outright and is never written back. The exclusion step is a
single comma-separated field — a root tag such as `ai` pulls in its whole family,
an empty answer excludes nothing. Tag fields complete from the local Danbooru
database as you type, and the run ends with a summary before the final
confirmation.

Common flags for `dl` / `sync`: `--limit`, `--per-page`, `--out`,
`--concurrency`, `--sidecar`, `--checksum`, `--force`, `--min-score`,
`--rating-include`, `--max-size-mb`, `--blacklist`, `--no-progress`,
`--no-notify`, `--timeout`.

## Directory layout

Files are written as `<out_root>/<source>/<bucket>/[videos/]/<filename>`, with
all sidecar JSON collected in a `json/` folder:

```
<out_root>/<source>/
├── safe/            # ratings: safe, s, general, g
│   ├── <file>.jpg
│   ├── videos/      # mp4, webm, mov, avi, mkv, flv, m4v, wmv
│   └── json/        # <file>.jpg.json sidecars (with --sidecar)
├── suggestive/      # ratings: questionable, q, sensitive
└── nsfw/            # ratings: explicit, e
```

The default `out_root` comes from `BDL_OUT`, then the `out` key in the config
file, then `~/Pictures/booru`. Unknown or missing ratings land in `nsfw/`.

## Configuration

- Config file: `~/.config/bdl/config.json`
- Download state (SQLite): `~/.local/share/bdl/downloads.sqlite3`
- Tag blacklist: `~/.config/bdl/blacklist.txt`
- Tag database cache: `~/.cache/bdl/`
- Log: `~/.local/state/bdl/bdl.log`

Environment variables:

- `BDL_OUT` — override the output root (`BOORU_DL_OUT` still works).
- `BDL_LOG_FILE` — duplicate logs to a file (`BOORU_DL_LOG_FILE` still works).
- `BDL_LANG` — wizard language, `ru` (default) or `en`. Also settable through
  `bdl config set lang en`.
- `bdl config set out ~/Pictures/booru` — default output root for every command
  that takes `--out` (`BDL_OUT` still wins over it).

## Development

```bash
./.venv/bin/python -m pytest tests -q    # unit tests, no network
./.venv/bin/ruff check bdl tests
```

## License

MIT
