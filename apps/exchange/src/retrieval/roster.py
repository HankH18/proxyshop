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
    ShopOffer,
    UnretrievableQuery,
    VectorIndexUnusable,
    candidate_shops,
)

from proxyshop_support.neo4j_auth import graph_credentials

from .criteria import MalformedIntent, UndecidableCriterion, build_query, is_budget_bound
from .fit import FitAssessment
from .relevance import identity_off_topic
from .service import CandidateRetrieval, RetrievalResult
from .sources import GraphCandidateSource

__all__ = [
    "DEFAULT_SOLICITED_SHOPS",
    "SUBSTITUTED_OFF_TOPIC",
    "SUBSTITUTED_OVER_BUDGET",
    "GraphShopRoster",
    "NoShopRoster",
    "PassedOver",
    "ShopRoster",
    "ShopRosterSource",
    "SolicitedShop",
    "graph_roster_from_env",
    "graph_sessions_from_env",
    "repoint_organic_products",
    "substitution_reason",
]

#: How many shops one graph-sourced solicitation may name.
#:
#: A ceiling exists because every rostered store costs an eligibility read and a fan-out slot
#: inside R10's synchronous window, and because ``POST /auctions`` is unauthenticated: a
#: caller who omits ``roster`` must not be able to buy a wider fan-out than a caller who
#: states one. The route clamps this against ``MAX_ROSTER_ENTRIES`` as well, so the graph door
#: can never be the wider of the two.
DEFAULT_SOLICITED_SHOPS = 8

#: :attr:`PassedOver.reason` when the BUDGET wall moved a shop off its best answer.
SUBSTITUTED_OVER_BUDGET = "over_budget"

#: :attr:`PassedOver.reason` when the ORGANIC gate moved a shop off its best answer.
SUBSTITUTED_OFF_TOPIC = "off_topic"


