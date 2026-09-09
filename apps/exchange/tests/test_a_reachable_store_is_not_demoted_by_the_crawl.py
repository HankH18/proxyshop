"""A store this exchange can actually reach is asked, whatever tier the crawl gave it.

Run it on its own::

    PROXYSHOP_WORKER=11 .venv/bin/python -m pytest \\
        apps/exchange/tests/test_a_reachable_store_is_not_demoted_by_the_crawl.py -q

The regression this exists to stop
----------------------------------
The crawl used to write ``tier: 2`` for every store it read — the most privileged tier under
D28 — and it wrote it on every refresh cycle, because ``upsert_store`` delegates to
``MERGE ... SET n += $props``. Correcting that means a crawled store carries no tier at all
and reads back as **0**, D28's catalogue-only.

Landing only that half is not a relabel, it is a measured demotion. Four of the nineteen demo
stores are BOTH crawled and hosted — ``gaiaherbs.com``, ``toniiq.com``, ``paradiseherbs.com``,
``oregonswildharvest.com`` — and they share a ``store_id`` with their crawled selves, so
``gaiaherbs.com`` is one graph node and not two. Measured over all nineteen with a solicitor
shaped like ``deploy/demo/exchange-deployment.json`` (4 of 19 sellers carry a ``bid_endpoint``)
and each hosted store bidding 92.00 against a 100.00 list price::

    every row tier 2 (before): solicited 4, sponsored 4, prices {92.0: 4, 100.0: 15}
    every row tier 0 (after,   solicited 0, sponsored 0, prices {100.0: 19}
      with no second half)     reasons {'tier_0_no_agent': 19}

Four stores lose a real bid and the shopper pays 100.00 instead of 92.00, on the exact path
``scripts/demo_check.sh`` exists to probe (``POST /auctions`` with no roster in the body,
which is the only door the graph tier reaches — the buyer's own served demo sends
``deploy/demo/buyer-roster.json``, whose fifteen rows all hardcode ``tier: 1``).

The second half, and why it belongs HERE
----------------------------------------
The tier gate asks "is there an agent to ask". The crawl cannot answer it; the exchange can,
out of ``sellers[].bid_endpoint`` in its own deployment document. ``stores_with_no_agent`` is
already exactly half of that overlay — the LOWERING half, which drops a store the solicitor
holds no address for. This is the other half, and it uses the same optional
``can_solicit(store_id)`` hook, so a solicitor that does not implement it changes nothing at
all.
"""

from __future__ import annotations

from typing import Any

from exchange.eligibility import ELIGIBLE, StaticSellerEligibility
from exchange.orchestration import solicit_bids

T_NOW = 1_700_000_000.0

HOSTED = "gaiaherbs.com"
CRAWLED_ONLY = "bulksupplements.com"


def _row(store_id: str, tier: int) -> dict[str, Any]:
    return {
        "store_id": store_id,
        "tier": tier,
        "product_ref": "product-1",
        "list_price": 100.0,
    }


class DemoShapedSolicitor:
    """Holds an endpoint for one store and not the other, and the one it holds BIDS.

    Shaped like ``composition.HttpBidSolicitor``: it answers ``can_solicit`` out of the
    endpoint registry the deployment document gave it, and opens a socket for nobody else.
    """

    def __init__(self, *, endpoints: tuple[str, ...]) -> None:
        self.endpoints = set(endpoints)
        self.asked: list[str] = []

    def can_solicit(self, store_id: Any) -> bool:
        return str(store_id) in self.endpoints

    def solicit(self, store: Any) -> Any:
        store_id = str(store["store_id"])
        self.asked.append(store_id)
        return {
            "store_id": store_id,
            "received_at": T_NOW - 1.0,
            "bid": {
                "store_id": store_id,
                "offer": {
                    "product_ref": "product-1",
                    "unit_price": 92.0,
                    "total_price": 92.0,
                    "currency": "USD",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "checkout_url": f"https://{store_id}/cart/44352913:1",
                },
                "claims": [],
            },
        }

    __call__ = solicit


def _run(roster: list[dict[str, Any]], solicitor: Any) -> Any:
    return solicit_bids(
        roster=roster,
        solicitor=solicitor,
        eligibility=StaticSellerEligibility({row["store_id"]: ELIGIBLE for row in roster}),
        now=T_NOW,
    )


def test_a_tier_zero_store_the_exchange_holds_an_endpoint_for_is_still_asked() -> None:
    """The crawl says catalogue-only; the endpoint registry says otherwise and it is right.

    This is the whole second half. A store whose only tier statement came from a crawl is
    tier 0, and tier 0 means "there is no agent to ask" — but this exchange is holding that
    store's bid endpoint, which is direct evidence that there IS one.
    """
    solicitor = DemoShapedSolicitor(endpoints=(HOSTED,))
    result = _run([_row(HOSTED, tier=0), _row(CRAWLED_ONLY, tier=0)], solicitor)

    assert solicitor.asked == [HOSTED], (
        f"a store this exchange holds an endpoint for was not dialled: asked={solicitor.asked}"
    )
    assert result.solicited == [HOSTED], result.solicited

    entries = {entry.store_id: entry for entry in result.entries}
    hosted = entries[HOSTED]
    assert hosted.fallback is False, (
        f"the hosted store's own bid was discarded as tier_0_no_agent: "
        f"{hosted.fallback_reason!r} at {hosted.unit_price}"
    )
    assert hosted.unit_price == 92.0, hosted.unit_price

    # The CONTROL, and it is what stops this being a rule that admits everybody: a store the
    # exchange holds no endpoint for is still catalogue-only, is still not dialled, and is
    # still reported by the reason that names the cause rather than blaming the store.
    quiet = entries[CRAWLED_ONLY]
    assert quiet.fallback is True, quiet
    assert quiet.unit_price == 100.0, quiet.unit_price
    assert quiet.fallback_reason is not None and "tier_0_no_agent" in quiet.fallback_reason, (
        quiet.fallback_reason
    )


def test_a_solicitor_that_cannot_answer_the_question_changes_nothing() -> None:
    """The hook is optional and absent means "ask on tier alone", exactly as before.

    ``stores_with_no_agent`` made the same promise for the lowering half and for the same
    reason: the closed answer here is "ask nobody", which would empty the market on any
    deployment whose solicitor predates the hook.
    """

    class NoCapabilityQuery:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def solicit(self, store: Any) -> None:
            self.asked.append(str(store["store_id"]))
            return None

        __call__ = solicit

    solicitor = NoCapabilityQuery()
    result = _run([_row(HOSTED, tier=0), _row(CRAWLED_ONLY, tier=1)], solicitor)

    assert solicitor.asked == [CRAWLED_ONLY], solicitor.asked
    assert result.solicited == [CRAWLED_ONLY], result.solicited
