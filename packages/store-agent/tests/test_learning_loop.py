"""R17/S4 driven through the two doors this agent actually serves.

``packages/store-agent/src/learning/`` was 955 lines with **zero product importers**, measured on
this branch by the scan in :func:`test_the_learning_package_has_a_product_importer`: the served
door ``POST /v1/bid-requests`` went ``solicitation/routes.py`` -> ``AgentRunner`` -> ``bid()`` and
never touched it, while ``hooks/tools.py`` read ``learned_policy`` off a **static operator JSON
file** and ``network_priors`` straight off the context dict. The consumer was served and the
producer was an island, so every assertion about the loop in ``test_learning.py`` graded a library
nobody called.

What this file grades is the wire, and every number below is read off an HTTP response body.

The axis (SPEC R17, after the D55 redirect)
--------------------------------------------
R17 used to read "(discount depth x commitment set)" and the loop was built as a discount tuner.
It now reads **"(pitch variant x commitment set x discount depth)"**, and S4 asserts a store
agent's *pitch-variant distribution* shifts with its own win/loss record, "with discount depth as
one axis of that policy rather than its whole content". So the arm this file measures is the
triple, and the coordinate it measures the *shift* on is the pitch variant — which facts the
advocate leads with, which is the thing a shop buys by joining (D55).

The outcome signal, stated exactly
------------------------------------
**A store agent cannot observe award.** There is no store-agent door for it: the published
contract declares exactly two operations (``packages/contracts/openapi/store-agent.openapi.json``
-> ``/v1/bid-requests``, ``/v1/trust-events``) and ``contracts.PINNED_ROUTES`` is compared against
the documents in both directions, so a third door is a contract change and not a wiring one. The
ledger's ``accepted`` event — the award itself — carries no ``dim``/``type``, so
``trust.feedback.deltas.delta_for_event`` returns ``None`` for it and the trust service pushes
nothing; that function's own docstring names ``accepted`` in the list of events there is "nothing
to tell a store about". ``contracts.LossReport`` exists as a type and is served by no route.

**What a store agent CAN observe is the trust verdict on an auction it bid in**, and it arrives
through a door that landed tonight: ``POST /v1/trust-events`` carries a ``TrustEventPayload``
whose ``event`` is the full ``LedgerEvent`` — including ``auction_id`` — and whose ``delta`` is
signed. ``AgentRunner`` already answers auctions and already ingests those events, so it is the
one object that holds both halves: the arm it played in auction A, and the verdict that later
names auction A. The join is on ``auction_id`` and it is the whole loop.

That is a narrower signal than "did I win", and it is narrower in an honest direction: a
``feedback`` event only exists for a routed buyer who bought, so a positive ``feedback_match``
delta on auction A is a purchase that matched the pitch played in A, and a negative one is a
purchase that did not. What the loop cannot see is the auctions it lost silently. This file
asserts what is reachable and does not simulate what is not.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from store_agent.main import create_app
from store_agent.solicitation import configure_solicitation

from .test_solicitation import CLUSTER, STORE_ID, context, request_body

SRC = Path(__file__).resolve().parents[1] / "src"

BID_ROUTE = "/v1/bid-requests"
TRUST_ROUTE = "/v1/trust-events"

#: How many auctions each measurement window runs. Enough that a distribution over four arms is
#: a distribution rather than a coin flip, small enough to stay a unit test.
WINDOW = 60

#: The arm the seeded record is made to favour. `availability_led` because this fixture's live
#: state carries two facts (`in_stock`, `units_left`) that no other variant leads with, so the
#: served *message* changes visibly when the arm does — the shift is legible in the product, not
#: only in the policy claim.
FAVOURED = "availability_led"


# =============================================================================================
# helpers — everything below reads an HTTP response body
# =============================================================================================


def client(**overrides: Any) -> TestClient:
    app = create_app()
    configure_solicitation(app, context=context(**overrides))
    return TestClient(app)


def served_bid(api: TestClient, auction_id: str) -> dict[str, Any]:
    response = api.post(BID_ROUTE, json=request_body(auction_id=auction_id))
    assert response.status_code == 200, (
        f"{BID_ROUTE} answered {response.status_code} for {auction_id}: {response.text}"
    )
    return response.json()


def policy_action(body: dict[str, Any]) -> dict[str, Any]:
    """The `policy_action` claim's value off a served bid. The arm, as the bid reports it."""
    actions = [c for c in body["claims"] if c["key"] == "policy_action"]
    assert len(actions) == 1, f"expected exactly one policy_action claim, got {len(actions)}"
    value = actions[0]["value"]
    assert isinstance(value, dict), f"policy_action value is not a mapping: {value!r}"
    return value


