"""The buyer hop carries D55's three new slot facts to a shopper (``POST /buyer/shortlist/render``).

The exchange publishes them; this is the half that decides whether anybody sees them.

``message`` needed NO change on this side and that is worth a test rather than an assumption:
``labels.slot_rows`` already handed the raw slot to ``pitch.writing.store_pitch_of``, which
already read ``message`` off it, and ``RenderedPitch.store_pitch`` already declared it. The
entire gap was the exchange's published contract, so the first test here is the one that proves
the seam was never broken — only starved.

``fallback``, ``fallback_reason`` and ``product.identity`` needed all four hops, and the fourth
is the one that fails silently: ``RenderedSlot`` is a pydantic model whose ``extra`` defaults to
``ignore``, so a field added to ``LabelledSlot`` and dumped by its ``to_dict`` reaches nobody,
with no exception and no 500, unless it is declared on the response model too.

Every payload is dumped from the pinned ``ShortlistSlot``, so a field spelled wrongly here is a
test error (``extra="forbid"``) rather than a key the wire silently ignores.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from contracts.labels import LABEL_STORE_CONFIRMED, LABEL_UNVERIFIED
from contracts.protocol import Shortlist, ShortlistSlot
from fastapi.testclient import TestClient

RENDER = "/buyer/shortlist/render"
AUCTION = "auc-voices-1"

#: Leading and trailing whitespace on purpose — see :func:`test_the_shops_bytes_are_not_tidied`.
STORE_PITCH = "  We hand-knit every one of these in Ballarat.\n\n  "


@pytest.fixture()
def client():
    main = importlib.import_module("buyer_svc.main")
    with TestClient(main.create_app()) as test_client:
        yield test_client


def _slot(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "slot": "fit",
        "bid_ref": f"{AUCTION}:demo-woolworks",
        "fit_score": 0.91,
        "trust_summary": {"score": 0.72},
        "provenance_labels": [LABEL_STORE_CONFIRMED],
        "product": {
            "product_ref": "prod-merino-crew",
            "variant_ref": "var-m-navy",
            "identity": {
                "title": "Merino Crew Jumper",
                "brand": "Woolworks",
                "source": "neo4j-crawl:demo-woolworks:prod-merino-crew",
                "observed_at": "2026-09-01T00:00:00Z",
            },
        },
        "price": {"unit_price": 78.0, "total_price": 78.0, "currency": "USD"},
        "message": STORE_PITCH,
        "fallback": False,
    }
    fields.update(overrides)
    return ShortlistSlot(**fields).model_dump(mode="json")


def _render(client: TestClient, *slots: dict[str, Any]) -> list[dict[str, Any]]:
    body = Shortlist(auction_id=AUCTION, slots=list(slots)).model_dump(mode="json")
    response = client.post(RENDER, json={"shortlist": body})
    assert response.status_code == 200, response.text
    return response.json()["slots"]


# =====================================================================================
# The shop's own voice
# =====================================================================================
def test_a_slot_carrying_a_message_renders_both_voices_and_labels_each(client) -> None:
    """The product's central promise, end to end on this hop.

    ``platform_case`` is Proxyshop's own, constrained to facts it holds. ``store_pitch`` is the
    shop's, and the two are never merged: ``voices`` says which are being shown and in what
    order, so a shopper reading one can tell whose it is.
    """
    [slot] = _render(client, _slot())

    pitch = slot["pitch"]
    assert pitch is not None, slot
    assert pitch["store_pitch"] == STORE_PITCH, pitch
    assert "store" in pitch["voices"], pitch
    assert pitch["platform_case_source"], "the platform's case must say how it was authored"


def test_the_shops_bytes_are_not_tidied_anywhere_on_this_hop(client) -> None:
    """VERBATIM, whitespace included.

    ``store_pitch_of`` returns the seller's bytes unchanged and ``wire.ts::readPitch`` refuses to
    trim them, both because this is the last hop before a person reads them. A strip introduced
    here would falsify both promises while every other assertion in this file stayed green.
    """
    [slot] = _render(client, _slot())

    published = slot["pitch"]["store_pitch"]
    assert published == STORE_PITCH, repr(published)
    assert published.startswith("  ") and published.endswith("  "), repr(published)


def test_a_slot_with_no_message_says_so_rather_than_letting_the_platform_stand_in(client) -> None:
    """A scraped shop has no advocate and no voice, and the response must not invent one."""
    [slot] = _render(client, _slot(message=None, provenance_labels=[LABEL_UNVERIFIED]))

    assert slot["pitch"] is None or slot["pitch"]["store_pitch"] is None, slot


# =====================================================================================
# Whose price, and whose name
# =====================================================================================
def test_the_served_slot_says_whose_price_it_is(client) -> None:
    """Three states, all the way through, and none of them collapsed into another."""
    [real] = _render(client, _slot(fallback=False))
    assert real["fallback"] is False, real
    assert real["fallback_reason"] is None, real

    [stand_in] = _render(
        client,
        _slot(fallback=True, fallback_reason="no_response", message=None),
    )
    assert stand_in["fallback"] is True, stand_in
    assert stand_in["fallback_reason"] == "no_response", stand_in

    # A shortlist from a producer older than the field says nothing, and "nothing" must not
    # arrive as "this was a real bid" — that is the whole reason the field is nullable.
    [silent_producer] = _render(client, _slot(fallback=None))
    assert silent_producer["fallback"] is None, silent_producer
    assert silent_producer["fallback_reason"] is None, silent_producer


def test_a_reason_is_never_shown_beside_a_price_a_store_actually_quoted(client) -> None:
    """A reason carried next to a real bid would be read as a reason about that bid."""
    [slot] = _render(client, _slot(fallback=False, fallback_reason="no_response"))

    assert slot["fallback"] is False, slot
    assert slot["fallback_reason"] is None, slot


def test_the_served_slot_names_the_product_in_the_platforms_words(client) -> None:
    """A shopper reads a name, and is told whose name it is.

    ``source`` is carried through deliberately: this package renders no title of its own, and a
    name with no traceable origin is indistinguishable from one somebody made up.
    """
    [slot] = _render(client, _slot())

    identity = slot["product"]["identity"]
    assert identity["title"] == "Merino Crew Jumper", slot
    assert identity["brand"] == "Woolworks", slot
    assert identity["source"] == "neo4j-crawl:demo-woolworks:prod-merino-crew", slot
    assert identity["observed_at"] == "2026-09-01T00:00:00Z", slot
    # The REFERENCES are untouched — they are what the accept path resolves against.
    assert slot["product"]["product_ref"] == "prod-merino-crew", slot
    assert slot["product"]["variant_ref"] == "var-m-navy", slot


def test_an_identity_with_no_source_is_not_rendered_at_all(client) -> None:
    """A title this hop cannot attribute is a title it does not show.

    The pinned model requires ``source``, so this drives the shape a producer OUTSIDE the model
    could still send — the shortlist reaches this route as a plain dict, which is exactly how a
    hand-built body arrives.
    """
    body = Shortlist(auction_id=AUCTION, slots=[_slot()]).model_dump(mode="json")
    body["slots"][0]["product"]["identity"] = {"title": "Merino Crew Jumper"}

    response = client.post(RENDER, json={"shortlist": body})
    assert response.status_code == 200, response.text

    [slot] = response.json()["slots"]
    assert slot["product"]["identity"] is None, slot
    assert slot["product"]["product_ref"] == "prod-merino-crew", "the ref must survive regardless"


def test_a_slot_with_no_identity_still_carries_its_reference(client) -> None:
    """The exchange holds no crawled snapshot for every product, and a ref is still a slot."""
    [slot] = _render(client, _slot(product={"product_ref": "prod-merino-crew"}))

    assert slot["product"]["product_ref"] == "prod-merino-crew", slot
    assert slot["product"]["identity"] is None, slot
    assert slot["product"]["variant_ref"] is None, slot
