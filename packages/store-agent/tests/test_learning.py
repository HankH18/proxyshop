"""T-042 — the store-agent learning loop, graded as behaviour rather than as shape.

R17 makes three promises that constrain each other, and this file grades each one by the
mechanism that could break it rather than by the happy path:

1. **A store's discount depth moves toward its OWN winners.** Seeded outcomes that convert at
   20% must raise the sampled depth; outcomes that convert at 0% must lower it. `sample_depth`
   is a pure function of `(state, cluster, seed)`, so this is measured, not sampled-and-hoped.

2. **The network prior is BLIND to discount fields.** The frozen goal proves discount fields do
   not *change* the answer. This file proves they are never *read*: the builder is fed mappings
   that log every key anybody asks for — including enumeration, so ``dict(record)`` is caught
   too — and the log must contain no discount key and nothing outside the declared allowlist.
   Byte-equality is a consequence of blindness; blindness is the requirement.

3. **Two stores learn independently.** Not "the test happened to pass": every value reachable
   from a learning state must be immutable, so there is no mutable structure for two stores to
   share in the first place, and the module itself must hold no mutable global. Independence
   that depends on callers being careful is not independence.

Plus the two things that make the loop usable rather than merely correct: own-outcome filtering
(a state bound to a store ignores another store's rows) and a rendered `learned_policy` in the
exact shape `store_agent.hooks.ToolHooks.choose_policy_action` reads — including the
fraction-to-percent conversion, which is the unit bug this loop is most likely to ship.

Offline by construction: no network, no clock, no LLM, no database, no unseeded randomness.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

#: Depth is a FRACTION on the wire the learning loop reads and writes (`discount_depth: 0.2`),
#: and a PERCENT in the envelope and in `learned_policy` (`discount_pct: 20.0`). Getting this
#: backwards silently turns a 20% ask into a 0.2% ask, or a 0.2 ask into a 20 000% one.
PERCENT_PER_UNIT = 100.0

#: Anything reachable from a learning state must be one of these, recursively. A `list`, `dict`
#: or `set` anywhere in a state is a channel through which one store's update can reach another.
IMMUTABLE_LEAVES = (str, int, float, bool, bytes, type(None))


def _canon(obj: Any) -> str:
    """A stable canonical string, matching how the frozen acceptance harness compares states."""
    return json.dumps(_plain(obj), sort_keys=True, default=str)


def _plain(obj: Any, _depth: int = 0) -> Any:
    if _depth > 40:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_plain(v, _depth + 1) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _plain(v, _depth + 1) for k, v in obj.items()}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name), _depth + 1) for f in dataclasses.fields(obj)}
    return str(obj)


def _mutable_paths(obj: Any, path: str = "state", _depth: int = 0) -> list[str]:
    """Every path from `obj` to a mutable container. Empty means "structurally immutable"."""
    if _depth > 40:
        return []
    if isinstance(obj, IMMUTABLE_LEAVES):
        return []
    if isinstance(obj, tuple):
        found: list[str] = []
        for i, item in enumerate(obj):
            found.extend(_mutable_paths(item, f"{path}[{i}]", _depth + 1))
        return found
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        if not obj.__dataclass_params__.frozen:
            return [f"{path} (unfrozen dataclass {type(obj).__name__})"]
        found = []
        for field in dataclasses.fields(obj):
            found.extend(_mutable_paths(getattr(obj, field.name), f"{path}.{field.name}", _depth + 1))
        return found
    if isinstance(obj, (list, dict, set, bytearray)):
        return [f"{path} ({type(obj).__name__})"]
    return [f"{path} (unrecognised mutable-capable {type(obj).__name__})"]


def _mean(values) -> float:
    values = list(values)
    return sum(values) / float(len(values))


# ---------------------------------------------------------------------------------------
# 1. the module exists at all, and exports the loop's four verbs
# ---------------------------------------------------------------------------------------


def test_learning_module_exports_the_loop():
    """The four verbs the frozen goal imports, plus the constants this file grades against."""
    from store_agent import learning

    for name in ("build_network_prior", "initial_state", "sample_depth", "update"):
        assert callable(getattr(learning, name, None)), f"{name} must be a callable export"
        assert name in learning.__all__, f"{name} must be in __all__"

    assert isinstance(learning.DEFAULT_DEPTH_BUCKETS, tuple)
    assert len(learning.DEFAULT_DEPTH_BUCKETS) >= 3
    assert learning.DEFAULT_DEPTH_BUCKETS == tuple(sorted(learning.DEFAULT_DEPTH_BUCKETS))
    assert all(0.0 <= b <= 1.0 for b in learning.DEFAULT_DEPTH_BUCKETS), (
        "depth buckets are FRACTIONS (0.2 == 20%), so none may exceed 1.0"
    )


def test_the_learning_module_holds_no_mutable_global():
    """Independence is structural: there is no module-level container two stores could share.

    A `_STATES = {}` cache keyed by store id would satisfy every behavioural assertion in this
    file while making two stores in one process share a mutable structure. It cannot exist.
    """
    from store_agent import learning

    offenders = {
        name: type(value).__name__
        for name, value in vars(learning).items()
        if not name.startswith("__") and isinstance(value, (list, dict, set, bytearray))
    }
    assert offenders == {}, (
        f"module-level mutable state in store_agent.learning: {offenders}. Per-store learning "
        "state must live in the value the caller holds, never in the module."
    )


# ---------------------------------------------------------------------------------------
# 2. the network prior never READS a discount field
# ---------------------------------------------------------------------------------------


def test_build_network_prior_never_reads_a_discount_field(
    learning_prior_records, learning_key_spy, learning_spy_class
):
    """R17, proved by observation: no discount key is ever asked for, and no record is slurped."""
    from store_agent.learning import PRIOR_RECORD_FIELDS, build_network_prior

    spied, log = learning_key_spy(learning_prior_records)
    build_network_prior(spied)

    assert log, "the builder read nothing at all — it cannot be measuring pitch outcomes"

    discount_reads = sorted({k for k in log if "discount" in k.lower()})
    assert discount_reads == [], (
        f"the network prior builder read discount fields {discount_reads}; R17 forbids pooling "
        "discount elasticity across stores"
    )

    assert learning_spy_class.ITER not in log, (
        "the builder enumerated a whole record (dict(record) / record.items() / **record). "
        "Slurping a record reads its discount fields even when it never names one; the builder "
        "must ask for the fields it is allowed to see, by name."
    )

    outside = sorted({k for k in log if k not in PRIOR_RECORD_FIELDS})
    assert outside == [], (
        f"the builder read fields outside its declared allowlist {PRIOR_RECORD_FIELDS}: {outside}"
    )


def test_the_prior_allowlist_cannot_name_a_discount_field():
    """The allowlist is the blindness mechanism, so it is itself checked, not merely trusted."""
    from store_agent.learning import PRIOR_RECORD_FIELDS

    assert PRIOR_RECORD_FIELDS, "the allowlist must not be empty"
    named = [f for f in PRIOR_RECORD_FIELDS if "discount" in f.lower()]
    assert named == [], f"the prior's allowlist names discount fields: {named}"


def test_the_prior_is_byte_identical_with_and_without_discount_fields(
    learning_prior_records, learning_strip_discounts
):
    """The frozen goal's own equality, restated here so this gate fails for the same reason."""
    from store_agent.learning import build_network_prior

    without = learning_strip_discounts(learning_prior_records)
    assert _canon(learning_prior_records) != _canon(without), (
        "the fixture must actually carry discount fields for this test to discriminate"
    )
    assert _canon(build_network_prior(learning_prior_records)) == _canon(
        build_network_prior(without)
    )


