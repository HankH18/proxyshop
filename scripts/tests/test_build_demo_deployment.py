"""Gates on ``scripts/build_demo_deployment.py`` — the WINDOW, and the two halves of D55.

Why this file exists
--------------------
The generator had no gates of its own. What existed was
``scripts/tests/test_seed_demo_trust.py``, which reads two of its tables, and a CI step that
runs ``--check``; nothing asserted anything about the documents themselves. So the defect this
file is mostly about lived in the tree unnoticed:

``_store_catalog`` sorted every store's products on ``(-_relevance(entry), price,
product_ref)``, and ``_relevance`` counts ``CLUSTER_TERMS`` — liver-supplement vocabulary. For
any store outside that one category every product scores zero, the sort collapses onto its
second key, and **the snapshot window becomes literally the N cheapest products.** The cheapest
rows in a furniture catalogue are its swatches, its components, its protection plans and its
gift cards. The visible symptom is a shortlist slot the shopper is shown and the platform
cannot name: ``ranking.verification.catalog_identity`` resolves a slot's identity out of this
window, and a row pointing outside it comes back ``identity: null``.

What each group here asserts
-----------------------------
1. **the window is chosen by the store's own inventory** — measured the way the defect was
   found, as the fraction of a store's on-topic products that fall inside its window, per store
   and per pinned probe family. :func:`test_the_old_price_ordering_fails_this_gate` runs the
   SAME measurement over the ordering this replaced and requires it to FAIL, because a gate
   that has never been shown to fail is not a gate.
2. **the window cannot collapse onto price**, which is the specific shape of the old bug and
   is checked directly rather than inferred from the fraction.
3. **D55** — the four hosted storefronts are the only sponsored ones, and everything the
   curated roster promoted is organic: no ``bid_endpoint``, no envelope, no store context, no
   discount depth.
4. **the document fits**, measured against ``composition.MAX_DEPLOYMENT_BYTES`` rather than
   estimated, because an exchange handed a document it refuses answers every auction from its
   fail-closed defaults on a stack that reports healthy.
5. **every rostered row is nameable** — each ``buyer-roster.json`` row points INSIDE its own
   store's window, which is the invariant the whole window question exists to serve.

Collection: ``scripts`` is in ``[tool.pytest.ini_options] testpaths``. Nothing here opens a
socket or needs a database; ``build()`` over the nineteen-store corpus takes about a second.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT = REPO_ROOT / "deploy" / "demo"

#: The probe families ``fixtures/tests/test_real_catalogs_demo.py`` scores the corpus with, and
#: the ones the window is measured against here. Read from that module rather than retyped: two
#: copies of a word list is how two measurements of the same thing start disagreeing.
#:
#: Only the families this roster actually declares are measured. ``apparel`` scores 27 across
#: the corpus — merch in a coffee roaster's catalogue — and measuring a store's window against
#: a category nobody promoted it for would be scoring the window on an accident.
MEASURED_FAMILIES = ("furniture", "coffee", "outdoor", "home-kitchen", "supplements")

#: A (store, family) pair is only measured when the store carries at least this many on-topic
#: products. Below it the fraction is a ratio of single digits and moves by 20% when one
#: product's title changes, which would make this gate flap rather than discriminate.
MIN_ON_TOPIC = 5

#: What fraction of on-topic products fall inside the window, over every measured pair.
#:
#: **Measured, both orderings, on the nineteen-store curated corpus at a window of 250:**
#:
#:     the ordering this replaced  (-relevance, price, ref)   74.1%
#:     an arbitrary permutation of each catalogue              80.8%
#:     the ordering in the tree    (coverage)                  86.7%
#:
#: The middle row is why the floor is not simply "better than before": the old ordering was
#: WORSE THAN NOT SORTING AT ALL, so beating it proves very little. 84% sits above the arbitrary
#: permutation with room for a re-collection to move a store's titles, and
#: :func:`test_the_old_price_ordering_fails_this_gate` shows both of the other orderings fail it.
MIN_ON_TOPIC_IN_WINDOW = 0.84

#: Per-store, so that one huge store cannot carry the aggregate. Measured, the six pairs where
#: the window actually bites (before -> after): bulksupplements/supplements 21% -> 37%,
#: nutricost/supplements 23% -> 52%, purebulk/supplements 50% -> 47%, sabai.design/furniture
#: 72% -> 95%, fellowproducts/coffee 44% -> 83%, fellowproducts/home-kitchen 22% -> 94%.
#: Everything else is 100% before and after — those stores fit inside the window whole.
#:
#: **Two pairs are honest about the limit.** ``purebulk.com`` goes DOWN three points, because
#: the old sort's price tie-break happened to favour its cheap bulk powders, which is where its
#: creatine and magnesium are. And ``bulksupplements.com`` at 37% is above the 31% an arbitrary
#: permutation would average — 250 of 805 products — but below the 42% one particular arbitrary
#: permutation scored, on 43 on-topic products. Neither is a defect in the ordering; both are a
#: store whose catalogue is three times its window.
#:
#: 30% is therefore a floor on the WORST pair and not an aspiration: for a store with 805
#: products in a 250-product window, 31% is what a probe family spread uniformly through the
#: catalogue allows, and asserting more would be asserting something no ordering can deliver.
MIN_ON_TOPIC_IN_WINDOW_PER_PAIR = 0.30


def _module(name: str) -> Any:
    if str(REPO_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator() -> Any:
    return _module("build_demo_deployment")


@pytest.fixture(scope="module")
def documents(generator: Any) -> dict[str, Any]:
    return generator.build()


@pytest.fixture(scope="module")
def catalogues(generator: Any) -> dict[str, tuple[dict[str, Any], list[str]]]:
    """``{host: (catalog, ranked)}`` — the generator's own per-store output."""
    hosts = generator.corpus_hosts()
    vocabularies = generator.store_vocabularies(hosts)
    return {
        host: generator._store_catalog(host, vocabularies, generator.SNAPSHOT_PRODUCTS_PER_STORE)
        for host in hosts
    }


