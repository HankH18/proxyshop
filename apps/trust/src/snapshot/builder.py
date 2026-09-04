"""The one versioned TrustSnapshot the exchange consumes.

R12. The exchange needs three things from trust and must not have to assemble them from
three shapes: **what the score is**, **whether the store is blacklisted**, and **whether we
have seen enough of it to trust the number**. This module serves exactly that, versioned, for
every store in one call.

Two flags, and why each is a flag rather than something the exchange computes
-----------------------------------------------------------------------------
``blacklisted`` is resolved through :func:`trust.scoring.is_blacklisted`, which keys on
**business identity** and fails closed. Handing the exchange a raw score and letting it
compare against a threshold would put a second, divergent blacklist policy in a second
service — and the one in the exchange would not know that a store re-registered under a new
``store_id``.

``low_data`` marks a store the exchange should treat as *unknown* rather than *average*. It
matters because the two look identical in the score: the neutral Beta(2,2) prior serves 0.5,
and 0.5 is also what a store with a genuinely mixed record serves. Without the flag the
exploration slice cannot tell "nobody has bought from them yet" from "half their orders go
wrong", and a new honest store is either starved of traffic or given the benefit of the
doubt it has not earned. The threshold is the manifest's ``new_store_prior_n``, read not
chosen.

Counting clean episodes without episode identifiers
---------------------------------------------------
An "episode" is one completed round of evidence. When the caller can say how many there were
it should — pass ``episodes`` on the store record, or tag observations with an ``episode`` —
and :func:`clean_episodes` uses it. Absent both, it derives the count as *the largest k such
that every dimension evidence can actually reach carries at least k positive observations*,
which is deliberately conservative: a store observed on one dimension only has not completed
a round, and reading its score as established is exactly the mistake ``low_data`` exists to
prevent.

"Every dimension evidence can reach" is five of the six, and which five is read off the
approved routing table rather than chosen here — see :data:`EPISODE_FLOOR_DIMENSIONS`. Taking
the min over all six instead made the flag inoperable: ``feedback_match`` takes no
verification outcome by policy, so its count stayed at zero and pinned the floor at zero for
every store forever. A flag that is always on carries no information, and this one is the
only thing standing between "we have never seen this store" and "half its orders go wrong".

No ranking and no exploration policy here (T-064 non-goal): this package publishes the
inputs, and the exchange decides what to do with them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# RELATIVE, not `from trust.scoring import ...`: this package is reachable under two dotted
# names, and the frozen acceptance suite runs with `-o pythonpath=` and ONLY the repo root on
# `sys.path`, where `trust.` does not resolve at all. A relative import follows whichever
# spelling is executing, and the scoring package's own binding makes both yield one object.
from ..scoring import (
    CLAIM_TYPE_DIMENSIONS,
    NEW_STORE_PRIOR_N,
    SCORE_VERSION,
    TRUST_DIMENSIONS,
    business_identity_of,
    is_blacklisted,
    score,
)
from .delisting import delisting_events

__all__ = [
    "EPISODE_FLOOR_DIMENSIONS",
    "SNAPSHOT_VERSION",
    "build_snapshot",
    "clean_episodes",
    "store_entry",
]

#: The served snapshot's shape version. The exchange client caches on it and refreshes when
#: it changes: a cache keyed on nothing serves a five-dimension snapshot forever after the
#: sixth dimension lands, and every ranking decision made from it is quietly stale.
SNAPSHOT_VERSION = "trust-snapshot-1.0.0"

#: Observation types that count towards a *clean* episode. A negative outcome does not make
#: an episode dirty for this purpose — ``low_data`` asks "have we seen enough?", not "was it
#: good?"; that second question is what the score itself answers.
_EVIDENCE_TYPES = frozenset(
    {"verified", "fulfilled", "contradicted", "mismatch_return", "severe_policy"}
)
_POSITIVE_TYPES = frozenset({"verified", "fulfilled"})


def _episode_floor_dimensions() -> tuple[str, ...]:
    """The dimensions a completed clean episode is counted over.

    The derived floor asks "has a whole round of evidence happened?", and a round is made of
    the evidence **the network can obtain on its own initiative** — a claim it verified
    against the catalog, a promise it reconciled against the webhook. It cannot be made of
    evidence that only exists if a third party volunteers it.

    That is the whole distinction, and it lands exactly on ``feedback_match``. The approved
    manifest states it as policy rather than as an accident of this deployment: that dimension
    "Takes NO verification outcome at all. It is the post-purchase, buyer-reported match
    between pitch and delivery (R14)". Buyer feedback IS evidence and IS scored — this
    package's sibling ``trust.feedback`` produces exactly such observations, and the manifest's
    own dishonest-store script emits one with ``claim_type: null``. It simply cannot be waited
    for. Counting it in the floor made the floor ``min(..., 0)`` for every store nobody had
    yet left feedback about, which pinned it at zero and made ``low_data`` — one of the two
    flags this package exists to publish — permanently ``True``. Measured: a store with forty
    clean rounds over every other dimension derived 0 clean episodes.

    The approved ``claim_type -> dimension`` table is read as the concrete expression of
    "network-obtained", because it is ground truth this package does not author and it names
    precisely the five: the four verification dimensions plus the two reconciliation lands on.
    It is NOT read as a prediction that a future manifest might route a claim type to
    ``feedback_match`` — the same manifest forbids that in the line quoted above, so that
    branch is dead by policy and this is a derivation, not an escape hatch.
    """
    routed = {str(dim) for dim in CLAIM_TYPE_DIMENSIONS.values()}
    reachable = tuple(dim for dim in TRUST_DIMENSIONS if dim in routed)
    # Fail safe rather than fail empty. An empty routing table would make the floor a min over
    # nothing; `min(())` raises and `0` would silently flag every store as established. Six
    # dimensions is the conservative reading, and it is the behaviour that was there before.
    return reachable or TRUST_DIMENSIONS


#: The dimensions :func:`clean_episodes` derives its floor over. See
#: :func:`_episode_floor_dimensions` — it is the reachable subset of the six, and the reason
#: it is a subset is written down there.
EPISODE_FLOOR_DIMENSIONS: tuple[str, ...] = _episode_floor_dimensions()


def _field(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def clean_episodes(store: Any, observations: Iterable[Any]) -> int:
    """How many completed clean episodes this store has. See the module docstring.

    Three sources, in order of how much the caller actually knows:

    1. an explicit ``episodes`` count on the store record;
    2. an ``episode`` identifier on the observations — episodes with no negative outcome;
    3. the derived floor: the largest ``k`` such that every dimension in
       :data:`EPISODE_FLOOR_DIMENSIONS` carries ``k`` positive observations.

    Positive observations on a dimension OUTSIDE that set — buyer feedback on
    ``feedback_match`` — are evidence and are scored as such; they simply cannot lower this
    count. A floor taken over "every dimension that carries evidence" would drop an
    established store from five clean episodes to one the moment its first piece of buyer
    feedback arrived, which would make receiving evidence a penalty.

    The three sources answer subtly different questions and can disagree, which matters when
    reading the number back. Source 2 counts episodes with **no negative outcome**; source 3
    counts positives and never looks at negatives. So a store with five clean rounds and two
    hundred contradictions derives 5 here and would tag as 0. Source 3's answer is the one
    ``low_data`` wants — the flag asks "have we seen enough of this store?", not "was what we
    saw any good?", and a store with two hundred contradictions is emphatically not unknown;
    it is known and blacklisted, which is a different field. The divergence was invisible
    while the derived floor could only ever return 0.
    """
    declared = _field(store, "episodes")
    if isinstance(declared, int) and not isinstance(declared, bool):
        return max(declared, 0)

    rows = list(observations)
    tagged = [row for row in rows if _field(row, "episode") is not None]
    if tagged:
        dirty: set[Any] = set()
        seen: set[Any] = set()
        for row in tagged:
            episode = _field(row, "episode")
            seen.add(episode)
            if str(_field(row, "type", "")) not in _POSITIVE_TYPES:
                dirty.add(episode)
        return len(seen - dirty)

    per_dimension = dict.fromkeys(EPISODE_FLOOR_DIMENSIONS, 0)
    for row in rows:
        dimension = str(_field(row, "dim", ""))
        if dimension in per_dimension and str(_field(row, "type", "")) in _POSITIVE_TYPES:
            per_dimension[dimension] += 1
    return min(per_dimension.values()) if per_dimension else 0


def store_entry(store: Any, *, blacklist: Any, as_of: Any) -> dict[str, Any]:
    """One store's entry in the served snapshot."""
    store_id = _field(store, "store_id")
    observations = list(_field(store, "observations", ()) or ())
    snapshot = score(observations, as_of=as_of)
    episodes = clean_episodes(store, observations)
    evidence = sum(1 for row in observations if str(_field(row, "type", "")) in _EVIDENCE_TYPES)
    return {
        "store_id": str(store_id) if store_id is not None else None,
        "business_identity": business_identity_of(store),
        "score": snapshot["score"],
        "confidence": snapshot["confidence"],
        "score_version": snapshot["score_version"],
        "dims": snapshot["dims"],
        "blacklisted": bool(is_blacklisted(blacklist, store, as_of=as_of)),
        "low_data": bool(episodes < NEW_STORE_PRIOR_N),
        "episodes": episodes,
        "observations": len(observations),
        "decided_observations": evidence,
        "as_of": snapshot["as_of"],
    }


