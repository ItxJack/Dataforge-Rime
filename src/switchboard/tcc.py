"""
TCC participant.

TCC is a protocol shape, not a safety guarantee. The guarantee comes from the
participant: idempotent Try/Confirm/Cancel, empty rollback, anti-suspension
(a late Try after a Cancel must not resurrect the branch), expiry that fails
closed, and version fencing against the PARTICIPANT's own version -- not the
application's tx_version, which the downstream system has never heard of.

The invariant is NOT "Try has no side effects". Oracle's own TCC documentation
notes that Try legitimately mutates local state: decrementing available
inventory, writing a reservation record. The correct invariant is:

    Try produces no irreversible effect and no downstream automation
    outside the declared reservation contract.

FORBIDDEN_TRY_EFFECTS below is that contract, and it is asserted in tests.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

PERMITTED_TRY_EFFECTS = frozenset({"atp_decrement", "reservation_record"})
FORBIDDEN_TRY_EFFECTS = frozenset(
    {
        "reorder_trigger",
        "purchase_order_suggestion",
        "pick_queue_enqueue",
        "customer_notification",
        "pricing_lock",
        "label_print",
    }
)


class BranchState(Enum):
    NEW = "NEW"
    TRY_SUCCEEDED = "TRY_SUCCEEDED"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class TccError(Exception):
    pass


class IllegalTransition(TccError):
    pass


class ReservationExpired(TccError):
    pass


class VersionConflict(TccError):
    pass


@dataclass
class Branch:
    reservation_id: str
    tx_id: str
    sku: str
    qty: int
    state: BranchState
    version: int
    expires_at: float
    effects: list[str] = field(default_factory=list)


class Participant:
    """Durable-ish reservation store. In the demo this is the mock ERP; in
    production it is an adapter that REFUSES the light path unless the real
    backend can satisfy this contract. A TCC wrapper around a non-TCC backend
    is theatre, and the adapter is where that gets caught."""

    def __init__(self, ttl_seconds: float = 180.0, clock=time.monotonic):
        self.ttl = ttl_seconds
        self.clock = clock
        self.branches: dict[str, Branch] = {}
        # Anti-suspension: a tombstone so a delayed Try cannot resurrect a
        # branch that was already cancelled.
        self.tombstones: set[str] = set()
        self.fulfilled: list[str] = []
        self.emitted_effects: list[str] = []

    # -- phases -----------------------------------------------------------

    def try_reserve(
        self, tx_id: str, sku: str, qty: int, reservation_id: Optional[str] = None
    ) -> Branch:
        rid = reservation_id or f"r-{uuid.uuid4().hex[:10]}"

        if rid in self.tombstones:
            raise IllegalTransition("late Try after Cancel (anti-suspension)")

        existing = self.branches.get(rid)
        if existing is not None:
            # Idempotent Try.
            if existing.state is BranchState.TRY_SUCCEEDED:
                return existing
            raise IllegalTransition(f"Try on branch in {existing.state.value}")

        branch = Branch(
            reservation_id=rid,
            tx_id=tx_id,
            sku=sku,
            qty=qty,
            state=BranchState.TRY_SUCCEEDED,
            version=1,
            expires_at=self.clock() + self.ttl,
            effects=["atp_decrement", "reservation_record"],
        )
        self._emit(branch.effects)
        self.branches[rid] = branch
        return branch

    def confirm(self, reservation_id: str, tx_id: str, expected_version: int) -> Branch:
        """tx_id is REQUIRED. Without it a buggy or racing client can confirm
        another transaction's reservation with a correct version number -- a
        genuine isolation bug, not a theoretical one."""
        b = self.branches.get(reservation_id)
        if b is None:
            if reservation_id in self.tombstones:
                raise IllegalTransition("Confirm after Cancel")
            raise IllegalTransition("Confirm on unknown reservation")

        if b.tx_id != tx_id:
            raise IllegalTransition(
                f"reservation belongs to {b.tx_id}, not {tx_id}"
            )

        if b.state is BranchState.CONFIRMED:
            return b  # idempotent: no duplicate fulfilment

        if b.state is BranchState.CANCELLED:
            raise IllegalTransition("Confirm after Cancel")

        if self._expired(b):
            b.state = BranchState.EXPIRED
            raise ReservationExpired(
                "reservation expired before Confirm; never silently recreate"
            )

        if b.version != expected_version:
            raise VersionConflict(
                f"participant version {b.version} != expected {expected_version}"
            )

        b.state = BranchState.CONFIRMED
        b.version += 1
        self.fulfilled.append(reservation_id)
        self._emit(["pick_queue_enqueue"])  # legal ONLY at Confirm
        return b

    def cancel(self, reservation_id: str) -> Branch | None:
        b = self.branches.get(reservation_id)
        self.tombstones.add(reservation_id)  # order-independent

        if b is None:
            return None  # empty rollback: Cancel arriving before Try

        if b.state is BranchState.CANCELLED:
            return b  # idempotent

        if b.state is BranchState.CONFIRMED:
            raise IllegalTransition("Cancel after Confirm")

        b.state = BranchState.CANCELLED
        b.version += 1
        return b

    # -- internals --------------------------------------------------------

    def _expired(self, b: Branch) -> bool:
        return self.clock() >= b.expires_at

    def _emit(self, effects: list[str]) -> None:
        for e in effects:
            if e in FORBIDDEN_TRY_EFFECTS and e != "pick_queue_enqueue":
                raise AssertionError(f"forbidden Try effect: {e}")
            self.emitted_effects.append(e)

    def try_phase_effects(self) -> set[str]:
        """Everything emitted before any Confirm. Asserted against the
        contract in tests."""
        out: set[str] = set()
        for e in self.emitted_effects:
            if e == "pick_queue_enqueue":
                break
            out.add(e)
        return out

    def query(self, reservation_id: str) -> Optional[BranchState]:
        """Query-then-reconcile. After a Confirm timeout the coordinator does
        NOT blindly retry -- it asks. The classic distributed failure is a
        participant that committed and a coordinator that never heard so."""
        b = self.branches.get(reservation_id)
        if b is None:
            return BranchState.CANCELLED if reservation_id in self.tombstones else None
        return b.state

    def externally_pick(self, reservation_id: str) -> None:
        """Simulates the warehouse acting outside the participant -- the race
        the whole design exists to make impossible. Bumps the participant
        version so a Confirm carrying a stale version is fenced out."""
        b = self.branches[reservation_id]
        b.version += 1
