"""The cross-store network prior — and the mechanism that keeps it blind to discounts.

R17 splits the learning loop in two along a line that is easy to state and easy to violate:

* **Pitches pool.** Which value propositions and which claims win in a cluster is an
  observation about buyers, it is the same observation whoever made it, and the platform is
  allowed to learn it across every store. That is what this module builds.
* **Discount elasticity does NOT pool.** What a discount buys is a fact about one store's
  margins, its brand and its customers. Pooling it would let the platform teach store B the
  depth store A had to go to — which is a price signal moving between competitors through the
  platform, and the reason the criterion exists at all.

**Why blindness is structural here rather than promised.** A builder written to "ignore" the
discount fields ignores the ones the author remembered. `dict(record)`, `record.items()`,
`{**record}` and `for k, v in record` all read every field including the ones nobody thought
about, and the resulting prior can then depend on a discount without a single line mentioning
one. So this module never enumerates a record. It asks for :data:`PRIOR_RECORD_FIELDS` by name
and reads nothing else, the allowlist is checked at import for a name that mentions a discount,
and :func:`prior_view` is the only path from a record into the builder. That makes the claim
observable: feed the builder a mapping that logs its lookups and the log is the proof.

Everything this module returns is a frozen dataclass of tuples and ints. Counts are integers, so
replaying the same evidence in a different order gives a byte-identical prior; rates are derived
at render time, where float association cannot accumulate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .grid import DEFAULT_DEPTH_BUCKETS

#: The substring that marks a record field as discount evidence.
DISCOUNT_MARKER = "discount"

#: The ONLY fields :func:`build_network_prior` is permitted to read off an outcome record.
#:
#: This is the blindness mechanism, not documentation of it. Adding a field here is the only way
#: to widen what the prior can see, and :func:`reject_discount_fields` refuses at import time to
#: let that field be a discount one.
PRIOR_RECORD_FIELDS: tuple[str, ...] = (
    "cluster_id",
    "store_id",
    "value_prop",
    "pitch_claims",
    "commitments",
    "won",
)


def reject_discount_fields(fields: Sequence[str]) -> None:
    """Refuse an allowlist that names a discount field. Called at import on the real one."""
    named = sorted(f for f in fields if DISCOUNT_MARKER in str(f).lower())
    if named:
        raise ValueError(
            f"the network prior's allowlist names discount fields {named}. R17 forbids pooling "
            "discount elasticity across stores; a store learns its own depth from its own "
            "outcomes (see store_agent.learning.state)."
        )


reject_discount_fields(PRIOR_RECORD_FIELDS)


def field(record: Any, name: str) -> Any:
    """One named field off a record, whether it is a mapping, a dataclass or a model.

    Named lookup only. There is deliberately no "give me everything" counterpart: the absence of
    one is what makes :data:`PRIOR_RECORD_FIELDS` binding rather than advisory.
    """
    if isinstance(record, Mapping):
        return record.get(name)
    getter = getattr(record, "get", None)
    if callable(getter):
        try:
            return getter(name)
        except TypeError:  # a `get` that is not a mapping's — fall through to attributes
            pass
    return getattr(record, name, None)


def prior_view(record: Any) -> dict[str, Any]:
    """The allowlisted projection of a record. The ONLY way evidence enters the prior."""
    return {name: field(record, name) for name in PRIOR_RECORD_FIELDS}


def _labels(value: Any) -> tuple[str, ...]:
    """A record's claim/commitment list as a tuple of labels, whatever shape it arrived in."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Mapping):
        return tuple(sorted(str(k) for k in value))
    if isinstance(value, Sequence):
        out = []
        for item in value:
            if isinstance(item, Mapping):
                label = item.get("key") or item.get("label") or item.get("name")
                if label:
                    out.append(str(label))
                continue
            text = str(item)
            if text:
                out.append(text)
        return tuple(out)
    text = str(value)
    return (text,) if text else ()


@dataclass(frozen=True)
class Tally:
    """How often one label was pitched in a cluster, and how often it was there for a win."""

    label: str
    wins: int
    observations: int


@dataclass(frozen=True)
class ClusterPrior:
    """What the network has seen in one cluster. Nothing here is about price."""

    cluster_id: str
    observations: int
    wins: int
    stores: int
    value_props: tuple[Tally, ...]
    pitch_claims: tuple[Tally, ...]
    commitments: tuple[Tally, ...]


@dataclass(frozen=True)
class NetworkPrior:
    """The platform's cross-store pitch evidence, by cluster. Immutable and shareable.

    Two stores are handed the SAME prior object on purpose: it is frozen tuples all the way
    down, so sharing it cannot become a channel between them. See
    :class:`store_agent.learning.state.StoreLearningState`.
    """

    clusters: tuple[ClusterPrior, ...]
    observations: int


