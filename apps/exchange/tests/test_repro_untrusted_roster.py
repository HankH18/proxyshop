"""Reproductions for the untrusted-roster surface of ``POST /auctions`` — T-223 and T-224.

Every test in this file drives the **real HTTP door**, because that is where both defects were
measured and because a wall proved only at the library boundary is a wall whose wiring is
untested. ``apps/exchange/src/auction/routes.py``'s own ``RosterEntry`` docstring already writes
both of these down as an unclosed follow-up; these are the gates that follow up.

Each of the first five tests arrived marked ``xfail(strict=True)`` and asserting the behaviour
that SHOULD hold: a normal run reported ``xfailed`` and stayed green, and the ticket's gate ran
the same test with ``--runxfail`` and got a real failure. **The markers are gone because the
defects are.** ``strict=True`` turns an XPASS into a failure, so a marker left on a repaired
defect fails the suite — the mechanism cleans itself up, and its absence here is the record that
both bugs were actually closed rather than deferred.

The tests appended below the two ticket sections pin the SHAPE rather than the two sites: a floor
that is a threshold holds at every spelling of "almost nothing", and a guard with no roster term
in it cannot be switched off by anything a caller writes in a request body.
"""

from __future__ import annotations

import math
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

#: A row that prices the product at nothing. ``list_price`` WAS ``Field(ge=0.0)``, so this body
#: was accepted by the request model and the auction ran on it; it is ``Field(gt=0.0)`` now and
#: this body is a 422.
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


# =====================================================================================
# The CLASS, not the two instances — appended with the fix.
#
# T-223 was one exact-equality floor and T-224 was one guard whose on/off switch was a
# caller-supplied number. Both are shapes rather than sites, so the tests below pin the shape:
# a floor that is a THRESHOLD holds at every spelling of "almost nothing", and a guard with no
# roster term in it cannot be switched off by anything written in a request body.
# =====================================================================================

NOW = 1_700_000_000.0


def _collect(roster: list[dict], responses: list[dict]):
    from exchange.auction import collect_bids  # noqa: PLC0415

    return list(collect_bids(roster, responses, NOW))


def _reply(priced: Any, store_id: str = "store-1") -> dict:
    return {
        "store_id": store_id,
        "received_at": NOW - 1.0,
        "bid": {
            "auction_id": "auc-1",
            "store_id": store_id,
            "offer": priced,
            "claims": [],
        },
    }


def test_the_module_under_test_is_this_worktree() -> None:
    """A green run that imported the PRIMARY checkout's `collect.py` is evidence about that tree.

    The venv's ``_proxyshop.pth`` hardcodes the primary checkout onto ``sys.path``, so this is
    not hypothetical: a suite can pass here while grading a file nobody in this branch edited.
    """
    from pathlib import Path  # noqa: PLC0415

    from exchange.auction import collect  # noqa: PLC0415

    here = Path(__file__).resolve().parents[3]
    resolved = Path(collect.__file__).resolve()
    assert here in resolved.parents, f"graded {resolved}, not {here}"


@pytest.mark.parametrize("unit", [1e-09, 1e-06, 0.0001, 0.001, 0.009])
@pytest.mark.parametrize("row", [CAP_100_ROW, UNCAPPED_ROW], ids=["cap_100", "no_cap"])
def test_t223_the_floor_is_a_threshold_so_no_spelling_of_almost_nothing_clears_it(
    row: dict, unit: float
) -> None:
    """The generalisation of the defect: `priced == 0.0` was defeated by ADDING A DIGIT.

    An equality has exactly one value that satisfies it, so the whole floor was one keystroke
    wide. Every price here is positive, so the boundary's ``:not_positive`` and ``:negative``
    refusals say nothing about any of them, and on the ``cap_100`` row every depth relation in
    the walk is satisfied too — ``100.00 * (100 - 100) / 100`` is 0.00 and any price at all is
    above it. Only a threshold refuses these, and it must refuse them on the row that authorizes
    everything and on the row that authorizes nothing alike.
    """
    entry = _collect([row], [_reply(_offer(unit, unit))])[0]

    assert entry.fallback is True, (row.get("max_discount_pct"), unit, entry.price_reasons)
    assert entry.fallback_reason == "bid_price_unreconcilable"
    assert entry.unit_price == 100.0, "the refused store is still represented, at its list price"


