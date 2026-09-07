"""R14's per-observation weight, applied where the evidence for it actually lives.

:mod:`.engine` computes the weight and :mod:`trust.scoring` consumes it. Between them there
was nothing: ``accept_feedback`` and ``feedback_observation`` implement R14's two clauses —
*weighted by buyer track record, cross-checked against return behavior* — and, measured on
this tree, the only callers of either were tests. A search for a non-test caller of
``accept_feedback`` outside ``apps/trust`` and ``.swarm-loop/acceptance`` returned nothing.

**Why the caller has to be here and could not be the buyer.** The buyer service emits the
``feedback`` ledger event and deliberately emits no ``weight``; its own
``feedback_payload`` says why in as many words — "the buyer service can see neither: it
holds no return record and no cross-order history of a pseudonym". So a weight invented
there would be a guess. The trust service, folding that event into an observation, is
holding the one record that decides R14's cross-check: the chain's own ``refund`` events.

What this module is, exactly
----------------------------
One function, :func:`fold_feedback`, which turns one ``feedback`` ledger event plus the
answer to "had this order already been returned when this feedback landed?" into the two
fields the trust projection needs: the observation ``type`` and its ``weight``. Both callers
go through it, so there is ONE rule:

* :func:`trust.ledger.replay.observations_from_events` — the projection every replay, every
  ``GET /events/replay?snapshots=true`` and the ``POST /events`` poison screen run; and
* :func:`trust.events.observations.persist_event_observations` — the row write that makes a
  folded observation visible to ``GET /snapshot``, the door the exchange reads.

They must agree to the bit, because R15/S3 is "recomputing all trust scores from the ledger
reproduces the served scores exactly" and those two are the served score and the
recomputation. A weight applied on one side only is exactly the divergence S3 exists to
catch, so it is applied in one place and read by both.

THE ROUTED-BUYER GATE IS NOT ENFORCED HERE, and saying so is the point
----------------------------------------------------------------------
:func:`~.engine.accept_feedback` carries R14's routed-buyer gate as well as its weight, and
the ``routed_orders`` map this module hands it holds exactly the one order the event names —
so the gate always admits. That is deliberate and it is not the gate going missing: the gate
is enforced by the *buyer* service, which refuses feedback for an order it did not route
with a ``403`` before any ledger event exists (``apps/buyer/svc/src/feedback/routes.py``).
By the time a ``feedback`` event is in an append-only hash chain the question "should this
have been accepted?" is already answered and unappealable; what is still open, and what this
module answers, is what the report is WORTH.

Re-deriving the gate here would also be unsound rather than merely redundant. The trust
service holds no routing table — ``accepted`` events carry a ``checkout_token`` and no
``order_ref`` (``contracts.LEDGER_PAYLOAD_SHAPES``), so "was this order routed?" is the
reconciliation join, not a lookup — and a second gate that guessed would silently discard
real buyer reports on the strength of an unfinished join.

The known limit, stated rather than papered over
------------------------------------------------
The cross-check reads the chain **as it stood when the feedback landed**. A return recorded
*after* the report does not retroactively rewrite the sealed observation. That is forced,
not chosen: the ledger is append-only under a ``BEFORE UPDATE OR DELETE ... ENABLE ALWAYS``
trigger, and a weight that depended on events after its own is not replayable — the same
stream would fold to two different numbers depending on when it was folded, which is
precisely what S3 forbids. The later return is not lost; it is its own evidence on
``not_returned``, through its own producer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .engine import BASE_FEEDBACK_WEIGHT, accept_feedback, feedback_observation

__all__ = [
    "ANSWER_FIELDS",
    "FEEDBACK_KIND",
    "RETURN_KIND",
    "fold_feedback",
    "order_identity",
    "states_an_answer",
]

#: The ledger kind this module folds. One kind, because R14's weight is about a *buyer's
#: report* and nothing else in the frozen vocabulary is one.
FEEDBACK_KIND = "feedback"

#: The ledger kind that records a return. The frozen ``LedgerEvent`` vocabulary (C11/D24)
#: has exactly one, and its published body is ``(order_ref, amount, reason)`` — so an order
#: with a ``refund`` in the chain is an order the buyer sent back, which is the behaviour
#: R14 says a positive report is cross-checked against.
RETURN_KIND = "refund"


#: Where a ``feedback`` payload states the buyer's answer. ``matched_pitch`` is the published
#: key (``contracts.LEDGER_PAYLOAD_SHAPES["feedback"]``); ``answer`` is the spelling
#: :func:`~.engine._is_positive` also reads.
ANSWER_FIELDS: tuple[str, ...] = ("matched_pitch", "answer")


def states_an_answer(payload: Any) -> bool:
    """Whether a ``feedback`` payload actually says what the buyer reported.

    **This is the guard that keeps silence from becoming a complaint.**
    :func:`~.engine._is_positive` answers ``False`` for a payload naming neither field, which
    is the right default for a report that IS present and unparseable — but read as a derived
    observation type it turns "this event says nothing" into ``mismatch_return``, a published
    1.5 negative, against a real store. So the derived type is offered only for a payload that
    states something; one that does not keeps the pre-existing behaviour of projecting no
    observation at all.
    """
    if not isinstance(payload, Mapping):
        return False
    for field in ANSWER_FIELDS:
        value = payload.get(field)
        if isinstance(value, bool) or (value is not None and str(value).strip()):
            return True
    return False


def order_identity(event: Any) -> tuple[str, str] | None:
    """``(store_id, order_ref)`` for one event, or ``None`` when it names neither.

    Scoped by store, and that is not decoration: a Shopify ``order_id`` is a PER-SHOP
    number, so ``o-1001`` alone names one order at every shop in the network at once.
    ``trust.reconcile.engine`` namespaces every join key for the same reason, and an
    unscoped return here would let one shop's refund discount another shop's feedback.

    Both halves are read top-level first and from the payload second, exactly as
    ``observations_from_events`` reads ``store_id``: the ledger carries them as columns, and
    a producer that put them only in the body still meant them.
    """
    if not isinstance(event, Mapping):
        return None
    payload = event.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    store_id = event.get("store_id") or payload.get("store_id")
    order_ref = event.get("order_ref") or payload.get("order_ref")
    if store_id is None or order_ref is None:
        return None
    return str(store_id), str(order_ref)


def fold_feedback(event: Any, *, returned: bool) -> dict[str, Any] | None:
    """The observation ``type`` and ``weight`` one ``feedback`` event is worth.

    Args:
        event: the ``feedback`` ledger event, as it is stored. Any other kind returns
            ``None`` — this is R14's rule and it is not a general-purpose weighting hook.
        returned: whether the chain already recorded a return for this order when this
            event landed. The caller answers it, because the two callers have different
            (and equally valid) ways of looking: the replay walks the stream it was handed,
            and the append path asks the database for a ``refund`` before this event's own
            ``seq``.

    Returns:
        ``{"type": str | None, "weight": float}``, or ``None`` when the event is not a
        ``feedback`` event or carries no store to attribute the report to.

        ``type`` is what :func:`~.engine.feedback_observation` derives from the buyer's own
        answer — ``fulfilled`` for "it matched the pitch", ``mismatch_return`` for "it did
        not" — and ``None`` for a payload that states no answer at all
        (:func:`states_an_answer`). The CALLER decides whether to use it: the emitter's own
        ``type`` wins when the event names one, because the translation belongs to the
        emitter (the rule ``trust.reconcile.engine.observation_events`` and the buyer's own
        ``feedback_payload`` both state), and this value is the fallback that makes a
        ``feedback`` event carrying ``matched_pitch`` and no ``type`` foldable at all
        instead of silently invisible.

        ``weight`` is ``1.0`` for an uncontradicted report and
        ``RETURN_CONTRADICTION_FACTOR`` of it for a positive report the buyer's own return
        contradicts. Downweighted and never discarded — :mod:`.engine` argues that at
        length: discarding it would hand a store a way to erase inconvenient feedback by
        provoking a return.

        No buyer track record is applied, and that is an honest absence rather than a
        forgotten multiplier. R5 keeps the buyer's pseudonym off the ledger entirely — the
        published ``feedback`` body is ``(matched_pitch, reason)`` and the event carries
        ``store_id`` and ``order_ref`` and no buyer at all — so this service cannot count a
        pseudonym's past reports without the identity R5 exists to withhold. ``None`` is
        what :func:`~.engine.accept_feedback` documents for that case: "omitted, every
        routed buyer weighs the same".
    """
    if not isinstance(event, Mapping):
        return None
    if str(event.get("kind") or "") != FEEDBACK_KIND:
        return None
    payload = event.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    store_id = event.get("store_id") or payload.get("store_id")
    if store_id is None:
        # A trust observation is a statement ABOUT a store. `observations_from_events` drops
        # such an event for the same reason, so weighting one would be arithmetic on a report
        # that reaches no Beta.
        return None

    order_ref = event.get("order_ref") or payload.get("order_ref")
    key = str(order_ref)
    verdict = accept_feedback(
        key,
        payload,
        routed_orders={key: {"store_id": str(store_id), "returned": bool(returned)}},
        # R5: see the Returns note above. Not a default that was never revisited.
        buyer_track_record=None,
    )
    observation = feedback_observation(
        verdict, observed_at=payload.get("observed_at") or event.get("ts")
    )
    if observation is None:  # pragma: no cover - the map above always admits its own order
        return None
    return {
        "type": str(observation["type"]) if states_an_answer(payload) else None,
        "weight": float(observation.get("weight", BASE_FEEDBACK_WEIGHT)),
    }
