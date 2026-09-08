# Seed panels — a manufactured merchant dashboard

**Everything in this directory was manufactured. No merchant has ever looked at this page, and
not one auction it reports was ever run.**

It exists because `GET /stores/{store_id}/dashboard` — R9's whole merchant page — is correct,
served, and empty on a fresh deployment. Every panel on it is downstream of a shopper who does
not exist yet: the trust panel is a projection of the trust ledger, the loss panel is the
exchange's report, and the bid journal records solicitations nobody has made. A demo of the
merchant surface shows an operator six panels saying "no observations yet", which is honest and
demonstrates nothing.

So the page was run once, against real code, and this is what it produced. The demo replays
these bytes. It does not re-run the producer.

## What is here

    merchant-dashboard/
      collection.json      provenance: what was generated, from what, by what, with a sha256
                           over the payload and the marker a reader needs — panel by panel
      dashboard-page.json  one complete `DashboardPage`, exactly as the route returned it

The shape follows `services/sim/seed-data/` and `fixtures/real-catalogs/collection.json`, which
are this repository's existing collect-once-replay-forever convention, rather than inventing a
third one. `dashboard-page.json` is written in the canonical form `seed.store.canonical_bytes`
produces — sorted keys, tightest separators, one trailing newline — so the digest of the *value*
and the digest of the *bytes* are the same number and there is no gap between them for an edit
to live in. Read it with `python -m json.tool`. `collection.json` is the readable one, and it is
the one file never digested.

## Reproducing it

    ./.venv/bin/python scripts/build_seed_dashboard.py                  # write
    ./.venv/bin/python scripts/build_seed_dashboard.py --check          # regenerate + diff

`--check` rebuilds in memory and compares. It byte-compares `dashboard-page.json` and
field-compares `collection.json` with exactly one field skipped — `source`, the git revision,
which legitimately moves with every commit. Its exit status is the finding: **0** means the tree
matches what the producer implies, **1** names the files that drifted.

Nothing in the producer reads a clock. `captured_at` is a **stated** constant, not a stamp, so
`--check` can compare the whole record; `determinism.stamped_by_the_producer` names the four
places a live page's values would move and these do not (`generated_at`, each row's
`recorded_at`, the offer's `expires_at`, and the rehearsal `auction_id`s).

## Nothing here was hand-written

The payload is the verbatim 200 body of the real route, driven in-process over
`httpx.ASGITransport`. Every panel on it came out of the code that serves it:

| panel | who authored it |
| --- | --- |
| `onboarding` | `merchant_svc.onboarding.script` — every question and turn |
| `envelope` | the service's own version algebra and approval digest over stated walls |
| `losses` | **stated** in `scripts/build_seed_dashboard.py`, served through the exchange's real bearer check |
| `trust` | the seeded ledger, replayed through the platform's own scorer |
| `trust_events` | the seeded ledger itself, narrowed to this store by the dashboard |
| `bids` | a **real** `store_agent` answering three real `POST /v1/bid-requests` |

The store agent is the real one and not a stub, for the reason
`apps/merchant/svc/tests/_fixtures_dashboard.py` gives: the agent's answer is the one thing on
this page that must not be a stub agreeing with itself. The three rows in `bids` are what it
said — `envelope_not_activated` while the envelope was in shadow, a `200` with an offer inside
the stated floors once it was approved, and `cluster_not_pursued` for a cluster this store does
not pursue.

## Seeded, not fake — and three different strengths of that

`collection.json`'s `marker.panels` says this per panel, and it is worth stating plainly because
the three are not equally strong and a single `simulated: true` would overclaim for four of the
six.

**`trust_events` and `trust` — a hash-chain fact.** Every event in the panel carries `order_ref`
beginning `sim-fb-` (`seed.provenance.SEED_MARKER_PREFIX`), and the buyer service copies
`order_ref` **verbatim** onto the sealed ledger event — so the marker is inside the hash chain.
A manufactured observation cannot be un-marked and an earned one cannot be marked without
breaking `GET /events/verify`. The events are the real ones from
`services/sim/seed-data/feedback-population/ledger.jsonl`, event hashes and `prev_hash` links
included, and the producer reads them through `seed.store.load()`, which verifies every digest
that corpus records before returning anything. The `trust` snapshot *inherits* that marker
rather than carrying one: it is those same events replayed through `trust.snapshot.build_snapshot`
at the corpus's recorded `as_of` and projected onto the published property set by
`trust.snapshot.routes.published_entry`. A posture is a number; it is seeded only because the
events under it are.

Five of the six dimensions in that snapshot sit at the neutral `Beta(2, 2)` prior. That is the
corpus being honest, not the panel being incomplete — the only observations in it are
post-purchase feedback, so `feedback_match` is the only dimension anything has moved.

**`bids` — a reserved id prefix, and weaker.** Each row's `auction_id` is
`dashboard-rehearsal-sim-fb-<uuid5>`. A live dashboard mints that id from `uuid.uuid4()`, which
cannot produce the substring `sim-fb-` (`s`, `i` and `m` are not hex digits), so the prefix does
separate these rows from a merchant's own rehearsals. **It is not a ledger row.** Nothing hashes
a solicitation, no chain links it, and an edit to one of these rows is detectable only by the
sha256 in `collection.json`. Do not read this as the same guarantee as the trust events.

