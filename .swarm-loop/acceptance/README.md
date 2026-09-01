# Frozen acceptance suite — ProxyShop

**These files are the goals.** They are hashed by `swarmloop.py freeze`; `swarmloop.py
verify` runs before every scoring and voids the cycle if anything here changed, and
`check-branch` vetoes any worker branch that touches `.swarm-loop/` at all.

Workers may **run** this suite as often as they like. Nobody may **edit** it. A worker that
believes a test here is wrong reports that in its `CONCERNS` — it does not work around it.
Only the user, awake, amends a frozen goal.

## What belongs here

Acceptance tests for the SPEC's requirements (R1–R19), constraints (C1–C11) and success
criteria (S1–S8), organised one file per epic plus `test_spec_criteria.py` for the
cross-cutting criteria and the three S8 release blockers.

These are **not** copies of the tickets' own unit tests. The tickets' `verify` commands test
implementation; this suite tests the promised behaviour through public surfaces, so an
implementation is free to change while the goal stays put.

## Two rules every test in this directory must follow

**1. Import the code under test INSIDE the test function, never at module scope.**

```python
@pytest.mark.epic("E3")
@pytest.mark.ticket("T-032")
def test_ranking_is_fee_and_tier_blind():
    from apps.exchange.src.ranking import rank        # <- inside, always
    ...
```

At the cycle-0 baseline none of the product code exists. A module-scope import would turn
the whole file into a *collection error*, which the runner must report as "the measuring
stick is broken" — when the truth is simply "this goal is not met yet". Lazy imports keep
every unmet goal a clean per-test failure and keep the denominator honest.

**2. Mark every test with its epic and its ticket.**

```python
@pytest.mark.epic("E6")        # E1..E8, or SPEC for cross-cutting criteria
@pytest.mark.ticket("T-062")   # the ticket responsible for making it pass
@pytest.mark.blocker("S8-1")   # optional: the SPEC S8 release blocker it guards
```

The per-epic metrics in `goals.json` count tests by their `epic` marker. An unmarked test
counts toward nothing and is a defect in the suite.

## Runner contract

`run.py` prints a bare number as its last stdout line, and distinguishes the two cases that
`references/goal-setting.md` insists must never be confused:

| situation | stdout | exit |
|---|---|---|
| suite ran, N passing | the number | 0 |
| suite ran, nothing passing | `0` | 0 |
| suite **could not run** (no pytest, no report, empty collection, unknown epic) | *nothing* | 1 |

`measure` treats a missing number as a failed measurement, which is correct: a broken
measuring stick is a top-priority defect, not a skippable inconvenience.

Modes: `--total`, `--count-passing [--epic Ex]`, `--pass-rate [--epic Ex]`, `--json`
(diagnostics to stderr).

Skipped is **not** passed. A goal you skipped is a goal you did not meet.
