"""T-043 — shadow mode, activation, and trust-event intake for the hosted store advocate.

The ticket's three acceptance criteria, written as behaviour, plus the four things the
frozen goal cannot see and this gate exists to hold:

1. **Shadow computes and logs a full bid and submits NOTHING.** Not "the submitter counted
   zero" — a disconnected wire counts zero too. The submitter here is a tripwire that
   raises on invocation *and* on any attribute read, so the criterion becomes "shadow mode
   never so much as looked at the submitter", and the same object in ``active`` mode proves
   the tripwire really fires (:func:`test_shadow_mode_never_touches_the_submitter`).
2. **Activation flips submission on, and the kill switch flips it off, on the SAME object.**
   Literally: ``runner.mode = "active"`` on a live instance, no reconstruction.
3. **An injected `TrustEventPayload` deterministically changes the next bid's rationale** —
   and, held here and nowhere else, changes NOTHING ELSE: the offer stays byte-identical.
   Trust moves the posture the agent states; it never moves the number (R10/S4).

And the four this file holds alone:

* **The rationale is not on the `Bid`.** `contracts.Bid` is ``extra="forbid"`` and has
  `message`, never `rationale`; attaching one raises. So the rationale rides on the WRAPPER
  the sink is handed. :func:`test_the_rationale_rides_on_the_wrapper_because_bid_forbids_it`
  asserts both halves — the Bid rejects it, the wrapper carries it — so a later "tidy-up"
  that moves the rationale onto the bid goes red here instead of at the boundary.
* **Logging happens before submitting.** A submitted bid that was never logged is the one
  ordering R7 cannot tolerate.
* **Activation with no submitter is refused at the flip**, not discovered at the first
  auction.
* **Byte-identical determinism** across two runners on identical input (S4).

**Section 5 exists because the first version of this file was not enough.** An adversarial
mutation run applied 36 mutations to `runner.py`; 13 survived BOTH this gate and the frozen
grader, including `_envelope_states` returning `active` instead of `shadow` when an envelope
states no activation — a store that submits live bids because its envelope forgot a key. Every
test in section 5 names the mutant it kills and was itself verified to fail against that mutant.
The frozen grader caught nothing this file missed: it cannot see ordering, the per-store seal,
determinism-of-order, or the decline path at all.

Product code is imported INSIDE each test body, following the acceptance suite's own
authoring rule: an unbuilt module is then a clean per-test failure rather than a collection
error that takes the directory down.

Offline and clock-free by construction: no network, no LLM, no database, no container, no
wall-clock assertion, no unseeded randomness.
"""

from __future__ import annotations

import dataclasses

import pytest

# ---------------------------------------------------------------------------
# 1. Shadow mode: logs everything, submits nothing.
# ---------------------------------------------------------------------------


def test_shadow_mode_never_touches_the_submitter(
    modes_context, modes_request, modes_recorder, modes_tripwire
):
    """R7: three shadow auctions, and the submitter is never called *or even read*.

    The negative claim ("it submitted nothing") is proved by an object that cannot be used
    silently, and the positive control below proves that object is not inert.
    """
    from store_agent.modes import AgentRunner

    sink = modes_recorder("sink")
    tripwire = modes_tripwire("submitter")
    runner = AgentRunner(modes_context(), sink=sink, submitter=tripwire, mode="shadow")

    for index in range(3):
        runner.run(modes_request(f"auc-{index:04d}"))

    assert sink.count == 3, "a shadow-mode store must log every would-be bid"

    # Positive control: the very same tripwire, in the very same runner, once activated.
    # Without this the assertion above would also pass against a submitter that was never
    # wired in at all.
    runner.mode = "active"
    with pytest.raises(modes_tripwire.Touched):
        runner.run(modes_request("auc-0003"))


def test_shadow_mode_needs_no_submitter_at_all(modes_context, modes_request, modes_recorder):
    """A store that has not been activated can run with no submission path in existence."""
    from store_agent.modes import AgentRunner

    sink = modes_recorder("sink")
    runner = AgentRunner(modes_context(), sink=sink, submitter=None, mode="shadow")

    runner.run(modes_request("auc-0001"))

    assert sink.count == 1


