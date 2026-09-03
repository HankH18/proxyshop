"""T-065 (trust side) -- the seam between the claim verifier and the trust engine.

The comparators themselves are graded in ``packages/verification/tests``. What is graded here
is everything *between* a ``VerificationResult`` and a moved Beta posterior, because that is
where D53's route can break without either end looking wrong:

**The import spelling.** ``packages.verification`` and ``claim_verification`` are two dotted
names onto one directory. Left alone, Python executes the files once per name and produces two
sets of classes, so an ``except InvalidVerificationStatus`` written against one spelling
silently stops catching the other's raise -- the failure is a caught exception becoming an
uncaught one, months later, in whichever service happened to import the other way. The
forwarding module exists to prevent exactly that, so the tests here assert *object identity*,
not equality: the same function object, the same module objects, the same exception classes.

**The route into a dimension.** A verified or contradicted claim becomes a trust observation
on the dimension the human-approved manifest maps its ``claim_type`` to. Product facts must
land on ``catalog_claim_accuracy`` and offer-integrity claims on their transaction dimension,
and -- the half that is easy to miss -- the dimensions that were *not* implicated must stay
bit-identically at the prior. A route that leaked evidence onto a neighbouring dimension would
still move the score in the right direction and would still pass a test that only looked at
the total.

**The untyped claim.** Where the verifier cannot honestly infer a ``claim_type`` it leaves it
``None``, and ``claim_dimension(None)`` raises. That loud failure is the point: a default
dimension would score a store for a failure it did not have.

**The approved golden set.** Every claim type the verifier produces for the golden set must be
in the manifest's routing table, every produced status is graded against the answer key across
all 13 pitches and all 36 claims with no allowance list, and every claim's approved
``expected_dimension`` must be where ``claim_dimension`` actually routes it. A divergence here
is a disagreement with human-approved ground truth and belongs in the comparator or in a new
approval -- never in a loosened assertion.

Pure data throughout: the verifier takes no clock, no socket and no model.
"""

from __future__ import annotations

import importlib
from typing import Any

import claim_verification
import pytest

import packages.verification as packages_verification
from apps.trust.src.scoring import (
    CATALOG_DIMENSION,
    DECIDING_OBSERVATION_TYPES,
    OBSERVATION_WEIGHTS,
    PRIOR_ALPHA,
    PRIOR_BETA,
    TRANSACTION_DIMENSIONS,
    TRUST_DIMENSIONS,
    UnmappedClaimType,
    claim_dimension,
    score,
)

#: The verifier generation these results were produced under. Recorded on every result so a
#: stored verdict can be invalidated by a comparator change; its value is not under test.
_VERIFIER_VERSION = "e6-seam-test"


def _claim(claim_ref: str, key: str, value: Any, **extra: Any) -> dict:
    """One atomic claim in the shape ``verify`` consumes."""
    return {"claim_ref": claim_ref, "key": key, "value": value, **extra}


def _observations(result: dict, *, as_of: str) -> list[dict]:
    """The D53 route: one verification result claim -> one trust observation.

    Two things happen here and nothing else, which is the whole point of the seam: the
    dimension comes from ``claim_dimension(claim_type)`` -- the human-approved routing table,
    consumed read-only -- and the observation *type* is the verification status verbatim,
    because the four statuses are themselves weighted observation types in the manifest. No
    translation table lives in between, so there is nowhere for a third opinion to form.
    """
    return [
        {
            "store_id": result["store_id"],
            "dim": claim_dimension(claim["claim_type"]),
            "type": claim["status"],
            "observed_at": as_of,
        }
        for claim in result["claims"]
    ]


def _by_ref(result: dict) -> dict[str, dict]:
    return {claim["claim_ref"]: claim for claim in result["claims"]}


def _assert_at_prior(dims: dict, names) -> None:
    """Every named dimension is bit-identically at the neutral prior."""
    for name in names:
        assert dims[name]["alpha"] == PRIOR_ALPHA, f"{name} took alpha it was never given"
        assert dims[name]["beta"] == PRIOR_BETA, f"{name} took beta it was never given"
        assert dims[name]["observations"] == 0, f"{name} was handed an observation"


