"""S3 gate: a weighted observation must score identically in memory and after a replay.

Owned by T-206. This file exists because the per-observation weight channel (R14) was wired
end to end on the producing side -- ``trust.feedback.accept_feedback`` computes a weight and
``trust.scoring.score`` consumes one -- while the projection between them,
``trust.ledger.replay.observations_from_events``, carried only
``{store_id, dim, type, observed_at}`` and dropped the number on the floor.

The consequence is precisely an S3 violation: *replaying the ledger reproduces the served
snapshot bit for bit* was true only for observations that carried no weight. A buyer report
discounted to 0.25 served ``alpha = 2.25`` from memory and replayed as ``alpha = 3.0``, because
the replayed copy took the "absent means exactly 1.0" default. Nothing routed weighted feedback
through the ledger yet, so nothing was wrong in production -- which is a property of the
call graph on one particular day, not of the code.

The tests below are written so that they fail if that ever becomes true again, in BOTH
directions:

* the explicit-weight case, which is the one that was measured broken;
* the **default** case, an observation with no weight at all. A fix that only carried an
  explicitly-set weight would leave the same class of bug on the default path -- an event
  whose payload happens to carry ``"weight": null``, say, or a projection that started
  synthesising a weight of its own. Absent must keep meaning exactly 1.0 on both sides.
* the **inadmissible** case, a weight the scorer refuses. Agreeing on a number is only half
  the guarantee; the two paths also have to agree on *refusing*. Measured on this file's
  first pass: replacing the verbatim assignment with
  ``min(1.0, max(0.0, float(weight)))`` left every test above green while a payload weight
  of ``5.0`` raised ``InvalidObservationWeight`` when served and replayed at full strength
  as ``alpha = 3.0`` -- the identical served-vs-replayed divergence this gate exists to
  close, wearing a clamp instead of a dropped field. A bare ``float()`` coercion was green
  too, and turns a string ``"0.25"`` that the served path refuses into a replayed 0.25.

The first group of tests needs no database: it compares the projection against the observation
the producer actually emits. The ``@pytest.mark.docker`` group takes the whole round trip
through Postgres (``append_event`` -> ``jsonb`` -> ``read_events`` -> ``replay``), because that
is the path S3 is an assertion about, and a projection that carried the weight in memory while
``jsonb`` or the row reader ate it would still be broken. (Measured while fixing this: the
write path was never at fault -- ``0.25`` goes into ``commerce_events.payload`` and comes back
out of ``read_events`` as ``0.25``, a ``float``. The projection was the only lossy step.)
"""

from __future__ import annotations

import pytest

from apps.trust.src.ledger import append_event, observations_from_events, read_events, replay
from apps.trust.src.scoring import InvalidObservationWeight, score

AS_OF = "2026-01-01T00:00:00Z"

#: The measured case from the finding: a routed buyer's positive report, discounted to a
#: quarter because that same buyer returned the item (``RETURN_CONTRADICTION_FACTOR``).
DISCOUNTED_WEIGHT = 0.25

#: Weights ``trust.scoring.relative_observation_weight`` refuses, one per way of refusing it,
#: chosen so that between them they pin every cheap way a projection could invent a number:
#:
#: * ``5.0``  -- above ``MAX_OBSERVATION_WEIGHT``; an upper clamp swallows it;
#: * ``-0.5`` -- below zero; a lower clamp (or an ``abs()``) swallows it;
#: * ``"0.25"`` -- a weight that crossed a boundary as text; ``float()`` swallows it, and it
#:   is the one case a range clamp alone would still let through;
#: * ``True`` -- refused because ``True`` is not a weight anybody meant; ``float()`` turns it
#:   into a full-strength 1.0 and so does a clamp.
#:
#: Every one of them is a value the **served** path raises on. The whole content of the
#: assertion is that the **replayed** path raises on it too, rather than quietly scoring a
#: number the projection made up.
INADMISSIBLE_WEIGHTS = (5.0, -0.5, "0.25", True)


def _feedback_observation(weight: object | None) -> dict:
    """The observation ``trust.feedback.feedback_observation`` emits, weighted or not."""
    observation = {
        "store_id": "s-weighted",
        "dim": "feedback_match",
        "type": "fulfilled",
        "observed_at": AS_OF,
    }
    if weight is not None:
        observation["weight"] = weight
    return observation


def _feedback_event(index: int, weight: object | None) -> dict:
    """The same observation as a ``feedback`` LedgerEvent, ready for the chain."""
    observation = _feedback_observation(weight)
    payload = {key: value for key, value in observation.items() if key != "store_id"}
    return {
        "event_id": f"ev-w{index}",
        "ts": AS_OF,
        "kind": "feedback",
        "store_id": observation["store_id"],
        "payload": payload,
    }


# =======================================================================================
# The projection itself
# =======================================================================================