def build_snapshot(stores: Iterable[Any], *, blacklist: Any, as_of: Any) -> dict[str, Any]:
    """Build the versioned TrustSnapshot the exchange consumes.

    Args:
        stores: store records carrying ``store_id``, ``business_identity`` and the store's
            trust ``observations``.
        blacklist: anything exposing ``lookup(business_identity)``. Reads fail closed, so an
            unavailable blacklist flags every store rather than admitting them all.
        as_of: the explicit instant every score is decayed against. Never a clock — an
            exchange that cached a snapshot computed against ``now()`` could not tell a stale
            entry from a fresh one.

    Returns:
        ``{version, score_version, dimensions, as_of, stores, delistings}`` where ``stores``
        is keyed by ``store_id`` and each entry carries ``store_id``, ``score``,
        ``confidence``, all six ``dims`` with ``alpha``/``beta``/``decayed_at``/``coverage``,
        ``blacklisted`` and ``low_data``.

        All six dimensions always appear, including ``catalog_claim_accuracy``: a five-dim
        snapshot is the exact regression D53 exists to prevent, and a consumer that has to
        ask whether the sixth is present will get it wrong.

        ``delistings`` is S2's second half (T-237): the ``blacklisted`` and
        ``blacklist_expired`` ledger events this snapshot's scores and the registry imply at
        ``as_of``, ready for ``trust.events.append``. It is a RECOMMENDATION, not the
        registry's answer — ``entry["blacklisted"]`` still reports only what the registry
        already says, so a caller that ignores ``delistings`` sees exactly what it saw
        before. Nothing here mutates the registry or writes anything; see
        :mod:`.delisting` for why the decision is pure.
    """
    entries: dict[str, dict[str, Any]] = {}
    for store in stores:
        entry = store_entry(store, blacklist=blacklist, as_of=as_of)
        key = entry["store_id"]
        if key is None:
            continue
        entries[key] = entry
    return {
        "version": SNAPSHOT_VERSION,
        "score_version": SCORE_VERSION,
        "dimensions": list(TRUST_DIMENSIONS),
        "as_of": as_of,
        "stores": entries,
        "delistings": delisting_events(entries.values(), blacklist=blacklist, as_of=as_of),
    }
