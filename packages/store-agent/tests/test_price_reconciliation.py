"""T-153 — the price a bid states must follow from the discount it was granted.

`test_hooks_boundary.py` §8 closed the *floor* half of this: a bid stating 1.00 on a product
whose approved floor is 10.00 is refused, because the floor is a wall on a price and a legal
percentage is not a licence to state any number under it. That wall is a wall on the *cheapest
price the merchant will ever accept*, and it is the only price wall there was.

It leaves the arithmetic between `list_price`, the granted depth and the stated price entirely
unchecked, and that gap is wider than the floor wall makes it look. `prod-cap` lists at 100.00
with a floor of 10.00 and a 20% cap, so the honest price behind an honest 20% grant is 80.00 —
and every price from 10.00 to 80.00 clears the floor, clears the cap, and is backed by a genuine
grant for a genuine depth. 70.00 of unauthorised discount sits inside the walls. The bid says
"20% off" and charges 88% off, and until now nothing on the path compared the two: the depth is
a *description* of the price, and nothing made the description true.

So the property under test is not "the price clears a limit" but **the discount the stated price
implies must be one a hook actually granted**:

    list_price - unit_price  <=  list_price * granted_pct / 100

Read the other way round — `unit_price >= list_price * (100 - granted_pct) / 100` — it is the
same arithmetic `ToolHooks._evaluate_discount` runs when it decides whether to grant at all, and
deliberately in the same `(100 - pct) / 100` form, so the wall and the grant cannot come to
different conclusions about what 20% off 100.00 is.

Only `unit_price` is reconciled. `Offer` carries `total_price` but no quantity, and the quantity
that would relate the two lives in the checkout path, not on the protocol object — so the
relation between them is genuinely undecidable at this boundary and is refused as a subject
rather than guessed at. See `_price_reconciliation_refusal`.

Every refusal test here fails against the tree as it stood before this file was written: the bid
in each was ADMITTED. The admission tests are the other half — a wall that refuses honest traffic
is not a wall, it is an outage — and several of them passed before, on purpose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from contracts import Bid, Discount, Offer
from store_agent.hooks import (
    REASON_UNPRICEABLE_PRODUCT,
    Denied,
    HookProvenanceError,
    ToolHooks,
    enforce_bid_provenance,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ENVELOPE_FIXTURES = REPO_ROOT / "fixtures" / "envelopes"

CLUSTER = "cluster-warm-layers"

#: `prod-cap` in the approved store-alpha envelope: lists at 100.00, floored at 10.00, capped at
#: 20%. Named rather than repeated so a fixture edit shows up as a failure here, not as a test
#: that quietly stops testing anything.
LIST_PRICE = 100.0
FLOOR = 10.0

#: A product the approved envelope names **no floor for**, so its floor is 0.0 and the floor wall
#: admits every non-negative number. Anything a test proves with this product is proved by the
#: wall under test rather than by the floor wall standing behind it.
UNFLOORED = "prod-unfloored"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _context_from(fixture: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    envelope = fixture["envelope"]
    context: dict[str, Any] = {
        "store_id": envelope["store_id"],
        "envelope": envelope,
        "catalog": fixture["catalog"],
        "live_state": fixture.get("live_state", {}),
        "learned_policy": None,
        "network_priors": {CLUSTER: {"depth_buckets": [0.0, 0.05, 0.1, 0.15, 0.2]}},
    }
    context.update(overrides)
    return context


@pytest.fixture
def alpha() -> dict[str, Any]:
    return _load(ENVELOPE_FIXTURES / "store-alpha.approved.json")


@pytest.fixture
def hooks(alpha: dict[str, Any]) -> ToolHooks:
    return ToolHooks(_context_from(alpha))


def _offer(product_ref: str, **overrides: Any) -> Offer:
    """An offer at list price with no discount — the honest cold-start shape.

    The default is deliberately the *reconciled* one: 100.00 is `prod-cap`'s list price, so a
    test that changes the price without saying why is changing the thing under test.
    """
    fields: dict[str, Any] = {
        "product_ref": product_ref,
        "unit_price": LIST_PRICE,
        "total_price": LIST_PRICE,
        "currency": "USD",
        "commitments": [],
    }
    fields.update(overrides)
    return Offer(**fields)


def _pct(value: float) -> Discount:
    return Discount(type="percentage", value=value)


def _bid(offer: Offer, claims: list[Any]) -> Bid:
    return Bid(
        auction_id="auction-1",
        store_id="store-alpha",
        offer=offer,
        claims=list(claims),
        agent_version="store-agent/test",
        schema_version="1.0.0",
    )


def _granted(hooks: ToolHooks, product_ref: str, pct: float) -> Any:
    grant = hooks.authorize_discount(product_ref, pct)
    assert not isinstance(grant, Denied), (
        f"{pct}% on {product_ref} must be inside the envelope's walls, or the test that follows "
        f"proves nothing: {grant!r}"
    )
    return grant


# ---------------------------------------------------------------------------------------------
# The defect: a genuine grant, a legal depth, a price that follows from neither
# ---------------------------------------------------------------------------------------------


def test_a_stated_price_that_does_not_follow_from_the_granted_depth_is_refused(
    hooks: ToolHooks,
) -> None:
    """The reported defect, exactly as reported. Everything about this bid is genuine but one number.

    A real `authorize_discount("prod-cap", 20.0)` grant, a real 20% discount on the offer that
    the grant backs, and a stated unit price of 10.00. 10.00 clears the 10.00 floor, so the floor
    wall has no objection; the depth is 20% and the cap is 20%, so the cap has no objection; the
    grant is in the ledger, scoped to this product and unspent, so the provenance walls have no
    objection. The honest price is 80.00. The bid takes 90.00 off and was granted 20.00.
    """
    grant = _granted(hooks, "prod-cap", 20.0)
    assert hooks.price_floor("prod-cap") == FLOOR, (
        "the floor must not object, or this is the H3 test"
    )

    dishonest = _bid(
        _offer("prod-cap", unit_price=10.0, total_price=10.0, discount=_pct(20.0)), [grant]
    )
    with pytest.raises(HookProvenanceError) as raised:
        enforce_bid_provenance(dishonest, hooks)

    reasons = " ".join(reason for _, reason in raised.value.offenders)
    assert ".offer.unit_price" in reasons, (
        f"the refusal must name the price it is about, not the discount: {reasons}"
    )
    assert "floor" not in reasons, (
        "10.00 clears the 10.00 floor — a refusal that says 'floor' here is the old wall firing "
        f"on the wrong evidence, not the reconciliation: {reasons}"
    )

    # And the honest number passes the identical call, so the test cannot be satisfied by a
    # boundary that simply refuses every bid carrying a price.
    honest = _bid(
        _offer("prod-cap", unit_price=80.0, total_price=80.0, discount=_pct(20.0)),
        [_granted(hooks, "prod-cap", 20.0)],
    )
    assert enforce_bid_provenance(honest, hooks), "100.00 less an authorized 20% is 80.00"


def test_a_price_between_the_floor_and_the_honest_price_is_refused(hooks: ToolHooks) -> None:
    """The whole 10.00-to-80.00 band, not just its bottom, and not just its edge.

    The floor wall makes the defect look like an edge case about implausibly cheap prices. It is
    not: every price in this band is one the floor admits and the grant does not authorize, and
    the most dangerous ones are the plausible ones near the top.
    """
    for stated in (11.0, 25.0, 50.0, 75.0, 79.5):
        grant = _granted(hooks, "prod-cap", 20.0)
        bid = _bid(
            _offer("prod-cap", unit_price=stated, total_price=stated, discount=_pct(20.0)), [grant]
        )
        with pytest.raises(HookProvenanceError, match="unit_price"):
            enforce_bid_provenance(bid, hooks)


def test_a_price_cut_that_declares_no_discount_at_all_is_refused(hooks: ToolHooks) -> None:
    """The same theft with the paperwork removed rather than forged.

    `_discount_refusal` guards `offer.discount`; an offer that simply omits the field has no
    discount to guard, and 50.00 on a 100.00 product clears the 10.00 floor. Nothing else on the
    path looks at `list_price`, so a bid that takes half off in silence was admitted with no
    grant, no discount and no claim of any kind. A price under list *is* a discount; the boundary
    should not need it to be announced before it will see it.
    """
    silent = _bid(_offer("prod-cap", unit_price=50.0, total_price=50.0), [])
    with pytest.raises(HookProvenanceError, match="unit_price"):
        enforce_bid_provenance(silent, hooks)


def test_an_undeclared_cut_is_refused_even_with_a_grant_for_that_depth_in_the_bid(
    hooks: ToolHooks,
) -> None:
    """A grant in the bid is not a discount on the offer, and the reconciliation is per offer.

    Fail closed on purpose. Letting a loose grant license any offer's price would make the depth
    a bid-wide allowance rather than a per-offer authorization — three offers at 80.00 behind one
    20% grant, which is precisely the "exactly once" property `_discount_refusal` exists to
    enforce, walked around by pricing instead of discounting. The offer that spends a depth must
    say so where it is priced.
    """
    grant = _granted(hooks, "prod-cap", 20.0)
    undeclared = _bid(_offer("prod-cap", unit_price=80.0, total_price=80.0), [grant])
    with pytest.raises(HookProvenanceError, match="unit_price"):
        enforce_bid_provenance(undeclared, hooks)


def test_the_price_is_reconciled_where_the_offer_is_rather_than_bid_wide(hooks: ToolHooks) -> None:
    """A second offer must not ride the first offer's discount.

    The bid names one product; the boundary collects one price per priced node. A dict-shaped bid
    can carry a second offer under a key the protocol does not define — which is where the last
    payload went — and each priced node has to answer for its own number.

    **`.bundled` is priced at 80.00 on purpose, and the number is the whole test.** At 40.00 this
    case proved nothing: 40.00 is refused by a bid-wide reconciliation too — it is 60% off a
    100.00 list price and the deepest grant in the bid is 20% — so the assertion held whether the
    declared depth was looked up per node or taken as the maximum anywhere in the bid. 80.00 is
    the price that separates the two readings: it follows exactly from the 20% `.offer` declares
    and from nothing `.bundled` declares at all, so a boundary that licensed every priced node
    with the deepest grant in the bid would admit it, and one that reconciles each node against
    its own declaration refuses it as an undeclared cut. (Measured: replacing the per-offer
    `declared.get(node, 0.0)` with `max(declared.values(), default=0.0)` left the 40.00 version
    of this test green.)
    """
    grant = _granted(hooks, "prod-cap", 20.0)
    two_offers = {
        "claims": [grant],
        "offer": {
            "product_ref": "prod-cap",
            "unit_price": 80.0,
            "total_price": 80.0,
            "discount": {"type": "percentage", "value": 20.0},
        },
        "bundled": {
            "product_ref": "prod-cap",
            "unit_price": 80.0,
            "total_price": 80.0,
        },
    }
    with pytest.raises(HookProvenanceError) as raised:
        enforce_bid_provenance(two_offers, hooks, product_ref="prod-cap")
    reasons = " ".join(reason for _, reason in raised.value.offenders)
    assert ".bundled.unit_price" in reasons, (
        f"the second priced node is the one that is wrong, and must be the one named: {reasons}"
    )


# ---------------------------------------------------------------------------------------------
# Two places a price could hide from BOTH walls, found while trying to defeat this one
# ---------------------------------------------------------------------------------------------


def test_a_priced_node_carrying_no_claim_fields_is_still_collected(hooks: ToolHooks) -> None:
    """The walker recorded a price only off a node that had a claim-bearing field to begin with.

    `Offer` always carries `commitments`, so every offer built as a model was collected and the
    gap never showed. A bid built as a *dict* can write `{"product_ref": ..., "unit_price": ...}`
    under any key it likes, and that node reached neither the floor wall nor the reconciliation —
    it was not in `ClaimMaterial.prices` at all. Both price walls were evadable this way, the
    floor one included, which is why this is pinned as a property of the walker rather than only
    as a case of the wall above.
    """
    from store_agent.hooks.provenance import collect_claim_material

    bid = {
        "claims": [],
        "offer": {"product_ref": "prod-cap", "unit_price": LIST_PRICE, "total_price": LIST_PRICE},
        "alternates": [{"product_ref": "prod-cap", "unit_price": 3.0, "total_price": 3.0}],
    }
    collected = collect_claim_material(bid)
    assert [path for path, _, _ in collected.prices] == [
        ".offer.unit_price",
        ".alternates[0].unit_price",
    ], f"every priced node in the bid must be in the boundary's field of view: {collected.prices}"

    with pytest.raises(HookProvenanceError) as raised:
        enforce_bid_provenance(bid, hooks, product_ref="prod-cap")
    reasons = " ".join(reason for _, reason in raised.value.offenders)
    assert "floor" in reasons and ".alternates[0].unit_price" in reasons, (
        f"3.00 is under the 10.00 floor AND 97% off list; both walls should say so: {reasons}"
    )


def test_a_price_hidden_behind_a_genuine_claims_identity_is_refused(hooks: ToolHooks) -> None:
    """The disguise test's sibling, with the disguise stripped down to what actually hid a price.

    `test_an_offer_wearing_a_genuine_claims_identity_is_refused` bolts a discount and commitments
    onto a real claim, and those fields are what made the walker treat the node as a container.
    Take them away and leave only `product_ref` and `unit_price` — the two fields that make a
    node an offer — and it was claim-shaped with no structural fields at all: a leaf, admitted on
    a fingerprint that genuinely matched, carrying a 1.00 price that no wall ever saw. A claim's
    identity covers the claim, not a price stapled to it.
    """
    real = hooks.get_owner_commitments(CLUSTER)[0].model_dump()
    assert "product_ref" not in real and "unit_price" not in real, (
        "a genuine commitment carries neither field, so the two below are the smuggled ones"
    )
    payload = {**real, "product_ref": "prod-cap", "unit_price": 1.0}

    with pytest.raises(HookProvenanceError) as raised:
        enforce_bid_provenance([payload], hooks, product_ref="prod-cap")
    reasons = " ".join(reason for _, reason in raised.value.offenders)
    assert "unit_price" in reasons, f"the price it smuggled must be named: {reasons}"
    assert "unit_price" in reasons.split("identity AND an offer's fields")[-1][:80], (
        f"and the disguise refusal must list the offer fields it found, not an empty set: {reasons}"
    )

    # The same claim without the two bolted-on fields is exactly what it says it is.
    assert enforce_bid_provenance([dict(real)], hooks, product_ref="prod-cap")


# ---------------------------------------------------------------------------------------------
# The other half: honest traffic still passes, and rounding is not theft
# ---------------------------------------------------------------------------------------------


def test_an_offer_at_list_price_with_no_discount_is_admitted(hooks: ToolHooks) -> None:
    """The cold-start bid: no grant, no discount, list price. Zero off is authorized by nothing."""
    cold = _bid(_offer("prod-cap"), list(hooks.get_owner_commitments(CLUSTER)))
    assert enforce_bid_provenance(cold, hooks)


def test_every_authorized_depth_prices_out_and_is_admitted(hooks: ToolHooks) -> None:
    """The honest price behind each depth the envelope allows, computed the envelope's own way."""
    for pct in (0.0, 5.0, 10.0, 15.0, 20.0):
        grant = _granted(hooks, "prod-cap", pct)
        price = LIST_PRICE * (100.0 - pct) / 100.0
        bid = _bid(
            _offer("prod-cap", unit_price=price, total_price=price, discount=_pct(pct)), [grant]
        )
        assert enforce_bid_provenance(bid, hooks), f"{pct}% off {LIST_PRICE} is {price}"