def served_variant(api: TestClient, auction_id: str) -> str:
    return str(policy_action(served_bid(api, auction_id))["pitch_variant"])


def trust_event(auction_id: str, *, delta: float, store_id: str = STORE_ID) -> dict[str, Any]:
    """A `TrustEventPayload` in the shape the trust service pushes, naming one auction.

    ``feedback_match`` and ``kind: feedback`` because that is R14's dimension and the one event
    kind that only exists for a buyer the network actually routed to this store — see the module
    docstring on why that, and not an award, is the signal.
    """
    return {
        "store_id": store_id,
        "event": {
            "event_id": f"evt-{auction_id}",
            "ts": "2026-01-02T00:00:00Z",
            "kind": "feedback",
            "auction_id": auction_id,
            "store_id": store_id,
            "payload": {"dim": "feedback_match", "type": "verified", "matched_pitch": delta > 0},
        },
        "dim": "feedback_match",
        "delta": delta,
        "pseudonymous_context": {"cluster_id": CLUSTER, "pseudonym": "psn-0001"},
    }


def push(api: TestClient, auction_id: str, *, delta: float) -> dict[str, Any]:
    response = api.post(TRUST_ROUTE, json=trust_event(auction_id, delta=delta))
    assert response.status_code == 200, (
        f"{TRUST_ROUTE} answered {response.status_code}: {response.text}"
    )
    return response.json()


def variant_distribution(api: TestClient, *, tag: str) -> Counter[str]:
    """The arm this agent chooses across a window of distinct auctions, off served bids."""
    return Counter(served_variant(api, f"auc-{tag}-{i:04d}") for i in range(WINDOW))


def teach(api: TestClient, *, favoured: str, rounds: int = WINDOW) -> Counter[str]:
    """Run auctions and answer each with the verdict its own arm earned.

    Every auction is answered through the served bid door, the arm is read off the served bid,
    and the verdict is pushed through the served trust door naming that auction. Nothing is
    injected into a state object.
    """
    played: Counter[str] = Counter()
    for index in range(rounds):
        auction_id = f"auc-teach-{index:04d}"
        variant = served_variant(api, auction_id)
        played[variant] += 1
        push(api, auction_id, delta=0.05 if variant == favoured else -0.05)
    return played


def teach_commitments(
    api: TestClient, *, favoured: tuple[str, ...], rounds: int = 90
) -> Counter[tuple[str, ...]]:
    """The same loop, rewarding the COMMITMENT SET the arm stood behind rather than its emphasis.

    A separate teacher because the two axes are learned from the same verdict: an arm is one
    triple and one trust event grades all of it. Rewarding on the commitment coordinate is how
    the middle axis of R17 is shown to move on its own.
    """
    played: Counter[tuple[str, ...]] = Counter()
    for index in range(rounds):
        auction_id = f"auc-commit-{index:04d}"
        chosen = tuple(policy_action(served_bid(api, auction_id))["commitment_keys"])
        played[chosen] += 1
        push(api, auction_id, delta=0.05 if chosen == favoured else -0.05)
    return played


# =============================================================================================
# 1. the island — armed, and it is the fact the whole ticket rests on
# =============================================================================================


