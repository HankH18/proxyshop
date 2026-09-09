"""Candidate retrieval and fit scoring for the exchange (T-031).

What this package is for
------------------------
The exchange's job list in DESIGN §Architecture opens with "intent-cluster assignment,
candidate retrieval (Neo4j vector + attributes)". This package is both of those steps, plus
the fit score the ranker consumes.

:mod:`~exchange.retrieval.clusters` is the first one — the join between the ``cluster_id`` a
buyer's clarifier mints by hashing the query and the NAMED catalogue clusters a merchant's
envelope authorises. Without it every store answered ``204 cluster_not_pursued`` and every
shortlist was empty; read that module's docstring for what it resolves against and what
nothing in this repository populates.

The rest of this package is the second one:

    intent  →  RetrievalQuery  →  CandidateSource  →  R19 hard filter  →  relevance  →
    features  →  Reranker (port)  →  FitAssessment  →  ledger

and its single output for the next ticket is ``FitAssessment.fit_score``, which enters the one
published rank formula as ``intent_match``. Nothing else about the formula lives here: the
``RankingWeights`` object (``w_m``, ``w_e``, ``w_t``, ``w_v``, ``w_d``) is `packages/contracts`'
and applying it is T-032's. DESIGN says it plainly — "``intent_match`` comes from
retrieval+rerank, but the combination is exactly the published formula" — and the two
weighting layers never mix.

The five rules this package exists to keep
------------------------------------------
0. **A result must be ABOUT what was asked** (:mod:`~exchange.retrieval.relevance`). A vector
   index answers every query with its top ``k``, so a catalogue with nothing on the subject
   returns exactly as many rows as one that serves it perfectly. Measured on the served route
   before that module existed: ``"a walnut coffee table for the lounge"`` came back as four
   supplements. Relevance is decided on whole-word agreement between the shopper's own words
   and the PLATFORM's observed identity of the product — never a seller's prose (D55) — and a
   candidate it refuses is reported on ``RetrievalResult.off_topic`` with the reason rather
   than dropped.
1. **Hard constraints are filters, and they are decided here** (R19). Pushing an
   ``AttributeFilter`` into Cypher narrows the fetch; it does not *decide* anything. Every
   hard constraint is re-evaluated locally against whatever the source returned, so the
   guarantee holds for any source — including a lenient one, which is exactly what the tests
   drive. Undecidable is never satisfied: a missing attribute, a reading in another unit, and
   an op the module cannot evaluate all fail closed.
2. **Fit is deterministic under the doubles** (A2). The reranker is a port with a
   deterministic default, embeddings go through T-012's ``EmbeddingProvider``, and the result
   is ordered by fit with ``product_id`` as a stable tie-break — so a shortlist never depends
   on the order rows came back in.
3. **Fit is blind** (R11). :class:`RerankItem` carries a similarity and a preference
   alignment. A store's tier, its fees and its envelope are not in the interface, so no
   implementation behind the port can read them even by accident.
4. **Fit inputs are logged per bid, for audit** (A2). :func:`record_fit_scores` writes one
   frozen-vocabulary ``bid_placed`` event per bid, carrying the score, the features that
   produced it and the reranker that scored it — and records an unassessed product as
   unassessed rather than inventing a number for it.

Wiring
------
::

    from exchange.retrieval import CandidateRetrieval, GraphCandidateSource, record_fit_scores

    retrieval = CandidateRetrieval(GraphCandidateSource(session))
    result = retrieval.retrieve(intent, limit=8)
    record_fit_scores(recorder, auction_id=auction_id, bids=bids, assessments=result.assessments)

Offline, swap :class:`GraphCandidateSource` for
:class:`~exchange.retrieval.sources.InMemoryCandidateSource`; everything else is unchanged,
which is the point of the seam.
"""

from __future__ import annotations

