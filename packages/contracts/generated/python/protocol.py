# GENERATED FILE — DO NOT EDIT.
#
# Source:    packages/contracts/schemas/protocol.schema.json
# Generator: python -m contracts.codegen   (datamodel-code-generator, pydantic v2)
#
# `tests/test_schema_bundle.py` re-runs the generator and fails if this file no longer matches
# the schema, so editing it by hand is a defect the suite catches rather than a shortcut.
# ruff: noqa

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel


class ProxyShopProtocol(RootModel[Any]):
    root: Any = Field(..., title="ProxyShopProtocol")
    """
    The single JSON Schema bundle for every cross-service protocol object in DESIGN §Interfaces.

    This file is the SOURCE OF TRUTH. `packages/contracts/generated/python/protocol.py` and
    `packages/contracts/generated/ts/protocol.schema.d.ts` are both produced from it by
    `python -m contracts.codegen`; a checked-in drift test re-runs both generators and fails if the
    committed output no longer matches. Nothing hand-edits the generated files.

    Every object closes its property set (`additionalProperties: false`). That is what makes R19
    mechanically true: a `weight` key on a hard constraint is a schema error rather than a field that
    rides silently through the exchange, and a seventh trust dimension cannot be smuggled into a
    snapshot (D53).

    Timestamps are `string` + `format: date-time` on the wire and `str` in the generated Python, so
    `model_dump()` is JSON-native and no protocol object needs a coercion pass to be serialized.
    """


class ProvenanceSource(StrEnum):
    """
    DESIGN §Interfaces `Provenance.source`. Closed: an unknown source is a schema error.

    The first six are HOOK provenances — a store-agent can only mint them by calling a tool hook
    (the hook→source table T-040 pins), which is what makes the hosted path checkable. `seller_asserted` is
    the only source an external agent can assert freely, so it is the only non-hook source, and it
    is rejected outright on the hosted path (R8/S5).
    """

    scraped = "scraped"
    pixel_feed = "pixel_feed"
    owner_statement = "owner_statement"
    envelope_rule = "envelope_rule"
    learned_policy = "learned_policy"
    network = "network"
    seller_asserted = "seller_asserted"


class ConstraintOp(StrEnum):
    """
    R19: the comparison a hard constraint filters with. No regex — an eligibility filter has to be decidable against an attribute value, not against a pattern language.
    """

    eq = "eq"
    lte = "lte"
    gte = "gte"
    in_ = "in"
    contains = "contains"


class PreferenceDirection(StrEnum):
    """
    R19: the direction a preference scores in.
    """

    maximize = "maximize"
    minimize = "minimize"
    prefer = "prefer"


class EnvelopeActivation(StrEnum):
    """
    DESIGN §Interfaces `Envelope.activation`.
    """

    shadow = "shadow"
    active = "active"
    killed = "killed"


class ShortlistSlotName(StrEnum):
    """
    D29 shortlist slot names. These are slot LABELS, not rank-formula terms (D50).
    """

    fit = "fit"
    value = "value"
    reliability = "reliability"
    specialist = "specialist"


class ClaimVerificationStatus(StrEnum):
    """
    R18 verification badge. Per D53 neither `unsupported` nor `ambiguous` ever satisfies a hard constraint or counts as verified evidence.
    """

    verified = "verified"
    contradicted = "contradicted"
    unsupported = "unsupported"
    ambiguous = "ambiguous"


class LedgerEventKind(StrEnum):
    """
    C11 / D24. The DESIGN §Interfaces enum, extended EXACTLY ONCE here with
    `auction_opened`, `auction_closed`, `offer_integrity`, `blacklisted`, `blacklist_expired`.
    The redirect path and the Shopify checkout path emit the same kinds; neither invents one.
    """

    bid_placed = "bid_placed"
    shown = "shown"
    accepted = "accepted"
    code_created = "code_created"
    checkout_redirect = "checkout_redirect"
    checkout_pixel = "checkout_pixel"
    order_paid = "order_paid"
    order_fulfilled = "order_fulfilled"
    refund = "refund"
    feedback = "feedback"
    reconciled = "reconciled"
    claim_verified = "claim_verified"
    policy_event = "policy_event"
    auction_opened = "auction_opened"
    auction_closed = "auction_closed"
    offer_integrity = "offer_integrity"
    blacklisted = "blacklisted"
    blacklist_expired = "blacklist_expired"


