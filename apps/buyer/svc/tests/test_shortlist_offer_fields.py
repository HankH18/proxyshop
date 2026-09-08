"""R2's other three things on a slot — PRODUCT, PRICE, COMMITMENTS — through the served route.

The exchange now publishes ``product``, ``price`` and ``commitments`` on every shortlist slot
(``contracts.protocol.ShortlistSlot``). Until this file existed the buyer hop dropped all
three: ``LabelledSlot`` carried neither, ``LabelledSlot.to_dict`` therefore could not emit
them, and ``RenderedSlot`` — a pydantic model whose ``extra`` defaults to ``ignore`` — would
have swallowed them even if it had. So a shopper driving ``POST /buyer/shortlist/render``
could read a fit score and a provenance label and could not read what the thing was or what
it cost.

Everything here is asserted on a **served response**, not on a function's return value, and
the payloads are built by dumping the **pinned contract model** rather than by hand. A
hand-written dict is a guess about the wire; ``ShortlistSlot(...).model_dump(mode="json")``
is the wire, and ``extra="forbid"`` on that model means a field this file spells wrongly is
a test error rather than a silently ignored key.

ABSENCE IS NOT AN ERROR, and it is not a zero. An R10 fallback bid carries no commitments
and a roster row with no readable list price carries no price; both are ``null`` here, never
``0``, never ``[]``, never a crash. Three separate tests hold that, because each of the
three is a different way for a screen to lie about what a store offered.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from contracts.labels import LABEL_FROM_THEIR_WEBSITE, LABEL_STORE_CONFIRMED, LABEL_UNVERIFIED
from contracts.protocol import Claim, Provenance, Shortlist, ShortlistSlot
from fastapi.testclient import TestClient

RENDER = "/buyer/shortlist/render"

AUCTION = "auc-offer-1"


@pytest.fixture()
def client():
    main = importlib.import_module("buyer_svc.main")
    with TestClient(main.create_app()) as test_client:
        yield test_client


def _claim(key: str, value: Any, source: str, *, unit: str | None = None) -> dict[str, Any]:
    """One published commitment, through the pinned ``Claim`` model."""
    return Claim(
        key=key,
        value=value,
        unit=unit,
        provenance=Provenance(
            source=source,  # type: ignore[arg-type]
            ref=f"ref://{key}",
            observed_at="2026-09-06T00:00:00Z",
            authority_rank=1,
        ),
    ).model_dump(mode="json")


def _slot(**overrides: Any) -> dict[str, Any]:
    """One shortlist slot, dumped from the pinned model so the shape is the wire's."""
    fields: dict[str, Any] = {
        "slot": "fit",
        "bid_ref": f"{AUCTION}:demo-woolworks",
        "fit_score": 0.91,
        "trust_summary": {"score": 0.72},
        "provenance_labels": [LABEL_STORE_CONFIRMED],
        "product": {"product_ref": "prod-merino-crew", "variant_ref": "var-m-navy"},
        "price": {
            "unit_price": 78.0,
            "total_price": 78.0,
            "currency": "USD",
            "discount": {"type": "percent", "value": 10.0},
            "expires_at": "2026-09-06T12:00:00Z",
        },
        "commitments": [
            _claim("free_returns", True, "owner_statement"),
            _claim("ships_in_days", 2, "scraped", unit="days"),
        ],
    }
    fields.update(overrides)
    return ShortlistSlot(**fields).model_dump(mode="json")


def _render(client: TestClient, *slots: dict[str, Any]) -> list[dict[str, Any]]:
    """Drive the real route and hand back the slots it served."""
    body = Shortlist(auction_id=AUCTION, slots=list(slots)).model_dump(mode="json")
    response = client.post(RENDER, json={"shortlist": body})
    assert response.status_code == 200, response.text
    served = response.json()["slots"]
    assert isinstance(served, list)
    return served


def test_the_served_slot_carries_the_product_the_exchange_named(client) -> None:
    """WHICH catalogue thing this slot offers survives the buyer hop."""
    [slot] = _render(client, _slot())
    assert slot["product"]["product_ref"] == "prod-merino-crew"
    assert slot["product"]["variant_ref"] == "var-m-navy"
    # `null`, and that is the honest answer rather than a gap: this slot fixture carries no
    # `identity`, so the exchange named no crawled title for it and the buyer invents none.
    assert slot["product"]["identity"] is None


def test_the_served_slot_carries_the_price_the_store_is_asking(client) -> None:
    """Both prices, the currency, the stated discount and the expiry, unmodified."""
    [slot] = _render(client, _slot())
    price = slot["price"]
    assert price["unit_price"] == 78.0
    assert price["total_price"] == 78.0
    assert price["currency"] == "USD"
    assert price["discount"] == {"type": "percent", "value": 10.0, "provenance": None}
    assert price["expires_at"] == "2026-09-06T12:00:00Z"


def test_every_commitment_reaches_the_screen_with_its_own_provenance_label(client) -> None:
    """The buyer's only signal of what has been checked, per promise, not per slot.

    The slot's own ``provenance_labels`` are one aggregate line. A shopper deciding whether
    to believe "free returns" needs the label for *that* promise, and the two commitments
    here deliberately have different ones.
    """
    [slot] = _render(client, _slot())
    commitments = slot["commitments"]
    assert [row["key"] for row in commitments] == ["free_returns", "ships_in_days"]
    assert commitments[0]["value"] is True
    assert commitments[0]["label"] == LABEL_STORE_CONFIRMED
    assert commitments[1]["value"] == 2
    assert commitments[1]["unit"] == "days"
    assert commitments[1]["label"] == LABEL_FROM_THEIR_WEBSITE


