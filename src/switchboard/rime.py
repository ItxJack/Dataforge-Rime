"""
Rime integration.

Three things this module does that a stock TTS plugin does not:

1. Exposes /ws3 word-level timestamps to the caller so the egress ledger can
   record which words were actually emitted.
2. Fences every chunk by contextId, so audio synthesised for a superseded epoch
   is dropped at the socket boundary rather than downstream.
3. Keeps at most ONE clause in generation at a time, bounding the
   un-cancellable synthesis tail (`clear` discards the buffer; it does not
   reliably abort in-flight server-side synthesis).

It also wraps the three non-TTS endpoints the safety path depends on:
/textnorm (normalised spoken form), /oov (dictionary coverage), /voices.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Callable, Optional

WS_URL = "wss://users-ws.rime.ai/ws3"
HTTP_BASE = "https://users.rime.ai"        # /oov, /voices
OPTIMIZE_BASE = "https://optimize.rime.ai"  # /textnorm, /phonemize

# Rime docs are internally inconsistent about phonemizeBetweenBrackets support.
# preflight.py resolves this empirically and records the finding; nothing in the
# runtime path depends on the doc claim.
INLINE_PHONEME_MODELS = {"mist", "mistv1", "mistv2"}
# Rime's speed table is explicit: inlineSpeedAlpha covers "Selected words" on
# Mist v2 AND Mist v3 (Coda does not support it). An earlier revision removed
# mistv3 after one failed measurement -- which then made supports_inline_speed()
# False, stripped the brackets, and disabled the very probe that would have
# investigated. Never narrow a capability on a single negative result when the
# experiment itself could be at fault.
INLINE_SPEED_MODELS = {"mist", "mistv1", "mistv2", "mistv3"}

# Whole-response speed. Direction and parameter differ BY MODEL -- do not infer
# one from another, the docs warn about exactly this:
#   Coda / Mist v3 : timeScaleFactor  >1.0 slower   (preferred)
#   Coda / Mist v3 : speedAlpha       >1.0 FASTER   (opposite direction)
#   Mist v2        : speedAlpha       >1.0 slower
# timeScaleFactor is documented to work over /ws, /ws2 and /ws3 as a query
# parameter, which makes it the reliable whole-utterance control here.
TIMESCALE_MODELS = {"coda", "arcana", "mistv3"}

# Synthesis arguments Rime accepts ONLY on the connection query string.
# Sending them in a text message is not an error -- it is silently ignored,
# which is far worse.
CONNECTION_ONLY_PARAMS = frozenset({
    "inlineSpeedAlpha", "speedAlpha", "timeScaleFactor", "reduceLatency",
    "phonemizeBetweenBrackets", "pauseBetweenBrackets",
})


class SocketState(Enum):
    """A quarantined socket may never be promoted back to LIVE."""

    LIVE = "live"
    QUARANTINED = "quarantined"
    DRAINED = "drained"
    CLOSED = "closed"


@dataclass(frozen=True)
class WordTiming:
    word: str
    start: float  # seconds, from the start of THIS synthesis unit
    end: float


@dataclass
class SynthUnit:
    """One clause. At most one of these is in generation at any time."""

    epoch: int
    seq: int
    text: str
    context_id: str
    words: list[WordTiming] = field(default_factory=list)
    audio: bytearray = field(default_factory=bytearray)
    done: bool = False
    dropped: bool = False

    @property
    def emitted_words(self) -> list[str]:
        return [w.word for w in self.words]


def make_context_id(epoch: int, seq: int) -> str:
    # Sent on EVERY text message. Rime does not maintain multiple simultaneous
    # context IDs and a set id persists across messages that omit one, so we
    # never rely on persistence.
    return f"e{epoch}.c{seq}"


def epoch_of(context_id: Optional[str]) -> Optional[int]:
    if not context_id or not context_id.startswith("e"):
        return None
    try:
        return int(context_id.split(".", 1)[0][1:])
    except (ValueError, IndexError):
        return None


def spell(identifier: str) -> str:
    """Rime's spell() forces letter-by-letter reading and chunks characters.

    Per Rime's prompting guide: use for confirmation codes, account numbers and
    SKUs; do NOT use for ordinary phone numbers or for real words that happen to
    be uppercase. Avoid dashes inside numeric IDs -- they cause awkward pauses.
    """
    cleaned = identifier.replace("-", " ").strip()
    return f"spell({cleaned})"


def inline_speed_alpha(segment_speeds: list[float]) -> str:
    """Comma-separated per-segment speed multipliers (Mist family only).

    This is the control with no equivalent elsewhere: slow ONLY the identifier
    span while the rest of the sentence keeps a natural pace, which is what a
    counter tech does by instinct.

    Mist convention: values BELOW 1.0 are faster, above 1.0 are slower.
    """
    return ",".join(f"{s:g}" for s in segment_speeds)


class RimeConfig:
    def __init__(
        self,
        speaker: str,
        model_id: str = "mistv3",
        lang: str = "eng",
        audio_format: str = "mulaw",
        sampling_rate: int = 8000,
        segment: str = "never",
    ):
        # modelId MUST be explicit. Without it /ws3 is served by the Mist v3
        # backend and speakers outside that catalog fail with "Speaker not
        # found" -- a preflight failure and therefore a disqualification path.
        if not model_id:
            raise ValueError("model_id must be set explicitly on /ws3")
        self.speaker = speaker
        self.model_id = model_id
        self.lang = lang
        self.audio_format = audio_format
        self.sampling_rate = sampling_rate
        self.segment = segment

    @classmethod
    def from_env(cls, env: dict | None = None) -> "RimeConfig":
        """Single source of truth for runtime configuration.

        Every parameter the .env.example advertises is read HERE and validated
        by preflight. A config surface that documents seven knobs and reads two
        is worse than one that documents two.
        """
        import os

        e = env if env is not None else os.environ
        cfg = cls(
            speaker=e.get("RIME_SPEAKER", "astra"),
            model_id=e.get("RIME_MODEL_ID", "mistv3"),
            lang=e.get("RIME_LANG", "eng"),
            audio_format=e.get("RIME_AUDIO_FORMAT", "mulaw"),
            sampling_rate=int(e.get("RIME_SAMPLING_RATE", "8000")),
            segment=e.get("RIME_SEGMENT", "never"),
        )
        cfg.validate()
        return cfg

    def validate(self) -> list[str]:
        """Fail loudly at boot rather than with a 400 mid-call."""
        problems = []
        if self.audio_format not in {"mulaw", "pcm", "mp3"}:
            problems.append(f"audioFormat '{self.audio_format}' not one of mulaw/pcm/mp3")
        if not (4000 <= self.sampling_rate <= 44100):
            problems.append(f"samplingRate {self.sampling_rate} outside 4000-44100")
        if self.audio_format == "mulaw" and self.sampling_rate != 8000:
            problems.append("G.711 mu-law is 8 kHz; other rates will not match the PSTN path")
        if self.segment not in {"never", "immediate", "bySentence"}:
            problems.append(f"segment '{self.segment}' not one of never/immediate/bySentence")
        if problems:
            raise ValueError("; ".join(problems))
        return problems

    def ws_url(self, base: str = WS_URL, extra: Optional[dict] = None) -> str:
        """/ws3 takes its configuration as QUERY PARAMETERS on the connection.

        Rime's docs are explicit: "all synthesis arguments are provided as
        query parameters when establishing the connection." An earlier version
        sent inlineSpeedAlpha in the per-message JSON body, where it was
        silently ignored -- two renders at alpha 1.60 and 0.60 came back byte
        -identical in duration, which is how the bug surfaced. Synthesis
        controls belong here, not in the text message.
        """
        from urllib.parse import urlencode

        params = self.query()
        if extra:
            params.update({k: str(v) for k, v in extra.items() if v not in (None, "")})
        return f"{base}?{urlencode(params)}"

    def query(self) -> dict[str, str]:
        return {
            "speaker": self.speaker,
            "modelId": self.model_id,
            "lang": self.lang,
            "audioFormat": self.audio_format,
            "samplingRate": str(self.sampling_rate),
            "segment": self.segment,
        }

    def supports_inline_speed(self) -> bool:
        return self.model_id in INLINE_SPEED_MODELS

    def supports_timescale(self) -> bool:
        return self.model_id in TIMESCALE_MODELS

    def supports_inline_phonemes(self) -> bool:
        return self.model_id in INLINE_PHONEME_MODELS

    def filter_controls(self, extra: dict[str, Any]) -> dict[str, Any]:
        """Strip controls this model does not accept.

        Sending inlineSpeedAlpha to a Coda voice is a 400 at runtime. The
        dynamic voice preflight can legitimately select a non-Mist model, so
        the guard has to live here rather than in a config comment.
        """
        out = dict(extra)
        if not self.supports_inline_speed():
            out.pop("inlineSpeedAlpha", None)
        if not self.supports_timescale():
            out.pop("timeScaleFactor", None)
        if not self.supports_inline_phonemes():
            out.pop("phonemizeBetweenBrackets", None)
        return out


class Transport:
    """Minimal duck-typed WebSocket. Swapped for a fake in tests."""

    async def send(self, message: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def recv(self) -> str:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover
        raise NotImplementedError


class RimeStream:
    """One /ws3 connection, epoch-fenced, one clause in generation.

    The caller drives it:
        await stream.speak(epoch, "Okay, that's three of them.")
        async for event in stream.events(): ...
        await stream.barge_in(new_epoch)
    """

    def __init__(self, transport: Transport, config: RimeConfig):
        self.t = transport
        self.cfg = config
        self.state = SocketState.LIVE
        self.epoch = 0
        self._seq = 0
        self._in_generation: Optional[SynthUnit] = None
        self.units: list[SynthUnit] = []
        self._idle = asyncio.Event()
        self._idle.set()
        self.dropped_chunks = 0  # fencing effectiveness; asserted 0-leak in tests

    # -- outbound ---------------------------------------------------------

    async def speak(
        self,
        epoch: int,
        text: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> SynthUnit:
        """Send one clause and flush it. Blocks until the previous clause's
        `done` arrives, which is what bounds the un-cancellable tail."""
        if self.state is not SocketState.LIVE:
            raise RuntimeError(f"cannot speak on a {self.state.value} socket")
        # One clause in generation. This awaits an EVENT set by the permanent
        # reader task -- never a busy-wait on a flag that only events() sets,
        # which deadlocks any caller that forgets to drain the stream.
        if self._in_generation is not None and not self._in_generation.done:
            await self._idle.wait()
        self.epoch = epoch
        self._seq += 1
        ctx = make_context_id(epoch, self._seq)
        unit = SynthUnit(epoch=epoch, seq=self._seq, text=text, context_id=ctx)
        self._in_generation = unit
        self.units.append(unit)

        self._idle.clear()
        msg: dict[str, Any] = {"text": text, "contextId": ctx}
        if extra:
            rejected = {k for k in extra if k in CONNECTION_ONLY_PARAMS}
            if rejected:
                raise ValueError(
                    f"{sorted(rejected)} are connection-level query parameters, "
                    "not per-message fields. Pass them to WebSocketTransport("
                    "extra_query=...) or they are silently ignored."
                )
            msg.update(self.cfg.filter_controls(extra))
        await self.t.send(json.dumps(msg))
        # segment=never: nothing is synthesised until we say so.
        # `flush` is documented as a bare operation; the contextId rides on the
        # TEXT message, which is what the resulting audio events are tagged
        # with. Inventing an extra field on the operation is not the contract.
        await self.t.send(json.dumps({"operation": "flush"}))
        return unit

    async def barge_in(self, new_epoch: int) -> None:
        """Discard the buffered text and advance the fence.

        NOTE: this does NOT stop audio reaching the caller. The egress
        controller mutes locally in <=1 RTP frame and never waits on this call.
        `clear` only discards Rime's accumulated text buffer.
        """
        assert new_epoch > self.epoch, "epoch must advance on barge-in"
        self.epoch = new_epoch
        await self.t.send(json.dumps({"operation": "clear"}))

    async def close(self) -> None:
        try:
            await self.t.send(json.dumps({"operation": "eos"}))
        finally:
            self.state = SocketState.CLOSED
            await self.t.close()

    # -- inbound ----------------------------------------------------------

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield fenced events. Chunks from a superseded epoch never escape."""
        while True:
            try:
                raw = await self.t.recv()
            except (StopAsyncIteration, ConnectionError):
                self.state = SocketState.CLOSED
                return
            if raw is None:
                self.state = SocketState.CLOSED
                return
            ev = json.loads(raw)
            ctx = ev.get("contextId")
            ev_epoch = epoch_of(ctx)
            unit = self._unit_for(ctx)

            # THE FENCE. Anything from a superseded epoch dies here.
            if ev_epoch is not None and ev_epoch < self.epoch:
                self.dropped_chunks += 1
                if unit:
                    unit.dropped = True
                if ev.get("type") == "done":
                    self._release(unit)
                continue

            kind = ev.get("type")
            if kind == "chunk":
                if unit is not None:
                    unit.audio.extend(base64.b64decode(ev["data"]))
                yield {"type": "audio", "unit": unit, "pcm": base64.b64decode(ev["data"])}
            elif kind == "timestamps":
                wt = ev["word_timestamps"]
                timings = [
                    WordTiming(w, s, e)
                    for w, s, e in zip(wt["words"], wt["start"], wt["end"])
                ]
                if unit is not None:
                    unit.words.extend(timings)
                yield {"type": "timestamps", "unit": unit, "words": timings}
            elif kind == "done":
                if unit is not None:
                    unit.done = True
                self._release(unit)
                yield {"type": "done", "unit": unit}
            elif kind == "error":
                yield {"type": "error", "error": ev}

    def _unit_for(self, ctx: Optional[str]) -> Optional[SynthUnit]:
        for u in reversed(self.units):
            if u.context_id == ctx:
                return u
        return None

    def _release(self, unit: Optional[SynthUnit]) -> None:
        if self._in_generation is not None and (
            unit is None or unit.context_id == self._in_generation.context_id
        ):
            self._in_generation.done = True
            self._in_generation = None
            self._idle.set()


