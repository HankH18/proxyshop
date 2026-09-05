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
what a weighted observation does to a dimension. :func:`feedback_observation` is the seam
between the two — one accepted verdict becomes one ``feedback_match`` observation carrying
that weight. It exists because for a while the two halves did not connect: the weight was
computed here and ``score`` read no per-observation weight at all, so both properties above
were properties of a number nothing consumed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .scrub import scrub, scrub_report

__all__ = [
    "BASE_FEEDBACK_WEIGHT",
    "FEEDBACK_DIMENSION",
    "FEEDBACK_NEGATIVE_TYPE",
    "FEEDBACK_POSITIVE_TYPE",
    "RETURN_CONTRADICTION_FACTOR",
    "TRUST_EVENT_SCHEMA_VERSION",
    "FeedbackRejected",
    "accept_feedback",
    "feedback_observation",
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

#: The one dimension buyer feedback lands on. The approved manifest is explicit about why it
#: is only ever this one: ``feedback_match`` "takes NO verification outcome at all. It is the
#: post-purchase, buyer-reported match between pitch and delivery (R14), cross-checked against
#: return behaviour."
FEEDBACK_DIMENSION = "feedback_match"

#: The observation type a buyer's "it matched the pitch" becomes.
#:
#: ``fulfilled``, and deliberately **not** ``verified``. The four verification statuses
#: (``verified`` / ``contradicted`` / ``unsupported`` / ``ambiguous``) are what the claim
#: verifier decides about a catalog fact, and the manifest says in as many words that this
#: dimension takes none of them. ``fulfilled`` is the published positive for "a promise the
#: transaction record shows was kept", which is exactly what a routed buyer is reporting, and
#: it carries the same published 1.0 — one framework, one weight table.
FEEDBACK_POSITIVE_TYPE = "fulfilled"

#: The observation type a buyer's "it did not match the pitch" becomes.
#:
#: The approved manifest settles this directly. Its ``dishonest_store.behaviours`` entry
#: ``pitch_delivery_mismatch`` is ``{dim: feedback_match, type: mismatch_return,
#: claim_type: null}``, described as "The buyer reports that what arrived **does not match**
#: what was pitched, and returns it." So ``mismatch_return`` on this dimension is the NEGATIVE
#: report, which is what is emitted here.
#:
#: Note this contradicts the gloss in ``trust.scoring.engine``'s weight table ("the buyer said
#: it matched and then returned it"), which reads it as the *positive*-report case. Ground
#: truth is the manifest, not a comment in the engine that consumes it (D18/A3), so the
#: manifest wins — but the comment is a real divergence and is reported rather than silently
#: worked around.
#:
#: The published 1.5 also sits where a buyer report belongs: above ``unsupported`` (0.5, "no
#: evidence either way") and below ``contradicted`` (2.0, which is the catalog or the
#: transaction record saying otherwise, not a person).
#:
#: KNOWN GAP: the manifest's behaviour is a mismatch report *and a return*. A buyer who
#: complains and KEEPS the item is not that behaviour, and the published vocabulary has no
#: second buyer-reported negative to carry it — so it currently lands at the same 1.5. Closing
#: that needs a published weight, which is a manifest change and not this engine's to make.
FEEDBACK_NEGATIVE_TYPE = "mismatch_return"


class FeedbackRejected(ValueError):
    """Raised only by the strict entry points. :func:`accept_feedback` returns a verdict."""


def _field(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


#: The auditable statement of WHY a scrub happened, carried with every pushed event.
DISCLOSURE_POLICY = "R13/R5: the affected store learns what moved and never who moved it."

#: The eight fields ``contracts.LedgerEvent`` declares, and therefore the ONLY keys a pushed
#: event may carry: the model sets ``extra="forbid"``, so anything else is a refusal at the
#: intake rather than a field the receiver ignores. A stored ledger row also has ``seq`` and
#: ``event_hash``; those are the ledger's own bookkeeping, the receiving agent has no use for
#: them, and they are dropped here rather than smuggled through.
_WIRE_EVENT_FIELDS = (
    "event_id",
    "ts",
    "kind",
    "auction_id",
    "store_id",
    "order_ref",
    "prev_hash",
    "payload",
)


def _pseudonymous_context(delta: Any, event: Any) -> dict[str, Any]:
    """What the store agent may know about the counterparty: a pseudonym, and nothing else.

    EXACTLY the two fields ``contracts.PseudonymousContext`` declares, and no more. That
    model sets ``extra="forbid"``, so the four extras this function used to return —
    ``order_ref``, ``identity_disclosed``, ``redacted_fields``, ``policy`` — were not "extra
    information the receiver ignores", they were a refusal of the whole payload at the
    intake. See :func:`trust_event_payload` for where each of them lives now.
    """
    payload = _field(event, "payload")
    payload = payload if isinstance(payload, Mapping) else {}
    pseudonym = (
        _field(delta, "buyer_pseudonym")
        or payload.get("pseudonym")
        or payload.get("buyer_pseudonym")
        or _field(event, "pseudonym")
    )
    cluster_id = (
        _field(delta, "cluster_id") or payload.get("cluster_id") or _field(event, "cluster_id")
    )
    return {
        "cluster_id": str(cluster_id) if cluster_id is not None else None,
        "pseudonym": str(pseudonym) if pseudonym is not None else None,
    }


def _wire_event(scrubbed: Any, *, delta: Any, redacted_fields: int, reason_code: Any) -> Any:
    """The scrubbed event projected onto the published wire shape, carrying the audit report.

    Two things happen here, and the ORDER of the second against the scrub is load-bearing.

    First the event is projected onto :data:`_WIRE_EVENT_FIELDS`, because
    ``contracts.LedgerEvent`` forbids extras and a storage column riding along would have the
    intake refuse the event rather than ignore the column.

    Then the audit report is parked in ``payload``. That is not an arbitrary address: it is
    the one OPEN mapping anywhere in this chain — ``LedgerEvent.payload`` is typed
    ``dict[str, Any]`` precisely because every kind carries a different body — while
    ``TrustEventPayload`` and ``PseudonymousContext`` both forbid extras. ``order_ref`` goes
    somewhere better still: it is a real ``LedgerEvent`` field, which is where the published
    OpenAPI example puts it, and it is set unconditionally (``None`` included) so a reader
    never has to distinguish "no order" from "this build forgot".

    The report is added AFTER :func:`~trust.feedback.scrub.scrub_report` has run, never
    before. Scrubbing first would let the redaction walk count and mangle the very fields
    that describe it — ``policy`` is prose and would be rewritten, and ``redacted_fields``
    would be counted as one of the things it is counting.
    """
    projected = (
        {key: value for key, value in scrubbed.items() if key in _WIRE_EVENT_FIELDS}
        if isinstance(scrubbed, Mapping)
        else {}
    )
    payload = projected.get("payload")
    payload = dict(payload) if isinstance(payload, Mapping) else {}
    payload.update(
        {
            "schema_version": TRUST_EVENT_SCHEMA_VERSION,
            "reason_code": reason_code,
            "identity_disclosed": False,
            "redacted_fields": redacted_fields,
            "policy": DISCLOSURE_POLICY,
        }
    )
    projected["payload"] = payload
    projected["order_ref"] = _field(scrubbed, "order_ref") or _field(delta, "order_ref")
    return projected


def trust_event_payload(delta: Any) -> dict[str, Any]:
    """The pseudonymous payload one trust delta becomes on the wire.

    Args:
        delta: ``{store_id, dim, delta, event}`` — the dimension that moved, by how much, and
            the originating ledger event.

    Returns:
        A plain mapping in the shape ``contracts.TrustEventPayload`` declares, and ONLY that
        shape: ``store_id``, ``dim``, ``delta``, the scrubbed ``event`` (``event_id`` and
        ``kind`` preserved: an unexplained score move is one a store cannot act on and will
        not trust) and a non-``None`` ``pseudonymous_context``.

        WHERE THE AUDIT REPORT LIVES, and why it moved (T-259). This function used to return
        ``schema_version`` and ``reason_code`` as siblings of ``store_id``, and to hang
        ``order_ref`` / ``identity_disclosed`` / ``redacted_fields`` / ``policy`` off
        ``pseudonymous_context``. Both of those models set ``extra="forbid"``, so the store
        agent's real intake — ``store_agent.modes.AgentRunner.ingest_trust_event``, which
        validates with ``contracts.TrustEventPayload`` — refused **60 of 60** emitted events.
        Nothing caught it because this side only ever pushed into a local recording sink and
        the intake side only ever ingested its own hand-written dict, so both suites stayed
        green across a seam neither of them crossed.

        Nothing was dropped to make the intake stop refusing, which is the cheap fix and
        would have silently retired the redaction report an auditor needs. Every carrier
        still crosses, at an address the contract admits: ``order_ref`` is a real
        ``LedgerEvent`` field, and the other five ride in ``event.payload``, the one open
        mapping in the chain. See :func:`_wire_event`.

    Raises:
        FeedbackRejected: the delta names no store, or names no originating event. A trust
            event with no addressee has nowhere to go, and broadcasting it would leak one
            store's movement to every other; one with no event is a score move the receiving
            store cannot explain, and ``TrustEventPayload.event`` is required, so emitting it
            would produce a payload the intake is bound to refuse.
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
    if event is None:
        raise FeedbackRejected(
            f"the trust delta for store {store_id!r} carries no originating event, so the "
            f"affected store would be told its score moved and never why. "
            f"contracts.TrustEventPayload requires `event`, so this would be refused at the "
            f"intake rather than delivered."
        )

    scrubbed_event, redacted = scrub_report(event)
    return {
        "store_id": str(store_id),
        "dim": _field(delta, "dim"),
        "delta": float(_field(delta, "delta", 0.0) or 0.0),
        "event": _wire_event(
            scrubbed_event,
            delta=delta,
            redacted_fields=redacted,
            reason_code=scrub(_field(delta, "reason_code")),
        ),
        "pseudonymous_context": _pseudonymous_context(delta, event),
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
    # Argument validation FIRST, before the routing gate. A bad `buyer_track_record` is a
    # caller bug, and a caller bug that only surfaces for routed orders is one that ships:
    # the same call raised on one order and passed silently on the next, and which it did
    # depended on data rather than on the code under test.
    factor = 1.0
    if buyer_track_record is not None:
        factor = float(buyer_track_record)
        if not 0.0 < factor <= 1.0:
            raise FeedbackRejected(
                f"buyer_track_record must be a multiplier in (0, 1], got {buyer_track_record!r}. "
                "A factor above 1 would let one account outweigh the rest of the network."
            )

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


def feedback_observation(verdict: Any, *, observed_at: Any) -> dict[str, Any] | None:
    """Turn one :func:`accept_feedback` verdict into the trust observation the scorer consumes.

    This is the seam R14's weight travels through. ``accept_feedback`` decides *whether* a
    report counts and *for how much*; :mod:`trust.scoring` decides what a weighted observation
    does to a dimension; this function is the one place that says how the first becomes the
    second, so no caller has to reinvent the dimension or the type mapping.

    Before it existed the weight was computed and thrown away: ``score`` derived an
    observation's weight solely from the published type table and read no per-observation
    weight at all, so "positive feedback contradicted by a return is downweighted" and "a
    single account cannot outvote the network" were properties of a number nothing consumed.

    Args:
        verdict: what :func:`accept_feedback` returned.
        observed_at: when the feedback was given. Explicit, never a clock — the scorer decays
            against it and the replay has to reproduce the same number (D17/S3).

    There is deliberately no way to name the store. It comes from the verdict, which got it
    from the routed-order record — the same record the R14 gate consulted to decide the
    feedback counts at all. An override parameter shipped here briefly and was removed unused:
    a caller free to name a different store could file one store's complaint against a rival,
    and no gate downstream would notice, because by then the report is a perfectly well-formed
    accepted verdict.

    Returns:
        ``{store_id, dim, type, observed_at, weight}`` — or ``None`` when the verdict was not
        accepted.

        ``None`` and not a zero-weight observation. A rejected report is not weak evidence,
        it is *not evidence*: an observation still counts towards the dimension's coverage and
        the store's observation count, so admitting one per astroturfed review would hand a
        store a dial on its own coverage for the price of some fake reviews — which is the
        thing the routed-buyer gate exists to prevent.

        The ``weight`` is carried through in RANGE terms unvalidated on purpose: whether a
        number is an admissible weight is decided where it is applied, by
        ``trust.scoring.relative_observation_weight``, so there is one rule about that and not
        a second copy here free to drift from it. What IS checked here is that it is a number
        at all — see below.

    Raises:
        FeedbackRejected: the verdict carries a ``weight`` that is not a number. Neither
            available default is safe to pick for it: treating it as 1.0 would admit an
            unverified report at full force, and treating it as 0.0 would erase a buyer's
            complaint — which of those a silent default did would depend on whether the report
            happened to be positive. A missing ``weight`` key is different and is fine: it
            means 1.0, exactly as it does in the scorer.
    """
    if not bool(_field(verdict, "accepted", False)):
        return None

    raw_weight = _field(verdict, "weight", BASE_FEEDBACK_WEIGHT)
    if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
        raise FeedbackRejected(
            f"feedback verdict carries weight {raw_weight!r}, which is not a number. The "
            "weight is what R14's two properties are made of; a verdict that lost it is a "
            "producer bug, and guessing a replacement would silently pick a side."
        )

    positive = bool(_field(verdict, "positive", False))
    resolved_store = _field(verdict, "store_id")
    return {
        "store_id": resolved_store,
        "dim": FEEDBACK_DIMENSION,
        "type": FEEDBACK_POSITIVE_TYPE if positive else FEEDBACK_NEGATIVE_TYPE,
        "observed_at": observed_at,
        "weight": float(raw_weight),
    }
