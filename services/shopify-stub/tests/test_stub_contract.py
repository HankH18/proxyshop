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

import httpx
from shopify_stub.state import DEFAULT_ACCESS_TOKEN
from shopify_stub.testing import SEED_VARIANT, StubClient

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
        assert "orders" in graphql.json()["data"]


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
