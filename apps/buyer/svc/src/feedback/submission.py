"""R14: a submitted answer lands as a ``feedback`` ``LedgerEvent`` (T-073).

    >>> from apps.buyer.svc.src.feedback import submit_feedback
    >>> event = submit_feedback(order, {"choice": "yes_as_described"}, sink)
    >>> event.kind, event.order_ref, event.payload
    (<LedgerEventKind.feedback: 'feedback'>, 'ord-e7-101', {'matched_pitch': True, 'reason': 'yes_as_described'})

"Lands as a ledger event" is the whole requirement, and the two halves of it are equally
load-bearing: a *real event on the ledger*, not a row in a table this service owns, and the
*published body* for that kind, not whatever fields happened to be in scope.

The published body, and why it is checked here
----------------------------------------------
``contracts.LEDGER_PAYLOAD_SHAPES["feedback"] == ("matched_pitch", "reason")``. That table is
D24's "one shape per kind" in the only form an append-only ledger can hold it — a description a
**producer** is checked against, never a filter the store applies to history — and
``contracts.validate_ledger_payload`` is the check. It is deliberately not wired into
``LedgerEvent`` itself, so a producer that does not call it is not checked at all.

This module calls it, on every event, before the event leaves. That is not defensive
programming for its own sake: there is a live defect of exactly this shape elsewhere in this
repo (T-235, HIGH), where ``apps/exchange/src/checkout/provider.py:983`` emits a
``code_created`` event carrying none of its three published keys while another path emits the
same kind correctly — one kind, two bodies, because nothing on that path validates. A ledger
whose rows of one kind have two shapes cannot be replayed, and the reader that discovers this
is downstream, later, and reading history that can no longer be fixed.

Once per order
--------------
Recorded in a :class:`FeedbackLedger` before the sink is called, and released if the call itself
fails so a genuine retry after a transient sink outage still works. Same shape T-072 gives the
accept ledger, and with the same honesty about its limits: it is process-local, it is not a
distributed lock, and the authoritative gate is trust's ``routed_orders`` table. What this closes
is the common case — one process, one buyer, a double-clicked Submit — and, more usefully, it
makes a duplicate a *refusal a caller can see* rather than a second row in the trust dimension.

The sink contract
-----------------
``sink`` is whatever records ledger events: the exchange's ledger client, an outbox writer, or a
recording double. It may expose any of :data:`LEDGER_SINK_METHODS`, or be a bare callable, and it
is called **exactly once** with **one positional argument** — the ``LedgerEvent``. One positional
and no keywords is deliberate: the frozen acceptance suite's sink collects positional args and
keyword *values* alike, so a call passing both would emit one event and record two.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from contracts import LedgerEvent, LedgerEventKind, validate_ledger_payload

from ._reading import first, flag, read, text
from .errors import (
    ContradictoryFeedback,
    FeedbackAlreadySubmitted,
    FeedbackNotYours,
    LedgerSinkUnusable,
    MalformedFeedbackEvent,
    MissingFeedbackChoice,
    OrderNotRouted,
    UnknownFeedbackChoice,
    UnknownFeedbackQuestion,
    UnusableOrder,
)
from .prompt import CHOICE_IDS, CHOICES_BY_ID, PROMPT_QUESTION_ID, FeedbackChoice
from .routing import ORDER_REF_FIELDS, Routing, routing

_log = logging.getLogger(__name__)

__all__ = [
    "FEEDBACK_KIND",
    "LEDGER_SINK_METHODS",
    "MAX_ECHOED_CHOICE",
    "RESPONSE_CHOICE_FIELDS",
    "RESPONSE_QUESTION_FIELDS",
    "FeedbackLedger",
    "event_view",
    "feedback_payload",
    "reset_submitted",
    "submit_feedback",
    "submitted",
]

#: The one ledger kind this package produces. A string rather than the enum where it is compared,
#: because ``LedgerEventKind`` is a ``StrEnum`` and the wire spells it plainly.
FEEDBACK_KIND = "feedback"

#: Method names a ledger sink may expose, most specific first. The first callable one wins; a
#: bare callable sink is the last resort. Mirrors ``buyer_svc.accept.EXCHANGE_ACCEPT_METHODS``.
#:
#: ``send`` is deliberately absent. ``trust.feedback.engine.push_trust_event`` calls
#: ``sink.send(store_id, payload)`` — two positional arguments, a different contract — and a sink
#: that happened to satisfy both would be called with the wrong arity by whichever of us guessed.
LEDGER_SINK_METHODS: tuple[str, ...] = (
    "append",
    "emit",
    "record",
    "publish",
    "write",
    "log_event",
    "add",
)

#: Where a response names the option the buyer picked, in order. ``reason`` is last and is there
#: because it is the payload key this package itself writes, so an event round-tripped back in is
#: readable.
RESPONSE_CHOICE_FIELDS: tuple[str, ...] = (
    "choice",
    "answer",
    "option",
    "selection",
    "value",
    "reason",
)

#: Where a response names the question it is answering. ``question`` is NOT in this list: it
#: holds the question *text* on a round-tripped prompt, and comparing that to a question id would
#: refuse a well-formed answer.
RESPONSE_QUESTION_FIELDS: tuple[str, ...] = ("question_id", "prompt_id")

#: Longest rejected value echoed back verbatim in a refusal.
MAX_ECHOED_CHOICE = 64

#: What a rejected value has to look like before it is quoted back. A typo'd option id
#: (``"yes_as_describd"``) is the case worth echoing, and this admits it. Everything else is
#: reported by length only, and that is an R5 rule rather than a length rule: the prompt offers
#: no free-text field, so a ``choice`` carrying a sentence is a client that invented one, and the
#: sentence a buyer typed is exactly where their own name and email address turn up. A refusal is
#: not a safe place to put it — it travels into an exception message, a log line and an HTTP
#: response body, none of which this package controls the destination of.
_ECHOABLE = re.compile(rf"^[A-Za-z0-9_.\-]{{1,{MAX_ECHOED_CHOICE}}}$")


class FeedbackLedger:
    """Which orders this process has already recorded feedback for, and as which event."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, str] = {}

    def claim(self, order_ref: str) -> None:
        """Reserve ``order_ref``. Raises if its feedback was already recorded."""
        with self._lock:
            if order_ref in self._events:
                raise FeedbackAlreadySubmitted(
                    f"feedback for order {order_ref!r} has already been recorded; R14 offers one "
                    f"structured prompt per routed order. A second submission would move the "
                    f"store's trust score twice for one purchase.",
                    self._events[order_ref],
                )
            self._events[order_ref] = ""

    def record(self, order_ref: str, event_id: str) -> None:
        with self._lock:
            self._events[order_ref] = event_id

    def release(self, order_ref: str) -> None:
        """Give an unspent claim back, so a failed submission can honestly be retried."""
        with self._lock:
            if self._events.get(order_ref) == "":
                del self._events[order_ref]

    def event_for(self, order_ref: str) -> str | None:
        """The event id this order's feedback became, or ``None`` if it has none here."""
        with self._lock:
            return self._events.get(order_ref)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


