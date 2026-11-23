"""Terminal output: status symbols, color and aligned status lines, following clig.dev conventions.

- Color only when the stream is a terminal; never when ``NO_COLOR`` is set, ``TERM=dumb`` or ``--no-color`` is
  passed. ``FORCE_COLOR`` forces it. Colors have one meaning each: green done, yellow action needed, red failed,
  cyan the next command to run, dim secondary detail.
- Every line that reports a problem is followed by the command that fixes it (``→ run: …``).
- Symbols are plain text characters, never emoji, with an ASCII fallback for non-UTF-8 terminals.
- Debug detail goes to stderr and only with ``--verbose`` / ``ROLEHOP_DEBUG=1``.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

VERBOSE = os.environ.get("ROLEHOP_DEBUG", "") not in ("", "0")
_no_color_flag = False

SYMBOLS = {"ok": "✓", "warn": "!", "error": "✗", "next": "→", "info": "·"}
ASCII = {"ok": "+", "warn": "!", "error": "x", "next": "->", "info": "-"}
COLORS = {"ok": "32", "warn": "33", "error": "31", "next": "36", "info": "2", "dim": "2", "bold": "1"}


def set_verbose(enabled: bool) -> None:
    """Turn on debug output (``--verbose`` or ``ROLEHOP_DEBUG=1``)."""
    global VERBOSE
    VERBOSE = VERBOSE or enabled


def set_no_color(enabled: bool) -> None:
    global _no_color_flag
    _no_color_flag = _no_color_flag or enabled


def color_enabled(stream: TextIO | None = None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("FORCE_COLOR", "") not in ("", "0"):
        return True
    if _no_color_flag or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _unicode(stream: TextIO) -> bool:
    try:
        "✓✗→·".encode(getattr(stream, "encoding", None) or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def symbol(kind: str, stream: TextIO | None = None) -> str:
    stream = stream or sys.stdout
    return (SYMBOLS if _unicode(stream) else ASCII)[kind]


def paint(text: str, kind: str, stream: TextIO | None = None) -> str:
    if not color_enabled(stream or sys.stdout):
        return text
    return f"\033[{COLORS[kind]}m{text}\033[0m"


def echo(text: str = "", *, err: bool = False) -> None:
    """Print a line; a leading ✓ / ✗ / ! / → gets its color when writing to a terminal."""
    stream = sys.stderr if err else sys.stdout
    stripped = text.lstrip()
    lead = text[: len(text) - len(stripped)]
    for kind in ("ok", "error", "warn", "next"):
        sym = SYMBOLS[kind]
        if stripped.startswith(sym + " ") or stripped == sym:
            shown = symbol(kind, stream)
            text = lead + paint(shown, kind, stream) + stripped[len(sym):]
            break
    print(text, file=stream)


def item(kind: str, name: str, detail: str, fix: str = "", *, width: int = 0, err: bool = False) -> None:
    """One aligned status line, plus the fix on its own line when there is one.

    ✓ acme      active · ~6h40m left (estimated)
    ✗ personal  no session
                → run: rolehop login personal
    """
    stream = sys.stderr if err else sys.stdout
    pad = max(width, len(name))
    head = f"{paint(symbol(kind, stream), kind, stream)} {paint(name.ljust(pad), 'bold', stream)}  {detail}"
    print(head.rstrip(), file=stream)
    if fix:
        print(f"{' ' * (pad + 4)}{paint(symbol('next', stream) + ' ' + fix, 'next', stream)}", file=stream)


def short(path: object) -> str:
    """Show a path with ~ for the home directory, as users type it."""
    text = str(path)
    home = os.path.expanduser("~")
    if home and home != "/" and (text == home or text.startswith(home + os.sep)):
        return "~" + text[len(home):]
    return text


def hint(text: str, *, err: bool = False) -> None:
    """Secondary, dimmed line (a tip, never required to act on)."""
    stream = sys.stderr if err else sys.stdout
    print(paint(text, "dim", stream), file=stream)


def debug(message: str) -> None:
    """Print a debug line to stderr when verbose output is on. Never pass secrets here."""
    if VERBOSE:
        print(f"[debug] {message}", file=sys.stderr)
