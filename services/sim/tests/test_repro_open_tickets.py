"""Regression guards for the simulation tickets that carried a PLACEHOLDER gate.

Same mechanism as the other ``test_repro_open_tickets.py`` files: each defect got one
``xfail(strict=True)`` test measured live at HEAD, so a normal run reported ``xfailed`` and
exited 0 while the ticket's gate ran ``--runxfail -k <id>`` and got a real ``1 failed``.
``strict=True`` turns the eventual repair into an XPASS *failure*, so the marker had to come
off with the fix.

**Every marker in this file is now off — T-242, T-265 and T-303 (a) are all FIXED**, and each
section's header comment records what was measured before its repair, what changed, and the
sabotage that returns its test to ``xfailed``. So nothing here is a reproduction: each graded
test must pass in its own name on an ordinary run and must fail in its own name if its defect
returns, and a ``--runxfail -k <id>`` command quoted from ``tickets.json`` reports a pass rather
than the ``1 failed`` it once did.

The ``..._is_armed`` controls beside the graded tests were never xfail and stay. Under
``xfail(strict=True)`` ANY exception in a graded body reads as ``xfailed``, i.e. green, so every
precondition that could make a gate measure NOTHING lives in an armer where it fails in its own
name; with the markers off, that is what still separates "passes because the defect is fixed"
from "passes because it stopped measuring".

Nothing in this file touches product source.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

# =====================================================================================
# T-242 — the simulation validates only half its own ledger, and the half it skips is
#          where the defect class the sim exists to catch actually lives
# =====================================================================================
#
# STATUS: FIXED. ``run_simulation`` now grades ``[*raw_events, *ledger_sink.events]``
# (services/sim/src/runner.py:721, reported at :748) and the xfail marker below is gone.
#
# WHAT THE DEFECT WAS. ``run_simulation`` built ``ledger_sink = InMemoryLedgerSink()``, wired
# it into the auction state machine, and then never read it: the run's conformance report was
# ``ledger_contract_problems(raw_events)``, and ``raw_events`` is appended only from the
# accept path. So the 36 events the state machine emits in a default run were graded by
# nothing at all.
#
# TWO THINGS THE ORIGINAL HEADER ASSERTED ARE FALSE ON THIS TREE, and they are corrected here
# rather than left standing — a comment that has outlived its subject is the same shape as the
# T-265 carve-out this file also gates. Both were re-measured on this branch:
#
#   * "``accepted`` is emitted CORRECTLY on the accept path and INCORRECTLY on the auction
#     path IN THE SAME RUN … the heart of the finding" — NO. ``accepted`` is clean on both
#     paths. ``apps/exchange/src/auction/ledger.py`` says so in its own docstring: "``accepted``
#     used to be listed beside them and was the one that did **not** belong there … Both keys
#     are now written by ``AuctionStateMachine.accept``." There is no ``accepted`` pair.
#   * "three published bodies that ledger.py documents as wrong" / "the four known deviations"
#     — NO. TWO. The sink holds 36 events (12 ``auction_opened``, 12 ``auction_closed``, 12
#     ``accepted``) and ``ledger_contract_problems(sink.events)`` returns exactly two problems:
#     ``auction_opened`` missing ``roster_size`` and ``auction_closed`` missing
#     ``shortlist_size``. Neither is on ``accepted``.
#
# The blindness was exactly as recorded. Its payload had shrunk before this lane arrived.
#
# The gate is written as a randomised property, not as a probe for the known deviations, and
# that choice is the whole design. The deviations are somebody else's ticket, so a lane could
# satisfy an example by repairing ``apps/exchange/src/auction/state.py`` and leave the
# simulation exactly as blind. What is asserted instead is that the run reports EVERY
# published-shape problem the sink holds, wherever in the stream it sits:
#
#   (A) every problem in the synthetic events the spy injects, and
#   (B) every problem in the sink's OWN real events.
#
# (B) is the load-bearing half, because it grades the system's stream rather than the test's.
# Without it the gate cannot tell "the runner reads the whole sink" from "the runner reads a
# filtered slice of it" — and the cheap filtered slice is not hypothetical: folding the sink
# in turns the neighbouring guard in test_simulation.py red, so a lane under pressure has an
# obvious move in excluding kinds already present in ``raw_events``.
#
# Measured by an earlier lane on scratch copies — every one of these stays RED:
#   exclude kinds already in raw_events      A_missing 6,  B_missing 2
#   fold in only sink.events[:11]  (prefix)  A_missing 30
#   fold in only sink.events[-11:] (suffix)  A_missing 36
#   filter out the three state-machine kinds A_missing 7,  B_missing 4
#   snapshot the sink mid-episode-loop       A_missing 3
#   repair state.py, leave the runner alone  A_missing 44  <- variant, not instance
# and the honest fix -- ``ledger_contract_problems(raw_events + ledger_sink.events)`` --
# takes it to A_missing 0, B_missing 0.
#
# (A) AND (B) BOTH READ ONLY SINK CONTENT, so neither can tell "grades both streams" from
# "grades only the sink" — dropping the accept path from the fold leaves this whole file
# green, because the accept path happens to emit no deviation today. That hole is closed by
# ``test_t242_both_ledger_streams_reach_the_published_shape_validator`` below, which watches
# the validator itself.

#: Synthetic events injected ahead of the first real emit. Randomised so a fold that keeps
#: ``events[:K]`` cannot be tuned against a fixed head size.
_HEAD_BATCH_RANGE = (3, 8)


def _shape_pairs() -> list[tuple[str, str]]:
    """Every ``(kind, published key)`` pair ``contracts.ledger`` publishes."""
    from contracts.ledger import LEDGER_PAYLOAD_SHAPES

    return [(kind, key) for kind, body in sorted(LEDGER_PAYLOAD_SHAPES.items()) for key in body]


def _malformed_event(kind: str, dropped: str, serial: int) -> dict[str, Any]:
    """A ``kind`` event carrying its published body MINUS one published key.

    Shapes, not values: the body is read out of the published table, so a kind whose shape
    changes changes what this injects. Values are unique per event so a fix that folds the
    sink into ``raw_events`` wholesale does not trip the run's "codes never repeat" check on
    a synthetic ``code_created``.
    """
    from contracts.ledger import LEDGER_PAYLOAD_SHAPES
    from exchange.auction import build_event

    body = {
        key: f"t242-{kind}-{key}-{serial:03d}"
        for key in LEDGER_PAYLOAD_SHAPES[kind]
        if key != dropped
    }
    return build_event(kind, auction_id=f"t242-synthetic-{serial:03d}", payload=body)


def _spy_sink(cases: list[tuple[str, str]], head_batch: int) -> tuple[type, list[Any]]:
    """A drop-in ``InMemoryLedgerSink`` that interleaves known-bad events with the real ones.

    Anchoring matters more than volume, and it is the correction to the first version of this
    probe. Injecting only on the FIRST real emit puts every synthetic event at the HEAD of
    the sink, so a fix that reaches the head but not the tail — a prefix slice, a mid-loop
    snapshot — passes while the real deviations, which land later, stay unreported. Here a
    batch goes in ahead of the first real emit AND one more after EVERY real emit, so the
    first and last elements of the sink are both synthetic and no contiguous slice contains
    every injected problem.
    """
    from exchange.auction import InMemoryLedgerSink

    instances: list[Any] = []

    class _SpyLedgerSink(InMemoryLedgerSink):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            self.real_indices: list[int] = []
            self.synthetic_indices: list[int] = []
            self._pool = list(cases)
            self._serial = 0
            self._opened = False
            instances.append(self)

        def _inject(self, how_many: int) -> None:
            for _ in range(how_many):
                if not self._pool:
                    return
                kind, dropped = self._pool.pop(0)
                self._serial += 1
                self.synthetic_indices.append(len(self.events))
                self.events.append(_malformed_event(kind, dropped, self._serial))

        def emit(self, event: Mapping[str, Any]) -> None:
            if not self._opened:
                self._opened = True
                self._inject(head_batch)
            self.real_indices.append(len(self.events))
            super().emit(event)
            self._inject(1)

        @property
        def real(self) -> list[dict[str, Any]]:
            return [self.events[i] for i in self.real_indices]

        @property
        def synthetic(self) -> list[dict[str, Any]]:
            return [self.events[i] for i in self.synthetic_indices]

    return _SpyLedgerSink, instances


def _published_shape_problems(events: Any) -> set[tuple[str, str]]:
    """``(kind, problem)`` for every published-shape violation, in the run's own spelling."""
    from contracts.ledger import validate_ledger_payload

    found: set[tuple[str, str]] = set()
    for event in events:
        kind = str(event.get("kind"))
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        for problem in validate_ledger_payload(kind, payload):
            found.add((kind, str(problem)))
    return found


