"""T-081 — the scripted dishonest actor (SPEC S2 / A3).

The dishonest store is the adversary the whole trust apparatus exists to catch, and the
single most important property of this module is a *negative* one: **it contains no
behaviour of its own.** Every behaviour it emits — the kind, the trust dimension, the
observation type, the claim type — is read out of ``fixtures/manifest.json``, the document a
human approved and digest-pinned.

That is SPEC A3, and it is not a stylistic preference. "The trust engine catches the
dishonest store" is a circular claim when the dishonest behaviours come from the trust
engine's own config; it is *equally* circular when they come from the simulator's source,
because then the attacker and the grader are the same hand writing down the same idea of
what a lie looks like. So the attack script lives in a third place, a human signs it, and
both the attacker (here) and the grader (``apps.trust``) read it. Change the approved
script and this module's output changes with it — there is no second copy to drift, and
:func:`run_dishonest_script` has no list of kinds to fall back on when the manifest is
trimmed. A test that hands this module a two-behaviour manifest gets two behaviours out.

The replay schedule is part of the contract, not a detail
---------------------------------------------------------
``manifest.expected_trust_trajectory_replay_rule`` states it in the approved document: **all
of ``dishonest_store.behaviours`` are replayed ONCE PER EPISODE, one episode per day**, and
decay is measured from each observation's own date against ``half_life_days``. The published
``expected_trust_trajectory`` numbers are what *that* schedule produces under *those*
weights. A simulator that replays a different subset, or spaces its episodes differently, is
not reproducing the approved trajectory and must not be graded against it.

So the two functions here are one script and its replay:

* :func:`run_dishonest_script` runs **one episode's** pass — the manifest's behaviour
  sequence, element for element, in document order. This is the function SPEC S2 grades.
* :func:`run_dishonest_campaign` replays that pass once per episode across
  ``manifest.episode_budget``, which is the stream ``apps.trust`` is supposed to sink.

Episode 0 is the *prior* — the trajectory's first row is the neutral Beta(2,2) score before
any observation exists — so a campaign's observations start at episode 1 and end at
``episode_budget``. Nothing is emitted at episode 0 by construction: an observation there
would move the very score the trajectory uses as its untouched baseline.

What the seed decides, and what it must never decide
-----------------------------------------------------
The seed decides the *incidentals*: the order and buyer identifiers, the size of the price
gap, how many days late the dispatch was. It must never decide **which** behaviours run, or
in what order — those come from the manifest and only from the manifest, which is why
:func:`run_dishonest_script` reads its sequence before it ever touches the RNG. Re-running
with the same seed reproduces the stream byte for byte (S4 / C9); re-running with a
different seed changes only the magnitudes, never the sequence. Both halves are tested.

Every emitted record is plain JSON data — ``dict`` of ``str``/``int``/``float``/``None`` —
because the frozen acceptance suite compares two runs by canonical JSON serialization, and a
record carrying a dataclass or an ``Enum`` would fail that comparison for a reason that has
nothing to do with determinism.

Offline by construction: no network, no clock, no database, no LLM. The manifest arrives as
an already-parsed mapping, so this module performs no I/O at all.
"""

from __future__ import annotations

__all__ = [
    "DishonestScriptError",
    "behaviour_kinds",
    "run_dishonest_campaign",
    "run_dishonest_script",
    "scripted_behaviours",
]

import hashlib
import random
from collections.abc import Mapping, Sequence
from typing import Any

from fixtures.manifest import TRUST_DIMENSIONS

#: The first episode an observation may land in. Episode 0 is the trajectory's untouched
#: prior; writing an observation there would move the baseline the trajectory is measured
#: against, which is the one number in the document that must stay unobserved.
FIRST_OBSERVED_EPISODE = 1

#: The manifest key holding the published weight for each observation ``type``. Read, never
#: authored: a weight this module chose for itself would be the trust engine grading its own
#: adversary through the back door.
_WEIGHTS_KEY = "observation_weights"

#: Fields of a manifest behaviour that are copied into the emitted record verbatim. `kind`
#: is the field SPEC S2 grades element-for-element; `dim` and `type` are what the trust
#: engine turns the behaviour into; `claim_type` routes it through the D53 mapping table and
#: is legitimately ``null`` for ``feedback_match``, which takes no verification outcome.
_COPIED_FIELDS = ("kind", "dim", "type", "claim_type")


class DishonestScriptError(ValueError):
    """The approved manifest does not describe a runnable dishonest script."""


