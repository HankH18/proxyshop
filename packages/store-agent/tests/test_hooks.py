"""T-040 — the tool hooks are the only way a fact or a discount enters a hosted bid (R8).

Three claims are under test, and each is checked against something the implementation did not
choose for itself:

1. **Every hook stamps the provenance source DESIGN assigns it.** Asserted against the published
   `HOOK_SOURCE_CLASSES` table, with *set equality* over the sources of every claim reachable in
   the return value — so a hook that slipped in one extra claim under a different source fails.
2. **`authorize_discount` enforces the envelope walls.** Driven from the approved envelope
   fixtures in `fixtures/envelopes/`: every row of `discount_expectations` is a request and the
   answer the envelope's own arithmetic gives, written down by a human alongside the envelope.
   The test does not recompute the walls — it would then only be checking the implementation
   against itself.
3. **A claim built outside the hooks is refused, by lint and at runtime.** The runtime guard is
   shown refusing a claim whose provenance is *legitimate in every visible respect* — right
   source class, right envelope ref — and differs only in what it asserts. The static lint is
   shown refusing a real offending module before it is shown finding none in the shipped tree, so
   "no offenders" is a result rather than a vacuous pass.

Offline and clock-free by construction: the hooks read nothing but the store context they were
built from, which is what makes the same inputs produce the same bid twice (S4).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from contracts import Envelope, ProvenanceSource
from store_agent.hooks import (
    HOOK_SOURCE_CLASSES,
    REASON_BELOW_PRICE_FLOOR,
    ClaimScopeError,
    Denied,
    HookInputError,
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
from store_agent.hooks.lint import MINTING_SITE, Offence

REPO_ROOT = Path(__file__).resolve().parents[3]
STORE_AGENT_SRC = REPO_ROOT / "packages" / "store-agent" / "src"
ENVELOPE_FIXTURES = REPO_ROOT / "fixtures" / "envelopes"

CLUSTER = "cluster-warm-layers"


# ---------------------------------------------------------------------------------------------
# The approved envelope fixtures
# ---------------------------------------------------------------------------------------------


def _fixture_paths() -> list[Path]:
    paths = sorted(ENVELOPE_FIXTURES.glob("*.approved.json"))
    assert paths, f"no approved envelope fixtures found under {ENVELOPE_FIXTURES}"
    return paths


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _context_from(fixture: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """The store context a `ToolHooks` is built from, taken straight out of a fixture file."""
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
def alpha_hooks(alpha: dict[str, Any]) -> ToolHooks:
    context = _context_from(
        alpha,
        live_state={"prod-cap": {"in_stock": True, "units_left": 7}},
    )
    return ToolHooks(context)


def _sources(claims: Any) -> set[str]:
    listed = claims if isinstance(claims, list) else [claims]
    return {str(claim.provenance.source) for claim in listed}


# ---------------------------------------------------------------------------------------------
# Acceptance 1 — each hook returns correctly sourced Provenance
# ---------------------------------------------------------------------------------------------


def test_the_six_hooks_are_the_ones_design_names(alpha_hooks: ToolHooks) -> None:
    """The contract is six hooks by those exact names — a renamed hook is a broken harness."""
    missing = [
        name for name in HOOK_SOURCE_CLASSES if not callable(getattr(alpha_hooks, name, None))
    ]
    assert not missing, f"tool hooks missing from the store-agent contract: {missing}"


def test_every_hook_stamps_the_provenance_source_design_assigns_it(alpha_hooks: ToolHooks) -> None:
    """R8/acceptance 1: set equality, so an extra claim under a foreign source fails too."""
    calls = {
        "get_product_fact": lambda: alpha_hooks.get_product_fact("prod-cap", "material"),
        "get_live_state": lambda: alpha_hooks.get_live_state("prod-cap"),
        "get_owner_commitments": lambda: alpha_hooks.get_owner_commitments(CLUSTER),
        "authorize_discount": lambda: alpha_hooks.authorize_discount("prod-cap", 5.0),
        "choose_policy_action": lambda: alpha_hooks.choose_policy_action(
            {"cluster_id": CLUSTER, "product_ref": "prod-cap"}
        ),
        "get_network_prior": lambda: alpha_hooks.get_network_prior(CLUSTER),
    }
    assert set(calls) == set(HOOK_SOURCE_CLASSES), "the call table and the source table disagree"

    for hook_name, expected_source in HOOK_SOURCE_CLASSES.items():
        returned = calls[hook_name]()
        assert _sources(returned) == {expected_source}, (
            f"{hook_name} must stamp provenance source {expected_source!r}"
        )


def test_authority_rank_comes_from_the_published_table_not_from_the_hook(
    alpha_hooks: ToolHooks,
) -> None:
    """D30: two hooks cannot number the same class of evidence differently."""
    scraped = alpha_hooks.get_product_fact("prod-cap", "material")
    pixel = alpha_hooks.get_live_state("prod-cap")[0]
    prior = alpha_hooks.get_network_prior(CLUSTER)

    assert scraped.provenance.authority_rank == 4, "scraped text is a third-party reading"
    assert pixel.provenance.authority_rank == 2, "the pixel is the store's own installed app"
    assert prior.provenance.authority_rank == 3, "a network prior is platform inference"


def test_a_hook_refuses_to_invent_a_fact_it_has_no_evidence_for(alpha_hooks: ToolHooks) -> None:
    """A hook that answered for an unknown product would be minting a fact from nothing."""
    with pytest.raises(HookInputError):
        alpha_hooks.get_product_fact("prod-does-not-exist", "material")
    with pytest.raises(HookInputError):
        alpha_hooks.get_product_fact("prod-cap", "thread_count")
    with pytest.raises(HookInputError):
        alpha_hooks.get_live_state("prod-does-not-exist")


def test_the_pixel_feed_is_lossy_rather_than_inventive(alpha_hooks: ToolHooks) -> None:
    """`prod-floor` is in the catalog but has fired no pixel: no claims, not a guessed one."""
    assert alpha_hooks.get_live_state("prod-floor") == []
    assert alpha_hooks.call_log[-1].outcome == "no_feed_data"


def test_the_cold_policy_hook_reports_its_version_and_improvises_nothing(
    alpha_hooks: ToolHooks,
) -> None:
    """R10: with no learned policy the action is the deterministic default at zero discount."""
    action = alpha_hooks.choose_policy_action({"cluster_id": CLUSTER, "product_ref": "prod-cap"})
    assert action.value["policy_version"] == "cold-start"
    assert action.value["discount_pct"] == 0.0
    assert action.value["commitment_keys"] == ["free_returns", "ships_within"]
    assert alpha_hooks.call_log[-1].detail == "cold-start", "the policy version must be logged"


def test_a_learned_policy_is_reported_under_its_own_version(alpha: dict[str, Any]) -> None:
    """The same hook, warm: the action comes from the policy and the ref names its version."""
    hooks = ToolHooks(
        _context_from(
            alpha,
            learned_policy={
                "version": "policy-2026-02-01",
                "actions": {CLUSTER: {"discount_pct": 12.5, "value_prop": "durability"}},
            },
        )
    )
    action = hooks.choose_policy_action({"cluster_id": CLUSTER, "product_ref": "prod-cap"})
    assert action.value["discount_pct"] == 12.5
    assert action.value["value_prop"] == "durability"
    assert action.provenance.ref == f"policy:store-alpha:policy-2026-02-01#{CLUSTER}"


def test_a_cluster_the_network_has_no_prior_for_says_so(alpha_hooks: ToolHooks) -> None:
    """An absent prior is network-sourced evidence of absence, not a missing claim."""
    prior = alpha_hooks.get_network_prior("cluster-nobody-has-seen")
    assert prior.value == {}
    assert prior.provenance.ref.endswith("#absent")
    assert prior.provenance.source == ProvenanceSource.network


def test_the_hooks_read_no_clock(alpha: dict[str, Any]) -> None:
    """S4: two facades over identical context emit byte-identical claims."""
    first = ToolHooks(_context_from(alpha))
    second = ToolHooks(_context_from(alpha))
    for hooks in (first, second):
        hooks.get_product_fact("prod-cap", "material")
        hooks.get_owner_commitments(CLUSTER)
        hooks.authorize_discount("prod-cap", 5.0)
        hooks.get_network_prior(CLUSTER)

    assert [c.model_dump() for c in first.emitted_claims] == [
        c.model_dump() for c in second.emitted_claims
    ]
    assert first.emitted_fingerprints == second.emitted_fingerprints


def test_the_context_can_be_passed_positionally_or_by_keyword(alpha: dict[str, Any]) -> None:
    """Both construction forms the frozen suite may use produce the same facade."""
    context = _context_from(alpha)
    positional = ToolHooks(context).get_owner_commitments(CLUSTER)
    keyword = ToolHooks(**context).get_owner_commitments(CLUSTER)
    assert [c.model_dump() for c in positional] == [c.model_dump() for c in keyword]


def test_an_envelope_model_works_as_well_as_its_dict_form(alpha: dict[str, Any]) -> None:
    """The merchant service holds an `Envelope`; the fixtures hold its dict. Same answers."""
    as_dict = ToolHooks(_context_from(alpha))
    as_model = ToolHooks(_context_from(alpha, envelope=Envelope.model_validate(alpha["envelope"])))
    assert [c.model_dump() for c in as_dict.get_owner_commitments(CLUSTER)] == [
        c.model_dump() for c in as_model.get_owner_commitments(CLUSTER)
    ]


# ---------------------------------------------------------------------------------------------
# Acceptance 2 — the envelope walls, against the approved fixtures
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.stem)
def test_every_approved_envelope_fixture_is_a_valid_envelope(path: Path) -> None:
    """A fixture that is not a `contracts.Envelope` grades nothing."""
    fixture = _load(path)
    envelope = Envelope.model_validate(fixture["envelope"])
    assert envelope.standing_commitments, f"{path.name} approves no standing commitments"
    assert fixture["discount_expectations"], f"{path.name} states no expected wall behaviour"
    for entry in fixture["catalog"].values():
        assert entry["list_price"] > 0


def _expectation_cases() -> list[tuple[Path, dict[str, Any]]]:
    return [
        (path, row) for path in _fixture_paths() for row in _load(path)["discount_expectations"]
    ]


@pytest.mark.parametrize(
    ("path", "expectation"),
    _expectation_cases(),
    ids=[
        f"{path.stem}-{row['product_ref']}-{row['requested_pct']}"
        for path, row in _expectation_cases()
    ],
)
def test_authorize_discount_answers_the_approved_envelope(
    path: Path, expectation: dict[str, Any]
) -> None:
    """Acceptance 2: below-floor and over-cap are refused; inside both walls is authorized."""
    fixture = _load(path)
    hooks = ToolHooks(_context_from(fixture))
    result = hooks.authorize_discount(expectation["product_ref"], expectation["requested_pct"])

    if expectation["outcome"] == "denied":
        assert isinstance(result, Denied), (
            f"{expectation} must be refused; {path.name} says {expectation.get('note')}"
        )
        assert not result, "a denial must be falsy so `if authorized:` cannot read it as a grant"
        assert result.reason == expectation["reason"]
        return

    assert not isinstance(result, Denied), (
        f"{expectation} must be authorized; {path.name} says {expectation.get('note')}"
    )
    assert result.provenance.source == ProvenanceSource.envelope_rule
    assert result.value == expectation["requested_pct"]
    assert result.provenance.ref.startswith(f"envelope:{fixture['envelope']['store_id']}:")


def test_the_two_walls_are_independent(alpha_hooks: ToolHooks) -> None:
    """Neither wall is a proxy for the other: each refuses a request the other allows."""
    # Refused by the cap while 75.00 sits far above prod-cap's 10.00 floor.
    over_cap = alpha_hooks.authorize_discount("prod-cap", 25.0)
    # Refused by the floor while 10% is only half of the 20% cap.
    below_floor = alpha_hooks.authorize_discount("prod-floor", 10.0)
    # The same 10% on the product with the low floor is fine, so depth alone decides nothing.
    allowed = alpha_hooks.authorize_discount("prod-cap", 10.0)

    assert isinstance(over_cap, Denied) and over_cap.reason == "over_max_discount_pct"
    assert isinstance(below_floor, Denied) and below_floor.reason == "below_price_floor"
    assert not isinstance(allowed, Denied)


def test_a_store_wide_floor_applies_to_every_product() -> None:
    """store-beta's `product_ref: null` floor binds a product that has no floor of its own."""
    beta = _load(ENVELOPE_FIXTURES / "store-beta.approved.json")
    hooks = ToolHooks(_context_from(beta))
    assert hooks.price_floor("prod-open") == 40.0, "the store-wide floor must reach prod-open"
    assert hooks.price_floor("prod-tight") == 48.0, "the stricter of the two floors binds"


