"""Hyprland notifications via notify-send (dunst / mako / any daemon)."""

from __future__ import annotations

import asyncio
import logging

LOG = logging.getLogger(__name__)


async def notify(title: str, message: str, *, enabled: bool = True) -> None:
    """Show a desktop notification; silently no-op when notify-send is missing."""
    if not enabled:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "notify-send",
            "-a",
            "bdl",
            "-i",
            "dialog-information",
            title,
            message[:250],
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except FileNotFoundError:
        LOG.debug("notify-send not found, skipping notification")
    except Exception:
        LOG.debug("failed to show notification", exc_info=True)
