"""Standalone CLI for batch downloading and syncing content from Booru boards.

Self-contained: own providers, DoH resolver, models, dedup database and tag
dictionaries. Runs as ``python -m bdl`` or via the ``bdl`` console script.
"""

__version__ = "2.2.2"

#: Repository the project is published from.
REPO_SLUG = "github.com/wexqk-boo/Booru-Downloader-py"

#: User-Agent for normal outgoing API and media requests.
USER_AGENT = f"bdl/{__version__} ({REPO_SLUG})"

#: Xbooru has an open DAPI with no keys, but its Cloudflare edge challenges a
#: client that announces itself as a script, so both the API call and the media
#: fetch present a full set of browser headers instead.
XBOORU_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0"

XBOORU_HEADERS: dict[str, str] = {
    "User-Agent": XBOORU_USER_AGENT,
    "Referer": "https://xbooru.com/index.php?page=post&s=list",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

__all__ = ["REPO_SLUG", "USER_AGENT", "XBOORU_HEADERS", "XBOORU_USER_AGENT", "__version__"]
