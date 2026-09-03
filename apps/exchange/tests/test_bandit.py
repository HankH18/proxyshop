"""T-034 gate — the exposure bandit (R16, R12, S4).

`exchange.policy` runs a contextual Thompson sampler over intent-cluster x store. It
decides *exposure*, never rank weights: DESIGN's single published rank formula is
untouched by anything in this file.

Three behaviours are load-bearing and each has its own test below:

* **R16/S4** — conversion outcomes move a store's exposure inside its own cluster and
  nowhere else, and the result is a deterministic function of the seed.
* **R12, exploration slice** — a low-data store is guaranteed at least the configured
  floor. The floor is *per low-data store*, not one slice split among them: with a floor
  of 0.25 and one low-data store, that store takes 0.25 and the other four share 0.75.
* **R12, blacklist** — a blacklisted store draws exactly zero, and the mass its floor
  would have taken is never handed back to it. The floor is applied first and the
  blacklist second, which is why a blacklisted low-data store is reported at 0.0 rather
  than at the floor.

Every import of the code under test is inside a test function, matching the frozen
acceptance suite's convention: on an unbuilt package that reads as "these behaviours are
not implemented yet" rather than as "the gate itself is broken".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.exchange.tests._fixtures_bandit import build_outcomes, build_trust_snapshot

FLOOR = "exploration_floor"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _shares(raw) -> dict[str, float]:
    """Normalize an exposure result to ``{store_id: float}``.

    Mirrors the frozen suite's ``_exposure_map`` so this gate cannot pass on a shape the
    grader would reject.
    """
    if isinstance(raw, dict):
        return {str(k): float(v) for k, v in raw.items()}
    out: dict[str, float] = {}
    for record in raw:
        sid = str(record["store_id"] if isinstance(record, dict) else record.store_id)
        for key in ("exposure", "share", "weight"):
            value = record.get(key) if isinstance(record, dict) else getattr(record, key, None)
            if value is not None:
                out[sid] = float(value)
                break
        else:
            raise AssertionError(f"exposure record {record!r} carries no exposure/share/weight")
    return out


# ---------------------------------------------------------------------------------
# R16 / S4 — outcomes move exposure, in one cluster only, deterministically.
# ---------------------------------------------------------------------------------
def test_positive_outcomes_raise_exposure_only_in_the_outcome_cluster() -> None:
    """A converting store gains exposure in its own cluster; other clusters do not move."""
    from exchange.policy import exposure, initial_state, update

    stores = ["store-a", "store-b", "store-c"]
    state = initial_state(
        stores, ["cluster-1", "cluster-2"], build_trust_snapshot(stores), {FLOOR: 0.10}
    )

    before_one = _shares(exposure(state, "cluster-1", 7))
    before_two = _shares(exposure(state, "cluster-2", 7))

    updated = update(state, build_outcomes(40, ["store-a"], ["store-b", "store-c"]))

    after_one = _shares(exposure(updated, "cluster-1", 7))
    after_two = _shares(exposure(updated, "cluster-2", 7))

    assert after_one["store-a"] > before_one["store-a"]
    assert after_one["store-a"] > after_one["store-b"]
    assert after_one["store-a"] > after_one["store-c"]
    assert after_two == before_two, "cluster-2 moved on cluster-1 outcomes"


def test_losses_lower_a_store_relative_to_a_store_with_no_record_at_all() -> None:
    """A losing record is worse than no record: the bandit still explores the unknown store."""
    from exchange.policy import exposure, initial_state, update

    stores = ["store-loser", "store-quiet"]
    state = initial_state(stores, ["cluster-1"], build_trust_snapshot(stores), {FLOOR: 0.0})
    updated = update(state, build_outcomes(30, [], ["store-loser"]))

    shares = _shares(exposure(updated, "cluster-1", 3))
    assert shares["store-quiet"] > shares["store-loser"]
    assert shares["store-loser"] > 0.0, "a store with a bad record is starved, never banned"


def test_exposure_is_a_deterministic_function_of_state_cluster_and_seed() -> None:
    """Two calls with the same seed agree exactly; the seed is what varies the draw."""
    from exchange.policy import exposure, initial_state, update

    stores = ["store-a", "store-b", "store-c"]
    state = initial_state(stores, ["cluster-1"], build_trust_snapshot(stores), {FLOOR: 0.0})
    updated = update(state, build_outcomes(5, ["store-a"], ["store-b"]))

    for seed in range(4):
        first = _shares(exposure(updated, "cluster-1", seed))
        second = _shares(exposure(updated, "cluster-1", seed))
        assert first == second, f"seed {seed} is not reproducible: {first} != {second}"

    distinct = {tuple(sorted(_shares(exposure(updated, "cluster-1", s)).items())) for s in range(8)}
    assert len(distinct) > 1, "the seed does not vary the draw — this is not a sampler"


def test_update_does_not_mutate_the_state_it_was_given() -> None:
    """``update`` returns a new state; the caller's state still reports its old exposure."""
    from exchange.policy import exposure, initial_state, update

    stores = ["store-a", "store-b"]
    state = initial_state(stores, ["cluster-1"], build_trust_snapshot(stores), {FLOOR: 0.0})
    before = _shares(exposure(state, "cluster-1", 11))

    update(state, build_outcomes(50, ["store-a"], ["store-b"]))

    assert _shares(exposure(state, "cluster-1", 11)) == before, "update mutated its argument"


