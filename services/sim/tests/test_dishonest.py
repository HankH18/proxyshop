"""T-081 — the scripted dishonest actor (S2 / A3 / S4).

These are the ticket-owned tests for ``sim.dishonest``. They sit beside the frozen
acceptance criterion (``.swarm-loop/acceptance/test_e8_proofs.py::
test_dishonest_script_emits_exactly_the_manifest_behaviours``) and deliberately assert more
than it does, because the frozen goal can be satisfied by a module that does the wrong thing
for the right shape:

* the frozen goal reads the real manifest and compares the emitted ``kind`` sequence to it —
  which a module holding a hard-coded copy of today's seven kinds would also pass;
* these hand the module a **synthetic** manifest and watch the output follow it, which is
  what makes A3's non-circularity claim testable rather than asserted, and they grep this
  module's own source to prove no kind string is transcribed into it;
* these also pin the fields the frozen goal does not look at — ``dim``, ``type``,
  ``claim_type`` and the published ``weight`` all come out of the approved document — and
  the replay schedule the manifest states, which is the only schedule under which the
  published trust trajectory means anything.

Offline: no network, no clock, no database. The only I/O is reading ``fixtures/manifest.json``
through ``fixtures.manifest.load_manifest``, which is the approved document itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The module under test, as a source file. Read as text by the "no transcribed attack" test:
#: the point is what the bytes do NOT contain.
DISHONEST_SOURCE = REPO_ROOT / "services/sim/src/dishonest.py"


def _approved_manifest() -> dict:
    """The real, digest-verified manifest — the same document the frozen suite reads."""
    from fixtures.manifest import load_manifest

    return load_manifest()


def _synthetic_manifest() -> dict:
    """A manifest that is NOT the approved one, scripting two behaviours nobody published.

    Every field the script needs and nothing else. If ``sim.dishonest`` follows this, it is
    reading the manifest; if it emits the seven approved kinds instead, it is reading itself.
    """
    return {
        "episode_budget": 3,
        "observation_weights": {"contradicted": 2.0, "mismatch_return": 1.5},
        "dishonest_store": {
            "store_id": "store-synthetic",
            "business_identity": "synthetic-llc",
            "behaviours": [
                {
                    "kind": "invented_behaviour_alpha",
                    "dim": "price_honored",
                    "type": "contradicted",
                    "claim_type": "price",
                },
                {
                    "kind": "invented_behaviour_beta",
                    "dim": "feedback_match",
                    "type": "mismatch_return",
                    "claim_type": None,
                },
            ],
        },
    }


def _canonical(payload: object) -> str:
    """The frozen suite's byte-identity serialization, reproduced exactly."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=None)


# ------------------------------------------------------------------------------------
# A3 — the attack is read, never transcribed
# ------------------------------------------------------------------------------------
def test_the_script_follows_a_synthetic_manifest_rather_than_its_own_source() -> None:
    """Hand it a manifest nobody approved and it runs THAT script, element for element."""
    from sim.dishonest import run_dishonest_script

    emitted = run_dishonest_script(_synthetic_manifest(), 7)

    assert [record["kind"] for record in emitted] == [
        "invented_behaviour_alpha",
        "invented_behaviour_beta",
    ], "the script must come from the manifest handed in, not from a list inside the module"


def test_no_approved_behaviour_kind_is_transcribed_into_the_module_source() -> None:
    """A3: the module may not carry a copy of the attack it is supposed to be reading.

    A hard-coded copy would pass the frozen criterion for exactly as long as the manifest
    stayed unchanged, and would silently stop tracking the approved document the moment a
    human amended it — which is the failure this criterion exists to make impossible.
    """
    from sim.dishonest import behaviour_kinds

    source = DISHONEST_SOURCE.read_text(encoding="utf-8")
    leaked = [kind for kind in behaviour_kinds(_approved_manifest()) if kind in source]
    assert not leaked, (
        f"these approved behaviour kinds are written into {DISHONEST_SOURCE.name}: {leaked}. "
        "The manifest is ground truth; a second copy in the simulator is a copy that drifts."
    )