def test_t223_the_floor_names_its_own_refusal_and_does_not_double_report_a_bad_number() -> None:
    """`fallback_reason` is a fixed vocabulary; `price_reasons` is what the store's operator is
    owed. A price under the floor is a POLICY refusal with a name of its own, and a price that is
    zero, negative or unreadable keeps the boundary's own name for that instead of collecting
    both — one bad number reported twice is the mislabelling `FALLBACK_REASONS` was split to end.
    """
    from exchange.auction.collect import PRICE_BELOW_FLOOR_REASON  # noqa: PLC0415

    under = _collect([CAP_100_ROW], [_reply(_offer(0.001, 0.001))])[0]
    assert (
        f"price_unreconcilable:offer.unit_price:{PRICE_BELOW_FLOOR_REASON}" in under.price_reasons
    )
    assert (
        f"price_unreconcilable:offer.total_price:{PRICE_BELOW_FLOOR_REASON}" in under.price_reasons
    )

    for priced, already_named in ((0.0, "not_positive"), (-5.0, "negative")):
        other = _collect([CAP_100_ROW], [_reply(_offer(priced, priced))])[0]
        assert other.fallback is True, priced
        assert f"price_unreconcilable:offer.unit_price:{already_named}" in other.price_reasons
        assert not [r for r in other.price_reasons if r.endswith(PRICE_BELOW_FLOOR_REASON)], (
            priced,
            other.price_reasons,
        )


@pytest.mark.parametrize("unit", [1.0, 80.0, 99.0, 100.0])
def test_t223_the_floor_leaves_every_undercut_r10_requires_admitted(unit: float) -> None:
    """The paired control, and the constraint that sets the floor's ceiling.

    A wall that refused every cheap bid would satisfy every rejection above and destroy the
    auction. ``test_auction_price_wall.py`` ::
    ``test_r10_still_admits_an_undeclared_undercut_where_nothing_is_authorized`` pins 99.00, 80.00
    and 1.00 on this uncapped 100.00 row as admitted — a 99% undercut is still an undercut — so
    the floor has to leave a whole dollar alone, and it does, by an order of magnitude.
    """
    entry = _collect([UNCAPPED_ROW], [_reply(_offer(unit, unit))])[0]
    assert entry.fallback is False, (unit, entry.price_reasons)
    assert entry.unit_price == unit


def test_t223_the_floor_bites_in_proportion_on_a_product_the_cent_floor_cannot_reach() -> None:
    """An absolute floor alone is not enough: 0.02 is a payable amount and it is not a price for a
    product listed at 1,000,000.00. The proportional half is what reaches that, and the paired
    control is that the same row still admits a real bid."""
    expensive = dict(UNCAPPED_ROW, list_price=1_000_000.0)

    refused = _collect([expensive], [_reply(_offer(0.02, 0.02))])[0]
    assert refused.fallback is True, refused.price_reasons
    assert refused.unit_price == 1_000_000.0

    kept = _collect([expensive], [_reply(_offer(900_000.0, 900_000.0))])[0]
    assert kept.fallback is False, kept.price_reasons


def test_t223_a_catalog_that_genuinely_prices_below_a_cent_is_not_walled_off() -> None:
    """The floor must not be closed rather than fail-closed. A row listing at 0.005 is a catalog
    describing a sub-cent product, not a caller minting a giveaway, so the absolute cent floor is
    dropped there and the proportional one — which is 0.000005 — is what applies."""
    micro = dict(UNCAPPED_ROW, list_price=0.005)

    kept = _collect([micro], [_reply(_offer(0.004, 0.004))])[0]
    assert kept.fallback is False, kept.price_reasons
    assert kept.unit_price == 0.004


@pytest.mark.parametrize("junk", ["cheap", None, float("nan"), float("inf"), [], {"a": 1}])
def test_t224_an_unreadable_price_is_judged_on_every_row_whatever_it_prices(junk: Any) -> None:
    """The guard has no roster term in it, and that is the repair.

    `_priced_at_nothing` turned an unreadable price into a fallback only ``if listed > 0``, so the
    protection was switched on and off by a number the caller writes. Below, the SAME junk price
    is refused on a row listing at 100.00, on a row listing at zero, and on a row that states no
    list price at all — three rosters, one verdict, and no ``ValueError`` out of any of them.
    """
    for row in (
        UNCAPPED_ROW,
        dict(UNCAPPED_ROW, list_price=0.0),
        {"store_id": "store-1", "tier": 1, "product_ref": "prod-1"},
    ):
        entry = _collect([row], [_reply(_offer(junk, junk))])[0]
        assert entry.fallback is True, (row.get("list_price"), junk, entry.price_reasons)
        assert entry.fallback_reason == "bid_price_unreconcilable"
        # It never raises, and the store is never dropped: R10 is not negotiable.
        assert entry.store_id == "store-1"
        assert isinstance(entry.unit_price, float)


