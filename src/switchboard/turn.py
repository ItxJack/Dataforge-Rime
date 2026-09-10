"""
Turn controller.

The overlap_id exists because of two live LiveKit defects: an interruption
result can be applied to a LATER overlap than the one it was requested for, and
a second agent speech segment after a tool call can disarm an open overlap and
silently swallow the user's interruption.

So: every interruption request carries an overlap_id, and a result is applied
only if it still matches the current overlap. Framework speech-lifecycle events
never mutate transaction state -- they are validated here first, and only the
canonical FSM decides anything.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Verdict(Enum):
    BARGE_IN = "barge_in"
    BACKCHANNEL = "backchannel"
    NOISE = "noise"


@dataclass
class Overlap:
    overlap_id: str
    epoch: int
    opened_at_ms: float
    resolved: bool = False
    verdict: Optional[Verdict] = None


@dataclass
class Evidence:
    """Inputs to the interruption decision. Arousal is a FEATURE, never an
    override -- on a shop floor an impact wrench is a high-energy transient
    with a sharp onset, and an energy-triggered override stops the agent every
    time someone drops a socket. Voicing is what separates a shout from a tool.
    """

    vad_posterior: float = 0.0
    voicing: float = 0.0
    arousal: float = 0.0
    partial_words: int = 0
    echo_aligned: bool = False  # partial matches words currently in the air
    speaker_consistent: bool = True


class TurnController:
    def __init__(self, egress, cost_fp: float = 1.0, cost_fn: float = 1.0):
        self.egress = egress
        self.epoch = 0
        self.current: Optional[Overlap] = None
        self.history: list[Overlap] = []
        self.cost_fp = cost_fp  # falsely cutting: bounded by one repair
        self.cost_fn = cost_fn  # failing to yield: wrong part ships
        self.false_yields = 0
        self._resume_budget = 3

    def open_overlap(self, t_ms: float) -> Overlap:
        ov = Overlap(overlap_id=uuid.uuid4().hex[:8], epoch=self.epoch, opened_at_ms=t_ms)
        self.current = ov
        self.history.append(ov)
        self.egress.begin_yield()  # duck first, decide after
        return ov

    def posterior(self, ev: Evidence) -> float:
        """Calibrated posterior that this is a genuine barge-in.

        Deliberately simple and monotone; the real system fits this on
        narrowband audio and publishes a reliability diagram, because Bayes
        risk on uncalibrated scores is decoration.
        """
        if ev.echo_aligned:
            return 0.0  # our own audio, uniquely detectable from the ledger
        if not ev.speaker_consistent:
            return 0.05

        # Arousal is GATED BY VOICING. An impact wrench is a high-energy
        # transient with a sharp onset and no voicing; an ungated energy term
        # makes it indistinguishable from a shout, and the commitment-span
        # cost multiplier then guarantees it wins. Unvoiced input is suppressed
        # outright rather than merely down-weighted.
        if ev.voicing < 0.30:
            return max(0.0, min(1.0, 0.10 * ev.vad_posterior))

        p = (
            0.45 * ev.vad_posterior
            + 0.35 * ev.voicing
            + 0.20 * min(ev.arousal, 1.0) * ev.voicing
        )
        if ev.partial_words >= 1:
            p = min(1.0, p + 0.05)
        return max(0.0, min(1.0, p))

    def decide(self, ov: Overlap, ev: Evidence, in_commitment: bool) -> Verdict:
        """Explicit Bayes risk. No threshold in the notation, one reading.

        Inside a commitment span the costs move TOGETHER, not apart: falsely
        cutting is cheap because the repair policy re-opens the span from its
        start, while failing to yield while the caller corrects a part number
        ships the wrong part. So the system yields MORE readily on exactly the
        words an earlier draft made it deaf on.
        """
        if ov.resolved or self.current is None or ov.overlap_id != self.current.overlap_id:
            # Stale result bound to a superseded overlap: drop it.
            return Verdict.NOISE

        p_genuine = self.posterior(ev)
        c_fp = self.cost_fp  # bounded by repair cost
        c_fn = self.cost_fn * (4.0 if in_commitment else 1.0)

        risk_cut = c_fp * (1.0 - p_genuine)
        risk_continue = c_fn * p_genuine

        if risk_cut < risk_continue:
            verdict = Verdict.BARGE_IN
        elif ev.partial_words > 0 and p_genuine > 0.25:
            verdict = Verdict.BACKCHANNEL
        else:
            verdict = Verdict.NOISE

        ov.resolved = True
        ov.verdict = verdict

        if verdict is Verdict.BARGE_IN:
            self.epoch += 1
            self.egress.mute(self.epoch)
        else:
            self.false_yields += 1
            self.egress.restore()
            self._desensitise()
        return verdict

    def _desensitise(self):
        """Escalating damping. Without it, unstable VAD on a shop floor turns
        the readback into an oscillating 'sorry, where was I' loop."""
        self.cost_fp *= 1.6
        self._resume_budget -= 1

    @property
    def yields_exhausted(self) -> bool:
        return self._resume_budget <= 0

    def announce_resume(self) -> bool:
        """First resume is silent -- continue from the item boundary with no
        announcement. Announce only from the second."""
        return self.false_yields > 1
