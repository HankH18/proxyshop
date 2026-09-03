"""T-041 — the advocate runtime: the deterministic cold bid, and the boundary it goes through.

The ticket's three acceptance criteria, and one finding, written as behaviour:

1. **The cold agent emits the exact deterministic default bid.** With no learned policy the
   answer is the catalog list price, the envelope's standing commitments, and nothing invented:
   the offer is goldened field for field, and two runs on identical inputs are byte-identical.
2. **Every claim traces to a hook.** The set difference between the claims reachable anywhere in
   the bid — the offer's `commitments` included — and what the facade recorded emitting is
   empty, by content fingerprint rather than by eyeball.
3. **The decline path produces a valid decline with a reason.** Four conditions, four named
   reasons, each a value the exchange can read and count.

**And finding T-152.** The runtime calls ``enforce_bid_provenance(bid, hooks)`` on the WHOLE
`Bid`, never on `bid.claims`. The difference is not stylistic and it is not checkable by reading
the call: `enforce_hook_provenance` inspects exactly the material it is handed, so a claim list
leaves the offer's `commitments`, its `discount` and the price it states outside the boundary —
which is how an unauthorised 25% once reached a bid whose every top-level claim was genuine.
`test_a_commitment_no_hook_emitted_is_refused_even_though_it_rides_on_the_offer` puts a smuggled
claim in the one place the claims-list form cannot see, and asserts in the same test that the
claims-list form would have admitted it. Reverting the call site to `bid.claims` turns it red;
nothing else in this file would notice.

Offline and clock-free by construction, like the code it grades: no network, no LLM, no
database, no wall clock, no unseeded randomness.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from contracts.signing import canonical_json
from store_agent.hooks import (
    Denied,
    HookInputError,
    ToolHooks,
    claim_fingerprint,
    enforce_hook_provenance,
    hosted_claim_construction_offenders,
    mint_claim,
)
from store_agent.runtime import (
    AGENT_VERSION,
    INTRO_DISCOUNT_KEY,
    OFFER_EXPIRES_AT_KEY,
    AuctionContext,
    Decline,
    DeclineReason,
    assemble_context,
    bid,
    is_decline,
    offer_id,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_SRC = REPO_ROOT / "packages" / "store-agent" / "src" / "runtime"
ENVELOPE_FIXTURE = REPO_ROOT / "fixtures" / "envelopes" / "store-alpha.approved.json"
BETA_FIXTURE = REPO_ROOT / "fixtures" / "envelopes" / "store-beta.approved.json"

CLUSTER = "cluster-warm-layers"
STORE_ID = "store-alpha"
LIST_PRICE = 100.0


# ---------------------------------------------------------------------------------------------
# Fixture data. The approved envelope is READ FROM THE FIXTURE rather than restated here: the
# walls this runtime bids inside are a merchant-approved artifact, and a test that retyped them
# would be grading the code against a copy nobody approved.
# ---------------------------------------------------------------------------------------------


def _fixture() -> dict[str, Any]:
    return json.loads(ENVELOPE_FIXTURE.read_text(encoding="utf-8"))


def _context(**overrides: Any) -> dict[str, Any]:
    fixture = _fixture()
    context: dict[str, Any] = {
        "store_id": STORE_ID,
        "envelope": fixture["envelope"],
        "catalog": fixture["catalog"],
        "live_state": {
            "prod-cap": {"in_stock": True, "units_left": 7},
            "prod-floor": {"in_stock": True, "units_left": 3},
        },
        "learned_policy": None,
        "network_priors": {CLUSTER: {"depth_buckets": [0.0, 0.05, 0.1, 0.15, 0.2]}},
    }
    context.update(overrides)
    return context


def _intent(**overrides: Any) -> dict[str, Any]:
    intent: dict[str, Any] = {
        "intent_id": "int-0001",
        "cluster_id": CLUSTER,
        "query": "a warm mid-layer for cold commutes",
        "category": "outerwear",
        "hard_constraints": [{"field": "material", "op": "eq", "value": "merino wool"}],
        "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
        "ship_to": "US-CA",
        "currency": "USD",
        "budget_band": "50-150",
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "1",
    }
    intent.update(overrides)
    return intent


def _request(auction_id: str = "auc-0001", **intent_overrides: Any) -> dict[str, Any]:
    return {
        "auction_id": auction_id,
        "intent": _intent(**intent_overrides),
        "profile": {
            "pseudonym": "pseu-0001",
            "buckets": {
                "budget_band": "50-150",
                "category_affinity": ["outerwear"],
                "frequency_tier": "occasional",
                "region": "US-W",
                "first_time": True,
            },
        },
        "respond_by": "2999-01-01T00:00:00Z",
    }


def _with_intro(depth: float) -> dict[str, Any]:
    """A context whose envelope defines a cold-start intro discount rule of `depth` percent."""
    context = _context()
    envelope = dict(context["envelope"])
    envelope[INTRO_DISCOUNT_KEY] = depth
    context["envelope"] = envelope
    return context


def _with_policy(depth: float, version: str = "policy-v2") -> dict[str, Any]:
    return _context(
        learned_policy={
            "version": version,
            "actions": {CLUSTER: {"discount_pct": depth, "commitment_keys": ["free_returns"]}},
        }
    )


def _canon(value: Any) -> str:
    """A stable canonical string for a bid or a decline, models and dataclasses alike."""
    return canonical_json(_plain(value))


def _plain(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json.loads(json.dumps(dataclasses.asdict(value), default=str))
    return value


def _claims_everywhere(answer: Any) -> list[Any]:
    """Every claim reachable in a bid — `bid.claims` AND the offer's `commitments`."""
    return [*answer.claims, *(answer.offer.commitments or [])]