def test_a_price_above_the_discounted_one_is_not_a_refusal(hooks: ToolHooks) -> None:
    """The wall is one-sided, because the harm is one-sided, and this pins that it is deliberate.

    An offer that states MORE than its depth would produce spends less of the envelope than it
    was granted: the merchant's floor is cleared by a wider margin and the discount actually
    given is shallower than the one authorized. There is nothing here for a wall about
    authorization to refuse, and refusing it would turn every rounding-up into an outage.

    What such an offer *is* is a discount advertised and not given, which is a claim-honesty
    question about what the buyer is told rather than an authorization question about what the
    merchant approved. It is not this wall's subject and is recorded as a known gap.
    """
    grant = _granted(hooks, "prod-cap", 20.0)
    generous = _bid(
        _offer("prod-cap", unit_price=LIST_PRICE, total_price=LIST_PRICE, discount=_pct(20.0)),
        [grant],
    )
    assert enforce_bid_provenance(generous, hooks)


def test_a_cent_of_currency_rounding_is_not_an_unauthorised_discount(alpha: dict[str, Any]) -> None:
    """19.99 less 15% is 16.9915, and no bid states that. The wall must survive real money.

    A reconciliation tightened to the float would refuse the honest rounded price of almost every
    real product, which is how a correct-looking wall becomes an outage. A cent of slack is far
    below any discount a merchant could feel, and the floor wall still caps how cheap the number
    may get regardless.

    **The second half is 16.00 rather than 15.99, and the change is what makes the assertion
    mean its own docstring.** The refusal is ``stated + tolerance < honest``, so at 15.99 it held
    for every tolerance below 1.0015 — the test that exists to say "a dollar is not rounding"
    survived a tolerance of a whole dollar, by a seventh of a cent. 16.00 is the cheapest price
    quotable in cents that a one-dollar tolerance *would* wave through (16.00 + 1.00 ≥ 16.9915),
    so the assertion now discriminates at the scale it claims to. How tight the tolerance
    actually is, is pinned separately and much harder by
    `test_the_reconciliation_tolerance_is_a_cent_of_currency_and_not_a_dollar`.
    """
    hooks = ToolHooks(
        _context_from(
            alpha,
            catalog={
                "prod-cap": {"product_ref": "prod-cap", "list_price": 19.99, "material": "merino"}
            },
        )
    )
    grant = _granted(hooks, "prod-cap", 15.0)
    rounded = _bid(
        _offer("prod-cap", unit_price=16.99, total_price=16.99, discount=_pct(15.0)), [grant]
    )
    assert enforce_bid_provenance(rounded, hooks), "16.99 is 16.9915 rounded to the cent"

    # A dollar is not rounding: 16.00 is 99 cents under the honest 16.9915.
    cheating = _bid(
        _offer("prod-cap", unit_price=16.00, total_price=16.00, discount=_pct(15.0)),
        [_granted(hooks, "prod-cap", 15.0)],
    )
    with pytest.raises(HookProvenanceError, match="unit_price"):
        enforce_bid_provenance(cheating, hooks)