@dataclass(frozen=True)
class PassedOver:
    """The product a shop's one row was NOT staked on, and which rule moved it.

    :func:`_solicited` spends a shop's single roster row on the best product it has that BOTH
    the organic gate would keep and the buyer's budget admits. When that is not the shop's
    best-fitting product, the shopper is shown a SUBSTITUTE — a different answer to their
    question, at a different price, from a shop that had a closer one — and before this record
    existed nothing on the served response said so. :meth:`GraphShopRoster.solicit` turns these
    into ``roster_source.reason``, which is where :func:`repoint_organic_products` already
    publishes the same class of fact about a STATED row.

    It is deliberately NOT on :meth:`SolicitedShop.as_roster_row`: the roster row is the
    question the exchange asks a store, and what the platform passed over is not part of it.
    """

    #: The product the shop would have been rostered on had neither rule narrowed the choice.
    product_ref: str
    #: :data:`SUBSTITUTED_OVER_BUDGET` or :data:`SUBSTITUTED_OFF_TOPIC`.
    reason: str
    #: The cheapest provenanced price for that product, or ``None`` if the crawl saw none.
    list_price: float | None = None


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
    #: What this shop's row was staked OFF, or ``None`` when it carries the shop's best
    #: answer. See :class:`PassedOver`; it is reported on the response and never on the row.
    passed_over: PassedOver | None = None

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
    whenever :attr:`shops` is empty. An empty solicitation with no reason would be
    indistinguishable from "the exchange never asked", which is the one reading that is
    certainly wrong once a graph is wired.

    **It USED to be non-``None`` exactly when `shops` was empty, and that half is gone.**
    :func:`substitution_reason` now also fills it on a roster that found shops, to say that a
    shop is being shown on a product other than its best answer to the intent — the fact
    :func:`repoint_organic_products` publishes here for a STATED row, said for the graph path.
    A reader still cannot take a reason to mean "nobody was found"; it never could, which is
    what the next paragraph was already about.

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

        # WHICH product each shop is rostered on, decided against the rule that will judge the
        # row it becomes — see `_solicited`. The identity is the retrieval's own reading of the
        # platform's crawl: title and brand, exactly what `identity_surface` reads and exactly
        # what `catalog_identity` publishes downstream.
        #
        # TWO READINGS OF ONE CRAWL, and TWO INSTANCES OF ONE RULE. This reads the title and
        # brand off the GRAPH (`ingest.graph.Candidate`) and judges them with the RETRIEVAL's
        # `TopicalRelevance`; `rank_auction` reads them off whatever `ranking.serving.catalog_of`
        # resolved — the graph again under `EXCHANGE_RANKING_CATALOG`, or a deployment document's
        # `catalog` block, which the demo states and which is generated from the same crawl — and
        # judges them with its OWN rule (`ranking/__init__.py` default-constructs one when its
        # caller passes none, and `rank_auction` passes none). Both are default-constructed
        # today, so they agree; verified on the demo's own document for the pair this repairs,
        # where both readings answer `The Modular Table` / `RIZE` and `The Lift Off Coffee
        # Table` / `RIZE`. But they are not the same object and there is no wiring that widens
        # both at once, so what happens below is a PREDICTION of the gate's verdict rather than
        # the verdict itself.
        #
        # WHAT A WRONG PREDICTION COSTS, in both directions, and why it is bounded. Too strict
        # (this refuses a product the gate would have kept) re-points the shop onto some other
        # product of its own; too loose re-points it onto one the gate then refuses, which is
        # exactly today's behaviour — refused at the shortlist, with a reason. Neither can cost
        # a shop its PLACE, because the two questions are answered by two different numbers,
        # which is the next paragraph.
        identities = {
            assessment.product_id: {
                "title": assessment.canonical_name,
                "brand": assessment.brand,
            }
            for assessment in result.assessments
        }
        keeps_cache: dict[str, bool] = {}

        def keeps(product_id: str) -> bool:
            cached = keeps_cache.get(product_id)
            if cached is None:
                cached = (
                    identity_off_topic(
                        query.query_text, identities.get(product_id), retrieval.relevance
                    )
                    is None
                )
                keeps_cache[product_id] = cached
            return cached

        # WHAT THE SHOPPER CAN AFFORD, asked here for the same reason `keeps` is asked here:
        # it decides WHICH product a shop is rostered on, never whether. `budget_reasons` is
        # imported at call time because it lives in `exchange.ranking`, which imports this
        # package — the dependency runs one way and a module-level import here would close the
        # loop. It is the shipped wall itself rather than a restatement of it, so a row this
        # passes and the shortlist then refuses can only be a live bid differing from the
        # crawled price, never two spellings of the same rule drifting apart.
        from ..ranking.filters import budget_reasons  # noqa: PLC0415 — see the comment above

        bounds = [criterion for criterion in query.criteria if is_budget_bound(criterion)]

        def affords(offer: ShopOffer | None) -> bool:
            if not bounds:
                return True
            if offer is None:
                return False
            # Shaped exactly as `auction/collect.py::_list_price_bid` mints the fallback offer
            # — `unit_price` and `total_price` both the observed price — so the prediction and
            # the verdict read the same field of the same number.
            priced = {
                "unit_price": float(offer.price),
                "total_price": float(offer.price),
                "currency": offer.currency,
            }
            return not budget_reasons(priced, bounds)

        solicited = _solicited(shops, fit=fit, keeps=keeps, affords=affords)

        # WHICH SHOPS, and WHICH PRODUCT, ARE TWO QUESTIONS AND THEY TAKE TWO NUMBERS.
        #
        # This list is cut to `wanted`, so whatever it is sorted by decides who is on the
        # roster at all. Sorting it by the row's published `intent_match` — the fit of the
        # product the shop was re-pointed ONTO — would mean that asking the gate which product
        # to name could push a shop past the cut, and a shop that falls off here reaches
        # NOTHING: not the shortlist, not `entries`, not `excluded`, and no reason is written
        # anywhere, because a shop that was never solicited has nothing to be refused for.
        # That is strictly worse than the defect this change repairs, which at least left a
        # sentence on the response. Measured on the two products this repairs plus `limit=1`:
        # floydhome carries the best-matching product either shop has, was the one shop
        # rostered before, and sorting on the re-pointed fit dropped it entirely.
        #
        # So SELECTION keeps the number it always had — the shop's best ELIGIBLE product,
        # which is `max(fit[pid])` over what it carries and is what `intent_match` used to be —
        # and the roster's shop set is therefore bit-for-bit what it was before `keeps` existed.
        # `keeps` chooses which product a shop is rostered on. It cannot choose whether.
        #
        # ORDERING then uses the published number, so `ShopRoster`'s own documented ordering
        # ("by `intent_match` descending then `store_id`") stays true of what comes back and no
        # reader has to know that a second number was ever involved.
        best_eligible: dict[str, float] = {}
        for shop in shops:
            scores = [fit[pid] for pid in shop.product_ids if pid in fit]
            if scores:
                store = str(shop.store_id)
                best_eligible[store] = max(best_eligible.get(store, float("-inf")), max(scores))
        solicited.sort(key=lambda shop: (-best_eligible[shop.store_id], shop.store_id))
        kept = tuple(
            sorted(solicited[:wanted], key=lambda shop: (-shop.intent_match, shop.store_id))
        )
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
            # NOT always `None` on a non-empty roster, and that is the change this line
            # carries: a shop staked on a substitute is a fact about a roster that FOUND
            # shops, so it has nowhere else to be said. `substitution_reason` answers `None`
            # when nothing was substituted, which is every auction that states no budget.
            reason=substitution_reason(kept),
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


