"""The second half of S2: the score catches the store, and something acts on it (T-237).

The first half was real long before this file existed — a scripted dishonest store crosses
``BLACKLIST_THRESHOLD`` within the manifest's episode budget, and the trajectory is approved
ground truth. The second half was not: ``BLACKLIST_THRESHOLD`` was exported and compared
**only in tests**, ``Blacklist(...)`` was constructed nowhere in product code, and
``blacklisted``/``blacklist_expired`` — two of the eighteen frozen ledger kinds, reserved in
``db/migrations/0002``, in ``protocol.schema.json`` and in ``trust.events.LEDGER_EVENT_KINDS``
— were emitted by nothing at all. "The trust engine catches the dishonest store" was true;
"and therefore the exchange stops asking it" was a seam nobody had implemented, so the
delisting decision was never recorded anywhere the exchange, an auditor or an appeal could
read it.

This module is that seam, and it is **pure**: it reads a snapshot's store entries and the
blacklist registry, and returns the ledger events those two imply at ``as_of``. It opens no
connection, appends nothing and mutates nothing. That is the house pattern rather than a
convenience — ``trust.reconcile.engine`` builds its ``reconciled`` events the same way and
lets its caller append them, which is what keeps the decision deterministic and replayable:
the same (entries, blacklist, as_of) always yields the same events, byte for byte, so a
replay reproduces them instead of re-deciding them against a different clock.

Two rules, independent on purpose:

* **Sub-threshold and not already listed** -> ``blacklisted``. The comparison lives HERE, once
  (:func:`below_blacklist_threshold`), so "what counts as sub-threshold" stops being a
  literal each caller re-derives — the simulator had to compute it twice by hand
  (``services/sim/src/runner.py``) to demonstrate S2 end to end, which is the duplication
  this replaces.
* **Listed, still blocking, and lapsed at as_of** -> ``blacklist_expired``.

They are evaluated independently, and a listing that lapsed while the score is still below
the threshold correctly produces BOTH: the old listing ended, and the current evidence
re-lists the store. Recording only one of the two would leave a gap in the audit trail
exactly where an appeal would look.

Fail-closed, in the same direction as :func:`trust.scoring.is_blacklisted`: a registry this
module cannot read yields no ``blacklist_expired`` event, so a store stays listed rather than
being quietly released because its source was down.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# Relative, like every import in `builder.py` beside it: the frozen acceptance suite reaches
# this package as `apps.trust.src.snapshot` with only the repo root on `sys.path`, where a
# `trust.` spelling does not resolve at all.
from ..scoring import BLACKLIST_THRESHOLD, BLOCKING_BLACKLIST_STATUSES, business_identity_of

__all__ = [
    "BLACKLISTED_KIND",
    "BLACKLIST_EXPIRED_KIND",
    "DELISTING_SOURCE",
    "TRUST_SCORE_REASON_CODE",
    "below_blacklist_threshold",
    "delisting_events",
]

#: The frozen ledger kind recording that a store has been delisted.
BLACKLISTED_KIND = "blacklisted"

#: The frozen ledger kind recording that a listing has lapsed.
BLACKLIST_EXPIRED_KIND = "blacklist_expired"

#: Why this module delists. The manifest publishes ``blacklist_threshold`` and nothing else
#: about blacklisting -- no vocabulary of reason codes -- so this names its own, once.
TRUST_SCORE_REASON_CODE = "trust_score_below_threshold"

#: Who decided. ``app.seller_blacklist.source`` and the ``blacklisted`` payload both carry it,
#: so a reviewer can tell an automatic delisting from a human one.
DELISTING_SOURCE = "trust-score"


def below_blacklist_threshold(value: Any) -> bool:
    """Is this score below the published blacklist threshold (S2)?

    The one comparison, so there is one answer. Strict ``<``, matching the published
    threshold's own reading and the simulator's. A score that is not a real number is
    **not** treated as sub-threshold: an unreadable score is missing evidence, and delisting
    a store on missing evidence is a different decision from delisting it on bad evidence.
    Absence is handled by the caller, which leaves such a store exactly as the registry
    already has it.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return float(value) < BLACKLIST_THRESHOLD


