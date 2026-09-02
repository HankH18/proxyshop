# Wave-1 verification findings — 35 items, UNADJUDICATED

**Read this section before acting on anything below.**

These are the raw output of six adversarial verification lenses run against the wave-1
branches. **None of them has been adjudicated.** The workflow that produced them also had a
refutation stage — three independent skeptics per finding, kill on 2-of-3 — and that stage was
**stopped before it ran**, because the fan-out was unbounded and burning agents and disk. So
what follows is *finder output only*: real reproductions in most cases, but with no second
opinion applied, and some will be wrong or overstated.

**Treat every entry as a hypothesis with evidence attached, not a confirmed defect.** The
first action on any of them is to reproduce it. This project has already had two findings
turn out to be false on inspection — a claim that missing `__init__.py` broke every
`packages.*` import (PEP-420 namespace packages resolve fine), and a harness claim that did
not reproduce under eight shapes — so the base rate of wrong findings here is not zero.

**Provenance and known bias:** three of these lenses ran against **task/T-013 while it was
still committing**, so some T-013 findings may already be fixed on the branch. T-013 landed
three further commits after these lenses ran, including the rounding fix that one lens
independently reported. Check the current tip before working any T-013 item.

---

## How to work this list

Each branch below is **unmerged**, with a live worktree already provisioned (own `.venv`, own
`node_modules`). Work a branch in its own worktree; do not merge, do not push.

| branch | worktree | worker index |
|---|---|---|
| `task/T-013` | `../proxyshop-worktrees/T-013` | `PROXYSHOP_WORKER=2` |
| `task/T-014` | `../proxyshop-worktrees/T-014` | `PROXYSHOP_WORKER=4` |
| `task/T-012` | `../proxyshop-worktrees/T-012` | `PROXYSHOP_WORKER=5` |

Every pytest invocation needs `PROXYSHOP_WORKER=<N>` set — the root `conftest.py` fails the
session without it. Run from the worktree root with that tree's own venv:
`PROXYSHOP_WORKER=<N> ./.venv/bin/python -m pytest ...`. Never a bare `python3`.

The datastore stack (Postgres/Neo4j/Redis) is shared and up. **Never `make deps-down`** and
never `FLUSHALL`.

**Do not modify anything under `.swarm-loop/`** — it is a frozen, hashed harness and one
changed byte voids the run's scores. Reading it is fine and encouraged.

**The reproduction commands below reference `scratch-*` worktrees that no longer exist.**
Each lens created its own throwaway tree to sabotage in; all eleven were torn down. The
commands are otherwise correct — **substitute the branch's real worktree path** and they run
as written. For example, a repro that says

    cd .../proxyshop-worktrees/scratch-conformance && PROXYSHOP_WORKER=2 ./.venv/bin/python -c ...

becomes

    cd ../proxyshop-worktrees/T-013 && PROXYSHOP_WORKER=2 ./.venv/bin/python -c ...

**Never sabotage in the real worktree.** If a check needs to break something to prove a test
has teeth, make your own scratch tree first — `git worktree add ../proxyshop-worktrees/scratch-<name> task/<id>`
then `./scripts/bootstrap.sh` inside it — and tear it down when done with
`rm -rf <literal path>` followed by `git worktree prune`. Note each bootstrapped tree costs
about 1 GB, so create them deliberately: most checks only need to READ a branch, which
`git show` and a plain worktree already allow.

To check whether a branch's frozen acceptance tests actually pass, run this **inside that
branch's worktree** and group by ticket (note `--json` writes to **stderr**):

```
PROXYSHOP_WORKER=<N> ./.venv/bin/python .swarm-loop/acceptance/run.py --json 2>&1 | \
  ./.venv/bin/python -c "import json,sys,collections; r=json.loads(sys.stdin.read()); \
  print(collections.Counter((x['ticket'],x['outcome']) for x in r))"
```

---


## `task/T-013` — 11 findings (5 high, 4 medium, 2 low)

### W1-01 · [HIGH] `services/shopify-stub/src/permalink.py:115`

**host_matches() accepts a port-bearing host as an exact domain match, directly contradicting its own S8-3 docstring ("a subdomain, a suffix, a homograph or a port-bearing host all fail"), and parse_permalink() silently discards the port so CartPermalink.to_url() rewrites the checkout URL to a different destination. No test in the branch supplies a port.**

*Reproduction (as reported — verify it):*

```
cd /Users/hankholcomb/.../proxyshop-worktrees/scratch-conformance && PROXYSHOP_WORKER=2 ./.venv/bin/python -c "import sys;sys.path.insert(0,'services/shopify-stub/src');from shopify_stub.permalink import host_matches,parse_permalink;u='https://store-a.example.com:8443/cart/44352913:2?discount=PSX-ABCDEFGH';print(host_matches(u,'store-a.example.com'));print(parse_permalink(u).to_url())"

Actual output:
  True
  https://store-a.example.com/cart/44352913:2?discount=PSX-ABCDEFGH

Full probe table (same tree):
  port 8443  -> True   urlsplit.hostname='store-a.example.com'
  port 443   -> True
  port 0     -> True
  subdomain  -> False
D22's template has no port, so a port-bearing URL is off-template; permalink.py:115 uses parts.hostname, which strips it, and permalink.py:173 then compares the stripped host.
```

*Claimed impact:* T-033 (exchange validator) and T-052 (merchant builder) are told by this module's docstring that a port-bearing host fails S8-3. It does not. A seller-supplied permalink pointing at https://store-a.example.com:8443/cart/... passes the release-blocker check and sends the buyer to whatever service occupies that port. Separately, any consumer that normalises a permalink through parse_permalink(...).to_url() silently changes the buyer's checkout destination from :8443 to :443 with no error. Both are invisible to the 156-test suite.

### W1-02 · [HIGH] `services/shopify-stub/src/permalink.py:81`

**build_permalink() validates shop_domain only for '://' and '/', so a domain containing '@', ':' or '\\' produces a permalink whose real host is a different domain — the builder emits an off-domain checkout URL and does not raise, contradicting its own "a scheme or a path here is a caller bug and raises" docstring.**

*Reproduction (as reported — verify it):*

```
cd .../scratch-conformance && PROXYSHOP_WORKER=2 ./.venv/bin/python -c "import sys;sys.path.insert(0,'services/shopify-stub/src');from shopify_stub.permalink import build_permalink,parse_permalink,host_matches;d='good.example.com@attacker.tld';u=build_permalink(shop_domain=d,variant_id=1,code='PSX-ABCDEFGH');print(u);print(parse_permalink(u).shop_domain);print(host_matches(u,d))"

Actual output:
  https://good.example.com@attacker.tld/cart/1:1?discount=PSX-ABCDEFGH
  attacker.tld
  False

Also reproduced with shop_domain='store-a.example.com\\@attacker.tld' (host -> attacker.tld) and shop_domain='store-a.example.com:8443' (builds fine, then host_matches(built, 'store-a.example.com:8443') is False — build and validate disagree about the same registered domain). Only 'store-a.example.com?x' and '...#f' raise, and they raise the wrong error ("permalink path must start with /cart/").
```

*Claimed impact:* T-052 builds the buyer-facing permalink from the seller's registered domain via this exact helper. A registered-domain string carrying '@' or a backslash yields a live checkout link at attacker.tld, and build_permalink reports success. The loud-failure point D22 intended at build time never fires; the only thing standing between that and a shipped off-domain link is whether T-033 happens to re-validate the built URL rather than the domain string. test_build_rejects_a_shop_domain_that_is_not_a_bare_host (test_stub_permalink.py:59) covers only 'https://' and '/cart', so nothing catches this.

### W1-03 · [HIGH] `services/shopify-stub/src/codes.py:120`

**D22's "randomly generated" clause has no test teeth: swapping secrets.SystemRandom() for a module-level random.Random(fixed_seed) — a fully predictable code stream — passes all 156 tests, including test_minted_codes_are_unpredictable, which only checks alphabet coverage and non-monotonicity.**

*Reproduction (as reported — verify it):*

```
SABOTAGE in the scratch worktree (.../scratch-conformance, since restored to HEAD): added `_GENERATOR = random.Random(20260901)` and changed codes.py:120 to `source = rng or _GENERATOR`.

  PROXYSHOP_WORKER=2 ./.venv/bin/python -m pytest services/shopify-stub -q
  -> 156 passed in 26.97s

Sabotage proven to reach the code path:
  server minted: ['PSX-X1KJMBSN', 'PSX-H6QNGVQR', 'PSX-C6Y04VR0']
  attacker pred: ['PSX-X1KJMBSN', 'PSX-H6QNGVQR', 'PSX-C6Y04VR0']
(the second line is a separate Random(20260901) reproducing the exact stream)

By contrast, the S8-3 subdomain sabotage DOES have teeth: changing permalink.py:173 to `actual == expected or actual.endswith("." + expected)` fails test_hostile_hosts_are_refused[subdomain] (1 failed, 155 passed).
```

