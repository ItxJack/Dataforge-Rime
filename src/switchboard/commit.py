"""
Commit escrow.

THE RACE THIS CLOSES. The caller says "yes, place it". The grammar parses it
deterministically and we fire Confirm. Fifty milliseconds later the caller
shouts "wait, wrong part". The interruption path works perfectly -- and it does
not matter, because the Confirm has already left the NIC. You cannot recall an
in-flight HTTP POST. The ERP commits, the warehouse picks, and the agent says
"stopping, what would you like to change?"

So dispatch is held locally for a short window after the parse. Inside the
window an abort is free. Outside it, the only remedy is a Cancel racing a
Confirm -- which is exactly why the participant's cancel must be idempotent and
order-independent, and why a Cancel arriving first leaves a tombstone that
rejects the later Confirm.

The window is NOT free: it is added to the time before the caller hears their
confirmation. 500 ms sits below the threshold at which a pause reads as the
system having failed, and it is the cheapest possible insurance against a race
whose failure mode is a physical part on a truck.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional

ESCROW_MS = 500


class Outcome(Enum):
    COMMITTED = "committed"
    ABORTED_IN_ESCROW = "aborted_in_escrow"
    CANCELLED_AFTER_DISPATCH = "cancelled_after_dispatch"
    RECONCILED = "reconciled"


@dataclass
class EscrowResult:
    outcome: Outcome
    dispatched: bool
    detail: str = ""


class CommitEscrow:
    def __init__(self, escrow_ms: int = ESCROW_MS, sleep=asyncio.sleep):
        self.escrow_ms = escrow_ms
        self._sleep = sleep
        self._abort = asyncio.Event()
        self.dispatched = False

    def abort(self) -> None:
        """Called by the turn controller the instant a barge-in is classified.
        Free if we are still inside the window."""
        self._abort.set()

    async def dispatch(
        self,
        confirm: Callable[[], Awaitable],
        cancel: Callable[[], Awaitable],
        query: Optional[Callable[[], Awaitable]] = None,
    ) -> EscrowResult:
        try:
            await asyncio.wait_for(
                self._abort.wait(), timeout=self.escrow_ms / 1000.0
            )
            return EscrowResult(Outcome.ABORTED_IN_ESCROW, dispatched=False,
                                detail="barge-in inside the escrow window")
        except asyncio.TimeoutError:
            pass

        self.dispatched = True
        try:
            await confirm()
        except Exception as exc:  # noqa: BLE001
            # Confirm timed out. Query-then-reconcile: never blindly retry, or
            # a participant that already committed gets a duplicate.
            if query is not None:
                state = await query()
                return EscrowResult(Outcome.RECONCILED, dispatched=True,
                                    detail=f"confirm raised {exc!r}; state={state}")
            raise

        if self._abort.is_set():
            # Barge-in landed after dispatch. Cancel races the Confirm; the
            # participant's tombstone is what makes the ordering irrelevant.
            await cancel()
            return EscrowResult(Outcome.CANCELLED_AFTER_DISPATCH, dispatched=True,
                                detail="cancel issued after dispatch")

        return EscrowResult(Outcome.COMMITTED, dispatched=True)