def test_the_projection_carries_a_per_observation_weight() -> None:
    """The field that was dropped. Asserted as an exact dict, not ``"weight" in row``.

    An exact comparison is what catches the two nearby wrong fixes: projecting the weight
    under a different key, and projecting it coerced (``"0.25"``, or ``1.0`` for everything).
    """
    projected = observations_from_events([_feedback_event(0, DISCOUNTED_WEIGHT)])
    assert projected == [
        {
            "store_id": "s-weighted",
            "dim": "feedback_match",
            "type": "fulfilled",
            "observed_at": AS_OF,
            "weight": DISCOUNTED_WEIGHT,
        }
    ]


def test_an_event_with_no_weight_projects_no_weight_key() -> None:
    """The default path. Absent stays absent, and a ``null`` in the payload is absent too.

    ``relative_observation_weight`` reads a missing weight as exactly 1.0. Projecting
    ``"weight": None`` would score the same today, but it makes the projected observation a
    different value from the one the producer emitted, and every equality assertion S3 rests
    on is over those values -- so the projection keeps the four-field shape when there is no
    number to carry.
    """
    assert observations_from_events([_feedback_event(0, None)]) == [
        {
            "store_id": "s-weighted",
            "dim": "feedback_match",
            "type": "fulfilled",
            "observed_at": AS_OF,
        }
    ]

    explicit_null = _feedback_event(1, None)
    explicit_null["payload"]["weight"] = None
    assert observations_from_events([explicit_null]) == [
        {
            "store_id": "s-weighted",
            "dim": "feedback_match",
            "type": "fulfilled",
            "observed_at": AS_OF,
        }
    ]


@pytest.mark.parametrize("weight", INADMISSIBLE_WEIGHTS)
def test_the_projection_hands_over_a_weight_the_scorer_refuses_instead_of_repairing_it(
    weight,
) -> None:
    """A weight the scorer will not accept must reach the scorer and be refused there.

    This is the half of S3 that "the two paths agree on a number" does not cover: they also
    have to agree on *raising*. ``relative_observation_weight`` is the single authority on
    what an admissible weight is (D49), and it is deliberately loud -- a clamped ``5.0`` is a
    producer that believes one report is worth five and is wrong about it in a way no test of
    its own would show. A projection that clamped or coerced on the way past would make the
    ledger the only path on which that producer is never told.

    Measured before this case existed: with
    ``observation[OBSERVATION_WEIGHT_FIELD] = min(1.0, max(0.0, float(weight)))`` in place of
    the verbatim assignment, an event carrying ``weight: 5.0`` raised
    ``InvalidObservationWeight`` when served and replayed at ``alpha = 3.0``, and the whole
    file plus ``test_ledger_chain.py`` stayed green at 129 passed.

    The type is asserted alongside the value because ``{"weight": 1.0} == {"weight": True}``
    in Python: a clamp that turned ``True`` into a full-strength ``1.0`` would satisfy an
    equality assertion on its own.
    """
    with pytest.raises(InvalidObservationWeight):
        score([_feedback_observation(weight)], as_of=AS_OF)

    projected = observations_from_events([_feedback_event(0, weight)])
    carried = projected[0].get("weight", "<no weight key>")
    assert type(carried) is type(weight) and carried == weight, (
        f"the projection turned a payload weight of {weight!r} into {carried!r}. "
        f"It carries the number, it does not decide about it -- and the served path raises "
        f"on {weight!r}, so a repaired copy is a value only the replayed path will score."
    )
    with pytest.raises(InvalidObservationWeight):
        score(projected, as_of=AS_OF)


def test_the_projection_and_the_scorer_spell_the_weight_field_the_same_way() -> None:
    """Two modules, one field name, and no import between them (D49 forbids the cycle).

    ``trust.ledger.replay`` cannot import ``trust.scoring`` at module scope -- the ledger has
    to stay importable without the scorer -- so the field name exists twice. Twice is fine;
    twice and drifting is a projection that carries ``weight`` into a scorer reading
    ``rel_weight``, which loses the number again with every test above still green because
    both sides would then agree on 1.0.
    """
    from apps.trust.src.ledger.replay import OBSERVATION_WEIGHT_FIELD as ledger_field
    from apps.trust.src.scoring.engine import OBSERVATION_WEIGHT_FIELD as scoring_field

    assert ledger_field == scoring_field == "weight"


def test_the_projection_drops_no_field_the_scorer_reads() -> None:
    """The wider question behind the weight: is anything ELSE lost on the way through?

    ``trust.scoring.score`` reads exactly four things off an observation -- ``dim``, ``type``,
    ``observed_at`` and ``weight`` -- and ``replay`` reads ``store_id`` to group by store. This
    asserts the projection round-trips a fully-populated observation unchanged, so a field
    added to the scorer later without a matching field here turns this red rather than
    silently reproducing the same divergence under a new name.
    """
    observation = _feedback_observation(DISCOUNTED_WEIGHT)
    projected = observations_from_events([_feedback_event(0, DISCOUNTED_WEIGHT)])
    assert projected == [observation]
    assert score(projected, as_of=AS_OF) == score([observation], as_of=AS_OF)


