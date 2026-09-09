# real-catalogs-broad — 38 storefronts, 12 categories on the roster, 11 with inventory

> **This is a POINT-IN-TIME SNAPSHOT of other companies' public catalogues**, taken on
> **2026-09-08**. Every price, title, tag and stock flag belonged to a real business on that day
> and has been drifting ever since. Nothing may pin a dollar amount or a single shelf's depth:
> assert **shape and relationships**, and the corpus keeps being true as the world moves.

17,409 products recorded verbatim from 38 real storefronts by
`scripts/collect_real_catalogs.py`. It is a superset of the committed corpus in
`fixtures/real-catalogs/`: all ten of those supplement storefronts were re-walked into this
collection, so nothing has to be dropped to adopt it.

**It is staged, not adopted.** No application code reads this directory — the exchange and the
demo still run on `fixtures/real-catalogs/`. What it does have is gates:
`fixtures/tests/test_real_catalogs_broad.py` pins every count, every digest, every recorded
miss and the breadth claim, so the corpus can no longer be corrupted, truncated or silently
narrowed without something going red. Adopting it is a deliberate act with a measured cost, set
out under *Promotion*.

**Every number in this file is reproducible by a command stated beside it.** Where a number
could not be reproduced off the bytes on disk, it has been removed rather than adjusted.

## Why it exists

`"a walnut coffee table for the lounge"` came back with four liver supplements. That is not a
retrieval defect — every store in the committed corpus sells supplements, so there was no coffee
table in it to find. Breadth is a hostname problem, and this is the hostnames.

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

```sh
python -c "import json;m=json.load(open('fixtures/real-catalogs-broad/collection.json'));\
[print(f\"{k:14s} {len(v['stores']):2d} {len(v['stores_with_inventory']):2d} {v['products']:6d}\") \
for k,v in m['categories'].items()]"
```

**Twelve categories are on the roster; eleven have inventory.** `totals.categories` counts the
eleven, because it counts categories with a stocked store, and the title of this file says the
same. Electronics is the twelfth: both candidates (nomadgoods.com, peakdesign.com) answered
`/products.json` with HTTP 404, and the category is recorded with zero products rather than
quietly dropped.

Four stores exceed 1,000 products: taylorstitch.com 3,805, titan.fitness 2,564,
marinelayer.com 2,556, cotopaxi.com 1,432. See *What promotion breaks*.

## What was collected and what refused

38 of 53 roster hosts served a catalogue. The other 15 are recorded outcomes, not holes, and
`collection.json` carries each one's reason and `walk_outcome`:

| outcome | hosts |
|---|---|
| `/products.json` → 404 | burrow.com, polyandbark.com, carawayhome.com, taylortoolworks.com, omsom.com, nomadgoods.com, peakdesign.com |
| `/products.json` → 403 | madeincookware.com, youthtothepeople.com |
| robots.txt unreadable (403 / 429 / dropped connection) | industrywest.com, bombas.com, katzmosestools.com |
| `off_platform_control`, expected miss, behaved | homedepot.com, chewy.com, thuma.co |

All three deliberate misses answered 404, so the control did not fail open —
`totals.off_platform_controls_that_answered` is 0. **No merchant in this corpus published a
rule against us**: not one store's `walk_outcome` is `robots_disallowed`. The three unreadable
robots files are our bad minute, not their stated policy, and are recorded as such — their
records stay marked retryable, which is honest.

Furniture is the category that lost the most: 7 roster hosts produced 3 stocked ones.

```sh
python -c "import json;m=json.load(open('fixtures/real-catalogs-broad/collection.json'));\
[print(s['host'], s['walk_outcome']) for s in m['stores'] if s['skipped']]"
```

## Politeness — what this collection cost the merchants

Measured, from `collection.json`, and recomputable from it:

* **155 HTTP requests are accounted for** across 53 hosts — one robots.txt fetch each, plus 102
  catalogue pages. That is `totals.requests_recorded`, and it is a **floor**, not a total: the
  scratch record is per store, so a host walked more than once contributes only what its
  surviving record carries. Records written by collector 2.2.0 and later carry
  `requests_charged`, which accumulates across walks; these were written by 2.1.0, which did
  not, so a re-walk earlier in the collection is not in the number. An earlier draft of this
  file put the true figure at 158 by counting from the session's own scrollback; that number
  cannot be read off any artifact and has been removed.

  ```sh
  python -c "import json;m=json.load(open('fixtures/real-catalogs-broad/collection.json'));\
  print(sum(s.get('requests_charged', len([f for f in s['fetches'] if f['status']!=-1])+bool(s['robots'])) \
  for s in m['stores']), m['totals']['requests_recorded'])"
  ```

* **Strictly serial.** One host at a time, one request at a time; no concurrency primitive
  exists in the collector.
* **≥2.0 s between requests to one host, enforced in code** rather than defaulted. The collector
  refuses a `--min-interval` below `MIN_INTERVAL_FLOOR`, in `parse_args` and again in
  `PolitenessBudget`, and each store records the interval it was walked at in
  `robots.crawl_delay_seconds`. `--min-interval 0` used to be accepted in silence.
* robots.txt fetched first for every host and parsed with `protego`; a declared `Crawl-delay`
  widens the interval and never narrows it.
* The research User-Agent, unspoofed, with a contact address.
* **No retry loop.** Within the run, no URL was requested twice — checkable, and checked by
  `test_the_retry_posture_says_what_a_resume_actually_does`. **Across runs, `--resume` does
  re-walk a host whose recorded outcome was retryable** (429, 5xx, transport error): 51 of these
  53 stores came from an earlier run, and bombas.com and katzmosestools.com would be asked again
  by the next `fetch`. `politeness.retries` says both halves; it used to say "none" flat out,
  which was false the day resume landed.
* **Zero 429s attributable to this crawler's pacing.** One host (bombas.com) answered 429 on its
  own robots.txt; no host this run had never touched returned one, which is the signature of the
  IP-wide block the feasibility study saw under parallelism.

## Verification

Every number in `collection.json` was checked back against the stored bytes, not against the
run's own log. `fixtures/tests/test_real_catalogs_broad.py` is that check, kept:

* every products/provenance file matches its recorded SHA-256, and no file in `stores/` is
  undeclared;
* every product line matches its provenance row's digest, and the rows are index-aligned;
* **all 90 pages reassemble byte-for-byte into the response the storefront served** — the
  property that lets the recording be replayed through the live crawl path;
* no duplicate product id survived the build (`duplicates_dropped` is 0);
* `per_store_counts`, `totals.products`, `totals.stores_collected` and every `categories`
  bucket agree with the files on disk;
* `ingest.adapters.recorded.RecordedCorpus` — the repository's own loader, the one that feeds
  `RecordedTransport` — opens this directory, sees all 38 stores and reports no integrity
  problems.

Compression is **10.1x**: 140,688,214 raw JSONL bytes stored as 13,954,284.

```sh
python -c "import json;t=json.load(open('fixtures/real-catalogs-broad/collection.json'))['totals'];\
print(t['bytes_raw'], t['bytes_on_disk'], round(t['bytes_raw']/t['bytes_on_disk'],2))"
```

Re-walking the incumbent ten reproduced their committed counts exactly, with one exception:
**livemomentous.com is 89 products here and 90 in the committed corpus.** The count moved by
one; the id set moved by five — three ids present on 2026-09-07 are gone and two are new. That
is the corpus being a point-in-time snapshot, working as designed, and it is why a count alone
is a weak way to say a catalogue is unchanged.