# ---------------------------------------------------------------------------------
# R12 — the guaranteed exploration slice.
# ---------------------------------------------------------------------------------
def test_exploration_floor_holds_for_a_low_data_store_across_a_seeded_window() -> None:
    """A brand-new store keeps the floor against four stores with a long winning record."""
    from exchange.policy import exposure, initial_state, update

    established = ["store-a", "store-b", "store-c", "store-d"]
    stores = [*established, "store-new"]
    floor = 0.25
    state = initial_state(
        stores,
        ["cluster-1"],
        build_trust_snapshot(stores, low_data=("store-new",)),
        {FLOOR: floor},
    )
    updated = update(state, build_outcomes(60, established))

    for seed in range(5):
        shares = _shares(exposure(updated, "cluster-1", seed))
        assert shares["store-new"] >= floor - 1e-9, f"seed {seed}: below the floor ({shares})"


def test_the_floor_is_per_low_data_store_and_leaves_the_rest_to_be_shared() -> None:
    """One low-data store at a 0.25 floor takes 0.25 — the other four share 0.75, not 0.75/5.

    This is the ordering that is easy to get backwards: a single global exploration slice
    split among the low-data stores, or a floor applied to every store, both satisfy the
    previous test and get this one wrong.
    """
    from exchange.policy import exposure, initial_state, update

    established = ["store-a", "store-b", "store-c", "store-d"]
    stores = [*established, "store-new"]
    floor = 0.25
    state = initial_state(
        stores,
        ["cluster-1"],
        build_trust_snapshot(stores, low_data=("store-new",)),
        {FLOOR: floor},
    )
    updated = update(state, build_outcomes(60, established))

    for seed in range(5):
        shares = _shares(exposure(updated, "cluster-1", seed))
        assert shares["store-new"] == pytest.approx(floor, abs=1e-6), (
            f"seed {seed}: the low-data store should sit at the floor, not above it: {shares}"
        )
        rest = sum(v for k, v in shares.items() if k != "store-new")
        assert rest == pytest.approx(1.0 - floor, abs=1e-6), (
            f"seed {seed}: the established stores should share {1.0 - floor}, got {rest}"
        )


def test_a_store_with_ample_data_is_not_lifted_by_the_exploration_floor() -> None:
    """The floor is an exploration subsidy for low-data stores, not a minimum for everyone."""
    from exchange.policy import exposure, initial_state, update

    stores = ["store-strong", "store-weak"]
    state = initial_state(
        stores, ["cluster-1"], build_trust_snapshot(stores), {FLOOR: 0.40}
    )
    updated = update(state, build_outcomes(80, ["store-strong"], ["store-weak"]))

    shares = _shares(exposure(updated, "cluster-1", 2))
    assert shares["store-weak"] < 0.40, (
        f"a well-observed loser was lifted to the low-data floor: {shares}"
    )


def test_an_oversubscribed_floor_is_capped_and_the_shares_still_sum_to_one() -> None:
    """Four low-data stores cannot each take a 0.40 floor; the slice is capped, not overdrawn."""
    from exchange.policy import exposure, initial_state

    stores = ["store-a", "store-b", "store-c", "store-d"]
    state = initial_state(
        stores, ["cluster-1"], build_trust_snapshot(stores, low_data=stores), {FLOOR: 0.40}
    )

    shares = _shares(exposure(state, "cluster-1", 5))
    assert sum(shares.values()) == pytest.approx(1.0, abs=1e-6), shares
    assert all(v >= 0.0 for v in shares.values()), shares