def test_a_refusal_is_recorded_in_the_call_log(alpha_hooks: ToolHooks) -> None:
    """A denial is an auditable event, not a silent return."""
    alpha_hooks.authorize_discount("prod-cap", 25.0)
    entry = alpha_hooks.call_log[-1]
    assert entry.hook == "authorize_discount"
    assert entry.outcome == "over_max_discount_pct"
    assert entry.claim_keys == (), "a refused request must mint no claim"


def test_a_refused_discount_never_reaches_the_ledger(alpha_hooks: ToolHooks) -> None:
    """The security property: a denial cannot be laundered into an admissible claim."""
    alpha_hooks.authorize_discount("prod-cap", 25.0)
    assert alpha_hooks.emitted_claims == []
    assert alpha_hooks.emitted_fingerprints == set()


# ---------------------------------------------------------------------------------------------
# Acceptance 3 — a claim built outside the hooks fails lint and the runtime guard
# ---------------------------------------------------------------------------------------------


def test_the_guard_admits_exactly_what_the_hooks_emitted(alpha_hooks: ToolHooks) -> None:
    genuine = alpha_hooks.get_owner_commitments(CLUSTER)
    assert genuine, "the envelope must approve at least one commitment for this to mean anything"
    assert enforce_hook_provenance(genuine, alpha_hooks) == genuine


