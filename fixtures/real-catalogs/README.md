# `fixtures/real-catalogs/` — ten real storefront catalogues, recorded whole

> **This is a POINT-IN-TIME SNAPSHOT of other companies' public catalogues**, taken on
> **2026-09-07**. Every price, title, tag and stock flag in here belonged to a real business on
> that day and has been drifting ever since. Nothing downstream may pin a dollar amount, a
> product count for a single store's shelf, or a specific SKU: assert **shape and
> relationships** — "a price spread exists", "these stores carry none of this" — and the corpus
> keeps being true as the world moves. See "Writing tests against this" below.

## What this is

Every catalogue byte in this repository used to be synthetic. `fixtures/catalog/coffee.json` is
a *generator config*: four product families, four products per store, one uniform attribute
template, one category, prices drawn from a band. Entity resolution and claim extraction had
therefore never met an empty `product_type`, a hundred-variant product, a tag that is a typo, or
two merchants naming one active ingredient differently.

This is the real thing: **3,093 products across 10 Shopify storefronts, recorded verbatim.**

## Entire catalogues, not a category sample — and that is the point

An earlier draft of this corpus sampled each store *biased toward the liver-supplement
category*. That was wrong, and it was caught before a single request went out.

A category-biased corpus builds a system that can only answer one question, and it silently
deletes the hardest and most important part of retrieval: **discriminating relevant inventory
from a large body of irrelevant inventory.** A graph in which almost everything matches proves
nothing about matching. The irrelevant stock is not overhead — it *is* the test.

So the collector paginates `?limit=250&page=N` until a page comes back short or empty and keeps
**every product on every page**. Liver support is now one demo query among several, and it is
**1.1% of the corpus** (35 of 3,093 products). What is in the other 98.9% includes protein
powders, creatine, electrolytes, collagen, bulk herbs, gummies, sweeteners — and hats, t-shirts,
shaker bottles, gift cards, a milligram scale and one product literally named
`Bold Test Product`, because that is what a real catalogue contains.

Two of the ten stores — **livemomentous.com** and **nakednutrition.com** — carry no
liver-support inventory at all; they answer a milk-thistle search with whey and protein stacks.
They are kept deliberately: **a shortlist that never has to reject anybody demonstrates
nothing.** For a *protein* query the roles invert — those two are among the six stores that have
it, and gaiaherbs/oregonswildharvest/toniiq/doublewood have none. Collecting whole catalogues is
what makes that inversion available.

## Per-store counts, and truncation

| store | role | products | pages fetched | robots.txt | truncated? |
|---|---|---:|---:|---|---|
| gaiaherbs.com | relevant | 111 | 1 | 200, allows `/products.json` | no |
| bulksupplements.com | relevant | 805 | 4 | 200, allows `/products.json` | no |
| nutricost.com | relevant | 755 | 4 | 200, allows `/products.json` | no |
| oregonswildharvest.com | relevant | 189 | 1 | 200, allows `/products.json` | no |
| toniiq.com | relevant | 103 | 1 | 200, allows `/products.json` | no |
| doublewoodsupplements.com | relevant | 201 | 1 | 200, allows `/products.json` | no |
| purebulk.com | relevant | 550 | 3 | 200, allows `/products.json` | no |
| paradiseherbs.com | relevant | 90 | 1 | 200, allows `/products.json` | no |
| livemomentous.com | negative control (liver) | 90 | 1 | 200, allows `/products.json` | no |
| nakednutrition.com | negative control (liver) | 199 | 1 | 200, allows `/products.json` | no |
| **total** | | **3,093** | **18** | 10 hosts, 0 disallowed, 0 skipped | **0 truncated** |

**No catalogue in this corpus is truncated.** Every store's walk ended on a short page — the
natural end of its catalogue — not on the runaway guard. The guard is `--max-pages 40`
(10,000 products per host); had any store hit it, that store's row would say `truncated: true`
with a reason in `collection.json`, and this README would say so here rather than presenting a
partial catalogue as complete.

## Politeness — these are real businesses

The whole collection cost **28 HTTP requests**: 10 `robots.txt` fetches and 18 catalogue pages.
One pass, no resumed stores, nothing re-requested — recompute it straight off the record:

```sh
python -c "import json;m=json.load(open('fixtures/real-catalogs/collection.json'));\
print(sum(len([f for f in s['fetches'] if f['status']!=-1])+bool(s['robots']) for s in m['stores']))"
```

* **`robots.txt` is fetched per host before anything else** and parsed with `protego`. A host
  that disallows the path is skipped, with the skip and its reason recorded. There is no
  override flag, deliberately. All 10 hosts returned HTTP 200 and allowed `/products.json`.
