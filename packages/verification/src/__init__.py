"""Claim verification: one pitch, one catalog snapshot, four honest statuses.

Owned by T-065 (scope ``packages/verification/**``). The flat-src namespace is
``claim_verification`` (via the tracked ``.pkgroot/claim_verification`` symlink — see this
member's ``pyproject.toml``); ``packages/verification/__init__.py`` forwards the repo-root
spelling ``packages.verification`` onto this same module, so the two paths yield the same
objects rather than two sets of classes.

=====================================  ==============================================
:func:`decompose_pitch`                a seller's prose -> atomic ``seller_asserted`` claims.
:func:`verify`                         a pitch -> a typed, evidenced VerificationResult.
:func:`satisfies_hard_constraint`      ``True`` for ``verified``, and nothing else.
:data:`FIELD_TOLERANCES`               published per-field relative tolerances.
:func:`compare`                        one claimed value vs one catalog attribute.
:data:`VERIFICATION_STATUSES`          the four, and exactly four.
:func:`verification_key`               the ``(pitch, snapshot, version)`` idempotency key.
=====================================  ==============================================

The two halves are deliberately separate objects. :func:`decompose_pitch` ASSERTS — it turns
prose into claims and never decides anything — and :func:`verify` DECIDES, against a catalog
snapshot the seller did not supply. Splitting them is what keeps the second one pure and
text-blind while the first one exists at all (R18, D55).

Three properties hold across the whole package and are worth stating once:

* **Deterministic and pure.** No clock, no randomness, no I/O, no model call, and neither
  argument is mutated. That is what makes verification idempotent per ``(pitch, verifier
  version, catalog snapshot)`` and what makes a stored verdict re-checkable.
* **Text-blind (C10).** :func:`verify` never reads ``pitch["text"]``. An injected
  instruction in it is data in a field the decision does not consult, so it cannot move a
  verdict. Prose does now MINT claims — :func:`decompose_pitch`, R18/D55 — and a minted claim
  is then graded by these same text-blind comparators against a catalog the seller did not
  supply, so untrusted text reaches a regular expression and a comparison, never an
  instruction and never a model.
* **Four statuses, one privilege.** Only ``verified`` may satisfy a hard constraint.
  ``unsupported`` and ``ambiguous`` reading as true is the failure R18/R19 name by name.

``claim_verification.verify`` is the CLAIM verifier. ``trust.ledger.verify_chain`` is the
LEDGER verifier. Different concepts, deliberately not merged.
"""

from __future__ import annotations

from .comparators import FIELD_TOLERANCES, ComparisonOutcome, compare, tolerance_for
from .normalize import (
    UNIT_FAMILIES,
    attribute_value,
    normalize_boolean,
    normalize_text,
    parse_quantity,
    to_base_unit,
    unit_family,
)
from .pitch import (
    KEY_ALIASES,
    MAX_PITCH_CHARS,
    MAX_PITCH_CLAIMS,
    PITCH_CONFIDENCE_FLOOR,
    PITCH_EXTRACTOR_VERSION,
    PITCH_RULES,
    SELLER_ASSERTED,
    decompose_pitch,
    pitch_ref_for,
)
from .statuses import (
    DECIDED_STATUSES,
    UNDECIDED_STATUSES,
    VERIFICATION_STATUSES,
    InvalidVerificationStatus,
    is_decided,
    require_status,
    satisfies_hard_constraint,
)
from .verifier import (
    KEY_CLAIM_TYPES,
    STATUS_CONFIDENCE,
    VerificationInputError,
    verification_key,
    verify,
)

__all__ = [
    "DECIDED_STATUSES",
    "FIELD_TOLERANCES",
    "KEY_ALIASES",
    "KEY_CLAIM_TYPES",
    "MAX_PITCH_CHARS",
    "MAX_PITCH_CLAIMS",
    "PITCH_CONFIDENCE_FLOOR",
    "PITCH_EXTRACTOR_VERSION",
    "PITCH_RULES",
    "SELLER_ASSERTED",
    "STATUS_CONFIDENCE",
    "UNDECIDED_STATUSES",
    "UNIT_FAMILIES",
    "VERIFICATION_STATUSES",
    "ComparisonOutcome",
    "InvalidVerificationStatus",
    "VerificationInputError",
    "attribute_value",
    "compare",
    "decompose_pitch",
    "is_decided",
    "normalize_boolean",
    "normalize_text",
    "parse_quantity",
    "pitch_ref_for",
    "require_status",
    "satisfies_hard_constraint",
    "to_base_unit",
    "tolerance_for",
    "unit_family",
    "verification_key",
    "verify",
]


def __dir__() -> list[str]:
    return sorted(__all__)
