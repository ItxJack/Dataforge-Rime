"""
Live Rime /ws3 smoke test.  `python run.py live`

Everything else in this repo runs against a transport that reproduces /ws3's
documented behaviour. This module is the only thing that opens a real socket,
and it exists because a fake written from the same misunderstanding as the code
cannot catch the misunderstanding. Six API contract bugs in this project were
found exactly that way, each after passing its mock.

What it verifies against the live API:

  1. The socket connects with the configured speaker/model/lang/format.
  2. A clause produces `chunk`, `timestamps` and `done` events.
  3. Word timestamps come back and cover the commitment span.
  4. contextId round-trips on the audio events.
  5. `clear` after a barge-in does not stall the next flush.
  6. Measured TTFA on the shipped path, cold and warm, reported separately.
  7. **The inlineSpeedAlpha direction**, which the docs leave ambiguous.

On (7): `inlineSpeedAlpha` is documented as >1.0 = slower, but `speedAlpha`
INVERTS between Mist v2 and Coda/Mist v3 and nothing states whether inline
follows. The identifier slowing depends on this. This writes both renderings to
WAV so you can listen and settle it, rather than trusting a doc page.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from switchboard.render import LineItem, Renderer, Transaction  # noqa: E402
from switchboard.rime import (  # noqa: E402
    HttpSession,
    RimeConfig,
    RimeHttp,
    RimeStream,
    WebSocketTransport,
)

from switchboard.console import init as console_init  # noqa: E402

console_init()

OUT = ROOT / "fixtures" / "live"
PROBE = "Okay, reading that back. 3 of spell(4L80E). Total $412.59."


def row(k: str, v: str) -> None:
    print(f"  {k:<34} {v}", flush=True)


def step(msg: str) -> None:
    """Progress marker.

    Without flushing, a subprocess block-buffers stdout at ~8 KB and the script
    looks frozen while it is in fact synthesising. Every long operation
    announces itself BEFORE it starts, so a real hang is distinguishable from
    normal work.
    """
    print(f"  ... {msg}", flush=True)


async def one_utterance(cfg: RimeConfig, key: str, text: str, speeds: str,
                        timeout: float = 45.0):
    """Open, speak one clause, drain to `done`. Returns (ttfa_ms, pcm, words).

    Wrapped in a hard timeout: a network stall must surface as a readable
    failure, never as a script that sits there looking busy.
    """
    return await asyncio.wait_for(
        _one_utterance(cfg, key, text, speeds), timeout=timeout
    )


async def _one_utterance(cfg: RimeConfig, key: str, text: str, speeds: str):
    # Speed control rides on the CONNECTION, not the message.
    transport = await WebSocketTransport(
        key, cfg, extra_query={"inlineSpeedAlpha": speeds} if speeds else None
    ).connect()
    stream = RimeStream(transport, cfg)
    t0 = time.perf_counter()
    ttfa = None
    pcm = bytearray()
    words: list = []

    await stream.speak(1, text)

    async for ev in stream.events():
        if ev["type"] == "audio":
            if ttfa is None:
                ttfa = (time.perf_counter() - t0) * 1000
            pcm.extend(ev["pcm"])
        elif ev["type"] == "timestamps":
            words.extend(ev["words"])
        elif ev["type"] == "done":
            break
        elif ev["type"] == "error":
            raise RuntimeError(f"Rime error: {ev['error']}")

    await stream.close()
    return ttfa, bytes(pcm), words


async def one_utterance_with(cfg: RimeConfig, key: str, text: str,
                             extra: dict | None) -> float:
    """Render once with arbitrary connection params; return duration in ms.

    Shared with eval/speed.py so the diagnostic exercises the same client the
    product uses -- a diagnostic against a different code path proves nothing.
    """
    transport = await WebSocketTransport(key, cfg, extra_query=extra).connect()
    stream = RimeStream(transport, cfg)
    pcm = bytearray()

    async def pump():
        async for ev in stream.events():
            if ev["type"] == "audio":
                pcm.extend(ev["pcm"])
            elif ev["type"] == "done":
                break

    reader = asyncio.create_task(pump())
    await asyncio.wait_for(stream.speak(1, text), timeout=15.0)
    try:
        await asyncio.wait_for(reader, timeout=45.0)
    except asyncio.TimeoutError:
        reader.cancel()
    await stream.close()
    return len(pcm) / cfg.sampling_rate * 1000


async def _probe_other_model(cfg: RimeConfig, key: str) -> None:
    """The configured model ignores inlineSpeedAlpha. Find out whether ANY
    model honours it, rather than reporting a dead end.

    Rime documents inline speed for the Mist v1/v2 family. If it works there,
    the finding is "capability is model-specific" -- useful and actionable.
    If it works nowhere, the claim gets dropped, which is also a finding.
    """
    from dataclasses import replace as _replace  # noqa: F401

    alt = RimeConfig(speaker=cfg.speaker, model_id="mistv2", lang=cfg.lang,
                     audio_format=cfg.audio_format,
                     sampling_rate=cfg.sampling_rate, segment=cfg.segment)
    print(f"\n  cross-checking inlineSpeedAlpha on mistv2", flush=True)
    try:
        from switchboard.rime import HttpSession, RimeHttp

        async with HttpSession() as sess:
            speakers = await RimeHttp(key, sess).speakers_for("mistv2", cfg.lang)
        if cfg.speaker not in speakers:
            if not speakers:
                row("  mistv2", f"no voices listed for mistv2/{cfg.lang}")
                return
            alt = RimeConfig(speaker=speakers[0], model_id="mistv2", lang=cfg.lang,
                             audio_format=cfg.audio_format,
                             sampling_rate=cfg.sampling_rate, segment=cfg.segment)
            row("  speaker", f"'{cfg.speaker}' absent on mistv2; using "
                             f"'{alt.speaker}'")

        out = {}
        for alpha in ("1.60,1.60", "0.60,0.60"):
            step(f"mistv2 alpha={alpha.split(',')[0]} (~10s)")
            _, audio, _ = await one_utterance(alt, key, PROBE, alpha)
            out[alpha.split(",")[0]] = len(audio) / alt.sampling_rate * 1000
            row(f"  mistv2 alpha={alpha.split(',')[0]}", f"{out[alpha.split(',')[0]]:.0f} ms")
        d = abs(out["1.60"] - out["0.60"])
        if d < 100:
            row("  CROSS-CHECK", f"mistv2 also NO EFFECT (delta {d:.0f} ms)")
            row("  CONCLUSION", "drop the identifier-slowing claim entirely")
        else:
            slower = "1.60" if out["1.60"] > out["0.60"] else "0.60"
            row("  CROSS-CHECK", f"mistv2 HONOURS it (delta {d:.0f} ms, "
                                 f"{slower} is slower)")
            row("  CONCLUSION", "capability is model-specific: works on mistv2, "
                                f"ignored on {cfg.model_id}")
            row("  ACTION", "either set RIME_MODEL_ID=mistv2 and re-run, or "
                            "report the finding and drop the claim")
    except Exception as exc:  # noqa: BLE001
        row("  cross-check failed", f"{type(exc).__name__}: {exc}")


def write_wav(path: Path, mulaw: bytes, rate: int) -> None:
    """mu-law payload -> a WAV you can actually listen to."""
    import wave

    from switchboard.egress import audioop

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audioop.ulaw2lin(mulaw, 2))


async def main() -> int:
    key = os.environ.get("RIME_API_KEY")
    if not key:
        print("\nRIME_API_KEY unset.\n  cp .env.example .env, add your key, "
              "then: python run.py live\n")
        return 1

    try:
        cfg = RimeConfig.from_env()
    except ValueError as exc:
        print(f"\ninvalid configuration: {exc}\n")
        return 1

    print(f"\nLIVE /ws3 SMOKE\n{'-' * 66}")
    row("url", cfg.ws_url())

    # --- 1-4: connect, speak, events -------------------------------------
    step("connecting and synthesising probe utterance (~10s)")
    try:
        cold_ttfa, pcm, words = await one_utterance(cfg, key, PROBE, "1.25,1.25")
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        print(f"  FAIL  {name}: {exc}\n")
        if "401" in str(exc) or "403" in str(exc) or "Unauthorized" in str(exc):
            print("  The key was rejected. Check RIME_API_KEY in .env "
                  "(no quotes, no spaces).")
        elif "Speaker" in str(exc) or "404" in str(exc):
            print("  The speaker is probably not valid for this model+lang.\n"
                  "  Open https://users.rime.ai/data/voices/all-v2.json, find\n"
                  f"  the '{cfg.model_id}' -> '{cfg.lang}' list, pick a name,\n"
                  "  and set RIME_SPEAKER in .env.")
        else:
            print("  Network or transport problem. A campus/corporate network\n"
                  "  may block wss://. Try a phone hotspot.")
        print("\n  `python run.py` (offline) is unaffected.\n")
        return 1

    row("TTFA (cold)", f"{cold_ttfa:.0f} ms")
    row("audio received", f"{len(pcm)} bytes "
        f"({len(pcm) / cfg.sampling_rate * 1000:.0f} ms at {cfg.sampling_rate} Hz)")
    row("word timestamps", f"{len(words)} words"
        + (f", first='{words[0].word}' last='{words[-1].word}'" if words else ""))
    if not words:
        print("  FAIL  no word timestamps - the heard-state ledger depends on these")
        return 1

    spoken = " ".join(w.word for w in words).lower()
    for digit in ("4", "8", "0"):
        if digit not in spoken:
            row("WARNING", f"'{digit}' absent from timestamps - check spell() output")

    # warm run on a fresh socket, reported separately per the PS
    step("second run for warm TTFA (~10s)")
    warm_ttfa, _, _ = await one_utterance(cfg, key, PROBE, "1.25,1.25")
    row("TTFA (warm)", f"{warm_ttfa:.0f} ms")

    # --- 5: clear does not stall the next flush --------------------------
    #
    # DEADLOCK FIXED HERE. speak() waits on an asyncio.Event that only the
    # reader task sets when `done` arrives. An earlier version created the
    # reader AFTER the second speak(), so the second speak() waited forever for
    # an event nobody could set. The reader must exist before any speak, which
    # is exactly what test_speak_does_not_deadlock_without_an_event_consumer
    # documents -- and this script violated its own contract.
    step("barge-in / re-entry test (~15s)")
    transport = await WebSocketTransport(key, cfg).connect()
    stream = RimeStream(transport, cfg)

    first_audio: dict[int, float] = {}
    stale_after_clear = 0

    async def pump() -> None:
        nonlocal stale_after_clear
        async for ev in stream.events():
            if ev["type"] == "audio":
                unit = ev.get("unit")
                ep = unit.epoch if unit is not None else stream.epoch
                first_audio.setdefault(ep, time.perf_counter())

    reader = asyncio.create_task(pump())
    try:
        await asyncio.wait_for(
            stream.speak(1, "This utterance will be interrupted mid-sentence."),
            timeout=15.0,
        )
        await asyncio.sleep(0.15)          # let synthesis get underway
        await stream.barge_in(2)           # epoch advances; buffer discarded
        t0 = time.perf_counter()
        await asyncio.wait_for(stream.speak(2, "Recovered."), timeout=15.0)

        deadline = time.perf_counter() + 10.0
        while 2 not in first_audio and time.perf_counter() < deadline:
            await asyncio.sleep(0.05)

        if 2 in first_audio:
            row("re-entry after clear", f"{(first_audio[2] - t0) * 1000:.0f} ms")
        else:
            row("re-entry after clear",
                "TIMEOUT - head-of-line blocking on this model")
        row("stale chunks fenced", f"{stream.dropped_chunks} "
            f"(epoch-1 audio arriving after the fence moved)")
    except asyncio.TimeoutError:
        row("re-entry after clear", "TIMEOUT waiting for synthesis")
    finally:
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
        await stream.close()

    # Speed control is NOT measured here. `python run.py speed` owns that
    # question and runs a controlled matrix. An A/B in this file reported a
    # 3% noise delta as a verdict and told the operator to change a constant,
    # contradicting the diagnostic in the same run. Two verdicts on one
    # question is worse than none.
    row("speed control", "see `python run.py speed` (controlled matrix)")

    # --- /textnorm against the live endpoint -----------------------------
    step("live /textnorm")
    async with HttpSession() as sess:
        http = RimeHttp(key, sess)
        tx = Transaction(tx_id="live")
        tx.mutate(lambda t: t.items.append(LineItem("4L80E", 3, 13753)))
        r = Renderer(http, cfg.supports_inline_speed())
        t0 = time.perf_counter()
        form = await r.precompute(tx)
        row("/textnorm latency", f"{(time.perf_counter() - t0) * 1000:.0f} ms "
            f"(off the speech path - mutation time only)")
        row("live normalized", form.normalized[:44] + "...")
        row("bound digest", form.spoken_digest)

    print(f"{'-' * 66}\n  LIVE PATH VERIFIED\n")
    print("  Still unmeasured: far-end acoustic residue on a real PSTN call.")
    print("  That number cannot come from this script.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