@functools.cache
def _probes() -> dict[str, tuple[str, ...]]:
    spec = importlib.util.spec_from_file_location(
        "_demo_corpus_probes", REPO_ROOT / "fixtures" / "tests" / "test_real_catalogs_demo.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_demo_corpus_probes"] = module
    spec.loader.exec_module(module)
    return {name: module.CATEGORY_PROBES[name] for name in MEASURED_FAMILIES}


@functools.cache
def _word(term: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)


def _on_topic(title: str, terms: Sequence[str]) -> bool:
    return any(_word(term).search(title) for term in terms)


def _in_window(
    catalogues: Mapping[str, tuple[dict[str, Any], list[str]]],
    window: int,
    order: Mapping[str, list[str]] | None = None,
) -> tuple[float, dict[tuple[str, str], float]]:
    """``(overall fraction, {(host, family): fraction})`` of on-topic products inside the window.

    ``order`` overrides the generator's ranking, which is how the ordering this replaced is put
    through the identical measurement.
    """
    inside = total = 0
    pairs: dict[tuple[str, str], float] = {}
    for host, (catalog, ranked) in catalogues.items():
        window_refs = set((order or {}).get(host, ranked)[:window])
        for family, terms in _probes().items():
            hits = [ref for ref, row in catalog.items() if _on_topic(row["title"], terms)]
            if len(hits) < MIN_ON_TOPIC:
                continue
            here = sum(1 for ref in hits if ref in window_refs)
            inside += here
            total += len(hits)
            pairs[(host, family)] = here / len(hits)
    assert total, "no (store, family) pair carried enough on-topic products to measure"
    return inside / total, pairs