_SUBMITTED = FeedbackLedger()


def submitted() -> FeedbackLedger:
    """The process-wide ledger :func:`submit_feedback` uses when none is injected."""
    return _SUBMITTED


def reset_submitted() -> None:
    """Forget every submission. For tests and for a fresh process only."""
    submitted().reset()


def _echo(value: str) -> str:
    """A rejected value in a form that is safe to put in a message. See :data:`_ECHOABLE`."""
    if _ECHOABLE.match(value):
        return repr(value)
    return f"<{len(value)} characters, which is not an option id>"


def _chosen(response: Any) -> FeedbackChoice:
    """The published option this response picked, or refuse.

    This is where "structured, no free text" stops being a property of the UI. The prompt offers
    no text box, but a client posts JSON and can put a sentence in ``choice`` anyway; admitting it
    would put a free-text feedback body on the ledger under a kind whose published shape says
    otherwise, and would hand ``trust`` a ``reason`` it has no vocabulary for.
    """
    if response is None or isinstance(response, (str, bytes, int, float, bool)):
        raise MissingFeedbackChoice(
            f"a feedback response is the object the prompt was answered with, not "
            f"{type(response).__name__} ({response!r}). Send "
            f'{{"question_id": "{PROMPT_QUESTION_ID}", "choice": "<one of {list(CHOICE_IDS)}>"}}.'
        )

    asked = first(response, RESPONSE_QUESTION_FIELDS)
    if asked and asked != PROMPT_QUESTION_ID:
        raise UnknownFeedbackQuestion(
            f"this response answers question {_echo(asked)}, but the only question R14 asks is "
            f"{PROMPT_QUESTION_ID!r} — did it match the pitch. Nothing was recorded."
        )

    picked = first(response, RESPONSE_CHOICE_FIELDS)
    if not picked:
        raise MissingFeedbackChoice(
            f"this feedback response names no choice; looked for {list(RESPONSE_CHOICE_FIELDS)}. "
            f"The prompt is a single-select over {list(CHOICE_IDS)} and there is no free-text "
            f"field to fall back to."
        )

    choice = CHOICES_BY_ID.get(picked)
    if choice is None:
        raise UnknownFeedbackChoice(
            f"{_echo(picked)} is not one of this prompt's options. R14's prompt is structured: "
            f"the answer must be one of {list(CHOICE_IDS)}, and free text is not an option the "
            f"buyer was offered — recording it would put a body on the ledger that "
            f"'feedback' has no published shape for.",
            _echo(picked),
            CHOICE_IDS,
        )

    stated = flag(read(response, PROMPT_QUESTION_ID))
    if stated is not None and stated is not choice.matched_pitch:
        raise ContradictoryFeedback(
            f"this response says matched_pitch={stated!r} and also picked {choice.id!r}, which "
            f"means matched_pitch={choice.matched_pitch!r}. Both spellings reach the ledger "
            f"event, so there is no way to record this that is not a guess about which half the "
            f"buyer meant. Nothing was recorded."
        )
    return choice