class TrustDimension(StrEnum):
    """
    D53: SIX dimensions inside ONE trust system. `catalog_claim_accuracy` carries product-fact
    verification outcomes, which have no honest home among the transaction dimensions — a false
    ingredient claim is not a late delivery, and folding it into `feedback_match` would assert that
    a buyer reported a mismatch when no buyer has bought anything yet.
    """

    price_honored = "price_honored"
    discount_honored = "discount_honored"
    shipped_on_time = "shipped_on_time"
    not_returned = "not_returned"
    feedback_match = "feedback_match"
    catalog_claim_accuracy = "catalog_claim_accuracy"


class ClaimType(StrEnum):
    """
    D53's typed, exhaustive claim-type vocabulary. The right-hand side of the claim_type→dimension
    mapping lives in `fixtures/manifest.json` (T-080) and is read from there, never inferred by the
    trust engine — the engine is the thing being graded.
    """

    price = "price"
    unit_price = "unit_price"
    total_price = "total_price"
    discount = "discount"
    promo_eligibility = "promo_eligibility"
    delivery = "delivery"
    shipping_speed = "shipping_speed"
    dispatch_window = "dispatch_window"
    return_policy = "return_policy"
    warranty = "warranty"
    ingredients = "ingredients"
    compatibility = "compatibility"
    nutrition = "nutrition"
    specifications = "specifications"


class StoreTier(IntEnum):
    """
    D28. 0 = catalog-present, no agent, no envelope (always a synthesized list-price bid).
    1 = network-hosted store-agent. 2 = external self-hosted agent behind the signed door.
    Solicitation is tier-aware (R10); ranking is tier-blind (R11).
    """

    integer_0 = 0
    integer_1 = 1
    integer_2 = 2


class BidPath(StrEnum):
    """
    R8/S5: which door a bid arrived through. The dual-path boundary reads exactly these two.
    """

    hosted = "hosted"
    external = "external"


class DenialCode(StrEnum):
    """
    The closed vocabulary a checkout refusal is drawn from: the token before the first colon of the 409 `denial_reason` on `POST /auctions/{auction_id}/accept` (T-204). `exchange.accept.DENIAL_REASONS` is the producer and this enum is the publication of it; `packages/contracts/tests/test_denial_vocabulary.py` fails if the two ever differ.

    **The served field is not one of these values.** It is `<code>` or `<code>: <free diagnostic prose>` — measured live off a booted exchange, `"checkout_refused: RuntimeError: merchant declined to mint"` and `"blacklisted: chargeback fraud"` — so the enumeration is published on the CODE and the 409 keeps a `pattern` over the composite string. Enumerating the bare codes on `denial_reason` itself would be a constraint essentially every real 409 violates.

    Parse the token before the first colon and match it here; everything behind that colon names the auction, the bid, the refused host or the exception class, and is for a human. The exchange writes `": "` itself but forwards a seller-eligibility source's reason verbatim when it already starts with a declared code, and it splits on a BARE colon after stripping blanks — so `blacklisted:no space` and a bare `blacklisted:` are values this field really carries. `contracts/ts/vocabulary.ts::denialCode` is the published parser; a hand-rolled `split(':')[0].trim()` disagrees with it on U+001C-U+001F.

    `unspecified` is the fail-safe that makes this vocabulary closed rather than advisory: a refusal reaching the published boundary with a code the exchange does not declare is re-published under it with its prose intact, so a client never sees a tenth token.
    """

    already_accepted = "already_accepted"
    auction_not_acceptable = "auction_not_acceptable"
    blacklisted = "blacklisted"
    checkout_refused = "checkout_refused"
    unavailable = "unavailable"
    unknown_bid = "unknown_bid"
    unrecordable_acceptance = "unrecordable_acceptance"
    unroutable_fallback = "unroutable_fallback"
    unspecified = "unspecified"


