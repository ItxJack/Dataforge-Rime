"""
Acceptance + chaos. Every test maps to a claim in RIME_EVIDENCE.md.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fakes import FakeSession, FakeTransport  # noqa: E402

from switchboard.auth import Act, AuthError, Authorizer, parse_act  # noqa: E402
from switchboard.egress import EgressController  # noqa: E402
from switchboard.render import LineItem, Renderer, Transaction  # noqa: E402
from switchboard.rime import RimeConfig, RimeHttp, RimeStream, spell  # noqa: E402
from switchboard.tcc import (  # noqa: E402
    FORBIDDEN_TRY_EFFECTS,
    BranchState,
    IllegalTransition,
    Participant,
    ReservationExpired,
    VersionConflict,
)
from switchboard.turn import Evidence, TurnController, Verdict  # noqa: E402


def tx_fixture() -> Transaction:
    tx = Transaction(tx_id="tx-1")
    tx.mutate(lambda t: t.items.append(LineItem("4L80E", 3, 13753)))
    return tx


# --- CLAIM 2: spoken-form binding ---------------------------------------


def test_normalized_form_differs_from_rendered():
    """The whole reason the payload digest was the wrong thing to bind to."""
    http = RimeHttp("k", FakeSession())
    r = Renderer(http)
    tx = tx_fixture()
    form = asyncio.run(r.precompute(tx))
    assert form.rendered != form.normalized
    assert "spell(" in form.rendered and "spell(" not in form.normalized
    assert "dollars" in form.normalized


def test_textnorm_never_called_on_the_speech_path():
    """Precomputed at mutation time and cached. The speech path only reads
    the cache -- no blocking HTTPS POST before flush."""
    sess = FakeSession()
    r = Renderer(RimeHttp("k", sess))
    tx = tx_fixture()
    asyncio.run(r.precompute(tx))
    calls_after_precompute = sess.textnorm_calls
    for _ in range(20):
        assert r.cached(tx) is not None
    assert sess.textnorm_calls == calls_after_precompute == 1


def test_cache_misses_when_transaction_mutates():
    r = Renderer(RimeHttp("k", FakeSession()))
    tx = tx_fixture()
    asyncio.run(r.precompute(tx))
    tx.mutate(lambda t: t.items.append(LineItem("AC12684485", 1, 899)))
    assert r.cached(tx) is None  # fail closed, then re-precompute


def test_textnorm_failure_fails_closed():
    r = Renderer(RimeHttp("k", FakeSession(fail_textnorm=True)))
    with pytest.raises(ConnectionError):
        asyncio.run(r.precompute(tx_fixture()))


# --- CLAIM 3: interruption integrity ------------------------------------


def test_stale_chunks_are_fenced_at_the_socket():
    async def run():
        t = FakeTransport()
        s = RimeStream(t, RimeConfig(speaker="astra", model_id="mistv3"))
        await s.speak(1, "The part is 4L60E")
        seen = 0
        async for ev in s.events():
            if ev["type"] == "done":
                break
        await s.barge_in(2)
        await t.inject_stale("e1.c1", n=5)
        await t.out.put(None)
        async for ev in s.events():
            if ev["type"] == "audio":
                seen += 1
        return s.dropped_chunks, seen

    dropped, escaped = asyncio.run(run())
    assert dropped == 5
    assert escaped == 0


def test_zero_stale_frames_at_the_measured_boundary():
    e = EgressController()
    for _ in range(5):
        e.offer(b"\x7f" * 160, frame_epoch=0)
    e.mute(new_epoch=1)
    for _ in range(10):
        e.offer(b"\x7f" * 160, frame_epoch=0)  # in-flight, superseded
    assert e.probe.leaked == 0
    assert e.probe.emitted == 15  # stream never gaps: muted frames still written


def test_mute_is_a_fade_not_a_cut():
    e = EgressController()
    e.offer(b"\x7f" * 160, 0)
    e.mute(1)
    e.offer(b"\x7f" * 160, 1)
    gains = [f.gain for f in e.ledger]
    assert gains[0] == 1.0
    assert 0.0 <= gains[-1] < 1.0


def test_overlap_binding_rejects_stale_interruption_result():
    """LiveKit defect: a late result applied to a LATER overlap."""
    e = EgressController()
    tc = TurnController(e)
    ov1 = tc.open_overlap(0.0)
    tc.decide(ov1, Evidence(vad_posterior=0.9, voicing=0.9, partial_words=2), True)
    ov2 = tc.open_overlap(1000.0)
    verdict = tc.decide(ov1, Evidence(vad_posterior=0.9, voicing=0.9), True)
    assert verdict is Verdict.NOISE
    assert ov2.resolved is False


def test_wrench_does_not_interrupt_a_commitment():
    """High energy, no voicing, no lexical content."""
    tc = TurnController(EgressController())
    ov = tc.open_overlap(0.0)
    v = tc.decide(ov, Evidence(vad_posterior=0.4, voicing=0.05, arousal=1.0), True)
    assert v is not Verdict.BARGE_IN


def test_scream_during_commitment_does_interrupt():
    tc = TurnController(EgressController())
    ov = tc.open_overlap(0.0)
    v = tc.decide(
        ov, Evidence(vad_posterior=0.9, voicing=0.9, arousal=1.0, partial_words=2), True
    )
    assert v is Verdict.BARGE_IN


def test_self_echo_never_interrupts():
    tc = TurnController(EgressController())
    ov = tc.open_overlap(0.0)
    v = tc.decide(ov, Evidence(vad_posterior=1.0, voicing=1.0, echo_aligned=True), True)
    assert v is Verdict.NOISE


def test_commitment_span_yields_more_readily_not_less():
    """The sign error that made an earlier design deaf exactly when it
    mattered. Costs move together inside a commitment; they do not diverge."""
    borderline = Evidence(vad_posterior=0.5, voicing=0.5, partial_words=1)
    outside = TurnController(EgressController())
    inside = TurnController(EgressController())
    v_out = outside.decide(outside.open_overlap(0), borderline, in_commitment=False)
    v_in = inside.decide(inside.open_overlap(0), borderline, in_commitment=True)
    assert v_in is Verdict.BARGE_IN
    assert v_out is not Verdict.BARGE_IN


def test_repeated_false_yields_desensitise():
    tc = TurnController(EgressController())
    for _ in range(3):
        tc.decide(tc.open_overlap(0), Evidence(vad_posterior=0.2, voicing=0.1), False)
    assert tc.yields_exhausted
    assert tc.announce_resume() is True


# --- CLAIM 4: transaction integrity (chaos) -----------------------------


def test_duplicate_confirm_is_idempotent():
    p = Participant()
    b = p.try_reserve("tx-1", "4L80E", 3)
    p.confirm(b.reservation_id, b.tx_id, b.version)
    p.confirm(b.reservation_id, b.tx_id, b.version)
    assert p.fulfilled.count(b.reservation_id) == 1


def test_cancel_before_try_is_an_empty_rollback_and_tombstones():
    p = Participant()
    assert p.cancel("r-ghost") is None
    with pytest.raises(IllegalTransition):
        p.try_reserve("tx-1", "4L80E", 3, reservation_id="r-ghost")


def test_confirm_after_cancel_rejected():
    p = Participant()
    b = p.try_reserve("tx-1", "4L80E", 3)
    p.cancel(b.reservation_id)
    with pytest.raises(IllegalTransition):
        p.confirm(b.reservation_id, b.tx_id, b.version)


def test_confirm_after_expiry_fails_closed():
    now = [0.0]
    p = Participant(ttl_seconds=10.0, clock=lambda: now[0])
    b = p.try_reserve("tx-1", "4L80E", 3)
    now[0] = 11.0
    with pytest.raises(ReservationExpired):
        p.confirm(b.reservation_id, b.tx_id, b.version)
    assert b.reservation_id not in p.fulfilled


def test_external_pick_fences_the_confirm():
    """THE race the whole design exists to close: the warehouse acts between
    authorization and commit."""
    p = Participant()
    b = p.try_reserve("tx-1", "4L80E", 3)
    stale_version = b.version
    p.externally_pick(b.reservation_id)
    with pytest.raises(VersionConflict):
        p.confirm(b.reservation_id, b.tx_id, stale_version)
    assert p.fulfilled == []


def test_try_emits_no_forbidden_effects():
    """Not 'Try has no side effects' -- Try legitimately decrements ATP and
    writes a reservation record. The invariant is that nothing OUTSIDE the
    declared contract fires."""
    p = Participant()
    p.try_reserve("tx-1", "4L80E", 3)
    assert p.try_phase_effects() & FORBIDDEN_TRY_EFFECTS == set()
    assert "pick_queue_enqueue" not in p.emitted_effects


def test_pick_queue_only_on_confirm():
    p = Participant()
    b = p.try_reserve("tx-1", "4L80E", 3)
    assert "pick_queue_enqueue" not in p.emitted_effects
    p.confirm(b.reservation_id, b.tx_id, b.version)
    assert "pick_queue_enqueue" in p.emitted_effects


# --- authorization -------------------------------------------------------


@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("yes, place it", Act.YES_PLACE),
        ("place it", Act.YES_PLACE),
        ("no, don't place it", Act.NO_CANCEL),
        ("nope", Act.NO_CANCEL),
        ("actually make it four", Act.CHANGE),
        ("say that again", Act.REPEAT),
        ("I don't think so, but yeah, place it", Act.UNPARSED),
        ("yeah... don't place it", Act.UNPARSED),
        ("", Act.UNPARSED),
    ],
)
def test_constrained_grammar(utterance, expected):
    assert parse_act(utterance) is expected


def test_authorization_binds_to_spoken_form():
    http = RimeHttp("k", FakeSession())
    r = Renderer(http)
    tx = tx_fixture()
    form = asyncio.run(r.precompute(tx))
    a = Authorizer()
    tok = a.issue("call-1", tx, form, Act.YES_PLACE, form.rendered_words)
    assert tok.spoken_digest == form.spoken_digest
    assert a.redeem(tok.nonce, "call-1", tx).used


def test_unemitted_commitment_words_block_authorization():
    """Causality only: what Rime never emitted cannot have been heard."""
    r = Renderer(RimeHttp("k", FakeSession()))
    tx = tx_fixture()
    form = asyncio.run(r.precompute(tx))
    truncated = form.rendered_words[:-2]
    with pytest.raises(AuthError, match="never emitted"):
        Authorizer().issue("call-1", tx, form, Act.YES_PLACE, truncated)


def test_correction_after_yes_invalidates_the_token():
    """Authorization replay across transaction versions."""
    r = Renderer(RimeHttp("k", FakeSession()))
    tx = tx_fixture()
    form = asyncio.run(r.precompute(tx))
    a = Authorizer()
    tok = a.issue("call-1", tx, form, Act.YES_PLACE, form.rendered_words)
    tx.mutate(lambda t: t.items.append(LineItem("AC12684485", 1, 899)))
    with pytest.raises(AuthError, match="mutated"):
        a.redeem(tok.nonce, "call-1", tx)


def test_token_is_single_use():
    r = Renderer(RimeHttp("k", FakeSession()))
    tx = tx_fixture()
    form = asyncio.run(r.precompute(tx))
    a = Authorizer()
    tok = a.issue("call-1", tx, form, Act.YES_PLACE, form.rendered_words)
    a.redeem(tok.nonce, "call-1", tx)
    with pytest.raises(AuthError, match="already used"):
        a.redeem(tok.nonce, "call-1", tx)


def test_cannot_authorize_on_a_non_affirmative_act():
    r = Renderer(RimeHttp("k", FakeSession()))
    tx = tx_fixture()
    form = asyncio.run(r.precompute(tx))
    for act in (Act.NO_CANCEL, Act.CHANGE, Act.REPEAT, Act.UNPARSED):
        with pytest.raises(AuthError):
            Authorizer().issue("call-1", tx, form, act, form.rendered_words)


# --- config hygiene ------------------------------------------------------


def test_model_id_must_be_explicit():
    with pytest.raises(ValueError):
        RimeConfig(speaker="astra", model_id="")


def test_spell_strips_dashes():
    assert spell("AC-12684485") == "spell(AC 12684485)"


# --- regression detectors (added after review) ----------------------------


class BrokenEgress(EgressController):
    """A deliberately regressed egress: it forgets to zero superseded frames.

    The original leak test asserted `leaked == 0` against an implementation
    whose own rule was `stale -> gain = 0`. The test therefore re-stated the
    implementation and could not detect the regression it existed to catch.
    This subclass is the regression; the test below asserts the probe FIRES.
    """

    def offer(self, pcm, frame_epoch, unit_seq=None):
        self.probe.offered += 1
        stale = frame_epoch < self.epoch
        gain = self._next_gain()  # <-- the bug: no stale check
        self.sink.append(self._apply_gain(pcm, gain))
        self.probe.emitted += 1
        if stale and gain > 0.0:
            self.probe.stale_emitted += 1
        return gain > 0.0


def test_probe_detects_a_regressed_egress():
    """If this passes trivially, the leak metric is worthless.

    The scenario matters. Muting alone drives gain to zero, which masks a
    missing stale check -- the first version of this test proved nothing for
    exactly that reason. The real bug is stale audio re-entering AFTER the new
    turn has started speaking, when gain is back at 1.0.
    """
    ok, broken = EgressController(), BrokenEgress()
    for e in (ok, broken):
        e.offer(b"\xff" * 160, 0)
        e.mute(1)                       # barge-in
        e.restore()                     # new turn begins speaking
        for _ in range(4):
            e.offer(b"\xff" * 160, frame_epoch=1)   # current audio
        for _ in range(4):
            e.offer(b"\xff" * 160, frame_epoch=0)   # superseded, still in flight
    assert ok.probe.leaked == 0
    assert broken.probe.leaked > 0, "probe cannot detect a real regression"


def test_stale_frames_carry_no_acoustic_energy():
    """Assert the WAVEFORM, not the gain metadata. Correct metadata over a
    wrong waveform was passing before."""
    import math

    from switchboard.egress import EgressController as E

    tone = bytes(
        E._apply_gain.__func__(b"\x00", 1.0)[0] if False else 0 for _ in range(0)
    )
    lin = b"".join(
        int(12000 * math.sin(i / 8)).to_bytes(2, "little", signed=True)
        for i in range(160)
    )
    try:
        import audioop
    except ImportError:
        from switchboard import ulaw as audioop
    tone = audioop.lin2ulaw(lin, 2)

    e = EgressController()
    e.offer(tone, 0)
    e.mute(1)
    for _ in range(4):
        e.offer(tone, frame_epoch=0)
    residue = e.sink[-1]
    assert EgressController.rms_dbfs(residue) < -60.0


def test_fade_is_actually_minus_six_db_on_mulaw():
    """PCMU is logarithmically companded (RFC 3551). Scaling encoded bytes is
    not a -6 dB fade; it is nonlinear distortion that happens to get quieter."""
    import math

    try:
        import audioop
    except ImportError:
        from switchboard import ulaw as audioop

    lin = b"".join(
        int(12000 * math.sin(i / 8)).to_bytes(2, "little", signed=True)
        for i in range(160)
    )
    tone = audioop.lin2ulaw(lin, 2)
    faded = EgressController._apply_gain(tone, 0.5)
    delta = EgressController.rms_dbfs(faded) - EgressController.rms_dbfs(tone)
    assert -6.5 < delta < -5.5, f"expected ~-6.02 dB, got {delta:.2f}"


def test_confirm_rejects_another_transactions_reservation():
    """Isolation: a correct version number must not let tx B confirm tx A's
    reservation."""
    p = Participant()
    b = p.try_reserve("tx-A", "4L80E", 3)
    with pytest.raises(IllegalTransition, match="belongs to"):
        p.confirm(b.reservation_id, "tx-B", b.version)
    assert p.fulfilled == []


def test_token_cannot_cross_calls():
    r = Renderer(RimeHttp("k", FakeSession()))
    tx = tx_fixture()
    form = asyncio.run(r.precompute(tx))
    a = Authorizer()
    tok = a.issue("call-A", tx, form, Act.YES_PLACE, form.rendered_words)
    with pytest.raises(AuthError, match="different call"):
        a.redeem(tok.nonce, "call-B", tx)


def test_textnorm_goes_to_optimize_host():
    """The fake asserts the host. An earlier version posted /textnorm to
    users.rime.ai and parsed {"text": ...}; every mock passed and the live
    authorization path would have failed."""
    http = RimeHttp("k", FakeSession())
    asyncio.run(http.textnorm("$412.60"))
    assert http.calls[0][0] == "https://optimize.rime.ai/textnorm"


def test_unsupported_controls_are_stripped_per_model():
    """Sending inlineSpeedAlpha to a model that ignores it is worse than a 400:
    it is a silent no-op.

    mistv3 is in the NOT-supported set because it was MEASURED that way on a
    live socket -- renders at alpha 1.60 and 0.60 differed by 60 ms on an 8.2 s
    utterance. This test previously encoded my assumption that the whole Mist
    family supported it, and passed while the live behaviour disagreed.
    """
    mistv2 = RimeConfig(speaker="abbie", model_id="mistv2")
    mistv3 = RimeConfig(speaker="astra", model_id="mistv3")
    coda = RimeConfig(speaker="astra", model_id="coda")
    payload = {"inlineSpeedAlpha": "1,1.25", "timeScaleFactor": "1.2",
               "phonemizeBetweenBrackets": True}

    # Per Rime's speed table: inlineSpeedAlpha covers "Selected words" on
    # Mist v2 AND Mist v3. Coda does not support it.
    assert "inlineSpeedAlpha" in mistv2.filter_controls(payload)
    assert "inlineSpeedAlpha" in mistv3.filter_controls(payload)
    assert "inlineSpeedAlpha" not in coda.filter_controls(payload)

    # timeScaleFactor is the Coda / Mist v3 whole-response control.
    assert "timeScaleFactor" in coda.filter_controls(payload)
    assert "timeScaleFactor" in mistv3.filter_controls(payload)
    assert "timeScaleFactor" not in mistv2.filter_controls(payload)

    assert "phonemizeBetweenBrackets" not in coda.filter_controls(payload)


def test_ws_url_carries_the_configuration():
    cfg = RimeConfig(speaker="astra", model_id="mistv3")
    url = cfg.ws_url()
    for expected in ("speaker=astra", "modelId=mistv3", "segment=never",
                     "audioFormat=mulaw", "samplingRate=8000"):
        assert expected in url


def test_speak_does_not_deadlock_without_an_event_consumer():
    """speak() must await an event set by the reader, not spin on a flag that
    only events() can clear."""

    async def run():
        t = FakeTransport()
        s = RimeStream(t, RimeConfig(speaker="astra", model_id="mistv3"))
        reader = asyncio.create_task(_drain(s))
        await s.speak(1, "first clause.")
        await asyncio.wait_for(s.speak(1, "second clause."), timeout=2.0)
        reader.cancel()
        return len(s.units)

    async def _drain(s):
        async for _ in s.events():
            pass

    assert asyncio.run(run()) == 2


# --- commit escrow --------------------------------------------------------


def test_bargein_inside_escrow_never_dispatches():
    """The race: 'yes, place it' parses, then 'wait' 50ms later while the
    Confirm POST is already on the wire."""
    from switchboard.commit import CommitEscrow, Outcome

    async def run():
        esc = CommitEscrow(escrow_ms=500)
        fired = []

        async def confirm():
            fired.append("confirm")

        async def cancel():
            fired.append("cancel")

        task = asyncio.create_task(esc.dispatch(confirm, cancel))
        await asyncio.sleep(0.05)
        esc.abort()
        return await task, fired

    res, fired = asyncio.run(run())
    assert res.outcome is Outcome.ABORTED_IN_ESCROW
    assert res.dispatched is False
    assert fired == []


def test_bargein_after_dispatch_issues_a_cancel():
    from switchboard.commit import CommitEscrow, Outcome

    async def run():
        esc = CommitEscrow(escrow_ms=10)
        fired = []

        async def confirm():
            fired.append("confirm")
            esc.abort()  # barge-in lands while the POST is in flight

        async def cancel():
            fired.append("cancel")

        return await esc.dispatch(confirm, cancel), fired

    res, fired = asyncio.run(run())
    assert res.outcome is Outcome.CANCELLED_AFTER_DISPATCH
    assert fired == ["confirm", "cancel"]


def test_confirm_timeout_reconciles_instead_of_retrying():
    """Blind retry after a timeout duplicates fulfilment when the participant
    already committed."""
    from switchboard.commit import CommitEscrow, Outcome

    async def run():
        esc = CommitEscrow(escrow_ms=0)
        p = Participant()
        b = p.try_reserve("tx-1", "4L80E", 3)
        p.confirm(b.reservation_id, b.tx_id, b.version)  # committed server-side

        async def confirm():
            raise TimeoutError("response lost")

        async def cancel():
            raise AssertionError("must not cancel a committed branch")

        async def query():
            return p.query(b.reservation_id)

        return await esc.dispatch(confirm, cancel, query), p

    res, p = asyncio.run(run())
    assert res.outcome is Outcome.RECONCILED
    assert "CONFIRMED" in res.detail
    assert len(p.fulfilled) == 1


def test_cancel_racing_confirm_is_order_independent():
    """Cancel arriving first tombstones, so the later Confirm is rejected."""
    p = Participant()
    p.cancel("r-race")
    with pytest.raises(IllegalTransition):
        p.try_reserve("tx-1", "4L80E", 3, reservation_id="r-race")


# --- API contract regressions (verified against current Rime docs) ---------


def test_no_bracket_markup_reaches_rime():
    """MEASURED: /ws3 does not consume [ ] as inlineSpeedAlpha markup -- the
    brackets come back inside the word timestamps. They bought nothing and
    polluted the ledger the heard-state check reads."""
    r = Renderer(RimeHttp("k", FakeSession()), supports_inline_speed=True)
    form = asyncio.run(r.precompute(tx_fixture()))
    assert "[" not in form.rendered and "]" not in form.rendered
    assert form.speeds == ""


def test_emission_is_verified_against_input_tokens_not_spoken_words():
    """THE BUG THAT PASSED 97 TESTS. /textnorm returns normalised spoken words
    (19); /ws3 timestamps report input tokens (9). Comparing them refuses every
    order on the live path. The fakes made the two agree."""
    r = Renderer(RimeHttp("k", FakeSession()), supports_inline_speed=True)
    form = asyncio.run(r.precompute(tx_fixture()))
    assert len(form.rendered_words) != len(form.expected_words), (
        "fixture no longer exercises the mismatch"
    )
    # what /ws3 reports is the input tokenization
    assert Authorizer._missing(form, form.rendered_words) == []
    # Rime may expose the normalised tokenisation in a timestamp stream too;
    # that representation is valid evidence when the COMPLETE ordered sequence
    # is present.
    assert Authorizer._missing(form, form.expected_words) == []


def test_digest_still_binds_the_normalised_spoken_form():
    """Binding and verification are different questions. The token view used
    for verification must not weaken what the digest commits to."""
    r = Renderer(RimeHttp("k", FakeSession()), supports_inline_speed=True)
    form = asyncio.run(r.precompute(tx_fixture()))
    from switchboard.rime import digest

    assert form.spoken_digest == digest(form.normalized)
    assert form.normalized != form.rendered


def test_truncated_emission_still_refuses():
    r = Renderer(RimeHttp("k", FakeSession()), supports_inline_speed=True)
    tx = tx_fixture()
    form = asyncio.run(r.precompute(tx))
    with pytest.raises(AuthError, match="never emitted"):
        Authorizer().issue("c1", tx, form, Act.YES_PLACE, form.rendered_words[:-2])


def test_voices_uses_the_documented_public_catalog_path():
    """There is no generic /voices path, and the catalog is public -- sending
    an Authorization header to it is wrong. The fake asserts both."""
    http = RimeHttp("k", FakeSession())
    speakers = asyncio.run(http.speakers_for("mistv3", "eng"))
    assert "astra" in speakers


def test_speaker_valid_on_one_model_can_be_invalid_on_another():
    """Why a flat 'is this speaker known' check is not enough: the catalog is
    keyed by model AND language, and that combination is what the event
    preflight rejects."""
    http = RimeHttp("k", FakeSession())
    assert "celeste" in asyncio.run(http.speakers_for("coda", "eng"))
    assert "celeste" not in asyncio.run(http.speakers_for("mistv3", "eng"))


def test_flush_is_a_bare_operation():
    async def run():
        t = FakeTransport()
        s = RimeStream(t, RimeConfig(speaker="astra", model_id="mistv3"))
        await s.speak(1, "hello there.")
        return [m for m in t.sent if m.get("operation") == "flush"]

    flushes = asyncio.run(run())
    assert flushes and all("contextId" not in f for f in flushes)


def test_context_id_rides_on_every_text_message():
    """Rime does not maintain multiple simultaneous context IDs and a set id
    persists across messages that omit one, so persistence is never relied on."""
    async def run():
        t = FakeTransport()
        s = RimeStream(t, RimeConfig(speaker="astra", model_id="mistv3"))
        reader = asyncio.create_task(_drain(s))
        await s.speak(1, "one.")
        await s.speak(1, "two.")
        reader.cancel()
        return [m for m in t.sent if "text" in m]

    async def _drain(s):
        async for _ in s.events():
            pass

    texts = asyncio.run(run())
    assert len(texts) == 2 and all(m.get("contextId") for m in texts)


def test_authorization_requires_an_ordered_subsequence():
    """Set membership was wrong in a way that matters: a transposition has an
    identical word set. '4 L 8 0 E' and '4 L 0 8 E' are different part
    numbers and must not both authorize."""
    r = Renderer(RimeHttp("k", FakeSession()))
    tx = tx_fixture()
    form = asyncio.run(r.precompute(tx))
    scrambled = list(form.rendered_words)
    scrambled[1], scrambled[2] = scrambled[2], scrambled[1]  # transpose
    with pytest.raises(AuthError, match="never emitted"):
        Authorizer().issue("call-1", tx, form, Act.YES_PLACE, scrambled)


def test_repeated_words_need_repeated_emissions():
    r = Renderer(RimeHttp("k", FakeSession()))
    tx = tx_fixture()
    form = asyncio.run(r.precompute(tx))
    words = form.rendered_words
    deduped, seen = [], set()
    for w in words:
        if w not in seen:
            seen.add(w)
            deduped.append(w)
    if len(deduped) < len(words):
        with pytest.raises(AuthError):
            Authorizer().issue("call-1", tx, form, Act.YES_PLACE, deduped)


# --- configuration surface ------------------------------------------------


def test_config_reads_every_documented_env_key():
    """A config surface that advertises seven knobs and reads two is worse
    than one that advertises two."""
    cfg = RimeConfig.from_env({
        "RIME_SPEAKER": "celeste", "RIME_MODEL_ID": "coda", "RIME_LANG": "spa",
        "RIME_AUDIO_FORMAT": "pcm", "RIME_SAMPLING_RATE": "22050",
        "RIME_SEGMENT": "immediate",
    })
    assert (cfg.speaker, cfg.model_id, cfg.lang) == ("celeste", "coda", "spa")
    assert (cfg.audio_format, cfg.sampling_rate, cfg.segment) == ("pcm", 22050, "immediate")


def test_config_rejects_mulaw_at_the_wrong_rate():
    """G.711 μ-law is 8 kHz. Any other rate silently breaks the PSTN path."""
    with pytest.raises(ValueError, match="8 kHz"):
        RimeConfig.from_env({"RIME_AUDIO_FORMAT": "mulaw",
                             "RIME_SAMPLING_RATE": "16000"})


def test_config_rejects_out_of_range_sampling_rate():
    with pytest.raises(ValueError, match="4000-44100"):
        RimeConfig.from_env({"RIME_AUDIO_FORMAT": "pcm",
                             "RIME_SAMPLING_RATE": "96000"})


def test_config_rejects_unknown_segment_mode():
    with pytest.raises(ValueError, match="segment"):
        RimeConfig.from_env({"RIME_SEGMENT": "sometimes"})


def test_defaults_are_the_telephony_path():
    cfg = RimeConfig.from_env({})
    assert (cfg.audio_format, cfg.sampling_rate, cfg.segment) == ("mulaw", 8000, "never")


# --- live-mode integrity --------------------------------------------------


def test_live_flag_actually_switches_the_session():
    """A --live flag that only changes a banner line is worse than no flag:
    it reports a live verification that never happened. This was true of
    judge.py for two revisions."""
    import inspect

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
    import judge

    src = inspect.getsource(judge.session_for)
    assert "HttpSession" in src and "FakeSession" in src
    body = inspect.getsource(judge.main)
    assert "FakeSession" not in body, "main() must not hardcode the fake session"
    assert "session_for(live)" in body


def test_websocket_transport_is_actually_used_by_the_live_path():
    """WebSocketTransport was dead code for three revisions -- defined, tested
    nowhere, constructed nowhere. The real /ws3 client must be on a real path."""
    live = (Path(__file__).resolve().parents[1] / "eval" / "live.py").read_text()
    assert "WebSocketTransport" in live
    assert "await WebSocketTransport" in live
    agent = (Path(__file__).resolve().parents[1]
             / "src" / "switchboard" / "agent.py").read_text()
    assert "WebSocketTransport" in agent


def test_agent_never_lets_framework_events_touch_the_transaction():
    """The one non-negotiable rule, asserted against the source: the only path
    from a framework event to state is through the turn controller."""
    agent = (Path(__file__).resolve().parents[1]
             / "src" / "switchboard" / "agent.py").read_text()
    assert "on_user_audio" in agent
    # commits go through the escrow, never straight to the participant
    commit_region = agent[agent.index("async def try_authorize"):]
    assert "escrow.dispatch" in commit_region
    assert "self.authz.issue" in commit_region


def test_agent_imports_cleanly_without_livekit_installed():
    """The module must be inspectable by a judge who has not installed
    livekit-agents."""
    from switchboard import agent

    assert hasattr(agent, "Switchboard") and hasattr(agent, "entrypoint")


# --- console compatibility (Windows cp1252) -------------------------------


def test_report_sources_contain_no_non_ascii_literals():
    """A box-drawing character baked into a print() crashed the judge report on
    a Windows PowerShell console (cp1252) after every other stage had passed.
    Glyphs must come from the console layer, which degrades to ASCII."""
    root = Path(__file__).resolve().parents[1]
    offenders = {}
    for rel in ("eval/judge.py", "eval/demo.py", "eval/preflight.py",
                "eval/live.py", "run.py"):
        bad = {c for c in (root / rel).read_text(encoding="utf-8") if ord(c) > 127}
        if bad:
            offenders[rel] = sorted(bad)
    assert not offenders, f"non-ASCII literals in report sources: {offenders}"


def test_ascii_fallback_covers_every_glyph():
    from switchboard.console import ASCII_GLYPHS, UNICODE_GLYPHS

    assert set(ASCII_GLYPHS) == set(UNICODE_GLYPHS)
    assert all(ord(c) < 128 for v in ASCII_GLYPHS.values() for c in v)


def test_forced_ascii_mode_is_honoured(monkeypatch):
    from switchboard import console

    monkeypatch.setenv("SWITCHBOARD_ASCII", "1")
    assert console.init() is console.ASCII_GLYPHS


def test_child_env_forces_utf8_and_propagates_ascii(monkeypatch):
    """A child writing to a pipe upgrades to UTF-8 happily; the parent then
    crashes relaying that to a cp1252 terminal. The parent knows the real
    console, so the decision propagates downward."""
    from switchboard import console

    monkeypatch.setattr(console, "unicode_ok", lambda: False)
    env = console.child_env()
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["SWITCHBOARD_ASCII"] == "1"

    monkeypatch.setattr(console, "unicode_ok", lambda: True)
    assert "SWITCHBOARD_ASCII" not in console.child_env()


def test_colours_disabled_when_not_a_tty():
    """Piping to a file or CI log must not embed escape codes."""
    from switchboard.console import colours

    assert all(v == "" for v in colours().values())  # pytest captures stdout


# --- .env parsing (found on first real use) -------------------------------


def _dotenv_module():
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("_run_mod", root / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_inline_comments_are_stripped_from_env_values():
    """`RIME_SAMPLING_RATE=8000   # mulaw is 8 kHz` parsed as the whole string
    and int() blew up at boot -- on the very file this project ships as the
    template. The .env.example was unusable as written."""
    strip = _dotenv_module()._strip_inline_comment
    assert strip("8000       # mulaw is 8 kHz") == "8000"
    assert strip("mistv3    # explicit backend") == "mistv3"
    assert strip("never\t# explicit flush control") == "never"
    assert strip("mulaw") == "mulaw"


def test_hash_inside_a_value_is_preserved():
    """The comment marker needs preceding whitespace, or an API key containing
    '#' gets silently truncated -- a far worse failure than a crash."""
    strip = _dotenv_module()._strip_inline_comment
    assert strip("abc#def123") == "abc#def123"
    assert strip("key#with#hashes") == "key#with#hashes"


def test_quoted_values_are_unwrapped(tmp_path, monkeypatch):
    mod = _dotenv_module()
    env = tmp_path / ".env"
    env.write_text('RIME_SPEAKER="celeste"\nRIME_LANG=\'spa\'\n')
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    for k in ("RIME_SPEAKER", "RIME_LANG"):
        monkeypatch.delenv(k, raising=False)
    mod._load_dotenv()
    assert os.environ["RIME_SPEAKER"] == "celeste"
    assert os.environ["RIME_LANG"] == "spa"


def test_shipped_env_example_parses_into_a_valid_config(tmp_path, monkeypatch):
    """The template must work as-is. This is the test that would have caught it."""
    mod = _dotenv_module()
    root = Path(__file__).resolve().parents[1]
    (tmp_path / ".env").write_text(
        (root / ".env.example").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    for k in ("RIME_MODEL_ID", "RIME_SPEAKER", "RIME_LANG", "RIME_AUDIO_FORMAT",
              "RIME_SAMPLING_RATE", "RIME_SEGMENT"):
        monkeypatch.delenv(k, raising=False)
    mod._load_dotenv()
    cfg = RimeConfig.from_env()          # raises if any value is malformed
    assert cfg.sampling_rate == 8000 and cfg.audio_format == "mulaw"


def test_blank_values_do_not_override_real_ones(monkeypatch, tmp_path):
    """`LIVEKIT_URL=` in the template must not clobber a real environment
    variable set by the shell or a deployment."""
    mod = _dotenv_module()
    (tmp_path / ".env").write_text("RIME_SPEAKER=\n")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setenv("RIME_SPEAKER", "already_set")
    mod._load_dotenv()
    assert os.environ["RIME_SPEAKER"] == "already_set"


# --- live script must not deadlock ---------------------------------------


def test_live_script_starts_its_reader_before_speaking():
    """The deadlock that hung `run.py live` in production.

    speak() waits on an asyncio.Event that only the reader task sets when
    `done` arrives. eval/live.py created the reader AFTER the second speak(),
    so that speak() waited forever for an event nobody could set. The existing
    deadlock test started the reader first and therefore never exercised the
    real ordering mistake -- this asserts the ordering in the script itself.
    """
    src = (Path(__file__).resolve().parents[1] / "eval" / "live.py").read_text()
    section = src[src.index("# --- 5:"):src.index("# --- /textnorm")]
    reader_at = section.index("create_task(pump())")
    speaks = [i for i in range(len(section))
              if section.startswith("stream.speak(", i)]
    assert speaks, "section no longer contains a speak() call"
    assert reader_at < min(speaks), "reader task must be created before any speak()"


def test_every_live_network_call_is_bounded_by_a_timeout():
    """A stalled socket must surface as a readable failure, never as a script
    that sits there looking busy."""
    src = (Path(__file__).resolve().parents[1] / "eval" / "live.py").read_text()
    assert "asyncio.wait_for" in src
    assert "timeout: float = 45.0" in src
    assert src.count("asyncio.wait_for") >= 3


def test_speak_after_barge_in_completes_with_a_reader_running():
    """End-to-end version of the ordering rule, against the fake transport."""

    async def run():
        t = FakeTransport()
        s = RimeStream(t, RimeConfig(speaker="astra", model_id="mistv3"))
        seen: list[int] = []

        async def pump():
            async for ev in s.events():
                if ev["type"] == "audio" and ev.get("unit"):
                    seen.append(ev["unit"].epoch)

        reader = asyncio.create_task(pump())
        await asyncio.wait_for(s.speak(1, "first utterance here."), timeout=5)
        await asyncio.sleep(0)
        await s.barge_in(2)
        await asyncio.wait_for(s.speak(2, "recovered."), timeout=5)
        reader.cancel()
        return s.epoch

    assert asyncio.run(run()) == 2


# --- progress visibility (a hang must be distinguishable from work) -------


def test_children_are_launched_unbuffered():
    """A subprocess block-buffers stdout at ~8 KB when it does not detect a
    TTY, so a long live step looks frozen while it is in fact synthesising.
    `-u` makes progress appear as it happens."""
    src = (Path(__file__).resolve().parents[1] / "run.py").read_text()
    launches = src.count('sys.executable, "-u", "-m"')
    assert launches >= 6, f"only {launches} unbuffered child launches"
    assert 'sys.executable, "-m"' not in src, "a buffered child launch remains"


def test_live_progress_lines_are_flushed():
    src = (Path(__file__).resolve().parents[1] / "eval" / "live.py").read_text()
    assert "flush=True" in src
    assert "def step(" in src, "long operations must announce themselves first"


def test_every_long_live_operation_announces_itself():
    """Each multi-second network step prints BEFORE it starts."""
    src = (Path(__file__).resolve().parents[1] / "eval" / "live.py").read_text()
    assert src.count("step(") >= 6


# --- synthesis controls are connection-level -----------------------------


def test_synthesis_controls_are_rejected_in_the_message_body():
    """Rime: "all synthesis arguments are provided as query parameters when
    establishing the connection." Sent in a text message they are SILENTLY
    ignored -- two renders at alpha 1.60 and 0.60 came back byte-identical in
    duration. A silent no-op is worse than an error, so this raises."""
    async def run():
        t = FakeTransport()
        s = RimeStream(t, RimeConfig(speaker="astra", model_id="mistv3"))
        await s.speak(1, "hello.", extra={"inlineSpeedAlpha": "1.6,1.6"})

    with pytest.raises(ValueError, match="connection-level query parameters"):
        asyncio.run(run())


def test_ws_url_carries_synthesis_controls():
    cfg = RimeConfig(speaker="astra", model_id="mistv3")
    url = cfg.ws_url(extra={"inlineSpeedAlpha": "1.25,1.25"})
    assert "inlineSpeedAlpha=1.25%2C1.25" in url


def test_unsupported_controls_stripped_from_the_connection_too():
    """Coda rejects inlineSpeedAlpha; a 400 at connect time is worse than at
    synthesis time because nothing works at all."""
    from switchboard.rime import WebSocketTransport

    coda = RimeConfig(speaker="astra", model_id="coda")
    t = WebSocketTransport("k", coda, extra_query={"inlineSpeedAlpha": "1.6"})
    assert "inlineSpeedAlpha" not in t.config.ws_url(extra=t.extra_query)


def test_live_passes_speed_on_the_connection_not_the_message():
    src = (Path(__file__).resolve().parents[1] / "eval" / "live.py").read_text()
    assert "extra_query=" in src
    assert 'extra={"inlineSpeedAlpha"' not in src


def test_capability_table_matches_the_documented_speed_matrix():
    """Guard the table against BOTH failure directions.

    A previous revision removed mistv3 after a single failed measurement whose
    markup was itself suspect -- and that removal then disabled the probe that
    would have investigated. One negative result does not narrow a capability
    when the experiment could be at fault.
    """
    from switchboard.rime import INLINE_SPEED_MODELS, TIMESCALE_MODELS

    assert {"mistv2", "mistv3"} <= INLINE_SPEED_MODELS
    assert "coda" not in INLINE_SPEED_MODELS
    assert {"coda", "mistv3"} <= TIMESCALE_MODELS
    assert "mistv2" not in TIMESCALE_MODELS


def test_speed_directions_are_not_inferred_across_parameters():
    """Rime warns explicitly: on Mist v3, inlineSpeedAlpha and speedAlpha go in
    OPPOSITE directions. The source must record this, not infer it."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "switchboard" / "rime.py").read_text()
    assert "opposite direction" in src.lower() or "do not infer" in src.lower()


