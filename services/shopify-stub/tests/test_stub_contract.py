"""The contract with the rest of the repo, and the seeding surface T-080 needs.

This ticket has **zero acceptance tests in the frozen suite** — the alias
``services.shopify_stub`` is registered there but nothing ever imports through it — so
nothing outside this directory pins any name the stub chooses. Two things nonetheless *are*
pinned from outside, and this file exercises both against the real fixture rather than
against a copy of it:

* ``shopify_stub.app:app``, which the root ``conftest.py``'s ``shopify_stub_url`` fixture
  imports by that exact path and serves. That fixture ``pytest.skip``s when the import
  fails, so a broken app would leave a *green* suite everywhere else in the repo. The test
  below asserts the fixture actually produced a working server, which converts that silent
  skip into a failure here.
* ``services/shopify-stub/fixtures/recorded`` existing, which the frozen scaffold smoke test
  checks.

The seeding tests cover the other outward-facing obligation: T-080 depends on this ticket
and nothing else, and its acceptance says the generator seeds stores *into shopify-stub* and
that ``make demo-seed`` is **idempotent against the stub**. Idempotency is therefore a
property of this stub, not of the seeder.
"""

from __future__ import annotations

import re

import httpx
import pytest
from shopify_stub import app as app_module
from shopify_stub.state import DEFAULT_ACCESS_TOKEN
from shopify_stub.testing import SEED_VARIANT, SUBSCRIBE_MUTATION, StubClient

VARIANT_ID = int(SEED_VARIANT["variant_id"])


async def test_the_root_fixture_serves_a_working_stub(shopify_stub_url: str) -> None:
    """The pinned import path really is served, not merely importable.

    ``shopify_stub_url`` skips rather than fails when ``shopify_stub.app:app`` cannot be
    imported. A skip is indistinguishable from a pass in the metrics, so this test does the
    work the fixture declines to: it makes a request and checks the answer.
    """
    async with httpx.AsyncClient(base_url=shopify_stub_url) as client:
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        graphql = await client.post(
            "/admin/api/2026-07/graphql.json",
            json={"query": "query { orders(first: 1) { edges { cursor } } }"},
            headers={"X-Shopify-Access-Token": DEFAULT_ACCESS_TOKEN},
        )
        assert graphql.status_code == 200
        body = graphql.json()
        assert "errors" not in body, body
        # Not `"orders" in data` — that is an is-not-None-grade check that any garbage
        # value satisfies. The connection has to be well formed.
        connection = body["data"]["orders"]
        assert isinstance(connection["edges"], list)
        assert set(connection["pageInfo"]) == {
            "hasNextPage",
            "hasPreviousPage",
            "startCursor",
            "endCursor",
        }

        # And the auth path must really be enforced on this instance, not just on the
        # per-test stubs — otherwise "the pinned entry point works" would be proven by an
        # endpoint that answers anyone.
        unauthenticated = await client.post(
            "/admin/api/2026-07/graphql.json",
            json={"query": "query { orders(first: 1) { edges { cursor } } }"},
        )
        assert unauthenticated.status_code == 401


def test_the_recorded_fixtures_directory_is_where_d21_puts_it() -> None:
    """D21 fixes this ticket's recordings at ``services/shopify-stub/fixtures/recorded/**``."""
    from shopify_stub.recordings import RECORDINGS_DIR

    assert RECORDINGS_DIR.is_dir()
    assert RECORDINGS_DIR.parts[-3:] == ("shopify-stub", "fixtures", "recorded")
    assert list(RECORDINGS_DIR.glob("*.json")), "the directory must not be empty"


async def test_two_stubs_in_one_process_do_not_share_state(stub: StubClient) -> None:
    """Per-test isolation, asserted rather than assumed.

    The root fixture promises each test owns its own stub state — the drop-rate knob and the
    discount-code table. That promise is only kept if the state hangs off the app instance
    and not off a module global, and a module global would pass every other test in this
    suite while silently leaking one test's codes into the next.
    """
    from shopify_stub.app import create_app

    from proxyshop_support.asgi_server import serve

    await stub.create_code("PSX-ISOLATE1")
    assert "PSX-ISOLATE1" in (await stub.codes())["codes"]

    with serve(create_app()) as other_url:
        async with httpx.AsyncClient(base_url=other_url) as client:
            other = StubClient(client, other_url)
            assert (await other.codes())["codes"] == {}
            await other.configure(pixel_drop_rate=1.0)
            assert (await other.config())["pixel_drop_rate"] == 1.0

    assert (await stub.config())["pixel_drop_rate"] == 0.0


