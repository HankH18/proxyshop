"""The ranking expiry filter reads the instant the SCHEMA permits (T-182, one file over).

Found while auditing ESC-020's own fix for other places the exchange trusts a field it has
not established. This one is not a trust defect — it is fail-closed — and that is exactly
why nothing had noticed it: an eligibility gate that refuses every conformant offer looks
like a strict gate and behaves like an always-empty shortlist.

The contradiction, measured before the fix:

* ``contracts.Offer.expires_at`` is ``str | None`` with ``format: date-time``
  (``packages/contracts/generated/python/protocol.py:297``), and ``validate_bid`` parses it
  with ``contracts.parse_timestamp``;
* ``packages/store-agent``'s hosted agent emits exactly that — ``AuctionContext``'s
  ``offer_expires_at`` returns ``str | None``;
* ``ranking/filters.py::expiry_reason`` parsed it with bare ``float()``, so
  ``"2030-01-01T00:00:00Z"`` came back ``expired_offer: … is not a readable instant``.

So every bid a real hosted store produced was excluded from every shortlist, and no test in
the tree could see it because every fixture here — the frozen acceptance suite included —
writes a float epoch.

``checkout/codes.py::expiry_epoch`` had already ended the same contradiction on the MINTING
path under T-182, including the subtlety that makes the obvious implementation wrong: try
``float()`` first and ISO-8601 basic format (``"20260903"``) reads as epoch second 20260903,
i.e. 23 August 1970 — a bid that validates at the door with a future expiry and mints a code
that expired fifty-six years ago. The ranking filter now imports that function rather than
growing a third parser, which is the same discipline ``domain_reason`` follows for
``is_on_domain``: a boundary with two implementations is a boundary with two answers.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

NOW = 1_700_000_000.0

# (label, expires_at as it arrives, is it live at NOW)
SPELLINGS: list[tuple[str, Any, bool]] = [
    ("rfc3339 future — the only shape the schema permits", "2030-01-01T00:00:00Z", True),
    ("rfc3339 past", "2000-01-01T00:00:00Z", False),
    ("rfc3339 with an explicit offset", "2030-01-01T00:00:00+00:00", True),
    ("iso-8601 basic date — must NOT read as epoch 20260903", "20260903", True),
    ("float epoch future — every fixture in this tree", 2_000_000_000.0, True),
    ("float epoch past", 1_600_000_000.0, False),
    ("numeric string future", "2000000000", True),
    ("unreadable", "not-an-instant", False),
]


@pytest.mark.parametrize(("label", "raw", "live"), SPELLINGS, ids=[row[0] for row in SPELLINGS])
def test_every_expiry_spelling_the_schema_or_this_tree_produces_is_read(label, raw, live):
    """One parametrisation per spelling, each closing exactly one way in.

    The two directions are both asserted: a live offer must not be refused, and a dead or
    unreadable one must be. A filter that admitted everything would pass half of this table
    and a filter that refused everything would pass the other half.
    """
    from exchange.ranking.filters import expiry_reason

    reason = expiry_reason({"expires_at": raw}, NOW)
    if live:
        assert reason is None, f"{label}: a live offer was refused — {reason}"
    else:
        assert reason is not None, f"{label}: a dead or unreadable offer was admitted"
        assert "expired_offer" in reason, reason


def test_the_ranking_filter_and_the_code_mint_read_one_instant():
    """Imported, not restated. Two parsers eventually disagree about some instant, and the
    instant they disagree about is one where an offer ranks and then cannot be paid for."""
    import exchange.ranking.filters as filters
    from exchange.checkout.codes import expiry_epoch

    assert filters.expiry_epoch is expiry_epoch


def test_a_contract_shaped_expiry_is_ranked_over_the_served_door():
    """The same property through `POST /auctions`, because that is where it mattered."""
    from exchange.auction.routes import configure_auctions
    from exchange.checkout.sellers import StaticRegisteredDomains
    from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
    from exchange.main import create_app
    from exchange.ranking.serving import configure_ranking
    from fastapi.testclient import TestClient

    store = "iso-store"
    domain = f"{store}.example.com"
    # An RFC-3339 instant, which is the only shape `contracts.Offer` allows, well in the
    # future so the assertion can never turn on the clock.
    expires_at = "2038-01-01T00:00:00Z"

    class _Solicitor:
        def solicit(self, entry):
            return {
                "store_id": store,
                "received_at": time.time(),
                "bid": {
                    "store_id": store,
                    "offer": {
                        "product_ref": "product-1",
                        "unit_price": 100.0,
                        "total_price": 100.0,
                        "currency": "USD",
                        "checkout_url": f"https://{domain}/cart/1:1",
                        "expires_at": expires_at,
                    },
                    "claims": [],
                    "agent_version": "1.0.0",
                    "schema_version": "1.0.0",
                },
            }

        __call__ = solicit

    app = create_app()
    configure_auctions(
        app,
        solicitor=_Solicitor(),
        eligibility=StaticSellerEligibility({store: ELIGIBLE}),
    )
    configure_ranking(
        app,
        trust_snapshot={store: {"blacklisted": False, "score": 0.6}},
        registered_domains=StaticRegisteredDomains({store: domain}),
    )
    response = TestClient(app).post(
        "/auctions",
        json={
            # No hard constraints: this test is about the expiry filter, and a constrained
            # intent would need a catalogue wired for a reason it is not about.
            "intent": {"intent_id": "intent-1", "cluster_id": "cluster-1", "hard_constraints": []},
            "roster": [
                {"store_id": store, "tier": 1, "product_ref": "product-1", "list_price": 100.0}
            ],
            "bid_timeout_seconds": 2.0,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    excluded = {row["store_id"]: row["exclusion_reasons"] for row in body["excluded"]}
    assert store not in excluded, f"a schema-shaped expiry was refused: {excluded}"
    assert [row["store_id"] for row in body["ranked"]] == [store], body
    assert len(body["shortlist"]["slots"]) == 1, body["shortlist"]
