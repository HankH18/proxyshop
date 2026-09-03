"""T-062 — the trust engine's own gate: the numbers are the manifest's, and the maths is exact.

The engine under test makes four claims that are cheap to *state* and easy to break silently,
so each one is attacked here rather than demonstrated:

**"The constants are read, not chosen."** SPEC A3 is circular the moment the trust engine
authors the weights that catch the dishonest store. So every published constant is compared
against ``fixtures/manifest.json`` read straight off disk by a fixture the engine never
touches — and so are the ``PUBLISHED_*`` transcriptions, which are the values that go live
inside the deployed container (``apps/trust/Dockerfile`` does not copy ``fixtures/``). A drift
between transcription and ground truth has to be a red test here; the alternative is
discovering it as a production score nobody can explain.

**"Decay is exact at zero age."** ``contradicted`` adds *exactly* 2.0 only if
``decay_factor`` returns *exactly* 1.0, and the replay assertion in ``trust.ledger`` compares
with ``==``. So the exactness is asserted with ``==``, not ``approx``, and the clamp on a
future timestamp is asserted to be the same exact 1.0 — a writer with a skewed clock must not
be able to buy a bigger positive than a punctual one.

**"Undecided outcomes cost coverage, not mean."** ``ambiguous`` is required to leave
``(alpha, beta)`` bit-identical to the prior, and a snapshot of a hundred undecided outcomes
is required to sit on the confidence floor. A scorer that merely moved the mean "a little"
for an unfalsifiable pitch would pass a tolerance test and fail these.

**"Unknowns are loud, and blacklist reads fail closed."** Every ground-truth gap is asserted
to raise, and asserted NOT to be a ``KeyError`` — a caller's ``except KeyError`` around a
dictionary read must not be able to swallow "nobody approved this claim type". The blacklist
is attacked from the other direction: three separate ways of not knowing the answer are each
required to return ``True``, with a control proving the function can still say ``False``.

No I/O, no clock, no fixtures beyond the flat ``e6_`` namespace: the whole surface is pure,
which is why every assertion here can be an equality rather than a tolerance.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.trust.src.scoring import (
    BLACKLIST_STATUSES,
    BLACKLIST_THRESHOLD,
    BLOCKING_BLACKLIST_STATUSES,
    CATALOG_DIMENSION,
    CLAIM_TYPE_DIMENSIONS,
    CLAIM_TYPE_DIMENSIONS_SOURCE,
    CONFIDENCE_FLOOR,
    DECIDING_OBSERVATION_TYPES,
    HALF_LIFE_DAYS,
    NEW_STORE_PRIOR_N,
    OBSERVATION_POLARITY,
    OBSERVATION_WEIGHTS,
    PRIOR_ALPHA,
    PRIOR_BETA,
    PUBLISHED_CLAIM_TYPE_DIMENSIONS,
    PUBLISHED_OBSERVATION_WEIGHTS,
    SCORE_VERSION,
    TRANSACTION_DIMENSIONS,
    TRUST_DIMENSIONS,
    Blacklist,
    BlacklistEntry,
    InvalidBlacklistState,
    UnknownObservationType,
    UnknownTrustDimension,
    UnmappedClaimType,
    business_identity_of,
    claim_dimension,
    decay_factor,
    is_blacklisted,
    manifest_source,
    prior_snapshot,
    score,
)
from apps.trust.src.scoring.manifest import (
    MANIFEST_ENV_VAR,
    load_manifest,
    manifest_path,
    reset_manifest_cache,
)

#: ``apps/trust/tests/test_scoring.py`` -> the checkout root, the same hop count the shared
#: E6 fixture file uses to find the approved manifest.
REPO_ROOT = Path(__file__).resolve().parents[3]


def numeric_manifest_weights(manifest: dict) -> dict[str, float]:
    """The manifest's ``observation_weights`` with its prose ``comment`` entry dropped.

    The approved document carries a rationale string in the same object as the numbers, so
    the comparison target is the numeric subset — coercing the comment would be a crash, and
    ignoring the whole object would make the comparison vacuous.
    """
    return {
        str(key): float(value)
        for key, value in manifest["observation_weights"].items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def dim_of(snapshot: dict, name: str) -> dict:
    """One dimension's served sub-mapping, with a message that names it when it is absent."""
    dims = snapshot["dims"]
    assert name in dims, f"snapshot served no {name!r} dimension; it served {sorted(dims)}"
    return dims[name]


def shifted(as_of: str, *, days: float) -> str:
    """``as_of`` moved by ``days`` (negative = older), spelled the way observations are."""
    moment = datetime.fromisoformat(as_of.replace("Z", "+00:00")) + timedelta(days=days)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def dishonest_episodes(manifest: dict, store_id: str, as_of: str, episodes: int) -> list[dict]:
    """The manifest's dishonest script replayed ``episodes`` times, in recorded order."""
    behaviours = manifest["dishonest_store"]["behaviours"]
    return [
        {
            "store_id": store_id,
            "dim": behaviour["dim"],
            "type": behaviour["type"],
            "observed_at": as_of,
        }
        for _ in range(episodes)
        for behaviour in behaviours
    ]


# --------------------------------------------------------------------------------------
# 1. The published constants ARE the approved manifest's — including the deploy fallbacks
# --------------------------------------------------------------------------------------


def test_the_live_weight_table_is_the_manifests_weight_table(e6_manifest) -> None:
    """D18/A3: every observation weight the engine applies comes from the approved manifest.

    Compared against the document read off disk, not against the engine's own transcription:
    an engine that authored its own weights could still "catch" the dishonest store, and the
    claim would mean nothing.
    """
    assert dict(OBSERVATION_WEIGHTS) == numeric_manifest_weights(e6_manifest)
    assert OBSERVATION_WEIGHTS["contradicted"] > OBSERVATION_WEIGHTS["unsupported"] > 0.0
    assert OBSERVATION_WEIGHTS["ambiguous"] == 0.0
    assert OBSERVATION_WEIGHTS["severe_policy"] == max(OBSERVATION_WEIGHTS.values())


def test_the_published_fallback_weights_are_a_faithful_transcription(e6_manifest) -> None:
    """The deploy-time fallback equals ground truth, so a drift fails HERE and not in prod.

    ``apps/trust/Dockerfile`` does not copy ``fixtures/``, so ``PUBLISHED_OBSERVATION_WEIGHTS``
    is what the deployed container actually scores with. It is a transcription of the approved
    table, never a second opinion, and this is the only place the two are compared.
    """
    assert dict(PUBLISHED_OBSERVATION_WEIGHTS) == numeric_manifest_weights(e6_manifest)