def _bid(request: Any = None, context: Any = None, hooks: Any = None) -> Any:
    """A bid that must BE a bid: a decline here is a failure to report, never a silent skip."""
    answer = bid(request or _request(), context if context is not None else _context(), hooks=hooks)
    assert not is_decline(answer), f"expected a bid, got {answer}"
    return answer


# ---------------------------------------------------------------------------------------------
# 1. Acceptance 1 — the deterministic cold-start default bid
# ---------------------------------------------------------------------------------------------


def test_the_cold_bid_is_the_documented_default_offer() -> None:
    """R10: list price, the envelope's standing commitments, no discount, nothing improvised.

    The offer is goldened whole rather than sampled field by field. "The deterministic default
    bid" is a statement about the WHOLE document, and an assertion that checked three fields
    would be satisfied by a bid carrying a fourth one nobody decided on.
    """
    answer = _bid()

    assert answer.offer.model_dump(mode="json") == {
        "bid_offer_id": offer_id("auc-0001", STORE_ID, "prod-cap"),
        "product_ref": "prod-cap",
        "variant_ref": None,
        "unit_price": LIST_PRICE,
        "currency": "USD",
        "discount": None,
        "commitments": [
            {
                "claim_id": None,
                "key": "free_returns",
                "claim_type": "return_policy",
                "value": "30 days",
                "unit": None,
                "source_span": None,
                "provenance": {
                    "source": "owner_statement",
                    "ref": "envelope:store-alpha:v3#free_returns",
                    "observed_at": "2026-01-01T00:00:00Z",
                    "authority_rank": 1,
                },
            },
            {
                "claim_id": None,
                "key": "ships_within",
                "claim_type": "dispatch_window",
                "value": "2 business days",
                "unit": None,
                "source_span": None,
                "provenance": {
                    "source": "owner_statement",
                    "ref": "envelope:store-alpha:v3#ships_within",
                    "observed_at": "2026-01-01T00:00:00Z",
                    "authority_rank": 1,
                },
            },
        ],
        "total_price": LIST_PRICE,
        "expires_at": "2999-01-01T00:00:00Z",
        "checkout_url": None,
        "delivery_estimate_days": None,
    }
    assert answer.auction_id == "auc-0001"
    assert answer.store_id == STORE_ID
    assert answer.agent_version == AGENT_VERSION == "store-agent/0.0.0", (
        "the version stamp is read downstream as 'which advocate built this', so it is pinned "
        "here as a literal — asserting it equals the constant it came from grades nothing"
    )
    assert answer.message is None, "the hosted path asserts nothing in prose; claims are evidence"

    # EXACTLY these claims, in this order. `⊆ what the hooks emitted` is the traceability
    # property and it is checked elsewhere; on its own it is also satisfied by a bid carrying
    # NO evidence at all, so dropping every pixel-feed claim would pass a subset test.
    assert [(c.key, c.provenance.source.value) for c in answer.claims] == [
        ("list_price", "scraped"),
        ("material", "scraped"),
        ("in_stock", "pixel_feed"),
        ("units_left", "pixel_feed"),
        ("policy_action", "learned_policy"),
    ]


def test_the_cold_price_is_the_catalog_list_price_and_not_a_model_invention() -> None:
    """S4: the number in the offer is the number in the catalog, and it is traceable to it."""
    context = _context()
    answer = _bid(context=context)

    catalog_price = float(context["catalog"][answer.offer.product_ref]["list_price"])
    assert answer.offer.unit_price == catalog_price
    assert answer.offer.total_price == catalog_price

    priced_by = [c for c in answer.claims if c.key == "list_price"]
    assert [c.value for c in priced_by] == [catalog_price], (
        "the price the offer states must itself be hook-emitted evidence, not a bare float"
    )
    assert priced_by[0].provenance.source.value == "scraped"
    assert (
        priced_by[0].provenance.ref == f"catalog:{STORE_ID}:{answer.offer.product_ref}#list_price"
    )


def test_the_cold_bid_carries_exactly_the_envelopes_standing_commitments() -> None:
    context = _context()
    answer = _bid(context=context)

    assert [c.key for c in (answer.offer.commitments or [])] == [
        str(c["key"]) for c in context["envelope"]["standing_commitments"]
    ]
    assert all(
        c.provenance.source.value == "owner_statement" for c in (answer.offer.commitments or [])
    )
    assert not any(c.key in {"free_returns", "ships_within"} for c in answer.claims), (
        "a commitment belongs to the offer; duplicating it into `claims` would present one "
        "hook emission twice and make the audit trail lie about how many there were"
    )


def test_the_cold_bid_records_the_policy_version_it_bid_under() -> None:
    """R10: 'no learned policy' is a decision, and it is evidence like any other."""
    answer = _bid()
    actions = [c for c in answer.claims if c.key == "policy_action"]
    assert len(actions) == 1, "exactly one policy decision per bid"
    assert actions[0].value["policy_version"] == "cold-start"
    assert actions[0].value["discount_pct"] == 0.0
    assert actions[0].provenance.source.value == "learned_policy"


# ---------------------------------------------------------------------------------------------
# 2. Determinism — the property most likely to break silently (S4)
# ---------------------------------------------------------------------------------------------


def test_two_cold_bids_on_identical_inputs_are_byte_identical() -> None:
    first = _bid(_request(), _context(), hooks=ToolHooks(_context()))
    second = _bid(_request(), _context(), hooks=ToolHooks(_context()))
    assert _canon(first) == _canon(second)


def test_a_bid_is_byte_identical_across_many_runs_and_a_reused_facade() -> None:
    """Two ways the same inputs are re-presented: fresh facades, and one reused facade.

    The reused facade is the case the T-040 constraint warns about. `start_bid()` is called
    between auctions, which is what the contract requires of a caller that keeps a facade; the
    assertion is that doing so gives back the identical bid rather than a differently-numbered
    one, so "one facade per auction" and "start_bid between auctions" are genuinely equivalent.
    """
    fresh = {_canon(_bid(_request(), _context(), hooks=ToolHooks(_context()))) for _ in range(25)}
    assert len(fresh) == 1, f"{len(fresh)} distinct bids from identical inputs: {sorted(fresh)}"

    reused = ToolHooks(_context())
    reused_bids = []
    for _ in range(5):
        reused.start_bid("auc-0001")
        reused_bids.append(_canon(_bid(_request(), _context(), hooks=reused)))
    assert set(reused_bids) == fresh, "a reused facade must produce the same bid as a fresh one"