def feedback_payload(response: Any) -> dict[str, Any]:
    """The published ``feedback`` body for one answered prompt.

    Exactly the two keys ``contracts.LEDGER_PAYLOAD_SHAPES["feedback"]`` publishes, and nothing
    else. ``matched_pitch`` is the boolean ``trust.feedback.engine._is_positive`` reads;
    ``reason`` is the option id, which is a closed vocabulary rather than prose.
    """
    choice = _chosen(response)
    return {"matched_pitch": choice.matched_pitch, "reason": choice.id}


def _sink_entrypoint(sink: Any) -> tuple[str, Any]:
    """The one callable that records an event on this sink."""
    if sink is None:
        raise LedgerSinkUnusable(
            "submit_feedback() was given no ledger sink, so this feedback has nowhere to land. "
            "R14's promise is that a submission becomes a ledger event; recording it nowhere and "
            "returning quietly would look identical to the buyer and grade the store on nothing."
        )
    for name in LEDGER_SINK_METHODS:
        candidate = getattr(sink, name, None)
        if callable(candidate):
            return name, candidate
    if callable(sink):
        return "__call__", sink
    raise LedgerSinkUnusable(
        f"the ledger sink {type(sink).__name__} exposes none of {list(LEDGER_SINK_METHODS)} and "
        f"is not callable, so this feedback cannot be recorded."
    )


def _guard_owner(routed: Routing, buyer_pseudonym: str) -> None:
    """Refuse feedback about an order the network routed to somebody else."""
    if not buyer_pseudonym:
        return
    if not routed.buyer_pseudonym:
        # Worth a line in the log rather than nothing: the caller asked for the feedback to be
        # bound to a buyer and the order record names none, so the binding did not happen. On
        # today's order shape there is no `buyer_pseudonym` to compare against; a composition
        # root that has one should put it on the record.
        _log.warning(
            "feedback for order %s was submitted under a buyer pseudonym, but the order record "
            "names no buyer_pseudonym, so ownership was NOT checked",
            routed.order_ref or "<unnamed>",
        )
        return
    if routed.buyer_pseudonym != buyer_pseudonym:
        raise FeedbackNotYours(
            f"order {routed.order_ref!r} was routed for a different buyer, so this session may "
            f"not leave feedback about it. R14 admits the feedback of the buyer the network "
            f"routed, not of anyone who can name an order reference."
        )