class _ClusterTally:
    """Scratch accumulator. Local to one build call — never module state, never in a prior."""

    __slots__ = (
        "cluster_id",
        "observations",
        "wins",
        "stores",
        "value_props",
        "pitch",
        "commitments",
    )

    def __init__(self, cluster_id: str) -> None:
        self.cluster_id = cluster_id
        self.observations = 0
        self.wins = 0
        self.stores: set[str] = set()
        self.value_props: dict[str, list[int]] = {}
        self.pitch: dict[str, list[int]] = {}
        self.commitments: dict[str, list[int]] = {}

    @staticmethod
    def _bump(table: dict[str, list[int]], label: str, won: bool) -> None:
        slot = table.setdefault(label, [0, 0])
        slot[0] += 1 if won else 0
        slot[1] += 1

    def add(self, view: Mapping[str, Any]) -> None:
        won = bool(view.get("won"))
        self.observations += 1
        self.wins += 1 if won else 0
        store_id = view.get("store_id")
        if store_id:
            self.stores.add(str(store_id))
        for label in _labels(view.get("value_prop")):
            self._bump(self.value_props, label, won)
        for label in _labels(view.get("pitch_claims")):
            self._bump(self.pitch, label, won)
        for label in _labels(view.get("commitments")):
            self._bump(self.commitments, label, won)

    @staticmethod
    def _freeze(table: Mapping[str, list[int]]) -> tuple[Tally, ...]:
        return tuple(
            Tally(label=label, wins=counts[0], observations=counts[1])
            for label, counts in sorted(table.items())
        )

    def freeze(self) -> ClusterPrior:
        return ClusterPrior(
            cluster_id=self.cluster_id,
            observations=self.observations,
            wins=self.wins,
            stores=len(self.stores),
            value_props=self._freeze(self.value_props),
            pitch_claims=self._freeze(self.pitch),
            commitments=self._freeze(self.commitments),
        )


def build_network_prior(records: Any) -> NetworkPrior:
    """Pool pitch outcomes across stores into a cluster-keyed prior. Blind to every discount.

    Args:
        records: an iterable of outcome rows. Mappings, dataclasses and pydantic models all
            work; only :data:`PRIOR_RECORD_FIELDS` is ever read off one, by name.

    Returns:
        A frozen :class:`NetworkPrior`. Byte-identical for the same evidence in any order, and
        byte-identical whether or not the records carry discount fields — because the discount
        fields are never looked at.

    A row naming no cluster is dropped: a prior is indexed by cluster, and filing clusterless
    evidence under the empty string invents a cluster nobody can query.
    """
    tallies: dict[str, _ClusterTally] = {}
    total = 0
    for record in records or ():
        view = prior_view(record)
        cluster_id = str(view.get("cluster_id") or "")
        if not cluster_id:
            continue
        tally = tallies.get(cluster_id)
        if tally is None:
            tally = tallies[cluster_id] = _ClusterTally(cluster_id)
        tally.add(view)
        total += 1
    return NetworkPrior(
        clusters=tuple(tallies[c].freeze() for c in sorted(tallies)),
        observations=total,
    )


def cluster_prior(prior: NetworkPrior | None, cluster_id: str) -> ClusterPrior | None:
    """The prior for one cluster, or ``None`` when the network has seen nothing there."""
    if prior is None:
        return None
    wanted = str(cluster_id)
    for cluster in prior.clusters:
        if cluster.cluster_id == wanted:
            return cluster
    return None


def _rate(wins: int, observations: int) -> float:
    return round(wins / observations, 6) if observations else 0.0


def _rendered(tallies: tuple[Tally, ...]) -> list[dict[str, Any]]:
    return [
        {
            "label": t.label,
            "wins": t.wins,
            "observations": t.observations,
            "win_rate": _rate(t.wins, t.observations),
        }
        for t in tallies
    ]


def best_label(tallies: tuple[Tally, ...], minimum: int = 1) -> str | None:
    """The label with the highest win rate, ties broken alphabetically so it is deterministic."""
    ranked = [t for t in tallies if t.observations >= minimum]
    if not ranked:
        return None
    return min(ranked, key=lambda t: (-_rate(t.wins, t.observations), t.label)).label


def to_context_priors(prior: NetworkPrior) -> dict[str, dict[str, Any]]:
    """Render a prior into the store context's `network_priors` shape.

    `hooks.ToolHooks.get_network_prior` puts `network_priors[cluster]` straight into a `Claim`
    value, so the result must be plain JSON-safe data. ``depth_buckets`` rides along because the
    context carries it and because it is a constant grid — the rungs, not what any store learned
    about them, which is exactly the line R17 draws.
    """
    return {
        cluster.cluster_id: {
            "depth_buckets": list(DEFAULT_DEPTH_BUCKETS),
            "observations": cluster.observations,
            "wins": cluster.wins,
            "win_rate": _rate(cluster.wins, cluster.observations),
            "stores": cluster.stores,
            "value_props": _rendered(cluster.value_props),
            "pitch_claims": _rendered(cluster.pitch_claims),
            "commitments": _rendered(cluster.commitments),
        }
        for cluster in prior.clusters
    }


__all__ = [
    "DISCOUNT_MARKER",
    "PRIOR_RECORD_FIELDS",
    "ClusterPrior",
    "NetworkPrior",
    "Tally",
    "best_label",
    "build_network_prior",
    "cluster_prior",
    "field",
    "prior_view",
    "reject_discount_fields",
    "to_context_priors",
]