async def test_reset_clears_state_but_keeps_configuration(stub: StubClient) -> None:
    await stub.configure(pixel_drop_rate=0.25, pixel_seed=7)
    await stub.create_code("PSX-RESETME1")
    await stub.buy(VARIANT_ID)

    assert (await stub.http.post("/_stub/reset")).status_code == 200
    assert (await stub.codes())["codes"] == {}
    assert await stub.events() == []
    assert (await stub.config())["pixel_drop_rate"] == 0.25, (
        "reset wipes data, not configuration: a test that configured a stub and then reset "
        "it must not silently get the defaults back"
    )


# ---------------------------------------------------------------------------------------
# Seeding — T-080's dependency
# ---------------------------------------------------------------------------------------


async def test_seeding_is_idempotent(stub: StubClient) -> None:
    """``make demo-seed`` must be safe to run twice (T-080 acceptance 3).

    An identical re-seed reports ``unchanged``, not ``created`` — so a caller can tell "the
    seed was already applied" from "the seed applied again", which is the whole content of
    the idempotency claim.
    """
    payload = [SEED_VARIANT, {**SEED_VARIANT, "variant_id": 44352914, "sku": "TR-43"}]

    first = (await stub.seed(payload)).json()
    assert first == {"created": 1, "unchanged": 1, "updated": 0}, (
        "the fixture already seeded SEED_VARIANT, so only the second is new"
    )

    second = (await stub.seed(payload)).json()
    assert second == {"created": 0, "unchanged": 2, "updated": 0}

    changed = (await stub.seed([{**SEED_VARIANT, "price": "120.00"}])).json()
    assert changed == {"created": 0, "unchanged": 0, "updated": 1}


async def test_a_seeded_variant_is_what_the_cart_prices(stub: StubClient) -> None:
    """The seed is the catalog the permalink resolves against, not decoration."""
    await stub.seed([{**SEED_VARIANT, "variant_id": 555001, "price": "42.50"}])
    cart = (await stub.visit_cart(555001, quantity=2)).json()
    assert cart["items_subtotal_price"] == "85.00"
    assert cart["items"][0]["price"] == "42.50"


async def test_an_unavailable_variant_cannot_be_bought(stub: StubClient) -> None:
    await stub.seed([{**SEED_VARIANT, "variant_id": 555002, "available": False}])
    assert (await stub.visit_cart(555002)).status_code == 404


async def test_a_malformed_seed_is_refused_without_partial_application(
    stub: StubClient,
) -> None:
    """A seed that half-applied would leave the store in a state no run can reproduce."""
    response = await stub.seed(
        [{**SEED_VARIANT, "variant_id": 555003}, {"variant_id": "not-a-number"}]
    )
    assert response.status_code == 400
    assert (await stub.visit_cart(555003)).status_code == 404, (
        "nothing from a refused seed may survive"
    )


async def test_seed_requires_a_variants_list(stub: StubClient) -> None:
    assert (await stub.http.post("/_stub/seed", json={})).status_code == 400
    assert (await stub.http.post("/_stub/seed", json={"variants": 3})).status_code == 400


# ---------------------------------------------------------------------------------------
# The Admin API version in the URL
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version",
    ["2019-04", "2025-01", "9999-99", "not-a-version", "unstable", "v1"],
)
async def test_an_admin_api_version_the_stub_does_not_serve_is_a_404(
    stub: StubClient, version: str
) -> None:
    """A version the stub does not model must not answer 200 with real data.

    ``{version}`` was captured and immediately discarded, so a consumer pinned to a retired
    version — or carrying a typo — got a full, plausible, correct-looking response here and a
    404 from Shopify, with nothing in between to tell it. That is the stub being *looser*
    than the API it stands in for, in the one direction that costs a production incident.
    """
    response = await stub.graphql(
        "query { orders(first: 1) { edges { cursor } } }", version=version
    )
    assert response.status_code == 404, response.text
    body = response.json()
    assert "data" not in body
    # Shopify's HTTP-layer errors are a bare string, not the list GraphQL-level errors use.
    assert isinstance(body["errors"], str)
    assert "2026-07" in body["errors"], "the message must name the version the stub does serve"