def test_every_logged_entry_carries_a_fully_formed_offer_and_a_rationale(
    modes_context, modes_request, modes_recorder, modes_first_value
):
    """R7: the shadow log is the audit trail, so it holds the priced offer and the why."""
    from store_agent.modes import AgentRunner

    sink = modes_recorder("sink")
    runner = AgentRunner(modes_context(), sink=sink, submitter=None, mode="shadow")
    runner.run(modes_request("auc-0001"))

    entry = sink.only()
    offer = modes_first_value(entry, "offer")
    assert isinstance(offer, dict) and offer.get("unit_price") is not None, (
        f"a shadow log entry must contain a fully-formed offer: {entry!r}"
    )
    rationale = modes_first_value(entry, "rationale")
    assert isinstance(rationale, str) and rationale.strip(), (
        f"a shadow log entry must carry a non-empty rationale: {entry!r}"
    )


def test_the_sink_is_called_exactly_once_per_auction(modes_context, modes_request, modes_recorder):
    """One auction, one log entry — a double-logged bid is a double-counted shadow result."""
    from store_agent.modes import AgentRunner

    sink = modes_recorder("sink")
    runner = AgentRunner(modes_context(), sink=sink, submitter=None, mode="shadow")

    for index in range(5):
        runner.run(modes_request(f"auc-{index:04d}"))

    assert sink.count == 5


# ---------------------------------------------------------------------------
# 2. Activation and the kill switch, on a live object.
# ---------------------------------------------------------------------------


def test_activation_and_the_kill_switch_work_on_the_same_live_object(
    modes_context, modes_request, modes_recorder
):
    """R7/R6: no restart, no reconstruction — the mode is state on the running instance."""
    from store_agent.modes import AgentRunner

    sink = modes_recorder("sink")
    submitter = modes_recorder("submitter")
    runner = AgentRunner(modes_context(), sink=sink, submitter=submitter, mode="shadow")

    runner.run(modes_request("auc-0001"))
    assert submitter.count == 0

    runner.mode = "active"
    runner.run(modes_request("auc-0002"))
    assert submitter.count == 1, "activation must submit the next bid without a restart"

    runner.mode = "killed"
    runner.run(modes_request("auc-0003"))
    assert submitter.count == 1, "the kill switch must stop submission again on the same object"

    assert sink.count == 3, "every mode must still log the bid it computed"


def test_active_mode_submits_the_very_bid_it_logged(
    modes_context, modes_request, modes_recorder, modes_canon
):
    """What went to the exchange and what went to the audit log agree field for field.

    Field-for-field rather than by object identity, deliberately — see
    :func:`test_a_mutating_submitter_cannot_rewrite_the_audit_row`, which is why they must NOT
    be the same object. The requirement being held is that the exchange gets the bid that was
    logged, not a second one computed for the occasion.
    """
    from store_agent.modes import AgentRunner

    sink = modes_recorder("sink")
    submitter = modes_recorder("submitter")
    runner = AgentRunner(modes_context(), sink=sink, submitter=submitter, mode="active")

    entry = runner.run(modes_request("auc-0001"))
    submitted = submitter.only()

    assert modes_canon(submitted) == modes_canon(entry.answer), (
        "the submitted bid must equal the answer the runner logged, not a re-render"
    )
    assert modes_canon(sink.only()) == modes_canon(entry)


def test_the_bid_is_logged_before_it_is_submitted(modes_context, modes_request, modes_recorder):
    """An unlogged submission is the one ordering R7 cannot tolerate.

    Proved by a sink that raises: if the submit came first, the submitter would already
    hold a call when the sink blew up.
    """
    from store_agent.modes import AgentRunner

    submitter = modes_recorder("submitter")

    class _ExplodingSink:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("the audit sink is down")

    runner = AgentRunner(modes_context(), sink=_ExplodingSink(), submitter=submitter, mode="active")

    with pytest.raises(RuntimeError, match="audit sink is down"):
        runner.run(modes_request("auc-0001"))

    assert submitter.count == 0, "nothing may be submitted once logging has failed"


def test_the_mode_vocabulary_is_the_envelopes_own_and_garbage_is_refused(
    modes_context, modes_recorder
):
    """The three modes are `contracts.EnvelopeActivation`, not strings this module invented."""
    from contracts import EnvelopeActivation
    from store_agent.modes import AgentRunner

    runner = AgentRunner(
        modes_context(), sink=modes_recorder(), submitter=modes_recorder(), mode="shadow"
    )
    assert runner.mode is EnvelopeActivation.shadow

    runner.mode = EnvelopeActivation.active
    assert runner.mode is EnvelopeActivation.active

    for rubbish in ("paused", "ACTIVE", "", None, 1):
        with pytest.raises(ValueError):
            runner.mode = rubbish
    assert runner.mode is EnvelopeActivation.active, "a refused flip must not change the mode"