* **User-Agent:** `ProxyShopBot/0.1 (catalog research; contact: hank.holcomb@challenger.gauntletai.com)`
* **At least 2 seconds between requests to the same host, and that is a FLOOR rather than a
  default.** `scripts/collect_real_catalogs.py` puts `--min-interval` through
  `is_polite_interval` in `parse_args`, and `PolitenessBudget` runs the same predicate on
  itself, because the CLI is not its only caller. The predicate requires the value to be
  **finite** as well as at or above `MIN_INTERVAL_FLOOR`: both checks used to be a bare `<`
  comparison, every comparison against NaN is False, and `--min-interval nan` therefore cleared
  both — after which `spend`'s own `if wait > 0` was False as well and the walk never paused at
  all, under a manifest declaring 2.0 s. Before that, `--min-interval 0` was accepted in
  silence. A declared `Crawl-delay` widens the interval and never narrows it, and a non-finite
  one is ignored rather than honoured. `www.` and the bare host share one budget, so a redirect
  cannot hand the same machine two independent rate limits.

  ```sh
  PROXYSHOP_WORKER=14 .venv/bin/python -m pytest scripts/tests/test_collect_real_catalogs.py \
      -k "interval or floor or crawl_delay" -q
  ```
* **A hard per-host cap** of 40 pages and 45 requests as a runaway guard.
* **No retry loop.** Within a run nothing is re-requested: a 403 or 404 on `/products.json`, or
  a 429, ends that store's walk and is recorded as its outcome. **Across runs, `--resume` does
  re-walk a host whose recorded outcome was retryable** — a fresh run started by a person, not
  a loop against a host that just answered. A 403 on *robots.txt* is retryable too, and
  `../real-catalogs-broad/README.md` sets out why that one is not a refusal.

  This collection was a single pass with nothing resumed. The 2.0.0 manifest committed here
  predates the `run` block and says `retries: none` outright; a rebuild by the current
  collector writes a posture that describes the *collector* — both halves — and states this
  run's own facts in `run.reused_from_earlier_runs`, which is what
  `test_nothing_in_this_collection_was_ever_re_requested` reads. It used to read the word
  "none", which meant an honest single-pass rebuild of this very corpus would have failed it.
* **Public catalogue data only.** `/products.json` carries no personal data and the collector
  goes looking for none.

## Layout

```
collection.json                            the collection run: politeness, robots decisions,
                                           every fetch, per-store counts, digests
stores/<host>.products.jsonl.gz            every product, one per line, VERBATIM bytes
stores/<host>.provenance.jsonl.gz          row-aligned: where each product's bytes came from
```

It is `collection.json` and **not** `manifest.json` on purpose: `fixtures/manifest.json` is this
repository's single approval-bearing manifest, and `fixtures/tests/test_manifest.py` fails on any
second file of that name under `fixtures/` — two files called "manifest" make every reader's
ground truth ambiguous. This one records a collection run, which is a different kind of thing.

`.gz` because this is a git repository: **20.4 MB of JSON compresses to 2.70 MB** at level 9,
with `mtime=0` so a re-build of the same pages is byte-identical and produces no spurious diff.
That is a *storage* decision and nothing more — **no product was dropped and no field was
trimmed.** What is stored is the whole catalogue.

### The records are verbatim

Each line of a `products.jsonl.gz` is the **exact byte slice** the storefront served for that
product. The collector lifts it out of the response with `json.JSONDecoder.raw_decode`, which
reports where each value ended, and never re-serialises it. Nothing is normalised, cleaned,
lower-cased or repaired on the way in: **the mess is the specimen**, and normalisation belongs
downstream in entity resolution where it can be tested.

### Provenance, and why it is load-bearing