def test_the_guard_survives_the_normalized_dict_view_of_a_claim(alpha_hooks: ToolHooks) -> None:
    """A `Claim` and its `model_dump()` must fingerprint identically, or the guard is a coin toss."""
    genuine = alpha_hooks.get_owner_commitments(CLUSTER)
    as_dicts = [claim.model_dump() for claim in genuine]
    assert enforce_hook_provenance(as_dicts, alpha_hooks) == as_dicts


def test_a_smuggled_claim_wearing_a_legitimate_provenance_is_refused(
    alpha_hooks: ToolHooks,
) -> None:
    """The refusal the whole ticket turns on.

    The smuggled claim is not sloppy: its source is `owner_statement`, one of the six a hook can
    mint, and its `ref` points at a real commitment path on a real approved envelope. Only what it
    *asserts* was changed — 30 days of returns became 90. A guard that inspected
    `provenance.source` would admit it, which is why the guard checks a ledger instead.
    """
    genuine = alpha_hooks.get_owner_commitments(CLUSTER)
    real = genuine[0].model_dump()
    smuggled = {
        "key": real["key"],
        "value": "90 days",
        "provenance": dict(real["provenance"]),
    }
    assert smuggled["provenance"]["source"] == ProvenanceSource.owner_statement
    assert smuggled["provenance"]["ref"] == "envelope:store-alpha:v3#free_returns"

    with pytest.raises(HookProvenanceError) as raised:
        enforce_hook_provenance([*genuine, smuggled], alpha_hooks)
    assert raised.value.offenders == ((len(genuine), raised.value.offenders[0][1]),)