def test_both_import_spellings_yield_the_same_verify_function():
    """``packages.verification.verify`` and ``claim_verification.verify`` are one object.

    Not "equal" and not "equivalent" -- the same function object. Two executions of the same
    file produce two functions that behave identically and two sets of classes that do not
    compare equal, and it is the classes that break silently. Asserting identity on the
    package's headline export is the cheapest way to notice that the forwarding module has
    stopped forwarding.
    """
    assert packages_verification.verify is claim_verification.verify
    assert packages_verification.satisfies_hard_constraint is (
        claim_verification.satisfies_hard_constraint
    )
    assert packages_verification.compare is claim_verification.compare


def test_every_submodule_is_one_object_across_both_spellings():
    """Each submodule resolves to a single module object under every spelling.

    Three spellings reach each file -- ``claim_verification.<mod>``,
    ``packages.verification.<mod>`` and ``packages.verification.src.<mod>`` -- and the
    ``sys.modules`` entry and the package *attribute* are separately necessary: an entry alone
    does not make ``import packages.verification.statuses`` followed by attribute access work.
    Both are exercised here, by attribute and by ``importlib.import_module``.
    """
    assert packages_verification.src is claim_verification

    for name in ("statuses", "comparators", "normalize", "verifier"):
        canonical = getattr(claim_verification, name)
        assert getattr(packages_verification, name) is canonical, f"{name} forked by attribute"
        assert getattr(packages_verification.src, name) is canonical, f"{name} forked under .src"
        assert importlib.import_module(f"packages.verification.{name}") is canonical
        assert importlib.import_module(f"packages.verification.src.{name}") is canonical
        assert importlib.import_module(f"claim_verification.{name}") is canonical


def test_an_exception_class_is_literally_the_same_object_across_spellings():
    """``except`` written against one spelling catches the other spelling's raise.

    This is the identity bug the forwarding module exists to prevent, stated as the thing that
    actually goes wrong. If the two spellings held two ``InvalidVerificationStatus`` classes,
    the ``except`` below would not catch the raise -- and nothing about either module would
    look wrong until a production traceback showed a handled error escaping.
    """
    assert (
        packages_verification.InvalidVerificationStatus
        is claim_verification.statuses.InvalidVerificationStatus
    )
    assert packages_verification.VerificationInputError is (
        claim_verification.verifier.VerificationInputError
    )
    assert (
        packages_verification.ComparisonOutcome is claim_verification.comparators.ComparisonOutcome
    )

    with pytest.raises(packages_verification.InvalidVerificationStatus):
        claim_verification.require_status("probably-fine")

    with pytest.raises(claim_verification.InvalidVerificationStatus):
        packages_verification.require_status("probably-fine")


def test_the_two_spellings_publish_the_same_public_surface():
    """``__all__`` agrees, so neither spelling can export something the other does not.

    The forwarding module copies ``__all__`` from the flat package. A hand-maintained second
    list would drift the first time an export was added, and the drift would show up as an
    ``ImportError`` in whichever service used the spelling that had not been updated.
    """
    assert list(packages_verification.__all__) == list(claim_verification.__all__)
    for name in claim_verification.__all__:
        assert getattr(packages_verification, name) is getattr(claim_verification, name)


def test_every_verification_status_is_a_weighted_trust_observation_type():
    """The four statuses are observation types the trust engine already weights.

    This is what lets the seam carry a status through verbatim instead of translating it. A
    status with no published weight would raise ``UnknownObservationType`` at score time --
    loudly, but only for the store unlucky enough to produce it first.
    """
    for status in claim_verification.VERIFICATION_STATUSES:
        assert status in OBSERVATION_WEIGHTS, f"status {status!r} has no published weight"


def test_every_approved_claim_type_routes_to_one_of_the_six_dimensions(e6_manifest):
    """Every key of the manifest's routing table resolves through ``claim_dimension``.

    The manifest is ground truth and the engine consumes it read-only, so the engine must be
    able to route every type ground truth publishes. A type in the approved table that the
    engine raised on would be a claim nobody could score -- the mirror image of a type the
    engine routes but nobody approved.
    """
    approved = e6_manifest["claim_type_dimensions"]

    assert approved, "the approved manifest publishes no claim_type -> dimension table"
    for claim_type, expected_dimension in approved.items():
        assert claim_dimension(claim_type) == expected_dimension
        assert expected_dimension in TRUST_DIMENSIONS