def test_exposure_shares_form_a_distribution() -> None:
    """Every share is in [0, 1] and the cluster's shares sum to one."""
    from exchange.policy import exposure, initial_state, update

    stores = ["store-a", "store-b", "store-c"]
    state = initial_state(
        stores,
        ["cluster-1"],
        build_trust_snapshot(stores, low_data=("store-c",)),
        {FLOOR: 0.15},
    )
    updated = update(state, build_outcomes(20, ["store-a"], ["store-b"]))

    for seed in range(6):
        shares = _shares(exposure(updated, "cluster-1", seed))
        assert set(shares) == set(stores), shares
        assert all(0.0 <= v <= 1.0 for v in shares.values()), shares
        assert sum(shares.values()) == pytest.approx(1.0, abs=1e-6), shares


# ---------------------------------------------------------------------------------
# R12 — the blacklist, applied after the floor and never redistributed back.
# ---------------------------------------------------------------------------------
def test_a_blacklisted_store_draws_exactly_zero_across_the_seeded_window() -> None:
    """Good outcomes and a low-data floor together do not buy a blacklisted store exposure."""
    from exchange.policy import exposure, initial_state, update

    stores = ["store-a", "store-b", "store-banned"]
    state = initial_state(
        stores,
        ["cluster-1"],
        build_trust_snapshot(stores, blacklisted=("store-banned",), low_data=("store-banned",)),
        {FLOOR: 0.10},
    )
    updated = update(state, build_outcomes(50, ["store-banned"], ["store-a"]))

    for seed in range(10):
        shares = _shares(exposure(updated, "cluster-1", seed))
        assert shares["store-banned"] == 0.0, f"seed {seed}: {shares}"
        assert sum(v for k, v in shares.items() if k != "store-banned") > 0.0, shares


def test_a_blacklisted_stores_floor_is_not_handed_back_to_it() -> None:
    """The banned store is reported present-and-zero, and the survivors absorb its slice.

    Order matters: the floor runs first, so the banned store is briefly holding the
    exploration slice; the blacklist then zeroes it and the remaining mass renormalizes
    over the survivors. An implementation that re-applies the floor after blacklisting
    hands the slice straight back.
    """
    from exchange.policy import exposure, initial_state, update

    stores = ["store-a", "store-b", "store-banned"]
    state = initial_state(
        stores,
        ["cluster-1"],
        build_trust_snapshot(stores, blacklisted=("store-banned",), low_data=("store-banned",)),
        {FLOOR: 0.30},
    )
    updated = update(state, build_outcomes(25, ["store-a", "store-banned"], ["store-b"]))

    for seed in range(6):
        shares = _shares(exposure(updated, "cluster-1", seed))
        assert "store-banned" in shares, "the banned store is reported at zero, not omitted"
        assert shares["store-banned"] == 0.0, f"seed {seed}: {shares}"
        assert sum(shares.values()) == pytest.approx(1.0, abs=1e-6), (
            f"seed {seed}: the banned slice was not absorbed by the survivors: {shares}"
        )


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("flag missing", {"drop_keys": {"store-unknown": ("blacklisted",)}}),
        ("flag null", {"none_keys": {"store-unknown": ("blacklisted",)}}),
        ("store absent from the snapshot", {"omit_stores": ("store-unknown",)}),
        ("whole record null", {"none_keys": {}}),
    ],
)
def test_an_unanswered_blacklist_question_fails_closed(label: str, kwargs: dict) -> None:
    """R12: the eligibility answer is fail-closed. Unknown is treated as blacklisted."""
    from exchange.policy import exposure, initial_state

    stores = ["store-a", "store-unknown"]
    snapshot = build_trust_snapshot(stores, **kwargs)
    if label == "whole record null":
        snapshot["store-unknown"] = None

    state = initial_state(stores, ["cluster-1"], snapshot, {FLOOR: 0.20})

    for seed in range(4):
        shares = _shares(exposure(state, "cluster-1", seed))
        assert shares.get("store-unknown", 0.0) == 0.0, (
            f"{label}: an unanswered blacklist question drew exposure ({shares})"
        )
        assert shares["store-a"] > 0.0, shares


