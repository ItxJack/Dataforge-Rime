"""Fakes. The container has no network access to rime.ai, so the runtime path
is written against the real API and exercised here against a transport that
reproduces /ws3's documented behaviour -- including the awkward parts."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Optional


class FakeTransport:
    """Reproduces the /ws3 behaviours that actually bite:

    - a chunk carries the contextId that was active when audio was REQUESTED
    - `clear` discards the buffered text but does NOT abort in-flight synthesis
    - `eos` may close without emitting `done`
    """

    def __init__(self, ms_per_word: float = 300.0):
        self.sent: list[dict] = []
        self.out: asyncio.Queue = asyncio.Queue()
        self.ms_per_word = ms_per_word
        self.closed = False
        self.tasks: list = []
        self._pending: Optional[dict] = None

    async def send(self, message: str) -> None:
        msg = json.loads(message)
        self.sent.append(msg)
        op = msg.get("operation")
        if op is None:
            self._pending = msg
        elif op == "flush":
            assert "contextId" not in msg, "flush is a bare operation"
            if self._pending:
                # Synthesis runs as an INDEPENDENT task, as the real server
                # does. A previous fake awaited it inline, which meant `clear`
                # could never arrive mid-synthesis -- so the fence test was
                # validating a model of the race rather than the race.
                self.tasks.append(asyncio.create_task(self._synthesize(self._pending)))
                self._pending = None
        elif op == "clear":
            # Discards the accumulated TEXT buffer. In-flight synthesis keeps
            # emitting; that is precisely what the epoch fence has to catch.
            self._pending = None
        elif op == "eos":
            self.closed = True
            await self.out.put(None)

    async def _synthesize(self, msg: dict) -> None:
        ctx = msg["contextId"]
        words = [w for w in msg["text"].replace(",", " ").split() if w]
        t = 0.0
        starts, ends = [], []
        for _ in words:
            starts.append(t / 1000.0)
            t += self.ms_per_word
            ends.append(t / 1000.0)
        for _ in words:
            await asyncio.sleep(0)  # yield: audio arrives over time
            pcm = b"\xff" * 160
            await self.out.put(
                json.dumps(
                    {
                        "type": "chunk",
                        "contextId": ctx,
                        "data": base64.b64encode(pcm).decode(),
                    }
                )
            )
        await self.out.put(
            json.dumps(
                {
                    "type": "timestamps",
                    "contextId": ctx,
                    "word_timestamps": {"words": words, "start": starts, "end": ends},
                }
            )
        )
        await self.out.put(json.dumps({"type": "done", "contextId": ctx}))

    async def inject_stale(self, context_id: str, n: int = 1) -> None:
        """Audio for a superseded epoch arriving after the fence moved."""
        for _ in range(n):
            await self.out.put(
                json.dumps(
                    {
                        "type": "chunk",
                        "contextId": context_id,
                        "data": base64.b64encode(b"\x7f" * 160).decode(),
                    }
                )
            )

    async def recv(self):
        return await self.out.get()

    async def close(self) -> None:
        self.closed = True


class FakeSession:
    """Stands in for /textnorm, /oov, /voices."""

    def __init__(self, fail_textnorm: bool = False):
        self.fail_textnorm = fail_textnorm
        self.textnorm_calls = 0

    async def post(self, url: str, json: dict, headers: dict) -> Any:  # noqa: A002
        if url.endswith("/textnorm"):
            self.textnorm_calls += 1
            if self.fail_textnorm:
                raise ConnectionError("textnorm unavailable")
            # Real contract: optimize.rime.ai/textnorm -> {"normalized": ...}
            assert url.startswith("https://optimize.rime.ai"), (
                f"/textnorm must go to optimize.rime.ai, got {url}"
            )
            return {"normalized": _normalize(json["text"])}
        if url.endswith("/oov"):
            return [w for w in json["text"].split() if w.lower() == "zzqx"]
        raise ValueError(url)

    async def get(self, url: str, headers: dict | None = None) -> Any:
        assert url.endswith("/data/voices/all-v2.json"), f"wrong voices path: {url}"
        assert not headers, "the voices catalog is public; sending auth is wrong"
        # Real shape: keyed by modelId, then language.
        return {
            "coda": {"eng": ["astra", "celeste"], "spa": ["aurelio"]},
            "mistv3": {"eng": ["astra", "alexis"], "spa": ["diego"]},
            "mistv2": {"eng": ["abbie", "allison"]},
        }


def _normalize(text: str) -> str:
    """Crude stand-in for Rime's normaliser. The point of the test is that the
    normalised form DIFFERS from the rendered form -- which is exactly why
    binding to the payload digest was wrong."""
    import re

    def spell_out(m):
        inner = m.group(1)
        return " ".join(c for c in inner if c != " ")

    text = re.sub(r"spell\(([^)]*)\)", spell_out, text)
    text = re.sub(
        r"\$(\d+)\.(\d{2})",
        lambda m: f"{m.group(1)} dollars and {int(m.group(2))} cents",
        text,
    )
    return " ".join(text.split())