def test_the_prior_carries_content_and_responds_to_outcomes(learning_prior_records):
    """A builder returning a constant would pass the equality above while measuring nothing."""
    from store_agent.learning import build_network_prior

    prior = build_network_prior(learning_prior_records)
    assert _canon(prior) not in ("null", "{}", "[]", '""')

    flipped = [dict(r, won=not r["won"]) for r in learning_prior_records]
    assert _canon(build_network_prior(flipped)) != _canon(prior), (
        "the prior must respond to pitch/value-prop outcomes"
    )

    # ...and it must respond to the pitch itself, not only to the win flag.
    reworded = [dict(r, value_prop=f"{r['value_prop']}-x") for r in learning_prior_records]
    assert _canon(build_network_prior(reworded)) != _canon(prior)


def test_the_prior_is_order_independent(learning_prior_records):
    """Two platforms replaying the same evidence in different orders must agree."""
    from store_agent.learning import build_network_prior

    forward = build_network_prior(learning_prior_records)
    backward = build_network_prior(list(reversed(learning_prior_records)))
    assert _canon(forward) == _canon(backward)


# ---------------------------------------------------------------------------------------
# 3. per-store independence, made structural
# ---------------------------------------------------------------------------------------


def test_a_learning_state_is_immutable_all_the_way_down(learning_prior_records):
    """No `list`, `dict` or `set` anywhere in a state — so there is nothing to share."""
    from store_agent.learning import build_network_prior, initial_state, update

    prior = build_network_prior(learning_prior_records)
    state = initial_state(prior)
    assert _mutable_paths(state) == [], _mutable_paths(state)

    from store_agent.learning import DEFAULT_DEPTH_BUCKETS  # noqa: F401  (import sanity)

    with pytest.raises(dataclasses.FrozenInstanceError):
        state.observations = 999  # type: ignore[misc]

    evolved = update(state, [])
    assert _mutable_paths(evolved) == [], _mutable_paths(evolved)