def test_the_prior_half_life_threshold_and_low_data_n_are_the_manifests(e6_manifest) -> None:
    """The Beta(2,2) prior, the half-life, the blacklist line and ``new_store_prior_n`` are read.

    Each of these four moves a served number or a delisting decision, so each is checked
    against the approved document rather than against a literal in the engine.
    """
    assert PRIOR_ALPHA == float(e6_manifest["trust_prior"]["alpha"]) == 2.0
    assert PRIOR_BETA == float(e6_manifest["trust_prior"]["beta"]) == 2.0
    assert HALF_LIFE_DAYS == float(e6_manifest["half_life_days"])
    assert BLACKLIST_THRESHOLD == float(e6_manifest["blacklist_threshold"])
    assert NEW_STORE_PRIOR_N == int(e6_manifest["new_store_prior_n"])
    assert 0.0 < BLACKLIST_THRESHOLD < PRIOR_ALPHA / (PRIOR_ALPHA + PRIOR_BETA) < 1.0


def test_the_claim_routing_table_and_its_fallback_are_both_the_manifests(e6_manifest) -> None:
    """T-080: the ``claim_type -> dimension`` routing is human-approved, in both spellings.

    The live table must be the manifest's, and the published transcription that takes over
    when ``fixtures/`` is absent must agree with it entry for entry — a fallback that routed
    ``ingredients`` anywhere but the catalog dimension would rescore product-fact dishonesty
    as a delivery failure, in production only.
    """
    approved = {str(k): str(v) for k, v in e6_manifest["claim_type_dimensions"].items()}
    assert dict(CLAIM_TYPE_DIMENSIONS) == approved
    assert dict(PUBLISHED_CLAIM_TYPE_DIMENSIONS) == approved


def test_the_manifests_outcome_treatment_agrees_with_polarity_and_evidence(e6_manifest) -> None:
    """``claim_outcome_treatment`` is a second, independent statement of the same rules.

    The manifest says each outcome's weight, whether it moves the mean, and whether it counts
    as evidence. The engine says the same three things through ``OBSERVATION_WEIGHTS``,
    ``OBSERVATION_POLARITY`` and ``DECIDING_OBSERVATION_TYPES``. They are cross-checked here
    because a change to one half of the approved document that missed the other would leave
    the engine consistent with itself and wrong about ground truth.
    """
    treatment = e6_manifest["claim_outcome_treatment"]
    assert treatment, "the approved manifest published no claim_outcome_treatment block"
    for outcome, rules in treatment.items():
        assert OBSERVATION_WEIGHTS[outcome] == float(rules["weight"]), outcome
        moves_mean = OBSERVATION_POLARITY[outcome] != "neutral"
        assert moves_mean is bool(rules["moves_mean"]), outcome
        decides = outcome in DECIDING_OBSERVATION_TYPES
        assert decides is bool(rules["counts_as_evidence"]), outcome
        expected_side = {"positive": "positive", "negative": "negative", "none": "neutral"}
        assert OBSERVATION_POLARITY[outcome] == expected_side[rules["observation"]], outcome


def test_the_vocabulary_is_the_six_and_no_claim_type_routes_to_feedback_match(e6_dims) -> None:
    """D53: six dimensions, and ``feedback_match`` is deliberately unreachable from a claim.

    ``feedback_match`` is buyer-reported (R14) and takes no verification outcome at all — the
    manifest says so in ``claim_type_dimensions_meta``. Routing a product fact there would
    score catalog dishonesty as a buyer complaint that never happened, so the *absence* is
    asserted rather than left to be noticed.
    """
    assert tuple(TRUST_DIMENSIONS) == tuple(e6_dims)
    assert len(set(TRUST_DIMENSIONS)) == 6
    assert CATALOG_DIMENSION == "catalog_claim_accuracy"
    assert set(TRANSACTION_DIMENSIONS) == set(e6_dims) - {CATALOG_DIMENSION}
    routed = set(CLAIM_TYPE_DIMENSIONS.values())
    assert "feedback_match" not in routed
    assert routed == set(e6_dims) - {"feedback_match"}


# --------------------------------------------------------------------------------------
# 9. The manifest actually in use is this checkout's approved document
# --------------------------------------------------------------------------------------


def test_the_live_manifest_is_this_checkouts_approved_document(e6_manifest) -> None:
    """The engine is reading ``fixtures/manifest.json``, not quietly running on its fallback.

    Every other assertion in this file about "the constants are the manifest's" is worth
    nothing if the fallback happened to be live and happened to agree. So the source is
    pinned: the real file, in this checkout, with the same bytes the fixture read.
    """
    expected = (REPO_ROOT / "fixtures" / "manifest.json").resolve()
    assert manifest_path() == expected
    assert manifest_source() == str(expected)
    assert manifest_source() != "published-fallback"
    assert CLAIM_TYPE_DIMENSIONS_SOURCE == "manifest"
    assert dict(load_manifest()) == e6_manifest


def test_an_unreachable_manifest_falls_back_without_inventing_ground_truth(monkeypatch) -> None:
    """The absent-``fixtures/`` path degrades to the published transcription and SAYS so.

    This is the deployed container's path. It has to keep working — a scoring module that
    required the document on disk would pass every test in this repo and fail to import
    inside the image — and it has to be visible, so ``manifest_source`` names the fallback
    rather than a file that is not there.
    """
    try:
        monkeypatch.setenv(MANIFEST_ENV_VAR, str(REPO_ROOT / "fixtures" / "no-such-manifest.json"))
        reset_manifest_cache()
        assert manifest_path() is None
        assert manifest_source() == "published-fallback"
        assert dict(load_manifest()) == {}
    finally:
        monkeypatch.undo()
        reset_manifest_cache()
    assert manifest_source() == str((REPO_ROOT / "fixtures" / "manifest.json").resolve())


# --------------------------------------------------------------------------------------
# 2. Time decay
# --------------------------------------------------------------------------------------


def test_a_contemporaneous_observation_decays_by_exactly_one(e6_as_of) -> None:
    """Exactly 1.0, not approximately: the replay assertion compares scores with ``==``.

    ``trust.ledger.replay`` asserts that a replayed snapshot equals the served one bit for
    bit. That only holds if an observation recorded at ``as_of`` contributes its published
    weight unrounded, so the exactness is asserted as an identity of floats.
    """
    assert decay_factor(e6_as_of, e6_as_of) == 1.0
    assert decay_factor(None, e6_as_of) == 1.0
    assert decay_factor("", e6_as_of) == 1.0


