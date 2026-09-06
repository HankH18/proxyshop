"""The catalog refresh pipeline, exercised end to end (T-236).

T-236's own gate is an AST sweep: *some module the running app imports constructs a catalog
adapter*. That is the right shape for the finding it was written against — the pipeline did
not exist as a running thing — but a sweep over source text cannot tell a construction that
runs from one that merely parses. A module containing ``SignedFetchAdapter()`` inside a
function nobody calls would satisfy it exactly as well as this one does.

So these tests grade the behaviour instead. Every one of them either serves a real storefront
over a real socket and crawls it through the ordinary public entry point, or drives the route
the way a client does. Between them they assert:

* the factory produces objects that really satisfy the ``CatalogAdapter`` protocol;
* a refresh reads the store, maps the snapshot and hands the ops to ``apply_upserts`` — the
  three stages the finding measured as UNREACHED, checked by capturing the session;
* a second refresh of unchanged content performs **no** graph work at all, including opening
  no session, and ``force=True`` undoes that;
* ``POST /refresh/{store_id}``, published in ``packages/contracts/openapi/ingest.openapi.json``
  and previously answered by nothing, is served by the running app and answers the contract's
  202 body exactly;
* a caller cannot name a URL, so the endpoint is not a request forwarder.

The graph is never required. ``apply_upserts`` is observed through an injected session
factory, which is what lets these run in a suite with no Neo4j — a skipped test is not a pass
(T-245), so nothing here is allowed to depend on the compose stack being up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ingest.adapters import CatalogAdapter, CatalogRequest, CatalogSnapshot, FetchPolicy, UpsertOp
from ingest.scheduler.catalog import (
    CATALOG_MCP,
    SIGNED_FETCH,
    STORES_ENV,
    CatalogRefreshRunner,
    StoreRegistry,
    StoreTarget,
    UnknownStore,
    build_catalog_adapter,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
INGEST_OPENAPI = REPO_ROOT / "packages/contracts/openapi/ingest.openapi.json"

#: Loopback is not publicly routable, so the default SSRF posture refuses the in-process
#: storefront. Same constant the T-020 suite and the repro gates use, for the same reason.
LOOPBACK = FetchPolicy(allowed_ports=None, extra_allowed_networks=("127.0.0.0/8",))


class _CapturingSession:
    """A stand-in Neo4j session that records the Cypher it is asked to run.

    ``apply_upserts`` calls into ``ingest.graph.upsert``, which calls ``session.run``. Holding
    the calls rather than a database is what makes "the write path was reached" observable
    without one, and it records the ops in order so dependency order stays checkable.
    """

    def __init__(self) -> None:
        self.queries: list[str] = []

    def run(self, query: str, /, **params: Any) -> Any:
        self.queries.append(str(query))
        return _CapturingResult(params)

    def __enter__(self) -> _CapturingSession:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


class _CapturingResult:
    """Whatever ``ingest.graph.upsert`` asks a result for, answered from the parameters."""

    def __init__(self, params: dict[str, Any]) -> None:
        self._params = params

    def single(self) -> _Row:
        """One row. Missing columns read as ``1`` — see :class:`_Row`."""
        return _Row({k: v for k, v in self._params.items() if k != "props"})

    def consume(self) -> None:
        """The driver's "I do not need the rows" call. Nothing to do here."""
        return None

    def __iter__(self) -> Any:
        return iter(())


class _Row(dict):
    """A result row whose absent columns read as ``1``.

    ``ingest.graph.upsert`` guards every write with an existence count (``RETURN count(n) AS
    c``) and every edge with ``RETURN count(r) AS written``, and raises ``ProvenanceRequired``
    when either is zero. A stand-in that answered ``0`` would make this fixture assert that
    the write path refuses everything, which is the opposite of what these tests measure. The
    graph's own refusal behaviour is covered against a real Neo4j in ``test_graph.py``.
    """

    def __missing__(self, key: str) -> int:
        return 1


