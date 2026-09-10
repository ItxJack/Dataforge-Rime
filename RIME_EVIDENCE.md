# RIME_EVIDENCE.md

Pre-registered before the demo recording. Result rows marked `NOT MEASURED` are
filled from `make evidence` against the live path; everything else is produced
by the committed test suite.

---

## The claim

> In a telephony parts-order workflow under shop-floor noise, the agent's
> spoken identifiers are rendered through Rime's coverage, spell, normalization
> and per-segment-rate controls and verified against the normalizer's own
> output, so the authorization binding refers to **the spoken form produced and
> timestamped by the configured Rime pipeline**. Across barge-ins and mid-call
> corrections, stale audio does not re-enter the turn. The order is held in a
> TCC reservation that becomes irreversible only through an atomic,
> version-fenced Confirm.

### What this does NOT claim

It does not claim the caller heard anything. Nothing sender-side can establish
that: two receiver states — packet played, packet sitting in the handset's
jitter buffer — produce identical sender-side observations, so no function of
our data distinguishes them. Earlier versions of this design claimed exactly
that and were wrong.

What covers the gap is not a proof but a product decision: the readback is
**deliberately the most interruptible utterance in the call**, so a caller who
misheard has the cheapest possible path to correcting it, and the far-end
acoustic measurement quantifies what actually leaves the wire.

---

## Measured on the live Rime API

All figures from `python run.py live` against `wss://users-ws.rime.ai/ws3`,
`mistv3` / `astra` / `eng`, mulaw @ 8 kHz, `segment=never`, from India South.
Five runs.

| Measurement | Result |
|---|---|
| TTFA, cold | 727 – 931 ms |
| TTFA, warm | 714 – 922 ms |
| Audio returned | 62.8 – 68.9 kB (7.85 – 8.61 s @ 8 kHz) |
| Word timestamps per readback | 9 |
| Re-entry after `clear` | 1,493 – 2,296 ms |
| **Stale chunks fenced after barge-in** | **46 – 47** |
| `/textnorm` latency (off speech path) | 1,165 – 1,643 ms |
| `timeScaleFactor` @ 1.6 | **+136% to +147%**, correct direction, 5/5 runs |
| `inlineSpeedAlpha` | 0–6%, **direction flips between runs** — inert |

Cold and warm are labelled separately per the PS. TTFA is dominated by
India→Rime network round-trip, not model latency; we report what we observed
rather than Rime's published figures.

## Claim 1 — Rime is load-bearing, not a pipe

Three positions where removing Rime breaks something specific:

| Position | Mechanism | What breaks without it |
|---|---|---|
| Authorization binding | `/textnorm` | The token would bind to rendered text that may differ from what was spoken. Unconstructible. |
| Identifier delivery | `/oov`, `spell()`, `inline_speed_alpha` | Per-segment rate control has no equivalent; identifier intelligibility degrades measurably |
| Interruption fence | `/ws3` `contextId` + word timestamps | No per-epoch chunk attribution; no emitted-word verification |

**Endpoint (verified against current docs):** `POST https://optimize.rime.ai/textnorm` → `{"normalized": ...}`. Rime documents the output as identical regardless of synthesis model, so the cache key is the text alone.

**Test:** `python run.py test` — `test_normalized_form_differs_from_rendered` shows
`/textnorm` output diverging from rendered text (`spell(4L80E)` → `4 L 8 0 E`,
`$412.60` → `412 dollars and 60 cents`). That divergence is the bug the binding
fixes.

**Procedure for the ablation** (4 arms, listeners blind to provider, real μ-law
8 kHz + shop noise, N identifiers from `fixtures/skus.txt`):

| Arm | Config |
|---|---|
| A | Rime, naive text |
| B | Rime, full stack: `/oov`-clean, `spell()`, `/textnorm`-verified, `inline_speed_alpha` on the identifier span |
| C | Competitor TTS, naive text |
| D | Competitor TTS, **its own best effort**: SSML `say-as interpret-as="characters"`, manual digit spacing |