def test_a_product_fact_route_lands_positive_and_negative_evidence_on_the_sixth_dimension(
    e6_as_of, e6_catalog, e6_pitch
):
    """D53 end to end: a true and a false ``ingredients`` claim both reach the sixth dimension.

    The pitch carries one claim the catalog confirms and one it refuses, both typed
    ``ingredients``. Routed through ``claim_dimension`` and scored, the true one must add its
    published positive weight to ``catalog_claim_accuracy``'s alpha and the false one its
    published negative weight to that same dimension's beta -- and the five transaction
    dimensions must be left bit-identically at the prior. Without the last clause a route that
    smeared catalog evidence across every dimension would still pass.
    """
    pitch = e6_pitch(
        "e6-product-fact",
        [
            _claim("c-true", "ingredients", "water", claim_type="ingredients"),
            _claim("c-false", "ingredients", "retinol", op="contains", claim_type="ingredients"),
        ],
    )

    result = claim_verification.verify(pitch, e6_catalog(), _VERIFIER_VERSION)
    claims = _by_ref(result)
    assert claims["c-true"]["status"] == "verified"
    assert claims["c-false"]["status"] == "contradicted"
    assert {claim["claim_type"] for claim in result["claims"]} == {"ingredients"}

    snapshot = score(_observations(result, as_of=e6_as_of), as_of=e6_as_of)
    catalog = snapshot["dims"][CATALOG_DIMENSION]

    assert catalog["alpha"] == PRIOR_ALPHA + OBSERVATION_WEIGHTS["verified"]
    assert catalog["beta"] == PRIOR_BETA + OBSERVATION_WEIGHTS["contradicted"]
    assert catalog["observations"] == 2
    assert catalog["coverage"] == 1.0
    _assert_at_prior(snapshot["dims"], TRANSACTION_DIMENSIONS)


def test_an_offer_integrity_route_lands_on_price_honored_and_not_on_the_catalog_dimension(
    e6_as_of, e6_catalog, e6_pitch
):
    """A ``unit_price`` claim is an offer-integrity failure, not a catalog lie.

    The same route with the other half of the vocabulary. ``price_honored`` takes both the
    confirmed and the refused claim, and ``catalog_claim_accuracy`` stays at the prior --
    routing a dishonoured price onto the catalog dimension would make the sixth dimension a
    dumping ground and would make S2's "the trust engine catches the dishonest store" unable
    to say *what* it caught.
    """
    pitch = e6_pitch(
        "e6-offer-integrity",
        [
            _claim("c-true", "unit_price", "19.99", claim_type="unit_price"),
            _claim("c-false", "unit_price", "24.99", claim_type="unit_price"),
        ],
    )

    result = claim_verification.verify(pitch, e6_catalog(), _VERIFIER_VERSION)
    claims = _by_ref(result)
    assert claims["c-true"]["status"] == "verified"
    assert claims["c-false"]["status"] == "contradicted"
    assert claim_dimension("unit_price") == "price_honored"

    snapshot = score(_observations(result, as_of=e6_as_of), as_of=e6_as_of)

    assert (
        snapshot["dims"]["price_honored"]["alpha"] == PRIOR_ALPHA + OBSERVATION_WEIGHTS["verified"]
    )
    assert snapshot["dims"]["price_honored"]["beta"] == (
        PRIOR_BETA + OBSERVATION_WEIGHTS["contradicted"]
    )
    _assert_at_prior(
        snapshot["dims"],
        [name for name in TRUST_DIMENSIONS if name != "price_honored"],
    )


def test_an_undecided_outcome_costs_coverage_and_moves_no_dimension_mean(
    e6_as_of, e6_catalog, e6_pitch
):
    """An ``ambiguous`` outcome routes to its dimension and leaves the Beta pair untouched.

    R19/D53: the two undecided statuses are evidence about how much the verifier could see,
    not about the store. So they must reach the dimension -- they lower its coverage, which is
    what stops a seller buying a clean record by pitching only unfalsifiable claims -- while
    leaving ``(alpha, beta)`` bit-identical to the prior rather than merely close to it.
    """
    pitch = e6_pitch(
        "e6-undecided",
        [_claim("c-vague", "voltage", "works on your mains", claim_type="specifications")],
    )

    result = claim_verification.verify(pitch, e6_catalog(), _VERIFIER_VERSION)
    assert result["claims"][0]["status"] == "ambiguous"
    assert claim_dimension("specifications") == CATALOG_DIMENSION

    snapshot = score(_observations(result, as_of=e6_as_of), as_of=e6_as_of)
    catalog = snapshot["dims"][CATALOG_DIMENSION]

    assert catalog["alpha"] == PRIOR_ALPHA
    assert catalog["beta"] == PRIOR_BETA
    assert catalog["observations"] == 1
    assert catalog["coverage"] == 0.0
    assert snapshot["confidence"] < 0.1, "an undecided outcome must not buy confidence"


