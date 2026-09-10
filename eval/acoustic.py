"""
Far-end acoustic residue.  `python run.py acoustic <recording.wav> <reference.wav>`

THE ONE NUMBER THIS PROJECT CANNOT GET ANY OTHER WAY.

Every other measurement here is taken at our own audio-source boundary. That
boundary is upstream of the kernel socket buffer, the NIC, the carrier and the
handset's jitter buffer, so "zero stale frames" says nothing about what came
out of the earpiece. The PS asks for what the user experiences, not a
convenient proxy. This is that measurement.

METHOD (matched filter)

You record the caller's handset earpiece with a second phone. That recording
contains the agent's speech AND the caller's barge-in mixed into one channel,
so energy thresholding cannot separate them -- both are speech.

The advantage we have is that we know exactly what the agent said: the
synthesised PCM is committed alongside the recording. So:

  1. cross-correlate the recording against the reference -> sample offset
  2. least-squares gain fit (a phone speaker attenuates unpredictably)
  3. subtract the aligned reference -> residual = caller voice + room noise
  4. T0 = onset of the residual envelope        (the caller, agent removed)
  5. T3 = last frame where the aligned reference is still audible
  6. residue = T3 - T0

WHAT IT DOES AND DOES NOT ESTABLISH

It measures acoustic energy at the earpiece. It does NOT establish that the
caller perceived, parsed or understood anything -- that remains unprovable from
our side, and this file does not claim it.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from switchboard.console import init as console_init  # noqa: E402

console_init()

FRAME_MS = 10
# Projection needs a longer window than the envelope: at 10 ms (80 samples at
# 8 kHz) a 210 Hz agent and an 870 Hz caller are not close to orthogonal, so
# leakage keeps the projection high and the agent never appears to stop.
PROJ_FRAME_MS = 30
# "Still audible" is decided per recording, not by a fixed constant.
#
# A fixed -12 dB floor worked on a loud handset and failed on a quiet one: the
# presence ratio divides by the fitted gain, so a small gain lifts the residual
# floor to around -10 dB and the agent never appears to stop. The signal itself
# was fine in every case -- the threshold was not.
#
# So split ON from OFF adaptively: take the midpoint between the level while
# the agent is definitely playing (lead-in) and the level once it has
# definitely stopped (tail). Refuse if the two are not separable.
MIN_ON_OFF_SEPARATION_DB = 10.0
# Speech is ~40 dB above a quiet room. -35 dBFS sits well clear of handset
# hiss while still catching a tail that a listener would notice.
AUDIBLE_DBFS = -35.0
ONSET_HOLD_MS = 60          # sustained, so a click is not an onset


def row(k: str, v: str) -> None:
    print(f"  {k:<34} {v}", flush=True)


def read_wav(path: Path):
    """Mono float samples in [-1, 1], plus sample rate."""
    import numpy as np

    with wave.open(str(path), "rb") as w:
        n, ch, sw, sr = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError(f"{path.name}: expected 16-bit PCM, got {sw * 8}-bit")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def resample(x, src: int, dst: int):
    import numpy as np

    if src == dst:
        return x
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)


def envelope_dbfs(x, sr: int, frame_ms: int = FRAME_MS):
    """Per-frame RMS in dBFS."""
    import numpy as np

    step = max(1, int(sr * frame_ms / 1000))
    n = len(x) // step
    if n == 0:
        return np.array([]), step
    frames = x[: n * step].reshape(n, step)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-9)), step


def align(recording, reference):
    """Sample offset of the reference within the recording, by cross-correlation."""
    import numpy as np

    a = recording - recording.mean()
    b = reference - reference.mean()
    n = 1 << (len(a) + len(b) - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    return int(np.argmax(corr[: max(1, len(a))]))


FIT_WINDOW_MS = 400          # agent-only audio used to fit the earpiece gain
MIN_LEAD_IN_MS = 300         # required agent-only audio before the barge-in


def fit_gain(recording, reference, offset: int, sr: int,
             window_ms: int = FIT_WINDOW_MS) -> float:
    """Least-squares earpiece gain, fitted ONLY on agent-only lead-in audio.

    THE BUG THIS FIXES. Fitting across the whole reference underestimated the
    gain badly (0.37 against a true 0.60) because the agent falls silent after
    the cut while the reference keeps going -- the fit averages in that
    silence. The residual then retained ~23% of the agent, which sat 10 dB
    above the caller and made the barge-in undetectable.

    The lead-in is agent-only by construction, so it is the only region where
    the fit is valid.
    """
    import numpy as np

    m = min(len(recording) - offset, len(reference), int(sr * window_ms / 1000))
    if m <= 0:
        return 0.0
    seg, ref = recording[offset : offset + m], reference[:m]
    denom = float(ref @ ref)
    return 0.0 if denom == 0 else float(seg @ ref) / denom


def agent_presence_ratio_db(recording, aligned_ref, sr: int, gain: float,
                            frame_ms: int = PROJ_FRAME_MS):
    """Per-frame amplitude of the agent AS PRESENT IN THE RECORDING.

    THE CONCEPTUAL BUG THIS FIXES. An earlier version read T3 off the aligned
    reference -- but the reference is what Rime synthesised, and it keeps going
    past the moment the caller stopped hearing it. Measuring the tail from the
    reference reports the reference's length, not the residue. It gave 700 ms
    against a true 120 ms.

    Instead, project each frame of the recording onto the corresponding frame
    of the reference. The projection coefficient is ~the earpiece gain while
    the agent is audible and ~0 once it stops, because the caller's voice is
    uncorrelated with the reference.
    """
    import numpy as np

    step = max(1, int(sr * frame_ms / 1000))
    n = min(len(recording), len(aligned_ref)) // step
    if n == 0:
        return np.array([])
    r = recording[: n * step].reshape(n, step)
    f = aligned_ref[: n * step].reshape(n, step)
    denom = (f * f).sum(axis=1)
    coef = np.where(denom > 1e-12, (r * f).sum(axis=1) / np.maximum(denom, 1e-12), 0.0)
    # Ratio to the fitted gain: ~0 dB while the agent plays normally, falling
    # away once it stops. Frames where the reference itself is silent carry no
    # information and are marked absent.
    ref_rms = np.sqrt((f * f).mean(axis=1))
    ratio = np.where(ref_rms > 1e-4, np.abs(coef) / max(abs(gain), 1e-9), 0.0)
    return 20 * np.log10(np.maximum(ratio, 1e-6))


def sustained_onset(db, sr: int, step: int, floor: float, hold_ms: int) -> int | None:
    """First frame that stays above `floor` for hold_ms. Rejects clicks."""
    need = max(1, hold_ms // FRAME_MS)
    run = 0
    for i, v in enumerate(db):
        run = run + 1 if v > floor else 0
        if run >= need:
            return i - need + 1
    return None


def analyse(recording_path: Path, reference_path: Path) -> dict:
    import numpy as np

    rec, sr_r = read_wav(recording_path)
    ref, sr_f = read_wav(reference_path)
    sr = min(sr_r, sr_f)
    rec, ref = resample(rec, sr_r, sr), resample(ref, sr_f, sr)

    offset = align(rec, ref)
    gain = fit_gain(rec, ref, offset, sr)

    aligned = np.zeros_like(rec)
    end = min(len(rec), offset + len(ref))
    aligned[offset:end] = ref[: end - offset] * gain

    residual = rec - aligned                      # caller + room, agent removed
    db_res, step = envelope_dbfs(residual, sr)
    # agent energy actually in the RECORDING, not in the reference
    presence = agent_presence_ratio_db(rec, aligned, sr, gain)

    # Noise floor from the LEAD-IN only: that region is agent-only, so after
    # subtraction it contains nothing but room noise and fit error. Taking the
    # floor over the whole file would include the caller and inflate it.
    lead_frames = max(1, int(MIN_LEAD_IN_MS / FRAME_MS))
    lead = db_res[:lead_frames]
    noise_floor = float(np.percentile(lead, 90)) if len(lead) else -90.0
    # max, NOT min. min() put the floor BELOW the measured noise, so on a
    # noisy recording the onset fired at frame 0 and every downstream level
    # was computed over the wrong window. A regression introduced while fixing
    # the gain fit, and invisible until a noisy case was tested.
    onset_floor = max(AUDIBLE_DBFS, noise_floor + 10.0)

    t0_frame = sustained_onset(db_res, sr, step, onset_floor, ONSET_HOLD_MS)
    if t0_frame is None:
        return {"error": "no caller barge-in found in the residual"}

    # map the envelope-frame index of T0 onto the coarser projection frames
    t0_proj = int(t0_frame * FRAME_MS / PROJ_FRAME_MS)
    after = presence[t0_proj:]
    if len(after) < 4:
        return {"error": "recording ends too soon after the barge-in"}

    on_level = float(np.median(presence[:max(1, t0_proj)]))
    tail = after[max(1, int(len(after) * 0.75)):]
    off_level = float(np.median(tail)) if len(tail) else -60.0
    separation = on_level - off_level
    if separation < MIN_ON_OFF_SEPARATION_DB:
        return {"error": f"agent-on and agent-off levels differ by only "
                         f"{separation:.0f} dB; raise the handset volume or "
                         f"reduce room noise and re-record"}
    threshold = (on_level + off_level) / 2.0

    # End of the last SUSTAINED run above threshold, not the last isolated
    # frame. A single noisy frame 700 ms after the agent stopped once dragged
    # a 120 ms residue out to 840 ms. Real audio does not reappear for 30 ms
    # and vanish again; noise does.
    runs, start = [], None
    for i, v in enumerate(after):
        if v > threshold and start is None:
            start = i
        elif v <= threshold and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(after) - 1))
    sustained = [r for r in runs if r[1] - r[0] + 1 >= 2]
    audible = [sustained[-1][1]] if sustained else (
        [runs[0][1]] if runs and runs[0][0] == 0 else [])
    # Index, NOT index+1. Frame 0 is the frame containing T0 itself, so a cut
    # exactly at T0 must read 0 ms. The +1 added one whole frame to every
    # measurement -- a constant +30 ms bias that a sweep against known values
    # made obvious and a single test case would have hidden.
    residue_ms = audible[-1] * PROJ_FRAME_MS if audible else 0.0

    return {
        "sample_rate": sr,
        "alignment_offset_ms": offset / sr * 1000,
        "reference_gain_db": 20 * np.log10(max(abs(gain), 1e-9)),
        "noise_floor_dbfs": noise_floor,
        "t0_barge_in_ms": t0_frame * FRAME_MS,
        "residue_ms": residue_ms,
        "quantisation_ms": PROJ_FRAME_MS,
        "agent_on_level_db": on_level,
        "agent_off_level_db": off_level,
        "threshold_db": threshold,
        "separation_db": separation,
    }


def main(argv: list[str]) -> int:
    try:
        import numpy  # noqa: F401
    except ImportError:
        print("\n  numpy is required for acoustic analysis:\n"
              "    pip install numpy\n")
        return 1

    if len(argv) < 2:
        print(__doc__)
        print("  Record the caller's handset with a second phone during a")
        print("  barge-in, then:\n")
        print("    python run.py acoustic call.wav fixtures/live/reference.wav\n")
        return 2

    rec, ref = Path(argv[0]), Path(argv[1])
    for p in (rec, ref):
        if not p.exists():
            print(f"\n  not found: {p}\n")
            return 1

    print(f"\nFAR-END ACOUSTIC RESIDUE\n{'-' * 66}")
    row("recording", rec.name)
    row("reference (what Rime synthesised)", ref.name)
    res = analyse(rec, ref)
    if "error" in res:
        row("FAILED", res["error"])
        print("\n  Check the recording actually contains a barge-in, and that\n"
              "  the reference is the utterance that was interrupted.\n")
        return 1

    print()
    row("alignment offset", f"{res['alignment_offset_ms']:.0f} ms")
    row("reference gain at the earpiece", f"{res['reference_gain_db']:.1f} dB")
    row("residual noise floor", f"{res['noise_floor_dbfs']:.1f} dBFS")
    row("T0 caller barge-in onset", f"{res['t0_barge_in_ms']:.0f} ms")
    print()
    row("FAR-END ACOUSTIC RESIDUE",
        f"{res['residue_ms']:.0f} ms  (+/- {res['quantisation_ms']} ms)")
    row("agent on / off levels", f"{res['agent_on_level_db']:.0f} / "
        f"{res['agent_off_level_db']:.0f} dB   separation "
        f"{res['separation_db']:.0f} dB")
    row("adaptive threshold", f"{res['threshold_db']:.0f} dB")
    print(f"{'-' * 66}")
    print("  This is acoustic energy at the earpiece. It does NOT establish\n"
          "  that the caller perceived it. Record the number and the method\n"
          "  in RIME_EVIDENCE.md; commit both WAVs alongside it.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