def test_one_half_life_halves_the_weight_and_two_quarters_it(e6_as_of) -> None:
    """``0.5 ** (age / half_life)``, checked at the two ages where the answer is exact.

    The half-life is taken from the published constant rather than hard-coded at 30 days, so
    this test grades the arithmetic and the earlier test grades the number.
    """
    assert decay_factor(shifted(e6_as_of, days=-HALF_LIFE_DAYS), e6_as_of) == 0.5
    assert decay_factor(shifted(e6_as_of, days=-2 * HALF_LIFE_DAYS), e6_as_of) == 0.25
    ages = [decay_factor(shifted(e6_as_of, days=-d), e6_as_of) for d in (0, 5, 20, 60, 365)]
    assert ages == sorted(ages, reverse=True), "decay is not monotonically decreasing in age"
    assert ages[-1] > 0.0


def test_a_future_timestamp_is_clamped_and_cannot_inflate_a_score(e6_as_of, e6_obs) -> None:
    """A skewed clock on one writer must not buy a store a bigger positive than a punctual one.

    Age is floored at zero, so an observation recorded *after* the reference instant decays by
    the same exact 1.0 as a contemporaneous one — asserted end-to-end through ``score`` and
    not only on ``decay_factor``, because the clamp is only worth anything where it is used.
    """
    assert decay_factor(shifted(e6_as_of, days=90), e6_as_of) == 1.0
    future = score(
        [e6_obs("s-1", "price_honored", "verified", shifted(e6_as_of, days=90))], as_of=e6_as_of
    )
    now = score([e6_obs("s-1", "price_honored", "verified")], as_of=e6_as_of)
    assert future == now


def test_an_unparseable_timestamp_is_refused_rather_than_read_as_now(e6_as_of, e6_obs) -> None:
    """Decay is a function of recorded time, so an unreadable instant is a hard error.

    Treating it as "now" would make a replay differ from the serve for a reason no assertion
    could name — the failure would surface as an unexplained score, days later.
    """
    with pytest.raises(ValueError, match="RFC-3339"):
        decay_factor("last tuesday", e6_as_of)
    with pytest.raises(ValueError, match="RFC-3339"):
        score([e6_obs("s-1", "price_honored", "verified", "2026-13-45")], as_of=e6_as_of)


def test_an_old_observation_moves_the_score_strictly_less_than_a_fresh_one(
    e6_as_of, e6_obs
) -> None:
    """Decay has to be visible in the served score, not merely in ``decay_factor``.

    Both observations are the same contradiction on the same dimension; only the recorded time
    differs. The old one must still move the score (it is evidence) and must move it strictly
    less (it is stale), which pins the direction a regression would most plausibly invert.
    """
    prior = prior_snapshot(as_of=e6_as_of)["score"]
    fresh = score([e6_obs("s-1", "price_honored", "contradicted")], as_of=e6_as_of)["score"]
    aged_at = shifted(e6_as_of, days=-4 * HALF_LIFE_DAYS)
    aged = score([e6_obs("s-1", "price_honored", "contradicted", aged_at)], as_of=e6_as_of)["score"]
    assert fresh < aged < prior


def test_a_fresh_contradiction_adds_exactly_the_published_weight_to_beta(e6_as_of, e6_obs) -> None:
    """The consequence of the exact 1.0: ``beta`` lands on ``PRIOR_BETA + 2.0``, to the bit.

    This is the assertion the "exactly 1.0" rule exists to make possible, and the one that
    would break first if decay were ever computed against a wall clock.
    """
    snapshot = score([e6_obs("s-1", "price_honored", "contradicted")], as_of=e6_as_of)
    entry = dim_of(snapshot, "price_honored")
    assert entry["beta"] == PRIOR_BETA + OBSERVATION_WEIGHTS["contradicted"]
    assert entry["alpha"] == PRIOR_ALPHA
    assert entry["evidence"] == OBSERVATION_WEIGHTS["contradicted"]


# --------------------------------------------------------------------------------------
# 3. Per-dimension isolation, both directions
# --------------------------------------------------------------------------------------


def test_a_transaction_observation_leaves_the_catalog_dimension_at_the_prior(
    e6_as_of, e6_obs
) -> None:
    """A dishonoured price is not a false product fact, and must not stain the sixth dimension.

    D53 gives ``catalog_claim_accuracy`` its own home precisely so a price failure and an
    ingredient lie are told apart; a scorer that spread one observation across dimensions
    would satisfy an aggregate-score test and fail this one.
    """
    snapshot = score(
        [
            e6_obs("s-1", "price_honored", "contradicted"),
            e6_obs("s-1", "price_honored", "severe_policy"),
        ],
        as_of=e6_as_of,
    )
    catalog = dim_of(snapshot, CATALOG_DIMENSION)
    assert (catalog["alpha"], catalog["beta"]) == (PRIOR_ALPHA, PRIOR_BETA)
    assert catalog["observations"] == 0
    assert catalog["coverage"] == 0.0
    assert dim_of(snapshot, "price_honored")["observations"] == 2


def test_a_catalog_observation_leaves_all_five_transaction_dimensions_at_the_prior(
    e6_as_of, e6_obs
) -> None:
    """The mirror image: a false ingredient claim is not a late parcel or a refused return.

    Asserted across all five, because a leak into exactly one of them is the shape a
    copy-pasted dimension name would take.
    """
    snapshot = score([e6_obs("s-1", CATALOG_DIMENSION, "contradicted")], as_of=e6_as_of)
    for name in TRANSACTION_DIMENSIONS:
        entry = dim_of(snapshot, name)
        assert (entry["alpha"], entry["beta"]) == (PRIOR_ALPHA, PRIOR_BETA), name
        assert entry["observations"] == 0, name
    assert dim_of(snapshot, CATALOG_DIMENSION)["beta"] > PRIOR_BETA


# --------------------------------------------------------------------------------------
# 4. Coverage and confidence
# --------------------------------------------------------------------------------------