class _SessionFactory:
    """A session factory that counts how many times a session was actually opened."""

    def __init__(self) -> None:
        self.opened = 0
        self.sessions: list[_CapturingSession] = []

    def __call__(self) -> _CapturingSession:
        self.opened += 1
        session = _CapturingSession()
        self.sessions.append(session)
        return session


def _target(base_url: str, store_id: str = "store-1", **kwargs: Any) -> StoreTarget:
    return StoreTarget(store_id=store_id, base_url=base_url, **kwargs)


def _forget_store(routes: Any, store_id: str) -> None:
    """Undo every piece of router module state one store can create.

    A single helper on purpose. The fixture and the newer tests each grew their own teardown
    and they DISAGREED — the fixture forgot ``_ingestors`` while a later test remembered it,
    so ``store-1`` leaked out of the file through the one piece neither guard checked. Three
    kinds of module state, one place that lists them.
    """
    routes.registry.unregister(store_id)
    routes.runner.forget(store_id)
    routes._ingestors.pop(store_id, None)


def _runner(base_url: str, **kwargs: Any) -> CatalogRefreshRunner:
    kwargs.setdefault("session_factory", None)
    return CatalogRefreshRunner(
        registry=StoreRegistry([_target(base_url)]), policy=LOOPBACK, **kwargs
    )


# ---------------------------------------------------------------------------------------
# the adapter factory
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", [SIGNED_FETCH, CATALOG_MCP])
def test_the_factory_builds_something_that_really_is_a_catalog_adapter(source: str) -> None:
    """``isinstance`` against the runtime-checkable protocol, not a signature comparison.

    The acceptance suite's own interface check compares ``inspect.signature`` parameter lists
    and never builds either adapter (T-245). This one builds the object the running service
    will actually use.
    """
    adapter = build_catalog_adapter(source)
    assert isinstance(adapter, CatalogAdapter), f"{source} did not satisfy CatalogAdapter"
    assert callable(adapter.fetch_catalog)
    assert callable(adapter.to_upserts)


def test_the_factory_refuses_a_source_it_does_not_implement() -> None:
    """An unknown source is a configuration error and has to say so."""
    with pytest.raises(ValueError, match="unknown catalog source"):
        build_catalog_adapter("carrier_pigeon")


# ---------------------------------------------------------------------------------------
# the run itself
# ---------------------------------------------------------------------------------------


def test_a_refresh_crawls_the_store_and_reaches_the_graph_write_path(storefront: Any) -> None:
    """fetch_catalog -> to_upserts -> apply_upserts, all three, over a real socket.

    The finding behind T-236 measured all three as unreached from ``ingest.main``. This runs
    them: a storefront is served in-process, the registered target is crawled through the
    ordinary adapter, and the ops land in a session the test holds.
    """
    base_url, stub = storefront
    sessions = _SessionFactory()
    runner = _runner(base_url, session_factory=sessions)

    report = runner.refresh("store-1")

    assert report.products == len(stub.products), (
        f"the crawl read {report.products} product(s) of {len(stub.products)}: {report.warnings}"
    )
    assert report.changed == report.products, "a first crawl has no previous hashes to match"
    assert report.ops > report.products, (
        "a product implies at least a store, a product node and a SELLS edge; "
        f"{report.ops} op(s) for {report.products} product(s) is too few"
    )
    assert sessions.opened == 1, "the ops never reached a graph session"
    assert sessions.sessions[0].queries, "a session was opened and nothing was run in it"
    assert report.written, f"apply_upserts wrote nothing: {report.warnings}"
    assert report.adapter == "signed_fetch"
    assert report.snapshot_ref.startswith("snapshot://"), report.snapshot_ref


def test_a_second_refresh_of_unchanged_content_opens_no_graph_session(storefront: Any) -> None:
    """The differential guarantee, in its strongest observable form.

    Not "fewer writes" — *none*, and no connection attempt either. ``build_upserts`` iterates
    ``snapshot.changed_products``, so a run whose every hash matched produces an empty op
    list, and the runner must not open a session for an empty list.
    """
    base_url, _ = storefront
    sessions = _SessionFactory()
    runner = _runner(base_url, session_factory=sessions)

    first = runner.refresh("store-1")
    assert first.ops > 0 and sessions.opened == 1, "the first run did no work; nothing to compare"

    second = runner.refresh("store-1")

    assert second.products == first.products, "the second crawl read a different catalog"
    assert second.changed == 0, f"unchanged content reported {second.changed} changed product(s)"
    assert second.ops == 0, f"unchanged content produced {second.ops} graph write(s)"
    assert sessions.opened == 1, "an empty op list still opened a graph session"


