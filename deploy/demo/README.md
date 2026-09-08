# The compose demo's deployment documents

Four of these are GENERATED from `fixtures/real-catalogs/` by
`./.venv/bin/python scripts/build_demo_deployment.py`, whose `--check` regenerates in memory and
diffs. CI runs `--check`, so a hand edit to a generated file fails the build rather than
surviving quietly. The generated column below says which.

| file | environment variable | generated? |
| --- | --- | --- |
| `exchange-deployment.json` | `EXCHANGE_DEPLOYMENT` | yes |
| `buyer-deployment.json` | `BUYER_DEPLOYMENT` | yes |
| `buyer-roster.json` | `BUYER_ROSTER` | yes (separate file because the buyer document is capped at 64 KiB and the roster at 512 KiB) |
| `store-contexts/<host>.json` | `STORE_AGENT_CONTEXT` | yes, one per hosted store agent |
| `buyer-store-window-tokens.json` | `PROXYSHOP_BUYER_STORE_WINDOW_TOKENS` | **no** — checked in by hand |

## `exchange-deployment.json` states no `trust_snapshot`, and that is deliberate

`exchange.composition._bind_live_ranking_snapshot` returns **without binding a live reader**
whenever the deployment document states a `trust_snapshot` — deliberately, and its docstring
says why. So a demo that stated one could not read the trust service at all. Measured on the
version of this document that did, over `POST /buyer/intent/clarify` → `/confirm` →
`GET /buyer/auctions/{id}`: every published `trust` term was exactly `0.20 x` a number typed
into this file, byte-identical on every run, and no feedback anyone gave through
`POST /buyer/feedback` moved any of them. The shortlist's order was a sorted copy of a
hand-written column.

With the key absent, the ranking gate binds `LiveTrustSnapshot` against this document's
`trust_url` and reads `GET /snapshot` on the running trust service.

## Which means the trust service has to know the ten sellers

**Run `make demo-trust` once, after `make demo-up`.**

R12 fails closed: `exchange.ranking.filters.blacklist_reason` EXCLUDES a store the live snapshot
holds no row for, because a store whose blacklist status cannot be established is not a store
you may shortlist. On a stack whose trust service has never heard of these ten storefronts that
is all of them, and the auction comes back `shortlist: []` — a symptom that points at ranking
and names trust nowhere. `scripts/demo_check.sh` reads the snapshot itself and says which step
is missing; `scripts/seed_demo_trust.py` is that step.

Two things it does, over the trust service's own doors:

* inserts the ten sellers into `app.sellers`, which `trust.snapshot.routes` joins against and
  which nothing in the product ever writes; and
* appends each store's opening posture as sealed ledger events through `POST /events`, five
  dimensions' worth, every one marked `sim-fb-` in `order_ref` — a top-level column inside the
  event digest, so a manufactured observation cannot be un-marked without breaking
  `GET /events/verify`.

It leaves `feedback_match` empty on purpose. Nobody has ever left feedback for these
storefronts, and it is the only dimension `POST /buyer/feedback` moves — so the demo's button
has the whole dimension to itself.

Measured on the served buyer route (`/buyer/intent/clarify` → `/confirm` →
`/buyer/auctions/{id}`), one press of the learning page's button, 6 rounds x 4 reviewers:

| store | `trust_summary.score` before | after |
| --- | --- | --- |
| `gaiaherbs.com` | 0.768412 | 0.770863 |
| `paradiseherbs.com` | 0.695497 | 0.705913 |
| `oregonswildharvest.com` | 0.670997 | **0.709885** |
| `toniiq.com` | 0.642748 | **0.681636** |
| `nutricost.com` (no bid, no feedback) | 0.637498 | 0.637497 |

`oregonswildharvest.com` overtook `paradiseherbs.com`. Five identical queries either side of
the press moved the trust term by 0.000001, so a shift of 0.0389 is four orders of magnitude
above the run-to-run noise. `gaiaherbs.com` moved least because it was the one store that
already carried feedback — a dimension with evidence on it is harder to move, which is the
trust engine working rather than the demo failing. A stack wound all the way back
(`make deps-down && make deps-up && make demo-up && make demo-trust`) starts with the full
range again.

What one press does NOT reliably move is the four slot NAMES. The same press wakes every store
agent's discount sampler (a pushed trust verdict is what gives an agent a cluster record), and
`price_value` then varies by up to 0.042 of `rank_score` per auction — measured, four distinct
slot assignments across five identical queries after a press versus one before it. That is the
store agents learning, not trust; the trust number on each card is the noise-free readout.

Re-running it is safe *for a stack seeded by the same version of this script*: `event_id` is the
ledger's idempotency key and `seeded_instant` adopts the `observed_at` the chain already holds
rather than stamping a fresh clock reading, so the second run rebuilds byte-identical events, the
ledger answers 200 to each, and it prints `0 appended, 568 already in the chain`.

A stack seeded by an OLDER version is the case that stops, and it stops loudly rather than
half-writing. The event ids are stable across versions of this script and the bodies are not, so
`POST /events` answers 409 on the first event whose content changed. Measured on a stack in that
state: the run named the event (`sim-fb-seed-bulksupplements.com-discount_honored-09`), reported
`0 event(s) were appended before this one and nothing after it was`, and exited 2 — and `--check`
against the same stack still passed, because the seed it already carries is a working one. That is
the choice the message offers: keep the seed you have, or wind the stack back with
`make deps-down && make deps-up && make demo-up` and seed it afresh. `--check` verifies without
writing and is the right thing to run first either way.