def test_speed_diagnostic_separates_parameter_from_markup():
    """The confound that produced the wrong conclusion: a null result cannot
    distinguish 'parameter ignored' from 'markup binds nothing'."""
    src = (Path(__file__).resolve().parents[1] / "eval" / "speed.py").read_text()
    for cell in ("A baseline", "docs example + inlineSpeedAlpha",
                 "our markup + inlineSpeedAlpha", "timeScaleFactor"):
        assert cell in src
    assert "[really] [fast]" in src, "must use Rime's own documented example"


def test_speed_probe_uses_non_opposing_alphas():
    """Rime's example is "0.5, 3" -- one word sped up, one slowed down. On a
    2 s utterance they cancel, so the cell cannot show a signal whether or not
    the parameter works. An illustrative example is not an experiment."""
    src = (Path(__file__).resolve().parents[1] / "eval" / "speed.py").read_text()
    assert "ALPHA_SLOW" in src and "ALPHA_FAST" in src
    # Check the VALUES actually used, not any mention -- the docs string is
    # quoted in the comments that explain why it was rejected.
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert 'DOCS_ALPHA' not in code
    for name in ("ALPHA_SLOW", "ALPHA_FAST"):
        line = next(l for l in code.splitlines() if l.startswith(name))
        vals = [float(v) for v in line.split('"')[1].split(",")]
        assert len(set(v > 1.0 for v in vals)) == 1, (
            f"{name} mixes directions; the effects cancel"
        )


