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

WHERE THE DATA COMES FROM. ``fixtures/real-catalogs-demo/`` -- NINETEEN real storefronts
across five stocked categories, 4,903 products, derived by ``scripts/build_demo_corpus.py``
from ``fixtures/real-catalogs-broad/``, which ``scripts/collect_real_catalogs.py`` collected
from public ``products.json`` endpoints under robots.txt. It is NOT
``fixtures/real-catalogs/`` -- the ten supplement storefronts that corpus holds are why the
demo answered *"a walnut coffee table for the lounge"* with liver capsules, and that corpus is
still in the tree, still gated, and still the one ``ingest``'s own tests replay.

Nothing here invents a store, a product or a price; the only invented numbers are the ones the
platform has to state because no storefront publishes them -- the approved envelope's discount
depth, the starting trust posture, and the intent-cluster vocabulary.

TWO KINDS OF SELLER. :data:`TRUST_SCORES` states a posture for the ten storefronts this demo
says the platform has TRANSACTED with; :data:`CATALOGUE_ACCURACY` states one number for the
nine it has only CRAWLED. The shapes differ because the evidence does, and
:func:`dimension_posteriors` returns one dimension for a crawled shop and five for a
transacting one. See :data:`CATALOGUE_ACCURACY`.

THE TRUST POSTURE IS NO LONGER A DOCUMENT KEY. :data:`TRUST_SCORES` and
:func:`dimension_posteriors` still live here, because a person still has to state what the
platform's opening reading of each store is, but nothing writes them into
``exchange-deployment.json`` any more: ``scripts/seed_demo_trust.py`` reads them and seeds
the live trust service, and the exchange reads ``GET /snapshot``. See the comment above
``exchange_doc`` in :func:`build` for why a stated key made the whole column unmovable.

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
import heapq
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
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
from trust.scoring.dimensions import (  # noqa: E402 - after the bootstrap
    CATALOG_DIMENSION,
    TRUST_DIMENSIONS,
)

#: The recorded corpus this demo is built from.
#:
#: ``fixtures/real-catalogs-demo/`` — nineteen storefronts across five stocked categories,
#: derived from ``fixtures/real-catalogs-broad/`` by ``scripts/build_demo_corpus.py`` with the
#: store files copied verbatim. It is NOT ``fixtures/real-catalogs/``, which is still in the
#: tree, still gated by ``fixtures/tests/test_real_catalogs.py``, and still ten supplement
#: storefronts — which is why the demo used to answer *"a walnut coffee table for the lounge"*
#: with liver capsules: there was no coffee table in it to find.
CORPUS = REPO_ROOT / "fixtures" / "real-catalogs-demo"

#: Where the generated documents land. Tracked, mounted read-only into the containers.
OUT = REPO_ROOT / "deploy" / "demo"

#: The one named catalogue cluster this demo addresses intents to.
#:
#: It is configuration rather than data because no service in this repository publishes the
#: cluster vocabulary -- ``exchange.retrieval.clusters`` says so at length. The agents'
#: ``pursue_clusters`` below name this same id, which is what authorises them to bid: an
#: envelope listing no cluster pursues nothing, and that is fail-closed by design.
CLUSTER_ID = "cluster-liver-support"

#: The shopper sentence this demo is tuned for.
DEMO_QUERY = "milk thistle silymarin liver support extract"

#: The terms the cluster matches on.
#:
#: WHY ``liver health`` IS HERE, since it reads as a duplicate of ``liver support`` and is not.
#: ``exchange.retrieval.clusters`` scores a matched term at ``TERM_WEIGHT`` per word up to
#: ``TERM_PHRASE_WORD_CAP`` (two), and ``MIN_ASSIGNMENT_SCORE`` is 2.0. A ONE-word term is
#: therefore deliberately below the bar — the worked counter-example is a cluster called
#: ``coffee`` and the query *"a walnut coffee table for the lounge"*, which must NOT authorise a
#: coffee merchant to bid at a furniture shopper. This line does not weaken that: it adds a
#: two-word phrase, matched as a whole word SEQUENCE, which cannot fall out of a sentence about
#: something else.
#:
#: What it fixes is one measured query. *"something for liver health"* matched only ``liver``,
#: scored 1.0, went unassigned, and every hosted agent answered ``204 cluster_not_pursued`` — so
#: a shopper asking the demo's own subject in the demo's own words got no sponsored row at all.
#: Measured through ``POST /buyer/intent/confirm`` before this term existed: 4 slots, 0
#: sponsored, ``all_fallback: true``.
#:
#: It is a DATA fix for ONE query and it is not the general repair, which is worth saying
#: because adding a phrase here is the tempting way to answer the next complaint too. What makes
#: an unassigned query still useful is the organic half — ``exchange.retrieval.relevance`` —
#: which decides whether the rows the PLATFORM manufactures are about what was asked. Vocabulary
#: added one phrase at a time answers the queries somebody thought of; the organic half answers
#: the rest, and answers "we have nothing on this" honestly when there is nothing.
CLUSTER_TERMS = (
    "milk thistle",
    "silymarin",
    "liver support",
    "liver health",
    "liver",
    "detox",
    "dandelion root",
    "artichoke extract",
)

#: The four storefronts that get a hosted store agent in compose. They are the four the
#: corpus README names as stocking the liver-support query; the other FIFTEEN storefronts stay
#: in the seller registry and the graph, are found by retrieval, and are represented by R10's
#: list-price fallback because no agent answers for them. A demo in which every rostered
#: store bids would not show that half of the market at all.
HOSTED = (
    "gaiaherbs.com",
    "toniiq.com",
    "paradiseherbs.com",
    "oregonswildharvest.com",
)

#: Two of the ten supplement storefronts stock no liver-support inventory at all (the corpus
#: README calls them
#: negative controls). They are eligible and rostered like everyone else -- retrieval is
#: what must leave them out, not the registry.
NEGATIVE_CONTROLS = ("livemomentous.com", "nakednutrition.com")

