# Switchboard

A phone-native order desk for an auto-parts distributor. The caller is a
mechanic under a lift with dirty hands, on an 8 kHz μ-law line, with an impact
wrench running. There is no screen. Remove speech and there is no product.

**The hard voice problem:** interruption and recovery. When the caller corrects
a part number *while the agent is speaking it* and an ERP lookup is already in
flight, the agent's audio must stop, superseded synthesis must not re-enter the
turn, and the order must not be corrupted.

```
Agent:  "reading that back, three of 4L60E, total four twelve..."
Caller: "No — 4L80E."
        → local mute inside one frame, no Rime round-trip
        → superseded synthesis fenced at the socket
        → order stays RESERVED; correction absorbed; version bumps
        → new readback → authorization → version-fenced Confirm
```

## Quick start

```
pip install -r requirements.txt
python run.py
```

That is the whole thing. `python run.py` checks the environment, runs the
acceptance suite, runs the adversarial races, runs the Rime preflight, runs the
stress demonstration, and prints a rubric-by-rubric report — with a PASS/FAIL
line per stage and an explicit list of what is **not** measured.

No `make` needed. Works on Windows. Works from a clean checkout with no install
beyond `pytest`, and no `PYTHONPATH`.

| Command | What it does |
|---|---|
| `python run.py` | everything, in order, with a PASS/FAIL report |
| `python run.py test` | acceptance suite only |
| `python run.py chaos` | adversarial races only (TCC + interruption) |
| `python run.py preflight` | blocking eligibility checks |
| `python run.py demo` | the 90-second stress case |
| `python run.py live` | preflight against the **real** Rime API |

`make` targets exist as a convenience and mirror these exactly; they are not
required.

### Going live

```
cp .env.example .env        # add RIME_API_KEY
pip install -r requirements.txt
python run.py live
```

`live` runs three gated steps: the catalog and HTTP endpoints, then a **real
`/ws3` WebSocket** (connect, speak, assert `chunk`/`timestamps`/`done`, measure
cold and warm TTFA, verify `clear` does not stall the next flush), then the
judge report against the live API. Each step gates the next — a bad speaker
makes the socket test meaningless.

It also writes two WAVs to `fixtures/live/` at `inlineSpeedAlpha` 1.60 and 0.60.
**Listen to both.** The docs say `>1.0` is slower for `inlineSpeedAlpha`, but
`speedAlpha` inverts on Mist v3 and nothing states whether inline follows. The
identifier slowing depends on it. The longer file is the slower alpha; record
the finding in `RIME_EVIDENCE.md`.

### Far-end acoustic residue

The one number no application-layer probe can produce. Every other measurement
here sits upstream of the kernel buffer, the carrier and the handset jitter
buffer.

```
python run.py acoustic call.wav reference.wav
```

**Procedure.** Place a real call. Record the caller's handset earpiece with a
second phone. Barge in mid-utterance. Commit both the recording and the
reference PCM that Rime synthesised for the interrupted utterance.

**Method.** Matched filter. The recording mixes agent and caller in one
channel, so energy thresholding cannot separate them -- but we know exactly
what the agent said. Cross-correlate to align, fit the earpiece gain on the
agent-only lead-in, subtract to isolate the caller (that gives T0), then
project each frame of the recording onto the reference to find when the agent
actually stopped (T3). Residue = T3 - T0.

**Validated against known answers.** Thirteen synthetic calls with the residue
built in by construction, across quiet and loud handsets, two noise levels, a
caller near the agent's pitch, and multiple seeds. All recover within one
projection frame (30 ms), which is the quantisation the tool reports.

**It refuses rather than guessing.** If agent-on and agent-off levels are not
separable, or no barge-in is found, it reports an error instead of a plausible
number. A tool that always returns something invites fabricated evidence.

**Requires** `pip install numpy`. Not needed for anything else.

### Telephony