*Claimed impact:* A seeded or otherwise predictable PRNG is uniform over the alphabet and unsorted, so it satisfies both checks the test file reasons about (it explicitly reasons about counters, timestamps and truncated hashes — not about a seeded PRNG). An attacker who observes a handful of codes recovers the Mersenne Twister state and predicts every future single-use redeemable, which is precisely the guessable-redeemable D22 forbids. Nothing asserts the source is secrets/SystemRandom, so this regression can be introduced by any later refactor "for performance" and the branch stays green.

### W1-04 · [HIGH] `services/shopify-stub/src/graphql_admin.py:323`

**No D22 code clause is enforced on the stub's production path — mint_code, is_well_formed and code_expiry have zero non-test callers. discountCodeBasicCreate accepts a code containing all four Crockford-excluded letters, usageLimit: null, and an endsAt 16 months out, with userErrors: [], and the stub then redeems it repeatedly.**

*Reproduction (as reported — verify it):*

```
grep -rn "mint_code\|code_expiry\|is_well_formed" services/shopify-stub/ | grep -v tests/  -> only definitions in codes.py; no call sites.

Live against the committed branch (scratch worktree, script at scratchpad/probe2.py):
  s.create_code("PSX-ILOU9999", usage_limit=None, ends_at="2027-01-01T00:00:00Z", offer_id="offer-777")
Actual output:
  userErrors: []
  stored usageLimit: None  endsAt: 2027-01-01T00:00:00Z
  redeemed twice -> PSX-ILOU9999 PSX-ILOU9999
Also: s.create_code("psx-offer42", offer_id="offer-42b") -> userErrors: []  (lowercase, and the body is literally the offer id).

Suite is green throughout: 156 passed.
```

*Claimed impact:* The stub is the surface T-052, T-033 and T-036 will test against. As shipped, it will accept and redeem a code that violates every D22 clause at once — a lowercase, offer-id-derived body containing I/L/O/U, unlimited usage, and a 16-month life. If T-052 ships `PSX-` + sha256(offer_id)[:8] with usageLimit 100 and a 30-day expiry, every one of its stub-backed tests passes. The D22 assertions in test_stub_codes.py are all on a library that no shipped path calls, so they prove the helper is correct, not that the system is conformant. Whether the stub should enforce D22 is arguable given the ticket's "no merchant-app logic" non-goal — but then nothing in this branch makes non-conformance detectable, which is the stated purpose of pinning D22 here.

### W1-05 · [HIGH] `services/shopify-stub/src/recordings.py:152`

**The criterion-1 parity matcher treats an EMPTY live array as conforming to a recorded non-empty array. `diff_shape` iterates `actual` (not `expected`) for array elements, so a stub response whose documented array is empty produces zero problems. Every `assert_conforms` call in test_stub_recordings.py is blind to a dropped array payload — line_items, note_attributes, discount_codes, discount_applications, orders edges — and `assert_no_invented_keys` is blind too, because an empty array emits no `path[]` entry in `collect_keys`.**

*Reproduction (as reported — verify it):*

```
Scratch worktree /Users/hankholcomb/…/proxyshop-worktrees/scratch-teeth at task/T-013 tip 17545e9. Baseline: `PROXYSHOP_WORKER=2 ./.venv/bin/python -m pytest services/shopify-stub -q` -> `156 passed in 27.85s`.

Plausible sabotage (off-by-one on a "skip removed line items" guard) in src/orders.py order_webhook_payload:
    for item in order.line_items
    if item.quantity > 1        # was: no filter; `> 1` instead of `> 0`

Re-run: `156 passed in 27.75s` — ZERO tests go red.

Probe test (subscribe ORDERS_PAID, buy variant with PSX-7QK2ZB0M, then run the exact criterion-1 assertions on the delivered body) printed:
  LINE_ITEMS ON THE WIRE: []
  NOTE_ATTRIBUTES: [{"name": "proxyshop_client_id", "value": "5dfbbd85-…"}]
  PARITY CHECK VERDICT: PASSED with 0 line items
so `assert_conforms(recording["payload"], delivered)` and `assert_no_invented_keys(...)` both accepted an orders/paid webhook carrying no line items at all.

The hole is also present in the CURRENT (uncommitted) working tree of T-013 — diff_shape lines 121-155 are byte-identical there. Direct proof against the live file:
  ./.venv/bin/python -c "import sys;sys.path.insert(0,'.pkgroot');from shopify_stub.recordings import diff_shape;print(diff_shape({'line_items':[{'id':1,'sku':'x'}]},{'line_items':[]}))"
  -> working-tree diff_shape, empty actual array vs documented element: []
```

*Claimed impact:* Acceptance criterion 1 ("contract tests assert request/response parity with recorded real-API shapes") is not proven for any array-valued field. T-050/T-052/T-061 build order ingest and pixel↔webhook reconciliation on this stub; an orders/paid webhook with empty `line_items` or empty `note_attributes` (the D24 `client_id` join key) would ship as green, and the consumer would be written against a payload real Shopify never sends. Fix: in the array branch, report a difference when `expected` is non-empty and `actual` is empty, or iterate `expected`-vs-`actual` with an explicit emptiness check.

### W1-06 · [MEDIUM] `services/shopify-stub/src/state.py:417`

**store_code silently overwrites codes_by_offer[offer_id], so two codes minted for the same offer leave BOTH live and redeemable while D22's offer index reports only the second — the first becomes an untracked, unrevocable single-use redeemable.**

*Reproduction (as reported — verify it):*

```
Live against the committed branch (scratchpad/probe2.py, section C):
  await s.create_code("PSX-AAAAAAAA", offer_id="offer-42")
  await s.create_code("PSX-BBBBBBBB", offer_id="offer-42")
Actual output:
  by_offer: {"offer-777": "PSX-ILOU9999", "offer-42": "PSX-BBBBBBBB"}
  codes present: ['PSX-AAAAAAAA', 'PSX-BBBBBBBB', 'PSX-ILOU9999', 'PSX-ROUND001']
  both still redeemable -> PSX-AAAAAAAA PSX-BBBBBBBB

No test covers this: test_the_code_is_stored_keyed_by_offer_id (test_stub_discounts.py:222) only creates codes for two DIFFERENT offers.
```

*Claimed impact:* A retry or at-least-once delivery in T-052's mint path produces two independent single-use discounts against one offer — a double-discount the merchant pays for twice — and the stub's own /_stub/codes index actively hides the first one from anyone auditing or revoking by offer_id. D22's "stored keyed by offer_id" implies one code per offer; the stub neither enforces it nor makes the violation visible.

### W1-07 · [MEDIUM] `services/shopify-stub/src/app.py:296`

**The cart quote and the order charge use different rounding (ROUND_HALF_EVEN at app.py:296 vs ROUND_HALF_UP at orders.py:131), so a shopper is quoted one price at the permalink and charged a different one at completion.**

*Reproduction (as reported — verify it):*

```
Live against the committed branch 17545e9 (scratchpad/probe2.py, section A): create PSX-ROUND001 at percentage=0.12345 on the 100.00 seed variant, visit the cart, complete the checkout.
Actual output:
  cart total_discount = 12.34 total_price = 87.66
  order total_price   = 87.65
  orders-query totalPriceSet = 87.65

All 156 tests pass with this discrepancy present — every test uses round percentages.
NOTE: as of this run, the T-013 worktree carries an UNCOMMITTED in-flight fix (codes.discount_amount_for + app.py/orders.py rewiring). This finding is against the committed branch tip; if that work lands, re-verify rather than re-report.
```

*Claimed impact:* A consumer built against this stub learns that the cart quote and the order total may legitimately differ, and any integration test that asserts "quoted == charged" against a non-round percentage fails for a reason that looks like the consumer's bug. On the real product surface this is a shopper being charged a price they were not quoted.

### W1-08 · [MEDIUM] `services/shopify-stub/src/graphql_admin.py:104`

**Only ROOT fields are validated against the schema. Any invented field nested inside a selection set — on `orders.edges.node`, on the connection, or on a mutation's payload node — is silently accepted and answered 200 with full data and no `errors`. This contradicts the module's own stated guarantee (graphql_lite.py:8, graphql_admin.py:18: "a caller that asks for a field the stub does not implement gets Shopify's `undefinedField` error rather than a plausible-looking success"), and it is not covered by the README's declared limitation, which is about returning a SUPERSET, not about accepting fields that do not exist. No test in the suite asserts a nested unknown field is refused.**

*Reproduction (as reported — verify it):*