#: Stated trust scores. The platform's reading of a store, not the store's own claim, so it
#: cannot come from the catalogue and a person states it. Nobody is blacklisted here: the
#: blacklist beat is `fixtures/manifest.json`'s scripted dishonest store, and marking a real
#: company blacklisted in a shipped fixture would be a claim this project cannot support.
#:
#: These no longer reach a `trust_snapshot` key in the exchange document, and that is the
#: point of the change that removed it: a stated snapshot makes `exchange.composition
#: ._bind_live_ranking_snapshot` skip the live reader entirely, so the demo's whole `trust`
#: column was a sorted copy of this table and nothing an audience did could move it. What
#: reads this table now is `scripts/seed_demo_trust.py`, which turns each store's posture
#: into marked ledger events on the running trust service, and the exchange reads what that
#: service serves. The numbers are the same; what changed is that they are a STARTING
#: POSITION rather than the answer.
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

#: The nine ORGANIC storefronts this demo promoted, and what the platform's crawl of each is
#: worth. One number, on one dimension, and the shape of this table is the argument.
#:
#: **A scraped shop has no transaction record, so it does not get one.** D55: an in-network
#: shop is SPONSORED and buys the right to make its case in its own voice; a scraped shop is an
#: ORGANIC result carrying a pitch the PLATFORM wrote. The ten in :data:`TRUST_SCORES` are
#: stores this demo says the platform has transacted with, and their postures are stated across
#: all six dimensions because a transaction record touches all six. Nobody has ever bought
#: anything from these nine. Handing them ``shipped_on_time`` evidence would be manufacturing a
#: dispatch history for a shop the platform has only ever read the catalogue of, and the whole
#: point of the trust column is that it is evidence rather than decoration.
#:
#: So five of the six dimensions are seeded with NOTHING and sit at the neutral ``Beta(2, 2)``
#: prior. ``catalog_claim_accuracy`` is the sixth, and it is the ONE the crawl can honestly
#: speak to: ``trust.scoring.dimensions`` says in its own words that it "grades a *product
#: fact*: whether what the pitch said about the goods matches the catalog", and for an organic
#: row the pitch is the platform's, written from the catalogue it crawled and graded against
#: the snapshot in ``exchange-deployment.json``. Five of the six grade an offer-integrity
#: PROMISE — something the store said it would do — and these stores have promised nothing.
#:
#: **The consequence is that every one of these stores is served ``low_data: True``, and that
#: is the correct reading rather than a cost.** ``trust.snapshot.builder`` opens by saying
#: ``low_data`` "marks a store the exchange should treat as *unknown* rather than *average*",
#: and a shop the platform has only crawled is exactly unknown. It does not exclude the store —
#: R12's exclusion is for a store with NO row at all, which is why these rows have to exist —
#: it turns on the exchange's exploration slice, one shortlist slot of four, which is the
#: exchange behaving correctly about a store it has never watched deliver.
#:
#: The NUMBERS are the platform's reading of the catalogue it crawled, in the same spirit as
#: :data:`TRUST_SCORES`: a person states them, because no storefront publishes them. They sit
#: in a narrow band a little above neutral -- a crawl that parsed cleanly and priced cleanly is
#: mild positive evidence, and nothing about a crawl supports 0.9. The two furniture stores
#: whose catalogues are mostly components, swatches and service contracts read lowest, because
#: those rows are the ones a platform-written pitch is most likely to get wrong.
CATALOGUE_ACCURACY = {
    "floydhome.com": 0.68,
    "branchfurniture.com": 0.64,
    "sabai.design": 0.62,
    "deathwishcoffee.com": 0.70,
    "vervecoffee.com": 0.69,
    "nemoequipment.com": 0.72,
    "hyperlitemountaingear.com": 0.70,
    "fromourplace.com": 0.71,
    "fellowproducts.com": 0.63,
}

#: Every seller this demo registers with the trust service, transacting ten first.
#:
#: ``scripts/seed_demo_trust.py`` seeds exactly this list and R12 excludes anyone not in it, so
#: :func:`build` refuses a corpus carrying a host this list does not.
DEMO_SELLERS: tuple[str, ...] = (*TRUST_SCORES, *CATALOGUE_ACCURACY)

#: The shops ``deploy/demo/buyer-roster.json`` names — the STATED candidate set the buyer
#: service sends on every confirmation, because it refuses to open an auction with none
#: (``buyer_svc.composition.NoRosterBound``).
#:
#: The four hosted stores plus two agent-less supplement storefronts, which is what it has
#: always been -- so a run still shows both halves of R10, a store that bids and a store
#: represented at its catalogue list price because nobody answered for it -- **plus the nine
#: promoted organic shops**, which is the change.
#:
#: WHY THE NINE HAD TO BE ADDED, measured rather than assumed. ``GraphShopRoster.solicit`` is
#: consulted for the shop set ONLY when the request body states no roster (``auction/routes``
#: says so at the top), and the buyer route always states one. ``retrieval.roster.
#: repoint_organic_products`` can move a stated row onto the product the graph says answers the
#: query, but it cannot add a shop that is not on the list. So a roster of six supplement shops
#: answers *"a walnut coffee table for the lounge"* with six honestly-off-topic rows and an
#: empty shortlist, no matter how many coffee tables are in the graph.
#:
#: The other four incumbent supplement stores stay OFF the roster, exactly as before: they are
#: in the seller registry and in the graph, and the roster-less route (``scripts/demo_check.sh``
#: and ``POST /auctions`` with no roster) finds them. A demo where the stated roster is the
#: whole registry would not show that there are two ways in.
ROSTER_HOSTS: tuple[str, ...] = (
    *HOSTED,
    "bulksupplements.com",
    "nutricost.com",
    *CATALOGUE_ACCURACY,
)

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
#: read as 2.2 days. `gaiaherbs.com` at 0.93 is quoted near face value. The six OTHER
#: transacting stores sit at parity with their overall score, because a store with no story does
#: not need an invented one, and the nine crawled ones carry no dispatch record at all.
DISPATCH_POSTERIOR = {
    "gaiaherbs.com": 0.93,
    "toniiq.com": 0.45,
    "paradiseherbs.com": 0.88,
    "oregonswildharvest.com": 0.62,
}

