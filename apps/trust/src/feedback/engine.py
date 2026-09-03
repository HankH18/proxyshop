"""Push trust deltas to the affected store, and take feedback only from routed buyers.

Two halves of R13/R14, deliberately in one module because they are two ends of the same
loop: the network learns something about a store, tells that store, and separately lets the
buyer who actually transacted say whether the pitch matched what arrived.

The push (R13/R5)
-----------------
:func:`push_trust_event` sends **once**, to **the affected store only**, carrying the full
originating event so the store can act on a concrete thing rather than an unexplained score
move — and carrying no buyer identity at all. "Once, to one store" is not a detail: a
broadcast is a leak of one store's trust movement to its competitors, and a duplicate send is
a second penalty for the same event once the receiving agent starts counting.

The sink is duck-typed (anything with ``send(store_id, payload)``). That keeps this module
free of any dependency on the store-agent package, which is what lets it be tested against a
recording double and lets the real intake be swapped without touching trust.

The feedback gate (R14)
-----------------------
Only a buyer the network actually routed may leave feedback about the store it routed them
to. Without that gate a store can astroturf its own ``feedback_match`` dimension for the price
of a few fake reviews, and the dimension stops meaning anything.

Feedback that survives the gate is **weighted, not simply counted**:

* a positive report from a buyer who *returned* the item is contradicted by their own
  behaviour, so it is downweighted — not discarded. Discarding it would throw away a real
  signal (the return itself is evidence) and would hand a store a way to erase inconvenient
  feedback by triggering a return;
* a buyer's own track record scales their weight, so a single account cannot outvote the
  network by leaving a lot of feedback.

No score math here either: this module produces a *weight*, and :mod:`trust.scoring` decides
what a weighted observation does to a dimension.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .scrub import scrub, scrub_report

__all__ = [
    "BASE_FEEDBACK_WEIGHT",
    "RETURN_CONTRADICTION_FACTOR",
    "TRUST_EVENT_SCHEMA_VERSION",
    "FeedbackRejected",
    "accept_feedback",
    "push_trust_event",
    "trust_event_payload",
]

#: The payload shape the store-agent intake is coded against. Bumped when a field's meaning
#: changes, so a receiving agent can refuse a payload it does not understand instead of
#: silently reading a field that no longer means what it did.
TRUST_EVENT_SCHEMA_VERSION = "trust-event-1.0.0"

#: The weight of one accepted, uncontradicted piece of feedback.
BASE_FEEDBACK_WEIGHT = 1.0

#: What positive feedback is worth when the same buyer returned the item. Strictly between 0
#: and 1: downweighted, never discarded — see the module docstring.
RETURN_CONTRADICTION_FACTOR = 0.25


class FeedbackRejected(ValueError):
    """Raised only by the strict entry points. :func:`accept_feedback` returns a verdict."""


def _field(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _pseudonymous_context(delta: Any, event: Any, redacted_fields: int) -> dict[str, Any]:
    """What the store agent may know about the counterparty: a pseudonym, and nothing else.

    The count of redacted fields is included and their NAMES are not — see
    :func:`trust.feedback.scrub.scrub_report`. ``redacted_fields`` lets the receiving agent
    (and an auditor) see that a scrub happened without learning what it removed.
    """
    payload = _field(event, "payload")
    payload = payload if isinstance(payload, Mapping) else {}
    pseudonym = (
        _field(delta, "buyer_pseudonym")
        or payload.get("pseudonym")
        or payload.get("buyer_pseudonym")
        or _field(event, "pseudonym")
    )
    return {
        "pseudonym": str(pseudonym) if pseudonym is not None else None,
        "order_ref": _field(event, "order_ref") or _field(delta, "order_ref"),
        "identity_disclosed": False,
        "redacted_fields": redacted_fields,
        "policy": "R13/R5: the affected store learns what moved and never who moved it.",
    }


def trust_event_payload(delta: Any) -> dict[str, Any]:
    """The pseudonymous payload one trust delta becomes on the wire.

    Args:
        delta: ``{store_id, dim, delta, event}`` — the dimension that moved, by how much, and
            the originating ledger event.

    Returns:
        A plain mapping carrying ``store_id``, ``dim``, ``delta``, the **full** scrubbed
        ``event`` (``event_id`` and ``kind`` preserved: an unexplained score move is one a
        store cannot act on and will not trust) and a non-``None``
        ``pseudonymous_context``.

    Raises:
        FeedbackRejected: the delta names no store. A trust event with no addressee has
            nowhere to go, and broadcasting it would leak one store's movement to every other.
    """
    store_id = _field(delta, "store_id")
    event = _field(delta, "event")
    if store_id is None and event is not None:
        store_id = _field(event, "store_id")
    if store_id is None:
        raise FeedbackRejected(
            "a trust delta names no store_id, so there is no affected store to push it to. "
            "R13 addresses the affected store and only the affected store."
        )

    scrubbed_event, redacted = scrub_report(event) if event is not None else (None, 0)
    return {
        "schema_version": TRUST_EVENT_SCHEMA_VERSION,
        "store_id": str(store_id),
        "dim": _field(delta, "dim"),
        "delta": float(_field(delta, "delta", 0.0) or 0.0),
        "event": scrubbed_event,
        "pseudonymous_context": _pseudonymous_context(delta, event, redacted),
        "reason_code": scrub(_field(delta, "reason_code")),
    }


def push_trust_event(delta: Any, sink: Any) -> dict[str, Any]:
    """Push one trust delta to the affected store's intake. Exactly once, to exactly one.

    Args:
        delta: see :func:`trust_event_payload`.
        sink: anything exposing ``send(store_id, payload)`` — the store-agent intake, or a
            recording double in a test. Duck-typed on purpose: trust must not depend on the
            store-agent package to be testable.

    Returns:
        The payload that was sent, so a caller can log or ledger exactly what left.

    Raises:
        FeedbackRejected: the delta names no store.
        AttributeError: the sink cannot ``send``. Deliberately NOT swallowed — a push that
            silently does nothing is indistinguishable from a store that ignored it, and the
            store would be graded on a signal it was never told about.
    """
    payload = trust_event_payload(delta)
    sink.send(payload["store_id"], payload)
    return payload


def _is_positive(response: Any) -> bool:
    """Whether a feedback response says the delivery matched the pitch."""
    matched = _field(response, "matched_pitch")
    if isinstance(matched, bool):
        return matched
    if matched is not None:
        return str(matched).strip().lower() in {"true", "yes", "y", "1", "matched"}
    answer = str(_field(response, "answer", "")).strip().lower()
    return answer in {"matched", "yes", "true", "good", "great"}


def accept_feedback(
    order_ref: Any,
    response: Any,
    *,
    routed_orders: Mapping[str, Any],
    buyer_track_record: float | None = None,
) -> dict[str, Any]:
    """Decide whether one piece of buyer feedback counts, and for how much.

    Args:
        order_ref: the order the feedback is about.
        response: the buyer's answer — ``{matched_pitch, answer, ...}``.
        routed_orders: the orders the network actually routed, keyed by ``order_ref``. Each
            record carries at least ``store_id`` and ``returned``.
        buyer_track_record: optional multiplier in ``(0, 1]`` for a buyer whose past feedback
            has been unreliable. Omitted, every routed buyer weighs the same.

    Returns:
        ``{accepted, weight, order_ref, store_id, reasons, ...}``.

        * an order the network did not route -> ``accepted: False``, ``weight: 0.0``. The
          gate is the point of R14: without it a store buys its own ``feedback_match``
          dimension with fake reviews.
        * a routed order -> ``accepted: True`` with a positive weight.
        * a routed order whose record says ``returned: True``, answered positively -> still
          ``accepted: True``, at a strictly smaller positive weight. Downweighted, never
          discarded: the buyer's own behaviour contradicts their answer, but discarding the
          answer would let a store erase feedback by provoking a return.
    """
    key = str(order_ref)
    record = routed_orders.get(key) if isinstance(routed_orders, Mapping) else None
    if record is None:
        return {
            "accepted": False,
            "weight": 0.0,
            "order_ref": key,
            "store_id": None,
            "positive": _is_positive(response),
            "reasons": ["not_network_routed"],
        }

    reasons: list[str] = ["network_routed"]
    weight = BASE_FEEDBACK_WEIGHT
    positive = _is_positive(response)
    returned = bool(_field(record, "returned", False))

    if positive and returned:
        weight *= RETURN_CONTRADICTION_FACTOR
        reasons.append("contradicted_by_return")

    if buyer_track_record is not None:
        factor = float(buyer_track_record)
        if not 0.0 < factor <= 1.0:
            raise FeedbackRejected(
                f"buyer_track_record must be a multiplier in (0, 1], got {buyer_track_record!r}. "
                "A factor above 1 would let one account outweigh the rest of the network."
            )
        weight *= factor
        reasons.append("buyer_track_record")

    return {
        "accepted": True,
        "weight": weight,
        "order_ref": key,
        "store_id": _field(record, "store_id"),
        "buyer_pseudonym": _field(record, "buyer_pseudonym"),
        "positive": positive,
        "returned": returned,
        "reasons": reasons,
    }