def test_ambiguous_leaves_the_beta_bit_identical_and_only_costs_coverage(e6_as_of, e6_obs) -> None:
    """R19/D53: an undecided outcome decides nothing — ZERO mean movement, coverage only.

    "Bit-identical" is the requirement, not "close": a scorer that nudged the mean for an
    unfalsifiable pitch would let a seller buy a clean record by pitching only claims nobody
    can check, and would still pass any tolerance-based test.
    """
    snapshot = score(
        [e6_obs("s-1", "price_honored", "ambiguous") for _ in range(3)], as_of=e6_as_of
    )
    entry = dim_of(snapshot, "price_honored")
    assert entry["alpha"] == PRIOR_ALPHA
    assert entry["beta"] == PRIOR_BETA
    assert entry["observations"] == 3
    assert entry["decided"] == 0
    assert entry["coverage"] == 0.0
    assert snapshot["score"] == prior_snapshot(as_of=e6_as_of)["score"]

    mixed = score(
        [
            e6_obs("s-1", "price_honored", "verified"),
            e6_obs("s-1", "price_honored", "ambiguous"),
        ],
        as_of=e6_as_of,
    )
    mixed_entry = dim_of(mixed, "price_honored")
    assert mixed_entry["alpha"] == PRIOR_ALPHA + OBSERVATION_WEIGHTS["verified"]
    assert mixed_entry["beta"] == PRIOR_BETA
    assert mixed_entry["coverage"] == 0.5


def test_unsupported_adds_only_to_beta_and_never_counts_as_evidence(e6_as_of, e6_obs) -> None:
    """ "No evidence either way" is a SMALL published negative whose real cost is confidence.

    It must not touch ``alpha`` (that would reward a thin catalog) and must not enter the
    evidence mass (that would let an unverifiable pitch buy confidence in a score it did not
    earn).
    """
    snapshot = score(
        [e6_obs("s-1", "shipped_on_time", "unsupported") for _ in range(4)], as_of=e6_as_of
    )
    entry = dim_of(snapshot, "shipped_on_time")
    assert entry["alpha"] == PRIOR_ALPHA
    assert entry["beta"] == PRIOR_BETA + 4 * OBSERVATION_WEIGHTS["unsupported"]
    assert entry["decided"] == 0
    assert entry["evidence"] == 0.0
    assert entry["coverage"] == 0.0
    assert "unsupported" not in DECIDING_OBSERVATION_TYPES
    assert snapshot["confidence"] == CONFIDENCE_FLOOR


def test_coverage_is_a_share_in_the_unit_interval_on_every_snapshot(e6_as_of, e6_obs) -> None:
    """Coverage is a share of observations, so it can never leave ``[0, 1]`` on any dimension.

    Checked on the prior, on an all-decided snapshot, on an all-undecided one and on a mixed
    one, because a division that used the wrong denominator would stay inside the range for
    some of those and not others.
    """
    mixed = [
        e6_obs("s-1", dim, otype)
        for dim in TRUST_DIMENSIONS
        for otype in ("verified", "ambiguous", "unsupported", "contradicted")
    ]
    snapshots = [
        prior_snapshot(as_of=e6_as_of),
        score([e6_obs("s-1", d, "verified") for d in TRUST_DIMENSIONS], as_of=e6_as_of),
        score([e6_obs("s-1", d, "ambiguous") for d in TRUST_DIMENSIONS], as_of=e6_as_of),
        score(mixed, as_of=e6_as_of),
    ]
    for snapshot in snapshots:
        for name, entry in snapshot["dims"].items():
            assert 0.0 <= entry["coverage"] <= 1.0, (name, entry["coverage"])
        assert 0.0 < snapshot["score"] < 1.0
        assert CONFIDENCE_FLOOR <= snapshot["confidence"] < 1.0
    assert dim_of(snapshots[3], "price_honored")["coverage"] == 0.5


def test_a_brand_new_store_sits_on_the_confidence_floor(e6_as_of) -> None:
    """ "We know nothing" is the floor, never zero — and never the prior's own score.

    A zero would collapse every downstream multiplication by confidence, and the prior is
    deliberately interior so that *confidence*, not *score*, is what says a store is new.
    """
    snapshot = prior_snapshot(as_of=e6_as_of)
    assert snapshot["confidence"] == CONFIDENCE_FLOOR
    assert CONFIDENCE_FLOOR > 0.0
    assert snapshot["score"] == PRIOR_ALPHA / (PRIOR_ALPHA + PRIOR_BETA)
    assert snapshot["observations"] == 0
    assert snapshot["effective_evidence"] == 0.0
    assert snapshot == score((), as_of=e6_as_of)


def test_confidence_rises_strictly_as_deciding_evidence_accumulates(e6_as_of, e6_obs) -> None:
    """More decided evidence, more confidence — strictly, monotonically, and never reaching 1.

    Strict monotonicity is the property that makes confidence usable for ranking at all; a
    saturating curve that plateaued early would report a twenty-episode store as no better
    known than a one-episode store.
    """
    observed = [
        score([e6_obs("s-1", "price_honored", "verified")] * n, as_of=e6_as_of)["confidence"]
        for n in range(0, 21)
    ]
    assert observed[0] == CONFIDENCE_FLOOR
    for earlier, later in zip(observed[:-1], observed[1:], strict=True):
        assert later > earlier
    assert observed[-1] < 1.0


def test_a_snapshot_of_only_undecided_outcomes_never_leaves_the_floor(e6_as_of, e6_obs) -> None:
    """A hundred outcomes that decided nothing are a hundred observations and no confidence.

    This is the anti-gaming property: a seller who pitches only unfalsifiable claims
    accumulates volume, not credibility. Volume is asserted alongside the floor so the test
    cannot pass by the observations having been dropped instead of discounted.
    """
    undecided = [
        e6_obs("s-1", dim, otype)
        for dim in TRUST_DIMENSIONS
        for otype in ("ambiguous", "unsupported")
        for _ in range(10)
    ]
    snapshot = score(undecided, as_of=e6_as_of)
    assert snapshot["observations"] == len(undecided) == 120
    assert snapshot["confidence"] == CONFIDENCE_FLOOR
    assert snapshot["effective_evidence"] == 0.0
    assert all(entry["coverage"] == 0.0 for entry in snapshot["dims"].values())


# --------------------------------------------------------------------------------------
# 5. Determinism and serialisation
# --------------------------------------------------------------------------------------


def test_the_same_observations_always_produce_the_same_snapshot(e6_as_of, e6_obs) -> None:
    """S3: two paths over one stream produce identical floats, so replay can compare with ``==``.

    Also driven from a one-shot generator, because a scorer that iterated its input twice
    would pass the list-versus-list comparison and silently score an empty stream when
    ``replay`` handed it a cursor.
    """
    observations = [
        e6_obs("s-1", "price_honored", "contradicted", shifted(e6_as_of, days=-3)),
        e6_obs("s-1", CATALOG_DIMENSION, "verified"),
        e6_obs("s-1", "feedback_match", "mismatch_return", shifted(e6_as_of, days=-11)),
    ]
    first = score(observations, as_of=e6_as_of)
    second = score(observations, as_of=e6_as_of)
    assert first == second
    assert score(iter(observations), as_of=e6_as_of) == first
    assert score(tuple(observations), as_of=e6_as_of) == first
    assert observations[0]["type"] == "contradicted", "score() mutated its input"