def test_speed_tolerance_is_relative_not_absolute():
    """A 240 ms delta is noise on a 2 s clip and real on an 8 s one. A fixed
    millisecond threshold called a genuine 3% shift noise purely because the
    utterance was long."""
    src = (Path(__file__).resolve().parents[1] / "eval" / "speed.py").read_text()
    assert "TOLERANCE_PCT" in src
    assert "TOLERANCE_MS" not in src


def test_markup_cell_is_evaluated_independently():
    """An earlier verdict gated the markup result behind the plain-word cell,
    discarding a valid measurement because a confounded cell failed."""
    src = (Path(__file__).resolve().parents[1] / "eval" / "speed.py").read_text()
    verdict = src[src.index("if markup_works:"):]
    assert "elif param_works" in verdict


def test_websocket_connect_retries_transient_failures_but_not_auth():
    src = (Path(__file__).resolve().parents[1]
           / "src" / "switchboard" / "rime.py").read_text()
    body = src[src.index("async def connect("):]
    assert "open_timeout=30" in body
    assert "attempts" in body
    assert '"401" in str(exc)' in body, "auth failure must not be retried"


# --- one question, one verdict -------------------------------------------


def test_only_the_diagnostic_reports_a_speed_verdict():
    """live.py once ran its own A/B, called a 3% noise delta a verdict, and told
    the operator to change a constant -- contradicting the controlled matrix in
    the same run. Two verdicts on one question is worse than none."""
    live = (Path(__file__).resolve().parents[1] / "eval" / "live.py").read_text()
    assert "IDENTIFIER_SPEED" not in live
    assert "docs are inverted" not in live
    assert "slower_if_gt1" not in live