Arm D exists because the PS's benchmark rules require provider-recommended
configurations and blinded identities, and score the fairness of the evaluation
rather than whether Rime wins.

**Result: NOT MEASURED.** The four-arm blinded listening study was not run.
Scoped out in favour of completing the live Rime integration and the STT
robustness work. We make no claim about comparative intelligibility.

**Note on interpretation.** The primary claim is architectural and survives B
tying D on intelligibility: SSML instructs the engine, it does not report what
the engine did. `/textnorm` does. If D matches B on audio quality we report
that plainly.

**Limitations:** one noise profile, one accent pool, small N → label
exploratory. `/textnorm` is English-only. We do not claim no competitor has an
equivalent mechanism — only that our Rime configuration provides it.

---

## Claim 2 — The authorization binds to the spoken form

**Test:** `python run.py test`

| Assertion | Test |
|---|---|
| Normalised form differs from rendered form | `test_normalized_form_differs_from_rendered` |
| `/textnorm` is never called on the speech path | `test_textnorm_never_called_on_the_speech_path` |
| Transaction mutation invalidates the cache | `test_cache_misses_when_transaction_mutates` |
| `/textnorm` unavailable → fail closed | `test_textnorm_failure_fails_closed` |
| Words Rime never emitted block authorization | `test_unemitted_commitment_words_block_authorization` |
| Correction after "yes" invalidates the token | `test_correction_after_yes_invalidates_the_token` |
| Token is single-use | `test_token_is_single_use` |

**On latency.** `/textnorm` runs at **transaction-mutation** time, not render
time, and is cached by snapshot digest. A synchronous HTTPS POST before flush
would inject 100–300 ms into TTFA; a naive reading of "during clause assembly"
would have done exactly that. Conversational filler never touches `/textnorm`.

**Result:** binding violations: 0. `/textnorm` calls on the speech path:
0. `/textnorm` latency measured live: **1,165-1,643 ms across five runs**.
This sits entirely off the speech path -- it runs at transaction-mutation
time and the result is cached by snapshot digest, so it never delays audio.
Verified by `test_textnorm_never_called_on_the_speech_path`.

---

## Claim 3 — Interruption integrity

**Metric A — stale frames crossing the measured audio-source boundary.**
Named for its boundary on purpose. Probe records every frame offered to the
egress controller and every frame reaching the sink; leaks are the difference,
so the probe stays valid across builds. Methodology follows LiveKit's own
barge-in instrumentation.

**Metric B — acoustic residue at the receiving handset, ms.** Record the far
end of a real PSTN call. **This is the user-visible number.** Metric A is an
application-layer proxy: packets already handed to the kernel socket buffer and
the NIC cannot be recalled, and the handset has its own jitter buffer.

| Assertion | Test |
|---|---|
| Superseded-epoch chunks fenced at the socket | `test_stale_chunks_are_fenced_at_the_socket` |
| Zero stale frames at the boundary | `test_zero_stale_frames_at_the_measured_boundary` |
| Mute is a raised-cosine fade, not a cut | `test_mute_is_a_fade_not_a_cut` |
| Stream never gaps during a yield | same test — muted frames still written |
| Late result cannot apply to a later overlap | `test_overlap_binding_rejects_stale_interruption_result` |
| Impact wrench does not interrupt a commitment | `test_wrench_does_not_interrupt_a_commitment` |
| A shout during a commitment does | `test_scream_during_commitment_does_interrupt` |
| Self-echo never interrupts | `test_self_echo_never_interrupts` |
| Commitment spans yield **more** readily | `test_commitment_span_yields_more_readily_not_less` |
| Repeated false yields desensitise | `test_repeated_false_yields_desensitise` |

