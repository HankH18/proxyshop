"""Replay a ledger stream into trust snapshots. Owned by T-011; scores nothing itself.

D49 settles an ownership collision that would otherwise be unresolvable: the frozen import
is ``from apps.trust.src.ledger import replay``, ``apps/trust/src/ledger/**`` is T-011's
exclusive scope, and the *scoring* belongs to T-062, which may not write this file. The
ruling is "keep the scoring in T-062-owned files and re-export ``replay`` from T-011's
``apps/trust/src/ledger/__init__.py`` -- one line, no scoring in it".

So this module is the seam, and it holds exactly two things:

* :func:`observations_from_events` -- the projection from ledger events to trust
  observations. That is a *ledger* concern: it is about what a ``LedgerEvent`` carries, and
  nothing about it decides a score.
* :func:`replay` -- group by store, hand each group to ``apps.trust.src.scoring.score``,
  return the snapshots keyed by ``store_id``.

There is no arithmetic here, and there must not be. If a number in this file ever
influenced a served score, the S3 assertion -- *replaying the ledger reproduces the served
snapshot bit for bit* -- would be comparing the scorer against a second, divergent scorer,
and it would pass while meaning nothing.

The scoring module is imported **inside** :func:`replay`, not at module scope, so
``apps.trust.src.ledger`` stays importable before T-062 lands. Everything else in this
package -- the chain, the writer, the verifier -- works without it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "CLAIM_VERIFIED_KIND",
    "FEEDBACK_KIND",
    "RETURN_KIND",
    "SCORING_MODULES",
    "observations_from_events",
    "replay",
]

#: Where the scorer is looked for, in order. Both spellings resolve to the same file: the
#: repo root is on ``sys.path`` (which is how the frozen suite imports
#: ``apps.trust.src.scoring``) and ``.pkgroot/trust`` symlinks to ``apps/trust/src`` (which
#: is how the member packages import each other).
SCORING_MODULES = ("trust.scoring", "apps.trust.src.scoring")

#: Fields a trust observation carries. ``dim``, ``type``, ``observed_at`` and ``weight`` are
#: what T-062's ``score`` reads; ``store_id`` is what :func:`replay` groups by. ``weight`` is
#: the only optional one -- absent means exactly 1.0, decided by
#: ``trust.scoring.relative_observation_weight`` and by nothing here.
OBSERVATION_FIELDS = ("store_id", "dim", "type", "observed_at", "weight")

#: The optional per-observation field that scales an observation type's published weight
#: (R14). Spelled here rather than imported from ``trust.scoring``: this module must stay
#: importable before the scorer lands, and a projection that had to import the scorer to know
#: a field's name would put the two in a cycle. It is asserted equal to
#: ``trust.scoring.engine.OBSERVATION_WEIGHT_FIELD`` by the replay-determinism gate, so the
#: two spellings cannot drift apart unnoticed.
OBSERVATION_WEIGHT_FIELD = "weight"

#: The kind whose published body spells its observation type ``status`` rather than ``type``.
#:
#: ``contracts.LEDGER_PAYLOAD_SHAPES["claim_verified"]`` is ``(claim_ref, status, dim)`` --
#: the frozen shape names a ``dim`` and NO ``type``, so every ``claim_verified`` event in the
#: chain projected to nothing at all. Measured on this tree before the fallback below: a
#: served ``POST /events`` of a ``contradicted`` ``catalog_claim_accuracy`` verdict answered
#: ``201`` and ``GET /events/replay?snapshots=true`` came back ``{}``. That is a whole
#: producer -- ``apps/exchange/src/ranking/verification.py``, which announces one
#: ``claim_verified`` per counted claim on the served auction path -- reaching no Beta.
#:
#: ``status`` IS the observation type for this kind: the four verification statuses
#: (``verified`` / ``contradicted`` / ``unsupported`` / ``ambiguous``) are four of the seven
#: published observation types, spelled identically. The fallback is gated on the kind rather
#: than applied to any event carrying a ``status``, so a vendor body that happens to have one
#: cannot mint an observation out of it.
CLAIM_VERIFIED_KIND = "claim_verified"

#: The two kinds R14's fold-time weight is a function of; see :func:`_feedback_fold`.
FEEDBACK_KIND = "feedback"
RETURN_KIND = "refund"

#: Where the R14 weighting rule is looked for, in order -- the same two-spelling arrangement
#: as :data:`SCORING_MODULES`, and imported lazily for the same reason.
FEEDBACK_MODULES = ("trust.feedback.weighting", "apps.trust.src.feedback.weighting")


def _feedback_fold(event: Mapping[str, Any], *, returned: bool) -> dict[str, Any] | None:
    """R14's ``{type, weight}`` for one ``feedback`` event, or ``None``.

    **No number is decided here, and that is the rule this module lives by.** The weight is
    computed by ``trust.feedback.weighting.fold_feedback``, which owns
    ``RETURN_CONTRADICTION_FACTOR`` and calls ``accept_feedback`` -- exactly as every score in
    this file is computed by ``trust.scoring`` and merely moved through. What this function
    contributes is the *lookup*, which is a ledger concern: which module, imported when.

    Lazily imported for the same reason :func:`replay` imports the scorer lazily -- this
    package must stay importable when the rest of ``apps/trust`` is not -- and ``None`` on an
    ``ImportError`` because a projection that raised would turn a missing sibling package into
    a ``500`` on ``POST /events``, whose poison screen runs this projection over every
    arriving event. That branch is unreachable in a real build: ``trust.feedback`` imports
    ``trust.scoring``, so a checkout that cannot import the first cannot score at all.
    """
    import importlib

    for name in FEEDBACK_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        folder = getattr(module, "fold_feedback", None)
        if folder is not None:
            result = folder(event, returned=returned)
            return dict(result) if isinstance(result, Mapping) else None
    return None  # pragma: no cover - see the docstring


def observations_from_events(
    events: Iterable[Mapping[str, Any]],
    *,
    returned_orders: Iterable[tuple[str, str]] = (),
) -> list[dict[str, Any]]:
    """Project a ledger stream onto the trust observations it carries.

    An event contributes an observation when its ``payload`` names a ``dim`` and a ``type``;
    every other event -- ``accepted``, ``order_paid``, ``auction_opened`` -- passes through
    untouched, because turning those into observations is a *scoring* decision and lives in
    T-062.

    Two kinds name their type under a different key, because the FROZEN payload shape says
    so, and both were silently invisible until they were read here:

    * ``claim_verified`` spells it ``status`` (:data:`CLAIM_VERIFIED_KIND`); and
    * ``feedback`` may spell it not at all -- the published body is
      ``(matched_pitch, reason)`` -- in which case R14's own rule derives it from the
      buyer's answer.

    Neither is a general fallback. Both are gated on the event's ``kind``, so a vendor body
    that happens to carry a ``status`` or a ``matched_pitch`` cannot mint an observation.

    Args:
        events: the stream, in order. Order is preserved in the output, which matters: decay
            is a function of recorded timestamps and the sequence they arrived in, and the
            S3 replay assertion compares against a served snapshot built from the same
            sequence. Order matters for a second reason now: a ``refund`` seen EARLIER in
            the stream is what discounts a later positive report about that order.
        returned_orders: ``(store_id, order_ref)`` pairs already known to have been returned
            before this stream begins. The stream's own ``refund`` events are added to them
            as it is walked, so a caller folding one appended event (``POST /events``, which
            has the chain behind it but not in hand) gets the same answer as a caller
            replaying the whole chain.

    Returns:
        ``[{"store_id", "dim", "type", "observed_at"[, "weight"]}, ...]``. ``store_id`` is
        taken from the event, falling back to the payload; ``observed_at`` from the payload,
        falling back to the event's ``ts`` -- so an observation always carries the instant the
        scorer decays against.

        ``weight`` -- R14's per-observation discount, in ``[0, 1]`` -- is carried through
        verbatim when the payload has one, and omitted when it does not. Both halves are
        load-bearing for S3 (*replaying the ledger reproduces the served snapshot bit for
        bit*), and until T-206 neither was true: the projection dropped the field, so a
        buyer report discounted to 0.25 served ``alpha = 2.25`` from memory and replayed at
        ``alpha = 3.0`` -- full strength, exactly as if the buyer's own return had never
        contradicted them. The write path was never the problem; a ``weight`` in an event
        payload survives ``jsonb`` and ``read_events`` unchanged, measured. This projection
        was the only place it was lost.

        No number is *validated* here, only moved: ``trust.scoring`` owns the rule about
        what an admissible weight is (D49), and a range check in this file would be a second
        copy of it free to drift. An observation with no ``weight`` keeps the four-field
        shape rather than carrying an explicit ``None``, because the scorer already reads an
        absent weight as exactly 1.0 and S3's assertion is an ``==`` over these values.

        R14's fold-time weight is the one weight the event does not carry, and it is still
        not decided here: a ``feedback`` event whose order the chain has already recorded a
        ``refund`` for is handed to ``trust.feedback.weighting.fold_feedback``, which owns
        the factor and the rule. It is applied only when the payload names no ``weight`` of
        its own -- a producer that computed one is not overruled by a second opinion -- and
        the resulting ``weight`` is attached only when it is not exactly 1.0, so a report
        nothing contradicts projects byte-identically to the way it always has.

        An event naming a ``dim`` and a ``type`` but **no** ``store_id`` is skipped: a trust
        observation is a statement about a store, and there is no honest store to attribute
        it to. That is a silent drop, and the reason it is tolerable is that the writer's own
        chain guard cannot produce such an event through any path this package owns.
    """
    observations: list[dict[str, Any]] = []
    returned: set[tuple[str, str]] = {(str(store), str(order)) for store, order in returned_orders}
    for event in events:
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        kind = str(event.get("kind") or "")
        store_id = event.get("store_id") or payload.get("store_id")
        order_ref = event.get("order_ref") or payload.get("order_ref")
        if kind == RETURN_KIND and store_id is not None and order_ref is not None:
            # Recorded BEFORE the projection below, and only for events already walked past,
            # so "was this order returned when the report landed?" is a question about the
            # stream's prefix. A return that arrives later cannot reach backwards and rewrite
            # a sealed observation -- see `trust.feedback.weighting` on why that limit is
            # forced by the append-only chain rather than chosen.
            returned.add((str(store_id), str(order_ref)))
        dim, observation_type = payload.get("dim"), payload.get("type")
        fold: dict[str, Any] | None = None
        if kind == FEEDBACK_KIND:
            fold = _feedback_fold(
                event,
                returned=(str(store_id), str(order_ref)) in returned,
            )
        if observation_type is None:
            if kind == CLAIM_VERIFIED_KIND:
                observation_type = payload.get("status")
            elif fold is not None:
                observation_type = fold.get("type")
        if dim is None or observation_type is None:
            continue
        if store_id is None:
            continue
        observation = {
            "store_id": store_id,
            "dim": dim,
            "type": observation_type,
            "observed_at": payload.get("observed_at") or event.get("ts"),
        }
        # R14's per-observation weight, carried verbatim and only when the event actually
        # has one. Verbatim because whether a number is an admissible weight is decided in
        # exactly one place -- `trust.scoring.relative_observation_weight` -- and a second
        # opinion here (a clamp, a float() coercion, a default) would be a number this
        # module invented, which is the one thing D49 says it may never do.
        #
        # Only when present because "absent" already means exactly 1.0 to the scorer, and an
        # observation projected with `weight: None` is a different VALUE from the one the
        # producer emitted even though it scores the same -- and S3 compares those values.
        #
        # `is not None` and not a truth test: 0.0 is an admissible weight meaning "this
        # report decided nothing", and `payload.get("weight") or default` would quietly
        # restore it to full strength.
        weight = payload.get(OBSERVATION_WEIGHT_FIELD)
        if weight is None and fold is not None and fold.get(OBSERVATION_WEIGHT_FIELD) != 1.0:
            # R14 applied at the fold, and ONLY when it changes something. `!= 1.0` rather
            # than a truthiness test for the reason above: 0.0 is an admissible weight.
            weight = fold.get(OBSERVATION_WEIGHT_FIELD)
        if weight is not None:
            observation[OBSERVATION_WEIGHT_FIELD] = weight
        observations.append(observation)
    return observations


def _load_score() -> Any:
    """Import ``score`` from whichever spelling of the scoring module resolves."""
    import importlib

    errors: list[str] = []
    for name in SCORING_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError as exc:
            # Only ImportError. A scorer that exists and raises something else while
            # importing is a broken scorer, and swallowing that into "T-062 has not landed
            # yet" would send the reader to the wrong ticket.
            errors.append(f"{name}: {exc}")
            continue
        scorer = getattr(module, "score", None)
        if scorer is not None:
            return scorer
        errors.append(f"{name}: imported, but exports no `score`")
    raise ModuleNotFoundError(
        "ledger.replay delegates every number to the trust scorer, and it is not "
        "importable yet. Tried " + "; ".join(errors) + ". The scorer is `score(observations, "
        "as_of=...)` in apps/trust/src/scoring/ and belongs to T-062 (D49); this module "
        "deliberately implements no scoring of its own."
    )


def replay(events: Iterable[Mapping[str, Any]], *, as_of: Any) -> dict[str, Any]:
    """Rebuild every store's trust snapshot from a ledger stream.

    Args:
        events: the stream, in insertion order.
        as_of: the reference instant decay is evaluated against -- an explicit value, never
            a wall clock. That is what makes the replay reproducible (D17/S3).

    Returns:
        ``{store_id: snapshot}``, where each snapshot is exactly what
        ``score(observations, as_of=as_of)`` returns for that store's observations in stream
        order. Stores with no observations in the stream do not appear.

    Raises:
        ModuleNotFoundError: the T-062 scorer is not built yet. Raised with a message naming
            it rather than silently returning an empty mapping -- an empty replay compares
            equal to nothing and would read as a pass.
    """
    scorer = _load_score()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for observation in observations_from_events(events):
        grouped.setdefault(str(observation["store_id"]), []).append(observation)
    return {store_id: scorer(rows, as_of=as_of) for store_id, rows in grouped.items()}