def test_relabelling_a_seller_assertion_as_an_owner_statement_is_refused(
    alpha_hooks: ToolHooks,
) -> None:
    """The other direction: right words, right ref, but no hook ever said them."""
    alpha_hooks.get_owner_commitments(CLUSTER)
    forged = mint_claim(
        key="free_shipping",
        value="always",
        source=ProvenanceSource.owner_statement,
        ref="envelope:store-alpha:v3#free_shipping",
    )
    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance([forged], alpha_hooks)


def test_a_facade_with_no_ledger_is_refused_rather_than_trusted(alpha_hooks: ToolHooks) -> None:
    """ "No ledger" and "an empty ledger" must not be the same answer — the first admits nothing."""
    genuine = alpha_hooks.get_owner_commitments(CLUSTER)

    class LedgerlessHooks:
        pass

    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance(genuine, LedgerlessHooks())


def test_the_lint_refuses_a_module_that_builds_a_claim_directly(tmp_path: Path) -> None:
    """Proof the lint can fail: a real offending tree, and the exact offences it names.

    Run before the shipped-tree assertion below on purpose. "No offenders in `src/`" is only
    evidence of anything once the checker has been seen refusing something.
    """
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "bid.py").write_text(
        "from contracts import Claim, Provenance\n"
        "\n"
        "def assemble():\n"
        "    return Claim(\n"
        "        key='free_returns',\n"
        "        value='90 days',\n"
        "        provenance=Provenance(\n"
        "            source='owner_statement',\n"
        "            ref='envelope:store-alpha:v3#free_returns',\n"
        "            observed_at='2026-01-01T00:00:00Z',\n"
        "            authority_rank=1,\n"
        "        ),\n"
        "    )\n",
        encoding="utf-8",
    )
    # The same construction, in the Tier-2 door, is legitimate: those claims are
    # `seller_asserted` and are disciplined by verification, not by this harness.
    (tmp_path / "external").mkdir()
    (tmp_path / "external" / "receive.py").write_text(
        "from contracts import Claim\n\ndef parse(row):\n    return Claim(**row)\n",
        encoding="utf-8",
    )
    # And the minting site itself must stay allowed, or there would be nowhere to mint.
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "provenance.py").write_text(
        "from contracts import Claim\n\ndef mint_claim(**kw):\n    return Claim(**kw)\n",
        encoding="utf-8",
    )

    offences = hosted_claim_construction_offenders(tmp_path)
    assert offences == [
        Offence(path="runtime/bid.py", line=4, name="Claim"),
        Offence(path="runtime/bid.py", line=7, name="Provenance"),
    ], format_offences(offences)
    assert MINTING_SITE in str(offences[0])


