# Cycle 9 — wave 6, eight hardening lanes

HEAD `6f78432` · acceptance **43/120 (35.83%)**, from 36/120 · 4/12 metrics at target
· harness intact · codebase stall 0/3 · goal-progress stall 0/3.

## Harness hermeticity: PARTIAL

`verify` answers *did the frozen bytes move* — they did not. It does not answer *can the
measurement be gamed*. The structural seam is unchanged and still open: product code
imported by the acceptance suite runs **in the scorer's own process** and can tamper with
its in-process state. Inherent to any in-process black-box suite; moved, not closed, by a
subprocess-per-test design this run does not pay for.

## Metrics

| metric | cycle 8 | cycle 9 | target |
|---|---|---|---|
| acceptance_pass_rate | 30.00 | **35.83** | 100 |
| spec_criteria_passing | 6 | **8** | 8 — AT TARGET |
| e2_ingestion_passing | 3 | **6** | 8 |
| e5_merchant_passing | 2 | **4** | 10 |
| e1 / e3 / e4 / e6 / e7 / e8 | 10/3/3/2/2/5 | 10/3/3/2/2/5 | 10/21/20/26/9/8 |
| acceptance_collected, build_succeeds | 120, 1 | 120, 1 | AT TARGET |

Priority next: **e6_trust_passing** (error 24, the only `converging_off_track` verdict).

## Tickets closed

T-021 (extraction, E2 3→6). Wave-6 hardening against findings from the cycle-8 rung-2
sweep: merchant webhook/install surface, store-agent R8 boundary, exchange fan-out and
timeout, trust availability, fixtures answer key, buyer k-knob, deployability.

## Tickets minted

T-151 (ledger role confusion) and, from the lanes' own reports, the unfinished-gap set
recorded in `findings.jsonl` — 111 findings recorded this run across three sweeps.

## What actually changed, beyond the number

Four services are **deployable and were verified by running them** — buyer, merchant,
exchange, trust all healthy in one compose project; a trust event hash-chained into
Postgres over the compose network and read back with `psql`. Before this wave the only
Dockerfile in the repo was the shopify stub's.

## Two of the orchestrator's briefs were REFUTED mid-flight

1. **Buyer k-anonymity was not a defect.** `SPEC.md:56` lists it under Non-goals: "No
   k-anonymity enforcement beyond a configurable floor (default 1 in fixtures)." The
   measurement (min class size = 1) was right; calling it a bug was wrong. The lane
   discarded a full implementation before committing and shipped only the missing knob,
   default byte-identical.
2. **`refresh_digests` does not defeat the approval tamper-evidence.** The two-document
   digest chain turns the frozen test RED at `test_e8_proofs.py:355`. Verified on staged
   mirror trees with the unmodified frozen test, including the strongest attack
   (`refresh_digests` then `reissue_request`).

Of 18 findings put to refutation, 10 survived and **8 were refuted**. The dominant refuted
pattern: "zero production callers" is not reliably a defect here — it is often a correctly
built half whose consumer is a scheduled ticket. Indistinguishable to a caller search.

## Corrected recorded guidance

`.swarm-loop/reports/T-132-amendment-design-REFUTED.json` tells the next author to assert
`min(class) >= K AND len(classes) <= len(pop)//K`, claiming the second kills a degenerate
implementation. It does not: the second is **implied by** the first, and collapse-
everything passes both (measured at N=4000, K=5). A **lower** bound on class count is what
catches it. Found by the lane told to implement it.

## Escalations

None open. One frozen-goal objection (exchange, on `test_e3_exchange.py:653`) was RULED on
rather than escalated — the goal pins accept mechanics and never claimed to test domain
trust; the binding requirement now lives on T-033's ticket, including that it add
`unbound_checkout_requests()` to its verify gate, since that lint is vacuously true until
T-033 writes the first call site.

## Open, recorded, not fixed

- The hardened R8 boundary has **no caller** — `enforce_bid_provenance` must run on the
  whole bid; `bid.claims` leaves the Offer outside, which is the original defect. T-041.
- Bid price is never reconciled against the granted discount;
  `contracts.boundary.validate_bid` has no price/floor check at all.
- The fan-out pool bounds the thread leak but cannot reclaim a worker stuck in a socket;
  the real fix is a timeout on the outbound bid client.
- No service has a readiness route — healthchecks prove only that uvicorn answers.
- T-109 and T-111/T-123 were reported fixed by earlier waves; git says the files were
  never touched. Back on the open frontier.

## Process learnings

- The gate failed **three times** on things no single lane could see: twice on
  `ruff format --check` (a lane that does not run the formatter), once on two mypy errors
  that exist only on the combined tree. Integration-level gates are not optional.
- One of those mypy errors was silenced by a `# type: ignore[union-attr]` where mypy
  raises `attr-defined` — the ignore was silencing nothing and the error had been live and
  invisible. A wrong ignore code is worse than none.
- Two lanes' branch tips **moved after collection**: merchant added a second commit and
  store-agent added four. Pinning the tip is necessary and not sufficient — re-check
  before declaring a wave closed.
- Every lane that ran its own adversarial pass found real defects in its own first fix.
  Store-agent's caught a **critical regression of the very defect it had just fixed**.