```
Probe run in the scratch worktree against the UNMODIFIED tree (`PROXYSHOP_WORKER=2 ./.venv/bin/python -m pytest services/shopify-stub/tests/test_zz_probe.py -q -s`):

  Q: query { orders(first: 1) { edges { node { totallyMadeUpField } } } }
    errors: null      has data: True
  Q: query { orders(first: 1) { edges { node { id } } totallyMadeUpConnectionField } }
    errors: null      has data: True
  Q: mutation { discountCodeBasicCreate(basicCodeDiscount: {...}) { codeDiscountNode { bogusMutationField } userErrors { field message } } }
    errors: null      has data: True

Contrast, root level, which IS validated:
  Q: query { madeUpRootField(first: 1) { edges { node { id } } } }
    errors: [{"message": "Field 'madeUpRootField' doesn't exist on type 'QueryRoot'", …"code": "undefinedField"…}]   has data: False
```

*Claimed impact:* Criterion 1 claims REQUEST parity as well as response parity, and the request side is unenforced below the root. A downstream ticket can ship a selection set containing a typo, or a field the app has no scope for (`customer { email }`), see the stub answer 200 with data, and have the entire operation rejected by real Shopify with `undefinedField` and no data — the exact "plausible-looking success for a call no one has exercised" the module says it exists to prevent. The correct error shape already exists at line 104; it just is not applied to nested selections.

### W1-09 · [MEDIUM] `services/shopify-stub/tests/test_stub_webhooks.py:236`

**"Webhooks always delivered" is never tested with more than one subscriber on a single topic. `test_topics_are_isolated` uses two DIFFERENT topics, and `test_a_duplicate_subscription_is_refused` only covers a duplicate (topic, uri) pair — but the stub accepts two subscriptions on the same topic with different URLs (graphql_admin.py:546 keys the duplicate check on topic AND callback_url). A dispatcher that keyed deliveries by topic alone would silently drop one accepted subscriber and the whole suite stays green.**

*Reproduction (as reported — verify it):*

```
Sabotage in src/state.py:419-420 — a plausible "dict keyed by topic" design:
    by_topic = {s.topic: s for s in self.subscriptions.values()}
    found = by_topic.get(topic)
    return [found] if found is not None else []

Full suite: `PROXYSHOP_WORKER=2 ./.venv/bin/python -m pytest services/shopify-stub -q` -> `156 passed in 27.69s`. Nothing goes red.

Probe (two RecordingReceivers, both subscribed to ORDERS_PAID on distinct ephemeral URLs, then one buy) printed:
  sub1 userErrors: []
  sub2 userErrors: []
  deliveries reported by the stub: 1
  receiver A got: 0  receiver B got: 1

Both subscriptions were accepted with no userErrors, one receiver got nothing, and the stub's OWN delivery log (`webhook_deliveries`, and `/_stub/deliveries`) reported a single delivery — so the loss is invisible from every surface a test can assert on.
```

*Claimed impact:* The suite cannot distinguish "webhooks always delivered" from "webhooks delivered to at most one subscriber per topic". Nothing in the ticket restricts a topic to one subscriber, and the criterion is precisely the guarantee that no configuration or registration pattern makes a webhook not arrive. Add a test that registers two distinct URLs on ORDERS_PAID and asserts BOTH receivers got the identical signed body and that `webhook_deliveries` has length 2.

### W1-10 · [LOW] `services/shopify-stub/src/app.py:215`

**A comma-bearing ?discount=A,B is silently ignored in its entirety, though the branch's own recorded fixture documents that Shopify treats a comma as a multi-code separator — and the stub explicitly 404s the analogous multi-variant path form rather than half-handling it.**

*Reproduction (as reported — verify it):*

```
Live against the committed branch (scratchpad/probe3.py):
  GET /cart/44352913:1?discount=PSX-SINGLE01,PSX-OTHER   (PSX-SINGLE01 exists and is valid)
Actual output:
  comma cart: 303 None            # no discount applied
  requested: PSX-SINGLE01,PSX-OTHER
parse_permalink on the same URL returns code='PSX-SINGLE01,PSX-OTHER' as a single code.

The fixture services/shopify-stub/fixtures/recorded/cart_permalink.json states: "A discount code may not contain a comma, because commas separate multiple codes" and "the stub implements only [the single form], refusing the multi-variant form explicitly rather than half-handling it."
```

*Claimed impact:* Real Shopify would apply PSX-SINGLE01 here; the stub applies nothing and reports UNKNOWN_CODE. A consumer that accidentally emits two codes gets a silent zero-discount against the stub and a working discount in production — the exact direction of divergence a contract stub exists to prevent. It is also the one place the stub half-handles a multi-valued form after documenting that it refuses such forms loudly.

### W1-11 · [LOW] `services/shopify-stub/tests/test_stub_recordings.py:123`

**The negative control for the shape matcher never exercises the array-element branch as a FAILURE, which is why finding 1 was never noticed. Four of the five failure cases pass `"d": []` as the actual value against a template whose `d` is `[{"e": True}]`; each of those assertions is satisfied entirely by the OTHER deliberate defect (missing/renamed/mistyped `a` or `b`), and the empty `d` contributes nothing. The one case that does probe the array branch (line 129) supplies a non-empty array. So the test that exists to prove "a parity test is worthless if its matcher cannot fail" itself demonstrates, four times over, that an empty array is accepted — and asserts nothing about it.**

*Reproduction (as reported — verify it):*

```
Read services/shopify-stub/tests/test_stub_recordings.py:120-131 (identical in the committed tip and the current working tree). Then, in the scratch worktree:
  ./.venv/bin/python -c "import sys;sys.path.insert(0,'.pkgroot');from shopify_stub.recordings import diff_shape;t={'a':1,'b':{'c':'x'},'d':[{'e':True}]};print(diff_shape(t,{'a':1,'b':{'c':'x'},'d':[]}))"
  -> []
That input — the template with ONLY the array emptied, nothing else wrong — is the case the negative control never tries, and it returns no differences.
```

*Claimed impact:* The matcher's self-test reports coverage it does not have, so finding 1 survives review. Adding the single line `assert diff_shape(template, {"a": 1, "b": {"c": "x"}, "d": []})` turns the negative control into one that actually fails today, and forces the diff_shape fix.


## `task/T-014` — 15 findings (1 high, 5 medium, 9 low)

### W1-12 · [HIGH] `packages/llm/src/client.py:203`

**The `system=` argument has OPPOSITE cache semantics depending on the runtime type of `prompt`: on the CachedPrompt path it is deliberately an uncached second block, but on the bare-string path it becomes block[0] WITH `cache_control: ephemeral`. A varying per-call system on a string prompt therefore writes a fresh cache entry on every single call that can never be read back — the exact failure prompting.py's header says the module exists to prevent, surviving the branch's own CRITICAL-2 fix.**

*Reproduction (as reported — verify it):*

```
In the scratch worktree, PROBE 4 of /private/tmp/.../scratchpad/probe.py drove three turns through a recording fake client:

  c4 = AnthropicLLM("store_agent", model="m", client=r4)
  for t in range(1,4): c4.complete("buyer question", system=f"You are the store agent. Turn {t}")

Actual output:
  [{'type': 'text', 'text': 'You are the store agent. Turn 1', 'cache_control': {'type': 'ephemeral'}}]
  [{'type': 'text', 'text': 'You are the store agent. Turn 2', 'cache_control': {'type': 'ephemeral'}}]
  [{'type': 'text', 'text': 'You are the store agent. Turn 3', 'cache_control': {'type': 'ephemeral'}}]

Contrast PROBE 2 (same client, CachedPrompt path, same varying system): block[0] stays byte-identical and block[1] carries NO cache_control — asserted at packages/llm/tests/test_llm_contract_seam.py:132 with the reasoning "caching a per-call one writes an entry nothing will ever read". The string path is asserted the other way at packages/llm/tests/test_llm_client_offline.py:272-274, which pins `cache_control` onto the per-call system. Both tests are green simultaneously.
```

*Claimed impact:* A downstream consumer (T-021/T-041/T-045/T-053/T-071) that writes `client.complete(buyer_text, system=f"turn {n} ...")` — the natural shape for a conversational agent that has not yet built a CachedPrompt — pays the 1.25x cache-write surcharge on every request and gets a 0% hit rate, forever. Invisible in output; shows up only as latency and spend. A refactor from `complete(str, system=X)` to `complete(CachedPrompt, system=X)` silently flips whether X is cached, with no error and no test to catch it.

### W1-13 · [MEDIUM] `packages/llm/src/doubles.py:151`

**A `when()` rule that matches text in the SYSTEM half silently destroys RecordedLLM's strictness guarantee — an unrecorded prompt gets the scripted reply instead of UnrecordedPromptError. The orchestrator-frozen LLMDouble matches on the prompt only, so this is a silent behavioural divergence on a swap.**