def test_the_mode_defaults_to_the_envelopes_activation(
    modes_context, modes_envelope, modes_recorder
):
    """An un-activated envelope must not produce a submitting runner because a caller forgot."""
    from contracts import EnvelopeActivation
    from store_agent.modes import AgentRunner

    default = AgentRunner(modes_context(), sink=modes_recorder(), submitter=modes_recorder())
    assert default.mode is EnvelopeActivation.shadow

    activated = modes_context(envelope=modes_envelope(activation="active"))
    assert (
        AgentRunner(activated, sink=modes_recorder(), submitter=modes_recorder()).mode
        is EnvelopeActivation.active
    )


def test_activating_a_runner_that_has_no_submitter_is_refused_at_the_flip(
    modes_context, modes_recorder
):
    """Discovered at the flip, not at the first auction — a store activated into a void
    would otherwise look live and bid nowhere."""
    from store_agent.modes import AgentRunner

    runner = AgentRunner(modes_context(), sink=modes_recorder(), submitter=None, mode="shadow")
    with pytest.raises(ValueError, match="submitter"):
        runner.mode = "active"

    with pytest.raises(ValueError, match="submitter"):
        AgentRunner(modes_context(), sink=modes_recorder(), submitter=None, mode="active")


def test_a_runner_with_no_sink_is_refused(modes_context, modes_recorder):
    """Shadow mode IS the log. A runner with nowhere to log is not a shadow runner."""
    from store_agent.modes import AgentRunner

    with pytest.raises(ValueError, match="sink"):
        AgentRunner(modes_context(), sink=None, submitter=modes_recorder(), mode="shadow")


# ---------------------------------------------------------------------------
# 3. Trust intake.
# ---------------------------------------------------------------------------


def test_an_injected_trust_event_changes_the_rationale_deterministically(
    modes_context, modes_request, modes_trust_event, modes_recorder, modes_first_value
):
    """R13: the posture the agent states moves with the evidence, reproducibly."""
    from store_agent.modes import AgentRunner

    def rationale_after(events) -> str:
        sink = modes_recorder("sink")
        runner = AgentRunner(modes_context(), sink=sink, submitter=None, mode="shadow")
        for event in events:
            runner.ingest_trust_event(event)
        runner.run(modes_request("auc-0007"))
        rationale = modes_first_value(sink.payloads(), "rationale")
        assert isinstance(rationale, str) and rationale.strip()
        return rationale

    baseline = rationale_after([])
    after = rationale_after([modes_trust_event()])
    again = rationale_after([modes_trust_event()])

    assert after != baseline, "an injected trust event must change the next bid's posture"
    assert after == again, "the same event twice must produce the same change"
    assert "shipped_on_time" in after, (
        f"the rationale must name the dimension the evidence landed on: {after!r}"
    )


def test_a_trust_event_moves_the_posture_and_never_the_price(
    modes_context, modes_request, modes_trust_event, modes_recorder, modes_canon
):
    """R10/S4: trust changes what the agent SAYS, not the number it bids.

    The cold bid is the catalog list price moved only by a depth `authorize_discount`
    granted. A trust signal that quietly re-prices an offer would be exactly the
    improvisation the hook boundary exists to forbid — and no assertion in the frozen goal
    would notice, because it only reads the rationale.
    """
    from store_agent.modes import AgentRunner

    def offer_and_rationale(events):
        sink = modes_recorder("sink")
        runner = AgentRunner(modes_context(), sink=sink, submitter=None, mode="shadow")
        for event in events:
            runner.ingest_trust_event(event)
        entry = runner.run(modes_request("auc-0007"))
        return modes_canon(entry.answer.offer), entry.rationale

    cold_offer, cold_rationale = offer_and_rationale([])
    warm_offer, warm_rationale = offer_and_rationale([modes_trust_event()])

    assert warm_offer == cold_offer, "a trust event must not move a single field of the offer"
    assert warm_rationale != cold_rationale


def test_trust_intake_accepts_a_mapping_or_the_contracts_model(
    modes_context, modes_request, modes_trust_event, modes_recorder
):
    """The push endpoint hands over a mapping; an in-process caller hands over the model."""
    from contracts import TrustEventPayload
    from store_agent.modes import AgentRunner

    def rationale_for(event):
        sink = modes_recorder("sink")
        runner = AgentRunner(modes_context(), sink=sink, submitter=None, mode="shadow")
        runner.ingest_trust_event(event)
        runner.run(modes_request("auc-0007"))
        return sink.only().rationale

    assert rationale_for(modes_trust_event()) == rationale_for(
        TrustEventPayload.model_validate(modes_trust_event())
    )