def test_a_product_the_catalog_does_not_list_cannot_be_reconciled_and_is_refused(
    hooks: ToolHooks,
) -> None:
    """No list price, no reconciliation — and "no reconciliation" must not mean "admitted".

    The floor for an unlisted product is 0.0 (the envelope names no floor for it), so the floor
    wall admits any non-negative number. Refusing rather than guessing is the same choice
    `_authorization_refusal` makes about an unnamed product.
    """
    stranger = _bid(_offer("prod-unlisted", unit_price=1.0, total_price=1.0), [])
    with pytest.raises(HookProvenanceError, match="unit_price"):
        enforce_bid_provenance(stranger, hooks)


def test_a_facade_that_cannot_report_a_list_price_is_refused_rather_than_trusted(
    hooks: ToolHooks,
) -> None:
    """The same choice `_price_refusal` makes about a facade with no `price_floor`.

    A boundary that skipped the reconciliation whenever the facade could not answer would be a
    boundary any facade could switch off by not implementing a method.
    """

    class Blind:
        """Everything the boundary needs except a list price."""

        def __init__(self, real: ToolHooks) -> None:
            self._real = real
            self.emitted_fingerprints = real.emitted_fingerprints

        def __getattr__(self, name: str) -> Any:
            if name == "list_price":
                raise AttributeError(name)
            return getattr(self._real, name)

    grant = _granted(hooks, "prod-cap", 20.0)
    bid = _bid(_offer("prod-cap", unit_price=80.0, total_price=80.0, discount=_pct(20.0)), [grant])
    with pytest.raises(HookProvenanceError, match="list price"):
        enforce_bid_provenance(bid, Blind(hooks))


