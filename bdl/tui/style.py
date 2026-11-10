"""Look and feel shared by every wizard screen.

Kept apart from the wizard itself so the palette and the section headers can
change without touching the question flow.
"""

from __future__ import annotations

from typing import Any

from ..i18n import t

#: Palette applied to every prompt. One definition keeps the screens
#: consistent: cyan accents, pink pointer, green for what stays selected.
_COLORS: tuple[tuple[str, str], ...] = (
    ("qmark", "fg:#00afff bold"),
    ("question", "bold"),
    ("answer", "fg:#00afff bold"),
    ("pointer", "fg:#ff5f87 bold"),
    ("highlighted", "fg:#ff5f87 bold"),
    ("selected", "fg:#5faf5f"),
    ("separator", "fg:#6c6c6c"),
    ("instruction", "fg:#808080 italic"),
    ("disabled", "fg:#6c6c6c italic"),
)

_console: Any = None
_style: Any = None


def console() -> Any:
    """The shared rich console; created on first use."""
    global _console
    if _console is None:
        from rich.console import Console

        _console = Console()
    return _console


def build_style() -> Any:
    """A questionary ``Style``; built once and reused by every prompt."""
    global _style
    if _style is None:
        import questionary

        _style = questionary.Style(list(_COLORS))
    return _style


def banner() -> None:
    """Header shown once, before the first question."""
    from rich.panel import Panel
    from rich.text import Text

    body = Text()
    body.append("bdl", style="bold #00afff")
    body.append("  ·  ", style="#6c6c6c")
    body.append(t("tui.tagline"), style="")
    console().print(Panel(body, border_style="#00afff", padding=(0, 2)))


def section(key: str) -> None:
    """Print a section heading above the questions it groups."""
    console().print(f"\n[bold #ff5f87]▌[/] [bold]{t(key)}[/]")


def note(text: str) -> None:
    """A dim one-line remark, used for hints and echoes."""
    console().print(f"  [dim]{text}[/]")
