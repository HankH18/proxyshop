"""T-064 -- the one versioned TrustSnapshot the exchange consumes.

The exchange asks trust three questions and must not have to assemble the answers from three
shapes: what the score is, whether the store is blacklisted, and whether we have seen enough
of the store to trust the number. This file grades the served document against each of those,
and against the three ways the obvious implementation of it goes wrong:

**A five-dimension snapshot.** ``catalog_claim_accuracy`` is the sixth dimension D53 added,
and a snapshot that quietly omits it still looks well-formed -- every consumer keeps working
and simply never sees catalog dishonesty. So the six are asserted by name against a list this
lane spelled out independently (``e6_dims``), the sixth is asserted present on its own, and a
seventh is asserted absent. A test that only said "all of ``TRUST_DIMENSIONS`` are present"
would pass on a five-dimension engine that had also dropped the sixth from its vocabulary.

**A blacklist that is a speed bump.** ``blacklisted`` is resolved through *business identity*,
so a delisted store returning under a brand-new ``store_id`` is still flagged; and a blacklist
whose backing store is down flags **every** entry rather than admitting them all. Both are
asserted here, the second with a lookup that raises.

**A ``low_data`` flag that re-derives its own threshold.** The threshold is the human-approved
manifest's ``new_store_prior_n``, and this file reads that number off disk through
``e6_manifest`` rather than writing ``5`` anywhere -- a test carrying its own copy of the
constant grades the engine against the test author, not against ground truth. The boundary is
asserted in both directions, on explicitly-declared episode counts and again on episode counts
the builder has to *derive*.

Everything here is pure data pinned to ``e6_as_of``. No clock, no socket, no datastore.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from apps.trust.src.scoring import (
    NEW_STORE_PRIOR_N,
    PRIOR_ALPHA,
    PRIOR_BETA,
    SCORE_VERSION,
    TRUST_DIMENSIONS,
    Blacklist,
)
from apps.trust.src.snapshot import (
    SNAPSHOT_VERSION,
    build_snapshot,
    clean_episodes,
    store_entry,
)

#: The four fields every served dimension must carry. The exchange reads all four: the Beta
#: pair is the score, ``decayed_at`` says what instant it was decayed to, and ``coverage``
#: says how much of the evidence behind it actually decided anything.
_SERVED_DIMENSION_FIELDS = ("alpha", "beta", "decayed_at", "coverage")

#: Observation types that count as positive evidence in the derived-floor path.
_POSITIVE_TYPES = ("verified", "fulfilled")


class _UnavailableBlacklist:
    """A blacklist whose backing store is down: every read raises.

    The persistent blacklist lives in Postgres (``app.seller_blacklist``), so "the lookup
    raised" is not a hypothetical -- it is what a connection-pool exhaustion looks like from
    inside :func:`build_snapshot`.
    """

    def __init__(self) -> None:
        self.calls = 0

    def lookup(self, business_identity: str) -> Any:
        self.calls += 1
        raise RuntimeError(f"blacklist store unavailable while reading {business_identity!r}")


def _store(
    store_id: str | None,
    identity: str | None,
    observations: list[dict] | None = None,
    **extra: Any,
) -> dict:
    """One store record in the shape :func:`build_snapshot` consumes."""
    record: dict[str, Any] = {
        "store_id": store_id,
        "business_identity": identity,
        "observations": list(observations or ()),
    }
    if store_id is None:
        del record["store_id"]
    record.update(extra)
    return record


def _clean_round(store_id: str, as_of: str, rounds: int) -> list[dict]:
    """``rounds`` complete clean rounds: one positive observation on each of the six dims.

    This is the shape the derived floor is defined over -- ``the largest k such that every
    dimension carries k positive observations`` -- so a store built this way has exactly
    ``rounds`` clean episodes without ever declaring a count or tagging an episode.
    """
    observations: list[dict] = []
    for index in range(rounds):
        for dim in TRUST_DIMENSIONS:
            observations.append(
                {
                    "store_id": store_id,
                    "dim": dim,
                    "type": _POSITIVE_TYPES[index % len(_POSITIVE_TYPES)],
                    "observed_at": as_of,
                }
            )
    return observations


def test_snapshot_serves_a_version_a_score_version_and_stores_keyed_by_store_id(e6_as_of):
    """The served envelope is ``{version, score_version, as_of, stores}``, keyed by store_id.

    R12: the exchange caches on ``version`` and compares served scores against the
    ``score_version`` of the arithmetic that produced them, so both have to be on the document
    itself and not something a consumer has to know out of band. ``stores`` is a mapping keyed
    by ``store_id`` because the exchange looks a store up; a list would make every lookup a
    scan and would not say what happens to a duplicate.
    """
    snapshot = build_snapshot(
        [_store("s-1", "bi-1"), _store("s-2", "bi-2")],
        blacklist=Blacklist(),
        as_of=e6_as_of,
    )

    assert isinstance(snapshot["version"], str) and snapshot["version"].strip()
    assert snapshot["version"] == SNAPSHOT_VERSION
    assert snapshot["score_version"] == SCORE_VERSION
    assert snapshot["as_of"] == e6_as_of
    assert sorted(snapshot["stores"]) == ["s-1", "s-2"]
    for store_id, entry in snapshot["stores"].items():
        assert entry["store_id"] == store_id, "an entry must be filed under its own store_id"


def test_every_entry_carries_all_six_dimensions_with_the_four_served_fields(e6_as_of, e6_dims):
    """Every entry serves all six dimensions, each with alpha/beta/decayed_at/coverage.

    The six are compared against ``e6_dims``, which this lane spelled out by hand rather than
    imported: asserting that the engine's ``TRUST_DIMENSIONS`` equals ``TRUST_DIMENSIONS`` is
    vacuous, and a five-dimension engine would pass it.

    ``coverage`` is checked as a served field and not merely as arithmetic because it is what
    lets the exchange tell a clean record from a record nobody could check.
    """
    snapshot = build_snapshot(
        [_store("s-1", "bi-1"), _store("s-2", "bi-2")],
        blacklist=Blacklist(),
        as_of=e6_as_of,
    )

    for entry in snapshot["stores"].values():
        assert tuple(entry["dims"]) == tuple(e6_dims), (
            "the served dimension set must be exactly the six, in the published order"
        )
        for name, dimension in entry["dims"].items():
            for field in _SERVED_DIMENSION_FIELDS:
                assert field in dimension, f"{name} is served without {field!r}"
            assert isinstance(dimension["alpha"], float)
            assert isinstance(dimension["beta"], float)
            assert isinstance(dimension["decayed_at"], str) and dimension["decayed_at"]
            assert 0.0 <= float(dimension["coverage"]) <= 1.0


def test_a_five_dimension_snapshot_fails_this_suite(e6_as_of):
    """``catalog_claim_accuracy`` is present, and there is no seventh dimension.

    D53 amended R12 from five dimensions to six, and the regression it exists to prevent is
    silent: a snapshot that drops the sixth still parses, still scores, and simply never
    reports catalog dishonesty. So the sixth is asserted by name -- not as a member of a set
    the engine also authored -- and the dimension count is pinned at exactly six so a seventh
    (a SPEC change) cannot arrive without this test saying so.
    """
    snapshot = build_snapshot([_store("s-1", "bi-1")], blacklist=Blacklist(), as_of=e6_as_of)
    entry = snapshot["stores"]["s-1"]

    assert "catalog_claim_accuracy" in entry["dims"], (
        "the sixth dimension is missing: this is the five-dimension snapshot D53 forbids"
    )
    assert len(entry["dims"]) == 6
    assert set(entry["dims"]) == {
        "price_honored",
        "discount_honored",
        "shipped_on_time",
        "not_returned",
        "feedback_match",
        "catalog_claim_accuracy",
    }
    assert list(snapshot["dimensions"]) == list(entry["dims"]), (
        "the envelope's published vocabulary and the entries' dims must not be able to drift"
    )


def test_blacklisted_follows_business_identity_under_a_brand_new_store_id(e6_as_of):
    """A delisted business is flagged even after it re-registers under a fresh store_id.

    R12: keying the blacklist on ``store_id`` makes it cost a bad actor one Shopify signup.
    The listing here names only the identity, and the store record carries a ``store_id`` that
    has never been listed -- exactly the re-registration case.
    """
    blacklist = Blacklist()
    blacklist.add(business_identity="brightbean-coffee-llc", reason_code="repeated_contradiction")

    snapshot = build_snapshot(
        [_store("store-brand-new-id-9999", "brightbean-coffee-llc")],
        blacklist=blacklist,
        as_of=e6_as_of,
    )

    entry = snapshot["stores"]["store-brand-new-id-9999"]
    assert entry["blacklisted"] is True
    assert entry["business_identity"] == "brightbean-coffee-llc"
    assert "store-brand-new-id-9999" not in blacklist.identities(), (
        "the flag must come from the identity; the store_id was never listed"
    )


def test_blacklisted_is_false_for_an_unrelated_business_identity(e6_as_of):
    """A store whose identity is not listed is admitted, and the flag is per entry.

    The negative half matters as much as the positive one: a snapshot that flagged everybody
    would satisfy the previous test and would route no buyer anywhere.
    """
    blacklist = Blacklist()
    blacklist.add(business_identity="brightbean-coffee-llc", reason_code="repeated_contradiction")

    snapshot = build_snapshot(
        [
            _store("s-listed", "brightbean-coffee-llc"),
            _store("s-clean", "north-roast-collective-llc"),
        ],
        blacklist=blacklist,
        as_of=e6_as_of,
    )

    assert snapshot["stores"]["s-listed"]["blacklisted"] is True
    assert snapshot["stores"]["s-clean"]["blacklisted"] is False


def test_blacklisted_fails_closed_when_the_blacklist_lookup_raises(e6_as_of):
    """An unreadable blacklist flags EVERY store rather than admitting them all.

    R12's fail-closed rule. Answering "not blacklisted" because the store holding the
    blacklist is down converts an infrastructure outage into an open door for precisely the
    sellers the blacklist was built to keep out. The lookup is asserted to have actually been
    attempted, so this cannot pass by never consulting the blacklist at all.
    """
    blacklist = _UnavailableBlacklist()

    snapshot = build_snapshot(
        [_store("s-1", "bi-1"), _store("s-2", "bi-2"), _store("s-3", "bi-3")],
        blacklist=blacklist,
        as_of=e6_as_of,
    )

    assert [entry["blacklisted"] for entry in snapshot["stores"].values()] == [True, True, True]
    assert blacklist.calls == 3, "every store must have been asked about, and every read failed"


def test_blacklisted_fails_closed_for_a_store_carrying_no_business_identity(e6_as_of):
    """A store record with no business identity is an unknown, and an unknown is a refusal.

    The blacklist is identity-bound, so a record with no identity is a store the question
    cannot be asked about. Admitting it would let a malformed record become the way past the
    blacklist -- the same fail-open hole as an outage, in a shape nobody would notice.
    """
    snapshot = build_snapshot([_store("s-1", None)], blacklist=Blacklist(), as_of=e6_as_of)

    entry = snapshot["stores"]["s-1"]
    assert entry["business_identity"] is None
    assert entry["blacklisted"] is True


def test_low_data_threshold_is_the_manifests_new_store_prior_n_read_off_disk(e6_manifest):
    """The engine's ``NEW_STORE_PRIOR_N`` is the approved manifest's number, not its own.

    D18/SPEC A3: the constants the trust engine applies are human-approved ground truth. This
    test compares the engine against ``fixtures/manifest.json`` as read off disk by
    ``e6_manifest`` -- the number ``5`` is never written here, so the assertion still grades
    the engine if a human re-approves the manifest with a different N.
    """
    approved = e6_manifest["new_store_prior_n"]

    assert isinstance(approved, int) and not isinstance(approved, bool)
    assert approved >= 1
    assert NEW_STORE_PRIOR_N == approved, (
        "the low_data threshold must be read from the approved manifest, never chosen here"
    )


def test_low_data_boundary_holds_in_both_directions_for_a_declared_episode_count(
    e6_as_of, e6_manifest
):
    """Exactly ``prior_n - 1`` declared clean episodes is low_data; exactly ``prior_n`` is not.

    ``low_data`` marks a store the exchange should treat as *unknown* rather than *average*,
    and the boundary is where that distinction is actually made. Asserting only the "few
    episodes" side would pass on an implementation that flagged every store forever.
    """
    prior_n = int(e6_manifest["new_store_prior_n"])

    snapshot = build_snapshot(
        [
            _store("s-below", "bi-below", episodes=prior_n - 1),
            _store("s-at", "bi-at", episodes=prior_n),
            _store("s-above", "bi-above", episodes=prior_n + 3),
        ],
        blacklist=Blacklist(),
        as_of=e6_as_of,
    )

    assert snapshot["stores"]["s-below"]["low_data"] is True
    assert snapshot["stores"]["s-below"]["episodes"] == prior_n - 1
    assert snapshot["stores"]["s-at"]["low_data"] is False
    assert snapshot["stores"]["s-at"]["episodes"] == prior_n
    assert snapshot["stores"]["s-above"]["low_data"] is False


def test_low_data_boundary_holds_for_episode_counts_the_builder_has_to_derive(
    e6_as_of, e6_manifest
):
    """The same boundary, with nothing declared: the count is derived from observations.

    The declared-count path is the easy one. This store says nothing about episodes at all, so
    the builder has to derive the floor from ``prior_n - 1`` and ``prior_n`` complete clean
    rounds -- which is the path a real store reaches ``low_data`` through.
    """
    prior_n = int(e6_manifest["new_store_prior_n"])

    snapshot = build_snapshot(
        [
            _store("s-below", "bi-below", _clean_round("s-below", e6_as_of, prior_n - 1)),
            _store("s-at", "bi-at", _clean_round("s-at", e6_as_of, prior_n)),
        ],
        blacklist=Blacklist(),
        as_of=e6_as_of,
    )

    assert snapshot["stores"]["s-below"]["episodes"] == prior_n - 1
    assert snapshot["stores"]["s-below"]["low_data"] is True
    assert snapshot["stores"]["s-at"]["episodes"] == prior_n
    assert snapshot["stores"]["s-at"]["low_data"] is False


def test_clean_episodes_prefers_an_explicit_count_on_the_store_record(e6_as_of, e6_obs):
    """A declared ``episodes`` count wins over anything the observations would imply.

    The caller that ran the episodes knows how many there were; the derived floor is a
    conservative estimate for callers that do not. So a store declaring 7 episodes reads as 7
    even though its observations, on their own, would derive 0.
    """
    observations = [e6_obs("s-1", "price_honored", "verified", e6_as_of)]

    assert clean_episodes({"store_id": "s-1", "episodes": 7}, observations) == 7
    assert clean_episodes({"store_id": "s-1"}, observations) == 0, (
        "without the declaration the same observations derive a floor of zero"
    )


def test_clean_episodes_clamps_a_negative_declaration_and_ignores_a_boolean_one(e6_as_of, e6_obs):
    """``episodes=-3`` reads as 0, and ``episodes=True`` is not a count at all.

    ``bool`` is an ``int`` in Python, so ``episodes: True`` would otherwise be read as "one
    clean episode" -- a store flagged on the strength of a flag somebody set. A negative count
    is nonsense rather than evidence, and clamping it to zero keeps it on the low_data side.
    """
    observations = [
        e6_obs("s-1", dim, "verified", e6_as_of)
        for dim in (
            "price_honored",
            "discount_honored",
            "shipped_on_time",
            "not_returned",
            "feedback_match",
            "catalog_claim_accuracy",
        )
    ]

    assert clean_episodes({"store_id": "s-1", "episodes": -3}, observations) == 0
    assert clean_episodes({"store_id": "s-1", "episodes": True}, observations) == 1, (
        "a boolean must fall through to the derived floor, which these six positives make 1"
    )


def test_clean_episodes_counts_episode_tags_with_no_negative_outcome(e6_as_of):
    """Tagged observations count the episodes that carried no negative outcome.

    An episode is one completed round of evidence. ``ep-clean`` carries only positives and
    counts; ``ep-dirty`` carries a contradiction and does not; ``ep-undecided`` carries an
    ``ambiguous`` outcome, which decided nothing and therefore is not a clean round either.
    """
    observations = [
        {
            "dim": "price_honored",
            "type": "verified",
            "observed_at": e6_as_of,
            "episode": "ep-clean",
        },
        {
            "dim": "not_returned",
            "type": "fulfilled",
            "observed_at": e6_as_of,
            "episode": "ep-clean",
        },
        {
            "dim": "price_honored",
            "type": "fulfilled",
            "observed_at": e6_as_of,
            "episode": "ep-dirty",
        },
        {
            "dim": "catalog_claim_accuracy",
            "type": "contradicted",
            "observed_at": e6_as_of,
            "episode": "ep-dirty",
        },
        {
            "dim": "feedback_match",
            "type": "ambiguous",
            "observed_at": e6_as_of,
            "episode": "ep-undecided",
        },
    ]

    assert clean_episodes({"store_id": "s-1"}, observations) == 1


def test_clean_episodes_derives_the_floor_from_the_weakest_dimension(e6_as_of, e6_obs, e6_dims):
    """With nothing declared or tagged, the count is the smallest per-dimension positive count.

    Five dimensions carry three positives each and the sixth carries one, so one complete
    round is all that has demonstrably happened. Taking the maximum, or the average, would
    read a store that is heavily observed on one axis as established on all six.
    """
    repeats = {dim: (1 if dim == "catalog_claim_accuracy" else 3) for dim in e6_dims}
    observations = [
        e6_obs("s-1", dim, "verified", e6_as_of)
        for dim, count in repeats.items()
        for _ in range(count)
    ]

    assert len(observations) == 16
    assert clean_episodes({"store_id": "s-1"}, observations) == 1


def test_a_store_observed_on_one_dimension_only_has_completed_no_round_and_reads_low_data(
    e6_as_of, e6_obs
):
    """Twenty positives on one dimension are still zero completed episodes.

    This is the mistake ``low_data`` exists to prevent, in its purest form: a store that looks
    heavily evidenced because one axis is dense, while five of the six have never been
    observed at all. The derived floor is deliberately conservative, so the flag stays on.
    """
    observations = [e6_obs("s-1", "price_honored", "verified", e6_as_of) for _ in range(20)]

    snapshot = build_snapshot(
        [_store("s-1", "bi-1", observations)], blacklist=Blacklist(), as_of=e6_as_of
    )

    entry = snapshot["stores"]["s-1"]
    assert entry["observations"] == 20
    assert entry["episodes"] == 0
    assert entry["low_data"] is True


def test_score_and_confidence_are_inside_the_unit_interval_for_every_entry(e6_as_of, e6_obs):
    """Every served ``score`` and ``confidence`` is in [0, 1], for clean and ruined stores.

    The exchange multiplies by these numbers. A score outside the unit interval -- from an
    unclamped weight, or a decay factor above one -- would not raise anywhere; it would
    silently reorder the ranking.
    """
    ruined = [e6_obs("s-bad", "price_honored", "severe_policy", e6_as_of) for _ in range(40)]
    ruined += [
        e6_obs("s-bad", "catalog_claim_accuracy", "contradicted", e6_as_of) for _ in range(9)
    ]
    clean = [
        e6_obs("s-good", dim, "verified", e6_as_of) for dim in TRUST_DIMENSIONS for _ in range(6)
    ]

    snapshot = build_snapshot(
        [
            _store("s-bad", "bi-bad", ruined),
            _store("s-good", "bi-good", clean),
            _store("s-new", "bi-new"),
        ],
        blacklist=Blacklist(),
        as_of=e6_as_of,
    )

    for store_id, entry in snapshot["stores"].items():
        assert 0.0 <= entry["score"] <= 1.0, f"{store_id} served a score outside [0, 1]"
        assert 0.0 <= entry["confidence"] <= 1.0, f"{store_id} served confidence outside [0, 1]"
    assert snapshot["stores"]["s-bad"]["score"] < snapshot["stores"]["s-good"]["score"], (
        "the interval assertion must not be satisfiable by a scorer that serves a constant"
    )


def test_a_store_with_no_observations_is_served_at_the_prior_rather_than_omitted(e6_as_of):
    """A store nobody has observed appears, at Beta(2,2), flagged low_data.

    Omitting it would make the exchange distinguish "unknown" from "absent" by the shape of
    the document, and the two are not the same thing. Serving it at the neutral prior with
    ``low_data`` set is what lets the exploration slice tell "nobody has bought from them yet"
    from "half their orders go wrong" -- both of which score ~0.5.
    """
    snapshot = build_snapshot([_store("s-new", "bi-new")], blacklist=Blacklist(), as_of=e6_as_of)

    entry = snapshot["stores"]["s-new"]
    assert entry["observations"] == 0
    assert entry["low_data"] is True
    assert entry["score"] == pytest.approx(0.5)
    for dimension in entry["dims"].values():
        assert dimension["alpha"] == PRIOR_ALPHA
        assert dimension["beta"] == PRIOR_BETA
        assert dimension["coverage"] == 0.0


def test_a_store_record_with_no_store_id_is_skipped_not_fatal(e6_as_of, e6_obs):
    """One unusable record must not take down the whole snapshot.

    ``stores`` is keyed by ``store_id``, so a record without one has no key to be filed under.
    The choice is between dropping that record and raising, and raising means a single
    malformed row makes the exchange's entire trust read fail -- every other store included.
    """
    snapshot = build_snapshot(
        [
            _store(None, "bi-orphan", [e6_obs("?", "price_honored", "verified", e6_as_of)]),
            _store("s-1", "bi-1"),
        ],
        blacklist=Blacklist(),
        as_of=e6_as_of,
    )

    assert sorted(snapshot["stores"]) == ["s-1"]
    assert all(entry["store_id"] is not None for entry in snapshot["stores"].values())


def test_the_snapshot_is_json_serialisable(e6_as_of, e6_obs):
    """The served document round-trips through JSON unchanged.

    The exchange reads this over the wire. A ``datetime``, a ``MappingProxyType`` or a tuple
    in the payload is not a type error anywhere in this process -- it is a serialisation
    failure at the boundary, discovered by whichever service asked for the snapshot first.
    """
    observations = [
        e6_obs("s-1", "catalog_claim_accuracy", "contradicted", e6_as_of),
        e6_obs("s-1", "price_honored", "fulfilled", e6_as_of),
    ]
    snapshot = build_snapshot(
        [_store("s-1", "bi-1", observations)], blacklist=Blacklist(), as_of=e6_as_of
    )

    assert json.loads(json.dumps(snapshot)) == snapshot


def test_two_builds_of_the_same_input_compare_equal(e6_as_of, e6_obs):
    """The snapshot is a pure function of (stores, blacklist, as_of).

    S3: decay is evaluated against the explicit ``as_of`` and never a clock, which is what
    makes a served snapshot reproducible and a replayed score comparable to it. Two builds
    that differed -- by a timestamp, a set iteration order or a dict ordering -- would make
    every "the replay matches the serve" assertion downstream a comparison of approximations.
    """
    observations = [
        e6_obs("s-1", "shipped_on_time", "contradicted", "2025-11-01T00:00:00Z"),
        e6_obs("s-1", "catalog_claim_accuracy", "verified", "2025-12-15T00:00:00Z"),
    ]
    blacklist = Blacklist()
    blacklist.add(business_identity="bi-2", reason_code="phantom_discount", status="appealed")

    stores = [_store("s-1", "bi-1", observations), _store("s-2", "bi-2")]
    first = build_snapshot(list(stores), blacklist=blacklist, as_of=e6_as_of)
    second = build_snapshot(list(stores), blacklist=blacklist, as_of=e6_as_of)

    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_snapshot_version_is_a_non_empty_string_and_is_what_version_carries():
    """``SNAPSHOT_VERSION`` is a real, non-empty string the served document reports verbatim.

    The exchange client caches on it and refreshes when it changes. An empty or absent version
    means a cache keyed on nothing, which serves a five-dimension snapshot forever after the
    sixth dimension lands -- and every ranking decision made from it is quietly stale.
    """
    assert isinstance(SNAPSHOT_VERSION, str)
    assert SNAPSHOT_VERSION.strip() == SNAPSHOT_VERSION
    assert SNAPSHOT_VERSION != ""
    assert SNAPSHOT_VERSION != SCORE_VERSION, (
        "the shape version and the arithmetic version answer different questions"
    )


def test_store_entry_and_build_snapshot_publish_the_same_entry(e6_as_of, e6_obs):
    """The single-store helper and the bulk builder cannot drift.

    ``store_entry`` is published so a caller that already holds one store need not build a
    whole snapshot. Two code paths producing two shapes is how a consumer ends up handling
    ``low_data`` in one and not the other, so they are asserted identical here.
    """
    observations = [e6_obs("s-1", "feedback_match", "mismatch_return", e6_as_of)]
    store = _store("s-1", "bi-1", observations)
    blacklist = Blacklist()

    direct = store_entry(store, blacklist=blacklist, as_of=e6_as_of)
    built = build_snapshot([store], blacklist=blacklist, as_of=e6_as_of)["stores"]["s-1"]

    assert direct == built