def test_the_floor_wall_is_still_an_independent_second_wall(hooks: ToolHooks) -> None:
    """Reconciliation does not replace the floor: a floored product refuses a depth outright.

    `prod-floor` lists at 100.00 with a 95.00 floor, so even a 15% grant — comfortably inside the
    20% cap — is denied by `authorize_discount` before any bid exists. The two walls answer
    different questions and this pins that both still fire.
    """
    assert isinstance(hooks.authorize_discount("prod-floor", 15.0), Denied), (
        "15% off 100.00 is 85.00, under prod-floor's 95.00 floor"
    )
    at_the_floor = _bid(
        _offer("prod-floor", unit_price=95.0, total_price=95.0, discount=_pct(5.0)),
        [_granted(hooks, "prod-floor", 5.0)],
    )
    assert enforce_bid_provenance(at_the_floor, hooks), "5% off 100.00 lands exactly on the floor"


# ---------------------------------------------------------------------------------------------
# T-155 — the walk must not stop, and stopping it must not be how the cycle hazard is handled
# ---------------------------------------------------------------------------------------------


def _buried(payload: dict[str, Any], layers: int) -> dict[str, Any]:
    """`payload` under `layers` dicts, each keyed by a name the protocol never defined.

    Every layer is a key `_sweep` has to look under of its own accord, which is exactly where the
    last three payloads went. Nesting is free to an attacker and the boundary is the only thing
    that pays for it, so "how deep" must not be a number the boundary has an opinion about.
    """
    for level in range(layers):
        payload = {f"layer{level}": payload}
    return payload


