"""T-040 hardening — the exclusivity property, re-tested where an auditor defeated it.

`test_hooks.py` proves the boundary refuses a claim no hook emitted *when the claim is handed
to it in a flat list*. That is not the property T-040 advertises. The property is "tool hooks
are the ONLY way facts and discounts enter a bid", and a bid is not a flat list: it is a `Bid`
carrying an `Offer`, and the `Offer` carries a `discount` and its own `commitments: list[Claim]`.
Each test below is one way that property was actually broken, written as the attack rather than
as a format check:

1. the payload one level down — an unauthorised discount and un-emitted commitments riding on
   the offer, invisible to a guard that only walks what it was handed;
2. a discount minted by a hook that never asked the envelope — `choose_policy_action` copies
   `discount_pct` out of the learned policy verbatim, and escapes the wall because its key is
   not the one key the wall looks for;
3. the admission ledger, writable by the party it guards;
4. the static half of R8, evaded by ordinary dynamic spellings of a construction;
5. a grant whose product scope is matched by suffix, so a longer product ref lends its tail
   authorization to a shorter one;
6. a grant that the docstring says is spendable "exactly once" and that was in fact spendable
   without limit;
7. two import spellings resolving to two module objects, so `isinstance` is a coin toss.

Every test here fails on the code as it stood before this file was written. That is the point:
a boundary test that passes on the broken boundary is evidence of nothing.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from contracts import Bid, Claim, Discount, Offer, Provenance, ProvenanceSource
from store_agent.hooks import (
    ClaimScopeError,
    Denied,
    HookProvenanceError,
    ToolHooks,
    claim_fingerprint,
    claim_scope,
    enforce_hook_provenance,
    format_offences,
    hosted_claim_construction_offenders,
    mint_claim,
    scoped_ref,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ENVELOPE_FIXTURES = REPO_ROOT / "fixtures" / "envelopes"

CLUSTER = "cluster-warm-layers"


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
    fields: dict[str, Any] = {
        "product_ref": product_ref,
        "unit_price": 100.0,
        "total_price": 100.0,
        "currency": "USD",
        "commitments": [],
    }
    fields.update(overrides)
    return Offer(**fields)


def _bid(offer: Offer, claims: list[Any]) -> Bid:
    return Bid(
        auction_id="auction-1",
        store_id="store-alpha",
        offer=offer,
        claims=list(claims),
        agent_version="store-agent/test",
        schema_version="1.0.0",
    )


def _fingerprints(claims: Any) -> set[str]:
    return {claim_fingerprint(c) for c in claims}


# ---------------------------------------------------------------------------------------------
# 1. The payload one level down: the Offer is part of the bid, so it is part of the boundary
# ---------------------------------------------------------------------------------------------


def test_the_boundary_admits_a_whole_bid_whose_offer_is_built_only_from_hooks(
    hooks: ToolHooks,
) -> None:
    """The honest path first: an offer's own commitments and discount must be *checkable*.

    A guard that cannot read an `Offer` at all cannot be the bid boundary — it can only be the
    boundary for whichever fragment a caller happened to hand it, which is exactly how the
    payload got in. So the first requirement is that a bid assembled entirely from hooks passes
    *as a bid*, with every claim reachable inside it accounted for.
    """
    commitments = hooks.get_owner_commitments(CLUSTER)
    fact = hooks.get_product_fact("prod-cap", "material")
    grant = hooks.authorize_discount("prod-cap", 15.0)
    assert not isinstance(grant, Denied), "15% is inside both of prod-cap's walls"

    bid = _bid(
        _offer(
            "prod-cap",
            unit_price=85.0,
            total_price=85.0,
            discount=Discount(type="percentage", value=15.0),
            commitments=list(commitments),
        ),
        [fact, grant],
    )

    admitted = enforce_hook_provenance(bid, hooks, product_ref="prod-cap")
    assert _fingerprints(admitted) == _fingerprints([fact, grant, *commitments]), (
        "every claim in the bid — including the ones carried on the offer — must be admitted"
    )


def test_an_unauthorised_discount_hidden_in_the_offer_is_refused(hooks: ToolHooks) -> None:
    """The exploit: 25% is refused by the envelope, so it arrives on the offer instead.

    The bid's top-level `claims` list is impeccable — every entry really was minted by a hook.
    The unauthorised depth rides on `offer.discount`, one level down, where a guard that walks
    only the list it was handed never looks. The envelope refuses 25% on `prod-cap`, and a
    boundary that admits this bid has made that refusal cosmetic.
    """
    refused = hooks.authorize_discount("prod-cap", 25.0)
    assert isinstance(refused, Denied), "25% must be past store-alpha's 20% cap"
    assert refused.reason == "over_max_discount_pct"

    honest_claims = hooks.get_owner_commitments(CLUSTER)
    bid = _bid(
        _offer(
            "prod-cap",
            unit_price=75.0,
            total_price=75.0,
            discount=Discount(type="percentage", value=25.0),
        ),
        list(honest_claims),
    )

    with pytest.raises(HookProvenanceError) as raised:
        enforce_hook_provenance(bid, hooks, product_ref="prod-cap")
    assert "discount" in str(raised.value), (
        f"the refusal must name the offer's discount: {raised.value}"
    )


def test_a_commitment_smuggled_onto_the_offer_is_refused(hooks: ToolHooks) -> None:
    """`Offer.commitments` is `list[Claim]`, so it is a second door of exactly the same kind.

    The smuggled commitment is the one from the frozen suite: right source class, right envelope
    ref, only what it *asserts* was changed. It sits on the offer rather than in `bid.claims`.
    """
    genuine = hooks.get_owner_commitments(CLUSTER)
    real = genuine[0].model_dump()
    smuggled = Claim(
        key=real["key"],
        value="90 days",
        provenance=Provenance(**dict(real["provenance"])),
    )

    bid = _bid(
        _offer("prod-cap", commitments=[*genuine, smuggled]),
        [],
    )
    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance(bid, hooks, product_ref="prod-cap")

    # And the same bid without the smuggled entry is admitted, so the wall refuses something
    # specific rather than refusing every offer.
    ok = _bid(_offer("prod-cap", commitments=list(genuine)), [])
    assert _fingerprints(enforce_hook_provenance(ok, hooks, product_ref="prod-cap")) == (
        _fingerprints(genuine)
    )


def test_an_offer_discount_must_be_backed_by_a_grant_carried_in_the_same_bid(
    hooks: ToolHooks,
) -> None:
    """A depth the envelope *would* allow is still not a depth this bid was granted.

    10% clears both of `prod-cap`'s walls, so the envelope has no objection to the number. What
    is missing is an authorization: no hook was asked, nothing was minted, and the bid carries
    no grant. R8 is "the hooks are the only door", not "the number is plausible".
    """
    plausible = hooks.would_authorize("prod-cap", 10.0)
    assert plausible, "10% must be inside prod-cap's walls, or this test proves nothing"

    bid = _bid(
        _offer("prod-cap", discount=Discount(type="percentage", value=10.0)),
        list(hooks.get_owner_commitments(CLUSTER)),
    )
    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance(bid, hooks, product_ref="prod-cap")

    # Asking the hook is all it takes.
    grant = hooks.authorize_discount("prod-cap", 10.0)
    assert not isinstance(grant, Denied)
    backed = _bid(
        _offer("prod-cap", discount=Discount(type="percentage", value=10.0)),
        [*hooks.get_owner_commitments(CLUSTER), grant],
    )
    assert enforce_hook_provenance(backed, hooks, product_ref="prod-cap")


def test_an_offer_discount_deeper_than_the_grant_that_backs_it_is_refused(
    hooks: ToolHooks,
) -> None:
    """The grant is real and admissible; the offer simply spends more of it than it says.

    This is the quietest version of the attack — one number changed on the offer, with a genuine
    authorization sitting beside it in the bid to make the paperwork look complete.
    """
    grant = hooks.authorize_discount("prod-cap", 5.0)
    assert not isinstance(grant, Denied)

    bid = _bid(
        _offer("prod-cap", discount=Discount(type="percentage", value=20.0)),
        [grant],
    )
    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance(bid, hooks, product_ref="prod-cap")


def test_the_bid_boundary_reads_the_product_off_the_bid_it_is_given(hooks: ToolHooks) -> None:
    """`enforce_bid_provenance` exists so "which product is this bid about" is not a caller's job.

    The scope wall fails closed on an unnamed product, which makes `product_ref=` a parameter a
    runtime can forget — and forgetting it turns a refusal into a traceback rather than into a
    silently admitted grant, but it still stops the honest path. A bid names its own product on
    `offer.product_ref`; the boundary should read it there.
    """
    from store_agent.hooks import enforce_bid_provenance

    grant = hooks.authorize_discount("prod-cap", 15.0)
    assert not isinstance(grant, Denied)
    bid = _bid(
        _offer("prod-cap", discount=Discount(type="percentage", value=15.0)),
        [grant],
    )
    assert enforce_bid_provenance(bid, hooks)

    # A bid about the other product does not get to spend it, even though nobody was asked.
    grant_b = hooks.authorize_discount("prod-cap", 15.0)
    assert not isinstance(grant_b, Denied)
    replayed = _bid(
        _offer("prod-floor", discount=Discount(type="percentage", value=15.0)),
        [grant_b],
    )
    with pytest.raises(HookProvenanceError):
        enforce_bid_provenance(replayed, hooks)


# ---------------------------------------------------------------------------------------------
# 2. Any claim that carries a discount goes through the envelope wall
# ---------------------------------------------------------------------------------------------


def test_the_learned_policy_cannot_mint_a_depth_the_envelope_refuses(
    alpha: dict[str, Any],
) -> None:
    """`choose_policy_action` is hook 5, and it was copying `discount_pct` out of a trained file.

    The learned policy is not a merchant approval. It is the output of the store's own learning
    loop, and a loop that has learned to ask for 25% must still be refused by a 20% envelope —
    otherwise `authorize_discount` is not "the only way a discount reaches an offer", it is one
    of two ways, and the second one has no walls at all.
    """
    learned = {
        "version": "policy-v7",
        "actions": {CLUSTER: {"discount_pct": 25.0, "commitment_keys": ["free_returns"]}},
    }
    hooks = ToolHooks(_context_from(alpha, learned_policy=learned))

    assert isinstance(hooks.authorize_discount("prod-cap", 25.0), Denied), (
        "the envelope must refuse 25% for this test to mean anything"
    )

    action = hooks.choose_policy_action({"cluster_id": CLUSTER, "product_ref": "prod-cap"})
    assert action.value["discount_pct"] == 0.0, (
        "a policy depth the envelope refuses must fail closed to no discount, not be minted"
    )
    assert action.value["policy_version"] == "policy-v7", "the action is still the policy's"
    assert action.value["commitment_keys"] == ["free_returns"], "only the depth is walled"


def test_a_policy_depth_inside_the_walls_is_minted_and_stays_checkable(
    alpha: dict[str, Any],
) -> None:
    """The other direction, and the boundary's half of the same wall.

    15% is inside store-alpha's envelope, so the policy's choice stands. It is still an
    authorization: when the merchant tightens the envelope to 5%, the banked action must stop
    being admissible, exactly as a `authorized_discount_pct` grant does. A wall that only
    watches one claim key is a wall around a spelling.
    """
    learned = {"version": "policy-v7", "actions": {CLUSTER: {"discount_pct": 15.0}}}
    hooks = ToolHooks(_context_from(alpha, learned_policy=learned))

    action = hooks.choose_policy_action({"cluster_id": CLUSTER, "product_ref": "prod-cap"})
    assert action.value["discount_pct"] == 15.0
    assert enforce_hook_provenance([action], hooks, product_ref="prod-cap") == [action]

    hooks.envelope["max_discount_pct"] = 5.0
    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance([action], hooks, product_ref="prod-cap")


def test_a_policy_action_carrying_a_depth_cannot_be_spent_on_another_product(
    alpha: dict[str, Any],
) -> None:
    """The policy chose a depth for one product; the floors it clears belong to that product."""
    learned = {"version": "policy-v7", "actions": {CLUSTER: {"discount_pct": 15.0}}}
    hooks = ToolHooks(_context_from(alpha, learned_policy=learned))

    action = hooks.choose_policy_action({"cluster_id": CLUSTER, "product_ref": "prod-cap"})
    assert action.value["discount_pct"] == 15.0
    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance([action], hooks, product_ref="prod-floor")


def test_a_cold_policy_action_needs_no_product_because_it_carries_no_depth(
    alpha: dict[str, Any],
) -> None:
    """R10's cold-start action is a fact about the store, not an authorization. It must pass."""
    hooks = ToolHooks(_context_from(alpha, learned_policy=None))
    action = hooks.choose_policy_action({"cluster_id": CLUSTER, "product_ref": "prod-cap"})
    assert action.value["discount_pct"] == 0.0
    assert enforce_hook_provenance([action], hooks) == [action]