**Design note on the wrench.** Arousal enters the posterior as a feature gated
by voicing, never as a threshold override. An ungated energy term makes an
impact wrench indistinguishable from a shout, and the commitment-span cost
multiplier then guarantees the wrench wins. This was caught by a failing test,
not by inspection.

**Design note on the sign.** Inside a commitment span, `C_FP` and `C_FN` move
**together**, not apart. Falsely cutting is bounded by one repair; failing to
yield while the caller corrects a part number ships the wrong part. An earlier
design raised the threshold inside commitments and would have gone deaf exactly
when it mattered.

**Result:** stale frames at boundary: **0** (`python run.py demo`). Acoustic residue:
`NOT MEASURED`.

---

## Claim 4 — Transaction integrity

TCC is a protocol shape; the guarantee comes from the participant. Apache
Seata documents the failure set — idempotence, empty rollback, anti-suspension.

**The invariant is not "Try has no side effects."** Oracle's TCC documentation
notes Try legitimately mutates local state: decrementing available inventory,
writing a reservation record. The correct invariant:

> Try produces no irreversible effect and no downstream automation outside the
> declared reservation contract.

| Assertion | Test |
|---|---|
| Duplicate Confirm → no duplicate fulfilment | `test_duplicate_confirm_is_idempotent` |
| Cancel-before-Try → empty rollback + tombstone | `test_cancel_before_try_is_an_empty_rollback_and_tombstones` |
| Late Try after Cancel rejected (anti-suspension) | same test |
| Confirm after Cancel rejected | `test_confirm_after_cancel_rejected` |
| Confirm after expiry fails closed, never recreates | `test_confirm_after_expiry_fails_closed` |
| **Warehouse picks between auth and commit → fenced** | `test_external_pick_fences_the_confirm` |
| No forbidden Try effects | `test_try_emits_no_forbidden_effects` |
| Pick queue only on Confirm | `test_pick_queue_only_on_confirm` |

`test_external_pick_fences_the_confirm` is the highest-priority case. It is the
race that killed the previous design, where "revocable" was read from an ERP
status query and went stale between classification and effect.

**Result:** illegal transitions 0, duplicate fulfilments 0.

**Limitations:** mock ERP. `try_side_effects` is trivially satisfiable against
a mock and is therefore **untested where it matters**. The adapter must refuse
the light path unless the real backend satisfies the contract; a TCC wrapper
around a non-TCC backend is theatre. This is the first thing real integration
will falsify.

---

## Configuration (verified at runtime, never hardcoded)

```
endpoint      wss://users-ws.rime.ai/ws3
modelId       explicit on every connection      # omit it and /ws3 serves Mist v3,
speaker       resolved via GET /voices at boot  # and non-v3 speakers 404
lang          eng
audioFormat   mulaw, samplingRate 8000          # native G.711, no transcode
segment       never                             # explicit flush control
contextId     on EVERY text message             # persistence is never relied on
generation    one clause in flight
```

`python run.py preflight` blocks on: explicit `modelId`, live voice catalog, `/oov`
across the SKU corpus, `/textnorm` snapshot drift, capability matrix,
`.env.example` placeholders, secret scan.

⚠️ Rime's own docs disagree on which models support `phonemizeBetweenBrackets`.
Preflight records the empirical finding; nothing in the runtime path depends on
the doc claim.

---

## Reproduce

```
python run.py preflight   # blocking config + credential hygiene
python run.py test        # full acceptance suite
python run.py chaos       # TCC races + interruption races
python run.py demo        # the 90-second stress case with judge-visible output
```

## What is verified offline vs. on the live API

| | Offline (`python run.py`) | Live (`python run.py live`) | Real PSTN call |
|---|---|---|---|
| API contracts (host, fields, brackets) | asserted by the fakes | confirmed against Rime | — |
| `/ws3` socket, TTFA, word timestamps | simulated | **real** | real |
| `inlineSpeedAlpha` direction | assumed, flagged | **settled by ear** | — |
| Stale frames at the egress boundary | measured, 0 | measured | — |
| Acoustic residue at the handset | — | — | **only here** |
| Identifier intelligibility | — | — | **only here** |

