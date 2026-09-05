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
emitter wrote down**.

T-306/T-307 CLOSED THE OTHER HALF OF IT, and the sentence that used to end this docstring —
"omitting the roster leaves the old abstention exactly where it was, which is why no existing
test in this suite changes" — was the fail-open written down as a feature. An omitted roster was
strictly more permissive than `list_prices={}`, so the door was gentler with a caller who said
nothing than with one who said "I hold no catalog", on the money path; and because the
abstention returned before the cap was read, a caller passing `max_discount_pct=0` and
forgetting the roster had its ceiling dropped in silence (T-307). The absent roster is now the
empty roster at every site, and three tests below carry the `justify-test-edit` record of the
assertions that pinned the old contract.
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

    JUSTIFY-TEST-EDIT. One assertion here was REPLACED. It was::

        admitted = check(silent, path)       # `roster=None`, i.e. no roster
        assert admitted.ok is True, (
            "the abstention this ticket exists to close must still be reproducible when no "
            "roster is supplied, or this test is measuring something else"
        )

    * **What it claimed.** With no roster supplied, this bid is ADMITTED.
    * **Origin.** `57c25df` ("fix(T-177): the declared depth is not its own authorization"), where
      it was the reproduction's "before" half: the point was that the roster changes the verdict.
    * **Would it still be wrong if the source change were reverted?** YES. It reads `ok is True`
      as the definition of "the roster reached the wall", and that is a false test of the same
      thing: `check(silent, path, roster={})` refused this bid throughout, so the admission was
      never evidence about the roster's effect — it was evidence about which arguments the caller
      named. T-306 is exactly that: the argument omitted was more permissive than the argument
      passed empty, which cannot be a property of the bid.
    * **Independent proof the code is right.** `test_repro_open_tickets.py::test_t306_...`,
      written as a reproduction in `fbc4636`, failed against the old behaviour and passes now.
    * **Blast radius.** `test_the_cap_is_never_consulted_without_a_roster` and
      `test_the_signed_external_door_takes_the_roster_too` in this file, the dual-path suite's
      `test_the_wall_abstains_deliberately_...`, its TypeScript peer, and the shared corpus row
      `no_list_price_carried`. Every one is changed in the same commit.

    What replaces it is stronger: the "before" half still exists, but the contrast the test is
    built on is now between a caller that CANNOT price the product and one that can — not between
    a caller that spoke and one that stayed quiet.
    """
    silent = make_bid(offer=priced_offer(15.0, 15.0))

    bare = check(silent, path)
    assert bare.ok is False, "an omitted roster was more permissive than an empty one"
    assert list(bare.reasons) == list(check(silent, path, roster={}).reasons), (
        "omission must be the empty roster to the byte",
        list(bare.reasons),
    )
    assert "price_unreconcilable:offer.unit_price:list_price_unavailable" in list(bare.reasons), (
        "the refusal must NAME the input the caller did not supply"
    )
    assert "price_under_declared_depth:offer.unit_price" not in list(bare.reasons), (
        "a door with no list price cannot have measured this bid against one"
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


def test_the_cap_is_consulted_even_when_no_roster_is_passed() -> None:
    """T-307. A ceiling the door does not read is not a ceiling.

    JUSTIFY-TEST-EDIT. This test's whole body was REPLACED, and it was previously named
    `test_the_cap_is_never_consulted_without_a_roster`. It asserted::

        bare = validate_bid(deep, path=path, trust_snapshot=table, now=NOW)
        for cap in (None, 0.0, 20.0, "nonsense", float("nan")):
            capped = validate_bid(
                deep, path=path, trust_snapshot=table, now=NOW, max_discount_pct=cap
            )
            assert (bare.ok, list(bare.reasons)) == (capped.ok, list(capped.reasons)), cap

    * **What it claimed.** `max_discount_pct` moves NO verdict at all for a caller that passes no
      roster — including `max_discount_pct=0`, a caller stating it authorizes no discount.
    * **Origin.** `57c25df` ("fix(T-177): the declared depth is not its own authorization"), where
      it guarded the opt-in property: nothing about the price wall was to change for a caller who
      did not opt in.
    * **Would it still be wrong if the source change were reverted?** YES — and it is the clearest
      case of the three, because this assertion is the exact INVERSE of the requirement T-307
      states. Reverting `boundary.py` makes it green again and leaves the ticket open: a caller
      that sets a 0% ceiling and forgets the roster is told nothing, silently, because the
      abstention returned before the ceiling was read at all. An argument that binds only in the
      presence of a second argument is an argument a caller loses by forgetting, which is the
      whole defect class T-306 and T-233 name.
    * **Independent proof the code is right.** `test_repro_open_tickets.py::test_t307_...`
      (`fbc4636`) failed against the old behaviour and passes now; the shared corpus rows
      `a_call_wide_ceiling_of_zero_binds_with_no_roster` and
      `a_call_wide_ceiling_that_covers_the_declared_depth_with_no_roster` assert the same property
      against BOTH doors.
    * **Blast radius.** Only this test asserted the negative. The positive is now asserted here,
      in the corpus (both languages) and in the T-307 reproduction.

    The replacement pins the ceiling in BOTH directions, because a cap that is read and a cap that
    is ignored answer identically when the cap happens to be satisfied. A ceiling of 0 or 20 must
    refuse this 85% bid by name; a ceiling of 85 must not.
    """
    table = make_snapshot_table()
    deep = make_bid(offer=priced_offer(15.0, 15.0, depth=85.0))
    over_depth = "discount_over_authorized_depth:offer.discount"
    unreadable = "price_unreconcilable:offer.discount:unreadable_authorized_depth"
    unavailable = "price_unreconcilable:offer.discount:authorized_depth_unavailable"

    for path in BOTH_PATHS:

        def judge(cap: Any, path: str = path) -> Any:
            return validate_bid(
                deep, path=path, trust_snapshot=table, now=NOW, max_discount_pct=cap
            )

        # No ceiling at all: the depth is unauthorized, and says so.
        assert unavailable in list(judge(None).reasons), list(judge(None).reasons)

        # A ceiling the declared 85% exceeds. `0.0` is the one the old contract lost outright.
        for cap in (0.0, 20.0, 84.999):
            assert over_depth in list(judge(cap).reasons), (cap, list(judge(cap).reasons))

        # ...and a ceiling that COVERS the declared depth does not refuse it. Without this half a
        # door that ignored the ceiling and refused everything would satisfy the half above.
        for cap in (85.0, 100.0):
            reasons = list(judge(cap).reasons)
            assert over_depth not in reasons, (cap, reasons)
            assert unavailable not in reasons, (cap, reasons)

        # A ceiling the door cannot read is a refusal, not an unbounded one — with no roster too.
        for cap in ("nonsense", float("nan"), -1.0, 101.0, True):
            assert unreadable in list(judge(cap).reasons), (cap, list(judge(cap).reasons))

        # The list price is still missing in every one of these calls, so none of them admits:
        # T-307 is about the ceiling being READ, not about it being sufficient on its own.
        for cap in (None, 0.0, 20.0, 85.0, 100.0, "nonsense"):
            assert judge(cap).ok is False, cap
            assert "price_unreconcilable:offer.unit_price:list_price_unavailable" in list(
                judge(cap).reasons
            ), cap


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
    """Every bid must answer identically with `list_prices` absent and with it explicitly `None`.

    Unchanged by T-306, and it says MORE now than it used to: the two spellings used to agree on
    the abstention, and they agree on the refusal instead. `test_the_absent_roster_is_the_empty_
    roster` below extends the same equality to `{}`, which is the spelling the two used to differ
    from.
    """
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
    store not running our runtime ever reaches.

    JUSTIFY-TEST-EDIT. One assertion was REPLACED::

        admitted = validate_external_submission(submission, trust_snapshot=table, now=NOW)
        assert admitted.ok is True, list(admitted.reasons)

    It claimed that a signed external submission charging 15.00 for `prod-1`, behind a declared
    20%, is ADMITTED when the exchange hands the door no roster. Origin `57c25df`, as the "before"
    half of the roster reproduction on the external door. It would still be wrong if the source
    change were reverted, for exactly the reason the other two in this file are: the same call
    with `list_prices={}` refused this submission the whole time, so the admission recorded which
    argument the caller named rather than anything about the bid — T-306, on the door the packet
    calls "the door it matters most on". The positive control that a door refusing everything is
    not a wall now sits on an honest submission instead, which is what a control should be.
    """
    submission = make_submission(
        offer=priced_offer(15.0, 15.0),
        auction_id="auc-1",
        store_id="store-1",
        signer_id="store-1",
        signature="sig-deadbeef",
    )
    table = make_snapshot_table()

    bare = validate_external_submission(submission, trust_snapshot=table, now=NOW)
    assert bare.ok is False, "the external door was gentler with a caller that said nothing"
    assert list(bare.reasons) == list(
        validate_external_submission(
            submission, trust_snapshot=table, now=NOW, list_prices={}
        ).reasons
    ), list(bare.reasons)
    assert "price_unreconcilable:offer.unit_price:list_price_unavailable" in list(bare.reasons)

    refused = validate_external_submission(
        submission, trust_snapshot=table, now=NOW, list_prices=ROSTER
    )
    assert refused.ok is False
    assert "price_under_declared_depth:offer.unit_price" in list(refused.reasons), refused.reasons

    # Positive control: the honest submission at the price that depth prices out at is admitted,
    # so this door is not simply refusing every signed submission it is handed.
    honest = make_submission(
        offer=priced_offer(80.0, 80.0),
        auction_id="auc-1",
        store_id="store-1",
        signer_id="store-1",
        signature="sig-deadbeef",
    )
    admitted = validate_external_submission(
        honest, trust_snapshot=table, now=NOW, list_prices=ROSTER
    )
    assert admitted.ok is True, list(admitted.reasons)