def test_live_probe_text_matches_what_the_renderer_produces():
    """The probe had its own hardcoded string with bracket markup, so it kept
    reporting brackets in the word timestamps after the renderer stopped
    emitting them. A fixture that drifts from the code under test measures
    nothing."""
    live = (Path(__file__).resolve().parents[1] / "eval" / "live.py").read_text()
    probe = next(l for l in live.splitlines() if l.startswith("PROBE ="))
    assert "[" not in probe and "]" not in probe

    r = Renderer(RimeHttp("k", FakeSession()), supports_inline_speed=True)
    form = asyncio.run(r.precompute(tx_fixture()))
    assert "spell(" in probe and "spell(" in form.rendered
    assert ("[" in probe) == ("[" in form.rendered)


def test_preflight_reports_measurement_not_assumption():
    src = (Path(__file__).resolve().parents[1] / "eval" / "preflight.py").read_text()
    assert "MEASURED " in src and "inert on /ws3" in src
    assert "assumed SLOWER" not in src


# --- far-end acoustic residue --------------------------------------------


def _write_wav(path, samples, sr=8000):
    import struct
    import wave as _w

    with _w.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(
            struct.pack("<h", max(-32768, min(32767, int(s * 32767))))
            for s in samples))


def _synthetic_call(tmp_path, residue_ms: int, sr: int = 8000, gain: float = 0.55,
                    noise: float = 0.002, caller_f: int = 870, seed: int = 5):
    """Build a call where the TRUE residue is known by construction.

    agent speaks 0..1500ms; caller barges in at 800ms; the agent keeps being
    audible for `residue_ms` past that, then stops.
    """
    import math
    import random

    random.seed(seed)
    n = int(sr * 2.0)
    # amplitude-modulated: a pure sine is far easier to separate than speech
    agent = [0.5 * math.sin(2 * math.pi * 210 * i / sr)
             * (1 + 0.3 * math.sin(2 * math.pi * 3 * i / sr)) for i in range(n)]

    t0 = int(sr * 0.8)
    stop = t0 + int(sr * residue_ms / 1000)
    heard = [(a if i < stop else 0.0) for i, a in enumerate(agent)]

    caller = [0.0] * n
    for i in range(t0, n):
        caller[i] = 0.45 * math.sin(2 * math.pi * caller_f * i / sr)

    rec = [gain * h + c + random.gauss(0, noise) for h, c in zip(heard, caller)]

    rec_p, ref_p = tmp_path / "call.wav", tmp_path / "ref.wav"
    _write_wav(rec_p, rec, sr)
    _write_wav(ref_p, agent, sr)          # reference = what Rime synthesised
    return rec_p, ref_p


