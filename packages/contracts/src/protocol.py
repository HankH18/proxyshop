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
    DenialCode,
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
#:
#: ``2.0.0`` (was ``1.0.0``) for D58's ``BidRequest.product_ref`` — the exchange now names the
#: product it is soliciting a bid on.
#:
#: **MAJOR, and an earlier draft of this constant called it minor on an argument that was
#: measured false.** That draft said "the field is nullable and unrequired: every
#: ``BidRequest`` valid under ``1.0.0`` is still valid, and a store agent that ignores the
#: field answers exactly as it did before." The first clause is true and irrelevant. The
#: second is wrong, because **the compatibility that decides a wire bump is the READER's, not
#: the writer's**, and both published readers here are closed: every object in
#: ``protocol.schema.json`` is ``additionalProperties: false``, and the generated model is
#: ``ConfigDict(extra="forbid")``. A store agent pinned to the previous generation cannot
#: ignore the field — it refuses the request. Measured, on a real socket, against the verbatim
#: previous ``BidRequest`` model::
#:
#:     body WITH product_ref -> HTTP 422 extra_forbidden ['body', 'product_ref']
#:     body WITHOUT it       -> HTTP 204
#:
#: So a field added to a closed request body is a breaking change for every counterparty, and
#: the rollout order is: **store agents first, exchange second.** Reached the wrong way round
#: it degrades rather than erroring — ``HttpBidSolicitor`` maps any non-200 to a refusal and
#: R10 represents the store at its list price — but it degrades SILENTLY, and what goes quiet
#: is the whole sponsored half of the market (D55). ``HttpBidSolicitor.solicit`` therefore
#: omits the key entirely when the auction names no product, so only the solicitations that
#: actually need it can break a stale agent.
SCHEMA_VERSION = "2.0.0"

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
    "DenialCode",
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