#: How much EVIDENCE stands behind each dimension -- observations, not the posterior's total
#: mass. The Beta the trust engine ends up serving is this evidence ON TOP of the neutral
#: `Beta(2, 2)` prior every dimension starts at, so a dimension whose evidence is entirely
#: positive is served `Beta(2 + 12, 2)` and reads 0.875, not 1.0.
#:
#: That distinction is the correction of a real mistake in the version of this file that stated
#: a `trust_snapshot`. It emitted `alpha = mean * 14, beta = (1 - mean) * 14` -- an ABSOLUTE
#: posterior, which for `gaiaherbs.com`'s dispatch record was `Beta(13.02, 0.98)`. A `beta`
#: BELOW the prior's 2.0 is not something any number of observations can produce: evidence only
#: ever adds. The document was internally consistent and unreachable, and nothing noticed while
#: the exchange read the document instead of the trust service.
#:
#: 12.0 is chosen against four floors, three of which bite:
#:
#: * `alpha + beta` reaches 16, clearing the exchange's dispatch admissibility floor --
#:   `features` refuses a promise while `mass - TRUST_PRIOR_MASS (4.0) < MIN_DISPATCH_
#:   OBSERVATIONS (5.0)`, so 9.0 itself is admitted and anything under it is not;
#: * `trust.snapshot.builder.clean_episodes` takes the minimum POSITIVE observation count across
#:   the five non-feedback dimensions and flags a store `low_data` below `NEW_STORE_PRIOR_N`
#:   (5), which would switch on the exchange's exploration floor. `toniiq.com` is the binding
#:   case at `shipped_on_time` 0.45: `0.45 * 12 = 5.4`, six positive rows;
#: * every unit of POSITIVE evidence costs one ledger event, because the published weight of
#:   `fulfilled`/`verified` is 1.0 and the per-observation channel only scales DOWN (R14's cap).
#:   12.0 puts the whole seed at 568 events (340 `reconciled`, 228 `claim_verified`); the 26.0 it
#:   would take to express a served mean of 0.93 would put it past 1,200, permanently, in an
#:   append-only chain. `scripts/tests/test_seed_demo_trust.py` bounds it;
#: * and it does NOT need to be large to leave room for the demo to move. A store's `score` is
#:   the unweighted mean of its six dimension MEANS, not a mass-weighted pool, so evidence here
#:   does not damp what buyer feedback does to `feedback_match` -- which the seed deliberately
#:   leaves empty.
#:
#: What it costs: the served scores are the stated ones pulled toward 0.5 by the prior and by an
#: unopinionated sixth dimension -- 0.86 is served as about 0.73. The ORDER, which is the demo's
#: actual claim, is untouched. Across all ten the gaps between adjacent stores run 0.0052 to
#: 0.0312; across the four HOSTED ones -- the only stores that bid, and so the only ones buyer
#: feedback ever reaches -- they run 0.0245 to 0.0312, inside what one press of the learning
#: page's button moves (measured on the served route: +0.0389).
DIMENSION_EVIDENCE = 12.0

#: The neutral prior `trust.scoring.engine` starts every dimension at, restated here because
#: this script must compute what the trust engine will serve without importing it (it runs
#: against the corpus, not against a running service). `apps/trust/tests/test_scoring.py` pins
#: the engine's copy; `scripts/tests/test_seed_demo_trust.py` pins that these two agree.
PRIOR_ALPHA = 2.0
PRIOR_BETA = 2.0

#: The approved envelope's discount depth per hosted store. Deployment configuration: the
#: merchant's authorisation, which no storefront publishes.
MAX_DISCOUNT_PCT = {
    "gaiaherbs.com": 15.0,
    "toniiq.com": 20.0,
    "paradiseherbs.com": 12.0,
    "oregonswildharvest.com": 18.0,
}

#: How many of a store's products reach the exchange's `catalog` snapshot.
#:
#: THIS IS THE WINDOW. `ranking.verification.catalog_identity` can name a shortlist slot only
#: if its product is in here; `catalogue_readings` resolves a slot's `product.identity` out of
#: this same document, so a row pointing outside comes back with **`identity: null`** — a row a
#: shopper is shown and the platform cannot name — and `ranking.filters.
#: organic_relevance_reason` reads that same identity and treats an absent one as "unchecked",
#: so the row is unfilterable as well as nameless. WHICH products land inside it is
#: `_store_catalog`'s question and the comment above it is where that is answered; this
#: constant is only HOW MANY.
#:
#: **250, and the number is the ceiling's, not a preference.** The document is capped at 4 MiB
#: across every store (`composition.MAX_DEPLOYMENT_BYTES`) and one store's snapshot at 1000
#: products (`ranking.verification.MAX_CATALOG_PRODUCTS`). Measured on this roster, a snapshot
#: product costs a flat 955 bytes, and the nineteen storefronts hold 4,830 priced products:
#:
#:     window   products   bytes       of 4 MiB
#:        200      2,984   2,853,585      68.0%
#:        250      3,241   3,097,455      73.8%   <- this one
#:        300      3,487   3,330,562      79.4%
#:        400      3,887   3,708,644      88.4%
#:       1000      4,830   4,602,384     109.7%   <- every product: OVER the ceiling
#:
#: The predecessor of this constant was 1000 — "every product" for ten supplement storefronts
#: holding 3,086 between them. At nineteen stores that is no longer true of any number, so the
#: trim is real again and where it falls is decided by the ordering rather than by price.
#:
#: **The constant does not bound the document on its own and must not be read as if it did.**
#: The bound is `sum over stores of min(products, 250) x 955`, so it moves with the STORE COUNT
#: as much as with this number: nineteen stores all at the cap would be 4.53 MB, over the
#: ceiling. :func:`build` therefore renders the document and refuses it against
#: :data:`DEPLOYMENT_BYTES_BUDGET` rather than trusting this line, because a demo whose
#: exchange refuses its own deployment document at composition time serves an empty shortlist
#: and says nothing about why.
#:
#: The agents' own catalogues are NOT trimmed, because an agent that cannot find the product
#: the graph rostered declines the auction with `no_matching_product` — which reads as "this
#: store does not stock it" when the truth is "this file was trimmed".
SNAPSHOT_PRODUCTS_PER_STORE = 250

