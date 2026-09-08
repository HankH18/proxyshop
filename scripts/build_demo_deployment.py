#!/usr/bin/env python
"""Generate the compose demo's deployment documents from the recorded real catalogues.

WHY THIS EXISTS. ``docker compose up`` brought up eleven services that could not serve a
shopper, and the reason was configuration rather than code: ``EXCHANGE_DEPLOYMENT`` and
``BUYER_DEPLOYMENT`` both defaulted to the empty string, so ``exchange.composition.
read_deployment`` returned ``None``, every fail-closed default stood, and ``POST /auctions``
answered ``ranked: []`` while ``docker compose ps`` reported every row ``(healthy)``. No
example deployment document was shipped anywhere in the repository -- the only ones that
existed were built in Python at run time by ``apps/buyer/devstack/run.py`` and
``proxyshop_demo/s1.py`` and thrown away with their temp directories.

WHAT IT WRITES, into ``deploy/demo/``:

* ``exchange-deployment.json``      -- ``EXCHANGE_DEPLOYMENT``
* ``buyer-deployment.json``         -- ``BUYER_DEPLOYMENT``
* ``buyer-roster.json``             -- ``BUYER_ROSTER`` (separate because the buyer document
                                       is capped at 64 KiB and the roster at 512 KiB)
* ``store-contexts/<host>.json``    -- ``STORE_AGENT_CONTEXT``, one per hosted store agent

The outputs are TRACKED IN THE TREE, so a clone runs the demo without running this script.
It exists so the next reader can see where every number came from and regenerate after the
corpus moves, not as a step in the runbook.

WHERE THE DATA COMES FROM. ``fixtures/real-catalogs/`` -- ten real supplement storefronts,
3,093 products, collected by ``scripts/collect_real_catalogs.py`` from public
``products.json`` endpoints under robots.txt. Nothing here invents a store, a product or a
price; the only invented numbers are the ones the platform has to state because no
storefront publishes them -- the approved envelope's discount depth, the trust score, and
the intent-cluster vocabulary.

THE PRODUCT IDS MUST MATCH THE GRAPH. ``ingest.scheduler.load_corpus`` writes each product
as ``prod_<stable_id(store_id, native_key)>`` through ``ingest.adapters.mapping.
product_id_for``. The exchange's graph roster returns those ids as ``product_ref``, the
store agent looks that ``product_ref`` up in its own catalogue, and the exchange grades the
resulting claims against the ``catalog`` snapshot keyed the same way. Three files, one id
space -- so this script imports the repository's own id function rather than reproducing it.

Usage::

    ./.venv/bin/python scripts/build_demo_deployment.py
    ./.venv/bin/python scripts/build_demo_deployment.py --check   # regenerate + diff, no write
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / ".pkgroot"))

from ingest.adapters.mapping import (  # noqa: E402 - after the sys.path bootstrap
    coerce_price,
    product_id_for,
)

# The six trust dimensions, imported rather than restated: D53 says EXACTLY six and the schema
# refuses a seventh, so a second copy here would be a vocabulary that could drift out of the one
# the trust service and the contracts share.
from trust.scoring.dimensions import TRUST_DIMENSIONS  # noqa: E402 - after the bootstrap

#: The recorded corpus this demo is built from.
CORPUS = REPO_ROOT / "fixtures" / "real-catalogs"

#: Where the generated documents land. Tracked, mounted read-only into the containers.
OUT = REPO_ROOT / "deploy" / "demo"

#: The one named catalogue cluster this demo addresses intents to.
#:
#: It is configuration rather than data because no service in this repository publishes the
#: cluster vocabulary -- ``exchange.retrieval.clusters`` says so at length. The agents'
#: ``pursue_clusters`` below name this same id, which is what authorises them to bid: an
#: envelope listing no cluster pursues nothing, and that is fail-closed by design.
CLUSTER_ID = "cluster-liver-support"

#: The shopper sentence this demo is tuned for, and the terms the cluster matches on.
DEMO_QUERY = "milk thistle silymarin liver support extract"
CLUSTER_TERMS = (
    "milk thistle",
    "silymarin",
    "liver support",
    "liver",
    "detox",
    "dandelion root",
    "artichoke extract",
)

#: The four storefronts that get a hosted store agent in compose. They are the four the
#: corpus README names as stocking the liver-support query; the other six stay in the
#: seller registry and the graph, are found by retrieval, and are represented by R10's
#: list-price fallback because no agent answers for them. A demo in which every rostered
#: store bids would not show that half of the market at all.
HOSTED = (
    "gaiaherbs.com",
    "toniiq.com",
    "paradiseherbs.com",
    "oregonswildharvest.com",
)

#: Two of the ten stock no liver-support inventory at all (the corpus README calls them
#: negative controls). They are eligible and rostered like everyone else -- retrieval is
#: what must leave them out, not the registry.
NEGATIVE_CONTROLS = ("livemomentous.com", "nakednutrition.com")

#: Stated trust scores. The platform's reading of a store, not the store's own claim, so it
#: cannot come from the catalogue and a person states it -- exactly as `.env.example` and
#: `exchange.composition` say the trust snapshot must be. Nobody is blacklisted here: the
#: blacklist beat is `fixtures/manifest.json`'s scripted dishonest store, and marking a real
#: company blacklisted in a shipped fixture would be a claim this project cannot support.
TRUST_SCORES = {
    "gaiaherbs.com": 0.86,
    "toniiq.com": 0.74,
    "paradiseherbs.com": 0.81,
    "oregonswildharvest.com": 0.78,
    "bulksupplements.com": 0.69,
    "nutricost.com": 0.72,
    "purebulk.com": 0.66,
    "doublewoodsupplements.com": 0.71,
    "livemomentous.com": 0.75,
    "nakednutrition.com": 0.70,
}

#: Each store's `shipped_on_time` posterior -- what its dispatch record says, as distinct from
#: what it is worth overall. Deployment configuration like `TRUST_SCORES` above, and invented for
#: the same reason: no storefront publishes its own fulfilment history.
#:
#: **These deliberately do NOT track `TRUST_SCORES`, and that is the point of the whole table.**
#: D57 rewired `delivery_fit` to read this dimension because a store used to buy `w_d = 0.10` of
#: the published score by typing a smaller number into `delivery_estimate_days`. If every store's
#: dispatch record simply equalled its overall score, `delivery_fit` would be a carbon copy of
#: `trust` -- the double-count D57 exists to avoid -- and the demo would demonstrate nothing.
#:
#: So the four hosted stores each carry a different story, and `toniiq.com` is the one to watch:
#: 0.74 overall but 0.45 on dispatch, a store that promises fast and does not deliver. Its quote
#: is divided by that posterior (`features.credible_delivery_estimate`), so a one-day promise is
#: read as 2.2 days. `gaiaherbs.com` at 0.93 is quoted near face value. The six stores nobody
#: bids for sit at parity with their overall score, because a store with no story does not need
#: an invented one.
DISPATCH_POSTERIOR = {
    "gaiaherbs.com": 0.93,
    "toniiq.com": 0.45,
    "paradiseherbs.com": 0.88,
    "oregonswildharvest.com": 0.62,
}

#: Evidence mass behind each Beta, as `alpha + beta`.
#:
#: 14.0 clears the exchange's admissibility floor with room to spare: `features` refuses to admit
#: a promise until mass exceeds `TRUST_PRIOR_MASS` (4.0) by `MIN_DISPATCH_OBSERVATIONS` (5.0), so
#: 9.0 is the line and a document at 9.5 would be one rounding away from every store reading the
#: neutral 0.5 and the demo silently proving nothing.
DIMENSION_MASS = 14.0

#: The instant these Betas were last decayed at, stated rather than stamped at generation.
#:
#: A wall-clock value would make `--check` report drift on every run of a generator whose inputs
#: had not changed. It is safe to fix because the exchange does NOT re-decay what it reads: a
#: stated deployment document's alpha/beta reach `dispatch_credibility` as written. Decay is the
#: trust engine's job, and a deployment that wants live decay binds the live reader instead of
#: stating a snapshot.
DIMENSION_DECAYED_AT = "2026-09-01T00:00:00+00:00"

#: The approved envelope's discount depth per hosted store. Deployment configuration: the
#: merchant's authorisation, which no storefront publishes.
MAX_DISCOUNT_PCT = {
    "gaiaherbs.com": 15.0,
    "toniiq.com": 20.0,
    "paradiseherbs.com": 12.0,
    "oregonswildharvest.com": 18.0,
}

#: How many of a hosted store's products reach the exchange's `catalog` snapshot. The
#: document is capped at 4 MiB across every store and a snapshot at 1000 products
#: (``MAX_CATALOG_PRODUCTS``); the agents' own catalogues below are NOT trimmed, because an
#: agent that cannot find the product the graph rostered declines the auction.
SNAPSHOT_PRODUCTS_PER_STORE = 60

#: A fixed observation stamp. The corpus is a point-in-time snapshot and every document
#: built from it must be byte-identical on every machine, so nothing here reads a clock.
OBSERVED_AT = "2026-01-01T00:00:00Z"


def _read_products(host: str) -> list[dict[str, Any]]:
    """Every recorded product for ``host``, in the order the corpus recorded them."""
    path = CORPUS / "stores" / f"{host}.products.jsonl.gz"
    if not path.is_file():
        raise SystemExit(f"FATAL: no recorded products at {path}")
    out: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _priced_variant(entry: dict[str, Any]) -> tuple[str | None, float] | None:
    """``(variant_id, price)`` for the cheapest readable variant, which is what the Offer carries.

    ``None`` when the storefront published no usable price -- and such a product is dropped
    rather than priced at zero. Zero is the cheapest number there is, so a product priced at
    nothing wins every ranking it enters; the exchange refuses a roster row spelled that way
    (``RosterEntry.list_price`` is ``Field(gt=0.0)``) and so does this.

    The id and the price come from the SAME variant, and that pairing is the whole point of
    returning them together. A cart permalink is variant-scoped (D25), so a permalink built on
    one variant while the bid quotes another's price sends the shopper to a cart whose total
    disagrees with the offer they accepted -- a worse failure than the missing id it replaced,
    because it is wrong rather than merely inert.

    Ties keep the first variant in document order: ``min`` is stable, and a storefront that
    prices two variants identically must still produce the same document on every run.

    ``variant_id`` is ``None`` when the storefront published a price but no usable id. That is
    a real Shopify shape, and it degrades to the pre-existing behaviour (a product-scoped
    permalink) rather than dropping a priced product from the catalogue.
    """
    best_price: float | None = None
    best_id: str | None = None
    for variant in entry.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        price = coerce_price(variant.get("price"))
        if price is None or price <= 0:
            continue
        if best_price is not None and price >= best_price:
            continue  # `>=` keeps the FIRST of equally-priced variants, so ties are stable
        raw = variant.get("id")
        # `id` is an int in every Shopify `products.json` this corpus holds. Anything that is
        # not a bare scalar -- a dict, a list -- names no variant a URL can carry, and
        # `str()` of it would build a permalink pointing at nothing.
        scalar = isinstance(raw, (str, int)) and not isinstance(raw, bool)
        best_price = price
        best_id = (str(raw).strip() or None) if scalar else None
    return None if best_price is None else (best_id, best_price)


def _relevance(entry: dict[str, Any]) -> int:
    """How many of the cluster's terms this product's own words carry.

    A crude lexical count on purpose: it decides which product a store LEADS with in the
    shipped roster, and nothing else. The exchange does its own retrieval over the graph
    and is not bound by this ordering.
    """
    blob = " ".join(
        str(entry.get(key) or "") for key in ("title", "handle", "product_type", "tags", "vendor")
    ).lower()
    return sum(1 for term in CLUSTER_TERMS if term in blob)


def _catalog_row(
    host: str, entry: dict[str, Any], price: float, variant_id: str | None
) -> tuple[str, dict[str, Any]]:
    """One ``{product_ref: row}`` pair for a store agent's own catalogue.

    ``variant_ref`` carries the storefront's OWN variant id, and it is the field that decides
    whether accepting a slot hands the shopper a checkout that exists. The chain is short and
    entirely downstream of this dict: ``store-agent``'s ``bidding._variant_ref`` reads it out
    of this row (``VARIANT_REF_KEYS = ("variant_ref", "variant_id")``), puts it on the bid's
    Offer, and ``exchange.checkout.provider.default_permalink`` spends it on
    ``build_cart_permalink(variant_id=offer.get("variant_ref") or ... or 1)``.

    Omitting it is what produced ``https://<store>/cart/1:1?discount=...`` -- the literal
    fallback ``1``, a variant id no storefront in this corpus issues, on a URL that resolves
    to an empty cart. The corpus always had the real id; this generator dropped it, so the
    store agent had nothing to put on the offer and the exchange fell back.

    ``handle`` rides along because it is the only field that names the product's own page
    (``https://<host>/products/<handle>``). Nothing consumes it today; it is recorded rather
    than re-derived later, since the raw entry it comes from is not shipped.
    """
    product_ref = product_id_for(host, entry)
    row = {
        "product_ref": product_ref,
        "list_price": price,
        "title": str(entry.get("title") or "").strip(),
        "brand": str(entry.get("vendor") or "").strip(),
        "product_type": str(entry.get("product_type") or "").strip(),
        "currency": "USD",
        "handle": str(entry.get("handle") or "").strip(),
    }
    # Absent rather than null when the storefront named none: `_variant_ref` skips a `None`
    # anyway, and a key that is sometimes null invites a reader to treat it as always present.
    if variant_id:
        row["variant_ref"] = variant_id
    return product_ref, row


def _store_catalog(host: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """``({product_ref: row}, [product_ref most relevant first])`` for one storefront."""
    catalog: dict[str, dict[str, Any]] = {}
    scored: list[tuple[int, float, str]] = []
    for entry in _read_products(host):
        priced = _priced_variant(entry)
        if priced is None:
            continue
        variant_id, price = priced
        try:
            product_ref, row = _catalog_row(host, entry, price, variant_id)
        except ValueError:
            continue  # an entry naming no id/handle: the loader skips it too
        catalog[product_ref] = row
        scored.append((-_relevance(entry), price, product_ref))
    scored.sort()
    return catalog, [product_ref for _, _, product_ref in scored]


def _store_context(host: str, catalog: dict[str, Any], ranked: list[str]) -> dict[str, Any]:
    """The document one hosted agent reads out of ``STORE_AGENT_CONTEXT``.

    The whole catalogue is carried, not the lead product alone. The exchange picks which
    product it rosters by walking the graph, and an agent whose catalogue does not hold that
    ``product_ref`` declines ``no_matching_product`` -- which would read as "this store does
    not stock it" when the truth is "this file was trimmed".
    """
    depth = MAX_DISCOUNT_PCT[host]
    lead = ranked[0]
    return {
        "store_id": host,
        "store_domain": host,
        "envelope": {
            "store_id": host,
            "version": "demo-1",
            # A store-wide floor (``product_ref: null``) plus one explicit floor on the
            # product this store leads with. The floor is the merchant's own limit and is
            # what stops the discount depth below from being the whole story.
            "floors": [
                {"product_ref": None, "min_price": 1.0},
                {"product_ref": lead, "min_price": round(catalog[lead]["list_price"] * 0.7, 2)},
            ],
            "max_discount_pct": depth,
            "budget_cap": 5000.0,
            "pursue_clusters": [CLUSTER_ID],
            "standing_commitments": [
                {
                    "key": "free_returns",
                    "value": "30 return window",
                    "provenance": {
                        "source": "owner_statement",
                        "ref": f"envelope:{host}:demo-1#free_returns",
                        "observed_at": OBSERVED_AT,
                        "authority_rank": 1,
                    },
                }
            ],
            "activation": "active",
        },
        "catalog": catalog,
        "live_state": {ref: {"in_stock": True, "units_left": 12} for ref in catalog},
        "learned_policy": None,
        "network_priors": {CLUSTER_ID: {"depth_buckets": [0.0, 0.05, 0.1, 0.15, 0.2]}},
    }


def _snapshot(host: str, catalog: dict[str, Any], ranked: list[str]) -> dict[str, Any]:
    """The catalogue snapshot the exchange grades this store's CLAIMS against.

    The exchange's own evidence about a store, which is why it is in the exchange's document
    and not in the agent's: a store that supplied both would be marking its own homework.
    """
    products = []
    for product_ref in ranked[:SNAPSHOT_PRODUCTS_PER_STORE]:
        row = catalog[product_ref]
        products.append(
            {
                "product_ref": product_ref,
                "canonical_name": row["title"] or product_ref,
                "evidence_ref": f"snap-{host}#{product_ref}",
                "observed_at": OBSERVED_AT,
                "attributes": {
                    "list_price": {"value": row["list_price"], "unit": "USD"},
                    "brand": {"value": row["brand"]},
                    "product_type": {"value": row["product_type"]},
                    "availability": {"value": "in_stock"},
                    "in_stock": {"value": True},
                    "free_returns": {"value": "30 return window"},
                },
                "offer": {
                    "unit_price": row["list_price"],
                    "currency": "USD",
                    "availability": "in_stock",
                },
            }
        )
    return {
        "snapshot_id": f"snap-{host}",
        "captured_at": OBSERVED_AT,
        "store_id": host,
        "products": products,
    }


def _dims(host: str) -> dict[str, dict[str, Any]]:
    """The six Betas for one store, whose means average to its stated `score`.

    The trust engine defines a store's `score` as the mean of its six dimension means (D53), so
    emitting dims that averaged to something else would publish a document contradicting itself.
    `shipped_on_time` is stated first, from :data:`DISPATCH_POSTERIOR`; the other five carry
    whatever mean makes the six average back to :data:`TRUST_SCORES` -- `m = (6*score - d)/5`.

    That arithmetic is why the dispatch numbers in `DISPATCH_POSTERIOR` are chosen close enough
    to the score to keep `m` inside [0, 1]: a store scored 0.74 cannot also have shipped on time
    0.05, because no set of five means fixes that average. The function refuses rather than
    clamping, since a clamp would publish a `score` the dims do not support and the mismatch
    would surface as an unexplained ranking rather than as a build failure.
    """
    score = TRUST_SCORES.get(host, 0.6)
    dispatch = DISPATCH_POSTERIOR.get(host, score)
    others = (6.0 * score - dispatch) / 5.0
    if not 0.0 <= others <= 1.0:
        raise SystemExit(
            f"FATAL: {host} states score={score} and shipped_on_time={dispatch}, which needs the "
            f"other five dimensions to average {others:.4f} -- outside [0, 1]. Move the dispatch "
            f"posterior closer to the score, or move the score."
        )

    def beta(mean: float) -> dict[str, Any]:
        return {
            "alpha": round(mean * DIMENSION_MASS, 6),
            "beta": round((1.0 - mean) * DIMENSION_MASS, 6),
            "decayed_at": DIMENSION_DECAYED_AT,
        }

    return {
        dimension: beta(dispatch if dimension == "shipped_on_time" else others)
        for dimension in TRUST_DIMENSIONS
    }


def agent_service(host: str) -> str:
    """The compose service name for ``host``'s store agent.

    ``gaiaherbs.com`` -> ``store-agent-gaiaherbs``. The exchange reaches it by that name on
    the compose network, so this function and the compose fragment have to agree; the
    runbook gate reads the fragment and this script writes the endpoint.
    """
    return "store-agent-" + host.split(".")[0].replace("_", "-")


def build() -> dict[str, Any]:
    """Every document, as ``{relative path: JSON object}``."""
    manifest = json.loads((CORPUS / "collection.json").read_text(encoding="utf-8"))
    hosts = [str(store["host"]) for store in manifest["stores"]]
    missing = [host for host in HOSTED if host not in hosts]
    if missing:
        raise SystemExit(f"FATAL: the corpus does not carry the hosted stores {missing}")

    catalogs: dict[str, dict[str, Any]] = {}
    ranked: dict[str, list[str]] = {}
    for host in hosts:
        catalogs[host], ranked[host] = _store_catalog(host)
        if not ranked[host]:
            raise SystemExit(f"FATAL: {host} recorded no priced product; nothing to auction")

    sellers = []
    trust_snapshot = {}
    for host in hosts:
        row: dict[str, Any] = {
            "store_id": host,
            "eligibility": "eligible",
            "registered_domain": host,
        }
        if host in HOSTED:
            row["bid_endpoint"] = f"http://{agent_service(host)}:8086/v1/bid-requests"
        sellers.append(row)
        trust_snapshot[host] = {
            "store_id": host,
            "blacklisted": False,
            "score": TRUST_SCORES.get(host, 0.6),
            "dims": _dims(host),
        }

    exchange_doc = {
        "sellers": sellers,
        "trust_snapshot": trust_snapshot,
        "intent_clusters": [
            {
                "cluster_id": CLUSTER_ID,
                "label": "Liver support supplements",
                "category": "supplements",
                "terms": list(CLUSTER_TERMS),
            }
        ],
        "catalog": {host: _snapshot(host, catalogs[host], ranked[host]) for host in HOSTED},
        "checkout_mode": "redirect",
        "trust_url": "http://trust:8084",
    }

    # The buyer's roster: the four hosted stores plus two real storefronts with no agent, so
    # a run shows both halves of R10 -- a store that bids and a store that is represented at
    # its catalogue list price because nobody answered for it.
    roster_hosts = list(HOSTED) + ["bulksupplements.com", "nutricost.com"]
    roster = []
    for host in roster_hosts:
        lead = ranked[host][0]
        entry = {
            "store_id": host,
            "tier": 1,
            "product_ref": lead,
            "list_price": catalogs[host][lead]["list_price"],
        }
        if host in MAX_DISCOUNT_PCT:
            entry["max_discount_pct"] = MAX_DISCOUNT_PCT[host]
        roster.append(entry)

    buyer_doc = {
        "exchange_url": "http://exchange:8083",
        "request_timeout_seconds": 30.0,
        "registered_domains": {host: host for host in hosts},
    }

    documents: dict[str, Any] = {
        "exchange-deployment.json": exchange_doc,
        "buyer-deployment.json": buyer_doc,
        "buyer-roster.json": {"roster": roster},
    }
    for host in HOSTED:
        documents[f"store-contexts/{host}.json"] = _store_context(
            host, catalogs[host], ranked[host]
        )
    return documents


def render(document: Any) -> str:
    return json.dumps(document, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate in memory and report drift instead of writing",
    )
    args = parser.parse_args()

    documents = build()
    drifted: list[str] = []
    for rel, document in documents.items():
        path = OUT / rel
        rendered = render(document)
        if args.check:
            current = path.read_text(encoding="utf-8") if path.is_file() else ""
            if current != rendered:
                drifted.append(rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)} ({len(rendered):,} bytes)")

    if args.check:
        if drifted:
            print("these documents differ from what the corpus implies:", file=sys.stderr)
            for rel in drifted:
                print(f"  deploy/demo/{rel}", file=sys.stderr)
            return 1
        print("deploy/demo is in sync with the recorded corpus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