def test_catalog_and_live_state_ordering_do_not_reach_the_bid() -> None:
    """A bid that depended on dict insertion order would be reproducible only by accident."""
    straight = _context()
    reversed_context = _context()
    reversed_context["catalog"] = dict(reversed(list(straight["catalog"].items())))
    reversed_context["live_state"] = dict(reversed(list(straight["live_state"].items())))

    assert _canon(_bid(context=straight)) == _canon(_bid(context=reversed_context))


def test_the_runtime_reads_no_clock_and_no_randomness() -> None:
    """The source, not the behaviour: a clock read once a day is still a clock.

    Behavioural determinism tests sample; this reads every module on the bid path and refuses
    the imports and the calls that end byte-identical reproduction. `observed_at` comes from the
    evidence or from `UNKNOWN_OBSERVED_AT`, and both live inside the hooks.
    """
    banned_modules = {
        "calendar",
        "datetime",
        "os",
        "random",
        "secrets",
        "socket",
        "time",
        "uuid",
    }
    banned_calls = {
        # `hash` and `id` need no import and are the two ways to get PYTHONHASHSEED-dependent
        # or address-dependent behaviour into a sort key without tripping the import scan.
        "hash",
        "id",
        "choice",
        "getenv",
        "monotonic",
        "now",
        "perf_counter",
        "randint",
        "random",
        "sample",
        "shuffle",
        "time",
        "today",
        "utcnow",
        "uuid1",
        "uuid4",
    }

    sources = sorted(RUNTIME_SRC.rglob("*.py"))
    assert sources, f"no runtime source found under {RUNTIME_SRC}"

    offences: list[str] = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in banned_modules:
                        offences.append(f"{path.name}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in banned_modules:
                    offences.append(f"{path.name}:{node.lineno}: from {node.module} import ...")
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in banned_calls:
                    offences.append(f"{path.name}:{node.lineno}: {name}(...)")
    assert not offences, "the bid path must read no clock and no RNG: " + "; ".join(offences)


def test_the_runtime_never_builds_a_claim_or_a_provenance_itself() -> None:
    """R8's static half, scoped to this lane: a fact enters through a hook or not at all."""
    offenders = [
        offence
        for offence in hosted_claim_construction_offenders(RUNTIME_SRC.parent)
        if offence.path.startswith("runtime/")
    ]
    assert not offenders, f"the runtime constructed protocol objects directly: {offenders}"


# ---------------------------------------------------------------------------------------------
# 3. Acceptance 2 — every claim in the bid traces to a hook call (S5/R8)
# ---------------------------------------------------------------------------------------------


def test_every_claim_anywhere_in_the_bid_traces_to_a_hook_emission() -> None:
    """By content fingerprint, and over the offer's commitments as well as `bid.claims`."""
    hooks = ToolHooks(_context())
    answer = _bid(hooks=hooks)

    in_bid = {claim_fingerprint(c) for c in _claims_everywhere(answer)}
    emitted = {claim_fingerprint(c) for c in hooks.emitted_claims}
    assert in_bid, "a bid with no claims makes this criterion vacuous"
    assert not in_bid - emitted, "these bid claims came from no hook call"

    assert hooks.call_log, "the facade must have recorded the calls the runtime made"
    assert {call.hook for call in hooks.call_log} == {
        "get_product_fact",
        "get_live_state",
        "get_owner_commitments",
        "choose_policy_action",
    }, "the cold path asks for facts, live state, commitments and the policy — and nothing else"


class _MuteFacade(ToolHooks):
    """A facade that knows no product fact at all, whatever the catalog says."""

    def get_product_fact(self, product_ref: str, key: str) -> Any:
        raise HookInputError(f"nothing known about {product_ref!r}")


def test_the_runtime_asks_the_hooks_before_it_asks_the_catalog() -> None:
    """A facade that answers nothing must produce no bid, not a bid assembled around it.

    The negative control for the test above. The context handed to `bid` carries a full,
    perfectly good catalog; only the FACADE is mute. If the runtime read that catalog directly
    for anything that reaches the offer, this would still come back priced.
    """
    context = _context()
    answer = bid(_request(), context, hooks=_MuteFacade(context))
    assert is_decline(answer), f"a facade that knows no product cannot yield a bid: {answer}"
    assert answer.reason == DeclineReason.no_priced_product


# ---------------------------------------------------------------------------------------------
# 4. T-152 — the boundary call takes the WHOLE bid, and it is load-bearing
# ---------------------------------------------------------------------------------------------


class _SmugglingFacade(ToolHooks):
    """A facade that adds one claim the ledger never recorded to its commitments.

    Minted rather than hand-built, so it carries a legitimate-looking `owner_statement`
    provenance and a real envelope `ref`: a guard that inspected `provenance.source` would wave
    it through. What it does not have is an emission in the ledger, because `mint_claim` is not
    `_emit`. It rides on the OFFER — commitments are offer fields — which is precisely the place
    a boundary handed `bid.claims` cannot see.
    """

    def get_owner_commitments(self, cluster_id: str) -> Any:
        genuine = super().get_owner_commitments(cluster_id)
        smuggled = mint_claim(
            key="free_shipping",
            value="always",
            source="owner_statement",
            ref=f"envelope:{STORE_ID}:v3#free_shipping",
            observed_at="2026-01-01T00:00:00Z",
            authority_rank=1,
        )
        return [*genuine, smuggled]


def test_a_commitment_no_hook_emitted_is_refused_even_though_it_rides_on_the_offer() -> None:
    """T-152: the whole `Bid` goes through the boundary, never `bid.claims`.

    Two assertions, and the second is what makes the first mean something:

    * the runtime declines, because `enforce_bid_provenance` walks the offer's commitments;
    * the claims-list form of the same check ADMITS the same smuggled claim, so the refusal
      above is a property of *what the call site passes*, not of the claim being obviously bad.

    Change the call site to `enforce_hook_provenance(bid.claims, hooks)` and this test — and
    only this test — goes red.
    """
    context = _context()
    hooks = _SmugglingFacade(context)
    answer = bid(_request(), context, hooks=hooks)

    assert is_decline(answer), (
        f"a bid carrying an un-emitted commitment must not be emitted: {answer}"
    )
    assert answer.reason == DeclineReason.provenance_refused
    assert "free_shipping" in answer.detail, (
        f"the decline must name the claim the boundary refused: {answer.detail!r}"
    )

    # The same claim, checked the way the finding says NOT to check it: the boundary is handed a
    # bid's `claims` list, the smuggled commitment is not in it, and nothing is refused.
    honest = ToolHooks(context)
    honest_bid = _bid(hooks=honest)
    admitted = enforce_hook_provenance(list(honest_bid.claims), honest, product_ref="prod-cap")
    assert len(admitted) == len(honest_bid.claims), (
        "the claims-list form inspects exactly the claims it is given — which is why the "
        "commitments and the discount hanging off the Offer must not be checked that way"
    )
    assert not any(c.key == "free_shipping" for c in honest_bid.claims), (
        "commitments never appear in `bid.claims`, so a claims-list boundary could never have "
        "seen the smuggled one at all"
    )


def test_the_boundary_refusal_becomes_a_decline_rather_than_a_traceback() -> None:
    """A hosted agent that cannot prove its own bid answers, it does not crash the solicitation."""
    answer = bid(_request(), _context(), hooks=_SmugglingFacade(_context()))
    assert isinstance(answer, Decline)
    assert answer.auction_id == "auc-0001"
    assert answer.store_id == STORE_ID
    assert answer.detail, "a provenance refusal must carry the boundary's own explanation"


# ---------------------------------------------------------------------------------------------
# 5. The discount enters through `authorize_discount` or not at all (R8)
# ---------------------------------------------------------------------------------------------


def test_an_intro_rule_inside_the_walls_prices_the_offer_and_carries_its_grant() -> None:
    """The envelope's cold-start intro rule, granted: 15% off 100.00 clears cap and floor."""
    hooks = ToolHooks(_with_intro(15.0))
    answer = _bid(context=_with_intro(15.0), hooks=hooks)

    assert answer.offer.unit_price == 85.0
    assert answer.offer.total_price == 85.0
    assert answer.offer.discount is not None
    assert (answer.offer.discount.type, answer.offer.discount.value) == ("percentage", 15.0)
    assert answer.offer.discount.provenance is None, (
        "a Discount is not a Claim: it has no key and therefore no ledger identity, and a "
        "provenance on it would read as a claim no hook emitted"
    )

    grants = [c for c in answer.claims if c.key == "authorized_discount_pct"]
    assert [c.value for c in grants] == [15.0], "the grant that backs the discount rides in the bid"
    assert grants[0].provenance.ref.endswith("@prod-cap"), (
        "an authorization is scoped to the product whose floor was actually checked"
    )
    assert claim_fingerprint(grants[0]) in hooks.spent_fingerprints, (
        "the boundary spends the grant, so it cannot furnish a second offer's discount"
    )


def test_an_intro_rule_the_envelope_refuses_falls_back_to_list_price() -> None:
    """`Denied` is a value to look at. 25% is past the approved 20% cap, so the bid goes out at 100."""
    hooks = ToolHooks(_with_intro(25.0))
    answer = _bid(context=_with_intro(25.0), hooks=hooks)

    assert answer.offer.unit_price == LIST_PRICE
    assert answer.offer.discount is None
    assert not [c for c in answer.claims if c.key == "authorized_discount_pct"]

    denials = [call for call in hooks.call_log if call.hook == "authorize_discount"]
    assert [call.outcome for call in denials] == ["over_max_discount_pct"], (
        "the refusal must be recorded, not swallowed: the call log is the audit trail"
    )
    assert isinstance(hooks.authorize_discount("prod-cap", 25.0), Denied), (
        "the fixture's own expectation: 25% is denied for prod-cap by the cap"
    )


def test_a_learned_policy_depth_reaches_the_price_only_through_the_envelope() -> None:
    inside = _bid(context=_with_policy(10.0))
    assert inside.offer.unit_price == 90.0
    assert inside.offer.discount is not None and inside.offer.discount.value == 10.0
    action = next(c for c in inside.claims if c.key == "policy_action")
    assert action.value["policy_version"] == "policy-v2"
    assert action.value["discount_pct"] == 10.0

    walled = _bid(context=_with_policy(25.0))
    assert walled.offer.unit_price == LIST_PRICE, (
        "a policy that learned to want 25% against a 20% envelope bids at list price"
    )
    assert walled.offer.discount is None
    walled_action = next(c for c in walled.claims if c.key == "policy_action")
    assert walled_action.value["discount_pct"] == 0.0


def test_the_price_floor_and_not_only_the_cap_bounds_the_price() -> None:
    """prod-floor's 95.00 floor refuses a 10% depth that is only half the discount cap."""
    context = _with_intro(10.0)
    context["catalog"] = {"prod-floor": _fixture()["catalog"]["prod-floor"]}
    context["live_state"] = {"prod-floor": {"in_stock": True}}
    answer = _bid(
        _request(hard_constraints=[{"field": "material", "op": "eq", "value": "alpaca"}]),
        context,
    )
    assert answer.offer.product_ref == "prod-floor"
    assert answer.offer.unit_price == LIST_PRICE, "90.00 is under the 95.00 floor, so no discount"
    assert answer.offer.discount is None


# ---------------------------------------------------------------------------------------------
# 5b. The bid has to survive the door it is bid through
# ---------------------------------------------------------------------------------------------


SNAPSHOT = {STORE_ID: {"store_id": STORE_ID, "score": 0.7, "blacklisted": False}}


@pytest.mark.parametrize(
    "context_of", [_context, lambda: _with_intro(15.0), lambda: _with_policy(10.0)]
)
def test_the_bid_is_admissible_at_the_exchanges_own_hosted_door(context_of: Any) -> None:
    """The gate this runtime's output is actually judged by, run against it.

    A bid this module considers finished is not finished if `contracts.boundary.validate_bid`
    refuses it, and the two live in different packages, so nothing else would have noticed. It
    already caught one: the offer stated no `expires_at`, and the boundary refuses that outright
    ("an offer with no stated expiry is an offer nobody can price the risk of") — every bid this
    runtime made would have been turned away at the door.
    """
    from contracts.boundary import validate_bid

    result = validate_bid(
        _bid(context=context_of()),
        path="hosted",
        trust_snapshot=SNAPSHOT,
        now="2026-01-02T00:00:00Z",
    )
    assert result.ok, (
        f"the exchange refused a bid this runtime considered finished: {result.reasons}"
    )
    assert list(result.reasons) == []


def test_the_offer_states_an_expiry_taken_from_the_context_never_from_a_clock() -> None:
    stated = _context()
    stated[OFFER_EXPIRES_AT_KEY] = "2026-06-01T00:00:00Z"
    assert _bid(context=stated).offer.expires_at == "2026-06-01T00:00:00Z"

    # With none stated, the floor: the offer stands at least as long as the auction it answers.
    assert _bid().offer.expires_at == _request()["respond_by"]


# ---------------------------------------------------------------------------------------------
# 5c. A second approved store, so the runtime is graded on data and not on one fixture
# ---------------------------------------------------------------------------------------------


def _beta_context(**overrides: Any) -> dict[str, Any]:
    """store-beta: a store-wide 40.00 floor plus a tighter 48.00 one on a single product."""
    fixture = json.loads(BETA_FIXTURE.read_text(encoding="utf-8"))
    context: dict[str, Any] = {
        "store_id": "store-beta",
        "envelope": fixture["envelope"],
        "catalog": fixture["catalog"],
        "live_state": {"prod-open": {"in_stock": True}, "prod-tight": {"in_stock": True}},
        "learned_policy": None,
        "network_priors": {},
    }
    context.update(overrides)
    return context


def _beta_request() -> dict[str, Any]:
    """No hard constraints: this store's fixture is about floors, not about eligibility."""
    return _request(hard_constraints=[])


def test_a_second_approved_store_bids_its_own_envelope() -> None:
    answer = _bid(_beta_request(), _beta_context())
    assert (answer.store_id, answer.offer.product_ref, answer.offer.unit_price) == (
        "store-beta",
        "prod-tight",
        60.0,
    )
    assert [c.key for c in (answer.offer.commitments or [])] == ["warranty"]


@pytest.mark.parametrize(
    ("depth", "expected_price", "discounted"),
    [
        # 25% off 60.00 is 45.00, under prod-tight's own 48.00 floor even though 25% is only
        # half this envelope's 50% cap — the floor refuses it on its own.
        (25.0, 60.0, False),
        # 20% lands exactly ON the floor. An approved floor is a limit to reach, not to stay under.
        (20.0, 48.0, True),
    ],
)
def test_the_stricter_of_the_store_wide_and_per_product_floors_binds(
    depth: float, expected_price: float, discounted: bool
) -> None:
    context = _beta_context()
    context["envelope"] = dict(context["envelope"], **{INTRO_DISCOUNT_KEY: depth})
    context["catalog"] = {"prod-tight": context["catalog"]["prod-tight"]}
    context["live_state"] = {"prod-tight": {"in_stock": True}}

    answer = _bid(_beta_request(), context)
    assert answer.offer.unit_price == expected_price
    assert (answer.offer.discount is not None) is discounted


# ---------------------------------------------------------------------------------------------
# 6. Product selection — deterministic, and constrained by R19
# ---------------------------------------------------------------------------------------------


def test_the_advocate_offers_the_cheapest_product_that_satisfies_the_hard_constraints() -> None:
    context = _context()
    catalog = dict(context["catalog"])
    catalog["prod-cheap"] = {
        "product_ref": "prod-cheap",
        "list_price": 60.0,
        "material": "merino wool",
    }
    context["catalog"] = catalog
    context["live_state"] = dict(context["live_state"], **{"prod-cheap": {"in_stock": True}})

    answer = _bid(context=context)
    assert answer.offer.product_ref == "prod-cheap"
    assert answer.offer.unit_price == 60.0


def test_products_at_one_price_are_broken_by_product_ref_not_by_dict_order() -> None:
    context = _context()
    twin = dict(_fixture()["catalog"]["prod-cap"], product_ref="prod-cap-2")
    context["catalog"] = {"prod-cap-2": twin, "prod-cap": _fixture()["catalog"]["prod-cap"]}
    context["live_state"] = {"prod-cap": {"in_stock": True}, "prod-cap-2": {"in_stock": True}}

    answer = _bid(context=context)
    assert answer.offer.product_ref == "prod-cap", (
        "with the twin listed FIRST in the catalog, the lexicographic tie-break must still win"
    )


def test_a_product_the_catalog_cannot_price_is_never_offered() -> None:
    context = _context()
    context["catalog"] = {
        "prod-cap": {
            k: v for k, v in _fixture()["catalog"]["prod-cap"].items() if k != "list_price"
        }
    }
    answer = bid(_request(), context)
    assert is_decline(answer) and answer.reason == DeclineReason.no_priced_product


@pytest.mark.parametrize(
    ("op", "wanted", "expected"),
    [
        ("eq", "merino wool", "prod-cap"),
        ("eq", "MERINO WOOL", "prod-cap"),
        ("eq", "alpaca", "prod-floor"),
        ("in", ["alpaca", "cashmere"], "prod-floor"),
        ("contains", "merino", "prod-cap"),
    ],
)
def test_the_hard_constraint_operators_filter_the_catalog(
    op: str, wanted: Any, expected: str
) -> None:
    """R19: a hard constraint is an eligibility filter, and it is applied to hook evidence."""
    answer = _bid(_request(hard_constraints=[{"field": "material", "op": op, "value": wanted}]))
    assert answer.offer.product_ref == expected


def test_a_constraint_operator_the_runtime_does_not_implement_disqualifies_rather_than_passes() -> (
    None
):
    """An unknown filter must mean 'this product does not qualify', never 'no filter applied'."""
    answer = bid(
        _request(hard_constraints=[{"field": "material", "op": "matches_regex", "value": ".*"}]),
        _context(),
    )
    assert is_decline(answer) and answer.reason == DeclineReason.no_matching_product


def test_a_constraint_on_an_attribute_the_catalog_lacks_disqualifies_the_product() -> None:
    """R19/R18: unverified catalog data cannot satisfy a hard constraint, and silence is not data."""
    answer = bid(
        _request(hard_constraints=[{"field": "waterproof", "op": "eq", "value": True}]),
        _context(),
    )
    assert is_decline(answer) and answer.reason == DeclineReason.no_matching_product
    assert "waterproof" in answer.detail


# ---------------------------------------------------------------------------------------------
# 6b. What an independent adversarial review found. Each of these crashed or was ungraded.
# ---------------------------------------------------------------------------------------------


def test_the_offer_id_is_injective_and_not_a_joinable_string() -> None:
    """`app.offers.offer_id` is a PRIMARY KEY, and colons are legal in all three components.

    `f"{auction}:{store}:{product}"` renders store `a` offering `b:c` and store `a:b` offering
    `c` identically — two stores in one auction, one row.
    """
    assert offer_id("auc", "a", "b:c") != offer_id("auc", "a:b", "c")
    assert offer_id("auc", "a", "b") == offer_id("auc", "a", "b"), "and still deterministic"
    assert _bid().offer.bid_offer_id.startswith("bidoffer:")


def test_a_store_whose_id_cannot_be_cited_bids_at_list_price_instead_of_crashing() -> None:
    """`scoped_ref` refuses to mint a grant whose rule half contains the scope separator.

    A `store_id` of `store@alpha` is the real case. The grant could never be matched back to
    its product, so there is no discount — but that is a fail-closed answer, not a reason to
    throw `ValueError` into the solicitation.
    """
    context = _with_intro(15.0)
    context["store_id"] = "store@alpha"
    context["envelope"] = dict(context["envelope"], store_id="store@alpha")

    answer = _bid(context=context)
    assert answer.store_id == "store@alpha"
    assert answer.offer.unit_price == LIST_PRICE, "no citable grant means no discount"
    assert answer.offer.discount is None


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        # A request or a context that does not identify itself. The models forbid the empty
        # string, so the alternative to a decline here is a ValidationError thrown at the caller.
        (lambda r, c: r.__setitem__("auction_id", ""), DeclineReason.unidentified_request),
        # The envelope names the store too, so blanking only `store_id` is NOT unidentified —
        # both have to be missing before the bid has no one to answer for.
        (
            lambda r, c: (
                c.__setitem__("store_id", ""),
                c.__setitem__("envelope", dict(c["envelope"], store_id="")),
            ),
            DeclineReason.unidentified_request,
        ),
        # A store context that is not the shape it claims to be.
        (
            lambda r, c: c.__setitem__("catalog", {"prod-cap": "not a mapping"}),
            DeclineReason.unusable_store_context,
        ),
        (
            lambda r, c: c.__setitem__("live_state", {"prod-cap": "not a mapping"}),
            DeclineReason.unusable_store_context,
        ),
        (
            lambda r, c: c.__setitem__(
                "envelope",
                dict(c["envelope"], floors=[{"product_ref": "prod-cap", "min_price": "cheap"}]),
            ),
            DeclineReason.unusable_store_context,
        ),
        (
            lambda r, c: c.__setitem__(
                "envelope", dict(c["envelope"], max_discount_pct="lots", intro_discount_pct=5.0)
            ),
            DeclineReason.unusable_store_context,
        ),
        (
            lambda r, c: c.__setitem__(
                "envelope", dict(c["envelope"], standing_commitments=[{"value": "no key here"}])
            ),
            DeclineReason.unusable_store_context,
        ),
        (
            lambda r, c: c.__setitem__(
                "catalog", {"prod-cap": dict(c["catalog"]["prod-cap"], list_price=float("nan"))}
            ),
            DeclineReason.unusable_store_context,
        ),
        (
            lambda r, c: r.__setitem__("intent", dict(r["intent"], hard_constraints=["nope"])),
            DeclineReason.unusable_store_context,
        ),
        # An expiry nobody can read is the same problem as no expiry at all.
        (
            lambda r, c: c.__setitem__(OFFER_EXPIRES_AT_KEY, "whenever"),
            DeclineReason.unstatable_offer_expiry,
        ),
        (lambda r, c: r.__setitem__("respond_by", ""), DeclineReason.unstatable_offer_expiry),
    ],
)
def test_malformed_input_is_answered_with_a_decline_and_never_raised(
    mutate: Any, expected: DeclineReason
) -> None:
    """The exchange asked a question; every one of these used to answer with a traceback.

    A crash mid-solicitation is strictly worse than a decline that names the problem: the
    merchant service owns the shape of the store context, and the advocate is not entitled to
    take the caller down over it.
    """
    request, context = _request(), _context()
    mutate(request, context)

    answer = bid(request, context)
    assert is_decline(answer), f"expected a decline, got {answer}"
    assert answer.reason == expected
    assert answer.detail, "a decline must say what was wrong with the input"