class Provenance(BaseModel):
    """
    DESIGN §Interfaces. Source ≡ Provenance: the Neo4j `Source` node carries the same enum in
    `source_class`.

    `authority_rank` has DEFINED semantics (D30): 1 is the most authoritative and larger is weaker,
    and `contracts.PROVENANCE_AUTHORITY_RANK` publishes the canonical rank per source so two hooks
    cannot pick different numbers for the same kind of evidence. It is validated as `>= 1` rather
    than pinned to that table, because a hook may legitimately down-rank a stale observation.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    source: ProvenanceSource
    ref: str = Field(..., min_length=1)
    """
    A pointer to the evidence: a snapshot URI, an envelope commitment path, a pitch span.
    """
    observed_at: str = Field(..., min_length=1)
    authority_rank: int = Field(..., ge=1)
    """
    1 = most authoritative; larger is weaker. See contracts.PROVENANCE_AUTHORITY_RANK.
    """


class ClaimSourceSpan(BaseModel):
    """
    D25: where in a pitch a claim was extracted from, so `VerificationResult.claim_ref` points at something.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    pitch_ref: str
    start: int = Field(..., ge=0)
    end: int = Field(..., ge=0)


class Claim(BaseModel):
    """
    DESIGN §Interfaces `Claim`, extended once here per D25.

    `claim_id` is a content hash over `(pitch_ref, key, canonicalized value, claim_type)` — stable
    across re-extraction, which is what makes T-065's idempotency criterion satisfiable. It is
    OPTIONAL on the wire because a hook-minted commitment has no pitch to hash against; compute it
    with `contracts.claim_id(...)`.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    claim_id: str | None = None
    key: str = Field(..., min_length=1)
    claim_type: ClaimType | None = None
    value: Any
    unit: str | None = None
    source_span: ClaimSourceSpan | None = None
    provenance: Provenance


class Discount(BaseModel):
    """
    The discount an Offer carries. `provenance` is optional because the reconciliation path (T-061) compares an accepted offer's discount against a webhook's `discountApplications`, which carry no provenance of their own.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: str = Field(..., min_length=1)
    value: float
    provenance: Provenance | None = None


class Offer(BaseModel):
    """
    DESIGN §Interfaces `Offer` — the PROTOCOL offer, i.e. the priced thing inside a Bid.

    D25 names three different objects "Offer": this one, the Neo4j `Offer` node (a store's observed
    listing) and the `app.offers` row. They are distinct. The protocol object's own identifier is
    `bid_offer_id` so a reader never has to guess which one a bare `offer_id` meant.

    `expires_at` is what T-032's expiry filter reads, and what `validate_bid` rejects on.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    bid_offer_id: str | None = None
    product_ref: str = Field(..., min_length=1)
    variant_ref: str | None = None
    """
    Cart permalinks are variant-scoped (D25). Optional on the protocol object because the frozen hosted-bid shape predates it; required by the checkout path before a permalink can be minted.
    """
    unit_price: float
    currency: str | None = None
    discount: Discount | None = None
    commitments: list[Claim] = Field([], validate_default=True)
    total_price: float
    expires_at: str | None = None
    checkout_url: str | None = None
    delivery_estimate_days: float | None = None


class Bid(BaseModel):
    """
    DESIGN §Interfaces `Bid`.

    This object is the SOLICITATION shape — what a Tier-1 hosted agent answers `POST /v1/bid-requests`
    with. It deliberately carries NO signing envelope: a hosted agent never crosses the external
    boundary, so requiring `signer_id`/`key_id`/`issued_at`/`nonce` here would demand a key of an
    agent that has none. The external door's shape is `SignedBidSubmission` (D52).
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    auction_id: str = Field(..., min_length=1)
    store_id: str = Field(..., min_length=1)
    pitch_ref: str | None = None
    offer: Offer
    claims: list[Claim]
    message: str | None = None
    """
    Prose the seller sends in its own voice — the SPONSORED half of D55, and the one part of a bid that carries the seller's own motive. Hosted advocates and external (Tier-2) agents both send it.

    It is neither ignored nor trusted. `claim_verification.pitch.decompose_pitch` decomposes it into atomic `seller_asserted` claims, each carrying a `source_span` back into these bytes, and `claim_verification.verify` grades every one of them against the exchange's own catalog snapshot with the same text-blind comparators any other claim meets (R8, R18). That is why two bids differing by one word rank apart and why a claim the snapshot refutes comes back `contradicted`. The sentence that stood here until now declared this field inert and no source of claims at all; it described the state before that path existed. A `description` constrains nothing, so nothing broke when it went stale — which is exactly why it survived, and why `tests/test_schema_bundle.py` now holds this text to what the running extractor does.

    The grading is what makes the prose safe to read at all. The extractor's own quality check is that a value appears in the document it was read from; run over the seller's own sentence that check degrades to "this store really did say this", which is authorship and not evidence. Comparison against a snapshot the seller did not supply is the missing half, so a claim minted from `message` is an ASSERTION until the verifier decides it, and `unsupported`/`ambiguous` are never read as true (R19). Untrusted text reaches a regular expression and a comparator here — never an instruction and never a model (C10).
    """
    agent_version: str = Field(..., min_length=1)
    signature: str | None = None
    schema_version: str = Field(..., min_length=1)


