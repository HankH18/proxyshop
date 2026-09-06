"""Reproductions for the simulation tickets that carry a PLACEHOLDER gate.

Same mechanism as the other ``test_repro_open_tickets.py`` files: one
``xfail(strict=True)`` test per defect measured live at HEAD, so a normal run reports
``xfailed`` and exits 0 while the ticket's gate runs ``--runxfail -k <id>`` and gets a real
``1 failed``. ``strict=True`` turns the eventual repair into an XPASS *failure*, so whoever
fixes the defect must delete the marker.

Covered here: T-242, T-265.

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
