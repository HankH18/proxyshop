"""``buyer_svc.feedback`` — R14's post-purchase prompt and its ledger event (T-073).

    >>> from apps.buyer.svc.src.feedback import feedback_prompt, submit_feedback
    >>> feedback_prompt({"order_ref": "ord-1", "store_id": "st-1", "routed": False}) is None
    True
    >>> prompt = feedback_prompt(order)                    # ONE question, no free-text field
    >>> event = submit_feedback(order, {"choice": "yes_as_described"}, ledger_sink)
    >>> event.kind, event.payload
    (<LedgerEventKind.feedback: 'feedback'>, {'matched_pitch': True, 'reason': 'yes_as_described'})

SPEC R14: "Post-purchase feedback: only network-routed buyers, one structured prompt ('did it
match the pitch?'), weighted by buyer track record, cross-checked against return behavior." The
weighting and the cross-check belong to ``trust.feedback.engine`` (T-063). What lives here is the
first clause and the last word of the middle one — *who is asked*, and *what shape the answer
takes on the way to the ledger* — and both are promises about **authority**:

* **Only network-routed buyers.** :mod:`buyer_svc.feedback.routing` decides, and its refusals are
  deliberately conservative: an order whose ``routed`` flag is not an unambiguous boolean, or
  which names no auction, is treated as un-routed. Without that gate a store can move its own
  ``feedback_match`` dimension for the price of a few fake reviews, and the dimension stops
  meaning anything.
* **One structured prompt.** :mod:`buyer_svc.feedback.prompt` publishes exactly one question over
  a closed set of five options, with no free-text field anywhere on it — and
  :mod:`buyer_svc.feedback.submission` re-checks that at the write boundary, because the prompt
  having no text box does not stop a client posting a sentence in ``choice``.
* **It lands as a ledger event.** Not a row in a table this service owns. The body is the one
  ``contracts.LEDGER_PAYLOAD_SHAPES["feedback"]`` publishes — ``("matched_pitch", "reason")`` —
  and ``contracts.validate_ledger_payload`` is called on every event before it leaves. See
  :mod:`buyer_svc.feedback.submission` for the live defect elsewhere in this repo that says why
  validating at the producing boundary is not ceremony.

What the neighbouring code consumes
-----------------------------------
``feedback_prompt(order)`` / ``submit_feedback(order, response, sink)``
    the two functions the frozen E7 suite calls, 1 and 3 positional arguments respectively.
``FeedbackPrompt.to_dict()``
    ``{order_ref, store_id, auction_id, question_id, question, input_type, options[]}``, where
    each option is ``{id, label, matched_pitch}``. ``apps/buyer/app/feedback`` renders this
    verbatim and holds no copy of the option table, for the same reason T-072's shortlist view
    holds no copy of the provenance labels (D30): two copies drift.
``submitted()`` / ``reset_submitted()``
    the process-local record of which orders have already been answered, and the way a test
    empties it.
``FeedbackError`` and its subclasses
    every refusal here is one of them, and none of them is a ``TypeError``/``ValueError``/
    ``KeyError``, so a domain refusal is distinguishable from a broken call — the same convention
    ``buyer_svc.intent`` (``IntentError``) and ``buyer_svc.accept`` (``AcceptError``) follow.

Imports inside this package are relative on purpose; see :mod:`buyer_svc.feedback._spellings` for
what goes wrong otherwise (this tree is reachable both as ``buyer_svc.feedback`` and as
``apps.buyer.svc.src.feedback``, and an absolute import silently picks one).
"""

from __future__ import annotations

from ._reading import first, flag, read, text
from ._spellings import bind_package
from .errors import (
    ContradictoryFeedback,
    FeedbackAlreadySubmitted,
    FeedbackError,
    FeedbackNotYours,
    LedgerSinkUnusable,
    MalformedFeedbackEvent,
    MissingFeedbackChoice,
    OrderNotRouted,
    UnknownFeedbackChoice,
    UnknownFeedbackQuestion,
    UnusableOrder,
)
from .prompt import (
    CHOICE_IDS,
    CHOICES_BY_ID,
    FEEDBACK_CHOICES,
    PROMPT_INPUT_TYPE,
    PROMPT_QUESTION,
    PROMPT_QUESTION_ID,
    FeedbackChoice,
    FeedbackPrompt,
    feedback_prompt,
    prompt_for,
)
from .routing import (
    ORDER_REF_FIELDS,
    ROUTED_FIELDS,
    STATUS_FIELDS,
    UNDELIVERED_STATUSES,
    Routing,
    is_routed,
    routing,
)
from .submission import (
    FEEDBACK_KIND,
    LEDGER_SINK_METHODS,
    RESPONSE_CHOICE_FIELDS,
    RESPONSE_QUESTION_FIELDS,
    FeedbackLedger,
    event_view,
    feedback_payload,
    reset_submitted,
    submit_feedback,
    submitted,
)

__all__ = [
    "CHOICES_BY_ID",
    "CHOICE_IDS",
    "FEEDBACK_CHOICES",
    "FEEDBACK_KIND",
    "LEDGER_SINK_METHODS",
    "ORDER_REF_FIELDS",
    "PROMPT_INPUT_TYPE",
    "PROMPT_QUESTION",
    "PROMPT_QUESTION_ID",
    "RESPONSE_CHOICE_FIELDS",
    "RESPONSE_QUESTION_FIELDS",
    "ROUTED_FIELDS",
    "STATUS_FIELDS",
    "UNDELIVERED_STATUSES",
    "ContradictoryFeedback",
    "FeedbackAlreadySubmitted",
    "FeedbackChoice",
    "FeedbackError",
    "FeedbackLedger",
    "FeedbackNotYours",
    "FeedbackPrompt",
    "LedgerSinkUnusable",
    "MalformedFeedbackEvent",
    "MissingFeedbackChoice",
    "OrderNotRouted",
    "Routing",
    "UnknownFeedbackChoice",
    "UnknownFeedbackQuestion",
    "UnusableOrder",
    "event_view",
    "feedback_payload",
    "feedback_prompt",
    "first",
    "flag",
    "is_routed",
    "prompt_for",
    "read",
    "reset_submitted",
    "routing",
    "submit_feedback",
    "submitted",
    "text",
]

# LAST, and it is not decoration: this tree is importable as `buyer_svc.feedback` and as
# `apps.buyer.svc.src.feedback`, and without this Python executes every file here TWICE — once
# per spelling — leaving two `FeedbackAlreadySubmitted` classes that do not catch each other and
# TWO submission ledgers, so "one routed order, one feedback event" would hold only per spelling.
# Measured on this worktree before this line existed:
# `apps.buyer.svc.src.feedback is buyer_svc.feedback` was False. See `_spellings.py`.
bind_package(__name__)