class SigningEnvelope(BaseModel):
    """
    D52 / DESIGN §Interfaces. ALL FIVE fields are required on every Bid submitted through the
    external door `POST /v1/auctions/{auction_id}/bids`. There is no legacy-tolerant production
    path and no optional-field mode: optionality would leave the public boundary unable to rotate
    keys, check freshness, or protect against replay — the three reasons the door is signed at all.

    `signer_id` is the registered submitting identity (equal to `store_id` for a single-store
    seller, distinct when one seller submits for several). `key_id` selects one of that signer's
    live keys, so the keyring is `{signer_id: {key_id: secret}}` and a lookup keyed on `key_id`
    alone is wrong. `nonce` is the replay/idempotency key, unique PER SIGNER, never globally.

    The five envelope fields are non-blank BY PATTERN, not merely by length. `minLength: 1`
    alone admits `"   "`, which `contracts.signing.missing_signing_fields` strips and calls
    MISSING - two gates on one rule, disagreeing. `canonical_signing_bytes` sits on the strict gate,
    so the asymmetry pointed the safe way, but a whitespace-only `nonce` is a constant nonce and no
    schema should admit one. The class ENUMERATES every blank character instead of spelling `\\s`, and that is the whole
    point of this artifact. `\\s` is engine-dependent: Rust's regex crate (what pydantic compiles)
    and Python's `re` read it as Unicode White_Space, which INCLUDES U+0085; ECMAScript (what Ajv
    compiles) reads it as a fixed list that EXCLUDES U+0085 and includes U+FEFF. The one file both
    languages read therefore stated two different rules, and Python and TypeScript disagreed about
    U+0085 and U+FEFF. The set below is the union of both readings plus U+001C-U+001F, which
    Python's `str.strip()` removes and Unicode does not classify as whitespace - so it is at least
    as strict as either gate was and relaxes neither. `contracts.signing.is_blank` and `isBlank`
    in `src/ts/signing.ts` implement exactly this set; a property test in each suite walks every
    code point in Unicode and fails if the compiled pattern and the function ever disagree.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    signer_id: str = Field(
        ...,
        min_length=1,
        pattern="[^\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]",
    )
    key_id: str = Field(
        ...,
        min_length=1,
        pattern="[^\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]",
    )
    issued_at: str = Field(
        ...,
        min_length=1,
        pattern="[^\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]",
    )
    nonce: str = Field(
        ...,
        min_length=1,
        pattern="[^\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]",
    )
    schema_version: str = Field(
        ...,
        min_length=1,
        pattern="[^\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]",
    )


class SignedBidSubmission(BaseModel):
    """
    The wire shape of `POST /v1/auctions/{auction_id}/bids`: a `Bid` with the `SigningEnvelope`
    flattened onto it. A submission missing ANY of the five envelope fields is Rejected at the
    exchange boundary with the same finality as a bad signature, before extraction and before
    verification (D52).

    The property set is asserted to be exactly `Bid` ∪ `SigningEnvelope` by a checked-in test, so
    this definition cannot drift away from the two it composes.

    The five envelope fields are non-blank BY PATTERN, not merely by length. `minLength: 1`
    alone admits `"   "`, which `contracts.signing.missing_signing_fields` strips and calls
    MISSING - two gates on one rule, disagreeing. `canonical_signing_bytes` sits on the strict gate,
    so the asymmetry pointed the safe way, but a whitespace-only `nonce` is a constant nonce and no
    schema should admit one. The class ENUMERATES every blank character instead of spelling `\\s`, and that is the whole
    point of this artifact. `\\s` is engine-dependent: Rust's regex crate (what pydantic compiles)
    and Python's `re` read it as Unicode White_Space, which INCLUDES U+0085; ECMAScript (what Ajv
    compiles) reads it as a fixed list that EXCLUDES U+0085 and includes U+FEFF. The one file both
    languages read therefore stated two different rules, and Python and TypeScript disagreed about
    U+0085 and U+FEFF. The set below is the union of both readings plus U+001C-U+001F, which
    Python's `str.strip()` removes and Unicode does not classify as whitespace - so it is at least
    as strict as either gate was and relaxes neither. `contracts.signing.is_blank` and `isBlank`
    in `src/ts/signing.ts` implement exactly this set; a property test in each suite walks every
    code point in Unicode and fails if the compiled pattern and the function ever disagree.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    auction_id: str = Field(..., min_length=1)
    store_id: str = Field(..., min_length=1)
    pitch_ref: str | None = None
    offer: Offer
    claims: list[Claim]
    message: str | None = None
    agent_version: str = Field(..., min_length=1)
    signature: str | None = None
    schema_version: str = Field(
        ...,
        min_length=1,
        pattern="[^\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]",
    )
    signer_id: str = Field(
        ...,
        min_length=1,
        pattern="[^\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]",
    )
    key_id: str = Field(
        ...,
        min_length=1,
        pattern="[^\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]",
    )
    issued_at: str = Field(
        ...,
        min_length=1,
        pattern="[^\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]",
    )
    nonce: str = Field(
        ...,
        min_length=1,
        pattern="[^\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]",
    )