def test_force_makes_the_next_refresh_re_read_everything(storefront: Any) -> None:
    """``force`` is the escape hatch from the hash ledger, and it has to actually work."""
    base_url, _ = storefront
    sessions = _SessionFactory()
    runner = _runner(base_url, session_factory=sessions)

    first = runner.refresh("store-1")
    forced = runner.refresh("store-1", force=True)

    assert forced.changed == first.changed, "force did not re-read the catalog"
    assert forced.ops == first.ops
    assert sessions.opened == 2


def test_an_unreachable_graph_costs_the_write_and_is_named_in_the_warnings(
    storefront: Any,
) -> None:
    """A crawl whose results cannot be stored is still information, and says why.

    ``ops > 0`` with ``written == ()`` and a warning is a different state from ``ops == 0``,
    and the report has to keep them apart — otherwise "nothing to do" and "could not do it"
    read identically to anything downstream.
    """
    base_url, _ = storefront

    def _explode() -> Any:
        raise ConnectionError("Neo4j is not listening")

    runner = _runner(base_url, session_factory=_explode)
    report = runner.refresh("store-1")

    assert report.ops > 0, "the crawl produced no work, so the write could not have failed"
    assert report.written == ()
    assert any("not applied" in w and "ConnectionError" in w for w in report.warnings), (
        f"the failed write was not reported: {report.warnings}"
    )


def test_a_refresh_of_an_unregistered_store_is_refused(storefront: Any) -> None:
    """The registry is closed. A store nobody configured cannot be crawled."""
    base_url, _ = storefront
    runner = _runner(base_url)
    with pytest.raises(UnknownStore, match="store-elsewhere"):
        runner.refresh("store-elsewhere")


def test_an_injected_adapter_is_used_instead_of_the_factory(storefront: Any) -> None:
    """The runner holds a ``CatalogAdapter`` and nothing more specific.

    C6's seam is only real if the runner works with an implementation it has never heard of,
    so this hands it one written here.
    """
    base_url, _ = storefront
    seen: list[CatalogRequest] = []

    class _Nothing:
        def fetch_catalog(self, request: CatalogRequest) -> CatalogSnapshot:
            seen.append(request)
            return CatalogSnapshot(
                store_id=request.store_id,
                base_url=request.base_url,
                observed_at="2026-01-01T00:00:00Z",
                adapter="test-double",
                extractor_version="test@1",
            )

        def to_upserts(self, snapshot: CatalogSnapshot) -> list[UpsertOp]:
            return []

    report = _runner(base_url).refresh("store-1", adapter=_Nothing())

    assert [r.store_id for r in seen] == ["store-1"], "the injected adapter was not called"
    assert report.adapter == "test-double"
    assert report.ops == 0


# ---------------------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------------------


def test_a_target_whose_base_url_does_not_parse_is_refused() -> None:
    """``http://[`` raises out of ``urlsplit``; a target holding one names no store."""
    with pytest.raises(ValueError, match="does not parse to a host"):
        StoreTarget(store_id="store-1", base_url="http://[")


