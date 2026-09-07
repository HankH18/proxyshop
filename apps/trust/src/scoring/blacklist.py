"""The blacklist: bound to *business identity*, stateful, and read fail-closed.

R12. Three properties, and each one exists because of a specific way the obvious
implementation fails:

**Bound to business identity, not ``store_id``.** A store that is blacklisted and comes back
under a fresh ``store_id`` is the same business. Keying on ``store_id`` makes the blacklist a
speed bump that costs a bad actor one Shopify signup, so the key is
``business_identity`` and a re-registration under a new ``store_id`` is still blocked.

**States, not a boolean.** ``active`` / ``under_review`` / ``appealed`` / ``expired`` all
round-trip, because a delisting has a lifecycle: an appeal is pending, not resolved, and a
store whose appeal is open is still not one the exchange should route buyers to. Only
``expired`` clears the block. The ``reason_code`` and ``expires_at`` a decision was recorded
with survive with it — a blacklist you cannot explain is one nobody will maintain.

**Reads fail closed.** :func:`is_blacklisted` returns ``True`` when the lookup raises. A
trust check that answers "not blacklisted" because the store holding the blacklist is down
converts an infrastructure outage into an open door for exactly the sellers it was built to
keep out; the safe direction of that error is to refuse the routing, not to allow it.

The fold: a sealed decision becomes a listing (T-303 b)
-------------------------------------------------------
:func:`fold_delisting_events` is the fourth thing here, and it is the one that was missing.
``trust.snapshot.delisting`` decides a delisting and ``trust.events.append`` seals it into
the hash chain -- and until this function existed nothing turned that sealed event into a
row in the registry the exchange's eligibility read actually consults. ``app.seller_blacklist``
had no writer anywhere in the tree: ``db/migrations/0004`` grants ``trust_rw`` INSERT on it
and no product code used the grant, so the platform decided to delist a store and then went
on serving it as fine. Measured on this tree: one simulation run scored ``store-brightbean``
at 0.0729 against the published 0.35 threshold, sealed the ``blacklisted`` event that
implies, and the same snapshot published ``blacklisted: false`` for it.

The fold is a per-identity state machine over the ledger, not a boolean assignment, because
the states above are the point:

* ``blacklisted`` onto an identity that is **not** currently blocking -> ``active``.
* ``blacklisted`` onto an identity that **is** blocking -> **nothing**. Folding the same
  sealed event twice is a no-op (the ledger is replayable, so it will happen), and a
  ``blacklisted`` landing on an open ``under_review`` or ``appealed`` must not silently
  close the review by resetting it to ``active``.
* ``blacklist_expired`` onto a blocking listing -> ``expired``, keeping the ``reason_code``
  the listing was OPENED with. This is the un-delisting half; without it a lapse recorded in
  the ledger never reaches the interface that enforces it either.
* ``blacklist_expired`` onto an identity with no blocking listing -> **nothing**. An expiry
  for a listing we do not hold is not a licence to invent one.

Fail-closed in the same direction as everything else here. When the underlying registry
cannot answer for an identity, a ``blacklisted`` is applied anyway (the ledger says list it)
and a ``blacklist_expired`` is **not** (a store is never released on the strength of a read
that failed).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "BLACKLISTED_KIND",
    "BLACKLIST_EXPIRED_KIND",
    "BLACKLIST_STATUSES",
    "BLOCKING_BLACKLIST_STATUSES",
    "CLEARING_BLACKLIST_STATUSES",
    "DELISTING_KINDS",
    "UNRECORDED_REASON_CODE",
    "Blacklist",
    "BlacklistEntry",
    "FoldedBlacklist",
    "InvalidBlacklistState",
    "UnreadableBlacklist",
    "business_identity_of",
    "delisting_identity",
    "fold_delisting_events",
    "is_blacklisted",
]

#: Every state a blacklist entry may be recorded in.
BLACKLIST_STATUSES: tuple[str, ...] = ("active", "under_review", "appealed", "expired")

#: The states that CLEAR a listing. Exactly one — and this is the frozenset the read path
#: tests against, deliberately the opposite way round from the obvious one.
#:
#: Written as "is this status in BLOCKING?" the read fails OPEN on a status it does not
#: recognise: a persistent blacklist row carrying a state this module has not heard of reads
#: as *allowed*, which is the one direction R12 forbids and exactly what the three enumerated
#: unknowns (no identity, no ``lookup``, a ``lookup`` that raises) are careful not to do.
#: ``Blacklist.add`` refuses a bad status, but the read path accepts ANY drop-in ``lookup``
#: — the Postgres ``app.seller_blacklist`` among them — so the write-time gate does not cover
#: it. Asking "is this status one of the ones that clear?" makes an unrecognised state block,
#: which is the same answer the other unknowns get.
CLEARING_BLACKLIST_STATUSES: frozenset[str] = frozenset({"expired"})

#: The states that BLOCK, as recorded. ``expired`` is the only one that does not: a store
#: under review or mid-appeal has not been cleared, and treating "we have not finished
#: deciding" as "allowed" is the same fail-open mistake in slower motion.
BLOCKING_BLACKLIST_STATUSES: frozenset[str] = frozenset({"active", "under_review", "appealed"})

#: The frozen ledger kind recording that a store has been delisted. Declared HERE, beside the
#: states it drives, rather than in :mod:`trust.snapshot.delisting` where it used to live
#: alone: the producer of the event and the fold that consumes it have to agree on the
#: spelling, and two literals in two modules is exactly how that agreement drifts.
#: ``delisting.py`` imports these back, so there is still one name for each.
BLACKLISTED_KIND = "blacklisted"

#: The frozen ledger kind recording that a listing has lapsed. The only kind that un-delists.
BLACKLIST_EXPIRED_KIND = "blacklist_expired"

#: The two kinds :func:`fold_delisting_events` reads. Every other ledger kind is skipped.
DELISTING_KINDS: tuple[str, ...] = (BLACKLISTED_KIND, BLACKLIST_EXPIRED_KIND)

#: The reason recorded for a delisting whose sealed event carried none. ``reason_code`` is a
#: NOT NULL column in ``app.seller_blacklist`` and the field an appeal is answered against,
#: so the fold records that it is missing rather than inventing a reason the decision never
#: had -- and rather than dropping the listing, which is the one direction R12 forbids.
UNRECORDED_REASON_CODE = "unrecorded"


class InvalidBlacklistState(ValueError):
    """A blacklist entry recorded in a state outside :data:`BLACKLIST_STATUSES`."""


class UnreadableBlacklist(RuntimeError):
    """The registry beneath a :class:`FoldedBlacklist` cannot be asked about an identity.

    Raised rather than answered, because every reader in this package treats a lookup that
    raises as an unknown and an unknown as a refusal. See :meth:`FoldedBlacklist.lookup`.
    """


@dataclass(frozen=True)
class BlacklistEntry:
    """One recorded delisting decision. Immutable: a state change replaces the entry."""

    business_identity: str
    reason_code: str
    status: str = "active"
    expires_at: str | None = None
    note: str | None = None

    @property
    def blocking(self) -> bool:
        """Whether this entry, as recorded, blocks.

        Tests against :data:`CLEARING_BLACKLIST_STATUSES`, not against the blocking set, so a
        directly-constructed ``BlacklistEntry(..., status="bogus")`` — which bypasses
        ``Blacklist.add``'s write-time gate entirely, since this is a frozen dataclass with no
        ``__post_init__`` — blocks rather than clears.
        """
        return self.status not in CLEARING_BLACKLIST_STATUSES

    def expired_at(self, as_of: Any) -> bool:
        """Whether the entry's own ``expires_at`` has passed by ``as_of``.

        ``expires_at is None`` means "no expiry", which is why a permanent delisting recorded
        with ``expires_at=None`` never lapses on its own. An unparseable instant is treated as
        NOT expired: the fail-closed direction, consistent with the rest of this module.
        """
        if self.expires_at is None:
            return False
        expiry = _instant(self.expires_at)
        moment = _instant(as_of)
        if expiry is None or moment is None:
            return False
        return expiry <= moment


def _instant(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Blacklist:
    """An in-process blacklist keyed by business identity.

    This is the reference implementation and the one the tests and the simulator drive. The
    persistent one lives in ``app.seller_blacklist`` (``db/migrations/0004``, which already
    grants ``trust_rw`` write access); anything exposing ``lookup(business_identity)`` is a
    drop-in for :func:`is_blacklisted`, which is why that function takes the store rather
    than reaching for a module-level singleton.
    """

    def __init__(self, entries: Mapping[str, BlacklistEntry] | None = None) -> None:
        self._entries: dict[str, BlacklistEntry] = dict(entries or {})

    def add(
        self,
        *,
        business_identity: str,
        reason_code: str,
        status: str = "active",
        expires_at: str | None = None,
        note: str | None = None,
    ) -> BlacklistEntry:
        """Record (or replace) the decision for one business identity.

        Raises:
            InvalidBlacklistState: ``status`` is not one of :data:`BLACKLIST_STATUSES`. A
                typo'd state would read as "not blocking" under any sane default, so it is
                refused at write time where a human can still see it.
        """
        state = str(status)
        if state not in BLACKLIST_STATUSES:
            raise InvalidBlacklistState(
                f"blacklist status {status!r} is not one of {list(BLACKLIST_STATUSES)}"
            )
        identity = str(business_identity)
        entry = BlacklistEntry(
            business_identity=identity,
            reason_code=str(reason_code),
            status=state,
            expires_at=expires_at,
            note=note,
        )
        self._entries[identity] = entry
        return entry

    def lookup(self, business_identity: str) -> BlacklistEntry | None:
        """The recorded entry for one identity, or ``None`` when there is none."""
        return self._entries.get(str(business_identity))

    def remove(self, business_identity: str) -> BlacklistEntry | None:
        """Drop an entry entirely (an erroneous listing, not an expiry)."""
        return self._entries.pop(str(business_identity), None)

    def __contains__(self, business_identity: object) -> bool:
        return str(business_identity) in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[BlacklistEntry]:
        return iter(self._entries.values())

    def identities(self) -> tuple[str, ...]:
        """Every listed identity, sorted. Deterministic, for snapshots and diffs."""
        return tuple(sorted(self._entries))


def business_identity_of(store: Any) -> str | None:
    """The business identity of a store record — mapping or object, ``None`` if absent."""
    if isinstance(store, Mapping):
        value = store.get("business_identity")
    elif isinstance(store, str):
        value = store
    else:
        value = getattr(store, "business_identity", None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_blacklisted(blacklist: Any, store: Any, *, as_of: Any = None) -> bool:
    """Whether ``store`` is blacklisted, resolved through its **business identity**.

    Args:
        blacklist: anything exposing ``lookup(business_identity)``.
        store: the store record, mapping or object, carrying ``business_identity``.
        as_of: optional instant. When given, an entry whose ``expires_at`` has passed reads
            as expired even if its recorded status still says otherwise. Omitted, the
            recorded status alone decides — so a caller that has no reference instant can
            never accidentally expire a listing against the wall clock.

    Returns:
        ``True`` when the store is blocked. **``True`` is also the answer whenever the question
        could not be answered**: a lookup that raises, an object with no ``lookup`` at all, a
        store record carrying no business identity, and an entry recorded in a status this
        module does not recognise. All four are cases where the system does not know, and
        R12's fail-closed rule says an unknown is a refusal — the alternative admits precisely
        the store an outage, a malformed record or a schema drift happens to be hiding. The
        last of the four is why the check below asks whether the status CLEARS rather than
        whether it blocks: the write-time gate in :meth:`Blacklist.add` does not cover a
        drop-in ``lookup`` against another store's rows.
    """
    identity = business_identity_of(store)
    if identity is None:
        return True

    lookup = getattr(blacklist, "lookup", None)
    if lookup is None:
        return True
    try:
        entry = lookup(identity)
    except Exception:  # noqa: BLE001 - any failed read is an unknown, and unknown means blocked
        return True

    if entry is None:
        return False

    status = str(_entry_field(entry, "status", "active"))
    if status in CLEARING_BLACKLIST_STATUSES:
        return False
    if as_of is not None:
        expiry = _instant(_entry_field(entry, "expires_at", None))
        moment = _instant(as_of)
        if expiry is not None and moment is not None and expiry <= moment:
            return False
    return True


def _entry_field(entry: Any, name: str, default: Any) -> Any:
    if isinstance(entry, Mapping):
        return entry.get(name, default)
    return getattr(entry, name, default)


# ==========================================================================================
# The fold: sealed delisting events -> listings. See the module docstring.
# ==========================================================================================


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def delisting_identity(event: Any, identities: Mapping[str, str] | None = None) -> str | None:
    """The business identity one sealed delisting event lists, or ``None``.

    Read from ``payload.business_identity`` first, which is what
    :func:`trust.snapshot.delisting.delisting_events` writes. ``identities`` is a
    ``store_id -> business_identity`` roster used only as the fallback, so an event written by
    some other producer that named only a store still binds to the business behind it — R12
    keys the blacklist on the business precisely so a delisted operator cannot return under a
    fresh ``store_id``, and a row keyed on the store id would be a listing
    :func:`is_blacklisted` could never match.

    ``None`` when neither can name a business. That is not a fail-open hole: an event naming
    no identity and a store no roster has heard of cannot describe a store in the snapshot,
    and a store record carrying no ``business_identity`` is already refused by
    :func:`is_blacklisted`'s first unknown.
    """
    payload = (
        event.get("payload") if isinstance(event, Mapping) else getattr(event, "payload", None)
    )
    if not isinstance(payload, Mapping):
        payload = {}
    identity = _text(payload.get("business_identity"))
    if identity:
        return identity
    store_id = _text(payload.get("store_id")) or _text(
        event.get("store_id") if isinstance(event, Mapping) else getattr(event, "store_id", None)
    )
    if store_id and identities:
        return _text(identities.get(store_id)) or None
    return None


class FoldedBlacklist:
    """A registry, with the sealed delisting decisions the ledger holds applied on top.

    A thin overlay rather than a copy, because ``base`` is anything exposing
    ``lookup(business_identity)`` — the in-process :class:`Blacklist`, or the
    ``app.seller_blacklist``-backed one ``trust.snapshot.routes.blacklist_for`` builds — and
    "materialise every row into this process" is not a shape a Postgres-backed registry has.
    Identities the fold did not touch are answered by ``base`` exactly as before, **including
    a lookup that raises**: that failure is one of the four unknowns :func:`is_blacklisted`
    fails closed on and it must not be converted into an answer here.

    Iteration is delegated and eager, for the reason
    :class:`trust.snapshot.builder._ReadOnceRegistry` documents: the expiry path in
    :mod:`trust.snapshot.delisting` keys on whether the registry can be iterated at all, so a
    base that cannot be raises ``TypeError`` from ``iter()`` here exactly as it would there.
    """

    __slots__ = ("_base", "_overlay")

    def __init__(self, base: Any, overlay: Mapping[str, BlacklistEntry]) -> None:
        self._base = base
        self._overlay: dict[str, BlacklistEntry] = dict(overlay)

    @property
    def overlay(self) -> dict[str, BlacklistEntry]:
        """The listings this fold changed, ``business_identity -> entry``.

        The write-back set, and nothing else: an identity the ledger and the registry already
        agree about is absent. ``trust.snapshot.routes`` persists exactly these into
        ``app.seller_blacklist``, so the table converges on the chain instead of being
        rewritten from it on every read.
        """
        return dict(self._overlay)

    def lookup(self, business_identity: Any) -> Any:
        key = str(business_identity)
        if key in self._overlay:
            return self._overlay[key]
        lookup = getattr(self._base, "lookup", None)
        if not callable(lookup):
            # RAISES rather than answering ``None``, and this is a fail-open hole closed
            # rather than a style choice. "A drop-in object that does not implement the
            # lookup at all" is one of the four unknowns :func:`is_blacklisted` returns
            # ``True`` for — but it recognises it by asking the object it was HANDED, and it
            # was handed this wrapper, which does have a ``lookup``. Answering ``None`` here
            # would convert that unknown into "not blacklisted" for every identity the fold
            # did not touch. Raising puts it back in the bucket it belongs to: both
            # :func:`is_blacklisted` and ``trust.snapshot.delisting`` read a raising lookup as
            # an unknown, and an unknown blocks.
            raise UnreadableBlacklist(
                f"the registry under this fold offers no lookup, so nothing can be said about "
                f"{key!r}; an unknown is a refusal (R12)"
            )
        return lookup(key)

    def __iter__(self) -> Iterator[Any]:
        records = list(self._base)
        folded: list[Any] = []
        seen: set[str] = set()
        for record in records:
            identity = _text(getattr(record, "business_identity", None))
            seen.add(identity)
            folded.append(self._overlay.get(identity, record))
        folded.extend(entry for identity, entry in self._overlay.items() if identity not in seen)
        return iter(folded)

    def __len__(self) -> int:
        # `self.__iter__()` and never `list(self)`: `list` asks a sized iterable for its
        # length first, so the obvious spelling recurses until the stack goes.
        return sum(1 for _ in self.__iter__())

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"FoldedBlacklist(base={self._base!r}, folded={sorted(self._overlay)})"


def _current_entry(base: Any, identity: str) -> tuple[bool, Any]:
    """``(answered, entry)`` for one identity — the same read :func:`is_blacklisted` makes.

    ``answered`` is ``False`` when the base could not say, which is the case the two rules in
    :func:`fold_delisting_events` split on rather than guessing at.
    """
    lookup = getattr(base, "lookup", None)
    if not callable(lookup):
        return False, None
    try:
        return True, lookup(identity)
    except Exception:  # noqa: BLE001 - an unreadable registry is an unknown, never an answer
        return False, None


def _blocking(entry: Any) -> bool:
    """Whether a record, as recorded, blocks — asked the way the read path asks it."""
    if entry is None:
        return False
    return str(_entry_field(entry, "status", "active")) not in CLEARING_BLACKLIST_STATUSES


def fold_delisting_events(
    events: Iterable[Any],
    *,
    base: Any,
    identities: Mapping[str, str] | None = None,
) -> Any:
    """``base``, with every sealed delisting event applied to it **in ledger order**.

    Args:
        events: sealed ledger events, oldest first. Anything that is not a
            :data:`DELISTING_KINDS` kind is skipped, so the whole chain may be handed over.
            ORDER IS THE SEMANTICS: a ``blacklisted`` followed by a ``blacklist_expired``
            leaves the store in service and the reverse order leaves it delisted, and those
            are two different histories rather than two spellings of one.
        base: the registry as it stands — anything exposing ``lookup(business_identity)``.
            Never mutated: a caller's registry is the caller's.
        identities: optional ``store_id -> business_identity`` roster, used only when an
            event's payload names no identity. See :func:`delisting_identity`.

    Returns:
        ``base`` itself when nothing folded — so the overwhelmingly common case adds no
        object and no behaviour — and otherwise a :class:`FoldedBlacklist` over it.

    **Idempotent.** The ledger is append-only and replayable, so the same event will be
    folded again; every rule below is a no-op when its outcome is already recorded. Two folds
    of one event, or of one event and its replay, leave the same registry.

    **Fail closed.** A ``blacklisted`` whose identity the base could not be read for is
    applied anyway; a ``blacklist_expired`` in the same position is not. The direction is the
    one R12 fixes for every unknown in this module: a store stays delisted rather than
    quietly returning to service because a read failed.
    """
    overlay: dict[str, BlacklistEntry] = {}
    for event in events:
        kind = str(_entry_field(event, "kind", ""))
        if kind not in DELISTING_KINDS:
            continue
        identity = delisting_identity(event, identities)
        if identity is None:
            continue

        payload = _entry_field(event, "payload", None)
        if not isinstance(payload, Mapping):
            payload = {}

        if identity in overlay:
            answered, current = True, overlay[identity]
        else:
            answered, current = _current_entry(base, identity)

        if kind == BLACKLISTED_KIND:
            # Already blocking -> nothing. This is the whole of the idempotency guarantee for
            # the listing half, and it is also what keeps an open `under_review` or
            # `appealed` open: re-stamping it `active` would close a review nobody finished.
            if answered and _blocking(current):
                continue
            overlay[identity] = BlacklistEntry(
                business_identity=identity,
                reason_code=_text(payload.get("reason_code")) or UNRECORDED_REASON_CODE,
                status="active",
                expires_at=payload.get("expires_at"),
                note=_text(payload.get("source")) or None,
            )
            continue

        # `blacklist_expired`. An expiry can only end a listing this registry holds, and only
        # a BLOCKING one -- an unanswered read and an absent entry both leave the store
        # exactly as it was, which for an unanswered read means `is_blacklisted` still refuses
        # it. Nothing here can turn "we could not tell" into "back in service".
        if not answered or not _blocking(current):
            continue
        overlay[identity] = BlacklistEntry(
            business_identity=identity,
            # The reason the listing was OPENED with, exactly as `delisting_events` carries it
            # into the event: an expiry stamped with a reason the listing never had is a false
            # record of why it ended, and this is the row an appeal is answered from.
            reason_code=str(_entry_field(current, "reason_code", "") or UNRECORDED_REASON_CODE),
            status="expired",
            expires_at=_entry_field(current, "expires_at", None),
            note=_entry_field(current, "note", None),
        )

    if not overlay:
        return base
    return FoldedBlacklist(base, overlay)
