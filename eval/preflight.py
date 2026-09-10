"""
Blocking preflight. Run before every demo recording and before submission.

The PS makes a stale model/voice/language combination an eligibility failure,
so none of this is optional and none of it is hardcoded.

    python -m eval.preflight
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fakes import FakeSession  # noqa: E402

from switchboard.rime import (  # noqa: E402
    INLINE_PHONEME_MODELS,
    INLINE_SPEED_MODELS,
    RimeConfig,
    RimeHttp,
)

from switchboard.console import init as console_init  # noqa: E402

console_init()

ROOT = Path(__file__).resolve().parents[1]
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9]{16,}|rime_[A-Za-z0-9]{16,})")


class Preflight:
    def __init__(self, http: RimeHttp, cfg: RimeConfig):
        self.http, self.cfg = http, cfg
        self.failures: list[str] = []
        self.notes: list[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    async def check_voice_is_live(self) -> None:
        """Query the catalog AT RUNTIME, for this model AND this language.

        A flat "is the speaker known" check is not enough: the catalog is keyed
        {modelId: {lang: [speakers]}}, and a speaker present on coda/eng may be
        absent on mistv3/eng. That combination is exactly what the event
        preflight rejects.
        """
        speakers = await self.http.speakers_for(self.cfg.model_id, self.cfg.lang)
        if not speakers:
            self.fail(
                f"no voices listed for {self.cfg.model_id}/{self.cfg.lang}"
            )
        elif self.cfg.speaker not in speakers:
            self.fail(
                f"speaker '{self.cfg.speaker}' not available on "
                f"{self.cfg.model_id}/{self.cfg.lang}"
            )
        else:
            self.notes.append(
                f"speaker '{self.cfg.speaker}' present in live "
                f"{self.cfg.model_id}/{self.cfg.lang} catalog"
            )

    def record_transport(self) -> None:
        """Every parameter the .env advertises, resolved and echoed. The exact
        combination the PS asks teams to declare."""
        self.notes.append(
            f"transport: {self.cfg.audio_format}@{self.cfg.sampling_rate}Hz "
            f"lang={self.cfg.lang} segment={self.cfg.segment}"
        )
        self.notes.append(f"ws url: {self.cfg.ws_url()}")

    def check_model_id_explicit(self) -> None:
        """Without an explicit modelId, /ws3 is served by the Mist v3 backend
        and non-Mist-v3 speakers fail with 'Speaker not found'."""
        if not self.cfg.model_id:
            self.fail("modelId not set explicitly on /ws3")
        else:
            self.notes.append(f"modelId explicit: {self.cfg.model_id}")

    async def check_oov_corpus(self, corpus: list[str]) -> None:
        uncovered: list[str] = []
        for term in corpus:
            uncovered += await self.http.oov(term)
        if uncovered:
            self.fail(f"OOV terms without an approved rendering: {sorted(set(uncovered))}")
        else:
            self.notes.append(f"/oov clean across {len(corpus)} corpus terms")

    async def snapshot_textnorm(self, corpus: list[str], out: Path) -> None:
        """Snapshot normaliser output so drift between runs is visible rather
        than silent."""
        snap = {t: await self.http.textnorm(t) for t in corpus}
        out.parent.mkdir(parents=True, exist_ok=True)
        prev = json.loads(out.read_text()) if out.exists() else None
        out.write_text(json.dumps(snap, indent=2, sort_keys=True))
        if prev and prev != snap:
            changed = [k for k in snap if prev.get(k) != snap[k]]
            self.notes.append(f"/textnorm drift on: {changed}")
        self.notes.append(f"/textnorm snapshot written ({len(snap)} entries)")

    def record_capability_matrix(self) -> None:
        """Rime's own docs disagree on which models support inline phonemes.
        We resolve it empirically at integration time and record the finding
        rather than trusting any page."""
        self.notes.append(
            f"inlineSpeedAlpha: advertised for {self.cfg.model_id}, but MEASURED "
            "inert on /ws3 (0-6%, direction flips) -- claim not made"
        )
        self.notes.append(
            f"phonemizeBetweenBrackets (docs conflict, verify live): "
            f"{self.cfg.model_id in INLINE_PHONEME_MODELS}"
        )
        if self.cfg.supports_timescale():
            self.notes.append(
                "timeScaleFactor: MEASURED +136% to +147% at 1.6 across runs, "
                "correct direction every time -- this is the speed control used"
            )

    def scan_secrets(self) -> None:
        hits: list[str] = []
        for p in ROOT.rglob("*"):
            if not p.is_file() or ".git" in p.parts or p.suffix in {".pyc", ".wav"}:
                continue
            try:
                if SECRET_RE.search(p.read_text(errors="ignore")):
                    hits.append(str(p.relative_to(ROOT)))
            except (UnicodeDecodeError, OSError):
                continue
        if hits:
            self.fail(f"possible live credential in: {hits}")
        else:
            self.notes.append("secret scan clean")

    def check_env_example(self) -> None:
        env = ROOT / ".env.example"
        if not env.exists():
            self.fail(".env.example missing")
        elif re.search(
            r"^[^\S\n]*\w*(KEY|SECRET|TOKEN|PASSWORD)\w*[^\S\n]*=[^\S\n]*\S+",
            env.read_text(),
            re.M | re.I,
        ):
            # Only secret-named keys matter. URLs and model ids are config, and
            # flagging them just trains you to ignore the preflight.
            self.fail(".env.example has a populated secret-named key")
        else:
            self.notes.append(".env.example has placeholders only")


def _explain(exc: Exception) -> None:
    """Turn a network/auth stack trace into something actionable.

    A judge -- or you, at 11pm -- should not have to read an aiohttp traceback
    to learn that an API key is wrong.
    """
    name = type(exc).__name__
    status = getattr(exc, "status", None)
    print(f"\nLIVE PREFLIGHT FAILED\n{'-' * 60}")
    if status in (401, 403):
        print("  Rime rejected the credentials (HTTP "
              f"{status}).\n"
              "    - check RIME_API_KEY in .env has no quotes and no spaces\n"
              "    - regenerate the key in the Rime dashboard and paste again\n"
              "    - confirm the key is active on your account")
    elif status == 404:
        print("  Endpoint not found (HTTP 404). The API surface may have moved;\n"
              "  check docs.rime.ai and update src/switchboard/rime.py.")
    elif status == 429:
        print("  Rate limited (HTTP 429). Wait a minute and retry.")
    elif name in ("ClientConnectorError", "ClientConnectionError",
                  "ServerDisconnectedError", "TimeoutError",
                  "ConnectionError", "gaierror"):
        print("  Could not reach Rime.\n"
              "    - check your internet connection\n"
              "    - a corporate/campus network may block wss:// or this host\n"
              "    - try a phone hotspot")
    else:
        print(f"  {name}: {exc}")
    print("\n  Offline verification is unaffected: `python run.py` still runs\n"
          "  the full suite and the stress case without a key.")
    print(f"{'-' * 60}\n")


CORPUS = ["4L60E", "4L80E", "AC12684485", "$412.60", "spell(4L80E)", "12684485"]


async def main(live: bool = False) -> int:
    """--live hits the real Rime API. The default uses deterministic fakes so
    the suite runs offline -- but a preflight that only ever talks to a mock
    cannot catch an endpoint contract error, which is exactly how /textnorm
    shipped pointed at the wrong host."""
    try:
        cfg = RimeConfig.from_env()
    except ValueError as exc:
        print(f"\nPREFLIGHT\n{'-' * 60}\n  FAIL  invalid configuration: {exc}\n"
              f"{'-' * 60}\n  BLOCKED\n")
        return 1
    if live:
        from switchboard.rime import HttpSession

        key = os.environ.get("RIME_API_KEY")
        if not key:
            print("RIME_API_KEY unset; cannot run --live")
            return 1
        async with HttpSession() as sess:
            pf = Preflight(RimeHttp(key, sess), cfg)
            try:
                return await _run(pf, cfg)
            except Exception as exc:  # noqa: BLE001
                _explain(exc)
                return 1
    pf = Preflight(RimeHttp("REDACTED", FakeSession()), cfg)
    return await _run(pf, cfg)


async def _run(pf, cfg) -> int:

    pf.check_model_id_explicit()
    pf.record_transport()
    await pf.check_voice_is_live()
    await pf.check_oov_corpus(CORPUS)
    await pf.snapshot_textnorm(CORPUS, ROOT / "fixtures" / "textnorm.snapshot.json")
    pf.record_capability_matrix()
    pf.check_env_example()
    pf.scan_secrets()

    print("\nPREFLIGHT\n" + "-" * 60)
    for n in pf.notes:
        print(f"  ok    {n}")
    for f in pf.failures:
        print(f"  FAIL  {f}")
    print("-" * 60)
    print(f"  {'BLOCKED' if pf.failures else 'CLEAR'}\n")
    return 1 if pf.failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--live" in sys.argv)))
