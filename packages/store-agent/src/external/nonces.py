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
"""

from __future__ import annotations

import threading
from datetime import datetime

from contracts.boundary import parse_timestamp

__all__ = ["NonceStore"]


class NonceStore:
    """An in-memory, single-process record of which `(signer_id, nonce)` pairs are spent.

    Retention is bounded by the auction the nonce was spent in, not by a global TTL. A
    nonce only has to be remembered for as long as a replay of it could still win
    something, and that is exactly until the auction's deadline has passed.
    """

    def __init__(self) -> None:
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
        """
        key = self._key(signer_id, nonce)
        # Parsed outside the critical section deliberately: `retain_until` is caller-supplied,
        # so how long this takes is not ours to bound, and holding the lock across it would
        # trade a race for a stall.
        retention = parse_timestamp(retain_until)
        with self._lock:
            if key in self._consumed:
                return False
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

    def __len__(self) -> int:
        with self._lock:
            return len(self._consumed)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"NonceStore(consumed={len(self._consumed)})"

    @staticmethod
    def _key(signer_id: object, nonce: object) -> tuple[str, str]:
        """Both halves as text, so a non-string arriving off the wire cannot key differently.

        `("s", "1")` and `("s", 1)` are the same claim about the same submission — a JSON
        number and its string form reach us through different decoders — and letting them
        occupy two slots would let a replay pick whichever slot was empty.
        """
        return (str(signer_id), str(nonce))