def test_a_snapshot_survives_a_json_round_trip_unchanged(e6_as_of, e6_obs) -> None:
    """``/events/replay`` serialises this mapping, so a non-JSON value in it is an outage.

    Round-tripped rather than merely dumped: a value that serialised to something that did not
    read back equal (a set rendered as a list, a tuple key) would still pass ``json.dumps``.
    """
    snapshot = score(
        [e6_obs("s-1", dim, "contradicted") for dim in TRUST_DIMENSIONS], as_of=e6_as_of
    )
    encoded = json.dumps(snapshot)
    assert json.loads(encoded) == snapshot
    assert json.loads(encoded)["score_version"] == SCORE_VERSION


def test_every_snapshot_serves_all_six_dimensions_and_one_normalised_instant(
    e6_as_of, e6_obs, e6_dims
) -> None:
    """A dimension with no observations is served at the prior, never omitted.

    A consumer must never have to tell "clean" from "absent", and every ``decayed_at`` on the
    snapshot must be the one reference instant — a per-dimension clock would make two
    dimensions of one snapshot incomparable.
    """
    snapshot = score([e6_obs("s-1", "not_returned", "severe_policy")], as_of=e6_as_of)
    assert set(snapshot["dims"]) == set(e6_dims)
    assert snapshot["score_version"] == SCORE_VERSION
    assert snapshot["as_of"] == snapshot["decayed_at"] == "2026-01-01T00:00:00.000Z"
    assert all(entry["decayed_at"] == snapshot["as_of"] for entry in snapshot["dims"].values())
    expected = sum(
        entry["alpha"] / (entry["alpha"] + entry["beta"]) for entry in snapshot["dims"].values()
    ) / len(e6_dims)
    assert snapshot["score"] == expected


# --------------------------------------------------------------------------------------
# 6. Loud failures — a ground-truth gap needs a human, not a retry
# --------------------------------------------------------------------------------------


def test_an_observation_naming_a_seventh_dimension_is_refused(e6_as_of, e6_obs) -> None:
    """The vocabulary is closed (D53): a seventh dimension is a SPEC change, not a data value.

    Silently dropping it would make the observation vanish from every served snapshot, so the
    dishonest behaviour it recorded would score as a clean record. Near-misses are included:
    an untrimmed name and a missing ``dim`` are the two most likely ways this arrives.
    """
    for bad in ("shipping_cost_honored", " price_honored", "PRICE_HONORED", "", None, 42):
        with pytest.raises(UnknownTrustDimension):
            score([{"store_id": "s-1", "dim": bad, "type": "verified"}], as_of=e6_as_of)
    with pytest.raises(UnknownTrustDimension):
        score([{"store_id": "s-1", "type": "verified"}], as_of=e6_as_of)
    assert score([e6_obs("s-1", "price_honored", "verified")], as_of=e6_as_of)["observations"] == 1


def test_an_observation_type_with_no_published_weight_is_refused(e6_as_of) -> None:
    """An unweighted type is a manifest gap; weighting it here would be the engine choosing.

    Dropping it is the failure that matters: a dishonest behaviour recorded under a type
    nobody approved would leave no trace at all in the score meant to catch it.
    """
    for bad in ("definitely_verified", "VERIFIED", "contradicted ", ""):
        with pytest.raises(UnknownObservationType):
            score([{"store_id": "s-1", "dim": "price_honored", "type": bad}], as_of=e6_as_of)
    with pytest.raises(UnknownObservationType):
        score([{"store_id": "s-1", "dim": "price_honored"}], as_of=e6_as_of)


def test_an_unmapped_claim_type_raises_instead_of_defaulting(e6_manifest) -> None:
    """ "Exhaustive" is worth exactly what the raise is worth.

    A silent default would start penalising whichever dimension the default happened to name,
    and the approved table would still look correct. The known types are checked against the
    manifest in the same test so the raise cannot be coming from an empty table.
    """
    for claim_type, dimension in e6_manifest["claim_type_dimensions"].items():
        assert claim_dimension(claim_type) == dimension
    assert claim_dimension("  unit_price  ") == "price_honored"
    for bad in ("", "   ", None, "colour", "price_honored"):
        with pytest.raises(UnmappedClaimType):
            claim_dimension(bad)


def test_a_ground_truth_gap_cannot_be_swallowed_by_except_key_error() -> None:
    """These three are ``LookupError`` and deliberately NOT ``KeyError``.

    Every one of them is raised from inside what looks like a dictionary read, so a caller's
    ``except KeyError`` around ``claim_dimension`` or ``score`` would convert "nobody approved
    this" into a shrug. The negative assertion is the whole point of the class hierarchy.
    """
    for error in (UnmappedClaimType, UnknownObservationType, UnknownTrustDimension):
        assert issubclass(error, LookupError), error
        assert not issubclass(error, KeyError), error
    try:
        claim_dimension("colour")
    except KeyError:  # pragma: no cover - would mean the hierarchy regressed
        pytest.fail("UnmappedClaimType was caught by except KeyError")
    except UnmappedClaimType as exc:
        assert "colour" in str(exc)


# --------------------------------------------------------------------------------------
# 7. S2/A3 — the scripted dishonest store, and the honest control
# --------------------------------------------------------------------------------------


def test_the_scripted_dishonest_store_crosses_the_line_well_inside_its_budget(
    e6_manifest, e6_as_of
) -> None:
    """S2: the manifest's own script falls below the published threshold before the budget ends.

    Two things are asserted beyond "it ends up below": the store STARTS above the line (a
    scorer whose prior was already a delisting would prove nothing), and the crossing happens
    at a strictly earlier episode than the budget allows, so the margin is a number in the
    failure message rather than a coin flip on the last episode.
    """
    budget = int(e6_manifest["episode_budget"])
    assert budget > 1
    store_id = e6_manifest["dishonest_store"]["store_id"]
    threshold = float(BLACKLIST_THRESHOLD)

    assert prior_snapshot(as_of=e6_as_of)["score"] > threshold

    trajectory = [
        score(dishonest_episodes(e6_manifest, store_id, e6_as_of, ep), as_of=e6_as_of)["score"]
        for ep in range(1, budget + 1)
    ]
    crossed = [ep for ep, value in enumerate(trajectory, start=1) if value < threshold]
    assert crossed, f"the dishonest script never crossed {threshold}; trajectory {trajectory}"
    first = crossed[0]
    assert first < budget, (
        f"the dishonest script only crossed {threshold} at episode {first} of a {budget}-episode "
        f"budget — no margin left; trajectory {trajectory}"
    )
    assert trajectory[-1] < threshold
    assert trajectory == sorted(trajectory, reverse=True), "the dishonest trajectory rose"