#: Depths both properties below are graded at. ADDITIVE: `[1, 11, 13, 14, 64]` is every value
#: this parametrization already carried, kept verbatim, with `65, 128, 256` appended (T-220).
#:
#: The original values were chosen either side of the OLD bound of 12 and stopped at 64, which
#: pinned the fix that was made rather than the property that was promised: a depth bound
#: re-introduced at 65 — or 100, or 200 — was invisible to the entire suite, and the ticket's own
#: reproduction is "re-introduce a depth bound of 65 in `_walk` and run the suite: green". 65 is
#: the first depth the old coverage could not see; 256 is four times the old ceiling.
#:
#: 256 rather than 1000, and that ceiling is measured rather than guessed: past roughly 450-500
#: layers `_walk` meets the interpreter's own recursion limit, `collect_claim_material` catches
#: the `RecursionError`, and the bid is still REFUSED — but with the `unwalkable` reason instead
#: of the price wall's, so `assert "unit_price" in reasons` would go red for a CORRECT behaviour.
#: 256 sits well inside the price-wall regime.
_DEPTHS = [1, 11, 13, 14, 64, 65, 128, 256]


@pytest.mark.parametrize("layers", _DEPTHS)
def test_a_priced_node_is_collected_however_deep_the_bid_buries_it(
    hooks: ToolHooks, layers: int
) -> None:
    """The walk gave up at depth 12 and gave up *silently*, which is a wall with a door in it.

    The bound was there to stop a self-referential dict hanging the boundary, and it did — by
    truncating every walk, cycle or not. A node the walk never reached is not admitted "on the
    evidence": it is admitted having been inspected by nothing at all, so this bypasses the claim
    provenance wall, the floor wall and the reconciliation alike. Measured on the tree before the
    fix: `{"product_ref": "prod-cap", "unit_price": 1.0}` under 14 dicts was ADMITTED, while the
    identical node under 10 was refused by both price walls.

    Depth is therefore parametrized either side of the old bound and far past it. There is no
    depth at which a bid stops being checked; the cycle hazard is handled by
    `test_a_self_referential_bid_is_refused_rather_than_walked_forever`, which is what a bound
    should have been all along.

    **And "far past it" now means past a bound a repair could re-introduce.** Every value here
    used to sit at or below 64 — one layer above the old bound of 12, chosen when 12 was the
    number that mattered — so a NEW bound at 65 would have been graded by nothing (T-220). See
    `_DEPTHS`.
    """
    bid = {
        "offer": {"product_ref": "prod-cap", "discount": None},
        "claims": [],
        **_buried({"product_ref": "prod-cap", "unit_price": 1.0, "total_price": 1.0}, layers),
    }
    with pytest.raises(HookProvenanceError) as raised:
        enforce_bid_provenance(bid, hooks, product_ref="prod-cap")
    reasons = " ".join(reason for _, reason in raised.value.offenders)
    assert "unit_price" in reasons, (
        f"1.00 on a 100.00 product is 99% off and under the 10.00 floor, at any depth: {reasons}"
    )


