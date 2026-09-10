#!/usr/bin/env python3
"""Switchboard -- single entry point.

    python run.py            everything, in order, with a PASS/FAIL report
    python run.py test       the acceptance suite
    python run.py chaos      adversarial races only
    python run.py preflight  blocking eligibility checks
    python run.py demo       the 90-second stress case
    python run.py live       the same, against the real Rime API

No `make` required -- judges may be on Windows. Works from a clean checkout
with no install and no PYTHONPATH; the only pip dependency is pytest, and only
for `test`/`chaos`. `live` additionally needs websockets and aiohttp.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _p in (ROOT, ROOT / "src", ROOT / "tests", ROOT / "eval"):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from switchboard.console import child_env, colours, init as console_init  # noqa: E402

_GL = console_init()
_C = colours()
B, G, R, Y, X = _C["B"], _C["G"], _C["R"], _C["Y"], _C["X"]
LIGHT = _GL["light"]
W = 78


def _run(args: list[str]) -> tuple[int, str]:
    """Capture child output as UTF-8 explicitly.

    Without `encoding=`, Python decodes child output with the locale codec,
    which on Windows is cp1252 and mangles or raises on anything outside it.
    errors="replace" means a stray byte degrades one character instead of
    killing the run.
    """
    p = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=child_env(),
    )
    return p.returncode, p.stdout + p.stderr


def check_env() -> tuple[bool, list[str]]:
    """Readable diagnostics instead of a raw ModuleNotFoundError."""
    notes, ok = [], True
    v = sys.version_info
    notes.append(f"Python {v.major}.{v.minor}.{v.micro}")
    if v < (3, 9):
        notes.append("  needs Python 3.9+")
        ok = False

    try:
        import pytest  # noqa: F401

        notes.append("pytest available")
    except ImportError:
        notes.append("pytest MISSING  ->  pip install -r requirements.txt")
        ok = False

    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import audioop  # noqa: F401
        notes.append("audio: stdlib audioop")
    except ImportError:
        notes.append("audio: bundled pure-python G.711 (audioop gone in 3.13+)")

    for name, why in (("websockets", "live /ws3"), ("aiohttp", "live HTTP")):
        try:
            __import__(name)
            notes.append(f"{name} available ({why})")
        except ImportError:
            notes.append(f"{name} absent - only needed for `run.py live`")

    if (ROOT / ".env").exists():
        notes.append("configuration: .env present")
    else:
        notes.append("configuration: using defaults (copy .env.example to .env for live)")
    return ok, notes


STAGES = [
    ("Environment", None),
    ("Acceptance suite", [sys.executable, "-u", "-m", "pytest", "tests/", "-q"]),
    ("Adversarial races", [sys.executable, "-u", "-m", "pytest", "tests/", "-q", "-k",
                           "confirm or cancel or try or expiry or external or stale "
                           "or overlap or escrow or regressed or reconcile"]),
    ("Rime preflight", [sys.executable, "-u", "-m", "eval.preflight"]),
    ("Stress demonstration", [sys.executable, "-u", "-m", "eval.demo"]),
    ("Judge report", [sys.executable, "-u", "-m", "eval.judge"]),
]


def full() -> int:
    _load_dotenv()
    print(f"\n{B}SWITCHBOARD{X}  phone-native parts desk | Rime x DataForge")
    print(f"{LIGHT * W}")
    failures = []

    for i, (name, cmd) in enumerate(STAGES, 1):
        label = f"[{i}/{len(STAGES)}] {name}"
        if cmd is None:
            ok, notes = check_env()
            print(f"\n{B}{label}{X}")
            for n in notes:
                print(f"        {n}")
            if not ok:
                print(f"        {R}FAIL - fix the above, then re-run{X}\n")
                return 1
            print(f"        {G}PASS{X}")
            continue

        print(f"\n{B}{label}{X}", flush=True)
        rc, out = _run(cmd)
        if name == "Judge report":
            print(out)
        else:
            for l in out.splitlines():
                if any(k in l for k in ("passed", "failed", "CLEAR", "BLOCKED",
                                        "PASS", "FAIL", "STALE FRAMES")):
                    print(f"        {l.strip()}")
        if rc != 0:
            failures.append(name)
            print(f"        {R}FAIL{X}")
        else:
            print(f"        {G}PASS{X}")

    print(f"\n{LIGHT * W}")
    if failures:
        print(f"  {R}FAILED:{X} {', '.join(failures)}\n")
        return 1
    print(f"  {G}ALL STAGES PASS{X}   |   scope is stated in the judge report\n"
          f"  above and in RIME_EVIDENCE.md.\n")
    return 0


def live_sequence() -> int:
    """The real-API path, in dependency order. Each step gates the next: a bad
    speaker makes the socket test meaningless, and a dead socket makes the
    judge report meaningless."""
    for missing, why in (("websockets", "the /ws3 socket"), ("aiohttp", "HTTP endpoints")):
        try:
            __import__(missing)
        except ImportError:
            print(f"{R}{missing} is required for {why}{X}\n  "
                  f"pip install -r requirements.txt")
            return 1
    _load_dotenv()
    if not os.environ.get("RIME_API_KEY"):
        print(f"{R}RIME_API_KEY unset.{X}  cp .env.example .env and add your key.")
        return 1

    steps = [
        ("Live catalog + endpoints", [sys.executable, "-u", "-m", "eval.preflight", "--live"]),
        ("Live /ws3 socket", [sys.executable, "-u", "-m", "eval.live"]),
        ("Speed control diagnostic", [sys.executable, "-u", "-m", "eval.speed"]),
        ("Judge report (live)", [sys.executable, "-u", "-m", "eval.judge", "--live"]),
    ]
    for i, (name, cmd) in enumerate(steps, 1):
        print(f"\n{B}[{i}/{len(steps)}] {name}{X}", flush=True)
        rc = subprocess.run(cmd, cwd=ROOT, env=child_env()).returncode
        if rc != 0:
            print(f"{R}  stopped at: {name}{X}")
            return rc
    return 0


def _strip_inline_comment(value: str) -> str:
    """Remove a trailing ` # comment` from a .env value.

    The comment marker must be preceded by whitespace, which is the usual
    dotenv rule and the reason an API key containing '#' survives intact.
    Without this, `RIME_SAMPLING_RATE=8000   # mulaw is 8 kHz` parsed as the
    literal string "8000   # mulaw is 8 kHz" and int() blew up at boot -- on
    the very file this project ships as the template.
    """
    out = value
    for marker in (" #", "\t#"):
        idx = out.find(marker)
        if idx != -1:
            out = out[:idx]
    return out.strip()


def _load_dotenv() -> None:
    """Minimal .env reader so `python run.py live` works without python-dotenv."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = _strip_inline_comment(v)
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]          # tolerate quoted values
        if v:                     # never overwrite a real value with an empty one
            os.environ.setdefault(k.strip(), v)


COMMANDS = {
    "test": [sys.executable, "-u", "-m", "pytest", "tests/", "-q"],
    "chaos": STAGES[2][1],
    "preflight": [sys.executable, "-u", "-m", "eval.preflight"],
    "demo": [sys.executable, "-u", "-m", "eval.demo"],
    "judge": [sys.executable, "-u", "-m", "eval.judge"],
    "speed": [sys.executable, "-u", "-m", "eval.speed"],
    "acoustic": [sys.executable, "-u", "-m", "eval.acoustic"],
    "live": None,  # multi-step; see live_sequence()
}

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    _load_dotenv()
    if arg is None:
        raise SystemExit(full())
    if arg in ("-h", "--help", "help"):
        print(__doc__)
        raise SystemExit(0)
    if arg == "live":
        raise SystemExit(live_sequence())
    if arg not in COMMANDS:
        print(f"unknown command '{arg}'\n")
        print(__doc__)
        raise SystemExit(2)
    ok, notes = check_env()
    if not ok and arg in ("test", "chaos"):
        print("\n".join(notes))
        raise SystemExit(1)
    raise SystemExit(
        subprocess.run(COMMANDS[arg] + sys.argv[2:], cwd=ROOT, env=child_env()).returncode
    )
