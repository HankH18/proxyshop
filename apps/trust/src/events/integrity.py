"""Verification that **names the link it broke on**. Owned by T-060.

:func:`trust.ledger.verify_chain` answers the question correctly and answers it in
machine terms: ``{"ok": False, "broken_at": 2, "reason": "tampered", ...}``. That is
everything a program needs and almost nothing a person needs -- index 2 of a stream the
reader is not holding identifies nothing, and "tampered" and "broken_link" are two very
different accusations that look identical at a glance.

A hash chain is only evidence if breaking it is *detectable and legible*. So this module
wraps the verifier and adds the two things that make a failure actionable:

``broken_event``
    the event at the offending index, by ``event_id`` and ``seq``, with the digest it
    stores next to the digest its content actually hashes to.
``detail``
    one sentence naming that event, the reason, and what the reason means -- including,
    for ``broken_link``, the *predecessor* it should have pointed at and did not.

It adds no verification of its own. Every ``ok`` in the returned mapping is
:func:`trust.ledger.verify_chain`'s ``ok``; a second opinion about integrity, computed
here, is the one thing this module must never grow.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..ledger import (
    GENESIS_HASH,
    CanonicalisationError,
    compute_event_hash,
    stream_hash,
    verify_chain,
)

__all__ = ["anchored_report", "describe_break", "verify_stream"]

#: What each ``verify_chain`` reason actually means, in the caller's terms.
_REASON_MEANINGS = {
    "unsealed": (
        "the event carries no event_hash, so there is nothing to check it against; an "
        "unsealed stream is unverifiable, which is not the same as verified"
    ),
    "broken_link": (
        "the event's stored prev_hash is not its predecessor's event_hash: the stream has "
        "been reordered, spliced, or had an event removed from the middle"
    ),
    "tampered": (
        "the event's content no longer hashes to the event_hash stored on it: its body was "
        "rewritten after it was sealed"
    ),
    "empty": (
        "there are no events at all; verifying nothing is not verifying, and an empty read "
        "arrives for reasons unrelated to integrity (wrong database, missing SELECT grant, "
        "a store that never loaded)"
    ),
    "truncated": (
        "every link verifies, but there are fewer events than the anchor recorded: the "
        "stream was cut from the tail, which no link inside it can detect"
    ),
    "head_mismatch": (
        "every link verifies, but the stream folds to a different head than the one "
        "recorded at write time: this is not the stream that was committed to"
    ),
    "anchor_missing": (
        "every link verifies, but the commitment that makes truncation detectable is gone, "
        "so 'intact' cannot be asserted"
    ),
}


def _recomputed_hash(prev: str, event: Mapping[str, Any]) -> str | None:
    """What ``event`` hashes to behind ``prev``, or ``None`` if it cannot be canonicalised."""
    try:
        return compute_event_hash(prev, event)
    except CanonicalisationError:
        return None


def describe_break(
    events: list[dict[str, Any]], report: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    """``(broken_event, detail)`` for one :func:`trust.ledger.verify_chain` result.

    Args:
        events: the stream that was verified, in the order it was verified.
        report: what ``verify_chain`` returned.

    Returns:
        ``broken_event`` is ``None`` when the chain is intact, and also when the break is
        one the *stream* cannot point at -- ``truncated``, ``head_mismatch``,
        ``anchor_missing`` and ``empty`` are all reported at an index past the last event,
        because the thing that is wrong is what is *absent*. ``detail`` is always a
        sentence, including for an intact chain.
    """
    reason = report.get("reason")
    verified = int(report.get("verified") or 0)
    if report.get("ok"):
        return None, (
            f"ledger chain intact: {verified} event(s) verified, head "
            f"{report.get('head_hash', GENESIS_HASH)}"
        )

    meaning = _REASON_MEANINGS.get(str(reason), "the chain did not verify")
    index = report.get("broken_at")
    if not isinstance(index, int) or not (0 <= index < len(events)):
        return None, (
            f"ledger chain NOT intact after {verified} event(s): reason={reason} -- {meaning}"
        )

    row = events[index]
    predecessor = events[index - 1] if index > 0 else None
    expected_prev = (
        GENESIS_HASH if predecessor is None else str(predecessor.get("event_hash") or "")
    )
    stored_hash = row.get("event_hash")
    broken: dict[str, Any] = {
        "index": index,
        "event_id": row.get("event_id"),
        "seq": row.get("seq"),
        "kind": row.get("kind"),
        "stored_prev_hash": row.get("prev_hash"),
        "stored_event_hash": stored_hash,
        "expected_prev_hash": expected_prev,
        "recomputed_event_hash": _recomputed_hash(expected_prev, row),
        "predecessor_event_id": None if predecessor is None else predecessor.get("event_id"),
    }

    where = (
        f"index {index} (event_id={row.get('event_id')!r}, seq={row.get('seq')!r}, "
        f"kind={row.get('kind')!r})"
    )
    detail = f"ledger chain broken at {where}: reason={reason} -- {meaning}."
    if reason == "tampered":
        detail += (
            f" It stores event_hash {stored_hash}, but its content hashes to "
            f"{broken['recomputed_event_hash']}."
        )
    elif reason == "broken_link" and predecessor is None:
        # Index 0 has no predecessor, and the sentence below must not invent one. Written
        # with the generic wording it read "the event before it (index -1,
        # event_id=None) hashes to 000...0" -- a negative index and a nameless event -- in
        # precisely the case where the reader most needs to be told what is MISSING. A
        # stream whose first event does not link to genesis is what "the front of the
        # ledger was deleted" and "the read started part-way through" both look like, and
        # neither is a claim about a predecessor.
        detail += (
            f" It is the FIRST event in this stream, so its prev_hash must be the genesis "
            f"link {GENESIS_HASH}; it stores {row.get('prev_hash')} instead. There is no "
            f"predecessor to compare it against: either the stream does not begin where "
            f"the ledger begins -- events ahead of it were removed, or the read was "
            f"filtered or started after seq 0 -- or this event's prev_hash was rewritten."
        )
    elif reason == "broken_link":
        detail += (
            f" It stores prev_hash {row.get('prev_hash')}, but the event before it "
            f"(index {index - 1}, event_id={broken['predecessor_event_id']!r}) hashes to "
            f"{expected_prev}."
        )
    return broken, detail


def verify_stream(
    events: Iterable[Mapping[str, Any]],
    *,
    expected_length: int | None = None,
    expected_head: str | None = None,
    allow_empty: bool = False,
) -> dict[str, Any]:
    """:func:`trust.ledger.verify_chain`, plus the identity of the link that broke.

    Args:
        events: the stream in insertion order.
        expected_length: the count recorded at write time. Without it, truncation from the
            tail is undetectable -- not merely unchecked, but undetectable in principle.
        expected_head: the head recorded at write time.
        allow_empty: accept zero events as intact.

    Returns:
        Every key ``verify_chain`` returns (``ok``, ``broken_at``, ``reason``,
        ``head_hash``, ``verified``) plus ``length``, ``stream_hash`` (recomputed from
        content, ``None`` if the stream cannot be canonicalised at all), ``broken_event``
        and ``detail``.
    """
    rows = [dict(row) for row in events]
    report: dict[str, Any] = dict(
        verify_chain(
            rows,
            expected_length=expected_length,
            expected_head=expected_head,
            allow_empty=allow_empty,
        )
    )
    broken, detail = describe_break(rows, report)
    report["length"] = len(rows)
    report["expected_length"] = expected_length
    report["expected_head"] = expected_head
    try:
        report["stream_hash"] = stream_hash(rows)
    except CanonicalisationError:
        report["stream_hash"] = None
    report["broken_event"] = broken
    report["detail"] = detail
    return report


def anchored_report(
    events: list[dict[str, Any]], anchor: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Verify ``events`` against the commitment ``anchor`` records, and name the break.

    The anchor is the *outside witness* ``verify_chain`` asks for. Without it a chain cut
    from the tail verifies perfectly -- delete the last forty of a hundred events and the
    remaining sixty are a flawless chain -- so a report computed without one is answering a
    narrower question than the caller asked. A missing anchor is therefore
    ``anchor_missing`` rather than ``ok``: the commitment being gone is the state an
    attacker who truncated the ledger would leave behind.

    The length half of ``anchor_ok`` is not redundant with the head half. A chain whose
    links all verify necessarily folds to its own last stored digest, so comparing the
    recomputed head against the anchor's head passes on any prefix that was cut and then
    re-anchored. It is the row count that catches that.
    """
    if anchor is None:
        report = verify_stream(events)
        report["anchor"] = None
        report["anchor_ok"] = False
        if report["ok"]:
            report["ok"] = False
            report["reason"] = "anchor_missing"
            report["broken_at"] = len(events)
            _, report["detail"] = describe_break(events, report)
        return report

    length = int(anchor["length"])
    report = verify_stream(
        events,
        expected_length=length,
        expected_head=str(anchor["head_hash"]) if length else None,
        allow_empty=length == 0,
    )
    report["anchor"] = dict(anchor)
    report["anchor_ok"] = bool(
        len(events) == length and (length == 0 or report["stream_hash"] == str(anchor["head_hash"]))
    )
    return report
