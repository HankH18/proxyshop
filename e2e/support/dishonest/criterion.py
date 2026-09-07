"""S2's pass/fail numbers, read out of the human-approved manifest and nowhere else.

This is the module SPEC S2's final clause is about: *"Checkable against the manifest, not the
trust engine's own config."* Everything ``e2e/test_dishonest.py`` grades against comes through
:func:`s2_criterion`, and :func:`s2_criterion` reads ``fixtures/manifest.json``.

Why that is a real distinction and not bookkeeping
--------------------------------------------------
``trust.scoring`` publishes its own ``BLACKLIST_THRESHOLD``. A test that imported it, ran the
engine, and asserted the engine's score fell below the engine's threshold would be true of an
engine whose threshold was ``1.0`` and whose scorer returned ``0.0`` for everybody — it grades
the engine against itself and cannot fail for the reason S2 cares about. The approved manifest
is the third document SPEC A3 requires: the attacker (:mod:`sim.dishonest`) reads it, the
grader (this package) reads it, and a human signed it. So the threshold this file publishes is
``manifest["blacklist_threshold"]``, and the engine's constant is checked *against* it by the
test rather than substituted for it.

Nothing here defaults
---------------------
Every read is strict. A manifest that has lost ``blacklist_threshold``, or carries it as a
string, raises :class:`ManifestCriterionError` — it does not fall back to the engine's number,
to ``0.35``, or to anything else. A silently defaulted criterion is the failure this whole
module exists to prevent: it would make S2 pass against a number no human approved, which is
indistinguishable from S2 not being checked at all.
"""

from __future__ import annotations

__all__ = ["ManifestCriterionError", "S2Criterion", "s2_criterion"]

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class ManifestCriterionError(ValueError):
    """The approved manifest does not state a criterion S2 can be graded against."""


@dataclass(frozen=True)
class S2Criterion:
    """The S2 success criterion, entirely in the human-approved manifest's own numbers.

    Attributes:
        blacklist_threshold: ``manifest["blacklist_threshold"]``. A store scoring **below**
            this is delisted. Not ``trust.scoring.BLACKLIST_THRESHOLD``.
        episode_budget: ``manifest["episode_budget"]``. The catch has to happen inside it.
        dishonest_store_id: the store ``manifest["dishonest_store"]`` names as the adversary.
        dishonest_identity: its ``business_identity`` — the key a blacklist row is bound to,
            which is what stops a delisted operator returning under a fresh ``store_id``.
        honest_store_ids: every other store on the approved roster. These are the positive
            control: a platform that blacklists all five passes S2's first half and destroys
            the product, so the honest stores are asserted on in the same episode.
        seed: ``manifest["seed"]`` — the approved run seed (S4/C9: one seed, one run).
        scripted_kinds: the approved behaviour ``kind`` sequence, for reporting. The
            element-for-element grading of it is ``services/sim/tests/test_dishonest.py``'s.
    """

    blacklist_threshold: float
    episode_budget: int
    dishonest_store_id: str
    dishonest_identity: str
    honest_store_ids: tuple[str, ...]
    seed: int
    scripted_kinds: tuple[str, ...]

    def describe(self) -> str:
        """One line naming the approved numbers, for an assertion message.

        Failure messages quote this rather than the engine's constants on purpose: a reader
        of a red S2 test needs to see which document the verdict was taken from.
        """
        return (
            f"manifest criterion: blacklist_threshold={self.blacklist_threshold} "
            f"episode_budget={self.episode_budget} adversary={self.dishonest_store_id!r} "
            f"({self.dishonest_identity!r}) honest={list(self.honest_store_ids)} "
            f"seed={self.seed} behaviours={len(self.scripted_kinds)}"
        )


