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

    assert registry.store_ids == ("store-1", "store-3"), registry.store_ids
    assert len(warnings) == 1 and "store target" in warnings[0], warnings
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
        routes.runner.registry, routes.runner.policy, routes.runner.session_factory = saved
        routes.runner.forget("store-1")


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
