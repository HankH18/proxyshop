# MOVED — harness and skill issues now live in ONE file

    ~/claude-build/observations/swarm-loop-HARNESS-OPEN.md

This file used to hold a pre-build audit of the harness (VERDICT / MUST FIX / SHOULD FIX / H-1..H-32). It no longer does, and nothing should be
written here again. Every issue an agent finds with the HARNESS or the SKILL —
the swarm-loop scripts, hooks, launcher, prompts, gates, task-packet protocol,
freeze machinery, verification ladder — goes to that one document, wherever the
agent is and whichever run it belongs to. Read the whole current backlog with:

    claude-build --issues

**This does NOT change where issues about the APP go.** Defects in this
project's own code, tickets, findings and reports stay exactly where they are:
`backlog.md`, `findings.jsonl`, `tickets.json`, `reports/`, and
`~/claude-build/observations/swarm-loop-BUILD-<project>-OPEN.md`. Nothing about
the product moved.

## What happened to what was here

83 items were triaged against the live skill: **3 still open, 52 already fixed, 28 historical.** All eight MUST FIX items, both SHOULD FIX items and all 32 `H-` entries are closed in the live skill — most verbatim, several with the original reproduction preserved as the motivating incident. The still-open items were carried into the central document under shift
`S09040030`, each rewritten to stand alone with its evidence, because an entry
that only makes sense beside its old neighbours does not survive a move. The
already-fixed ones were dropped deliberately: they are closed in the live skill,
and re-recording them centrally would create the second drifting copy this
consolidation exists to remove. The full original is preserved outside the repo.