@pytest.mark.parametrize("layers", _DEPTHS)
def test_a_claim_is_checked_however_deep_the_bid_buries_it(hooks: ToolHooks, layers: int) -> None:
    """Wider than pricing: the *provenance* wall was reachable around too.

    `test_a_claim_under_a_key_the_protocol_never_defined_is_still_checked` proves the sweep looks
    under unknown keys; it nests two deep. The same unhooked commitment under fourteen was
    admitted, ledger unconsulted — the defect is in the walk, so it is in every wall the walk
    feeds.

    Graded at the same depths as the priced-node property above, and for the same reason: this
    one stopped at 64 too, so a re-introduced bound at 65 was invisible to it as well (T-220).
    See `_DEPTHS`.
    """
    unhooked = {
        "key": "free_returns",
        "value": "90 days, no questions asked",
        "provenance": {
            "source": "owner_statement",
            "ref": "envelope:store-alpha:v3#free_returns",
            "observed_at": "2026-01-01T00:00:00Z",
            "authority_rank": 1,
        },
    }
    bid = {
        "offer": {"product_ref": "prod-cap", "discount": None},
        "claims": [],
        **_buried(unhooked, layers),
    }
    with pytest.raises(HookProvenanceError):
        enforce_bid_provenance(bid, hooks, product_ref="prod-cap")