def test_the_absent_roster_is_the_empty_roster_on_both_doors() -> None:
    """T-306, stated as its own property rather than as a side effect of the tests above.

    Three spellings of "this caller supplied no catalog" — the argument OMITTED, the argument
    `None`, the argument `{}` — must produce one verdict, reasons included, for every bid. The
    one that used to differ is the first, and it differed in the permissive direction, which is
    the direction a forgotten argument must never differ in.
    """
    table = make_snapshot_table()
    bids = (
        make_bid(),
        make_bid(offer=priced_offer(15.0, 15.0)),
        make_bid(offer=priced_offer(15.0, 15.0, depth=0.0)),
        make_bid(
            claims=[make_claim("list_price", 100.0, dict(HOOK_PROVENANCE))],
            offer=priced_offer(15.0, 15.0),
        ),
        make_bid(offer=priced_offer(100.0, 15.0)),
        make_bid(offer=priced_offer(0.0, 0.0, depth=100.0)),
    )
    for bid in bids:
        for path in BOTH_PATHS:
            for cap in (None, 0.0, 20.0):
                omitted = validate_bid(
                    bid, path=path, trust_snapshot=table, now=NOW, max_discount_pct=cap
                )
                spellings = [
                    validate_bid(
                        bid,
                        path=path,
                        trust_snapshot=table,
                        now=NOW,
                        list_prices=roster,
                        max_discount_pct=cap,
                    )
                    for roster in (None, {})
                ]
                for other in spellings:
                    assert (omitted.ok, list(omitted.reasons)) == (
                        other.ok,
                        list(other.reasons),
                    ), (path, cap, list(omitted.reasons), list(other.reasons))
