"""T-177, second half — the exchange's own list price, so the wall does not need the bid's.

`test_boundary_dual_path.py` put the price arithmetic on the validating door, but on ONE piece
of evidence only: a `list_price` claim the bid itself carries. A bid that simply omits that
claim gave the relation no number to be a percentage of, and the wall abstained — pinned there
as a deliberate gap by
`test_the_wall_abstains_deliberately_when_the_bid_carries_no_list_price`.

That gap is the whole attack. Measured through the real door, on the tree before this file:

    hosted bid, genuine hook-minted 20% grant, declares 20%, charges 15.00, carries no
    list_price claim, product really lists at 100.00
    validate_bid(bid, path="hosted", trust_snapshot=...)  ->  ok=True, reasons=[]

The exchange knows what `prod-1` lists at — it holds the roster the auction was opened from —
and it had no way to tell the boundary. So these pin the roster parameter: an exchange that can
price a product may refuse a bid that underprices it, **without trusting a single number the
emitter wrote down**. Omitting the roster leaves the old abstention exactly where it was, which
is why no existing test in this suite changes.
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.contracts import (
    EXTERNAL_PATH,
    HOSTED_PATH,
    validate_bid,
    validate_external_submission,
)
from packages.contracts.tests._fixtures_protocol import (
    HOOK_PROVENANCE,
    make_bid,
    make_claim,
    make_offer,
    make_snapshot_table,
    make_submission,
)

NOW = "2026-06-01T00:00:00Z"
BOTH_PATHS = (HOSTED_PATH, EXTERNAL_PATH)

#: What the catalog actually publishes for `make_offer()`'s product, and how deep a discount the
#: merchant approved on it. This is the exchange's own roster, not anything the bid said — and it
#: carries BOTH numbers, because a list price with no cap bounds nothing: the bid picks its own
#: depth and any price at all prices out against it.
ROSTER: dict[str, Any] = {"prod-1": {"list_price": 100.0, "max_discount_pct": 20.0}}

#: The same roster with the authorization taken out: it prices the product and authorizes no
#: discount on it. Kept as its own name because "the exchange can price this but has no envelope
#: for it" is a real state with its own verdict, pinned below.
UNAUTHORIZING_ROSTER: dict[str, Any] = {"prod-1": 100.0}


def check(bid, path, roster=None, snapshot=None, now=NOW, cap=None):
    return validate_bid(
        bid,
        path=path,
        trust_snapshot=make_snapshot_table() if snapshot is None else snapshot,
        now=now,
        list_prices=roster,
        max_discount_pct=cap,
    )


def priced_offer(unit: Any, total: Any, depth: Any = 20.0, **overrides: Any) -> dict:
    """An offer that states a price and declares a hook-provenanced percentage depth."""
    return make_offer(
        unit_price=unit,
        total_price=total,
        discount={"type": "percentage", "value": depth, "provenance": dict(HOOK_PROVENANCE)},
        **overrides,
    )


def test_the_module_under_test_is_this_worktree() -> None:
    """Guards against the `.pth` that puts the PRIMARY checkout on `sys.path`: a green run that
    imported another tree's `boundary.py` is evidence about that tree, not about this one."""
    from pathlib import Path

    from contracts import boundary

    here = Path(__file__).resolve().parents[3]
    resolved = Path(boundary.__file__).resolve()
    assert here in resolved.parents, f"graded {resolved}, not {here}"


