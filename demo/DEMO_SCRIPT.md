# Demo script — 4:40

Slides in `demo/` as `.svg` and `.png` (1600×900).

**Two notes on framing.** The PS says twice to disclose limitations, and that
unverified numbers earn no credit — so there is a short **Scope** panel near
the end. Three neutral lines, not a wall of crosses. And `inlineSpeedAlpha` is
presented as *"we tested both controls and shipped the one that works"*, which
is a positive statement about method.

---

## 0:00 – 0:40 · Krish, on location

**Show:** Krish outdoors or in a workshop. Frame it so the viewer can see
there is **nothing around him** — no laptop, no counter, no screen. Just him
and one phone. Hold the wide shot at least five seconds before cutting tighter.

**Krish dials on camera.** Let the ring be audible.

**Voice-over:**

> "This is Krish. He orders auto parts by phone. Hands busy, no screen, and
> the part numbers all look alike — 4L80E, 4L60E. Get one character wrong and
> the wrong transmission ships.
>
> He's calling a real number on a real phone. Everything you're about to hear
> him talk to is Rime."

**Cut to SLIDE 1** (`slide1_problem.png`) at **0:28**, hold to 0:40.

Open here. "Problem and necessity of voice" is 25%, and a wide shot of a man
alone with a phone makes that case faster than any diagram.

---

## 0:40 – 1:40 · The call, and the stress case

**Show:** Krish on the phone, worker terminal as a corner inset if you can.
Real call audio.

**The exchange to capture:**

```
Agent:  "Okay, reading that back. One of 4L80E. Total $137.53.
         Say yes place it, or no cancel."
Krish:  "Wait, change that—"          ← interrupt MID-SENTENCE
        [agent stops]
Krish:  "I need 4L60E instead."
Agent:  "Changed. Okay, reading that back. One of 4L60E..."
Krish:  "Yes, place it."
Agent:  "Result: committed."
```

**Voice-over across the interruption:**

> "There's the hard problem. He corrects the part number *while the agent is
> speaking it*, and while a reservation is already open. Queued Rime audio has
> to stop, superseded audio must not re-enter the turn, and the order has to
> end up as what he actually asked for — not what the agent was halfway
> through saying."

**Point at the terminal:** `barge-in verdict: barge_in (in_commitment=True)`

---

## 1:40 – 2:15 · Where Rime is load-bearing

**Cut to SLIDE 2** (`slide2_textnorm.png`). Hold a full 20 seconds — strongest
slide in the deck.

**You say:**

> "Rime isn't a text-to-speech pipe here. It's in three load-bearing places.
>
> First: we send `spell(4L80E)` and `$137.53`. Rime speaks 'four, L, eight
> zero, E' and 'four hundred twelve dollars fifty nine cents'. Nothing we
> wrote survives to the ear. So we call Rime's `/textnorm` endpoint and bind
> the authorization token to the **normalised spoken form** — not to the text
> we wrote. Remove Rime and that binding cannot be constructed.
>
> Second: `/ws3` word timestamps gate authorization. We refuse to commit an
> order unless Rime confirms the commitment words were actually emitted.
>
> Third: `contextId` fences superseded audio at the socket, per epoch. That's
> what the forty-six chunks on the next slide are."

That's the 20% category. Say all three.

---

## 2:15 – 2:50 · Recognition is the other half

**Cut to SLIDE 4** (`slide4_hearing.png`).

**You say:**

> "The identifier problem runs the other way too. On a live call, Deepgram
> returned '4 L A T E' for 4L80E — it wrote 'eighty' as the word A-T-E. And
> 'yes place it' arrived as two separate transcripts because he paused.
>
> Alias tables can't fix that — the set of strings ASR produces for an
> alphanumeric is open-ended. We normalise to a phonetic skeleton and match by
> similarity, with a confidence floor below which the agent asks again rather
> than guessing. Commands assemble across a six-second window.
>
> And a bare 'yes' stays unparsed. An acknowledgement is not an
> authorization — a backchannel must never commit an order."

---

## 2:50 – 3:30 · The numbers

**Cut to SLIDE 3** (`slide3_numbers.png`).

**You say:**

> "Five runs against the live API. Forty-six to forty-seven stale chunks
> fenced per barge-in — audio that would have reached the caller without the
> epoch fence. Zero stale frames at our egress boundary.
>
> On rate control we tested both of Rime's options live. `timeScaleFactor`
> moved duration a hundred and thirty-six to a hundred and forty-seven
> percent, same direction every run. `inlineSpeedAlpha` moved it under six
> percent with the direction flipping between runs. So we ship the one that
> works, and we say which."

---

## 3:30 – 4:05 · One command, and the phone path

**Show:** terminal, full screen.

```powershell
python run.py
```

**You say** as it scrolls:

> "One command reproduces everything on those slides. Environment, a hundred
> and sixty tests, adversarial races, the Rime preflight against the live
> catalog at runtime, the stress case, and a rubric-by-rubric report.
>
> The regression detectors fail on deliberately broken code — so a green run
> means something."

**Freeze on** `ALL STAGES PASS`, then **cut to SLIDE 5**
(`slide5_telephony.png`) at ~3:55.

> "And the whole path is real: handset, PSTN, Twilio, SIP, LiveKit, our agent,
> Rime. Call volume is limited by international call cost, not capability —
> Exotel needed a registered company, and Indian numbers route into TRAI
> compliance, so a US number was the only route open to an individual."

---

## 4:05 – 4:40 · Close

**Cut to SLIDE 6** (`slide6_scope.png`).

**You say:**

> "Everything on the left is measured on the live Rime API. Everything on the
> right is where those measurements end, stated by the tool itself on every
> run.
>
> One hard voice problem — interruption and recovery, with the order staying
> consistent — solved on a real phone call, with Rime doing work no other
> component could do."

End there. No summary.

---

## Commands

```powershell
python run.py                    # the reproducibility section
python -m switchboard.agent dev  # leave running during the call
python run.py live               # optional: the real /ws3 socket
python run.py speed              # optional: the two speed controls
```

---

## Submission zip

Everything in the repository except `.env`, `.venv`, `__pycache__`,
`.pytest_cache`:

```
README.md   RIME_EVIDENCE.md   requirements.txt   run.py   Makefile
conftest.py   .env.example
src/switchboard/  rime render hearing egress turn auth tcc commit
                  ulaw console agent
eval/             judge demo preflight live speed acoustic
tests/            test_acceptance.py  fakes.py
fixtures/         skus.txt  textnorm.snapshot.json
demo/             6 slides + this script
```

Plus the recorded demo, and the raw call recording if you keep it separate.

**Check:** `.env` is not in the archive, `python run.py` passes from a clean
unzip, no API key in any committed file.

---

## PS coverage

| Requirement | Where |
|---|---|
| Rime provides primary spoken output | our own `/ws3` client on the agent path |
| One hard voice problem, acceptance test defined first | interruption/heard-state, `RIME_EVIDENCE.md` |
| Real conditions | PSTN call, G.711 mulaw @ 8 kHz |
| Stress case in the demo | mid-utterance correction with a reservation open |
| Which speech provider is active | slide 5 and the terminal |
| Working code judges can inspect | 160 tests, `python run.py` |
| README: model, speaker, language, endpoint, format, transport | declared, preflight-verified at runtime |
| `RIME_EVIDENCE.md`: claim, test, procedure, result, limits | ✓ |
| `.env.example` placeholders only | enforced by preflight |
| Cold vs warm, cached vs uncached labelled | ✓ |
| Fallbacks disclosed, Rime the default path | ✓ |
| Synthetic catalog data | ✓ |