```sh
python - <<'EOF'
import gzip, json, pathlib
def ids(d):
    m = json.load(open(pathlib.Path(d) / "collection.json"))
    s = next(s for s in m["stores"] if s["host"] == "livemomentous.com")
    blob = gzip.decompress((pathlib.Path(d) / s["files"]["products"]).read_bytes())
    return {json.loads(l)["id"] for l in blob.split(b"\n") if l}
a, b = ids("fixtures/real-catalogs"), ids("fixtures/real-catalogs-broad")
print(len(a), len(b), len(a - b), len(b - a))      # 90 89 3 2
EOF
```

## The query this corpus was collected for

The corpus holds **25 table products** — 24 in the three furniture stores, and one camp table at
nemoequipment.com, which is a useful reminder that a word is not a category. Of those, **6
products name a coffee table**: floydhome's `The Lift Off Coffee Table` and two service items,
branchfurniture's two `Nested Coffee Tables` and its `Coffee Table`. There are 153 sofa/sectional
products and 120 desk/chair products inside the furniture stores (151 corpus-wide, because
"desk" and "chair" also appear in apparel and office accessories).

Counted with **whole-word** matching. Substring matching inflates all of these — `"table" in
title` also matches `Adjustable Footrest` and `Adjustable Laptop Stand`, which is how an earlier
draft of this file reported 161 desk/chair products and 25 tables "across three furniture
stores".

```sh
python - <<'EOF'
import gzip, json, pathlib, re
d = pathlib.Path("fixtures/real-catalogs-broad")
m = json.load(open(d / "collection.json"))
def load(s):
    return [json.loads(l) for l in
            gzip.decompress((d / s["files"]["products"]).read_bytes()).split(b"\n") if l]
stores = {s["host"]: (s["category"], load(s)) for s in m["stores"] if s["skipped"] is None}
def n(*terms, only=None):
    pats = [re.compile(rf"\b{t}\b", re.I) for t in terms]
    return sum(1 for h, (c, ps) in stores.items() if only in (None, c)
               for p in ps if any(x.search(p["title"] or "") for x in pats))
print("tables", n("tables?"), "| in furniture", n("tables?", only="furniture"))
print("coffee tables", n("coffee tables?"))
print("sofa/sectional in furniture", n("sofas?", "sectionals?", only="furniture"))
print("desk/chair", n("desks?", "chairs?"), "| in furniture", n("desks?", "chairs?", only="furniture"))
EOF
```

**Furniture inventory is thinner than 714 suggests.** 210 of those 714 products are not a piece
of furniture at all — `Component` (110 at sabai.design alone), `Swatch`, `Protection Plan`,
`Clyde Service Contract`, `Serviceability`, `Fabric`, gift cards and a calendar. 504 remain.
That is not a defect in the collection; it is what a merchant's whole catalogue contains, and it
is the irrelevant stock that makes retrieval a test rather than a lookup.

```sh
python - <<'EOF'
import gzip, json, pathlib
NOT_AN_ITEM = {"Component", "Swatch", "Protection Plan", "Fabric", "Gift Cards", "Gift Card",
               "Serviceability", "Clyde Service Contract", "Calendar", "Add On/Expansion"}
d = pathlib.Path("fixtures/real-catalogs-broad")
m = json.load(open(d / "collection.json"))
tot = junk = 0
for s in m["stores"]:
    if s["skipped"] or s["category"] != "furniture":
        continue
    ps = [json.loads(l) for l in
          gzip.decompress((d / s["files"]["products"]).read_bytes()).split(b"\n") if l]
    tot += len(ps)
    junk += sum(1 for p in ps if (p.get("product_type") or "") in NOT_AN_ITEM)
print(tot, junk, tot - junk)          # 714 210 504
EOF
```

### Walnut: the answer is on disk and title-and-brand retrieval cannot reach it