# --- the attack ------------------------------------------------------------------------------


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_bid_that_omits_its_list_price_is_still_measured_against_the_roster(path: str) -> None:
    """THE T-177 REPRODUCTION, with the evidence the emitter controls taken away from it.

    Every wall this bid meets was already there and every one of them passed it: the discount is
    hook-provenanced, the depth is a legible 20%, the total is consistent with the unit price,
    the offer is unexpired and the store is not blacklisted. It charges 15.00 for a product the
    exchange lists at 100.00 — 85% off behind a 20% authorization — and it never says so. The
    emitter's only move was to stay silent, and staying silent used to work.
    """
    silent = make_bid(offer=priced_offer(15.0, 15.0))

    admitted = check(silent, path)
    assert admitted.ok is True, (
        "the abstention this ticket exists to close must still be reproducible when no roster "
        "is supplied, or this test is measuring something else"
    )

    refused = check(silent, path, roster=ROSTER)
    assert refused.ok is False, "the exchange's own list price did not reach the wall"
    assert "price_under_declared_depth:offer.unit_price" in list(refused.reasons), refused.reasons


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_the_honest_bid_at_that_same_roster_price_is_still_admitted(path: str) -> None:
    """The wall that refuses everything is not a wall. An authorized 20% off 100.00 prices out
    at 80.00, and the identical bid at 80.00 — and at every price above it — is admitted with
    the roster in place."""
    for unit in (80.0, 85.0, 100.0, 130.0):
        honest = make_bid(offer=priced_offer(unit, unit))
        result = check(honest, path, roster=ROSTER)
        assert result.ok is True, (unit, list(result.reasons))

    # And the suite's own default bid — 10% declared, 49.00 on a product the roster prices at
    # 49.00 and authorizes 10% on — is untouched by the roster it is now measured against.
    assert (
        check(
            make_bid(), path, roster={"prod-1": {"list_price": 49.0, "max_discount_pct": 10.0}}
        ).ok
        is True
    )


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_the_declared_depth_is_not_its_own_authorization(path: str) -> None:
    """THE OTHER HALF OF T-177, and the one the roster alone did not close.

    The roster bounds the price by the depth the offer DECLARES, and until this the offer chose
    that number. Measured on the door with `list_prices={"prod-1": 100.0}` and nothing else, a
    bid charging 15.00 for a 100.00 product was admitted `ok=True, reasons=[]` by writing 85
    into `discount.value` instead of 20 — and at a declared 100% it charged 0.00 for it. The
    catalog's real cap existed the whole time (`authorize_discount` denies 25% against
    `prod-cap`'s 20.0) but only on the EMITTING side, which a Tier-2 store never reaches. That
    asymmetry is the ticket.
    """
    for depth in (20.001, 25.0, 85.0, 86.0, 99.0, 100.0):
        over = make_bid(offer=priced_offer(15.0, 15.0, depth=depth))
        result = check(over, path, roster=ROSTER)
        assert result.ok is False, (depth, list(result.reasons))
        assert "discount_over_authorized_depth:offer.discount" in list(result.reasons), (
            depth,
            list(result.reasons),
        )

    # The free item: 100% off is a depth like any other, and it is the one that costs the most.
    free = make_bid(offer=priced_offer(0.0, 0.0, depth=100.0))
    assert check(free, path, roster=ROSTER).ok is False

    # Positive control. A depth exactly AT the authorized cap is authorized — `authorize_discount`
    # grants at `max_discount_pct` and denies above it, and the two walls must agree about that
    # boundary or an honest bid is refused by the door that checks what the door that grants
    # allowed.
    at_cap = make_bid(offer=priced_offer(80.0, 80.0, depth=20.0))
    assert check(at_cap, path, roster=ROSTER).ok is True, list(
        check(at_cap, path, roster=ROSTER).reasons
    )


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_roster_that_prices_a_product_but_authorizes_no_depth_fails_closed(path: str) -> None:
    """A cap that can be omitted is a cap an attacker omits. So a roster row that prices a product
    and says nothing about how deep a discount is allowed on it does not fall back to trusting the
    bid's own number: the authorized depth is UNKNOWN, and unknown fails closed at zero, exactly
    as an unpriceable product and an unavailable trust snapshot do.

    The consequence is bounded and is the point: the offer is measured against the FULL list
    price. A store may still bid at or above list, and an undiscounted offer never consults the
    cap at all — a zero depth takes nothing off, so it authorizes itself.
    """
    discounted = make_bid(offer=priced_offer(80.0, 80.0, depth=20.0))
    result = check(discounted, path, roster=UNAUTHORIZING_ROSTER)
    assert result.ok is False, list(result.reasons)
    assert "price_unreconcilable:offer.discount:authorized_depth_unavailable" in list(
        result.reasons
    ), list(result.reasons)

    # Bidding AT list needs no authorization, so the same roster admits it.
    at_list = make_bid(offer=priced_offer(100.0, 100.0, depth=0.0))
    assert check(at_list, path, roster=UNAUTHORIZING_ROSTER).ok is True, list(
        check(at_list, path, roster=UNAUTHORIZING_ROSTER).reasons
    )

    # And a cap supplied at the CALL is the same authority as one on the row, for a caller that
    # holds one approved number rather than a column.
    assert check(discounted, path, roster=UNAUTHORIZING_ROSTER, cap=20.0).ok is True


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_an_unreadable_cap_is_a_refusal_rather_than_a_default(path: str) -> None:
    """A cap the door cannot read is not a cap. Every one of these is the shape an attacker would
    put in a roster row it could influence, and none of them may degrade into "unbounded"."""
    for bad in ("20", True, -1.0, 101.0, float("nan"), float("inf"), None, [20.0]):
        roster = {"prod-1": {"list_price": 100.0, "max_discount_pct": bad}}
        result = check(make_bid(offer=priced_offer(80.0, 80.0)), path, roster=roster)
        assert result.ok is False, (bad, list(result.reasons))
        assert any(r.startswith("price_unreconcilable:offer.discount") for r in result.reasons), (
            bad,
            list(result.reasons),
        )