# -- HTTP endpoints in the safety path ------------------------------------


class RimeHttp:
    """/textnorm, /oov and /voices.

    NOTE the two different hosts. /textnorm and /phonemize live on
    optimize.rime.ai and return {"normalized": ...} / {"phonemeString": ...};
    /oov and /voices live on users.rime.ai. An earlier version of this file had
    /textnorm on the wrong host parsing the wrong field, which would have failed
    the whole authorization path at runtime while every mock test passed.
    """

    def __init__(
        self,
        api_key: str,
        session: Any = None,
        base: str = HTTP_BASE,
        optimize_base: str = OPTIMIZE_BASE,
    ):
        self.api_key = api_key
        self.base = base
        self.optimize_base = optimize_base
        self.session = session
        self.calls: list[tuple[str, Any]] = []

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def textnorm(self, text: str) -> str:
        """Exact normalised form the TTS model receives before synthesis.

        Rime documents the output as identical regardless of which model you
        synthesise with, so the cache key needs the text only -- not the model.
        """
        url = f"{self.optimize_base}/textnorm"
        self.calls.append((url, text))
        r = await self.session.post(url, json={"text": text}, headers=self._headers())
        return r["normalized"]

    async def oov(self, text: str) -> list[str]:
        """Words not in Rime's pronunciation dictionary."""
        url = f"{self.base}/oov"
        self.calls.append((url, text))
        r = await self.session.post(url, json={"text": text}, headers=self._headers())
        return r if isinstance(r, list) else r.get("oov", [])

    async def voices(self) -> Any:
        """GET /data/voices/all-v2.json -- the canonical live catalog.

        Two corrections from an earlier version: there is no generic /voices
        path, and this endpoint is PUBLIC (no Authorization header). The shape
        is nested by model AND language:

            {"coda": {"eng": [...], "spa": [...]}, "mistv3": {"eng": [...]}}

        A flat speaker list is not enough to validate a config, because a
        speaker can exist on one model/language pair and not another.
        """
        return await self.session.get(f"{self.base}/data/voices/all-v2.json")

    async def speakers_for(self, model_id: str, lang: str) -> list[str]:
        catalog = await self.voices()
        return list((catalog.get(model_id) or {}).get(lang, []))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