def test_t242_the_ledger_sink_probe_is_armed() -> None:
    """Not xfail, and the separation is why it exists.

    Under ``xfail(strict=True)`` ANY exception in the graded body is reported ``xfailed``,
    which is green — so a probe that had stopped working would be indistinguishable from the
    defect it is supposed to detect, and under the ticket's own ``--runxfail`` gate every
    dead-probe state reads as "still broken". The structural preconditions that do not need
    a simulation run are therefore asserted here, where they fail in their own name during
    ``make verify``; the ones that can only be known after a run stay inside the body.
    """
    from contracts.ledger import LEDGER_EVENT_KINDS, LEDGER_PAYLOAD_SHAPES

    assert set(LEDGER_PAYLOAD_SHAPES) <= set(LEDGER_EVENT_KINDS), (
        "the published shape table names kinds the ledger vocabulary does not"
    )
    assert len(LEDGER_PAYLOAD_SHAPES) >= 12, (
        f"the published shape table holds {len(LEDGER_PAYLOAD_SHAPES)} kinds; 18 at HEAD"
    )
    pairs = _shape_pairs()
    assert len(pairs) >= 30, f"only {len(pairs)} (kind, key) pairs to draw from; 51 at HEAD"

    # The patch target, checked rather than assumed. ``run_simulation`` imports the sink
    # INSIDE its own body, so the package attribute is what it resolves at call time; the
    # defining module ``exchange.auction.ledger`` is the wrong target and patching it is a
    # silent no-op that would leave the spy uninstalled and the sweep grading nothing.
    import exchange.auction as auction_pkg

    assert hasattr(auction_pkg, "InMemoryLedgerSink"), (
        "exchange.auction no longer exports InMemoryLedgerSink, so the monkeypatch target "
        "below is stale and the spy would never be installed"
    )

    # One malformed body really does produce a problem, or clause (A) grades nothing.
    kind, dropped = pairs[0]
    assert _published_shape_problems([_malformed_event(kind, dropped, 1)]), (
        f"dropping the published key {dropped!r} from a {kind!r} body produced no "
        "validate_ledger_payload problem; the injector or the validator has stopped working"
    )


