"""R14: one structured prompt, offered only for network-routed orders (T-073).

    >>> from apps.buyer.svc.src.feedback import feedback_prompt
    >>> feedback_prompt({"order_ref": "ord-1", "routed": False}) is None
    True
    >>> feedback_prompt({"order_ref": "ord-2", "auction_id": "auc-3", "routed": True})["question"]
    'Did what arrived match what the store pitched?'

Everything about the shape below is R14 read literally.

**One prompt, not a survey.** ``feedback_prompt`` returns a single :class:`FeedbackPrompt` — one
question, with options — and never a list of them. Post-purchase attention is the scarcest thing
the network gets from a buyer, and a five-question form answered by nobody produces a
``feedback_match`` dimension built on the handful of buyers with time to fill in a form.

**No free-text field, anywhere.** Not on the prompt, not on an option, not as an optional
"anything else?" box. Three reasons, and the third is the one that makes it structural rather
than tasteful:

1. R5. A free-text box is the one place in the buyer surface where a buyer types their own name,
   their address, or the store's reply to their email — and this text is destined for a ledger
   event that is pushed to the store the feedback is about. There is no scrubber that beats not
   having the field.
2. Trust reads a *verdict*, not prose. ``trust.feedback.engine`` scores ``feedback_match`` off
   ``matched_pitch``; free text would have to be classified back into that boolean by something,
   and that something would be an LLM guessing at what the buyer meant.
3. The published payload for a ``feedback`` ledger event is ``("matched_pitch", "reason")``
   (``contracts.LEDGER_PAYLOAD_SHAPES``), where ``reason`` is one of a closed set of option ids.
   A free-text answer has nowhere to go that is not a lie about the shape.

The prompt is deliberately a **pure function of the order record**. It consults no submission
ledger, so ``feedback_prompt`` for an order that has already been answered still returns the
prompt. That is not an oversight — see :func:`feedback_prompt` for the two reasons — and the
once-only guarantee lives at the write boundary, in
:func:`~buyer_svc.feedback.submission.submit_feedback`, which is the only place it can be
enforced anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .routing import Routing, routing

__all__ = [
    "CHOICES_BY_ID",
    "CHOICE_IDS",
    "FEEDBACK_CHOICES",
    "PROMPT_INPUT_TYPE",
    "PROMPT_QUESTION",
    "PROMPT_QUESTION_ID",
    "FeedbackChoice",
    "FeedbackPrompt",
    "feedback_prompt",
    "prompt_for",
]

#: The prompt's id, and — not by coincidence — the published payload key a ``feedback`` ledger
#: event carries (``contracts.LEDGER_PAYLOAD_SHAPES["feedback"] == ("matched_pitch", "reason")``).
#: One question, one key: there is no mapping table between what was asked and what was recorded,
#: so the two cannot drift.
PROMPT_QUESTION_ID = "matched_pitch"

#: SPEC R14 names the question itself — "did it match the pitch?". This is that, in the second
#: person and in the past tense, because it is asked after delivery.
PROMPT_QUESTION = "Did what arrived match what the store pitched?"

#: How the answer is collected. Named on the prompt so a client cannot decide for itself that a
#: text box would be friendlier: a renderer reading ``input_type`` finds a closed vocabulary here
#: and nothing that admits typing.
PROMPT_INPUT_TYPE = "single_select"


@dataclass(frozen=True, slots=True)
class FeedbackChoice:
    """One option on the prompt: what the buyer picks, and what it means to trust.

    ``matched_pitch`` is the verdict this option carries into the ledger event, so the mapping
    from "what the buyer clicked" to "what the store is graded on" is written down here, once,
    beside the words the buyer actually read. A choice whose label and verdict disagree is a
    visible bug in this table rather than an invisible one in a scoring function.
    """

    id: str
    label: str
    matched_pitch: bool

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "matched_pitch": self.matched_pitch}


#: The closed set of answers. Five, and every one of them is a fact the buyer can check by
#: looking at what is in front of them — none asks them to rate anything out of five.
#:
#: ``as_described_but_late`` is a positive ``matched_pitch``: what arrived DID match the pitch,
#: and lateness is a different dimension (``shipped_on_time``), graded from the fulfilment
#: timestamps rather than from an opinion. Folding it into ``matched_pitch`` would charge a
#: catalogue-accuracy penalty for a courier's bad week — which is precisely the mistake DESIGN
#: §120 records for ``catalog_claim_accuracy``, in the other direction.
FEEDBACK_CHOICES: tuple[FeedbackChoice, ...] = (
    FeedbackChoice("yes_as_described", "Yes — it was what the store described", True),
    FeedbackChoice("as_described_but_late", "Yes, but it arrived later than promised", True),
    FeedbackChoice("not_as_described", "No — it was not what the store described", False),
    FeedbackChoice("wrong_item", "No — a different item arrived", False),
    FeedbackChoice("never_arrived", "It never arrived", False),
)

#: The option ids, in prompt order. The closed vocabulary a submitted ``choice`` is checked
#: against — see :class:`~buyer_svc.feedback.errors.UnknownFeedbackChoice`.
CHOICE_IDS: tuple[str, ...] = tuple(choice.id for choice in FEEDBACK_CHOICES)

#: The same table, keyed. Read-only so a caller cannot register a sixth answer at runtime.
CHOICES_BY_ID = MappingProxyType({choice.id: choice for choice in FEEDBACK_CHOICES})


@dataclass(frozen=True, slots=True)
class FeedbackPrompt:
    """The one prompt an order is offered. Serializes to a single mapping, never a collection."""

    order_ref: str
    store_id: str
    auction_id: str
    question_id: str
    question: str
    input_type: str
    options: tuple[FeedbackChoice, ...]

    def to_dict(self) -> dict[str, Any]:
        """The published projection. Plain data throughout, and free of any text field."""
        return {
            "order_ref": self.order_ref,
            "store_id": self.store_id,
            "auction_id": self.auction_id,
            "question_id": self.question_id,
            "question": self.question,
            "input_type": self.input_type,
            "options": [option.to_dict() for option in self.options],
        }

    def __getitem__(self, key: str) -> Any:
        """Subscriptable as well as attribute-addressed, like T-072's ``AcceptedOffer``."""
        try:
            return self.to_dict()[key]
        except KeyError:
            raise KeyError(key) from None