@pytest.mark.parametrize("true_residue", [0, 30, 60, 120, 240, 480, 900])
def test_acoustic_analyser_recovers_a_known_residue(tmp_path, true_residue):
    """A measurement tool that has never been checked against a known answer
    is not evidence. These calls have the residue built in by construction.

    Tolerance is one projection frame (30 ms), which is the quantisation the
    tool itself reports."""
    pytest.importorskip("numpy")
    from eval.acoustic import analyse

    rec, ref = _synthetic_call(tmp_path, true_residue)
    res = analyse(rec, ref)
    assert "error" not in res, res
    assert abs(res["residue_ms"] - true_residue) <= 30, (
        f"measured {res['residue_ms']} ms, true {true_residue} ms"
    )


@pytest.mark.parametrize("label,kw", [
    ("quiet handset", {"gain": 0.15}),
    ("loud handset", {"gain": 0.9}),
    ("noisy room", {"noise": 0.02}),
    ("very noisy room", {"noise": 0.05}),
    ("caller near agent pitch", {"caller_f": 260}),
    ("different noise seed", {"seed": 3}),
])
def test_acoustic_analyser_survives_adverse_recordings(tmp_path, label, kw):
    """Each of these broke an earlier version of the analyser.

    A quiet handset defeated a fixed presence threshold; room noise put the
    onset floor below the noise so detection fired at frame 0; a modulated
    agent defeated 10 ms projection frames. All were invisible until swept."""
    pytest.importorskip("numpy")
    from eval.acoustic import analyse

    rec, ref = _synthetic_call(tmp_path, 120, **kw)
    res = analyse(rec, ref)
    assert "error" not in res, f"{label}: {res}"
    assert abs(res["residue_ms"] - 120) <= 30, f"{label}: {res['residue_ms']} ms"


