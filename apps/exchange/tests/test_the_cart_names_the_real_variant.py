"""The cart the shopper is handed has to name the variant that was actually offered.

Run it on its own::

    PROXYSHOP_WORKER=11 .venv/bin/python -m pytest \\
        apps/exchange/tests/test_the_cart_names_the_real_variant.py -q

What was measured, and why a unit test would not have caught it
---------------------------------------------------------------
``/cart/{variant}:{qty}`` is Shopify's own cart URL. ``default_permalink`` read
``offer.get("variant_ref") or offer.get("variant_id") or 1`` and the organic path put NEITHER
spelling on the offer it minted, so every organic accept in the nineteen-store demo handed the
shopper ``https://<store>/cart/1:1?discount=<code>``. Measured on the served artifact::

    grep -o "variant_ref" deploy/demo/exchange-deployment.json | wc -l   -> 0
    grep -o "variant_id"  deploy/demo/exchange-deployment.json | wc -l   -> 0

and the corpus's own native ids run 9 to 14 digits, so ``1`` names no variant any of those
nineteen stores issues — ``services/shopify-stub`` answers it
``404 {"errors": "Variant 1 is not available"}``. The protocol's own ``Offer.variant_ref``
docstring already forbade the reading that produced it: *"absent means 'the bid did not name
one', never 'the default variant'."*

Both tests here drive the real HTTP door — ``POST /auctions`` then
``POST /auctions/{id}/accept`` — because the defect was not "a field is unpopulated". It was
"the field is dropped somewhere between the request body and the permalink", and only the
served route crosses every hop that could drop it (``RosterEntry`` -> ``collect_bids`` ->
``_list_price_bid`` -> ranking -> ``accept`` -> ``SimulatedRedirectProvider.mint`` ->
``default_permalink``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from exchange.accept import use_registered_domains
from exchange.composition import ENV_DEPLOYMENT, ENV_DEPLOYMENT_JSON
from exchange.main import create_app
from fastapi.testclient import TestClient

#: A real Shopify numeric variant id, copied from ``fixtures/real-catalogs-demo``.
NATIVE_VARIANT = "43866134282275"

STORE_ID = "s1"
STORE_DOMAIN = "s1.example.com"

INTENT: dict[str, Any] = {
    "intent_id": "intent-variant-permalink-1",
    "cluster_id": "cluster-1",
    "query": "a walnut coffee table for the lounge",
    "hard_constraints": [],
    "preferences": [],
    "created_at": "2026-01-01T00:00:00Z",
    "schema_version": "1.0.0",
}


def _deployment_document() -> dict[str, Any]:
    """A deployment with no ``bid_endpoint``, so every store takes the ORGANIC path.

    That is the configuration the defect lives in: a hosted agent names its own variant
    (``store_agent.runtime.bidding._variant_ref``) and never reaches ``default_permalink``.
    """
    return {
        "sellers": [
            {
                "store_id": STORE_ID,
                "eligibility": "eligible",
                "registered_domain": STORE_DOMAIN,
            }
        ],
        "trust_snapshot": {
            "stores": {STORE_ID: {"store_id": STORE_ID, "blacklisted": False, "score": 0.8}}
        },
        "checkout_mode": "redirect",
    }


@pytest.fixture
def unwired() -> Iterator[None]:
    """Leave the process-wide registered-domain seam exactly as this file found it."""
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


@pytest.fixture
def deployed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unwired: None
) -> Iterator[TestClient]:
    """A client onto ``create_app()``, configured the way a deployment configures it."""
    document = tmp_path / "deployment.json"
    document.write_text(json.dumps(_deployment_document(), indent=2), encoding="utf-8")
    monkeypatch.setenv(ENV_DEPLOYMENT, str(document))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)
    with TestClient(create_app()) as client:
        yield client


def _open(client: TestClient, row: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/auctions",
        json={
            "intent": INTENT,
            "profile": {"pseudonym": "psn-variant-1", "buckets": {}},
            "roster": [row],
        },
    )
    assert response.status_code == 201, f"POST /auctions -> {response.status_code}: {response.text}"
    return response.json()


def _armed(body: dict[str, Any]) -> str:
    """This run really is an ORGANIC one, and it really produced a slot to accept."""
    entries = body.get("entries") or []
    assert entries, f"the auction collected no entries at all: {body}"
    assert all(entry.get("fallback") for entry in entries), (
        f"a store answered its solicitation, so this is not the organic path: {entries}"
    )
    slots = body["shortlist"]["slots"]
    assert slots, f"R10: a silent store must still reach the shortlist — {body.get('excluded')}"
    return str(slots[0]["bid_ref"])


def test_a_rostered_variant_reaches_the_cart_permalink(deployed: TestClient) -> None:
    """The caller named the variant; the shopper's cart URL must name the same one.

    This is the whole fix in one assertion: the id crosses ``RosterEntry`` (which declares no
    ``model_config``, so pydantic's default ``extra='ignore'`` silently discarded it),
    ``_list_price_bid`` (which built ``{product_ref, currency, expires_at, unit_price,
    total_price}`` and nothing else) and ``default_permalink``.
    """
    body = _open(
        deployed,
        {
            "store_id": STORE_ID,
            "tier": 1,
            "product_ref": "prod-walnut",
            "list_price": 549.0,
            "variant_ref": NATIVE_VARIANT,
        },
    )
    ref = _armed(body)

    # The shortlist publishes the variant it is about, so the row the shopper reads and the
    # cart they are handed name the same thing.
    slot = body["shortlist"]["slots"][0]
    assert slot["product"]["variant_ref"] == NATIVE_VARIANT, slot

    accepted = deployed.post(f"/auctions/{body['auction_id']}/accept", json={"bid_ref": ref})
    payload = accepted.json()
    assert accepted.status_code == 200, accepted.text
    assert payload["permalink_url"] == f"https://{STORE_DOMAIN}/cart/{NATIVE_VARIANT}:1", payload


def test_a_row_that_names_no_variant_is_never_sent_to_variant_one(
    deployed: TestClient,
) -> None:
    """No variant is not variant ``1``, on either side of the accept.

    ``1`` is not a neutral default — it is a *different, specific* variant. The corpus's own
    native ids run 9 to 14 digits (histogram over 28,134 records: 9->12, 10->3, 11->23,
    12->1, 13->73, 14->28,022), so ``1`` names no variant any of the nineteen recorded stores
    issues, and ``services/shopify-stub`` answers it ``404 Variant 1 is not available``.

    R10 is not traded away to get this: the silent store still reaches the shortlist and the
    accept still succeeds with no discount code. What changes is that the destination is one
    the platform can stand behind — the seller's own registered domain — instead of a cart
    for a variant nobody offered.
    """
    body = _open(
        deployed,
        {
            "store_id": STORE_ID,
            "tier": 1,
            "product_ref": "prod-walnut",
            "list_price": 549.0,
        },
    )
    ref = _armed(body)

    slot = body["shortlist"]["slots"][0]
    assert slot["product"].get("variant_ref") is None, (
        f"the shortlist published a variant nobody named: {slot['product']}"
    )

    accepted = deployed.post(f"/auctions/{body['auction_id']}/accept", json={"bid_ref": ref})
    payload = accepted.json()
    assert accepted.status_code == 200, accepted.text
    assert payload["code"] is None, f"a fallback minted a discount code: {payload}"
    handed = str(payload["permalink_url"])
    assert "/cart/1:" not in handed, f"the accept handed over a fabricated variant: {handed}"
    # Whatever it is, it is still on the seller's registered domain (S8).
    assert handed.startswith(f"https://{STORE_DOMAIN}/"), handed


def test_the_minted_permalink_names_the_offers_variant_rather_than_variant_one() -> None:
    """The MINT path — the one that attaches a live single-use code — uses the real variant.

    ``SimulatedRedirectProvider.mint`` is the one non-test caller of ``default_permalink``;
    ``ShopifyCheckoutProvider`` takes the merchant's own URL and never reaches it. Minting
    first and building the URL second is what made ``1`` expensive rather than merely wrong: a
    real code is created and recorded, and then attached to a cart the storefront answers
    ``404 Variant 1 is not available``.

    Both spellings are asserted because hosted agents use both —
    ``store_agent.runtime.bidding.VARIANT_REF_KEYS`` is ``("variant_ref", "variant_id")``, in
    that order — and both name the storefront's own id.

    What this test does NOT assert is a refusal when the offer names neither. That refusal is
    the honest behaviour and it is deliberately not shipped; see ``default_permalink``'s
    docstring for the measurement (62 tests in twelve files, the frozen acceptance goals among
    them, all of whose fixtures build offers with no variant).
    """
    from exchange.checkout.provider import CheckoutRequest, default_permalink

    def permalink(offer: dict[str, Any]) -> str:
        return default_permalink(
            CheckoutRequest(
                auction_id="auction-1",
                bid_ref="bid-1",
                store_id=STORE_ID,
                store_domain=STORE_DOMAIN,
                offer=offer,
                now=0.0,
            ),
            "PSX-CODE1",
        )

    base = {"product_ref": "prod-walnut", "unit_price": 549.0, "currency": "USD"}
    expected = f"https://{STORE_DOMAIN}/cart/{NATIVE_VARIANT}:1?discount=PSX-CODE1"
    assert permalink({**base, "variant_ref": NATIVE_VARIANT}) == expected
    assert permalink({**base, "variant_id": NATIVE_VARIANT}) == expected
    # `variant_ref` wins when both are stated, which is the order the protocol declares.
    assert permalink({**base, "variant_ref": NATIVE_VARIANT, "variant_id": "1"}) == expected