`src/switchboard/agent.py` is the LiveKit + SIP entrypoint. It is the **one
module that cannot be verified offline** — it needs a trunk, a number and a
key. Everything it wires together is tested; the wiring is not. That is stated
in the module rather than implied, because the rest of the evidence would be
worth less otherwise.

Everything except `live` runs offline against a transport that reproduces
`/ws3`'s documented behaviour — including the parts that bite: a chunk carries
the `contextId` active when audio was *requested*; `clear` discards the buffer
but does not abort in-flight synthesis; `eos` can close without a `done`.

## Architecture

```
 caller ──PSTN──► VAD / STT
                     │
              TURN CONTROLLER          overlap_id, speech epoch
                     │                 Bayes risk, arousal gated by voicing
                     ▼
            CANONICAL TRANSACTION FSM  ◄── the ONLY authority
             tx_id · version · digest
                     │
              deterministic renderer   emits text AND span offsets
                     │
     ┌───────────────┴────────────────┐
     │  /textnorm  (mutation-time)    │  ← spoken-form digest
     │  /ws3       (speech-time)      │  ← audio + word timestamps + contextId
     │  spell() · inline_speed_alpha  │  ← identifier delivery
     └───────────────┬────────────────┘
                     ▼
             EGRESS CONTROLLER         ledger · epoch fence · fade
                     │                 mute = write muted frames, never stop
        ┌────────────┴────────────┐
        ▼                         ▼
   frame probe              PSTN handset ──► far-end acoustic measurement
```

Transaction lifecycle is TCC: **Try** creates only the declared reservation
effects — an ATP decrement and a reservation record, and **no fulfilment or
irreversible downstream action begins before Confirm**. The conversation mutates
the reservation freely. **Confirm** is the single irreversible moment: atomic,
version-fenced against the participant's own version, and scoped to the
transaction that owns the reservation.

## The one non-negotiable rule

> **LiveKit and Rime events may never directly mutate transaction state.**

```
Rime "done"          → synthesis finished. NOT "the caller heard it."
LiveKit "interrupted"→ a framework event. NOT "cancel the order."
```

Framework events are validated by the turn controller; only the canonical FSM
decides anything. LiveKit has live 2026 races — stale replies starting after a
new user turn, speech handles wedged after tool interruption, interruption
results applied to the wrong overlap, tool results lost across interruption. If
the framework's speech lifecycle is your transaction authority, those races
become order corruption. Keeping the FSM outside is what makes them merely
annoying.

## Failure behaviour

| Condition | Behaviour |
|---|---|
| `/textnorm` unavailable | Fail closed — action loses the light path, escalates |
| Spoken form stale vs transaction | Authorization refused |
| Commitment words never emitted by Rime | Authorization refused (causality only) |
| Transaction mutates after "yes" | Token invalidated; re-read and re-authorize |
| Reservation expired before Confirm | Reject. Never silently recreate |
| Warehouse picks between auth and commit | Version conflict; Confirm refused |
| Utterance outside the closed grammar | `UNPARSED` → ask again, never commit |
| Repeated false yields on the readback | Escalating desensitisation; first resume silent |
| Rime socket closes without `done` | Terminal; socket drained, never promoted |
| Barge-in within 500 ms of "yes" | Confirm never dispatched; escrow aborts |
| Barge-in after Confirm dispatched | Cancel issued; participant tombstone makes ordering irrelevant |
| Confirm response lost (timeout) | Query-then-reconcile. Never a blind retry |
| Model lacks `inline_speed_alpha` | Control stripped per model, not sent and 400'd |

## What is deliberately not claimed

The system does not know what the caller heard, and cannot. Two receiver states
— played, or sitting in the handset's jitter buffer — are indistinguishable
from anything we can observe. What it establishes is that the authorization
refers to the spoken form **produced and timestamped by the configured Rime
pipeline**, and that superseded audio does not re-enter the turn.

The frame count is a local-egress number. Packets handed to the kernel cannot
be recalled. The user-visible number is the far-end acoustic measurement.

## Layout

