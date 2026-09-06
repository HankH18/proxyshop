"""The replay memory behind the external door (D52).

`NonceStore` is simultaneously the port name and its default implementation. It is a
concrete, directly instantiable class rather than a `Protocol` or an ABC on purpose: the
frozen suite constructs `NonceStore()` and then *reuses the instance across two
`receive_bid` calls* to prove replay is caught, so an abstract stand-in would make the
criterion unreachable.

**In memory, not in Postgres.** `db/migrations/0003_*.sql` defines `app.bid_nonces`
(`signer_id, nonce, auction_id, consumed_at, retain_until`, unique on
`(signer_id, nonce)`) and that table is the durable implementation of this port for a
deployed exchange. Nothing here touches it: this class is the offline, injectable half,
and a database-backed sibling is a separate object that answers the same three methods.

**Scoping is per signer, never global.** Two sellers picking the same nonce string is not
a replay; one seller reusing theirs is. The key is therefore `(signer_id, nonce)`, which
is also the uniqueness constraint the migration carries.

**Eviction is a policy, and it has two rules (T-378).** `purge_expired` used to exist with
no caller at all, which made the nonce COUNT grow with process lifetime even though each
nonce's LENGTH was bounded: measured, 200 admitted bids retained 200 entries and 74,504
bytes with `retain_until` populated and never consulted. Both rules are now here, and both
lean the same way — a store that forgets too early is not a bounded replay memory, it is an
absent one:

1. **Expiry.** `purge_expired` is the only thing that drops an entry, and it drops one only
   once its retention window has strictly closed. `door.py` runs it on every submission that
   reaches the replay gate, at the same instant it judges the rest of the submission by.
2. **A hard ceiling**, `MAX_TRACKED_NONCES`. Expiry alone bounds *rate x window*, which is
   not a bound. At the ceiling this store **refuses the new nonce** rather than evicting one
   that is still inside its window: an LRU or a random eviction here would drop precisely the
   entry whose auction is still open, trading a memory bound for a replay hole. Refusing
   costs an honest submitter one retry; evicting costs the property this class exists for.
"""

from __future__ import annotations

import threading
from datetime import datetime

from contracts.boundary import parse_timestamp

__all__ = ["MAX_TRACKED_NONCES", "NonceStore", "NonceStoreFull"]

#: The most `(signer_id, nonce)` pairs one process-local store will hold at once.
#:
#: Derived, not picked. T-378 measured this exact structure at **74,504 bytes for 200
#: entries** — 373 bytes per pair, `sys.getsizeof` over the dict and both key strings —
#: and `apps/exchange/compose.yaml` caps the exchange container at `mem_limit: 256m`, which
#: is the process this store lives in. 32,768 pairs is therefore ~11.9 MiB, under 5% of the
#: container's whole budget, and it is a power of two so the dict's growth stops on a
#: resize boundary rather than mid-doubling.
#:
#: It is also far above any honest working set: entries survive only until their auction
#: closes or their `issued_at` ages out of the door's freshness window (300s by default), so
#: reaching 32,768 means one process took ~109 admitted bids per second sustained across
#: that window — two orders of magnitude past what a single uvicorn worker holding a
#: FastAPI app in 256 MiB serves. Hitting this ceiling is a signal, not a routine.
MAX_TRACKED_NONCES = 32_768


class NonceStoreFull(RuntimeError):
    """The store is at `max_entries` and every entry it holds is still inside its window.

    Raised by `consume`, and it is deliberately NOT a `False` return: `False` means "this
    pair was already spent", which is a verdict about the *submitter*, and answering it here
    would log an honest seller as a replayer for our own capacity problem. `door.py` catches
    this by name and refuses `replay_memory_exhausted`.

    A separate type rather than a flag because there is no safe fallback for the caller to
    choose: admitting a submission whose nonce could not be recorded is admitting an
    unlimited replay of it, so the only correct handling is a refusal.
    """


