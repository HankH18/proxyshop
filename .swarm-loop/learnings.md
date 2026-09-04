# MOVED — harness and skill issues now live in ONE file

    ~/claude-build/observations/swarm-loop-HARNESS-OPEN.md

This file used to hold process-loop lessons about how the swarm was run. It no longer does, and nothing should be
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

31 items were triaged: **12 still open, 17 already fixed, 2 historical.** The still-open items were carried into the central document under shift
`S09040030`, each rewritten to stand alone with its evidence, because an entry
that only makes sense beside its old neighbours does not survive a move. The
already-fixed ones were dropped deliberately: they are closed in the live skill,
and re-recording them centrally would create the second drifting copy this
consolidation exists to remove. The full original is preserved outside the repo.