def _price_order(generator: Any, host: str) -> list[str]:
    """The ordering this change replaced: ``(-cluster relevance, price, product_ref)``."""
    scored: list[tuple[int, float, str]] = []
    for entry in generator._read_products(host):
        priced = generator._priced_variant(entry)
        if priced is None:
            continue
        variant_id, price = priced
        try:
            product_ref, _ = generator._catalog_row(host, entry, price, variant_id)
        except ValueError:
            continue
        scored.append((-generator._relevance(entry), price, product_ref))
    scored.sort()
    return [product_ref for _, _, product_ref in scored]


# =====================================================================================
# 1. the window is chosen by the store's own inventory
# =====================================================================================
def test_the_window_holds_the_products_each_store_is_asked_for(catalogues: Any) -> None:
    """The measurement the whole window change is for, as an assertion.

    A product outside its store's window cannot be named on a shortlist:
    ``ranking.verification.catalog_identity`` resolves a slot's identity out of this document,
    and ``ranking.filters.organic_relevance_reason`` reads that same identity and treats an
    absent one as "unchecked" — so a row outside the window is both nameless and unfilterable.
    """
    generator = _module("build_demo_deployment")
    overall, pairs = _in_window(catalogues, generator.SNAPSHOT_PRODUCTS_PER_STORE)
    assert overall >= MIN_ON_TOPIC_IN_WINDOW, (
        f"only {overall:.1%} of on-topic products fall inside their store's window, under "
        f"{MIN_ON_TOPIC_IN_WINDOW:.0%}. A shopper asking any of these stores for what it "
        f"stocks gets a row the platform cannot name."
    )
    worst = min(pairs.items(), key=lambda kv: kv[1])
    assert worst[1] >= MIN_ON_TOPIC_IN_WINDOW_PER_PAIR, (
        f"{worst[0][0]} holds only {worst[1]:.0%} of its {worst[0][1]} products in its window; "
        f"the aggregate hides it"
    )


def test_the_old_price_ordering_fails_this_gate(catalogues: Any, generator: Any) -> None:
    """The gate is shown to discriminate, on the exact ordering it was written against.

    Without this, :func:`test_the_window_holds_the_products_each_store_is_asked_for` is a number
    nobody has watched go red, and the standing lesson in this repository is that a gate which
    has never failed catches nothing. Two counter-orderings, and the second matters more:

    * ``(-relevance, price, ref)`` — what the tree did — scores 74.1%.
    * an arbitrary permutation of each catalogue scores 80.8%, which is *better than the
      ordering it replaced*. The old sort was worse than not sorting at all, so "beats the old
      one" would have been a very weak claim, and the floor is set above the arbitrary
      permutation instead.
    """
    window = generator.SNAPSHOT_PRODUCTS_PER_STORE
    old = {host: _price_order(generator, host) for host in catalogues}
    old_score, _ = _in_window(catalogues, window, old)
    assert old_score < MIN_ON_TOPIC_IN_WINDOW, (
        f"the price-ordered window scores {old_score:.1%}, which passes this gate — so the "
        f"gate does not discriminate and the ordering change is unmeasured"
    )

    # A fixed permutation, not `random`: `product_ref` is a hash, so sorting on it reversed is
    # arbitrary with respect to every property of the product, and it is the same number on
    # every machine and on every run.
    arbitrary = {
        host: sorted(sorted(catalog), key=lambda ref: ref[::-1])
        for host, (catalog, _) in catalogues.items()
    }
    arbitrary_score, _ = _in_window(catalogues, window, arbitrary)
    assert arbitrary_score < MIN_ON_TOPIC_IN_WINDOW, (
        f"an arbitrary order scores {arbitrary_score:.1%} and passes this gate, so the gate is "
        f"measuring the window's SIZE rather than its contents"
    )