def test_a_self_referential_bid_is_refused_rather_than_walked_forever(hooks: ToolHooks) -> None:
    """The hazard the depth bound was actually for, closed where it belongs.

    A cycle is not honest traffic and cannot be: JSON has no way to express one, so a bid that
    arrived over the wire never contains one. Pruning it silently would leave the boundary
    reporting on a structure that is not the one it was handed, so the cycle is *named* and
    refused — the same choice `_sweep` makes about everything else it cannot vouch for.

    Cut at an arbitrary depth, because the point is that the cycle is what stops the walk now and
    not the layer count.
    """
    grant = _granted(hooks, "prod-cap", 20.0)
    cyclic: dict[str, Any] = {
        "offer": {
            "product_ref": "prod-cap",
            "unit_price": 80.0,
            "total_price": 80.0,
            "discount": {"type": "percentage", "value": 20.0},
        },
        "claims": [grant],
    }
    nested = _buried(cyclic, 20)
    cyclic["ouroboros"] = nested

    with pytest.raises(HookProvenanceError) as raised:
        enforce_bid_provenance(cyclic, hooks, product_ref="prod-cap")
    reasons = " ".join(reason for _, reason in raised.value.offenders)
    assert "ouroboros" in reasons, (
        f"the refusal must name where the bid closed back on itself: {reasons}"
    )
    assert "itself" in reasons or "self-referential" in reasons, (
        f"and must say that is what happened, not report it as a bad claim: {reasons}"
    )


def test_one_object_reachable_by_two_paths_is_not_a_cycle(hooks: ToolHooks) -> None:
    """Cycle detection scoped to the current path, not to the whole walk.

    A bid that mentions the same object twice — one dict bound to two keys, which is ordinary in
    any structure assembled in memory rather than parsed from JSON — is a DAG, not a cycle. A
    walker that remembered every object it had ever seen would walk the second mention as
    "already visited" and skip it, which is the depth bound's defect wearing a different hat: a
    priced node admitted because the boundary decided it had looked at it somewhere else.
    """
    from store_agent.hooks.provenance import collect_claim_material

    shared = {"product_ref": "prod-cap", "unit_price": 1.0, "total_price": 1.0}
    bid = {
        "claims": [],
        "offer": {"product_ref": "prod-cap", "unit_price": LIST_PRICE, "total_price": LIST_PRICE},
        "first": shared,
        "second": {"deeper": shared},
    }
    collected = collect_claim_material(bid)
    assert [path for path, _, _ in collected.prices] == [
        ".offer.unit_price",
        ".first.unit_price",
        ".second.deeper.unit_price",
    ], f"both mentions of one object are two priced nodes in the bid: {collected.prices}"
    assert not collected.unwalkable, f"nothing here is a cycle: {collected.unwalkable}"

    with pytest.raises(HookProvenanceError) as raised:
        enforce_bid_provenance(bid, hooks, product_ref="prod-cap")
    reasons = " ".join(reason for _, reason in raised.value.offenders)
    assert ".first.unit_price" in reasons and ".second.deeper.unit_price" in reasons, (
        f"and both must be refused, not just the first one reached: {reasons}"
    )


def test_an_honestly_deep_metadata_blob_is_still_admitted(hooks: ToolHooks) -> None:
    """The other half, and the reason "refuse everything past the bound" was never the fix.

    `_sweep` is written to tolerate a bid carrying material the protocol says nothing about. Deep
    nesting is not evidence of anything — a trace blob is genuinely shaped like that — so depth
    alone must not refuse a bid any more than it may admit one.
    """
    grant = _granted(hooks, "prod-cap", 20.0)
    assert enforce_bid_provenance(
        {
            "auction_id": "a1",
            "store_id": "store-alpha",
            "offer": {
                "product_ref": "prod-cap",
                "unit_price": 80.0,
                "total_price": 80.0,
                "discount": {"type": "percentage", "value": 20.0},
            },
            "claims": [grant],
            "trace": _buried({"note": "nothing claim-shaped down here", "spans": [1, 2, 3]}, 40),
            "agent_version": "v",
            "schema_version": "1.0.0",
        },
        hooks,
    ) == [grant]


# ---------------------------------------------------------------------------------------------
# T-179 — the tolerance is a cent, and a test has to be able to tell
# ---------------------------------------------------------------------------------------------


def test_the_reconciliation_tolerance_is_a_cent_of_currency_and_not_a_dollar(
    hooks: ToolHooks,
) -> None:
    """Both sides of the slack, because a tolerance defended from one side is not defended.

    `prod-cap` lists at 100.00 and an honest 20% prices out at exactly 80.00, which makes the two
    numbers here readable without a calculator: 79.99 is the honest price rounded to the cent and
    must be admitted, 79.98 is two cents of discount nobody granted and must be refused.

    Measured before this test existed: the tolerance could be widened to 0.49 or 0.25 — a
    49-cent-per-unit unauthorised discount, on every unit — and the entire 3368-test suite stayed
    green. Together these two assertions bracket it into [0.01, 0.02): shrinking it to zero turns
    the first red, loosening it by a single cent turns the second red.
    """
    rounded = _bid(
        _offer("prod-cap", unit_price=79.99, total_price=79.99, discount=_pct(20.0)),
        [_granted(hooks, "prod-cap", 20.0)],
    )
    assert enforce_bid_provenance(rounded, hooks), (
        "79.99 is 80.00 short by a cent, which is the rounding the tolerance exists for"
    )

    two_cents_under = _bid(
        _offer("prod-cap", unit_price=79.98, total_price=79.98, discount=_pct(20.0)),
        [_granted(hooks, "prod-cap", 20.0)],
    )
    with pytest.raises(HookProvenanceError, match="unit_price"):
        enforce_bid_provenance(two_cents_under, hooks)