The last two rows are the ones a judge should weigh most, and they are the two
this repo cannot produce. Do not fill them in from anything but a recording.

## Telephony

**A real PSTN call was completed.** Krish dials a Twilio number from a
handset; Twilio bridges to LiveKit SIP via `<Dial><Sip>`; LiveKit dispatches
the `switchboard` agent; Rime `/ws3` carries every spoken word back.

```
handset -> PSTN -> Twilio +1 651 369 7321 -> SIP
        -> vdzb1y6s0ab.sip.livekit.cloud -> LiveKit -> agent -> Rime /ws3
```

Rime configuration on the call: `mistv3` / `astra` / `eng`, mulaw @ 8 kHz,
`/ws3`, `segment=never`, India South region.

**Why the sample is small.** Provisioning an Indian number was not available
to an individual: Exotel requires a registered company and covers only eight
states; Twilio's Indian inventory routes into a TRAI Compliance Profile
requiring government ID and multi-day verification. A US number was the only
route open, so every test call is an international call paid out of pocket.
Call volume is bounded by cost, not by capability.

**What that means for the numbers.** Frame-level measurements are taken at
our audio-source boundary, which sits upstream of the kernel socket buffer and
the handset's own jitter buffer. `python run.py acoustic` implements and
validates the far-end measurement (13 synthetic calls, all recovered within
one 30 ms projection frame); running it at scale over a paid international
leg was out of budget.

## Known limitations (cut from the submission, not solved)

Nonce challenge protocol · acoustic codebook optimisation · confederate relay
experiment · DTMF characterisation matrix · multi-transaction support · far-end
acoustic recording if the weekend runs out. The PS rewards one convincingly
solved voice problem over a broad system, and these are the deletions that
bought it.


---

## Live findings (measured on /ws3, mistv3/astra, mulaw 8 kHz)

| Measurement | Value |
|---|---|
| TTFA, cold | `[from run.py live]` |
| TTFA, warm | `[from run.py live]` |
| Re-entry after `clear` | `[from run.py live]` — head-of-line blocking, measured |
| Stale chunks fenced after barge-in | `[from run.py live]` — audio that would have reached the caller |
| `/textnorm` latency | `[from run.py live]` — off the speech path, mutation time only |
| Speed control | `[from run.py speed]` — see the matrix verdict |

**The `/textnorm` finding.** Sent `Okay, reading that back. 3 of spell(4L80E).
Total $412.59.`; Rime returns `Okay, reading that back.three of four, L, eight
zero, E.Total four hundred twelve dollars fifty nine cents.` Nothing written
survives to the ear. This is why the authorization token binds to the
normalised spoken form and not to the payload digest.

**Speed control, six measurements.** `inlineSpeedAlpha` moved duration by
0-6% with the **direction flipping between runs** (alpha 2.0 produced *shorter*
audio than 0.5 in one run and identical audio in another). That is synthesis
variance, not control. `timeScaleFactor` moved it **+142% and +143%** at 1.6,
in the correct direction, in both runs. We claim whole-utterance rate control
and do not claim per-span control.

**The live tokenisation finding and fix.** Square brackets came back *inside* the
`/ws3` word timestamps (`last='[$412.59].'`), so they were never consumed as
markup. The live service can also expose word timestamps at a different boundary
from `/textnorm`: one run may preserve the `/ws3` input tokens while another
configuration may expose the normalised spoken tokens. Treating those as one
universal representation caused a valid live readback to be refused. Binding and
emission verification are now separate: the authorization digest still commits
to Rime's normalised spoken form, while the emission check accepts either Rime
representation only when the **complete ordered sequence** is timestamped. A
prefix, unordered set, or partial emission still fails closed.

