"""
Speech-recognition robustness.

Two failures observed on a live LiveKit session, both of which made a working
product look broken:

  1. "4L80E" came back as "4 L A T E". STT wrote the spoken "eighty" as the
     word "ATE". Exact alias lookup missed it, the agent said "tell me the
     part number", and the caller had no idea why.

  2. "yes place it" arrived as TWO final transcripts -- "yes." then "place
     it." -- because the caller paused. Neither half parses as a command on
     its own, so the authorization never fired.

Both are the identifier-over-a-degraded-channel problem in the RECOGNITION
direction, and neither is solvable by adding more literal aliases: the space
of things STT writes for an alphanumeric is effectively open. So this module
normalises to a phonetic skeleton and matches by similarity, and assembles
commands across turns instead of requiring one clean utterance.
"""

from __future__ import annotations

import difflib
import re
import time

# Words STT produces for digits, and the homophone traps that come with them.
# "for"/"four", "ate"/"eight", "oh"/"zero" are the ones seen live.
_SPOKEN_DIGITS = [
    ("eighteen", "18"), ("nineteen", "19"), ("seventeen", "17"),
    ("sixteen", "16"), ("fifteen", "15"), ("fourteen", "14"),
    ("thirteen", "13"), ("twelve", "12"), ("eleven", "11"),
    ("hundred", "00"),
    ("eighty", "80"), ("ate e", "80"), ("ate", "8"),
    ("seventy", "70"), ("sixty", "60"), ("fifty", "50"),
    ("forty", "40"), ("thirty", "30"), ("twenty", "20"), ("ninety", "90"),
    ("zero", "0"), ("oh", "0"), ("owe", "0"),
    ("one", "1"), ("won", "1"),
    ("two", "2"), ("too", "2"), ("to", "2"),
    ("three", "3"), ("tree", "3"),
    ("four", "4"), ("for", "4"), ("fore", "4"),
    ("five", "5"), ("six", "6"), ("seven", "7"),
    ("eight", "8"), ("nine", "9"), ("ten", "10"),
]

# Letters STT spells out inside identifiers: "el" for L, "ee" for E.
_SPOKEN_LETTERS = [
    ("double you", "w"), ("el", "l"), ("em", "m"), ("en", "n"),
    ("ee", "e"), ("ay", "a"), ("bee", "b"), ("see", "c"), ("sea", "c"),
    ("dee", "d"), ("jay", "j"), ("kay", "k"), ("pee", "p"), ("cue", "q"),
    ("are", "r"), ("ess", "s"), ("tee", "t"), ("you", "u"), ("vee", "v"),
    ("ex", "x"), ("why", "y"), ("zee", "z"), ("zed", "z"),
]


def normalise_identifier(text: str) -> str:
    """Collapse a spoken identifier to a compact alphanumeric skeleton."""
    out = f" {text.lower()} "
    for word, repl in _SPOKEN_DIGITS + _SPOKEN_LETTERS:
        out = re.sub(rf"(?<=[\s.,-]){re.escape(word)}(?=[\s.,-])", repl, out)
    out = re.sub(r"[^a-z0-9]+", "", out)
    # A bare "o" between alphanumerics is a spoken zero, not the letter.
    out = re.sub(r"(?<=[a-z0-9])o(?=[a-z0-9])", "0", out)
    return out


# Letter runs that are really a spoken digit. These only become visible AFTER
# the skeleton is built: "4 L A T E" tokenises as separate letters, so the
# word-level "ate" -> "8" rule never fires and you are left with "4late".
_SKELETON_SUBS = [("ate", "8"), ("ait", "8"), ("oh", "0"), ("owe", "0"),
                  ("won", "1"), ("tu", "2"), ("thr", "3"), ("fr", "4")]


def _variants(skeleton: str) -> list[str]:
    """The skeleton plus plausible digit re-readings of letter runs."""
    out = [skeleton]
    for run, digit in _SKELETON_SUBS:
        if run in skeleton:
            out.append(skeleton.replace(run, digit))
    return out


def match_sku(text: str, catalog: dict[str, str], threshold: float = 0.72):
    """Best catalog match for a spoken identifier, or None.

    Returns (sku, confidence). Similarity rather than exact lookup, because
    the set of things STT writes for "4L80E" is open-ended -- "4 L A T E",
    "for L 80 E", "4 l 8 oe" are all the same utterance.
    """
    skeleton = normalise_identifier(text)
    if len(skeleton) < 3:
        return None, 0.0

    best, best_score = None, 0.0
    for spoken in _variants(skeleton):
        for key, sku in catalog.items():
            for window in _windows(spoken, len(key)):
                score = difflib.SequenceMatcher(None, key, window).ratio()
                if score > best_score:
                    best, best_score = sku, score
    return (best, best_score) if best_score >= threshold else (None, best_score)


def _windows(s: str, size: int):
    """Sliding windows so a SKU embedded in a sentence still matches.

    Sizes vary around the key length because the re-read skeleton can be
    SHORTER than the key -- "4 L A T E please" becomes "4l8please", where the
    identifier core is only three characters. Fixed-width windows always
    dragged in the trailing filler and pushed the score below threshold.
    """
    seen = set()
    for width in range(max(3, size - 2), size + 3):
        if len(s) <= width:
            if s not in seen:
                seen.add(s)
                yield s
            continue
        for i in range(len(s) - width + 1):
            w = s[i : i + width]
            if w not in seen:
                seen.add(w)
                yield w


class CommandAssembler:
    """Assembles a command from consecutive partial utterances.

    "yes place it" arrives as "yes." then "place it." when the caller pauses.
    Neither half is a command. This keeps a short rolling window and offers
    the joined text to the parser, longest span first, so the complete command
    is found without waiting for the caller to say it perfectly in one breath.

    The window is deliberately short: joining across a long gap would let an
    old "yes" combine with a much later "place it" and authorize something the
    caller never said in one thought.
    """

    def __init__(self, window_seconds: float = 6.0, max_parts: int = 4):
        self.window = window_seconds
        self.max_parts = max_parts
        self._parts: list[tuple[float, str]] = []

    def add(self, transcript: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        text = transcript.strip()
        if text:
            self._parts.append((now, text))
        self._prune(now)

    def _prune(self, now: float) -> None:
        self._parts = [(t, s) for t, s in self._parts if now - t <= self.window]
        self._parts = self._parts[-self.max_parts :]

    def candidates(self) -> list[str]:
        """Joined spans, longest first. The longest match wins, so "yes place
        it" is preferred over the bare "place it" that follows it."""
        texts = [s for _, s in self._parts]
        out: list[str] = []
        for start in range(len(texts)):
            span = " ".join(texts[start:])
            if span:
                out.append(span)
        out.sort(key=len, reverse=True)
        return out

    def clear(self) -> None:
        self._parts.clear()