async def test_the_configured_version_is_the_one_that_answers(stub: StubClient) -> None:
    """Both directions: the configured version works, and moving it moves the door.

    The version is a configured, first-class value — it is echoed in every webhook's
    ``X-Shopify-API-Version`` header and in ``webPixelCreate``'s ``apiVersion.handle``. A
    stub that answered on versions it then contradicts in its own headers is disagreeing with
    itself.
    """
    configured = (await stub.config())["api_version"]
    assert configured == "2026-07"
    ok = await stub.graphql("query { orders(first: 1) { edges { cursor } } }", version=configured)
    assert ok.status_code == 200

    await stub.configure(api_version="2027-01")
    assert (await stub.config())["api_version"] == "2027-01"
    stale = await stub.graphql("query { orders(first: 1) { edges { cursor } } }", version="2026-07")
    assert stale.status_code == 404
    fresh = await stub.graphql("query { orders(first: 1) { edges { cursor } } }", version="2027-01")
    assert fresh.status_code == 200
    assert fresh.json()["data"]["orders"] is not None, (
        "the version gate must not change what a valid request answers"
    )


async def test_the_version_that_answers_is_the_version_the_stub_stamps_on_its_output(
    stub: StubClient,
) -> None:
    """The coherence the gate exists to protect, asserted end to end."""
    await stub.configure(api_version="2027-01")
    response = await stub.graphql(
        SUBSCRIBE_MUTATION,
        {"topic": "ORDERS_PAID", "sub": {"uri": "https://example.test/hook"}},
        version="2027-01",
    )
    subscription = response.json()["data"]["webhookSubscriptionCreate"]["webhookSubscription"]
    assert subscription["apiVersion"]["handle"] == "2027-01"


# ---------------------------------------------------------------------------------------
# T-129 (stub 3): the module route table is the served surface, not a summary of it
# ---------------------------------------------------------------------------------------
#
# `shopify_stub.app`'s module docstring is the only index of this service's HTTP surface —
# there is no OpenAPI page (`docs_url` and `redoc_url` are both None), so a consumer reads
# the docstring or reads every decorator. It drifted: `GET /_stub/codes` was registered at
# `@router.get("/codes")` and served from the day the route landed, and appeared in neither
# group of the table; the delivery-log commit edited that exact table and did not notice.
#
# The two tests below close the drift in both directions by comparing the prose to the
# LIVE application rather than to another copy of the prose. `create_app().openapi()`
# enumerates what is actually routable, so a documented route that is not served and a
# served route that is not documented are each a red test.

#: A row of either reStructuredText table: ``` ``/path`` ``` then the method column.
#: ``GET/PUT`` is one row with two methods, so the method cell is split on ``/``.
_TABLE_ROW = re.compile(r"^``(?P<path>/[^`]+)``\s+(?P<methods>[A-Z]+(?:/[A-Z]+)*)(?:\s|$)", re.M)

#: The "**Operational**: ``GET /healthz``." line, which is prose rather than a table row.
_INLINE_ROUTE = re.compile(r"``(?P<methods>[A-Z]+) (?P<path>/[^`]+)``")


def _documented_surface() -> set[tuple[str, str]]:
    """``(method, path)`` pairs named in ``shopify_stub.app``'s module docstring."""
    doc = app_module.__doc__ or ""
    documented: set[tuple[str, str]] = set()
    for match in _TABLE_ROW.finditer(doc):
        for method in match.group("methods").split("/"):
            documented.add((method, match.group("path")))
    for match in _INLINE_ROUTE.finditer(doc):
        documented.add((match.group("methods"), match.group("path")))
    return documented


def _served_surface() -> set[tuple[str, str]]:
    """``(method, path)`` pairs the freshly-built application actually routes."""
    spec = app_module.create_app().openapi()
    return {
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
    }


def test_the_module_route_table_is_the_served_surface() -> None:
    """Every served route is documented and every documented route is served.

    Set equality, not containment: containment in one direction lets a route be added
    without a table row (the `/_stub/codes` defect) and containment in the other lets a
    row outlive the route it names.
    """
    documented = _documented_surface()
    served = _served_surface()
    assert documented, "the docstring parser found no routes at all — it has stopped working"
    assert served - documented == set(), (
        f"served but undocumented in shopify_stub.app's route table: {sorted(served - documented)}"
    )
    assert documented - served == set(), (
        f"documented in the route table but not served: {sorted(documented - served)}"
    )


def test_the_route_table_names_the_control_plane_route_that_was_missing() -> None:
    """The specific regression, named, so the set comparison above cannot be read as noise.

    ``GET /_stub/codes`` is what the drift hid. Asserted here against the live surface as
    well as the prose, so this stays a fact about the service and not a fact about a
    string.
    """
    assert ("GET", "/_stub/codes") in _served_surface(), "the route is registered and served"
    assert ("GET", "/_stub/codes") in _documented_surface(), (
        "and the control-plane table must name it"
    )