def test_a_catalog_key_that_names_nothing_is_not_offered() -> None:
    context = _context()
    context["catalog"] = {"": dict(_fixture()["catalog"]["prod-cap"])}
    answer = bid(_request(hard_constraints=[]), context)
    assert is_decline(answer) and answer.reason == DeclineReason.no_priced_product


def test_a_negative_catalog_price_is_not_offered() -> None:
    context = _context()
    context["catalog"] = {"prod-cap": dict(_fixture()["catalog"]["prod-cap"], list_price=-5.0)}
    answer = bid(_request(), context)
    assert is_decline(answer) and answer.reason == DeclineReason.no_priced_product


def test_the_price_arithmetic_is_the_hooks_spelling_to_the_last_bit() -> None:
    """`list * (100 - pct) / 100`, not `list * (1 - pct/100)`. They are not the same float.

    The boundary re-checks the stated price against the floor that `authorize_discount`'s own
    arithmetic cleared, so a bid a bit-width away from it is an honest bid the guard refuses.
    """
    assert 19.99 * (100.0 - 19.0) / 100.0 != 19.99 * (1.0 - 19.0 / 100.0), (
        "this fixture only discriminates if the two spellings actually differ here"
    )
    context = _with_intro(19.0)
    context["catalog"] = {"prod-odd": {"product_ref": "prod-odd", "list_price": 19.99}}
    context["live_state"] = {"prod-odd": {"in_stock": True}}

    answer = _bid(_request(hard_constraints=[]), context)
    assert answer.offer.unit_price == 19.99 * (100.0 - 19.0) / 100.0
    assert answer.offer.unit_price != 19.99 * (1.0 - 19.0 / 100.0)


