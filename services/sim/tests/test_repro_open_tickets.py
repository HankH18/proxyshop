"""Reproductions for the simulation tickets that carry a PLACEHOLDER gate.

Same mechanism as the other ``test_repro_open_tickets.py`` files: one
``xfail(strict=True)`` test per defect measured live at HEAD, so a normal run reports
``xfailed`` and exits 0 while the ticket's gate runs ``--runxfail -k <id>`` and gets a real
``1 failed``. ``strict=True`` turns the eventual repair into an XPASS *failure*, so whoever
fixes the defect must delete the marker.

Covered here: T-242.

Nothing in this file touches product source.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from typing import Any

import pytest

# =====================================================================================
# T-242 — the simulation validates only half its own ledger, and the half it skips is
#          where the defect class the sim exists to catch actually lives
# =====================================================================================
#
# ``sim.runner.run_simulation`` builds ``ledger_sink = InMemoryLedgerSink()``
# (services/sim/src/runner.py:559), wires it into the auction state machine (:560), and then
# never reads it: the run's conformance report is ``ledger_contract_problems(raw_events)``
# (:727), and ``raw_events`` is appended only from the accept path (:661). So the 36 events
# the state machine emits in a default run — including three published bodies that
# ``apps/exchange/src/auction/ledger.py:134-139`` documents as wrong in its own docstring —
# are graded by nothing at all.
#
# The heart of the finding is that ``accepted`` is emitted CORRECTLY on the accept path and
# INCORRECTLY on the auction path IN THE SAME RUN — the identical one-kind-two-bodies defect
# the sim successfully catches for ``code_created``. It catches that one only because the
# stream it inspects happens to contain it.
#
# This is written as a randomised property, not as a probe for the four known deviations,
# and that choice is the whole design. The deviations are somebody else's ticket, so a lane
# could satisfy an example by repairing ``apps/exchange/src/auction/state.py``'s three bodies
# and leave the simulation exactly as blind. What is asserted instead is that the run reports
# EVERY published-shape problem the sink holds, wherever in the stream it sits:
#
#   (A) every problem in the synthetic events the spy injects, and
#   (B) every problem in the sink's OWN real events.
#
# (B) is the load-bearing half, because it grades the system's stream rather than the test's.
# Without it the gate cannot tell "the runner reads the whole sink" from "the runner reads a
# filtered slice of it" — and the cheap filtered slice is not hypothetical: folding the sink
# in turns the neighbouring guard in test_simulation.py red, so a lane under pressure has an
# obvious move in excluding kinds already present in ``raw_events``, which kills exactly the
# ``accepted`` pair that is the point of the ticket.
#
# Measured, on scratch copies of runner.py and state.py — every one of these stays RED:
#   exclude kinds already in raw_events      A_missing 6,  B_missing 2
#   fold in only sink.events[:11]  (prefix)  A_missing 30
#   fold in only sink.events[-11:] (suffix)  A_missing 36
#   filter out the three state-machine kinds A_missing 7,  B_missing 4
#   snapshot the sink mid-episode-loop       A_missing 3
#   repair state.py, leave the runner alone  A_missing 44  <- variant, not instance
# and the honest fix -- ``ledger_contract_problems(raw_events + ledger_sink.events)`` --
# takes it to A_missing 0, B_missing 0, i.e. XPASS.

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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-242: run_simulation never reads its own ledger_sink — the conformance report is "
        "built from raw_events (the accept path) only, so every event the auction state "
        "machine emits is ungraded, including the `accepted` body that is malformed on that "
        "path and well-formed on the accept path in the same run; remove this marker with "
        "the fix"
    ),
)
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