def test_an_untyped_claim_stays_untyped_and_raises_rather_than_defaulting(e6_catalog, e6_pitch):
    """The verifier leaves ``claim_type`` ``None``, and ``claim_dimension(None)`` raises.

    ``availability`` resolves out of the ``offer`` block, which implies no claim type: the
    offer keys that carry one are named explicitly, and guessing a type for the rest would
    route a claim to a dimension nobody approved. So the honest answer is ``None``, and the
    route then fails loudly instead of silently defaulting -- which is the entire value of
    calling the mapping "exhaustive".

    ``UnmappedClaimType`` is deliberately a ``LookupError`` and NOT a ``KeyError``, so a
    caller's ``except KeyError`` around a dictionary read cannot swallow a ground-truth gap.
    """
    pitch = e6_pitch(
        "e6-untyped",
        [_claim("c-availability", "availability", "in_stock")],
    )

    result = claim_verification.verify(pitch, e6_catalog(), _VERIFIER_VERSION)
    claim = result["claims"][0]

    assert claim["status"] == "verified", "the claim itself is checkable; only its type is not"
    assert claim["claim_type"] is None

    with pytest.raises(UnmappedClaimType) as raised:
        claim_dimension(claim["claim_type"])
    assert isinstance(raised.value, LookupError)
    assert not isinstance(raised.value, KeyError)
    assert "approved" in str(raised.value), (
        "the raise must say the routing table is ground truth, so the fix is an approval "
        "rather than a default this engine picks"
    )

    for empty in (None, "", "   "):
        with pytest.raises(UnmappedClaimType):
            claim_dimension(empty)


def test_every_claim_type_the_verifier_produces_for_the_golden_set_is_approved(
    e6_golden_set, e6_manifest
):
    """No golden claim routes to a dimension nobody approved.

    Every pitch and every claim in the approved golden set is verified, and each produced
    ``claim_type`` must be either ``None`` -- the honest "I cannot type this" -- or a key of
    the manifest's ``claim_type_dimensions``. A produced type outside that table is a claim the
    engine would route somewhere ground truth never sanctioned, which is exactly the silent
    failure the approval gate exists to stop.
    """
    approved = set(e6_manifest["claim_type_dimensions"])
    seen: set[str] = set()

    for pitch in e6_golden_set["pitches"]:
        result = claim_verification.verify(pitch, pitch["catalog_snapshot"], _VERIFIER_VERSION)
        for claim in result["claims"]:
            claim_type = claim["claim_type"]
            if claim_type is None:
                continue
            assert claim_type in approved, (
                f"{pitch['pitch_id']}/{claim['claim_ref']} produced unapproved "
                f"claim_type {claim_type!r}"
            )
            seen.add(claim_type)

    assert len(seen) >= 8, "the golden set must exercise a real spread of the approved table"


def test_the_golden_sets_expected_dimension_agrees_with_the_engines_route(e6_golden_set):
    """Each golden claim's approved ``expected_dimension`` is where the engine routes it.

    This is the seam graded against the answer key rather than against itself: the verifier
    produces a ``claim_type``, the engine's ``claim_dimension`` turns it into a dimension, and
    the human-approved document says which dimension that should be. The two sides never share
    a spelling -- ``expected_dimension`` is the key, the engine's return value is the produce.
    """
    for pitch in e6_golden_set["pitches"]:
        result = claim_verification.verify(pitch, pitch["catalog_snapshot"], _VERIFIER_VERSION)
        expected = {
            claim["claim_ref"]: claim.get("expected_dimension") for claim in pitch["claims"]
        }
        for claim in result["claims"]:
            wanted = expected[claim["claim_ref"]]
            if not wanted:
                continue
            assert claim_dimension(claim["claim_type"]) == wanted, (
                f"{pitch['pitch_id']}/{claim['claim_ref']} routes to the wrong dimension"
            )