def test_t224_an_unreadable_roster_value_cannot_raise_out_of_the_collector_either() -> None:
    """The same shape one row over, on the FALLBACK path rather than the bid path.

    `_list_price_bid` read ``float(entry.get("list_price", 0.0))`` and `collect_bids` read
    ``int(rostered.get("tier", 1))``, so a silent store on a row carrying ``list_price: "cheap"``
    — or any store on a row carrying ``tier: "one"`` — raised out of the middle of the auction the
    same way an unreadable BID price did. Both are unreadable claims now, and an unreadable claim
    establishes nothing: no list price, and no bidding agent.
    """
    unpriced = _collect([dict(UNCAPPED_ROW, list_price="cheap")], [])[0]
    assert unpriced.fallback is True
    assert unpriced.fallback_reason == "no_response"
    # This read ``== 0.0`` until T-277's second spelling was closed, and 0.0 is what the
    # ``float(entry.get("list_price", 0.0))`` above HAPPENED to produce — not a contract. It
    # contradicted this docstring's own last sentence: "an unreadable claim establishes nothing:
    # NO LIST PRICE" and "the list price is zero" cannot both be true, and zero is the cheapest
    # number there is, so the entry the sentence calls unpriced won every ranking it reached.
    # The purpose the assertion serves is unchanged and is what is asserted instead: the
    # collector does not RAISE on an unreadable roster value, and the store is still there.
    assert unpriced.unit_price == math.inf
    assert "unit_price" not in unpriced.offer, unpriced.bid

    for junk in ("one", float("nan"), float("inf"), None, [1]):
        untiered = _collect([dict(UNCAPPED_ROW, tier=junk)], [_reply(_offer(80.0, 80.0))])[0]
        assert untiered.tier == 0, junk
        assert untiered.fallback is True, junk
        assert untiered.fallback_reason == "tier_0_no_agent", junk


def test_t224_the_door_refuses_a_zero_list_price_the_same_way_it_refuses_a_missing_one() -> None:
    """ "Prices it at nothing" is not a different statement from "does not price it", and the 422
    on the missing field was reached around by writing the zero. Both are 422 now, and the paired
    control is that a priced row still opens an auction."""
    for row in (
        {"store_id": "store-1", "tier": 1, "product_ref": "prod-1"},
        ZERO_PRICED_ROW,
        dict(ZERO_PRICED_ROW, list_price=-5.0),
    ):
        posted = _post(row, None)
        assert posted.status_code == 422, (row.get("list_price"), posted.text[:200])
        assert posted.json()["detail"][0]["loc"][-1] == "list_price"

    priced = _post(UNCAPPED_ROW, None)
    assert priced.status_code == 201, priced.text
    assert priced.json()["entries"][0]["unit_price"] == 100.0


# ======================================================================================
# F1 — a non-finite list price re-opened T-223 AND T-224 through the real door
# ======================================================================================
def test_a_non_finite_roster_list_price_is_refused_at_the_door() -> None:
    """``1e400`` is legal RFC-8259 JSON and ``inf > 0.0`` is ``True``.

    So ``Field(gt=0.0)`` accepted it, ``_number`` then excluded it as non-finite, and every
    downstream guard took its ``listed is None`` early-out — switching the entire price wall
    off on that row. Found by a rung-2 adversarial verifier, not by this suite.
    """
    from exchange.auction.routes import RosterEntry
    from pydantic import ValidationError

    for hostile in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValidationError):
            RosterEntry(store_id="s1", product_ref="p1", list_price=hostile, tier=1)

    assert (
        RosterEntry(store_id="s1", product_ref="p1", list_price=100.0, tier=1).list_price == 100.0
    )


def test_an_unreadable_roster_list_price_keeps_the_price_wall_on() -> None:
    """The library path, which no door validates — and where the fail-open actually lived.

    Both price guards opened with ``if listed is None or listed <= 0.0: return []``, and
    ``_number`` returns ``None`` for a non-finite or non-numeric list price. So a row the caller
    priced at ``inf`` switched the ENTIRE wall off, and an underpriced bid on that row was
    admitted at its stated price rather than refused.

    PRESENT-BUT-UNREADABLE is now distinguished from ABSENT: the former is a caller asserting
    something the exchange cannot read, and it keeps the wall ON. The latter does not, and the
    end of this test pins that distinction by the thing the two cases actually differ in — the
    wall — so it cannot quietly collapse back into one case.
    """
    absent_row = {k: v for k, v in UNCAPPED_ROW.items() if k != "list_price"}

    for hostile in (float("inf"), float("nan"), "cheap", True):
        entry = _collect(
            [dict(UNCAPPED_ROW, list_price=hostile)],
            [_reply({"unit_price": 1e-09, "total_price": 1e-09})],
        )[0]
        assert entry.fallback is True, (
            f"list_price={hostile!r} admitted a 1e-09 bid at its stated price: an unreadable "
            f"catalog price switched the price wall off on the row carrying it"
        )

    # The distinction, pinned where it lives. An ABSENT list price states nothing for the wall to
    # judge against, so the honest bid the store actually made is admitted at its own price —
    # which is also `test_repro_verifier_findings.py::test_t273_...`'s stated repair, and the
    # control proving this file's gates are not satisfied by refusing everything.
    honest = _collect([absent_row], [_reply(_offer(80.0, 80.0))])[0]
    assert honest.fallback is False, honest.price_reasons
    assert honest.unit_price == 80.0, honest.bid

    # This read ``absent.unit_price == 0.0`` — "the latter keeps its historical behaviour" — and
    # that historical behaviour WAS the free item: a row naming no price minted a live, rankable
    # 0.00 offer with no bid involved at all, which is exactly what `RosterEntry.list_price`'s
    # docstring measures through the door (`HTTP 201, entries=[{fallback: true, unit_price: 0.0}]`)
    # and what T-273's gate refuses one caller down. The 0.0 also discriminated nothing: at the
    # time it was written PRESENT-BUT-UNREADABLE minted the identical 0.0 (the assertion in
    # `test_t224_an_unreadable_roster_value_cannot_raise_out_of_the_collector_either`), so the
    # sentence above cannot have been resting on it. T-277's second spelling.
    absent = _collect([absent_row], [])[0]
    assert absent.fallback is True
    assert absent.unit_price == math.inf, absent.bid
    assert "unit_price" not in absent.offer, absent.bid