# ---------------------------------------------------------------------------------------------
# 3. The ledger cannot be written by the thing being guarded
# ---------------------------------------------------------------------------------------------


def test_the_admission_ledger_cannot_be_written_through_the_facade(hooks: ToolHooks) -> None:
    """R8's whole argument is "the ledger cannot be written by the thing being guarded".

    The hosted path holds the facade — that is what a tool harness *is*. If the ledger is a
    public mutable set on that facade, the guarded party can enter its own forgery into the
    record it is graded against, and every other wall in this module is decoration.
    """
    forged = mint_claim(
        key="free_returns",
        value="90 days",
        source=ProvenanceSource.owner_statement,
        ref="envelope:store-alpha:v3#free_returns",
    )
    fingerprint = claim_fingerprint(forged)

    ledger = hooks.emitted_fingerprints
    with pytest.raises((AttributeError, TypeError)):
        ledger.add(fingerprint)  # type: ignore[union-attr]
    with pytest.raises((AttributeError, TypeError)):
        hooks.emitted_fingerprints |= {fingerprint}
    with pytest.raises(AttributeError):
        hooks.emitted_fingerprints = {fingerprint}  # type: ignore[misc]

    assert fingerprint not in hooks.emitted_fingerprints
    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance([forged], hooks)


def test_no_public_attribute_of_the_facade_is_the_writable_ledger(hooks: ToolHooks) -> None:
    """A read-only property in front of a public mutable set would be a fig leaf.

    The check is over the instance's own public attributes: none of them may be the live set the
    guard reads, or the property is only a suggestion.
    """
    hooks.get_owner_commitments(CLUSTER)
    live = set(hooks.emitted_fingerprints)
    assert live, "the facade must have emitted something for this to be a real check"

    public_sets = {
        name: value
        for name, value in vars(hooks).items()
        if not name.startswith("_") and isinstance(value, (set, list, dict))
    }
    for name, value in public_sets.items():
        assert not (isinstance(value, set) and value & live), (
            f"public attribute {name!r} exposes the admission ledger for writing"
        )