`stores/<host>.provenance.jsonl.gz` is row-aligned with the products file and carries, per
product: `source_url`, `http_status`, `fetched_at`, `response_sha256` (the digest of the whole
page), `page`, `byte_span` (where in that page the record sits) and `sha256` (the digest of the
product's own bytes).

This is not decoration. The graph **refuses a material fact with no `SUPPORTED_BY -> Source`
edge**; `candidate_shops()` drops an unsourced shop from the roster entirely and drops the
*price* of a shop whose offer chain is unsourced. A corpus that cannot say where a fact came
from yields a graph that looks full and rosters empty.

## What the mess actually looks like (measured on this corpus, not assumed)

* **`product_type` is not a taxonomy.** 69 distinct non-empty values and **43.3% empty**
  (1,340 of 3,093). It holds dosage forms (`Capsules`, `Tablets`, `Powder`), marketing
  categories (`Herbal Supplement`, `Vitamins & Supplements`), a flavour (`Chocolate Protein`),
  merchandise (`Hat`, `T-Shirt`, `Shaker Bottle`), the word `Product`, a leaked internal
  `Bold Test Product`, and the empty string. Two stores (toniiq, paradiseherbs) leave it empty
  on **100%** of products; gaiaherbs and oregonswildharvest fill it on every one. Code that
  switches on `product_type` is broken before it is written.
* **Tags are an operational dumping ground.** 28,671 tag instances, 3,231 distinct. Real
  product facts sit in the same list as `back_in_stock` (106), `discountable` (89), app
  namespaces (`__ppblock:AU`, `__with:powdered-peanut-butter`, `__related:...` — 192 instances),
  internal SKU-ish strings (`Els PW 8602`, `TC-PF051000`), and outright typos: `bee ropoole`,
  `bee polon`, `beepoll`, `beet rook`, `beetroon`.
* **`body_html` and tags are inversely rich.** purebulk averages **4,649 characters** of prose
  and **0.92 tags**; paradiseherbs 715 characters and **zero** tags; gaiaherbs **141**
  characters and **15.4 tags**; bulksupplements 440 characters and 23.4 tags. A naive
  "read `body_html`" extractor returns almost nothing for a third of the corpus, and a naive
  "read the tags" extractor returns nothing at all for another store.
* **One product is not one offer.** The deepest product carries **100 variants**; the richest
  carries **81 image records**. 1,440 of 3,093 products have exactly one variant. Code that
  reads `variants[0]` picks an arbitrary size at an arbitrary price.
* **Prices are decimal STRINGS** — all 9,667 of them. Arithmetic on the raw field is a bug.
* **`compare_at_price` exists natively** on 653 of 9,667 variants: a discount signal already in
  the catalogue on the merchant's own terms, and null the other 93% of the time, so nothing may
  rely on it.
* **Stock is mixed**: 7,095 variants available, 2,572 not.

## Demo queries this corpus can actually answer

Each has a genuine spread and, crucially, stores that must be **thrown out**:

| query | products | stores with inventory | stores with none |
|---|---:|---:|---:|
| liver support | 35 | 8 | 2 (livemomentous, nakednutrition) |
| protein | 113 | 6 | 4 (doublewood, gaia, oregonswildharvest, toniiq) |
| creatine | 49 | 8 | 2 (gaia, oregonswildharvest) |
| magnesium | 92 | 8 | 2 (gaia, oregonswildharvest) |
| collagen | 47 | 7 | 3 (gaia, oregonswildharvest, toniiq) |
| electrolytes | 21 | 5 | 5 |
| probiotics | 12 | 2 | 8 |

Note that the "relevant" and "negative control" labels are **per query, not per store**. There
is no globally irrelevant store in here; there are stores that stock the thing and stores that
do not, and which is which changes with the question.

## Writing tests against this

`fixtures/tests/test_real_catalogs.py` is the worked example. Two rules it obeys:

1. **Nothing opens a socket.** The suite runs offline (D3/C9) and the root `conftest.py` arms
   `pytest-socket`. Collection is a by-hand operation; the tests only read bytes off disk, and
   `test_loading_the_corpus_opens_no_socket` proves that rather than asserting it.
2. **No assertion names a dollar amount.** Every economic assertion is a *ratio* or an
   *ordering* — "the cheapest and dearest store differ by more than 3x" — never
   "milk thistle costs $5.75". Live prices rot within weeks; ratios and "this store stocks
   none of it" survive.

## Where the roster comes from — and the one thing wrong with this corpus

Every store above sells supplements. That is this corpus's single real limitation, and it is
visible in the demo: **`"a walnut coffee table for the lounge"` returns four liver
supplements.** Retrieval is working; there is no coffee table in the corpus to find. Breadth is
a *hostname* problem, so the roster now lives in reviewable files rather than a tuple in the
collector — and the hostnames have since been walked into `../real-catalogs-broad/`, which
holds six of them:

| file | what it is |
|---|---|
| `incumbent-hosts.txt` | these ten, restated in the roster format. A test asserts it matches the collector's built-in `RELEVANT_HOSTS`/`NEGATIVE_CONTROL_HOSTS` exactly, so the two cannot drift. |
| `candidate-hosts.txt` | **43** direct-to-consumer storefronts across **11 categories** — furniture, coffee, apparel, outdoor, pet, home/kitchen, tools, food, beauty, sports, electronics — with three deliberate misses. **All 43 were collected on 2026-09-08** into `../real-catalogs-broad/`; 28 served a catalogue, 15 did not, and every outcome is on the record. That corpus is STAGED, not adopted — this one is untouched. |

The format is one host per line with its **category** beside it, `#` for comments:

```
burrow.com            furniture
homedepot.com         tools     off_platform_control   # measured: not a Shopify store
```

The category is not decoration. `collection.json` carries a `categories` block giving the
stores and product count per category, so a corpus that has quietly narrowed back into one
category can be *seen* to have done so rather than being taken on trust.

`off_platform_control` is the third role, beside `relevant` and `negative_control`. It marks a
host measured **not** to serve `/products.json` at all, so that a skip in `collection.json`
can be read: without it, "this merchant does not serve the endpoint" and "the collector broke"
leave the same record. The collector counts them separately and shouts if one of them answers.

## Re-collecting

The collector is two phases on purpose. `fetch` is the only phase that opens a socket;
`build` derives the committed corpus from the saved response bodies and never fetches, so any
later decision about the corpus's *shape* costs those businesses nothing.

```sh
# phase one — the only network pass. The ten stores above, unchanged:
.venv/bin/python scripts/collect_real_catalogs.py fetch --raw-dir /tmp/rc-raw

# ...or a broader roster. --hosts-file is repeatable, so the incumbents and the candidates
# can be walked as ONE roster and the corpus grows without losing the stores its gates pin:
.venv/bin/python scripts/collect_real_catalogs.py fetch --raw-dir /tmp/rc-raw \
    --hosts-file fixtures/real-catalogs/incumbent-hosts.txt \
    --hosts-file fixtures/real-catalogs/candidate-hosts.txt

# phase two — offline, repeatable, deterministic
.venv/bin/python scripts/collect_real_catalogs.py build \
    --raw-dir /tmp/rc-raw --out fixtures/real-catalogs
```

`--no-compress` writes plain `.jsonl` for eyeballing. `--only <host>` narrows whichever roster
is in play, and a `--only` naming a host the roster does not carry is an **error** — it used to
be filtered against the built-in tuple and nothing else, so `--only allbirds.com` produced an
empty roster, fetched nothing and exited 0.

### Resume — `fetch` is interruptible now

`fetchlog.json` used to be written once, after the last store. A crash at store 900 threw away
hours of deliberately slow fetching *and* re-hit 900 merchants on the retry. Now each store's
record lands in `<raw-dir>/<host>/store.json` the moment that store finishes, the log is
rewritten after every store, and **`--resume` is on by default**: re-running the same command
picks up where it stopped.

A store is skipped only when all four of these hold, and the run says on stdout which one
failed when they do not:

1. the record exists and parses — it is written temp-file-plus-rename, so it is never half-there;
2. it ended on an **answer** — a short page, a page cap, a robots decision, a 403/404. A
   transport error, a 429, a 5xx, an empty body, an unparseable page or an unreadable
   `robots.txt` are *retryable* and the store is walked again. An outcome the collector does
   not recognise is retryable too: re-walking a store is the cheap error, skipping one that
   never finished is not. The read path **re-judges `walk_outcome`** rather than trusting the
   `complete` flag written beside it, and then requires the two to agree — it used to trust the
   flag, so a record saying `{"complete": true, "walk_outcome": "interrupted"}` was reused with
   no request and the three-way protection above never ran;
3. the settings that decide the *result* are unchanged — page size, page cap, request cap,
   collector version, User-Agent. Raise `--max-pages` and every record taken under the old cap
   is invalidated, so a truncated catalogue cannot survive as "already collected";
4. every page file it names still exists **and still matches its recorded digest**.

Point 4 is the one the mechanism exists for. A page truncated mid-product still parses as a
file and still has the right name; only the digest knows. Measured, on a real page: truncating
`onyxcoffeelab.com/page-001.json` by five bytes made the next run print
`re-walking: page file … no longer matches its recorded digest` and fetch it again.

`--no-resume` forces a clean re-walk of everything — and a re-walk that FAILS no longer costs
anything. The stale-page sweep used to run before the robots fetch, so a `--no-resume` run made
while the egress IP happened to be blocked emptied the scratch directory host by host while
collecting nothing. Pages are now swept only once a record that does not claim them is on disk,
and a retryable walk cannot replace a finished record: the failed walk is parked beside it as
`store.last-failed-walk.json`.

**A re-collection changes the counts, and that is meant to be noticed.** The per-store totals
above and the pinned counts in `fixtures/tests/test_real_catalogs.py` will fail until they are
updated with the new measurements — deliberately, so that a corpus cannot quietly narrow, lose
a store, or drift into being a single-category sample again without somebody looking at the
numbers and saying so.
