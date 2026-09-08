"""WHAT THE PLATFORM CHECKED — the catalogue snapshots, out of the crawl (D55).

What this module is, in one sentence: it is the evidence half of the organic/sponsored split,
so that a shop's purchased message is graded against the products the platform actually
crawled instead of against a document an operator typed.

Why it had to exist
-------------------
D55's asymmetry is the thing that makes a persuasion market safe: a scraped shop is an
organic result rendered by a platform-authored pitch, an in-network shop is sponsored and
buys the right to make its own case, and **the seller's purchased message is adversarially
checked against the platform's own snapshot of the catalogue.** The exchange's whole authority
to do that checking rests on holding such a snapshot.

It did not hold one from the crawl. ``ranking/serving.py::catalog_of`` defaults to
``NoCatalogSnapshots`` — a catalogue that knows nothing, so every claim grades ``unsupported``
— and the only other implementation, ``StaticCatalogSnapshots``, is built from an
operator-supplied deployment document. Meanwhile ``services/ingest`` had loaded ten real
supplement storefronts into Neo4j and ``candidate_shops`` was walking that graph on every
served auction to decide the roster. So the two halves were disconnected: the graph knew the
real products, and the verifier graded against hand-authored fiction or against nothing. Every
claim verification on the served route was therefore either vacuous or a measurement of the
operator's own document.

:class:`GraphCatalogSnapshots` is the join. It answers the same one question
``catalog_of()``'s consumers ask — "what does this exchange hold about this store's product?"
— out of :func:`ingest.graph.query.catalogue_entry`.

How a store's own assertion is kept OUT of its own evidence
-----------------------------------------------------------
This is the security argument, so it is stated rather than implied. A snapshot assembled from
data the platform did not itself observe would launder the store's assertion into a platform
observation, and the verifier would then confirm the store's claim against the store's own
word — a check that cannot fail, which is exactly the failure D55's asymmetry exists to
prevent. Four gates, all of them in Cypher in
:data:`~ingest.graph.query._CATALOGUE_ENTRY` rather than in a Python convention, because this
library must survive nodes written by another writer:

1. **An unsourced Store or Product yields no snapshot at all** (``None``), so every claim
   comes back ``unsupported``. The platform has checked nothing about this shop.
2. **An unsourced carrying relation yields no snapshot** (``None``). Unless the platform
   observed a provenanced ``SELLS`` edge or a fully provenanced ``MAKES_OFFER -> Offer -> FOR
   -> Variant <- HAS_VARIANT`` chain, it never saw this store carrying this product, and
   grading the store's claim against somebody else's shelf is not a check.
3. **An unsourced Offer chain keeps the row and loses its price.** The snapshot then carries
   no ``offer`` block at all — no ``price`` key, not a zero — so a price claim answers "the
   catalogue records no ``'price'`` for this product" rather than being compared against a
   number nobody observed. ``lowest_price is None`` means *never checked*, never *free*, and
   this is the same reading :attr:`~ingest.graph.query.ShopCandidate.lowest_price` insists on.
   A fully provenanced chain whose ``Offer`` states no ``price`` lands here too, rather than
   costing the store its whole snapshot: the platform observed the listing, and only the number
   is missing.
4. **An unsourced attribute is dropped from the snapshot**, so its key is outside
   ``claim_verification.verifier.catalog_keys``' vocabulary for this product and a claim on it
   is ``ambiguous`` — undecided rather than laundered.

And one gate the roster deliberately does not have. *Sourced* is not the same as *observed by
us*: ``ingest.graph.model.SOURCE_CLASSES`` includes ``seller_asserted`` and
``owner_statement``, which are perfectly good provenance for "the store said so". A snapshot
built from those is the store's own homework. So only ``Source`` rows whose ``source_class``
is in :data:`~ingest.graph.query.PLATFORM_OBSERVED_SOURCE_CLASSES` — the crawl and the
platform's own instrumentation — count as evidence here. The recorded corpus loads as
``scraped``, so the real crawl passes this gate and a hypothetical seller-fed row does not.

What this module does NOT do
----------------------------
It never widens what the exchange believes. Every failure — an unreachable graph, a driver
that will not build, a Cypher error, a store the crawl never saw — answers ``None``, which is
:class:`~exchange.ranking.verification.NoCatalogSnapshots`' answer, which is ``unsupported``,
which R19 refuses to let satisfy a hard constraint. An outage must not turn an unverifiable
claim into a verified one; that is the one direction this seam is not allowed to fail in.

The vocabulary is the CRAWL's, and what the crawl actually writes is smaller than it looks
-------------------------------------------------------------------------------------------
``claim_verification.verifier.catalog_keys`` decides a claim only on keys the snapshot
carries, in three places: the ``attributes`` block, the ``offer`` block, and the product record
itself. **Measured on the recorded corpus — ten storefronts, 3,093 products loaded through the
real crawl path — the ``attributes`` block is EMPTY**, because
``ingest.adapters.mapping.build_upserts`` emits ``store``/``product``/``sells``/``category``/
``media``/``variant``/``offer`` ops and no ``attribute`` op at all: the graph's
``HAS_ATTRIBUTE`` writer is reached only from ``seed_products``, which nothing outside tests
calls. So for a crawled product the decidable vocabulary is exactly

    price, currency, availability, observed_at        (the graph Offer)
    product_ref, canonical_name, brand, status        (the Product record)

and everything else — including ``list_price``, which is the key the hosted store agent
actually publishes (``store_agent.runtime.bidding.LIST_PRICE_KEY``) — falls outside it and is
attested ``ambiguous``, which costs the store exactly what silence costs.

Two consequences worth stating rather than discovering. ``verification.catalog_units`` reads
only the ``attributes`` block, so ``declared_attributes`` answers ``None`` for a crawl-only
catalogue — which is ESC-020's fail-closed direction (nothing is relaxed) and means a hard
constraint is decidable here only where the crawl has an attribute to decide it on. And **no
synonym table is applied**: the platform states what it observed under the name it observed it
under. Mapping a seller's ``list_price`` onto the crawl's ``price`` would be defensible — they
are the same fact — but it is a product decision with a real false-positive cost (a store
whose price moved since the last crawl would be attested ``contradicted``, which carries the
published penalty), and it belongs to whoever owns the claim vocabulary rather than to the
module that happens to hold both names.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

# From the SUBMODULE rather than the package root, the same way
# `exchange.ranking.verification` imports `catalog_keys`: `ingest/graph/__init__.py`
# re-exports a fixed list and is outside this change's file scope. The name is published in
# `query.__all__` either way, and the submodule path is the same module object the root would
# have handed back.
from ingest.graph.query import (
    PLATFORM_OBSERVED_SOURCE_CLASSES,
    CatalogueAttribute,
    CatalogueEntry,
    catalogue_entry,
    latest_instant,
)

from .roster import graph_sessions_from_env

__all__ = [
    "CATALOG_SNAPSHOT_PREFIX",
    "GraphCatalogSnapshots",
    "graph_catalog_from_env",
]

_log = logging.getLogger(__name__)

#: What every snapshot this module mints names itself, before the pair it is about.
#:
#: The id is stamped on every verdict and covered by its MAC, and the published
#: ``VerificationResult.catalog_snapshot`` is ``minLength: 1`` — so it exists to make a verdict
#: traceable back to the thing that decided it. Naming the SOURCE of the snapshot, not just the
#: pair, is what lets an auditor tell a verdict decided against the crawl apart from one
#: decided against a deployment document, which is the distinction this whole ticket is about.
CATALOG_SNAPSHOT_PREFIX = "neo4j-crawl"


def _attribute_rows(readings: Sequence[CatalogueAttribute]) -> dict[str, dict[str, Any]]:
    """``{key: {value, unit, observed_at}}`` — one row per KEY, not one per reading.

    **A key the graph holds twice becomes a LIST, never the last one read**, and that is a
    correctness rule rather than tidiness. The graph stores a multi-valued attribute as several
    ``AttributeValue`` nodes sharing a key (``voltage`` at ``120 V`` and ``230 V`` is the
    golden set's own example), and a dict keyed on ``key`` silently keeps whichever row sorted
    last — so a store claiming the OTHER true reading would be attested ``contradicted``, with
    the published penalty, for a fact its shelf genuinely carries. A list is also the shape
    ``claim_verification.comparators._compare_multivalued`` is written for: any member matching
    is ``verified``, an empty set is ``unsupported``, and only a claim readable in the members'
    own domain and matching none of them is ``contradicted``.

    Units are folded into the members when the readings DISAGREE about them, because the
    snapshot carries one ``unit`` per row and there is no honest single answer otherwise;
    ``_member_matches`` parses ``"230 V"`` back into a quantity, so nothing is lost. When every
    reading agrees, the unit stays where the comparator expects it.
    """
    grouped: dict[str, list[CatalogueAttribute]] = {}
    for reading in readings:
        grouped.setdefault(reading.key, []).append(reading)

    rows: dict[str, dict[str, Any]] = {}
    for key, group in grouped.items():
        # `latest_instant` rather than `max()`: the graph holds both `…Z` and `…+00:00`
        # spellings of UTC and a text max orders them by punctuation, not by time.
        observed = latest_instant([one.observed_at for one in group])
        if len(group) == 1:
            rows[key] = {
                "value": group[0].value,
                "unit": group[0].unit,
                "observed_at": observed,
            }
            continue
        units = {one.unit for one in group}
        if len(units) == 1:
            value: Any = [one.value for one in group]
            unit = group[0].unit
        else:
            value = [one.value if one.unit is None else f"{one.value} {one.unit}" for one in group]
            unit = None
        rows[key] = {"value": value, "unit": unit, "observed_at": observed}
    return rows


class GraphCatalogSnapshots:
    """Catalogue snapshots read from the platform's own crawl, per (store, product).

    Implements the one method ``catalog_of()``'s consumers require —
    ``snapshot_for(store_id, product_ref=None)`` returning a snapshot mapping or ``None`` —
    which is the same interface :class:`~exchange.ranking.verification.NoCatalogSnapshots` and
    :class:`~exchange.ranking.verification.StaticCatalogSnapshots` implement, and the shape
    :func:`exchange.ranking.verification.snapshot_for` calls with two positional arguments.

    **One session per lookup, taken from a factory.** A ``neo4j.Session`` is explicitly
    single-threaded and this object lives on ``app.state``, shared by every concurrent
    request, so holding one session here would corrupt it — the same rule
    :class:`~exchange.retrieval.roster.GraphShopRoster` states about itself.

    **The cost, stated rather than buried.** ``ranking/serving.py`` asks this source twice per
    candidate per auction — once through ``declared_attributes`` and once through
    ``attest_candidates`` — so a full roster costs ``2 x len(roster)`` point lookups inside
    R10's synchronous window. Both are indexed seeks on ``store_id`` and ``product_id``, and
    the roster is capped at ``MAX_ROSTER_ENTRIES``, so the product is bounded by configuration
    rather than by anything a bidder sends. Nothing is cached between the two calls **on
    purpose**: a verdict is minted per auction from the snapshot held at that moment, and a
    cache is the mechanism by which a corrected catalogue stops re-deciding the next auction.
    """

    name = "neo4j"

    def __init__(
        self,
        sessions: Callable[[], Any],
        *,
        source_classes: Sequence[str] = tuple(sorted(PLATFORM_OBSERVED_SOURCE_CLASSES)),
        freshness_window_days: float | None = None,
    ) -> None:
        """
        Args:
            sessions: a zero-argument callable returning a **context manager** that yields an
                open ``neo4j.Session`` — ``driver.session`` itself satisfies this, and so does
                :func:`~exchange.retrieval.roster.graph_sessions_from_env`.
            source_classes: which ``Source.source_class`` values count as the platform's own
                observation. Defaults to
                :data:`~ingest.graph.query.PLATFORM_OBSERVED_SOURCE_CLASSES`; widening it to
                include ``seller_asserted`` would let a store supply its own evidence, which is
                the failure this module's header describes.
            freshness_window_days: published on every snapshot as ``freshness_window_days``,
                which is what turns on ``claim_verification``'s per-attribute stale-evidence
                gate. ``None`` (the default) publishes no window and runs no gate — the same
                posture ``StaticCatalogSnapshots`` has, because how old a reading may be before
                it stops being evidence is an operator's policy and inventing one here would
                silently turn honest crawled evidence into ``unsupported``.
        """
        if isinstance(source_classes, (str, bytes)):
            # `tuple("scraped")` is seven one-character classes, none of which matches
            # anything, so the silent version of this mistake is a catalogue that answers
            # `None` for every pair in the graph while looking correctly configured — and this
            # constructor's answer to that is `unsupported` for every claim in every auction.
            # `catalogue_entry` refuses the same input for the same reason; refusing it here
            # too means the operator finds out at wiring time rather than per lookup.
            raise TypeError(
                f"source_classes must be a sequence of class names, not the single string "
                f"{source_classes!r}; write ({source_classes!r},)"
            )
        self.sessions = sessions
        self.source_classes = tuple(str(one) for one in source_classes)
        self.freshness_window_days = freshness_window_days

    @contextmanager
    def _session(self) -> Iterator[Any]:
        """One session for one lookup, closed however this exits."""
        opened = self.sessions()
        enter = getattr(opened, "__enter__", None)
        if enter is None:  # a factory that hands back a bare session
            try:
                yield opened
            finally:
                close = getattr(opened, "close", None)
                if callable(close):
                    close()
            return
        with opened as session:
            yield session

    def snapshot_for(
        self, store_id: str, product_ref: str | None = None
    ) -> Mapping[str, Any] | None:
        """This exchange's crawled snapshot of one store's product. **Never raises.**

        Args:
            store_id: the store whose claims are being graded.
            product_ref: the product the AUCTION named — the roster's answer, never the
                bidder's, which is what ``attest_candidates``' ``product_refs`` argument
                enforces one layer up.

        Returns:
            The snapshot mapping, or ``None`` when this exchange holds no crawled evidence it
            may grade a claim against for this pair.

        ``None`` when ``product_ref`` is absent, and that is a refusal rather than a gap. This
        source is per (store, product) because the crawl holds hundreds of products per store
        and a claim is graded against the ONE product the auction is about; handing back the
        whole shelf would be an unbounded read whose only effect is that
        ``claim_verification._resolve_product`` answers ``ambiguous`` for a store carrying more
        than one row. The two spellings differ in cost, not in outcome, so the cheap one wins —
        and a caller that named no product had nothing to resolve against either way.

        ``None`` on every failure, too: an unreachable graph, a driver that will not build, a
        Cypher error. :func:`exchange.ranking.verification.snapshot_for` already treats a
        source that raises as a source that knows nothing, and this method agrees with it
        rather than relying on it, because "I could not check" must resolve to ``unsupported``
        and never to ``verified``.
        """
        name = str(store_id or "")
        if not name or product_ref is None or not str(product_ref):
            return None
        try:
            with self._session() as session:
                entry = catalogue_entry(
                    session,
                    store_id=name,
                    product_id=str(product_ref),
                    source_classes=self.source_classes,
                )
        except Exception as exc:  # noqa: BLE001 — an unreadable catalogue holds no snapshot
            # SAID OUT LOUD, because the denial itself is indistinguishable from the healthy
            # answer: "the graph is down" and "the crawl never saw this pair" both arrive at
            # the ranker as `unsupported`, and only one of them is an operator's problem. The
            # same reasoning `roster_provenance_exclusions` exists for one layer down — a
            # silent refusal reads as "no such thing" — except that this seam has no response
            # field to publish a reason on, so the log is the only channel there is.
            # WARNING and not DEBUG: nothing reaches here on the healthy path (a pair the crawl
            # never saw returns no rows and raises nothing), so anything logged is a real fault.
            _log.warning(
                "graph catalogue: no snapshot for store %r product %r (%s: %s); every claim "
                "this store made is graded unsupported for this auction",
                name,
                str(product_ref),
                type(exc).__name__,
                exc,
            )
            return None
        if entry is None:
            return None
        return self.as_snapshot(entry)

    def as_snapshot(self, entry: CatalogueEntry) -> dict[str, Any]:
        """One crawled entry as the snapshot document ``claim_verification.verify`` reads.

        Shaped to the three places ``claim_verification.verifier._lookup_attribute`` resolves a
        key, in its order of authority:

        * ``attributes`` — the typed block, ``{key: {"value", "unit", "observed_at"}}``, one
          row per key (see :func:`_attribute_rows` for what a repeated key does). Only this
          block carries a unit, which is what ``verification.catalog_units`` reads and what
          ``HardCriterion.decide`` needs before it will compare a constrained quantity. It is
          EMPTY for every product the recorded corpus crawl wrote — see this module's header.
        * ``offer`` — the graph ``Offer``'s own fields as bare scalars, and **present only when
          the platform observed a fully provenanced priced listing.** An absent block is the
          third provenance tier: the row is here, its price is not.
        * the product record itself — ``canonical_name``, ``brand``, ``status``: crawled facts
          that are properties of the ``Product`` node rather than attributes hanging off it.

        ``evidence_refs`` carries the platform-authored ``Source`` ids behind the entry, and
        ``claim_verification._evidence_refs`` appends them to every verdict — so a reader of a
        verdict can go from the claim to the crawl record that decided it without holding this
        object.
        """
        product: dict[str, Any] = {
            "product_ref": entry.product_id,
            "canonical_name": entry.canonical_name,
            "brand": entry.brand,
            "status": entry.status,
            "attributes": _attribute_rows(entry.attributes),
        }
        if entry.offer is not None:
            # Bare scalars, deliberately: `_lookup_attribute` reads the offer block without
            # `attribute_value`, because the offer's `currency` is a label rather than a unit
            # and handing it over as one would make "USD" a dimension.
            product["offer"] = {
                "price": entry.offer.price,
                "currency": entry.offer.currency,
                "availability": entry.offer.availability,
                "observed_at": entry.offer.observed_at,
            }
        snapshot: dict[str, Any] = {
            "snapshot_id": f"{CATALOG_SNAPSHOT_PREFIX}:{entry.store_id}:{entry.product_id}",
            "captured_at": entry.observed_at,
            "evidence_refs": list(entry.source_ids),
            "products": [product],
        }
        if self.freshness_window_days is not None:
            snapshot["freshness_window_days"] = float(self.freshness_window_days)
        return snapshot


def graph_catalog_from_env(env: Mapping[str, str] | None = None) -> GraphCatalogSnapshots | None:
    """A :class:`GraphCatalogSnapshots` on the configured Neo4j, or ``None`` when unconfigured.

    **Its own switch, ``EXCHANGE_RANKING_CATALOG``, rather than the roster's.** The two are
    different powers: ``EXCHANGE_SHOP_ROSTER=graph`` decides who gets ASKED, and this decides
    whose claims get BELIEVED. An operator who turned on organic discovery has not thereby
    said "grade every sponsored claim against my crawl", and the reverse is just as real — a
    deployment serving request-stated rosters of crawled store ids can grade those claims
    against the crawl with no roster query at all. One env var for both would make each
    decision unstateable without the other.

    **It defaults to the roster's setting, and that is the fix rather than a convenience.** An
    exchange with ``EXCHANGE_SHOP_ROSTER=graph`` and no ``catalog`` in its deployment document
    holds a snapshot for nobody, so every claim from every graph-rostered shop grades
    ``unsupported`` and a hard-constrained auction shortlists nobody — the vacuum this ticket
    exists to close. Defaulting the catalogue on with the roster makes the working
    configuration the one an operator already asked for; ``EXCHANGE_RANKING_CATALOG=none``
    turns it back off explicitly.

    **Never connects here, and answers ``None`` rather than raising** — see
    :func:`~exchange.retrieval.roster.graph_sessions_from_env`. ``None`` binds nothing, which
    leaves ``ranking.serving.catalog_of``'s ``NoCatalogSnapshots`` default: it verifies
    nothing, which denies rather than admits. There is no configuration of this function that
    produces a permissive catalogue.

    Args:
        env: the environment to read; the process environment when ``None``.

    Returns:
        The source, or ``None`` when this deployment has not asked for it.
    """
    import os  # noqa: PLC0415 — read at call time so a test can drive `env`

    source = dict(os.environ if env is None else env)
    stated = str(source.get("EXCHANGE_RANKING_CATALOG", "")).strip().lower()
    if not stated:
        stated = str(source.get("EXCHANGE_SHOP_ROSTER", "")).strip().lower()
    if stated != "graph":
        return None
    return GraphCatalogSnapshots(graph_sessions_from_env(source))