# ---------------------------------------------------------------------------------------------
# 4. The static half must be a rule about building a Claim, not about how it is spelled
# ---------------------------------------------------------------------------------------------


def test_the_lint_catches_the_dynamic_spellings_of_a_forbidden_construction(
    tmp_path: Path,
) -> None:
    """The static half exists for the branch the runtime guard never executes.

    Every module below builds a real `contracts.Claim` or `contracts.Provenance` on the hosted
    path without writing the guarded token as the callee. None is exotic — `getattr`, a registry
    dict, a module attribute bound to a local name, a dotted class reference — and a lint that
    misses them is a lint that a hosted branch walks straight past.
    """
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "by_getattr.py").write_text(
        "from contracts import protocol\n"
        "\n"
        "def make(row):\n"
        "    return getattr(protocol, 'Claim')(**row)\n",
        encoding="utf-8",
    )
    (tmp_path / "runtime" / "by_registry.py").write_text(
        "from contracts import Claim, Provenance\n"
        "\n"
        "MODELS = {'Claim': Claim, 'Provenance': Provenance}\n"
        "\n"
        "def make(row):\n"
        "    return MODELS['Provenance'](**row)\n",
        encoding="utf-8",
    )
    (tmp_path / "runtime" / "by_module_attr.py").write_text(
        "import contracts\n\nFact = contracts.Claim\n\ndef make(row):\n    return Fact(**row)\n",
        encoding="utf-8",
    )
    (tmp_path / "runtime" / "by_dotted_validate.py").write_text(
        "from contracts import protocol as p\n"
        "\n"
        "def make(row):\n"
        "    return p.Claim.model_validate(row)\n",
        encoding="utf-8",
    )
    (tmp_path / "runtime" / "by_named_key.py").write_text(
        "from contracts import Claim\n"
        "\n"
        "WANTED = 'Claim'\n"
        "MODELS = {'Claim': Claim}\n"
        "\n"
        "def make(row):\n"
        "    return MODELS[WANTED](**row)\n",
        encoding="utf-8",
    )

    offences = hosted_claim_construction_offenders(tmp_path)
    assert {(o.path, o.name) for o in offences} == {
        ("runtime/by_getattr.py", "Claim"),
        ("runtime/by_registry.py", "Provenance"),
        ("runtime/by_module_attr.py", "Claim"),
        ("runtime/by_dotted_validate.py", "Claim"),
        ("runtime/by_named_key.py", "Claim"),
    }, format_offences(offences)