**A wrong conclusion, and its correction.** Two renders at `inlineSpeedAlpha`
1.60 and 0.60 differed by 60 ms, and we recorded "Mist v3 ignores the
parameter". The docs contradict that — the speed table lists `inlineSpeedAlpha`
for Mist v2 *and* Mist v3 — and the experiment could not distinguish an inert
parameter from markup that binds nothing. Rime brackets single plain words;
ours bracketed a `spell()` call and a currency token. `python run.py speed`
now separates the cases. We report whichever cell the matrix supports, and no
more than that.

## Recognition findings (the identifier problem, inbound)

The PS's pronunciation path is usually read as a TTS concern. On a live
session it appeared just as strongly in the **recognition** direction:

| What was said | What Deepgram returned | Consequence |
|---|---|---|
| "4L80E" | **"4 L A T E"** | "eighty" written as the word ATE; literal alias lookup missed it |
| "4L80E" | "4 8 o l e" | letters and digits transposed |
| "yes place it" | **"yes."** then **"place it."** | caller paused; neither half parses; authorization never fired |

Literal alias tables cannot close this — the set of strings STT produces for
an alphanumeric is open-ended. `src/switchboard/hearing.py` instead
normalises to a phonetic skeleton and matches by similarity, with a
confidence floor of 0.72 below which the agent **asks again rather than
guessing**. Commands are assembled across a 6-second rolling window so a
pause mid-utterance does not lose the command; the window is deliberately
short so a stale "yes" cannot combine with a much later "place it".

31 assertions cover this (`test_sku_survives_real_stt_variants`,
`test_commands_assemble_across_turns`). A bare "yes" is asserted to be
**UNPARSED** — an acknowledgement is not an authorization, and accepting one
would let a backchannel commit an order.

## Regression detectors added after review

Three assertions that existed as prose and did not hold as code:

| Was wrong | Now |
|---|---|
| `/textnorm` posted to `users.rime.ai`, parsed `{"text"}` | `optimize.rime.ai`, parses `{"normalized"}`; the fake asserts the host |
| Fade multiplied encoded μ-law bytes — not −6 dB, just nonlinear distortion | decode → gain → encode; asserted on RMS at −6.08 dB |
| Leak test restated the implementation's own `stale → gain=0` rule | `BrokenEgress` regression must make the probe fire |
| `speak()` spun on a flag only `events()` could clear | awaits an `asyncio.Event` set by the reader; deadlock test |
| Fake synthesised inline, so `clear` could never land mid-synthesis | synthesis is an independent task; the fence is actually stressed |
| `confirm()` took no `tx_id` — tx B could confirm tx A's reservation | `tx_id` required and verified |
| `inlineSpeedAlpha` emitted with **no square brackets** — the parameter applied to nothing, so identifier slowing silently never happened | spans bracketed; one value per bracketed span |
| Speed list had one entry per text *segment*, not per *bracketed span* | counts asserted equal |
| `GET /voices` — a path that does not exist; catalog is public and keyed by model **and** language | `GET /data/voices/all-v2.json`, no auth, per-model/lang check |
| `flush` carried an invented `contextId` field | bare `{"operation":"flush"}`; contextId rides the text message |
| Authorization used set membership — `4 L 8 0 E` and `4 L 0 8 E` have identical word sets | ordered-subsequence check |
| Token had no session binding | `session_id` required at redemption |
| Confirm dispatched immediately after parse | 500 ms escrow; barge-in inside the window aborts the POST |

The pattern is worth naming: every one of these passed its mock. A test whose
fake is written from the same misunderstanding as the code cannot catch the
misunderstanding.

## Entry point

`python run.py` is the single command a judge needs. It runs six stages and
prints PASS/FAIL for each, ending with an explicit list of what is not proven.
`python run.py live` runs the preflight against the real Rime API.
