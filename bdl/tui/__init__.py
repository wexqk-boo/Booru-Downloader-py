"""Interactive TUI package for ``bdl``.

Re-exports :func:`run_wizard`, the entry point that collects download
parameters through a ``questionary`` wizard, plus the shared screen style.
"""

from __future__ import annotations

from . import style
from .wizard import run_wizard

__all__ = ["run_wizard", "style"]