def test_an_honest_control_running_the_same_budget_stays_above_the_line(
    e6_manifest, e6_as_of, e6_obs
) -> None:
    """The other half of S2: the threshold test must not be satisfiable by sinking everybody.

    The control runs the same number of episodes on the same dimensions with the same
    observation count — only the outcomes differ — so the difference measured is dishonesty
    and not volume.
    """
    budget = int(e6_manifest["episode_budget"])
    behaviours = e6_manifest["dishonest_store"]["behaviours"]
    honest = [
        e6_obs("store-northroast", behaviour["dim"], "verified")
        for _ in range(budget)
        for behaviour in behaviours
    ]
    dishonest = dishonest_episodes(e6_manifest, "store-brightbean", e6_as_of, budget)
    control = score(honest, as_of=e6_as_of)
    condemned = score(dishonest, as_of=e6_as_of)

    assert control["observations"] == condemned["observations"]
    assert control["score"] > float(BLACKLIST_THRESHOLD)
    assert control["score"] > prior_snapshot(as_of=e6_as_of)["score"]
    assert control["confidence"] > CONFIDENCE_FLOOR


# --------------------------------------------------------------------------------------
# 8. The blacklist: identity-bound, stateful, fail-closed
# --------------------------------------------------------------------------------------


def test_a_blacklisting_follows_the_business_not_the_store_id(e6_manifest) -> None:
    """R12: a re-registration under a fresh ``store_id`` is the same business, still blocked.

    Keying on ``store_id`` makes the blacklist cost a bad actor one signup. The identity used
    here is the manifest's own dishonest store, so the test is bound to ground truth rather
    than to a name this file invented.
    """
    listed = e6_manifest["dishonest_store"]
    identity = listed["business_identity"]
    blacklist = Blacklist()
    blacklist.add(business_identity=identity, reason_code="s2_below_threshold")

    assert is_blacklisted(
        blacklist, {"store_id": listed["store_id"], "business_identity": identity}
    )
    assert is_blacklisted(
        blacklist, {"store_id": "store-brightbean-2", "business_identity": identity}
    )
    assert is_blacklisted(blacklist, SimpleNamespace(store_id="s-9", business_identity=identity))
    assert is_blacklisted(blacklist, identity)
    assert not is_blacklisted(blacklist, {"business_identity": "north-roast-collective-llc"})
    assert business_identity_of({"business_identity": "  x  "}) == "x"
    assert business_identity_of({"business_identity": "   "}) is None
    assert business_identity_of(SimpleNamespace(other=1)) is None


def test_all_four_recorded_states_round_trip_through_lookup() -> None:
    """A delisting has a lifecycle, so the recorded decision has to survive being read back.

    ``reason_code``, ``expires_at`` and ``note`` come back with the entry: a blacklist you
    cannot explain is one nobody will maintain, and an appeal that lost its reason code is an
    appeal nobody can decide.
    """
    assert set(BLACKLIST_STATUSES) == {"active", "under_review", "appealed", "expired"}
    blacklist = Blacklist()
    for status in BLACKLIST_STATUSES:
        blacklist.add(
            business_identity=f"biz-{status}",
            reason_code=f"reason-{status}",
            status=status,
            expires_at="2026-08-01T00:00:00Z",
            note=f"note-{status}",
        )
    for status in BLACKLIST_STATUSES:
        entry = blacklist.lookup(f"biz-{status}")
        assert isinstance(entry, BlacklistEntry)
        assert entry.status == status
        assert entry.reason_code == f"reason-{status}"
        assert entry.expires_at == "2026-08-01T00:00:00Z"
        assert entry.note == f"note-{status}"
        assert entry.blocking is (status != "expired")
    assert blacklist.lookup("biz-nobody") is None


def test_expired_is_the_only_state_that_clears_the_block() -> None:
    """ "We have not finished deciding" is not "allowed" — that is fail-open in slow motion.

    ``under_review`` and ``appealed`` both block, because a store mid-appeal has not been
    cleared and the exchange should not be routing buyers to it in the meantime.
    """
    assert BLOCKING_BLACKLIST_STATUSES == set(BLACKLIST_STATUSES) - {"expired"}
    for status in BLACKLIST_STATUSES:
        blacklist = Blacklist()
        blacklist.add(business_identity="biz-1", reason_code="r", status=status)
        blocked = is_blacklisted(blacklist, {"business_identity": "biz-1"})
        assert blocked is (status != "expired"), status


def test_an_unrecognised_state_is_refused_at_write_time(e6_manifest) -> None:
    """A typo'd state would read as "not blocking", so it is refused where a human can see it.

    Nothing is recorded when the write is refused: a half-written entry would be the same
    fail-open outcome with an audit trail suggesting otherwise. The accepted vocabulary is the
    same four the ``app.seller_blacklist`` CHECK constraint enforces.
    """
    blacklist = Blacklist()
    for bad in ("banned", "", "ACTIVE", "expired ", "deleted"):
        with pytest.raises(InvalidBlacklistState):
            blacklist.add(business_identity="biz-1", reason_code="r", status=bad)
    assert len(blacklist) == 0
    assert blacklist.lookup("biz-1") is None
    assert issubclass(InvalidBlacklistState, ValueError)
    assert e6_manifest["stores"], "the approved manifest published no stores to key against"


def test_every_way_of_not_knowing_the_answer_reads_as_blocked() -> None:
    """R12 fail-closed: an unknown is a refusal, in all three shapes it arrives in.

    An outage in the store holding the blacklist, a drop-in object that does not implement the
    lookup at all, and a store record with no business identity are each a case where the
    system cannot answer — and answering "not blacklisted" would admit precisely the seller
    the failure happens to be hiding. The control at the end proves the function can still
    say ``False``, so the test is not passing by returning ``True`` unconditionally.
    """

    class Exploding:
        def lookup(self, business_identity: str) -> None:
            raise RuntimeError("blacklist store unreachable")

    store = {"store_id": "s-1", "business_identity": "biz-1"}
    assert is_blacklisted(Exploding(), store) is True
    assert is_blacklisted(object(), store) is True
    assert is_blacklisted(Blacklist(), {"store_id": "s-1"}) is True
    assert is_blacklisted(Blacklist(), {"store_id": "s-1", "business_identity": None}) is True
    assert is_blacklisted(Blacklist(), {"store_id": "s-1", "business_identity": "  "}) is True
    assert is_blacklisted(Blacklist(), SimpleNamespace(store_id="s-1")) is True
    assert is_blacklisted(Blacklist(), store) is False


