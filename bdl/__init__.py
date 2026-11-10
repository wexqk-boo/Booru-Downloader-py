"""Standalone CLI for batch downloading and syncing content from Booru boards.

Self-contained: own providers, DoH resolver, models, dedup database and tag
dictionaries. Runs as ``python -m bdl`` or via the ``bdl`` console script.
"""

__version__ = "2.1.0"

#: Repository the project is published from.
REPO_SLUG = "github.com/wexqk-boo/Booru-Downloader-py"

#: User-Agent for normal outgoing API and media requests.
USER_AGENT = f"bdl/{__version__} ({REPO_SLUG})"

#: A conventional browser UA for Xbooru, which rejects this tool's UA.
XBOORU_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0"

__all__ = ["REPO_SLUG", "USER_AGENT", "XBOORU_USER_AGENT", "__version__"]
