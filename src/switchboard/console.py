"""
Console output compatibility.

WHY THIS EXISTS. The reports in eval/ used box-drawing characters and check
marks. On a Windows PowerShell console the default encoding is cp1252, which
cannot represent them, so `print` raised UnicodeEncodeError and the judge
report crashed after every other stage had passed. The repo ran clean on Linux
and macOS and died on the machine most likely to be running it.

Two-stage fix, in order of preference:

  1. Ask stdout to re-encode as UTF-8. Works on Python 3.7+ and on modern
     Windows Terminal, which renders the glyphs correctly.
  2. If that fails (older consoles, redirected pipes with a fixed codec), fall
     back to an ASCII glyph set. Degraded, never broken.

Colour is handled the same way: enabled only when the stream is a real TTY, so
piping to a file or a CI log produces clean text instead of escape codes.
"""

from __future__ import annotations

import os
import sys

UNICODE_GLYPHS = {
    "heavy": "\u2501",  # ━
    "light": "\u2500",  # ─
    "tick": "\u2713",   # ✓
    "cross": "\u2717",  # ✗
    "bang": "!",
    "dot": "\u00b7",    # ·
    "arrow": "\u2192",  # →
}

ASCII_GLYPHS = {
    "heavy": "=",
    "light": "-",
    "tick": "+",
    "cross": "x",
    "bang": "!",
    "dot": "|",
    "arrow": "->",
}


def _try_utf8(stream) -> bool:
    """Re-encode stdout as UTF-8 if the stream will allow it."""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
        return True
    except (AttributeError, ValueError, OSError):
        return False


def _encodes_glyphs(stream) -> bool:
    enc = getattr(stream, "encoding", None) or "ascii"
    try:
        "".join(UNICODE_GLYPHS.values()).encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def init() -> dict:
    """Call once at the top of any script that prints a report.

    Returns the glyph set that is actually safe to print on this console.

    SWITCHBOARD_ASCII=1 forces the ASCII set. run.py sets it for children when
    its OWN console cannot render the glyphs -- otherwise a child writing to a
    pipe happily upgrades to UTF-8, and the parent then crashes relaying that
    output to a cp1252 terminal. The parent knows the real terminal; children
    only see a pipe, so the decision has to come from above.
    """
    if os.environ.get("SWITCHBOARD_ASCII") == "1":
        return ASCII_GLYPHS
    stream = sys.stdout
    if not _encodes_glyphs(stream):
        _try_utf8(stream)
    return UNICODE_GLYPHS if _encodes_glyphs(stream) else ASCII_GLYPHS


def unicode_ok() -> bool:
    """Whether THIS console can render the glyph set, after any upgrade."""
    return _encodes_glyphs(sys.stdout)


def colours() -> dict:
    """ANSI codes, or empty strings when the output is not a terminal.

    Windows Terminal and PowerShell 7 handle ANSI; redirected output should not
    contain escape codes, and NO_COLOR is honoured because it is a convention
    worth respecting.
    """
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return {k: "" for k in ("B", "G", "R", "Y", "X")}
    return {
        "B": "\033[1m",
        "G": "\033[32m",
        "R": "\033[31m",
        "Y": "\033[33m",
        "X": "\033[0m",
    }


def child_env() -> dict:
    """Environment for subprocesses that print reports.

    run.py captures child output; without this the child inherits cp1252 and
    crashes exactly the way the parent used to.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if not unicode_ok():
        env["SWITCHBOARD_ASCII"] = "1"
    return env
