"""R9's loss reports on a served route, fed by a served auction (T-9b).

Ticket verify::

    PROXYSHOP_WORKER=<n> pytest apps/exchange/tests/test_loss_reports_served.py -q

What was measured before this file existed
------------------------------------------
``apps/exchange/src/reports/`` was 454 lines with 20 passing tests, **no** ``routes.py`` and
**no producer**. ``exchange.main.create_app``'s frozen automount is
``glob("*/routes.py")``, so nothing under ``reports/`` could ever be reached over HTTP; and
``build_loss_report`` was a pure function whose only input in the whole repository was a test
fixture. ``grep -rn 'build_loss_report'`` outside ``reports/`` and its own tests returned
nothing. So R9's win/loss reporting was built, tested, and reachable by nobody — twice over,
because a route with no producer would have served an empty report and looked healthy.

Both halves are driven here, in one direction each:

* a real ``POST /auctions`` writes the loss log (``test_a_served_auction_writes_the_loss_log``),
* a real ``GET /reports/losses`` reads it back through the projection
  (``test_the_served_report_is_the_losing_stores_own_and_carries_no_rival_amount``).

The privacy assertion is the load-bearing one
---------------------------------------------
R9 is explicit that a report carries reason categories and no rival amounts, and the reason is
mechanical rather than legal: a merchant who can read what a competitor bid is holding a price
feed, and a market whose participants can see each other's prices collapses back into the
price auction D55 redirected away from. So the assertion here is inverted, exactly as
``test_loss_reports.py``'s is: the auction is driven with distinctive amounts, and the test
asserts that **none of them appears anywhere in the served body**, rather than that a list of
remembered field names is absent.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from exchange.auction.routes import configure_auctions
from exchange.checkout.sellers import StaticRegisteredDomains
from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.main import create_app
from exchange.ranking.serving import configure_ranking
from exchange.ranking.verification import StaticCatalogSnapshots
from exchange.reports.routes import REPORTS_PATH, configure_reports
from fastapi.testclient import TestClient

CLUSTER = "cluster-loss-1"
PRODUCT = "product-1"

#: The hard constraint every auction below states. A store whose catalogue is under it is
#: excluded ``hard_constraint_unsatisfied``, which is R9's ``fit``.
CAPACITY_FLOOR = 30

#: Six stores, chosen so that every path through ``reports.log`` is reachable from ONE served
#: auction and none of them depends on slot arithmetic going a particular way:
#:
#: ``WINNER``          best on every axis, so it fills a slot and must be reported as having
#:                     lost nothing. It is the control — a producer that logged every candidate
#:                     as a loss would give this store a report too.
#: ``LOSER_ON_FIT``    catalogue capacity under the floor, so the ranker excludes it
#:                     ``hard_constraint_unsatisfied``.
#: ``LOSER_ON_TRUST``  blacklisted, so R12 excludes it before it is ever scored.
#: ``ALSO_RAN_*``      eligible, scored, and — with four slots and five eligible stores — at
#:                     least one of them is ranked and NOT shown, which is the only way to
#:                     reach ``_trailing_component``. They differ from each other and from the
#:                     winner only in trust, so the axis they trail on is ``trust``.
WINNER = "store-winner"
LOSER_ON_FIT = "store-small"
LOSER_ON_TRUST = "store-blacklisted"
ALSO_RAN = ("store-also-ran-a", "store-also-ran-b", "store-also-ran-c", "store-also-ran-d")

#: Amounts chosen so that no two share a spelling and none can be produced by a count of
#: anything in this fixture. Every one is in the auction the report is built from; none may
#: reach the report.
PRICES = {
    WINNER: 71.37,
    LOSER_ON_FIT: 66.13,
    LOSER_ON_TRUST: 58.29,
    ALSO_RAN[0]: 93.41,
    ALSO_RAN[1]: 84.77,
    ALSO_RAN[2]: 79.53,
    ALSO_RAN[3]: 88.19,
}
LIST_PRICES = {
    WINNER: 111.11,
    LOSER_ON_FIT: 444.44,
    LOSER_ON_TRUST: 333.33,
    ALSO_RAN[0]: 222.22,
    ALSO_RAN[1]: 555.55,
    ALSO_RAN[2]: 666.66,
    ALSO_RAN[3]: 777.77,
}
TRUST = {
    WINNER: 0.97,
    LOSER_ON_FIT: 0.55,
    LOSER_ON_TRUST: 0.02,
    ALSO_RAN[0]: 0.44,
    ALSO_RAN[1]: 0.33,
    ALSO_RAN[2]: 0.22,
    ALSO_RAN[3]: 0.11,
}
CAPACITIES = dict.fromkeys(LIST_PRICES, 40) | {LOSER_ON_FIT: 10}

TOKENS = {store: f"tok-{store}-{index}9d41" for index, store in enumerate(sorted(LIST_PRICES))}


def _domain(store_id: str) -> str:
    return f"{store_id}.example.com"


def _claim(key: str, value: Any) -> dict[str, Any]:
    return {
        "key": key,
        "value": value,
        "provenance": {
            "source": "owner_statement",
            "ref": f"envelope:{key}",
            "observed_at": "2026-01-01T00:00:00Z",
            "authority_rank": 1,
        },
    }


def _offer(store_id: str, price: float) -> dict[str, Any]:
    return {
        "product_ref": PRODUCT,
        "unit_price": price,
        "total_price": price,
        "currency": "USD",
        "checkout_url": f"https://{_domain(store_id)}/cart/1:1",
        "expires_at": time.time() + 3600.0,
    }


def _bid(store_id: str, price: float, *, capacity: int) -> dict[str, Any]:
    return {
        "auction_id": None,
        "store_id": store_id,
        "offer": _offer(store_id, price),
        "claims": [_claim("capacity_l", capacity)],
        "agent_version": "1.0.0",
        "schema_version": "1.0.0",
    }


class _Bidders:
    def __init__(self, bids: Mapping[str, dict[str, Any]]) -> None:
        self.bids = dict(bids)

    def solicit(self, store: dict[str, Any]) -> dict[str, Any] | None:
        store_id = str(store["store_id"])
        bid = self.bids.get(store_id)
        if bid is None:
            return None
        return {"store_id": store_id, "received_at": time.time(), "bid": dict(bid)}

    __call__ = solicit


def _catalog(capacities: Mapping[str, int]) -> Any:
    return StaticCatalogSnapshots(
        {
            store: {
                "snapshot_id": f"snap-{store}",
                "products": [
                    {
                        "product_ref": PRODUCT,
                        "canonical_name": PRODUCT,
                        "evidence_ref": f"snap-{store}#{PRODUCT}",
                        "attributes": {"capacity_l": {"value": capacity}},
                    }
                ],
            }
            for store, capacity in capacities.items()
        }
    )


@pytest.fixture
def served() -> Iterator[tuple[TestClient, Any]]:
    """One wired exchange serving both routes, with the report tokens bound."""
    app = create_app()
    configure_auctions(
        app,
        solicitor=_Bidders(
            {store: _bid(store, PRICES[store], capacity=CAPACITIES[store]) for store in LIST_PRICES}
        ),
        eligibility=StaticSellerEligibility({store: ELIGIBLE for store in LIST_PRICES}),
    )
    configure_ranking(
        app,
        trust_snapshot={
            store: {"blacklisted": store == LOSER_ON_TRUST, "score": TRUST[store]}
            for store in LIST_PRICES
        },
        registered_domains=StaticRegisteredDomains(
            {store: _domain(store) for store in LIST_PRICES}
        ),
        catalog=_catalog(CAPACITIES),
    )
    configure_reports(app, tokens=dict(TOKENS))
    yield TestClient(app), app


def _open_one_auction(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/auctions",
        json={
            "intent": {
                "intent_id": "intent-loss-1",
                "cluster_id": CLUSTER,
                "hard_constraints": [{"field": "capacity_l", "op": "gte", "value": CAPACITY_FLOOR}],
            },
            "roster": [
                {
                    "store_id": store,
                    "tier": 1,
                    "product_ref": PRODUCT,
                    "list_price": list_price,
                }
                for store, list_price in LIST_PRICES.items()
            ],
            "bid_timeout_seconds": 5.0,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _shown(body: Mapping[str, Any]) -> set[str]:
    """Which stores filled a slot, read the way the buyer's own response exposes it.

    A published ``ShortlistSlot`` carries no ``store_id`` — it names ``bid_ref`` and a
    ``trust_summary``. Resolved here through ``ranked``'s ``bid_ref`` rather than by splitting
    the id, for the reason ``reports.log._shown_store_ids`` gives: the id's internal shape is
    not a contract, and the first version of both this helper and that function read a key that
    is never there.
    """
    by_ref = {row["bid_ref"]: row["store_id"] for row in body["ranked"]}
    return {
        by_ref[slot["bid_ref"]]
        for slot in body["shortlist"]["slots"]
        if slot.get("bid_ref") in by_ref
    }


def _report(client: TestClient, store_id: str, *, start: float, end: float) -> Any:
    return client.get(
        REPORTS_PATH,
        params={"start": start, "end": end},
        headers={"Authorization": f"Bearer {TOKENS[store_id]}"},
    )


# =====================================================================================
# 1. The route exists, is mounted by the frozen automount, and is reachable
# =====================================================================================
def test_the_frozen_automount_reaches_the_reports_router() -> None:
    """T-9b's first half: ``reports/`` had no ``routes.py``, so the glob could never find it.

    Asserted off the real application object rather than off the filesystem — a
    ``routes.py`` that defines no module-level ``router`` is skipped by ``create_app`` and is
    indistinguishable, from outside, from one nobody wrote.
    """
    app = create_app()
    assert "exchange.reports.routes" in app.state.mounted_routers, app.state.mounted_routers
    assert REPORTS_PATH in app.openapi()["paths"], sorted(app.openapi()["paths"])


# =====================================================================================
# 2. Authorisation: who may call, and how the caller says which store it is
# =====================================================================================
def test_an_exchange_with_no_report_tokens_serves_nobody() -> None:
    """The unset state refuses every caller, which is the shape the merchant service uses.

    An empty expected value compared against an empty supplied one is a route that opens
    itself the moment the deployment is incomplete — see
    ``merchant_svc.install.routes._refuse_unless_admin``, whose 503 this mirrors.
    """
    client = TestClient(create_app())
    for headers in ({}, {"Authorization": "Bearer tok-winner-3f2a"}, {"Authorization": "Bearer "}):
        response = client.get(REPORTS_PATH, params={"start": 0, "end": 9e9}, headers=headers)
        assert response.status_code == 503, (headers, response.status_code, response.text)
        assert response.json()["error"] == "reports-not-configured", response.text


@pytest.mark.parametrize(
    ("label", "headers"),
    [
        ("no authorization header", {}),
        ("a bearer that matches no store", {"Authorization": "Bearer tok-nobody"}),
        ("the right token under the wrong scheme", {"Authorization": f"Basic {TOKENS[WINNER]}"}),
        ("a token with a store id but no secret", {"Authorization": f"Bearer {WINNER}"}),
        ("an empty bearer", {"Authorization": "Bearer "}),
    ],
)
def test_a_caller_the_exchange_cannot_identify_reads_nobodys_losses(
    served: tuple[TestClient, Any], label: str, headers: dict[str, str]
) -> None:
    client, _ = served
    _open_one_auction(client)
    response = client.get(REPORTS_PATH, params={"start": 0, "end": 9e9}, headers=headers)
    assert response.status_code == 401, f"{label}: {response.status_code} {response.text}"
    # The refusal names no store and echoes no token.
    body = response.text
    for secret in TOKENS.values():
        assert secret not in body, f"{label}: the refusal echoed a bearer token: {body}"


def test_the_token_chooses_the_store_so_there_is_no_field_to_ask_for_anothers_losses(
    served: tuple[TestClient, Any],
) -> None:
    """A merchant reads its OWN losses, and the route gives it no way to name a different one.

    The store is resolved from the bearer rather than taken from a path or query parameter, so
    "may this caller read that store" is not a check that can be forgotten — there is no
    parameter in which to ask the wrong question. Every token is driven, because a route that
    returned the same store for all of them would pass a one-token test.
    """
    client, _ = served
    _open_one_auction(client)
    window = {"start": 0, "end": 9e9}

    for store in LIST_PRICES:
        response = _report(client, store, **window)
        assert response.status_code == 200, response.text
        assert response.json()["store_id"] == store, response.text

    # And the query string cannot override it.
    smuggled = client.get(
        REPORTS_PATH,
        params={"start": 0, "end": 9e9, "store_id": LOSER_ON_TRUST},
        headers={"Authorization": f"Bearer {TOKENS[WINNER]}"},
    )
    assert smuggled.status_code in (200, 422), smuggled.text
    if smuggled.status_code == 200:
        assert smuggled.json()["store_id"] == WINNER, smuggled.text


# =====================================================================================
# 3. The producer: a served auction really writes the log the route reads
# =====================================================================================
def test_a_served_auction_writes_the_loss_log(served: tuple[TestClient, Any]) -> None:
    """Without this the route is an island: it would answer 200 with an empty report forever.

    The control is the winner. A build that logged every candidate as a loss would give every
    store a report, and this asserts the shortlisted store's is empty while the losers' are not.
    """
    client, _ = served
    before = time.time()
    body = _open_one_auction(client)
    after = time.time()

    shown = _shown(body)
    assert WINNER in shown, f"the control never won a slot; the fixture is wrong: {body}"
    unshown = set(LIST_PRICES) - shown
    assert unshown, "every store filled a slot; there is no loss in this auction to report"

    for store in sorted(unshown):
        report = _report(client, store, start=before, end=after).json()
        assert [entry["cluster_id"] for entry in report["by_cluster"]] == [CLUSTER], report
        (entry,) = report["by_cluster"]
        assert entry["lost"] == 1, (store, entry)
        # Every loss lands in exactly one category, and the counts add up to the total.
        assert sum(entry["reasons"].values()) == entry["lost"], (store, entry)

    winner = _report(client, WINNER, start=before, end=after).json()
    assert winner["by_cluster"] == [], (
        f"the store that filled a slot was reported as having lost: {winner}"
    )


def test_each_loss_is_categorised_by_what_actually_happened_to_that_store(
    served: tuple[TestClient, Any],
) -> None:
    """R12's blacklist reads ``trust``, an unmet hard constraint reads ``fit``.

    Both categories are reached through the served route by two stores that lost in two
    different ways in the SAME auction, which is what makes this a statement about the mapping
    rather than about one code path being hit.
    """
    client, _ = served
    before = time.time()
    _open_one_auction(client)
    after = time.time()

    (refused,) = _report(client, LOSER_ON_TRUST, start=before, end=after).json()["by_cluster"]
    assert refused["reasons"]["trust"] == 1, (
        f"a store R12 blacklisted must be told it lost on TRUST: {refused}"
    )
    assert refused["reasons"]["fit"] == 0, refused

    (misfit,) = _report(client, LOSER_ON_FIT, start=before, end=after).json()["by_cluster"]
    assert misfit["reasons"]["fit"] == 1, (
        f"a store excluded on an unmet hard constraint must be told it lost on FIT: {misfit}"
    )
    # And it is told WHICH buyer criterion it failed — in the buyer's own words, which is the
    # one thing on the row guaranteed to say nothing about a rival.
    assert misfit["unmet_criteria"] == [f"capacity_l gte {CAPACITY_FLOOR}"], misfit


def test_a_scored_store_that_filled_no_slot_is_told_which_axis_it_trailed_on(
    served: tuple[TestClient, Any],
) -> None:
    """The other half of the mapping: a candidate that WAS ranked and still was not shown.

    These stores differ from the winner and from each other only in trust, so the axis they
    trail on is ``trust``. That is a comparison against the field, and it still leaks nothing:
    what the merchant is handed is one of four fixed words.
    """
    client, _ = served
    before = time.time()
    body = _open_one_auction(client)
    after = time.time()

    ranked = {row["store_id"] for row in body["ranked"]}
    unshown_but_ranked = sorted(ranked - _shown(body))
    assert unshown_but_ranked, (
        f"no store was scored and left out of the shortlist, so `_trailing_component` is not "
        f"exercised: ranked={sorted(ranked)} shown={sorted(_shown(body))}"
    )

    for store in unshown_but_ranked:
        (entry,) = _report(client, store, start=before, end=after).json()["by_cluster"]
        assert entry["reasons"]["trust"] == 1, (
            f"{store} was outranked on trust and told something else: {entry}"
        )


def test_the_window_bounds_what_the_report_counts(served: tuple[TestClient, Any]) -> None:
    """A report is aggregated over a window; a window before the auction counts nothing."""
    client, _ = served
    before = time.time()
    _open_one_auction(client)

    inside = _report(client, LOSER_ON_TRUST, start=before, end=time.time()).json()
    assert inside["by_cluster"], inside

    outside = _report(client, LOSER_ON_TRUST, start=0.0, end=before - 3600.0).json()
    assert outside["by_cluster"] == [], outside
    assert outside["store_id"] == LOSER_ON_TRUST, outside


def test_two_auctions_in_one_cluster_are_aggregated_rather_than_listed(
    served: tuple[TestClient, Any],
) -> None:
    """R9 says *aggregated by intent cluster*, so a second loss raises a count, not a row."""
    client, _ = served
    before = time.time()
    _open_one_auction(client)
    _open_one_auction(client)

    report = _report(client, LOSER_ON_TRUST, start=before, end=time.time()).json()
    (entry,) = report["by_cluster"]
    assert entry["lost"] == 2, entry


def test_every_exclusion_reason_the_ranker_publishes_maps_to_a_category() -> None:
    """An unmapped filter would become a loss nobody is told about — the quietest wrong report.

    Asserted against the ranker's OWN published tuple rather than against a list retyped here,
    so a filter added to ``ranking/reasons.py`` turns this red instead of silently defaulting.
    """
    from contracts import LossReasons  # noqa: PLC0415
    from exchange.ranking.reasons import EXCLUSION_REASON_PREFIXES  # noqa: PLC0415
    from exchange.reports.log import COMPONENT_CATEGORIES, EXCLUSION_CATEGORIES  # noqa: PLC0415

    categories = set(LossReasons.model_fields)
    missing = set(EXCLUSION_REASON_PREFIXES) - set(EXCLUSION_CATEGORIES)
    assert not missing, f"exclusion reasons with no loss category: {sorted(missing)}"
    assert set(EXCLUSION_CATEGORIES.values()) <= categories, EXCLUSION_CATEGORIES
    assert set(COMPONENT_CATEGORIES.values()) <= categories, COMPONENT_CATEGORIES
    # The other direction: a category no filter can produce would be a column that is always
    # zero, which reads to a merchant as "you never lose on this".
    assert set(EXCLUSION_CATEGORIES.values()) == categories, sorted(
        categories - set(EXCLUSION_CATEGORIES.values())
    )


# =====================================================================================
# 4. R9's privacy rule, asserted by inversion rather than by a list of field names
# =====================================================================================
def _scalars(node: Any) -> Iterator[Any]:
    if isinstance(node, Mapping):
        for value in node.values():
            yield from _scalars(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _scalars(value)
    else:
        yield node


def test_the_served_report_carries_no_rival_amount(served: tuple[TestClient, Any]) -> None:
    """No amount from the auction reaches the report — not the winner's, not the store's own.

    Every price in this fixture is distinctive, so a leak is findable by search rather than by
    knowing which field it would have ridden in. The check is over the raw response TEXT as
    well as over its parsed scalars: a number smuggled inside a criterion string would not be
    a number in the parsed body.
    """
    client, _ = served
    before = time.time()
    _open_one_auction(client)

    for store in (LOSER_ON_TRUST, LOSER_ON_FIT, ALSO_RAN[-1]):
        response = _report(client, store, start=before, end=time.time())
        assert response.status_code == 200, response.text
        text = response.text
        parsed = response.json()
        numbers = {
            value
            for value in _scalars(parsed)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        for amount in (*PRICES.values(), *LIST_PRICES.values()):
            assert amount not in numbers, f"{store}: the report carries {amount}: {parsed}"
            assert str(amount) not in text, f"{store}: {amount} rode through as text: {text}"

    # And no rival's identity either: a category is what a merchant is told, never who beat it.
    losers = _report(client, LOSER_ON_TRUST, start=before, end=time.time())
    for rival in set(LIST_PRICES) - {LOSER_ON_TRUST}:
        assert rival not in losers.text, f"the report names the rival {rival}: {losers.text}"


def test_the_reason_categories_are_exactly_r9s_four(served: tuple[TestClient, Any]) -> None:
    """fit / price / commitments / trust, taken from the contract rather than restated here."""
    from contracts import LossReasons  # noqa: PLC0415

    client, _ = served
    before = time.time()
    _open_one_auction(client)

    report = _report(client, LOSER_ON_TRUST, start=before, end=time.time()).json()
    (entry,) = report["by_cluster"]
    assert set(entry["reasons"]) == set(LossReasons.model_fields), entry
    assert set(entry["reasons"]) == {"fit", "price", "commitments", "trust"}, entry


def test_the_served_body_is_the_published_loss_report_model(
    served: tuple[TestClient, Any],
) -> None:
    """The route answers the pinned contract object, so a field it invented would 500 here."""
    from contracts import LossReport  # noqa: PLC0415

    client, _ = served
    before = time.time()
    _open_one_auction(client)

    response = _report(client, LOSER_ON_TRUST, start=before, end=time.time())
    assert response.status_code == 200, response.text
    parsed = LossReport.model_validate(json.loads(response.text))
    assert parsed.store_id == LOSER_ON_TRUST