class WebSocketTransport(Transport):
    """The real /ws3 connection.

    Kept deliberately thin: config goes in the query string, the API key goes
    in the Authorization header and never into source, logs or the URL. The
    fake transport in tests implements the same three methods, so the code path
    under test is the code path that ships.
    """

    def __init__(self, api_key: str, config: RimeConfig, base: str = WS_URL,
                 extra_query: Optional[dict] = None):
        self.api_key = api_key
        self.config = config
        self.base = base
        # Per-connection synthesis controls (inlineSpeedAlpha, speedAlpha...).
        # These CANNOT be changed per message; a different speed profile needs
        # a different connection.
        self.extra_query = config.filter_controls(extra_query or {})
        self._ws = None

    async def connect(self, attempts: int = 3):
        """Open the socket, retrying transient handshake failures.

        A 10 s open timeout with no retry was too tight: a single slow TLS
        handshake on a home connection aborted a run that had already
        completed every other step. Transient network failure is the expected
        case here, not the exceptional one.
        """
        import asyncio

        import websockets

        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                self._ws = await websockets.connect(
                    self.config.ws_url(self.base, self.extra_query),
                    additional_headers={"Authorization": f"Bearer {self.api_key}"},
                    open_timeout=30,
                    close_timeout=5,
                    ping_interval=20,
                    max_size=None,          # audio frames can be large
                )
                return self
            except Exception as exc:  # noqa: BLE001
                last = exc
                # An auth rejection will never succeed on retry.
                if "401" in str(exc) or "403" in str(exc):
                    raise
                if attempt < attempts:
                    await asyncio.sleep(1.5 * attempt)
        raise last  # type: ignore[misc]

    async def send(self, message: str) -> None:
        await self._ws.send(message)

    async def recv(self) -> Optional[str]:
        import websockets

        try:
            msg = await self._ws.recv()
        except websockets.exceptions.ConnectionClosed:
            # `eos` can close the socket without emitting `done`. Treat close
            # as terminal rather than waiting for a done that never arrives.
            return None
        return msg if isinstance(msg, str) else msg.decode()

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()


class HttpSession:
    """Real aiohttp session for /textnorm, /oov, /voices."""

    def __init__(self, session=None):
        self._s = session

    async def __aenter__(self):
        import aiohttp

        self._s = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc):
        await self._s.close()

    async def post(self, url: str, json: dict, headers: dict):  # noqa: A002
        async with self._s.post(url, json=json, headers=headers) as r:
            r.raise_for_status()
            return await r.json()

    async def get(self, url: str, headers: dict | None = None):
        async with self._s.get(url, headers=headers or {}) as r:
            r.raise_for_status()
            return await r.json()
