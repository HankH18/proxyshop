"""Reproductions for the cycle-16 rung-2 verifier's exchange findings — T-270 to T-277.

Every gate here arrived ``xfail(strict=True)`` and asserts the behaviour that SHOULD hold, so a
normal run reports ``xfailed`` and the repo-wide build gate stays green, while each ticket's own
gate runs the same node under ``--runxfail`` and gets a real failure. ``strict=True`` is what
forces the marker to be deleted with the fix: a repaired defect makes the test XPASS, and a strict
XPASS is a failure.

Two of the findings in this batch are not shaped like that, and they are written down here rather
than faked into markers:

* **T-271** is a *missing consumer*, not a wrong behaviour. The price floor's two constants are
  mutation-invisible — ``MINIMUM_PAYABLE_AMOUNT`` can be 0.0, 0.5 or 1.0 and ``PRICE_FLOOR_FRACTION``
  can move four orders of magnitude with ``apps/exchange`` still green — because nothing asserts
  either magnitude. Today's *behaviour* in the blind band is correct, so there is no red to write:
  the deliverable is the assertion itself, and the four T-271 tests below are ordinary passing
  tests that kill every one of those mutants.
* **T-274** is a bookkeeping finding about a gate wired onto a closed ticket, and **T-275** lives
  in ``apps/trust``; neither belongs in this file.

The two library-door gates (T-272, T-273, T-277) drive :func:`exchange.auction.collect.collect_bids`
rather than ``POST /auctions`` on purpose. ``RosterEntry`` coerces and refuses before any of these
defects can be reached through HTTP, and ``collect.py``'s own module docstring already writes down
why that is not a defence: *"a repair that lives only in a request model is a repair a second
caller of ``collect_bids`` does not get"* — and ``orchestration/solicitation.py``,
``services/sim/src/runner.py`` and the frozen ``test_e3_exchange.py`` acceptance suite are all
second callers.
"""

from __future__ import annotations

from typing import Any

import pytest