# =====================================================================================
# 2. it cannot collapse onto price
# =====================================================================================
def test_no_stores_window_is_its_cheapest_products(catalogues: Any, generator: Any) -> None:
    """The specific shape of the bug: a tie on every key but price.

    Every product in a store outside the cluster's category scores ``_relevance`` 0, so the old
    sort's SECOND key decided the whole window. This asserts the failure directly rather than
    through the fraction above: a window that is the N cheapest rows, for any store, is the bug
    back — whatever it scores.
    """
    window = generator.SNAPSHOT_PRODUCTS_PER_STORE
    for host, (catalog, ranked) in sorted(catalogues.items()):
        if len(catalog) <= window:
            continue  # the whole catalogue fits; there is no window to get wrong
        cheapest = {
            ref for ref in sorted(catalog, key=lambda r: (catalog[r]["list_price"], r))[:window]
        }
        overlap = len(cheapest & set(ranked[:window])) / window
        assert overlap < 0.9, (
            f"{host}'s window is {overlap:.0%} the same set as its {window} cheapest products"
        )


def test_the_lead_product_of_every_store_is_not_its_cheapest(catalogues: Any) -> None:
    """``ranked[0]`` is what ``buyer-roster.json`` pins the shop to and what the hosted agent's
    envelope puts an explicit floor on. Under the old tie-break it was, for every store outside
    the liver cluster, the cheapest thing in the catalogue — measured, three $1.00 items opened
    one store's window."""
    for host, (catalog, ranked) in sorted(catalogues.items()):
        cheapest = min(catalog, key=lambda ref: (catalog[ref]["list_price"], ref))
        assert ranked[0] != cheapest, (
            f"{host} leads with {catalog[cheapest]['title']!r}, its cheapest product"
        )


def test_the_window_is_deterministic(generator: Any) -> None:
    """Two builds of one corpus must be byte-identical; ``--check`` compares them to the tree."""
    first = generator.build()["exchange-deployment.json"]
    second = generator.build()["exchange-deployment.json"]
    assert generator.render(first) == generator.render(second)


def test_the_committed_documents_are_what_the_corpus_implies(generator: Any) -> None:
    """`--check`, in process. The documents are TRACKED, so a clone runs the demo without
    running the generator — which only works while the two agree."""
    for relative, document in generator.build().items():
        path = OUT / relative
        assert path.is_file(), f"deploy/demo/{relative} is missing"
        assert path.read_text(encoding="utf-8") == generator.render(document), (
            f"deploy/demo/{relative} is not what the corpus implies; re-run "
            f"scripts/build_demo_deployment.py"
        )


# =====================================================================================
# 3. D55 — one sponsored half, one organic half
# =====================================================================================
def test_only_the_four_hosted_storefronts_are_sponsored(
    documents: dict[str, Any], generator: Any
) -> None:
    """A scraped shop is an ORGANIC result carrying a pitch the PLATFORM wrote; an in-network
    shop is SPONSORED and buys the right to make its case in its own voice. Promoting nine
    storefronts into the corpus must not hand any of them a voice.

    Four things make a store sponsored in these documents, and all four are checked: a
    ``bid_endpoint`` on its seller row, a ``STORE_AGENT_CONTEXT`` document, an approved discount
    depth, and a ``max_discount_pct`` on its roster row.
    """
    exchange = documents["exchange-deployment.json"]
    bidding = {str(row["store_id"]) for row in exchange["sellers"] if row.get("bid_endpoint")}
    assert bidding == set(generator.HOSTED), (
        f"these stores can bid: {sorted(bidding)}; the sponsored four are "
        f"{sorted(generator.HOSTED)}"
    )
    contexts = {
        key.split("/")[-1].removesuffix(".json")
        for key in documents
        if key.startswith("store-contexts/")
    }
    assert contexts == set(generator.HOSTED)
    assert set(generator.MAX_DISCOUNT_PCT) == set(generator.HOSTED)

    promoted = set(generator.CATALOGUE_ACCURACY)
    assert promoted & set(generator.HOSTED) == set()
    roster = documents["buyer-roster.json"]["roster"]
    for row in roster:
        if str(row["store_id"]) in promoted:
            assert "max_discount_pct" not in row, (
                f"{row['store_id']} is organic and carries an approved discount depth"
            )


