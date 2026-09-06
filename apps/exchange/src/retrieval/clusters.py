"""Intent-cluster assignment — the exchange's first job, and the one nothing performed.

The defect this module exists to remove
---------------------------------------
Two namespaces met at ``POST /v1/bid-requests`` and could never match. Measured, live, on
the real services over loopback::

    >> POST http://127.0.0.1:60266/v1/bid-requests  -> 204  (store-northroast)
       declined, reason: cluster_not_pursued

``apps/buyer/svc/src/intent/clarifier.py::_cluster_id`` mints an intent's ``cluster_id`` by
hashing what the shopper asked for — ``cl-306c4c3b28e29cfd`` — because that is the identity a
clarifier can compute with no catalogue in scope. A merchant's approval record, meanwhile,
authorises bidding inside **named** catalogue clusters — ``cluster-espresso`` — and
``store_agent.runtime.context.AuctionContext.pursues`` is a set membership over exactly those
names. A content hash is never a member of a set of names, so every store declined every
auction and every shortlist was empty.

Whose job the join is
---------------------
``DESIGN.md:34`` opens the exchange's responsibilities with "intent-cluster assignment,
candidate retrieval (Neo4j vector + attributes)". The buyer's line (``DESIGN.md:33``) does not
mention it, and ``clarifier.py``'s own contract — "takes no auction client, no exchange handle
and no HTTP session" — makes the buyer structurally unable to perform it. So it is here, in
the package DESIGN names, next to the retrieval rule it shares its inputs with.

What it resolves against, stated plainly
----------------------------------------
An assignment needs a **catalogue of named clusters to assign to**, and no service in this
repository publishes one. That is measured, not assumed:

* ``services/ingest/src/graph/upsert.py::upsert_intent_cluster`` is the writer for the
  ``IntentCluster`` node whose own docstring calls it "the retrieval-side grouping T-031 and
  the exchange key off". It has **zero callers of any kind** — its only other mentions are
  two ``__all__`` entries, one re-export in ``services/ingest/src/graph/__init__.py``, and a
  string inside a skip-set in ``services/ingest/tests/test_graph.py:1092``. The
  ``IntentCluster`` dataclass is constructed exactly once repo-wide, in a ``parametrize``
  lambda that never touches a session. ``apply_upserts`` is a closed dispatcher over eight
  ``UpsertOp`` kinds and ``intent_cluster`` is not one of them, so no catalog ingest can
  write the node even in principle. ``services/ingest/src/graph/schema.py`` still creates a
  uniqueness constraint for it: the graph has an index for a label nothing populates.
* No ``MATCH (:IntentCluster)`` exists anywhere, so nothing reads it either.
* There is no Postgres table of clusters — the merchant-approval table's ``pursue_clusters`` column,
  the learned-policy table's ``cluster_id`` column and ``app.intents.cluster_id`` are opaque columns with
  no referenced table, no foreign key and no CHECK — and no registry module, and no
  catalogue JSON.

The vocabulary *is* authored, though, and by a real product path — which is why this module
takes ``(cluster_id, label)`` rows rather than inventing a shape. A merchant's
``pursue_clusters`` comes from ``apps/merchant/svc/src/onboarding/flow.py::_clusters``, which
refuses free text outright ("a free-text cluster name cannot be resolved to a cluster id")
and resolves the merchant's prose against an **option list** of ``{cluster_id, label}`` pairs
carried on the interview question. That option list is the named vocabulary, in the one place
it exists — and nothing publishes it anywhere the exchange can read. The lists that exist today
are hand-written: ``fixtures/interviews/northwind-outfitters.json:73-77`` (the ``options`` array
on the ``pursue_clusters`` question) and ``apps/merchant/svc/tests/test_onboarding_interview.py``'s
``_OPTIONS``. No route, service or UI generates one.

So this module does not pretend to read a populated catalogue. It takes one through a port,
:class:`IntentClusterCatalogue`, and the implementation that is real **today** is
:class:`StaticIntentClusterCatalogue`, stated by the operator in the exchange's deployment
document beside the seller registry and the trust snapshot. That is the same convention, and
the same honesty, as ``composition._trust_snapshot``: the exchange cannot invent which
clusters its catalogue holds any more than it can invent which stores are blacklisted, so a
person states it, once, in configuration — with the same ``{cluster_id, label}`` spelling the
onboarding interview already uses, so the day a service publishes that vocabulary this port
is what it fills.

The consequence is stated rather than hidden: **an exchange whose deployment names no
clusters assigns none**, every intent keeps whatever ``cluster_id`` it arrived with, and a
store whose approval record pursues names still declines. That is the pre-existing behaviour,
unchanged, which is what makes wiring this a deployment decision rather than a silent one.

The four rules
--------------
1. **Never invent a member.** An intent that matches nothing is assigned nothing — the
   :class:`ClusterAssignment` carries ``cluster_id=None`` and the evidence it weighed. A
   "closest" cluster with no supporting evidence would authorise a store to bid on an auction
   its merchant never approved, which is that record's whole purpose.
2. **Never overwrite a name the caller already stated.** If the intent's own ``cluster_id`` is
   a cluster the catalogue knows, it is kept and reported as :data:`SOURCE_STATED`. Only an
   id the catalogue does not know — a clarifier hash, or a stale name — is replaced.
3. **Deterministic** (A2/S4). No clock, no RNG, no embedding call. Ties break on the lowest
   ``cluster_id``, so two runs on one intent and one catalogue assign the same cluster.
4. **Evidence, not a score.** Every assignment carries the specific facts that decided it —
   ``category=coffee``, ``brew_method=espresso``, ``term:espresso machine`` — because "why is
   this auction addressed to cluster-espresso" is a question an operator will ask about a
   shortlist, and a bare 4.0 does not answer it. Stated honestly: today that explanation reaches
   a CALLER of :func:`assign_cluster` and the tests, and no further. The auction route reads
   only :attr:`ClusterAssignment.assigned` and :meth:`ClusterAssignment.applied_to`; nothing
   logs the rest, because ``apps/exchange`` contains no logging call site at all (T-308), and
   the ``auction_opened`` ledger payload's key vocabulary is frozen at ``(intent_id,
   cluster_id, roster_size)``. The evidence is built so that the answer EXISTS when a sink for
   it does; it is not yet surfaced.

Wiring
------
::

    from exchange.retrieval import assign_cluster, intent_clusters_of

    assignment = assign_cluster(intent, intent_clusters_of(app))
    intent = assignment.applied_to(intent)     # a copy; the caller's mapping is untouched

The deployment document's half::

    {"sellers": [...],
     "intent_clusters": [
       {"cluster_id": "cluster-espresso",
        "label": "Espresso machines",
        "category": "coffee",
        "terms": ["espresso machine", "espresso"],
        "attributes": {"brew_method": "espresso"}}]}
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ingest.graph.model import canonical_text, slug

__all__ = [
    "CATEGORY_WEIGHT",
    "CONSTRAINT_WEIGHT",
    "MAX_CATALOGUE_CLUSTERS",
    "MAX_QUERY_CHARS_SCANNED",
    "MIN_ASSIGNMENT_SCORE",
    "SOURCE_ASSIGNED",
    "SOURCE_STATED",
    "SOURCE_UNASSIGNED",
    "TERM_WEIGHT",
    "ClusterAssignment",
    "ClusterRow",
    "IntentClusterCatalogue",
    "NoIntentClusters",
    "StaticIntentClusterCatalogue",
    "assign_cluster",
    "configure_clusters",
    "intent_clusters_of",
]

#: What a matching ``category`` contributes. The heaviest single signal: a category is the
#: one field of an `Intent` that is already vocabulary rather than prose, so an intent and a
#: cluster agreeing on it are agreeing about the same axis the catalogue is grouped on.
CATEGORY_WEIGHT = 3.0

#: What one satisfied hard constraint contributes. Heavier than a term because it is
#: structured — ``brew_method eq espresso`` is the shopper's own must-have, decided by R19's
#: comparators, not a word that happened to appear in a sentence.
CONSTRAINT_WEIGHT = 2.0

#: What one matched term contributes. The lightest, because free text is the weakest evidence
#: this module has and DESIGN forbids retrieval by product name alone.
TERM_WEIGHT = 1.0

#: The least evidence that may address an auction to a cluster. One :data:`TERM_WEIGHT` is
#: deliberately BELOW it, so a single incidental word cannot authorise a store to bid.
#: Measured before this bar existed: the row ``{"cluster_id": "cluster-coffee", "label":
#: "Coffee"}`` and the query *"a walnut coffee table for the lounge"* assigned
#: ``cluster-coffee`` on the evidence ``('term:coffee',)`` — a furniture shopper addressed to a
#: coffee merchant's approval record. Rule 1 says never invent a member, and "score > 0" made that
#: rule true only nominally.
#:
#: 2.0 is exactly the bar that admits ONE STRUCTURED signal — a category match
#: (:data:`CATEGORY_WEIGHT`) or one satisfied hard constraint (:data:`CONSTRAINT_WEIGHT`) — or
#: TWO independent words, and refuses one word alone. A cluster that can only ever be found by
#: a single common word should say so with a ``category`` or an ``attributes`` entry, which is
#: the catalogue stating what it groups rather than the exchange guessing.
MIN_ASSIGNMENT_SCORE = 2.0

#: How many clusters a deployment may name. The catalogue is walked once per auction on the
#: request path, so its size is time a shopper waits; and a vocabulary this large is a data
#: set that belongs in the graph rather than in a configuration file.
MAX_CATALOGUE_CLUSTERS = 2000

#: The intent arrived naming a cluster the catalogue knows. Nothing was changed.
SOURCE_STATED = "stated"

#: This module chose the cluster from the evidence below.
SOURCE_ASSIGNED = "assigned"

#: Nothing matched. The intent keeps whatever id it arrived with, and a store whose approval record
#: pursues names will decline it — which is the honest outcome, not a bug in this module.
SOURCE_UNASSIGNED = "unassigned"

#: Word characters, for whole-word term matching. ``"espresso"`` must not match inside
#: ``"espressos"``-adjacent noise like ``"despresso"``, and a naive ``in`` does exactly that.
_WORD = re.compile(r"[a-z0-9]+")


def _constraints_of(value: Any) -> tuple[Any, ...]:
    """``value`` as a tuple of hard constraints; ``()`` when it is not a collection at all.

    ``assign_cluster`` promises in its own docstring that it "never raises on a malformed
    intent", because "a cluster assignment that raised would turn a bad hard constraint into a
    500 on a route that has a 422 for it". ``tuple(fields.get("hard_constraints") or ())`` did
    not keep that promise: ``intent`` is annotated ``dict[str, Any]`` on ``POST /auctions``, so
    a scalar there is ACCEPTED by validation and reaches this line, where ``tuple(1e400)``
    raises ``TypeError: 'float' object is not iterable`` and an anonymous caller gets a 500
    (T-270 — measured on both the freshly built app and the served ``exchange.main:app``).

    Only the raising case changes. A falsy value still yields ``()`` and every iterable is
    still passed to ``tuple`` unchanged — including a bare ``str``, which this deliberately
    does NOT re-interpret as a single constraint: that is a separate judgement about what a
    caller meant, and making it here would silently alter an input the function already
    accepts today.
    """
    if not value:
        return ()
    try:
        return tuple(value)
    except TypeError:
        return ()


def _fields(obj: Any) -> Mapping[str, Any]:
    """Read a protocol object as a mapping, whether it arrived as one or as a model.

    The same tolerance ``retrieval.criteria._fields`` applies, and for the same reason: an
    intent reaches the exchange as JSON on one path and as a pydantic model on another, and a
    rule that only worked for one of them would be a rule that worked in tests.
    """
    if isinstance(obj, Mapping):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dict(dump())
    return {}


def _folded_terms(values: Iterable[Any]) -> tuple[str, ...]:
    """De-duplicated, non-empty terms reduced to their word sequence, in a stable order.

    Reduced to WORDS — ``"heat-exchange"`` and ``"Heat Exchange"`` both become
    ``"heat exchange"`` — because that is the form :func:`_haystack` reduces the shopper's
    query to, and two spellings of one normalisation would mean a catalogue term written with
    a hyphen silently matched nothing. Done here, once per deployment, rather than per auction.
    """
    seen: dict[str, None] = {}
    for raw in values:
        folded = " ".join(_WORD.findall(canonical_text(str(raw))))
        if folded:
            seen.setdefault(folded, None)
    return tuple(seen)


@dataclass(frozen=True)
class ClusterRow:
    """One NAMED catalogue cluster, exactly as the catalogue states it.

    This is the vocabulary side of the join — the ``cluster-espresso`` half. It deliberately
    carries more than ``ingest.graph.model.IntentCluster``'s ``(cluster_id, label)``: a label
    alone cannot decide whether an intent belongs to a cluster, and the whole point of this
    module is to decide that from evidence rather than from string similarity.

    Attributes:
        cluster_id: the name a merchant's approval record authorises. Never empty.
        label: the human-facing name. Also matched, as ONE term and therefore as an exact
            phrase — ``"Espresso machines"`` is found in ``"espresso machines for the office"``
            and NOT in ``"an espresso machine for the office"``, because the plural is part of
            the phrase. The label is a convenience, not a substitute for ``terms``: a catalogue
            that wants the singular, an abbreviation or a synonym to match states it. Splitting
            the label into independent words instead would drop the evidence bar to one common
            word, which :data:`MIN_ASSIGNMENT_SCORE` exists to keep it above.
        category: the `Intent.category` vocabulary term this cluster groups, folded. ``None``
            when the cluster is not category-scoped.
        terms: the phrases a shopper uses for this cluster, folded.
        attributes: ``{field: value}`` — what products in this cluster carry. Matched against
            the intent's hard constraints through R19's own comparators, so ``brew_method eq
            espresso`` is decided the same way retrieval decides it against a product.
    """

    cluster_id: str
    label: str = ""
    category: str | None = None
    terms: tuple[str, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        cluster_id = str(self.cluster_id).strip()
        if not cluster_id:
            raise ValueError(
                "a catalogue cluster needs a non-empty cluster_id; an unnamed cluster cannot "
                "be the member of an approval record's pursue_clusters that this module exists to find"
            )
        object.__setattr__(self, "cluster_id", cluster_id)
        object.__setattr__(self, "label", str(self.label))
        raw_category = self.category
        object.__setattr__(
            self, "category", None if raw_category is None else canonical_text(str(raw_category))
        )
        # The label is an implicit term. A catalogue that names a cluster "Espresso machines"
        # should not also have to repeat those words under `terms` to be findable by them.
        object.__setattr__(self, "terms", _folded_terms((*self.terms, self.label)))
        object.__setattr__(
            self,
            "attributes",
            {slug(str(key)): value for key, value in dict(self.attributes).items()},
        )

    @classmethod
    def from_mapping(cls, raw: Any) -> ClusterRow:
        """Build a row from the shape a deployment document writes."""
        fields = _fields(raw)
        if not fields:
            raise ValueError(f"a catalogue cluster must be a JSON object, got {raw!r}")
        # `is None`, never truthiness. `fields.get("terms") or ()` reads `[]`, `0`, `""` and
        # `{}` all as "the key is absent", so a document that states `"attributes": []` — a
        # real typo, an array where an object belongs — would be silently degraded to "this
        # cluster has no attributes" and simply stop matching. That is the exact failure mode
        # this whole module refuses: a vocabulary that loads and finds nothing.
        raw_terms = fields.get("terms")
        terms: Sequence[Any] = ()
        if raw_terms is not None:
            if isinstance(raw_terms, (str, bytes)) or not isinstance(raw_terms, Sequence):
                raise ValueError(
                    f"cluster {fields.get('cluster_id')!r}: 'terms' must be an array of "
                    f"strings, got {type(raw_terms).__name__}; a bare string is one term "
                    f"written as its letters, not a list of them"
                )
            terms = raw_terms
        raw_attributes = fields.get("attributes")
        attributes: Mapping[str, Any] = {}
        if raw_attributes is not None:
            if not isinstance(raw_attributes, Mapping):
                raise ValueError(
                    f"cluster {fields.get('cluster_id')!r}: 'attributes' must be a JSON object "
                    f"of {{field: value}}, got {type(raw_attributes).__name__}"
                )
            attributes = raw_attributes
        return cls(
            cluster_id=str(fields.get("cluster_id") or ""),
            label=str(fields.get("label") or ""),
            category=fields.get("category"),
            terms=tuple(str(term) for term in terms),
            attributes=attributes,
        )


class IntentClusterCatalogue(Protocol):
    """Anything that can name the clusters an intent may be assigned to.

    The seam exists so the source can change without the rule changing — the same shape
    ``retrieval.sources.CandidateSource`` has, and for the same reason. The graph-backed
    implementation is deliberately **not** written here: ``upsert_intent_cluster`` has no
    product caller, so a ``GraphIntentClusterCatalogue`` would read a node label nothing in
    this repository ever writes, and a path that can only ever answer empty is worse than an
    absent one because it looks like coverage.
    """

    name: str

    def clusters(self) -> Sequence[ClusterRow]: ...


@dataclass(frozen=True)
class NoIntentClusters:
    """The default: a catalogue that knows no clusters, so nothing is ever assigned.

    Fail-closed, in the same direction as ``ranking.serving.catalog_of``'s
    ``NoCatalogSnapshots`` and ``trust_snapshot_of``'s empty snapshot. An exchange nobody has
    told about a cluster vocabulary must not guess one: guessing would put an auction in front
    of a store whose merchant authorised a different catalogue, which is the one thing the
    approval record exists to prevent.
    """

    name: str = "none"

    def clusters(self) -> Sequence[ClusterRow]:
        return ()


@dataclass(frozen=True)
class StaticIntentClusterCatalogue:
    """The clusters an operator stated, in the deployment document.

    The implementation that is real today. See this module's docstring for why it is
    configuration rather than a graph read.
    """

    rows: tuple[ClusterRow, ...] = ()
    name: str = "deployment"

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))
        # Checked HERE and not only in `from_rows`, because a cap that lives in one
        # constructor is a cap on one caller. The class is public and `configure_auctions`
        # takes any catalogue, so a deployment that assembled its rows some other way could
        # hand the request path an unbounded walk.
        if len(self.rows) > MAX_CATALOGUE_CLUSTERS:
            raise ValueError(
                f"a cluster catalogue may name at most {MAX_CATALOGUE_CLUSTERS} clusters, got "
                f"{len(self.rows)}; it is walked once per auction on the request path, so its "
                f"size is time a shopper waits"
            )
        seen: dict[str, int] = {}
        for row in self.rows:
            seen[row.cluster_id] = seen.get(row.cluster_id, 0) + 1
        duplicated = sorted(cluster_id for cluster_id, count in seen.items() if count > 1)
        if duplicated:
            raise ValueError(
                f"intent_clusters names {duplicated} more than once; the later row would "
                f"silently decide which terms and attributes that cluster is found by"
            )

    @classmethod
    def from_rows(cls, raw: Any) -> StaticIntentClusterCatalogue:
        """Build a catalogue from the array a deployment document writes."""
        if raw is None:
            return cls()
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise ValueError(f"'intent_clusters' must be a JSON array, got {type(raw).__name__}")
        if len(raw) > MAX_CATALOGUE_CLUSTERS:
            # Refused BEFORE the rows are parsed: an over-cap document is rejected without
            # first building the objects it is over the cap for. `__post_init__` enforces the
            # same bound for every other way a catalogue can be built.
            raise ValueError(
                f"'intent_clusters' names {len(raw)} clusters; this exchange reads at most "
                f"{MAX_CATALOGUE_CLUSTERS}. The catalogue is walked once per auction on the "
                f"request path, so its size is time a shopper waits"
            )
        return cls(rows=tuple(ClusterRow.from_mapping(entry) for entry in raw))

    def clusters(self) -> Sequence[ClusterRow]:
        return self.rows


@dataclass(frozen=True)
class ClusterAssignment:
    """Which named cluster this auction is addressed to, and what decided it.

    Attributes:
        cluster_id: the assigned cluster, or ``None`` when nothing matched.
        source: one of :data:`SOURCE_STATED`, :data:`SOURCE_ASSIGNED`,
            :data:`SOURCE_UNASSIGNED`.
        score: the evidence weight the winner carried. ``0.0`` when nothing matched, and for
            :data:`SOURCE_STATED`, where no evidence was weighed because none was needed.
        evidence: the specific facts that decided it, in a stable order.
        considered: how many catalogue clusters were weighed. ``0`` says the exchange has no
            cluster catalogue wired, which is a different condition from "the catalogue had
            nothing for this shopper" and must not be read as the same one.
        catalogue: the catalogue's ``name``, so a shortlist can be traced to the vocabulary
            that produced it.
    """

    cluster_id: str | None
    source: str
    score: float = 0.0
    evidence: tuple[str, ...] = ()
    considered: int = 0
    catalogue: str = ""

    @property
    def assigned(self) -> bool:
        """Whether this module chose a cluster the intent did not already state."""
        return self.source == SOURCE_ASSIGNED

    def applied_to(self, intent: Any) -> dict[str, Any]:
        """``intent`` as a plain mapping, carrying the assigned ``cluster_id``.

        A COPY, always. The caller's request body is FastAPI's, and mutating it in place would
        make the auction's own record of what was asked for depend on when it was read.
        """
        applied = dict(_fields(intent))
        if self.cluster_id:
            applied["cluster_id"] = self.cluster_id
        return applied


def _reading(key: str, value: Any) -> dict[str, Any]:
    """One cluster attribute in the shape ``Candidate.attributes`` projects.

    ``{key, value_string, value_number, value_bool, unit}`` — the exact shape
    :meth:`~.criteria.HardCriterion.decide` reads, so the comparison below is R19's own and
    not a second one written here. ``unit`` is always ``None``: a cluster states what its
    products *are*, not what they measure, and a constraint stated in a unit is therefore
    undecidable against it — which ``decide`` already fails closed on.
    """
    is_bool = isinstance(value, bool)
    is_number = isinstance(value, (int, float)) and not is_bool
    return {
        "key": key,
        "value_string": None if (is_bool or is_number) else str(value),
        "value_number": float(value) if is_number else None,
        "value_bool": value if is_bool else None,
        "unit": None,
    }


def _satisfies(criterion: Any, key: str, value: Any) -> bool:
    """Whether a cluster's stated attribute value satisfies one hard constraint.

    Decided through R19's own comparators — :meth:`~.criteria.HardCriterion.decide` — over a
    single synthetic reading, rather than through a second comparison written here. A cluster
    is a group of products, so "does this cluster satisfy the constraint" is the same question
    as "does a product in it", and asking it twice in two spellings is how the two answers
    drift apart.
    """
    from .criteria import HardCriterion, MalformedIntent  # noqa: PLC0415

    try:
        parsed = HardCriterion.from_mapping(criterion)
    except (MalformedIntent, ValueError, TypeError):
        # An undecidable constraint contributes no evidence. It is NOT raised here: the
        # auction route refuses a malformed intent on its own terms, and a cluster assignment
        # that raised would turn a bad constraint into a 500 on a route that has a 422 for it.
        return False
    return parsed.decide([_reading(key, value)]).satisfied


#: The most of ``intent.query`` that is scanned for catalogue terms. An `Intent`'s query is the
#: shopper's own sentence; past 4 KiB it is not one, and the exchange has no body-size limit and
#: no bound on this field — ``_refuse_an_oversized_intent`` measures ``hard_constraints`` only.
#: Without this, one request's cost is the caller's chosen length multiplied by the catalogue
#: size, spent SYNCHRONOUSLY inside ``async def create_auction``, which blocks the event loop
#: for every other buyer. Measured at the 2000-cluster cap before this bound: a 1,000,000-char
#: query took 3.26s of CPU in one auction. Truncation is stated rather than silent: it changes
#: only which TERMS are found, never which cluster wins for a query of a plausible length.
MAX_QUERY_CHARS_SCANNED = 4096


def _haystack(query_text: str) -> str:
    """The shopper's words, folded once, space-delimited on both ends for whole-word search.

    Computed ONCE per auction and reused for every catalogue row. Folding it inside the
    per-cluster loop instead made the cost of one ``POST /auctions`` the product of the query
    length and the catalogue size — up to :data:`MAX_CATALOGUE_CLUSTERS` NFKC normalisations
    and regex scans of a string the CALLER chose the length of, on the request path. Nothing
    bounds ``intent.query``: ``_refuse_an_oversized_intent`` measures ``hard_constraints``
    only.
    """
    words = _WORD.findall(canonical_text(query_text[:MAX_QUERY_CHARS_SCANNED]))
    return f" {' '.join(words)} " if words else ""


def _term_hits(haystack: str, terms: Sequence[str]) -> tuple[str, ...]:
    """The catalogue terms that appear in the shopper's own words, as whole words.

    Whole words, and phrases as whole word sequences: a substring test reports ``"tea"``
    inside ``"steam"`` and would put a shopper asking for an espresso machine into the tea
    cluster on the strength of one accident.

    ``terms`` are already folded by :meth:`ClusterRow.__post_init__`, so only the delimiters
    are added here — the catalogue is validated once at deployment, not once per auction.
    """
    if not haystack:
        return ()
    return tuple(term for term in terms if f" {term} " in haystack)


def _weigh(
    row: ClusterRow,
    *,
    category: str | None,
    constraints: Sequence[Any],
    haystack: str,
) -> tuple[float, tuple[str, ...]]:
    """One cluster's evidence weight and the facts behind it."""
    score = 0.0
    evidence: list[str] = []

    if category is not None and row.category is not None and category == row.category:
        score += CATEGORY_WEIGHT
        evidence.append(f"category={row.category}")

    for criterion in constraints:
        fields = _fields(criterion)
        name = slug(str(fields.get("field") or ""))
        if name == "_" or name not in row.attributes:
            continue
        if _satisfies(criterion, name, row.attributes[name]):
            score += CONSTRAINT_WEIGHT
            evidence.append(f"{name}={row.attributes[name]}")

    for term in _term_hits(haystack, row.terms):
        score += TERM_WEIGHT
        evidence.append(f"term:{term}")

    return score, tuple(evidence)