An earlier version of this file said *"no coffee table here is made of walnut"* and built a story
about a hard discrimination on it. **That is false.** Five of the six coffee-table products offer
a walnut finish: floydhome's `The Lift Off Coffee Table` carries `Walnut / Black` and eleven more
walnut variants, branchfurniture's `Nested Coffee Tables` carry `Walnut / Medium`, `Walnut /
Large`, `Walnut / Set of 2`, and its `Coffee Table` carries `Walnut/White` and `Walnut/Charcoal`.

What is true is sharper, and it is a finding about retrieval rather than about the corpus's
inventory:

* **Not one title in this corpus contains both "coffee table" and "walnut."** The finish lives in
  *variant* titles.
* `exchange.retrieval.relevance.identity_surface` joins `title` and `brand` and nothing else —
  `sed -n '369,381p' apps/exchange/src/retrieval/relevance.py`. Variant titles are not in the
  surface a query is matched against.
* Meanwhile **20 titles do contain "walnut"**, 18 of them at taylorstitch.com: 14 garments in a
  colourway called Walnut (`The Hudson Sweater in Walnut`, `The Utility Shirt in Walnut Double
  Cloth`) and **4 pieces of walnut-wood homeware** (`Walnut Chopping Board`, `French Style Walnut
  Cutting Board`, `Walnut Arrow Board`, `The Valet Tray in Walnut and Brass`). The other two are
  supplements — `Wormwood Black Walnut Supreme`, `Nutricost Black Walnut Hulls Extract`.

So the literal query has a correct answer sitting in the corpus that the current identity surface
cannot see, and a strong wrong one — walnut-coloured trousers — that it can. Calling walnut
simply "a clothing colour in this corpus" overstated it too; a fifth of the taylorstitch matches
are walnut wood.

This is recorded as an observation about the corpus, which is where the fact lives. **No
retrieval code was changed for it.** `test_the_walnut_answer_exists_on_disk_and_the_identity_surface_cannot_see_it`
pins all of it, including a guard on the sentence that was wrong.

```sh
python - <<'EOF'
import gzip, json, pathlib, re
d = pathlib.Path("fixtures/real-catalogs-broad")
m = json.load(open(d / "collection.json"))
W, CT = re.compile(r"\bwalnut\b", re.I), re.compile(r"\bcoffee tables?\b", re.I)
both = wal = ct = 0
for s in m["stores"]:
    if s["skipped"]:
        continue
    for line in gzip.decompress((d / s["files"]["products"]).read_bytes()).split(b"\n"):
        if not line:
            continue
        p = json.loads(line)
        t = p["title"] or ""
        wal += bool(W.search(t)); ct += bool(CT.search(t))
        both += bool(W.search(t) and CT.search(t))
print("walnut titles", wal, "| coffee-table titles", ct, "| both", both)   # 20 6 0
EOF
```

## Promotion — offline, and it costs the merchants nothing

The raw response bodies are preserved outside the repository at
`../proxyshop-worktrees/rc-raw-broad`. `build` never opens a socket, so adopting this corpus is a
re-run of `build` alone:

```sh
.venv/bin/python scripts/collect_real_catalogs.py build \
    --raw-dir ../proxyshop-worktrees/rc-raw-broad \
    --out fixtures/real-catalogs
