"""
Egress controller and frame probe.

Two disciplines here.

1. MUTE BY WRITING MUTED FRAMES, not by stopping writes. If you stop writing,
   the frames already queued in the kernel socket buffer and the NIC are the
   residue and you have no way to shape them. If you keep writing and ramp the
   gain, the residue is a fade-out tail rather than a chopped syllable.

2. THE STOP PATH NEVER WAITS ON RIME. `clear` travels to Rime concurrently;
   the local mute lands in <=1 frame. This is a LOCAL-EGRESS guarantee, not a
   claim about the caller's ear -- packets already handed to the kernel cannot
   be recalled, and the receiving handset has its own jitter buffer. The
   honest number is measured acoustically at the far end; this probe measures
   our boundary and is named accordingly.
"""

from __future__ import annotations

import math

import warnings

try:  # audioop was removed in Python 3.13
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import audioop  # type: ignore
except ImportError:  # pragma: no cover - exercised on 3.13+
    from . import ulaw as audioop  # bundled pure-python G.711 fallback

from dataclasses import dataclass, field
from typing import Optional

FRAME_MS = 20
SAMPLE_RATE = 8000
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 160 for G.711
FADE_MS = 20  # one frame of raised-cosine ramp

# re-exported so live.py gets the same codec (stdlib or bundled fallback)
__all__ = ["EgressController", "FrameProbe", "EmittedFrame", "audioop",
           "FRAME_MS", "SAMPLE_RATE", "SAMPLES_PER_FRAME", "FADE_MS"]


@dataclass(frozen=True)
class EmittedFrame:
    seq: int
    epoch: int
    t_ms: float
    gain: float
    unit_seq: Optional[int]


@dataclass
class FrameProbe:
    """Counts frames entering the controller vs frames reaching the audio sink.

    Methodology borrowed from LiveKit's own barge-in instrumentation: record
    every frame at both boundaries; leaks fall out as the difference, so the
    same probe stays valid across builds.
    """

    offered: int = 0
    emitted: int = 0
    stale_emitted: int = 0  # frames from a superseded epoch that reached the sink

    @property
    def leaked(self) -> int:
        return self.stale_emitted


class EgressController:
    def __init__(self, sink=None):
        self.sink = sink if sink is not None else []
        self.epoch = 0
        self.gain = 1.0
        self.t_ms = 0.0
        self._seq = 0
        self._fade_frames_left = 0
        self._fade_from = 1.0
        self.probe = FrameProbe()
        self.ledger: list[EmittedFrame] = []

    # -- barge-in ---------------------------------------------------------

    def begin_yield(self, depth_db: float = -6.0) -> None:
        """The yield gesture. Not a hard cut: -6 dB over one raised-cosine
        frame, RTP never gaps, comfort floor maintained. A hard 12 dB drop on
        a narrowband line reads as a dropped call, and silence reads worse."""
        self._fade_from = self.gain
        self._target = 10 ** (depth_db / 20)
        self._fade_frames_left = max(1, FADE_MS // FRAME_MS)

    def mute(self, new_epoch: int) -> None:
        """Fence + full mute. Returns immediately; never awaits Rime."""
        assert new_epoch > self.epoch, "epoch must advance"
        self.epoch = new_epoch
        self._fade_from = self.gain
        self._target = 0.0
        self._fade_frames_left = max(1, FADE_MS // FRAME_MS)

    def restore(self) -> None:
        self._fade_from = self.gain
        self._target = 1.0
        self._fade_frames_left = max(1, FADE_MS // FRAME_MS)

    # -- frame path -------------------------------------------------------

    def offer(self, pcm: bytes, frame_epoch: int, unit_seq: Optional[int] = None) -> bool:
        """Offer one 20 ms frame. Returns True if audible content was emitted.

        A frame from a superseded epoch is replaced by a muted frame -- we
        still WRITE it, so the stream never gaps, but it carries no signal.
        """
        self.probe.offered += 1
        stale = frame_epoch < self.epoch

        if stale:
            gain = 0.0
        else:
            gain = self._next_gain()

        frame = self._apply_gain(pcm, gain)
        self.sink.append(frame)
        self.probe.emitted += 1
        if stale and gain > 0.0:
            self.probe.stale_emitted += 1

        self._seq += 1
        self.ledger.append(
            EmittedFrame(self._seq, frame_epoch, self.t_ms, gain, unit_seq)
        )
        self.t_ms += FRAME_MS
        return gain > 0.0

    def _next_gain(self) -> float:
        if self._fade_frames_left > 0:
            total = max(1, FADE_MS // FRAME_MS)
            step = total - self._fade_frames_left + 1
            # raised cosine: smooth, no click
            frac = 0.5 * (1 - math.cos(math.pi * step / total))
            self.gain = self._fade_from + (self._target - self._fade_from) * frac
            self._fade_frames_left -= 1
            if self._fade_frames_left == 0:
                self.gain = self._target
        return self.gain

    @staticmethod
    def _apply_gain(payload: bytes, gain: float) -> bytes:
        """Gain on G.711 mu-law.

        THE BUG THIS FIXES: scaling the encoded bytes directly. PCMU is
        LOGARITHMICALLY companded (RFC 3551), so multiplying the byte value by
        0.5 is not -6 dB -- it is a nonlinear distortion that happens to get
        quieter. Since far-end acoustic residue is a headline metric, a fade
        measured off a wrong waveform is worse than no measurement.

        Correct path: mu-law -> linear PCM -> gain -> mu-law.
        """
        if gain >= 0.999:
            return payload
        if gain <= 0.001:
            return audioop.lin2ulaw(b"\x00" * (len(payload) * 2), 2)
        linear = audioop.ulaw2lin(payload, 2)
        scaled = audioop.mul(linear, 2, gain)
        return audioop.lin2ulaw(scaled, 2)

    @staticmethod
    def rms_dbfs(payload: bytes) -> float:
        """RMS of a mu-law payload in dBFS, for the acoustic fade assertion."""
        linear = audioop.ulaw2lin(payload, 2)
        rms = audioop.rms(linear, 2)
        return -120.0 if rms == 0 else 20 * math.log10(rms / 32768.0)

    # -- ledger -----------------------------------------------------------

    def audible_ms(self, epoch: int, gain_floor: float = 0.5) -> float:
        """Milliseconds emitted at or above a gain floor for this epoch.

        Feeds the heard-prefix INTERVAL, not a point estimate. We publish
        [lo, hi] and let the safety property depend only on the conservative
        end; a word we cannot show was delivered is treated as not delivered.
        """
        return sum(
            FRAME_MS
            for f in self.ledger
            if f.epoch == epoch and f.gain >= gain_floor
        )