def test_the_registry_reads_the_environment_and_skips_what_it_cannot_read() -> None:
    """One bad entry must not take the service down at import time."""
    warnings: list[str] = []
    registry = StoreRegistry.from_env(
        {
            STORES_ENV: json.dumps(
                [
                    {"store_id": "store-1", "base_url": "https://one.example.com"},
                    {"store_id": "store-2", "base_url": "http://["},
                    {"store_id": "store-3", "base_url": "https://three.example.com", "nope": 1},
                ]
            )
        },
        warnings=warnings,
    )

    # CHANGED from `len(warnings) == 1`. The assertion said "one bad entry produces one
    # warning". Would it still be wrong if I reverted my change? YES: `store-3` carries the
    # unknown field `nope`, and at the time this was written `from_env` dropped unknown fields
    # in SILENCE. An adversarial verifier showed what that buys — `{"souce": "catalog_mcp"}`
    # registers a signed_fetch target and crawls the live URL, a typo turning into traffic at
    # a merchant. The old count encoded that silence as the contract. `from_env` now names
    # ignored fields, so the fixture below legitimately produces TWO warnings, and this
    # asserts each by what it says rather than by counting.
    assert registry.store_ids == ("store-1", "store-3"), registry.store_ids
    assert any("store target" in w and "[1]" in w for w in warnings), warnings
    assert any("does not read" in w and "nope" in w for w in warnings), warnings
    assert len(warnings) == 2, warnings
    assert registry.get("store-1").source == SIGNED_FETCH


def test_a_registry_from_unparseable_configuration_is_empty_rather_than_fatal() -> None:
    warnings: list[str] = []
    assert len(StoreRegistry.from_env({STORES_ENV: "{not json"}, warnings=warnings)) == 0
    assert warnings and "not valid JSON" in warnings[0], warnings


# ---------------------------------------------------------------------------------------
# the published door
# ---------------------------------------------------------------------------------------


@pytest.fixture
def refresh_client(storefront: Any) -> Any:
    """The real app, with the scheduler router's module state pointed at a served store."""
    from fastapi.testclient import TestClient as _TestClient  # noqa: PLC0415
    from ingest.main import create_app  # noqa: PLC0415
    from ingest.scheduler import routes  # noqa: PLC0415

    base_url, stub = storefront
    sessions = _SessionFactory()

    saved = (routes.runner.registry, routes.runner.policy, routes.runner.session_factory)
    routes.registry.register(_target(base_url))
    routes.runner.registry = routes.registry
    routes.runner.policy = LOOPBACK
    routes.runner.session_factory = sessions
    routes.runner.forget("store-1")
    try:
        yield _TestClient(create_app()), stub, sessions
    finally:
        # The router's registry and runner are MODULE state — that is the point of them, and
        # it is also how a fixture leaks into the next test. Everything touched here is put
        # back, including the registration itself: a store left registered would make the
        # unknown-store 404 test next door pass or fail depending on ordering.
        routes.runner.registry, routes.runner.policy, routes.runner.session_factory = saved
        _forget_store(routes, "store-1")


def test_the_published_refresh_route_is_served_by_the_running_app() -> None:
    """It is published in the contract and, before T-236, was answered by nothing (404)."""
    from ingest.main import create_app  # noqa: PLC0415

    app = create_app()
    served = {
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        for method in item
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }
    published = {
        (method.upper(), path)
        for path, item in json.loads(INGEST_OPENAPI.read_text(encoding="utf-8"))["paths"].items()
        for method in item
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }

    assert published, "the ingest contract publishes nothing; this test would measure nothing"
    assert published <= served, (
        f"published but not served: {sorted(published - served)}; served: {sorted(served)}"
    )


def test_posting_a_refresh_runs_the_pipeline_and_answers_the_contracts_body(
    refresh_client: Any,
) -> None:
    """The 202 body is exactly ``{store_id, job_id, provenance}`` — the schema forbids more."""
    client, stub, sessions = refresh_client

    response = client.post("/refresh/store-1", json={"sections": ["products"]})

    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"store_id", "job_id", "provenance"}, sorted(body)
    assert body["store_id"] == "store-1"
    assert body["job_id"]
    assert set(body["provenance"]) == {"source", "ref", "observed_at", "authority_rank"}
    assert body["provenance"]["source"] == "scraped"
    assert body["provenance"]["ref"].startswith("snapshot://")
    assert sessions.opened == 1, "the request did not reach the graph write path"
    assert stub.requests, "the request did not reach the storefront"


