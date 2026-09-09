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
t=m['totals']['products'];\
[print(f\"{k:14s} {len(v['stores']):2d} {len(v['stores_with_inventory']):2d} {v['products']:6d} {100*v['products']/t:5.1f}%\") \
for k,v in sorted(m['categories'].items(), key=lambda kv:-kv[1]['products'])];\
print('total', sum(len(v['stores']) for v in m['categories'].values()), m['totals']['stores_collected'], t)"
```

**Twelve categories are on the roster; eleven have inventory.** `totals.categories` counts the
eleven, because it counts categories with a stocked store, and the title of this file says the
same. Electronics is the twelfth: both candidates (nomadgoods.com, peakdesign.com) answered
`/products.json` with HTTP 404, and the category is recorded with zero products rather than
quietly dropped.

Four stores exceed 1,000 products: taylorstitch.com 3,805, titan.fitness 2,564,
marinelayer.com 2,556, cotopaxi.com 1,432. See *What promotion breaks*.

```sh
python -c "import json;m=json.load(open('fixtures/real-catalogs-broad/collection.json'));\
print(sorted(((h,n) for h,n in m['per_store_counts'].items() if n>1000), key=lambda x:-x[1]))"
```

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
rule against us**: not one store's `walk_outcome` is `robots_disallowed`.

The three unreadable robots files are our bad minute rather than a stated policy, and until
this round only two of them were treated that way. `outcome_is_retryable('robots_http_403')`
returned False — 403 fell in the same branch as a catalogue 403 — so **industrywest.com was
permanently skipped as though it had answered**, while bombas.com (429) and katzmosestools.com
(dropped connection) were re-asked. The manifest beside this paragraph is the evidence:
`run.reused_from_earlier_runs` contains industrywest.

That is now decided the other way, and the code, the manifest and this sentence say the same
thing. A 403 on **robots.txt** is a policy we were not allowed to read, not an answer about a
catalogue: the collector's own fail-closed rule means `/products.json` was never requested at
all, so recording it as "they answered" is the same mistake as recording an IP-wide 429 as an
empty shop. The counter-argument is real — a 403 is a refusal, and re-asking a host that
refused is what the politeness posture exists to prevent — so the reversal is scoped as
tightly as it can be: it costs that host exactly **one robots.txt GET on a later run started
by a person**, at the 2 s floor, with no catalogue request following unless the file is then
both readable and permissive. A 403 on `/products.json` (madeincookware, youthtothepeople) is
unchanged and still terminal, and so is a robots.txt that answers 404, which is a policy that
does not exist rather than one withheld.

`CORPUS_VERSION` is deliberately not bumped for it. A bump retires every record in a scratch
directory, charging 53 merchants a fresh walk to correct one host's judgement; it is not
needed, because `reusable_entry` judges the outcome before it consults the `complete` boolean
stored beside it, so exactly the affected records re-walk.
`test_a_record_written_under_the_old_403_rule_is_re_walked_rather_than_believed` is that
check.

```sh
PROXYSHOP_WORKER=14 .venv/bin/python -m pytest scripts/tests/test_collect_real_catalogs.py \
    -k "403 or robots" -q
```

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
  surviving record carries.

  Which of the two a number is, is now in the artifact rather than only in this paragraph.
  `build` writes `requests_charged` for **every** store, so its presence never meant anything;
  `requests_charged_accumulated` is the field that says whether the count was carried across
  walks (collector 2.2.0 and later) or synthesised from the walk that survived. These 53
  records were fetched by 2.1.0, so all 53 are the second kind and
  `totals.requests_recorded_is_floor` is `true`. An earlier draft of this file put the true
  figure at 158 by counting from the session's own scrollback; that number cannot be read off
  any artifact and has been removed.

  ```sh
  python -c "import json;m=json.load(open('fixtures/real-catalogs-broad/collection.json'));\
  print(sum(s.get('requests_charged', len([f for f in s['fetches'] if f['status']!=-1])+bool(s['robots'])) \
  for s in m['stores']), m['totals']['requests_recorded'], m['totals']['requests_recorded_is_floor'], \
  sum(s['requests_charged_accumulated'] for s in m['stores']))"
  ```

* **Strictly serial.** One host at a time, one request at a time; no concurrency primitive
  exists in the collector.
* **≥2.0 s between requests to one host, enforced in code** rather than defaulted. `parse_args`
  and `PolitenessBudget` both put the value through `is_polite_interval`, which requires it to
  be **finite** and at or above `MIN_INTERVAL_FLOOR`; each store records the interval it was
  walked at in `robots.crawl_delay_seconds`. Both checks used to be a bare `<` comparison, and
  every comparison against NaN is False, so `--min-interval nan` cleared both — and then
  `spend`'s own `if wait > 0` was False too, giving a walk that never paused at all under a
  manifest declaring 2.0 s. `--min-interval 0` was accepted in silence before that.
* robots.txt fetched first for every host and parsed with `protego`; a declared `Crawl-delay`
  widens the interval and never narrows it. A non-finite declared delay is ignored rather than
  honoured, because an infinite one is a hang, not a politeness.
* The research User-Agent, unspoofed, with a contact address.
* **No retry loop.** Within the run, no URL was requested twice — checkable, and checked by
  `test_the_retry_posture_says_what_a_resume_actually_does`. **Across runs, `--resume` does
  re-walk a host whose recorded outcome was retryable** (429, 5xx, transport error, empty body,
  unparseable page, interrupted walk, an unrecognised outcome, and now a
  403 on robots.txt): 51 of these 53 stores came from an earlier run, and three records here
  are retryable — bombas.com, katzmosestools.com and industrywest.com. In practice the next
  `fetch` against that scratch directory re-walks all 53 anyway, because `collector_version`
  moved from 2.1.0 to 2.2.0 and it is part of the resume fingerprint; the three are what would
  be re-asked on the outcome rule alone. `politeness.retries` says both halves; it used to say
  "none" flat out, which was false the day resume landed.

  ```sh
  python -c "import json;m=json.load(open('fixtures/real-catalogs-broad/collection.json'));\
  print(len(m['run']['reused_from_earlier_runs']), \
  [s['host'] for s in m['stores'] if s['walk_outcome'] in \
  ('robots_http_403','robots_http_429','robots_transport_error')])"
  ```
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

The corpus holds **25 table products** — 24 in the three stocked furniture stores, and one
`Moonlander™ Dual-Height Camp Table` at nemoequipment.com, which is a useful reminder that a
word is not a category. Of those, **6 products name a coffee table**, and all six are at two
stores: floydhome's `The Lift Off Coffee Table`, its `Lift Off Coffee Table - Expansion Kit`
and its `Serviceability - Coffee Table`, and branchfurniture's two products both titled
`Nested Coffee Tables` plus its `Coffee Table`. There are 153 sofa/sectional products and 120
desk/chair products inside the furniture stores (151 corpus-wide, because "desk" and "chair"
also appear in apparel and office accessories).

Counted with **whole-word** matching. Substring matching inflates all of these: `"table" in
title` finds 31 products in the three furniture stores where `\btables?\b` finds 24, the seven
extras being `Adjustable Footrest`, `Adjustable Laptop Stand`, their two `Open Box` twins,
`Muse Portable Lamp`, `Adjustable Headboard Hardware` and `The Adjustable Base`. Corpus-wide
the substring finds 204 against 25. That is how an earlier draft of this file came to report
25 tables "across three furniture stores" — 25 is the corpus-wide figure and 24 is the
furniture one. It also reported a desk/chair count this corpus does not produce under any
matching rule tried here; that number has been removed rather than adjusted.

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
a walnut finish, and the sixth is floydhome's `Serviceability - Coffee Table`, a service line.
floydhome's `The Lift Off Coffee Table` has **12** walnut variants, every one of them a size
crossed with a finish (`One Panel - 18" w x 67" l x 15" h / Walnut / Black` through
`Three Panel - 53" w x 67" l x 15" h / Walnut / Stainless`); its `Lift Off Coffee Table -
Expansion Kit` has 4 (`Walnut / Almond`, `Walnut / Black`, `Walnut / Marine`,
`Walnut / Stainless Steel`); branchfurniture's two `Nested Coffee Tables` have 3
(`Walnut / Medium`, `Walnut / Large`, `Walnut / Set of 2`) and 2 (`Walnut / Medium`,
`Walnut / Large`); and its `Coffee Table` has 2 (`Walnut/White`, `Walnut/Charcoal`).

What is true is sharper, and it is a finding about retrieval rather than about the corpus's
inventory:

* **Not one title in this corpus contains both "coffee table" and "walnut."** The finish lives in
  *variant* titles.
* `exchange.retrieval.relevance.identity_surface` joins `title` and `brand` and nothing else.
  Variant titles are not in the surface a query is matched against. Find it by name —
  `grep -n 'def identity_surface' apps/exchange/src/retrieval/relevance.py` — because a line
  range here was already wrong the day it was written, and pointed at code the function had
  moved away from.
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
variants, taylorstitch = [], []
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
        if W.search(t) and s["host"] == "taylorstitch.com":
            taylorstitch.append(t)
        if CT.search(t):
            n = sum(1 for v in (p.get("variants") or []) if W.search(str(v.get("title") or "")))
            variants.append((s["host"], t, n))
print("walnut titles", wal, "| coffee-table titles", ct, "| both", both)   # 20 6 0
for row in sorted(variants):
    print("   ", row)   # the six, with their walnut-variant counts: 12, 4, 3, 2, 2, and 0
home = [t for t in taylorstitch if re.search(r"\b(board|tray)\b", t, re.I)]
print("taylorstitch walnut", len(taylorstitch), "| walnut-wood homeware", len(home))  # 18 4
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
| `test_the_corpus_stays_small_enough_to_live_in_git` | `assert total < 5 MB` | 2.8 MB → 14.1 MB |
| `test_every_store_contributes_a_catalogue_rather_than_a_shelf` | `assert min(counts.values()) >= 50` | smallest store 90 (paradiseherbs, livemomentous) → flybyjing.com at 32 |
| `test_this_corpus_is_a_single_category_corpus_and_says_so` | asserts every roster line reads `supplements` | would correctly fail: the corpus is no longer single-category |
| `DEMO_QUERIES` gates (`stores_with` / `stores_without`) | exact store counts over the whole corpus | 10-store denominators → 38 |
| `scripts/build_demo_deployment.py` → `exchange-deployment.json` | the EXCHANGE's `apps/exchange/src/composition.py` `MAX_DEPLOYMENT_BYTES`, 4 MiB — not the buyer's constant of the same name in `apps/buyer/svc/src/composition.py`, which is 64 KiB and holds a different document | see below |

Every row of that table's third column comes out of this, which reads the two manifests and the
bytes on disk and asserts nothing:

```sh
.venv/bin/python - <<'EOF'
import json
from pathlib import Path
for d in ("fixtures/real-catalogs", "fixtures/real-catalogs-broad"):
    m = json.loads((Path(d) / "collection.json").read_text())
    live = [s for s in m["stores"] if s["skipped"] is None]
    c = {s["host"]: s["products_recorded"] for s in live}
    lo = min(c.values())
    size = sum(f.stat().st_size for f in Path(d).rglob("*") if f.is_file())
    print(d, "| rows", len(m["stores"]), "| collected", len(live),
          "| products", sum(c.values()), "| pages", sum(s["pages_fetched"] for s in live),
          "| smallest", lo, sorted(h for h, n in c.items() if n == lo),
          "| livemomentous", c.get("livemomentous.com"), f"| {size / 1e6:.1f} MB")
EOF
# fixtures/real-catalogs       | rows 10 | collected 10 | products  3093 | pages 18 | smallest 90 ['livemomentous.com', 'paradiseherbs.com'] | livemomentous 90 |  2.8 MB
# fixtures/real-catalogs-broad | rows 53 | collected 38 | products 17409 | pages 90 | smallest 32 ['flybyjing.com']                              | livemomentous 89 | 14.1 MB
```

The size is that `rglob` sum, which is the one
`test_the_corpus_stays_small_enough_to_live_in_git` itself computes — README and `collection.json`
included, not the gzip payload alone. It is printed to one decimal because **this file is inside
the directory it measures**: editing these paragraphs moves the byte count, so a byte-exact
figure here would be stale the moment it was written. The row read **2.7 MB** until 2026-09-08,
which was `totals.bytes_on_disk` (2,697,017) — the gzip payload, a smaller definition of "total"
on the left of the arrow than on the right. Both halves are now the gate's own definition.

`ranking.verification.MAX_CATALOG_PRODUCTS` (1,000) is **not** breached, because
`SNAPSHOT_PRODUCTS_PER_STORE` already trims each store to 1,000 before the document is built.
The ceiling that does break is the document's total size. Measured: the current
`deploy/demo/exchange-deployment.json` is 2,950,154 bytes for 3,086 priced products — 956 bytes
per product, at 70% of the 4 MiB ceiling. Trimming this corpus at 1,000 per store leaves 11,052
products, projected at **10.6 MB, 2.5x over the ceiling**. The 4 MiB document can hold roughly
**4,387 products**, i.e. about **115 per store across 38 stores**. So promoting this corpus
forces a real decision about what reaches the exchange snapshot — and a per-store window near 115
is the size that was measured to cost shoppers a product NAME on 62% of graph-route shortlist
slots when it was 60 (63 of 101 slots carried `identity: null`; the measurement is recorded in
`scripts/build_demo_deployment.py` beside `SNAPSHOT_PRODUCTS_PER_STORE`).

```sh
python -c "
import json, os
b = os.path.getsize('deploy/demo/exchange-deployment.json')
d = json.load(open('deploy/demo/exchange-deployment.json'))
p = sum(len(v['products']) for v in d['catalog'].values())
m = json.load(open('fixtures/real-catalogs-broad/collection.json'))
c = [s['products_recorded'] for s in m['stores'] if s['skipped'] is None]
t = sum(min(1000, x) for x in c)
print(b, p, round(b/p), f'{100*b/(4*1024*1024):.0f}% of ceiling')
print(t, f'{t*b/p/1e6:.1f} MB', f'{t*(b/p)/(4*1024*1024):.1f}x', int((4*1024*1024)/(b/p)), int((4*1024*1024)/(b/p)/len(c)))"
```

### The breadth gate the incumbent suite had, and why it certified nothing

`test_no_single_category_dominates_the_corpus` passed on **both** corpora, and would have passed
on a corpus of nothing but liver capsules provided the capsules were named variously enough. All
sixteen of its `BREADTH_PROBES` — the constant is `SUPPLEMENT_SUB_CATEGORY_PROBES` now, renamed
for what it holds — were supplement vocabulary, *creatine, magnesium, collagen,
ashwagandha, cholecalciferol, withania*, so it scored a corpus with zero furniture exactly as
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
| probe families scoring 0 | 5 of 11: beauty, coffee, furniture, home-kitchen, sports | none |
| probe families below the floor of 15 | 10 of 11 — every one but supplements, at 265 | none (thinnest: beauty, 20) |
| largest declared category | supplements, **100%** | apparel, 38.2% |
| verdict | **fails** | passes |

Both columns are pinned — `RECORDED_PROBE_HITS` and `INCUMBENT_PROBE_HITS` in the gate file —
and the gates below assert them, so this table cannot drift from the corpus without something
going red. The row that used to say "furniture, coffee, home-kitchen, beauty" was missing
sports, "8 of 11" was ten, and "thinnest: beauty, 21" was twenty; all three came from a
docstring rather than from the function.

```sh
PROXYSHOP_WORKER=14 .venv/bin/python -m pytest fixtures/tests/test_real_catalogs_broad.py \
    -k "breadth or spans_genuinely or dominates" -q
```

Per-family counts, both corpora, off the bytes on disk:

```sh
PROXYSHOP_WORKER=14 .venv/bin/python -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('g', 'fixtures/tests/test_real_catalogs_broad.py')
g = importlib.util.module_from_spec(spec); sys.modules['g'] = g; spec.loader.exec_module(g)
for name, d in (('broad', g.CORPUS), ('committed', g.SUPPLEMENT_CORPUS)):
    print(name, g._probe_hits(g._load(d), g.CATEGORY_PROBES))"
```

## Provenance of the roster

`../real-catalogs/incumbent-hosts.txt` (the built-in ten, category `supplements`) and
`../real-catalogs/candidate-hosts.txt` (43 breadth candidates, 11 categories) were walked as one
roster: `--hosts-file` is repeatable. `collection.json` records, per store, which file and line
it came from and the curator's note.