def test_the_listing_behaves_like_the_collection_it_claims_to_be() -> None:
    """Membership, length, removal and a deterministic identity listing.

    ``identities()`` is sorted because snapshots and diffs are compared across runs; an
    insertion-ordered listing would make an unrelated ordering change look like a delisting.
    """
    blacklist = Blacklist()
    blacklist.add(business_identity="zeta-llc", reason_code="r1")
    blacklist.add(business_identity="alpha-llc", reason_code="r2", status="appealed")
    assert len(blacklist) == 2
    assert "alpha-llc" in blacklist
    assert "nobody-llc" not in blacklist
    assert blacklist.identities() == ("alpha-llc", "zeta-llc")
    assert {entry.business_identity for entry in blacklist} == {"alpha-llc", "zeta-llc"}

    removed = blacklist.remove("zeta-llc")
    assert isinstance(removed, BlacklistEntry) and removed.business_identity == "zeta-llc"
    assert blacklist.remove("zeta-llc") is None
    assert len(blacklist) == 1 and "zeta-llc" not in blacklist
    assert not is_blacklisted(blacklist, {"business_identity": "zeta-llc"})

    blacklist.add(business_identity="alpha-llc", reason_code="r3", status="expired")
    assert len(blacklist) == 1, "re-recording a decision must replace, not duplicate"
    assert blacklist.lookup("alpha-llc").status == "expired"


def test_an_entry_lapses_only_against_an_as_of_that_was_actually_supplied() -> None:
    """Expiry is evaluated against an explicit instant or not at all — never the wall clock.

    Omitting ``as_of`` means the recorded status alone decides, so a caller with no reference
    instant can never accidentally expire a live listing. Supplied, a lapsed ``expires_at``
    clears the block even while the recorded status still says ``active`` — the case the
    manifest's ``store-secondchance`` fixture exists to exercise.
    """
    blacklist = Blacklist()
    blacklist.add(
        business_identity="second-chance-coffee-llc",
        reason_code="resolved_dispute",
        status="active",
        expires_at="2026-08-01T00:00:00Z",
    )
    store = {"store_id": "store-secondchance", "business_identity": "second-chance-coffee-llc"}

    assert is_blacklisted(blacklist, store) is True
    assert is_blacklisted(blacklist, store, as_of="2026-07-01T00:00:00Z") is True
    assert is_blacklisted(blacklist, store, as_of="2026-09-01T00:00:00Z") is False

    entry = blacklist.lookup("second-chance-coffee-llc")
    assert entry.expired_at("2026-09-01T00:00:00Z") is True
    assert entry.expired_at("2026-07-01T00:00:00Z") is False
    assert entry.expired_at("not a date") is False

    permanent = Blacklist()
    permanent.add(business_identity="biz-forever", reason_code="fraud", expires_at=None)
    forever = {"business_identity": "biz-forever"}
    assert permanent.lookup("biz-forever").expired_at("2099-01-01T00:00:00Z") is False
    assert is_blacklisted(permanent, forever, as_of="2099-01-01T00:00:00Z") is True


# --------------------------------------------------------------------------------------
# T-186 — the per-observation weight channel (R14)
#
# `trust.feedback.accept_feedback` computes a weight (base 1.0, x0.25 when the buyer's own
# return contradicts their positive report, x the buyer's track record) and R14 rests two
# properties on it: "positive feedback contradicted by a return is downweighted" and "a
# single account cannot outvote the network". Both were properties of a number nothing
# consumed -- `score` derived an observation's weight SOLELY from OBSERVATION_WEIGHTS[type]
# and had no weight channel at all. Measured before the fix: feeding 1.0 / 0.25 / 0.01 on an
# otherwise identical observation gave alpha=2.0 beta=3.5 in every case.
# --------------------------------------------------------------------------------------


def _weighted_beta(weight, e6_as_of, *, otype="mismatch_return", dim="feedback_match"):
    """(alpha, beta) for one observation of ``otype`` carrying ``weight``."""
    observation = {"store_id": "s-1", "dim": dim, "type": otype, "observed_at": e6_as_of}
    if weight is not None:
        observation["weight"] = weight
    entry = score([observation], as_of=e6_as_of)["dims"][dim]
    return float(entry["alpha"]), float(entry["beta"])


def test_a_per_observation_weight_scales_the_beta_update(e6_as_of):
    """R14: three different weights on one identical observation must give three results.

    This is the whole seam. The published type weight says what *kind* of evidence this is
    worth; the per-observation weight says how much this particular report is worth given who
    made it and whether their own behaviour contradicts it. Without the second, a piece of
    feedback from an account with no track record lands with exactly the force of one from
    the network's most reliable buyer.
    """
    published = float(OBSERVATION_WEIGHTS["mismatch_return"])

    full = _weighted_beta(1.0, e6_as_of)
    quarter = _weighted_beta(0.25, e6_as_of)
    hundredth = _weighted_beta(0.01, e6_as_of)

    assert full == (PRIOR_ALPHA, PRIOR_BETA + published), (
        "weight 1.0 must land exactly the published type weight -- no more, no less"
    )
    assert quarter == (PRIOR_ALPHA, PRIOR_BETA + published * 0.25)
    assert hundredth == (PRIOR_ALPHA, PRIOR_BETA + published * 0.01)
    assert full[1] > quarter[1] > hundredth[1], (
        "the per-observation weight is computed and thrown away: three weights that differ "
        "by two orders of magnitude produced one identical Beta update"
    )


def test_a_positive_observation_is_scaled_by_its_weight_too(e6_as_of):
    """The channel is symmetric: a downweighted positive moves alpha less, not beta more."""
    published = float(OBSERVATION_WEIGHTS["fulfilled"])

    full = _weighted_beta(1.0, e6_as_of, otype="fulfilled")
    quarter = _weighted_beta(0.25, e6_as_of, otype="fulfilled")

    assert full == (PRIOR_ALPHA + published, PRIOR_BETA)
    assert quarter == (PRIOR_ALPHA + published * 0.25, PRIOR_BETA)