class NonceStore:
    """An in-memory, single-process record of which `(signer_id, nonce)` pairs are spent.

    Retention is bounded by the submission itself, not by a global TTL. A nonce only has to
    be remembered for as long as a replay of it could still win something, and the caller is
    the one who knows when that is: `door.py::_nonce_retention` hands over the auction's
    deadline when the auction has one, and the freshness horizon `issued_at + window` when it
    does not, because a replay of the same signed bytes is refused `issued_at_stale` past
    that instant. This class does not compute either; it stores what it is told and forgets
    strictly after it.
    """

    def __init__(self, max_entries: int = MAX_TRACKED_NONCES) -> None:
        """`max_entries` overrides `MAX_TRACKED_NONCES` for one store.

        It exists so a test can drive the ceiling in a few calls instead of 32,768, and so a
        deployment that knows its own memory budget can say so. A non-positive or non-integral
        value is refused at construction rather than silently treated as "unbounded" — an
        unbounded replay memory is the defect this ceiling closes (T-378).
        """
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise TypeError(f"max_entries must be an int, not {type(max_entries).__name__}")
        if max_entries < 1:
            raise ValueError(f"max_entries must be at least 1, not {max_entries}")
        self._max_entries = max_entries
        #: `(signer_id, nonce) -> retain_until`. A `None` retention means "keep forever":
        #: an unparseable deadline is not a licence to forget a spent nonce early.
        self._consumed: dict[tuple[str, str], datetime | None] = {}
        #: Guards every read-modify-write of `_consumed`. "Spend this nonce" is one decision,
        #: not a test followed by a store, and the two halves must not be separable by a
        #: scheduler. `RLock` rather than `Lock` so a future subclass that calls one of these
        #: methods from inside another cannot deadlock itself.
        self._lock = threading.RLock()

    # -- the write side -----------------------------------------------------------------
    #
    # `receive_bid` is the only caller, and it always injects a real store, so this name is
    # not pinned by anything frozen. It is spelled `consume` because that is what it does:
    # it spends the nonce and reports whether it was still spendable.

    def consume(self, signer_id: str, nonce: str, retain_until: object = None) -> bool:
        """Spend `(signer_id, nonce)`. `False` — and no state change — if already spent.

        `retain_until` is the instant after which the pair may be forgotten, normally the
        auction deadline. Anything `parse_timestamp` cannot read is retained indefinitely
        rather than dropped: forgetting early is the failure mode that reopens replay.

        **Atomic (T-234).** The membership test and the store are one critical section, and
        nothing slow happens inside it: `parse_timestamp` is a full function call and used to
        sit in the gap BETWEEN the test and the store, so two threads racing on one
        `(signer_id, nonce)` could both pass the test before either stored. It was masked by
        the 5ms GIL switch interval — 0/300 at the default, 25/300 at
        `sys.setswitchinterval(1e-6)` — which is a scheduler accident, not a defence, and it
        goes live the moment there is a concurrent HTTP caller. Both halves of the fix are
        applied: the retention is parsed BEFORE the section so no caller-supplied value can be
        evaluated while the lock is held, and the test-and-store is then indivisible.

        This makes the pair spendable exactly once per process. It is still per-process: the
        durable, cross-worker implementation of this port is `app.bid_nonces`
        (`UNIQUE (signer_id, nonce)`), which nothing in this tree binds to yet — see the module
        docstring.

        **Raises `NonceStoreFull` at `max_entries` (T-378)**, and does not store the pair. The
        alternative — evicting some other entry to make room — is the one thing this class must
        never do, because the entries it holds are exactly the ones whose replay windows are
        still open; anything whose window had closed was already dropped by `purge_expired`. So
        "full" means "full of live entries", and freeing one of those is freeing a replay.

        This method has NO clock of its own and so cannot purge on its own behalf: `retain_until`
        is a FUTURE instant, and sweeping against it would drop entries whose windows have not
        closed. Keeping the store under its ceiling is therefore the caller's job, and
        `door.py` does it by calling `purge_expired(now)` immediately before every consume. A
        caller that never purges eventually wedges at the ceiling and refuses everything — which
        is the fail-closed direction, and is why it is a wedge rather than a hole.
        """
        key = self._key(signer_id, nonce)
        # Parsed outside the critical section deliberately: `retain_until` is caller-supplied,
        # so how long this takes is not ours to bound, and holding the lock across it would
        # trade a race for a stall.
        retention = parse_timestamp(retain_until)
        with self._lock:
            if key in self._consumed:
                return False
            if len(self._consumed) >= self._max_entries:
                # Counts only. The message reaches a log and, through `door.py`'s reason code,
                # an unauthenticated submitter's error body; echoing the signer or the nonce
                # that happened to arrive at the ceiling would put caller-controlled text into
                # both.
                raise NonceStoreFull(
                    f"replay memory is at its ceiling of {self._max_entries} live entries; "
                    f"refusing to forget one that can still be replayed"
                )
            self._consumed[key] = retention
            return True

    # -- the read side ------------------------------------------------------------------

    def seen(self, signer_id: str, nonce: str) -> bool:
        """Whether this pair has been spent. A **pure read** — it records nothing.

        That purity is load-bearing rather than stylistic: a `seen` that recorded on read
        would re-arm every nonce anybody merely asked about, so a caller checking before
        deciding would extend the retention of a pair that was about to expire, and an
        expiry test would never observe the entry going away.
        """
        key = self._key(signer_id, nonce)
        with self._lock:
            return key in self._consumed

    def purge_expired(self, as_of: object) -> int:
        """Forget every pair whose retention window closed strictly before `as_of`.

        Returns how many were dropped. The comparison is strict, so a pair retained until
        the auction deadline is still remembered *at* the deadline and is dropped only
        once that instant has passed — "until after the auction deadline", read literally.

        An `as_of` this cannot parse purges nothing. A caller passing a malformed instant
        has told us nothing about the time, and answering "then everything has expired"
        would clear the replay memory on a typo.
        """
        moment = parse_timestamp(as_of)
        if moment is None:
            return 0
        # Select and delete under one lock. Iterating `_consumed` while another thread spends a
        # nonce would raise `RuntimeError: dictionary changed size during iteration` out of a
        # housekeeping call, and a two-phase purge that released the lock between selecting and
        # deleting could drop a pair spent in the gap — forgetting a nonce that was never
        # replay-checked, which is the one direction this store must never fail in.
        with self._lock:
            expired = [
                key
                for key, retain_until in self._consumed.items()
                if retain_until is not None and retain_until < moment
            ]
            for key in expired:
                del self._consumed[key]
            return len(expired)

    @property
    def max_entries(self) -> int:
        """The ceiling this store enforces. Read-only: it is a memory budget, not a dial."""
        return self._max_entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._consumed)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"NonceStore(consumed={len(self._consumed)}, max_entries={self._max_entries})"

    @staticmethod
    def _key(signer_id: object, nonce: object) -> tuple[str, str]:
        """Both halves as text, so a non-string arriving off the wire cannot key differently.

        `("s", "1")` and `("s", 1)` are the same claim about the same submission — a JSON
        number and its string form reach us through different decoders — and letting them
        occupy two slots would let a replay pick whichever slot was empty.
        """
        return (str(signer_id), str(nonce))