def test_every_promoted_store_is_on_the_roster_and_in_the_registry(
    documents: dict[str, Any], generator: Any
) -> None:
    """A shop the stated roster does not name cannot be shortlisted on the buyer route at all.

    ``GraphShopRoster.solicit`` answers the shop set only when the request states NO roster, and
    the buyer service always states one (``buyer_svc.composition.NoRosterBound`` refuses to open
    an auction without). ``repoint_organic_products`` can move a stated row onto the product the
    graph says answers the query; it cannot add a shop. So a furniture query against a roster of
    six supplement shops comes back empty however many coffee tables are in the graph.
    """
    exchange = documents["exchange-deployment.json"]
    sellers = {str(row["store_id"]) for row in exchange["sellers"]}
    assert sellers == set(generator.corpus_hosts())
    assert set(exchange["catalog"]) == sellers, "every seller needs a snapshot to be nameable"

    rostered = {str(row["store_id"]) for row in documents["buyer-roster.json"]["roster"]}
    assert rostered == set(generator.ROSTER_HOSTS)
    missing = set(generator.CATALOGUE_ACCURACY) - rostered
    assert not missing, (
        f"{sorted(missing)} were promoted into the corpus but are not on the stated roster, so "
        f"no buyer-route query can ever reach them"
    )


# =====================================================================================
# 4. the document fits
# =====================================================================================
def test_the_scripts_copy_of_the_deployment_ceiling_agrees_with_the_exchange(
    generator: Any,
) -> None:
    """The generator refuses against its own restated copy, so the copy has to be right."""
    from exchange.composition import MAX_DEPLOYMENT_BYTES

    assert generator.MAX_DEPLOYMENT_BYTES == MAX_DEPLOYMENT_BYTES


def test_the_exchange_document_fits_under_the_ceiling_with_room(
    documents: dict[str, Any], generator: Any
) -> None:
    """Measured on the rendered bytes, which is what ``composition`` measures.

    An exchange handed a document over the ceiling raises at composition time and then keeps
    every fail-closed default — ``ranked: []`` on a stack where ``docker compose ps`` reports
    every row healthy. The budget is the distance from that, not the ceiling itself.
    """
    size = len(generator.render(documents["exchange-deployment.json"]).encode("utf-8"))
    budget = int(generator.MAX_DEPLOYMENT_BYTES * generator.DEPLOYMENT_BYTES_BUDGET)
    assert size <= budget, (
        f"exchange-deployment.json is {size:,} bytes, over the "
        f"{generator.DEPLOYMENT_BYTES_BUDGET:.0%} budget of {budget:,}"
    )


def test_no_stores_snapshot_exceeds_the_per_store_catalogue_ceiling(
    documents: dict[str, Any],
) -> None:
    """``ranking.verification`` refuses a snapshot over ``MAX_CATALOG_PRODUCTS`` per store."""
    from exchange.ranking.verification import MAX_CATALOG_PRODUCTS

    for host, snapshot in documents["exchange-deployment.json"]["catalog"].items():
        assert len(snapshot["products"]) <= MAX_CATALOG_PRODUCTS, host


# =====================================================================================
# 5. every rostered row is nameable
# =====================================================================================
def test_every_roster_row_points_inside_its_own_stores_window(
    documents: dict[str, Any],
) -> None:
    """The invariant the window question exists to serve, on the rows the demo actually ships.

    ``catalogue_readings`` resolves a shortlist slot's ``product.identity`` out of the same
    ``catalog`` block, so a roster row whose product is outside its store's snapshot is served
    to a shopper with ``identity: null`` — a row nobody can name and no relevance filter can
    refuse.
    """
    exchange = documents["exchange-deployment.json"]
    windows = {
        host: {str(p["product_ref"]) for p in snapshot["products"]}
        for host, snapshot in exchange["catalog"].items()
    }
    for row in documents["buyer-roster.json"]["roster"]:
        host, ref = str(row["store_id"]), str(row["product_ref"])
        assert ref in windows[host], (
            f"{host}'s roster row points at {ref}, which is outside its own snapshot; the "
            f"shopper is shown a row the platform cannot name"
        )