def test_update_returns_a_new_state_and_leaves_its_input_alone(
    learning_prior_records, learning_outcomes
):
    from store_agent.learning import build_network_prior, initial_state, update

    prior = build_network_prior(learning_prior_records)
    state = initial_state(prior)
    before = _canon(state)

    evolved = update(state, learning_outcomes(0.20, 0.00))

    assert evolved is not state
    assert _canon(state) == before, "update mutated the state it was given"
    assert _canon(evolved) != before, "an update fed real outcomes must change the state"


def test_two_stores_from_one_prior_do_not_leak_into_each_other(
    learning_prior_records, learning_outcomes
):
    from store_agent.learning import build_network_prior, initial_state, update

    prior = build_network_prior(learning_prior_records)
    a, b = initial_state(prior), initial_state(prior)
    assert _canon(a) == _canon(b), "two stores seeded from one prior must start identical"

    b_before = _canon(b)
    updated_a = update(a, learning_outcomes(0.20, 0.00))
    assert _canon(b) == b_before, "updating store A must not mutate store B"

    updated_b = update(b, learning_outcomes(0.00, 0.20))
    assert _canon(updated_a) != _canon(updated_b)

    # A second round changes A again without ever touching B.
    b_after = _canon(updated_b)
    update(updated_a, learning_outcomes(0.20, 0.00))
    assert _canon(updated_b) == b_after


def test_a_store_bound_state_ignores_another_stores_outcomes(
    learning_prior_records, learning_outcomes, learning_store_id, learning_other_store_id
):
    """"Learns from its OWN outcomes only", enforced rather than assumed of the caller."""
    from store_agent.learning import build_network_prior, initial_state, update

    prior = build_network_prior(learning_prior_records)
    mine = initial_state(prior, store_id=learning_store_id)
    assert mine.store_id == learning_store_id

    foreign = learning_outcomes(0.20, 0.00, store_id=learning_other_store_id)
    assert _canon(update(mine, foreign)) == _canon(mine), (
        "a state bound to a store must ignore another store's outcomes entirely"
    )

    own = learning_outcomes(0.20, 0.00, store_id=learning_store_id)
    assert _canon(update(mine, own)) != _canon(mine)

    mixed = update(mine, own + foreign)
    assert _canon(mixed) == _canon(update(mine, own)), (
        "another store's rows must not change the answer even when mixed into the same batch"
    )


# ---------------------------------------------------------------------------------------
# 4. depth moves toward the store's own winners
# ---------------------------------------------------------------------------------------


def test_sample_depth_is_a_pure_function_of_state_cluster_and_seed(
    learning_prior_records, learning_cluster
):
    from store_agent.learning import (
        DEFAULT_DEPTH_BUCKETS,
        build_network_prior,
        initial_state,
        sample_depth,
    )

    state = initial_state(build_network_prior(learning_prior_records))
    for seed in (0, 7, 41, 399):
        first = sample_depth(state, learning_cluster, seed)
        assert first == sample_depth(state, learning_cluster, seed)
        assert first in DEFAULT_DEPTH_BUCKETS, f"{first} is not one of the depth buckets"

    # Two equal-but-distinct states must sample identically: the answer depends on the state's
    # VALUE, never on object identity or on a hidden per-instance rng.
    twin = initial_state(build_network_prior(learning_prior_records))
    assert [sample_depth(state, learning_cluster, s) for s in range(50)] == [
        sample_depth(twin, learning_cluster, s) for s in range(50)
    ]

    # ...and different seeds must not all collapse onto one bucket.
    drawn = {sample_depth(state, learning_cluster, s) for s in range(200)}
    assert len(drawn) > 1, "a seeded sampler that ignores the seed is not sampling"


