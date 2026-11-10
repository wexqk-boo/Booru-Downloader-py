"""Tag families: adding a root tag to /blacklist
automatically pulls in its related sub-tags.

Order matters: exact match in TAG_FAMILIES first,
then prefix lookup over TAGS_CATALOG (e.g. "ai" -> ai_generated).
"""

from __future__ import annotations

from .library import TAGS_CATALOG

TAG_FAMILIES: dict[str, tuple[str, ...]] = {
    "ai": (
        "ai_generated",
        "ai-generated",
        "ai_art",
        "ai_assisted",
        "stable_diffusion",
        "stable-diffusion",
        "midjourney",
        "novelai",
        "artificial_intelligence",
        "neural_network",
        "dall-e",
        "dalle",
        "machine_learning",
    ),
    "furry": (
        "anthro",
        "anthropomorphic",
        "feral",
        "fursuit",
        "canine",
        "equine",
        "dragon",
        "scalie",
        "avian",
        "werewolf",
    ),
    "futa": (
        "futanari",
        "futa",
        "dickgirl",
        "intersex",
        "transgender",
        "shemale",
        "newhalf",
        "herm",
        "hermaphrodite",
    ),
    "yaoi": (
        "gay",
        "male/male",
        "boys_love",
        "bara",
        "trap",
        "femboy",
        "crossdressing",
    ),
    "guro": (
        "gore",
        "vore",
        "inflation",
        "hyper",
    ),
    "fat": (
        "fat",
        "fat_arms",
        "fat_legs",
        "fat_belly",
        "chubby",
        "obese",
        "belly",
        "belly_button",
        "love_handles",
    ),
    "fart": (
        "farting",
        "fart",
        "flatulence",
        "gass",
        "toot",
    ),
    "feet": (
        "feet",
        "foot_focus",
        "footjob",
        "sole",
        "toes",
        "socks",
        "foot_lick",
    ),
}

# Single index of all known tags for prefix expansion.
_ALL_TAGS: frozenset[str] = frozenset(tag for pool in TAGS_CATALOG.values() for tag in pool)


def expand_blacklist_tags(tag: str) -> list[str]:
    """Return the sub-tags for the given root tag.

    Exact match in TAG_FAMILIES first. If none — add every known
    tag with the `tag_` prefix.
    """
    tag = tag.strip().lower()
    if not tag:
        return []
    exact = TAG_FAMILIES.get(tag)
    if exact:
        return list(exact)
    prefix = tag + "_"
    return sorted(t for t in _ALL_TAGS if t.startswith(prefix))


def family_preview(root: str, limit: int = 4) -> str:
    """Short ``(+ a, b, ... N more)`` note for a root tag, empty without one.

    Used wherever the blacklist is shown: the full list is long, but the point
    of a family is that adding the root also adds its sub-tags.
    """
    members = [tag for tag in expand_blacklist_tags(root) if tag != root]
    if not members:
        return ""
    head = ", ".join(members[:limit])
    rest = len(members) - limit
    return f"(+ {head}, {rest} more)" if rest > 0 else f"(+ {head})"