**`losses`, `envelope`, `onboarding` — no marker at all.** The loss report is deployment
configuration stated in the producer, and `LossReport` is `additionalProperties: false`, so
there is nowhere in it to put a marker; none is smuggled into a cluster id either. The envelope's
terms are stated, and its approver is `owner@store-secondchance.simulated.invalid` — an
obviously-synthetic address on the reserved `.invalid` TLD, because a placeholder that looked
like a real person would be a claim about a real person. A reader tells all three from real ones
by the fact that they are in this file.

## Why the store is `store-secondchance` and not `gaiaherbs.com`

The compose demo (`deploy/demo/`) is a supplement market: four real storefronts and the
`cluster-liver-support` cluster. This page is not about any of them, deliberately.

The only real feedback chain in this tree belongs to the five **simulated** stores of the seeded
population (`store-brightbean` … `store-slowreply`), and its cluster is `coffee` —
`seed.population` addresses every simulated auction to `cluster_id = seed_category` and
`fixtures/manifest.json` states that category. Writing `gaiaherbs.com` at the top of a page whose
trust events are sealed under `store-secondchance` would forge the only unforgeable thing in the
artifact, and a reader checking the marker would catch it immediately.

The four supplement storefronts have no feedback chain because nobody has ever left them
feedback. That is a true fact about them, and this file does not paper over it.

`store-secondchance` is the one of the five worth shipping: seven observations, `feedback_match`
at `Beta(8.0, 3.5)` — a posterior mean of 0.696 against the neutral 0.5 prior — and a loss report
that says six of its nine losses were on price. A store whose buyers say it delivers what it
pitched, and which is being undercut anyway.

## What is NOT on this page, and why

**There is no `store_killed` row in `bids`.** R9's kill switch works and is observable, and a
page demonstrating it would be the page of a stopped store — which is a state this seed is not
about, not a state it could never leave. Measured on
`merchant_svc.envelope.store.EnvelopeVersions`:

    v.kill(store)                      -> v1 filed as `killed`
    v.activate(store, approval)        -> ApprovalRejected: "envelope v1 has been killed; a
                                          killed envelope is never reactivated by an approval,
                                          however well bound — restart the store first
                                          (POST /stores/{store_id}/revive), which brings it
                                          back in shadow, and approve these terms then"
    v.put(store, new_terms)            -> v2 … also `killed`
    v.revive(store)                    -> v2 back in `shadow`, approval dropped
    v.activate(store, approval)        -> `active` again, under a fresh signature

This paragraph used to call the kill **terminal** and the refusal's advice unreachable, and both
were true of the service it described: the refusal then read "publish a new version and approve
that", and `put` reaches the new version through `edit_envelope`, which carries the head's
activation forward — so a merchant who followed the advice arrived back at the same refusal one
version later. `revive_envelope` is the door that was missing. `edit_envelope` still carries
`killed` forward, deliberately, so saving new terms is not a way to un-stop a store; the restart
is its own act and the approval that follows it is the ordinary one.

The row a killed store produces looks like every other decline in `bids`, with
`"decline_reason": "store_killed"`, `"activation": "killed"` and `"may_bid": false`.

The related row a merchant must never scroll past — `"contradiction": true`, a store the merchant
stopped whose agent bid anyway — is also absent for the same reason, and
`merchant_svc.dashboard.journal.SolicitationRecord.contradiction` documents the measured cause:
the agent is a separately deployed process reading its own `STORE_AGENT_CONTEXT`, and nothing
delivers the merchant's kill to it.

**`onboarding.missing` names `NETWORK_INTENT_CLUSTERS`**, and that is real rather than a
generation artefact: this deployment states no readable intent-cluster taxonomy, so the pursue
question offers only the clusters this store's own envelope and loss report already name. The
panel says so in its own `detail`.

## Going live

**Server side: nothing changes.** `merchant_svc.dashboard.routes.read_dashboard` has no notion
that a seed panel exists — no branch on the caller, no field only a producer fills in. The
producer drives the ordinary route with an ordinary admin bearer. Point the three deployment
variables at real services and the same route assembles the same page from real rows:

| panel | what makes it live |
| --- | --- |
| `losses` | `EXCHANGE_URL` + `MERCHANT_REPORT_TOKENS` (or `…_JSON`) — the bearer IS the store's identity at the exchange, so the merchant service holds it and the browser never sees it |
| `trust`, `trust_events` | `TRUST_URL` — and real feedback arriving through `POST /buyer/feedback`, which is the same door the seeded answers went through |
| `bids` | `STORE_AGENT_URL` — then click Solicit; every row is a real probe |
| `envelope`, `onboarding` | already live: answer the interview and PUT the transcript |

**The trust panel is the one with a story after the switch.** The ledger is append-only and has
no delete, so on the day real buyers arrive either the manufactured observations can still be
separated from the earned ones or they can never be — retroactively, permanently. They can:
`python -m seed posture` replays the chain twice, once whole and once with the `sim-fb-`
observations filtered out, and prints each store's posture with the seed and without it. That is
what makes this seed data rather than fake data.

The code that produced this directory is `scripts/build_seed_dashboard.py`, and the corpus it
reads is `services/sim/seed-data/feedback-population/`. Neither is hosted and neither ships in a
container image.