*Reproduction (as reported — verify it):*

```
PROXYSHOP_WORKER=4 PYTHONPATH=.pkgroot:. ./.venv/bin/python -c with:

    ctx = "STORE CONTEXT\nenvelope v3: floor 18.00 USD"
    ask = "BUYER REQUEST\nquote 2 bags"
    d = RecordedLLM({(ctx, ask): "recorded offer"})
    d.when("envelope", "SCRIPTED-DENIAL")
    d.complete(ask, system=ctx)
    d.complete("a totally unrecorded prompt", system=ctx)

Actual output:
    RecordedLLM -> SCRIPTED-DENIAL | unrecorded prompt -> SCRIPTED-DENIAL

The same two lines against proxyshop_support.llm_double.LLMDouble:
    frozen LLMDouble -> double:default:b78d58f3213238b3   (the `when` rule never fires)

Cause: `_scripted` at doubles.py:151 builds `haystack = f"{system}{SECTION_SEPARATOR}{prompt}"`, while the frozen double matches `needle in prompt` (proxyshop_support/llm_double.py:76). `_canned` is never consumed and `reset()` deliberately keeps it (doubles.py:189-196), so one `when` set in a fixture stays live for every subsequent call in the test.
```

*Claimed impact:* T-041 (store agent) and T-045 are the likely victims: the store context supplied to those doubles literally contains words like `envelope`, `floor`, `discount`, and a test scripting `when("envelope", ...)` intends to match the buyer's turn. Every call in that test — including calls the author expected to raise UnrecordedPromptError, and calls that should have replayed a reviewed recording — returns the scripted string instead. The suite is green and asserting on nothing. The branch's own tests cover the analogous queue hole (`test_queue_wins_over_the_recorded_table_but_a_miss_still_raises`, test_llm_doubles.py:271) but there is no equivalent for `when`, and `test_a_when_rule_can_match_on_the_system_contract` (test_llm_doubles.py:281) asserts the divergent behaviour as intended rather than checking that a miss still raises.

### W1-14 · [MEDIUM] `packages/llm/src/doubles.py:417`

**DeterministicLLM.complete_json swallows the JSONDecodeError and returns {"double": role, "digest": hex} — so a consumer running under D20's DEFAULT provider gets a well-formed dict containing none of its schema keys, instead of the loud failure the frozen LLMDouble raises.**

*Reproduction (as reported — verify it):*

```
PROXYSHOP_WORKER=4 PYTHONPATH=.pkgroot:. ./.venv/bin/python -c:

    ex = build_llm("extract")                 # LLM_PROVIDER unset -> D20 default
    out = ex.complete_json("PITCH\nOur Guji is a light roast")

Actual output:
    type: dict value: {'double': 'extract', 'digest': 'e5554897fe7945f1'}
    out.get('claims') -> None

The frozen double in the same position raises json.JSONDecodeError at the call site (proxyshop_support/llm_double.py:74 -> json.loads("double:default:...")).
```

*Claimed impact:* T-021 (claim extraction) and T-071 (buyer intent) both parse JSON replies, and both run offline under the default provider. `parsed.get("claims", [])` yields an empty extraction that looks like a legitimately empty pitch; `parsed["claims"]` raises KeyError far from the LLM call with no mention of the double. A ticket that swaps the `llm_double` fixture for a `build_llm()` result therefore trades an immediate, self-explaining JSONDecodeError for silently empty data. The behaviour is documented in the docstring ("deliberately NOT your schema") and asserted by test_llm_doubles.py:303, so this is a deliberate design choice — but it is the one place on this surface where the new double is *less* safe than the frozen one, and the failure is silent.

### W1-15 · [MEDIUM] `packages/llm/src/client.py:194`

**The claimed identity is false: `wire_key` is NOT used by the client. `AnthropicLLM.complete` never calls it — the two derivations share only `CachedPrompt.to_system_blocks`, and ONLY on the CachedPrompt branch. The bare-string branch is duplicated logic in two files, and a client-side change to it leaves the entire 132-test suite green, including the test literally named `test_the_double_keys_on_exactly_what_the_client_sends`.**

*Reproduction (as reported — verify it):*

```
Structural: `grep -rn wire_key packages proxyshop_support` returns prompting.py:165 (def), doubles.py:107/283/382, src/__init__.py (export), test_llm_contract_seam.py:112/113. Zero hits in client.py.

Teeth test (scratch worktree, one-line client change to the string branch at client.py:202):
  cached = CachedPrompt(static_context=(system or "").strip(), dynamic_tail=str(prompt))
Result: `132 passed, 8 deselected` — fully green. Same for normalising the user turn (`str(prompt).strip()`): `132 passed`.

Proof the sabotage reached the path (same tree, sabotage E applied):
  wire_key says : ('  EXTRACTION CONTRACT: never infer.  ', 'PITCH: light roast')
  client sent   : ('EXTRACTION CONTRACT: never infer.', 'PITCH: light roast')
  AGREE         : False
  recording reachable via client composition: <<UNREACHABLE>>
File restored afterwards; `git status --porcelain` clean, 130 passed.

The single anti-drift test (test_llm_contract_seam.py:98-114) exercises exactly ONE input — a CachedPrompt plus a per-call system. It never covers the string branch, the no-system branch, or a custom separator. Only the CachedPrompt join is protected: changing wire_key's join to '|' does turn that test red (1 failed).
```

*Claimed impact:* The commit message states "computed by the new llm.prompting.wire_key() — the SAME function the client uses to compose the request, so the two cannot drift (asserted directly)", and doubles.py:17-18 repeats it. Neither is true for the bare-string path, which is the path the frozen acceptance suite and every simple consumer uses. A future edit to either side makes recordings unreachable through the very client they were recorded for, with a fully green suite — the failure mode surfaces only in a downstream ticket as UnrecordedPromptError on a prompt that is visibly in the fixture.

### W1-16 · [MEDIUM] `packages/llm/tests/test_llm_contract_seam.py:70`

**The mutation table's headline row is inflated. The worker reports "double ignores the system half -> 8 tests go red". Two independent reconstructions measure 3 and 5. The other seven rows are accurate within +/-1, so the table is directionally honest, but the one row that grades the ticket's most serious defect is overstated by 1.6-2.7x.**

*Reproduction (as reported — verify it):*

```
Scratch worktree at task/T-014 (detached 9a1ea2f), selector `-k "llm or T014 or role_model or double_answers"` = 132 tests (130 own + 2 frozen), baseline green.

(a) Semantic mutation (normalize_recording_key and RecordedLLM.complete both drop the system half):
  3 failed, 129 passed  -> test_dropping_the_contract_entirely_is_also_a_miss, test_inverting_the_contract_does_not_replay_the_reviewed_answer, test_every_committed_fixture_replays_every_prompt_it_records

(b) Faithful pre-fix keying (table keyed ("", user_half); lookup key = ("", prompt_text(prompt)), i.e. the 3683a01 behaviour):
  5 failed, 127 passed  -> the three above plus test_the_double_keys_on_exactly_what_the_client_sends and test_a_cached_prompt_looks_up_by_the_two_halves_it_actually_sends

Full measured table (claimed -> measured):
  double ignores system half        8 -> 3 (semantic) / 5 (faithful)
  per-call system in front of prefix 3 -> 3 (verbatim pre-fix client hunk) / 2 (merged-block variant)
  kwargs overwrite model/messages   2 -> 3
  stop_reason ignored               3 -> 3
  role validated only on live path  4 -> 4
  LLMCall order transposed          1 -> 1
  queue/when removed                3 -> 4
  complete_json raises again        1 -> 1

The "frozen suite still reads 2 passed" claim IS confirmed: all eight mutations produced 0 frozen-acceptance failures.
```

*Claimed impact:* An orchestrator reading the table over-credits the coverage of the ticket's core value. Anyone later removing or weakening one of the three tests that actually catch it would believe five more still stand guard. The defect is genuinely caught by tests that name it, so this is a credibility defect in the report rather than a hole in the code.

### W1-17 · [MEDIUM] `packages/llm/src/client.py:288`

**`build_llm` silently discards **kwargs on the offline (double) path — the same defect shape the branch just fixed for role validation. A typo'd or provider-specific keyword is accepted and dropped when LLM_PROVIDER is unset (which is every verify run, D3), and raises TypeError only under LLM_PROVIDER=anthropic.**

*Reproduction (as reported — verify it):*

```
PROBE 6, scratch worktree:
  build_llm("buyer", provider="double", model="smuggled-model", api_key="k", timeout=1, nonsense=True)
    -> DeterministicLLM   (accepted; every kwarg silently dropped)
  build_llm("buyer", provider="anthropic", nonsense=True)
    -> TypeError: AnthropicLLM.__init__() got an unexpected keyword argument 'nonsense'

The branch's own comment at client.py:282-285 gives the reasoning for why this is a bug: "Every test in this repo runs offline (D3), so the typo would have reached production unexercised." That reasoning was applied to `role` and not to `**kwargs`.
```