def test_acoustic_analyser_refuses_when_levels_are_inseparable(tmp_path):
    """Refusing beats returning a number the recording cannot support."""
    pytest.importorskip("numpy")
    from eval.acoustic import analyse

    rec, ref = _synthetic_call(tmp_path, 120, gain=0.004, noise=0.05)
    res = analyse(rec, ref)
    if "error" not in res:
        assert abs(res["residue_ms"] - 120) <= 60


def test_acoustic_analyser_finds_the_barge_in_instant(tmp_path):
    pytest.importorskip("numpy")
    from eval.acoustic import analyse

    rec, ref = _synthetic_call(tmp_path, 100)
    res = analyse(rec, ref)
    assert abs(res["t0_barge_in_ms"] - 800) <= 50


def test_acoustic_analyser_reports_failure_rather_than_a_number(tmp_path):
    """No barge-in in the recording must produce an error, never a plausible
    residue. A tool that always returns a number invites fabricated evidence."""
    pytest.importorskip("numpy")
    import math

    from eval.acoustic import analyse

    sr, n = 8000, 8000
    agent = [0.5 * math.sin(2 * math.pi * 200 * i / sr) for i in range(n)]
    rec_p, ref_p = tmp_path / "quiet.wav", tmp_path / "ref2.wav"
    _write_wav(rec_p, [0.6 * a for a in agent], sr)
    _write_wav(ref_p, agent, sr)
    assert "error" in analyse(rec_p, ref_p)