def test_no_shipped_hosted_module_builds_a_claim_directly() -> None:
    """Acceptance 3, the static half, against the tree that actually ships."""
    offences = hosted_claim_construction_offenders(STORE_AGENT_SRC)
    assert offences == [], format_offences(offences)


def test_the_minting_site_the_lint_exempts_is_the_one_that_exists() -> None:
    """A lint whose exemption points at a moved file would exempt nothing and forbid minting."""
    assert (STORE_AGENT_SRC / MINTING_SITE).is_file()


# ---------------------------------------------------------------------------------------------
# The real path, end to end: approved envelope on disk -> hooks -> guarded claim set
# ---------------------------------------------------------------------------------------------


def test_a_bid_claim_set_assembled_only_from_hooks_passes_the_boundary_it_is_graded_at() -> None:
    """The whole R8 loop on real data, and the one tampering that must break it.

    Nothing here is stubbed: the envelope and catalog are read off `fixtures/envelopes/`, the
    claim set is whatever the six hooks emitted for a plausible bid, and the guard is the same
    function the hosted bid boundary calls. The green says the honest path survives the boundary;
    the second half says the boundary is not merely waving everything through, by re-running it
    with a single claim's value edited and requiring a refusal.

    The bid is about `prod-cap`, and the boundary is told so. That is not ceremony: the claim set
    carries an authorization, and an authorization is only valid for the product whose walls were
    checked to grant it.
    """
    fixture = _load(ENVELOPE_FIXTURES / "store-alpha.approved.json")
    hooks = ToolHooks(
        _context_from(fixture, live_state={"prod-cap": {"in_stock": True, "units_left": 7}})
    )

    claims = [
        *hooks.get_owner_commitments(CLUSTER),
        hooks.get_product_fact("prod-cap", "material"),
        *hooks.get_live_state("prod-cap"),
    ]
    authorized = hooks.authorize_discount("prod-cap", 15.0)
    assert not isinstance(authorized, Denied)
    claims.append(authorized)

    assert enforce_hook_provenance(claims, hooks, product_ref="prod-cap") == claims
    assert {str(c.provenance.source) for c in claims} == {
        "owner_statement",
        "scraped",
        "pixel_feed",
        "envelope_rule",
    }
    assert len(hooks.call_log) == 4

    tampered = [claim.model_dump() for claim in claims]
    tampered[-1]["value"] = 40.0  # the depth the envelope refused, under the ref that allowed 15
    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance(tampered, hooks, product_ref="prod-cap")

    # The same honest claim set, offered as evidence in a bid about the other product, is the
    # replay this ticket's hardening exists to stop — every claim in it is genuine.
    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance(claims, hooks, product_ref="prod-floor")


