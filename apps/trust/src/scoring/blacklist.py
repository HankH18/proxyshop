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
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "BLACKLIST_STATUSES",
    "BLOCKING_BLACKLIST_STATUSES",
    "CLEARING_BLACKLIST_STATUSES",
    "Blacklist",
    "BlacklistEntry",
    "InvalidBlacklistState",
    "business_identity_of",
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


class InvalidBlacklistState(ValueError):
    """A blacklist entry recorded in a state outside :data:`BLACKLIST_STATUSES`."""


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