def _lapsed_reasons(blacklist: Any, as_of: Any) -> dict[str, str]:
    """Business identity -> reason code, for every listing still blocking but lapsed at ``as_of``.

    The reason travels with the identity because the expiry event has to carry the reason the
    listing was CREATED with -- ``blacklist_expired``'s published payload is
    ``("store_id", "reason_code")``, and stamping this module's own
    ``trust_score_below_threshold`` on a listing a human opened for ``manual_review`` would
    file a false record of why it ended.

    Read by ITERATING the registry once, deliberately, rather than by calling
    ``lookup(identity)`` per store. ``build_snapshot`` has already asked ``lookup`` exactly
    once for every store on its way to ``entry["blacklisted"]``, and asking a second time
    would double every read against a source that is a Postgres table
    (``app.seller_blacklist``) — a real cost, and one an existing test pins by counting the
    calls. Iteration is also the only shape that can see a lapsed listing whose store is not
    in this snapshot at all.

    Every failure yields nothing, which leaves the store listed: a registry that is not
    iterable, one whose iteration raises, and an entry that cannot say whether it expired.
    Fail closed, in the same direction as :func:`trust.scoring.is_blacklisted`.
    """
    try:
        records = list(blacklist)
    except Exception:  # noqa: BLE001 - a registry that cannot answer must not release a store
        return {}

    lapsed: dict[str, str] = {}
    for record in records:
        identity = getattr(record, "business_identity", None)
        expired_at = getattr(record, "expired_at", None)
        if not identity or not callable(expired_at):
            continue
        if str(getattr(record, "status", "")) not in BLOCKING_BLACKLIST_STATUSES:
            continue
        try:
            if expired_at(as_of):
                lapsed[str(identity)] = str(getattr(record, "reason_code", "") or "unrecorded")
        except Exception:  # noqa: BLE001 - same reason
            continue
    return lapsed


def _event(kind: str, *, store_id: str, as_of: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """One ledger event, carrying only the fields ``trust.ledger.canonical.EVENT_FIELDS`` names.

    ``event_id`` IS the ledger's idempotency key (D16), so it is derived from the decision
    rather than minted: the same store, kind and instant produce the same id, and re-running
    the same snapshot appends nothing a second time. ``ts`` is ``as_of`` and never a clock —
    a decision taken against a snapshot belongs at that snapshot's instant, or a replay would
    time-stamp it differently from the serve.
    """
    moment = str(as_of)
    return {
        "event_id": f"{kind}:{store_id}:{moment}",
        "ts": moment,
        "kind": kind,
        "store_id": store_id,
        "payload": payload,
    }


def delisting_events(
    entries: Iterable[Mapping[str, Any]], *, blacklist: Any, as_of: Any
) -> list[dict[str, Any]]:
    """The ledger events the given store entries and registry imply at ``as_of``.

    Args:
        entries: store entries as :func:`trust.snapshot.store_entry` publishes them -- each
            carrying ``store_id``, ``business_identity``, ``score`` and ``blacklisted``.
        blacklist: the registry ``build_snapshot`` was handed; anything exposing
            ``lookup(business_identity)`` (``trust.scoring.Blacklist``, or the
            ``app.seller_blacklist``-backed source that replaces it).
        as_of: the instant the snapshot was decayed against. Never a clock.

    Returns:
        A list of ledger-event mappings, in store order, ready for ``trust.events.append``.
        Empty when nothing changed -- which is the common case and is not an error.

    A store with no ``business_identity`` yields nothing: blacklisting is identity-bound (a
    delisted operator must not return under a fresh ``store_id``), and a store the registry
    cannot be keyed by is already denied by ``is_blacklisted``'s fail-closed path.
    """
    entries = list(entries)
    lapsed = _lapsed_reasons(blacklist, as_of) if entries else {}
    events: list[dict[str, Any]] = []
    for entry in entries:
        store_id = entry.get("store_id")
        identity = business_identity_of(entry)
        if store_id is None or not identity:
            continue
        store_id = str(store_id)
        identity = str(identity)

        if identity in lapsed:
            events.append(
                _event(
                    BLACKLIST_EXPIRED_KIND,
                    store_id=store_id,
                    as_of=as_of,
                    payload={
                        "store_id": store_id,
                        # The reason the listing was OPENED with, not this module's.
                        "reason_code": lapsed[identity],
                        "business_identity": identity,
                    },
                )
            )

        # `blacklisted` is the registry's answer at `as_of`, so a listing that just lapsed
        # already reads False here and a store the registry still blocks is not re-listed.
        if not entry.get("blacklisted") and below_blacklist_threshold(entry.get("score")):
            events.append(
                _event(
                    BLACKLISTED_KIND,
                    store_id=store_id,
                    as_of=as_of,
                    payload={
                        "store_id": store_id,
                        "reason_code": TRUST_SCORE_REASON_CODE,
                        "source": DELISTING_SOURCE,
                        # No automatic expiry: the threshold decided this and the threshold
                        # can undo it, so the listing lasts as long as the evidence does.
                        # A reviewer setting a window writes it here.
                        "expires_at": None,
                        "business_identity": identity,
                        "score": entry.get("score"),
                        "threshold": BLACKLIST_THRESHOLD,
                    },
                )
            )
    return events
