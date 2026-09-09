# `fixtures/real-catalogs-demo/` — the curated demo roster

**Derived, not collected.** Every byte of every store file in `stores/` is a verbatim
copy of the same file in `fixtures/real-catalogs-broad/stores/`, checked by SHA-256
against that corpus's own manifest. Nothing here opened a socket. Reproduce with:

```
./.venv/bin/python scripts/build_demo_corpus.py
./.venv/bin/python scripts/build_demo_corpus.py --check   # report drift, write nothing
```

19 storefronts, 4,903 products, 5 categories, 5.05 MB on disk.

## Why this directory exists

`fixtures/real-catalogs/` is ten supplement storefronts, which is why *"a walnut
coffee table for the lounge"* came back with liver capsules — there was no coffee
table in it to find. `fixtures/real-catalogs-broad/` has the coffee tables and 38
stores, and the exchange's `catalog` snapshot is capped at 4 MiB across every store,
so at 38 stores each one's window is about 137 products. This roster is the middle:
the ten incumbents plus nine organic storefronts, wide enough to answer a furniture,
coffee, outdoor or kitchen query and narrow enough that each store's window holds
what a shopper asks it for.

## The roster

| host | category | products | role |
| --- | --- | ---: | --- |
| `gaiaherbs.com` | supplements | 111 | incumbent |
| `bulksupplements.com` | supplements | 805 | incumbent |
| `nutricost.com` | supplements | 755 | incumbent |
| `oregonswildharvest.com` | supplements | 189 | incumbent |
| `toniiq.com` | supplements | 103 | incumbent |
| `doublewoodsupplements.com` | supplements | 201 | incumbent |
| `purebulk.com` | supplements | 550 | incumbent |
| `paradiseherbs.com` | supplements | 90 | incumbent |
| `livemomentous.com` | supplements | 89 | incumbent |
| `nakednutrition.com` | supplements | 199 | incumbent |
| `floydhome.com` | furniture | 173 | organic (promoted) |
| `branchfurniture.com` | furniture | 220 | organic (promoted) |
| `sabai.design` | furniture | 321 | organic (promoted) |
| `deathwishcoffee.com` | coffee | 142 | organic (promoted) |
| `vervecoffee.com` | coffee | 175 | organic (promoted) |
| `nemoequipment.com` | outdoor | 97 | organic (promoted) |
| `hyperlitemountaingear.com` | outdoor | 119 | organic (promoted) |
| `fromourplace.com` | home-kitchen | 123 | organic (promoted) |
| `fellowproducts.com` | home-kitchen | 441 | organic (promoted) |

### Why each promoted store

* **`floydhome.com`** — furniture. 173 products, 148 priced, 38 whole-word furniture-probe hits (sofas, sectionals, beds, tables). The cleanest of the three: 29 of its rows are Serviceability or Swatch and the rest are furniture.
* **`branchfurniture.com`** — furniture. One of only two storefronts in the entire broad corpus that stocks a COFFEE TABLE, which is the query this roster exists to answer. 220 products, 207 priced, 48 furniture-probe hits. Replays to zero products under the pre-fix ingest response ceiling, which is why that fix and this roster land together.
* **`sabai.design`** — furniture. 321 products, 296 priced, 151 furniture-probe hits — the deepest furniture shelf in the corpus by a wide margin. 145 of its rows are components, swatches, protection plans and gift cards; taken anyway because the other 176 are sofas, sectionals, ottomans and side tables and no other store carries that depth.
* **`deathwishcoffee.com`** — coffee. 142 products, 40 coffee-probe hits — the strongest coffee vocabulary in the corpus (roast, cold brew, espresso). 1 junk row.
* **`vervecoffee.com`** — coffee. 175 products, 15 coffee-probe hits, 1 junk row. Picked over onyxcoffeelab.com and counterculturecoffee.com, which are equally clean but name their coffees after farms — 4 probe hits each — so they read as breadth this corpus cannot demonstrate.
* **`nemoequipment.com`** — outdoor. 97 products, 63 outdoor-probe hits and ZERO junk rows — tents, sleeping bags, sleeping pads. The purest catalogue in the broad corpus.
* **`hyperlitemountaingear.com`** — outdoor. 119 products, packs and shelters. Picked over cotopaxi.com (1,432 products, 51 outdoor hits, 627 APPAREL hits and only 711 distinct titles among 1,431 priced rows) because cotopaxi would have made outdoor the largest category in this corpus on the strength of colourway duplicates of t-shirts.
* **`fromourplace.com`** — home-kitchen. 123 products, 123 distinct titles, 1 junk row, 14 home-kitchen-probe hits. The clean one.
* **`fellowproducts.com`** — home-kitchen. 441 products, of which 139 are Replacement Part or Internal. Taken as the SECOND kitchen store rather than the first: home-kitchen is the thinnest probe family in this roster and fromourplace.com alone scores 14, under the broad corpus's measured floor of 15. With this store the family scores 32.

## What is NOT here

**No store promoted here is sponsored.** D55: a scraped shop is an ORGANIC result
carrying a pitch the platform wrote; an in-network shop is SPONSORED and buys the
right to make its own case. The four hosted store agents — `gaiaherbs.com`,
`toniiq.com`, `paradiseherbs.com`, `oregonswildharvest.com` — are the sponsored ones
and stay the only sponsored ones. Nothing promoted here gets a `bid_endpoint`, a
discount envelope or a store agent.

The gates are `fixtures/tests/test_real_catalogs_demo.py`.
