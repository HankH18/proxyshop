# real-catalogs-broad — a 38-store, 12-category corpus, collected and STAGED

17,409 products recorded verbatim from 38 real storefronts across 12 product categories on
**2026-09-08**, by `scripts/collect_real_catalogs.py`. It is a superset of the committed
corpus in `fixtures/real-catalogs/`: all ten of those supplement storefronts were re-walked
into this collection, so nothing has to be dropped to adopt it.

**It is staged, not adopted.** Nothing in this repository reads this directory. The committed
corpus in `fixtures/real-catalogs/` is untouched and every gate on it still passes. Adopting
this one is a deliberate act with a measured cost, set out under *Promotion* below.

## Why it exists

`"a walnut coffee table for the lounge"` came back with four liver supplements. That is not a
retrieval defect — every store in the committed corpus sells supplements, so there was no
coffee table in it to find. Breadth is a hostname problem, and this is the hostnames.

The corpus now holds `The Lift Off Coffee Table` (floydhome.com), `Nested Coffee Tables`
(branchfurniture.com), `The Semi Side Table` (sabai.design) — 25 table products across three
furniture stores, 153 sofa/sectional products, 161 desk/chair products.

It also holds a distractor set nobody designed: **"walnut" in this corpus is a clothing
colour.** Eighteen of the twenty non-supplement products whose title contains "walnut" are
taylorstitch.com apparel — `The Democratic All Day Pant in Walnut Cord`, `The Hudson Sweater
in Walnut`, `The Utility Shirt in Walnut Double Cloth`. No coffee table here is made of
walnut. The demo query as literally worded therefore has a defensible answer (a coffee table)
and a strong wrong one (walnut-coloured trousers), which is a far harder discrimination than
the query had before, when the corpus contained neither.

## What is in it

| category | stores on roster | stores with inventory | products | share |
|---|---:|---:|---:|---:|
| apparel | 4 | 3 | 6,655 | 38.2% |
| supplements | 10 | 10 | 3,092 | 17.8% |
| sports | 3 | 3 | 2,867 | 16.5% |
| outdoor | 4 | 4 | 1,896 | 10.9% |
| coffee | 4 | 4 | 741 | 4.3% |
| furniture | 7 | 3 | 714 | 4.1% |
| home-kitchen | 4 | 2 | 564 | 3.2% |
| pet | 5 | 4 | 535 | 3.1% |
| beauty | 3 | 2 | 156 | 0.9% |
| food | 3 | 2 | 112 | 0.6% |
| tools | 4 | 1 | 77 | 0.4% |
| electronics | 2 | **0** | **0** | 0.0% |
| **total** | **53** | **38** | **17,409** | |

Electronics is empty: both candidates (nomadgoods.com, peakdesign.com) answered
`/products.json` with HTTP 404. The category is on the roster and produced nothing, which is
recorded rather than quietly dropped — `collection.json` lists it with zero products.

Four stores exceed 1,000 products: taylorstitch.com 3,805, titan.fitness 2,564,
marinelayer.com 2,556, cotopaxi.com 1,432. See *What promotion breaks*.

## What was collected and what refused

38 of 53 roster hosts served a catalogue. The other 15 are recorded outcomes, not holes, and
`collection.json` carries each one's reason:

| outcome | hosts |
|---|---|
| `/products.json` → 404 | burrow.com, polyandbark.com, carawayhome.com, taylortoolworks.com, omsom.com, nomadgoods.com, peakdesign.com |
| `/products.json` → 403 | madeincookware.com, youthtothepeople.com |
| robots.txt unreadable (403 / 429 / dropped connection) | industrywest.com, bombas.com, katzmosestools.com |
| `off_platform_control`, expected miss, behaved | homedepot.com, chewy.com, thuma.co |

All three deliberate misses answered 404, so the control did not fail open —
`totals.off_platform_controls_that_answered` is 0.

bombas.com and katzmosestools.com were each walked **twice**, minutes apart, and refused
identically both times; their records stay marked retryable, which is honest. Furniture is the
category that lost the most: three of six real candidates refused, so 7 roster hosts produced
3 stocked ones.

## Politeness — what this collection actually cost the merchants

Measured, from `collection.json` and the run log:

* **158 HTTP requests were actually made** to 53 hosts: 153 in the main pass (52 hosts × 1
  robots + 101 catalogue pages), 2 in a one-host plumbing check beforehand, and 3 in a second
  pass that re-walked three hosts. The main pass took **4 minutes 1 second** of wall clock.
  `collection.json` reports `requests_made: 155`, which is the count the *record* holds — it
  keeps one record per store, so the three re-walks replaced their store's earlier record
  rather than adding to it. 158 is the number those merchants saw.
* **Strictly serial.** One host at a time, one request at a time; no concurrency anywhere.
* **Zero 429s attributable to this crawler's pacing.** One host (bombas.com) answered 429 on
  its own robots.txt on both attempts; no host this run had never touched ever returned one,
  which is the signature of the IP-wide block the feasibility study saw under parallelism.
* robots.txt fetched first for every host and parsed with `protego`; a declared `Crawl-delay`
  widens the interval and never narrows it. ≥2.0 s between requests to one host.