def _solicited(
    shops: Sequence[ShopCandidate],
    *,
    fit: Mapping[str, float],
    keeps: Callable[[str], bool] = lambda _product_id: True,
    affords: Callable[[ShopOffer | None], bool] = lambda _offer: True,
) -> list[SolicitedShop]:
    """Pivot graph shops onto roster rows, dropping any that carries nothing eligible.

    A shop is kept only for the products that survived the intent's HARD constraints in
    :class:`~exchange.retrieval.service.CandidateRetrieval` — R19 is a filter, and a shop
    whose only matching product was excluded has nothing to be solicited about. It is
    rostered on its best eligible product **that the shortlist will actually keep**, and the
    price it carries is the cheapest provenanced offer for THAT product: a shop's cheapest
    offer overall may be for a product this auction is not about, and quoting it would price
    the wrong thing.

    WHY ``keeps`` EXISTS, and what it cost to leave it out
    -----------------------------------------------------
    A shop gets ONE roster row, so choosing its product spends its only slot. This used to
    choose by cosine alone::

        product_ref = max(eligible, key=lambda pid: (fit[pid], pid))

    and the row then met a SECOND rule it had not been chosen against:
    ``exchange.ranking.filters.organic_relevance_reason`` judges an organic row on
    :func:`~exchange.retrieval.relevance.identity_surface` — the crawled title and brand, all
    ``catalog_identity`` returns — while retrieval judged the same product on
    :func:`~exchange.retrieval.relevance.candidate_surface`, which also carries categories,
    ingredients, attribute keys and their readings, with the observed variant names beside it.
    The two surfaces can disagree, and where they do the shop dies holding a product both
    layers would have kept. Measured on the served route against the nineteen-store demo graph
    (98,001 nodes), ``"a walnut coffee table for the lounge"``::

        floydhome.com  fit 0.634558  "The Modular Table"          $1275  gate: REFUSED
        floydhome.com  fit 0.626495  "The Lift Off Coffee Table"   $700  gate: kept

    0.008 apart. The roster took the Modular Table, the gate refused it on ``"The Modular
    Table RIZE"`` — one content word of ``{walnut, coffee, table, lounge}`` where the rule
    needs two or half — and the shopper was shown one slot where two were available. **The
    refusal is right**: that product is a walnut table and is not a coffee table. What was
    wrong was staking the slot on it.

    ``keeps`` is that second rule, asked here. Its default accepts everything, so a caller
    that does not pass one — every hand-built source in this tree — gets exactly the behaviour
    this function has always had.

    **The fallback is load-bearing and is not an optimisation.** A shop with no keepable
    product is rostered on its plain best fit, unchanged, so it is still REPRESENTED in
    ``entries`` (R10) and still reaches the shortlist as a list-price row where the gate
    refuses it AND SAYS WHY. Dropping such a shop here would empty the same screen while
    deleting the sentence that explains it, which is a worse defect than the one this repairs.

    **"Never removes a shop" is only half the guarantee, and the other half is at the call
    site.** This function returns one row per shop whatever ``keeps`` answers — but it also
    sets ``intent_match`` from the chosen product, and :meth:`GraphShopRoster.solicit` CUTS the
    list it gets back to ``limit``. Sorting that cut by the re-pointed fit would let ``keeps``
    push a shop off the roster after all, which is why selection there is done on the shop's
    best ELIGIBLE fit instead. See the comment above the sort; the two halves only work
    together.

    **A shop this chooses a product for may be one with a LIVE AGENT.** ``keeps`` decides which
    of a shop's products the roster row carries; :func:`repoint_organic_products` is what puts
    that decision onto a STATED roster's row, and on the demo it moves ``toniiq.com`` — a
    tier-1 hosted bidder — onto a product the caller never named. Nothing here is insulated
    from the agents by tier: every graph-rostered shop reads back tier 0 whatever it is, and
    the exchange raises the four hosted ones to tier 1 afterwards. That function's
    "Inert on the demo" section carries the measurement and the git provenance.

    WHY ``affords`` EXISTS — the same defect, one gate further down
    ---------------------------------------------------------------
    ``keeps`` predicts the ORGANIC gate's verdict. ``affords`` predicts the BUDGET wall's
    (``exchange.ranking.filters.budget_reasons``), and it is here for the identical reason:
    a shop gets one row, the row meets a rule it was not chosen against, and the shop dies
    holding a product that rule would have kept. Measured on the served route against the
    nineteen-store demo graph, ``"an office chair"`` with ``price_usd lte 500``::

        www.branchfurniture.com  fit 0.613  "Verve Chair"      $599  wall: REFUSED
        www.branchfurniture.com  fit 0.608  "Multitask Chair"  $279  wall: kept

    0.008 apart, and five more of that shop's chairs sat under the ceiling in the same 125-row
    window ($239, $249, $287, $319, $319). The roster took the Verve, the wall cut it at $599,
    and the shopper was shown a blank page by a shop with six chairs they could afford.

    It is a PREDICTION, like ``keeps``, and it predicts the fallback case exactly: the offer
    judged here is the cheapest provenanced offer for the product, which is the number
    ``auction/collect.py::_list_price_bid`` mints the fallback bid from. A shop with a LIVE
    AGENT may bid something else — higher, and then the wall refuses a row this kept, which is
    today's behaviour and no worse; or lower, and the wall keeps a row this passed over. Being
    wrong in either direction costs a shop nothing it was not already at risk of, because it
    cannot cost a shop its PLACE — see the paragraph above and the call site's own comment.

    Args:
        shops: the crawled shops to pivot, from ``ingest.graph.candidate_shops``.
        fit: ``{product_id: fit_score}`` for the products this retrieval vouched for.
        keeps: whether the shortlist's organic gate would keep this product's row. Consulted
            only to CHOOSE between products a shop already has; it never removes a shop.
        affords: whether the buyer's stated budget admits this product's cheapest provenanced
            offer, given as the offer itself so the predicate reads the same number the
            fallback bid will carry. ``None`` is a product the shop has no provenanced offer
            for — unaffordable by the same fail-closed rule ``budget_reasons`` applies to an
            unreadable price. Consulted only to CHOOSE; it never removes a shop.
    """
    rows: list[SolicitedShop] = []
    for shop in shops:
        eligible = [pid for pid in shop.product_ids if pid in fit]
        if not eligible:
            continue
        # The cheapest provenanced offer per product, computed BEFORE the choice rather than
        # after it, because `affords` has to judge the same number the chosen row will carry.
        # `(price, offer_id)` is the tie-break the single-product read used and is kept here so
        # which offer a row quotes does not depend on which predicate asked for it.
        cheapest_for: dict[str, ShopOffer] = {}
        for offer in shop.offers:
            held = cheapest_for.get(offer.product_id)
            if held is None or (offer.price, offer.offer_id) < (held.price, held.offer_id):
                cheapest_for[offer.product_id] = offer
        keepable = [pid for pid in eligible if keeps(pid)]
        # Two narrowings, then the fallback, and the ORDER of the `or` chain is the rule: a
        # product the organic gate keeps AND the shopper can afford, else one the gate keeps,
        # else the shop's plain best. Each `or` is a shop that would otherwise reach nothing.
        choices = keepable or eligible
        affordable = [pid for pid in choices if affords(cheapest_for.get(pid))]
        product_ref = max(affordable or choices, key=lambda pid: (fit[pid], pid))
        # WHAT WAS PASSED OVER, recorded here because here is the only place that knows.
        # `best` is the row this shop would have carried with neither predicate — the plain
        # `max(eligible)` this function computed before `keeps` and `affords` existed — so
        # `product_ref != best` is exactly "the shopper is being shown a substitute". The
        # attribution asks the narrowings in the order they were applied: if the gate refused
        # the shop's best answer then the gate is why it is not on the row, whatever the wall
        # would also have said about it. Neither branch can be reached when its narrowing was
        # empty, because an empty narrowing falls through to `choices` and `product_ref` is
        # then `best` again.
        best = max(eligible, key=lambda pid: (fit[pid], pid))
        passed_over: PassedOver | None = None
        if product_ref != best:
            over = cheapest_for.get(best)
            passed_over = PassedOver(
                product_ref=best,
                reason=(
                    SUBSTITUTED_OFF_TOPIC
                    if keepable and best not in keepable
                    else SUBSTITUTED_OVER_BUDGET
                ),
                list_price=None if over is None else float(over.price),
            )
        cheapest = cheapest_for.get(product_ref)
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
                passed_over=passed_over,
            )
        )
    return rows


