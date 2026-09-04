"""Entity resolution: the same product, sold by two stores, is one product (T-022).

A buyer searching for a shoe should be shown *one* shoe with three prices, not three shoes.
Nothing in the catalog makes that true on its own — each store mints its own product ID (see
:func:`~ingest.adapters.mapping.product_id_for`, which derives the ID from the store *and* the
store's own identifier, deliberately, so two stores can never collide), and so the same trade
item arrives as two unrelated ``Product`` nodes. This package is what puts an edge between
them: ``(Product)-[:SAME_AS {confidence}]->(Product)``, DESIGN §Data models.

The public surface, and where each piece decides something
----------------------------------------------------------

============================  ================================================================
:func:`normalize_gtin`        What counts as an identifier. A GTIN is canonical GTIN-14, check
                              digit validated, placeholder rejected — or it is nothing.
:func:`match`                 What counts as the same product. One pair, one score, and
                              ``linked == (confidence >= threshold)`` with no exceptions.
:func:`resolve`               Which pairs are worth asking about at all, and what edges the
                              answers justify. Pure; writes nothing.
:func:`link_resolved`         The same, then written to the graph with provenance.
============================  ================================================================

Reading order for anyone changing this: :mod:`ingest.er.identity` (what the strings mean),
:mod:`ingest.er.similarity` (how alike two of them are), :mod:`ingest.er.matching` (the one
rule the guarantees are theorems of), :mod:`ingest.er.linking` (blocking, and why edges are
not transitively closed).

Everything above :mod:`ingest.er.linking` is pure — no network, no clock, no datastore, no
randomness — so a resolution decision can be re-derived from the two records that produced it
and a disagreement between two runs is a defect rather than noise.

    >>> left = {"product_id": "p-1", "gtin": "00012345678905", "canonical_name": "Trail Shoe"}
    >>> right = {"product_id": "p-2", "gtin": "012345678905", "canonical_name": "Zapatilla"}
    >>> match(left, right, 0.95).linked          # UPC-A and GTIN-14 are one identifier
    True

``link_resolved`` and the HTTP surface in ``ingest.er.routes`` are the two callers that reach
the graph; the frozen entrypoint ``ingest.main.create_app`` discovers that router by glob, so
``match`` is reachable in a running ``proxyshop-ingest`` process with no wiring anywhere else.
"""

from __future__ import annotations

from .identity import (
    GTIN_LENGTHS,
    canonical_text,
    fold,
    gtin_check_digit,
    is_valid_gtin,
    normalize_gtin,
    tokens,
)
from .linking import (
    DEFAULT_MAX_BLOCK_SIZE,
    MIN_BLOCK_TOKEN_LENGTH,
    ResolutionReport,
    SameAsEdge,
    blocking_keys,
    candidate_pairs,
    edges_as_json,
    link_resolved,
    resolve,
)
from .matching import (
    BRAND_CONFLICT_CEILING,
    CORROBORATION_WEIGHT,
    DEFAULT_MATCH_THRESHOLD,
    MAX_SIMILARITY_CONFIDENCE,
    METHOD_BRAND_CONFLICT,
    METHOD_GTIN,
    METHOD_GTIN_CONFLICT,
    METHOD_NO_EVIDENCE,
    METHOD_SAME_NODE,
    METHOD_SIMILARITY,
    NAME_WEIGHT,
    MatchDecision,
    match,
    read_field,
    record_name,
)
from .similarity import (
    TOKEN_WEIGHT,
    TRIGRAM_ORDER,
    text_similarity,
    token_alignment,
    token_jaccard,
    trigram_dice,
    trigrams,
)

__all__ = [
    "BRAND_CONFLICT_CEILING",
    "CORROBORATION_WEIGHT",
    "DEFAULT_MATCH_THRESHOLD",
    "DEFAULT_MAX_BLOCK_SIZE",
    "GTIN_LENGTHS",
    "MAX_SIMILARITY_CONFIDENCE",
    "METHOD_BRAND_CONFLICT",
    "METHOD_GTIN",
    "METHOD_GTIN_CONFLICT",
    "METHOD_NO_EVIDENCE",
    "METHOD_SAME_NODE",
    "METHOD_SIMILARITY",
    "MIN_BLOCK_TOKEN_LENGTH",
    "NAME_WEIGHT",
    "TOKEN_WEIGHT",
    "TRIGRAM_ORDER",
    "MatchDecision",
    "ResolutionReport",
    "SameAsEdge",
    "blocking_keys",
    "candidate_pairs",
    "canonical_text",
    "edges_as_json",
    "fold",
    "gtin_check_digit",
    "is_valid_gtin",
    "link_resolved",
    "match",
    "normalize_gtin",
    "read_field",
    "record_name",
    "resolve",
    "text_similarity",
    "token_alignment",
    "token_jaccard",
    "tokens",
    "trigram_dice",
    "trigrams",
]
