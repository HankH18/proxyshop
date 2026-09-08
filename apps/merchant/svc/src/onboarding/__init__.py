"""Merchant onboarding: a plain-language interview becomes an approved, versioned envelope.

R6/R7/R9. The public surface is six functions and the vocabulary they refuse with::

    envelope = envelope_from_transcript(transcript)   # version 1, always shadow
    digest    = approval_digest(envelope)             # what the merchant signs
    live      = activate(envelope, approval)          # refuses without a bound artifact
    v2        = edit(envelope, {"max_discount_pct": 15})   # new version, prior untouched
    stopped   = kill(live)                            # instant, takes no artifact
    restarted = revive(stopped)                       # back to shadow; approve again to bid

The last two are a pair and are deliberately asymmetric. Stopping is one call with no
paperwork, because a control that can be refused is not a kill switch. Starting again is two
deliberate acts — ``revive`` un-stops the store, ``activate`` puts it back on the network — so
nothing a merchant does by accident, and no edit they save, resumes a store on its own.

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
    revive,
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
from merchant_svc.onboarding.script import (
    INTENT_CLUSTERS_ENV,
    STANDING_COMMITMENTS,
    cluster_options,
    interview_script,
    shop_domain_for,
)

__all__ = [
    "INTENT_CLUSTERS_ENV",
    "QUESTIONS",
    "REQUIRED_QUESTIONS",
    "STANDING_COMMITMENTS",
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
    "cluster_options",
    "edit",
    "envelope_from_transcript",
    "interview_script",
    "kill",
    "read_transcript",
    "revive",
    "shop_domain_for",
    "store_id_from_transcript",
]
