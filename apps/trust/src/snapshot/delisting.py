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
    "MalformedDelistingPayload",
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


def _listed_records(blacklist: Any, identities: Iterable[str]) -> list[Any]:
    """The registry's records, by iteration if it offers one and by lookup if it does not.

    ITERATION FIRST, and that ordering is the whole design. ``build_snapshot`` has already
    asked ``lookup`` exactly once for every store on its way to ``entry["blacklisted"]``, so
    asking again per store would double every read against a source that is a Postgres table
    (``app.seller_blacklist``) — a real cost, pinned by a test that counts the calls
    (``test_snapshot.test_blacklisted_fails_closed_when_the_blacklist_lookup_raises``).
    Iteration is also the only shape that can see a lapsed listing whose store is not in this
    snapshot at all, so it stays the preferred path and the one every in-process registry
    takes.

    THE FALLBACK IS T-320's FIX. ``list(blacklist)`` was previously wrapped in a bare
    ``except`` that returned ``{}``, which made a registry that cannot be iterated
    indistinguishable from a registry with nothing lapsed. That is the shape a Postgres-backed
    adapter over ``app.seller_blacklist`` naturally has — a keyed read, not "materialise every
    row into this process" — and under it NO listing ever expired and no store was ever
    re-listed. The direction was fail-closed (the store stayed delisted) so it was a coverage
    gap in the expiry path rather than a security hole, but "the only signal is silence" is
    not a property an expiry path may have.

    So when iteration is unavailable, the identities THIS SNAPSHOT carries are asked for
    directly. That is strictly narrower than iteration — a lapsed listing whose store is
    absent from this snapshot still cannot be seen — and it is the most such a registry can
    answer. The ticket's own remedy ("iterate the blacklist rather than the registry") is a
    no-op as written: in ``delisting_events(entries, *, blacklist=...)`` the blacklist IS the
    registry, and this function already iterated it.

    Every failure still yields nothing, which leaves the store listed: a registry that offers
    neither iteration nor a callable ``lookup``, and a ``lookup`` that raises. Fail closed, in
    the same direction as :func:`trust.scoring.is_blacklisted`.
    """
    try:
        return list(blacklist)
    except Exception:  # noqa: BLE001 - not iterable, or iteration raised; try the other door
        pass

    lookup = getattr(blacklist, "lookup", None)
    if not callable(lookup):
        return []

    records: list[Any] = []
    seen: set[str] = set()
    for identity in identities:
        if identity in seen:
            continue
        seen.add(identity)
        try:
            record = lookup(identity)
        except Exception:  # noqa: BLE001 - an unreadable entry must not release a store
            continue
        if record is not None:
            records.append(record)
    return records


def _lapsed_reasons(blacklist: Any, as_of: Any, identities: Iterable[str] = ()) -> dict[str, str]:
    """Business identity -> reason code, for every listing still blocking but lapsed at ``as_of``.

    The reason travels with the identity because the expiry event has to carry the reason the
    listing was CREATED with -- ``blacklist_expired``'s published payload is
    ``("store_id", "reason_code")``, and stamping this module's own
    ``trust_score_below_threshold`` on a listing a human opened for ``manual_review`` would
    file a false record of why it ended.

    ``identities`` are the business identities this snapshot carries; they are used only when
    the registry cannot be iterated. See :func:`_listed_records` for why, and for the cost
    argument that keeps iteration the preferred path.
    """
    records = _listed_records(blacklist, identities)

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


class MalformedDelistingPayload(ValueError):
    """A delisting body this module built that is not the one its kind publishes.

    A programming error in THIS file and never a caller's input: every key the two published
    bodies name is written a few lines below, from values this module owns. It is raised
    rather than logged because a delisting the ledger will not accept is a decision that
    silently does not get recorded, and silence is the failure mode the whole module exists
    to remove.
    """


def _event(kind: str, *, store_id: str, as_of: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """One ledger event, carrying only the fields ``trust.ledger.canonical.EVENT_FIELDS`` names.

    ``event_id`` IS the ledger's idempotency key (D16), so it is derived from the decision
    rather than minted: the same store, kind and instant produce the same id, and re-running
    the same snapshot appends nothing a second time. ``ts`` is ``as_of`` and never a clock —
    a decision taken against a snapshot belongs at that snapshot's instant, or a replay would
    time-stamp it differently from the serve.

    T-333: VALIDATED AT THE PRODUCING BOUNDARY, which is what this module was the odd one out
    for not doing. ``apps/exchange/src/auction/ledger.py``, ``apps/merchant/svc/src/codes/
    ledger.py``, ``apps/buyer/svc/src/feedback/submission.py`` and
    ``apps/exchange/src/retrieval/fit.py`` all call :func:`contracts.ledger.
    validate_ledger_payload` on the way out; this one did not, so the only validation these
    events ever received lived in a TEST — applied to events the test itself constructed,
    which is a different object from the one production emits. That is how T-332's blank
    ``store_id`` reached a written row.

    Kept honest about what the validator can and cannot see: it is PRESENCE-only for these
    kinds, so it would not have caught the blank id on its own. It catches the other half —
    a published key dropped or renamed — which is the class that turns a delisting into a row
    the auditor cannot read, and it is checked here rather than nowhere.

    The import is local because ``contracts`` is a sibling package: ``services/sim/Dockerfile``
    ships ``apps/trust/src/`` and the image's ``.pkgroot`` carries ``contracts``, but a
    module-scope import would make this file unimportable anywhere that layout is not in
    place, and this function is the only thing here that needs it.
    """
    from contracts.ledger import validate_ledger_payload

    problems = validate_ledger_payload(kind, payload)
    if problems:
        raise MalformedDelistingPayload(
            f"refusing to emit a {kind!r} delisting for store {store_id!r} whose body is not "
            f"the published one: {'; '.join(problems)}"
        )
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
    # NORMALISED FIRST, then used for both the lookup fallback and the events themselves, so
    # the identities offered to a lookup-only registry are exactly the ones an event could be
    # written about. See the `store_id` guard below for why blank is not a value here.
    subjects: list[tuple[Mapping[str, Any], str, str]] = []
    for candidate in entries:
        raw_store_id = candidate.get("store_id")
        identity = business_identity_of(candidate)
        # T-332. `store_id is None` was the whole guard, while `business_identity_of` beside
        # it does `str(v).strip() or None`. So '' and '   ' passed, were `str()`-ed into the
        # event, and survived every layer below: `validate_ledger_payload` is presence-only
        # for this field, `normalise_event` accepts it, and `append` writes the row. The harm
        # is that `store.read(store_id=...)`, `read_events(store_id=...)` and
        # `GET /events?store_id=` all match on equality, so a delisting recorded against ''
        # is returned by none of them — the store it happened to cannot find it, and neither
        # can an appeal. `event_id` is `f"{kind}:{store_id}:{moment}"` too, so every
        # blank-store delisting at one instant collapses onto ONE idempotency key.
        #
        # Normalised the same way as the identity beside it, because a store id that is not a
        # store id is missing evidence, and the module already declines to act on that.
        store_id = str(raw_store_id).strip() if raw_store_id is not None else ""
        if not store_id or not identity:
            continue
        subjects.append((candidate, store_id, str(identity)))

    lapsed = (
        _lapsed_reasons(blacklist, as_of, [identity for _, _, identity in subjects])
        if subjects
        else {}
    )
    events: list[dict[str, Any]] = []
    for entry, store_id, identity in subjects:
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
