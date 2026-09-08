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
#: ``3.0.0`` (was ``2.0.0``) for D55's four shortlist fields — the published slot now carries
#: the SHOP's own message verbatim (``ShortlistSlot.message``), says whose price it is
#: (``fallback`` / ``fallback_reason``), and names the product in the platform's own crawled
#: words (``ShortlistProduct.identity`` -> ``ShortlistProductIdentity``).
#:
#: **MAJOR by the same rule the ``2.0.0`` note below establishes, and NOT because a reader in
#: this tree breaks.** The rule is that the compatibility deciding a wire bump is the READER's
#: and that every object here is closed, so a field added to any published body is refused by a
#: counterparty pinned to the previous generation. That is a property of the schema, not of who
#: happens to consume it today, so it applies to a response body exactly as it applied to
#: ``BidRequest``.
#:
#: What differs is the ROLLOUT, and it is worth stating because it is the opposite shape of
#: D58's. ``BidRequest`` is written by the exchange and read by third-party store agents, so
#: that bump had to reach the readers first. ``ShortlistSlot`` is written by the exchange and,
#: in this tree, read by nobody through a closed model: ``buyer_svc`` imports no
#: ``contracts.protocol`` object at all and parses the shortlist as plain dicts
#: (``composition.HttpExchangeClient.shortlist_for`` returns ``dict(...)``), and the SPA types
#: it ``unknown`` and forwards it untouched. The one closed reader is the exchange's own
#: ``GET /auctions/{auction_id}/shortlist``, whose ``response_model=Shortlist`` is generated
#: from this schema and ships in the same image. So the exchange moves alone, and an external
#: consumer pinned to ``2.0.0`` is the counterparty this bump is announcing itself to.
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
SCHEMA_VERSION = "3.0.0"

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