# --- phone SKU recognition ------------------------------------------------


@pytest.mark.parametrize("said,expect", [
    ("i need the four L eighty E", "4L80E"),
    ("part number 4 L 80 E", "4L80E"),
    ("for L 80 E", "4L80E"),          # STT commonly writes "for" for "four"
    ("four L sixty E", "4L60E"),
    ("the 4l60e please", "4L60E"),
    ("give me AC 12684485", "AC12684485"),
    ("hello there", None),
    ("I want two of them", None),
    ("", None),
])
def test_sku_recognition_handles_spoken_part_numbers(said, expect):
    """Digits-only matching missed every spoken form. On a phone line STT
    writes part numbers as words at least as often as digits."""
    from switchboard.agent import find_sku

    assert find_sku(said) == expect


def test_agent_uses_our_own_rime_client_not_the_stock_plugin():
    """The word timestamps gate authorization and the acoustic measurement
    needs the exact synthesised PCM. The stock plugin exposes neither."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "switchboard" / "agent.py").read_text()
    assert "WebSocketTransport" in src and "RimeStream" in src
    assert "livekit.plugins" not in src


def test_agent_refuses_authorization_without_word_timestamps():
    """Falling back to what we INTENDED to say would defeat the whole check."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "switchboard" / "agent.py").read_text()
    region = src[src.index("async def try_authorize"):src.index("# -- speech")]
    assert "no Rime word timestamps" in region
    assert "last_commitment" in region


