"""``ShortlistSlot``'s D55 fields, and the one number that has to agree in three places.

The slot gained four things at ``SCHEMA_VERSION`` 3.0.0: the shop's own ``message``, ``fallback``
and ``fallback_reason`` for whose price it is, and ``ShortlistProduct.identity`` for a name a
person can read. This file pins the two properties a reader downstream is entitled to rely on.

**The bound is ONE rule, stated once and enforced at both ends.** The contract declares
``maxLength: 1200`` on ``message``; the exchange refuses to publish a longer one
(``exchange.ranking.serving.MAX_SLOT_MESSAGE_CHARS``) so a bidder cannot 500 a live auction
through the pinned model, and the buyer's renderer refuses to display one
(``buyer_svc.pitch.writing.MAX_STORE_PITCH_CHARS``) so nothing arrives that the screen will drop.
Three constants that must be one number, asserted against each other rather than restated — the
failure this prevents is an exchange publishing a string the renderer at the other end silently
throws away, which looks from a shopper's seat exactly like the defect this change closes.

**Nullability is load-bearing on ``fallback``**, so it is asserted rather than assumed: the field
has THREE states and a reader that collapses ``null`` into ``false`` presents a price nobody
quoted as a quote.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from contracts.protocol import SCHEMA_VERSION, ShortlistSlot

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schemas" / "protocol.schema.json").read_text()
)
SLOT = SCHEMA["$defs"]["ShortlistSlot"]
PRODUCT = SCHEMA["$defs"]["ShortlistProduct"]
IDENTITY = SCHEMA["$defs"]["ShortlistProductIdentity"]


def _slot(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "slot": "fit",
        "bid_ref": "auction-1:store-a",
        "fit_score": 0.5,
        "trust_summary": {"store_id": "store-a"},
        "provenance_labels": ["unverified"],
    }
    fields.update(overrides)
    return fields


def test_the_slot_still_refuses_everything_a_bidder_could_invent() -> None:
    """The refusal that had to survive D55's four new fields.

    ``extra="forbid"`` is why no store can assert its own score, label or verdict, and widening
    the object by a message is only safe while that stays true. ``message`` is admitted as an
    assertion to CHECK — the exchange decomposes it, grades every reading against its own
    catalogue snapshot and attests the verdicts under a MAC the bidder cannot compute.
    """
    with pytest.raises(ValidationError):
        ShortlistSlot(**_slot(intent_match=1.0))
    with pytest.raises(ValidationError):
        ShortlistSlot(**_slot(rank_score=1.0))
    assert SLOT["additionalProperties"] is False


@pytest.mark.parametrize("field", ["message", "fallback", "fallback_reason"])
def test_every_new_slot_field_is_optional_and_admits_null(field: str) -> None:
    """Required would have broken every existing producer on the same commit.

    And ``null`` is a real answer with a real meaning on each of them: no message, and — on
    ``fallback`` — a producer that did not say whose price this is, which is a THIRD state and
    not a spelling of ``false``.
    """
    assert field in SLOT["properties"], sorted(SLOT["properties"])
    assert field not in SLOT["required"], SLOT["required"]
    assert "null" in SLOT["properties"][field]["type"], SLOT["properties"][field]
    assert getattr(ShortlistSlot(**_slot()), field) is None
    assert getattr(ShortlistSlot(**_slot(**{field: None})), field) is None


def test_fallback_keeps_false_and_null_apart_through_the_model() -> None:
    """The distinction a consumer must not collapse, asserted on the dumped wire object."""
    assert ShortlistSlot(**_slot(fallback=False)).model_dump(mode="json")["fallback"] is False
    assert ShortlistSlot(**_slot(fallback=True)).model_dump(mode="json")["fallback"] is True
    assert ShortlistSlot(**_slot()).model_dump(mode="json")["fallback"] is None


def test_the_message_bound_is_one_number_in_all_three_places() -> None:
    """The contract's ``maxLength``, the exchange's refusal and the buyer's, asserted equal.

    Restating the number would let the three drift, and the quiet failure mode of drift is an
    exchange publishing prose the renderer at the other end drops — which a shopper cannot tell
    apart from the shop having said nothing at all.
    """
    from buyer_svc.pitch.writing import MAX_STORE_PITCH_CHARS
    from exchange.ranking.serving import MAX_SLOT_MESSAGE_CHARS

    published = SLOT["properties"]["message"]["maxLength"]
    assert published == MAX_SLOT_MESSAGE_CHARS == MAX_STORE_PITCH_CHARS, (
        f"contract={published} exchange={MAX_SLOT_MESSAGE_CHARS} buyer={MAX_STORE_PITCH_CHARS}"
    )

    ShortlistSlot(**_slot(message="a" * published))
    with pytest.raises(ValidationError):
        ShortlistSlot(**_slot(message="a" * (published + 1)))


def test_the_message_is_carried_verbatim_through_the_pinned_model() -> None:
    """No coercion, no strip. It is the seller's bytes and the model is one of the hops."""
    padded = "  a shop's own words.\n\n  "
    assert ShortlistSlot(**_slot(message=padded)).model_dump(mode="json")["message"] == padded


def test_the_product_identity_must_name_itself() -> None:
    """``source`` is REQUIRED, and that is traceability rather than bookkeeping.

    A rendered name a reader cannot trace back to the record that produced it cannot be told
    apart from one the exchange invented, and telling a name decided against the platform's crawl
    apart from one decided against an operator's deployment document is the whole reason the
    field exists.
    """
    assert sorted(IDENTITY["required"]) == ["source", "title"]
    assert IDENTITY["additionalProperties"] is False
    assert IDENTITY["properties"]["title"]["minLength"] == 1
    assert "identity" in PRODUCT["properties"]
    assert "identity" not in PRODUCT["required"]

    slot = ShortlistSlot(
        **_slot(
            product={
                "product_ref": "prod-1",
                "identity": {"title": "Merino Crew", "source": "neo4j-crawl:store-a:prod-1"},
            }
        )
    )
    assert slot.product is not None and slot.product.identity is not None
    assert slot.product.identity.title == "Merino Crew"

    with pytest.raises(ValidationError):
        ShortlistSlot(**_slot(product={"product_ref": "p", "identity": {"title": "No source"}}))


def test_the_wire_version_records_this_change() -> None:
    """A field added to a closed published body is MAJOR — the rule the 2.0.0 note establishes."""
    assert SCHEMA_VERSION.startswith("3."), SCHEMA_VERSION