def test_the_policies_section_actually_crawls_the_store(refresh_client: Any) -> None:
    """The other half of the published door, which nothing else here exercises.

    The contract's summary is "re-crawl and re-extract one store's catalog AND policy pages",
    and the request body publishes a ``sections`` array to select between them. A route whose
    ``policies`` branch was never run by a test would be exactly the stub-shaped green this
    file exists to avoid: it would import, it would return 202, and it would touch no store.

    The crawl must also obey the runner's configured SSRF posture rather than the transport
    default — a policy fetcher that built its own public-only client would refuse this
    loopback store while the catalog half read it happily, and the 202 would look identical.
    """
    client, stub, _sessions = refresh_client

    response = client.post("/refresh/store-1", json={"sections": ["policies"]})

    assert response.status_code == 202, response.text
    assert set(response.json()) == {"store_id", "job_id", "provenance"}
    attempted = {path for _method, path, _headers in stub.requests}
    assert any(path.startswith(("/policies/", "/pages/")) for path in attempted), (
        "the policies section returned 202 without asking the store for a single policy "
        f"page; the store was asked for {sorted(attempted)}"
    )


def test_the_default_refresh_does_both_sections(refresh_client: Any) -> None:
    """An omitted ``sections`` means everything, so a caller cannot silently get half a run."""
    client, stub, sessions = refresh_client

    response = client.post("/refresh/store-1", json={})

    assert response.status_code == 202, response.text
    attempted = {path for _method, path, _headers in stub.requests}
    assert "/products.json" in attempted, f"the catalog was not read: {sorted(attempted)}"
    assert any(path.startswith(("/policies/", "/pages/")) for path in attempted), (
        f"the policy pages were not read: {sorted(attempted)}"
    )
    # NOT `sessions.opened >= 1`, which the products half satisfies on its own and which
    # therefore said nothing about the policies half. Measured: a policies-only refresh
    # against this stub opens ZERO sessions, because the stub's fallthrough HTML yields no
    # claims and `to_upserts` is empty. So the honest evidence that the policies half RAN is
    # that it recorded page digests in its own ledger.
    from ingest.scheduler import routes as _routes  # noqa: PLC0415

    assert sessions.opened >= 1, "the products half did not reach the graph write path"
    assert _routes.ingestor_for("store-1").ledger.hashes, (
        "the policies half recorded no page digest, so the default refresh ran only half"
    )


def test_a_refresh_for_a_store_nobody_registered_is_a_404(refresh_client: Any) -> None:
    """A caller cannot name a URL, so an unknown store is the end of the road."""
    client, _stub, _sessions = refresh_client
    response = client.post("/refresh/store-nowhere", json={})
    assert response.status_code == 404, response.text
    assert "store-nowhere" in response.json()["detail"]


def test_the_refresh_body_takes_no_address(refresh_client: Any) -> None:
    """The one property that keeps this endpoint from being an SSRF proxy."""
    client, _stub, _sessions = refresh_client
    response = client.post("/refresh/store-1", json={"base_url": "http://169.254.169.254/"})
    assert response.status_code == 422, response.text


def test_an_unknown_section_is_refused_rather_than_silently_ignored(refresh_client: Any) -> None:
    """A 202 for work that did not happen is worse than a refusal."""
    client, _stub, _sessions = refresh_client
    response = client.post("/refresh/store-1", json={"sections": ["policys"]})
    assert response.status_code == 422, response.text
    assert "policys" in response.text


def test_two_refreshes_of_an_unchanged_store_do_the_graph_work_once(refresh_client: Any) -> None:
    """The differential guarantee across two *requests*, which is where it has to hold."""
    client, _stub, sessions = refresh_client

    first = client.post("/refresh/store-1", json={"sections": ["products"]})
    second = client.post("/refresh/store-1", json={"sections": ["products"]})

    assert (first.status_code, second.status_code) == (202, 202), (first.text, second.text)
    assert sessions.opened == 1, (
        "a re-crawl of unchanged content opened a second graph session; the ledger the route "
        "holds between requests is not being consulted"
    )


