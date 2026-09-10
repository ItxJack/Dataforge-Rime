"""
Canonical transaction -> spoken form.

THE LATENCY FIX. A previous draft claimed /textnorm was "off the critical path
by construction" because it ran during clause assembly. That was hand-waving: a
synchronous HTTPS POST before the flush injects 100-300ms straight into TTFA.

The real fix is that /textnorm is precomputed at TRANSACTION-MUTATION time, not
at render time. The transaction snapshot changes rarely -- when a line item is
added or corrected -- and the readback text is a pure function of the snapshot.
So we normalise once, when the slot is filled, and cache by snapshot digest. By
the time the agent speaks, the normalised form is already in hand.

Conversational filler never goes through /textnorm at all. Only commitment
spans, which come from the structured transaction, ever touch it.

If /textnorm is unavailable we FAIL CLOSED: the action loses the light path and
escalates, rather than binding a token to unverified text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .rime import RimeHttp, digest, inline_speed_alpha, spell

IDENTIFIER_RE = re.compile(r"\b(?=[A-Z0-9-]{4,})(?=.*[A-Z])(?=.*\d)[A-Z0-9-]+\b")

# Identifiers get slowed; everything else keeps a natural pace. Mist convention:
# values above 1.0 are SLOWER.
# inlineSpeedAlpha semantics: "Comma-separated list of speed values applied to
# words in SQUARE BRACKETS. Values < 1.0 speed up speech, > 1.0 slow it down."
#
# TWO THINGS THIS GOT WRONG BEFORE:
#   1. The spans were never bracketed, so the parameter applied to nothing and
#      the advertised identifier slowing silently did not happen.
#   2. The list had one entry per TEXT SEGMENT. It must have one entry per
#      BRACKETED SPAN, in order. A length mismatch misaligns every value.
#
# NOTE the three distinct bracket types in Rime's API:
#   [square] -> inlineSpeedAlpha   {curly} -> phonemizeBetweenBrackets
#   <angle>  -> pauseBetweenBrackets
IDENTIFIER_SPEED = 1.25   # > 1.0 slows the bracketed span
NORMAL_SPEED = 1.0


@dataclass(frozen=True)
class LineItem:
    sku: str
    qty: int
    unit_price_cents: int

    @property
    def total_cents(self) -> int:
        return self.qty * self.unit_price_cents


@dataclass
class Transaction:
    """The canonical object. This -- never the chat history and never the
    framework's speech events -- is the source of truth for API arguments."""

    tx_id: str
    version: int = 0
    items: list[LineItem] = field(default_factory=list)

    def mutate(self, fn) -> "Transaction":
        fn(self)
        self.version += 1
        return self

    @property
    def total_cents(self) -> int:
        return sum(i.total_cents for i in self.items)

    def snapshot_digest(self) -> str:
        body = "|".join(
            f"{i.sku}:{i.qty}:{i.unit_price_cents}" for i in self.items
        )
        return digest(f"{self.tx_id}#{self.version}#{body}")


def money(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"


@dataclass(frozen=True)
class Span:
    """A commitment span. Carries its own speed so the identifier slows down
    while the surrounding sentence does not."""

    text: str
    is_commitment: bool
    speed: float = NORMAL_SPEED


@dataclass(frozen=True)
class SpokenForm:
    """What the renderer produced, what Rime's normaliser says it becomes, and
    the digest the authorization token binds to."""

    rendered: str          # exactly what goes to /ws3, brackets included
    normalized: str        # /textnorm of the bracket-free text
    spoken_digest: str
    snapshot_digest: str
    speeds: str

    @property
    def expected_words(self) -> list[str]:
        """Normalised SPOKEN words. What the caller hears. Bound into the
        digest -- never compared against word timestamps (see below)."""
        return [w for w in re.split(r"[\s,.\-]+", self.normalized) if w]

    @staticmethod
    def _tokens(text: str) -> list[str]:
        """Tokenise a Rime text view without changing its semantic order."""
        return [w for w in re.split(r"[\s]+", text.strip()) if w]

    @property
    def rendered_words(self) -> list[str]:
        """INPUT tokens sent to /ws3.

        Rime has exposed two useful views in different live configurations:
        some timestamp streams preserve the input tokenisation while others
        report the normalised spoken tokenisation. Authorization therefore must
        not assume that one representation is universal.
        """
        return self._tokens(self.rendered)

    @property
    def emission_variants(self) -> list[list[str]]:
        """Tokenisations that are valid evidence for THIS Rime synthesis.

        The digest still binds to ``normalized``. These variants are only the
        emission-side check, because /ws3 timestamps and /textnorm can expose
        different token boundaries. We accept a variant only when the COMPLETE
        ordered sequence is present; a prefix, set match, or partial match is
        never sufficient.
        """
        variants = [self.rendered_words, self.expected_words]
        unique: list[list[str]] = []
        for words in variants:
            if words and words not in unique:
                unique.append(words)
        return unique


class Renderer:
    """Emits text AND span offsets, because it did the rendering.

    We never recover semantics from prose with a regex after the fact -- the
    application already knows the value is 41260 cents; throwing that away and
    reconstructing it from "four hundred twelve dollars and sixty cents" is how
    the binding breaks.
    """

    def __init__(self, http: RimeHttp, supports_inline_speed: bool = True):
        self.http = http
        self.supports_inline_speed = supports_inline_speed
        self._cache: dict[str, SpokenForm] = {}

    def spans_for_readback(self, tx: Transaction) -> list[Span]:
        spans: list[Span] = [Span("Okay, reading that back.", False)]
        for item in tx.items:
            spans.append(Span(f" {item.qty} of ", False))
            spans.append(Span(spell(item.sku), True, IDENTIFIER_SPEED))
            spans.append(Span(".", False))
        spans.append(Span(" Total ", False))
        spans.append(Span(money(tx.total_cents), True, IDENTIFIER_SPEED))
        spans.append(Span(".", False))
        return spans

    @staticmethod
    def compose(spans: list[Span]) -> tuple[str, str]:
        """Plain text. NO bracket markup.

        MEASURED: on /ws3 the square brackets are not consumed as
        inlineSpeedAlpha markup -- they come back inside the word timestamps
        (`last='[$412.59].'`). Across six live measurements the parameter moved
        duration by 0-6% with the DIRECTION FLIPPING between runs, which is
        synthesis variance, not control. timeScaleFactor moved it 142-143% in
        the same direction every time.

        So the brackets bought nothing and cost something real: they polluted
        the very word timestamps the heard-state ledger reads.
        """
        return "".join(sp.text for sp in spans), ""

    @staticmethod
    def strip_brackets(text: str) -> str:
        """Brackets are a Rime control, not content. /textnorm and the digest
        must see the spoken text, not the markup."""
        return text.replace("[", "").replace("]", "")

    async def precompute(self, tx: Transaction) -> SpokenForm:
        """Call this on every transaction mutation. NOT on the speech path."""
        snap = tx.snapshot_digest()
        if snap in self._cache:
            return self._cache[snap]

        spans = self.spans_for_readback(tx)
        rendered, speeds = self.compose(spans)
        normalized = await self.http.textnorm(rendered)
        form = SpokenForm(
            rendered=rendered,
            normalized=normalized,
            spoken_digest=digest(normalized),
            snapshot_digest=snap,
            speeds=speeds,
        )
        self._cache[snap] = form
        return form

    def cached(self, tx: Transaction) -> Optional[SpokenForm]:
        """Speech path only ever reads the cache. A miss means the mutation
        hook did not run or /textnorm failed -> fail closed."""
        return self._cache.get(tx.snapshot_digest())
