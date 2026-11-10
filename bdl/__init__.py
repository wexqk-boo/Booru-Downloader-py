"""Standalone CLI for batch downloading and syncing content from Booru boards.

Self-contained: own providers, DoH resolver, models, dedup database and tag
dictionaries. Runs as ``python -m bdl`` or via the ``bdl`` console script.
"""

__version__ = "2.1.0"

#: Repository the project is published from.
REPO_SLUG = "github.com/wexqk-boo/Booru-Downloader-py"

#: User-Agent for every outgoing request, so boards can identify the client.
USER_AGENT = f"bdl/{__version__} ({REPO_SLUG})"

__all__ = ["REPO_SLUG", "USER_AGENT", "__version__"]
