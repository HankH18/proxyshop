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

#: What the catalog actually publishes for `make_offer()`'s product. This is the exchange's own
#: roster, not anything the bid said.
ROSTER: dict[str, Any] = {"prod-1": 100.0}


def check(bid, path, roster=None, snapshot=None, now=NOW):
    return validate_bid(
        bid,
        path=path,
        trust_snapshot=make_snapshot_table() if snapshot is None else snapshot,
        now=now,
        list_prices=roster,
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
    # 49.00 — is untouched by the roster it is now measured against.
    assert check(make_bid(), path, roster={"prod-1": 49.0}).ok is True


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_the_cent_of_tolerance_is_the_same_one_the_carried_claim_gets(path: str) -> None:
    """19.99 less an honest 15% is 16.9915; money is quoted to the cent. The roster relation
    must round exactly as the carried-claim relation does, or a bid priced honestly against the
    catalog is refused for the fraction of a cent it could not state."""
    roster = {"prod-1": 19.99}
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
        assert check(make_bid(offer=priced_offer(15.0, 15.0)), path, roster=roster).ok is False
        assert check(make_bid(offer=priced_offer(80.0, 80.0)), path, roster=roster).ok is True


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