def test_the_widened_lint_still_ignores_code_that_builds_nothing(tmp_path: Path) -> None:
    """A lint that fires on everything catches nothing: the widening must stay a rule about
    constructing a guarded object."""
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "innocent.py").write_text(
        "from contracts import Claim, Envelope\n"
        "\n"
        "LABELS = {'Claim': 'a claim', 'Provenance': 'a provenance'}\n"
        "\n"
        "def describe(claim: Claim) -> str:\n"
        "    dump = getattr(claim, 'model_dump')\n"
        "    dump()\n"
        "    return LABELS['Claim']\n"
        "\n"
        "def load(row) -> Envelope:\n"
        "    return Envelope.model_validate(row)\n"
        "\n"
        "def sizes(rows):\n"
        "    return [len(r) for r in rows]\n",
        encoding="utf-8",
    )
    assert hosted_claim_construction_offenders(tmp_path) == []


# ---------------------------------------------------------------------------------------------
# 5. A grant's product scope is matched exactly, not by suffix
# ---------------------------------------------------------------------------------------------


def test_a_grant_for_a_longer_product_ref_does_not_authorize_its_tail(
    alpha: dict[str, Any],
) -> None:
    """`ref.endswith('@' + product_ref)` lends `bundle@prod-cap`'s grant to `prod-cap`.

    The separator is a legal character in a product ref — nothing upstream forbids it — so a
    catalog that carries one turns the scope wall into a substring match. The grant below is
    entirely genuine; it was simply granted for a different product whose name ends the same way.
    """
    fixture = _load(ENVELOPE_FIXTURES / "store-alpha.approved.json")
    fixture["catalog"]["bundle@prod-cap"] = {
        "product_ref": "bundle@prod-cap",
        "list_price": 100.0,
        "material": "merino wool",
    }
    hooks = ToolHooks(_context_from(fixture))

    granted = hooks.authorize_discount("bundle@prod-cap", 20.0)
    assert not isinstance(granted, Denied)
    assert claim_scope(granted) == "bundle@prod-cap", (
        "the scope reported must be the product the hook was actually asked about"
    )

    with pytest.raises(ClaimScopeError):
        enforce_hook_provenance([granted], hooks, product_ref="prod-cap")

    # Exact, not merely stricter: the product it really was granted for still spends it.
    assert enforce_hook_provenance([granted], hooks, product_ref="bundle@prod-cap") == [granted]