def test_the_route_is_mounted_by_the_frozen_entrypoints_own_discovery() -> None:
    """No wiring anywhere but the ``*/routes.py`` glob ``main.py`` already performs."""
    from ingest import main  # noqa: PLC0415

    assert "ingest.scheduler.routes" in main.discover_router_modules()
    assert "ingest.scheduler.routes" in main.create_app().state.mounted_routers


def test_the_refresh_404_comes_from_the_handler_and_not_from_an_unmounted_route(
    refresh_client: Any,
) -> None:
    """Arming control for the 404 above: a routing 404 would prove nothing about the registry.

    A path this app genuinely does not serve answers Starlette's own ``{"detail": "Not
    Found"}``. The unknown-store 404 answers a message naming the store and the stores that
    ARE registered, so the two are distinguishable and the test above is measuring the
    registry rather than an unmounted router.
    """
    client, _stub, _sessions = refresh_client

    routing = client.post("/no-such-door", json={})
    unknown_store = client.post("/refresh/store-nowhere", json={})

    assert routing.status_code == 404 and routing.json()["detail"] == "Not Found"
    assert unknown_store.status_code == 404
    assert unknown_store.json()["detail"] != "Not Found", (
        "the unknown-store 404 is indistinguishable from an unmounted route"
    )
    assert "store-1" in unknown_store.json()["detail"], (
        "the refusal does not name what IS registered, so it cannot be told from a routing 404"
    )


# ---------------------------------------------------------------------------------------
# regressions an adversarial verifier found after the first pass
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("base_url", "http://example.com:notaport/", "no usable port"),
        ("max_products", "lots", "not a whole number"),
        ("max_products", 0, "at least 1"),
        ("fetch_product_pages", "yes", "true or false"),
        ("allowed_hosts", ("",), "non-empty strings"),
        ("cassette", "/tmp/recording.json", "only meaningful"),
    ],
)
def test_a_target_that_would_fail_mid_request_is_refused_at_configuration(
    field: str, value: Any, match: str
) -> None:
    """Every one of these reached a request as an HTTP 500 before it was validated here.

    The nastiest is the port. ``urlsplit`` parses it LAZILY, so
    ``"http://example.com:notaport/"`` splits cleanly, reports a hostname, and passes a
    ``safe_split``-based check — then throws the first time anything reads ``.port``, which
    happens while building the crawl's provenance, AFTER the crawl has already succeeded.
    Measured before this validation: ``POST /refresh/{store}`` -> 500 for a store whose
    catalog had been read correctly.

    ``StoreTarget``'s whole reason to exist is that a bad entry is rejected where the message
    names the entry. A field it does not check is a field that fails somewhere else.
    """
    fields: dict[str, Any] = {"store_id": "store-1", "base_url": "https://shop.example.com"}
    fields[field] = value
    with pytest.raises(ValueError, match=match):
        StoreTarget(**fields)


def test_forcing_one_store_does_not_discard_another_stores_extraction_ledger(
    storefront_factory: Any,
) -> None:
    """``force`` means "re-read THIS store", and it used to mean "re-read everything".

    ``ExtractionLedger.hashes`` is keyed by page ref across every store, so clearing it on one
    process-wide ingestor threw away every store's ledger. Measured: forcing store A dropped
    all of store B's remembered pages, and B's next refresh re-extracted work it had already
    done — silently, and at the cost of real model calls once a real extractor is configured.
    """
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from ingest.main import create_app  # noqa: PLC0415
    from ingest.scheduler import routes  # noqa: PLC0415

    url_a, _ = storefront_factory()
    url_b, _ = storefront_factory()
    saved = (routes.runner.policy, routes.runner.session_factory)
    routes.runner.policy = LOOPBACK
    routes.runner.session_factory = _SessionFactory()
    routes.registry.register(_target(url_a, "store-a"))
    routes.registry.register(_target(url_b, "store-b"))
    try:
        client = TestClient(create_app())
        for store in ("store-a", "store-b"):
            assert client.post(f"/refresh/{store}", json={"sections": ["policies"]}).status_code
        remembered_b = dict(routes.ingestor_for("store-b").ledger.hashes)
        assert remembered_b, "store-b remembered no policy page; nothing to lose"

        client.post("/refresh/store-a", json={"force": True, "sections": ["policies"]})

        assert routes.ingestor_for("store-b").ledger.hashes == remembered_b, (
            "forcing store-a discarded what store-b had already extracted"
        )
    finally:
        routes.runner.policy, routes.runner.session_factory = saved
        for store in ("store-a", "store-b"):
            _forget_store(routes, store)