```

That is exactly how `collection.json` in this directory was last regenerated — the store files
came out byte-identical, which is what `mtime=0` gzip buys.

A *narrower* shape is a `build` from the same scratch directory against an edited log, or a fresh
`fetch` with a shorter roster. Note that a fresh `fetch` against this scratch directory will
**re-walk every host**: `collector_version` is part of the resume fingerprint and it moved from
2.1.0 to 2.2.0, which is what retires records taken before the politeness floor was enforced in
code. `corpus_version` is 2.2.0 and `collector_version_at_fetch` is 2.1.0, so the two are on the
record separately.

### What promotion breaks — measured, not predicted

| gate / consumer | why it fails | current → new |
|---|---|---|
| `test_all_ten_stores_are_accounted_for` | exact set equality against `ALL_HOSTS` | 10 → 53 hosts |
| `test_the_per_store_and_total_product_counts_are_pinned` | exact dict equality against `RECORDED_COUNTS` | 3,093 → 17,409, and livemomentous 90 → 89 |
| `test_every_page_reassembles_byte_for_byte_into_the_response_that_was_served` | ends on `assert pages == 18` | 18 → 90 pages |
| `test_the_corpus_stays_small_enough_to_live_in_git` | `assert total < 5 MB` | 2.7 MB → 14.1 MB |
| `test_every_store_contributes_a_catalogue_rather_than_a_shelf` | `assert min(counts.values()) >= 50` | smallest store 89 → flybyjing.com at 32 |
| `test_this_corpus_is_a_single_category_corpus_and_says_so` | asserts every roster line reads `supplements` | would correctly fail: the corpus is no longer single-category |
| `DEMO_QUERIES` gates (`stores_with` / `stores_without`) | exact store counts over the whole corpus | 10-store denominators → 38 |
| `scripts/build_demo_deployment.py` → `exchange-deployment.json` | `composition.MAX_DEPLOYMENT_BYTES` is 4 MiB | see below |

`ranking.verification.MAX_CATALOG_PRODUCTS` (1,000) is **not** breached, because
`SNAPSHOT_PRODUCTS_PER_STORE` already trims each store to 1,000 before the document is built.
The ceiling that does break is the document's total size. Measured: the current
`deploy/demo/exchange-deployment.json` is 2,950,154 bytes for 3,086 priced products — 956 bytes
per product, at 70% of the 4 MiB ceiling. Trimming this corpus at 1,000 per store leaves 11,052
products, projected at **10.6 MB, 2.5x over the ceiling**. The 4 MiB document can hold roughly
**4,387 products**, i.e. about **115 per store across 38 stores**. So promoting this corpus
forces a real decision about what reaches the exchange snapshot — and a per-store window near 115
is the size that was measured to cost shoppers a product NAME on 62% of graph-route shortlist
slots when it was 60.

### The breadth gate the incumbent suite had, and why it certified nothing

`test_no_single_category_dominates_the_corpus` passed on **both** corpora, and would have passed
on a corpus of nothing but liver capsules provided the capsules were named variously enough. All
sixteen of its `BREADTH_PROBES` were supplement vocabulary — *creatine, magnesium, collagen,
ashwagandha, cholecalciferol, withania* — so it scored a corpus with zero furniture exactly as
broad as one with 714 furniture products, and stayed silent while apparel became 38.2% of this
one. Its docstring called them "sixteen unrelated categories"; they are sixteen sub-categories of
one category.

That gate is now two:

* `test_no_single_sub_category_dominates_the_supplement_shelf` in the incumbent suite, renamed for
  what it actually measures — the depth of a supplement shelf, which is a real and useful thing
  and is not breadth;
* `test_no_single_declared_category_dominates_the_corpus` here, computed over the **declared**
  category each host was put on the roster for, with `CATEGORY_PROBES` written in eleven
  categories' own vocabulary — *coffee table, sofa, tent, skillet, barbell, leash, serum, chisel,
  hot sauce*.

And the discrimination is measured rather than assumed:
`test_the_breadth_gate_would_have_failed_the_supplement_corpus` runs the new probes and the new
share rule against `fixtures/real-catalogs` and requires them to **fail** it.

| | committed (10 supplement stores) | broad (38 stores) |
|---|---|---|
| probe families scoring 0 | furniture, coffee, home-kitchen, beauty | none |
| probe families below the floor of 15 | 8 of 11 | none (thinnest: beauty, 21) |
| largest declared category | supplements, **100%** | apparel, 38.2% |
| verdict | **fails** | passes |

```sh
PROXYSHOP_WORKER=14 .venv/bin/python -m pytest fixtures/tests/test_real_catalogs_broad.py \
    -k "breadth or spans_genuinely or dominates" -q
```

## Provenance of the roster

`../real-catalogs/incumbent-hosts.txt` (the built-in ten, category `supplements`) and
`../real-catalogs/candidate-hosts.txt` (43 breadth candidates, 11 categories) were walked as one
roster: `--hosts-file` is repeatable. `collection.json` records, per store, which file and line
it came from and the curator's note.