# FIXED — the ``xfail(strict=True)`` marker that stood here was removed with the repair, not
# around it. ``run_simulation`` now grades ``[*raw_events, *ledger_sink.events]``
# (services/sim/src/runner.py). CAUSATION PROVEN, not assumed: reverting that single
# expression to ``ledger_contract_problems(raw_events)`` and changing nothing else returns
# this test to ``xfailed`` (measured: ``1 passed, 2 deselected, 1 xfailed``); restoring it
# returns it to a pass. No assertion in this test's body was touched.
def test_t242_run_grades_every_event_its_own_ledger_sink_holds(
    sim_manifest: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed body must be reported whichever path put it in the ledger."""
    from contracts.ledger import LEDGER_PAYLOAD_SHAPES

    # Not the session-scoped ``sim_run`` fixture: this run has to differ, and
    # ``services/sim/tests/_fixtures_sim.py`` says so itself — "anything that needs to see
    # the run differ builds its own".
    from sim.runner import run_simulation

    entropy = random.SystemRandom()
    cases = _shape_pairs()
    entropy.shuffle(cases)
    head_batch = entropy.randint(*_HEAD_BATCH_RANGE)

    spy_cls, instances = _spy_sink(cases, head_batch)
    monkeypatch.setattr("exchange.auction.InMemoryLedgerSink", spy_cls)

    run = run_simulation(sim_manifest, int(sim_manifest["seed"]))

    # --- the sweep is armed, on the facts only this run can establish -------------------
    assert len(LEDGER_PAYLOAD_SHAPES) >= 12, "published shape table shrank"
    assert len(cases) >= 30, f"only {len(cases)} (kind, key) pairs to draw from"
    assert len(instances) == 1, f"expected one ledger sink in the run, saw {len(instances)}"
    spy = instances[0]
    assert len(spy.real_indices) >= 24, f"the sink saw only {len(spy.real_indices)} real events"
    assert len(spy.synthetic_indices) == head_batch + len(spy.real_indices), (
        "the injection pool ran dry, so the tail of the real stream is unguarded and a "
        f"prefix-only fold would pass: {len(spy.synthetic_indices)} injected for "
        f"{len(spy.real_indices)} real emits"
    )
    assert 0 in spy.synthetic_indices, "nothing was injected ahead of the first real emit"
    assert len(spy.events) - 1 in spy.synthetic_indices, "nothing injected after the last emit"
    assert len(run.events) >= 24, f"the accept path recorded only {len(run.events)} events"

    synthetic = _published_shape_problems(spy.synthetic)
    real = _published_shape_problems(spy.real)
    reported = set(run.ledger_contract_problems)
    assert len(synthetic) >= 30, f"only {len(synthetic)} synthetic problems built"
    assert real, (
        "arming: the ledger stream the simulation drives no longer carries any "
        "published-shape deviation of its own, so clause (B) below cannot grade anything. "
        "If apps/exchange/src/auction/state.py was repaired, this gate must be RE-DERIVED "
        "against whatever the sink now emits — do not delete the clause. This is a "
        "deliberate coupling and it is the one tree shape on which this test fails rather "
        "than xpasses: state.py repaired AND the runner fixed."
    )

    # --- (A) the run reports problems in events only the sink ever held ------------------
    assert not synthetic - reported, (
        f"{len(synthetic - reported)} of {len(synthetic)} injected published-shape problems "
        f"are absent from run.ledger_contract_problems, which reported {len(reported)}. The "
        "run is not validating the ledger sink at all, or is validating only a slice of it. "
        f"Example: {sorted(synthetic - reported)[0]}"
    )

    # --- (B) and the problems in its OWN stream -----------------------------------------
    assert not real - reported, (
        f"{len(real - reported)} of {len(real)} published-shape problems in the simulation's "
        "OWN ledger stream are absent from run.ledger_contract_problems. A fold that "
        "excludes kinds already present in raw_events, or that filters the state machine's "
        f"kinds out, lands exactly here. Example: {sorted(real - reported)[0]}"
    )


def test_t242_both_ledger_streams_reach_the_published_shape_validator(
    sim_manifest: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every event of BOTH streams is handed to ``validate_ledger_payload``, not just one.

    The randomised sweep above reads only sink content, so it cannot distinguish the honest
    fold from one that grades the SINK ALONE and drops the accept path — measured, that
    substitution leaves this entire file and the whole ``services/sim`` suite green, because
    the accept path emits no deviation today. Nothing would notice until it did.

    So this watches the validator rather than the report: it counts what
    ``ledger_contract_problems`` actually submitted for grading and compares that, kind for
    kind, against the two streams the run produced. Not xfail — it is a live guard on the
    repaired behaviour, and it must fail in its own name if the fold ever narrows again.
    """
    import sys
    from collections import Counter

    import contracts.ledger as ledger_module
    from sim.runner import run_simulation

    submitted: list[str] = []
    real_validate = ledger_module.validate_ledger_payload

    def watching(kind: str, payload: Any) -> Any:
        # Only calls made from inside the run's conformance report count. The exchange also
        # validates at its own PRODUCING boundary (``build_published_event``), and counting
        # those would let a missing stream hide behind them.
        caller = sys._getframe(1).f_code.co_name
        if caller == "ledger_contract_problems":
            submitted.append(str(kind))
        return real_validate(kind, payload)

    monkeypatch.setattr(ledger_module, "validate_ledger_payload", watching)

    sink_cls, instances = _spy_sink([], 0)
    monkeypatch.setattr("exchange.auction.InMemoryLedgerSink", sink_cls)

    run = run_simulation(sim_manifest, int(sim_manifest["seed"]))

    assert len(instances) == 1, f"expected one ledger sink in the run, saw {len(instances)}"
    sink = instances[0]
    assert sink.events, "the auction state machine emitted nothing; this grades nothing"
    assert run.events, "the accept path emitted nothing; this grades nothing"
    assert submitted, (
        "ledger_contract_problems submitted no event at all to validate_ledger_payload — "
        "either the report is not being built or the frame filter above is stale"
    )

    accept_path = Counter(str(event.get("kind")) for event in run.events)
    auction_path = Counter(str(event.get("kind")) for event in sink.events)
    graded = Counter(submitted)
    assert graded == accept_path + auction_path, (
        "the run graded a different stream from the one it produced.\n"
        f"  accept path emitted: {dict(sorted(accept_path.items()))}\n"
        f"  auction sink emitted: {dict(sorted(auction_path.items()))}\n"
        f"  actually graded:      {dict(sorted(graded.items()))}\n"
        f"  missing from grading: {dict(sorted(((accept_path + auction_path) - graded).items()))}"
    )

    # And the separation runner.py promises in the same breath: the sink is NOT folded into
    # the hash-chained record, so the minted-code scan still sees the accept path only.
    assert len(run.codes) == accept_path["code_created"], (
        f"the run reported {len(run.codes)} minted codes for "
        f"{accept_path['code_created']} accept-path code_created events; the sink has been "
        "folded into raw_events, which double-counts the record rather than widening the audit"
    )


# =====================================================================================
# T-265 — the confinement guard whitelists, BY NAME, the one defect it was written for,
#          so it is green whether that defect is live or repaired
# =====================================================================================
#
# ``services/sim/tests/test_simulation.py``'s
# ``test_no_kind_other_than_code_created_deviates_from_its_published_payload`` asserts::
#
#     deviating = {kind for kind, _ in sim_run.ledger_contract_problems}
#     assert deviating <= {"code_created"}
#
# ``<=`` PERMITS ``code_created`` to deviate. The guard therefore returns the same verdict
# whether T-235 is live or repaired, which is what makes it uncitable as a guard for T-235 —
# the carve-out-with-no-exercising-case shape.
#
# Measured at HEAD from this worktree: ``run.ledger_contract_problems`` is EMPTY — zero
# problems, so ``deviating`` is ``set()``. ``code_created`` no longer deviates at all (see
# ``apps/exchange/src/auction/ledger.py``'s own docstring on T-235), which means the carve-out
# has been dead for some time and the guard could not tell. That is the finding, demonstrated
# rather than argued: an allowlist entry whose subject has gone away, still being honoured.
#
# The gate EXECUTES the guard against a stand-in run rather than reading its source. Reading
# it would be grading a file inside this lane's own write scope, which measures nothing; and
# T-265's subject IS a test, so there is nothing else to drive.
#
# What this gate forbids, deliberately:
#   * leaving ``<=`` alone                       -> still passes on a deviating code_created
#   * widening to ``<= {"code_created", ...}``   -> ditto; a widened allowlist is the cheap
#                                                   move once T-242 folds the sink in
#   * ``== {"code_created"}``                    -> passes here, but reds the guard against
#                                                   the real run, so make verify catches it
# Only an EXACT set naming the deviations that are actually live turns this green, and such a
# set expires by itself the moment any one of them is repaired.


def _simulation_module() -> Any:
    """``services/sim/tests/test_simulation.py``, preferring the copy pytest already loaded.

    Under the ticket's own gate only this file is collected, so the fallback load is the
    normal path; under ``make verify`` the module is already in ``sys.modules`` and
    re-executing it would be a side effect nobody asked for.
    """
    import importlib.util
    import sys
    from pathlib import Path

    target = Path(__file__).resolve().with_name("test_simulation.py")
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename and Path(filename).resolve() == target:
            return module

    spec = importlib.util.spec_from_file_location("_t265_simulation_probe", target)
    assert spec is not None and spec.loader is not None, target
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The guard under test, by the name the ticket records.
_CONFINEMENT_GUARD = "test_no_kind_other_than_code_created_deviates_from_its_published_payload"


class _StandInRun:
    """The one attribute the confinement guard reads off a run."""

    def __init__(self, problems: Sequence[tuple[str, str]]) -> None:
        self.ledger_contract_problems = tuple(problems)


def _problem(kind: str, key: str) -> tuple[str, str]:
    """A ``(kind, problem)`` pair in ``ledger_contract_problems``' own spelling."""
    return (kind, f"{kind!r} payload is missing published key {key!r}")


def _live_deviating_kinds(run: Any) -> set[str]:
    """The kinds actually deviating in a real run — the set the guard must accept exactly."""
    return {kind for kind, _ in run.ledger_contract_problems}


def test_t265_the_confinement_guard_probe_is_armed(sim_run: Any) -> None:
    """Not xfail, and the separation is why it exists.

    Under ``xfail(strict=True)`` ANY exception in the graded body reports ``xfailed``, which
    is green — so a probe that could no longer find or call the guard would be
    indistinguishable from the defect. Everything that must hold for the red below to mean
    "the guard is blind" is asserted here, where it fails in its own name.
    """
    guard = getattr(_simulation_module(), _CONFINEMENT_GUARD, None)
    assert callable(guard), (
        f"{_CONFINEMENT_GUARD} is no longer defined in test_simulation.py; T-265's subject "
        "has moved and this gate must be re-derived against wherever it went"
    )

    # It is satisfiable by the real run. A guard that reds against the system it grades would
    # make the assertion below meaningless — every input would raise.
    guard(sim_run)

    # And it is a live assertion, not a no-op: a kind nobody has ever excused reds it.
    with pytest.raises(AssertionError):
        guard(_StandInRun([_problem("bid_received", "store_id")]))

    # The one-variable comparison the gate below depends on. The stand-in it builds carries
    # ``code_created`` and NOTHING ELSE, so if code_created were also deviating in the real
    # run the gate would be comparing the guard against its own live input and its red would
    # mean nothing. Asserted here, un-xfailed, so that state fails in its own name.
    live = _live_deviating_kinds(sim_run)
    assert "code_created" not in live, (
        "code_created deviates in the real run again, so the gate below is no longer a "
        "one-variable comparison; re-derive it against whatever kind is now unexercised"
    )
    # The neighbour sweep below needs something to perturb. An empty live set makes its
    # removal and swap clauses vacuous, and a vacuous clause in a test about vacuous clauses
    # is not a joke this file gets to make.
    assert len(live) >= 2, (
        f"the run reports {len(live)} deviating ledger kinds ({sorted(live)}); the gate's "
        "neighbour sweep needs at least two to perturb. If the auction path was repaired "
        "upstream this gate must be RE-DERIVED against whatever still deviates — do not "
        "delete the sweep"
    )


# FIXED — the ``xfail(strict=True)`` marker that stood here was removed with the repair. The
# guard in test_simulation.py now asserts an EXACT set instead of ``deviating <=
# {"code_created"}``. CAUSATION PROVEN by three measured tree shapes, with the T-242 runner
# fix held constant in all three:
#   assert deviating == KNOWN_DEVIATING_LEDGER_KINDS          -> this test passes
#   assert deviating <= {"code_created"}                      -> back to xfailed
#   assert deviating <= {"code_created", <the two auction kinds>}  -> back to xfailed
# The third shape matters most: widening the allowlist is the cheap move a lane reaches for
# once T-242 makes the auction path visible, and it is rejected here while leaving every other
# test green. No assertion in this test's body was touched.
def test_t265_the_confinement_guard_notices_its_whitelisted_kind_deviating(sim_run: Any) -> None:
    """A guard that cannot see its own carve-out's subject is not guarding it.

    The stand-in run reports exactly what T-235 looked like while it was live, and NOTHING
    ELSE: ``code_created`` missing a published key. The guard must reject it. If it accepts
    it, the guard's verdict is independent of whether ``code_created`` conforms, which is the
    whole finding.

    The stand-in deliberately carries no other problem. An earlier draft of this gate folded
    the real run's live deviations in alongside; measured, that made the ORIGINAL ``<=
    {"code_created"}`` assertion satisfy this gate the moment T-242 landed, because the folded
    auction-path kinds broke the subset on their own and ``code_created`` never had to be
    looked at. One variable, or the gate grades the wrong thing.
    """
    guard = getattr(_simulation_module(), _CONFINEMENT_GUARD)

    with pytest.raises(AssertionError):
        guard(_StandInRun([_problem("code_created", "code")]))

    # And the guard must accept EXACTLY the live set — no neighbour of it. Without these the
    # gate grades only "code_created is no longer excused", and a one-character revert to
    # ``deviating <= KNOWN_DEVIATING_LEDGER_KINDS`` — the very allowlist shape T-265 exists to
    # forbid, recreated one level up — passes it with the whole suite green. Measured; that is
    # the substitution an adversarial verifier found, and each clause below kills one operator:
    #   removal     kills ``<=``   (a repaired deviation would leave a stale entry forever)
    #   addition    kills ``>=``   (a brand-new deviating kind would be permitted)
    #   swap        kills ``len(deviating) == 2`` and any other cardinality-only check
    live = sorted(_live_deviating_kinds(sim_run))
    novel = "bid_received"
    neighbours = {
        f"one fewer: {sorted(set(live) - {live[0]})}": set(live) - {live[0]},
        f"one more: {sorted({*live, novel})}": {*live, novel},
        f"one swapped: {sorted({*live[1:], novel})}": {*live[1:], novel},
    }
    for label, kinds in neighbours.items():
        with pytest.raises(AssertionError, match="deviating"):
            guard(_StandInRun([_problem(kind, "some-published-key") for kind in sorted(kinds)]))
            pytest.fail(f"the guard accepted a set that is not the live one — {label}")


# =====================================================================================
# T-303 (a) — the delisting the run computes is sealed nowhere
# =====================================================================================
#
# STATUS: FIXED. ``run_simulation`` now builds the snapshot BEFORE it verifies, seals every
# ``delistings`` event the snapshot produced through ``trust.events.append``, and only then
# calls ``event_store.verify()`` — so the chain that is verified is the chain that holds the
# decision (services/sim/src/runner.py).
#
# WHAT THE DEFECT WAS, measured on the approved manifest at HEAD before the repair:
#
#   run.events                 36  (12 accepted, 12 code_created, 12 checkout_redirect)
#   run.snapshot["delistings"]  1  ('blacklisted', 'store-brightbean', score 0.0729 vs 0.35)
#   appends of that delisting   0
#
# ``run_simulation`` appended only ``outcome.events`` from ``exchange.accept`` inside the
# episode loop, called ``event_store.verify()``, and THEN called ``build_snapshot(...)``. The
# ``delistings`` the snapshot computed rode out on the returned dataclass and were appended to
# nothing. The platform decided a store should be delisted and the ledger an exchange, an
# auditor or an appeal reads never heard about it.
#
# The ordering matters as much as the append. Verifying first and appending after would leave
# the run's ``chain_ok`` a statement about a chain that is missing its last events — a weaker
# check than the one the field's name promises — so the sequence is snapshot, seal, verify.
#
# WHY THESE GATES WATCH THE WRITER AND NOT THE REPORT. ``sim.runner.normalise_event``
# deliberately drops ``event_id`` and ``ts``, so comparing ids against ``run.events`` grades
# the single string ``"None"``; and splicing the delistings into a reported list after the
# fact would satisfy any report-shaped assertion while sealing nothing. So the spy sits on
# ``InMemoryEventStore.append`` — the one seam ``trust.events.append`` delegates to — and the
# appends are grouped BY INSTANCE, because appending to a throwaway store the run never
# verifies is not sealing either.

#: The two kinds ``apps/trust/src/snapshot/delisting.py`` emits. Both are in the frozen
#: ``LEDGER_EVENT_KINDS`` vocabulary, which is what makes them appendable at all.
_T303A_DELISTING_KINDS = frozenset({"blacklisted", "blacklist_expired"})


def _t303a_seal_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[str, int, Any]], list[Any]]:
    """Watch ``InMemoryEventStore``'s write and verify seams, in call order.

    Returns the ordered log of ``("append" | "verify", id(store), payload)`` and the list of
    live store objects. The second return value is not decoration: ``id()`` is unique only
    among LIVE objects, so a discarded throwaway store could otherwise inherit the principal
    store's id and make an append to it look like an append to the run's own chain. The
    caller must keep the returned list referenced for as long as it reads the log.
    """
    from trust.events.store import InMemoryEventStore

    log: list[tuple[str, int, Any]] = []
    alive: list[Any] = []
    real_append = InMemoryEventStore.append
    real_verify = InMemoryEventStore.verify

    def append_spy(self: Any, event: Mapping[str, Any]) -> Any:
        alive.append(self)
        log.append(("append", id(self), dict(event)))
        return real_append(self, event)

    def verify_spy(self: Any) -> Any:
        alive.append(self)
        report = real_verify(self)
        log.append(("verify", id(self), dict(report)))
        return report

    monkeypatch.setattr(InMemoryEventStore, "append", append_spy)
    monkeypatch.setattr(InMemoryEventStore, "verify", verify_spy)
    return log, alive