def test_depth_shifts_toward_the_stores_own_winners(learning_prior_records, learning_outcomes, learning_cluster):
    """R17/S4: converting deep raises sampled depth; converting shallow lowers it."""
    from store_agent.learning import build_network_prior, initial_state, sample_depth, update

    prior = build_network_prior(learning_prior_records)

    def depth_mean(state) -> float:
        return _mean(sample_depth(state, learning_cluster, seed) for seed in range(400))

    base = depth_mean(initial_state(prior))
    deep = depth_mean(update(initial_state(prior), learning_outcomes(0.20, 0.00)))
    shallow = depth_mean(update(initial_state(prior), learning_outcomes(0.00, 0.20)))

    assert deep > base, f"deep winners must raise sampled depth: {deep} !> {base}"
    assert shallow < base, f"shallow winners must lower sampled depth: {shallow} !< {base}"
    assert deep > shallow

    # The shift must be decisive, not a coin-flip's worth of noise. 400 draws over buckets
    # spanning 0.0-0.2 have a standard error near 0.0035, so a real shift clears 0.02 easily.
    assert deep - base > 0.02, f"the shift toward deep winners is inside sampling noise: {deep - base}"
    assert base - shallow > 0.02, f"the shift toward shallow winners is inside sampling noise: {base - shallow}"


def test_an_unseen_cluster_falls_back_to_the_neutral_grid(
    learning_prior_records, learning_outcomes, learning_cluster
):
    """Learning about one cluster must not silently move another cluster's depth."""
    from store_agent.learning import build_network_prior, initial_state, sample_depth, update

    prior = build_network_prior(learning_prior_records)
    base = initial_state(prior)
    taught = update(base, learning_outcomes(0.20, 0.00))

    other = "cluster-never-seen"
    assert [sample_depth(taught, other, s) for s in range(60)] == [
        sample_depth(base, other, s) for s in range(60)
    ], "outcomes in one cluster leaked into an unrelated cluster's depth distribution"


# ---------------------------------------------------------------------------------------
# 5. what the runtime actually consumes
# ---------------------------------------------------------------------------------------


def test_learned_policy_renders_in_the_shape_choose_policy_action_reads(
    learning_prior_records, learning_outcomes, learning_cluster
):
    """`ToolHooks.choose_policy_action` reads `learned_policy['actions'][cluster]['discount_pct']`.

    It compares that number against the envelope's `max_discount_pct`, which is a PERCENT. A
    loop that hands it the 0.2 fraction it learned in asks for a 0.2% discount and nobody
    notices, because 0.2 is inside every wall.
    """
    from store_agent.learning import (
        build_network_prior,
        initial_state,
        to_learned_policy,
        update,
    )

    state = update(
        initial_state(build_network_prior(learning_prior_records)),
        learning_outcomes(0.20, 0.00),
    )
    policy = to_learned_policy(state)

    assert isinstance(policy, dict)
    assert isinstance(policy.get("version"), str) and policy["version"]
    actions = policy.get("actions")
    assert isinstance(actions, dict) and learning_cluster in actions

    action = actions[learning_cluster]
    pct = action["discount_pct"]
    assert isinstance(pct, float)
    assert 0.0 <= pct <= 100.0, f"discount_pct is a PERCENT, got {pct}"
    assert pct > 1.0, (
        f"a store that learned to win at a 20% depth must ask in percent, got {pct} — that is "
        "the learned fraction handed over unconverted"
    )
    assert isinstance(action.get("commitment_keys"), list)

    # Deterministic, and JSON-safe: it rides into a provenance-tagged claim value.
    assert json.dumps(policy, sort_keys=True) == json.dumps(to_learned_policy(state), sort_keys=True)


def test_context_priors_render_json_safe_and_discount_blind(learning_prior_records, learning_cluster):
    """`ToolHooks.get_network_prior` puts `network_priors[cluster]` straight into a claim value."""
    from store_agent.learning import build_network_prior, to_context_priors

    priors = to_context_priors(build_network_prior(learning_prior_records))
    assert isinstance(priors, dict) and learning_cluster in priors
    json.dumps(priors, sort_keys=True)  # must not raise

    blob = json.dumps(priors, sort_keys=True).lower()
    assert "discount" not in blob.replace("depth_buckets", ""), (
        f"a rendered network prior mentions a discount: {blob}"
    )