#: A hook-shaped provenance block, so a declared discount is legibly authorized paperwork rather
#: than something the wall refuses for an unrelated reason.
HOOK_PROVENANCE: dict[str, Any] = {
    "source": "envelope_rule",
    "ref": "envelope:store-1:v3#max_discount_pct@prod-1",
    "observed_at": "2026-01-01T00:00:00Z",
    "authority_rank": 1,
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


def _client(row: dict, priced: dict | None):
    """A ``TestClient`` over one rostered, eligible store answering ``priced``.

    ``raise_server_exceptions=False`` is deliberate: an unhandled exception out of the app is what
    T-270 is about, and it has to be OBSERVED as a 500 rather than re-raised into the runner where
    it would look like a broken test.
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
    return TestClient(app, raise_server_exceptions=False)


def _post(row: dict, priced: dict | None, *, timeout: float = 1.0):
    """POST one auction with one rostered store, through the real HTTP door."""
    return _client(row, priced).post(
        "/auctions",
        json={
            "intent": {"intent_id": "intent-1", "cluster_id": "cluster-1"},
            "roster": [row],
            "bid_timeout_seconds": timeout,
        },
    )


def _post_raw(row: dict, body: str):
    """POST a request body **as text**, so the client cannot refuse to serialise it.

    ``httpx``'s ``json=`` helper encodes with ``allow_nan=False`` and raises on ``nan``/``inf``
    before a byte leaves the process — which is the client refusing to make the request, not the
    server refusing to answer it. A real attacker writes the bytes; so does this.
    """
    return _client(row, None).post(
        "/auctions", content=body, headers={"content-type": "application/json"}
    )


def _entry(response) -> dict:
    return response.json()["entries"][0]


def _collect(row: dict, offer: dict | None, *, received_at: float = 0.5, deadline: float = 1.0):
    """One rostered store through the library door, with ``offer`` as its on-time answer."""
    from exchange.auction.collect import collect_bids  # noqa: PLC0415

    responses = (
        []
        if offer is None
        else [
            {
                "store_id": row["store_id"],
                "received_at": received_at,
                "bid": {
                    "auction_id": "auc-1",
                    "store_id": row["store_id"],
                    "offer": offer,
                    "claims": [],
                },
            }
        ]
    )
    return collect_bids([row], responses, deadline)[0]


# =====================================================================================
# T-270 — the ERROR path 500s, so a rejected value is worse than an accepted one
# =====================================================================================

#: The roster row every T-270 body is built from, as raw JSON text with a ``%s`` where the
#: hostile number goes. Written as text because the numbers below cannot survive a JSON encoder.
_NAN_BODY = (
    '{"intent": {"intent_id": "intent-1", "cluster_id": "cluster-1"}, '
    '"roster": [{"store_id": "store-1", "tier": %s, "product_ref": "prod-1", '
    '"list_price": %s}], "bid_timeout_seconds": 1.0}'
)

_SANE_ROW: dict[str, Any] = {
    "store_id": "store-1",
    "tier": 1,
    "product_ref": "prod-1",
    "list_price": 100.0,
}


def test_t270_a_non_finite_number_in_the_request_body_is_never_a_server_error() -> None:
    """Nothing an unauthenticated caller can write may produce a 5xx — now a regression guard.

    THE MARKER IS GONE BECAUSE THE DEFECT IS CLOSED, and the history is worth keeping because the
    shape recurs. This node was written on ``repro/R-verifier``, which forked BEFORE the fix and
    merged AFTER it, so it arrived carrying ``xfail(strict=True)`` for a defect the tree no longer
    had. A strict xfail that starts passing is a FAILURE, so the stale marker reddened the build
    at the merge rather than at the branch — measured here: ``1 failed ... [XPASS(strict)]``.

    What closed it: ``RenderableValidationErrorRoute`` / ``RenderableJSONResponse`` on the auction
    router (``apps/exchange/src/auction/routes.py``), which render a rejected non-finite value into
    a serialisable 422 instead of exploding inside the 422 the rejection builds. The sibling node
    that the fixing lane owned, ``test_t270_no_field_of_any_request_can_produce_a_5xx``, had ITS
    marker removed in the same commit; this one was simply not re-measured against the merged tree.

    All four bodies below now answer 422. The assertions are unchanged — nothing was weakened to
    make this pass; only the marker was removed.

    ``1e400`` is the sharp end of this: it is **legal RFC-8259 JSON**, needing no malformed body
    and no lenient parser, and it parses to ``inf``. ``NaN`` is the second spelling. Both are
    refused by ``RosterEntry`` exactly as they should be, and the refusal is what fails::

        POST /auctions  list_price: NaN    ->  HTTP 500  (before the fix)  ->  422 (now)
        POST /auctions  list_price: 1e400  ->  HTTP 500  (before the fix)  ->  422 (now)
        POST /auctions  tier: 1e400        ->  HTTP 500  (before the fix)  ->  422 (now)

    The control in the same test is the point of it: ``list_price: "cheap"`` is refused by the
    identical model on the identical field and answers a clean ``422`` with the offending input
    echoed back, because a string survives ``json.dumps``. So the handler is not wrong about
    *rejecting* — it is wrong about what it is safe to quote back, and the fix belongs on the
    exception handler rather than on any field. A 422 naming the field is the right answer to all
    four of these bodies.
    """
    verdicts = {
        "list_price: NaN": _post_raw(_SANE_ROW, _NAN_BODY % ("1", "NaN")).status_code,
        "list_price: 1e400": _post_raw(_SANE_ROW, _NAN_BODY % ("1", "1e400")).status_code,
        "tier: 1e400": _post_raw(_SANE_ROW, _NAN_BODY % ("1e400", "100.0")).status_code,
    }
    exploded = {body: code for body, code in verdicts.items() if code >= 500}
    assert exploded == {}, (
        "an unauthenticated request body took the service down instead of being refused: "
        f"{exploded}. Every one of these is a value pydantic correctly REJECTS; the 500 comes "
        "out of the 422 the rejection builds."
    )

    # The paired control: the same field, refused by the same model, answers a clean 422 today —
    # so a fix cannot satisfy the assertion above by making the door refuse everything.
    readable = _post(dict(_SANE_ROW, list_price="cheap"), None)
    assert readable.status_code == 422, (readable.status_code, readable.text[:300])


# =====================================================================================
# T-271 — the price floor's MAGNITUDE, which nothing asserted (no marker: see the module
# docstring — today's behaviour in the blind band is correct, the assertion was missing)
# =====================================================================================

#: A row authorizing everything, so the only thing that can refuse a bid on it is the floor.
#: ``max_discount_pct: 100`` makes every other relation in the price walk true at once.
_CAP_100: dict[str, Any] = {
    "store_id": "store-1",
    "tier": 1,
    "product_ref": "prod-1",
    "max_discount_pct": 100.0,
}


@pytest.mark.parametrize(
    ("listed", "priced", "admitted"),
    [
        # list price 5.00 is inside [0.01, 10.00), where 0.1% of list (0.005) is BELOW one minor
        # currency unit — so MINIMUM_PAYABLE_AMOUNT is the binding half and 0.01 is the floor.
        (5.00, 0.005, False),
        (5.00, 0.02, True),
        # ...and 1,000,000.00 is the other side, where 0.1% of list (1000.00) is far above one
        # cent, so PRICE_FLOOR_FRACTION is the binding half.
        (1_000_000.00, 999.00, False),
        (1_000_000.00, 1001.00, True),
    ],
)
def test_t271_both_halves_of_the_price_floor_have_a_pinned_magnitude(
    listed: float, priced: float, admitted: bool
) -> None:
    """The floor's two constants are load-bearing, and this is what makes them so.

    Not an xfail: today's answers here are all correct. The finding is that nothing ASKED. A
    rung-2 verifier mutated both constants against the whole of ``apps/exchange`` and the suite
    stayed green at 695 passed for ``MINIMUM_PAYABLE_AMOUNT`` = 0.0, 0.5 and 1.0, and for
    ``PRICE_FLOOR_FRACTION`` = 1e-07 — four orders of magnitude. A constant no test can see the
    value of is a constant the next reader may freely change, and this one decides whether a
    hundredth of a cent is a rankable bid for a five-dollar product.

    The blind band was ``[0.01, 10.00)``: below one cent the absolute half is deliberately dropped
    (see :func:`~exchange.auction.collect._price_floor`), above ten dollars the proportional half
    dominates, and in between the absolute half is the only thing holding the floor up — which is
    exactly the band no test used. Each parameter kills mutants in one direction and its partner
    kills them in the other, which is why they are a pair rather than a single "too cheap" case:
    ``0.005`` refused kills ``MINIMUM_PAYABLE_AMOUNT = 0.0``, and ``0.02`` admitted kills 0.5 and
    1.0; ``999.00`` refused kills ``PRICE_FLOOR_FRACTION = 1e-07``, and ``1001.00`` admitted kills
    a fraction raised far enough to refuse honest undercuts.
    """
    posted = _post(dict(_CAP_100, list_price=listed), _offer(priced, priced))

    assert posted.status_code == 201, posted.text
    entry = _entry(posted)
    if admitted:
        assert entry["fallback"] is False, entry
        assert entry["unit_price"] == priced, entry
    else:
        assert entry["fallback"] is True, entry
        assert entry["fallback_reason"] == "bid_price_unreconcilable", entry
        # R10 is not negotiable: the refused store is still represented, at its catalog price.
        assert entry["unit_price"] == listed, entry


# =====================================================================================
# T-272 — `_tier` downgrades well-formed input, and the buyer pays list for it
# =====================================================================================


def test_t272_a_tier_written_as_a_string_does_not_discard_a_legitimate_bid() -> None:
    """A silent downgrade that costs the buyer money, on input nobody would call malformed.

    ``_tier`` was hardened so that ``tier: "one"`` and ``tier: inf`` fail closed at tier 0 instead
    of raising out of the middle of an auction, and that part is right. What it also did was take
    ``tier: "2"`` — a well-formed tier in the spelling every JSON-over-the-wire caller produces —
    down the same path. Tier 0 means the store has no bidding agent, so ``collect_bids`` never
    looks at the answer it actually sent. Measured::

        roster tier "2", store bids 80.00  ->  fallback=True  tier_0_no_agent  unit_price=100.00
        roster tier  2 , store bids 80.00  ->  fallback=False                  unit_price= 80.00

    Same roster row, same bid, one pair of quotes between them, and the buyer pays twenty dollars
    more. The control below is the second assertion for a reason: a fix that reads a stringly
    tier must not also start admitting the unreadable ones ``_tier`` exists to refuse.
    """
    row: dict[str, Any] = {
        "store_id": "store-1",
        "tier": "2",
        "product_ref": "prod-1",
        "list_price": 100.0,
    }
    entry = _collect(row, _offer(80.0, 80.0))

    assert entry.fallback_reason != "tier_0_no_agent", (
        "a roster row stating tier '2' was read as tier 0 — no bidding agent — so the store's "
        f"80.00 bid was discarded and the buyer pays the {entry.unit_price} list price"
    )
    assert entry.fallback is False, entry
    assert entry.unit_price == 80.0, entry
    assert entry.tier == 2, entry

    # Controls, in the same test so the assertions above cannot be satisfied by loosening `_tier`
    # into the coercion it was hardened out of: a tier nobody can read still fails closed.
    unreadable = _collect(dict(row, tier="one"), _offer(80.0, 80.0))
    assert unreadable.tier == 0, unreadable
    assert unreadable.fallback_reason == "tier_0_no_agent", unreadable


# =====================================================================================
# T-273 — an offer stating only a unit price becomes a rankable 0.00
# =====================================================================================


def test_t273_an_offer_stating_only_a_unit_price_is_not_turned_into_a_rankable_zero() -> None:
    """The wall against a free item mints one, on the row that gives it nothing to compare to.

    ``_price_is_unreadable`` asks whether ``float()`` would raise on what the store called a
    price, and it asks it of ``unit_price`` AND ``total_price``. ``_number(None)`` is ``None``
    for a key that was never sent, which is not the same statement as a key sent unreadably — so
    an offer of ``{"unit_price": 80.0}`` reads as unreadable, the exchange judges it, the boundary
    refuses it, and the store falls back to a list price the row does not carry. Measured::

        roster {store_id, product_ref, tier}  offer {unit_price: 80.0}
          ->  fallback=True  unit_price=0.00
              reasons ['price_unreconcilable:offer.total_price:not_a_number',
                       'price_unreconcilable:offer.unit_price:unreadable_roster_list_price']

    Two separate faults in one entry. The 0.00 is the free item ``RosterEntry.list_price``'s
    ``Field(gt=0.0)`` closed at the HTTP door and nothing closes here. And the second reason names
    an offer site for a roster-side cause — telling a store its ``unit_price`` is the problem when
    the exchange is the party with no price — which is precisely the double-reporting
    ``FALLBACK_REASONS`` was split apart to end.

    Either repair satisfies this: admit the offer the store actually made, or refuse the
    unpriceable row. What may not stand is a rankable 0.00.
    """
    row: dict[str, Any] = {"store_id": "store-1", "tier": 1, "product_ref": "prod-1"}
    offer = {
        "product_ref": "prod-1",
        "unit_price": 80.0,
        "currency": "USD",
        "expires_at": "2999-01-01T00:00:00Z",
    }
    entry = _collect(row, offer)

    assert entry.unit_price != 0.0, (
        "an 80.00 bid was replaced by a rankable 0.00 offer minted from a roster row that "
        f"carries no list_price at all: fallback={entry.fallback} reasons={entry.price_reasons}"
    )
    misattributed = [
        reason
        for reason in entry.price_reasons
        if ":offer." in reason and reason.endswith("unreadable_roster_list_price")
    ]
    assert misattributed == [], (
        "the exchange blamed an OFFER site for a ROSTER-side cause — the store is told its own "
        f"price is unreadable because the exchange has none: {misattributed}"
    )


# =====================================================================================
# T-276 — the floor refuses EVERYTHING on a product listed at a cent
# =====================================================================================


def test_t276_a_product_listed_at_a_cent_can_still_be_bid_on() -> None:
    """The absolute half of the floor swallows its own band.

    THE MARKER IS GONE BECAUSE THE DEFECT IS CLOSED, in
    ``packages/contracts/src/boundary.py`` rather than here. The floor is the boundary's to set --
    T-250 moved the threshold there precisely so the door and the boundary cannot drift onto two
    different floors -- so suppressing the refusal at the collector would have re-opened that
    drift rather than closing this. ``ABSOLUTE_FLOOR_MAX_FRACTION`` now clamps the ABSOLUTE half
    of the floor to a share of the roster's own list price, applied inside the ``max`` and never
    outside it, so the clamp can never lower the floor on an expensive product.

    The constant is 2%, and it is pinned from BOTH sides rather than chosen: from above by this
    very node, which requires 0.008 admitted on a 0.02 listing and so forbids a clamp over 40%;
    and from below by the shared corpus, which asserts ``price_floor(0.50)`` is EXACTLY
    ``MINIMUM_PAYABLE_AMOUNT`` and still refuses 0.005 there -- 0.01 is 2% of 0.50, so any clamp
    under 2% would bind on that row and admit the half cent those cases refuse. The admissible
    window is [2%, 40%] and the fix takes the strictest end, leaving ``price_floor`` identical to
    its pre-T-276 self for every listing at or above half a dollar.

    :func:`~exchange.auction.collect._price_floor` drops the absolute half where the roster lists
    the product *below* one minor unit, precisely so a catalog pricing something at 0.005 is not
    refused every bid on it. At exactly ``0.01`` the drop does not apply and ``max(1e-05, 0.01)``
    is the list price itself, so every price under list is under the floor. Measured through the
    real door, on a row authorizing 100%::

        list 0.01, bid 0.009 declaring 10% off  ->  201  fallback=true  bid_price_unreconcilable
        list 0.02, bid 0.008 declaring 60% off  ->  201  fallback=true  bid_price_unreconcilable

    Both bids reconcile against everything else the wall checks; the floor alone refuses them.
    That is the same "refuses everything" failure the function's own docstring says it dropped the
    absolute half to avoid, one cent higher up. Either repair satisfies this — grade the floor so
    a cheap product keeps a usable range, or refuse a row the exchange cannot run an auction on —
    and the control keeps a fix from buying it by opening the floor on normal products.
    """
    cent = _post(dict(_CAP_100, list_price=0.01), _offer(0.009, 0.009, depth=10.0))
    assert cent.status_code < 500, (cent.status_code, cent.text[:300])
    refused_a_cent = cent.status_code == 201 and _entry(cent)["fallback"] is True
    assert not refused_a_cent, (
        "a product the roster lists at 0.01 refused a bid declaring 10% off under a 100% "
        f"authorized cap: {_entry(cent) if cent.status_code == 201 else cent.text[:300]}"
    )

    two_cents = _post(dict(_CAP_100, list_price=0.02), _offer(0.008, 0.008, depth=60.0))
    assert two_cents.status_code < 500, (two_cents.status_code, two_cents.text[:300])
    refused_two_cents = two_cents.status_code == 201 and _entry(two_cents)["fallback"] is True
    assert not refused_two_cents, _entry(two_cents)

    # The control: the floor still bites where it is supposed to. 0.001 on a 100.00 product is
    # T-223's own measured attack and must stay refused whatever this fix does to the cheap band.
    attacked = _post(dict(_CAP_100, list_price=100.0), _offer(0.001, 0.001, depth=100.0))
    assert attacked.status_code == 201, attacked.text
    assert _entry(attacked)["fallback"] is True, _entry(attacked)


# =====================================================================================
# T-277 — the free item T-224 closed at the door, still live one caller down
# =====================================================================================


def test_t277_a_zero_priced_roster_row_cannot_mint_a_free_offer_at_the_library_door() -> None:
    """The dead branch's subject, at the layer where it is still reachable.

    T-224 closed the free item twice: ``RosterEntry.list_price`` became ``Field(gt=0.0)``, and
    ``_price_is_unreadable`` was added so the OFFER-side half needs nothing from the roster. The
    ROSTER-side half was closed only at the request model, so the test that guards it —
    ``test_repro_untrusted_roster.py::test_t224_a_roster_row_that_prices_nothing_cannot_mint_a_free_offer``
    — now takes its 422 arm on every run and its ``if posted.status_code == 201`` arm has been
    unreachable ever since. That is T-166's defect class exactly: a conditional test that silently
    stopped covering the case it was written for.

    Restoring the coverage means asserting the case somewhere it can still happen, and it can::

        collect_bids([{store_id, product_ref, tier: 1, list_price: 0.0}], [], deadline)
          ->  fallback=True  fallback_reason='no_response'  unit_price=0.00

    A rankable offer of nothing, minted from the roster alone, with no bid involved — the same
    free item, reached by a caller that is not the HTTP door. ``orchestration/solicitation.py``,
    ``services/sim/src/runner.py`` and the frozen ``test_e3_exchange.py`` all call ``collect_bids``
    directly, and ``collect.py``'s own docstring already says a repair living only in a request
    model is one a second caller does not get.

    The control is in the same test: R10 still requires the silent store to be REPRESENTED. This
    gate refuses a zero-priced entry, not the entry.
    """
    row: dict[str, Any] = {
        "store_id": "store-1",
        "tier": 1,
        "product_ref": "prod-1",
        "list_price": 0.0,
    }
    entry = _collect(row, None)

    assert entry.unit_price != 0.0, (
        "a roster row pricing the product at nothing minted a rankable 0.00 offer with no bid "
        f"involved at all: fallback={entry.fallback} reason={entry.fallback_reason}"
    )

    # R10: whatever the repair, a rostered store is still represented exactly once.
    priced = _collect(dict(row, list_price=100.0), None)
    assert priced.fallback is True, priced
    assert priced.unit_price == 100.0, priced