*Claimed impact:* A consumer writing `build_llm("store_agent", max_tokens=4096)` or `build_llm("extract", timeout=120)` gets a green offline suite in which the setting has no effect, and either a silently different configuration or a hard TypeError at the first live run. `model=` in particular is the field the reserved-keyword guard and the frozen AST scan exist to protect, and this path accepts and discards it without a word.

### W1-18 · [LOW] `packages/llm/src/doubles.py:24`

**The module docstring claims `.calls` is how a test asserts "that the static store context precedes the dynamic tail (C4)", but for a CachedPrompt — the input AnthropicLLM and llm.prompting explicitly prefer — `.prompt` holds only the tail and the static half is in `.system`, so that ordering assertion is impossible to write and raises ValueError.**

*Reproduction (as reported — verify it):*

```
PROXYSHOP_WORKER=4 PYTHONPATH=.pkgroot:. ./.venv/bin/python -c:

    ctx = "STORE CONTEXT\nenvelope v3: floor 18.00 USD"; ask = "BUYER REQUEST\nquote 2 bags"
    d = build_llm("store_agent")
    d.complete(assemble_prompt(ctx, ask))
    p = d.last_prompt; p.index(ctx) < p.index(ask)

Actual output:
    frozen LLMDouble: static@0 < tail@44 -> OK
    build_llm double: ValueError: substring not found

and:
    last_prompt = 'BUYER: quote 2 bags'
    call.system = 'STORE CONTEXT\nenvelope v3: floor 18.00'
    static in call.prompt? False
```

*Claimed impact:* The behaviour itself is defensible (it mirrors the wire), is asserted by the branch's own tests, and fails loudly. But doubles.py:24-25 tells five downstream tickets to do exactly the thing that cannot work, and the frozen LLMDouble's docstring says the same. Each of T-021/T-041/T-045/T-053/T-071 that writes the documented C4 ordering assertion gets `ValueError: substring not found` and has to reverse-engineer why. The fix is a docstring that says the ordering assertion is `call.system` + `call.prompt`, or `assemble_prompt(...).text`.

### W1-19 · [LOW] `packages/llm/src/doubles.py:249`

**RecordedLLM.from_fixture(name) returns a double that cannot answer a single one of its own recordings unless the caller separately calls load_system_contract(name) and passes system=; the miss message explains the problem but never names the function that solves it.**

*Reproduction (as reported — verify it):*

```
PROXYSHOP_WORKER=4 PYTHONPATH=.pkgroot:. ./.venv/bin/python -c:

    d = RecordedLLM.from_fixture("store_agent_envelope", role="store_agent")
    d.complete("BUYER REQUEST\nquote a price for 2 bags of the Ethiopia Guji, buyer is new")

Actual output (verbatim, truncated):
    RecordedLLM (store_agent_envelope) has this prompt recorded, but under a DIFFERENT system contract, so the recorded reply is not valid for this call (D21: recordings are reviewed answers to a specific contract).
    prompt: 'BUYER REQUEST\nquote a price for 2 bags of the Ethiopia Guji, buyer is new'
    system sent    (0 chars): ''
    system recorded: (197 chars) 'STORE CONTEXT\nstore: bean-cellar (store-1)...'
    If the contract genuinely changed, re-review the recording. If it did not, pass the same system text the fixture was authored against.

The branch's own test does the working spelling (test_llm_doubles.py:194: `RecordedLLM.from_fixture(name).complete(prompt, system=system)` with `system` pulled from `load_recording(name)` keys), so the trap is not exercised.
```

*Claimed impact:* Five agents will each write `RecordedLLM.from_fixture("x")` then `.complete(prompt)` — the only shape the constructor's own docstring example shows — and each will hit this. The message is legible about WHAT is wrong (a real strength of this branch) but not about HOW to fix it: it never mentions `llm.recordings.load_system_contract`, and `from_fixture` does not stash the fixture's contract on the instance so a `system=` default could apply it. Cost is ~5 x one debugging detour on a package whose whole point is to be consumed.

### W1-20 · [LOW] `packages/llm/src/recordings.py:163`

**load_system_contract is the only loader in llm.recordings that does not raise RecordingError — it reads and json.loads the file with no error handling, so a missing fixture surfaces as a bare FileNotFoundError and a malformed one as json.JSONDecodeError.**

*Reproduction (as reported — verify it):*

```
PROXYSHOP_WORKER=4 PYTHONPATH=.pkgroot:. ./.venv/bin/python -c "from packages.llm import load_system_contract; load_system_contract('no_such_fixture')"

Actual output:
    FileNotFoundError : [Errno 2] No such file or directory: '/Users/hankholcomb/.../packages/llm/fixtures/recorded/no_such_fixture.json'

Compare load_recording('no_such_fixture'), which raises `RecordingError: no recorded fixture named 'no_such_fixture' in ...; have: buyer_intent, extraction_claims, onboarding_interview, store_agent_envelope` — the message that lists what does exist. load_recording_file wraps OSError and JSONDecodeError into RecordingError (recordings.py:88-95); load_system_contract at 161-165 bypasses all of it.
```

*Claimed impact:* `load_system_contract` is in `llm.__all__` and — per finding 4 — is the function every downstream ticket must call to make `from_fixture` usable, so it is on the hot path for all five consumers. A ticket that catches `RecordingError` around fixture loading (the documented contract of this module, errors.py:18) will not catch this, and a typo'd fixture stem produces a bare OS error with no list of valid names instead of the good message its siblings give.

### W1-21 · [LOW] `packages/llm/src/client.py:292`

**build_llm silently discards the `recordings` argument when the anthropic provider is selected — no warning, no error; the caller's reviewed replay table simply vanishes.**

*Reproduction (as reported — verify it):*

```
PROXYSHOP_WORKER=4 PYTHONPATH=.pkgroot:. ./.venv/bin/python -c "from packages.llm import build_llm; c = build_llm('extract', recordings={'p':'r'}, provider='anthropic'); print(type(c).__name__, hasattr(c,'recordings'))"

Actual output:
    type: AnthropicLLM | recordings silently dropped: True
    MissingApiKeyError : ANTHROPIC_API_KEY is not set, so the live Anthropic client cannot be built. Verification i...

client.py:288-293 handles `recordings` only inside the `if name == PROVIDER_DOUBLE` branch; the anthropic branch forwards `**kwargs` to AnthropicLLM and drops `recordings` on the floor.
```

*Claimed impact:* On this machine D3 makes the follow-on failure loud (MissingApiKeyError), so the risk is bounded here. It stops being bounded the moment anyone exports LLM_PROVIDER=anthropic with a real key — a test that thought it was replaying reviewed fixtures issues a live, billed call instead, with no signal that its recordings were ignored. A one-line guard (raise if `recordings is not None` and the provider is not the double) closes it. Same class of asymmetry: `provider=` is taken verbatim while `resolve_provider` strips and lowercases the env var, so `build_llm(role, provider='Anthropic')` raises ProviderNotConfiguredError where LLM_PROVIDER=Anthropic works.

### W1-22 · [LOW] `packages/llm/src/doubles.py:363`

**Two loud-but-real breaks in the claimed drop-in compatibility with the frozen LLMDouble: DeterministicLLM's `default` is keyword-only where LLMDouble's is positional, and build_llm(role) sets role=<role> so the deterministic reply no longer matches LLMDouble.deterministic('default', prompt).**

*Reproduction (as reported — verify it):*

```
PROXYSHOP_WORKER=4 PYTHONPATH=.pkgroot:. ./.venv/bin/python -c:

    DeterministicLLM("a fixed reply")
    -> TypeError: DeterministicLLM.__init__() takes 1 positional argument but 2 were given
       (frozen: LLMDouble("a fixed reply") works — proxyshop_support/llm_double.py:44)

    LLMDouble().complete("hello")            -> 'double:default:e20959cec7d01949'
    DeterministicLLM().complete("hello")     -> 'double:default:e20959cec7d01949'   (agree)
    build_llm("buyer").complete("hello")     -> 'double:buyer:f735e47a9c183f78'     (differ)
```