* The research User-Agent, unspoofed, with a contact address.
* **No retries.** Every 403, 404 and 429 above is a recorded outcome.

## Verification

Every number in `collection.json` was checked back against the stored bytes, not against the
run's own log:

* every products/provenance file matches its recorded SHA-256 and its recorded on-disk size;
* every product line matches its provenance row's digest, and the rows are index-aligned;
* **all 90 pages reassemble byte-for-byte into the response the storefront served** — the
  property that lets the recording be replayed through the live crawl path;
* no duplicate product id survived the build (`duplicates_dropped` is 0);
* `per_store_counts`, `totals.products`, `totals.stores_collected` and every `categories`
  bucket agree with the files on disk.

Compression is **9.6x** — 133,575,173 raw JSONL bytes stored as 13,954,284.

Re-walking the incumbent ten reproduced their committed counts exactly, with one exception:
**livemomentous.com is 89 products here and 90 in the committed corpus.** The storefront
changed by one product between 2026-09-07 and 2026-09-08. That is the corpus being a
point-in-time snapshot, working as designed.

## Promotion — offline, and it costs the merchants nothing

The raw response bodies are preserved outside the repository at
`../proxyshop-worktrees/rc-raw-broad`. `build` never opens a socket, so adopting this corpus
is a re-run of `build` alone:

```
.venv/bin/python scripts/collect_real_catalogs.py build \
    --raw-dir ../proxyshop-worktrees/rc-raw-broad \
    --out fixtures/real-catalogs
```

Any narrower shape — fewer stores, a different category mix — is also just a `build` away,
by editing the roster files and re-running `fetch` (resume reuses every record on disk).

### What promotion breaks — measured, not predicted

| gate / consumer | why it fails | current → new |
|---|---|---|
| `test_all_ten_stores_are_accounted_for` | exact set equality against `ALL_HOSTS` | 10 → 53 hosts |
| `test_the_per_store_and_total_product_counts_are_pinned` | exact dict equality against `RECORDED_COUNTS` | 3,093 → 17,409, and livemomentous 90 → 89 |
| `test_every_page_reassembles_byte_for_byte_into_the_response_that_was_served` | ends on `assert pages == 18` | 18 → 90 pages |
| `test_the_corpus_stays_small_enough_to_live_in_git` | `assert total < 5 MB` | 2.7 MB → 14.1 MB |
| `test_every_store_contributes_a_catalogue_rather_than_a_shelf` | `assert min(counts.values()) >= 50` | smallest store 89 → flybyjing.com at 32 |
| `DEMO_QUERIES` gates (`stores_with` / `stores_without`) | exact store counts over the whole corpus | 10-store denominators → 38 |
| `scripts/build_demo_deployment.py` → `exchange-deployment.json` | `composition.MAX_DEPLOYMENT_BYTES` is 4 MiB | see below |

`ranking.verification.MAX_CATALOG_PRODUCTS` (1,000) is **not** breached, because
`SNAPSHOT_PRODUCTS_PER_STORE` already trims each store to 1,000 before the document is built.
The ceiling that does break is the document's total size. Measured: the current
`deploy/demo/exchange-deployment.json` is 2,950,154 bytes for 3,086 priced products — 956
bytes per product, at 70% of the 4 MiB ceiling. Trimming this corpus at 1,000 per store leaves
11,052 products, projected at **10.6 MB, 2.5x over the ceiling**. The 4 MiB document can hold
roughly **4,387 products**, i.e. about **115 per store across 38 stores**. So promoting this
corpus forces a real decision about what reaches the exchange snapshot — and a per-store
window near 115 is the size that was measured to cost shoppers a product NAME on 62% of
graph-route shortlist slots when it was 60.

### Two gates that will pass, and should not be trusted for it

`test_the_corpus_spans_many_product_categories` and `test_no_single_category_dominates_the_corpus`
pass on **both** corpora — measured, by evaluating their own `BREADTH_PROBES` against each:

| | committed (10 supplement stores) | broad (38 stores, 12 categories) |
|---|---|---|
| probes below the 8-hit floor | none | none |
| distinct matched products (needs ≥400) | 616 | 645 |
| any category over 15% | none | none |
| liver-support share (needs ≤5%) | 1.13% | 0.25% |

All sixteen `BREADTH_PROBES` are supplement vocabulary — *creatine, magnesium, collagen,
ashwagandha, cholecalciferol, withania*. The gate's docstring calls them "sixteen unrelated
categories", but they are sixteen sub-categories of one category, so the gate scores a corpus
with zero furniture exactly as broad as one with 714 furniture products. That is why an
all-supplement corpus could carry a passing anti-narrowing gate while the demo returned liver
capsules for a coffee table. **Whoever promotes this corpus should add probes in the
vocabulary of the categories that were missing** — coffee table, sofa, tent, skillet, barbell,
leash — or the gate will keep certifying breadth it cannot see.

## Provenance of the roster

`../real-catalogs/incumbent-hosts.txt` (the built-in ten, category `supplements`) and
`../real-catalogs/candidate-hosts.txt` (43 breadth candidates, 11 categories) were walked as
one roster: `--hosts-file` is repeatable. `collection.json` records, per store, which file and
line it came from and the curator's note.
