"""Sealed envelope state: the value object, its version algebra, and its history.

C3/S7 — nothing in ``apps/exchange`` may import this package, and ``.importlinter`` enforces
it. The merchant service and the store-agent runtime are the only readers.

The onboarding interview (:mod:`merchant_svc.onboarding`) is what *produces* an envelope;
this package is what an envelope *is*.
"""

from __future__ import annotations

from merchant_svc.envelope.digest import (
    DIGEST_ALGORITHM,
    approval_covers,
    approval_digest,
    approved_terms,
    canonical_text,
)
from merchant_svc.envelope.frozen import FrozenDict, freeze, thaw
from merchant_svc.envelope.model import (
    ACTIVE,
    EDITABLE_FIELDS,
    ENVELOPE_FIELDS,
    FIRST_VERSION,
    KEEP,
    KILLED,
    SHADOW,
    TERM_FIELDS,
    ApprovalArtifact,
    ApprovalRejected,
    Envelope,
    EnvelopeEditRefused,
    EnvelopeError,
    EnvelopeInvalid,
    as_document,
)
from merchant_svc.envelope.store import (
    ENVELOPES,
    EnvelopeVersions,
    StoreMismatch,
    UnknownStore,
    VersionWentBackwards,
)
from merchant_svc.envelope.versions import activate_envelope, edit_envelope, kill_envelope

__all__ = [
    "ACTIVE",
    "DIGEST_ALGORITHM",
    "EDITABLE_FIELDS",
    "ENVELOPES",
    "ENVELOPE_FIELDS",
    "FIRST_VERSION",
    "KEEP",
    "KILLED",
    "SHADOW",
    "TERM_FIELDS",
    "ApprovalArtifact",
    "ApprovalRejected",
    "Envelope",
    "EnvelopeEditRefused",
    "EnvelopeError",
    "EnvelopeInvalid",
    "EnvelopeVersions",
    "FrozenDict",
    "StoreMismatch",
    "UnknownStore",
    "VersionWentBackwards",
    "activate_envelope",
    "approval_covers",
    "approval_digest",
    "approved_terms",
    "as_document",
    "canonical_text",
    "edit_envelope",
    "freeze",
    "kill_envelope",
    "thaw",
]
