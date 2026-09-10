"""
Speed-control diagnostic.  `python run.py speed`

WHY THIS IS A SEPARATE EXPERIMENT. An earlier run observed that two renders at
inlineSpeedAlpha 1.60 and 0.60 differed by 60 ms on an 8.2 s utterance, and I
concluded "mistv3 ignores the parameter". That conclusion was not supported by
the observation, because the test confounded three independent things:

  1. Is the parameter reaching the model at all?
  2. Is my BRACKET MARKUP valid?
  3. Does the model honour it?

Rime's documented example brackets single plain words -- "This sentence is
[really] [fast]" with "0.5, 3". My markup bracketed `[spell(4L80E)]` (a
function call) and `[$412.59]` (a currency token that normalises to six words).
Either could bind nothing while the parameter works perfectly.

So this runs a MATRIX and lets each cell answer one question:

  A  docs example, no speed control          -> baseline duration
  B  docs example + inlineSpeedAlpha         -> does the parameter work at all?
  C  our readback markup + inlineSpeedAlpha  -> does OUR markup bind?
  D  docs example + timeScaleFactor          -> does any speed control work?

Reading the result:
  B differs from A, C does not  -> the parameter works; OUR MARKUP is wrong
  B and C both match A, D differs -> inlineSpeedAlpha specifically is inert
  nothing differs from A          -> no speed control reaches this model
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from switchboard.console import init as console_init  # noqa: E402

console_init()

from switchboard.render import LineItem, Renderer, Transaction  # noqa: E402
from switchboard.rime import RimeConfig, RimeHttp, HttpSession  # noqa: E402

# Rime's documented example text, but NOT its alpha values.
#
# The docs use "0.5, 3" -- 0.5 SPEEDS UP "really" while 3 SLOWS DOWN "fast".
# On a two-second utterance those cancel, so the cell can never show a clean
# signal no matter whether the parameter works. Copying an illustrative
# example into an experiment without checking that it can produce a signal is
# how the first run reached the wrong conclusion.
#
# Here both words move the SAME way, and we compare slow-vs-fast rather than
# treated-vs-baseline, which doubles the effect size.
DOCS_TEXT = "This sentence is [really] [fast]."
ALPHA_SLOW = "2.0, 2.0"   # >1.0 slows on the Mist family
ALPHA_FAST = "0.5, 0.5"   # <1.0 speeds up

OUT = ROOT / "fixtures" / "live"

# RELATIVE, not absolute. A 240 ms difference is noise on a 2 s clip and a
# real effect on an 8 s one; a fixed millisecond threshold called a genuine
# 3% shift "noise" purely because the utterance was long.
TOLERANCE_PCT = 4.0


def differs(a: float | None, b: float | None) -> tuple[bool, float]:
    """Relative difference, and whether it clears the noise floor."""
    if a is None or b is None or a == 0:
        return False, 0.0
    pct = abs(b - a) / a * 100.0
    return pct > TOLERANCE_PCT, pct


def row(k: str, v: str) -> None:
    print(f"  {k:<38} {v}", flush=True)


async def render(cfg: RimeConfig, key: str, text: str, extra: dict | None):
    from eval.live import one_utterance_with  # local import to share the client

    return await one_utterance_with(cfg, key, text, extra)


async def main() -> int:
    key = os.environ.get("RIME_API_KEY")
    if not key:
        print("\nRIME_API_KEY unset. cp .env.example .env, add your key.\n")
        return 1
    cfg = RimeConfig.from_env()

    print(f"\nSPEED CONTROL DIAGNOSTIC  ({cfg.model_id})")
    print("-" * 70)
    row("inlineSpeedAlpha supported", str(cfg.supports_inline_speed()))
    row("timeScaleFactor supported", str(cfg.supports_timescale()))
    print()

    cells: dict[str, float] = {}

    async def measure(label: str, text: str, extra: dict | None, note: str):
        try:
            ms = await render(cfg, key, text, extra)
        except Exception as exc:  # noqa: BLE001
            row(label, f"FAILED {type(exc).__name__}: {exc}")
            return
        cells[label] = ms
        row(label, f"{ms:7.0f} ms   {note}")

    await measure("A baseline", DOCS_TEXT, None, "plain words, no control")
    if cfg.supports_inline_speed():
        await measure("B1 plain words, alpha SLOW", DOCS_TEXT,
                      {"inlineSpeedAlpha": ALPHA_SLOW}, f"alpha={ALPHA_SLOW}")
        await measure("B2 plain words, alpha FAST", DOCS_TEXT,
                      {"inlineSpeedAlpha": ALPHA_FAST}, f"alpha={ALPHA_FAST}")

    # our real readback markup
    async with HttpSession() as sess:
        r = Renderer(RimeHttp(key, sess), cfg.supports_inline_speed())
        tx = Transaction(tx_id="diag")
        tx.mutate(lambda t: t.items.append(LineItem("4L80E", 3, 13753)))
        form = await r.precompute(tx)
    if cfg.supports_inline_speed() and form.speeds:
        await measure("C our markup + inlineSpeedAlpha", form.rendered,
                      {"inlineSpeedAlpha": form.speeds},
                      f"spans={form.rendered.count('[')}, alpha={form.speeds}")
        await measure("C0 our markup, no control", form.rendered, None, "baseline for C")

    if cfg.supports_timescale():
        await measure("D docs example + timeScaleFactor", DOCS_TEXT,
                      {"timeScaleFactor": "1.6"}, "1.6 = slower on this model")

    # ---- verdict ---------------------------------------------------------
    print("\n" + "-" * 70)
    a = cells.get("A baseline")
    b1, b2 = cells.get("B1 plain words, alpha SLOW"), cells.get("B2 plain words, alpha FAST")
    c, c0 = cells.get("C our markup + inlineSpeedAlpha"), cells.get("C0 our markup, no control")
    d = cells.get("D docs example + timeScaleFactor")

    # slow-vs-fast doubles the effect size versus comparing either to baseline
    param_works, param_pct = differs(b2, b1)
    markup_works, markup_pct = differs(c0, c)
    timescale_works, ts_pct = differs(a, d)

    row("plain-word span effect", f"{param_pct:5.1f}%  "
        f"({'REAL' if param_works else 'noise'})")
    row("our-markup span effect", f"{markup_pct:5.1f}%  "
        f"({'REAL' if markup_works else 'noise'})")
    row("whole-utterance effect", f"{ts_pct:5.1f}%  "
        f"({'REAL' if timescale_works else 'noise'})")
    print()

    # Markup is evaluated INDEPENDENTLY. An earlier version gated it behind the
    # plain-word cell, so a valid C result was discarded because a confounded
    # B cell failed.
    if markup_works:
        direction = "slower" if (c or 0) > (c0 or 0) else "faster"
        row("VERDICT", f"our markup DOES bind ({markup_pct:.1f}%, "
                       f">1.0 = {direction})")
        row("", "small but consistent; confirm by ear before claiming it")
    elif param_works:
        row("VERDICT", "parameter binds plain words but NOT our markup")
        row("CAUSE", "spell(...) and multi-word currency spans bind nothing")
    else:
        row("VERDICT", f"per-span control has no usable effect on {cfg.model_id}")

    if timescale_works:
        row("timeScaleFactor", f"WORKS decisively ({ts_pct:.0f}%) - use this for "
                               "whole-utterance rate")
    row("ACTION", "report only the cells above; do not generalise beyond them")

    print("-" * 70)
    print("  Durations are a proxy for rate. Confirm the winning cell by ear\n"
          "  before putting it in RIME_EVIDENCE.md.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
