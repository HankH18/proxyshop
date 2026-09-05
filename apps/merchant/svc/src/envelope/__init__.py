"""Sealed envelope state: the value object, its version algebra, and its history.

C3/S7 — nothing in ``apps/exchange`` may import this package, and ``.importlinter`` enforces
it. The merchant service and the store-agent runtime are the only readers.

The onboarding interview (:mod:`merchant_svc.onboarding`) is what *produces* an envelope;
this package is what an envelope *is*.
"""

from __future__ import annotations

from ._spellings import bind_package
from .digest import (
    DIGEST_ALGORITHM,
    approval_covers,
    approval_digest,
    approved_terms,
    canonical_text,
)
from .frozen import FrozenDict, freeze, thaw
from .model import (
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
from .repository import (
    ENVELOPES_TABLE,
    EnvelopeRepository,
    InMemoryEnvelopeRepository,
    PostgresEnvelopeRepository,
)
from .store import (
    ENVELOPES,
    EnvelopeVersions,
    StoreMismatch,
    UnknownStore,
    VersionWentBackwards,
)
from .versions import activate_envelope, edit_envelope, kill_envelope

__all__ = [
    "ACTIVE",
    "DIGEST_ALGORITHM",
    "EDITABLE_FIELDS",
    "ENVELOPES",
    "ENVELOPES_TABLE",
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
    "EnvelopeRepository",
    "EnvelopeVersions",
    "FrozenDict",
    "InMemoryEnvelopeRepository",
    "PostgresEnvelopeRepository",
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

# LAST, and it is not decoration: this tree is importable as `merchant_svc.envelope` and as
# `apps.merchant.svc.src.envelope`, and without this Python executes every file here TWICE —
# once per spelling — leaving TWO `ENVELOPES` histories, so "this store has been killed" would
# hold only per spelling, and `except UnknownStore` imported through one spelling would not
# catch the class the other raises. A pytest session running the frozen E5 acceptance suite
# (`apps.merchant.svc.src.envelope`) beside the FastAPI app (`merchant_svc.envelope`) is
# exactly such a process. Measured on this worktree before this existed:
# `apps.merchant.svc.src.envelope.store is merchant_svc.envelope.store` was False, and so was
# `... .ENVELOPES is ... .ENVELOPES`. See `_spellings.py`; T-052 does the same for `codes/`.
bind_package(__name__)