# ---------------------------------------------------------------------------------------------
# A grant is an authorization for ONE product — the envelope's walls are per product
# ---------------------------------------------------------------------------------------------
#
# `authorize_discount` is the only hook whose answer depends on a wall that differs between
# products, and the only one whose citation did not name its subject. `prod-cap` and `prod-floor`
# both list at 100.00; the same 20% request clears the first (80.00 over a 10.00 floor) and
# breaches the second (80.00 under a 95.00 floor). A grant that cites only the store-wide
# `#max_discount_pct` rule is therefore the *same claim* in both worlds, and the refusal is one
# dictionary lookup away from being undone. These tests are about the difference between "a hook
# authorized 20%" and "a hook authorized 20% here".


def test_an_authorized_discount_cites_the_product_whose_walls_were_checked(
    alpha_hooks: ToolHooks,
) -> None:
    """The grant's ref must name the product, because the floor it cleared belongs to one."""
    granted = alpha_hooks.authorize_discount("prod-cap", 20.0)
    assert not isinstance(granted, Denied)

    refused = alpha_hooks.authorize_discount("prod-floor", 20.0)
    assert isinstance(refused, Denied), "the 95.00 floor must refuse 20% off a 100.00 list price"
    assert refused.reason == REASON_BELOW_PRICE_FLOOR

    assert claim_scope(granted) == "prod-cap"
    assert granted.provenance.ref == scoped_ref(
        alpha_hooks.envelope_ref("max_discount_pct"), "prod-cap"
    ), "the ref must cite both the rule that was applied and the product it was applied to"


def test_two_products_granted_the_same_depth_are_not_one_ledger_entry(
    alpha_hooks: ToolHooks,
) -> None:
    """The ledger's unit of admission is a fingerprint, so two grants must not share one.

    5% is granted on both products — on `prod-floor` it lands exactly on the 95.00 floor. They
    are still different authorizations: different walls were checked to reach them. If they
    fingerprint alike, the ledger holds one entry that redeems either.
    """
    on_cap = alpha_hooks.authorize_discount("prod-cap", 5.0)
    on_floor = alpha_hooks.authorize_discount("prod-floor", 5.0)
    assert not isinstance(on_cap, Denied) and not isinstance(on_floor, Denied)

    assert claim_fingerprint(on_cap) != claim_fingerprint(on_floor)
    assert len(alpha_hooks.emitted_fingerprints) == 2, (
        "two distinct grants must leave two distinct entries in the audit ledger"
    )