def test_the_golden_sets_expected_statuses_are_what_the_verifier_produces(e6_golden_set):
    """Every claim of every approved golden pitch verifies to its ``expected_status``.

    All 13 pitches and all 36 claims, with no allowance list and no narrowing: the approved
    document is the answer key and the verifier is graded against it, whole. The two sides
    never share a spelling -- the key says ``expected_status`` and the verifier produces
    ``status`` -- so this cannot degenerate into comparing the verifier with itself.

    The claim count is pinned at 36 because the cheapest way to make a whole-set assertion
    vacuous is to stop iterating over most of the set; a golden pitch that lost its claims, or
    a verifier that returned fewer results than it was given, would otherwise pass silently.
    Every failure here is a real divergence between the comparator and human-approved ground
    truth, and belongs in the comparator or in an approval, never in this assertion.
    """
    divergences: dict[tuple[str, str], tuple[str, str]] = {}
    graded = 0

    for pitch in e6_golden_set["pitches"]:
        result = claim_verification.verify(pitch, pitch["catalog_snapshot"], _VERIFIER_VERSION)
        expected = {claim["claim_ref"]: claim["expected_status"] for claim in pitch["claims"]}
        assert len(result["claims"]) == len(pitch["claims"]), (
            f"{pitch['pitch_id']}: the verifier returned a different number of claims than the "
            "pitch carried, so some claim was never graded"
        )
        for claim in result["claims"]:
            graded += 1
            wanted = expected[claim["claim_ref"]]
            assert wanted in claim_verification.VERIFICATION_STATUSES
            if claim["status"] != wanted:
                divergences[(pitch["pitch_id"], claim["claim_ref"])] = (wanted, claim["status"])

    assert graded == 36, "the whole approved golden set must be graded, not a subset of it"
    assert divergences == {}, (
        "the verifier disagrees with the human-approved golden set, as "
        "{(pitch_id, claim_ref): (expected, produced)}"
    )


def test_the_golden_set_is_graded_on_more_than_one_status(e6_golden_set):
    """The answer key exercises all four statuses, so the whole-set assertion is not trivial.

    A key that expected ``verified`` everywhere would be satisfied by a verifier that returned
    a constant, and the test above would still be green. This asserts the approved document
    demands real discrimination -- every one of the four R18 statuses is expected somewhere,
    and each of them is actually produced.
    """
    expected = {
        claim["expected_status"] for pitch in e6_golden_set["pitches"] for claim in pitch["claims"]
    }
    produced = {
        claim["status"]
        for pitch in e6_golden_set["pitches"]
        for claim in claim_verification.verify(pitch, pitch["catalog_snapshot"], _VERIFIER_VERSION)[
            "claims"
        ]
    }

    assert expected == set(claim_verification.VERIFICATION_STATUSES)
    assert produced == set(claim_verification.VERIFICATION_STATUSES)


def test_satisfies_hard_constraint_is_true_for_verified_and_nothing_else():
    """Only ``verified`` may stand in for a fact the buyer refused to go without.

    R18/R19. ``unsupported`` and ``ambiguous`` reading as true turns "we could not check" into
    "we checked and it is fine", and every unfalsifiable pitch becomes a compliant one. An
    unrecognised status is not a reason to admit a claim either -- if anything it is a stronger
    reason to refuse it -- so it must be ``False`` rather than raise or default.
    """
    assert claim_verification.satisfies_hard_constraint("verified") is True
    for status in ("contradicted", "unsupported", "ambiguous"):
        assert claim_verification.satisfies_hard_constraint(status) is False
    for unknown in ("Verified", "VERIFIED", "probably-fine", "", None, True, 1):
        assert claim_verification.satisfies_hard_constraint(unknown) is False


def test_only_decided_statuses_carry_deciding_weight_into_the_trust_engine():
    """The statuses that decide something are the ones the engine counts as evidence.

    The two vocabularies are published independently -- ``DECIDED_STATUSES`` in the verifier,
    ``DECIDING_OBSERVATION_TYPES`` in the trust engine -- and the seam only works while they
    agree about which outcomes are evidence about the seller. ``unsupported`` and ``ambiguous``
    must be absent from the engine's deciding set for the same reason they cannot satisfy a
    hard constraint.
    """
    assert claim_verification.DECIDED_STATUSES <= DECIDING_OBSERVATION_TYPES
    for status in claim_verification.UNDECIDED_STATUSES:
        assert status not in DECIDING_OBSERVATION_TYPES
        assert claim_verification.is_decided(status) is False
