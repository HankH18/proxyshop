"""THE STATED ROSTER HAS TO NAME A VARIANT, or the rows it gets RIGHT are the ones that break.

What was false, in shipped source
---------------------------------
``exchange.retrieval.roster.repoint_organic_products`` explains why it invents no variant for a
row it leaves alone, and closes the argument by naming who does state one::

    A stated roster that wants a working cart states its own ``variant_ref`` — ``RosterEntry``
    declares the field, and ``scripts/build_demo_deployment.py`` writes one per row into
    ``deploy/demo/buyer-roster.json``.

The first half was true and the second was not. Measured on the tracked document before this
file existed: fifteen rows, whose keys across all of them were exactly ``{list_price,
max_discount_pct, product_ref, store_id, tier}``, and **zero** carried ``variant_ref``. The
generator never set the key, though it had the value in hand — ``_priced_variant`` already
returns the cheapest variant's storefront id beside the price the roster row quotes, and
``_catalog_row`` already writes it into every hosted agent's own catalogue.

Why that is worse the better the roster is
------------------------------------------
``repoint_organic_products`` fills ``variant_ref`` only on rows it MOVES, and it moves a row
only when this exchange's own search did NOT return the product the row pinned. So on the demo
roster, before this: a row whose pinned product was *wrong* got a working cart link off the
graph, and a row whose pinned product was *right* got none at all — the shopper accepted a slot
and ``checkout.provider.default_permalink`` had no variant to build the D25 permalink from.
``repoint_organic_products`` records the size of that held set as it measured it, on the
six-row roster of the time: *"measured on 'milk thistle liver support', four of the demo's six
rows are returned by the search and do not move."* The roster is fifteen rows now and that
count has not been re-measured, so the number below is not restated as current — what is
asserted here is the shape, which does not depend on it: whichever rows are held, they are the
rows the platform judged RIGHT, and they were the rows with no cart.

That asymmetry is not a bug in ``repoint_organic_products``: an unmoved row keeps the CALLER's
``list_price``, so pairing it with a variant the PLATFORM chose would state one observation's
price beside another observation's variant — the exact disagreement that function refuses one
paragraph earlier, and the one ``_priced_variant`` refuses in the generator. The row is the
caller's statement and the fix belongs where the statement is written.

What is graded here
-------------------
1. the tracked ``deploy/demo/buyer-roster.json`` names, for every row, a variant the RECORDED
   CORPUS says belongs to that row's own ``product_ref`` and is priced at that row's own
   ``list_price`` — re-derived from ``fixtures/real-catalogs-demo`` rather than from the
   generator, so this cannot pass by agreeing with the code that wrote it.
2. a row the platform declines to move still reaches ``auction.collect``'s fallback offer with
   that variant on it, which is the only path a cart permalink is ever built from for a store
   that does not bid.
3. the two halves of the moving rule that ``repoint_organic_products`` states and nothing
   asserted: a moved row takes the platform's variant, and a moved row whose new product the
   platform observed no id for is CLEARED rather than left naming the old product's variant.

WHICH IDENTITY. The storefront's own id (``43866134282275``), never the graph's ``var_<hash>``
key and never the SKU. ``ingest.adapters.base.VariantRecord`` says why in the source: over this
same corpus ``seller_sku`` is all-digits 5,258 times and equals the native id 0 times, so a
SKU-built permalink passes the storefront's ``isdigit()`` gate and names the WRONG product
silently. Membership in the recorded entry's own variant ids is what test 1 checks, and it is
the check that rules out all three wrong spellings at once.
"""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from typing import Any

import pytest
from exchange.auction.collect import _list_price_bid
from exchange.retrieval.fit import FitAssessment, FitFeatures
from exchange.retrieval.roster import ShopRoster, SolicitedShop, repoint_organic_products
from ingest.adapters.mapping import coerce_price, product_id_for

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The tracked document the buyer service binds as ``BUYER_ROSTER``. Read off disk rather than
#: rebuilt, because a hand-edited or stale document is exactly what this grades.
ROSTER_DOCUMENT = REPO_ROOT / "deploy" / "demo" / "buyer-roster.json"

#: The recorded corpus the generator derives that document from.
CORPUS_STORES = REPO_ROOT / "fixtures" / "real-catalogs-demo" / "stores"