def submit_feedback(
    order: Any,
    response: Any,
    sink: Any,
    *,
    buyer_pseudonym: str = "",
    ledger: FeedbackLedger | None = None,
    now: datetime | None = None,
    event_id: str | None = None,
) -> LedgerEvent:
    """Record one buyer's answer as a ``feedback`` ledger event (R14).

    Args:
        order: the order record the feedback is about. Must be network-routed — see
            :mod:`buyer_svc.feedback.routing` for what that means and what it deliberately does
            not mean.
        response: the answered prompt — ``{"question_id": "matched_pitch", "choice": "<option
            id>"}``. The choice must be one of :data:`~buyer_svc.feedback.prompt.CHOICE_IDS`.
        sink: whatever records ledger events. Called **exactly once**, with the event as its one
            positional argument.
        buyer_pseudonym: the pseudonym of the session submitting, when the caller has an
            authenticated one. Checked against the order's own ``buyer_pseudonym``; ``""``
            (the default) means the caller cannot bind an owner and none is checked.
        ledger: the submission ledger; defaults to the process-wide one.
        now: timestamp for the event.
        event_id: the event's id. Generated when omitted; injectable so a test can pin it.

    Returns:
        The :class:`~contracts.LedgerEvent` that was emitted, so a caller can log or return
        exactly what landed.

    Raises:
        UnusableOrder: this is not an order record, or it names no ``order_ref``.
        OrderNotRouted: R14's gate. **Nothing was emitted.**
        FeedbackNotYours: the order was routed for a different buyer.
        MissingFeedbackChoice / UnknownFeedbackChoice / UnknownFeedbackQuestion /
        ContradictoryFeedback: the answer is not one of the published structured options.
        FeedbackAlreadySubmitted: this order's feedback was already recorded in this process.
        LedgerSinkUnusable: there is nowhere to record this.
        MalformedFeedbackEvent: the body built here does not match ``feedback``'s published
            shape. It cannot fire on today's code, and that is the point.
    """
    routed = routing(order)
    if not routed.order_ref:
        raise UnusableOrder(
            f"this order record names no order reference (looked for "
            f"{list(ORDER_REF_FIELDS)}), so its feedback could not be attached to an order. A "
            f"ledger event with no order_ref cannot be joined to anything (D24)."
        )
    if not routed.routed:
        raise OrderNotRouted(
            f"no feedback was recorded for order {routed.order_ref!r}: {routed.reason}",
            routed.reason,
        )
    _guard_owner(routed, text(buyer_pseudonym))

    payload = feedback_payload(response)
    problems = validate_ledger_payload(FEEDBACK_KIND, payload)
    if problems:
        raise MalformedFeedbackEvent(
            f"the feedback body built for order {routed.order_ref!r} does not match the shape "
            f"published for the 'feedback' ledger kind: {'; '.join(problems)}. Fix the payload "
            f"here rather than the ledger — a kind with two bodies cannot be replayed.",
            tuple(problems),
        )

    moment = (now or datetime.now(UTC)).astimezone(UTC)
    event = LedgerEvent(
        event_id=event_id or f"fb-{uuid.uuid4().hex}",
        ts=moment.isoformat().replace("+00:00", "Z"),
        kind=LedgerEventKind.feedback,
        auction_id=routed.auction_id or None,
        store_id=routed.store_id or None,
        order_ref=routed.order_ref,
        # The buyer does not chain the ledger. `prev_hash` is the ledger writer's to set, and a
        # producer that invented one would be asserting a position in a history it cannot see.
        prev_hash=None,
        payload=payload,
    )

    book = ledger if ledger is not None else submitted()
    book.claim(routed.order_ref)
    try:
        name, method = _sink_entrypoint(sink)
        method(event)
    except Exception:
        # Nothing landed, so the claim was not spent. Give it back, or a transient sink outage
        # would permanently refuse this buyer's one piece of feedback.
        book.release(routed.order_ref)
        raise
    book.record(routed.order_ref, event.event_id)

    _log.info(
        "recorded feedback for order %s (auction %s, store %s) via sink.%s as %s",
        routed.order_ref,
        routed.auction_id or "<none>",
        routed.store_id or "<none>",
        name,
        event.event_id,
    )
    return event


def event_view(event: Any) -> dict[str, Any]:
    """A ``feedback`` event as a screen or a log line may see it. Never echoes the order record.

    Named fields only, for the reason T-072's ``AcceptedOffer.to_dict`` gives: an order record
    carries plenty this service was handed and should not hand back, and a projection that
    round-tripped it would put caller-chosen data one careless template away from a page.
    """
    payload = read(event, "payload", {}) or {}
    if not isinstance(payload, Mapping):
        payload = {}
    return {
        "event_id": text(read(event, "event_id")),
        "kind": text(read(event, "kind")),
        "ts": text(read(event, "ts")),
        "order_ref": text(read(event, "order_ref")),
        "store_id": text(read(event, "store_id")),
        "auction_id": text(read(event, "auction_id")),
        "matched_pitch": bool(payload.get("matched_pitch")),
        "reason": text(payload.get("reason")),
    }
