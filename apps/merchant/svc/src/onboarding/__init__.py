"""Merchant onboarding: a plain-language interview becomes an approved, versioned envelope.

R6/R7/R9. The public surface is four functions and the vocabulary they refuse with::

    envelope = envelope_from_transcript(transcript)   # version 1, always shadow
    digest    = approval_digest(envelope)             # what the merchant signs
    live      = activate(envelope, approval)          # refuses without a bound artifact
    v2        = edit(envelope, {"max_discount_pct": 15})   # new version, prior untouched

C3/S7 — ``.importlinter`` forbids ``apps/exchange`` from importing this package. The envelope
never crosses into the exchange.
"""

from __future__ import annotations

from merchant_svc.envelope.model import (
    ApprovalArtifact,
    ApprovalRejected,
    Envelope,
    EnvelopeEditRefused,
    EnvelopeError,
    EnvelopeInvalid,
)
from merchant_svc.onboarding.flow import (
    activate,
    approval_artifact_template,
    approval_digest,
    edit,
    envelope_from_transcript,
    kill,
    store_id_from_transcript,
)
from merchant_svc.onboarding.interview import (
    QUESTIONS,
    REQUIRED_QUESTIONS,
    AnswerNotUnderstood,
    Interview,
    TranscriptRejected,
    read_transcript,
)

__all__ = [
    "QUESTIONS",
    "REQUIRED_QUESTIONS",
    "AnswerNotUnderstood",
    "ApprovalArtifact",
    "ApprovalRejected",
    "Envelope",
    "EnvelopeEditRefused",
    "EnvelopeError",
    "EnvelopeInvalid",
    "Interview",
    "TranscriptRejected",
    "activate",
    "approval_artifact_template",
    "approval_digest",
    "edit",
    "envelope_from_transcript",
    "kill",
    "read_transcript",
    "store_id_from_transcript",
]
