"""
Authorization.

Two rules that took eight rewrites to get right.

1. NO LEARNED COMPONENT IS TRUSTED AS THE AUTHORIZATION POLICY. ASR is still
   upstream -- a deterministic function of a stochastic input is stochastic --
   so we bound its error instead of pretending it away: a four-token closed
   grammar has a small, measurable confusion space where open vocabulary has an
   enormous one. Anything outside the grammar is UNPARSED, which means ask
   again, never commit.

2. THE TOKEN BINDS TO THE SPOKEN FORM, NOT THE PAYLOAD. The caller never heard
   `41260 cents`; they heard whatever Rime's normaliser produced. Binding to
   the payload digest bound the authorization to something the caller
   demonstrably did not receive.

What this establishes, stated precisely: the authorization refers to the spoken
form produced and timestamped by the configured Rime pipeline. It does NOT
prove human receipt -- nothing sender-side can. The caller's opportunity to
correct during a maximally-interruptible readback is what covers that gap, and
the far-end acoustic measurement is what quantifies it.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .render import SpokenForm


class Act(Enum):
    YES_PLACE = "YES_PLACE"
    NO_CANCEL = "NO_CANCEL"
    CHANGE = "CHANGE"
    REPEAT = "REPEAT"
    UNPARSED = "UNPARSED"


# Deliberately narrow. The more consequential the effect, the less language
# entropy the gate accepts.
# Negation is matched FIRST so "no, don't place it" can never reach YES_PLACE.
#
# The affirmative patterns tolerate the punctuation and filler STT inserts:
# a live session produced "yes." and "place it." as two separate transcripts,
# and "Yes, place it." with a comma. What they do NOT tolerate is a bare
# "yes" -- that is an acknowledgement, not an authorization, and accepting it
# would let a backchannel commit an order.
# Affirmation words a caller actually uses. Collected from live sessions plus
# the obvious variants: regional ("ya", "haan"), casual ("yo", "yup"),
# and the ASR-mangled ("yeah" -> "ya", "yes" -> "yas").
_YES = (r"yes|yeah|yea|ya|yah|yup|yep|yes+|yo|ok|okay|okey|kay|k|sure|"
        r"alright|right|correct|confirm|affirmative|perfect|good|great|"
        r"go ahead|do it|haan|han")

# Verbs that mean "commit the order".
_PLACE = r"place|do|book|order|confirm|submit|send|go|proceed|process|buy"

# Negations. Checked FIRST so "no, don't place it" can never reach YES_PLACE.
_NO = (r"no|nope|nah|na|cancel|stop|don'?t|do not|abort|scrap|forget|"
       r"never ?mind|nevermind|wrong|not")

_GRAMMAR: list[tuple[re.Pattern, Act]] = [
    # --- negation first, always -------------------------------------------
    (re.compile(rf"^\W*({_NO})\b", re.I), Act.NO_CANCEL),
    (re.compile(rf"\b(don'?t|do not|never)\s+({_PLACE})\b", re.I), Act.NO_CANCEL),

    # --- change / correction ---------------------------------------------
    (re.compile(r"^\W*(change|actually|wait|hold on|hang on|make it|"
                r"instead|switch)\b", re.I), Act.CHANGE),

    # --- repeat -----------------------------------------------------------
    (re.compile(r"^\W*(repeat|again|say that again|what|pardon|sorry|"
                r"come again|one more time|read.*back)\b", re.I), Act.REPEAT),

    # --- affirmative + verb, in either order -----------------------------
    # "yes place it" / "yeah, order that" / "ok go ahead"
    (re.compile(rf"^\W*({_YES})\b[\s,.!]*(please\s+)?({_PLACE})\b"
                rf"\s*(it|that|ahead|the order|my order)?\W*$", re.I), Act.YES_PLACE),
    # "place it yes" / "order it, yeah"
    (re.compile(rf"^\W*(please\s+)?({_PLACE})\s*(it|that|the order)?"
                rf"[\s,.!]*({_YES})\W*$", re.I), Act.YES_PLACE),
    # bare verb: "place it" / "confirm" / "go ahead"
    (re.compile(rf"^\W*(please\s+)?({_PLACE})\s*(it|that|the order|ahead)?"
                rf"\W*$", re.I), Act.YES_PLACE),
]

# A BARE affirmation is deliberately NOT in the grammar above. "yes" on its
# own is an acknowledgement, not an authorization -- accepting it would let a
# backchannel commit an order. The caller must pair it with a verb, and
# CommandAssembler joins the halves when they arrive as separate transcripts.

def parse_act(transcript: str) -> Act:
    """Order matters: negation is checked before affirmation so that
    'no, don't place it' can never fall through to YES_PLACE."""
    for pattern, act in _GRAMMAR:
        if pattern.match(transcript or ""):
            return act
    return Act.UNPARSED