#: The share of ``composition.MAX_DEPLOYMENT_BYTES`` the generated exchange document may spend.
#:
#: The exchange refuses a document over 4 MiB outright (`composition` raises, and the container
#: then serves every auction from its fail-closed defaults), so the interesting number is not
#: the ceiling but the distance from it. 85% leaves about 630 KB — room for a re-collection to
#: find more inventory, or for one more storefront on the roster, without anybody having to
#: re-derive the arithmetic above. :func:`build` renders and measures rather than estimating.
DEPLOYMENT_BYTES_BUDGET = 0.85

#: ``exchange.composition.MAX_DEPLOYMENT_BYTES``, restated because this script runs against the
#: corpus rather than against a running exchange and importing the app to read one integer
#: would pull in its whole composition root. ``scripts/tests/test_build_demo_deployment.py``
#: pins that these two agree.
MAX_DEPLOYMENT_BYTES = 4 * 1024 * 1024

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


# =====================================================================================
# THE WINDOW — which of a store's products the exchange can NAME
#
# `_snapshot` ships `ranked[:SNAPSHOT_PRODUCTS_PER_STORE]`, and that slice is the set of
# products `ranking.verification.catalog_identity` can resolve a shortlist slot against. A
# roster row pointing outside it comes back with `product.identity: null` — a row a shopper is
# shown and the platform cannot name — and `ranking.filters.organic_relevance_reason` reads
# that same identity and treats an absent one as "unchecked", so the row is also unfilterable.
#
# WHAT WAS WRONG. The order was `(-_relevance(entry), price, product_ref)` and `_relevance`
# counts CLUSTER_TERMS — liver-supplement vocabulary. For any store outside that one category
# every product scores zero, the sort collapses onto its second key, and the window becomes
# literally the N CHEAPEST products. That is not a near miss on a furniture store: the cheapest
# rows in a furniture catalogue are its swatches, components, service contracts and gift cards.
# Measured on THIS roster, the first five products of each promoted store's window under the
# old order:
#
#   branchfurniture.com   five identical `XCover Protection Plan` rows, $12.99 each
#   floydhome.com         `Serviceability - Sofa`, `- Lift Off Smaller Components`,
#                         `- The Shelving System (Storage)`, `- The Floyd Leg`, `- HARDWARE`
#   fellowproducts.com    `Replacement Carter Slide Mug Mouth Gasket` ($1.00), the piston
#                         gasket, two grinder screws, a descaler
#   sabai.design          a $0.25 carbon-removal donation, `Terra Leather`, three furniture legs
#   nemoequipment.com     tent stakes, a pump sack and a trucker hat
#
# Not one of those twenty-five rows is a thing the store is rostered for.
#
# Meanwhile `retrieval.roster.repoint_organic_products` picks a product from `GraphShopRoster.
# solicit` over the WHOLE ingested corpus and never consults the snapshot. So the graph ranks a
# store's products by relevance to the query and the window ranked them by cheapness — two
# orderings with nothing to do with each other, and every row where they disagreed was nameless.
#
# WHAT REPLACES IT, and why this signal rather than another. The window is now chosen by a
# greedy pass over the STORE'S OWN title vocabulary that saturates each term as it is covered
# (:func:`_window_order`). Three properties, and each is the answer to a way this could have
# gone wrong:
#
#  * it is PER STORE. There is no category->vocabulary table anywhere in this file, and
#    deliberately: a table is the cluster bug generalised — it answers the queries somebody
#    thought of and files everything else at zero. The only vocabulary consulted for a store is
#    the vocabulary that store's own catalogue publishes.
#  * it CANNOT collapse onto price. Price is not a key at any level; ties break on
#    `product_ref`, which is a hash of the store id and the storefront's own product key. A
#    store whose products all scored zero would come out in `product_ref` order, which is
#    arbitrary — and arbitrary is a far better failure than "every cheap accessory first".
#  * it PREFERS BREADTH over repetition. `cotopaxi.com` publishes 1,431 priced rows under 711
#    distinct titles; without saturation a window would fill with colourways of one hip pack.
#
# WHAT IT DOES NOT DO is decide relevance for the auction. The exchange runs its own retrieval
# over the graph and is not bound by this ordering; this decides only which products the
# exchange holds a snapshot row for.
# =====================================================================================

#: Words carrying no discriminating power in a product title. Deliberately short: this is a
#: stoplist for English function words and the two units of measure that appear in most sizes,
#: not a curation of what counts as a product. Every judgement about which products matter is
#: made by the corpus, never by this tuple.
_STOPWORDS = frozenset(
    """
    a an and are as at be by for from in into is it its of on or the this to with
    oz ct pcs pack size color colour new
    """.split()
)

#: A term must appear in at least this many of a store's products before it can be covered.
#:
#: A term carried by exactly one product describes THAT PRODUCT and says nothing about the
#: store, so covering it buys the window nothing and rewards whichever row has the longest
#: unusual title. Measured on this roster: admitting them costs 2.7 points of
#: on-topic-in-window (86.7% to 84.0% at a window of 250) and moves no store's lead product.
_MIN_STORE_DOCUMENT_FREQUENCY = 2