#: The sentence the demo is tuned for. The test below does not depend on how many rows the real
#: graph holds on it — it vouches every pinned ref outright, so ALL fifteen take the held path.
DEMO_QUERY = "milk thistle silymarin liver support extract"


def _shipped_roster() -> list[dict[str, Any]]:
    document = json.loads(ROSTER_DOCUMENT.read_text(encoding="utf-8"))
    rows = document["roster"]
    assert isinstance(rows, list) and rows, f"{ROSTER_DOCUMENT} names no rows"
    return [dict(row) for row in rows]


def _recorded_products(host: str) -> dict[str, dict[str, Any]]:
    """``{product_ref: recorded entry}`` for one storefront, straight off the corpus."""
    path = CORPUS_STORES / f"{host}.products.jsonl.gz"
    assert path.is_file(), f"the corpus records no products for {host} at {path}"
    entries: dict[str, dict[str, Any]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            try:
                entries[product_id_for(host, entry)] = entry
            except ValueError:
                continue  # an entry naming no id/handle: the loader skips it too
    return entries


def _priced_variants(entry: dict[str, Any]) -> dict[str, float]:
    """``{the storefront's own variant id: its price}`` for the variants it published a price on."""
    priced: dict[str, float] = {}
    for variant in entry.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        price = coerce_price(variant.get("price"))
        raw = variant.get("id")
        if price is None or price <= 0 or isinstance(raw, bool) or not isinstance(raw, (str, int)):
            continue
        native = str(raw).strip()
        if native:
            priced.setdefault(native, float(price))
    return priced


def test_every_shipped_roster_row_names_the_variant_its_own_price_prices() -> None:
    """The claim ``repoint_organic_products`` makes about this file, checked against the corpus.

    Not "carries a plausible string": the value has to be a variant the RECORDED corpus says
    belongs to this row's ``product_ref``, priced at this row's ``list_price``. That single
    membership check is what rules out the graph's ``var_<hash>`` key, the SKU, and another
    product's variant — the three spellings that are worse than the absent field, because the
    absent field declines at accept time while a wrong one builds a cart for the wrong thing.
    """
    rows = _shipped_roster()

    stateless = [
        str(row["store_id"]) for row in rows if not str(row.get("variant_ref") or "").strip()
    ]
    assert not stateless, (
        f"{len(stateless)} of {len(rows)} roster rows name no variant_ref ({stateless}). "
        f"An unmoved row is the one repoint_organic_products never fills in, so these stores "
        f"reach accept with no variant and default_permalink can build no cart at all."
    )

    for row in rows:
        host, ref = str(row["store_id"]), str(row["product_ref"])
        recorded = _recorded_products(host)
        entry = recorded.get(ref)
        assert entry is not None, (
            f"{host}'s roster row points at {ref}, which the corpus has no record of"
        )
        priced = _priced_variants(entry)
        variant = str(row["variant_ref"])
        assert variant in priced, (
            f"{host}'s roster row names variant {variant!r}, which is not a priced variant of "
            f"its own product {ref} ({entry.get('title')!r}). The storefront's OWN id is what "
            f"https://{host}/cart/{{variant}}:{{qty}} needs — not the graph's var_<hash> key "
            f"and not the SKU."
        )
        assert priced[variant] == pytest.approx(float(row["list_price"])), (
            f"{host}'s roster row quotes {row['list_price']} and names variant {variant}, which "
            f"the corpus prices at {priced[variant]}. The price and the variant are one "
            f"observation or the cart totals differently from the offer that was accepted."
        )


class _Source:
    """A roster source that has opted in and answers one prepared solicitation."""

    name = "double"
    repoints_stated_rosters = True

    def __init__(self, answer: ShopRoster) -> None:
        self.answer = answer
        self.calls = 0

    def solicit(self, intent: Any, *, limit: int | None = None) -> ShopRoster:
        self.calls += 1
        return self.answer


def _vouching(rows: list[dict[str, Any]]) -> ShopRoster:
    """A solicitation that VOUCHES every pinned product while rostering something else.

    Both halves matter. Vouching the pinned refs is what holds each row still — the rule
    ``repoint_organic_products`` states first. Rostering each shop on a different, priced,
    variant-bearing product is the sabotage: if the guard were absent, every row would move and
    take the platform's variant, and this test would pass for the wrong reason.
    """
    return ShopRoster(
        shops=tuple(
            SolicitedShop(
                store_id=str(row["store_id"]),
                tier=1,
                product_ref=f"prod-elsewhere-{index}",
                intent_match=0.9,
                list_price=9.99,
                currency="USD",
                variant_ref=f"9999{index:04d}",
            )
            for index, row in enumerate(rows)
        ),
        source="neo4j",
        considered=25,
        fit=tuple(
            FitAssessment(
                product_id=str(row["product_ref"]),
                canonical_name=str(row["product_ref"]),
                fit_score=0.7,
                features=FitFeatures(similarity=0.7, preference_alignment=0.7),
                reranker="identity",
            )
            for row in rows
        ),
    )


def _intent(query: str) -> dict[str, Any]:
    return {
        "intent_id": "intent-roster-variant",
        "cluster_id": "cluster-liver-support",
        "query": query,
        "hard_constraints": [],
        "preferences": [],
        "currency": "USD",
        "budget_band": "unspecified",
        "created_at": "2026-01-01T00:00:00Z",
        "schema_version": "1.0.0",
    }


def test_a_row_the_platform_leaves_alone_still_reaches_the_offer_with_a_cart_link() -> None:
    """THE ASYMMETRY, driven: the rows the roster gets RIGHT must not be the ones with no cart.

    Every pinned product is vouched, so nothing moves and nothing is filled in — which is the
    whole point. What the shopper can be sold is then decided entirely by what the document
    stated, and it is followed all the way to the fallback offer ``collect_bids`` mints for a
    store that does not bid, because that offer is the only thing a permalink can be built from
    for the eleven rostered stores that have no agent at all.
    """
    stated = _shipped_roster()
    source = _Source(_vouching(stated))

    rows, reason = repoint_organic_products(source, stated, _intent(DEMO_QUERY))

    assert source.calls == 1
    assert reason is None, f"every pinned product was vouched, so nothing may move: {reason}"
    assert [row["product_ref"] for row in rows] == [row["product_ref"] for row in stated]

    deadline = time.time() + 30.0
    for row in rows:
        bid = _list_price_bid(row, "auction-1", deadline)
        offer = bid["offer"]
        assert offer["variant_ref"] == str(row["variant_ref"]), (
            f"{row['store_id']}'s unmoved row reached the fallback offer with no variant; "
            f"https://{row['store_id']}/cart/{{variant}}:{{qty}} cannot be built from it"
        )
        assert offer["unit_price"] == pytest.approx(float(row["list_price"]))


def test_a_moved_row_takes_the_platforms_variant_and_a_variantless_one_is_cleared() -> None:
    """The other half of the same rule, which nothing asserted.

    A re-pointed row that kept the caller's variant would name a variant of the product it moved
    OFF — a cart for something the shopper was never shown, which succeeds and is wrong, where
    the absent field declines and says so.
    """
    stated = [
        {
            "store_id": "s-known",
            "tier": 1,
            "product_ref": "prod-old",
            "list_price": 25.49,
            "variant_ref": "111",
        },
        {
            "store_id": "s-blind",
            "tier": 1,
            "product_ref": "prod-old",
            "list_price": 25.49,
            "variant_ref": "222",
        },
    ]
    roster = ShopRoster(
        shops=(
            SolicitedShop(
                store_id="s-known",
                tier=1,
                product_ref="prod-new",
                intent_match=0.8,
                list_price=19.99,
                currency="USD",
                variant_ref="333",
            ),
            SolicitedShop(
                store_id="s-blind",
                tier=1,
                product_ref="prod-new",
                intent_match=0.8,
                list_price=19.99,
                currency="USD",
                variant_ref=None,
            ),
        ),
        source="neo4j",
        considered=25,
    )

    rows, reason = repoint_organic_products(
        _Source(roster), stated, _intent("creatine monohydrate")
    )

    assert reason is not None and "2 of 2" in reason, reason
    assert rows[0]["product_ref"] == "prod-new" and rows[0]["variant_ref"] == "333", rows[0]
    assert rows[1]["product_ref"] == "prod-new", rows[1]
    assert "variant_ref" not in rows[1], (
        "the platform observed no variant for the new product, so the row must carry none — "
        "keeping '222' would name a variant of the product the row moved off"
    )