def test_the_scope_wall_still_matches_a_product_ref_containing_the_separator(
    alpha: dict[str, Any],
) -> None:
    """The rule half of a scoped ref carries no separator, so the split is unambiguous."""
    fixture = _load(ENVELOPE_FIXTURES / "store-alpha.approved.json")
    fixture["catalog"]["kit@bundle@1"] = {"product_ref": "kit@bundle@1", "list_price": 50.0}
    hooks = ToolHooks(_context_from(fixture))

    granted = hooks.authorize_discount("kit@bundle@1", 10.0)
    assert not isinstance(granted, Denied)
    assert granted.provenance.ref == scoped_ref(
        hooks.envelope_ref("max_discount_pct"), "kit@bundle@1"
    )
    assert enforce_hook_provenance([granted], hooks, product_ref="kit@bundle@1") == [granted]


# ---------------------------------------------------------------------------------------------
# 6. "A grant is not restatable, only spendable, and exactly once"
# ---------------------------------------------------------------------------------------------


def test_a_grant_is_spendable_exactly_once(hooks: ToolHooks) -> None:
    """The documented property, which the ledger's membership test did not implement.

    One `authorize_discount` call is one authorization. A membership check admits it as often as
    it is presented, so a single grant could furnish the discount for every offer in a bid —
    the exchange sees one authorization and any number of discounted offers built on it.
    """
    granted = hooks.authorize_discount("prod-cap", 20.0)
    assert not isinstance(granted, Denied)

    assert enforce_hook_provenance([granted], hooks, product_ref="prod-cap") == [granted]
    with pytest.raises(HookProvenanceError) as raised:
        enforce_hook_provenance([granted], hooks, product_ref="prod-cap")
    assert "spent" in str(raised.value).lower(), (
        f"the refusal must say the grant is spent, not that no hook emitted it: {raised.value}"
    )

    # Asking the hook again is all it takes; the wall is about spending one grant twice.
    regranted = hooks.authorize_discount("prod-cap", 20.0)
    assert not isinstance(regranted, Denied)
    assert enforce_hook_provenance([regranted], hooks, product_ref="prod-cap") == [regranted]