def test_a_grant_minted_for_product_a_cannot_be_used_for_product_b(
    alpha_hooks: ToolHooks,
) -> None:
    """The defect this hardening exists for: replay, not forgery. Mint for A, spend on B.

    Written as an attack, not as a format check. Asserting that the ref *string* now contains a
    product would pass while a cross-product replay still worked — the guard-that-rejects-nothing
    shape — so every step here is behavioural:

    1. the envelope really does refuse this depth on B (otherwise the replay wins nothing);
    2. the claim really is genuine — minted by a real hook, fingerprint really in the ledger —
       so the ledger wall cannot refuse it and never could;
    3. presented in a bid about B it is refused anyway;
    4. and the same claim in a bid about A is admitted, so the wall refuses something specific
       rather than refusing everything.
    """
    # (1) B's own answer for this depth is a denial. The replay is worth attempting.
    refused_on_b = alpha_hooks.authorize_discount("prod-floor", 20.0)
    assert isinstance(refused_on_b, Denied)
    assert refused_on_b.reason == REASON_BELOW_PRICE_FLOOR
    assert alpha_hooks.emitted_claims == [], "a denial must mint nothing"

    # (2) A's grant is entirely legitimate.
    granted_on_a = alpha_hooks.authorize_discount("prod-cap", 20.0)
    assert not isinstance(granted_on_a, Denied)
    assert claim_fingerprint(granted_on_a) in alpha_hooks.emitted_fingerprints, (
        "the replay is only interesting because the claim is real"
    )

    # (3) Spending A's grant on B is refused.
    with pytest.raises(ClaimScopeError) as raised:
        enforce_hook_provenance([granted_on_a], alpha_hooks, product_ref="prod-floor")
    assert "prod-cap" in str(raised.value) and "prod-floor" in str(raised.value)

    # (4) The very same claim, in the bid it was actually granted for, is admitted.
    assert enforce_hook_provenance([granted_on_a], alpha_hooks, product_ref="prod-cap") == [
        granted_on_a
    ]


def test_a_transferred_grant_is_refused_by_the_same_except_clause(alpha_hooks: ToolHooks) -> None:
    """A caller already refusing un-hooked claims must refuse transferred ones without a change."""
    granted = alpha_hooks.authorize_discount("prod-cap", 20.0)
    assert not isinstance(granted, Denied)
    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance([granted], alpha_hooks, product_ref="prod-floor")


def test_an_authorization_presented_without_naming_the_bid_product_is_refused(
    alpha_hooks: ToolHooks,
) -> None:
    """Fail closed: "which product is this bid about" has no safe default.

    A boundary that let the question go unanswered would restore the whole defect — the grant
    would be admitted precisely because nobody said what it was being spent on.
    """
    granted = alpha_hooks.authorize_discount("prod-cap", 20.0)
    assert not isinstance(granted, Denied)
    with pytest.raises(ClaimScopeError):
        enforce_hook_provenance([granted], alpha_hooks)

    # A claim set carrying no authorization needs no product: only grants are per product.
    commitments = alpha_hooks.get_owner_commitments(CLUSTER)
    assert enforce_hook_provenance(commitments, alpha_hooks) == commitments


def test_the_scope_check_reads_the_ref_the_hook_minted_not_a_field_beside_it(
    alpha_hooks: ToolHooks,
) -> None:
    """Editing the scope onto a claim must break the fingerprint, or the wall is decorative."""
    granted = alpha_hooks.authorize_discount("prod-cap", 20.0)
    assert not isinstance(granted, Denied)

    relabelled = granted.model_dump()
    relabelled["provenance"] = dict(relabelled["provenance"])
    relabelled["provenance"]["ref"] = scoped_ref(
        alpha_hooks.envelope_ref("max_discount_pct"), "prod-floor"
    )
    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance([relabelled], alpha_hooks, product_ref="prod-floor")