class HardConstraint(BaseModel):
    """
    R19: a hard constraint is an eligibility FILTER — `field`/`op`/`value`, never weighted. The
    closed property set is the enforcement: a `weight` key is a schema error, not a field the
    ranker silently ignores while the buyer believes it was scored.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    field: str = Field(..., min_length=1)
    op: ConstraintOp
    value: Any
    unit: str | None = None


class Preference(BaseModel):
    """
    R19: a preference is a SCORE term — `field`/`direction`/`weight`.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    field: str = Field(..., min_length=1)
    direction: PreferenceDirection
    weight: float


class Intent(BaseModel):
    """
    DESIGN §Interfaces `Intent` (partner schema adopted, extended).
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    intent_id: str = Field(..., min_length=1)
    cluster_id: str | None = None
    query: str
    category: str | None = None
    hard_constraints: list[HardConstraint]
    preferences: list[Preference]
    ship_to: str | None = None
    currency: str | None = None
    budget_band: str | None = None
    created_at: str = Field(..., min_length=1)
    schema_version: str = Field(..., min_length=1)


class ProfileBuckets(BaseModel):
    """
    Coarse buckets only. No identity fields ever appear here (R13).
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    budget_band: str | None = None
    category_affinity: list[str] = []
    frequency_tier: str | None = None
    region: str | None = None
    first_time: bool | None = None


class BuyerProfile(BaseModel):
    """
    DESIGN §Interfaces `BuyerProfile` — no identity fields; the pseudonym rotates per session.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    pseudonym: str = Field(..., min_length=1)
    buckets: ProfileBuckets


class BidRequest(BaseModel):
    """
    DESIGN §Interfaces `BidRequest` — the SOLICITATION body of `POST /v1/bid-requests`.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    auction_id: str = Field(..., min_length=1)
    intent: Intent
    profile: BuyerProfile
    respond_by: str = Field(..., min_length=1)


class ClaimVerification(BaseModel):
    """
    One decided claim inside a VerificationResult.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    claim_ref: str = Field(..., min_length=1)
    status: ClaimVerificationStatus
    observed_value: Any | None = None
    evidence_refs: list[str]
    confidence: float = Field(..., ge=0.0, le=1.0)


class VerificationResult(BaseModel):
    """
    DESIGN §Interfaces `VerificationResult`. Records the catalog snapshot and the verifier version at decision time, so a verdict is reproducible against what was actually seen.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    pitch_ref: str = Field(..., min_length=1)
    catalog_snapshot: str = Field(..., min_length=1)
    claims: list[ClaimVerification]
    verified_claim_ratio: float = Field(..., ge=0.0, le=1.0)
    verifier_version: str = Field(..., min_length=1)