def test_the_cap_is_never_consulted_without_a_roster() -> None:
    """`max_discount_pct` on its own is a cap with no list price to apply it to. Passing one — or
    passing nonsense as one — must not move a single verdict for a caller that supplies no
    catalog, or the opt-in property this whole parameter rests on is not true."""
    table = make_snapshot_table()
    deep = make_bid(offer=priced_offer(15.0, 15.0, depth=85.0))
    for path in BOTH_PATHS:
        bare = validate_bid(deep, path=path, trust_snapshot=table, now=NOW)
        for cap in (None, 0.0, 20.0, "nonsense", float("nan")):
            capped = validate_bid(
                deep, path=path, trust_snapshot=table, now=NOW, max_discount_pct=cap
            )
            assert (bare.ok, list(bare.reasons)) == (capped.ok, list(capped.reasons)), cap


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_the_cent_of_tolerance_is_the_same_one_the_carried_claim_gets(path: str) -> None:
    """19.99 less an honest 15% is 16.9915; money is quoted to the cent. The roster relation
    must round exactly as the carried-claim relation does, or a bid priced honestly against the
    catalog is refused for the fraction of a cent it could not state."""
    roster = {"prod-1": {"list_price": 19.99, "max_discount_pct": 15.0}}
    rounded = make_bid(offer=priced_offer(16.99, 16.99, depth=15.0))
    assert check(rounded, path, roster=roster).ok is True, check(
        rounded, path, roster=roster
    ).reasons

    whole_unit_under = make_bid(offer=priced_offer(15.99, 15.99, depth=15.0))
    assert check(whole_unit_under, path, roster=roster).ok is False