def test_the_stores_own_currency_outranks_the_one_the_buyer_asked_in() -> None:
    """A price is the store's assertion, so the currency it is asserted in is the store's."""
    context = _context(currency="GBP")
    answer = _bid(_request(currency="USD"), context)
    assert answer.offer.currency == "GBP"
    assert _bid(_request(currency="USD"), _context()).offer.currency == "USD", (
        "and with no store currency, the one that was asked for"
    )


def test_the_hook_call_log_reads_the_same_on_two_runs() -> None:
    """S5's audit trail is an ORDERED record, and catalog dict order must not reach it.

    The bid itself is protected by the price/product_ref tie-break, so a runtime that walked
    the catalog in insertion order would still emit an identical bid — and an identical bid is
    all the other determinism tests look at. The call log is where that difference shows.
    """

    def log_of(context: dict[str, Any]) -> list[tuple[str, str]]:
        hooks = ToolHooks(context)
        _bid(context=context, hooks=hooks)
        return [(call.hook, call.subject) for call in hooks.call_log]

    straight = _context()
    reversed_context = _context()
    reversed_context["catalog"] = dict(reversed(list(straight["catalog"].items())))

    assert log_of(straight) == log_of(reversed_context)
    assert log_of(straight) == [
        ("get_product_fact", "prod-cap"),
        ("get_product_fact", "prod-cap"),
        ("get_live_state", "prod-cap"),
        ("get_product_fact", "prod-floor"),
        ("get_product_fact", "prod-floor"),
        ("get_owner_commitments", CLUSTER),
        ("choose_policy_action", CLUSTER),
    ]


