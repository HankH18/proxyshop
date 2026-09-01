# ProxyShop — Execution Notes

1. Work the graph, not the doc: pull ONE ready ticket (all dependencies
   closed) from tickets.json, load only its contract + SPEC/DESIGN sections
   it Refs + files in its scope. Never load the full task graph or other
   tickets' contracts.
2. A ticket is closed only by its Verify command passing, then the
   top-level `make verify`. No model judgment substitutes for a passing
   check.
3. Parallelize only tickets marked parallel_safe. Everything else
   serializes in dependency order.
4. On failure: revert to the ticket's starting state before retrying —
   do not iterate on top of a failed attempt.
5. On discovered reality (the plan is wrong): stop, rewrite the un-started
   tail of the graph with the strongest available model, then resume.
   Never push through a stale plan. Protocol-schema changes route through
   E1 tickets only.
6. Escalation: done-ness disputes, repeated failures (2+ on one ticket),
   and re-planning go to the strongest available model or the human, not
   to the executing model. T-080's manifest approval is a human gate —
   do not fabricate the approval artifact.
7. Live Shopify credentials never appear in verify commands; anything
   needing them belongs to the demo runbook (T-085) only.