def test_the_learning_package_has_a_product_importer() -> None:
    """R17's producer must be imported by something that is served, not only by tests.

    A static scan and not a call count, because "it has callers" is true of dead code by
    construction: a test is a caller. What this asks is whether a module under
    ``packages/store-agent/src`` that is NOT itself part of ``learning/`` imports it.
    """
    importers: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.parent.name == "learning":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and "learning" in (node.module or "").split("."):
                importers.append(f"{path.relative_to(SRC)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                if any("learning" in a.name.split(".") for a in node.names):
                    importers.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert importers, (
        "packages/store-agent/src/learning/ is imported by no product module: the store's own "
        "learning loop (R17) is an island, and the served bid door reaches it through nothing"
    )


# =============================================================================================
# 2. the arm is on the served bid
# =============================================================================================


def test_the_served_bid_reports_the_whole_arm_it_played() -> None:
    """R17's axis is (pitch variant x commitment set x discount depth), and the bid says which.

    All three coordinates ride on the `policy_action` claim, which is provenance-tagged
    `learned_policy` and emitted by hook 5 — so the arm a store played is evidence in the bid
    rather than a private decision, and R18 can check the pitch against the arm that produced it.
    """
    action = policy_action(served_bid(client(), "auc-0001"))
    assert "pitch_variant" in action, (
        "the served bid's policy_action names no pitch variant, so the axis SPEC R17 mandates "
        f"— (pitch variant x commitment set x discount depth) — has one coordinate: {action!r}"
    )
    assert isinstance(action["pitch_variant"], str) and action["pitch_variant"]
    assert isinstance(action["commitment_keys"], list)
    assert isinstance(action["discount_pct"], float)


def test_a_cold_store_leads_with_buyer_relevance_and_asks_for_no_discount() -> None:
    """No record of its own means no learned emphasis and no depth. Fail closed, both axes.

    A store that has run no auctions has learned nothing, and improvising an arm on a merchant's
    live bid before any evidence exists is the improvisation the hook boundary forbids. The
    depth half is the one with money on it: the envelope authorises a 20% cap, and a cap is not
    a mandate to spend it.
    """
    action = policy_action(served_bid(client(), "auc-0001"))
    assert action["pitch_variant"] == "buyer_led"
    assert action["discount_pct"] == 0.0


# =============================================================================================
# 3. the loop — outcomes through the served trust door move the served arm
# =============================================================================================


def test_the_pitch_variant_distribution_shifts_toward_this_stores_own_record() -> None:
    """S4, at the door: served bids in, served verdicts out, and the arm moves. Before and after.

    The teaching loop never touches a state object. It reads the arm off a served bid and pushes
    the verdict that arm earned through ``POST /v1/trust-events``, which is the only outcome
    channel a store agent has — see the module docstring.
    """
    api = client()
    before = variant_distribution(api, tag="before")
    assert set(before) == {"buyer_led"}, f"a cold store must explore nothing: {dict(before)}"

    played = teach(api, favoured=FAVOURED)
    assert len(played) > 1, (
        f"once it has a record the loop must explore more than one arm; it played {dict(played)}"
    )

    after = variant_distribution(api, tag="after")
    assert after[FAVOURED] > before[FAVOURED], (
        f"the favoured arm must gain share: {dict(before)} -> {dict(after)}"
    )
    assert after[FAVOURED] > WINDOW / 2, (
        f"a store taught that {FAVOURED} converts must mostly play it: {dict(after)}"
    )
    assert after.most_common(1)[0][0] == FAVOURED, (
        f"the favoured arm must be the modal one: {dict(after)}"
    )


def test_the_shift_is_visible_in_the_message_the_shopper_would_read() -> None:
    """The arm is not bookkeeping: a shifted variant changes which true facts lead the pitch."""
    api = client()
    cold = served_bid(api, "auc-cold")
    assert policy_action(cold)["pitch_variant"] == "buyer_led"

    teach(api, favoured=FAVOURED)
    warm = served_bid(api, "auc-warm")
    assert policy_action(warm)["pitch_variant"] == FAVOURED, dict(policy_action(warm))
    assert warm["message"] != cold["message"], (
        f"the learned emphasis changed no word of the pitch: {cold['message']!r}"
    )
    assert "in stock" in warm["message"].casefold() or "units left" in warm["message"].casefold(), (
        f"an availability-led pitch must lead with the live feed: {warm['message']!r}"
    )


def test_the_commitment_set_the_advocate_stands_behind_is_learned_too() -> None:
    """R17's middle axis, served. Which approved promises the advocate LEADS with is learned.

    The offer is untouched by this and that is the wall: `offer.commitments` comes from
    ``get_owner_commitments`` and carries every promise the merchant approved, whatever the loop
    decided to say first. A learned policy that could drop one would be a policy that can edit an
    envelope.
    """
    api = client()
    cold = served_bid(api, "auc-commit-cold")
    assert tuple(policy_action(cold)["commitment_keys"]) == ("free_returns", "ships_within"), (
        "a cold store stands behind every approved commitment and prefers none of them"
    )

    played = teach_commitments(api, favoured=("free_returns",))
    assert len(played) > 2, f"the commitment axis explored nothing: {dict(played)}"

    after = Counter(
        tuple(policy_action(served_bid(api, f"auc-commit-after-{i:03d}"))["commitment_keys"])
        for i in range(40)
    )
    assert after.most_common(1)[0][0] == ("free_returns",), (
        f"a store taught that leading with free_returns alone converts must mostly do it: "
        f"{dict(after)}"
    )

    warm = served_bid(api, "auc-commit-final")
    assert warm["message"].casefold().startswith("free returns"), (
        f"the learned commitment lead changed no word of the pitch: {warm['message']!r}"
    )
    approved = {c["key"] for c in warm["offer"]["commitments"]}
    assert approved == {"free_returns", "ships_within"}, (
        f"the loop dropped a merchant-approved commitment from the OFFER, which is an envelope "
        f"edit and not an emphasis: {sorted(approved)}"
    )


def test_a_store_taught_the_opposite_moves_the_opposite_way() -> None:
    """The control. Same code, same seeds, opposite record — and the arm goes the other way."""
    favoured = client()
    teach(favoured, favoured=FAVOURED)
    punished = client()
    teach(punished, favoured="assurance_led")

    a = variant_distribution(favoured, tag="split")
    b = variant_distribution(punished, tag="split")
    assert a.most_common(1)[0][0] == FAVOURED, dict(a)
    assert b.most_common(1)[0][0] == "assurance_led", dict(b)
    assert a != b, "two opposite records produced the same policy"


# =============================================================================================
# 4. determinism (S4) — no clock, no ambient RNG, on the served path
# =============================================================================================


def test_two_identical_runs_are_byte_identical() -> None:
    """Same context, same auctions, same verdicts, in two fresh processes' worth of state."""

    def run() -> str:
        api = client()
        trace = [served_bid(api, "auc-pre")]
        for index in range(20):
            auction_id = f"auc-det-{index:04d}"
            body = served_bid(api, auction_id)
            trace.append(body)
            variant = str(policy_action(body)["pitch_variant"])
            trace.append(push(api, auction_id, delta=0.05 if variant == FAVOURED else -0.05))
        trace.append(served_bid(api, "auc-post"))
        return json.dumps(trace, sort_keys=True)

    assert run() == run()


# =============================================================================================
# 5. R17's boundary: a store never learns from another store's discount data
# =============================================================================================


#: A cross-store prior in the shape `store_agent.learning.to_context_priors` renders and the
#: store context already carries. `value_props` is the pitch evidence R17 permits to pool; the
#: two discount keys are the evidence it forbids, spelled the way a platform bug would spell them.
RIVAL_DISCOUNT_FIELDS = {"rival_discount_depth": 0.2, "winning_discount_pct": 17.5}
PITCH_PRIOR = {
    "depth_buckets": [0.0, 0.05, 0.1, 0.15, 0.2],
    "observations": 40,
    "wins": 22,
    "stores": 6,
    "value_props": [{"label": "fast_dispatch", "wins": 9, "observations": 12}],
}


def taught_bid(prior: dict[str, Any], auction_id: str = "auc-r17") -> dict[str, Any]:
    """A served bid from a store that HAS a record, initialised from ``prior``.

    Taught first, deliberately. A cold store is answered under the policy its context already
    stated, so a cold bid would be byte-identical whatever the prior said and the comparison
    below would measure nothing — which is exactly how the first draft of this assertion passed
    against a tree where the prior reached the bid through no path at all.
    """
    api = client(network_priors={CLUSTER: prior})
    teach(api, favoured=FAVOURED, rounds=12)
    return served_bid(api, auction_id)


def test_the_network_prior_reaches_the_served_bid_at_all() -> None:
    """The control that arms the two assertions below: the prior's PITCH evidence does arrive.

    R17 says a store's policy is "initialized from network priors built from pitch/value-prop
    outcomes only". The path is real and this names it end to end: the store context's
    ``network_priors`` -> ``learning.from_context_priors`` -> ``initial_state`` ->
    ``to_learned_policy``'s ``value_prop`` -> ``ToolHooks.choose_policy_action`` -> the
    `policy_action` claim on the served bid. Without this the discount assertions would pass on a
    tree where nothing read the prior, and "no leak" would be indistinguishable from "no prior".
    """
    action = policy_action(taught_bid(dict(PITCH_PRIOR)))
    assert action.get("value_prop") == "fast_dispatch", (
        f"the network prior's pitch evidence reached the served bid through nothing: {action!r}"
    )


@pytest.mark.parametrize("field", sorted(RIVAL_DISCOUNT_FIELDS))
def test_a_rivals_discount_data_never_reaches_this_stores_served_bid(field: str) -> None:
    """R17: pitch outcomes pool across stores; discount elasticity never does.

    Each field is smuggled on its own, so a filter that happened to catch one spelling cannot
    stand in for the rule. The assertion is equality against the same bid served from a prior
    without it — not "the number is absent", which a bid that dropped its whole prior would also
    satisfy.
    """
    smuggled = {**PITCH_PRIOR, field: RIVAL_DISCOUNT_FIELDS[field]}
    served = taught_bid(smuggled)
    blob = json.dumps(served)
    assert field not in blob, (
        f"a rival's discount evidence reached this store's served bid ({field!r})"
    )
    assert str(RIVAL_DISCOUNT_FIELDS[field]) not in blob, (
        f"the rival's {field!r} value is in the served bid even though its key is not"
    )
    control = taught_bid(dict(PITCH_PRIOR))
    assert served == control, (
        f"the served bid is not byte-identical with and without the rival's {field!r}"
    )
    # The price is pinned by the equality above; what this adds is that it is inside the wall.
    # A taught store DOES discount — measured, 80.00 against a 100.00 list price — because its
    # own outcomes moved its own depth, which is R17 working rather than a leak. The envelope's
    # approved 20% cap is what bounds it, and no amount of network prior can widen that.
    price = served["offer"]["unit_price"]
    assert 80.0 <= price <= 100.0, (
        f"the served price {price} is outside the merchant's approved band; the envelope caps "
        f"the discount at 20% of a 100.00 list price"
    )


def test_the_depth_a_taught_store_plays_is_its_own_and_stays_inside_the_envelope() -> None:
    """The arming control for the price assertion above: the depth axis is LIVE through the door.

    Without this, "the price is between 80 and 100" would be satisfied by a loop that never moved
    the price at all, and the R17 equality above would be comparing two identical list-price bids.
    """
    cold = served_bid(client(), "auc-cold-price")
    assert cold["offer"]["unit_price"] == 100.0, (
        "a store with no record of its own must ask for no discount; a merchant's approved cap "
        f"is a wall, not a mandate to spend it: {cold['offer']}"
    )
    warm = taught_bid(dict(PITCH_PRIOR), auction_id="auc-warm-price")
    assert warm["offer"]["unit_price"] < cold["offer"]["unit_price"], (
        f"teaching moved no price: {warm['offer']['unit_price']} vs {cold['offer']['unit_price']}"
    )
    assert warm["offer"]["unit_price"] >= 80.0


def test_a_smuggled_discount_field_is_scrubbed_out_of_the_prior_hook_too() -> None:
    """The hook that publishes a prior filters it, not only the loop that reads one.

    ``ToolHooks.get_network_prior`` puts ``network_priors[cluster]`` into a provenance-tagged
    claim value. It has no caller on the bid path today — measured: a served bid's claims are
    ``list_price``, ``material``, ``in_stock``, ``units_left``, ``policy_action`` and nothing
    else — so a bid-level assertion could not grade it. This grades the hook directly, because
    the day something does call it, publishing a rival's elasticity would be a leak whether or
    not anyone remembered to re-check.
    """
    from store_agent.hooks import ToolHooks

    smuggled = {**PITCH_PRIOR, **RIVAL_DISCOUNT_FIELDS}
    hooks = ToolHooks(context(network_priors={CLUSTER: smuggled}))
    value = hooks.get_network_prior(CLUSTER).value
    assert isinstance(value, dict)
    assert value.get("value_props") == PITCH_PRIOR["value_props"], (
        f"the hook dropped the prior's pitch evidence as well: {value!r}"
    )
    for leak in RIVAL_DISCOUNT_FIELDS:
        assert leak not in value, f"the prior hook published a rival's {leak!r}: {value!r}"
    dropped = [c for c in hooks.call_log if c.hook == "get_network_prior"]
    assert dropped and "discount" in dropped[-1].detail, (
        f"a scrubbed prior must say so in the call log: {dropped!r}"
    )


def test_the_pitch_variant_vocabulary_and_the_pitch_kinds_agree() -> None:
    """``learning.arms.VARIANT_KIND`` names kinds ``runtime.pitch`` implements, and no others.

    The two are spelled independently — ``runtime.pitch`` imports ``learning.arms`` and not the
    other way round, because the reverse closes an import cycle — so this is what keeps a variant
    from promoting a kind no fact can ever have, which would render as a variant that silently
    does nothing.
    """
    from store_agent.learning import DEFAULT_PITCH_VARIANT, PITCH_VARIANTS, VARIANT_KIND
    from store_agent.runtime.pitch import KIND_BY_SOURCE, KIND_ORDER

    assert set(VARIANT_KIND.values()) <= set(KIND_ORDER), (
        f"a pitch variant promotes a kind the pitch does not implement: "
        f"{sorted(set(VARIANT_KIND.values()) - set(KIND_ORDER))}"
    )
    assert set(KIND_ORDER) == set(KIND_BY_SOURCE.values())
    assert set(VARIANT_KIND) <= set(PITCH_VARIANTS)
    assert DEFAULT_PITCH_VARIANT not in VARIANT_KIND, (
        "the neutral arm must promote nothing, or there is no neutral arm"
    )
    assert set(PITCH_VARIANTS) - set(VARIANT_KIND) == {DEFAULT_PITCH_VARIANT}, (
        "every non-neutral variant must promote a kind, or it is an arm that cannot do anything"
    )