def test_the_respond_by_expiry_floor_is_a_known_limitation_and_is_written_down() -> None:
    """The fallback expiry is the auction's own deadline, so the offer dies when it closes.

    Asserted rather than left implicit, because it is the one place this runtime's output has a
    lifetime shorter than the flow that consumes it: a store that wants its offers to outlive
    the auction must state `offer_expires_at`. Stating it removes the limitation entirely, and
    that is asserted too, so this test fails the day someone "fixes" the fallback silently.
    """
    from contracts.boundary import validate_bid

    at_close = _bid().offer.expires_at
    assert at_close == _request()["respond_by"]
    refused = validate_bid(_bid(), path="hosted", trust_snapshot=SNAPSHOT, now=at_close)
    assert list(refused.reasons) == ["offer_expired"], (
        "documented: at the instant the auction closes, the fallback expiry has lapsed"
    )

    stated = _context()
    stated[OFFER_EXPIRES_AT_KEY] = "2999-06-01T00:00:00Z"
    ok = validate_bid(_bid(context=stated), path="hosted", trust_snapshot=SNAPSHOT, now=at_close)
    assert ok.ok and list(ok.reasons) == [], "a stated expiry removes the limitation"


# ---------------------------------------------------------------------------------------------
# 7. Acceptance 3 — the decline path
# ---------------------------------------------------------------------------------------------


