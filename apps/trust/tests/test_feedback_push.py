"""T-063 — the trust-event push and the routed-buyer feedback gate (R13, R14, R5).

Two halves of one loop, and each has a failure mode that looks like success:

**The push (R13/R5).** A scrub is trivially "passed" by a payload nobody buried anything in.
So the event pushed here hides buyer identity three levels down inside nested dicts *and*
inside a list of line items, and the assertions walk the delivered payload recursively —
collecting every key and every string at every depth with a walker written here rather than
borrowed from the product — to require that no forbidden key NAME and no identity VALUE
survives anywhere. The mirror-image failure is a scrub so eager it eats ``canonical_name``
and ``store_id``: that payload is perfectly private and completely useless, so the innocent
keys are asserted present in the same breath.

The redaction *report* is its own trap. Telling the store "we removed ``buyer_email`` and
``recipient_name``" re-publishes on the wire exactly the field names the scrub existed to
keep off it, so the context must carry a count and never the names — asserted by looking for
those names anywhere in the delivered payload.

**The gate (R14).** Feedback that is merely counted is astroturfable, so the tests below pin
the *weights*: unrouted is worth zero, routed is worth the base, a positive report from a
buyer who returned the item is worth strictly less but strictly more than nothing, and — the
asymmetry that is easy to get wrong — a NEGATIVE report from a returning buyer is not
downweighted at all, because the return corroborates it rather than contradicting it.

Pure data throughout: the sink is a recording double, and nothing here opens a socket.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

import pytest

from apps.trust.src.feedback import (
    BASE_FEEDBACK_WEIGHT,
    REDACTED,
    RETURN_CONTRADICTION_FACTOR,
    TRUST_EVENT_SCHEMA_VERSION,
    FeedbackRejected,
    accept_feedback,
    push_trust_event,
    scrub,
    scrub_report,
    trust_event_payload,
)

BUYER_EMAIL = "dana@example.com"
BUYER_NAME = "Dana Shopper"

#: Every identity key planted in :func:`_identity_laden_event`, and therefore the exact count
#: the scrub must report. Nesting one of these inside another would make the count ambiguous
#: (a removed subtree is never walked into), so each sits under an innocent parent.
PLANTED_IDENTITY_KEYS = (
    "buyer_email",
    "address",
    "phone",
    "recipient_name",
    "customer_id",
    "user_agent",
    "ip",
)

#: Identity that must not survive anywhere in the delivered payload, in any form.
PLANTED_IDENTITY_VALUES = (
    BUYER_EMAIL,
    BUYER_NAME,
    "cust-42",
    "Mozilla",
    "203.0.113.9",
    "555 0100",
    "Baker Street",
    "SW1A",
    "4111111111111111",
)


class _RecordingSink:
    """A store-agent intake double that records every ``(store_id, payload)`` it is handed."""

    def __init__(self) -> None:
        self.sent: list[tuple[Any, Any]] = []

    def send(self, store_id: Any, payload: Any) -> None:
        self.sent.append((store_id, payload))


class _BrokenSink:
    """An intake that is reachable but cannot accept the push."""

    def send(self, store_id: Any, payload: Any) -> None:
        raise RuntimeError("store-agent intake refused the connection")


def _keys_and_strings(
    value: Any, keys: set[str] | None = None, strings: list[str] | None = None
) -> tuple[set[str], list[str]]:
    """Every mapping key and every string value at every depth of ``value``.

    Written here, recursively, rather than reusing anything the product ships: a scrub graded
    by the product's own walker is graded by whatever the scrub happens to walk into.
    """
    if keys is None or strings is None:
        keys, strings = set(), []
    if isinstance(value, Mapping):
        for key, item in value.items():
            keys.add(str(key))
            _keys_and_strings(item, keys, strings)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _keys_and_strings(item, keys, strings)
    elif isinstance(value, str):
        strings.append(value)
    return keys, strings


def _identity_laden_event(make) -> dict[str, Any]:
    """A ``reconciled`` event with buyer identity buried in nested dicts and in a list.

    The identity is placed where a top-level-only scrub would miss it: under an innocent
    container (``shipping.address``), three levels down (``audit.nested.deeper``), and inside
    two different line items. Emails also sit as free-text *values* under innocent keys, which
    the key denylist cannot help with.
    """
    return make(
        "ev-77",
        "reconciled",
        store_id="s-affected",
        order_ref="o-9",
        payload={
            "price_honored": False,
            "pseudonym": "px-77",
            "display_name": "Widget Pro",
            "buyer_email": BUYER_EMAIL,
            "shipping": {
                "carrier": "ups",
                "address": {"line1": "221B Baker Street", "postcode": "SW1A 1AA"},
            },
            "contact": {"method": "chat", "phone": "+1 555 0100"},
            "line_items": [
                {
                    "sku": "sku-1",
                    "canonical_name": "Widget",
                    "qty": 1,
                    "recipient_name": BUYER_NAME,
                },
                {
                    "sku": "sku-2",
                    "canonical_name": "Gadget",
                    "qty": 2,
                    "customer_id": "cust-42",
                    "gift_note": f"deliver to {BUYER_EMAIL}",
                },
            ],
            "audit": {
                "nested": {
                    "deeper": {
                        "ok": True,
                        "ip": "203.0.113.9",
                        "user_agent": "Mozilla/5.0 (Macintosh)",
                    }
                }
            },
            "note": f"buyer wrote in from {BUYER_EMAIL}, card 4111111111111111",
        },
    )


def _delta(make, **overrides: Any) -> dict[str, Any]:
    """One trust delta: the dimension that moved, by how much, and the event that moved it."""
    delta: dict[str, Any] = {
        "store_id": "s-affected",
        "dim": "price_honored",
        "delta": -0.12,
        "event": _identity_laden_event(make),
    }
    delta.update(overrides)
    return delta


def _routed(order_ref: str = "o-1", *, returned: bool = False) -> dict[str, dict[str, Any]]:
    """The routed-order table the R14 gate checks feedback against."""
    return {
        order_ref: {
            "order_ref": order_ref,
            "store_id": "s-1",
            "returned": returned,
            "buyer_pseudonym": "px-1",
        }
    }


# --------------------------------------------------------------------------------------
# R13/R5 — the push: once, to one store, carrying no identity
# --------------------------------------------------------------------------------------


def test_a_trust_event_is_sent_exactly_once_to_the_affected_store(e6_make_event):
    """R13: one delta becomes one send, addressed to the affected store, and is returned.

    "Once, to one" is the whole guarantee: a duplicate send is a second penalty for one event
    once the receiving agent starts counting, and the returned payload has to be the object
    that actually left so a caller can ledger exactly what was disclosed.
    """
    sink = _RecordingSink()

    returned = push_trust_event(_delta(e6_make_event), sink)

    assert len(sink.sent) == 1, f"the delta was sent {len(sink.sent)} times, not once"
    target, delivered = sink.sent[0]
    assert target == "s-affected", "the trust event went to the wrong store"
    assert delivered["store_id"] == "s-affected"
    assert delivered["dim"] == "price_honored"
    assert delivered["delta"] == pytest.approx(-0.12)
    assert returned is delivered, "the returned payload is not the object that was sent"


def test_each_store_receives_only_its_own_delta(e6_make_event):
    """R13: with several stores moving, no store's payload reaches another store's intake.

    A broadcast leaks one merchant's trust movement to its competitors, which is a
    confidentiality failure that a single-store test cannot see.
    """
    sink = _RecordingSink()
    for store in ("s-1", "s-2", "s-3"):
        event = e6_make_event(
            f"ev-{store}", "reconciled", store_id=store, order_ref=f"o-{store}", payload={}
        )
        push_trust_event(
            {"store_id": store, "dim": "price_honored", "delta": -0.1, "event": event}, sink
        )

    assert [target for target, _ in sink.sent] == ["s-1", "s-2", "s-3"]
    for target, delivered in sink.sent:
        assert delivered["store_id"] == target
        _, strings = _keys_and_strings(delivered)
        others = {"s-1", "s-2", "s-3"} - {target}
        for other in others:
            assert not any(other in text for text in strings), (
                f"{target}'s payload named {other}, so one store learned another's movement"
            )


def test_the_scrub_removes_identity_keys_at_every_depth_and_inside_lists(e6_make_event):
    """R13/R5: no forbidden key name and no identity value survives anywhere in the payload.

    The identity is buried inside nested dicts and inside a list of line items, so a scrub
    that only walked the top level would pass every simple test and still ship the customer
    list. The keys must be *removed*, not nulled: a labelled empty ``buyer_email`` tells the
    recipient exactly what to correlate against and invites the next writer to repopulate it.
    """
    sink = _RecordingSink()
    push_trust_event(_delta(e6_make_event), sink)
    _, delivered = sink.sent[0]

    keys, strings = _keys_and_strings(delivered)

    for forbidden in PLANTED_IDENTITY_KEYS:
        assert forbidden not in keys, (
            f"the pushed payload still carries a {forbidden!r} key — nulling is not scrubbing"
        )
    for value in PLANTED_IDENTITY_VALUES:
        assert not any(value in text for text in strings), (
            f"the pushed payload still carries the buyer identity {value!r}"
        )


def test_innocent_keys_survive_the_scrub(e6_make_event):
    """R13: a scrub that ate the store's own fields would be private and useless.

    The pushed event exists so the store can act on a concrete thing rather than an
    unexplained score move, so the identifying fields of the *event* — and the product names
    it is about — have to arrive intact.
    """
    sink = _RecordingSink()
    push_trust_event(_delta(e6_make_event), sink)
    keys, _ = _keys_and_strings(sink.sent[0][1])

    for needed in ("canonical_name", "display_name", "store_id", "order_ref", "event_id", "kind"):
        assert needed in keys, f"the scrub removed {needed!r}, which the store agent needs"


def test_the_full_originating_event_reaches_the_store(e6_make_event):
    """R13: the store is told *which of its own events* moved the dimension, and under which schema.

    An unexplained score move is one a store cannot act on and will not trust, so the event's
    own identity survives the scrub alongside a schema version the receiving agent can refuse.
    """
    sink = _RecordingSink()
    push_trust_event(_delta(e6_make_event), sink)
    _, delivered = sink.sent[0]

    assert delivered["schema_version"] == TRUST_EVENT_SCHEMA_VERSION
    assert delivered["event"]["event_id"] == "ev-77", "the originating event was not pushed"
    assert delivered["event"]["kind"] == "reconciled"
    assert delivered["event"]["payload"]["price_honored"] is False, (
        "the event's own verdict was lost"
    )


def test_an_email_in_a_free_text_value_under_an_innocent_key_is_redacted(e6_make_event):
    """R13/R5: identity that hides in a note survives the key denylist, so values are scanned too.

    ``gift_note`` and ``note`` are keys a store agent legitimately reads; the buyer's e-mail
    address sitting inside their text is identity all the same and must be replaced in place
    rather than the whole field being dropped.
    """
    sink = _RecordingSink()
    push_trust_event(_delta(e6_make_event), sink)
    payload = sink.sent[0][1]["event"]["payload"]

    note = payload["note"]
    gift_note = payload["line_items"][1]["gift_note"]

    assert BUYER_EMAIL not in note, "an e-mail address in a free-text note reached the store"
    assert REDACTED in note, "the note was left without any sign that something was removed"
    assert "4111111111111111" not in note, "a long digit run in free text was not redacted"
    assert note.startswith("buyer wrote in from "), "the surrounding text was thrown away with it"
    assert BUYER_EMAIL not in gift_note and REDACTED in gift_note


def test_the_pseudonymous_context_carries_the_pseudonym_and_never_the_redacted_names(e6_make_event):
    """R13/R5: the store may know a pseudonym and a count, never the names of removed fields.

    "We removed ``buyer_email`` and ``recipient_name``" would re-publish on the wire precisely
    what the scrub existed to keep off it, and would tell the recipient exactly what the
    network holds about its buyers — so the names are searched for across the whole payload.
    """
    sink = _RecordingSink()
    push_trust_event(_delta(e6_make_event), sink)
    _, delivered = sink.sent[0]
    context = delivered["pseudonymous_context"]

    assert context is not None, "the payload carried no pseudonymous context at all"
    assert context["pseudonym"] == "px-77", "the pseudonym the event carried was dropped"
    assert context["identity_disclosed"] is False

    _, strings = _keys_and_strings(delivered)
    for name in ("buyer_email", "recipient_name", "customer_id", "user_agent"):
        assert not any(name in text for text in strings), (
            f"the payload names {name!r} as a redacted field, re-publishing what it removed"
        )


def test_the_pseudonymous_context_reports_how_many_fields_were_redacted(e6_make_event):
    """R13/R5: a count, so an auditor can see a scrub happened without learning what it removed.

    The exact number is asserted because it is the only observable that would move if the
    recursion stopped early: a scrub that gave up below the top level would still remove
    ``buyer_email`` and would report one instead of seven.
    """
    sink = _RecordingSink()
    push_trust_event(_delta(e6_make_event), sink)
    context = sink.sent[0][1]["pseudonymous_context"]

    assert context["redacted_fields"] == len(PLANTED_IDENTITY_KEYS), (
        "the redaction count does not match the identity keys planted at every depth: "
        f"expected {len(PLANTED_IDENTITY_KEYS)} ({', '.join(PLANTED_IDENTITY_KEYS)}), "
        f"got {context['redacted_fields']}"
    )


def test_the_pseudonymous_context_is_present_even_when_there_is_no_pseudonym(e6_make_event):
    """R13: the context is never ``None`` — an absent pseudonym is a null field, not a null context.

    A caller that reads ``payload["pseudonymous_context"]["identity_disclosed"]`` must not
    have to guard against the whole block vanishing for an event that carried no pseudonym.
    """
    event = e6_make_event("ev-anon", "reconciled", store_id="s-1", order_ref="o-2", payload={})

    delivered = trust_event_payload(
        {"store_id": "s-1", "dim": "not_returned", "delta": 0.05, "event": event}
    )
    context = delivered["pseudonymous_context"]

    assert context is not None
    assert context["pseudonym"] is None
    assert context["redacted_fields"] == 0
    assert context["order_ref"] == "o-2"


def test_the_delta_reason_code_is_scrubbed_too(e6_make_event):
    """R13/R5: the covering note travels with the payload, so it gets the same treatment.

    A reason code is written by whatever produced the delta and is free text as far as this
    module knows, which makes it the obvious place for identity to ride along unscanned.
    """
    delta = _delta(e6_make_event, reason_code=f"overcharge reported by {BUYER_EMAIL}")

    delivered = trust_event_payload(delta)

    assert BUYER_EMAIL not in delivered["reason_code"], "the reason code carried the buyer's e-mail"
    assert REDACTED in delivered["reason_code"]


def test_scrub_does_not_mutate_its_input(e6_make_event):
    """R13: the scrub returns a NEW structure — the ledger's own copy of the event is untouched.

    Events are sealed into a hash chain. A scrub with a side effect on the caller's object
    would silently rewrite a chained event and invalidate every link after it, and the damage
    would only surface at the next verification.
    """
    event = _identity_laden_event(e6_make_event)
    before = copy.deepcopy(event)

    scrubbed = scrub(event)

    assert event == before, "scrub() mutated the event it was handed"
    assert scrubbed != event, "scrub() returned its input unchanged — nothing was removed"

    delta = _delta(e6_make_event)
    delta_before = copy.deepcopy(delta)
    push_trust_event(delta, _RecordingSink())
    assert delta == delta_before, "the push mutated the delta it was given"


def test_scrub_report_agrees_with_scrub_and_adds_a_count(e6_make_event):
    """R13: the reporting wrapper changes nothing about the result, it only counts.

    If the two ever diverged, the count in the payload would be describing a different scrub
    from the one that produced the payload.
    """
    event = _identity_laden_event(e6_make_event)

    scrubbed, removed = scrub_report(event)

    assert scrubbed == scrub(event), "scrub_report() and scrub() disagree about the same event"
    assert removed == len(PLANTED_IDENTITY_KEYS)
    assert isinstance(removed, int) and not isinstance(removed, bool)


def test_a_delta_naming_no_store_anywhere_is_rejected(e6_make_event):
    """R13: a trust event with no addressee is refused rather than broadcast.

    There is no safe default here: sending it to everyone leaks one store's movement to every
    other, and dropping it silently loses a graded outcome. So it raises, and nothing is sent.
    """
    sink = _RecordingSink()

    with pytest.raises(FeedbackRejected):
        trust_event_payload({"dim": "price_honored", "delta": -0.1})

    with pytest.raises(FeedbackRejected):
        push_trust_event({"dim": "price_honored", "delta": -0.1, "event": None}, sink)

    assert sink.sent == [], "a delta with no addressee still reached an intake"
    assert issubclass(FeedbackRejected, ValueError), (
        "callers catching ValueError must keep catching this"
    )


def test_the_store_id_falls_back_to_the_events_own_store(e6_make_event):
    """R13: a delta that omits the store is addressed from the event that caused it.

    The event always knows whose it is, so requiring the producer to repeat the store id would
    reject perfectly attributable deltas.
    """
    delta = {"dim": "price_honored", "delta": -0.12, "event": _identity_laden_event(e6_make_event)}
    sink = _RecordingSink()

    delivered = push_trust_event(delta, sink)

    assert delivered["store_id"] == "s-affected"
    assert sink.sent[0][0] == "s-affected", (
        "the fallback store id was computed but not used to address it"
    )


def test_a_sink_that_cannot_send_is_not_swallowed(e6_make_event):
    """R13: a push that silently does nothing is worse than one that fails.

    A store graded on a signal it was never told about cannot act on it and cannot appeal it,
    so both failure shapes — an intake with no ``send`` at all, and one whose ``send`` raises —
    must reach the caller.
    """
    with pytest.raises(AttributeError):
        push_trust_event(_delta(e6_make_event), object())

    with pytest.raises(RuntimeError):
        push_trust_event(_delta(e6_make_event), _BrokenSink())


def test_the_pushed_payload_is_json_serialisable(e6_make_event):
    """R13: the payload crosses a process boundary to the store agent, so it must encode.

    Every assertion above would pass on a payload holding a ``Decimal`` or a set that then
    failed at the wire.
    """
    sink = _RecordingSink()
    push_trust_event(_delta(e6_make_event), sink)

    encoded = json.dumps(sink.sent[0][1])

    assert json.loads(encoded) == sink.sent[0][1], "the payload did not survive a JSON round-trip"
    assert BUYER_EMAIL not in encoded, "the buyer's e-mail survived into the encoded wire form"


# --------------------------------------------------------------------------------------
# R14 — the routed-buyer gate and the weight it earns
# --------------------------------------------------------------------------------------


def test_feedback_about_an_order_the_network_never_routed_is_rejected():
    """R14: the gate is the point — without it a store buys its own ``feedback_match`` score.

    Rejected feedback is worth exactly zero, names no store, and says why: a caller that
    merely saw ``accepted: False`` and defaulted the weight to 1.0 would reopen the hole.
    """
    verdict = accept_feedback("o-not-routed", {"matched_pitch": True}, routed_orders=_routed("o-1"))

    assert verdict["accepted"] is False
    assert verdict["weight"] == 0.0, "unrouted feedback carried a non-zero weight"
    assert verdict["store_id"] is None, "unrouted feedback was attributed to a store anyway"
    assert "not_network_routed" in verdict["reasons"], "the rejection does not say why"


def test_feedback_about_a_routed_order_is_accepted_at_the_base_weight():
    """R14: a buyer the network actually routed is heard, at the full base weight.

    The order is matched by its reference as a string, and the verdict carries the store the
    feedback is about so the caller does not have to re-derive it.
    """
    verdict = accept_feedback("o-1", {"matched_pitch": True}, routed_orders=_routed("o-1"))

    assert verdict["accepted"] is True
    assert verdict["weight"] == pytest.approx(BASE_FEEDBACK_WEIGHT)
    assert verdict["weight"] > 0.0
    assert verdict["store_id"] == "s-1"
    assert verdict["buyer_pseudonym"] == "px-1"
    assert "network_routed" in verdict["reasons"]


def test_positive_feedback_from_a_buyer_who_returned_the_item_is_downweighted_not_discarded():
    """R14: the buyer's own behaviour contradicts their answer, so it weighs less — not nothing.

    Discarding it would throw away a real signal *and* hand a store a way to erase inconvenient
    feedback by provoking a return, so the weight must stay strictly positive and strictly
    below the uncontradicted base.
    """
    kept = accept_feedback("o-1", {"matched_pitch": True}, routed_orders=_routed("o-1"))
    returned = accept_feedback(
        "o-1", {"matched_pitch": True}, routed_orders=_routed("o-1", returned=True)
    )

    assert returned["accepted"] is True, (
        "contradicted feedback was discarded instead of downweighted"
    )
    assert 0.0 < returned["weight"] < kept["weight"], (
        f"a return did not downweight the answer it contradicts "
        f"(kept={kept['weight']}, returned={returned['weight']})"
    )
    assert returned["weight"] == pytest.approx(BASE_FEEDBACK_WEIGHT * RETURN_CONTRADICTION_FACTOR)
    assert returned["returned"] is True
    assert "contradicted_by_return" in returned["reasons"], "the downweight is unexplained"
    assert "contradicted_by_return" not in kept["reasons"]


def test_negative_feedback_from_a_buyer_who_returned_the_item_is_not_downweighted():
    """R14: a return corroborates "it did not match", so it must not discount that answer.

    This is the asymmetry an implementation that keys the downweight on ``returned`` alone
    gets wrong: it would quietly suppress the best-evidenced complaints in the system — the
    ones where the buyer both said the pitch was wrong and sent the item back.
    """
    verdict = accept_feedback(
        "o-1",
        {"matched_pitch": False, "answer": "mismatched"},
        routed_orders=_routed("o-1", returned=True),
    )

    assert verdict["accepted"] is True
    assert verdict["positive"] is False
    assert verdict["returned"] is True
    assert verdict["weight"] == pytest.approx(BASE_FEEDBACK_WEIGHT), (
        "a complaint corroborated by a return was downweighted as if the return contradicted it"
    )
    assert "contradicted_by_return" not in verdict["reasons"]


def test_the_buyer_track_record_scales_the_weight_and_is_named_in_the_reasons():
    """R14: an unreliable buyer weighs less, so one account cannot outvote the network.

    The factor multiplies rather than replaces, so it composes with the return contradiction
    instead of overriding it — and the verdict says the factor was applied.
    """
    plain = accept_feedback("o-1", {"matched_pitch": True}, routed_orders=_routed("o-1"))
    scaled = accept_feedback(
        "o-1", {"matched_pitch": True}, routed_orders=_routed("o-1"), buyer_track_record=0.5
    )
    unit = accept_feedback(
        "o-1", {"matched_pitch": True}, routed_orders=_routed("o-1"), buyer_track_record=1.0
    )
    contradicted = accept_feedback(
        "o-1",
        {"matched_pitch": True},
        routed_orders=_routed("o-1", returned=True),
        buyer_track_record=0.5,
    )

    assert scaled["weight"] == pytest.approx(plain["weight"] * 0.5)
    assert unit["weight"] == pytest.approx(plain["weight"]), (
        "a perfect track record changed the weight"
    )
    assert contradicted["weight"] == pytest.approx(
        BASE_FEEDBACK_WEIGHT * RETURN_CONTRADICTION_FACTOR * 0.5
    ), "the track record replaced the return contradiction instead of composing with it"
    assert "buyer_track_record" in scaled["reasons"]
    assert "buyer_track_record" not in plain["reasons"]


@pytest.mark.parametrize("factor", [0.0, -0.5, 1.5, 2, 100.0])
def test_a_buyer_track_record_outside_the_unit_interval_is_rejected(factor):
    """R14: the multiplier is bounded to ``(0, 1]`` — above 1 is an amplifier, not a discount.

    A factor above 1 would let a single account outweigh the rest of the network, which is
    the exact attack the gate exists to stop; zero and negatives are silent data corruption.
    """
    with pytest.raises(FeedbackRejected):
        accept_feedback(
            "o-1", {"matched_pitch": True}, routed_orders=_routed("o-1"), buyer_track_record=factor
        )


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"matched_pitch": True}, True),
        ({"matched_pitch": "Yes"}, True),
        ({"matched_pitch": "matched"}, True),
        ({"matched_pitch": "true"}, True),
        ({"answer": "matched"}, True),
        ({"answer": "Great"}, True),
        ({"matched_pitch": False}, False),
        ({"matched_pitch": "no"}, False),
        ({"answer": "returned it"}, False),
        ({}, False),
    ],
)
def test_the_several_spellings_of_a_positive_answer_are_recognised(response, expected):
    """R14: buyers answer in whatever shape the collecting surface produced.

    A boolean, the word "Yes" in any casing, and the word "matched" all mean the delivery
    matched the pitch; anything else — including an unanswered response — does not, and a
    parser that treated every non-empty string as agreement would read "no" as praise.
    """
    verdict = accept_feedback("o-1", response, routed_orders=_routed("o-1"))

    assert verdict["positive"] is expected, (
        f"{response!r} was read as positive={verdict['positive']}"
    )


def test_an_explicit_matched_pitch_wins_over_a_contradicting_free_text_answer():
    """R14: the structured field is the answer; the free-text one is only a fallback.

    Consulting both and taking whichever is positive would let a "matched" left over in a
    free-text box overturn a buyer's explicit "no".
    """
    verdict = accept_feedback(
        "o-1", {"matched_pitch": False, "answer": "matched"}, routed_orders=_routed("o-1")
    )

    assert verdict["positive"] is False, "a stale free-text answer overrode the buyer's explicit no"


def test_the_routed_table_is_consulted_by_the_order_reference_as_a_string():
    """R14: an order reference arriving as a non-string still finds its routed record.

    Order references cross HTTP and JSON boundaries and come back as whatever the caller had;
    a lookup that missed on ``12345`` versus ``"12345"`` would reject a genuinely routed
    buyer, which reads exactly like the astroturf case it is supposed to catch.
    """
    routed = {"12345": {"order_ref": "12345", "store_id": "s-1", "returned": False}}

    verdict = accept_feedback(12345, {"matched_pitch": True}, routed_orders=routed)

    assert verdict["accepted"] is True, "a numeric order reference was treated as unrouted"
    assert verdict["order_ref"] == "12345"


# --------------------------------------------------------------------------------------
# T-186 — the weight has to REACH the score (R14)
#
# `accept_feedback` returned a weight and nothing consumed it: `trust.scoring.score` derived
# an observation's weight solely from the published type table. So both R14 properties this
# file already asserts -- "downweighted by a contradicting return" and "one account cannot
# outvote the network" -- held over a number that never touched a Beta. These tests assert
# the weight all the way through to alpha/beta, which is the only place it means anything.
# --------------------------------------------------------------------------------------


def _feedback_alpha(verdict, as_of):
    """The alpha ``feedback_match`` carries after scoring one accepted feedback verdict."""
    from apps.trust.src.feedback import feedback_observation
    from apps.trust.src.scoring import score

    observation = feedback_observation(verdict, observed_at=as_of)
    assert observation is not None, "an accepted verdict produced no trust observation"
    return float(score([observation], as_of=as_of)["dims"]["feedback_match"]["alpha"])


def test_accepted_feedback_becomes_a_weighted_feedback_match_observation(e6_as_of):
    """R14: the routed buyer's report lands on ``feedback_match``, carrying its weight.

    ``feedback_match`` takes NO verification outcome -- the approved manifest says so in
    ``claim_type_dimensions_meta`` -- so the observation type is the buyer-reported one
    (``fulfilled`` for "it matched"), never ``verified``.
    """
    from apps.trust.src.feedback import feedback_observation

    verdict = accept_feedback("o-1", {"matched_pitch": True}, routed_orders=_routed("o-1"))
    observation = feedback_observation(verdict, observed_at=e6_as_of)

    assert observation["store_id"] == "s-1"
    assert observation["dim"] == "feedback_match"
    assert observation["type"] == "fulfilled"
    assert observation["observed_at"] == e6_as_of
    assert float(observation["weight"]) == BASE_FEEDBACK_WEIGHT


def test_unrouted_feedback_produces_no_observation_at_all(e6_as_of):
    """R14: the gate is the point -- rejected feedback is not a zero-weight observation.

    A zero-weight observation still counts towards coverage and towards the store's
    observation count, so admitting one for every astroturfed review would let a store dilute
    its own coverage on demand. Rejected feedback simply is not evidence.
    """
    from apps.trust.src.feedback import feedback_observation

    verdict = accept_feedback("o-nope", {"matched_pitch": True}, routed_orders=_routed("o-1"))

    assert verdict["accepted"] is False
    assert feedback_observation(verdict, observed_at=e6_as_of) is None


def test_a_return_contradicting_a_positive_report_moves_the_beta_strictly_less(e6_as_of):
    """R14, all the way to the number: the downweight has to show up in alpha.

    Measured before the fix: the kept order and the returned order produced weights 1.0 and
    0.25 and then the identical alpha, because ``score`` never read the weight.
    """
    kept = accept_feedback("o-kept", {"matched_pitch": True}, routed_orders=_routed("o-kept"))
    returned = accept_feedback(
        "o-returned",
        {"matched_pitch": True},
        routed_orders=_routed("o-returned", returned=True),
    )

    kept_alpha = _feedback_alpha(kept, e6_as_of)
    returned_alpha = _feedback_alpha(returned, e6_as_of)
    prior_alpha = 2.0

    assert kept_alpha > returned_alpha > prior_alpha, (
        f"the return contradiction never reached the Beta update "
        f"(kept alpha={kept_alpha}, returned alpha={returned_alpha})"
    )
    assert (returned_alpha - prior_alpha) == pytest.approx(
        (kept_alpha - prior_alpha) * RETURN_CONTRADICTION_FACTOR
    ), "the alpha movement did not scale by the published return-contradiction factor"


def test_one_account_with_a_poor_track_record_cannot_outvote_the_network(e6_as_of):
    """R14: ten reports from a 0.1-track-record account weigh exactly one honest report.

    Stated as an equality rather than an inequality on purpose: "weighs less" is satisfied by
    an engine that ignores the track record entirely as long as it also ignores the count.
    """
    from apps.trust.src.feedback import feedback_observation
    from apps.trust.src.scoring import score

    honest = accept_feedback("o-1", {"matched_pitch": True}, routed_orders=_routed("o-1"))
    spammer = accept_feedback(
        "o-1", {"matched_pitch": True}, routed_orders=_routed("o-1"), buyer_track_record=0.1
    )

    one_honest = [feedback_observation(honest, observed_at=e6_as_of)]
    ten_spammed = [feedback_observation(spammer, observed_at=e6_as_of) for _ in range(10)]

    honest_alpha = float(score(one_honest, as_of=e6_as_of)["dims"]["feedback_match"]["alpha"])
    spam_alpha = float(score(ten_spammed, as_of=e6_as_of)["dims"]["feedback_match"]["alpha"])

    assert spam_alpha == pytest.approx(honest_alpha), (
        "a buyer's track record does not scale what their feedback is worth to the score"
    )


def test_a_negative_report_lands_as_the_published_buyer_reported_negative(e6_as_of):
    """A buyer who says the delivery did not match the pitch moves ``feedback_match`` down.

    ``mismatch_return`` is the only buyer-reported negative the approved weight table
    publishes, and it is NOT one of the four verification statuses the manifest forbids on
    this dimension.
    """
    from apps.trust.src.feedback import feedback_observation
    from apps.trust.src.scoring import score

    verdict = accept_feedback(
        "o-1", {"matched_pitch": False}, routed_orders=_routed("o-1", returned=True)
    )
    observation = feedback_observation(verdict, observed_at=e6_as_of)

    assert observation["type"] == "mismatch_return"
    assert float(observation["weight"]) == BASE_FEEDBACK_WEIGHT, (
        "a negative report from a returning buyer is corroborated by the return, not "
        "contradicted by it, so it is not downweighted"
    )
    entry = score([observation], as_of=e6_as_of)["dims"]["feedback_match"]
    assert float(entry["beta"]) > 2.0 and float(entry["alpha"]) == 2.0


def test_a_verdict_whose_weight_is_not_a_number_is_refused_not_guessed(e6_as_of):
    """Neither default is safe, so neither is picked.

    Defaulting a lost weight to 1.0 admits an unverified report at full force; defaulting it
    to 0.0 erases a buyer's complaint. Which one a silent default did would depend on whether
    the report happened to be positive, which is the worst possible way for it to be decided.
    """
    from apps.trust.src.feedback import feedback_observation

    verdict = accept_feedback("o-1", {"matched_pitch": True}, routed_orders=_routed("o-1"))

    with pytest.raises(FeedbackRejected):
        feedback_observation({**verdict, "weight": None}, observed_at=e6_as_of)
    with pytest.raises(FeedbackRejected):
        feedback_observation({**verdict, "weight": "1.0"}, observed_at=e6_as_of)
    with pytest.raises(FeedbackRejected):
        feedback_observation({**verdict, "weight": True}, observed_at=e6_as_of)

    # A verdict with no `weight` KEY at all is a different thing and means 1.0, exactly as it
    # does in the scorer.
    without = {key: value for key, value in verdict.items() if key != "weight"}
    assert feedback_observation(without, observed_at=e6_as_of)["weight"] == BASE_FEEDBACK_WEIGHT


def test_a_feedback_observation_cannot_be_filed_against_a_store_of_the_callers_choosing(
    e6_as_of,
):
    """R14: the store comes from the routed-order record the gate already consulted.

    An override parameter shipped here briefly. Nothing used it, and a caller free to name a
    different store could file one store's complaint against a rival -- with nothing
    downstream to notice, because by then it is a perfectly well-formed accepted verdict.
    """
    import inspect

    from apps.trust.src.feedback import feedback_observation

    verdict = accept_feedback("o-1", {"matched_pitch": False}, routed_orders=_routed("o-1"))

    assert feedback_observation(verdict, observed_at=e6_as_of)["store_id"] == "s-1"
    assert "store_id" not in inspect.signature(feedback_observation).parameters, (
        "feedback_observation accepts a caller-chosen store_id"
    )
    with pytest.raises(TypeError):
        feedback_observation(verdict, observed_at=e6_as_of, store_id="rival-store")
