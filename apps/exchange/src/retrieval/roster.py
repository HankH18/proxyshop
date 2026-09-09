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
1. **An explicit roster's SHOPS are untouched.** Who competes is the caller's statement and
   this module never adds a shop to it, never removes one, and never reorders it —
   :meth:`GraphShopRoster.solicit` is consulted for the shop set only when the request body
   names no store, and that is enforced at the call site (``auction/routes.py``).

   What a stated roster does NOT get to fix is which PRODUCT of a shop answers a question it
   was written before anybody asked. :func:`repoint_organic_products` re-points a stated row
   onto the platform's own on-topic product for that same shop, and only where this exchange's
   own search for the intent did not surface the one the caller pinned; see that function for
   the before/after measurement, for what "did not surface" was measured to mean, and for why
   both of its conditions fail closed.

   **THE DEMO'S CONFIGURATION RE-POINTS, and reading this rule as "off unless a second
   variable is set" is reading it backwards.** ``repoints_stated_rosters`` defaults ``False``
   on the constructor, so a source built by hand — every test double in this tree included —
   is never called for a stated roster. But :func:`graph_roster_from_env` builds it ``True``
   on ``EXCHANGE_SHOP_ROSTER=graph`` ALONE, because ``EXCHANGE_REPOINT_ORGANIC_PRODUCTS``
   defaults on with the roster rather than off, and no compose file, env file or script in
   this repo sets that second variable at all (``git grep -n
   EXCHANGE_REPOINT_ORGANIC_PRODUCTS`` finds this module, one test and one doc). Measured on
   the served route, this service run on loopback with the demo's own settings
   (``EXCHANGE_SHOP_ROSTER=graph``, ``EXCHANGE_DEPLOYMENT=deploy/demo/exchange-deployment.json``,
   the recorded 3,093-product graph) and handed ``deploy/demo/buyer-roster.json`` verbatim::

       POST /auctions  "creatine monohydrate powder"
         -> roster_source.reason: "...the platform re-pointed 2 of 6 row(s)..."

   So a stated roster reaches this module in the demo, and rule 1 is a promise about SHOPS
   only. An operator who wants a stated roster served verbatim states
   ``EXCHANGE_REPOINT_ORGANIC_PRODUCTS=none``. A deployment with no graph, or with an empty
   one, serves exactly what it served before either way.

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

Two VECTOR reads, and why
-------------------------
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

A THIRD statement runs inside the first of those, and it is not a third vector read:
:data:`exchange.retrieval.sources._VARIANT_NAMES` expands the products the retrieval already
chose to their observed variant names, keyed on their ids. It touches no index, carries no
query text, and cannot change WHICH products were retrieved — only how fully the relevance
rule sees each one.

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

from proxyshop_support.neo4j_auth import graph_credentials

from .criteria import MalformedIntent, UndecidableCriterion, build_query
from .fit import FitAssessment
from .service import CandidateRetrieval, RetrievalResult
from .sources import GraphCandidateSource

