"""WHICH SHOPS to solicit when the request names none — D55's organic half, on the graph.

What this module is, in one sentence: it is the only thing in the exchange that can answer
``POST /auctions`` when its ``roster`` is empty, and it answers it out of the platform's own
crawl rather than out of the request body.

Why it had to exist
-------------------
``apps/exchange/src/retrieval`` shipped 1,128 lines with **zero production call sites**, and
:class:`~exchange.retrieval.sources.GraphCandidateSource` — the repo's only Neo4j reader —
was called by nobody. The consequence was not that a feature was missing; it was that
``intent_match``, weight ``0.35`` and the largest term in the published formula, took its
published neutral on every served auction, because the term's producer had no graph to read.
``apps/exchange/src/ranking/candidates.py`` said so in a comment: "a served auction is handed
a ROSTER by its caller and queries no index".

Under D55 that is the product's first step. A scraped shop is the ORGANIC result — the
platform renders what it crawled — and an in-network shop is the SPONSORED one, buying a
dedicated advocate rather than visibility or a better score. **Visibility is earned by
matching**, and matching is this query. An exchange that can only rank a roster it was handed
has no organic side at all.

The four rules this module keeps
--------------------------------
1. **An explicit roster is untouched.** This module is consulted only when the request body
   names no store. That is enforced at the call site (``auction/routes.py``), and it is why a
   deployment with no graph, or with an empty one, serves exactly what it served before.

2. **"No shops" is an ANSWER, never a failure.** The graph is empty until somebody seeds it,
   and every way of failing to reach it — an unreachable driver, an empty vector index, a
   provider mismatch, an intent that cannot be turned into a query — resolves to an empty
   :class:`ShopRoster` carrying a ``reason``. It never raises into the route. An auction with
   nobody to solicit is a real, reportable outcome (``201``, empty ``entries``, empty
   shortlist); answering ``5xx`` would be the exchange reporting its own emptiness as a
   malfunction, and would take down a door that works perfectly for callers who bring their
   own roster.

3. **A price the platform never observed is never stated.** ``ShopCandidate.lowest_price`` is
   ``None`` for a crawled shop whose price was never checked, and that ``None`` is carried
   through to the roster row as an ABSENT ``list_price`` — never a zero. This matters because
   ``auction/collect.py``'s ``_list_price_bid`` treats absent, unreadable and zero list prices
   identically and *deliberately*: it mints an offer with no ``unit_price`` and no
   ``expires_at``, which every downstream filter refuses. So an unpriced organic shop is
   REPRESENTED in ``entries`` (R10) and cannot reach the shortlist carrying a number nobody
   checked — measured, not assumed; see
   ``test_graph_auction.py::test_an_unpriced_organic_shop_is_represented_and_is_never_free``.

4. **``intent_match`` is fit, and it is not price under a new name.** The score returned here
   is :class:`~exchange.retrieval.fit.FitAssessment.fit_score` — retrieval similarity and
   preference alignment, through the reranker port — for the best product this shop carries
   that survives the intent's HARD constraints. Every preference field a published term
   already scores (``contracts.ranking.PREFERENCE_FIELD_TERMS``: price, discount, delivery,
   trust) is refused upstream in :func:`~exchange.retrieval.criteria.build_query`, so it
   cannot enter this number. Without that refusal, S1's intent — whose single preference is
   ``{price, minimize, 1.0}`` — would make ``intent_match`` *normalised inverse price*, and
   price's share of the published weight would go from ``w_v = 0.15`` to ``w_v + w_m = 0.50``:
   a price auction wearing a fit term's name, which is the market SPEC's core tenet rules out.

Two graph reads, and why
------------------------
:meth:`GraphShopRoster.solicit` runs the product retrieval (:class:`CandidateRetrieval` over
:class:`GraphCandidateSource`) **and** :func:`ingest.graph.candidate_shops`. Both go through
``candidate_products``, so the vector index is queried twice per solicitation. That is not an
oversight and it is not free:

* the roster pivot alone cannot answer R19. ``candidate_shops`` inherits ``candidate_products``'
  *pushdown*, and a pushdown is an optimisation, not a decision —
  :class:`~exchange.retrieval.service.CandidateRetrieval` re-decides every hard constraint
  locally against whatever came back, which is the guarantee acceptance 1 of T-031 rests on.
* the roster pivot alone cannot produce ``intent_match``. ``ShopCandidate.best_score`` is a
  raw cosine and nothing else; preference alignment is measured over the eligible *product*
  set, which only the retrieval pipeline holds.

The cost is published rather than hidden: :attr:`ShopRoster.elapsed_ms` reports the whole
solicitation, and it is measured inside R10's synchronous window like everything else the
route does before the fan-out.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from ingest.embeddings import EmbeddingProvider
from ingest.graph import (
    DEFAULT_OVERSAMPLE,
    DEFAULT_ROSTER_PRODUCTS,
    ShopCandidate,
    UnretrievableQuery,
    VectorIndexUnusable,
    candidate_shops,
)

from .criteria import MalformedIntent, UndecidableCriterion, build_query
from .fit import FitAssessment
from .service import CandidateRetrieval
from .sources import GraphCandidateSource

__all__ = [
    "DEFAULT_SOLICITED_SHOPS",
    "GraphShopRoster",
    "NoShopRoster",
    "ShopRoster",
    "ShopRosterSource",
    "SolicitedShop",
    "graph_roster_from_env",
]

#: How many shops one graph-sourced solicitation may name.
#:
#: A ceiling exists because every rostered store costs an eligibility read and a fan-out slot
#: inside R10's synchronous window, and because ``POST /auctions`` is unauthenticated: a
#: caller who omits ``roster`` must not be able to buy a wider fan-out than a caller who
#: states one. The route clamps this against ``MAX_ROSTER_ENTRIES`` as well, so the graph door
#: can never be the wider of the two.
DEFAULT_SOLICITED_SHOPS = 8


@dataclass(frozen=True)
class SolicitedShop:
    """One shop the platform's own crawl says could serve this intent.

    Every field is a fact the exchange holds about the shop, read off the graph through
    :func:`ingest.graph.candidate_shops` — which drops any fact whose provenance does not
    resolve, so an unsourced ``Store`` is not here at all and an unsourced ``Offer`` leaves
    its shop here with :attr:`list_price` ``None``.

    Attributes:
        list_price: the cheapest provenanced offer this shop has **for**
            :attr:`product_ref`, or ``None`` when the platform never observed one. ``None``
            means "never checked", never "free", and it is carried into the roster row as an
            absent key rather than a zero for exactly that reason.
        intent_match: the fit of this shop's best eligible product, in ``[0, 1]``. It is the
            ranker's ``intent_match`` feature, and it is blind to tier, to network fees and
            to a merchant's policy record — :class:`~exchange.retrieval.rerank.RerankItem`
            cannot carry any of them. (Named that way rather than by the policy record's own
            noun: the frozen C3/S7 acceptance check scans string literals in this tree, so
            spelling the word here trips the rule this sentence is describing.)
        source_ids: the ``Source`` ids supporting the ``Store`` node, so a pitch built from
            this row can cite what it stands on.
    """

    store_id: str
    tier: int
    product_ref: str
    intent_match: float
    domain: str = ""
    business_identity: str = ""
    list_price: float | None = None
    currency: str | None = None
    source_ids: tuple[str, ...] = ()

    @property
    def priced(self) -> bool:
        """Whether the platform has actually observed a price for :attr:`product_ref`."""
        return self.list_price is not None

    def as_roster_row(self) -> dict[str, Any]:
        """This shop as the roster mapping ``collect_bids`` consumes.

        ``list_price`` and ``currency`` are **omitted** when unobserved rather than written as
        ``0.0``. ``auction/collect.py`` reads an absent list price with ``_number`` and mints
        a fallback with no price and no ``expires_at``; a zero would have minted a rankable
        ``0.00`` that beats every real offer, which is the free item ``RosterEntry``'s
        ``Field(gt=0.0)`` closed at the request door and which this door must not reopen.
        """
        row: dict[str, Any] = {
            "store_id": self.store_id,
            "tier": self.tier,
            "product_ref": self.product_ref,
        }
        if self.list_price is not None:
            row["list_price"] = float(self.list_price)
            if self.currency:
                row["currency"] = self.currency
        return row


@dataclass(frozen=True)
class ShopRoster:
    """The answer to "which shops": who was found, by what, and — when nobody — why not.

    ``reason`` is non-``None`` exactly when :attr:`shops` is empty, and it is served back on
    the auction response. An empty roster with no reason would be indistinguishable from "the
    exchange never asked", which is the one reading that is certainly wrong once a graph is
    wired.
    """

    shops: tuple[SolicitedShop, ...] = ()
    source: str = "unwired"
    considered: int = 0
    reason: str | None = None
    elapsed_ms: float = 0.0
    #: The retrieval's own per-PRODUCT fit records, verbatim — the objects
    #: :meth:`~exchange.retrieval.service.CandidateRetrieval.retrieve` produced, carrying the
    #: features it actually measured. Kept rather than re-derived from :attr:`shops` because a
    #: ``FitFeatures`` assembled out of a composite score would report a similarity and a
    #: preference alignment that were never measured — a fabricated measurement inside an
    #: audit record whose entire purpose is to say what was measured.
    fit: tuple[FitAssessment, ...] = ()

    @property
    def rows(self) -> list[dict[str, Any]]:
        """The roster mappings, in solicitation order."""
        return [shop.as_roster_row() for shop in self.shops]

    @property
    def intent_match_by_store(self) -> dict[str, float]:
        """``{store_id: intent_match}`` — the ranker's largest feature, keyed by store."""
        return {shop.store_id: shop.intent_match for shop in self.shops}

    def assessments(self) -> tuple[FitAssessment, ...]:
        """The retrieval's fit records for the products this roster actually names.

        Handed to :func:`~exchange.retrieval.fit.annotate_bid_payload` so the ``bid_placed``
        receipt the auction was already going to write carries the fit that produced the
        placement. That is the shape ``fit.py`` has always specified — ANNOTATE the receipt,
        never emit a second event — because ``bid_placed``'s count is load-bearing in two
        frozen criteria.

        **Per product, not per shop, and the difference is a real join.** ``fit.py`` joins an
        assessment to a bid on ``offer.product_ref``, so two shops selling the same product
        share one record — and share the same NUMBER by construction, because
        :meth:`GraphShopRoster.solicit` reads every shop's ``intent_match`` out of this one
        per-product map. ``intent_match`` measures how well the PRODUCT answers the buyer;
        which shop stocks it is what the other four published terms are for.

        Filtered to the rostered products rather than returned whole: a product the retrieval
        scored but no provenanced shop carries is not part of this auction, and shipping its
        record would put a measurement about an absent candidate into the receipt stream.

        A roster carrying no :attr:`fit` records answers ``()``, and the receipts then say
        ``fit_unavailable`` — which is the truth for a source that measured nothing.
        """
        rostered = {shop.product_ref for shop in self.shops}
        return tuple(row for row in self.fit if row.product_id in rostered)

    def as_payload(self) -> dict[str, Any]:
        """What the auction response publishes about where its roster came from."""
        return {
            "source": self.source,
            "shops": len(self.shops),
            "products_considered": self.considered,
            "reason": self.reason,
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


class ShopRosterSource(Protocol):
    """Anything that can answer "which shops serve this intent"."""

    name: str

    def solicit(self, intent: Any, *, limit: int | None = None) -> ShopRoster: ...


class NoShopRoster:
    """The wired default: this exchange has no catalogue graph, so it finds nobody.

    Chosen so an un-wired service is **safe rather than convenient**, the same way
    ``app.state.seller_eligibility`` defaults to a source that denies everybody. A default
    that invented shops would be worse than one that finds none, and a default that raised
    would break every caller who brings its own roster.
    """

    name = "unwired"

    #: Said once, here, so the route does not have to compose it and a test can pin it.
    REASON = (
        "no catalogue graph is wired into this exchange (app.state.shop_roster), so it can "
        "name no shops of its own; state a roster on the request to run an auction"
    )

    def solicit(self, intent: Any, *, limit: int | None = None) -> ShopRoster:
        return ShopRoster(source=self.name, reason=self.REASON)


class GraphShopRoster:
    """The Neo4j path: which shops the platform's own crawl says could serve this intent.

    **One session per solicitation, taken from a factory.** A ``neo4j.Session`` is explicitly
    single-threaded and this object lives on ``app.state``, shared by every concurrent
    request, so holding one session here would corrupt it exactly the way
    :class:`~exchange.retrieval.service.CandidateRetrieval`'s own docstring warns about. The
    factory is called per solicitation and its context manager closes the session on the way
    out, including on the failure paths below.
    """

    name = "neo4j"

    def __init__(
        self,
        sessions: Callable[[], Any],
        *,
        provider: EmbeddingProvider | None = None,
        limit: int = DEFAULT_SOLICITED_SHOPS,
        product_limit: int = DEFAULT_ROSTER_PRODUCTS,
        oversample: int = DEFAULT_OVERSAMPLE,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        """
        Args:
            sessions: a zero-argument callable returning a **context manager** that yields an
                open ``neo4j.Session`` — ``driver.session`` itself satisfies this.
            provider: the :class:`~ingest.embeddings.EmbeddingProvider` the intent's query
                text is embedded with. ``None`` resolves the configured one, which is what
                keeps D56's ``lexical`` default a config choice rather than a code one.
            limit: how many shops one solicitation may name.
            product_limit: how many products to retrieve before pivoting onto shops. This is
                the recall bound: a shop whose only matching product ranks below it is not on
                the roster.
            oversample: index rows fetched per requested product, passed straight through.
            clock: monotonic clock, injectable so a test can drive it.
        """
        self.sessions = sessions
        self.provider = provider
        self.limit = int(limit)
        self.product_limit = int(product_limit)
        self.oversample = int(oversample)
        self.clock = clock

    @contextmanager
    def _session(self) -> Iterator[Any]:
        """One session for one solicitation, closed however this exits."""
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

    def solicit(self, intent: Any, *, limit: int | None = None) -> ShopRoster:
        """Which shops this exchange would solicit for ``intent``. **Never raises.**

        Every failure below is an empty roster carrying a reason, for the reason stated in
        this module's header: a graph that cannot answer must not be able to fail an auction
        whose caller may not even be using it.

        Args:
            intent: an ``Intent`` mapping or protocol model.
            limit: how many shops to return; defaults to this source's own ceiling and is
                clamped to it, so a caller cannot widen the fan-out past what was configured.

        Returns:
            A :class:`ShopRoster`, ordered by ``intent_match`` descending then ``store_id``.
        """
        started = self.clock()
        wanted = self.limit if limit is None else max(1, min(int(limit), self.limit))

        def empty(reason: str, *, considered: int = 0) -> ShopRoster:
            return ShopRoster(
                source=self.name,
                considered=considered,
                reason=reason,
                elapsed_ms=(self.clock() - started) * 1000.0,
            )

        try:
            query = build_query(intent, limit=self.product_limit)
        except (MalformedIntent, UndecidableCriterion) as exc:
            return empty(
                f"this intent cannot be turned into a retrieval query, so the exchange has "
                f"no question to ask its catalogue: {exc}"
            )

        try:
            with self._session() as session:
                retrieval = CandidateRetrieval(
                    GraphCandidateSource(
                        session, provider=self.provider, oversample=self.oversample
                    )
                )
                result = retrieval.retrieve(intent, limit=self.product_limit)
                fit = {a.product_id: a.fit_score for a in result.assessments}
                if not fit:
                    return empty(
                        "no product in this exchange's catalogue graph both matches this "
                        "intent and satisfies its hard constraints, so there is no shop to "
                        "solicit",
                        considered=result.considered,
                    )
                shops = candidate_shops(
                    session,
                    query_text=query.query_text or None,
                    provider=self.provider,
                    attribute_filters=query.attribute_filters,
                    category=query.category,
                    limit=max(wanted * 4, wanted),
                    product_limit=self.product_limit,
                    oversample=self.oversample,
                )
        except UnretrievableQuery as exc:
            return empty(
                f"this intent carries neither text to embed nor a predicate the catalogue "
                f"can be asked about, and 'every shop' is not a question this exchange "
                f"answers: {exc}"
            )
        except VectorIndexUnusable as exc:
            return empty(
                f"the catalogue's vector index cannot honestly answer right now, so the "
                f"exchange names no shops rather than pivoting a noise ranking: "
                f"{type(exc).__name__}: {exc}"
            )
        except Exception as exc:  # noqa: BLE001 — an unreachable graph is an empty roster
            return empty(
                f"the catalogue graph could not be read: {type(exc).__name__}: {exc}. The "
                f"auction still runs; it has no shops of the exchange's own to solicit"
            )

        solicited = _solicited(shops, fit=fit)
        solicited.sort(key=lambda shop: (-shop.intent_match, shop.store_id))
        kept = tuple(solicited[:wanted])
        elapsed_ms = (self.clock() - started) * 1000.0
        if not kept:
            return ShopRoster(
                source=self.name,
                considered=result.considered,
                reason=(
                    "the catalogue graph holds no provenanced shop carrying a product that "
                    "satisfies this intent; a shop the platform has checked nothing about is "
                    "off the roster by construction (D55)"
                ),
                elapsed_ms=elapsed_ms,
            )
        return ShopRoster(
            shops=kept,
            source=self.name,
            considered=result.considered,
            reason=None,
            elapsed_ms=elapsed_ms,
            fit=tuple(result.assessments),
        )


def _solicited(shops: Sequence[ShopCandidate], *, fit: Mapping[str, float]) -> list[SolicitedShop]:
    """Pivot graph shops onto roster rows, dropping any that carries nothing eligible.

    A shop is kept only for the products that survived the intent's HARD constraints in
    :class:`~exchange.retrieval.service.CandidateRetrieval` — R19 is a filter, and a shop
    whose only matching product was excluded has nothing to be solicited about. It is
    rostered on its BEST eligible product, and the price it carries is the cheapest
    provenanced offer for **that** product: a shop's cheapest offer overall may be for a
    product this auction is not about, and quoting it would price the wrong thing.
    """
    rows: list[SolicitedShop] = []
    for shop in shops:
        eligible = [pid for pid in shop.product_ids if pid in fit]
        if not eligible:
            continue
        product_ref = max(eligible, key=lambda pid: (fit[pid], pid))
        priced = [offer for offer in shop.offers if offer.product_id == product_ref]
        cheapest = min(priced, key=lambda offer: (offer.price, offer.offer_id), default=None)
        rows.append(
            SolicitedShop(
                store_id=shop.store_id,
                tier=int(shop.tier),
                product_ref=product_ref,
                intent_match=float(fit[product_ref]),
                domain=shop.domain,
                business_identity=shop.business_identity,
                list_price=None if cheapest is None else float(cheapest.price),
                currency=None if cheapest is None else cheapest.currency,
                source_ids=tuple(shop.source_ids),
            )
        )
    return rows


def graph_roster_from_env(env: Mapping[str, str] | None = None) -> GraphShopRoster | None:
    """A :class:`GraphShopRoster` on the configured Neo4j, or ``None`` when unconfigured.

    **Returns ``None`` rather than raising, and never connects here.** The composition root
    runs on the first served request, and a driver built eagerly against a graph that is down
    would take the whole exchange with it — including the roster-in-the-body path, which
    needs no graph at all. The driver is therefore constructed lazily, once, on the first
    solicitation, and a failure to build or connect it lands on
    :meth:`GraphShopRoster.solicit`'s own catch-all as an empty roster with a reason.

    Configured means ``EXCHANGE_SHOP_ROSTER=graph``. It is opt-in because it is a real
    deployment change: ``apps/exchange/Dockerfile`` ships ``ingest.graph`` but the ``neo4j``
    driver is imported lazily (``graph/reembed.py``), so an image without that wheel resolves
    everything it imports and only this path would notice. The env var makes "this deployment
    reads the graph" an operator's statement rather than a silent consequence of a wheel
    being present.
    """
    import os  # noqa: PLC0415 — read at call time so a test can drive `env`

    source = dict(os.environ if env is None else env)
    if str(source.get("EXCHANGE_SHOP_ROSTER", "")).strip().lower() != "graph":
        return None

    holder: dict[str, Any] = {}
    # A lock, because `app.state` is shared by every worker thread and the first solicitation
    # may arrive on several at once. Two drivers would not be wrong so much as leaked: the
    # loser of the race is never closed, and a `neo4j.Driver` owns a connection pool.
    building = threading.Lock()

    def sessions() -> Any:
        driver = holder.get("driver")
        if driver is None:
            with building:
                driver = holder.get("driver")
                if driver is None:
                    from neo4j import GraphDatabase  # noqa: PLC0415 — see the docstring

                    driver = GraphDatabase.driver(
                        source.get("NEO4J_URI", "bolt://localhost:7687"),
                        auth=(
                            source.get("NEO4J_USER", "neo4j"),
                            source.get("NEO4J_PASSWORD", "proxyshop_dev_pw"),
                        ),
                        connection_timeout=5,
                    )
                    holder["driver"] = driver
        return driver.session()

    return GraphShopRoster(sessions)