def test_a_trust_event_for_another_store_is_refused(
    modes_context, modes_trust_event, modes_recorder
):
    """Sealed state is per store. A misrouted event must be loud, never quietly absorbed."""
    from store_agent.modes import AgentRunner

    runner = AgentRunner(modes_context(), sink=modes_recorder(), submitter=None, mode="shadow")
    with pytest.raises(ValueError, match="store"):
        runner.ingest_trust_event(modes_trust_event(store_id="store-beta"))


def test_a_malformed_trust_event_is_refused(modes_context, modes_trust_event, modes_recorder):
    """`TrustEventPayload` is the contract; an unreadable event never reaches the posture."""
    from store_agent.modes import AgentRunner

    runner = AgentRunner(modes_context(), sink=modes_recorder(), submitter=None, mode="shadow")
    for bad in ({"store_id": "store-alpha"}, modes_trust_event(dim="vibes"), "not an event"):
        with pytest.raises(ValueError):
            runner.ingest_trust_event(bad)


def test_the_posture_is_order_independent_over_the_same_evidence(
    modes_context, modes_request, modes_trust_event, modes_recorder
):
    """Two events, either order, one posture — the log must not depend on delivery order."""
    from store_agent.modes import AgentRunner

    late = modes_trust_event()
    priced = modes_trust_event(dim="price_honored", delta=0.2)

    def rationale_for(events):
        sink = modes_recorder("sink")
        runner = AgentRunner(modes_context(), sink=sink, submitter=None, mode="shadow")
        for event in events:
            runner.ingest_trust_event(event)
        runner.run(modes_request("auc-0007"))
        return sink.only().rationale

    assert rationale_for([late, priced]) == rationale_for([priced, late])


# ---------------------------------------------------------------------------
# 4. Where the rationale lives, and determinism.
# ---------------------------------------------------------------------------


def test_the_rationale_rides_on_the_wrapper_because_bid_forbids_it(
    modes_context, modes_request, modes_recorder
):
    """`contracts.Bid` is ``extra="forbid"``: a `rationale` attached to it RAISES.

    Both halves are asserted here so a later refactor that "simplifies" by folding the
    rationale onto the bid fails in this file, with this explanation, rather than as an
    opaque pydantic error somewhere downstream.
    """
    import pydantic
    from contracts import Bid
    from store_agent.modes import AgentRunner

    sink = modes_recorder("sink")
    runner = AgentRunner(modes_context(), sink=sink, submitter=None, mode="shadow")
    entry = runner.run(modes_request("auc-0001"))

    assert Bid.model_config.get("extra") == "forbid"
    assert "rationale" not in Bid.model_fields
    with pytest.raises(pydantic.ValidationError):
        Bid(**{**entry.answer.model_dump(), "rationale": "smuggled"})

    assert not hasattr(entry.answer, "rationale")
    assert isinstance(entry.rationale, str) and entry.rationale.strip()


def test_the_log_entry_is_an_inert_record(modes_context, modes_request, modes_recorder):
    """The audit entry is frozen: a downstream reader cannot rewrite what was logged."""
    from store_agent.modes import AgentRunner

    runner = AgentRunner(modes_context(), sink=modes_recorder(), submitter=None, mode="shadow")
    entry = runner.run(modes_request("auc-0001"))

    assert dataclasses.is_dataclass(entry)
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.rationale = "rewritten"


def test_two_runners_on_identical_input_log_byte_identical_entries(
    modes_context, modes_request, modes_recorder, modes_canon
):
    """S4: no clock, no RNG, no set iteration anywhere on this path."""
    from store_agent.modes import AgentRunner

    def entry_for():
        sink = modes_recorder("sink")
        runner = AgentRunner(modes_context(), sink=sink, submitter=None, mode="shadow")
        runner.run(modes_request("auc-0001"))
        return modes_canon(sink.only())

    assert entry_for() == entry_for()


def test_a_decline_is_logged_too_and_carries_its_reason(
    modes_context, modes_envelope, modes_request, modes_recorder, modes_first_value
):
    """Not bidding is a decision the audit log must hold, with the reason it was taken."""
    from store_agent.modes import AgentRunner
    from store_agent.runtime import is_decline

    context = modes_context(envelope=modes_envelope(pursue_clusters=["cluster-something-else"]))
    sink = modes_recorder("sink")
    submitter = modes_recorder("submitter")
    runner = AgentRunner(context, sink=sink, submitter=submitter, mode="active")

    entry = runner.run(modes_request("auc-0001"))

    assert is_decline(entry.answer), "an un-pursued cluster must produce a decline, not a bid"
    assert sink.count == 1, "a decline is logged like anything else"
    assert submitter.count == 1, "an active store answers the exchange even when it declines"
    assert "cluster_not_pursued" in modes_first_value(sink.only(), "rationale")