def test_a_trimmed_manifest_produces_a_trimmed_script() -> None:
    """Remove a behaviour from the approved script and exactly that behaviour disappears."""
    from sim.dishonest import run_dishonest_script

    manifest = _approved_manifest()
    full = [record["kind"] for record in run_dishonest_script(manifest, manifest["seed"])]

    manifest["dishonest_store"]["behaviours"] = manifest["dishonest_store"]["behaviours"][:-1]
    trimmed = [record["kind"] for record in run_dishonest_script(manifest, manifest["seed"])]

    assert trimmed == full[:-1], (
        f"dropping the last approved behaviour must drop exactly it: {full} -> {trimmed}"
    )


# ------------------------------------------------------------------------------------
# The fields the frozen goal does not look at
# ------------------------------------------------------------------------------------
def test_every_record_copies_the_approved_dimension_type_and_claim_type() -> None:
    """``dim``/``type``/``claim_type`` are the manifest's, not the simulator's."""
    from sim.dishonest import run_dishonest_script

    manifest = _approved_manifest()
    emitted = run_dishonest_script(manifest, manifest["seed"])
    approved = manifest["dishonest_store"]["behaviours"]

    assert len(emitted) == len(approved)
    for record, behaviour in zip(emitted, approved, strict=True):
        where = record["kind"]
        assert record["dim"] == behaviour["dim"], where
        assert record["type"] == behaviour["type"], where
        assert record["claim_type"] == behaviour.get("claim_type"), where
        assert record["store_id"] == manifest["dishonest_store"]["store_id"], where


def test_the_published_weight_is_read_from_the_manifest_not_invented() -> None:
    """D18: the observation weights are manifest constants, so the script reads them."""
    from sim.dishonest import run_dishonest_script

    manifest = _approved_manifest()
    weights = manifest["observation_weights"]
    for record in run_dishonest_script(manifest, manifest["seed"]):
        assert record["published_weight"] == float(weights[record["type"]]), record["kind"]

    # ...and moving the approved weight moves the emitted one.
    manifest["observation_weights"]["contradicted"] = 9.25
    moved = [
        r["published_weight"]
        for r in run_dishonest_script(manifest, 1)
        if r["type"] == "contradicted"
    ]
    assert moved and set(moved) == {9.25}, (
        "a weight amended in the manifest must reach the emitted record; a simulator with "
        "its own copy would keep emitting 2.0"
    )


def test_an_observation_type_with_no_published_weight_is_refused() -> None:
    """A weight the simulator invented would be the trust engine grading its own adversary."""
    from sim.dishonest import DishonestScriptError, run_dishonest_script

    manifest = _synthetic_manifest()
    del manifest["observation_weights"]["contradicted"]
    with pytest.raises(DishonestScriptError, match="no published weight"):
        run_dishonest_script(manifest, 1)


def test_every_emitted_record_is_plain_json_data() -> None:
    """The frozen determinism check serializes with ``default=None`` — anything exotic raises."""
    from sim.dishonest import run_dishonest_campaign

    manifest = _approved_manifest()
    _canonical(run_dishonest_campaign(manifest, manifest["seed"]))


# ------------------------------------------------------------------------------------
# S4 / C9 — determinism, and what the seed is allowed to decide
# ------------------------------------------------------------------------------------
def test_the_same_seed_reproduces_the_stream_byte_for_byte() -> None:
    from sim.dishonest import run_dishonest_campaign

    manifest = _approved_manifest()
    first = run_dishonest_campaign(manifest, manifest["seed"])
    second = run_dishonest_campaign(manifest, manifest["seed"])
    assert _canonical(first) == _canonical(second)


def test_a_different_seed_moves_the_incidentals_but_never_the_sequence() -> None:
    """The seed decides magnitudes. The manifest decides which behaviours run, and in what order."""
    from sim.dishonest import run_dishonest_script

    manifest = _approved_manifest()
    a = run_dishonest_script(manifest, manifest["seed"])
    b = run_dishonest_script(manifest, manifest["seed"] + 1)

    assert [r["kind"] for r in a] == [r["kind"] for r in b], (
        "a reseeded run that changed the behaviour sequence would make the manifest advisory"
    )
    assert [r["order_id"] for r in a] != [r["order_id"] for r in b], (
        "a seed that changed nothing at all would make S4's determinism criterion vacuous"
    )