def test_a_roster_row_the_exchange_cannot_price_mints_no_rankable_free_offer() -> None:
    """T-277's second spelling: the free item reached by omitting ``list_price``, or garbling it.

    T-277 closed the row that prices the product at nothing *legibly* — ``list_price: 0.0`` — and
    the repair read ``if listed is not None and listed <= 0.0``. ``_number`` answers ``None`` for
    an ABSENT key and for an UNREADABLE value alike, so both walked past that guard and
    ``_list_price_bid`` minted them ``unit_price = total_price = 0.0`` with a live ``expires_at``:
    the same rankable free offer, for a store that never bid, reached by writing nothing or by
    writing junk instead of by writing the zero. Measured on the library path before this gate::

        collect_bids([{store_id, tier: 1, product_ref}], [], deadline)
          ->  fallback=True  no_response  unit_price=0.00  expires_at='2023-11-14T22:...Z'
        the same row with list_price='cheap' / inf / nan / True / None / [100.0]  ->  identical

    ``RosterEntry.list_price`` refuses all of these at the HTTP door (``Field(gt=0.0,
    allow_inf_nan=False)``, and the field is required), and that door is precisely what
    ``orchestration/solicitation.py``, ``services/sim/src/runner.py`` and the frozen
    ``test_e3_exchange.py`` do not have when they call ``collect_bids`` themselves — a repair
    living only in a request model is one a second caller does not get.

    The question is not "did the caller write a zero" but "can the exchange name a price it could
    charge", so every way of failing it gets one answer: no price for a ranking to prefer, and no
    ``expires_at``, which ``ranking.filters.expiry_reason`` already fails closed on. R10 is not
    negotiable and the store is still represented — the controls at the end are the proof this
    gate is not satisfied by refusing everything.
    """
    rows: list[tuple[str, dict[str, Any]]] = [
        ("absent", {k: v for k, v in UNCAPPED_ROW.items() if k != "list_price"})
    ]
    rows += [
        (repr(junk), dict(UNCAPPED_ROW, list_price=junk))
        for junk in ("cheap", float("inf"), float("nan"), True, None, [100.0])
    ]

    for label, row in rows:
        entry = _collect([row], [])[0]

        # R10 first: the store is represented exactly once whatever the roster says.
        assert entry.store_id == "store-1", label
        assert entry.fallback is True, label
        assert entry.fallback_reason == "no_response", label

        offer = entry.bid["offer"]
        assert "unit_price" not in offer, (
            f"list_price={label} minted a rankable {offer.get('unit_price')!r} offer for a store "
            f"that never bid, from a roster row the exchange cannot price at all: {offer}"
        )
        assert "total_price" not in offer, (label, offer)
        # The fail-CLOSED spelling of an absent price, and the direction the rest of the module
        # already fails in: `inf` is a price nothing can pay, where 0.00 is one everything beats.
        assert entry.unit_price == math.inf, (label, offer)
        # And it cannot be shown to be live, so it reaches no shortlist to be cheapest in.
        assert offer["expires_at"] is None, (label, offer)

    # The controls, in the same test. A row the exchange CAN price still mints its offer, dated,
    # and a store that answers such a row is still admitted at the price it actually bid.
    silent = _collect([UNCAPPED_ROW], [])[0]
    assert silent.fallback is True, silent.bid
    assert silent.unit_price == 100.0, silent.bid
    assert silent.bid["offer"]["expires_at"] is not None, silent.bid

    bidding = _collect([UNCAPPED_ROW], [_reply(_offer(80.0, 80.0))])[0]
    assert bidding.fallback is False, bidding.price_reasons
    assert bidding.unit_price == 80.0, bidding.bid