def _t303a_principal(log: Sequence[tuple[str, int, Any]]) -> int:
    """The id of the store that took the most appends — the run's own chain."""
    from collections import Counter

    counts = Counter(store for verb, store, _ in log if verb == "append")
    assert counts, (
        "the spy saw no append at all go through InMemoryEventStore during a whole "
        "simulation run. The write seam moved, so every assertion below would conclude from "
        "nothing"
    )
    principal, appends = counts.most_common(1)[0]
    assert appends >= 20, (
        f"the busiest event store took only {appends} appends during a whole simulation run "
        f"(36 when this was written), across {len(counts)} store(s). The run's chain is not "
        f"being written through this seam any more"
    )
    return int(principal)


def test_t303a_the_delisting_seal_probe_is_armed() -> None:
    """Not xfail, and the separation is why it exists.

    Everything the gates below need that does NOT require a simulation run is asserted here,
    where a stale probe fails in its own name instead of being reported as the defect it was
    written to detect.
    """
    import trust.events as events_pkg
    from contracts.ledger import LEDGER_EVENT_KINDS
    from trust.events.store import InMemoryEventStore

    # The patch target, checked rather than assumed. ``run_simulation`` does
    # ``from trust.events import InMemoryEventStore, append`` inside its own body; if the
    # package attribute were a different class object from the one patched below, the spy
    # would be installed on a door the run never opens.
    assert events_pkg.InMemoryEventStore is InMemoryEventStore, (
        "trust.events.InMemoryEventStore is no longer the class defined in "
        "trust.events.store, so the monkeypatch below would be a silent no-op"
    )
    assert callable(getattr(InMemoryEventStore, "append", None)), (
        "InMemoryEventStore has no callable `append`; trust.events.append delegates to it by "
        "getattr, so the seam this gate watches has moved"
    )

    # An append cannot seal a kind the frozen vocabulary does not carry, so the two kinds the
    # delisting seam emits have to be in it or the fix below is impossible rather than absent.
    assert _T303A_DELISTING_KINDS <= set(LEDGER_EVENT_KINDS), (
        f"the delisting kinds {sorted(_T303A_DELISTING_KINDS - set(LEDGER_EVENT_KINDS))} are "
        "outside the frozen LedgerEvent vocabulary, so they could not be appended at all"
    )

    from apps.trust.src.snapshot.delisting import BLACKLIST_EXPIRED_KIND, BLACKLISTED_KIND

    assert {BLACKLISTED_KIND, BLACKLIST_EXPIRED_KIND} == _T303A_DELISTING_KINDS, (
        "the delisting seam emits kinds this gate does not know about: "
        f"{sorted({BLACKLISTED_KIND, BLACKLIST_EXPIRED_KIND})}"
    )