class ShortlistProduct(BaseModel):
    """
    R2's PRODUCT on a shortlist slot: WHICH catalogue thing this slot is offering.

    A reference, never a rendered name. The exchange holds `product_ref` (and, where the bid named
    one, `variant_ref`) because that is what the roster and the offer agree on; a title, an image or
    a description belongs to the catalogue the buyer app already resolves refs against, and copying
    one through the exchange would publish a second, staler spelling of a fact the exchange does not
    own.

    `variant_ref` is optional for the same reason it is optional on `Offer`: cart permalinks are
    variant-scoped (D25), but the frozen hosted-bid shape predates the field and a fallback offer
    minted from a roster row names no variant at all. Absent means "the bid did not name one", never
    "the default variant".
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    product_ref: str = Field(..., min_length=1)
    variant_ref: str | None = None


class ShortlistPrice(BaseModel):
    """
    R2's PRICE on a shortlist slot: what this store is asking, and until when.

    BOTH prices are required together. A slot showing a unit price with no total (or the reverse)
    invites the buyer to compare two different quantities as if they were the same offer, so the
    producer publishes this object only when it can read both as finite numbers and nulls the whole
    price otherwise — a slot with no readable price says so with `price: null`, rather than by
    carrying a zero, which is the cheapest number there is and would win every comparison a shopper
    makes (the T-277 shape, one surface over).

    `expires_at` is the instant this quote stops being live, and it is ISO-8601 UTC and nothing else.
    `Offer.expires_at` is nominally `format: date-time` while half this tree writes a float epoch into
    it, and T-182 is the measurement of what that ambiguity costs; a NEW buyer-facing surface does not
    inherit it. The exchange normalizes through the same reader its own expiry filter uses
    (`checkout.codes.expiry_epoch`) and renders one spelling, so a client never has to guess which it
    was handed.

    `discount` is the published `Discount` — a *stated* depth, not an entitlement. No single-use code
    exists until the buyer accepts (D22), so this is what the store says it will honour, which is
    exactly what the `discount_honored` trust dimension later grades it against.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    unit_price: float
    total_price: float
    currency: str | None = None
    discount: Discount | None = None
    expires_at: str | None = None


class ShortlistSlot(BaseModel):
    """
    One shortlist slot. `provenance_labels` is an OPEN list of strings, not a closed enum: the
    exchange supplies the buyer-facing labels and the buyer app renders what it was given (D30).

    R2 asks one slot to show five things — PRODUCT, PRICE, COMMITMENTS, a store trust indicator and
    provenance labels — and for a long time this object declared only the last two, so the other three
    were dropped at the schema rather than anywhere in a service. `product`, `price` and `commitments`
    close that. All three are OPTIONAL and all three admit `null`, deliberately on both counts:
    `additionalProperties: false` means every existing producer of a `Shortlist` would have had to be
    changed on the same commit if they were required, and an R10 list-price fallback genuinely has no
    priced offer to read when its roster row names no price the exchange could charge.

    NULL is the honest answer for "the exchange has nothing here", and it is never a zero, an empty
    product or a `[]` that could be read as "this store committed to nothing". Absent and `null` say
    the same thing, and a producer serving through the pinned model necessarily says it as `null` —
    the exchange's own two doors are exactly that case, one typing its body `Shortlist` and the other
    `dict`, and they are asserted to serve byte-identical objects.

    All three are the EXCHANGE's reading of the bid, not the bid's own bytes. Nothing here is copied
    verbatim from a store: a value the exchange cannot read as the shape declared below is nulled
    rather than published, because the alternative is a buyer-facing route that a malformed bid can
    turn into a 500.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    slot: ShortlistSlotName
    bid_ref: str = Field(..., min_length=1)
    fit_score: float
    trust_summary: dict[str, Any]
    provenance_labels: list[str]
    product: ShortlistProduct | None = None
    price: ShortlistPrice | None = None
    commitments: list[Claim] | None = None
    """
    What this store is promising alongside the price — free returns, a shipping window, a warranty. Every entry is a published `Claim` with real provenance, which is what makes it gradeable later; a commitment the exchange cannot read as one is dropped rather than shown, because the buyer is being told this is a promise somebody can be held to. `null` is spelled out rather than left to `--strict-nullable` to widen (T-195): an optional non-nullable array generates as `list[Claim] | None` in Python regardless, and a schema that did not also say `null` would have pydantic accepting a payload ajv refuses.
    """


class Shortlist(BaseModel):
    """
    DESIGN §Interfaces `Shortlist`.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    auction_id: str = Field(..., min_length=1)
    slots: list[ShortlistSlot]


