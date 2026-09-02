"""The identity vault: one buyer, many pseudonyms, none of them ever reused (R5).

Owned by T-070. SPEC R5 asks for two separable things and this module is the second:

1. stores never receive buyer identity — that is :mod:`buyer_svc.profile`;
2. **the pseudonym rotates per session and a retired one is never handed out again** —
   that is :class:`PseudonymVault`.

The mapping itself (``email → pseudonym``, and the reverse) lives in Postgres ``vault.*``,
which D5's grant model leaves reachable by exactly one role, ``buyer_vault``. Nothing in
this module weakens that: :class:`PseudonymVault` never opens a connection of its own, it
is handed a store, and a store built on a connection authenticated as any other role fails
loudly rather than quietly falling back to memory.

Freshness, and why it is not left to the entropy
------------------------------------------------
``secrets.token_hex(16)`` collides with probability far below anything that matters, so the
temptation is to call a random string "fresh" and skip the check. The check exists anyway
because the property SPEC R5 promises is *never reissued*, not *unlikely to be reissued*,
and because the generator is injectable: a deterministic generator — a test double, a
seeded PRNG, a well-meaning "readable pseudonym" scheme someone adds later — makes repeats
ordinary. :meth:`PseudonymVault.issue` therefore asks the store whether each candidate has
ever been seen, and gives up with :class:`PseudonymExhausted` rather than repeating itself.
``tests/test_auth_vault.py`` drives exactly that: a generator pinned to one constant gets
one pseudonym and then an exception.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime

from .store import (
    InMemoryPseudonymStore,
    PseudonymRecord,
    PseudonymReissued,
    PseudonymStore,
)

__all__ = [
    "PSEUDONYM_PREFIX",
    "PseudonymExhausted",
    "PseudonymVault",
    "default_pseudonym",
]

#: Every pseudonym is prefixed so a value that escapes into a log or a bid payload is
#: recognisable as a pseudonym rather than mistaken for an account id.
PSEUDONYM_PREFIX = "psn-"

#: Bytes of entropy per pseudonym. 16 bytes = 32 hex characters.
_ENTROPY_BYTES = 16

#: How many candidates :meth:`PseudonymVault.issue` will draw before giving up.
_DEFAULT_ATTEMPTS = 8


class PseudonymExhausted(RuntimeError):
    """The generator could not produce a pseudonym that had never been issued before.

    With the default generator this is unreachable in practice; with an injected
    deterministic one it is the correct, loud outcome. Reissuing is never the fallback.
    """


def default_pseudonym() -> str:
    """A fresh opaque pseudonym. Carries no derivation from the buyer it is issued to."""
    return f"{PSEUDONYM_PREFIX}{secrets.token_hex(_ENTROPY_BYTES)}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def normalise_buyer_key(buyer_key: str) -> str:
    """Canonical form of the vault key (an email address).

    Case-folded and stripped, so ``Dana@Example.com`` and ``dana@example.com`` are one
    buyer with one history rather than two buyers whose pseudonym sets never rotate against
    each other.
    """
    if not isinstance(buyer_key, str) or not buyer_key.strip():
        raise ValueError("a vault key must be non-empty text (the buyer's email address)")
    return buyer_key.strip().casefold()


class PseudonymVault:
    """Issues, retires and resolves buyer pseudonyms.

    Constructed with no arguments it keeps its history in memory, which is what unit tests
    and a database-less boot want::

        vault = PseudonymVault()
        first = vault.issue("dana.reyes@example.com")
        second = vault.issue("dana.reyes@example.com")   # a NEW session -> a NEW pseudonym
        assert first != second

    In the service it is constructed over :class:`~buyer_svc.vault.store.PostgresPseudonymStore`
    so the history survives a restart and stays inside ``vault.*``.
    """

    def __init__(
        self,
        store: PseudonymStore | None = None,
        *,
        generator: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        attempts: int = _DEFAULT_ATTEMPTS,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        self._store: PseudonymStore = store if store is not None else InMemoryPseudonymStore()
        self._generate = generator if generator is not None else default_pseudonym
        self._clock = clock if clock is not None else _utcnow
        self._attempts = attempts

    @property
    def store(self) -> PseudonymStore:
        """The backing history. Reachable only by code that already holds the vault."""
        return self._store

    def issue(self, buyer_key: str) -> str:
        """Retire ``buyer_key``'s current pseudonym and return a brand-new one.

        Args:
            buyer_key: the buyer's email address — the vault's only identity key.

        Returns:
            A pseudonym that has never been issued to anybody, to this buyer or another.

        Raises:
            PseudonymExhausted: every candidate the generator produced was already in the
                history. Reissuing an old pseudonym is not an available outcome.
        """
        email = normalise_buyer_key(buyer_key)
        issued_at = self._clock()
        last: Exception | None = None
        for _ in range(self._attempts):
            candidate = self._generate()
            if not isinstance(candidate, str) or not candidate.strip():
                raise ValueError(f"the pseudonym generator produced {candidate!r}")
            candidate = candidate.strip()
            if self._store.is_known(candidate):
                continue
            try:
                self._store.record(email, candidate, issued_at)
            except PseudonymReissued as exc:  # lost a race with another process
                last = exc
                continue
            return candidate
        raise PseudonymExhausted(
            f"could not draw a pseudonym that had never been issued before in "
            f"{self._attempts} attempts; refusing to reissue a retired pseudonym (R5)"
            + (f" (last store error: {last})" if last is not None else "")
        )

    def history(self, buyer_key: str) -> list[PseudonymRecord]:
        """Every pseudonym this buyer has held, oldest first."""
        return self._store.history(normalise_buyer_key(buyer_key))

    def active(self, buyer_key: str) -> str | None:
        """The buyer's current pseudonym, or ``None`` if they have never signed in."""
        for row in reversed(self.history(buyer_key)):
            if row.active:
                return row.pseudonym
        return None

    def resolve(self, pseudonym: str) -> str | None:
        """The email behind a pseudonym, or ``None``.

        This is the de-anonymising direction and it exists only inside the vault. No
        store-facing object carries it, and no role but ``buyer_vault`` can run the query
        underneath it.
        """
        return self._store.resolve(pseudonym)