# ---------------------------------------------------------------------------------------------
# T-173 — a catalog entry that names no usable price grants nothing
# ---------------------------------------------------------------------------------------------


def _unpriceable(alpha: dict[str, Any], list_price: Any) -> ToolHooks:
    return ToolHooks(
        _context_from(
            alpha, catalog={UNFLOORED: {"product_ref": UNFLOORED, "list_price": list_price}}
        )
    )


@pytest.mark.parametrize(
    "list_price",
    ["one hundred dollars", None, {"amount": 100.0}, float("nan"), float("inf")],
    ids=["text", "missing", "structured", "nan", "inf"],
)
def test_a_catalog_entry_with_no_usable_list_price_grants_no_discount(
    alpha: dict[str, Any], list_price: Any
) -> None:
    """`x = self.list_price(ref) or 0.0` turned "unanswerable" into "free", inside the grant.

    `list_price` returns `None` rather than raising so the *bid boundary* can refuse a product it
    cannot reconcile instead of crashing on it. Reading it through `or 0.0` in the hook that
    grants undoes exactly that: `None` becomes a real list price of 0.0, every depth prices out
    at 0.0, and 0.0 clears any floor the envelope does not name — so a malformed catalog entry
    that used to raise instead mints a genuine, ledger-recorded 20% grant.

    Measured before the fix, on an unfloored product: `authorize_discount` GRANTED and
    `would_authorize` answered True for every one of these entries.

    The denial is the loud form the raise used to be: a named reason, a `Denied` the caller
    cannot mistake for a claim, and a line in the call log — without reintroducing the crash the
    accessor was added to prevent.
    """
    hooks = _unpriceable(alpha, list_price)
    assert hooks.list_price(UNFLOORED) is None, "the accessor's own answer is 'unanswerable'"
    assert hooks.price_floor(UNFLOORED) == 0.0, (
        "and no floor stands behind this product, so nothing else is doing the refusing"
    )

    verdict = hooks.authorize_discount(UNFLOORED, 20.0)
    assert isinstance(verdict, Denied), f"a price nobody can read authorizes nothing: {verdict!r}"
    assert verdict.reason == REASON_UNPRICEABLE_PRODUCT, verdict.reason
    assert not hooks.would_authorize(UNFLOORED, 20.0), (
        "the boundary's re-check reads the same arithmetic and must reach the same answer"
    )
    assert [call.outcome for call in hooks.call_log] == [REASON_UNPRICEABLE_PRODUCT], (
        f"and the refusal is recorded rather than silent: {hooks.call_log}"
    )
    assert not hooks.emitted_claims, "nothing was minted"


def test_an_unpriceable_product_is_refused_at_the_bid_boundary_too(alpha: dict[str, Any]) -> None:
    """One catalog reading, two walls, and neither of them guesses.

    The hook refuses to grant against an unreadable list price; the boundary refuses to reconcile
    one. A bid can therefore not be assembled around such a product from either end — which is
    the point of the two of them reading the merchant's numbers through the same accessor.
    """
    hooks = _unpriceable(alpha, "one hundred dollars")
    stranger = _bid(_offer(UNFLOORED, unit_price=1.0, total_price=1.0), [])
    with pytest.raises(HookProvenanceError, match="unit_price"):
        enforce_bid_provenance(stranger, hooks)


def test_a_list_price_of_zero_is_a_price_and_not_a_missing_one(alpha: dict[str, Any]) -> None:
    """The distinction the `or 0.0` erased, from the other side.

    A catalog that genuinely lists a product at 0.00 is answerable — 20% off 0.00 is 0.00 — and
    must not be swept into the same refusal as an entry nobody can read. `list_price` returns the
    float, not a falsy value the caller has to disambiguate.
    """
    hooks = _unpriceable(alpha, 0.0)
    assert hooks.list_price(UNFLOORED) == 0.0
    grant = hooks.authorize_discount(UNFLOORED, 20.0)
    assert not isinstance(grant, Denied), f"0.00 less 20% is 0.00, above a 0.00 floor: {grant!r}"
