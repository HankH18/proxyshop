"""A real crawl, into the real Neo4j — the organic half of D55, end to end.

WHY THIS FILE EXISTS. ``services/ingest`` could scrape a storefront and it could write a
graph, and no test had ever made it do both at once. ``test_scheduler_refresh.py`` proves
the pipeline reaches the write path, but it observes that path through ``_CapturingSession``
— a stand-in whose result rows answer ``1`` for every missing column *by design*, because
``ingest.graph.upsert`` guards every write with ``RETURN count(...)`` and a stand-in
answering ``0`` would make the fixture assert that the graph refuses everything. The
consequence is precise and was measured before this file was written: **every provenance
guard in the write path passes unconditionally under that double.** An ingest that wrote
material facts with no ``Source`` behind them would be green there.

Measured on the live instance before this file existed: ``MATCH (n) RETURN count(n)`` → 0,
``CALL db.relationshipTypes()`` → ``[]``. Ten uniqueness constraints and twenty-six indexes,
all ONLINE, and nothing had ever been written through them in anger.

WHAT THESE TESTS GRADE, that nothing else does:

* the scrape -> map -> upsert seam against a database that can actually refuse it, so
  ``provenance_violations`` is being run over rows Neo4j really holds;
* **that what the crawl wrote is retrievable.** This is the half that was missing, and it is
  the one D55 turns on: the organic result is the platform rendering its own crawl, so a
  crawl whose products the platform's own retrieval cannot see is not an organic result —
  it is an island in a new place. Before the fix beside this file, a crawl landed a
  complete, fully-provenanced graph and ``candidate_shops(query_text=...)`` answered ``[]``,
  silently, for every shopper query;
* idempotence against the real uniqueness constraints rather than against a counter;
* the published door — ``POST /refresh/{store_id}`` — writing that same real graph.

Every test here takes ``@pytest.mark.docker`` and ``@pytest.mark.graph``; the root
conftest's ``_neo4j_guard`` holds the D37 flock and resets the graph for each one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from ingest.adapters.netguard import FetchPolicy
from ingest.embeddings import HashEmbedding
from ingest.graph import (
    apply_schema,
    candidate_shops,
    products_missing_embeddings,
    provenance_violations,
    reembed_products,
)
from ingest.scheduler.catalog import CatalogRefreshRunner, StoreRegistry, StoreTarget

#: Loopback is not publicly routable, so the default SSRF posture refuses the in-process
#: storefront. Named at the call site on purpose — ``FetchPolicy`` has no environment
#: variable and no test mode, so this loosening is visible wherever it is used.
LOOPBACK = FetchPolicy(allowed_ports=None, extra_allowed_networks=("127.0.0.0/8",))

STORE_ID = "crawl-live-store"

#: What a shopper types. Never compared against a product name — it is embedding input, and
#: the roster's whole vector path depends on the crawl having left something to compare it
#: against.
SHOPPER_QUERY = "trail running shoes for rough ground"


def _counts(session: Any) -> tuple[dict[str, int], dict[str, int]]:
    """Node counts by label and relationship counts by type, read back out of Neo4j."""
    nodes = {
        row["label"]: row["count"]
        for row in session.run(
            "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS count ORDER BY label"
        ).data()
    }
    edges = {
        row["type"]: row["count"]
        for row in session.run(
            "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count ORDER BY type"
        ).data()
    }
    return nodes, edges


@pytest.fixture
def crawl_runner(graph_schema_session: Any, storefront: Any) -> Iterator[Any]:
    """A runner that crawls the in-process storefront straight into the real graph.

    The session factory hands back the *test's own* session rather than opening a second
    driver, so the writes land inside the D37 flock this test already holds and are readable
    by the same connection immediately afterwards.

    Yields:
        ``(runner, session, stub)``.
    """
    base_url, stub = storefront

    @contextmanager
    def live_session() -> Iterator[Any]:
        yield graph_schema_session

    runner = CatalogRefreshRunner(
        registry=StoreRegistry(
            [StoreTarget(store_id=STORE_ID, base_url=base_url, fetch_product_pages=True)]
        ),
        policy=LOOPBACK,
        session_factory=live_session,
    )
    yield runner, graph_schema_session, stub


# =======================================================================================
# 1. The crawl lands a real, provenanced graph
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_a_crawl_lands_a_provenanced_catalog_in_the_real_graph(crawl_runner: Any) -> None:
    """Scrape -> map -> upsert, against a database that can refuse it.

    The node and relationship counts are asserted against the storefront's own contents
    rather than against a magic number, so a crawl that silently dropped a variant fails
    here instead of quietly halving the catalog.
    """
    runner, session, stub = crawl_runner

    report = runner.refresh(STORE_ID)

    assert report.warnings == (), report.warnings
    assert report.products == len(stub.products)
    variants = sum(len(p["variants"]) for p in stub.products)

    nodes, edges = _counts(session)
    assert nodes.get("Store") == 1, nodes
    assert nodes.get("Product") == len(stub.products), nodes
    assert nodes.get("Variant") == variants, nodes
    assert nodes.get("Offer") == variants, nodes
    assert nodes.get("Source", 0) >= 1 + len(stub.products), nodes

    assert edges.get("SELLS") == len(stub.products), edges
    assert edges.get("HAS_VARIANT") == variants, edges
    assert edges.get("MAKES_OFFER") == variants, edges
    assert edges.get("FOR") == variants, edges
    assert edges.get("SUPPORTED_BY", 0) >= 1 + len(stub.products), edges

    assert provenance_violations(session) == [], (
        "the crawl wrote material facts with no Source behind them; candidate_shops() drops "
        "an unsourced shop from the roster entirely, so this graph would look full and "
        "roster empty"
    )


@pytest.mark.docker
@pytest.mark.graph
def test_the_crawl_leaves_no_product_unembedded(crawl_runner: Any) -> None:
    """A product with no vector is invisible to retrieval — the island, in a new place.

    ``reembed_products`` is a whole-catalog operator script, not something a crawl can call:
    it would re-embed every store on every refresh. The crawl embeds what it wrote, and this
    is the assertion that it did.
    """
    runner, session, _stub = crawl_runner

    report = runner.refresh(STORE_ID)

    assert report.warnings == (), report.warnings
    assert products_missing_embeddings(session) == [], (
        "the crawl wrote products the vector index cannot see; every roster query for them "
        "answers [] and nothing says why"
    )
    assert report.embedded == report.products, (
        f"the refresh embedded {report.embedded} of the {report.products} product(s) it read"
    )


# =======================================================================================
# 2. The roster answers from what the crawl wrote  (D55: the organic result)
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_a_crawled_shop_is_on_the_roster_for_a_shoppers_query(crawl_runner: Any) -> None:
    """The spine of the redirected product, driven end to end.

    ``candidate_shops`` is what the exchange asks "which shops do we solicit". A crawl that
    populates the graph and leaves this answering ``[]`` produces a system that is correct,
    wired, and says "no shops" forever.
    """
    runner, session, _stub = crawl_runner

    runner.refresh(STORE_ID)
    roster = candidate_shops(session, query_text=SHOPPER_QUERY, limit=5)

    assert [shop.store_id for shop in roster] == [STORE_ID], (
        f"the crawled shop is not on the roster: {roster}"
    )
    shop = roster[0]
    assert shop.scored, "the roster answered off the structured path; the vector index was unused"
    assert shop.source_ids, "a shop with no Source cannot be pitched under D55"
    assert shop.offers, "the roster carries no priced listing for a store that published prices"
    assert shop.lowest_price == 18.0, shop.offers
    for offer in shop.offers:
        assert offer.source_ids, f"an offer with no provenance reached the roster: {offer}"
        assert offer.price > 0


@pytest.mark.docker
@pytest.mark.graph
def test_the_roster_prices_come_from_the_storefront_not_from_a_default(
    crawl_runner: Any,
) -> None:
    """Every price on the roster is a price the stub actually published."""
    runner, session, stub = crawl_runner

    runner.refresh(STORE_ID)
    roster = candidate_shops(session, query_text=SHOPPER_QUERY, limit=5)

    published = {
        float(variant["price"]) for product in stub.products for variant in product["variants"]
    }
    quoted = {offer.price for shop in roster for offer in shop.offers}
    assert quoted <= published, f"the roster quoted a price nobody published: {quoted - published}"
    assert quoted == published, f"the roster lost a published price: {published - quoted}"


# =======================================================================================
# 3. Idempotence, against the real uniqueness constraints
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_a_second_crawl_of_the_same_store_duplicates_nothing(crawl_runner: Any) -> None:
    """Three crawls, one catalog.

    The second is the differential path (nothing changed, no graph work at all). The third
    forces a full re-read, so every write really is replayed against the constraints — which
    is the case a counter-based idempotence test cannot reach.
    """
    runner, session, _stub = crawl_runner

    runner.refresh(STORE_ID)
    first_nodes, first_edges = _counts(session)

    second = runner.refresh(STORE_ID)
    assert second.changed == 0 and second.ops == 0, "unchanged content did graph work"
    assert _counts(session) == (first_nodes, first_edges), "a no-op crawl changed the graph"

    forced = runner.refresh(STORE_ID, force=True)
    assert forced.ops > 0, "force did not re-read the catalog, so nothing was replayed"
    assert _counts(session) == (first_nodes, first_edges), (
        "a forced re-crawl duplicated nodes or edges"
    )
    assert provenance_violations(session) == []
    assert products_missing_embeddings(session) == []
    assert [shop.store_id for shop in candidate_shops(session, query_text=SHOPPER_QUERY)] == [
        STORE_ID
    ], "the roster stopped answering after a re-crawl"


# =======================================================================================
# 4. The embed the crawl performs must not mix vector spaces
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_a_crawl_refuses_to_embed_into_another_providers_index(crawl_runner: Any) -> None:
    """A recorded pass owns the index's vector space; a crawl may not quietly widen it.

    ``reembed_products`` stamps the index with the provider that filled it. If a later crawl
    embedded its own products with a *different* provider, the index would hold two spaces
    behind a marker saying it holds one — the exact state
    ``EmbeddingProviderMismatch`` exists to prevent, arriving through a door that never
    passes the read-side guard.

    The positive control is in the same test: after the marker names the crawl's own
    provider, the identical crawl embeds normally.
    """
    runner, session, _stub = crawl_runner

    # A full pass by a *different* provider claims the index.
    reembed_products(session, HashEmbedding())

    report = runner.refresh(STORE_ID)

    assert report.written, "the write half must be unaffected: the facts are still facts"
    assert report.embedded == 0, f"the crawl wrote into a foreign vector space: {report.embedded}"
    assert any(
        "EmbeddingProviderMismatch" in warning and "NOT embedded" in warning
        for warning in report.warnings
    ), f"the refusal was silent, which is the failure it exists to prevent: {report.warnings}"
    assert products_missing_embeddings(session), "the skipped products must stay visibly missing"

    # POSITIVE CONTROL. Same crawl, same code path, marker now naming the crawl's provider.
    reembed_products(session)
    forced = runner.refresh(STORE_ID, force=True)
    assert forced.embedded == forced.products, f"honest traffic was refused too: {forced.warnings}"
    assert products_missing_embeddings(session) == []


# =======================================================================================
# 5. The published door writes the same real graph
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_the_served_refresh_route_writes_the_real_graph(
    graph_schema_session: Any, storefront: Any
) -> None:
    """``POST /refresh/{store_id}`` through the running app, into Neo4j.

    The route's own tests observe a ``_CapturingSession``; this one gives the router's module
    runner a factory that yields a real session, so the 202 is evidence that a *request*
    populated the graph rather than that a handler returned.
    """
    from fastapi.testclient import TestClient
    from ingest.main import create_app
    from ingest.scheduler import routes

    base_url, stub = storefront

    @contextmanager
    def live_session() -> Iterator[Any]:
        yield graph_schema_session

    saved = (routes.runner.registry, routes.runner.policy, routes.runner.session_factory)
    routes.registry.register(
        StoreTarget(store_id=STORE_ID, base_url=base_url, fetch_product_pages=True)
    )
    routes.runner.registry = routes.registry
    routes.runner.policy = LOOPBACK
    routes.runner.session_factory = live_session
    routes.runner.forget(STORE_ID)
    try:
        client = TestClient(create_app())
        response = client.post(f"/refresh/{STORE_ID}", json={"sections": ["products"]})

        assert response.status_code == 202, response.text
        assert stub.requests, "the request never reached the storefront"

        nodes, edges = _counts(graph_schema_session)
        assert nodes.get("Product") == len(stub.products), nodes
        assert edges.get("SELLS") == len(stub.products), edges
        assert provenance_violations(graph_schema_session) == []
        assert products_missing_embeddings(graph_schema_session) == []
        assert [
            shop.store_id
            for shop in candidate_shops(graph_schema_session, query_text=SHOPPER_QUERY)
        ] == [STORE_ID], "a served refresh left the roster unable to see the shop it crawled"
    finally:
        routes.runner.registry, routes.runner.policy, routes.runner.session_factory = saved
        routes.registry.unregister(STORE_ID)
        routes.runner.forget(STORE_ID)
        routes._ingestors.pop(STORE_ID, None)


# =======================================================================================
# 6. The command an operator actually runs
# =======================================================================================


@pytest.fixture
def crawl_command_env(
    graph_schema_session: Any, storefront: Any, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """``PROXYSHOP_INGEST_STORES`` pointed at the in-process storefront.

    ``ingest.scheduler.crawl`` opens its own driver out of the environment, exactly as it
    does for an operator, so its writes land in the same database this test already holds
    the D37 flock on and are readable through ``graph_schema_session`` afterwards.
    """
    import json

    base_url, stub = storefront
    monkeypatch.setenv(
        "PROXYSHOP_INGEST_STORES",
        json.dumps([{"store_id": STORE_ID, "base_url": base_url, "fetch_product_pages": True}]),
    )
    return graph_schema_session, stub


@pytest.mark.docker
@pytest.mark.graph
def test_the_crawl_command_populates_the_graph_and_reports_what_it_holds(
    crawl_command_env: Any, capsys: Any
) -> None:
    """The whole point of the command: after it, the graph is not empty and the roster works.

    Driven through ``main`` rather than through ``crawl`` so the argument parsing, the exit
    status and the printed summary are all graded — the first version of this command shipped
    with ``registry.store_ids()`` calling a property, which every other kind of test would
    have missed.
    """
    from ingest.scheduler.crawl import main

    session, stub = crawl_command_env

    status = main(["--all", "--allow-network", "127.0.0.0/8", "--allow-any-port"])

    assert status == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "provenance violations: 0" in out, out
    assert "products missing embeddings: 0" in out, out

    nodes, edges = _counts(session)
    assert nodes.get("Product") == len(stub.products), nodes
    assert edges.get("SUPPORTED_BY", 0) > 0, edges
    assert [shop.store_id for shop in candidate_shops(session, query_text=SHOPPER_QUERY)] == [
        STORE_ID
    ]


@pytest.mark.docker
@pytest.mark.graph
def test_the_crawl_command_will_not_reach_loopback_without_being_told_to(
    crawl_command_env: Any, capsys: Any
) -> None:
    """The SSRF default is intact, and the flag is the only way past it.

    ``FetchPolicy`` has no environment variable on purpose. If this command had grown one —
    or defaulted to a loosened posture because its own demo needed one — the guard's default
    would stop being something anything else could rely on.
    """
    from ingest.scheduler.crawl import main

    session, _stub = crawl_command_env

    status = main(["--all"])

    assert status == 1, "the crawler reached a private address with the default posture"
    assert _counts(session) == ({}, {}), "a refused crawl still wrote to the graph"
    assert "warning" in capsys.readouterr().err.lower()


def test_a_bare_crawl_command_refuses_to_crawl_everything_by_accident() -> None:
    """No store named and no ``--all`` is a mistake, not "every merchant we know of"."""
    from ingest.scheduler.crawl import main

    with pytest.raises(SystemExit) as exit_status:
        main([])
    assert exit_status.value.code == 2


# =======================================================================================
# 7. Honest traffic: an empty catalog still answers nothing rather than refusing
# =======================================================================================


@pytest.mark.docker
@pytest.mark.graph
def test_a_graph_with_no_crawl_still_answers_nothing_rather_than_raising(
    neo4j_session: Any,
) -> None:
    """The control for every refusal above: ``[]`` is the whole truth on an empty catalog."""
    apply_schema(neo4j_session)
    assert candidate_shops(neo4j_session, query_text=SHOPPER_QUERY) == []
