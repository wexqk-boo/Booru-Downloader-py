"""Localization layer for the CLI and TUI.

Translations live in the sibling ``ru.json`` / ``en.json`` files and are pure
data — importing this module never pulls in ``questionary`` or any UI code.
Russian is the default language, English is the alternative.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_LANGUAGE = "ru"
SUPPORTED_LANGUAGES: tuple[str, ...] = ("ru", "en")

_LANG_DIR = Path(__file__).resolve().parent
_ENV_VAR = "BDL_LANG"

# Loaded dictionaries, keyed by language code. Loading is lazy.
_CACHE: dict[str, dict[str, str]] = {}
# Language forced through ``set_language``; ``None`` means auto-detect.
_forced: str | None = None
# Cached ``lang`` setting from the config file (read at most once per process).
_config_lang: str | None = None


def _load_dictionary(code: str) -> dict[str, str]:
    """Read one language file; a missing or broken file yields an empty dict."""
    cached = _CACHE.get(code)
    if cached is not None:
        return cached
    path = _LANG_DIR / f"{code}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    dictionary: dict[str, str] = {}
    if isinstance(data, dict):
        dictionary = {str(key): str(value) for key, value in data.items()}
    _CACHE[code] = dictionary
    return dictionary


def _normalize(code: str) -> str:
    """Reduce ``ru-RU`` / ``RU`` / ``en_US`` to a supported base code."""
    base = code.strip().lower().replace("-", "_").split("_", 1)[0]
    return base if base in SUPPORTED_LANGUAGES else ""


def _config_language() -> str:
    """Language from the config file; never raises when config is unavailable."""
    global _config_lang
    if _config_lang is None:
        try:
            from ..config import get_setting

            _config_lang = get_setting("lang") or ""
        except Exception:
            _config_lang = ""
    return _config_lang


def _detect_language() -> str:
    """Priority: ``BDL_LANG`` env → config ``lang`` → Russian default."""
    for candidate in (os.getenv(_ENV_VAR, ""), _config_language()):
        code = _normalize(candidate or "")
        if code:
            return code
    return DEFAULT_LANGUAGE


def current_language() -> str:
    """The active language code (``"ru"`` or ``"en"``)."""
    if _forced is not None:
        return _forced
    return _detect_language()


def set_language(code: str) -> None:
    """Force the active language.

    Unknown codes are ignored so a typo never breaks the UI; an empty string
    clears the override and returns to automatic detection.
    """
    global _forced, _config_lang
    normalized = _normalize(code or "")
    _forced = normalized or None
    if not normalized:
        # Re-read the config on the next lookup after an explicit reset.
        _config_lang = None


def t(key: str, **kwargs: object) -> str:
    """Translate *key*, interpolating ``kwargs`` through ``str.format``.

    A missing key returns the key itself instead of raising, so an incomplete
    translation file degrades gracefully. A broken placeholder does the same.
    """
    active = current_language()
    text = _load_dictionary(active).get(key)
    if text is None and active != DEFAULT_LANGUAGE:
        text = _load_dictionary(DEFAULT_LANGUAGE).get(key)
    if text is None:
        return key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def available_languages() -> tuple[str, ...]:
    """Language codes that ship with the package."""
    return SUPPORTED_LANGUAGES


__all__ = [
    "DEFAULT_LANGUAGE",
    "SUPPORTED_LANGUAGES",
    "available_languages",
    "current_language",
    "set_language",
    "t",
]
