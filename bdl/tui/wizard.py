"""Interactive setup wizard built on ``questionary``.

The wizard collects every parameter the downloader needs and returns it as a
plain dictionary. All user-facing strings come from :mod:`bdl.i18n`; no other
module in the project is modified by the wizard except the tag blacklist,
which is updated through its own public helpers.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..blacklist import add_tag, load_blacklist, remove_tag
from ..config import default_out_dir, env_first, get_setting, set_setting
from ..filters import AVATAR_ASPECT, WALLPAPER_ASPECT, parse_aspect_range
from ..i18n import t
from ..providers import ALL, PROVIDERS
from ..tags.families import TAG_FAMILIES, family_preview
from . import style

if TYPE_CHECKING:
    from types import ModuleType

# Rating option codes mapped to the value stored in the result dictionary.
# ``all`` means "no restriction" and is reported as ``None``.
_RATING_OPTIONS: tuple[tuple[str, str | None, str], ...] = (
    ("safe", "safe", "tui.rating_safe"),
    ("suggestive", "suggestive", "tui.rating_suggestive"),
    ("nsfw", "nsfw", "tui.rating_nsfw"),
    ("all", None, "tui.rating_all"),
)

_GEOMETRY_WALLPAPERS = "wallpapers"
_GEOMETRY_AVATARS = "avatars"
_GEOMETRY_CUSTOM = "custom"
_GEOMETRY_NONE = "none"

_ORDER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("new", "tui.order_new"),
    ("top", "tui.order_top"),
)

#: Toggles offered on the "extras" screen, in the order they are shown.
_OPTION_KEYS: tuple[str, ...] = ("sidecar", "checksum", "force", "notify")

#: Families that ship with the project; a root tag shows what it expands into.
CUSTOM_TAGS: frozenset[str] = frozenset(TAG_FAMILIES)

_DEFAULT_LIMIT = 50
_DEFAULT_CONCURRENCY = 3

#: How many blacklist entries the summary prints before collapsing the rest.
_SUMMARY_BLACKLIST = 8


def _load_questionary() -> ModuleType:
    """Import ``questionary`` lazily, translating a missing dependency."""
    try:
        import questionary
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(t("error.questionary_missing")) from exc
    return questionary


def _positive_int(text: str) -> bool | str:
    """Validator: the answer must be a whole number greater than zero."""
    stripped = text.strip()
    if stripped.isdigit() and int(stripped) > 0:
        return True
    return t("tui.limit_invalid")


def _non_empty(text: str) -> bool | str:
    """Validator: something has to be typed in."""
    return True if text.strip() else t("tui.out_required")


def _aspect_range(text: str) -> bool | str:
    """Validator: the answer must parse through ``parse_aspect_range``."""
    stripped = text.strip()
    if not stripped:
        return t("tui.aspect_required")
    try:
        parse_aspect_range(stripped)
    except ValueError as exc:
        return t("tui.aspect_invalid", value=stripped, error=str(exc))
    return True


def _at_least_one(selected: list[str]) -> bool | str:
    """Validator for the source checkbox."""
    return True if selected else t("tui.sources_required")


def _family_members() -> dict[str, str]:
    """Map each known tag to the family it belongs to."""
    members: dict[str, str] = {}
    for family, tags in TAG_FAMILIES.items():
        for tag in tags:
            members.setdefault(tag, family)
    return members


def _parse_extra_tags(raw: str) -> list[str]:
    """Split a free-form tag line on commas and whitespace."""
    return [chunk.strip().lower() for chunk in re.split(r"[,\s]+", raw) if chunk.strip()]


def _tag_completer() -> Any:
    """A prompt_toolkit completer over the local dictionary and the tag DB.

    Two entry points on purpose: prompt_toolkit drives the synchronous
    generator when it wants completions now, and only the async one may await
    the tag database, so the synchronous path serves the in-memory dictionary.
    """
    from prompt_toolkit.completion import Completer, Completion

    def typed_word(document: Any) -> str:
        return document.get_word_before_cursor(WORD=True).lower()

    def local_tags(word: str) -> list[str]:
        from ..tags.commands import local_hints

        try:
            return list(local_hints(word, limit=16))
        except Exception:  # pragma: no cover - the dictionary is optional
            return []

    def as_completions(word: str, hints: list[str]) -> list[Completion]:
        seen: set[str] = set()
        found: list[Completion] = []
        for hint in hints:
            if hint in seen:
                continue
            seen.add(hint)
            found.append(Completion(hint, start_position=-len(word)))
            if len(found) >= 16:
                break
        return found

    class _LocalTags(Completer):
        def get_completions(self, document: Any, complete_event: Any):
            word = typed_word(document)
            if len(word) < 2:
                return
            yield from as_completions(word, local_tags(word))

        async def get_completions_async(self, document: Any, complete_event: Any):
            word = typed_word(document)
            if len(word) < 2:
                return
            hints = local_tags(word)
            if len(hints) < 16:
                try:
                    from ..tags.store import db_hints, load_db

                    data = await load_db()
                    if data is not None:
                        hints.extend(db_hints(data, word, 16))
                except (OSError, RuntimeError, ValueError):
                    pass
            for completion in as_completions(word, hints):
                yield completion

    return _LocalTags()


async def _ask_sources(q: ModuleType, defaults: dict[str, Any]) -> list[str] | None:
    """Step 1 — pick one or more sources (or ``all``)."""
    preset = {key for key in (defaults.get("sources") or []) if key in PROVIDERS or key == ALL}
    if not preset:
        preset = {ALL}
    # questionary takes pre-selection per choice, not as a list of defaults.
    choices = [
        *(q.Choice(title=PROVIDERS[key].title, value=key, checked=key in preset) for key in PROVIDERS),
        q.Choice(title=t("tui.source_all"), value=ALL, checked=ALL in preset),
    ]
    return await q.checkbox(
        t("tui.sources"),
        choices=choices,
        validate=_at_least_one,
        instruction=t("tui.sources_hint"),
        style=style.build_style(),
    ).ask_async()


async def _ask_tags(q: ModuleType, defaults: dict[str, Any]) -> str | None:
    """Step 2 — free-form search tags; an empty string is allowed."""
    from prompt_toolkit.shortcuts import CompleteStyle

    return await q.text(
        t("tui.tags"),
        default=str(defaults.get("tags") or ""),
        instruction=t("tui.tags_hint"),
        style=style.build_style(),
        completer=_tag_completer(),
        complete_style=CompleteStyle.MULTI_COLUMN,
    ).ask_async()


async def _ask_limit(q: ModuleType, defaults: dict[str, Any]) -> int | None:
    """Step 3 — posts per source, a positive whole number."""
    preset = str(defaults.get("limit") or _DEFAULT_LIMIT)
    answer = await q.text(
        t("tui.limit"),
        default=preset,
        validate=_positive_int,
        instruction=t("tui.limit_hint"),
        style=style.build_style(),
    ).ask_async()
    if answer is None:
        return None
    return int(answer.strip())


async def _ask_order(q: ModuleType, defaults: dict[str, Any]) -> str | None:
    """Step 3b — how the board ranks the results."""
    preset = str(defaults.get("order") or "new").lower()
    choices = [q.Choice(title=t(label), value=code) for code, label in _ORDER_OPTIONS]
    answer = await q.select(
        t("tui.order"),
        choices=choices,
        default=preset if preset in {code for code, _ in _ORDER_OPTIONS} else "new",
        instruction=t("tui.order_hint"),
        style=style.build_style(),
    ).ask_async()
    return None if answer is None else ("" if answer == "new" else answer)


async def _ask_rating(q: ModuleType, defaults: dict[str, Any]) -> str | None:
    """Step 4 — allowed content category."""
    preset = str(defaults.get("rating") or "all").lower()
    choices = [q.Choice(title=t(label), value=code) for code, _, label in _RATING_OPTIONS]
    answer = await q.select(
        t("tui.category"),
        choices=choices,
        default=preset if preset in {code for code, _, _ in _RATING_OPTIONS} else "all",
        instruction=t("tui.category_hint"),
        style=style.build_style(),
    ).ask_async()
    for code, value, _ in _RATING_OPTIONS:
        if code == answer:
            return value
    return None


async def _ask_out(q: ModuleType, defaults: dict[str, Any]) -> Path | None:
    """Step 5 — output root, asked once and then remembered in the config.

    ``BDL_OUT`` wins outright and is never written back; a saved answer skips
    the question. ``bdl config set out`` (or deleting the key) brings it back.
    """
    from_env = env_first("BDL_OUT", "BOORU_DL_OUT")
    if from_env:
        return Path(from_env).expanduser()
    saved = get_setting("out").strip()
    if saved:
        return Path(saved).expanduser()

    preset = str(defaults.get("out") or default_out_dir())
    answer = await q.text(
        t("tui.out"),
        default=preset,
        validate=_non_empty,
        instruction=t("tui.out_hint"),
        style=style.build_style(),
    ).ask_async()
    if answer is None:
        return None
    out = Path(answer.strip()).expanduser()
    set_setting("out", str(out))
    return out


async def _ask_concurrency(q: ModuleType, defaults: dict[str, Any]) -> int | None:
    """Step 5b — how many files to download at once."""
    preset = str(defaults.get("concurrency") or _DEFAULT_CONCURRENCY)
    answer = await q.text(
        t("tui.concurrency"),
        default=preset,
        validate=_positive_int,
        instruction=t("tui.concurrency_hint"),
        style=style.build_style(),
    ).ask_async()
    if answer is None:
        return None
    return int(answer.strip())


async def _ask_aspect(q: ModuleType, defaults: dict[str, Any]) -> tuple[float, float] | None:
    """Step 6 — aspect-ratio window, asked only for the custom option."""
    options = (
        (_GEOMETRY_WALLPAPERS, "tui.geometry_wallpapers"),
        (_GEOMETRY_AVATARS, "tui.geometry_avatars"),
        (_GEOMETRY_CUSTOM, "tui.geometry_custom"),
        (_GEOMETRY_NONE, "tui.geometry_none"),
    )
    preset = str(defaults.get("geometry") or _GEOMETRY_NONE).lower()
    choices = [q.Choice(title=t(label), value=code) for code, label in options]
    answer = await q.select(
        t("tui.geometry"),
        choices=choices,
        default=preset if preset in {code for code, _ in options} else _GEOMETRY_NONE,
        instruction=t("tui.geometry_hint"),
        style=style.build_style(),
    ).ask_async()
    if answer is None:
        return None
    if answer == _GEOMETRY_WALLPAPERS:
        return WALLPAPER_ASPECT
    if answer == _GEOMETRY_AVATARS:
        return AVATAR_ASPECT
    if answer != _GEOMETRY_CUSTOM:
        return None
    raw = await q.text(
        t("tui.aspect"),
        default="",
        validate=_aspect_range,
        instruction=t("tui.aspect_hint"),
        style=style.build_style(),
    ).ask_async()
    if raw is None:
        return None
    return parse_aspect_range(raw)


async def _ask_blacklist(q: ModuleType, defaults: dict[str, Any]) -> set[str] | None:
    """Step 7 — review the blacklist, uncheck to drop tags, add new ones."""
    current = load_blacklist() | {str(tag).lower() for tag in (defaults.get("blacklist") or [])}
    selected: set[str] = set(current)
    style_obj = style.build_style()
    if current:
        # Show which family a tag belongs to, so `ai` and `ai_generated` read
        # as one entry rather than two unrelated lines. A root carries the
        # preview of what it pulls in, which is the whole point of a family.
        members = _family_members()
        ordered = sorted(current, key=lambda tag: (members.get(tag, tag), tag))
        width = max((len(tag) for tag in ordered), default=0)
        choices = []
        for tag in ordered:
            family = members.get(tag)
            if tag in CUSTOM_TAGS:
                suffix = family_preview(tag)
                title = f"{tag:<{width}}  {suffix}" if suffix else tag
            elif family:
                title = f"{tag:<{width}}  {family}"
            else:
                title = tag
            choices.append(q.Choice(title=title, value=tag, checked=True))
        answer = await q.checkbox(
            t("tui.blacklist"),
            choices=choices,
            instruction=t("tui.blacklist_hint"),
            style=style_obj,
        ).ask_async()
        if answer is None:
            return None
        selected = {str(tag) for tag in answer}

    extra_raw = await q.text(
        t("tui.blacklist_extra"),
        default="",
        instruction=t("tui.blacklist_extra_hint"),
        style=style_obj,
    ).ask_async()
    if extra_raw is None:
        return None

    for tag in sorted(current - selected):
        remove_tag(tag)
    for tag in [*selected, *_parse_extra_tags(extra_raw)]:
        add_tag(tag)
    return load_blacklist()


async def _ask_options(q: ModuleType, defaults: dict[str, Any]) -> dict[str, Any] | None:
    """Step 8 — the remaining switches."""
    preset = {key for key in (defaults.get("options") or []) if key in _OPTION_KEYS}
    # A first run should not silently lose the "done" notification, so the
    # switch starts on unless the caller says otherwise.
    checked = {key: key in preset or (key == "notify" and not preset) for key in _OPTION_KEYS}
    choices = [
        q.Choice(title=t(f"tui.opt_{key}"), value=key, checked=checked[key]) for key in _OPTION_KEYS
    ]
    picked = await q.checkbox(
        t("tui.options"),
        choices=choices,
        instruction=t("tui.options_hint"),
        style=style.build_style(),
    ).ask_async()
    if picked is None:
        return None

    return {
        "sidecar": "sidecar" in picked,
        "checksum": "checksum" in picked,
        "force": "force" in picked,
        "notify": "notify" in picked,
    }


def _describe_geometry(aspect: tuple[float, float] | None) -> str:
    if aspect is None:
        return t("tui.geometry_none")
    if aspect == WALLPAPER_ASPECT:
        return t("tui.geometry_wallpapers")
    if aspect == AVATAR_ASPECT:
        return t("tui.geometry_avatars")
    return f"{aspect[0]:g}–{aspect[1]:g}"


def _describe_options(options: dict[str, Any]) -> str:
    """One line: the switch names that are on."""
    labels = [t(f"tui.short_{key}") for key in _OPTION_KEYS if options.get(key)]
    return ", ".join(labels) if labels else t("tui.no_options")


def _describe_rating(rating: str | None) -> str:
    """Short label for the summary; ``None`` means every rating is allowed."""
    return t(f"tui.cat_{rating or 'all'}")


def _describe_sources(sources: list[str]) -> str:
    """Provider titles instead of the raw keys."""
    names = [PROVIDERS[key].title if key in PROVIDERS else t("tui.source_all") for key in sources]
    return ", ".join(names)


def _print_summary(answers: dict[str, Any]) -> None:
    """Echo the collected answers once, before the confirmation prompt."""
    rows = (
        (t("tui.summary_sources"), _describe_sources(answers["sources"])),
        (t("tui.summary_tags"), answers["tags"] or t("tui.no_tags")),
        (t("tui.summary_limit"), str(answers["limit"])),
        (t("tui.summary_rating"), _describe_rating(answers["rating"])),
        (t("tui.summary_geometry"), _describe_geometry(answers["aspect"])),
        (t("tui.summary_out"), str(answers["out"])),
        (t("tui.summary_concurrency"), str(answers["concurrency"])),
        (t("tui.summary_options"), _describe_options(answers["options"])),
    )
    console = style.console()
    console.print(f"\n[bold #ff5f87]▌[/] [bold]{t('tui.summary')}[/]")
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        console.print(f"  [dim]{label:<{width}}[/]  {value}")
    if answers["blacklist"]:
        tags = sorted(answers["blacklist"])
        shown = ", ".join(tags[:_SUMMARY_BLACKLIST]) if len(tags) > 1 else tags[0]
        if len(tags) > _SUMMARY_BLACKLIST:
            shown = f"{shown} {t('tui.summary_more', count=len(tags) - _SUMMARY_BLACKLIST)}"
        console.print(f"  [dim]{t('tui.summary_blacklist'):<{width}}[/]  {shown}")


async def run_wizard(defaults: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Run the interactive wizard and return the download parameters.

    Returns ``None`` when the user cancels (Ctrl+C, Esc or an empty answer to
    a prompt). Raises :class:`RuntimeError` when ``questionary`` is missing.
    """
    q = _load_questionary()
    preset: dict[str, Any] = dict(defaults or {})
    try:
        style.banner()
        style.section("tui.step_sources")
        sources = await _ask_sources(q, preset)
        if sources is None:
            return None

        style.section("tui.step_search")
        tags = await _ask_tags(q, preset)
        if tags is None:
            return None
        limit = await _ask_limit(q, preset)
        if limit is None:
            return None
        order = await _ask_order(q, preset)
        if order is None:
            return None
        rating = await _ask_rating(q, preset)

        style.section("tui.step_geometry")
        aspect = await _ask_aspect(q, preset)

        style.section("tui.step_blacklist")
        blacklist = await _ask_blacklist(q, preset)
        if blacklist is None:
            return None

        style.section("tui.step_options")
        out = await _ask_out(q, preset)
        if out is None:
            return None
        concurrency = await _ask_concurrency(q, preset)
        if concurrency is None:
            return None
        options = await _ask_options(q, preset)
        if options is None:
            return None

        answers = {
            "sources": list(sources),
            "tags": tags.strip(),
            "limit": limit,
            "order": order,
            "out": out,
            "concurrency": concurrency,
            "rating": rating,
            "aspect": aspect,
            "blacklist": blacklist,
            "options": options,
        }
        style.section("tui.step_review")
        _print_summary(answers)
        confirmed = await q.confirm(t("tui.confirm"), default=True, style=style.build_style()).ask_async()
    except (KeyboardInterrupt, EOFError):
        # Ctrl+C or stdin closed (e.g. piped input) — treat both as "cancel".
        return None
    if not confirmed:
        return None
    return answers


__all__ = ["run_wizard"]