from .catalogue import (
    CATALOG_SNAPSHOT_PREFIX,
    GraphCatalogSnapshots,
    graph_catalog_from_env,
)
from .clusters import (
    CATEGORY_WEIGHT,
    CONSTRAINT_WEIGHT,
    MAX_CATALOGUE_CLUSTERS,
    SOURCE_ASSIGNED,
    SOURCE_STATED,
    SOURCE_UNASSIGNED,
    TERM_PHRASE_WORD_CAP,
    TERM_WEIGHT,
    ClusterAssignment,
    ClusterRow,
    IntentClusterCatalogue,
    NoIntentClusters,
    StaticIntentClusterCatalogue,
    assign_cluster,
    configure_clusters,
    intent_clusters_of,
)
from .criteria import (
    CONSTRAINT_OPS,
    DEFAULT_CANDIDATE_LIMIT,
    MAX_CANDIDATE_LIMIT,
    NEUTRAL_ALIGNMENT,
    PREFERENCE_DIRECTIONS,
    CriterionVerdict,
    HardCriterion,
    MalformedIntent,
    RefusedPreference,
    RetrievalQuery,
    SoftPreference,
    UndecidableCriterion,
    build_query,
)
from .fit import (
    FIT_LEDGER_KIND,
    FitAssessment,
    FitFeatures,
    FitLogError,
    annotate_bid_payload,
    intent_match_by_bid,
    record_fit_scores,
)
from .relevance import (
    MIN_SHARED_SHARE,
    MIN_SHARED_TERMS,
    OFF_TOPIC_DETAIL,
    PHRASE_BREAK_WORDS,
    STOPWORDS,
    RelevanceVerdict,
    TopicalRelevance,
    candidate_surface,
    content_terms,
    head_term,
    identity_surface,
    variant_surface,
)
from .rerank import (
    DETERMINISTIC_RERANKER_SIMILARITY_SHARE,
    NEUTRAL_SIMILARITY,
    RERANKER_INTERFACE_VERSION,
    DeterministicReranker,
    Reranker,
    RerankerContractError,
    RerankItem,
    read_rerank,
)
from .roster import (
    DEFAULT_SOLICITED_SHOPS,
    GraphShopRoster,
    NoShopRoster,
    ShopRoster,
    ShopRosterSource,
    SolicitedShop,
    graph_roster_from_env,
    graph_sessions_from_env,
)
from .service import (
    RETRIEVAL_LATENCY_BUDGET_MS,
    CandidateRetrieval,
    ExcludedCandidate,
    RetrievalResult,
)
from .sources import (
    LOCAL_FILTER_OVERSAMPLE,
    VARIANT_NAMES_PER_PRODUCT,
    CandidateSource,
    GraphCandidateSource,
    InMemoryCandidateSource,
    VariantCandidate,
    attribute_rows,
    make_candidate,
)

__all__ = [
    "CATALOG_SNAPSHOT_PREFIX",
    "CATEGORY_WEIGHT",
    "CONSTRAINT_OPS",
    "CONSTRAINT_WEIGHT",
    "DEFAULT_CANDIDATE_LIMIT",
    "DEFAULT_SOLICITED_SHOPS",
    "DETERMINISTIC_RERANKER_SIMILARITY_SHARE",
    "FIT_LEDGER_KIND",
    "LOCAL_FILTER_OVERSAMPLE",
    "VARIANT_NAMES_PER_PRODUCT",
    "MAX_CANDIDATE_LIMIT",
    "MAX_CATALOGUE_CLUSTERS",
    "MIN_SHARED_SHARE",
    "MIN_SHARED_TERMS",
    "NEUTRAL_ALIGNMENT",
    "NEUTRAL_SIMILARITY",
    "OFF_TOPIC_DETAIL",
    "PHRASE_BREAK_WORDS",
    "PREFERENCE_DIRECTIONS",
    "RERANKER_INTERFACE_VERSION",
    "RETRIEVAL_LATENCY_BUDGET_MS",
    "SOURCE_ASSIGNED",
    "SOURCE_STATED",
    "SOURCE_UNASSIGNED",
    "STOPWORDS",
    "TERM_PHRASE_WORD_CAP",
    "TERM_WEIGHT",
    "CandidateRetrieval",
    "CandidateSource",
    "ClusterAssignment",
    "ClusterRow",
    "CriterionVerdict",
    "DeterministicReranker",
    "ExcludedCandidate",
    "FitAssessment",
    "FitFeatures",
    "FitLogError",
    "GraphCandidateSource",
    "GraphCatalogSnapshots",
    "GraphShopRoster",
    "HardCriterion",
    "InMemoryCandidateSource",
    "IntentClusterCatalogue",
    "MalformedIntent",
    "NoIntentClusters",
    "NoShopRoster",
    "RefusedPreference",
    "RerankItem",
    "Reranker",
    "RerankerContractError",
    "RelevanceVerdict",
    "RetrievalQuery",
    "RetrievalResult",
    "ShopRoster",
    "ShopRosterSource",
    "SoftPreference",
    "TopicalRelevance",
    "VariantCandidate",
    "SolicitedShop",
    "StaticIntentClusterCatalogue",
    "UndecidableCriterion",
    "annotate_bid_payload",
    "assign_cluster",
    "attribute_rows",
    "build_query",
    "candidate_surface",
    "content_terms",
    "configure_clusters",
    "graph_catalog_from_env",
    "graph_roster_from_env",
    "graph_sessions_from_env",
    "intent_clusters_of",
    "head_term",
    "identity_surface",
    "intent_match_by_bid",
    "make_candidate",
    "variant_surface",
    "read_rerank",
    "record_fit_scores",
]