class LossWindow(BaseModel):
    """
    The aggregation window. Epoch seconds or an RFC-3339 instant; the producer picks one and the reader is told which by the type.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    start: float | str
    end: float | str


class LossReasons(BaseModel):
    """
    Why bids in this cluster lost, by count. Closed: a new reason is a schema change, not a new dict key that the merchant UI silently drops.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    fit: int = Field(0, ge=0)
    price: int = Field(0, ge=0)
    commitments: int = Field(0, ge=0)
    trust: int = Field(0, ge=0)


class ClusterLoss(BaseModel):
    """
    One cluster's losses inside a LossReport: how many bids were lost, why, and which buyer criteria this store failed to meet. Counts and criteria only — naming the rival that won would turn a seller feedback loop into a price-intelligence feed.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    cluster_id: str = Field(..., min_length=1)
    lost: int = Field(..., ge=0)
    reasons: LossReasons
    unmet_criteria: list[str]


class LossReport(BaseModel):
    """
    DESIGN §Interfaces `LossReport` — aggregated and delayed. No amounts, no rival identities: the closed property set is what keeps a competitor's price out of a merchant's dashboard.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    store_id: str = Field(..., min_length=1)
    window: LossWindow
    by_cluster: list[ClusterLoss]


class LedgerEvent(BaseModel):
    """
    DESIGN §Interfaces `LedgerEvent` — append-only, hash-chained (`prev_hash`).

    `payload` is an OPEN mapping on purpose. Every kind carries a different body — a reconciliation
    compares `discountApplications` from a webhook against a protocol-shaped `discount`, a feedback
    event carries `matched_pitch` — and a closed union here would reject bodies the ledger must
    record verbatim. The per-kind shapes are published as `contracts.LEDGER_PAYLOAD_SHAPES` and
    checked with `contracts.validate_ledger_payload()` at the producing boundary, which is where a
    malformed body can still be refused without making the ledger itself lossy.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    event_id: str = Field(..., min_length=1)
    ts: str = Field(..., min_length=1)
    kind: LedgerEventKind
    auction_id: str | None = None
    store_id: str | None = None
    order_ref: str | None = None
    prev_hash: str | None = None
    payload: dict[str, Any]


class TrustDimensionState(BaseModel):
    """
    One Beta inside the single trust framework, plus the instant its decay was last evaluated at (D17: recomputation re-evaluates each observation at its RECORDED `decayed_at`).
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    alpha: float = Field(..., ge=0.0)
    beta: float = Field(..., ge=0.0)
    decayed_at: str = Field(..., min_length=1)


class TrustDims(BaseModel):
    """
    D53: EXACTLY the six dimensions, all required, nothing else admitted. Omitting
    `catalog_claim_accuracy` is invalid and so is naming a seventh dimension — both are schema
    errors rather than a silently partial snapshot that scores differently from the one that was
    served.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    price_honored: TrustDimensionState
    discount_honored: TrustDimensionState
    shipped_on_time: TrustDimensionState
    not_returned: TrustDimensionState
    feedback_match: TrustDimensionState
    catalog_claim_accuracy: TrustDimensionState


class TrustSnapshot(BaseModel):
    """
    DESIGN §Interfaces `TrustSnapshot` — six dimensions inside ONE trust system (D53).

    `computed_through_event` is the last `LedgerEvent.event_id` folded in, which is what makes the
    S3 replay assertion checkable against a SERVED snapshot rather than against the trust engine's
    own opinion of where it had got to.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    store_id: str = Field(..., min_length=1)
    score: float
    confidence: float | None = None
    effective_sample_size: float | None = None
    score_version: str | None = None
    snapshot_version: str | None = None
    computed_through_event: str | None = None
    low_data: bool | None = None
    dims: TrustDims
    blacklisted: bool


class PseudonymousContext(BaseModel):
    """
    R13: a trust event may carry the cluster and the rotating pseudonym, and nothing that identifies a person.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    cluster_id: str | None = None
    pseudonym: str | None = None


class TrustEventPayload(BaseModel):
    """
    DESIGN §Interfaces `TrustEventPayload` (R13).
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    store_id: str = Field(..., min_length=1)
    event: LedgerEvent
    dim: TrustDimension
    delta: float
    pseudonymous_context: PseudonymousContext