def test_every_hosted_agents_floor_names_a_product_it_actually_carries(
    documents: dict[str, Any], generator: Any
) -> None:
    """The envelope's explicit floor names the lead. An agent whose catalogue does not hold the
    product the graph rostered declines ``no_matching_product``."""
    for host in generator.HOSTED:
        context = documents[f"store-contexts/{host}.json"]
        catalog = context["catalog"]
        floors = [f for f in context["envelope"]["floors"] if f.get("product_ref")]
        assert floors, f"{host} states no per-product floor"
        for floor in floors:
            assert str(floor["product_ref"]) in catalog, (
                f"{host}'s envelope floors a product its own catalogue does not carry"
            )
        assert context["store_id"] == host
        assert context["envelope"]["pursue_clusters"] == [generator.CLUSTER_ID]


def test_the_documents_in_the_tree_parse_and_carry_the_stores_they_claim() -> None:
    """Read off disk rather than from ``build()``, so a hand-edited document is caught."""
    exchange = json.loads((OUT / "exchange-deployment.json").read_text(encoding="utf-8"))
    roster = json.loads((OUT / "buyer-roster.json").read_text(encoding="utf-8"))["roster"]
    buyer = json.loads((OUT / "buyer-deployment.json").read_text(encoding="utf-8"))
    assert len(exchange["sellers"]) == 19
    assert len(exchange["catalog"]) == 19
    assert len(buyer["registered_domains"]) == 19
    assert len(roster) == 15
    assert sum(len(s["products"]) for s in exchange["catalog"].values()) == 3241


# =====================================================================================
# 6. the roster states a tier this deployment can actually back
# =====================================================================================
def _stores_this_deployment_can_solicit(documents: Mapping[str, Any]) -> set[str]:
    """The stores the exchange holds a ``bid_endpoint`` for, read off the document itself.

    THE SAME REGISTRY THE RUNNING EXCHANGE ASKS, and that is the whole point of reading it
    here rather than re-deriving the set from ``HOSTED`` or from which ``store-contexts/``
    files exist. ``exchange.composition`` builds ``HttpBidSolicitor._endpoints`` out of
    ``sellers[].bid_endpoint`` in this document; ``orchestration.solicitation
    .stores_with_no_agent`` asks that solicitor ``can_solicit(store_id)``; and a store it
    answers ``False`` for is dropped from ``askable`` and never dialled. So this set is
    exactly "who can bid", and a roster row claiming a bidding tier for a store outside it is
    a claim no component of this deployment can honour.
    """
    return {
        str(row["store_id"])
        for row in documents["exchange-deployment.json"]["sellers"]
        if row.get("bid_endpoint")
    }


