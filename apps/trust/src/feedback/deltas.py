"""What ONE ledger event did to the affected store's posture, in the scorer's own arithmetic.

R13 asks trust to tell a store that its score moved *and by how much*. Nothing in this
service produced that number before this module: :mod:`trust.reconcile` emits
``{store_id, dim, type, observed_at}`` OBSERVATIONS into the ledger, and
``trust.scoring.score`` recomputes a whole posture from every observation a store has. A
per-event **delta** was the missing quantity, and :func:`trust.feedback.push_trust_event`
has always required one.

WHY THIS RECOMPUTES AND DIFFS RATHER THAN PRICING AN EVENT
-----------------------------------------------------------
The tempting shape is a table — "a ``contradicted`` is worth −0.1" — and it is the one thing
this file must not be. ``trust.scoring`` already publishes what each observation type is
worth, and what a store's dimension does when one lands depends on the Beta posterior that
dimension is already carrying: the same ``contradicted`` moves a brand-new store's
``feedback_match`` mean by −0.167 and a store with fifty clean observations by a fraction of
that. A second table would be a second, disagreeing notion of what an event is worth, and
D49's whole point is that there is exactly one scorer. So the delta here is a *measurement*
of the one scorer, taken twice:

    before = score(prior observations,               as_of=the event's own instant)
    after  = score(prior observations + this one,    as_of=the same instant)
    delta  = after[dim].mean - before[dim].mean

and the mean is ``alpha / (alpha + beta)``, which is the number ``score`` itself averages
across the six dimensions to produce the served score. Nothing is invented, and a change to
the manifest's weights moves this delta without this file being touched.

``as_of`` IS THE EVENT'S OWN ``ts``, NEVER A CLOCK
---------------------------------------------------
Two runs over the same ledger must produce the same delta (S3/S4), and a delta computed
against ``datetime.now()`` cannot be reproduced by a replay tomorrow. The event's normalised
``ts`` is the instant the door already uses for exactly this purpose — see
``trust.events.routes._unreplayable_field``, which folds a single event at ``as_of=ts`` to
decide whether it is scoreable at all. Using the same reference here means the delta the
store was told is the delta a replay recomputes.

THE HISTORY BOUND, AND WHY IT REFUSES RATHER THAN TRUNCATES
------------------------------------------------------------
The diff needs the store's prior observations, which is a read of the store's own ledger
projection on a write path. It is bounded by :data:`MAX_DELTA_HISTORY_EVENTS`. A store whose
history is longer gets **no** delta rather than one computed from a truncated prefix: the
prefix is the OLDEST events, decay makes those the least relevant, and a confidently wrong
number pushed at a merchant is worse than a push that did not happen and said so.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..ledger.replay import observations_from_events
from ..scoring.engine import score

__all__ = [
    "MAX_DELTA_HISTORY_EVENTS",
    "HistoryTooLong",
    "bounded_history",
    "delta_for_event",
    "dimension_mean",
    "observation_of",
]

#: How many of the affected store's prior ledger rows one delta may be computed over.
#:
#: The read is index-backed (``commerce_events_store_seq_idx`` on ``(store_id, seq)``), so
#: this is a bound on rows materialised into Python on a request path rather than on a scan.
#: Chosen high enough that no store in this build reaches it and low enough that a store that
#: did could not stall ``POST /events``; crossing it raises :class:`HistoryTooLong`, which the
#: caller reports as an undelivered push rather than as a failed append.
MAX_DELTA_HISTORY_EVENTS = 2000


class HistoryTooLong(RuntimeError):
    """The affected store has more ledger rows than one delta may be computed over.

    Deliberately an error and not a silent truncation: see the module docstring. It is raised
    where the history is READ, so the caller can report "no delta for this store, and why"
    instead of pushing a number computed from the wrong half of the record.
    """


def observation_of(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """The trust observation ``event`` carries, or ``None`` when it carries none.

    The projection is ``trust.ledger.replay.observations_from_events`` — the same one the
    replay endpoint uses — run over a single event, so "does this event move a dimension"
    has one answer in this service rather than two.
    """
    projected = observations_from_events([event])
    return projected[0] if projected else None


def dimension_mean(snapshot: Mapping[str, Any], dim: str) -> float:
    """The Beta mean ``score`` folded for one dimension: ``alpha / (alpha + beta)``.

    Read off the served snapshot rather than recomputed from observations, so this cannot
    drift from what ``score`` publishes. Both parameters of a Beta are strictly positive
    here (the prior is ``Beta(2, 2)`` and every observation only ever adds), so the divisor
    cannot be zero; it is still guarded, because a snapshot is data and a zero divisor on a
    write path would be a 500 for an event that was already stored.
    """
    entry = snapshot["dims"][dim]
    alpha = float(entry["alpha"])
    beta = float(entry["beta"])
    total = alpha + beta
    return alpha / total if total > 0.0 else 0.0


def delta_for_event(
    event: Mapping[str, Any],
    *,
    history: Iterable[Mapping[str, Any]],
    as_of: Any = None,
) -> dict[str, Any] | None:
    """The ``{store_id, dim, delta, event, reason_code}`` one stored event is worth.

    Args:
        event: the stored ledger row, chain fields and all. It is handed on verbatim as the
            delta's ``event``; :func:`trust.feedback.trust_event_payload` is what projects it
            onto the published wire shape and scrubs it, and doing any of that here would put
            two opinions of the wire shape in the service.
        history: the affected store's ledger rows **excluding this event**. Order matters —
            the fold is order-preserving (S3) — and rows carrying no observation are ignored
            by the projection rather than by this function.
        as_of: the instant decay is evaluated against. Defaults to the event's own ``ts``;
            see the module docstring for why that is not a convenience.

    Returns:
        The delta, or ``None`` when the event carries no trust observation at all — an
        ``accepted``, an ``auction_opened``, a ``code_created``. Those are the majority of the
        ledger and they move no dimension, so there is nothing to tell a store about.

        ``reason_code`` is the observation *type* (``contradicted``, ``verified``, …) and
        nothing else. It is the published vocabulary term that decided the movement, it
        carries no identity, and it is what a merchant reading the push needs in order to
        know which of its own behaviours is being graded.

    Raises:
        UnknownTrustDimension / UnknownObservationType / InvalidObservationWeight / ValueError:
            straight out of ``trust.scoring.score``, unwrapped. ``POST /events`` refuses an
            unscoreable payload before it is stored, so reaching one of these here means a row
            that predates that check is in the history — a real condition, and one the caller
            reports rather than swallows.
    """
    observation = observation_of(event)
    if observation is None:
        return None

    prior = observations_from_events(history)
    reference = event.get("ts") if as_of is None else as_of
    dim = str(observation["dim"])

    before = score(prior, as_of=reference)
    after = score([*prior, observation], as_of=reference)

    return {
        "store_id": str(observation["store_id"]),
        "dim": dim,
        "delta": dimension_mean(after, dim) - dimension_mean(before, dim),
        "event": event,
        "reason_code": str(observation["type"]),
    }


def bounded_history(rows: Sequence[Mapping[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """``rows`` as a list, or :class:`HistoryTooLong` when there are more than ``limit``.

    The caller reads ``limit + 1`` rows precisely so that "there are more" is distinguishable
    from "there are exactly this many" without a second query; this is where that extra row
    is spent.
    """
    if len(rows) > limit:
        raise HistoryTooLong(
            f"the affected store has more than {limit} ledger rows, which is the ceiling one "
            f"trust delta may be computed over. A delta from a truncated prefix would be a "
            f"number this service cannot stand behind, so none is pushed"
        )
    return [dict(row) for row in rows]
