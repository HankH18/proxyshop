"""Reproductions for the untrusted-roster surface of ``POST /auctions`` — T-223 and T-224.

Every test in this file drives the **real HTTP door**, because that is where both defects were
measured and because a wall proved only at the library boundary is a wall whose wiring is
untested. ``apps/exchange/src/auction/routes.py``'s own ``RosterEntry`` docstring already writes
both of these down as an unclosed follow-up; these are the gates that follow up.

Each test is marked ``xfail(strict=True)`` and asserts the behaviour that SHOULD hold. A normal
run reports ``xfailed`` and stays green; the ticket's gate runs the same test with
``--runxfail`` and gets a real failure. When the defect is repaired the test XPASSes, which
``strict=True`` turns into a failure — so the marker cannot outlive the bug.
"""

from __future__ import annotations

from typing import Any

import pytest

#: A hook-shaped provenance block, so a declared discount here is legibly authorized paperwork
#: rather than something the wall refuses for an unrelated reason.
HOOK_PROVENANCE: dict[str, Any] = {
    "source": "envelope_rule",
    "ref": "envelope:store-1:v3#max_discount_pct@prod-1",
    "observed_at": "2026-01-01T00:00:00Z",
    "authority_rank": 1,
}

#: The roster row that authorizes *everything*. ``max_discount_pct`` arrives on an
#: unauthenticated request body, so 100 is one keystroke away for any caller.
CAP_100_ROW: dict[str, Any] = {
    "store_id": "store-1",
    "tier": 1,
    "product_ref": "prod-1",
    "list_price": 100.0,
    "max_discount_pct": 100.0,
}

#: The same product, with no authorized depth stated at all.
UNCAPPED_ROW: dict[str, Any] = {
    "store_id": "store-1",
    "tier": 1,
    "product_ref": "prod-1",
    "list_price": 100.0,
}

#: A row that prices the product at nothing. ``list_price`` is ``Field(ge=0.0)``, so this body
#: is accepted by the request model today.
ZERO_PRICED_ROW: dict[str, Any] = {
    "store_id": "store-1",
    "tier": 1,
    "product_ref": "prod-1",
    "list_price": 0.0,
}


def _offer(unit: Any, total: Any, depth: Any = None, product_ref: str = "prod-1") -> dict:
    body: dict[str, Any] = {
        "product_ref": product_ref,
        "unit_price": unit,
        "total_price": total,
        "currency": "USD",
        "expires_at": "2999-01-01T00:00:00Z",
    }
    if depth is not None:
        body["discount"] = {
            "type": "percentage",
            "value": depth,
            "provenance": dict(HOOK_PROVENANCE),
        }
    return body


def _post(row: dict, priced: dict | None, *, timeout: float = 1.0):
    """POST one auction with one rostered, eligible store answering ``priced``.

    ``priced=None`` makes the store silent, so the auction falls back to the roster's list price.
    ``raise_server_exceptions=False`` is deliberate: an unhandled exception out of the route is
    exactly what T-224 is about, and this test must OBSERVE it as a 500 rather than re-raise it
    into the test runner where it would look like a broken test.
    """
    from exchange.auction.routes import configure_auctions  # noqa: PLC0415
    from exchange.eligibility import ELIGIBLE, StaticSellerEligibility  # noqa: PLC0415
    from exchange.main import create_app  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    class Solicitor:
        def solicit(self, store: Any) -> dict | None:
            if priced is None:
                return None
            store_id = store["store_id"] if isinstance(store, dict) else store.store_id
            return {
                "store_id": store_id,
                "bid": {
                    "auction_id": "auc-1",
                    "store_id": store_id,
                    "offer": dict(priced),
                    "claims": [],
                },
            }

        __call__ = solicit

    app = create_app()
    configure_auctions(
        app,
        solicitor=Solicitor(),
        eligibility=StaticSellerEligibility({row["store_id"]: ELIGIBLE}),
    )
    return TestClient(app, raise_server_exceptions=False).post(
        "/auctions",
        json={
            "intent": {"intent_id": "intent-1", "cluster_id": "cluster-1"},
            "roster": [row],
            "bid_timeout_seconds": timeout,
        },
    )