class EnvelopeFloor(BaseModel):
    """
    A price the store-agent may never bid below. `product_ref` is optional: an entry without one is the store-wide floor, which is what makes a single rule expressible without enumerating the catalogue.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    product_ref: str | None = None
    min_price: float = Field(..., ge=0.0)


class Envelope(BaseModel):
    """
    DESIGN §Interfaces `Envelope`. It NEVER crosses into the exchange (C3/S7): it is sealed state,
    and `.importlinter` forbids `exchange` from importing the modules that hold it. It is defined
    here because the store-agent runtime and the merchant service must agree on its shape, not
    because the exchange may read one.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    store_id: str = Field(..., min_length=1)
    version: int = Field(..., ge=0)
    floors: list[EnvelopeFloor]
    max_discount_pct: float = Field(..., ge=0.0, le=100.0)
    budget_cap: float = Field(..., ge=0.0)
    pursue_clusters: list[str]
    standing_commitments: list[Claim]
    activation: EnvelopeActivation


class Store(BaseModel):
    """
    D28: the tier vocabulary is pinned here so solicitation (tier-aware, R10) and ranking (tier-blind, R11) read the same three values.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    store_id: str = Field(..., min_length=1)
    domain: str | None = None
    business_identity: str | None = None
    tier: StoreTier


class NormalizationBounds(BaseModel):
    """
    D12/D13: every feature the rank formula reads is normalized into [feature_min, feature_max].
    `delivery_fit` and `verified_claim_ratio` are 0.5 (NEUTRAL) when absent, never 0 — a missing
    value is not evidence of a bad one, and scoring it as 0 would silently punish every store the
    verifier has not reached yet.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    feature_min: float
    feature_max: float
    delivery_fit_when_absent: float = Field(..., ge=0.0, le=1.0)
    verified_claim_ratio_when_absent: float = Field(..., ge=0.0, le=1.0)


class PenaltyCatalogue(BaseModel):
    """
    D13: `policy_penalties = Σ per-kind penalties over open `ledger.policy_events` for that store in
    the scoring window`. The catalogue is published here so the ranker cannot invent a penalty, and
    `max_total_penalty` bounds the sum — without a bound one policy event kind repeated enough
    times drives `rank_score` arbitrarily negative and the formula stops being a comparison.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    per_kind: dict[str, float]
    """
    policy-event kind -> penalty. Values are non-negative; the bound is enforced by RankingWeights in both languages rather than by `minimum` here, because a per-value constraint makes the generated Python a wrapper model instead of a plain float map.
    """
    max_total_penalty: float = Field(..., ge=0.0)


class RankingWeights(BaseModel):
    """
    D13 + D50. The ONE published rank formula is DESIGN's five-term one:

        rank_score = w_m*intent_match + w_e*verified_claim_ratio + w_t*trust
                   + w_v*price_value + w_d*delivery_fit − policy_penalties

    The three-term `w_f*fit + w_v*offer_value + w_t*trust` found elsewhere in DESIGN is superseded
    prose (D12): it predates the reconciliation amendment and lacks the `verified_claim_ratio` term.

    The five weights MUST sum to 1.0. That rule cannot be written in JSON Schema, so it is enforced
    in both generated languages by `RankingWeights` / `rankingWeightsErrors` and is asserted by
    tests in both. `version` makes a weight change a visible, comparable event rather than a silent
    re-scoring of history.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    version: str = Field(..., min_length=1)
    w_m: float = Field(..., ge=0.0, le=1.0)
    """
    intent_match
    """
    w_e: float = Field(..., ge=0.0, le=1.0)
    """
    verified_claim_ratio
    """
    w_t: float = Field(..., ge=0.0, le=1.0)
    """
    trust
    """
    w_v: float = Field(..., ge=0.0, le=1.0)
    """
    price_value
    """
    w_d: float = Field(..., ge=0.0, le=1.0)
    """
    delivery_fit
    """
    normalization: NormalizationBounds
    penalties: PenaltyCatalogue
    tie_breakers: list[str]
    """
    D13's tie-break order, applied left to right.
    """


class BidValidationResult(BaseModel):
    """
    What the dual-path boundary answers with. `reasons` is non-empty whenever `ok` is false — a
    refusal that will not say why is indistinguishable from a bug, and the exchange has to log a
    reason code back to the seller.
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    ok: bool
    path: str
    reasons: list[str]
    requires_verification: bool
    unverified_claim_indexes: list[int]
