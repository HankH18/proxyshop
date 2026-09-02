"""ProxyShop protocol contracts — the schemas every service agrees on, and the boundaries they buy.

`packages/contracts/schemas/protocol.schema.json` is the single source of truth. The typed views
of it in both languages are generated from that file and committed; a drift test regenerates and
diffs, so the types cannot quietly stop describing the protocol.

The public surface, by topic:

**Protocol objects** — the fourteen pinned objects of DESIGN §Interfaces (`Intent`,
`BuyerProfile`, `BidRequest`, `Claim`, `Provenance`, `Offer`, `Bid`, `VerificationResult`,
`Shortlist`, `LossReport`, `LedgerEvent`, `TrustSnapshot`, `TrustEventPayload`, `Envelope`) plus
their component types and closed enums.

**The dual-path bid boundary** — `validate_bid(bid, path=…, trust_snapshot=…)`. Hosted bids reject
on any non-hook provenance; external bids admit `seller_asserted` claims flagged for verification;
expired offers, blacklisted stores and schema-invalid bids reject on every path. `validate_bid`
judges that table and nothing else: it does NOT check the signing envelope, because `Bid` does not
carry one. The wire door is `validate_external_submission`, which adds D52's envelope to the same
verdict.

**The external signing envelope** — `SigningEnvelope`, `SignedBidSubmission`,
`canonical_signing_bytes`, `payload_hash`. Five required fields on every submission through
`POST /v1/auctions/{auction_id}/bids`; one canonicalizer that both signer and verifier call.

**Shared policy contracts** — `RankingWeights` (the one versioned weight set the one published
rank formula reads), `PROVENANCE_BUYER_LABELS` (R2's two label strings),
`PROVENANCE_AUTHORITY_RANK`, `LedgerEventKind` and its join keys, `claim_id`, `StoreTier`.

Import from the package root::

    from contracts import Bid, Intent, LedgerEventKind, validate_bid

Both `contracts` (the flat-src namespace, D42) and `packages.contracts` (the repo-root dotted
path) resolve to this module.
"""

from __future__ import annotations

from contracts.boundary import (
    BID_PATHS,
    EXTERNAL_PATH,
    HOOK_PROVENANCE_SOURCES,
    HOSTED_PATH,
    NON_HOOK_PROVENANCE_SOURCES,
    REASON_SIGNATURE_MISSING,
    REASON_SIGNING_ENVELOPE_INCOMPLETE,
    parse_timestamp,
    validate_bid,
    validate_external_submission,
)
from contracts.claims import claim_id, claim_id_for
from contracts.labels import (
    BUYER_PROVENANCE_LABELS,
    LABEL_FROM_THEIR_WEBSITE,
    LABEL_STORE_CONFIRMED,
    LABEL_UNVERIFIED,
    PROVENANCE_AUTHORITY_RANK,
    PROVENANCE_BUYER_LABELS,
    buyer_label,
    canonical_authority_rank,
)
from contracts.ledger import (
    JOINABLE_KINDS,
    LEDGER_EVENT_KINDS,
    LEDGER_JOIN_KEYS,
    LEDGER_PAYLOAD_SHAPES,
    join_key_view,
    validate_ledger_payload,
)
from contracts.protocol import (
    PINNED_PROTOCOL_OBJECTS,
    SCHEMA_VERSION,
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
from contracts.ranking import (
    DEFAULT_RANKING_WEIGHTS,
    RANK_FEATURES,
    RANKING_WEIGHTS_VERSION,
    WEIGHT_FIELDS,
    RankingWeights,
)
from contracts.registry import (
    PROTOCOL_SCHEMA_PATH,
    is_valid,
    protocol_schema,
    schema_for,
    schema_names,
    validation_errors,
)
from contracts.signing import (
    REQUIRED_SIGNING_FIELDS,
    SIGNED_FIELDS,
    CanonicalisationError,
    canonical_json,
    canonical_signing_bytes,
    envelope_of,
    keyring_secret,
    missing_signing_fields,
    payload_hash,
)

#: `LedgerEventKindEnum` is an accepted alias for the kind vocabulary.
LedgerEventKindEnum = LedgerEventKind

#: D53: the six trust dimensions, as plain strings.
TRUST_DIMENSIONS: frozenset[str] = frozenset(member.value for member in TrustDimension)

__all__ = [
    # protocol objects — the fourteen pinned ones first, then their components
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
    "BidPath",
    "BidValidationResult",
    "ClaimSourceSpan",
    "ClaimType",
    "ClaimVerification",
    "ClaimVerificationStatus",
    "ClusterLoss",
    "ConstraintOp",
    "Discount",
    "EnvelopeActivation",
    "EnvelopeFloor",
    "HardConstraint",
    "LedgerEventKind",
    "LedgerEventKindEnum",
    "LossReasons",
    "LossWindow",
    "NormalizationBounds",
    "PenaltyCatalogue",
    "Preference",
    "ProfileBuckets",
    "ProvenanceSource",
    "PseudonymousContext",
    "ShortlistSlot",
    "ShortlistSlotName",
    "SignedBidSubmission",
    "SigningEnvelope",
    "Store",
    "StoreTier",
    "TrustDimension",
    "TrustDimensionState",
    "TrustDims",
    # the dual-path boundary
    "BID_PATHS",
    "EXTERNAL_PATH",
    "HOOK_PROVENANCE_SOURCES",
    "HOSTED_PATH",
    "NON_HOOK_PROVENANCE_SOURCES",
    "REASON_SIGNATURE_MISSING",
    "REASON_SIGNING_ENVELOPE_INCOMPLETE",
    "parse_timestamp",
    "validate_bid",
    "validate_external_submission",
    # signing
    "REQUIRED_SIGNING_FIELDS",
    "SIGNED_FIELDS",
    "CanonicalisationError",
    "canonical_json",
    "canonical_signing_bytes",
    "envelope_of",
    "keyring_secret",
    "missing_signing_fields",
    "payload_hash",
    # ranking policy
    "DEFAULT_RANKING_WEIGHTS",
    "RANKING_WEIGHTS_VERSION",
    "RANK_FEATURES",
    "RankingWeights",
    "WEIGHT_FIELDS",
    # provenance labelling
    "BUYER_PROVENANCE_LABELS",
    "LABEL_FROM_THEIR_WEBSITE",
    "LABEL_STORE_CONFIRMED",
    "LABEL_UNVERIFIED",
    "PROVENANCE_AUTHORITY_RANK",
    "PROVENANCE_BUYER_LABELS",
    "buyer_label",
    "canonical_authority_rank",
    # ledger vocabulary
    "JOINABLE_KINDS",
    "LEDGER_EVENT_KINDS",
    "LEDGER_JOIN_KEYS",
    "LEDGER_PAYLOAD_SHAPES",
    "join_key_view",
    "validate_ledger_payload",
    # claim identity
    "claim_id",
    "claim_id_for",
    # schemas
    "PINNED_PROTOCOL_OBJECTS",
    "PROTOCOL_SCHEMA_PATH",
    "SCHEMA_VERSION",
    "TRUST_DIMENSIONS",
    "is_valid",
    "protocol_schema",
    "schema_for",
    "schema_names",
    "validation_errors",
]
