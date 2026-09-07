"""What a hosted store agent can actually OBSERVE about its own record, and what it cannot.

This module is small because the honest answer is small, and writing it down is most of its
value: every plan that reached this loop assumed a win/loss feed that does not exist.

WHAT A STORE AGENT CANNOT SEE
------------------------------
**Award.** Whether this store won the shortlist slot is not observable from inside the agent.

* The published contract declares exactly two store-agent operations —
  ``packages/contracts/openapi/store-agent.openapi.json`` -> ``POST /v1/bid-requests`` and
  ``POST /v1/trust-events`` — and ``contracts.PINNED_ROUTES`` is compared against the documents
  in BOTH directions, so a third door is a contract change and not a wiring one.
* ``contracts.LossReport`` exists as a type (R9's aggregated, delayed loss report) and is served
  by no route in this repository.
* The award itself is the ledger's ``accepted`` event, and an ``accepted`` carries no ``dim`` and
  no ``type``, so ``trust.ledger.replay.observations_from_events`` projects nothing from it and
  ``trust.feedback.deltas.delta_for_event`` returns ``None``. That function's own docstring names
  ``accepted`` in the list of events for which "there is nothing to tell a store about". No push
  is made, so nothing arrives.

So the arm-selection half of this loop is built against the signal that IS reachable, and the
gap is stated rather than papered over with a synthesized feed.

WHAT A STORE AGENT CAN SEE
---------------------------
**The trust system's verdict on an auction it bid in**, pushed to ``POST /v1/trust-events`` by
``trust.feedback.notify`` and taken in by ``AgentRunner.ingest_trust_event``. The payload carries
the whole ``LedgerEvent``, **including its ``auction_id``**, and a signed ``delta``. The runner
is the one object holding both halves of the join — the arm it played in auction A, and the
verdict that later names auction A — so the join key is ``auction_id`` and that is the loop.

**How much of a win/loss record that is.** A ``feedback`` event (R14's "did it match the pitch?")
only exists for a buyer the network routed and who then bought, so a positive ``feedback_match``
delta on auction A is a purchase that matched the pitch played in A and a negative one is a
purchase that did not. That is a genuine outcome on the arm, and it is the one R17 cares about —
"a store agent updates its own bid policy from its own outcomes". What it is not is a record of
the auctions this store lost silently, and no arithmetic here can invent those.

**A zero delta is not a loss.** An observation that moved no dimension is not evidence about the
arm that was played, and counting it against the arm would punish an emphasis for a non-event.
Zero-delta events are ingested into the posture (that is the trust door's business) and
contribute no outcome row.
"""

from __future__ import annotations

from typing import Any

from .arms import Arm

__all__ = ["OUTCOME_SOURCE", "outcome_row", "verdict"]

#: Stamped on every row this module produces, so a reader of a fold can tell an outcome the agent
#: OBSERVED from one a composition root with real award data injected. There is exactly one
#: observable source today and naming it is what makes a second one visible if it ever appears.
OUTCOME_SOURCE = "trust_event"


def verdict(delta: Any) -> bool | None:
    """``True`` for a win, ``False`` for a loss, ``None`` when the event says nothing.

    ``None`` is not "no", and the distinction is the whole reason this is a tri-state: a delta of
    exactly zero moved no dimension, so it is silence about the arm rather than evidence against
    it. A non-numeric or non-finite delta is silence for the same reason — `AgentRunner._accept`
    already refuses a non-finite one outright, and this is the second, independent refusal that
    keeps a `nan` from being read as a loss if that guard is ever moved.
    """
    try:
        number = float(delta)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number == 0.0:
        return None
    return number > 0.0


def outcome_row(arm: Arm, *, store_id: str, won: bool) -> dict[str, Any]:
    """One outcome row for :func:`store_agent.learning.state.update`, in this store's name.

    Every field the fold reads is stated explicitly, in the unit the fold declares for it:
    ``discount_depth`` is a FRACTION (the arm holds fractions; the percent crossing happens once,
    in :func:`~store_agent.learning.grid.as_percent`), ``commitments`` is the set the advocate
    actually led with, and ``pitch_variant`` is the emphasis it actually played.

    ``store_id`` is this runner's own, never the event's. A bound state drops foreign rows, and
    handing it a row named after whatever arrived on the wire would make that seal decide nothing
    — the runner has already refused a misrouted event by the time this is reached, and the seal
    here is the second lock on the same door.
    """
    return {
        "store_id": str(store_id),
        "cluster_id": arm.cluster_id,
        "won": bool(won),
        "discount_depth": float(arm.depth),
        "commitments": list(arm.commitment_set),
        "pitch_variant": arm.pitch_variant,
        "source": OUTCOME_SOURCE,
    }