def test_both_import_spellings_are_one_module_and_one_class():
    """The frozen suite imports `packages.store_agent.src.modes`; production imports
    `store_agent.modes`. Two module objects would be two `AgentRunner` classes, and an
    `isinstance` check in a caller that spelled it the other way would answer False about a
    genuine runner. The canonical alias is what makes them the same object — see the same
    arrangement in `store_agent.runtime`.
    """
    import importlib
    import importlib.machinery
    import sys
    import types
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    # `packages/store-agent` is not a valid dotted name, so the acceptance suite's conftest
    # registers `packages.store_agent` as a namespace rooted there. Repeated here, idempotently,
    # so this gate does not depend on a file it does not own.
    if "packages.store_agent" not in sys.modules:
        module = types.ModuleType("packages.store_agent")
        spec = importlib.machinery.ModuleSpec("packages.store_agent", None, is_package=True)
        spec.submodule_search_locations = [str(repo_root / "packages" / "store-agent")]
        module.__spec__ = spec
        module.__path__ = spec.submodule_search_locations
        sys.modules["packages.store_agent"] = module
        setattr(importlib.import_module("packages"), "store_agent", module)

    canonical = importlib.import_module("store_agent.modes")
    aliased = importlib.import_module("packages.store_agent.src.modes")

    assert aliased is canonical, "two module objects would be two AgentRunner classes"
    assert aliased.AgentRunner is canonical.AgentRunner


# ---------------------------------------------------------------------------
# 5. The branches an adversarial mutation run found unexecuted.
#
# Every test below was written because a mutation to `runner.py` survived BOTH this gate and
# the frozen E4 grader. Each names the mutant it kills.
# ---------------------------------------------------------------------------


def test_an_envelope_that_states_no_activation_defaults_to_shadow(
    modes_context, modes_envelope, modes_recorder
):
    """Kills: `_envelope_states` returning `active` when the envelope states nothing.

    Every earlier test built an envelope that DID say `"activation": "shadow"`, so the
    `stated is None` branch — the fail-closed default the module docstring calls "the one
    default nobody can undo after the fact" — was never executed. Flipping it to `active`
    left this file and the frozen grader fully green while shipping a store that submits live
    bids because its envelope forgot a key.
    """
    from contracts import EnvelopeActivation
    from store_agent.modes import AgentRunner

    silent = modes_envelope()
    del silent["activation"]

    no_envelope_key = modes_context()
    del no_envelope_key["envelope"]

    for context in (
        modes_context(envelope=silent),
        modes_context(envelope={}),
        modes_context(envelope=None),
        modes_context(envelope="not an envelope"),
        no_envelope_key,
    ):
        runner = AgentRunner(context, sink=modes_recorder(), submitter=modes_recorder())
        assert runner.mode is EnvelopeActivation.shadow, (
            f"an envelope that does not say `active` must not produce one: {context!r}"
        )


def test_an_unreadable_activation_falls_back_to_shadow(
    modes_context, modes_envelope, modes_recorder
):
    """Kills: dropping the `except ValueError` fallback in `_envelope_states`.

    Without it a garbage `activation` raises at construction instead of failing closed. The
    branch was unexecuted because no fixture ever produced a malformed envelope.
    """
    from contracts import EnvelopeActivation
    from store_agent.modes import AgentRunner

    for rubbish in ("on", "ACTIVE", "", 7, ["active"]):
        context = modes_context(envelope=modes_envelope(activation=rubbish))
        runner = AgentRunner(context, sink=modes_recorder(), submitter=modes_recorder())
        assert runner.mode is EnvelopeActivation.shadow, (
            f"activation {rubbish!r} is not the word `active` and must read as shadow"
        )


