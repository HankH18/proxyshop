"""The pinned protocol objects of DESIGN §Interfaces, re-exported under their contract names.

Every class here is GENERATED from `packages/contracts/schemas/protocol.schema.json` (see
`contracts.codegen`); this module only names them. Keeping the re-export explicit rather than a
star-import is deliberate: the fourteen pinned objects are a contract other tickets read, and a
name that silently disappears from the schema should break here, loudly, at import time.

`contracts.generated` is a tracked symlink to `packages/contracts/generated/python`, so the
committed codegen artifact lives in the directory the ticket scope names while still being an
ordinary, statically-analysable import.
"""

from __future__ import annotations

from contracts.generated.protocol import (
    Bid,
    BidPath,
    BidRequest,
    BidValidationResult,
    BuyerProfile,
    Claim,
    ClaimSourceSpan,
    ClaimType,
    ClaimVerification,
    ClaimVerificationStatus,
    ClusterLoss,
    ConstraintOp,
    Discount,
    Envelope,
    EnvelopeActivation,
    EnvelopeFloor,
    HardConstraint,
    Intent,
    LedgerEvent,
    LedgerEventKind,
    LossReasons,
    LossReport,
    LossWindow,
    NormalizationBounds,
    Offer,
    PenaltyCatalogue,
    Preference,
    ProfileBuckets,
    Provenance,
    ProvenanceSource,
    PseudonymousContext,
    Shortlist,
    ShortlistSlot,
    ShortlistSlotName,
    SignedBidSubmission,
    SigningEnvelope,
    Store,
    StoreTier,
    TrustDimension,
    TrustDimensionState,
    TrustDims,
    TrustEventPayload,
    TrustSnapshot,
    VerificationResult,
)
from contracts.generated.protocol import (
    RankingWeights as _GeneratedRankingWeights,
)

#: DESIGN §Interfaces pins exactly these fourteen as the cross-service protocol objects. Other
#: tickets read this tuple to assert they are looking at the whole surface, not a subset.
PINNED_PROTOCOL_OBJECTS: tuple[str, ...] = (
    "Intent",
    "BuyerProfile",
    "BidRequest",
    "Claim",
    "Provenance",
    "Offer",
    "Bid",
    "VerificationResult",
    "Shortlist",
    "LossReport",
    "LedgerEvent",
    "TrustSnapshot",
    "TrustEventPayload",
    "Envelope",
)

#: The wire version every protocol object is currently emitted at.
SCHEMA_VERSION = "1.0.0"

__all__ = [
    "PINNED_PROTOCOL_OBJECTS",
    "SCHEMA_VERSION",
    "Bid",
    "BidPath",
    "BidRequest",
    "BidValidationResult",
    "BuyerProfile",
    "Claim",
    "ClaimSourceSpan",
    "ClaimType",
    "ClaimVerification",
    "ClaimVerificationStatus",
    "ClusterLoss",
    "ConstraintOp",
    "Discount",
    "Envelope",
    "EnvelopeActivation",
    "EnvelopeFloor",
    "HardConstraint",
    "Intent",
    "LedgerEvent",
    "LedgerEventKind",
    "LossReasons",
    "LossReport",
    "LossWindow",
    "NormalizationBounds",
    "Offer",
    "PenaltyCatalogue",
    "Preference",
    "ProfileBuckets",
    "Provenance",
    "ProvenanceSource",
    "PseudonymousContext",
    "Shortlist",
    "ShortlistSlot",
    "ShortlistSlotName",
    "SignedBidSubmission",
    "SigningEnvelope",
    "Store",
    "StoreTier",
    "TrustDimension",
    "TrustDimensionState",
    "TrustDims",
    "TrustEventPayload",
    "TrustSnapshot",
    "VerificationResult",
    "_GeneratedRankingWeights",
]