# ---------------------------------------------------------------------------------------------
# The fingerprint must cover everything that decides what a claim means downstream
# ---------------------------------------------------------------------------------------------


def test_a_source_span_bolted_onto_a_hooked_claim_is_refused(alpha_hooks: ToolHooks) -> None:
    """`source_span` decides the claim's downstream identity, so the fingerprint must cover it.

    `contracts.claim_id_for` reads `source_span.pitch_ref` to compute a claim's id. A claim the
    hooks minted from the envelope carries no span at all; bolting one on re-attributes the
    merchant's own words to a pitch they were never in, while every visible provenance field
    still matches the genuine article exactly.
    """
    genuine = alpha_hooks.get_owner_commitments(CLUSTER)
    tampered = [claim.model_dump() for claim in genuine]
    tampered[0]["source_span"] = {"pitch_ref": "pitch:not-this-one", "start": 0, "end": 7}
    assert tampered[0]["provenance"] == genuine[0].model_dump()["provenance"], (
        "the tampering must be invisible in the provenance, or it proves nothing"
    )

    with pytest.raises(HookProvenanceError):
        enforce_hook_provenance(tampered, alpha_hooks)

    # The untouched dict view still passes, so the new material did not break the model/dict
    # equivalence the guard depends on.
    untouched = [claim.model_dump() for claim in genuine]
    assert enforce_hook_provenance(untouched, alpha_hooks) == untouched


# ---------------------------------------------------------------------------------------------
# The static half must be a rule about forging a Claim, not a rule about typing "Claim"
# ---------------------------------------------------------------------------------------------


def test_the_lint_refuses_the_ordinary_aliases_of_a_forbidden_construction(
    tmp_path: Path,
) -> None:
    """Each module below builds a guarded object without writing the guarded name as the callee.

    None of these is exotic; every one is a spelling an ordinary Python author reaches for. A
    lint that matches only the bare callee name lets all three through, which makes it a rule
    about spelling rather than a rule about putting a fact into a bid.
    """
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "aliased.py").write_text(
        "from contracts import Claim as Fact\n\ndef make():\n    return Fact(key='k', value='v')\n",
        encoding="utf-8",
    )
    (tmp_path / "runtime" / "rebound.py").write_text(
        "from contracts import Provenance\n\nP = Provenance\n\n"
        "def make():\n    return P(source='owner_statement', ref='r')\n",
        encoding="utf-8",
    )
    (tmp_path / "runtime" / "validated.py").write_text(
        "from contracts import Claim\n\ndef make(row):\n    return Claim.model_validate(row)\n",
        encoding="utf-8",
    )

    offences = hosted_claim_construction_offenders(tmp_path)
    assert {(o.path, o.name) for o in offences} == {
        ("runtime/aliased.py", "Claim"),
        ("runtime/rebound.py", "Provenance"),
        ("runtime/validated.py", "Claim"),
    }, format_offences(offences)


def test_the_lint_does_not_fire_on_things_that_are_not_constructions(tmp_path: Path) -> None:
    """The other direction, so the strengthened rule is not simply matching more of everything.

    Annotating with `Claim`, importing it, dumping one, and calling an unrelated
    `model_validate` on something that is not a guarded class are all legitimate.
    """
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "innocent.py").write_text(
        "from contracts import Claim, ClaimType, Envelope\n"
        "\n"
        "def dump(claim: Claim) -> dict:\n"
        "    kind = ClaimType('price')\n"
        "    return claim.model_dump()\n"
        "\n"
        "def load(row) -> Envelope:\n"
        "    return Envelope.model_validate(row)\n",
        encoding="utf-8",
    )
    assert hosted_claim_construction_offenders(tmp_path) == []