def test_a_cluster_the_envelope_does_not_pursue_is_declined() -> None:
    answer = bid(_request(cluster_id="cluster-espresso"), _context())
    assert is_decline(answer)
    assert answer.reason == DeclineReason.cluster_not_pursued
    assert "cluster-espresso" in answer.detail


def test_an_envelope_that_pursues_nothing_authorizes_nothing() -> None:
    """Fail-closed: an empty `pursue_clusters` is the narrowest envelope, not the widest."""
    context = _context()
    context["envelope"] = dict(context["envelope"], pursue_clusters=[])
    answer = bid(_request(), context)
    assert is_decline(answer) and answer.reason == DeclineReason.cluster_not_pursued


def test_nothing_matching_the_intent_is_declined_rather_than_substituted() -> None:
    answer = bid(
        _request(hard_constraints=[{"field": "material", "op": "eq", "value": "cashmere"}]),
        _context(),
    )
    assert is_decline(answer)
    assert answer.reason == DeclineReason.no_matching_product
    assert "prod-cap" in answer.detail and "prod-floor" in answer.detail, (
        "a decline must account for every product it considered, not just report a count"
    )


def test_a_product_the_pixel_reports_out_of_stock_is_not_bid() -> None:
    context = _context()
    context["live_state"] = {"prod-cap": {"in_stock": False}, "prod-floor": {"in_stock": True}}
    answer = bid(_request(), context)
    assert is_decline(answer)
    assert answer.reason == DeclineReason.no_available_product