def assign_cluster(intent: Any, catalogue: Any) -> ClusterAssignment:
    """Address one intent to a named catalogue cluster.

    Args:
        intent: an `Intent` mapping or pydantic model.
        catalogue: an :class:`IntentClusterCatalogue`.

    Returns:
        The assignment. Never raises on a malformed intent — the auction route refuses those
        on its own terms, with its own message, and a cluster assignment that raised would
        turn a bad hard constraint into a 500 on a route that has a 422 for it.

    The three outcomes, in the order they are decided:

    1. The intent already names a cluster the catalogue knows -> :data:`SOURCE_STATED`,
       unchanged. A caller that named a real cluster is not overruled by an inference.
    2. Some cluster carries evidence -> :data:`SOURCE_ASSIGNED`, highest weight wins, ties
       broken on the lowest ``cluster_id`` so the answer does not depend on catalogue order.
    3. Nothing carries evidence -> :data:`SOURCE_UNASSIGNED`, ``cluster_id=None``, and the
       intent keeps whatever it arrived with. No "closest" cluster: an approval record authorises a
       set, and the nearest miss is not a member of it.
    """
    rows = tuple(getattr(catalogue, "clusters", lambda: ())())
    name = str(getattr(catalogue, "name", "") or "")
    fields = _fields(intent)

    stated = str(fields.get("cluster_id") or "").strip()
    if stated and any(row.cluster_id == stated for row in rows):
        return ClusterAssignment(
            cluster_id=stated,
            source=SOURCE_STATED,
            considered=len(rows),
            catalogue=name,
        )

    raw_category = fields.get("category")
    category = None if raw_category is None else canonical_text(str(raw_category))
    constraints = _constraints_of(fields.get("hard_constraints"))
    # Folded ONCE, outside the loop. See :func:`_haystack`.
    haystack = _haystack(str(fields.get("query") or ""))

    best: tuple[float, str, tuple[str, ...]] | None = None
    for row in rows:
        score, evidence = _weigh(row, category=category, constraints=constraints, haystack=haystack)
        if score < MIN_ASSIGNMENT_SCORE:
            continue
        # `-score` first, then the id: highest weight wins and a tie is broken on the LOWEST
        # cluster id, which is a property of the catalogue's contents rather than of the order
        # a JSON array happened to list them in (S4).
        candidate = (-score, row.cluster_id, evidence)
        if best is None or candidate[:2] < best[:2]:
            best = candidate

    if best is None:
        return ClusterAssignment(
            cluster_id=None,
            source=SOURCE_UNASSIGNED,
            considered=len(rows),
            catalogue=name,
        )
    return ClusterAssignment(
        cluster_id=best[1],
        source=SOURCE_ASSIGNED,
        score=-best[0],
        evidence=best[2],
        considered=len(rows),
        catalogue=name,
    )


# =====================================================================================
# The app-state seam
# =====================================================================================
#: Set on ``app.state``. Read through :func:`intent_clusters_of` rather than by name, so
#: "does this exchange have a cluster catalogue" is one question with one answer.
STATE_ATTR = "intent_clusters"


def configure_clusters(app: Any, catalogue: Any) -> None:
    """Give ``app`` the cluster catalogue its auctions are assigned against."""
    setattr(app.state, STATE_ATTR, catalogue)


def intent_clusters_of(app: Any) -> Any:
    """This app's cluster catalogue, defaulting to :class:`NoIntentClusters`.

    The default is EMPTY rather than absent, and the consequence is the one this module's
    docstring states: an exchange nobody has given a vocabulary assigns nothing and behaves
    exactly as it did before this module existed.
    """
    catalogue = getattr(app.state, STATE_ATTR, None)
    if catalogue is None:
        catalogue = NoIntentClusters()
        setattr(app.state, STATE_ATTR, catalogue)
    return catalogue