# ------------------------------------------------------------------------------------
# The replay schedule the manifest states
# ------------------------------------------------------------------------------------
def test_the_campaign_replays_every_behaviour_once_per_episode_across_the_budget() -> None:
    """``expected_trust_trajectory_replay_rule``, in its own words, is what this asserts."""
    from sim.dishonest import behaviour_kinds, run_dishonest_campaign

    manifest = _approved_manifest()
    budget = manifest["episode_budget"]
    kinds = behaviour_kinds(manifest)

    stream = run_dishonest_campaign(manifest, manifest["seed"])
    assert len(stream) == budget * len(kinds), (
        f"the approved schedule is all {len(kinds)} behaviours once per episode for "
        f"{budget} episodes; got {len(stream)} records"
    )

    episodes = [record["episode"] for record in stream]
    assert episodes == sorted(episodes), "observations must be in non-decreasing episode order"
    assert min(episodes) == 1, "episode 0 is the untouched prior the trajectory measures from"
    assert max(episodes) == budget, "the campaign must fill the approved budget"

    for episode in range(1, budget + 1):
        per_episode = [r["kind"] for r in stream if r["episode"] == episode]
        assert per_episode == kinds, f"episode {episode} replayed {per_episode}"


def test_the_campaign_may_be_shortened_but_never_run_past_the_approved_budget() -> None:
    """S1 acceptance 3: the run is bounded, and the bound is the human's, not the caller's."""
    from sim.dishonest import DishonestScriptError, behaviour_kinds, run_dishonest_campaign

    manifest = _approved_manifest()
    short = run_dishonest_campaign(manifest, manifest["seed"], episodes=2)
    assert len(short) == 2 * len(behaviour_kinds(manifest))
    assert {r["episode"] for r in short} == {1, 2}

    with pytest.raises(DishonestScriptError, match="episode budget"):
        run_dishonest_campaign(manifest, manifest["seed"], episodes=manifest["episode_budget"] + 1)


def test_an_episode_outside_the_budget_is_refused() -> None:
    from sim.dishonest import DishonestScriptError, run_dishonest_script

    manifest = _approved_manifest()
    with pytest.raises(DishonestScriptError, match="episode budget"):
        run_dishonest_script(manifest, manifest["seed"], episode=manifest["episode_budget"] + 1)
    with pytest.raises(DishonestScriptError, match="episode budget"):
        run_dishonest_script(manifest, manifest["seed"], episode=-1)


# ------------------------------------------------------------------------------------
# Refusals — a manifest that cannot supply a script must not be completed on its behalf
# ------------------------------------------------------------------------------------
def test_an_empty_behaviour_list_is_refused_rather_than_reported_green() -> None:
    """An empty script would make S2 vacuous: nothing emitted equals nothing approved."""
    from sim.dishonest import DishonestScriptError, run_dishonest_script

    manifest = _synthetic_manifest()
    manifest["dishonest_store"]["behaviours"] = []
    with pytest.raises(DishonestScriptError, match="at least one behaviour"):
        run_dishonest_script(manifest, 1)


def test_a_dimension_outside_the_published_six_is_refused() -> None:
    """The six-dimension vocabulary is DESIGN's; a seventh would score into nothing."""
    from sim.dishonest import DishonestScriptError, run_dishonest_script

    manifest = _synthetic_manifest()
    manifest["dishonest_store"]["behaviours"][0]["dim"] = "vibes"
    with pytest.raises(DishonestScriptError, match="published trust dimensions"):
        run_dishonest_script(manifest, 1)


@pytest.mark.parametrize("missing", ["kind", "dim", "type"])
def test_a_behaviour_missing_a_required_field_is_refused(missing: str) -> None:
    """Never filled in: a behaviour this module completed is a behaviour no human approved."""
    from sim.dishonest import DishonestScriptError, run_dishonest_script

    manifest = _synthetic_manifest()
    del manifest["dishonest_store"]["behaviours"][0][missing]
    with pytest.raises(DishonestScriptError, match=missing):
        run_dishonest_script(manifest, 1)


def test_a_manifest_with_no_dishonest_store_is_refused() -> None:
    from sim.dishonest import DishonestScriptError, run_dishonest_script

    with pytest.raises(DishonestScriptError, match="dishonest_store"):
        run_dishonest_script({"episode_budget": 3}, 1)