```
src/switchboard/
  rime.py     /ws3 client, epoch fence, one clause in generation,
              /textnorm + /oov + /voices, spell(), inline_speed_alpha
  render.py   transaction → spans → text → /textnorm → spoken digest (cached)
  egress.py   playout ledger, muted-frame writer, raised-cosine fade, probe
  turn.py     overlap_id binding, Bayes-risk interruption, damping
  auth.py     closed grammar, single-use session+snapshot-bound token
  commit.py   500ms escrow closing the in-flight-Confirm race
  ulaw.py     pure-python G.711 (audioop is gone in Python 3.13)
  tcc.py      participant FSM: idempotence, anti-suspension, expiry, fencing
eval/
  judge.py      the one-command rubric walkthrough
  demo.py       the 90-second stress case
  preflight.py  blocking eligibility checks (--live for the real API)
```

## Scope boundaries

One active consequential transaction at a time; a second triggers explicit
serialization. Synthetic catalog and accounts throughout. Deliberate relay of a
challenge by a cooperating third party is out of the threat model — the caller
is placing their own order and has no incentive. See `RIME_EVIDENCE.md` for the
full list of what was cut and why.


## Verified Rime API contracts

Every one of these was wrong in an earlier revision, passed its mock, and would
have failed on first contact with the live API. Each now has a regression test
and the fake asserts the contract.

| Surface | Contract |
|---|---|
| Text normalization | `POST https://optimize.rime.ai/textnorm` → `{"normalized": ...}` |
| Voice catalog | `GET https://users.rime.ai/data/voices/all-v2.json` — **public, no auth**, keyed `{modelId: {lang: [speakers]}}` |
| Streaming | `wss://users-ws.rime.ai/ws3?speaker=&modelId=&lang=&audioFormat=&samplingRate=&segment=`, key in the `Authorization` header |
| Flush | `{"operation":"flush"}` — a bare operation; `contextId` rides on the **text** message |
| Per-span speed | `inlineSpeedAlpha` applies **only to `[square brackets]`**, one value per bracketed span, in order |
| Bracket types | `[square]` speed · `{curly}` phonemes · `<angle>` pauses — three different controls |
| Model capability | `inlineSpeedAlpha` and `phonemizeBetweenBrackets` are Mist-family; Coda/Arcana reject them, so controls are stripped per model |
| Speed matrix | `inlineSpeedAlpha` (selected words): **Mist v2 + Mist v3**, `<1.0` faster. `timeScaleFactor` (whole response): **Coda + Mist v3**, `>1.0` slower. `speedAlpha` on Coda/Mist v3 runs the **opposite** way. Directions are per-parameter and must not be inferred across them. |
| Bracket binding | `inlineSpeedAlpha` applies to **words in `[square brackets]`**, one value per bracketed span. Rime's own example brackets single plain words: `"This sentence is [really] [fast]"`. |

### Speed control: a worked example of getting this wrong

`python run.py speed` exists because an earlier revision drew a conclusion its
experiment could not support. Two renders at `inlineSpeedAlpha` 1.60 and 0.60
differed by 60 ms on an 8.2 s utterance, and that was read as "Mist v3 ignores
the parameter". It confounded three separate questions:

1. is the parameter reaching the model?
2. is the **bracket markup** valid?
3. does the model honour it?

Rime's documented example brackets single plain words — `"This sentence is
[really] [fast]"`. The markup under test bracketed `[spell(4L80E)]` (a function
call) and `[$412.59]` (a token that normalises to six words). Either binds
nothing while the parameter works perfectly.

Worse, the wrong conclusion was then written into the capability table, which
made `supports_inline_speed()` return False, which stripped the brackets — and
disabled the very probe that would have investigated. **A single negative
result must not narrow a capability when the experiment itself is suspect.**

The diagnostic now runs a matrix — Rime's own example with and without the
parameter, our markup with and without, and `timeScaleFactor` as a control —
so a null result names its own cause.