def test_two_concurrent_refreshes_of_one_store_do_the_work_once(storefront: Any) -> None:
    """The handler is ``def``, so Starlette really does run two of them at the same time.

    That is advertised — the module docstring says two stores refresh concurrently — and it is
    exactly what made the differential guarantee false for ONE store: measured, two concurrent
    requests both opened a graph session, because each read the hash ledger before either
    wrote it. The per-store lock closes that: the second request now sees the first's hashes,
    finds nothing changed, and does no graph work.

    What this test deliberately does NOT assert, because it is not true and claiming it would
    be worse than the gap: the merchant is still fetched twice. Serialising two refreshes does
    not merge them, and it should not — a refresh that arrives after another has finished is
    entitled to re-read the store; that is what "refresh" means. Capping how often a caller
    may ask is RATE LIMITING, and this service has none, on this route or any other. That is
    reported as an open gap rather than papered over here.
    """
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    from fastapi.testclient import TestClient  # noqa: PLC0415
    from ingest.main import create_app  # noqa: PLC0415
    from ingest.scheduler import routes  # noqa: PLC0415

    base_url, stub = storefront
    sessions = _SessionFactory()
    saved = (routes.runner.policy, routes.runner.session_factory)
    routes.runner.policy = LOOPBACK
    routes.runner.session_factory = sessions
    routes.registry.register(_target(base_url))
    routes.runner.forget("store-1")
    try:
        client = TestClient(create_app())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                f.result()
                for f in [
                    pool.submit(client.post, "/refresh/store-1", json={"sections": ["products"]})
                    for _ in range(2)
                ]
            ]
        assert [r.status_code for r in results] == [202, 202], [r.text for r in results]
        assert sessions.opened == 1, (
            "two concurrent refreshes of one store both wrote the graph; the second read the "
            "differential ledger before the first updated it"
        )
        # The differential guarantee, stated as the thing it protects: the SECOND refresh
        # found nothing to write, rather than racing the first to write the same nodes twice.
        remembered = routes.runner.hashes.get("store-1") or {}
        assert len(remembered) > 1, f"the first refresh remembered nothing: {remembered}"
        assert stub.paths_fetched().count("/products.json") == 2, (
            "the two requests were merged rather than serialised — see the docstring: this "
            "test asserts they do not RACE, not that the second never happens"
        )
    finally:
        routes.runner.policy, routes.runner.session_factory = saved
        _forget_store(routes, "store-1")


def test_a_locked_dev_store_yields_its_policy_pages_too(locked_storefront: Any) -> None:
    """SPEC A1, both halves of one refresh — the half that was silently reading nothing.

    A password-protected dev store redirects EVERY page to ``/password``. The catalog half of
    a refresh posts the password form and reads the store; the policy half did not, so it read
    zero of six pages and reported six ``redirect-loop`` refusals — while the 202 looked
    exactly the same as a successful run and the warnings never reached the response body.
    A1 is the entire reason ``signed_fetch`` exists, so a refresh that unlocks the store for
    one surface and not the other is half a feature.

    The control is the same store crawled WITHOUT the password: it must still read nothing, or
    this test would pass on a store that was never locked.
    """
    from ingest.extraction.pipeline import PolicyPageIngestor  # noqa: PLC0415

    base_url, stub = locked_storefront

    locked = PolicyPageIngestor().run(store_id="store-1", base_url=base_url, policy=LOOPBACK)
    assert locked.documents == (), (
        "control: without the password this store must yield nothing, or the test below "
        f"proves nothing; it yielded {[d.url for d in locked.documents]}"
    )

    unlocked = PolicyPageIngestor().run(
        store_id="store-1",
        base_url=base_url,
        policy=LOOPBACK,
        storefront_password=stub.password,
    )

    assert unlocked.documents, (
        f"the policy crawl still read nothing from an unlocked store: {unlocked.warnings}"
    )
    assert not any("redirect-loop" in w for w in unlocked.warnings), unlocked.warnings