def test_silence_from_the_pixel_is_not_evidence_of_being_out_of_stock() -> None:
    """The feed is lossy by documented design: 'nothing said' and 'not in stock' differ."""
    context = _context()
    context["live_state"] = {}
    answer = _bid(context=context)
    assert answer.offer.product_ref == "prod-cap"
    assert not [c for c in answer.claims if c.key == "in_stock"]


def test_a_decline_is_a_complete_json_serializable_answer() -> None:
    answer = bid(_request("auc-0042", cluster_id="cluster-espresso"), _context())
    assert isinstance(answer, Decline)
    payload = dataclasses.asdict(answer)
    assert json.loads(json.dumps(payload)) == {
        "auction_id": "auc-0042",
        "store_id": STORE_ID,
        "reason": "cluster_not_pursued",
        "detail": payload["detail"],
        "agent_version": AGENT_VERSION,
        "schema_version": "1.0.0",
    }
    assert payload["detail"], "a decline without a reason detail explains nothing"
    assert DeclineReason(payload["reason"]) is answer.reason


def test_declining_emits_no_bid_shaped_answer_by_accident() -> None:
    """`is_decline` is the test callers make; it must not answer True for a bid or for None."""
    assert is_decline(bid(_request(cluster_id="nope"), _context()))
    assert not is_decline(_bid())
    assert not is_decline(None)


# ---------------------------------------------------------------------------------------------
# 8. Context assembly and the prompt-cache layout
# ---------------------------------------------------------------------------------------------


def test_the_context_is_assembled_from_models_as_readily_as_from_dicts() -> None:
    """The frozen suite hands the runtime dicts; the wire hands it models. One answer either way."""
    from contracts import BidRequest

    context = _context()
    as_dicts = _bid(_request(), context)
    as_model = _bid(BidRequest.model_validate(_request()), context)
    assert _canon(as_dicts) == _canon(as_model)


def test_the_cache_layout_puts_the_stable_context_in_front() -> None:
    """DESIGN: static-context-first, because a cache prefix survives only what does not move."""
    layout = assemble_context(_request(), _context()).cache_layout()
    tiers = [tier for tier, _name, _block in layout]
    assert tiers == sorted(tiers, key=["store", "session", "request"].index)
    assert [name for _tier, name, _block in layout if _tier == "request"] == [
        "auction_id",
        "intent",
        "profile",
    ]


def test_the_cache_prefix_survives_a_new_request_and_a_reordered_catalog() -> None:
    context = _context()
    reordered = _context()
    reordered["catalog"] = dict(reversed(list(context["catalog"].items())))

    first = assemble_context(_request("auc-0001"), context).cache_layout()
    same_store_new_auction = assemble_context(_request("auc-0002"), reordered).cache_layout()

    def prefix(layout: Any) -> list[tuple[str, str]]:
        return [(name, block) for tier, name, block in layout if tier != "request"]

    assert prefix(first) == prefix(same_store_new_auction), (
        "a reordered catalog and a new auction id must leave the cacheable prefix untouched"
    )
    assert first != same_store_new_auction, "the request tier must actually differ"


def test_an_auction_context_is_immutable_and_reads_the_envelope_it_was_given() -> None:
    context = _context()
    assembled = assemble_context(_request(), context)
    assert isinstance(assembled, AuctionContext)
    assert assembled.pursues(CLUSTER)
    assert not assembled.pursues("cluster-espresso")
    assert assembled.is_cold
    assert assembled.intro_discount_pct == 0.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        assembled.store_id = "someone-else"  # type: ignore[misc]


def test_the_runtime_does_not_mutate_the_context_it_was_handed() -> None:
    context = _context()
    before = copy.deepcopy(context)
    _bid(context=context)
    assert context == before, "a bid must be a read of the store context, never a write to it"


# ---------------------------------------------------------------------------------------------
# 9. One module object, whichever way the import is spelled
# ---------------------------------------------------------------------------------------------


def test_both_import_spellings_of_the_runtime_are_one_module_object() -> None:
    """Two module objects would be two `Decline` classes, and `isinstance` a coin toss.

    The same hole `store_agent.hooks` closed for `HookProvenanceError`: a caller that spelled
    the import the other way would read a genuine decline as a bid.
    """
    import importlib
    import importlib.machinery
    import sys
    import types

    canonical = importlib.import_module("store_agent.runtime")

    for dotted, target in (
        ("packages", REPO_ROOT / "packages"),
        ("packages.store_agent", REPO_ROOT / "packages" / "store-agent"),
    ):
        if dotted not in sys.modules:
            module = types.ModuleType(dotted)
            spec = importlib.machinery.ModuleSpec(dotted, None, is_package=True)
            spec.submodule_search_locations = [str(target)]
            module.__spec__ = spec
            module.__path__ = spec.submodule_search_locations
            sys.modules[dotted] = module
            if "." in dotted:
                parent, _, leaf = dotted.rpartition(".")
                setattr(sys.modules[parent], leaf, module)

    through_repo_path = importlib.import_module("packages.store_agent.src.runtime")
    assert through_repo_path is canonical
    assert through_repo_path.Decline is Decline
    assert isinstance(bid(_request(cluster_id="nope"), _context()), through_repo_path.Decline)
