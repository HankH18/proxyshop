"""R14: a submitted answer lands as a ``feedback`` ``LedgerEvent`` (T-073).

    >>> from apps.buyer.svc.src.feedback import submit_feedback
    >>> event = submit_feedback(order, {"choice": "yes_as_described"}, sink)
    >>> event.kind, event.order_ref, sorted(event.payload)
    (<LedgerEventKind.feedback: 'feedback'>, 'ord-e7-101', ['dim', 'matched_pitch', 'reason', 'type'])

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
Recorded in a :class:`FeedbackLedger` before the sink is called. It is process-local, it is not a
distributed lock, and the authoritative gate is trust's ``routed_orders`` table — the same honesty
T-072 gives the accept ledger. What this closes is the common case (one process, one buyer, a
double-clicked Submit) and, more usefully, it makes a duplicate a *refusal a caller can see*
rather than a second row in the trust dimension.

The subtle half is what happens when the sink RAISES. This module used to release the claim then,
on the reasoning "nothing landed" — and that reasoning is wrong for any at-least-once client whose
write commits and whose acknowledgement is lost, which is most of them. The claim now stands, a
blind retry is refused, and :class:`~buyer_svc.feedback.errors.LedgerWriteUncertain` hands back
the ``event_id`` to re-attempt with so the second write is the same row. See :class:`FeedbackLedger`.

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
    LedgerWriteUncertain,
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
    "FEEDBACK_DIMENSION",
    "FEEDBACK_KIND",
    "FEEDBACK_NEGATIVE_TYPE",
    "FEEDBACK_POSITIVE_TYPE",
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

#: The trust dimension a routed buyer's answer lands on, and the two observation types it can
#: become. These three keys are what turn a ``feedback`` row into a trust OBSERVATION, and
#: without them R14's loop does not exist — see :func:`feedback_payload` for the measurement.
#:
#: ``feedback_match`` is one of ``contracts.TRUST_DIMENSIONS`` (D53's closed six) and is the only
#: dimension buyer feedback may touch: the approved manifest says it "takes NO verification
#: outcome at all. It is the post-purchase, buyer-reported match between pitch and delivery
#: (R14), cross-checked against return behaviour."
FEEDBACK_DIMENSION = "feedback_match"

#: What "it matched the pitch" becomes. ``fulfilled`` and deliberately not ``verified``: the four
#: verification statuses are what a claim verifier decides about a catalog fact, and this
#: dimension takes none of them. ``fulfilled`` is the published positive for "a promise the
#: transaction record shows was kept", which is what a routed buyer is reporting.
FEEDBACK_POSITIVE_TYPE = "fulfilled"

#: What "it did not match the pitch" becomes. The approved manifest's ``dishonest_store``
#: behaviour ``pitch_delivery_mismatch`` is ``{dim: feedback_match, type: mismatch_return}``,
#: glossed there as "the buyer reports that what arrived does not match what was pitched".
#:
#: KNOWN GAP, stated rather than papered over: the manifest's behaviour is a mismatch report
#: *and a return*, and the published vocabulary has no second buyer-reported negative — so
#: ``never_arrived`` and ``not_as_described`` land on the same 1.5 as a returned mismatch.
#: Splitting them needs a new published weight, which is a manifest change and not this
#: service's to make.
FEEDBACK_NEGATIVE_TYPE = "mismatch_return"

#: The three names above are spelled HERE rather than imported from ``apps/trust``, and that is
#: a dependency rule rather than a convenience: the buyer service does not depend on the trust
#: service's package (it reaches it over HTTP, through ``proxyshop_support.trust_ledger``), and
#: importing the scorer to learn two strings would put a buyer deployment's import graph through
#: the trust engine. Ground truth is ``fixtures/manifest.json`` (D18/A3) — the human-approved
#: document the scorer itself reads its weights from — and
#: ``tests/test_feedback_ledger_wiring.py`` compares all three against it and against
#: ``contracts.TRUST_DIMENSIONS``, so a drift is a failure in this lane rather than a silently
#: unscored observation.

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

#: What a rejected value has to look like before it is quoted back: the shape of an option id,
#: which is lower_snake_case. A typo'd id (``"yes_as_describd"``) is the case worth echoing and
#: this admits it; everything else is reported by length only.
#:
#: That is an R5 rule rather than a length rule. The prompt offers no free-text field, so a
#: ``choice`` carrying prose is a client that invented one, and prose a buyer typed is exactly
#: where their own name and email address turn up — and a refusal travels into an exception
#: message, a log line and an HTTP response body, none of whose destinations this package
#: controls. The earlier pattern allowed capitals, which let ``Dana_Reyes_1985`` through; that is
#: the residual this shape closes. It is not airtight and the docstring does not claim it is: a
#: lower-case single token could still be a name. It cannot be a sentence, an email address (no
#: ``@``), a phone number (no long digit runs with punctuation) or a postal address (no spaces).
_ECHOABLE = re.compile(rf"^[a-z][a-z0-9_]{{0,{MAX_ECHOED_CHOICE - 1}}}$")


class FeedbackLedger:
    """Which orders this process has submitted feedback for, as which event, and how certainly.

    Each entry is ``(event_id, landed)``. The second half is the whole design, and it exists
    because an earlier version of this class got the retry story exactly backwards.

    **The measured failure.** :func:`submit_feedback` used to *release* the claim whenever the
    sink raised, on the reasoning "nothing landed, so the claim was not spent". An exception out
    of a ledger client does not prove that. Any at-least-once client whose write commits and
    whose acknowledgement is then lost — a socket reset after send, a read timeout — raises with
    the row already on the ledger. Released, the next honest retry minted a **second** event id
    and wrote a **second** ``feedback`` row for one order: the doubled ``feedback_match``
    observation this whole package exists to prevent, produced by the recovery path rather than
    by an attacker.

    **The rule now.** A sink exception leaves the claim standing, holding the event id that may
    or may not have landed. A blind retry — one that mints a fresh event id — is refused. A
    *deliberate* retry, one that passes the same ``event_id`` back to :func:`submit_feedback`, is
    admitted, so the second attempt writes the SAME row and any ledger that de-duplicates on
    ``event_id`` ends up with one. That is the difference between "retryable" and "safe to
    retry", and only the caller holding the id can tell the two apart.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, tuple[str, bool]] = {}

    def claim(self, order_ref: str, event_id: str) -> None:
        """Reserve ``order_ref`` for ``event_id``. Raises unless this is a re-attempt of it."""
        with self._lock:
            held = self._events.get(order_ref)
            if held is None:
                self._events[order_ref] = (event_id, False)
                return
            held_id, landed = held
            if held_id == event_id and not landed:
                return  # a deliberate re-attempt of the one event whose fate is unknown
            raise FeedbackAlreadySubmitted(
                f"feedback for order {order_ref!r} has already been "
                f"{'recorded' if landed else 'submitted, with its outcome unknown'}; R14 offers "
                f"one structured prompt per routed order, and a second submission would move the "
                f"store's trust score twice for one purchase."
                + (
                    ""
                    if landed
                    else " If you are retrying that submission, pass its event_id back to "
                    "submit_feedback so the ledger writes the same row rather than a second one."
                ),
                held_id,
            )

    def record(self, order_ref: str, event_id: str) -> None:
        """Mark this order's event as having definitely landed."""
        with self._lock:
            self._events[order_ref] = (event_id, True)

    def release(self, order_ref: str) -> None:
        """Give back a claim for which **nothing was attempted**.

        Called on the one path where that is knowable: the sink turned out to be unusable, so no
        write was ever issued. Never called after the sink itself raised — see the class
        docstring for why that reading cost a doubled trust observation.
        """
        with self._lock:
            held = self._events.get(order_ref)
            if held is not None and not held[1]:
                del self._events[order_ref]

    def event_for(self, order_ref: str) -> str | None:
        """The event id this order's feedback became, or ``None`` if it has none here."""
        with self._lock:
            held = self._events.get(order_ref)
            return None if held is None else held[0]

    def landed(self, order_ref: str) -> bool:
        """Whether this order's event is known to have reached the ledger."""
        with self._lock:
            held = self._events.get(order_ref)
            return bool(held and held[1])

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
        # The value is NOT interpolated, and a bare string is the likeliest way a buyer's own
        # prose reaches this function: `submit_feedback(order, "the seller lied, I'm Dana", sink)`
        # is the shortest wrong call, and echoing its argument would put that sentence in a log
        # line and an HTTP body.
        raise MissingFeedbackChoice(
            f"a feedback response is the object the prompt was answered with, not "
            f"{type(response).__name__}. Send "
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
    """The published ``feedback`` body for one answered prompt, plus its trust routing.

    The two keys ``contracts.LEDGER_PAYLOAD_SHAPES["feedback"]`` publishes — ``matched_pitch``,
    the boolean ``trust.feedback.engine._is_positive`` reads, and ``reason``, the option id from
    a closed vocabulary rather than prose — and the ``dim``/``type`` pair that makes the row a
    trust OBSERVATION rather than an inert record of one.

    WHY THE ROUTING IS HERE, WHICH IS THE WHOLE OF R14'S SECOND HALF
    ----------------------------------------------------------------
    ``trust.ledger.replay.observations_from_events`` is the one projection the trust scorer,
    ``GET /events/replay?snapshots=true`` and ``POST /events``' own poison check all run, and it
    makes an observation **only** from an event whose payload names both a ``dim`` and a
    ``type``. Measured on this tree before these two keys existed, on a payload this function
    itself produced::

        >>> observations_from_events([{"kind": "feedback", "store_id": "st-1", "ts": "...",
        ...                            "payload": {"matched_pitch": True,
        ...                                        "reason": "yes_as_described"}}])
        []

    So a shopper's answer landed on the append-only ledger, was replayed forever, and moved no
    store's posture by any amount. The loop R14 exists to close did not exist.

    The translation belongs to the EMITTER by precedent and by argument. ``trust.reconcile.
    engine.observation_events`` says it in its own docstring — "these events are shaped to what
    that consumer already requires, rather than the consumer being asked to learn what a
    ``reconciled`` payload means" — and this service is the emitter of ``feedback``. Extra keys
    are admitted by design (``contracts.validate_ledger_payload``: "a vendor body carries
    plenty"), so the published shape is satisfied exactly as before.

    NO ``weight``, AND THAT IS A DECISION
    --------------------------------------
    R14 weights feedback by buyer track record and cross-checks it against return behaviour.
    The buyer service can see neither: it holds no return record and no cross-order history of a
    pseudonym. An absent ``weight`` is read by the scorer as exactly ``1.0``, which is
    ``trust.feedback.engine.BASE_FEEDBACK_WEIGHT`` — "the weight of one accepted, uncontradicted
    piece of feedback" — so what is emitted is the honest base case, and the discount for a
    contradicting return stays where the evidence for it is. Inventing a number here would be a
    second opinion about a quantity D49 gives to exactly one owner.
    """
    choice = _chosen(response)
    return {
        "matched_pitch": choice.matched_pitch,
        "reason": choice.id,
        "dim": FEEDBACK_DIMENSION,
        "type": FEEDBACK_POSITIVE_TYPE if choice.matched_pitch else FEEDBACK_NEGATIVE_TYPE,
    }


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
        event_id: the event's id. Generated when omitted. Pass one back to **re-attempt** a
            submission that raised :class:`~buyer_svc.feedback.errors.LedgerWriteUncertain`: the
            same id writes the same row, which is what makes the retry safe rather than merely
            possible. A fresh id for an order already claimed is refused.

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
        LedgerSinkUnusable: there is nowhere to record this. **Nothing was attempted**, and the
            order's claim is given back, so a corrected deployment can submit normally.
        LedgerWriteUncertain: the sink raised. Whether the event landed is unknown, the claim
            stands, and the refusal carries the ``event_id`` to retry with.
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
    book.claim(routed.order_ref, event.event_id)
    try:
        name, method = _sink_entrypoint(sink)
    except Exception:
        # The ONE path on which "nothing was written" is knowable: there was nothing to write
        # with. Give the claim back, or a mis-wired deployment would permanently burn this
        # buyer's one piece of feedback the first time anyone tried.
        book.release(routed.order_ref)
        raise

    try:
        method(event)
    except Exception as exc:
        # NOT released, and NOT re-raised as itself. Two separate reasons:
        #
        # * the claim stands because a sink that raised may still have written (see
        #   FeedbackLedger). Retrying is allowed — with this event id, so the ledger writes the
        #   same row — and a blind retry is refused;
        # * the sink's own exception does not travel. A ledger client's message routinely
        #   carries a DSN, a host, or credentials, and this exception is rendered into an HTTP
        #   response body by the route above. It is logged here, where it belongs, and replaced
        #   with one that says what the caller actually needs to do.
        _log.error(
            "the ledger sink raised while recording feedback for order %s as %s; the write may "
            "or may not have landed: %s: %s",
            routed.order_ref,
            event.event_id,
            type(exc).__name__,
            exc,
        )
        raise LedgerWriteUncertain(
            f"the ledger sink raised while recording feedback for order {routed.order_ref!r}, so "
            f"whether it landed is unknown. The submission is NOT being treated as failed: retry "
            f"it with event_id={event.event_id!r} so a ledger that de-duplicates writes one row "
            f"rather than two. A fresh submission for this order is refused.",
            event.event_id,
        ) from exc

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