# =======================================================================================
# The round trip S3 is actually an assertion about
# =======================================================================================


@pytest.mark.docker
@pytest.mark.parametrize("weight", [DISCOUNTED_WEIGHT, 0.0, 1.0, None])
def test_a_weighted_observation_replays_to_the_snapshot_it_was_served(ledger_clean, weight) -> None:
    """S3, over the real chain: memory and replay agree bit for bit, weighted or not.

    ``weight=None`` is the default case and is in the same parametrisation on purpose: the
    guarantee is one guarantee, and splitting it into "the weighted test" and "the unweighted
    test" is how a fix that only handles one of them passes.

    ``weight=0.0`` is here because it is the value a falsy check silently turns back into the
    1.0 default -- ``payload.get("weight") or None`` scores a report that decided nothing as a
    whole honest one.
    """
    connection = ledger_clean
    observation = _feedback_observation(weight)

    served = score([observation], as_of=AS_OF)

    append_event(connection, _feedback_event(0, weight))
    replayed = replay(read_events(connection), as_of=AS_OF)

    assert set(replayed) == {"s-weighted"}
    assert replayed["s-weighted"] == served, (
        f"a weight={weight!r} observation served alpha "
        f"{served['dims']['feedback_match']['alpha']!r} and replayed "
        f"{replayed['s-weighted']['dims']['feedback_match']['alpha']!r} -- the ledger "
        f"projection is not carrying what the scorer reads"
    )


@pytest.mark.docker
def test_a_discounted_report_does_not_replay_as_a_full_strength_one(ledger_clean) -> None:
    """The finding, stated as the number it was measured as.

    A ``fulfilled`` observation carries a published weight of 1.0 and the prior alpha is 2.0,
    so a report discounted to a quarter must land alpha at 2.25. Before the projection carried
    the weight it replayed at 3.0 -- full strength, exactly as if the buyer's own return had
    never contradicted them. Pinned as a literal so that "they are equal" cannot be satisfied
    by both sides being wrong in the same way.
    """
    connection = ledger_clean
    append_event(connection, _feedback_event(0, DISCOUNTED_WEIGHT))
    replayed = replay(read_events(connection), as_of=AS_OF)

    alpha = replayed["s-weighted"]["dims"]["feedback_match"]["alpha"]
    assert alpha == 2.25, f"replayed alpha {alpha!r}; the discount did not survive the ledger"


def _verdict(call) -> tuple:
    """What a path actually DID with an observation: refused it, or scored it.

    Returned as a comparable value so that "served and replayed agree" is one assertion over
    both outcomes rather than two assertions that could drift apart. A refusal compares equal
    only to a refusal, so a path that quietly scored where the other raised is a mismatch and
    not a passing test about an exception nobody asserted the other side raised too.
    """
    try:
        return ("scored", call())
    except InvalidObservationWeight:
        return ("refused",)


@pytest.mark.docker
@pytest.mark.parametrize("weight", INADMISSIBLE_WEIGHTS)
def test_an_inadmissible_weight_is_refused_on_the_replayed_path_too(ledger_clean, weight) -> None:
    """S3 over the real chain, for weights the scorer rejects: both paths must refuse.

    The tests above pin agreement on a *number*. This pins agreement on a *verdict*, which is
    the case that was measured escaping the gate: a clamp or a ``float()`` in the projection
    leaves every equality assertion green while the served path raises
    ``InvalidObservationWeight`` and the replayed path scores a full-strength observation the
    producer never earned -- the same served-vs-replayed divergence T-206 was opened to close,
    reached through validation instead of through a dropped field.

    Over the round trip on purpose, not just the projection: ``jsonb`` is the boundary a
    weight would most plausibly be coerced at, and a ``"0.25"`` that came back out of Postgres
    as ``0.25`` would be exactly this bug with nothing in this repo to blame for it.
    """
    connection = ledger_clean
    served = _verdict(lambda: score([_feedback_observation(weight)], as_of=AS_OF))

    append_event(connection, _feedback_event(0, weight))
    replayed = _verdict(lambda: replay(read_events(connection), as_of=AS_OF).get("s-weighted"))

    assert served == ("refused",), (
        f"the premise of this case is gone: the scorer no longer refuses a weight of "
        f"{weight!r} on the served path, it returned {served!r}. Either "
        f"`relative_observation_weight` changed or this parametrisation is stale -- fix the "
        f"case, do not delete it."
    )
    assert replayed == served, (
        f"a payload weight of {weight!r} was REFUSED when served and {replayed[0]} when "
        f"replayed"
        + (
            f" (alpha {replayed[1]['dims']['feedback_match']['alpha']!r})"
            if replayed[0] == "scored" and replayed[1] is not None
            else ""
        )
        + ". The ledger projection is repairing a weight instead of carrying it, so the "
        "ledger is the one path on which an inadmissible weight is never rejected."
    )