# ---------------------------------------------------------------------------------
# Surface and input validation.
# ---------------------------------------------------------------------------------
def test_update_rejects_outcomes_for_stores_and_clusters_it_does_not_know() -> None:
    """A misrouted outcome is loud. Silently dropping it would corrupt the posterior."""
    from exchange.policy import initial_state, update

    stores = ["store-a", "store-b"]
    state = initial_state(stores, ["cluster-1"], build_trust_snapshot(stores), {FLOOR: 0.0})

    with pytest.raises(ValueError, match="cluster"):
        update(state, [{"store_id": "store-a", "cluster_id": "cluster-9", "converted": True}])
    with pytest.raises(ValueError, match="store"):
        update(state, [{"store_id": "store-z", "cluster_id": "cluster-1", "converted": True}])


def test_exposure_rejects_an_unknown_cluster() -> None:
    """Asking for a cluster the state has never heard of is an error, not an empty map."""
    from exchange.policy import exposure, initial_state

    stores = ["store-a"]
    state = initial_state(stores, ["cluster-1"], build_trust_snapshot(stores), {FLOOR: 0.0})

    with pytest.raises(ValueError, match="cluster"):
        exposure(state, "cluster-9", 1)


def test_the_frozen_acceptance_import_path_exposes_the_whole_surface() -> None:
    """The grader imports ``apps.exchange.src.policy``; both namespaces must answer."""
    import importlib

    module = importlib.import_module("apps.exchange.src.policy")
    for name in ("exposure", "initial_state", "update"):
        assert callable(getattr(module, name, None)), f"{name} is missing from the package"


def test_update_rejects_an_outcome_that_never_says_what_happened() -> None:
    """An outcome with no ``converted`` result is not an outcome. R16 learns from results."""
    from exchange.policy import initial_state, update

    stores = ["store-a"]
    state = initial_state(stores, ["cluster-1"], build_trust_snapshot(stores), {FLOOR: 0.0})

    with pytest.raises(ValueError, match="converted"):
        update(state, [{"store_id": "store-a", "cluster_id": "cluster-1"}])
    with pytest.raises(ValueError, match="converted"):
        update(state, [{"store_id": "store-a", "cluster_id": "cluster-1", "converted": None}])


def test_exposure_agrees_with_itself_across_separate_interpreters() -> None:
    """The seed must mean the same thing in another process, or S4's fixed seeds are noise.

    ``hash()`` on strings is salted per interpreter, so an implementation that derived its
    RNG seed from ``hash(cluster_id)`` passes every same-process determinism check above
    and still returns different numbers on the next run. This is the check that catches it.
    """
    import json
    import subprocess
    import sys

    program = (
        "import json, sys;"
        "sys.path[:0] = ['.', '.pkgroot'];"
        "from exchange.policy import exposure, initial_state, update;"
        "stores = ['store-a', 'store-b', 'store-c'];"
        "snap = {s: {'score': 0.5, 'confidence': 0.4, 'blacklisted': False,"
        " 'low_data': s == 'store-c'} for s in stores};"
        "st = initial_state(stores, ['cluster-1'], snap, {'exploration_floor': 0.1});"
        "st = update(st, [{'store_id': 'store-a', 'cluster_id': 'cluster-1',"
        " 'converted': True}] * 12);"
        "print(json.dumps(exposure(st, 'cluster-1', 4)))"
    )
    runs = [
        json.loads(
            subprocess.run(
                [sys.executable, "-c", program],
                capture_output=True,
                check=True,
                text=True,
                cwd=str(REPO_ROOT),
            ).stdout
        )
        for _ in range(2)
    ]
    assert runs[0] == runs[1], f"the same seed gave two answers in two processes: {runs}"

    in_process = _shares(_reference_exposure())
    assert runs[0] == pytest.approx(in_process), (
        f"a subprocess disagrees with this one at the same seed: {runs[0]} vs {in_process}"
    )


def _reference_exposure() -> dict[str, float]:
    """The same computation the subprocess above runs, in this interpreter."""
    from exchange.policy import exposure, initial_state, update

    stores = ["store-a", "store-b", "store-c"]
    snapshot = build_trust_snapshot(stores, low_data=("store-c",))
    state = initial_state(stores, ["cluster-1"], snapshot, {FLOOR: 0.1})
    state = update(state, build_outcomes(12, ["store-a"]))
    return exposure(state, "cluster-1", 4)
