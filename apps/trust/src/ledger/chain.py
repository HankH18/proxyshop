"""Sealing and verifying the single global ledger chain (D16). Owned by T-011.

The chain is one global sequence over ``ledger.commerce_events``, ordered by insertion
sequence. Every event carries the two fields that make the sequence checkable:

``prev_hash``
    the link it was written behind -- :data:`~.canonical.GENESIS_HASH` for the first event.
``event_hash``
    ``sha256(prev_hash || canonical_json(event))``, computed over the event's body with the
    chain fields removed.

**Anything that appends must seal through :func:`seal_event`.** ``verify_chain`` cannot
detect tampering in a stream that carries no stored digests -- recomputing a chain over
mutated events just yields a different, internally consistent chain, with nothing to compare
it against. The stored ``event_hash`` is the anchor, so an event store that keeps only a
running head hash and does not stamp its events has nothing for a verifier to check. That is
why D16 puts the hashing here and forbids a second implementation: ``apps.trust.src.events``
(T-060) appends by calling :func:`seal_event`, and gets ``verify_chain`` for free.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .canonical import GENESIS_HASH, compute_event_hash

__all__ = [
    "GENESIS_HASH",
    "ChainIntegrityError",
    "chain_events",
    "chain_head",
    "seal_event",
    "stream_hash",
    "verify_chain",
]


class ChainIntegrityError(RuntimeError):
    """A stream cannot be read as a chain at all.

    Distinct from a *broken* chain, which :func:`verify_chain` reports as data rather than
    raising: this is the case where the question cannot be asked, because the events carry
    no chain fields to check.

    Defined here rather than in :mod:`.errors` so that the canonicaliser, the sealer and the
    verifier stay importable with **nothing but the standard library** -- ``.errors`` pulls
    in ``psycopg`` and ``redis``, and T-060 needs none of that to append to a chain.
    """


def seal_event(event: Mapping[str, Any], prev_hash: str = GENESIS_HASH) -> dict[str, Any]:
    """Return a copy of ``event`` carrying its chain fields.

    Args:
        event: the ``LedgerEvent``. Not mutated -- the ledger is append-only in memory as
            well as on disk, and a caller that still holds the pre-seal dict must keep
            seeing the pre-seal dict.
        prev_hash: the link to write behind. Defaults to the genesis link.

    Returns:
        ``dict(event)`` plus ``prev_hash`` and ``event_hash``. Sealing an already-sealed
        event re-seals it against the ``prev_hash`` given, because
        :func:`~.canonical.canonical_event` strips the old chain fields before hashing.
    """
    sealed = dict(event)
    sealed["prev_hash"] = prev_hash
    sealed["event_hash"] = compute_event_hash(prev_hash, event)
    return sealed


def chain_events(
    events: Iterable[Mapping[str, Any]], prev_hash: str = GENESIS_HASH
) -> list[dict[str, Any]]:
    """Seal a whole sequence, each event behind the one before it."""
    sealed: list[dict[str, Any]] = []
    link = prev_hash
    for event in events:
        row = seal_event(event, link)
        link = row["event_hash"]
        sealed.append(row)
    return sealed


def chain_head(events: Sequence[Mapping[str, Any]]) -> str:
    """The **stored** head: the last event's ``event_hash``, verbatim.

    This is what the next append links behind, so it reads the column rather than
    recomputing -- a writer appending event *n+1* wants the head the stream *claims*, and
    :func:`verify_chain` is what decides whether that claim is true.

    It is therefore **not** a digest of the stream and must never be used as one. Use
    :func:`stream_hash` where the question is "is this the same stream?".

    Raises:
        ChainIntegrityError: the last event carries no ``event_hash``. The old behaviour was
            to skip such an event and return an earlier head, which meant an unsealed tail
            silently produced the head of the sealed prefix -- and in T-060's in-memory
            store, where no ``char(64) NOT NULL`` column exists to make that impossible, the
            next append would then have linked behind the wrong event.
    """
    ordered = list(events)
    if not ordered:
        return GENESIS_HASH
    stored = ordered[-1].get("event_hash")
    if not stored:
        raise ChainIntegrityError(
            f"the last event in the stream (index {len(ordered) - 1}) carries no "
            f"`event_hash`: it was never sealed, so there is no head to link behind. "
            f"Append through seal_event() so every event is sealed as it lands."
        )
    return str(stored)


def stream_hash(events: Iterable[Mapping[str, Any]]) -> str:
    """The stream's identity, **recomputed from content** -- never read off a column.

    Folds :func:`~.canonical.compute_event_hash` over every event from
    :data:`~.canonical.GENESIS_HASH`, ignoring the stored ``prev_hash`` and ``event_hash``
    entirely. That is the whole point: this used to return :func:`chain_head`, so the
    "replay reproduces the stream hash" assertion (T-011 acceptance 3, T-060 acceptance 3)
    compared a stored column against the same stored column and would have passed against
    any self-consistent forgery -- and against a stream whose events had been rewritten
    wholesale, as long as the digests were rewritten with them.

    Recomputed, it commits to every field of every event in order, so it changes if any
    event's content changes, if two events swap places, or if one is inserted or removed.
    Compare it against a value recorded at write time (``AppendResult.head_hash``, or
    ``ledger.chain_head.head_hash``) and the comparison has content on both sides.
    """
    prev = GENESIS_HASH
    for event in events:
        prev = compute_event_hash(prev, event)
    return prev


def verify_chain(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Verify a sealed stream link by link.

    Args:
        events: the stream in insertion order. Each event is expected to carry the
            ``prev_hash`` / ``event_hash`` fields :func:`seal_event` writes.

    Returns:
        ``{"ok", "broken_at", "reason", "head_hash", "verified"}`` -- **the same five keys
        on every outcome**, so a caller never has to know which branch it is on before
        reading one. ``broken_at`` is the index of the first event that does not verify and
        is ``None`` for a clean stream; ``head_hash`` is the head as far as verification
        got, which is the whole stream's head when ``ok``; ``verified`` is how many events
        checked out before it stopped.

    Three ways a stream breaks, all reported at the offending index:

    * ``unsealed`` -- the event carries no ``event_hash``, so there is nothing to check it
      against. An unsealed stream is unverifiable, which is not the same as verified.
    * ``broken_link`` -- the event's stored ``prev_hash`` is not the previous event's
      ``event_hash``: the stream has been reordered, spliced or truncated in the middle.
    * ``tampered`` -- the event's content no longer hashes to its stored ``event_hash``.
    """

    def broken(index: int, reason: str, head: str) -> dict[str, Any]:
        return {
            "ok": False,
            "broken_at": index,
            "reason": reason,
            "head_hash": head,
            "verified": index,
        }

    prev = GENESIS_HASH
    index = -1
    for index, event in enumerate(events):
        stored_hash = event.get("event_hash")
        if not stored_hash:
            return broken(index, "unsealed", prev)
        stored_prev = event.get("prev_hash")
        if stored_prev is not None and str(stored_prev) != prev:
            return broken(index, "broken_link", prev)
        if compute_event_hash(prev, event) != str(stored_hash):
            return broken(index, "tampered", prev)
        prev = str(stored_hash)
    return {
        "ok": True,
        "broken_at": None,
        "reason": None,
        "head_hash": prev,
        "verified": index + 1,
    }