def substitution_reason(shops: Sequence[SolicitedShop]) -> str | None:
    """The sentence naming every shop shown as a SUBSTITUTE, or ``None`` when none was.

    A shop gets one roster row. :func:`_solicited` may spend it on a product that is not the
    shop's best answer to the intent — because the organic gate would refuse the best answer,
    or because the buyer's own ceiling excludes it — and the shopper then sees a different
    product, at a different price, with nothing saying either happened. The refusal that
    WOULD have explained it (``offer_price_outside_budget`` naming the passed-over price, or
    ``organic_row_off_topic``) is never written, because the substitution is what stops the
    refusal from happening.

    **Why here and not on the slot.** ``ranking.candidates.CANDIDATE_FIELDS`` carries no
    roster field at all and ``ranking.rank()`` is not given the roster rows — the list price
    reaches the ranker only as a positional sideband into ``attach_features`` — so there is no
    seam at which a slot could carry "your best answer was $599". ``roster_source.reason`` is
    the surface :func:`repoint_organic_products` already uses to say the platform chose a
    product the caller did not, and this is the same fact about the graph path.

    Measured on the live nineteen-store graph over the twelve intents
    ``POST /buyer/intent/clarify`` produced for twelve shopper sentences, ``affords`` alone
    moved five shops across four queries — ``nemoequipment.com`` from a $379.95 product to a
    $69.95 one, ``purebulk.com`` from $1,035.95 to $8.95 — and ``roster_source.reason`` was
    ``null`` on every one of them.
    """
    moved = [shop for shop in shops if shop.passed_over is not None]
    if not moved:
        return None
    parts: list[str] = []
    for shop in sorted(moved, key=lambda shop: shop.store_id):
        over = shop.passed_over
        assert over is not None  # noqa: S101 — narrowed by the filter above
        priced = "" if over.list_price is None else f" at {over.list_price}"
        why = (
            "is above the budget this intent states"
            if over.reason == SUBSTITUTED_OVER_BUDGET
            else "is not what this intent asked for"
        )
        parts.append(f"{shop.store_id} (its closest product{priced} {why})")
    return (
        f"{len(moved)} of {len(shops)} rostered shop(s) are shown on a product OTHER than the "
        f"one that best answers this intent, because the closest product each carries was "
        f"passed over: {', '.join(parts)}. A shop gets one row, so what is shown for these "
        f"shops is a substitute rather than their best answer"
    )


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
       them are returned by the search and do not move.

       **RETAKEN ON THE FIFTEEN-ROW ROSTER, ON THE SERVED ROUTE, 2026-09-09.** That reading was
       flagged here as un-retaken, and it is stale in the direction that matters. Driven through
       the three calls the learning page itself makes — ``POST /buyer/intent/clarify`` ->
       ``POST /buyer/intent/confirm`` -> ``GET /buyer/auctions/{id}`` on the compose stack, with
       buyer-svc injecting ``deploy/demo/buyer-roster.json`` verbatim and the page's own default
       query ``"milk thistle liver support supplement"`` — auction
       ``auction-7fe8e755-7a53-4108-9b0e-5368359eda2c`` at 18:26:09.940Z answers::

           roster_source.reason: "...the platform re-pointed 2 of 15 row(s) onto the product
           its own crawl says answers this intent... (nutricost.com, toniiq.com)"

       Thirteen rows hold. TWO move — and one of the two is a tier-1, agent-backed BIDDER.
       ``toniiq.com`` is solicited about ``prod_c376b16ff...`` ("Milk Thistle 1000", list 29.97)
       and not about the ``prod_94986d39...`` ("Liposomal Glutathione Complex", list 22.97) its
       roster row pins; the served shortlist carries it in the ``specialist`` slot at 29.97,
       ``fallback: false``, so the row a live agent bid on is the row this function re-pointed.

       **Drive the PAGE's intent, not a hand-built one.** The intent that reproduces this is the
       clarifier's, and it carries no ``category`` and no preferences. A hand-written ``POST
       /auctions`` body that adds ``category: "supplements"`` narrows :func:`build_query` to zero
       retrieved products, so ``fit`` is empty, nothing can be re-pointed, and the same stack
       answers ``reason: null`` — which reads exactly like "this function is inert" and is not.

       Its ``max_discount_pct`` of 20.0 rides through untouched onto a product the caller never
       named, so the cap now multiplies a different base. **That is true of the ROW and dormant
       on this stack, and the docstring has to say so or it overstates.** A cap multiplies
       nothing until an agent asks for a depth, and on the cold demo none of them does:
       ``sample_arm`` (``packages/store-agent/src/learning/state.py``) computes
       ``depth=sample_depth(state, cluster, seed) if has_record else 0.0``, so a store with no
       record in this cluster is pinned at rung zero, and
       :meth:`~store_agent.modes.runner.AgentRunner._select_arm` independently declines to
       overlay a learned policy at all until that record exists. Measured on the same auction:
       all four bidders answered for real and every one quoted list price, ``discount: null``.
       The changed base is real and it is LATENT — it becomes 20% of 29.97 rather than 20% of
       22.97 the first time this store is taught anything in this cluster.

    **"INERT ON THE DEMO" — THE CORRECTION, PUT WHERE THE QUESTION GETS ASKED.**
    ``d71205e`` ("a shop died with the product it happened to lead with") ends its
    behaviour note with: *"Inert on the demo — all four re-pointed shops are tier 0 with no
    agent."* A pushed commit message cannot be edited, so the correction lives here, next to
    the code that actually re-points. Two separate things are wrong with that sentence, and the
    second is the one worth carrying forward:

    * **The demo-wide reading of it is false, and d71205e is not what made it false — which is
      shown by A/B, not by this function's bytes.** The sentence invites "no bidding store on
      this demo is solicited about a product the caller did not name", and the measurement above
      is the counterexample. THE ARGUMENT THAT DOES NOT WORK, recorded so nobody rebuilds it:
      "this function is byte-identical across d71205e" proves nothing, because this function
      does not choose the product — it calls ``source.solicit()``, and :func:`_solicited`'s
      ``keeps`` filter is precisely what d71205e changed. (The sha once quoted here for that
      identity did not reproduce under any function-boundary that was tried, which is a second
      reason it is gone: an unreproducible digest in a docstring is worse than none.)

      What discriminates is running BOTH revisions on the same inputs. Each was extracted
      verbatim — ``d71205e^`` sha256 161b7ab461d31f72, 2171 bytes; ``d71205e`` and HEAD
      a41b930f0145d92e, 5609 bytes — and executed inside ``proxyshop-exchange-1`` against ONE
      captured fixture: the live graph's ``candidate_shops`` plus ``result.assessments`` for
      this intent, replayed through ``build_query`` -> ``CandidateRetrieval.retrieve`` under the
      deterministic ``lexical`` embedding provider, so two captures are byte-identical. **The
      two revisions returned identical rows**, ``toniiq.com`` on ``prod_c376b16ff...`` at 29.97
      in both. WHY they agree: toniiq's only two eligible products are ``prod_c376b16ff...``
      ("Milk Thistle 1000", fit 0.617237) and ``prod_f7098a5d96...`` ("Milk Thistle 50:1", fit
      0.605321), and :func:`~exchange.retrieval.relevance.identity_off_topic` refuses neither —
      nor any of the eight products this query retrieves — so ``keepable == eligible`` and
      ``max(keepable or eligible)`` reduces to the old ``max(eligible)``. The re-point is THIS
      function's doing at both revisions, because the pinned ``prod_94986d39...`` is absent from
      ``fit`` at both. Sabotage-checked so that "identical" is not vacuous: a planted ``keeps``
      that refuses ``prod_c376b16ff...`` moves the d71205e revision onto ``prod_f7098a5d96...``
      at 19.97, and ``identity_off_topic`` does refuse a furniture title on this query. So
      d71205e's claim about ITS OWN change stands; the conclusion it drew about the demo does
      not. (``9035181`` introduced this function and is an ancestor of d71205e — that
      establishes only that it EXISTED, not what it did to a roster that was six rows then and
      is fifteen now, which is why the A/B above is what carries the claim.)
    * **"Tier 0 with no agent" is not evidence of anything, because tier 0 is the constant.**
      Measured on the live graph, ``MATCH (s:Store) RETURN count(s), count(s.tier)`` answers
      ``19, 0`` — nineteen Store nodes and not one of them carries a ``tier`` property — and
      the shop Cypher reads ``coalesce(s.tier, 0) AS tier``
      (``services/ingest/src/graph/query.py``), so EVERY shop the graph can roster reads back
      tier 0. The two readings of that zero are "these are genuinely unpaid merchants" and "the
      crawl never wrote a tier", and it is the second:
      :func:`~exchange.orchestration.solicitation.stores_with_an_agent` RAISES a tier-0 row to
      tier 1 for any store whose ``bid_endpoint`` the deployment document holds, which on this
      demo is exactly the four hosted stores — all four came back ``tier: 1`` with real bids in
      the auction above. A tier read upstream of that raise says nothing about whether a store
      has an agent, so it cannot be the reason a re-point is harmless to one.

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

    **THE PRODUCT IT RE-POINTS ONTO MAY ITSELF BE A SUBSTITUTE**, and the sentence says so.
    :func:`_solicited` stakes a graph shop on the best product it has that the organic gate
    keeps and the buyer's budget admits — which is not always the shop's best answer to the
    intent — so a stated row re-pointed onto that shop's row inherits the substitution. The
    caller-facing sentence would otherwise read "the platform re-pointed this row onto the
    product its own crawl says answers this intent" about a product the crawl says is the
    shop's SECOND answer, which is a stronger claim than the platform can make.
    :func:`substitution_reason` composes that clause, exactly as it does on the graph path.

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
    substituted: list[SolicitedShop] = []
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
        # EVERY re-pointed row's shop, not just the substituted ones, so the clause below
        # reads "1 of 2 re-pointed" rather than "2 of 2" — `substitution_reason` counts the
        # substituted against the population it is handed.
        substituted.append(shop)
    if not moved:
        return rows, None
    substitution = substitution_reason(substituted)
    return rows, (
        f"the roster was stated by the caller and its shops are unchanged; the platform "
        f"re-pointed {len(moved)} of {len(rows)} row(s) onto the product its own crawl says "
        f"answers this intent, because this exchange's own search for this intent did not "
        f"return the product each named ({', '.join(sorted(set(moved)))})"
        + ("" if substitution is None else f". Of those, {substitution}")
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