# --- the roster is evidence too, so it fails closed --------------------------------------------


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_roster_that_cannot_price_the_product_refuses_rather_than_abstains(path: str) -> None:
    """An exchange that passes a roster is saying "I can price these". A product it turns out it
    cannot price is an UNAVAILABLE read, denied the way an unavailable trust snapshot is (R12) —
    not silently downgraded back to the abstention this ticket closed, which would hand the
    attacker one unknown `product_ref` as a way round the whole wall."""
    for roster in (
        {},
        {"prod-other": 100.0},
        {"prod-1": None},
        {"prod-1": "n/a"},
        {"prod-1": -5.0},
    ):
        result = check(make_bid(offer=priced_offer(15.0, 15.0)), path, roster=roster)
        assert result.ok is False, (roster, list(result.reasons))
        assert any(reason.startswith("price_unreconcilable") for reason in result.reasons), (
            roster,
            list(result.reasons),
        )


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_roster_row_may_be_a_record_as_well_as_a_bare_number(path: str) -> None:
    """The trust snapshot is `{store_id: row}` with fields; a caller holding a catalog row rather
    than a naked float must not have to unwrap it into a shape this door invented."""
    for roster in ({"prod-1": 100.0}, {"prod-1": {"list_price": 100.0}}, {"prod-1": 100}):
        # The depth is authorized at the call, so the only variable left is the row's SHAPE.
        assert (
            check(make_bid(offer=priced_offer(15.0, 15.0)), path, roster=roster, cap=20.0).ok
            is False
        )
        assert (
            check(make_bid(offer=priced_offer(80.0, 80.0)), path, roster=roster, cap=20.0).ok
            is True
        )


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_bid_whose_carried_list_price_contradicts_the_roster_is_refused(path: str) -> None:
    """`get_product_fact(product_ref, "list_price")` mints the carried claim, so a hosted bid can
    only carry a list price the catalog published. A claim naming a DIFFERENT number than the
    exchange's roster is therefore evidence the claim did not come from the catalog — and it is
    the move an attacker makes next: inflate the stated list price until 15.00 looks like 20%
    off. Refused, and refused whichever direction it lies in."""
    for claimed in (18.75, 250.0):
        bid = make_bid(
            claims=[make_claim("list_price", claimed, dict(HOOK_PROVENANCE))],
            offer=priced_offer(15.0, 15.0),
        )
        result = check(bid, path, roster=ROSTER)
        assert result.ok is False, (claimed, list(result.reasons))
        assert any("list_price_contradicts_roster" in r for r in result.reasons), list(
            result.reasons
        )

    # Control: a carried claim that AGREES with the roster is not a contradiction, and the bid
    # is judged on the arithmetic exactly as before.
    agreeing = make_bid(
        claims=[make_claim("list_price", 100.0, dict(HOOK_PROVENANCE))],
        offer=priced_offer(80.0, 80.0),
    )
    assert check(agreeing, path, roster=ROSTER).ok is True, list(
        check(agreeing, path, roster=ROSTER).reasons
    )


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_the_roster_read_never_raises_and_never_admits_on_a_hostile_read(path: str) -> None:
    """The roster is caller-supplied and the `product_ref` comes off the wire. A lookup that
    raises must be an unavailable read, not a 500 at the public boundary — the same property
    `_eligibility_reasons` holds for an unhashable `store_id`."""

    class Hostile(dict):
        def get(self, key, default=None):  # noqa: ANN001, ANN201
            raise RuntimeError("boom")

    result = check(make_bid(offer=priced_offer(15.0, 15.0)), path, roster=Hostile())
    assert result.ok is False
    assert any(r.startswith("price_unreconcilable") for r in result.reasons), list(result.reasons)

    # An unhashable `product_ref` is a lookup that raises `TypeError`, not a crash.
    unhashable = check(
        make_bid(offer=priced_offer(15.0, 15.0, product_ref=["prod-1"])), path, roster=ROSTER
    )
    assert unhashable.ok is False
    assert unhashable.reasons


def test_omitting_the_roster_changes_nothing_about_any_existing_verdict() -> None:
    """The parameter is opt-in. Every bid in the pinned price table must answer identically with
    `list_prices` absent and with it explicitly `None`, or this is a behaviour change wearing a
    default argument."""
    bids = (
        make_bid(),
        make_bid(offer=priced_offer(15.0, 15.0)),
        make_bid(
            claims=[make_claim("list_price", 100.0, dict(HOOK_PROVENANCE))],
            offer=priced_offer(15.0, 15.0),
        ),
        make_bid(offer=priced_offer(100.0, 15.0)),
    )
    table = make_snapshot_table()
    for bid in bids:
        for path in BOTH_PATHS:
            bare = validate_bid(bid, path=path, trust_snapshot=table, now=NOW)
            explicit = validate_bid(bid, path=path, trust_snapshot=table, now=NOW, list_prices=None)
            assert (bare.ok, list(bare.reasons)) == (explicit.ok, list(explicit.reasons))


def test_the_signed_external_door_takes_the_roster_too() -> None:
    """`validate_external_submission` is the function an exchange actually calls on the wire body.
    A roster the Tier-2 door could not be handed would leave the hole open on the ONLY door a
    store not running our runtime ever reaches."""
    submission = make_submission(
        offer=priced_offer(15.0, 15.0),
        auction_id="auc-1",
        store_id="store-1",
        signer_id="store-1",
        signature="sig-deadbeef",
    )
    table = make_snapshot_table()

    admitted = validate_external_submission(submission, trust_snapshot=table, now=NOW)
    assert admitted.ok is True, list(admitted.reasons)

    refused = validate_external_submission(
        submission, trust_snapshot=table, now=NOW, list_prices=ROSTER
    )
    assert refused.ok is False
    assert "price_under_declared_depth:offer.unit_price" in list(refused.reasons), refused.reasons