# =====================================================================================
# T-223 — the price floor is an exact equality, so any positive price clears it
# =====================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-223: contracts/boundary.py's zero-price floor tests `priced == 0.0`, not a "
        "proportional floor, so under max_discount_pct: 100 an offer at unit_price 0.001 buys a "
        "100.00 product through the real POST /auctions door; remove this marker with the fix"
    ),
)
@pytest.mark.parametrize("depth", [None, 100.0])
def test_t223_one_tenth_of_a_cent_does_not_buy_a_hundred_dollar_product(depth: Any) -> None:
    """A price of nothing-but-not-exactly-zero is still the absence of a price.

    ``boundary.py``'s floor is the one relation in the price walk that no declared depth can
    satisfy — every other check is an inequality against the authorized depth, and an authorized
    100 makes all of them true at once. But the floor is written ``priced == 0.0``, an EXACT
    equality, so the attacker's answer is to write ``0.001`` instead of ``0.0``. Measured through
    this exact call before the fix, in both spellings::

        HTTP 201  entries=[{fallback: false, unit_price: 0.001, fallback_reason: null}]

    R10 compels the exchange to admit an aggressive undercut; it does not compel it to rank a
    thousandth of a cent as the winning consideration for a product its own roster prices at
    100.00. ``depth=None`` is the silent spelling and ``depth=100.0`` the declared one — the
    floor is meant to hold under both, and today it holds under neither.
    """
    posted = _post(CAP_100_ROW, _offer(0.001, 0.001, depth=depth))

    assert posted.status_code == 201, posted.text
    entry = posted.json()["entries"][0]
    assert entry["fallback"] is True, entry
    assert entry["fallback_reason"] == "bid_price_unreconcilable", entry
    # R10 is not negotiable: the refused store is still represented, at its catalog price.
    assert entry["unit_price"] == 100.0, entry


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-223: on a roster row stating no max_discount_pct, collect.py's _is_judged returns "
        "False for an undeclared price, so the price walk never runs and unit_price 1e-09 is "
        "HTTP 201 as a rankable bid; remove this marker with the fix"
    ),
)
def test_t223_an_undeclared_billionth_of_a_cent_is_not_a_rankable_bid() -> None:
    """The second, separate failure: the wall is not merely too loose, it is SKIPPED.

    ``_is_judged`` (apps/exchange/src/auction/collect.py) admits three ways for the exchange to
    hold something to judge an offer against — a declared discount, a rostered
    ``max_discount_pct``, or a price of nothing. A store that declares no discount, on a row that
    authorizes no depth, at a price that is positive, satisfies none of them, so
    ``_price_refusal`` returns ``[]`` without ever calling the boundary. Measured::

        HTTP 201  entries=[{fallback: false, unit_price: 1e-09, fallback_reason: null}]

    This is NOT the frozen R10 case and must not be confused with it. That case
    (``test_r10_still_admits_an_undeclared_undercut_where_nothing_is_authorized``) pins 99.00,
    80.00 and 1.00 on this same row as admitted, and they must stay admitted — a fix here has to
    leave a whole dollar alone. One billionth of a cent is below the smallest unit the currency
    has; it is not an undercut, it is the absence of a price wearing a positive sign.
    """
    posted = _post(UNCAPPED_ROW, _offer(1e-09, 1e-09))

    assert posted.status_code == 201, posted.text
    entry = posted.json()["entries"][0]
    assert entry["fallback"] is True, entry
    assert entry["unit_price"] == 100.0, entry

    # The paired control, in the same test so a fix cannot satisfy the assertion above by
    # closing the auction: the frozen R10 undercut on this identical row stays admitted.
    honest = _post(UNCAPPED_ROW, _offer(80.0, 80.0))
    assert honest.status_code == 201, honest.text
    kept = honest.json()["entries"][0]
    assert kept["fallback"] is False, kept
    assert kept["unit_price"] == 80.0, kept


# =====================================================================================
# T-224 — `list_price: 0.0` is an accepted roster value, and it removes the protection
# =====================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-224: list_price: 0.0 is an accepted roster value (Field(ge=0.0)); on such a row "
        "_is_judged returns False, so an unparseable store price reaches float() unguarded and "
        "raises ValueError out of the auction as an unauthenticated HTTP 500; remove this "
        "marker with the fix"
    ),
)
def test_t224_an_unreadable_store_price_never_becomes_a_server_error() -> None:
    """One keystroke in an unauthenticated request body takes the auction down.

    ``_priced_at_nothing`` is what normally converts a store's unreadable price into a fallback
    instead of a ``ValueError`` — and it returns ``False`` when ``listed <= 0``, so the whole
    protection is conditional on a number the *caller* supplies. Write ``list_price: 0.0`` in the
    roster and the guard is off; the store answers ``unit_price: "cheap"``, ``BidEntry.unit_price``
    calls ``float()`` on it, and the exception escapes ``collect_bids``, ``solicit_bids`` and the
    route. Measured through this exact call::

        HTTP 500 Internal Server Error
        ValueError("could not convert string to float: 'cheap'")

    Store-shaped data is never trusted anywhere else in this module — the unparseable *arrival
    stamp* was fixed for precisely this reason — and no roster value a caller writes may switch
    that off. Either shape of fix satisfies this: refuse the row at the request model, or judge
    the offer on it. Nothing the caller can write may produce a 5xx.
    """
    posted = _post(ZERO_PRICED_ROW, _offer("cheap", "cheap"))

    assert posted.status_code < 500, (posted.status_code, posted.text[:400])


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-224: a roster row of list_price: 0.0 mints a 0.00 rankable fallback offer for a "
        "silent store (HTTP 201, entries=[{fallback: true, unit_price: 0.0}]) — the same free "
        "item the required-list_price 422 closed, reached by writing the zero instead of "
        "omitting the field; remove this marker with the fix"
    ),
)
def test_t224_a_roster_row_that_prices_nothing_cannot_mint_a_free_offer() -> None:
    """The free item that needs no bid at all, reached by writing ``0.0`` instead of omitting it.

    ``list_price`` was made required precisely because a row naming no price produced a 0.00
    *fallback* offer for a silent store, and a fallback is a real, rankable offer that wins every
    ranking there is. ``Field(ge=0.0)`` left the other door open. Measured, with nobody answering::

        HTTP 201  entries=[{fallback: true, unit_price: 0.0, fallback_reason: 'no_response'}]

    A caller that cannot price a product cannot auction it. The assertion below accepts either
    repair — a 4xx on the row, or an auction that answers without a zero-priced offer — and
    refuses only the outcome that is actually wrong: a rankable offer of nothing.
    """
    posted = _post(ZERO_PRICED_ROW, None)

    assert posted.status_code < 500, (posted.status_code, posted.text[:400])
    if posted.status_code == 201:
        entries = posted.json()["entries"]
        free = [e for e in entries if e["unit_price"] == 0.0 or e["total_price"] == 0.0]
        assert not free, f"a rankable offer of nothing was minted from the roster alone: {free}"
    else:
        assert posted.status_code == 422, (posted.status_code, posted.text[:400])
