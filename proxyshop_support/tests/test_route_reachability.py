"""Every served route must be driven over HTTP by some test. The gate, and its own proof.

**What this gate is for.** This repo's dominant recurring defect is a feature that is built,
unit-tested and reached by no served request — ``EXCHANGE_SHOP_ROSTER=graph`` set in no compose
file so the graph retrieval is off everywhere; the S1 driver hand-building a ``checkout_pixel``
row while the real emitter on ``POST /pixel/collect`` is never called. The frozen acceptance
suite at ``.swarm-loop/acceptance/`` cannot see any of it: measured by AST, its nine modules
and its conftest contain **zero** occurrences of ``create_app``, ``TestClient``,
``ASGITransport`` or ``httpx.Client``. Its assertions are real, but not one request is served,
so it is structurally incapable of catching an unreachable route. That suite is frozen and is
not touched here. This is a second gate beside it, asking the one question it cannot ask.

**The evidence is static, and here is exactly how strong it is.** A route counts as driven
when some test file both (a) constructs a client or an app and (b) names the route's path in
its CODE — where "names" also covers importing the constant the product spells the path with
(``COLLECTOR_PATH``, ``REPORTS_PATH``) and building it out of the test's own constants
(``f"{AUCTION_VIEW}/{auction_id}"``), because that is how tests in this repo actually write a
URL. See :func:`~proxyshop_support.route_census.driven_split`. Four limits, stated plainly
rather than buried:

* **It cannot tell a real assertion from a smoke call.** A test that posts to a route and
  asserts nothing satisfies this gate. Coverage of the *behaviour* is a different question and
  this file does not pretend to answer it.
* **It is method-blind.** Evidence is matched per path, not per verb, so a driven
  ``GET /reconcile`` also credits ``POST /reconcile``. Closing that needs the first argument of
  every client call resolved through the test's own locals, which is a bigger machine than this
  and would trade this gate's few false positives for many false negatives.
* **It can still be fooled by prose, though far less than a raw grep would be.** Comments and
  docstrings are blanked before the search (:func:`~proxyshop_support.route_census.code_text`)
  precisely because they fooled it: ``POST /claims/verifications`` is named in three test files,
  four times, and EVERY ONE is a comment or a docstring — an ASCII service diagram (twice) in
  ``apps/exchange/tests/test_repro_open_tickets.py``, a note in
  ``apps/trust/tests/test_repro_open_tickets.py``, prose in ``e2e/test_s1_flow.py``. A raw-text
  scan calls that route driven. It is not driven by anything. What is NOT blanked is prose
  inside a string that is code — ``RuntimeError("merchant POST /codes is down")`` in
  ``apps/exchange/tests/test_accept_denials.py``, or a spec assertion such as
  ``document["paths"]["/stores/{store_id}/envelope"]["put"]``. Those still read as evidence.
* **A file's evidence is whole-file, not per-call-site, and that is the loosest part.**
  Importing ``COLLECTOR_PATH`` for an assertion about the beacon's registered URL, in a module
  that separately builds a client for something else, credits ``POST /pixel/collect``.
  Adversarial audit of the 308 (route, driver-file) pairs this emitted before the three fixes
  below found 94 of them — 31% — naming a file that issues no such request. **Every one was a
  file-attribution error, not a route-level one**: no route was found called driven that
  nothing in the repo requests. The gate asserts at route level, which is the level the
  measurement holds at; the per-file ``driven_by`` list is a pointer, not a proof.

Three false-positive mechanisms the same audit found were closed rather than documented, each
pinned by a test below: ``serve(`` matching the unrelated local helpers ``_serve(`` and
``_observe(`` (which admitted a file importing only ``ast``/``json``/``pathlib``/``pytest`` as
a driver of five routes); a path regex that could stop inside an f-string placeholder, so
``/auctions/{auction_id}`` matched ``f"/auctions/{auction_id}/accept"`` and was credited to
eighteen files when four issue that GET; and test modules that STAND UP a double of a route —
``@app.get("/reports/losses")`` — counting as drivers of the route they answer.

**What it CANNOT be fooled by, which is the case it exists for.** A route whose path no test
file names anywhere in its code is certainly not driven by a test. That direction is airtight,
and it is the direction a newly added, unreachable route falls in.

**Why an allowlist rather than a bare "everything must be driven".** Starting from the measured
reality is the only way a gate like this lands green and stays honest. Two assertions, and the
second is what keeps the first from rotting:

* nothing outside :data:`UNDRIVEN_ALLOWLIST` may be undriven — this is the regression catch;
* nothing **inside** it may be driven — so the moment somebody writes the missing test, the
  gate tells them to delete the entry. An allowlist nobody prunes is how a gate quietly becomes
  a list of things it no longer checks.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from proxyshop_support.route_census import (
    SERVICES,
    ConstantIndex,
    Route,
    RouteCensusError,
    Service,
    UncensusedRouterCall,
    UnresolvablePath,
    census,
    client_marker_re,
    code_text,
    contract_divergence,
    driven_split,
    expanded_urls,
    route_files,
    route_path_regex,
    routes_in,
)
from proxyshop_support.route_census import test_sources as scanned_test_sources

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Routes no test drives, each with the reason it is not covered yet.
#:
#: **Empty, measured on this branch: 0 entries out of 68 served routes across seven services.**
#: Every route has at least one test file that builds a client and names it.
#:
#: It held exactly one when this census was built — `POST /claims/verifications`, the
#: claim-verification door: served, published in `trust.openapi.json`, and requested by
#: nothing. The path appeared in three test files and every mention was prose. Nor did product
#: code call it: `persist_claim_verification` had one caller in the tree, the handler itself.
#: The verification pipeline was tested through `packages/verification` and through the ledger
#: rows the route would write, never through the route, so nothing would have noticed if the
#: handler stopped answering. `apps/trust/tests/test_claims_verifications_route.py` drives it
#: now, and :func:`test_no_allowlisted_route_is_actually_driven` is what demanded this deletion.
#:
#: The rule for adding an entry: it must name the route AND say why the coverage is absent.
#: "Not covered yet" alone is not a reason, it is a restatement of the fact that put the entry
#: here. The rule for removing one: the moment a test drives the route,
#: :func:`test_no_allowlisted_route_is_actually_driven` fails and demands the deletion.
UNDRIVEN_ALLOWLIST: dict[tuple[str, str, str], str] = {}

#: The manual survey this census was cross-checked against, kept as a second opinion rather
#: than as truth. Both agree, so a future disagreement means one of them moved.
#:
#: The buyer moved: 14 -> 15 when `GET /buyer/store-window` landed (T-142). This test caught it
#: on the full suite and named the service and both numbers, which is the whole point of keeping
#: a hand count beside a derived one — the census cannot notice that IT changed, only that the
#: tree did. Update this the same way: name the route and say why it moved, so the next reader
#: can tell a real drift from a deliberate addition.
EXPECTED_COUNTS: dict[str, int] = {
    "exchange": 7,
    "buyer": 18,  # 14 at first census; +GET /buyer/store-window (T-142, the store's read of
    # the buyer window), +POST /buyer/feedback/order (the order reference R14's
    # post-purchase prompt needs, fetched from the network's own reconciled record),
    # +GET /buyer/auth/sign-in — one boolean saying whether this deployment holds a mail
    # transport it could really deliver a login link through. The SPA reads it on load and
    # renders the sign-in form only when it is true, so a deployment with no MTA stops
    # showing a panel that says "we email you a single-use link" and cannot.
    # +POST /buyer/chat/ask (060225e, the ask-about-these-options stretch stream): the
    # shopper's follow-up questions about a rendered shortlist. Deliberate and driven —
    # `test_chat_ask.py` and `test_chat_ask_live.py`, the latter against a real exchange on a
    # real socket — and published in `packages/contracts/src/openapi.py`. The hand count was
    # simply not moved with it, which is the drift this second opinion exists to catch.
    "merchant": 13,  # +1: POST /stores/{id}/revive, the kill switch's other half
    # (12 was 11 routes + the /dashboard SPA mount)
    "trust": 10,
    "ingest": 9,
    "store-agent": 2,
    "shopify-stub": 14,
}


@pytest.fixture(scope="module")
def routes() -> list[Route]:
    return census(SERVICES, REPO_ROOT)


@pytest.fixture(scope="module")
def split(routes: list[Route]) -> tuple[dict[tuple[str, str, str], list[str]], list[Route]]:
    return driven_split(routes, REPO_ROOT)


# =====================================================================================
# The census itself is non-empty and agrees with what the repo publishes
# =====================================================================================
def test_the_census_finds_every_service_and_matches_the_manual_survey(routes: list[Route]) -> None:
    """An empty or shrunken census would make every assertion below vacuously true.

    Three of this repo's sweeps were caught going QUIET rather than red — a loop that iterates
    zero cases and passes. A reachability gate has the same hazard in a nastier form, because
    "no undriven routes" and "no routes" are the same green.
    """
    counted = {service: 0 for service in EXPECTED_COUNTS}
    for route in routes:
        counted[route.service] = counted.get(route.service, 0) + 1
    assert counted == EXPECTED_COUNTS, "the served surface moved: " + ", ".join(
        f"{service}: {counted.get(service, 0)} (expected {expected})"
        for service, expected in sorted(EXPECTED_COUNTS.items())
        if counted.get(service, 0) != expected
    )


def test_every_service_directory_that_serves_http_is_in_the_service_table() -> None:
    """A service added with routes and not registered here would be invisible to the gate."""
    known = {service.root for service in SERVICES}
    unregistered = sorted(
        str(path.relative_to(REPO_ROOT))
        for base in ("apps", "packages", "services")
        for path in (REPO_ROOT / base).rglob("*/routes.py")
        if "node_modules" not in path.parts
        and not any(str(path.relative_to(REPO_ROOT)).startswith(f"{root}/") for root in known)
    )
    assert unregistered == [], (
        "these route modules belong to no service in route_census.SERVICES, so nothing "
        f"censuses them: {unregistered}"
    )


def test_the_census_agrees_with_every_published_openapi_contract(routes: list[Route]) -> None:
    """What we serve against what we publish, in both directions.

    A disagreement here is a real finding either way round: a published operation nobody serves
    is a lie to an integrator, and a served operation nobody published is an undocumented door.
    """
    divergence = contract_divergence(routes, SERVICES, REPO_ROOT)
    assert divergence == {}, "\n".join(
        f"{service}: {message}" for service, message in sorted(divergence.items())
    )


def test_the_buyer_service_publishes_a_contract_and_the_census_covers_it() -> None:
    """Inverted rather than deleted, because the record is the useful part.

    This used to assert the opposite, and it was true: every service but the buyer published an
    OpenAPI document, the buyer served fourteen routes and published none, and
    :func:`test_the_census_agrees_with_every_published_openapi_contract` therefore compared six
    services while reading like it compared all of them. Nothing was broken and nothing was red,
    which is exactly why it lasted.

    Now that ``buyer.openapi.json`` exists, the comparison covers seven. Keeping the assertion —
    pointed the other way — means the coverage cannot quietly narrow again by someone deleting a
    document or setting ``contract=None`` on a service.
    """
    buyer = next(service for service in SERVICES if service.name == "buyer")
    assert buyer.contract == "packages/contracts/openapi/buyer.openapi.json", (
        "the buyer's Service.contract no longer names its published document, so the "
        "served/published comparison has stopped covering the buyer"
    )
    assert (REPO_ROOT / buyer.contract).is_file(), (
        f"{buyer.contract} is named by the census and is not on disk"
    )
    assert all(service.contract for service in SERVICES if not service.dev_double), (
        "a product service states no contract; every one of them publishes a document, and a "
        "None here removes it from the comparison silently"
    )


# =====================================================================================
# The gate
# =====================================================================================
def _report(routes: list[Route]) -> str:
    return "\n".join(f"    {route.located()}" for route in routes)


#: Which services are product surfaces a deployment answers on, and which are dev doubles.
_DEV_DOUBLES = {service.name for service in SERVICES if service.dev_double}


def test_no_product_route_is_undriven_outside_the_allowlist(
    split: tuple[dict[tuple[str, str, str], list[str]], list[Route]],
) -> None:
    """The regression catch: a new PRODUCT route with no test that drives it fails here.

    Split from the dev double deliberately. ``services/shopify-stub`` is a development stand-in
    for Shopify, not a surface this system deploys; letting an uncovered control endpoint on it
    fail the same assertion as an uncovered ``POST /codes`` would put the two in one bucket and
    train a reader to skim the bucket.
    """
    _driven, undriven = split
    unexpected = [
        route
        for route in undriven
        if route.key not in UNDRIVEN_ALLOWLIST and route.service not in _DEV_DOUBLES
    ]
    assert not unexpected, (
        f"{len(unexpected)} served route(s) that no test drives over HTTP, and that are not on "
        "the allowlist in this file:\n"
        f"{_report(unexpected)}\n\n"
        "A route nothing sends a request to is the defect class this gate exists for: it can "
        "be built, unit-tested and green while answering nobody. Write a test that constructs "
        "the service's app (or launches it) and calls the path — or, if it genuinely cannot be "
        "covered yet, add it to UNDRIVEN_ALLOWLIST with the reason."
    )


def test_no_dev_double_route_is_undriven_outside_the_allowlist(
    split: tuple[dict[tuple[str, str, str], list[str]], list[Route]],
) -> None:
    """The same question of the Shopify stub, asked separately so the answers stay apart.

    All fourteen of its routes are driven today — twelve of them through
    ``shopify_stub.testing.StubClient``, which eight test modules across four services import.
    """
    _driven, undriven = split
    unexpected = [
        route
        for route in undriven
        if route.key not in UNDRIVEN_ALLOWLIST and route.service in _DEV_DOUBLES
    ]
    assert not unexpected, (
        "the Shopify stub serves control endpoints no test calls. It is a dev double, so this "
        "is a smaller finding than an uncovered product route — but an untested double is a "
        "double that can start lying to every test that leans on it:\n" + _report(unexpected)
    )


def test_no_allowlisted_route_is_actually_driven(
    split: tuple[dict[tuple[str, str, str], list[str]], list[Route]],
) -> None:
    """The self-cleaning half. An allowlist nobody prunes stops being a gate."""
    driven, _undriven = split
    stale = {key: driven[key] for key in UNDRIVEN_ALLOWLIST if key in driven}
    assert not stale, (
        "these routes are on UNDRIVEN_ALLOWLIST but a test now drives them — delete the "
        "entries:\n"
        + "\n".join(
            f"    {' '.join(key)}  driven by {', '.join(sorted(files)[:4])}"
            for key, files in sorted(stale.items())
        )
    )


def test_a_newly_added_route_with_no_test_fails_this_gate(routes: list[Route]) -> None:
    """The case the gate exists to catch, exercised rather than assumed.

    Every assertion above is currently green, which on its own is equally consistent with "the
    repo is well covered" and "this gate cannot see anything". So a route that nothing could
    possibly drive is put through the SAME :func:`driven_split` the gate uses, and the gate's
    own condition is evaluated against it. If this ever passes silently, the gate is decorative.
    """
    invented = Route(
        service="exchange",
        method="GET",
        path="/route-census-canary/never-served-anywhere",
        handler="exchange.canary.routes.read_canary",
        file="apps/exchange/src/canary/routes.py",
        line=1,
    )
    driven, undriven = driven_split([*routes, invented], REPO_ROOT)
    assert invented.key not in driven
    assert invented in undriven, "driven_split credited a path no file in the repo contains"
    unexpected = [route for route in undriven if route.key not in UNDRIVEN_ALLOWLIST]
    assert unexpected == [invented], (
        "the gate's own condition did not single out the invented route: "
        f"{[route.located() for route in unexpected]}"
    )


def test_every_allowlist_entry_names_a_route_that_still_exists(routes: list[Route]) -> None:
    """A stale key would sit there forever excusing a route that no longer exists."""
    live = {route.key for route in routes}
    orphans = sorted(key for key in UNDRIVEN_ALLOWLIST if key not in live)
    assert orphans == [], f"UNDRIVEN_ALLOWLIST names routes nothing serves any more: {orphans}"


def test_every_allowlist_entry_gives_a_reason_not_a_restatement() -> None:
    """ "Not covered yet" is not a reason; it is the fact that put the entry on the list."""
    for key, reason in UNDRIVEN_ALLOWLIST.items():
        assert len(reason) >= 80, f"{key}: the reason is too short to be one — {reason!r}"
        assert "/" in reason or ".py" in reason, (
            f"{key}: the reason cites no file, so nobody can check it — {reason!r}"
        )


# =====================================================================================
# The census fails loudly rather than under-reporting — proved, not asserted
# =====================================================================================
def _synthetic_service(tmp_path: Path, body: str, *, feature: str = "widget") -> Service:
    """A one-module service laid out exactly like a real one, for the refusal tests."""
    source = tmp_path / "svc" / "src" / feature
    source.mkdir(parents=True)
    (source / "routes.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return Service(
        name="synthetic",
        root=str((tmp_path / "svc").relative_to(tmp_path)),
        package="synthetic",
        globs=("src/*/routes.py",),
    )


def _index_for(tmp_path: Path) -> ConstantIndex:
    """A :class:`ConstantIndex` rooted at ``tmp_path``, with a ``.pkgroot`` it can read."""
    (tmp_path / ".pkgroot").mkdir(exist_ok=True)
    (tmp_path / ".pkgroot" / "synthetic").symlink_to(tmp_path / "svc" / "src")
    return ConstantIndex(tmp_path)


def _census_one(tmp_path: Path, body: str) -> list[Route]:
    service = _synthetic_service(tmp_path, body)
    index = _index_for(tmp_path)
    file = route_files(service, tmp_path)[0]
    return routes_in(service, file, index)


def test_a_computed_route_path_raises_rather_than_being_skipped(tmp_path: Path) -> None:
    """**The load-bearing test of this whole file.**

    A census that silently drops a route it cannot read turns the gate above into a green light
    for exactly the routes it could not see — the same "built and unreachable" failure, one
    level up and harder to notice, because the gate would be reporting success. So an
    unresolvable path is fatal.
    """
    with pytest.raises(UnresolvablePath) as raised:
        _census_one(
            tmp_path,
            """
            from fastapi import APIRouter

            router = APIRouter()


            def _prefix() -> str:
                return "/computed"


            @router.get(_prefix() + "/thing")
            def read_thing() -> dict[str, str]:
                return {}
            """,
        )
    message = str(raised.value)
    assert "routes.py" in message
    assert "Call" in message or "evaluate" in message


def test_a_route_path_built_from_a_variable_raises(tmp_path: Path) -> None:
    """The same refusal for the shape a worker is most likely to write by accident."""
    with pytest.raises(UnresolvablePath):
        _census_one(
            tmp_path,
            """
            import os

            from fastapi import APIRouter

            router = APIRouter()
            PREFIX = os.environ.get("PREFIX", "/x")


            @router.get(f"{PREFIX}/thing")
            def read_thing() -> dict[str, str]:
                return {}
            """,
        )


def test_an_unrecognised_router_call_raises(tmp_path: Path) -> None:
    """``include_router`` would hide every route of the router it folds in, so it is fatal."""
    with pytest.raises(UncensusedRouterCall) as raised:
        _census_one(
            tmp_path,
            """
            from fastapi import APIRouter

            router = APIRouter()
            inner = APIRouter()
            router.include_router(inner)
            """,
        )
    assert "include_router" in str(raised.value)


def test_a_route_module_that_exports_no_router_raises(tmp_path: Path) -> None:
    """``create_app`` skips such a module with a warning; a silent census would not."""
    with pytest.raises(RouteCensusError) as raised:
        _census_one(
            tmp_path,
            """
            from fastapi import APIRouter

            handlers = APIRouter()


            @handlers.get("/thing")
            def read_thing() -> dict[str, str]:
                return {}
            """,
        )
    assert "router" in str(raised.value)


def test_a_service_whose_glob_matches_nothing_raises(tmp_path: Path) -> None:
    """ "Zero routes" must never be reported as "zero problems"."""
    (tmp_path / "svc" / "src").mkdir(parents=True)
    empty = Service(name="synthetic", root="svc", package="synthetic", globs=("src/*/routes.py",))
    with pytest.raises(RouteCensusError):
        census((empty,), tmp_path)


# =====================================================================================
# The census reads what it claims to read
# =====================================================================================
def test_paths_spelled_as_constants_are_resolved(tmp_path: Path) -> None:
    """A literal, a same-module constant, an imported constant and an f-string, in one file."""
    service = _synthetic_service(
        tmp_path,
        """
        from fastapi import APIRouter
        from synthetic.shared.config import MOUNTED, PREFIX

        router = APIRouter(prefix="/api")
        LOCAL = "/local"


        @router.get("/literal")
        def a() -> dict[str, str]:
            return {}


        @router.get(LOCAL)
        def b() -> dict[str, str]:
            return {}


        @router.post(MOUNTED)
        def c() -> dict[str, str]:
            return {}


        @router.put(f"{PREFIX}/{{item_id}}")
        def d(item_id: str) -> dict[str, str]:
            return {}
        """,
    )
    shared = tmp_path / "svc" / "src" / "shared"
    shared.mkdir()
    (shared / "__init__.py").write_text("", encoding="utf-8")
    (shared / "config.py").write_text('MOUNTED = "/mounted"\nPREFIX = "/items"\n', encoding="utf-8")
    index = _index_for(tmp_path)
    found = routes_in(service, route_files(service, tmp_path)[0], index)
    assert [(route.method, route.path) for route in found] == [
        ("PUT", "/api/items/{item_id}"),
        ("GET", "/api/literal"),
        ("GET", "/api/local"),
        ("POST", "/api/mounted"),
    ]
    assert {alias for route in found for alias in route.aliases} == {"LOCAL", "MOUNTED", "PREFIX"}


def test_the_four_real_constant_spelled_routes_are_resolved(routes: list[Route]) -> None:
    """The specific paths that are NOT literals in this repo, pinned by their resolved value."""
    resolved = {(route.service, route.method, route.path): route.aliases for route in routes}
    assert resolved[("merchant", "POST", "/pixel/collect")] == ("COLLECTOR_PATH",)
    assert resolved[("merchant", "POST", "/codes")] == ("CODES_PATH",)
    assert resolved[("merchant", "MOUNT", "/dashboard")] == ("DASHBOARD_MOUNT",)
    assert resolved[("merchant", "POST", "/webhooks/shopify/{topic:path}")] == (
        "WEBHOOK_PATH_PREFIX",
    )
    assert resolved[("exchange", "GET", "/reports/losses")] == ("REPORTS_PATH",)


def test_routers_defined_inside_functions_are_censused(routes: list[Route]) -> None:
    """The Shopify stub builds its routers inside factory functions, not at module scope."""
    stub = {(route.method, route.path) for route in routes if route.service == "shopify-stub"}
    assert ("POST", "/admin/api/{version}/graphql.json") in stub
    assert ("GET", "/_stub/config") in stub and ("PUT", "/_stub/config") in stub


# =====================================================================================
# The evidence scan reads what it claims to read
# =====================================================================================
def test_prose_is_not_evidence() -> None:
    """A comment and a docstring naming a path must not count as driving it."""
    stripped = code_text(
        textwrap.dedent(
            '''
            """This module drives POST /claims/verifications."""

            # See also GET /snapshot.
            REAL = "/events"
            '''
        )
    )
    assert "/claims/verifications" not in stripped
    assert "/snapshot" not in stripped
    assert '"/events"' in stripped


def test_a_test_that_serves_a_path_is_not_a_driver_of_it() -> None:
    """A double is the opposite of a caller, and it used to be counted as one.

    ``apps/merchant/svc/tests/_fixtures_dashboard.py`` stands up stand-in servers for the
    exchange and the trust service — ``@app.get("/reports/losses")`` at :180,
    ``@app.get("/snapshot")`` at :199 — and was therefore credited as a driver of two routes it
    is guaranteed never to request.
    """
    stripped = code_text(
        textwrap.dedent(
            """
            @app.get("/reports/losses")
            def stand_in() -> dict[str, str]:
                return {}


            def drive(client):
                return client.get("/snapshot")
            """
        )
    )
    assert "/reports/losses" not in stripped
    assert "/snapshot" in stripped


def test_a_parameterised_route_does_not_match_a_deeper_route_under_it() -> None:
    """The f-string-placeholder truncation, pinned.

    Before ``}`` joined the trailing lookahead, ``/auctions/{auction_id}`` matched the text
    ``/auctions/{auction_id`` inside ``f"/auctions/{auction_id}/accept"``, and eighteen files
    were credited with a GET that only four of them issue.
    """
    pattern = route_path_regex("/auctions/{auction_id}")
    assert pattern.search('client.post(f"/auctions/{auction_id}/accept")') is None
    assert pattern.search('client.get(f"/auctions/{auction_id}/shortlist")') is None
    assert pattern.search('client.get(f"/auctions/{auction_id}")') is not None
    assert pattern.search('client.get("/auctions/abc-1")') is not None
    assert pattern.search("/auctions/{}") is not None


def test_a_local_helper_named_like_a_marker_is_not_a_client() -> None:
    """``_serve(`` and ``_observe(`` both contain ``serve(``; neither builds a client.

    ``packages/contracts/tests/test_repro_open_tickets.py`` imports ``ast``, ``json``,
    ``pathlib`` and ``pytest``, constructs nothing, and reads OpenAPI documents off disk. Its
    local ``_serve(status, reason)`` helper was admitting it as a driver of five routes.
    """
    assert client_marker_re().search("result = _serve(404, 'gone')") is None
    assert client_marker_re().search("value = _observe(sample)") is None
    assert client_marker_re().search("with serve(create_app()) as base_url:") is not None
    assert client_marker_re().search("client = TestClient(app)") is not None


def test_a_path_bound_to_a_test_local_constant_is_evidence() -> None:
    """``AUCTION_VIEW = "/buyer/auctions"`` then ``f"{AUCTION_VIEW}/{id}"`` is a real drive."""
    expanded = expanded_urls(
        textwrap.dedent(
            """
            AUCTION_VIEW = "/buyer/auctions"


            def test_it(client):
                client.get(f"{AUCTION_VIEW}/{auction_id}")
            """
        )
    )
    assert "/buyer/auctions/{}" in expanded


def test_the_frozen_acceptance_suite_drives_no_request() -> None:
    """The measurement this whole file rests on, re-taken every run rather than quoted.

    If the acceptance suite ever grows a client, that is excellent news and this assertion is
    the thing that tells us — at which point this test gets inverted, not deleted.
    """
    acceptance = REPO_ROOT / ".swarm-loop" / "acceptance"
    modules = sorted(acceptance.glob("*.py"))
    assert len(modules) >= 9, f"the acceptance suite moved: {modules}"
    drivers = [
        module.name
        for module in modules
        if any(
            marker in code_text(module.read_text(encoding="utf-8"))
            for marker in ("create_app", "TestClient", "ASGITransport", "httpx.Client")
        )
    ]
    assert drivers == [], (
        "the frozen acceptance suite now constructs an app or a client in "
        f"{drivers} — it can finally see unreachable routes, so revisit this gate's premise"
    )


def test_the_test_corpus_the_scan_reads_is_the_whole_repo() -> None:
    """A scan that quietly narrowed would start manufacturing undriven routes."""
    sources = scanned_test_sources(REPO_ROOT)
    assert len(sources) > 200, f"only {len(sources)} test source files found"
    names = {str(path.relative_to(REPO_ROOT)) for path in sources}
    for required in (
        "e2e/support/s1/flow.py",  # the S1 driver: test_s1_flow.py itself issues no request
        "e2e/support/dishonest/served.py",
        "services/shopify-stub/src/testing.py",  # StubClient, imported by eight test modules
        ".swarm-loop/acceptance/test_e3_exchange.py",
        "apps/merchant/svc/tests/conftest.py",
    ):
        assert required in names, f"{required} is not in the scanned corpus"


# =====================================================================================
# The CLI
# =====================================================================================
def test_the_census_is_runnable_as_a_module() -> None:
    """``python -m proxyshop_support.route_census`` prints the table and the split."""
    finished = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "proxyshop_support.route_census", "--include-dev-doubles"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=180,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr
    assert "served route(s) across 7 service(s)" in finished.stdout
    assert "driven by at least one test file:" in finished.stdout
    assert "/pixel/collect" in finished.stdout