def _require(mapping: Any, key: str, kinds: type | tuple[type, ...], where: str) -> Any:
    """Fetch ``key`` off a manifest mapping, refusing a missing or mistyped value."""
    if not isinstance(mapping, Mapping):
        raise DishonestScriptError(f"{where} must be a JSON object, got {type(mapping).__name__}")
    if key not in mapping:
        raise DishonestScriptError(f"{where} must declare {key!r}")
    value = mapping[key]
    if isinstance(value, bool) and kinds is int:
        raise DishonestScriptError(f"{where}.{key} must be an integer, not a bool")
    if not isinstance(value, kinds):
        raise DishonestScriptError(f"{where}.{key} must be {kinds}, got {type(value).__name__}")
    return value


def _nonempty_str(mapping: Any, key: str, where: str) -> str:
    value = _require(mapping, key, str, where)
    if not value.strip():
        raise DishonestScriptError(f"{where}.{key} must not be empty")
    return value


def _rng(seed: int, *parts: object) -> random.Random:
    """A deterministic stream for one record.

    Derived through sha256 rather than seeded on a tuple, following
    :mod:`fixtures.generator`: ``Random(str)`` is stable in CPython but not contractually so,
    and hashing the parts keeps every record's stream independent of how many records were
    drawn before it. Reordering the emitted records therefore cannot change any record's
    incidentals, which is what makes "the seed decides magnitudes, the manifest decides the
    sequence" true rather than merely intended.
    """
    material = ":".join(str(part) for part in (seed, *parts))
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def scripted_behaviours(manifest: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """The approved dishonest behaviours, in document order, validated but not rewritten.

    Raises :class:`DishonestScriptError` when the manifest cannot supply a script: no
    ``dishonest_store``, an empty behaviour list, a behaviour with no ``kind``, or a ``dim``
    outside the six dimensions DESIGN publishes. It never *fills in* a missing field — a
    behaviour this module completed on the manifest's behalf would be a behaviour no human
    approved.
    """
    store = _require(manifest, "dishonest_store", Mapping, "manifest")
    behaviours = _require(store, "behaviours", Sequence, "manifest.dishonest_store")
    if isinstance(behaviours, (str, bytes)):
        raise DishonestScriptError("manifest.dishonest_store.behaviours must be a list")
    if not behaviours:
        raise DishonestScriptError(
            "manifest.dishonest_store.behaviours must script at least one behaviour — an "
            "empty script makes S2 vacuous rather than satisfied"
        )

    out: list[Mapping[str, Any]] = []
    for index, behaviour in enumerate(behaviours):
        where = f"manifest.dishonest_store.behaviours[{index}]"
        if not isinstance(behaviour, Mapping):
            raise DishonestScriptError(
                f"{where} must be a JSON object, got {type(behaviour).__name__}"
            )
        _nonempty_str(behaviour, "kind", where)
        dim = _nonempty_str(behaviour, "dim", where)
        if dim not in TRUST_DIMENSIONS:
            raise DishonestScriptError(
                f"{where}.dim={dim!r} is not one of the six published trust dimensions "
                f"{sorted(TRUST_DIMENSIONS)}"
            )
        _nonempty_str(behaviour, "type", where)
        out.append(behaviour)
    return tuple(out)


def behaviour_kinds(manifest: Mapping[str, Any]) -> list[str]:
    """The approved behaviour ``kind`` sequence — the exact list SPEC S2 grades against."""
    return [str(behaviour["kind"]) for behaviour in scripted_behaviours(manifest)]


def _episode_budget(manifest: Mapping[str, Any]) -> int:
    budget = _require(manifest, "episode_budget", int, "manifest")
    if budget < FIRST_OBSERVED_EPISODE:
        raise DishonestScriptError(
            f"manifest.episode_budget must be at least {FIRST_OBSERVED_EPISODE}, got {budget}"
        )
    return int(budget)


def _behaviour_weight(manifest: Mapping[str, Any], observation_type: str, where: str) -> float:
    """The published weight for an observation type. Never defaulted (D18)."""
    weights = manifest.get(_WEIGHTS_KEY)
    if not isinstance(weights, Mapping) or observation_type not in weights:
        raise DishonestScriptError(
            f"{where}.type={observation_type!r} has no published weight in "
            f"manifest.{_WEIGHTS_KEY}. The weight is ground truth; a simulator that "
            "invented one would be scoring its own adversary."
        )
    value = weights[observation_type]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DishonestScriptError(
            f"manifest.{_WEIGHTS_KEY}.{observation_type} must be a number, "
            f"got {type(value).__name__}"
        )
    return float(value)


def run_dishonest_script(
    manifest: Mapping[str, Any], seed: int, *, episode: int = FIRST_OBSERVED_EPISODE
) -> list[dict[str, Any]]:
    """Run one episode of the approved dishonest script.

    Returns one plain-JSON record per behaviour in ``manifest.dishonest_store.behaviours``,
    in document order — same length, same order, same ``kind`` strings. SPEC S2 grades that
    equality element for element, so emitting a superset fails exactly as hard as emitting a
    subset, and this function has nothing to add: it copies ``kind``, ``dim``, ``type`` and
    ``claim_type`` off the approved behaviour and derives everything else.

    ``seed`` fixes the incidentals (identifiers and magnitudes) and nothing else; two calls
    with the same ``(manifest, seed, episode)`` are byte-identical under canonical JSON.
    """
    behaviours = scripted_behaviours(manifest)
    budget = _episode_budget(manifest)
    if isinstance(episode, bool) or not isinstance(episode, int):
        raise DishonestScriptError(f"episode must be an integer, got {type(episode).__name__}")
    if not 0 <= episode <= budget:
        raise DishonestScriptError(
            f"episode={episode} falls outside the approved episode budget 0..{budget}"
        )

    store = _require(manifest, "dishonest_store", Mapping, "manifest")
    store_id = _nonempty_str(store, "store_id", "manifest.dishonest_store")
    identity = store.get("business_identity")

    emitted: list[dict[str, Any]] = []
    for index, behaviour in enumerate(behaviours):
        where = f"manifest.dishonest_store.behaviours[{index}]"
        # `claim_type` is legitimately absent/null for `feedback_match`; the other three are
        # validated present by `scripted_behaviours`, so `.get` cannot silently drop one.
        record: dict[str, Any] = {field: behaviour.get(field) for field in _COPIED_FIELDS}
        record["episode"] = int(episode)
        record["sequence"] = index
        record["store_id"] = store_id
        record["business_identity"] = str(identity) if isinstance(identity, str) else None
        # NOT spelled `weight`, and the distinction is load-bearing. A trust observation's
        # `weight` field is a RELATIVE multiplier in [0, 1] — a buyer's track record (R14) —
        # while this is the published ABSOLUTE weight for the observation type, which the
        # engine looks up itself from `type`. `trust.scoring.score` raises
        # `InvalidObservationWeight` on a `weight` of 2.0 (measured), so a record spelling it
        # `weight` would be a record that cannot be handed to the engine it is aimed at.
        record["published_weight"] = _behaviour_weight(manifest, str(behaviour["type"]), where)
        # One episode per day, counted from the first observed episode — the replay rule's
        # own words. The trust engine decays each observation from its own day.
        record["observed_day"] = int(episode) - FIRST_OBSERVED_EPISODE
        record.update(_incidentals(seed, episode, index, str(behaviour["kind"]), store_id))
        emitted.append(record)
    return emitted


def _incidentals(seed: int, episode: int, index: int, kind: str, store_id: str) -> dict[str, Any]:
    """The seeded, behaviour-agnostic detail of one emitted record.

    Deliberately says nothing about *what* the behaviour is: these are the order the buyer
    placed and the size of the gap between what was pitched and what arrived. A field here
    that varied by ``kind`` would be this module quietly authoring the attack.
    """
    rng = _rng(seed, episode, index, kind)
    return {
        "order_id": f"sim-order-{rng.getrandbits(32):08x}",
        "buyer_id": f"sim-buyer-{rng.getrandbits(24):06x}",
        # A gap the buyer can see: the pitched value understates the delivered one by this
        # fraction, or the promise is missed by this many days.
        "gap_ratio": round(rng.uniform(0.10, 0.45), 4),
        "days_late": rng.randint(1, 9),
        "seed": int(seed),
    }


def run_dishonest_campaign(
    manifest: Mapping[str, Any], seed: int, *, episodes: int | None = None
) -> list[dict[str, Any]]:
    """Replay the approved script once per episode, episodes 1..``episode_budget``.

    This is the schedule ``manifest.expected_trust_trajectory_replay_rule`` states, and the
    only schedule the published trajectory numbers describe. ``episodes`` may shorten the
    run for a bounded test but never lengthen it past the approved budget: an episode the
    human did not approve a budget for is not part of the ground truth.

    The returned records are in non-decreasing episode order, and within an episode in the
    manifest's own behaviour order.
    """
    budget = _episode_budget(manifest)
    if isinstance(episodes, bool):
        raise DishonestScriptError("episodes must be an integer, not a bool")
    last = budget if episodes is None else int(episodes)
    if last < 0 or last > budget:
        raise DishonestScriptError(
            f"episodes={last} is outside the approved episode budget 0..{budget}"
        )
    stream: list[dict[str, Any]] = []
    for episode in range(FIRST_OBSERVED_EPISODE, last + 1):
        stream.extend(run_dishonest_script(manifest, seed, episode=episode))
    return stream