def test_the_refresh_route_unlocks_the_store_for_the_policy_half(
    locked_storefront: Any,
) -> None:
    """The same property through the published door, which is where it was broken."""
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from ingest.main import create_app  # noqa: PLC0415
    from ingest.scheduler import routes  # noqa: PLC0415

    base_url, stub = locked_storefront
    saved = (routes.runner.policy, routes.runner.session_factory)
    routes.runner.policy = LOOPBACK
    routes.runner.session_factory = _SessionFactory()
    routes.registry.register(_target(base_url, storefront_password=stub.password))
    try:
        response = TestClient(create_app()).post(
            "/refresh/store-1", json={"sections": ["policies"]}
        )
        assert response.status_code == 202, response.text
        assert routes.ingestor_for("store-1").ledger.hashes, (
            "the refresh reported 202 having extracted no policy page from the locked store"
        )
        assert any(path == "/password" for _m, path, _h in stub.requests), (
            f"the policy half never posted the storefront password: {stub.paths_fetched()}"
        )
    finally:
        routes.runner.policy, routes.runner.session_factory = saved
        _forget_store(routes, "store-1")


# ---------------------------------------------------------------------------------------
# the module-state guard. Keep this LAST in the file.
# ---------------------------------------------------------------------------------------


def test_the_routers_module_state_does_not_leak_out_of_this_file() -> None:
    """Takes no fixture, so it sees exactly what the tests above left behind.

    pytest runs a module's tests in definition order, so this only works while it is last —
    and it did NOT stay last: three tests were appended after it and its own docstring went on
    claiming otherwise, which an adversarial verifier caught. That is the failure mode of a
    positional guard, so the comment above is a rule and this assertion set is deliberately
    exhaustive rather than relying on position alone.

    Why a guard at all: the router's registry, runner and per-store ingestors are module
    state — deliberately, because the differential guarantee is a statement about two
    requests. A leak of that state makes other tests PASS, never fail, so nothing else in the
    suite would notice. The ingestor half is not hypothetical: ``refresh_client``'s teardown
    and a later test's teardown disagreed about whether ``_ingestors`` needed clearing, and
    ``store-1`` leaked out of this file through exactly that gap.
    """
    from ingest.scheduler import routes  # noqa: PLC0415

    leaked = [
        name
        for name, present in (
            (f"registry:{sid}", sid in routes.registry) for sid in ("store-1", "store-a", "store-b")
        )
        if present
    ]
    leaked += [f"hashes:{sid}" for sid in routes.runner.hashes]
    leaked += [f"ingestor:{sid}" for sid in routes._ingestors]

    assert not leaked, f"this file leaked router module state out of itself: {leaked}"

    # Identity against the IMPORTED default, not `is not None`. The first version of this line
    # said `is not None`, and an adversarial verifier showed it could never fail: the fixture's
    # substitute is a `_SessionFactory()`, which is also not None — so a teardown that never
    # restored the factory passed the guard whose own message said it caught exactly that.
    # An assertion that cannot fail is worse than no assertion, because it reads as coverage.
    from ingest.scheduler.catalog import graph_session  # noqa: PLC0415

    assert routes.runner.session_factory is graph_session, (
        f"a fixture left the runner's session factory swapped out: "
        f"{routes.runner.session_factory!r}"
    )
    assert routes.runner.policy is None, "a fixture left its SSRF posture on the runner"
    assert routes.runner.budget is None, "a fixture left its crawl budget on the runner"
    # The dropped assertion, recorded rather than silently deleted: `runner.registry is
    # registry` was also here and also could not fail — they are the same object from import
    # and every fixture assigns the same one back, so the identity had no way to break. It
    # measured nothing and is gone.