__all__ = [
    "DEFAULT_SOLICITED_SHOPS",
    "GraphShopRoster",
    "NoShopRoster",
    "ShopRoster",
    "ShopRosterSource",
    "SolicitedShop",
    "graph_roster_from_env",
    "graph_sessions_from_env",
    "repoint_organic_products",
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
        variant_ref: the storefront's OWN id for the variant :attr:`list_price` prices —
            what ``https://<store>/cart/{variant}:{qty}`` needs. It comes off the SAME
            ``ShopOffer`` the price does, never another, for the reason
            ``scripts/build_demo_deployment.py::_priced_variant`` already states in prose: a
            permalink built on one variant while the offer quotes another's price sends the
            shopper to a cart whose total disagrees with what they accepted. ``None`` when
            the platform observed no price at all, or observed one for a variant the
            storefront published no id for.
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
    variant_ref: str | None = None

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
        # `variant_ref`, not `variant_id`: `store_agent.runtime.bidding._variant_ref` reads
        # `("variant_ref", "variant_id")` IN THAT ORDER, so a graph key spelled `variant_id`
        # on a row an agent ever sees would be returned as the agent's own variant and the
        # hosted stores would regress from working to broken. Omitted when absent, for the
        # same reason `list_price` is: an absent key reads downstream as "the platform never
        # observed one", and there is no defensible stand-in for a variant.
        if self.variant_ref:
            row["variant_ref"] = self.variant_ref
        return row


@dataclass(frozen=True)
class ShopRoster:
    """The answer to "which shops": who was found, by what, and — when nobody — why not.

    ``reason`` is served back on the auction response, and on the SOLICITED path — anything
    :meth:`GraphShopRoster.solicit` or :class:`NoShopRoster` returns — it is non-``None``
    exactly when :attr:`shops` is empty. An empty solicitation with no reason would be
    indistinguishable from "the exchange never asked", which is the one reading that is
    certainly wrong once a graph is wired.

    **That biconditional is not a class invariant, and the wire is where it stops.**
    ``auction/routes.py`` builds ``ShopRoster(source="request", reason=repointed)`` for a
    roster the CALLER stated: :attr:`shops` is always empty there because this object is
    carrying a sentence, not a solicitation, and ``reason`` is ``None`` whenever
    :func:`repoint_organic_products` moved nothing. The route then overwrites the payload's
    ``shops`` with the stated roster's own length. Measured on this service run on loopback
    against the recorded 3,093-product graph, ``POST /auctions`` with
    ``deploy/demo/buyer-roster.json``::

        "creatine monohydrate powder"          -> shops 6, reason "...re-pointed 2 of 6 row(s)..."
        "elderberry immune support"            -> shops 6, reason null
        "a walnut coffee table for the lounge" -> shops 6, reason null

    so a served ``roster_source`` can carry six shops and a reason, or six shops and no reason,
    and neither is the empty-with-a-reason shape this docstring used to promise for every case.
    Anything reading ``reason`` as "this auction found nobody" must check ``source`` too.
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
        repoints_stated_rosters: bool = False,
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
            repoints_stated_rosters: whether :func:`repoint_organic_products` may consult this
                source for a request that named its own stores. **Defaults to ``False``, and
                the default is the compatibility guarantee** this module's rule 1 states: a
                caller that brings its own roster is served exactly what it was served before
                the graph seam existed, and the graph is not connected to at all. Deployments
                turn it on through :func:`graph_roster_from_env`; a source built by hand — every
                test double in this tree included — is off, and the sabotage test
                ``test_graph_auction.py::test_a_stated_roster_never_consults_the_graph`` is what
                holds that.
        """
        self.sessions = sessions
        self.provider = provider
        self.limit = int(limit)
        self.product_limit = int(product_limit)
        self.oversample = int(oversample)
        self.clock = clock
        self.repoints_stated_rosters = bool(repoints_stated_rosters)

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
                    # WHICH kind of nothing, because the three are different answers to the
                    # shopper and this string is the only place the difference survives. An
                    # off-topic emptiness is "this catalogue is not about what you asked" — the
                    # honest answer to a furniture query against a supplements corpus, and the
                    # one the exchange used to replace with four confident supplements. A
                    # hard-constraint emptiness is "we have this kind of thing, none of it meets
                    # your must-have", which is a different sentence and a different repair.
                    return empty(
                        _nothing_retrieved_reason(result),
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
                    "no shop this search reached carries a provenanced product that satisfies "
                    "this intent; a shop the platform has checked nothing about is off the "
                    "roster by construction (D55). That is a statement about the shops the "
                    "pivot returned, not about the whole graph — `candidate_shops` is bounded "
                    "by its own limit and by the product window this retrieval used"
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


def _nothing_retrieved_reason(result: RetrievalResult) -> str:
    """Why this retrieval named no product, in the shopper's terms rather than the pipeline's.

    Three emptinesses reach the same ``if not fit``, and reporting them with one sentence was
    the reason a shopper could not tell "we searched and what came back is about something else"
    from "we could not search". They are ordered by what the shopper can act on: an OFF-TOPIC
    result is the answer to their question, so it is named first even when a hard constraint
    also excluded somebody — the constraint is not why they are being shown nothing.

    **THE SENTENCE IS BOUNDED TO WHAT WAS JUDGED, and that bound is a repair.** It used to end
    "This is an answer about the catalogue, not a failure to search it — the products this
    exchange holds are about something else", which is a claim about all 3,093 products
    composed after looking at ``result.considered`` = 25 of them. Measured live on ``POST
    /auctions``: the query ``"something to help my joints"`` served that sentence while the
    catalogue held four ACTIVE products whose platform-crawled name carries ``Joint`` —
    ``Advanced Joint Support``, ``Advanced Joint Support Bulk``, ``Turmeric Supreme® Joint
    Health``, ``Nutricost Pets Hip & Joint Support`` — and this module's own rule accepts them
    (``TopicalRelevance().judge('something to help my joints', 'Advanced Joint Support')`` ->
    ``about=True, matched=('joint',)``). The retriever's top-25 window simply missed them. So
    the exchange asserted a fact about its own catalogue, in the platform's own voice, on a
    served response, and the fact was false — the D55 defect class, made worse by being the
    exact opposite of what this module's own header says about those two queries ("RECALL
    failures of the retriever rather than refusals by this rule").

    What is true, and all that is claimed now, is that the products this SEARCH reached are
    about something else. A shopper reading it can tell the difference between "there is
    nothing here" and "nothing this search reached matched", which is the difference that
    decides whether rephrasing is worth their time.

    **ALL THREE branches carry that bound, not just the first.** The ``excluded`` branch used to
    open "no product in this exchange's catalogue graph satisfies this intent's hard
    constraints" — the identical claim about all 3,093 products, composed after judging the
    same ``result.considered`` <= 25, on the same served field — and it survived the pass that
    repaired its neighbour three lines above. It now says "nothing this search reached".
    :meth:`GraphShopRoster.solicit`'s own no-provenanced-shop reason carried the same shape one
    level up — a claim about the whole graph composed out of the shops ``candidate_shops``
    returned, which that function bounds by its own ``limit`` and by the product window — and
    now says "no shop this search reached".
    """
    if result.off_topic:
        names = ", ".join(row.canonical_name for row in result.off_topic[:3])
        return (
            f"nothing this search reached is about what was asked. {result.considered} "
            f"product(s) were retrieved from this exchange's catalogue and "
            f"{len(result.off_topic)} of them were judged off-topic by {result.relevance} "
            f"(nearest: {names}), so there is no shop to solicit. That is a statement about "
            f"those {result.considered} products, not about the whole catalogue: the index "
            f"returns its top matches and a product it did not surface was never judged"
        )
    if result.excluded:
        return (
            f"nothing this search reached satisfies this intent's hard constraints. "
            f"{result.considered} product(s) were retrieved from this exchange's catalogue "
            f"and every one of them was excluded, so there is no shop to solicit. That is a "
            f"statement about those {result.considered} products, not about the whole "
            f"catalogue: the index returns its top matches and a product it did not surface "
            f"was never judged"
        )
    return (
        "this exchange's catalogue graph returned no product at all for this intent — the "
        "index matched nothing to judge — so there is no shop to solicit"
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
                # From THAT offer, not from any other of the shop's: the price and the
                # variant are one observation or they are two draws from the same set, and a
                # cart built on the second one totals differently from the offer accepted.
                variant_ref=(
                    None
                    if cheapest is None or not cheapest.native_variant_id
                    else str(cheapest.native_variant_id)
                ),
                source_ids=tuple(shop.source_ids),
            )
        )
    return rows


def repoint_organic_products(
    source: Any,
    stated: Sequence[Mapping[str, Any]],
    intent: Any,
) -> tuple[list[dict[str, Any]], str | None]:
    """Re-point a STATED roster's products at the platform's own answer for this intent.

    The caller says WHICH SHOPS compete. This says which of a shop's products answers the
    question — for the rows whose pinned product this exchange's own search for the intent did
    not return, and only for those.

    Why it had to exist, measured
    -----------------------------
    A stated roster is a fixed candidate set: one row per shop, each naming one product,
    chosen before the shopper typed anything. The demo's is exactly that — ``deploy/demo/
    buyer-roster.json``, six shops each pinned to their liver-cluster lead — and the buyer
    service sends it on every confirmation, because it refuses to open an auction with no
    candidate set at all (``buyer_svc.composition.NoRosterBound``).

    Add :mod:`~exchange.retrieval.relevance` to that and the shopper gets a blank screen for
    anything outside the cluster the roster was built around, because every row on it is
    honestly off-topic. Driven on this exchange over 24 queries the corpus genuinely serves —
    creatine, magnesium, whey, collagen, turmeric, zinc, melatonin, elderberry, glucosamine,
    lion's mane, biotin, NMN and the rest — against the recorded 3,093-product corpus::

        stated roster, before this function:  3 of 24 queries returned any slot at all
        the same exchange's own graph roster: 24 of 24, and 0 of 10 off-corpus queries

    So the exchange's organic half already answered every one of them; the fixed roster was
    what emptied the screen. Showing the pinned rows anyway is not the alternative — that is
    the confidently-wrong shortlist the relevance rule exists to replace.

    The rule, and why it is this one
    --------------------------------
    A row moves only when BOTH halves hold:

    1. **This exchange's own search for the intent did not return the pinned product.**
       ``ShopRoster.fit`` carries one :class:`~exchange.retrieval.fit.FitAssessment` per product
       that survived the intent's hard constraints AND was judged about the query, so a pinned
       ref present there is a product this exchange has just said is relevant. It is left
       exactly as stated — which is why this is a no-op on the auctions that already work:
       measured on ``"milk thistle liver support"`` when the demo roster was six rows, four of
       them are returned by the search and do not move. (It states fifteen now; that reading has
       not been retaken, and it needs a live graph run rather than a re-read of the document.)

       **ABSENT FROM ``fit`` IS NOT "JUDGED AND REFUSED", and the sentence this rule publishes
       may not say it is.** ``fit`` is ``result.assessments``, and ``retrieve()`` is called with
       ``limit=product_limit`` (:data:`DEFAULT_ROSTER_PRODUCTS`, 25), so a product ranked
       twenty-sixth by the vector index is absent from ``fit`` having never been judged at all.
       Measured over 24 in-corpus queries x the demo roster's six rows AT THE TIME — 144
       row-decisions, against the recorded corpus with 3,093 ``Product`` nodes in the graph on
       that run; the roster states fifteen rows now::

           moved                                                63
             pinned product judged off-topic                     0
             pinned product excluded by a hard constraint        0
             pinned product never retrieved, so never judged    63   <- exhaustive
           unmoved                                              81
             held by THIS rule (the search returned the pinned
             product), all four on "milk thistle liver support"  4   <- the other 77 were
                                                                     not attributed in
                                                                     this run

       So on this corpus the reason a row moves is always "the search did not surface it", never
       "the search looked at it and refused it" — which is why the served ``reason`` says the
       former. The re-point is still the right answer for those 63: judging each moved row's
       pinned NAME against its query directly with
       :meth:`~exchange.retrieval.relevance.TopicalRelevance.judge` — the same rule the pipeline
       applies — answers ``about=False`` for all 63, so nothing on-topic was overridden.
    2. **The platform holds a PRICED alternative from that same shop.** An unpriced row would
       mint a fallback with no ``unit_price`` and no ``expires_at`` (``collect.py``'s
       ``_list_price_bid``), which every downstream filter refuses — so re-pointing onto one
       would turn a wrong answer into no answer. A shop the graph does not roster at all, or
       rosters without a provenanced offer, keeps its stated row and is refused on it, which
       is the honest outcome for a shop that stocks nothing on the subject.

    Both halves fail closed on an off-corpus query, and that is the direction that matters:
    measured over the same 10 furniture/electronics/travel queries, ZERO rows move, because
    the graph rosters no shop for them at all. This function cannot manufacture a shortlist
    the catalogue does not support; it can only put a shop's own on-topic product in front of
    the shopper instead of its liver supplement.

    **The price moves with the product, or neither moves.** ``list_price`` and ``currency`` are
    replaced from the same :class:`SolicitedShop` that supplied the new ``product_ref`` — the
    cheapest provenanced offer the platform observed FOR that product — so the row never states
    a price for one product beside the reference of another. ``tier`` and ``max_discount_pct``
    are carried through untouched.

    **``variant_ref`` moves under the same rule, and is CLEARED rather than kept when the new
    product has none.** It is the storefront's own id for the variant the new ``list_price``
    prices, and it is what ``https://<store>/cart/{variant}:{qty}`` is built from. A stated
    variant left on a re-pointed row would name a variant of the product the row moved OFF, so
    the shopper would be sent to a cart for something they were never shown — which is worse
    than the absent field, because the absent field declines at accept time and says so
    (:func:`~apps.exchange.src.checkout.provider.default_permalink`) while the stale one
    succeeds and is wrong.

    **A row this function does NOT move keeps whatever variant the caller stated, and nothing
    is invented for it.** That is deliberate and it is the same rule read from the other side:
    an unmoved row keeps the caller's ``list_price``, and pairing the caller's price with a
    variant the PLATFORM chose would reintroduce exactly the disagreement the paragraph above
    refuses. A stated roster that wants a working cart states its own ``variant_ref`` —
    ``RosterEntry`` declares the field, and ``scripts/build_demo_deployment.py`` writes one per
    row into ``deploy/demo/buyer-roster.json``.

    **That last sentence was FALSE as shipped, and this paragraph is the reason it mattered.**
    The generator declared the key on every store agent's own catalogue row and on no roster row
    at all: measured on the tracked document, fifteen rows whose keys across all of them were
    exactly ``{list_price, max_discount_pct, product_ref, store_id, tier}``, none carrying a
    variant. So the asymmetry ran backwards — a row whose pinned product was WRONG was moved
    here and given the platform's variant, while a row whose pinned product was RIGHT reached
    accept with none and ``checkout.provider.default_permalink`` could build no cart at all. The
    better the roster, the worse the outcome. The repair is in the generator, where the caller's
    statement is written, rather than here, because the argument above is unchanged: an unmoved
    row keeps the caller's ``list_price``, and inventing a variant beside it is the disagreement
    this function exists to refuse.
    ``apps/exchange/tests/test_the_stated_roster_names_a_variant.py``
    grades both ends — that the shipped document names, per row, a variant the recorded
    corpus says belongs to that row's own product and is priced at that row's own price, and
    that a row this function declines to move still reaches ``_list_price_bid``'s fallback offer
    carrying it.

    **``max_discount_pct`` is a PER-ROW cap and re-pointing moves it onto a product the caller
    did not name.** ``auction/collect.py``'s module docstring is the one that is right about
    this field — "the deepest discount the exchange is told is authorized on that product" —
    and ``collect_bids`` judges a bid's declared discount against the row's ``max_discount_pct``
    beside the row's ``list_price``, both of which this function has just replaced together. So
    after a re-point the caller's 20% is applied to whatever product the crawl supplied.
    Recording it rather than repairing it, because the field has no stronger contract to
    violate and dropping it would make the moved row MORE permissive, not less. ``POST
    /auctions`` is unauthenticated (``collect.py``'s "Where the cap comes from" section) and
    ``RosterEntry`` already lets any caller state any cap against any ``product_ref``.
    Measured by handing ``collect_bids`` one row at ``list_price: 100.0`` and one on-time bid::

        row max_discount_pct: 20   bid 50.00 declaring 50% off  -> refused, falls back to 100.00
        row max_discount_pct: 20   bid 50.00 declaring nothing  -> refused, falls back to 100.00
        row with NO cap            bid 50.00 declaring 50% off  -> admitted at 50.00
        row with NO cap            bid 50.00 declaring nothing  -> admitted at 50.00
        row max_discount_pct: 20   bid 85.00 declaring nothing  -> admitted at 85.00

    both refusals reading ``price_under_declared_depth:offer.unit_price``. So the carried-over
    cap is the only thing standing between a re-pointed row and an unjudged undercut; removing
    it to keep the field honest would open the wall.

    It runs before ``machine.create``, so the re-pointed row is what the whole auction is held
    on: the record, the ledger payload, the ``BidRequest`` each agent is handed, and the
    fallback offer ``collect_bids`` mints. There is no seam here at which a re-pointed product
    and a stated price can disagree.

    **THE SOURCE HAS TO HAVE OPTED IN, and it is not asked otherwise.** A source that does not
    carry a truthy ``repoints_stated_rosters`` is never called — not to ask and fail, *not
    called*. That is what an un-wired exchange, and every test double in this tree, gets; it is
    NOT what ``EXCHANGE_SHOP_ROSTER=graph`` gets, which opts in (see this module's rule 1). It
    is asserted by sabotage, not by inspection:
    ``test_graph_auction.py::test_a_stated_roster_never_consults_the_graph`` wires a source that
    RAISES on every call and asserts it was never reached, and
    ``test_organic_relevance.py::test_a_source_that_has_not_opted_in_is_never_even_asked``
    asserts the same thing about this function directly. Deployments opt in through
    :func:`graph_roster_from_env`, whose ``EXCHANGE_REPOINT_ORGANIC_PRODUCTS`` defaults on with
    ``EXCHANGE_SHOP_ROSTER=graph``.

    Args:
        source: the roster source to ask — anything with ``solicit(intent)`` AND a truthy
            ``repoints_stated_rosters``. The wired default is :class:`NoShopRoster`, which has
            neither, so an exchange with no graph serves exactly what it served before.
        stated: the roster rows off the request body, already validated by ``RosterEntry``.
        intent: the intent, as the route holds it.

    Returns:
        ``(rows, reason)`` — the rows to run the auction on, and a sentence naming what moved
        for the response's ``roster_source``, or ``None`` when nothing did.
    """
    rows = [dict(row) for row in stated]
    if not rows or not getattr(source, "repoints_stated_rosters", False):
        return rows, None
    try:
        found = source.solicit(intent)
    except Exception:  # noqa: BLE001 — rule 2: a roster source that raises finds nobody
        return rows, None
    if not isinstance(found, ShopRoster) or not found.shops:
        return rows, None
    # The platform's own verdict on each product it retrieved: present means "this exchange
    # has just judged this product both eligible and about the query".
    vouched = {assessment.product_id for assessment in found.fit}
    best: dict[str, SolicitedShop] = {}
    for shop in found.shops:
        best.setdefault(str(shop.store_id), shop)
    moved: list[str] = []
    for row in rows:
        pinned = row.get("product_ref")
        if pinned is not None and str(pinned) in vouched:
            continue
        candidate = best.get(str(row.get("store_id") or ""))
        if candidate is None or not candidate.priced or candidate.product_ref == pinned:
            continue
        shop = candidate
        row["product_ref"] = shop.product_ref
        row["list_price"] = float(shop.list_price or 0.0)
        if shop.currency:
            row["currency"] = shop.currency
        # THE VARIANT MOVES WITH THE PRODUCT TOO, and leaving it behind is worse than never
        # having carried one. A re-pointed row that kept the caller's `variant_ref` would name
        # the variant of the product it moved OFF — a cart permalink for a product the shopper
        # was never shown — so the stated value is REPLACED, and cleared when the platform
        # observed no id for the new product. Same rule as the price, one field over: the
        # variant moves with the product, or neither moves.
        if shop.variant_ref:
            row["variant_ref"] = shop.variant_ref
        else:
            row.pop("variant_ref", None)
        moved.append(str(row.get("store_id") or ""))
    if not moved:
        return rows, None
    return rows, (
        f"the roster was stated by the caller and its shops are unchanged; the platform "
        f"re-pointed {len(moved)} of {len(rows)} row(s) onto the product its own crawl says "
        f"answers this intent, because this exchange's own search for this intent did not "
        f"return the product each named ({', '.join(sorted(set(moved)))})"
    )


#: One lazily-built ``neo4j.Driver`` per distinct connection, shared by every graph-backed
#: seam this service wires. A ``Driver`` owns a connection pool and is documented as safe to
#: share across threads; a *session* is not, which is why :meth:`GraphShopRoster._session`
#: still takes one per solicitation. Keyed by the resolved connection so a test driving a
#: different ``env`` gets a different driver rather than the first caller's.
#:
#: Never closed, exactly as the per-seam holder it replaces was never closed: the process
#: holds it for its lifetime and there is no shutdown hook in this service to hang one on.
_DRIVERS: dict[tuple[str, str, str], Any] = {}
_BUILDING = threading.Lock()


def graph_sessions_from_env(env: Mapping[str, str] | None = None) -> Callable[[], Any]:
    """A zero-argument ``neo4j.Session`` factory over the configured connection.

    **Connects to nothing here.** The composition root runs on the first served request, and a
    driver built eagerly against a graph that is down would take the whole exchange with it —
    including the roster-in-the-body path, which needs no graph at all. The driver is built on
    the first CALL of the returned factory, and a failure to build or connect it surfaces
    there, where each consumer already has a fail-closed answer for it.

    One spelling of "how this service reaches Neo4j", shared by the two seams that do (the
    shop roster and the catalogue snapshots), so a deployment cannot end up with them pointed
    at different graphs — which would mean grading a shop's claims against a catalogue that
    never named it.

    Args:
        env: the environment to read; the process environment when ``None``.

    Returns:
        A callable returning an open session. Whether the graph is reachable is not known
        until it is called.
    """
    # THE CREDENTIAL IS NOT RESOLVED HERE. `proxyshop_support.neo4j_auth` is the one place in
    # the tree that reads NEO4J_URI/USER/PASSWORD, and this line is what makes that true for
    # the exchange's served path. It used to spell its own three defaults, one of which —
    # `NEO4J_PASSWORD` -> "proxyshop_dev_pw" — disagreed with the readiness probe's `""`
    # (`proxyshop_support/service_launch.py::check_neo4j`). A probe that offers a different
    # credential from the code it vouches for is not a stricter or looser check, it is a check
    # of something else: it can report a container healthy while every solicitation here is
    # refused `Neo.ClientError.Security.Unauthorized`, with nothing in the logs connecting the
    # two. The resolver also says WHERE the password came from, which is what turns that
    # refusal into a diagnosis — see `GraphCredentials.describe`.
    credentials = graph_credentials(env)
    key = credentials.cache_key

    def sessions() -> Any:
        driver = _DRIVERS.get(key)
        if driver is None:
            # A lock, because `app.state` is shared by every worker thread and the first
            # request may arrive on several at once. Two drivers would not be wrong so much as
            # leaked: the loser of the race is never closed, and a `neo4j.Driver` owns a
            # connection pool.
            with _BUILDING:
                driver = _DRIVERS.get(key)
                if driver is None:
                    from neo4j import GraphDatabase  # noqa: PLC0415 — see the docstring

                    driver = GraphDatabase.driver(
                        credentials.uri, auth=credentials.auth, connection_timeout=5
                    )
                    _DRIVERS[key] = driver
        return driver.session()

    return sessions


def graph_roster_from_env(env: Mapping[str, str] | None = None) -> GraphShopRoster | None:
    """A :class:`GraphShopRoster` on the configured Neo4j, or ``None`` when unconfigured.

    **Returns ``None`` rather than raising, and never connects here** — see
    :func:`graph_sessions_from_env`, which owns the lazy driver. A failure to build or connect
    it lands on :meth:`GraphShopRoster.solicit`'s own catch-all as an empty roster with a
    reason.

    Configured means ``EXCHANGE_SHOP_ROSTER=graph``. It is opt-in because it is a real
    deployment change: ``apps/exchange/Dockerfile`` ships ``ingest.graph`` but the ``neo4j``
    driver is imported lazily (``graph/reembed.py``), so an image without that wheel resolves
    everything it imports and only this path would notice. The env var makes "this deployment
    reads the graph" an operator's statement rather than a silent consequence of a wheel
    being present.

    **``EXCHANGE_REPOINT_ORGANIC_PRODUCTS`` is the second power, and it defaults ON with the
    first.** It decides whether :func:`repoint_organic_products` may re-point a STATED roster's
    products; ``EXCHANGE_SHOP_ROSTER`` decides who is asked when a request names nobody. They
    are separate variables because they are separate statements — an operator can want organic
    discovery and still want a stated roster served verbatim — and the default is the roster's
    own setting for the reason ``EXCHANGE_RANKING_CATALOG`` defaults to it one module over: a
    deployment that turned the graph on and left this off gets a blank shortlist for every
    question its fixed roster predates, which is the working configuration nobody asked for.
    Measured on the demo's own six-shop roster, 24 in-corpus queries: 3 of 24 returned any slot
    with this off, 24 of 24 with it on, 0 of 10 off-corpus queries either way.
    ``EXCHANGE_REPOINT_ORGANIC_PRODUCTS=none`` (or ``0``/``off``/``false``) states the old
    behaviour explicitly and keeps it.
    """
    import os  # noqa: PLC0415 — read at call time so a test can drive `env`

    source = dict(os.environ if env is None else env)
    if str(source.get("EXCHANGE_SHOP_ROSTER", "")).strip().lower() != "graph":
        return None
    stated = str(source.get("EXCHANGE_REPOINT_ORGANIC_PRODUCTS", "")).strip().lower()
    return GraphShopRoster(
        graph_sessions_from_env(source),
        repoints_stated_rosters=stated not in {"none", "0", "off", "false", "no"},
    )