def test_the_store_seal_holds_for_every_dimension_and_for_an_unidentified_runner(
    modes_context, modes_trust_event, modes_recorder
):
    """Kills: narrowing the store-id seal to one dimension, and the `if self._store_id` guard.

    The seal was written `if self._store_id and payload.store_id != self._store_id`. A context
    whose store key is missing — or merely typo'd, which `_field` swallows silently — produced a
    runner with an empty `_store_id`, and that runner absorbed EVERY store's feedback while
    logging an entry that read `store_id='store-alpha'`. A runner with no store of its own must
    refuse every event, not accept every event.
    """
    from store_agent.modes import AgentRunner

    sealed = AgentRunner(modes_context(), sink=modes_recorder(), submitter=None, mode="shadow")
    for dim in ("shipped_on_time", "price_honored", "not_returned"):
        with pytest.raises(ValueError, match="store"):
            sealed.ingest_trust_event(modes_trust_event(store_id="store-beta", dim=dim))
    assert sealed.trust_posture.signals == (), "nothing from another store may be absorbed"

    for broken in (modes_context(store_id=""), modes_context(store_id=None)):
        unidentified = AgentRunner(broken, sink=modes_recorder(), submitter=None, mode="shadow")
        with pytest.raises(ValueError, match="store"):
            unidentified.ingest_trust_event(modes_trust_event())
        assert unidentified.trust_posture.signals == ()

    typoed = modes_context()
    typoed["storeId"] = typoed.pop("store_id")
    runner = AgentRunner(typoed, sink=modes_recorder(), submitter=None, mode="shadow")
    with pytest.raises(ValueError, match="store"):
        runner.ingest_trust_event(modes_trust_event())


def test_repeated_events_on_one_dimension_accumulate(
    modes_context, modes_trust_event, modes_recorder
):
    """Kills: `self._deltas[dim] = [delta]` instead of `.append`, and `observations=1`.

    No earlier test sent two events on the same dimension, so overwriting looked identical to
    accumulating and trust evidence could silently stop adding up after the first event.
    """
    from store_agent.modes import AgentRunner

    runner = AgentRunner(modes_context(), sink=modes_recorder(), submitter=None, mode="shadow")
    for delta in (-0.25, -0.25, -0.5):
        runner.ingest_trust_event(modes_trust_event(delta=delta))

    (signal,) = runner.trust_posture.signals
    assert signal.observations == 3, "every event must be counted, not just the last one"
    assert signal.net_delta == pytest.approx(-1.0), "the deltas must sum, not replace"
    assert "over 3 signals" in runner.trust_posture.describe()


def test_the_posture_keeps_the_sign_and_the_size_of_the_evidence(
    modes_context, modes_trust_event, modes_recorder
):
    """Kills: `abs(sum(deltas))`, a constant `stance`, and an always-empty `weak_dimensions`.

    Nothing asserted the DIRECTION of the posture, so discarding the sign of trust evidence —
    which makes `guarded` unreachable and reads a late store as a good one — survived both
    suites. Asserted structurally here, not by reading the prose back out of the rationale.
    """
    from contracts import TrustDimension
    from store_agent.modes import GUARDED, NEUTRAL, REINFORCED, AgentRunner

    def posture(events):
        runner = AgentRunner(modes_context(), sink=modes_recorder(), submitter=None, mode="shadow")
        for event in events:
            runner.ingest_trust_event(event)
        return runner.trust_posture

    empty = posture([])
    assert empty.signals == () and empty.stance == NEUTRAL and empty.weak_dimensions == ()

    bad = posture([modes_trust_event(delta=-0.35)])
    assert bad.stance == GUARDED
    assert bad.weak_dimensions == (TrustDimension.shipped_on_time,)
    assert bad.signals[0].net_delta == pytest.approx(-0.35), "the sign is the whole signal"

    good = posture([modes_trust_event(dim="price_honored", delta=0.2)])
    assert good.stance == REINFORCED
    assert good.weak_dimensions == (), "positive evidence is not a weakness"

    mixed = posture(
        [modes_trust_event(delta=-0.35), modes_trust_event(dim="price_honored", delta=9.0)]
    )
    assert mixed.stance == GUARDED, "a store that is late is late, however well it prices"


def test_the_log_entry_names_the_mode_and_the_auction_it_answers(
    modes_context, modes_request, modes_recorder
):
    """Kills: a hard-coded `mode=shadow` and a hard-coded `auction_id=""` in `BidLogEntry`.

    Both fields were asserted by nothing, so an audit row that claimed shadow for a bid it had
    really submitted — or that could not be reconciled to any auction at all — was invisible.
    """
    from contracts import EnvelopeActivation
    from store_agent.modes import AgentRunner

    sink = modes_recorder("sink")
    runner = AgentRunner(modes_context(), sink=sink, submitter=modes_recorder(), mode="shadow")

    expected = (EnvelopeActivation.shadow, EnvelopeActivation.active, EnvelopeActivation.killed)
    for index, mode in enumerate(expected):
        runner.mode = mode
        entry = runner.run(modes_request(f"auc-{index:04d}"))
        assert entry.mode is mode, "the row must record the mode that actually decided its fate"
        assert entry.auction_id == f"auc-{index:04d}"
        assert entry.store_id == "store-alpha"
        assert entry.submitting is (mode is EnvelopeActivation.active)


