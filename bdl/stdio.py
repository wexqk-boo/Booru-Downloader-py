"""Keep the console streams writable after the event loop has touched them.

The event loop puts stdout and stderr into non-blocking mode when it wires them
up for a subprocess, and not every code path puts them back. A long summary —
or a few hundred kilobytes of graphics escape codes — written to a
non-blocking pipe then dies half-way through with
``BlockingIOError [Errno 11] Resource temporarily unavailable``, which is a
silly way to lose the end of a download run.

Flushing and restoring blocking mode before writing anything substantial costs
nothing and removes the whole class of failure.
"""

from __future__ import annotations

import contextlib
import os
import sys


def restore_blocking() -> None:
    """Flush stdout/stderr and return them to blocking mode.

    Safe on anything: a stream captured by a test runner or replaced by a
    wrapper has no usable file descriptor, and that is fine — the streams that
    actually break are the ones backed by a pipe or a terminal.
    """
    set_blocking = getattr(os, "set_blocking", None)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(OSError, ValueError):
            stream.flush()
        if set_blocking is None:  # Windows without the call
            continue
        with contextlib.suppress(AttributeError, OSError, ValueError):
            set_blocking(stream.fileno(), True)
