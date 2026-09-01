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

from collections.abc import Iterable, Mapping
from typing import Any

from .canonical import GENESIS_HASH, compute_event_hash

__all__ = [
    "GENESIS_HASH",
    "chain_events",
    "chain_head",
    "seal_event",
    "stream_hash",
    "verify_chain",
]


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


def chain_head(events: Iterable[Mapping[str, Any]]) -> str:
    """The last event's ``event_hash``, or the genesis link for an empty stream."""
    head = GENESIS_HASH
    for event in events:
        stored = event.get("event_hash")
        if stored:
            head = str(stored)
    return head


#: The event-stream hash of T-060 acceptance 3 is the chain head: it commits to every event
#: in the stream, in order, because each link is folded into the next. Named separately from
#: :func:`chain_head` so the two concepts can diverge later without a rename at every call
#: site, and so "replay reproduces the stream hash" has one function to point at.
def stream_hash(events: Iterable[Mapping[str, Any]]) -> str:
    """The stream's identity: the chain head over ``events``.

    Recomputing this from a replayed stream and comparing it to the head recorded at write
    time is the "replay reproduces the stream hash" assertion (T-011 acceptance 3, T-060
    acceptance 3).
    """
    return chain_head(events)


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