def test_an_observation_with_no_weight_field_is_bit_identical_to_weight_one(e6_as_of):
    """S3: the ledger projection carries no weight, so absent must mean exactly 1.0.

    ``trust.ledger.replay.observations_from_events`` projects only
    ``{store_id, dim, type, observed_at}``. If "no weight" meant anything other than exactly
    1.0, the S3 assertion -- replaying the ledger reproduces the served snapshot bit for bit
    -- would start comparing two different arithmetics.
    """
    for otype in ("verified", "fulfilled", "unsupported", "contradicted", "mismatch_return"):
        assert _weighted_beta(None, e6_as_of, otype=otype) == _weighted_beta(
            1.0, e6_as_of, otype=otype
        ), f"an unweighted {otype!r} observation did not score identically to weight 1.0"


def test_a_weight_above_one_is_refused_because_it_would_outvote_the_network(e6_as_of):
    """R14: no single report may weigh more than one whole piece of evidence.

    A weight channel that accepted 5.0 would hand back exactly the astroturfing the routed-
    buyer gate exists to prevent, through a different door: one account's feedback worth five
    honest buyers'. Loud, not clamped -- a silently clamped 5.0 is a caller that believes it
    is doing something it is not.
    """
    with pytest.raises(ValueError) as excinfo:
        _weighted_beta(1.5, e6_as_of)
    assert "weight" in str(excinfo.value).lower()

    with pytest.raises(ValueError):
        _weighted_beta(-0.5, e6_as_of)
    with pytest.raises(ValueError):
        _weighted_beta("heavy", e6_as_of)


def test_a_zero_weight_observation_moves_nothing_and_still_costs_coverage(e6_as_of):
    """Weight 0.0 is admissible and means what ``ambiguous`` means: it decides nothing.

    Asserted bit-identical to the prior rather than merely close, for the same reason
    ``ambiguous`` is: "moved it by almost nothing" and "did not move it" are different claims.
    """
    snapshot = score(
        [
            {
                "store_id": "s-1",
                "dim": "feedback_match",
                "type": "mismatch_return",
                "observed_at": e6_as_of,
                "weight": 0.0,
            }
        ],
        as_of=e6_as_of,
    )
    entry = snapshot["dims"]["feedback_match"]

    assert (float(entry["alpha"]), float(entry["beta"])) == (PRIOR_ALPHA, PRIOR_BETA)
    assert int(entry["observations"]) == 1, "a zero-weight observation still happened"


def test_a_downweighted_observation_buys_proportionally_less_coverage(e6_as_of):
    """A discounted report must not buy FULL coverage credit for a fraction of the evidence.

    Coverage is ``decided_mass / mass`` and confidence is
    ``floor + (1-floor) * saturation(evidence) * coverage``. Scaling only ``evidence`` by the
    per-observation weight left ``decided_mass`` at full strength, so a report discounted to
    0.25 -- or to 0.0 -- bought the same coverage as a full-weight one. That reopens R14's
    "a single account cannot outvote the network" through the confidence channel instead of
    the mean, which is the channel the weight was added to close.
    """

    def _coverage(weight):
        snapshot = score(
            [
                {
                    "store_id": "s-1",
                    "dim": "feedback_match",
                    "type": "fulfilled",
                    "observed_at": e6_as_of,
                    "weight": weight,
                }
            ],
            as_of=e6_as_of,
        )
        return float(snapshot["dims"]["feedback_match"]["coverage"])

    assert _coverage(1.0) == 1.0
    assert _coverage(0.25) == 0.25, "a quarter-weight report bought full coverage credit"
    assert _coverage(0.0) == 0.0, (
        "a zero-weight report decided nothing and must cost coverage exactly as `ambiguous` "
        "does -- otherwise it is free confidence"
    )


def test_zero_weight_padding_cannot_manufacture_confidence(e6_as_of):
    """The measured attack: pad a real record with weightless 'deciding' observations.

    Measured before the fix: 18.0 units of real evidence plus 360 zero-weight observations
    took confidence from 0.1728 to 0.5659 (+227%) while ``effective_evidence`` never moved.
    Confidence that can be bought with evidence nobody weighted is not confidence.

    The record starts with LOW coverage -- three decided outcomes against twelve undecided
    ones per dimension -- because coverage is the factor the padding inflates. A baseline
    already at coverage 1.0 has no room to be inflated and would pass this test vacuously.
    """
    real = [
        {"store_id": "s-1", "dim": dim, "type": otype, "observed_at": e6_as_of}
        for dim in TRUST_DIMENSIONS
        for otype in (["contradicted"] * 3 + ["ambiguous"] * 12)
    ]
    padding = [
        {
            "store_id": "s-1",
            "dim": dim,
            "type": "fulfilled",
            "observed_at": e6_as_of,
            "weight": 0.0,
        }
        for dim in TRUST_DIMENSIONS
        for _ in range(60)
    ]

    baseline = score(real, as_of=e6_as_of)
    padded = score([*real, *padding], as_of=e6_as_of)

    assert padded["effective_evidence"] == baseline["effective_evidence"]
    assert padded["confidence"] <= baseline["confidence"], (
        f"padding with weightless observations raised confidence from "
        f"{baseline['confidence']} to {padded['confidence']}"
    )


def test_the_weight_channel_accepts_any_real_number_not_only_int_and_float(e6_as_of):
    """A ``Decimal`` weight is a perfectly good weight and must not crash the scorer.

    Reachable from any JSON parsed with ``parse_float=Decimal`` or read out of a NUMERIC
    column -- and ``ledger.trust_observations.weight`` is a ``double precision`` column, so
    weights really do arrive from a database. Refusing them with "is not a number" takes
    scoring down for a value that is one.
    """
    from decimal import Decimal
    from fractions import Fraction

    for value in (Decimal("0.25"), Fraction(1, 4)):
        entry = score(
            [
                {
                    "store_id": "s-1",
                    "dim": "feedback_match",
                    "type": "fulfilled",
                    "observed_at": e6_as_of,
                    "weight": value,
                }
            ],
            as_of=e6_as_of,
        )["dims"]["feedback_match"]
        assert float(entry["alpha"]) == PRIOR_ALPHA + 0.25, f"{type(value).__name__} was refused"

    # ...and the refusals that matter still refuse.
    for bad in (True, "0.25", None.__class__, float("nan"), float("inf"), Decimal("2")):
        with pytest.raises(ValueError):
            score(
                [
                    {
                        "store_id": "s-1",
                        "dim": "feedback_match",
                        "type": "fulfilled",
                        "observed_at": e6_as_of,
                        "weight": bad,
                    }
                ],
                as_of=e6_as_of,
            )