def test_the_rationale_states_the_price_and_the_posture_it_bid_on(
    modes_context, modes_request, modes_trust_event, modes_recorder
):
    """Kills: dropping the price clause, and dropping the posture detail, from the rationale.

    The frozen goal only asks that the rationale be a non-empty string that CHANGES when a trust
    event lands, so most of its content was unpinned — a rationale reduced to the posture clause
    alone, or to the offer clause alone, passed everything.
    """
    from store_agent.modes import AgentRunner

    sink = modes_recorder("sink")
    runner = AgentRunner(modes_context(), sink=sink, submitter=None, mode="shadow")
    runner.ingest_trust_event(modes_trust_event())
    entry = runner.run(modes_request("auc-0001"))

    assert "100.00" in entry.rationale, "the rationale must state the price it actually bid"
    assert "prod-cap" in entry.rationale
    assert "free_returns" in entry.rationale and "ships_within" in entry.rationale
    assert "shipped_on_time" in entry.rationale and "-0.35" in entry.rationale


def test_the_entry_carries_the_posture_structurally_not_only_as_prose(
    modes_context, modes_request, modes_trust_event, modes_recorder
):
    """Kills: `trust_posture=()` hard-coded on the entry.

    A reader of `sealed.shadow_bids` must be able to count evidence without parsing English.
    """
    from contracts import TrustDimension
    from store_agent.modes import AgentRunner

    runner = AgentRunner(modes_context(), sink=modes_recorder(), submitter=None, mode="shadow")
    runner.ingest_trust_event(modes_trust_event())
    entry = runner.run(modes_request("auc-0001"))

    (signal,) = entry.trust_posture
    assert signal.dim is TrustDimension.shipped_on_time
    assert signal.net_delta == pytest.approx(-0.35)
    assert signal.observations == 1


def test_a_batch_of_trust_events_is_all_or_nothing(
    modes_context, modes_trust_event, modes_recorder
):
    """`ingest_trust_events` promised atomicity and did not deliver it.

    Folding over the single-event path left every event before the bad one permanently absorbed,
    so a caller that fixed the batch and retried DOUBLE-COUNTED them. Measured before the fix:
    a four-event batch with a misrouted third event left `price_honored -2.00` behind, and the
    retry took it to `-4.00`.
    """
    from store_agent.modes import AgentRunner

    runner = AgentRunner(modes_context(), sink=modes_recorder(), submitter=None, mode="shadow")
    batch = [
        modes_trust_event(delta=-1.0),
        modes_trust_event(dim="price_honored", delta=-2.0),
        modes_trust_event(store_id="store-beta", delta=-3.0),
    ]

    with pytest.raises(ValueError, match="store"):
        runner.ingest_trust_events(batch)
    assert runner.trust_posture.signals == (), "a refused batch must leave nothing behind"

    accepted = runner.ingest_trust_events(batch[:2])
    assert len(accepted) == 2
    assert [signal.net_delta for signal in runner.trust_posture.signals] == [-2.0, -1.0]


def test_a_non_finite_delta_is_refused(modes_context, modes_trust_event, modes_recorder):
    """One NaN used to blind a dimension permanently.

    `TrustEventPayload` admits `nan`; `sum()` then stays `nan` forever, and because `nan < 0`
    and `nan > 0` are both False the stance silently reads `neutral`. Measured before the fix:
    one NaN followed by twenty -5.0 events still reported
    `trust posture: neutral (shipped_on_time +nan over 21 signals)`.
    """
    from store_agent.modes import GUARDED, AgentRunner

    runner = AgentRunner(modes_context(), sink=modes_recorder(), submitter=None, mode="shadow")
    for poison in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite"):
            runner.ingest_trust_event(modes_trust_event(delta=poison))

    assert runner.trust_posture.signals == ()
    runner.ingest_trust_event(modes_trust_event(delta=-5.0))
    assert runner.trust_posture.stance == GUARDED, "real evidence still lands after a refusal"


