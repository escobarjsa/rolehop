"""Console helpers shared by every command."""

from __future__ import annotations

import os
import sys

VERBOSE = os.environ.get("ROLEHOP_DEBUG", "") not in ("", "0")


def set_verbose(enabled: bool) -> None:
    """Turn on debug output (``--verbose`` or ``ROLEHOP_DEBUG=1``)."""
    global VERBOSE
    VERBOSE = VERBOSE or enabled


def debug(message: str) -> None:
    """Print a debug line to stderr when verbose output is on. Never pass secrets here."""
    if VERBOSE:
        print(f"[debug] {message}", file=sys.stderr)