def test_no_roster_row_claims_a_tier_the_deployment_cannot_back(
    documents: dict[str, Any],
) -> None:
    """D28's tier vocabulary is ``0`` catalogue-only, ``1`` network-hosted agent, ``2`` external
    agent behind the signed door. A roster row is the buyer service's STATED claim about a
    store, sent on every confirmation, and a claim of tier 1 for a store this deployment holds
    no bid endpoint for is a claim about an agent that does not exist.

    **The contradiction it puts on the shopper's screen.** ``collect_bids`` tests ``tier <= 0``
    FIRST, so a tier-1 row survives into ``tiered``, is dropped from ``askable`` by
    ``stores_with_no_agent``, and comes back carrying the exchange's own
    ``tier_0_no_agent:no_bid_endpoint`` marker — an entry published as ``tier: 1`` beside a
    reason whose first word is ``tier_0``. Measured on the served buyer route before this gate,
    on ``"I want a cherry wood table under $200"``: eleven of the fifteen entries read exactly
    that way, and the two halves of one row disagreed about whether the store had an agent.

    It also splits the two roster paths. ``ingest.graph.model.Store`` asserts no tier, so the
    graph route reads ``coalesce(s.tier, 0)`` — catalogue-only — for the same eleven stores this
    hand-built document called tier 1.
    """
    can_bid = _stores_this_deployment_can_solicit(documents)
    lying = [
        (str(row["store_id"]), row.get("tier"))
        for row in documents["buyer-roster.json"]["roster"]
        if int(row.get("tier", 1)) > 0 and str(row["store_id"]) not in can_bid
    ]
    assert not lying, (
        f"{len(lying)} roster rows claim a bidding tier for a store this deployment holds no "
        f"bid_endpoint for: {lying}. The exchange answers each of them "
        f"tier_0_no_agent:no_bid_endpoint, so the document contradicts the service."
    )


def test_every_store_with_an_endpoint_is_rostered_at_a_bidding_tier(
    documents: dict[str, Any], generator: Any
) -> None:
    """The other direction, and it is not symmetric with the one above.

    Understating a tier is not the safe error it looks like. ``collect_bids`` reads ``tier <= 0``
    before it looks at whether an answer arrived and DISCARDS whatever came back under that
    store's name, so a hosted agent rostered at 0 is dialled, bids, and has its bid thrown away
    — measured in ``stores_with_an_agent`` as ``solicited 0, sponsored 0, prices {100.0: 19}``.

    ``solicit_bids`` does raise such a row back to 1, but only when the solicitor answers
    ``can_solicit`` affirmatively, and that hook is OPTIONAL by design: ``NullSolicitor``,
    ``e2e``'s ``HostedAgentSolicitor`` and every in-process double do not implement it and get
    "decide on tier alone". A document that stated 0 for the four hosted stores would therefore
    be correct only on deployments that happen to run the HTTP solicitor. So this document
    states the tier it can justify rather than leaning on a repair meant for a crawl that
    cannot know.
    """
    can_bid = _stores_this_deployment_can_solicit(documents)
    rostered = {
        str(row["store_id"]): int(row.get("tier", 1))
        for row in documents["buyer-roster.json"]["roster"]
    }
    assert can_bid == set(generator.HOSTED)
    for host in sorted(can_bid & set(rostered)):
        assert rostered[host] > 0, (
            f"{host} has a bid_endpoint in this deployment and is rostered tier "
            f"{rostered[host]}; collect_bids would discard its bid"
        )


def test_the_committed_roster_in_the_tree_states_the_tiers_it_can_back() -> None:
    """The same gate on the tracked artifact, read off disk rather than from ``build()``.

    A clone runs the demo off these files without ever running the generator, so a hand-edited
    or stale ``buyer-roster.json`` is what the shopper would actually meet.
    """
    exchange = json.loads((OUT / "exchange-deployment.json").read_text(encoding="utf-8"))
    roster = json.loads((OUT / "buyer-roster.json").read_text(encoding="utf-8"))["roster"]
    can_bid = {str(row["store_id"]) for row in exchange["sellers"] if row.get("bid_endpoint")}
    assert len(can_bid) == 4, sorted(can_bid)
    by_tier: dict[int, list[str]] = {}
    for row in roster:
        by_tier.setdefault(int(row.get("tier", 1)), []).append(str(row["store_id"]))
    assert sorted(by_tier) == [0, 1], f"the demo roster states tiers {sorted(by_tier)}"
    assert set(by_tier[1]) == can_bid, (
        f"tier-1 rows are {sorted(by_tier[1])}; the stores with an endpoint are {sorted(can_bid)}"
    )
    assert len(by_tier[0]) == 11, sorted(by_tier[0])
