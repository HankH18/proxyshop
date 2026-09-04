"""``buyer_svc.intent`` — R1's clarification loop (T-071).

    >>> from apps.buyer.svc.src.intent import clarify, confirm
    >>> outcome = clarify(["something for espresso, cheap"], llm)
    >>> outcome.questions                 # at most THREE, and often none
    ('What is the most you would want to spend?',)
    >>> outcome.intent.budget_band        # never empty; 'unspecified' when nobody said
    '0-50'
    >>> outcome.confirmed                 # always False. Clarifying is not confirming.
    False
    >>> created = confirm(outcome.intent, exchange, confirmed=True)   # ONE auction

SPEC R1 in three sentences, and where each one lives:

* *at most three clarifying questions* — :mod:`buyer_svc.intent.clarifier`. The cap is
  counted, is clamped so no caller can raise it, and is re-checked by the outcome object.
* *a structured intent (use case, constraints, budget band)* —
  :mod:`buyer_svc.intent.models`, where R19's filter/score split is structural: a hard
  constraint has no ``weight`` attribute and a preference has no ``op``.
* *no auction exists before the buyer confirms* — :mod:`buyer_svc.intent.confirmation`.
  ``clarify`` takes no auction client at all, so the early path cannot reach the side
  effect even in principle; ``confirm`` checks ``confirmed is True`` before it so much as
  looks the client up.

What the neighbouring tickets consume
-------------------------------------
``T-072`` (``buyer_svc.accept``) and ``T-073`` (``buyer_svc.feedback``) read this surface
and never reach inside it:

``AuctionCreated.auction_id``
    the auction a shortlist and an accept belong to.
``AuctionCreated.intent`` / ``Intent.to_dict()``
    the ``contracts.Intent``-shaped payload, safe to send over the wire; optional fields
    that were never set are omitted rather than emitted as nulls.
``ClarifyOutcome``
    subscriptable as well as attribute-addressed (``outcome["questions"]`` ==
    ``outcome.questions``), so a route handler and a test can read it the same way.

Imports inside this package are relative on purpose; see :mod:`buyer_svc.vault` for why
(this tree is reachable both as ``buyer_svc.intent`` and as
``apps.buyer.svc.src.intent``, and an absolute import silently picks one).
"""

from __future__ import annotations

from ._spellings import bind_package
from .clarifier import CANNED_QUESTIONS, INTENT_CONTRACT, clarify
from .confirmation import (
    AUCTION_CLIENT_METHODS,
    ConfirmationLedger,
    confirm,
    confirmations,
    reset_confirmations,
)
from .errors import (
    AuctionClientUnusable,
    ConfirmationWithheld,
    EmptyDialogue,
    IntentAlreadyConfirmed,
    IntentError,
    InvalidConstraint,
    InvalidPreference,
    UnstructuredIntent,
)
from .extraction import (
    GAP_BUDGET,
    GAP_CONSTRAINTS,
    GAP_ORDER,
    GAP_USE_CASE,
    BudgetReading,
    Extraction,
    IntentDraft,
    LLMProposal,
    band_for_amount,
    extract,
    parse_llm_reply,
)
from .models import (
    BUDGET_BAND_UNSPECIFIED,
    BUDGET_BAND_VOCABULARY,
    CONSTRAINT_OPS,
    DEFAULT_CURRENCY,
    INTENT_SCHEMA_VERSION,
    MAX_CLARIFYING_QUESTIONS,
    PREFERENCE_DIRECTIONS,
    AuctionCreated,
    ClarifyOutcome,
    HardConstraint,
    Intent,
    Preference,
    QuestionCapBroken,
    coerce_intent,
    intent_from_payload,
)

__all__ = [
    "AUCTION_CLIENT_METHODS",
    "BUDGET_BAND_UNSPECIFIED",
    "BUDGET_BAND_VOCABULARY",
    "CANNED_QUESTIONS",
    "CONSTRAINT_OPS",
    "DEFAULT_CURRENCY",
    "GAP_BUDGET",
    "GAP_CONSTRAINTS",
    "GAP_ORDER",
    "GAP_USE_CASE",
    "INTENT_CONTRACT",
    "INTENT_SCHEMA_VERSION",
    "MAX_CLARIFYING_QUESTIONS",
    "PREFERENCE_DIRECTIONS",
    "AuctionClientUnusable",
    "AuctionCreated",
    "BudgetReading",
    "ClarifyOutcome",
    "ConfirmationLedger",
    "ConfirmationWithheld",
    "EmptyDialogue",
    "Extraction",
    "HardConstraint",
    "Intent",
    "IntentAlreadyConfirmed",
    "IntentDraft",
    "IntentError",
    "InvalidConstraint",
    "InvalidPreference",
    "LLMProposal",
    "Preference",
    "QuestionCapBroken",
    "UnstructuredIntent",
    "band_for_amount",
    "clarify",
    "coerce_intent",
    "confirm",
    "confirmations",
    "extract",
    "intent_from_payload",
    "parse_llm_reply",
    "reset_confirmations",
]

# LAST, and it is not decoration: this tree is importable as `buyer_svc.intent` and as
# `apps.buyer.svc.src.intent`, and without this Python executes every file here TWICE —
# once per spelling — leaving two `ConfirmationWithheld` classes that do not catch each
# other and TWO confirmation ledgers, so "one confirmation opens one auction" would hold
# only per spelling. Measured on this worktree: `apps.buyer.svc.src.intent is
# buyer_svc.intent` was False. See `_spellings.py`.
bind_package(__name__)