def _title_terms(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """The vocabulary one product publishes: its title's words and adjacent word pairs.

    THE TITLE, AND NOTHING ELSE, because the title is what the reader downstream reads.
    ``exchange.retrieval.relevance.identity_surface`` — the function
    ``ranking.filters.organic_relevance_reason`` judges a slot's identity with — reads title
    and brand, and ``brand`` is one constant string per storefront here, so within a store it
    carries no information at all.

    Adjacent pairs are included because the phrases that decide this are two words long —
    ``coffee table``, ``sleeping bag``, ``dutch oven``, ``milk thistle``. A window chosen on
    single words alone treats "coffee" in a furniture store and "coffee" in a roastery as the
    same evidence.

    ``tags`` is excluded because ``fixtures/tests/test_real_catalogs.py`` has a gate called
    ``test_tags_carry_operational_junk_and_literal_typos``: covering a store's ``YGroup_``
    slugs would spend the window on vocabulary no shopper types.

    ``product_type`` is excluded too, and that one was measured rather than reasoned. It is a
    category LABEL repeated verbatim down a column, so its frequency is an artefact of the
    merchant's taxonomy rather than of what the store sells — ``branchfurniture.com`` files 25
    rows under ``Clyde Service Contract``, which made those three words the heaviest terms in
    its catalogue and put **"XCover Protection Plan" at the head of its window and on its
    roster row**. Dropping it moved that lead to a real product and was worth 0.7 points of
    on-topic-in-window across this roster (86.1% to 86.8% at a window of 250).
    """
    words = [
        word
        for word in re.split(r"[^a-z0-9]+", str(entry.get("title") or "").lower())
        if word and word not in _STOPWORDS and not word.isdigit()
    ]
    pairs = [f"{first} {second}" for first, second in zip(words, words[1:], strict=False)]
    # `dict.fromkeys` rather than `set`: a stable order makes the greedy below reproducible
    # without sorting every product's term list on every visit.
    return tuple(dict.fromkeys([*words, *pairs]))


def _term_weights(store_terms: Mapping[str, Mapping[str, int]], host: str) -> dict[str, float]:
    """How much covering each of ``host``'s terms is worth, given every other store.

    ``log(1 + S / stores_carrying(t)) * log(1 + times this store uses t)`` — the two halves of
    the question, and each one alone gets it wrong:

    * **across stores.** ``log(1 + S / stores_carrying(t))`` over the ``S`` stores in the
      corpus. A word only this storefront uses is worth about three times one every storefront
      uses, which is what keeps the window off the words that are everywhere — ``set``,
      ``kit``, ``bundle``, ``gift`` — without a hand-written list of them.
    * **within the store.** ``log(1 + count)``, the ordinary sublinear damping. Without it a
      term two products share weighs as much as one two hundred share, and the window fills
      with the store's oddities instead of what it sells. Measured on this roster, dropping
      this half costs 2.2 points of on-topic-in-window (86.7% to 84.5% at a window of 250).

    Terms under :data:`_MIN_STORE_DOCUMENT_FREQUENCY` within the store are dropped entirely;
    see that constant.
    """
    stores = len(store_terms)
    carrying: dict[str, int] = {}
    for counts in store_terms.values():
        for term in counts:
            carrying[term] = carrying.get(term, 0) + 1
    return {
        term: math.log(1.0 + stores / carrying[term]) * math.log(1.0 + count)
        for term, count in store_terms[host].items()
        if count >= _MIN_STORE_DOCUMENT_FREQUENCY
    }


def _window_order(
    terms_by_ref: Mapping[str, tuple[str, ...]], weights: Mapping[str, float], window: int
) -> list[str]:
    """``window`` product refs that cover this store's vocabulary, most representative first.

        A greedy maximisation of a saturating coverage objective: a product is worth the sum, over
        the terms it carries, of ``weight(term) / sqrt(1 + times that term is already covered)``.
        The first pick is the product carrying the most of what makes this store distinctive; every
        pick after it is worth less the more its vocabulary has already been said.

    **Both halves are measured on this roster**, at a window of 250, as the fraction of on-topic
        products falling inside it:

            no saturation at all (a fixed per-product score)   86.1%
            ``1 + k``, the obvious harmonic decay             84.3%
            ``sqrt(1 + k)``, in the tree                      86.7%

        ``1 + k`` is the interesting loser: it forgets a term almost immediately and spends the
        window on the store's tail. ``sqrt`` halves a term's value every fourth time it is covered,
        which keeps the window on what the store actually sells while still refusing to fill it
        with one product.

        **Saturation is worth only 0.6 points HERE, and that is a fact about this roster rather
        than about the idea.** What it defends against is a catalogue of near-duplicates —
        ``cotopaxi.com`` publishes 1,431 priced rows under 711 distinct titles — and this roster
        deliberately does not carry one; ``scripts/build_demo_corpus.py`` says why cotopaxi was
        left off. Keep the saturation: the store that needs it is one re-collection away, and
        without it the objective is a fixed per-product score that cannot see a duplicate at all.

        Ties break on ``product_ref`` and on nothing else. Price is absent from this function by
        construction, which is the property the ordering it replaces did not have.

        Implemented lazily (Robertson's accelerated greedy): the objective is monotone and
        submodular, so a product's gain never rises, and a heap entry whose recomputed gain still
        equals its stored key is the true maximum. Exact, not approximate — the same answer the
        quadratic loop gives, measured on every store in this roster.
    """
    covered: dict[str, int] = {}

    def gain(ref: str) -> float:
        return sum(
            weights[term] / math.sqrt(1.0 + covered.get(term, 0))
            for term in terms_by_ref[ref]
            if term in weights
        )

    heap = [(-gain(ref), ref) for ref in terms_by_ref]
    heapq.heapify(heap)
    order: list[str] = []
    while heap and len(order) < window:
        stale, ref = heapq.heappop(heap)
        fresh = -gain(ref)
        if fresh <= stale + 1e-12:  # still exact: this really is the best remaining product
            order.append(ref)
            for term in terms_by_ref[ref]:
                if term in weights:
                    covered[term] = covered.get(term, 0) + 1
        else:
            heapq.heappush(heap, (fresh, ref))
    return order


def _store_catalog(
    host: str, store_terms: Mapping[str, Mapping[str, int]], window: int
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """``({product_ref: row}, [product_ref, the window first])`` for one storefront.

    The returned order is ``[the cluster lead] + [the window, most representative first] +
    [everything else]``.

    **Why the lead is still chosen by :data:`CLUSTER_TERMS` when nothing else is.** It is not
    part of the window question. ``ranked[0]`` is what ``deploy/demo/buyer-roster.json`` pins
    each shop to and what ``_store_context`` puts an explicit envelope floor on — a STATED
    roster, built before the shopper typed anything, around the one cluster this demo
    addresses intents to. For the ten supplement storefronts that keeps the demo's lead
    products exactly what they were. For the nine promoted ones every product scores zero, so
    the lead is simply the window's own first pick — the store's most representative product,
    not its cheapest, which is what the old tie-break made it.

    Past the window the order is by descending base score and then ``product_ref``. Nothing
    reads it; it is deterministic rather than arbitrary so a diff of two runs is empty.
    """
    catalog: dict[str, dict[str, Any]] = {}
    terms_by_ref: dict[str, tuple[str, ...]] = {}
    relevance: dict[str, int] = {}
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
        terms_by_ref[product_ref] = _title_terms(entry)
        relevance[product_ref] = _relevance(entry)
    if not catalog:
        return catalog, []

    weights = _term_weights(store_terms, host)
    order = _window_order(terms_by_ref, weights, window)
    placed = {ref: position for position, ref in enumerate(order)}
    outside = len(order) + 1
    # Most cluster-relevant; among equals, the one the window already ranks first; then the id.
    # Price is not consulted at any level, which is the whole repair.
    lead = min(catalog, key=lambda ref: (-relevance[ref], placed.get(ref, outside), ref))

    base = {
        ref: sum(weights[term] for term in terms if term in weights)
        for ref, terms in terms_by_ref.items()
    }
    ranked = [lead, *(ref for ref in order if ref != lead)]
    seen = set(ranked)
    ranked += sorted((ref for ref in catalog if ref not in seen), key=lambda ref: (-base[ref], ref))
    return catalog, ranked


#: How long an offer these demo merchants make STANDS, in seconds, past the auction's deadline.
#:
#: Read by ``store_agent.runtime.context.AuctionContext.offer_expires_at``, whose fallback
#: without it is the auction's own ``respond_by`` — an offer with zero usable life, dead the
#: instant the shortlist is handed to the shopper. Measured on the served buyer route before
#: these contexts stated anything: all four sponsored rows expired **3.2 seconds** after they
#: were served.
#:
#: **Why 600 and not the exchange's own 900.** The obvious number is
#: ``exchange.auction.collect.FALLBACK_OFFER_TTL_SECONDS`` — 900 s, what the exchange gives the
#: list-price stand-in it mints for a silent store. It is the wrong number, and an adversarial
#: review measured why: the auction RECORD and its shortlist also live 900 s
#: (``exchange.auction.state.AUCTION_TTL_SECONDS``), anchored at the CLOSE, while the offer's
#: expiry is anchored at ``respond_by`` — which the fan-out usually reaches a second or two
#: after the close. So a 900 s offer outlives the record that explains it, and a shopper who
#: clicks late is answered::
#:
#:     404  "auction '...' is not in Redis at auction:... — it was never created, or its
#:           900s TTL has expired."
#:
#: rather than the honest ``409 DiscountDoesNotApply: the offer's own expiry``. The whole
#: argument for putting a merchant's offer on a shortlist at all is that a late click gets told
#: the truth about the OFFER, so the window has to end while the record is still readable.
#:
#: 600 s is ten minutes of usable life — long enough to read a shortlist, talk about it and
#: click — and leaves roughly five minutes in which a lapsed offer is refused by name.
OFFER_VALID_FOR_SECONDS = 600


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
        "offer_valid_for_seconds": OFFER_VALID_FOR_SECONDS,
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


def _beta(mean: float) -> dict[str, Any]:
    """``mean`` as evidence laid on top of the prior, never as an absolute posterior."""
    return {
        "alpha": round(PRIOR_ALPHA + mean * DIMENSION_EVIDENCE, 6),
        "beta": round(PRIOR_BETA + (1.0 - mean) * DIMENSION_EVIDENCE, 6),
    }


def dimension_posteriors(host: str) -> dict[str, dict[str, Any]]:
    """The six Betas for one store: :data:`DIMENSION_EVIDENCE` of evidence, over the prior.

    THIS IS NOT WRITTEN INTO ANY DOCUMENT ANY MORE. It is the target
    `scripts/seed_demo_trust.py` seeds the live trust service to, one marked ledger event per
    unit of evidence, so the ranking gate can read a real `GET /snapshot` instead of a frozen
    key. What each store's EVIDENCE says is unchanged, which is what makes the demo's story
    about who is more reliable survive the switch.

    :data:`TRUST_SCORES` and :data:`DISPATCH_POSTERIOR` are read here as the ratio the evidence
    carries, NOT as the posterior the trust engine will serve. The trust engine defines a
    store's `score` as the mean of its six dimension means (D53) over a `Beta(2, 2)` prior, so
    a store whose evidence is 86% positive is served about 0.73 once the prior is included and
    `feedback_match` is counted at the neutral 0.5. Both are true statements about the same
    store; only one of them is a posterior.

    `shipped_on_time` takes its ratio from :data:`DISPATCH_POSTERIOR`; the other five carry
    whatever ratio makes the six average back to :data:`TRUST_SCORES` -- `m = (6*score - d)/5`.
    That arithmetic is why the dispatch numbers are chosen close enough to the score to keep `m`
    inside [0, 1]: a store scored 0.74 cannot also have shipped on time 0.05, because no set of
    five ratios fixes that average. The function refuses rather than clamping, since a clamp
    would imply a `score` the dims do not support and the mismatch would surface as an
    unexplained ranking rather than as a build failure.

    The `feedback_match` entry it returns is what that dimension WOULD carry if the demo pretended
    buyers had spoken. The seeder skips it on purpose and leaves the dimension at the neutral
    prior -- nobody has ever left feedback for any of these storefronts, and it is the only
    dimension `POST /buyer/feedback` can move.

    A HOST IN :data:`CATALOGUE_ACCURACY` GETS ONE ENTRY, NOT SIX, and the missing five are the
    statement. Those nine storefronts have been crawled and never transacted with, so the only
    dimension the platform holds evidence on is `catalog_claim_accuracy`; a `shipped_on_time`
    Beta for a shop nobody has ever ordered from would be a manufactured dispatch record. The
    seeder writes what this returns and nothing else, so those five dimensions stay at the bare
    prior and the store is served `low_data: True` -- unknown rather than average, which is the
    truth about it. See :data:`CATALOGUE_ACCURACY`.
    """
    if host in CATALOGUE_ACCURACY:
        return {CATALOG_DIMENSION: _beta(CATALOGUE_ACCURACY[host])}
    score = TRUST_SCORES.get(host, 0.6)
    dispatch = DISPATCH_POSTERIOR.get(host, score)
    others = (6.0 * score - dispatch) / 5.0
    if not 0.0 <= others <= 1.0:
        raise SystemExit(
            f"FATAL: {host} states score={score} and shipped_on_time={dispatch}, which needs the "
            f"other five dimensions to average {others:.4f} -- outside [0, 1]. Move the dispatch "
            f"posterior closer to the score, or move the score."
        )

    return {
        dimension: _beta(dispatch if dimension == "shipped_on_time" else others)
        for dimension in TRUST_DIMENSIONS
    }


def agent_service(host: str) -> str:
    """The compose service name for ``host``'s store agent.

    ``gaiaherbs.com`` -> ``store-agent-gaiaherbs``. The exchange reaches it by that name on
    the compose network, so this function and the compose fragment have to agree; the
    runbook gate reads the fragment and this script writes the endpoint.
    """
    return "store-agent-" + host.split(".")[0].replace("_", "-")


def corpus_hosts() -> list[str]:
    """Every storefront the corpus carries, in the order its manifest lists them."""
    manifest = json.loads((CORPUS / "collection.json").read_text(encoding="utf-8"))
    return [str(store["host"]) for store in manifest["stores"] if store.get("skipped") is None]


def store_vocabularies(hosts: Sequence[str]) -> dict[str, dict[str, int]]:
    """``{host: {term: how many of that store's products carry it}}`` over the whole corpus.

    Computed once for every store rather than per store, because :func:`_term_weights` needs
    the CROSS-STORE half — how many storefronts use a word — and that is not knowable from one
    catalogue. It is the signal that tells ``sofa`` (three stores) from ``set`` (all nineteen).
    """
    vocabularies: dict[str, dict[str, int]] = {}
    for host in hosts:
        counts: dict[str, int] = {}
        for entry in _read_products(host):
            if _priced_variant(entry) is None:
                continue
            for term in _title_terms(entry):
                counts[term] = counts.get(term, 0) + 1
        vocabularies[host] = counts
    return vocabularies


def build() -> dict[str, Any]:
    """Every document, as ``{relative path: JSON object}``."""
    hosts = corpus_hosts()
    missing = [host for host in HOSTED if host not in hosts]
    if missing:
        raise SystemExit(f"FATAL: the corpus does not carry the hosted stores {missing}")
    unregistered = [host for host in hosts if host not in DEMO_SELLERS]
    if unregistered:
        # R12 is fail-closed: `ranking.filters.blacklist_reason` excludes a store the live
        # trust snapshot holds no row for, and `scripts/seed_demo_trust.py` seeds exactly the
        # stores TRUST_SCORES names. A seller in the corpus and not in that table is a seller
        # the exchange refuses on every shortlist, silently, and the only symptom is a shorter
        # shortlist. Refusing here is the only place that can say so before the demo does not.
        raise SystemExit(
            f"FATAL: {unregistered} are in the corpus but not in DEMO_SELLERS, so "
            f"scripts/seed_demo_trust.py will not seed them and R12 will exclude them from "
            f"every shortlist. Add a posture for each, or take them off the roster."
        )

    vocabularies = store_vocabularies(hosts)
    catalogs: dict[str, dict[str, Any]] = {}
    ranked: dict[str, list[str]] = {}
    for host in hosts:
        catalogs[host], ranked[host] = _store_catalog(
            host, vocabularies, SNAPSHOT_PRODUCTS_PER_STORE
        )
        if not ranked[host]:
            raise SystemExit(f"FATAL: {host} recorded no priced product; nothing to auction")

    sellers = []
    for host in hosts:
        row: dict[str, Any] = {
            "store_id": host,
            "eligibility": "eligible",
            "registered_domain": host,
        }
        if host in HOSTED:
            row["bid_endpoint"] = f"http://{agent_service(host)}:8086/v1/bid-requests"
        sellers.append(row)

    # NO `trust_snapshot` KEY, deliberately, and this is the one line of this document worth
    # reading twice. `exchange.composition._bind_live_ranking_snapshot` returns without binding
    # a live reader whenever the document states that key, so a demo that stated one could not
    # read the trust service at all -- every `trust` term was 0.20 x a number typed into this
    # file, identical on every run, unmovable by anything an audience did. Leaving the key out
    # binds `LiveTrustSnapshot` against `trust_url` below.
    #
    # It is fail-closed: a store the live snapshot holds no row for is EXCLUDED from ranking
    # (`exchange.ranking.filters.blacklist_reason`, R12), so a stack whose trust service knows
    # nobody shortlists nobody. `scripts/seed_demo_trust.py` is what puts all nineteen there,
    # and `scripts/demo_check.sh` names it by name when the shortlist comes back empty.
    exchange_doc = {
        "sellers": sellers,
        "intent_clusters": [
            {
                "cluster_id": CLUSTER_ID,
                "label": "Liver support supplements",
                "category": "supplements",
                "terms": list(CLUSTER_TERMS),
            }
        ],
        # EVERY SELLER, not just the four with an agent, and the widening is a measured repair
        # rather than completeness for its own sake.
        #
        # This block used to be `for host in HOSTED`, on the reading that a snapshot is evidence
        # against a store's CLAIMS and only a store that bids makes any. Under D55 that reading
        # is half the market: `GraphShopRoster` may roster any seller at all, `buyer-roster.json`
        # names eleven agent-less ones outright, and for those the exchange held no snapshot at
        # all. Two consequences, both measured through the served route:
        #
        #  * `catalogue_readings` resolved no identity, so the shortlist slot carried
        #    `product.identity: null` — a row a shopper is shown and the platform cannot NAME.
        #  * `ranking.filters.organic_relevance_reason` reads that same identity, and an absent
        #    one means "unchecked", which is the correct fail-open and left exactly those two
        #    stores unfilterable: `"a walnut coffee table for the lounge"` came back with four
        #    slots before this and TWO after, and the two survivors were the two nameless ones.
        #
        # WHICH STORES, not which products, and that is all this line closed. Naming every
        # seller fixed the nameless row for the fixed six-store `buyer-roster.json`, whose rows
        # point at each store's lead product; it did NOT fix it for the graph route, where the
        # rostered product is whatever answers the query and was usually outside the trimmed
        # window — measured at the old `SNAPSHOT_PRODUCTS_PER_STORE = 60`, 63 of 101 graph-route
        # shortlist slots still carried `identity: null`. That half is closed by the constant
        # itself; see its docstring for the measurement and for what the size now is.
        #
        # The cost is document size, and it is MEASURED below rather than bounded here:
        # `SNAPSHOT_PRODUCTS_PER_STORE` products per store over nineteen stores is 3,241
        # snapshot rows and 3,097,455 bytes against `composition.MAX_DEPLOYMENT_BYTES` of
        # 4 MiB. Nothing else about ranking moves — a store that never bid states no claim, so
        # `verified_claim_ratio` is unchanged, and the attribute vocabulary these snapshots
        # declare is the same six keys the hosted four already declared.
        "catalog": {host: _snapshot(host, catalogs[host], ranked[host]) for host in hosts},
        "checkout_mode": "redirect",
        "trust_url": "http://trust:8084",
    }

    # THE ROSTER ROW CARRIES THE VARIANT, and the row that does NOT move is the reason.
    #
    # `exchange.retrieval.roster.repoint_organic_products` fills `variant_ref` only on rows it
    # MOVES, and it moves a row only when the exchange's own search did not return the product
    # the row pinned. So without this the asymmetry ran the wrong way round: a row whose pinned
    # product was WRONG got a working cart link off the graph, and a row whose pinned product
    # was RIGHT got none at all -- `checkout.provider.default_permalink` had no variant to build
    # the D25 permalink from, so accepting the slot handed the shopper nothing. How many rows are
    # held is a fact about the query: that function records "four of the demo's six" on `milk
    # thistle liver support`, measured when the roster was six rows, and it has not been
    # re-measured at fifteen. The shape does not depend on the count.
    #
    # That function is right to invent nothing for an unmoved row: such a row keeps the CALLER's
    # `list_price`, so pairing it with a variant the PLATFORM chose would state one observation's
    # price beside another observation's variant. It says so, and names this script as what
    # states the row's own variant instead. This is that sentence being true.
    #
    # The value is the row's OWN listing's -- `_catalog_row` put it there off `_priced_variant`,
    # which returns the id and the price of the SAME variant, so `list_price` two lines up and
    # `variant_ref` here are one observation rather than two draws. It is the storefront's own
    # id and nothing else (`ingest.adapters.base.VariantRecord`: over this corpus `seller_sku` is
    # all-digits 5,258 times and equals the native id 0 times, so a SKU-built permalink passes
    # the storefront's `isdigit()` gate and names the WRONG product silently).
    #
    # ABSENT rather than null when the storefront published no usable id, exactly as
    # `_catalog_row` omits it: `RosterEntry.variant_ref` defaults to `None`, `_list_price_bid`
    # omits the key from the offer rather than writing a default, and a key that is sometimes
    # null invites a reader to treat it as always present. Measured over the fifteen rostered
    # storefronts, every lead product publishes one, so the branch is a guard rather than a case.
    roster = []
    for host in ROSTER_HOSTS:
        lead = ranked[host][0]
        listing = catalogs[host][lead]
        entry = {
            "store_id": host,
            "tier": 1,
            "product_ref": lead,
            "list_price": listing["list_price"],
        }
        if listing.get("variant_ref"):
            entry["variant_ref"] = listing["variant_ref"]
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

    # MEASURED, not estimated, and fatal rather than a warning. `exchange.composition` refuses
    # a deployment document over `MAX_DEPLOYMENT_BYTES` outright; a container handed one it
    # refuses keeps every fail-closed default and answers `ranked: []` on a stack where
    # `docker compose ps` reports every row healthy, which is the exact failure the top of this
    # module exists to have fixed once. Growing the roster or the window past the budget must
    # fail HERE, where the person who did it is standing.
    size = len(render(exchange_doc).encode("utf-8"))
    budget = int(MAX_DEPLOYMENT_BYTES * DEPLOYMENT_BYTES_BUDGET)
    if size > budget:
        raise SystemExit(
            f"FATAL: exchange-deployment.json is {size:,} bytes, over the "
            f"{DEPLOYMENT_BYTES_BUDGET:.0%} budget of composition.MAX_DEPLOYMENT_BYTES "
            f"({budget:,} of {MAX_DEPLOYMENT_BYTES:,}). Lower SNAPSHOT_PRODUCTS_PER_STORE "
            f"(currently {SNAPSHOT_PRODUCTS_PER_STORE}, worth about 955 bytes per product per "
            f"store) or take a storefront off the corpus roster."
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