def test_a_refused_boundary_call_spends_nothing(hooks: ToolHooks) -> None:
    """Consumption must be atomic with admission, or a refused bid burns an honest grant."""
    granted = hooks.authorize_discount("prod-cap", 20.0)
    assert not isinstance(granted, Denied)

    smuggled = {
        "key": "free_returns",
        "value": "90 days",
        "provenance": {
            "source": "owner_statement",
            "ref": "envelope:store-alpha:v3#free_returns",
            "observed_at": "2026-01-01T00:00:00Z",
            "authority_rank": 1,
        },
    }
    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance([granted, smuggled], hooks, product_ref="prod-cap")

    assert enforce_hook_provenance([granted], hooks, product_ref="prod-cap") == [granted], (
        "a call that was refused as a whole must leave the grant unspent"
    )


def test_a_fact_stays_restatable_within_the_bid_it_was_read_in(hooks: ToolHooks) -> None:
    """The asymmetry is the point: a fact is true however often it is said, a grant is not."""
    fact = hooks.get_product_fact("prod-cap", "material")
    assert enforce_hook_provenance([fact], hooks) == [fact]
    assert enforce_hook_provenance([fact], hooks) == [fact]


def test_the_docstring_property_and_the_code_agree_about_single_use() -> None:
    """The module claims "exactly once" in prose; this pins that the prose is implemented."""
    from store_agent.hooks import provenance as module

    assert module.__doc__ is not None
    assert "exactly once" in module.__doc__
    assert callable(getattr(ToolHooks, "spend_authorization", None)), (
        "single use needs somewhere to record a spend; the facade owns the ledger"
    )


# ---------------------------------------------------------------------------------------------
# 7. Two import spellings, one module object
# ---------------------------------------------------------------------------------------------


def _register_namespace(dotted: str, directory: Path) -> None:
    """The frozen suite's alias for the hyphenated tree, replayed here."""
    parts = dotted.split(".")
    for index, part in enumerate(parts):
        name = ".".join(parts[: index + 1])
        target = directory if index == len(parts) - 1 else REPO_ROOT.joinpath(*parts[: index + 1])
        module = sys.modules.get(name)
        if module is None:
            module = types.ModuleType(name)
            spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
            spec.submodule_search_locations = [str(target)]
            module.__spec__ = spec
            module.__path__ = spec.submodule_search_locations  # type: ignore[attr-defined]
            module.__package__ = name
            sys.modules[name] = module
            if index:
                setattr(sys.modules[".".join(parts[:index])], part, module)
        else:
            search = getattr(module, "__path__", None)
            if search is not None and str(target) not in list(search):
                search.append(str(target))


def test_both_advertised_import_spellings_resolve_to_one_module_object() -> None:
    """The package advertises two spellings. Two module objects means two of every class.

    The frozen acceptance suite imports `packages.store_agent.src.hooks`; this package's own
    tests import `store_agent.hooks`. If those are distinct modules then a `ToolHooks` built by
    one is not an instance of the other's `ToolHooks`, and an `except HookProvenanceError`
    written against one does not catch the other's — a security boundary whose exception type
    depends on which spelling the caller typed.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    _register_namespace("packages.store_agent", REPO_ROOT / "packages" / "store-agent")

    canonical = importlib.import_module("store_agent.hooks")
    long_spelling = importlib.import_module("packages.store_agent.src.hooks")

    assert long_spelling is canonical, (
        "the two advertised spellings must be one module object, or nothing exported by them "
        "is identical"
    )
    assert long_spelling.ToolHooks is canonical.ToolHooks
    assert long_spelling.HookProvenanceError is canonical.HookProvenanceError

    long_tools = importlib.import_module("packages.store_agent.src.hooks.tools")
    assert long_tools is importlib.import_module("store_agent.hooks.tools")


def test_a_facade_built_through_one_spelling_is_refused_by_the_other_guard(
    alpha: dict[str, Any],
) -> None:
    """The consequence, as behaviour rather than as an identity check."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    _register_namespace("packages.store_agent", REPO_ROOT / "packages" / "store-agent")
    long_spelling = importlib.import_module("packages.store_agent.src.hooks")

    built = long_spelling.ToolHooks(_context_from(alpha))
    assert isinstance(built, ToolHooks), (
        "a facade built through one spelling must be an instance of the other's class"
    )

    granted = built.authorize_discount("prod-cap", 20.0)
    assert not isinstance(granted, Denied)
    with pytest.raises(HookProvenanceError):
        # This except clause is written against *this* module's exception type.
        long_spelling.enforce_hook_provenance([granted], built, product_ref="prod-floor")
