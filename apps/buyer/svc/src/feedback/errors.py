"""Refusals this package raises (T-073).

Every one of them is a **domain** refusal: the call was well formed and the answer is no. None
of them is a ``TypeError``, ``ValueError``, ``KeyError`` or ``AttributeError``, and that is the
whole point of the hierarchy — a caller catching :class:`FeedbackError` catches "the buyer
service would not record this feedback" and nothing else, while a genuinely broken call (the
wrong number of arguments, a typo'd attribute) still surfaces as the builtin it is.
T-071's :mod:`buyer_svc.intent.errors` and T-072's :mod:`buyer_svc.accept.errors` make the same
promise for the clarification loop and the checkout handoff, and this package follows it
deliberately: a consumer that already handles ``IntentError`` and ``AcceptError`` handles this
surface the same way.

The refusals split into three groups, and the split is the ticket:

* **the R14 gate** — :class:`OrderNotRouted`, and :class:`FeedbackNotYours` for the order that
  was routed to somebody else. These are the reason the package exists: without them a store
  buys its own ``feedback_match`` dimension with fake reviews;
* **the structured-answer gate** — :class:`MissingFeedbackChoice`, :class:`UnknownFeedbackChoice`,
  :class:`UnknownFeedbackQuestion`, :class:`ContradictoryFeedback`. R14 says *structured*, and
  the only place "structured" can be enforced is where an arbitrary string would otherwise
  become a ledger row;
* **the write gate** — :class:`FeedbackAlreadySubmitted`, :class:`LedgerSinkUnusable`,
  :class:`MalformedFeedbackEvent`.
"""

from __future__ import annotations

__all__ = [
    "ContradictoryFeedback",
    "FeedbackAlreadySubmitted",
    "FeedbackError",
    "FeedbackNotYours",
    "LedgerSinkUnusable",
    "MalformedFeedbackEvent",
    "MissingFeedbackChoice",
    "OrderNotRouted",
    "UnknownFeedbackChoice",
    "UnknownFeedbackQuestion",
    "UnusableOrder",
]


class FeedbackError(RuntimeError):
    """Base class for every refusal in :mod:`buyer_svc.feedback`."""


class UnusableOrder(FeedbackError):
    """The thing handed in is not an order record, or names no order.

    Distinct from :class:`OrderNotRouted` on purpose. "This is not an order" is a broken caller;
    "the network did not route this order" is a decision about a real order, and a screen
    handling the two the same way would tell a buyer their order was not routed when in fact
    nothing was passed at all.
    """


class OrderNotRouted(FeedbackError):
    """R14's gate: only a buyer the network actually routed may leave feedback.

    :attr:`reason` says which part of the gate refused, in words a screen can show.
    """

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class FeedbackNotYours(FeedbackError):
    """This order was routed for a different buyer's pseudonym.

    R14 admits the feedback of the buyer the network routed — not of anyone who can name their
    order reference. Without this check the routing gate is only as strong as the guessability
    of an order ref, which is not a secret.
    """


class MissingFeedbackChoice(FeedbackError):
    """The response names no choice, so there is no answer to record."""


class UnknownFeedbackChoice(FeedbackError):
    """The response names a choice that is not one of the published options.

    This is where "structured, no free text" is actually enforced. The prompt offers no
    free-text field, but a client posts JSON and could put a sentence in ``choice`` anyway; a
    ledger that accepted it would carry a free-text feedback body under a kind whose published
    shape says otherwise, and R14's "one structured prompt" would be true only of the UI.

    :attr:`choice` is the offending value, truncated — an enormous body must not become an
    enormous exception message, a log line and an HTTP response body.
    """

    def __init__(self, message: str, choice: str = "", options: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.choice = choice
        self.options = options


class UnknownFeedbackQuestion(FeedbackError):
    """The response answers a question this prompt did not ask."""


class ContradictoryFeedback(FeedbackError):
    """The response's explicit ``matched_pitch`` contradicts the option it chose.

    Both spellings reach the ledger event — ``matched_pitch`` is the boolean the trust service
    reads and ``reason`` is the option id — so recording one of them and dropping the other
    would put a verdict in the ledger that the buyer did not give. There is no tie-break that
    is not a guess about which half the buyer meant.
    """


class FeedbackAlreadySubmitted(FeedbackError):
    """This order's one piece of feedback has already been recorded.

    R14 is "one structured prompt", and a ledger that accepted a second submission for one
    order would let a buyer push the same store's ``feedback_match`` dimension as far as they
    liked. :attr:`event_id` carries the event the first submission became, so a screen can show
    the buyer what was already recorded instead of an error about a thing that worked.
    """

    def __init__(self, message: str, event_id: str = "") -> None:
        super().__init__(message)
        self.event_id = event_id


class LedgerSinkUnusable(FeedbackError):
    """No ledger sink was supplied, or the one supplied exposes no way to record an event.

    A refusal rather than a shrug. R14's promise is that a submission *lands* on the ledger; a
    submit that silently recorded nothing is indistinguishable to the buyer from one that
    worked, and the store would never be graded on the feedback they gave.
    """


class MalformedFeedbackEvent(FeedbackError):
    """The event this package built does not match ``feedback``'s published payload shape.

    A self-check at the producing boundary, using ``contracts.validate_ledger_payload``. It
    cannot fire on today's code — and that is the point: it fires on the day someone edits the
    payload without reading ``LEDGER_PAYLOAD_SHAPES``, which is exactly how one kind ends up
    with two bodies.

    :attr:`problems` is what the validator reported.
    """

    def __init__(self, message: str, problems: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.problems = problems
