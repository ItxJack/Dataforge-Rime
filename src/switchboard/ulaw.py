"""Pure-python G.711 mu-law codec.

`audioop` was removed from the stdlib in Python 3.13. Judges reproduce on
whatever Python they have, so the mu-law path cannot depend on it. Implements
only the four calls egress.py needs, with the same signatures.
"""

from __future__ import annotations

import math
import struct

_BIAS = 0x84
_CLIP = 32635


def _lin2ulaw_sample(s: int) -> int:
    sign = 0x80 if s < 0 else 0
    if s < 0:
        s = -s
    s = min(s, _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (s & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (s >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _ulaw2lin_sample(u: int) -> int:
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = ((mantissa << 3) + _BIAS) << exponent
    sample -= _BIAS
    return -sample if sign else sample


def lin2ulaw(fragment: bytes, width: int) -> bytes:
    assert width == 2
    n = len(fragment) // 2
    samples = struct.unpack(f"<{n}h", fragment[: n * 2])
    return bytes(_lin2ulaw_sample(s) for s in samples)


def ulaw2lin(fragment: bytes, width: int) -> bytes:
    assert width == 2
    return struct.pack(
        f"<{len(fragment)}h", *[_ulaw2lin_sample(b) for b in fragment]
    )


def mul(fragment: bytes, width: int, factor: float) -> bytes:
    assert width == 2
    n = len(fragment) // 2
    samples = struct.unpack(f"<{n}h", fragment[: n * 2])
    out = [max(-32768, min(32767, int(s * factor))) for s in samples]
    return struct.pack(f"<{n}h", *out)


def rms(fragment: bytes, width: int) -> int:
    assert width == 2
    n = len(fragment) // 2
    if n == 0:
        return 0
    samples = struct.unpack(f"<{n}h", fragment[: n * 2])
    return int(math.sqrt(sum(s * s for s in samples) / n))