def test_a_mutating_submitter_cannot_rewrite_the_audit_row(
    modes_context, modes_request, modes_recorder
):
    """`BidLogEntry` is frozen, but that froze only the REFERENCE to a mutable `Bid`.

    `contracts.Bid` is not a frozen model, and the submitter used to receive the very object the
    entry holds. Measured before the fix: a submitter doing `bid.offer.unit_price = 1.0` rewrote
    an audit row that had already been written — `entry.answer.offer.unit_price == 1.0` while
    `entry.rationale` still read `offer prod-cap at 100.00 USD`. Two records of one auction that
    disagree, with the log the one that lost.
    """
    from store_agent.modes import AgentRunner

    def vandal(submitted):
        submitted.offer.unit_price = 1.0
        submitted.claims.clear()

    sink = modes_recorder("sink")
    runner = AgentRunner(modes_context(), sink=sink, submitter=vandal, mode="active")
    entry = runner.run(modes_request("auc-0001"))

    assert entry.answer.offer.unit_price == 100.0, "the audit row must survive its own submitter"
    assert entry.answer.claims, "and keep the claims it was logged with"
    assert "100.00" in entry.rationale


def test_an_unusable_submitter_is_refused_at_the_flip(modes_context, modes_recorder):
    """`submitter is None` was too narrow a test for "there is no submission path".

    `0`, `""` and a bare `object()` are all not-None, all passed the activation check, and all
    then raised `TypeError` at the first auction — precisely the "activated into a void" outcome
    the check exists to prevent. Resolving the delivery at the flip is what closes it.
    """
    from store_agent.modes import AgentRunner

    for useless in (0, "", object(), 3.5):
        runner = AgentRunner(
            modes_context(), sink=modes_recorder(), submitter=useless, mode="shadow"
        )
        with pytest.raises(ValueError, match="submitter"):
            runner.mode = "active"

    # A list IS a usable submitter: `.append` is a real delivery, and an in-memory one is a
    # legitimate thing to activate into.
    outbox: list = []
    AgentRunner(modes_context(), sink=modes_recorder(), submitter=outbox, mode="active")


def test_an_envelope_killed_in_place_stops_submission(modes_context, modes_request, modes_recorder):
    """The kill switch must not depend on anyone reaching the live object.

    `mode` is a snapshot — it has to be, or an explicit activation would be undone by an
    envelope that still reads `shadow`. But the context itself is live: `bid()` re-reads the
    catalog and the floors on every run. Before this, a merchant editing `activation` to
    `killed` in the shared envelope changed which products were bid and did NOT stop the
    submission. The override goes one way only: it can suppress a submission, never cause one.
    """
    from contracts import EnvelopeActivation
    from store_agent.modes import AgentRunner

    context = modes_context()
    submitter = modes_recorder("submitter")
    runner = AgentRunner(context, sink=modes_recorder(), submitter=submitter, mode="active")

    runner.run(modes_request("auc-0001"))
    assert submitter.count == 1

    context["envelope"]["activation"] = "killed"
    entry = runner.run(modes_request("auc-0002"))

    assert submitter.count == 1, "an envelope killed in place must stop the next submission"
    assert entry.submitting is False, "and the audit row must say so"
    assert runner.mode is EnvelopeActivation.active, (
        "the explicit flip is still what it was; the envelope suppressed it, it did not undo it"
    )

    context["envelope"]["activation"] = "shadow"
    runner.run(modes_request("auc-0003"))
    assert submitter.count == 2, (
        "only `killed` suppresses; an envelope reading `shadow` must not silently deactivate an "
        "explicitly activated runner, which is what the frozen goal requires"
    )


def test_the_posture_number_never_contradicts_the_stance_beside_it(
    modes_context, modes_trust_event, modes_recorder
):
    """Kills: rendering the net delta with a plain `+.2f`.

    ``f"{-1e-9:+.2f}"`` is ``-0.00``, which sat in the audit log immediately beside the word
    `guarded` and read as "no evidence". Small evidence is still evidence; the number and the
    stance must never disagree.
    """
    from store_agent.modes import GUARDED, NEUTRAL, AgentRunner

    runner = AgentRunner(modes_context(), sink=modes_recorder(), submitter=None, mode="shadow")
    runner.ingest_trust_event(modes_trust_event(delta=-1e-9))

    described = runner.trust_posture.describe()
    assert runner.trust_posture.stance == GUARDED
    assert "-0.00 " not in described, (
        f"non-zero evidence must not render as zero next to `guarded`: {described!r}"
    )
    assert "-1.00e-09" in described

    exact_zero = AgentRunner(modes_context(), sink=modes_recorder(), submitter=None, mode="shadow")
    exact_zero.ingest_trust_event(modes_trust_event(delta=0.0))
    assert exact_zero.trust_posture.stance == NEUTRAL
    assert "+0.00" in exact_zero.trust_posture.describe(), "a real zero still reads as zero"