*Claimed impact:* Both fail loudly, so neither can corrupt a result — but they are precisely the two lines a ticket writes when swapping the `llm_double` conftest fixture for a `build_llm()` result, which is the migration doubles.py:27-32 advertises as working unchanged. A test carrying `assert reply == LLMDouble.deterministic("default", prompt)` goes red after the swap for a reason that looks like a hash bug rather than a role default. Worth one sentence in the module docstring listing the two known non-drop-in spots, since the rest of the surface genuinely is drop-in (verified: zero public members of LLMDouble are missing from either double, and LLMCall's fields are identical in name, type and order — positional construction of both gives the same (role, prompt, system, kwargs)).

### W1-23 · [LOW] `packages/llm/src/client.py:179`

**The public `AnthropicLLM.complete` docstring still documents the removed CRITICAL-2 behaviour: it says a per-call `system` "is prepended to the prompt's own static context" — the exact merge the commit says it deleted, and the opposite of the code 20 lines below it.**

*Reproduction (as reported — verify it):*

```
packages/llm/src/client.py:178-180 reads:
    system: static system text. With a string prompt this becomes the cacheable
        prefix; with a :class:`~llm.prompting.CachedPrompt` it is prepended to the
        prompt's own static context.
The implementation at client.py:200 is `cached.to_system_blocks(extra=system)`, and the inline comment at client.py:196-199 says the opposite ("The per-call system goes AFTER the store context"). Verified by PROBE 2: block[0] is the store context on all five turns and the per-call system is block[1].
```

*Claimed impact:* The API reference a downstream ticket reads is the one that still describes the defect. A consumer designing prompt layout from the docstring will put stable bytes where they expect to be pushed behind the per-call text, or will assume a merge that no longer happens. Docs-only, but it is the single sentence a caller reads before using the parameter that carries this branch's two criticals.

### W1-24 · [LOW] `packages/llm/src/prompting.py:187`

**`wire_key` — documented as "the canonical identity of a call" — collides: two materially different wire payloads (one cached block containing 'A\n\nB' vs. a cached 'A' plus an uncached 'B') produce the identical key, so RecordedLLM replays one call's reviewed answer for the other.**

*Reproduction (as reported — verify it):*

```
PROBE 3, scratch worktree:
  wire_key(assemble_prompt("A\n\nB", "q"))          == ('A\n\nB', 'q')
  wire_key(assemble_prompt("A", "q"), "B")          == ('A\n\nB', 'q')
  COLLIDE: True
Actual wire payloads through AnthropicLLM with a recording fake:
  A: [{'type':'text','text':'A\n\nB','cache_control':{'type':'ephemeral'}}]
  B: [{'type':'text','text':'A','cache_control':{'type':'ephemeral'}}, {'type':'text','text':'B'}]
  RecordedLLM({wire_key_A: "answer-for-A"}).complete(pB, system="B") -> 'answer-for-A'
```

*Claimed impact:* The cache breakpoint position is part of what a call is, and the double is blind to it. A recording authored for a store envelope that ends with a contract section replays unchanged for a call that splits that section out as a per-call system — the second wire request has a different cacheable prefix and, against a live model, a different system-block structure. Narrow (requires the separator to appear at the split point), but it contradicts the module's own "canonical identity" framing.

### W1-25 · [LOW] `packages/llm/src/doubles.py:363`

**`DeterministicLLM` is not the drop-in replacement for the orchestrator-frozen `LLMDouble` that the module docstring and commit message claim, and the test that is supposed to prove it (test_llm_doubles.py:238-244) compares NAMES ONLY, so both divergences pass.**

*Reproduction (as reported — verify it):*

```
PROBE 7, scratch worktree:
  LLMDouble("fixed reply").complete("x")     -> 'fixed reply'
  DeterministicLLM("fixed reply")            -> TypeError: __init__() takes 1 positional argument but 2 were given   (`default` is keyword-only here, positional-or-keyword on the frozen double)

  LLMDouble().complete("p", role="buyer", system="S") -> double:buyer:a9f7046be404cb54
  DeterministicLLM(role="buyer").complete("p", system="S") -> double:buyer:80cd4771ecbaefc1
yet doubles.py:396-399 asserts "deterministic agrees with the frozen LLMDouble byte for byte for the calls that double can make" — the frozen double accepts `system=` (proxyshop_support/llm_double.py:63), so it can make exactly that call.

  LLMDouble().when("SYS","hit").complete("p", system="SYS contract")        -> double:default:93c5... (no match)
  DeterministicLLM().when("SYS","hit").complete("p", system="SYS contract") -> 'hit'

The surface test at test_llm_doubles.py:243 is `missing = [name for name in frozen_surface if not hasattr(type(double), name)]` — presence of the attribute, never its signature or behaviour.
```

*Claimed impact:* A ticket that swaps `LLMDouble("canned")` for a build_llm() result gets a TypeError on construction; one that asserts `reply == LLMDouble.deterministic(role, prompt)` while passing a system contract gets a mismatch; one relying on `when()` matching only the user turn gets a rule that now fires on system text. Each is a small, legible break, but the docstring promises they cannot happen.

### W1-26 · [LOW] `packages/llm/src/client.py:200`

**A `CachedPrompt` with an empty `static_context` makes the client send `system=[{"type":"text","text":"","cache_control":{"type":"ephemeral"}}]` — an empty text block, which the Anthropic Messages API rejects with a 400. The equivalent guard exists on the string path (client.py:203 and test_llm_client_offline.py:231) but not here.**

*Reproduction (as reported — verify it):*

```
PROBE 5, scratch worktree:
  AnthropicLLM("buyer", model="m", client=rec).complete(CachedPrompt(static_context="", dynamic_tail="q"))
  request keys: ['max_tokens', 'messages', 'model', 'system']
  system: [{'type': 'text', 'text': '', 'cache_control': {'type': 'ephemeral'}}]
Compare the string path: `complete("just this")` correctly omits `system` entirely (asserted at test_llm_client_offline.py:238).
```

*Claimed impact:* `CachedPrompt` is in `__all__` and constructible directly; `assemble_prompt` guards the empty case but the dataclass does not, and `with_dynamic` propagates it. A consumer that builds a CachedPrompt by hand (e.g. from a store record whose envelope is not yet populated) gets a 400 from the API rather than the legible PromptAssemblyError this package raises everywhere else. Not reachable through `assemble_prompt`, hence low.


## `task/T-012` — 9 findings (3 high, 4 medium, 2 low)

### W1-27 · [HIGH] `services/ingest/src/graph/reembed.py:190`

**Nothing records WHICH provider produced the stored vectors, and the only guard (`resolved.dimension != index_dimensions`) is structurally incapable of firing for the one swap D19 actually describes — both `HashEmbedding` and `LocalBgeEmbedding` hard-code `dimension = EMBEDDING_DIM = 1024`. A half-applied `EMBEDDING_PROVIDER` swap therefore embeds queries in a different space from the stored catalog and every health signal in the library stays clean.**

*Reproduction (as reported — verify it):*

```
Scratch worktree /Users/hankholcomb/.../proxyshop-worktrees/scratch-embed-contract, file services/ingest/tests/test_zzz_provider_mismatch.py. It defines `FakeBge` — a second VALID 1024-d L2-normalised provider standing in for `local_bge` (identical width, so the dimension guard cannot fire) — re-embeds the seeded catalog with it, then queries with the configured default `hash`.

  PROXYSHOP_WORKER=5 ./.venv/bin/python -m pytest services/ingest/tests/test_zzz_provider_mismatch.py -q -s

Actual output:
  reembed report: ReembedReport(provider='fake_bge', dimension=1024, products=4, embedded=4, skipped=[])
  report.complete: True
  reader provider: hash writer provider: fake_bge
  shortlist: [('prod-cream-night', 0.5129, 0.0258), ('prod-spf-daily', 0.5074, 0.0148), ('prod-serum-c', 0.5063, 0.0126)]
  products_missing_embeddings: []
  schema_report: ... vector_index ... 'state': 'ONLINE'
  1 passed

`prod-serum-c` — the exact semantic match for the query text — ranks LAST, at cosine 0.0126. No exception, no warning, `report.complete is True`, `products_missing_embeddings() == []`, the index is ONLINE. `ReembedReport.provider` is printed to stdout by `main()` (reembed.py:322) and then discarded; it is never written to the graph or the index, and `candidate_products` (query.py:272) never checks it.
```

*Claimed impact:* This is not hypothetical: .swarm-loop/decisions.md:221 states "`make e2e-live` sets `local_bge`" while `.env.example:20` sets `EMBEDDING_PROVIDER=hash` for every other environment. The moment T-085's runbook flips the env for the live demo — or flips it for the ingest worker but not the query service, or re-embeds and then a stale process keeps the old env — retrieval returns a full, plausibly-scored shortlist of pure noise with zero diagnostic signal. Ranking (T-031/T-032) then feeds `cosine_from_score` values around 0.01–0.03 into a weighted score and produces a confident, meaningless ordering. The fix is cheap: persist `report.provider` (an `:EmbeddingRun` node or a `Product.embedded_with` property) and have `candidate_products` refuse a query whose provider name differs.

### W1-28 · [HIGH] `services/ingest/src/graph/upsert.py:734`

**The provenance audit — the one mechanism six downstream tickets use to prove their writes are sourced — can be narrowed from 6 material-fact labels to `Product` and from 10 material-fact edge types to `CONTAINS` with the entire suite still green. Every "the audit catches a violation" test plants its violation on a Product node (line 940-948 of test_graph.py) and on a CONTAINS edge (line 963-966); no test ever plants an unsourced Store, Variant, Offer, AttributeValue or PolicyPage, or an unsourced SELLS/MAKES_OFFER/FOR/HAS_VARIANT/IN_CATEGORY/HAS_ATTRIBUTE/COMPATIBLE_WITH/SAME_AS/STATES edge. The three tests that DO cover the other labels only ever assert `provenance_violations(...) == []`, which a narrowed audit satisfies trivially.**

*Reproduction (as reported — verify it):*

```
In scratch worktree /Users/hankholcomb/.../proxyshop-worktrees/scratch-graph-teeth, patch upsert.py:734 `labels=sorted(MATERIAL_FACT_LABELS)` -> `labels=["Product"]` and upsert.py:756 `types=sorted(MATERIAL_FACT_EDGES)` -> `types=["CONTAINS"]`. Then `PROXYSHOP_WORKER=5 ./.venv/bin/python -m pytest services/ingest/tests/test_graph.py -q` -> "133 passed, 1 skipped in 15.89s" (identical to baseline).  Live proof of what the narrowed audit misses, same graph run against both trees: create an unsourced Offer, an unsourced Store, and a raw `MERGE (st)-[:SELLS]->(p)` with no source_id. Sabotaged tree: `violations reported: []`. Pristine T-012: `[ProvenanceViolation(kind='unsourced_edge', label_or_type='SELLS', ... detail='missing source_id'), ProvenanceViolation(kind='unsourced_node', label_or_type='Offer', identity='off-ghost', ...), ProvenanceViolation(kind='unsourced_node', label_or_type='Store', identity='st-ghost', ...)]`.
```

*Claimed impact:* T-020, T-021, T-022, T-023, T-024 and T-031 all call `assert_provenance_complete(session)` / `provenance_violations(session)` as their evidence that their own adapter writes carry a Source. If a merge, refactor, or a downstream ticket's edit narrows the audit's label or edge vocabulary, every one of those tickets keeps getting a clean bill while writing unsourced Offers, AttributeValues and SELLS edges into the shared catalog. Acceptance 1 ("every material fact upserts with a SUPPORTED_BY Source") would then be enforced only for Products, and nothing in T-012's suite would say so.

### W1-29 · [HIGH] `services/ingest/src/graph/query.py:86`

**`AttributeFilter.as_parameter` canonicalises the filter key (line 86) and unit (line 94) so that a caller's `"SPF"` matches an attribute stored with `canonical_key="spf"`. That fold is real, load-bearing behaviour and has zero test coverage: every attribute key in the fixtures (`spf`, `fragrance_free`, `volume`, `skin_type`) and every filter key/unit passed in the suite is already in canonical form, so the fold is a no-op in every assertion. Deleting it is invisible.**

*Reproduction (as reported — verify it):*

```
In the scratch worktree, patch query.py:86 `"key": canonical_text(self.key),` -> `"key": self.key,`. `PROXYSHOP_WORKER=5 ./.venv/bin/python -m pytest services/ingest/tests/test_graph.py -q` -> "133 passed, 1 skipped in 15.48s" — no test moves.  The behaviour it silently removes, measured on pristine T-012 against a seeded 2-product graph: `AttributeFilter('SPF', min_number=30)` -> `['p-space', 'p-under']` and `AttributeFilter('spf', equals_number=50, unit='Index')` -> `['p-space', 'p-under']`; under the sabotage both return `[]`. (Compare: the *value* fold is covered — `AttributeFilter('skin_type', value_string='sensitive')` against stored `"Sensitive"` is asserted at test_graph.py:1354.)
```

*Claimed impact:* The adapters in T-020..T-024 extract attribute keys and units as they appear on a product page — "SPF", "Fragrance Free", "Volume", unit "ML" — not in pre-folded form; the whole point of `canonical_key`/`canonical_unit` is to absorb that. A regression here does not error: `candidate_products` returns an empty candidate list for a perfectly well-formed structured query, and a buyer's shortlist silently goes empty with no message anywhere naming the cause.

### W1-30 · [MEDIUM] `services/ingest/src/graph/upsert.py:649`

**`set_product_embedding` validates only the vector's LENGTH, so it accepts the all-zero 1024-d vector that this branch's own `HashEmbedding().embed("")` deliberately returns. Neo4j stores it without complaint and then never returns that node from `db.index.vector.queryNodes` — verbatim the silent-unrankability failure the function's own docstring (upsert.py:618-632) claims the guard exists to prevent.**

*Reproduction (as reported — verify it):*

```
Scratch worktree, services/ingest/tests/test_zzz_adversarial_embed.py::test_zero_vector_write_is_accepted_and_silently_unrankable:

  PROXYSHOP_WORKER=5 ./.venv/bin/python -m pytest services/ingest/tests/test_zzz_adversarial_embed.py -q -s

Actual output:
  BEFORE ids: ['prod-cream-night', 'prod-spf-daily', 'prod-serum-c']
  AFTER  ids: ['prod-cream-night', 'prod-spf-daily']
  products_missing_embeddings: []
  4 passed

The body does `zero = hash_embed("")` (asserted `len == 1024` and `set(zero) == {0.0}`, i.e. exactly `HashEmbedding().embed("")`) then `set_product_embedding(session, product_id="prod-serum-c", embedding=zero)` — NO exception is raised — and the product vanishes from `candidate_products` forever while `products_missing_embeddings()` still reports it as embedded.

Neo4j's validation is asymmetric and this is the gap: the same test file's `test_nan_vector_write` shows `db.create.setNodeVectorProperty` DOES reject NaN — "neo4j.exceptions.ClientError ... Vector must only contain finite values, and have positive and finite l2-norm" — but it does NOT enforce the positive-norm half on write, only on query. So finiteness is caught for free and zero-norm is not.
```

*Claimed impact:* `set_product_embedding` is a public export (`upsert.py:849`) and is the API the six downstream tickets named in hash_embedding.py:14 (T-020…T-024, T-031) will use to seed `Product.embedding` from the root conftest's `hash_embedding` fixture. `reembed_products` happens to guard its own path (`if not text: skipped`, reembed.py:209-214), which proves the author understood the hazard — but the guard lives in the caller, not in the function that documents itself as the safety net. Any other caller that embeds a product whose composed text is empty writes a permanently invisible product with every health check green. A one-line norm check (`if not any(vector)`) next to the length check closes it.

### W1-31 · [MEDIUM] `services/ingest/src/graph/query.py:270`

**`candidate_products(embedding=...)` passes a caller-supplied vector to Neo4j verbatim with no validation, so a zero vector — again, exactly what this branch's `HashEmbedding().embed("")` returns — surfaces as a raw `neo4j.exceptions.ClientError` carrying a Java exception string, not one of the two exceptions the docstring's `Raises:` section declares (`UnretrievableQuery`, `ValueError`).**

*Reproduction (as reported — verify it):*

```
Scratch worktree, services/ingest/tests/test_zzz_adversarial_embed.py::test_zero_query_vector_raises_raw_driver_error:

  PROXYSHOP_WORKER=5 ./.venv/bin/python -m pytest services/ingest/tests/test_zzz_adversarial_embed.py -q -s

Actual output:
  zero query vector raised: neo4j.exceptions.ClientError
  {code: Neo.ClientError.Procedure.ProcedureCallFailed} {message: Failed to invoke procedure `db.index.vector.queryNodes`: Caused by: java.lang.IllegalArgumentException: Vector must only contain finite values, and have positive and finite l2-norm. Provided: SequenceValueVectorCandidate[sequence=List{D

The `query_text` entry point IS guarded (`elif query_text:` truthiness, query.py:271), so `query_text=""` falls through to the structured path or `UnretrievableQuery`. The `embedding=` entry point — which query.py:233 advertises as "a pre-computed query vector, used verbatim when given" and which takes precedence over `query_text` — has no equivalent guard.
```

*Claimed impact:* A downstream caller doing the natural thing, `candidate_products(session, embedding=provider.embed(intent_text))` (the shape T-031's ranking wants, so it can reuse one vector across several queries), gets an unhandled driver exception the moment `intent_text` is empty — a 500 out of the store agent instead of an empty shortlist or a typed `UnretrievableQuery`. Note the branch simultaneously documents the zero vector as a legitimate, total part of the embedding contract (base.py:55, hash_embedding.py:48, and a dedicated test `test_empty_text_embeds_to_the_zero_vector`), so the two halves of this branch disagree about whether a zero vector is a valid value to hand around.

### W1-32 · [MEDIUM] `services/ingest/src/graph/model.py:269`

**`attribute_value_id` builds the readable prefix with `slug()` (which folds `_`, `-` and space to `-`) but the identity digest with `canonical_text()` (which does NOT fold separators). The two halves therefore disagree: the same attribute written `fragrance_free` by one adapter and `fragrance free` by another produces TWO AttributeValue nodes whose ids both begin `av_fragrance-free_`. The convergence tests (test_graph.py:491-506) cover only case and whitespace variation, which does converge; separator variation is untested and does not.**

*Reproduction (as reported — verify it):*

```
Live on pristine T-012, two products seeded through `seed_products` with the two spellings:
  'fragrance_free'  canonical_text='fragrance_free'  slug='fragrance-free'  attr_id=av_fragrance-free_a98d914b39260a515c3cf4c8638cdc22
  'fragrance free'  canonical_text='fragrance free'  slug='fragrance-free'  attr_id=av_fragrance-free_866d16cdfbaf4b846d465f46f134cd98
AttributeValue nodes actually in the graph:
  {'id': 'av_fragrance-free_866d16cdfbaf4b846d465f46f134cd98', 'key': 'fragrance free', 'ck': 'fragrance free'}
  {'id': 'av_fragrance-free_a98d914b39260a515c3cf4c8638cdc22', 'key': 'fragrance_free', 'ck': 'fragrance_free'}
AttributeFilter('fragrance_free', value_bool=True) -> ['p-under']
AttributeFilter('fragrance free', value_bool=True) -> ['p-space']
(The SPF attributes in the same run, written as ('SPF', 50, 'Index') and ('spf', 50, 'index'), DID converge to one node — so the case/whitespace half works and only the separator half is broken.)
```

*Claimed impact:* `attribute_value_id`'s own docstring states the requirement: "Two adapters that independently observe 'SPF 50' must land on the same node, or the attribute half of the candidate query degenerates into a scan." T-021's LLM extraction and T-022's entity resolution will emit both `fragrance_free` and `fragrance free` for the same claim; the catalog then holds two nodes for one fact and a `fragrance-free` filter returns roughly half the qualifying products. Because both nodes carry the identical readable prefix `av_fragrance-free_`, a human inspecting the graph sees what looks like one attribute.

### W1-33 · [MEDIUM] `services/ingest/src/graph/reembed.py:305`

**`main()` — the shipped provider-swap script DESIGN asks for — is never executed by any test; only `build_parser()` is (test_graph.py:752). Its `--rebuild-index` branch (line 305-312) calls `rebuild_vector_index(...)` and, unlike the `else` branch, never calls `apply_schema(session)`, so that path creates the vector index and nothing else: no uniqueness constraints, no lookup indexes.**

*Reproduction (as reported — verify it):*

```
Live against the compose Neo4j from the scratch worktree: drop all constraints and non-builtin indexes, seed one product, then call `ingest.graph.reembed.main(["--rebuild-index"])`.
  after strip: constraints= 0 indexes= []
  provider=hash dim=1024 products=1 embedded=1 skipped=0
  main(--rebuild-index) rc = 0
  AFTER --rebuild-index: constraints= [] 
     indexes= ['product_embedding']
     duplicate Product count for cli-1 = 2      <- `CREATE (p:Product {product_id:'cli-1'})` succeeded a second time
The `else` branch (`apply_schema(session)`, line 312) restores all 10 constraints, so the asymmetry is clearly unintended.
```

*Claimed impact:* The module docstring documents exactly this command for an operator: `EMBEDDING_PROVIDER=local_bge python -m ingest.graph.reembed --rebuild-index`. Run against a database whose schema was not already applied by another path (a fresh dev DB, a restore, CI), it exits 0 having left the catalog with no uniqueness constraint on any stable ID — after which duplicate `product_id`/`attr_id` nodes are accepted and the MERGE-based idempotence of the whole upsert library no longer has a backstop. Because `main()` is untested, an outright crash on this path would also ship unnoticed.

### W1-34 · [LOW] `services/ingest/src/graph/reembed.py:219`

**`reembed_products` validates each written vector against the LIVE index width (`dimensions=index_dimensions`, line 219) rather than D6's constant — the fix the first adversarial pass extracted, per the comment at line 180-182. Making that check tautological leaves the suite green: there is no test that a vector is validated against the width of the index that actually exists.**

*Reproduction (as reported — verify it):*

```
In the scratch worktree, patch reembed.py:219 `dimensions=index_dimensions,` -> `dimensions=len(vector),`. `PROXYSHOP_WORKER=5 ./.venv/bin/python -m pytest services/ingest/tests/test_graph.py -q` -> "133 passed, 1 skipped in 15.32s". The only test touching wrong-length writes (`test_a_wrong_length_embedding_is_refused_rather_than_silently_stored`, test_graph.py:1859) calls `set_product_embedding` directly with its 1024 default, so it never exercises this wiring; `test_rebuilding_the_index_at_another_width_actually_changes_the_width` (test_graph.py:1936) only asserts the *pre-flight* provider-vs-index check, which fires before line 219 is ever reached.
```

*Claimed impact:* Defense in depth today (HashEmbedding is honest and LocalBgeEmbedding self-checks its output at local_bge.py:136-141), so no user-visible failure right now. But this is the guard against the exact failure the ticket calls its worst — `db.create.setNodeVectorProperty` accepting a wrong-width vector with no error, leaving a product silently unrankable while `products_missing_embeddings()` calls it embedded — and it has no test. A future provider that mis-declares its width, or a refactor of this line, restores the original defect invisibly.

### W1-35 · [LOW] `services/ingest/src/graph/query.py:176`

**The structured-only path stamps `0.0 AS score` (_STRUCTURED_HEAD, line 174-177) as a sentinel, but 0.0 is a genuine, reachable value on the vector path: Neo4j reports exactly 0.0 for an antipodal vector, and `cosine_from_score(0.0)` returns -1.0. A structured-path candidate therefore decodes to the worst possible cosine rather than to "no similarity measured".**

*Reproduction (as reported — verify it):*

```
Measured live against the compose Neo4j (this also independently confirms the worker's rescaling claim). Writing an exactly-orthogonal vector (Gram-Schmidt, python cosine = -2.7e-18) and an exactly antipodal vector (python cosine = -1.0), then querying the index:
  RAW neo4j score for p-a (orthogonal): 0.5004217028617859   cosine_from_score=0.00084340572
  RAW neo4j score for p-b (antipodal):  0.0                  cosine_from_score=-1.0
  self-query score: 0.9999683499336243
So the rescale is (1 + cos) / 2 exactly as claimed, `cosine_from_score` inverts it correctly, and 0.0 is a real vector-path score — the same value the structured path emits for every row.
```

*Claimed impact:* Nothing inside T-012 thresholds on the raw score (I grepped and read every use: the only consumer is a test asserting `abs(cosine_from_score(...)) < 0.2`, i.e. on the recovered cosine, so the '0.5 looks like a half match' trap is NOT present in this branch). The exposure is downstream: T-031 mixing vector-path and structured-path candidates, or uniformly applying `cosine_from_score` to `Candidate.score`, ranks every structured-only candidate at cosine -1.0 — below an actively dissimilar product. Typing the field `score: float | None` (None on the structured path) would make the ambiguity unrepresentable; today it is only a docstring.


---

## What is NOT in this list

- **T-010 and T-011 were never verified.** The second workflow, which covered them plus the
  two-canonicalizer collision, was stopped before any lens returned. T-010 is the
  highest-leverage ticket in the graph (34 transitive unblocks) and its `packages/contracts`
  is imported by everything downstream; T-011 owns the ledger and the grant model. **Both are
  unverified by anyone except their own authors.**
- **The cross-branch integration check never ran.** Nothing has merged all five branches
  together and looked for interference. This project has already lost 600 seconds to a
  two-lane flock deadlock that every individual ticket's verify passed, and a duplicate
  fixture name once killed a whole test directory including already-merged work.
- **The two-canonicalizer collision is unexamined.** T-010 wrote an RFC-8785 JCS canonicaliser
  in `packages/contracts/src/signing.py` (plus a TypeScript twin) for the D52 signing
  envelope; T-011 independently wrote one in `apps/trust/src/ledger/` for the D16 event chain.
  D16 says nothing outside the ledger defines its own hashing and D52 says there is exactly
  one signing implementation. T-011 reportedly **refuses** integers past 2^53 while T-010
  reportedly **renders** them — if so, the same payload is canonicalisable by one and fatal to
  the other. **This is the single highest-value unexamined check on the run.**
- **Harness defects are deliberately elsewhere** — `.swarm-loop/harness-review.md`, entries
  H-1..H-32. Nothing about the skill, `swarmloop.py`, the git-guard or the gates belongs in
  this file.