def _mapping(document: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(document, Mapping):
        raise ManifestCriterionError(
            f"{where} must be a JSON object, got {type(document).__name__}"
        )
    return document


def _number(document: Mapping[str, Any], key: str, where: str) -> float:
    if key not in document:
        raise ManifestCriterionError(
            f"{where} must publish {key!r}; S2 is graded against the approved document and "
            "this module will not substitute the trust engine's own constant for it"
        )
    value = document[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestCriterionError(f"{where}.{key} must be a number, got {type(value).__name__}")
    return float(value)


def _integer(document: Mapping[str, Any], key: str, where: str) -> int:
    if key not in document:
        raise ManifestCriterionError(f"{where} must publish {key!r}")
    value = document[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestCriterionError(
            f"{where}.{key} must be an integer, got {type(value).__name__}"
        )
    return int(value)


def _text(document: Mapping[str, Any], key: str, where: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestCriterionError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def s2_criterion(manifest: Mapping[str, Any]) -> S2Criterion:
    """The S2 criterion this manifest states. Strict: nothing is defaulted or inferred.

    Args:
        manifest: the parsed, digest-verified ``fixtures/manifest.json``, as
            ``fixtures.manifest.load_manifest`` returns it.

    Returns:
        The approved threshold, budget, adversary and honest roster.

    Raises:
        ManifestCriterionError: the document does not state the criterion — a missing or
            mistyped ``blacklist_threshold`` / ``episode_budget``, no ``dishonest_store``, a
            roster with no honest store to act as the positive control, or an adversary that
            is not itself on the roster.
    """
    document = _mapping(manifest, "manifest")

    threshold = _number(document, "blacklist_threshold", "manifest")
    if not 0.0 < threshold < 1.0:
        raise ManifestCriterionError(
            f"manifest.blacklist_threshold={threshold} is not a score a store can be on "
            "either side of; a threshold of 0 catches nobody and a threshold of 1 catches "
            "everybody, and neither is a criterion"
        )

    budget = _integer(document, "episode_budget", "manifest")
    if budget < 1:
        raise ManifestCriterionError(
            f"manifest.episode_budget={budget} leaves no episode for the platform to catch "
            "the adversary in"
        )

    adversary = _mapping(document.get("dishonest_store"), "manifest.dishonest_store")
    dishonest_id = _text(adversary, "store_id", "manifest.dishonest_store")
    dishonest_identity = _text(adversary, "business_identity", "manifest.dishonest_store")

    behaviours = adversary.get("behaviours")
    if not isinstance(behaviours, Sequence) or isinstance(behaviours, (str, bytes)):
        raise ManifestCriterionError("manifest.dishonest_store.behaviours must be a list")
    if not behaviours:
        raise ManifestCriterionError(
            "manifest.dishonest_store.behaviours is empty, so there is no attack to catch and "
            "S2 would be vacuously satisfied by a platform that does nothing"
        )
    kinds = tuple(
        _text(_mapping(row, f"manifest.dishonest_store.behaviours[{index}]"), "kind", "behaviour")
        for index, row in enumerate(behaviours)
    )

    stores = document.get("stores")
    if not isinstance(stores, Sequence) or isinstance(stores, (str, bytes)) or not stores:
        raise ManifestCriterionError("manifest.stores must list the approved store roster")
    rostered = tuple(
        _text(_mapping(row, f"manifest.stores[{index}]"), "store_id", "store")
        for index, row in enumerate(stores)
    )
    if dishonest_id not in rostered:
        raise ManifestCriterionError(
            f"manifest.dishonest_store.store_id={dishonest_id!r} is not on manifest.stores "
            f"{list(rostered)}; an adversary nobody solicits cannot be caught by an auction"
        )
    honest = tuple(store_id for store_id in rostered if store_id != dishonest_id)
    if not honest:
        raise ManifestCriterionError(
            "the approved roster carries no honest store, so S2's positive control cannot be "
            "asserted and a platform that blacklists everybody would pass"
        )

    return S2Criterion(
        blacklist_threshold=threshold,
        episode_budget=budget,
        dishonest_store_id=dishonest_id,
        dishonest_identity=dishonest_identity,
        honest_store_ids=honest,
        seed=_integer(document, "seed", "manifest"),
        scripted_kinds=kinds,
    )