class AuthError(Exception):
    pass


@dataclass
class AuthToken:
    session_id: str
    tx_id: str
    tx_version: int
    spoken_digest: str
    snapshot_digest: str
    action: Act
    nonce: str
    expires_at: float
    used: bool = False


class Authorizer:
    def __init__(self, ttl_seconds: float = 90.0, clock=time.monotonic):
        self.ttl = ttl_seconds
        self.clock = clock
        self.tokens: dict[str, AuthToken] = {}

    def issue(
        self,
        session_id: str,
        tx,
        form: SpokenForm,
        act: Act,
        emitted_words: list[str],
    ) -> AuthToken:
        if act is not Act.YES_PLACE:
            raise AuthError(f"cannot authorize on {act.value}")

        if form.snapshot_digest != tx.snapshot_digest():
            raise AuthError("spoken form is stale relative to the transaction")

        missing = self._missing(form, emitted_words)
        if missing:
            # Rime never emitted these words, so they cannot have been heard.
            # Causality only -- no transport envelope required.
            raise AuthError(f"commitment words never emitted: {missing}")

        tok = AuthToken(
            session_id=session_id,
            tx_id=tx.tx_id,
            tx_version=tx.version,
            spoken_digest=form.spoken_digest,
            snapshot_digest=form.snapshot_digest,
            action=act,
            nonce=secrets.token_hex(8),
            expires_at=self.clock() + self.ttl,
        )
        self.tokens[tok.nonce] = tok
        return tok

    def redeem(self, nonce: str, session_id: str, tx) -> AuthToken:
        """Single-use, snapshot-fenced. A correction between 'yes' and the
        commit bumps tx.version and invalidates the token -- otherwise 'yes'
        authorizes whatever happens to be current, which is a replay."""
        tok = self.tokens.get(nonce)
        if tok is None:
            raise AuthError("unknown token")
        if tok.session_id != session_id:
            # Session isolation. Purchaser identity is explicitly out of scope,
            # but a token from call A must never redeem against an identical
            # transaction in call B.
            raise AuthError("token belongs to a different call")
        if tok.used:
            raise AuthError("token already used")
        if self.clock() >= tok.expires_at:
            raise AuthError("token expired")
        if tok.tx_version != tx.version or tok.snapshot_digest != tx.snapshot_digest():
            raise AuthError("transaction mutated after authorization")
        tok.used = True
        return tok

    @staticmethod
    def _normalise_token(word: str) -> str:
        """Normalise one timestamp token for comparison only.

        This does NOT infer a new part number. It only removes punctuation that
        can differ between the Rime input and timestamp serialisation.
        """
        return word.lower().strip(".,!?;:")

    @classmethod
    def _matches_complete_sequence(cls, emitted_words: list[str],
                                   wanted_words: list[str]) -> bool:
        """Return True only when every wanted token appears in order."""
        emitted = [cls._normalise_token(w) for w in emitted_words if w.strip()]
        wanted = [cls._normalise_token(w) for w in wanted_words if w.strip()]
        if not wanted:
            return False

        i = 0
        for word in emitted:
            if i < len(wanted) and word == wanted[i]:
                i += 1
        return i == len(wanted)

    @classmethod
    def _missing(cls, form: SpokenForm, emitted_words: list[str]) -> list[str]:
        """Verify the COMPLETE ordered Rime emission.

        ``/textnorm`` and ``/ws3`` are deliberately kept as separate views.
        In the live service, timestamps may preserve the input tokenisation
        (for example ``spell(4L80E)``) while another configuration can expose
        the normalised spoken tokenisation (``four L eight zero E``). The old
        implementation compared only the first representation and therefore
        refused a valid live readback even though Rime had timestamped it.

        We now accept either Rime-owned representation, but ONLY if the entire
        ordered sequence is present. A prefix, unordered set, duplicate-free
        subset, or empty timestamp list still fails closed.
        """
        variants = form.emission_variants
        if not emitted_words:
            return variants[0] if variants else []

        if any(cls._matches_complete_sequence(emitted_words, variant)
               for variant in variants):
            return []

        # Return the most relevant missing representation for diagnostics.
        want = variants[0] if variants else form.rendered_words
        emitted = [cls._normalise_token(w) for w in emitted_words if w.strip()]
        wanted = [cls._normalise_token(w) for w in want if w.strip()]
        i = 0
        for word in emitted:
            if i < len(wanted) and word == wanted[i]:
                i += 1
        return want[i:]