def test_t303a_every_delisting_the_run_computes_is_sealed_into_the_chain_it_verifies(
    sim_manifest: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision is computed. It is only recorded when it also reaches the writer.

    Two properties, and the second is the one a naive reorder loses:

    1. every event in ``run.snapshot["delistings"]`` went through the append seam of the
       store that holds the run's own chain — not a throwaway, and not merely into a list on
       the returned dataclass;
    2. the run's ``verify()`` ran AFTER those appends. ``verify`` exists to check the chain,
       and a chain verified before its last events land is a weaker check than one verified
       after — ``chain_ok`` would be a statement about a prefix.
    """
    from sim.runner import run_simulation

    log, alive = _t303a_seal_spy(monkeypatch)
    run = run_simulation(sim_manifest, int(sim_manifest["seed"]))
    assert alive, "the spy kept no store alive; the id grouping below is unsound"

    principal = _t303a_principal(log)
    delistings = list(run.snapshot["delistings"])
    assert delistings, (
        "the run computed no delisting at all, so this gate grades nothing. The approved "
        "manifest's dishonest store is scored far below BLACKLIST_THRESHOLD by design; if "
        "that is no longer true the manifest or the scorer moved and this gate must be "
        "re-derived, not deleted"
    )

    sealed_ids = {
        str(event.get("event_id"))
        for verb, store, event in log
        if verb == "append" and store == principal
    }
    elsewhere = {
        str(event.get("event_id"))
        for verb, store, event in log
        if verb == "append" and store != principal
    }
    dropped = [
        f"{event['kind']} for {event['payload']['store_id']} ({event['event_id']})"
        for event in delistings
        if str(event.get("event_id")) not in sealed_ids
    ]
    assert dropped == [], (
        f"{len(dropped)} of {len(delistings)} delisting decisions the run computed never "
        f"reached the store that seals the run's chain: {dropped}."
        + (
            " They DID reach some other event store — sealing them into a chain the run "
            "never verifies is not sealing them."
            if any(str(event.get("event_id")) in elsewhere for event in delistings)
            else ""
        )
    )

    # (2) the order. The last delisting append must precede the run's own verify.
    positions = [
        index
        for index, (verb, store, event) in enumerate(log)
        if verb == "append"
        and store == principal
        and str(event.get("event_id")) in {str(row["event_id"]) for row in delistings}
    ]
    verifies = [
        index
        for index, (verb, store, _) in enumerate(log)
        if verb == "verify" and store == principal
    ]
    assert verifies, (
        "the run never verified the store that holds its chain, so `chain_ok` is not a "
        "statement about anything this run wrote"
    )
    assert max(positions) < max(verifies), (
        f"the run's last verify() of its own chain ran at call {max(verifies)}, BEFORE the "
        f"last delisting was appended at call {max(positions)}. The delisting is in the "
        "chain but outside the check, so `chain_ok` describes a prefix that does not contain "
        "the decision"
    )
    assert run.chain_ok, "the chain that now carries the delistings does not verify"


def test_t303a_an_honest_store_never_acquires_a_delisting(
    sim_manifest: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control. Sealing every store is not a fix for sealing none of them.

    A repair that appended a ``blacklisted`` event per rostered store would satisfy the gate
    above completely. So this reads the delisting-kind events that actually reached the run's
    chain and requires them to name EXACTLY the stores the trust snapshot delisted — and it
    names the honest control store explicitly, so the assertion is about a store the run
    really rewarded rather than about an empty set.
    """
    from sim.runner import run_simulation
    from trust.scoring import BLACKLIST_THRESHOLD

    log, alive = _t303a_seal_spy(monkeypatch)
    run = run_simulation(sim_manifest, int(sim_manifest["seed"]))
    assert alive, "the spy kept no store alive; the id grouping below is unsound"
    principal = _t303a_principal(log)

    decided = {str(event["payload"]["store_id"]) for event in run.snapshot["delistings"]}
    honest = {
        str(store_id)
        for store_id, entry in run.snapshot["stores"].items()
        if float(entry["score"]) >= BLACKLIST_THRESHOLD and str(store_id) not in decided
    }
    winners = {episode.winner for episode in run.episodes if episode.accepted and episode.winner}
    assert decided, "no store was delisted; the sweep below cannot discriminate"
    assert honest & winners, (
        f"no store both stayed above BLACKLIST_THRESHOLD and won an accepted auction — "
        f"honest={sorted(honest)}, winners={sorted(winners)}. Without one this control is "
        "asserting about a store the run never rewarded"
    )

    recorded = {
        str(event.get("store_id"))
        for verb, store, event in log
        if verb == "append"
        and store == principal
        and str(event.get("kind")) in _T303A_DELISTING_KINDS
    }
    assert recorded == decided, (
        "the delisting events sealed into the run's chain do not name the stores the trust "
        f"snapshot delisted.\n  snapshot delisted: {sorted(decided)}\n  chain recorded:    "
        f"{sorted(recorded)}\n  recorded but not decided: {sorted(recorded - decided)}\n"
        f"  decided but not recorded: {sorted(decided - recorded)}\n"
        "Recording a decision nobody made is not the fix for dropping the one that was."
    )
    assert not (recorded & honest), (
        f"the run sealed a delisting against {sorted(recorded & honest)}, which the trust "
        f"engine scored at or above {BLACKLIST_THRESHOLD} and which won auctions in this very "
        "run. A fix that delists everybody has not distinguished anything"
    )


def test_t303a_sealing_the_delisting_refuses_none_of_the_runs_honest_traffic(
    sim_manifest: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new write is a wall in the run's own path. Drive the real traffic through it.

    Every configuration here is REAL traffic out of this repository: the approved manifest,
    the seeded catalogs ``fixtures.generator`` builds, the whole episode budget, each shorter
    prefix of it, the empty run, and four seeds' worth of different catalogs and prices. None
    of it is a corpus hand-written by the same mind that chose where the append goes.

    The expected accept-path stream is read off ``run.episodes[*].ledger_kinds`` — the run's
    own record of what ``exchange.accept`` returned — rather than from a count typed in here,
    so a seed on which an episode denies instead of accepting is graded against what that run
    actually produced. What must survive:

    * every accept-path event the run produced is STILL sealed, in order (the reorder did not
      displace, drop, or duplicate the traffic that was already being recorded);
    * the chain still verifies, and the head hash is still published;
    * the minted discount codes are still exactly the codes the sealed ``code_created``
      events carry;
    * a run with nothing to delist still completes and seals its accept path — the append
      loop must be a no-op on an empty decision, not a crash and not an empty-event append.
    """
    from sim.runner import minted_codes, run_simulation

    budget = int(sim_manifest["episode_budget"])
    approved_seed = int(sim_manifest["seed"])
    cases: list[tuple[int, int]] = [
        (approved_seed, 0),
        (approved_seed, 1),
        (approved_seed, 2),
        (approved_seed, budget),
        (approved_seed + 1, budget),
        (approved_seed + 7, budget),
        (12345, budget),
    ]
    empty_seen = False
    for seed, episodes in cases:
        label = f"seed={seed} episodes={episodes}"
        log, alive = _t303a_seal_spy(monkeypatch)
        run = run_simulation(sim_manifest, seed, episodes=episodes)
        assert alive, f"{label}: the spy kept no store alive"

        sealed = [event for verb, _, event in log if verb == "append"]
        accept_kinds = [
            str(event.get("kind"))
            for event in sealed
            if str(event.get("kind")) not in _T303A_DELISTING_KINDS
        ]
        expected = [str(kind) for episode in run.episodes for kind in episode.ledger_kinds]
        assert accept_kinds == expected, (
            f"{label}: the accept path produced {expected} and the chain sealed "
            f"{accept_kinds}. Sealing the delisting has displaced the traffic that was "
            "already being recorded"
        )
        if episodes:
            assert run.chain_ok, f"{label}: the chain no longer verifies"
            assert run.head_hash, f"{label}: no head hash was published"
            assert list(run.codes) == minted_codes(sealed), (
                f"{label}: the codes the run reports ({len(run.codes)}) are no longer the "
                f"codes it actually sealed ({len(minted_codes(sealed))})"
            )
            assert len(set(run.codes)) == len(run.codes), (
                f"{label}: a discount code was minted twice: {run.codes}"
            )
        if not run.snapshot["delistings"]:
            empty_seen = True
            spurious = [
                event for event in sealed if str(event.get("kind")) in _T303A_DELISTING_KINDS
            ]
            assert spurious == [], (
                f"{label}: a run that computed no delisting sealed {len(spurious)} delisting "
                f"events anyway: {spurious}"
            )
    assert empty_seen, (
        "no configuration in this sweep produced an empty delisting list, so the "
        "nothing-to-seal path was never exercised"
    )