def test_a_seller_asserted_commitment_is_shown_and_is_never_dressed_as_evidence(client) -> None:
    """R2's whole point: an unevidenced promise reads as unverified, not as store-confirmed."""
    [slot] = _render(client, _slot(commitments=[_claim("gift_wrap", True, "seller_asserted")]))
    [commitment] = slot["commitments"]
    assert commitment["label"] == LABEL_UNVERIFIED
    assert commitment["label"] != LABEL_STORE_CONFIRMED


def test_a_fallback_bid_with_no_commitments_serves_null_and_not_an_empty_list(client) -> None:
    """``[]`` reads as "this store committed to nothing". ``null`` says the exchange sent none."""
    [slot] = _render(client, _slot(commitments=None))
    assert slot["commitments"] is None


def test_a_slot_with_no_price_serves_null_and_never_a_zero(client) -> None:
    """A zero is the cheapest number there is and would win every comparison a shopper makes."""
    [slot] = _render(client, _slot(price=None))
    assert slot["price"] is None


def test_a_slot_with_no_product_serves_null(client) -> None:
    [slot] = _render(client, _slot(product=None))
    assert slot["product"] is None


def test_a_half_price_is_not_published_as_a_whole_one(client) -> None:
    """Unit without total invites comparing two quantities as if they were one offer.

    Not reachable through the pinned model, which requires both — so this drives the route
    with the raw dict a non-conforming producer would send. The route accepts
    ``shortlist: dict[str, Any]``, so this is input the service really can be handed.
    """
    raw = _slot()
    raw["price"] = {"unit_price": 78.0, "currency": "USD"}
    response = client.post(RENDER, json={"shortlist": {"auction_id": AUCTION, "slots": [raw]}})
    assert response.status_code == 200, response.text
    assert response.json()["slots"][0]["price"] is None


def test_an_unreadable_price_is_absence_and_not_a_five_hundred(client) -> None:
    """A malformed price must not turn a buyer-facing route into a crash."""
    raw = _slot()
    raw["price"] = {"unit_price": "seventy-eight", "total_price": None}
    response = client.post(RENDER, json={"shortlist": {"auction_id": AUCTION, "slots": [raw]}})
    assert response.status_code == 200, response.text
    assert response.json()["slots"][0]["price"] is None


def test_a_commitment_with_no_readable_key_is_dropped_and_the_rest_survive(client) -> None:
    """A promise with nothing a shopper could read is not a promise. Its neighbours stay."""
    raw = _slot()
    raw["commitments"] = [{"value": True}, _claim("free_returns", True, "owner_statement")]
    response = client.post(RENDER, json={"shortlist": {"auction_id": AUCTION, "slots": [raw]}})
    assert response.status_code == 200, response.text
    [commitment] = response.json()["slots"][0]["commitments"]
    assert commitment["key"] == "free_returns"


def test_the_three_new_fields_never_disturb_the_labels_the_exchange_supplied(client) -> None:
    """Honest traffic that carried none of the three still labels exactly as it did."""
    bare = {
        "slot": "value",
        "bid_ref": f"{AUCTION}:demo-fastfleece",
        "fit_score": 0.4,
        "trust_summary": {},
        "provenance_labels": [LABEL_FROM_THEIR_WEBSITE],
    }
    [slot] = _render(client, ShortlistSlot(**bare).model_dump(mode="json"))
    assert slot["provenance_labels"] == [LABEL_FROM_THEIR_WEBSITE]
    assert slot["labels_source"] == "exchange"
    assert slot["product"] is None
    assert slot["price"] is None
    assert slot["commitments"] is None


def test_a_commitment_whose_source_nobody_published_a_label_for_is_never_store_confirmed(
    client,
) -> None:
    """The exact R2 failure: a source that fell through a ``.get`` rendered as evidence.

    ``seller_asserted`` is PINNED, so it reaches ``unverified`` through the published table
    and cannot catch a bad default. An unpinned source is the case that can, so it gets its
    own test: the promise is still shown — hiding it would hide something the store said —
    and it is shown as something nobody has checked.
    """
    raw = _slot()
    raw["commitments"] = [
        {
            "key": "carbon_neutral",
            "value": True,
            "provenance": {
                "source": "a_source_this_build_has_never_heard_of",
                "ref": "ref://x",
                "observed_at": "2026-09-06T00:00:00Z",
                "authority_rank": 1,
            },
        },
        {"key": "gift_note", "value": True},  # no provenance at all
    ]
    response = client.post(RENDER, json={"shortlist": {"auction_id": AUCTION, "slots": [raw]}})
    assert response.status_code == 200, response.text
    commitments = response.json()["slots"][0]["commitments"]
    assert [row["key"] for row in commitments] == ["carbon_neutral", "gift_note"]
    assert [row["label"] for row in commitments] == [LABEL_UNVERIFIED, LABEL_UNVERIFIED]