def prompt_for(routed: Routing) -> FeedbackPrompt:
    """Build the prompt for an already-decided :class:`Routing`. No gate of its own."""
    return FeedbackPrompt(
        order_ref=routed.order_ref,
        store_id=routed.store_id,
        auction_id=routed.auction_id,
        question_id=PROMPT_QUESTION_ID,
        question=PROMPT_QUESTION,
        input_type=PROMPT_INPUT_TYPE,
        options=FEEDBACK_CHOICES,
    )


def feedback_prompt(order: Any) -> FeedbackPrompt | None:
    """The one structured prompt for ``order``, or ``None`` if R14 offers it none.

    Args:
        order: the order record — ``{order_ref, store_id, auction_id, routed}`` at minimum, as
            the exchange sends it. See :func:`~buyer_svc.feedback.routing.routing` for exactly
            which readings of ``routed`` are honoured and which are refused as guesses.

    Returns:
        A :class:`FeedbackPrompt`, or ``None`` when no prompt is offered — which is the case for
        an order the network did not route, an order that names no auction, and an order
        cancelled before it ever arrived. ``None`` rather than an exception: "there is no prompt
        for this order" is an ordinary answer to an ordinary question, and a screen that renders
        an order list would otherwise have to wrap every row in a ``try``.

    Raises:
        UnusableOrder: ``order`` is not an order record at all.

    Why this does not consult the submission ledger. It would make the function's answer depend
    on process history, and both consequences are bad:

    * **It would be order-dependent under test.** The frozen acceptance suite asks for the prompt
      for ``ord-e7-101`` in one test and submits feedback for the same order in another. A prompt
      that went quiet after a submission would pass or fail depending on which test ran first —
      a property of pytest's collection order, not of R14.
    * **It would be wrong in production anyway.** The submission ledger is process-local (see
      :class:`~buyer_svc.feedback.submission.FeedbackLedger`), so behind two workers it answers
      "already submitted" for half of the buyers who submitted and "not yet" for the rest.
      Whether this buyer has already answered is a fact about the *order*, which the caller can
      put on the record it passes in; it is not a fact this function can know.
    """
    routed = routing(order)
    if not routed.eligible:
        return None
    return prompt_for(routed)