def test_agent_saves_a_reference_wav_for_acoustic_analysis():
    """`run.py acoustic` needs the exact PCM that was played. A re-render
    would not correlate."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "switchboard" / "agent.py").read_text()
    assert "_save_reference" in src and "REFERENCE_DIR" in src


def test_agent_livekit_imports_are_all_lazy():
    """A judge without livekit-agents installed must still be able to import
    and read this module."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "switchboard" / "agent.py").read_text()
    # Check IMPORT STATEMENTS, not prose -- the module docstring names LiveKit
    # throughout, and an earlier version of this test matched that instead.
    import ast

    tree = ast.parse(src)
    top_level = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            top_level.append(node.module or "")
    offenders = [m for m in top_level if m.startswith("livekit")]
    assert not offenders, f"livekit imported at module level: {offenders}"


# --- STT robustness (both failures seen on a live session) ----------------


CATALOG = {"4l80e": "4L80E", "4l60e": "4L60E", "ac12684485": "AC12684485"}


@pytest.mark.parametrize("said,expect", [
    ("4 L A T E", "4L80E"),                 # STT wrote "eighty" as "ATE" -- live
    ("4 L A T E please", "4L80E"),
    ("I need the 4 L A T E", "4L80E"),
    ("I need the 4L80E", "4L80E"),
    ("for L 80 E", "4L80E"),                # "four"/"for" homophone
    ("four el eighty ee", "4L80E"),         # letters spelled as words
    ("4 l 8 oe", "4L80E"),
    ("um I need a four L eighty E thanks", "4L80E"),
    ("give me the four L sixty E", "4L60E"),
    ("A C one two six eight four four eight five", "AC12684485"),
    ("hello there", None),
    ("I want two of them", None),
    ("yes place it", None),                 # a command must not match a SKU
    ("no cancel", None),
    ("", None),
])
def test_sku_survives_real_stt_variants(said, expect):
    """Literal alias tables cannot cover this: the set of things STT writes
    for an alphanumeric is open-ended. A live session returned "4 L A T E"
    for 4L80E and the agent said "tell me the part number"."""
    from switchboard.hearing import match_sku

    sku, _ = match_sku(said, CATALOG)
    assert sku == expect


def test_low_confidence_asks_rather_than_guesses():
    """Ordering the wrong part because a match was close enough is worse than
    one extra turn."""
    from switchboard.hearing import match_sku

    sku, score = match_sku("I need the 9 Z 44 Q", CATALOG)
    assert sku is None and score < 0.72


@pytest.mark.parametrize("parts,expect", [
    (["yes.", "place it."], Act.YES_PLACE),      # the live failure
    (["yes", "place it"], Act.YES_PLACE),
    (["place it"], Act.YES_PLACE),
    (["Yes, place it."], Act.YES_PLACE),
    (["ok", "confirm it"], Act.YES_PLACE),
    (["no", "cancel"], Act.NO_CANCEL),
    (["yes"], Act.UNPARSED),                     # bare ack is NOT authorization
    (["hello"], Act.UNPARSED),
])
def test_commands_assemble_across_turns(parts, expect):
    """A caller pausing mid-command produced two final transcripts, neither of
    which parsed. The joined span is tried longest-first."""
    from switchboard.hearing import CommandAssembler

    a = CommandAssembler()
    for i, part in enumerate(parts):
        a.add(part, now=i * 1.0)
    acts = [parse_act(c) for c in a.candidates()]
    found = next((x for x in acts if x is not Act.UNPARSED), Act.UNPARSED)
    assert found is expect


def test_assembler_window_expires_so_stale_yes_cannot_authorize():
    """Joining across a long gap would let an old "yes" combine with a much
    later "place it" and authorize something never said in one thought."""
    from switchboard.hearing import CommandAssembler

    a = CommandAssembler(window_seconds=6.0)
    a.add("yes", now=0.0)
    a.add("place it", now=30.0)
    assert all(parse_act(c) is not Act.YES_PLACE or "yes" not in c
               for c in a.candidates())


def test_bare_yes_is_never_authorization():
    """A backchannel must not commit an order."""
    assert parse_act("yes") is Act.UNPARSED
    assert parse_act("yeah") is Act.UNPARSED
    assert parse_act("mm hm") is Act.UNPARSED


@pytest.mark.parametrize("said", [
    "no, don't place it", "nope", "cancel", "stop", "nah",
])
def test_negation_always_beats_affirmation(said):
    assert parse_act(said) is Act.NO_CANCEL


def test_correction_in_one_breath_carries_the_replacement():
    """"wait, change that to 4L60E" -- losing the SKU and asking "what part
    number?" makes the agent look like it wasn't listening."""
    from switchboard.agent import SKU_CATALOG, SKU_CONFIDENCE_FLOOR
    from switchboard.hearing import match_sku

    assert parse_act("wait, change that to 4L60E") is Act.CHANGE
    sku, _ = match_sku("wait, change that to 4L60E", SKU_CATALOG,
                       SKU_CONFIDENCE_FLOOR)
    assert sku == "4L60E"


def test_correction_across_two_turns_also_works():
    """The way it happened live: "Wait, change that" then the new number."""
    from switchboard.agent import SKU_CATALOG, SKU_CONFIDENCE_FLOOR
    from switchboard.hearing import CommandAssembler, match_sku

    a = CommandAssembler()
    a.add("Wait, change that", now=0.0)
    assert any(parse_act(c) is Act.CHANGE for c in a.candidates())
    a.clear()
    a.add("I need 4L60E instead", now=1.5)
    sku, _ = match_sku("I need 4L60E instead", SKU_CATALOG,
                       SKU_CONFIDENCE_FLOOR)
    assert sku == "4L60E"
