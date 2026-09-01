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

__all__ = ["SCORING_MODULES", "observations_from_events", "replay"]

#: Where the scorer is looked for, in order. Both spellings resolve to the same file: the
#: repo root is on ``sys.path`` (which is how the frozen suite imports
#: ``apps.trust.src.scoring``) and ``.pkgroot/trust`` symlinks to ``apps/trust/src`` (which
#: is how the member packages import each other).
SCORING_MODULES = ("trust.scoring", "apps.trust.src.scoring")

#: Fields a trust observation carries (T-062's ``score`` reads exactly these).
OBSERVATION_FIELDS = ("store_id", "dim", "type", "observed_at")


def observations_from_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project a ledger stream onto the trust observations it carries.

    An event contributes an observation when its ``payload`` names both a ``dim`` and a
    ``type``; every other event -- ``accepted``, ``order_paid``, ``auction_opened`` -- passes
    through untouched, because turning those into observations is a *scoring* decision and
    lives in T-062.

    Args:
        events: the stream, in order. Order is preserved in the output, which matters: decay
            is a function of recorded timestamps and the sequence they arrived in, and the
            S3 replay assertion compares against a served snapshot built from the same
            sequence.

    Returns:
        ``[{"store_id", "dim", "type", "observed_at"}, ...]``. ``store_id`` is taken from
        the event, falling back to the payload; ``observed_at`` from the payload, falling
        back to the event's ``ts`` -- so an observation always carries the instant the
        scorer decays against.

        An event naming a ``dim`` and a ``type`` but **no** ``store_id`` is skipped: a trust
        observation is a statement about a store, and there is no honest store to attribute
        it to. That is a silent drop, and the reason it is tolerable is that the writer's own
        chain guard cannot produce such an event through any path this package owns.
    """
    observations: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        dim, observation_type = payload.get("dim"), payload.get("type")
        if dim is None or observation_type is None:
            continue
        store_id = event.get("store_id") or payload.get("store_id")
        if store_id is None:
            continue
        observations.append(
            {
                "store_id": store_id,
                "dim": dim,
                "type": observation_type,
                "observed_at": payload.get("observed_at") or event.get("ts"),
            }
        )
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
