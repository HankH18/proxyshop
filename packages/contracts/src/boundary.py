"""The dual-path bid boundary (R8 / R18 / S5).

Two doors lead into the auction and they admit different things:

* **hosted** — a Tier-1 agent running on our own runtime. Every claim it can make had to come out
  of a tool hook (`get_product_fact`, `get_owner_commitments`, `authorize_discount`, …), and each
  hook stamps its own provenance source. So a hosted bid carrying a `seller_asserted` claim is
  not a policy question: it is evidence that something bypassed the hooks, and it is rejected.
* **external** — a Tier-2 self-hosted agent behind the signed door. It CAN assert claims freely,
  so `seller_asserted` is admitted — and flagged, so the claim reaches `packages/verification`
  before it is ever shown to a buyer as fact (R18). Admitted is not the same as trusted.

**A claim is judged by where it CAME FROM, never by which field it was written in.** The Offer is
inside the bid boundary and carries claim material of its own — `offer.commitments` is a list of
`Claim`, and `offer.discount` is stamped with the same `provenance` block a claim is — so all
three sites are walked. A boundary that inspected only `bid.claims` did not have an exclusivity
property; it had a naming convention, and moving the claim one field over defeated it outright.
The external path's admit-and-flag applies at `bid.claims` alone, because that is the only site
`unverified_claim_indexes` can address; a `seller_asserted` source in the offer is refused with
`unverifiable_claim_site` rather than admitted with nobody assigned to check it.

Four conditions reject on BOTH paths, because none of them is about who is speaking:

* a claim with no `provenance` key at all, or with an empty `source`;
* an offer whose `expires_at` has passed — or is missing, which fails closed;
* a store the trust snapshot marks blacklisted, or has no row for at all (R12: an unavailable
  eligibility read denies exactly like a positive one);
* a bid that is not schema-valid.

`validate_bid` never raises on bad input. A boundary that threw would make "reject" and "crash"
indistinguishable to the caller and would give the exchange nothing to log back to the seller;
every refusal comes back as `ok=False` with at least one machine-readable reason.

**The signing envelope is a FIFTH condition, and it applies to the external path only.** D52:
a submission missing any of `signer_id`, `key_id`, `issued_at`, `nonce`, `schema_version` — or
its `signature` — is rejected at the exchange boundary, before extraction and before verification,
with the same finality as a bad signature. `validate_bid` validates the bid against `Bid`, which
by design carries no envelope, so it cannot see those fields and does not check them by default:
its verdict is the R8/R18/S5 table and nothing more. Use `validate_external_submission` — or
`validate_bid(..., require_signing_envelope=True)` — for the real Tier-2 door. Calling plain
`validate_bid(bid, path="external", ...)` on a wire submission admits an UNSIGNED bid.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from contracts.protocol import Bid, BidValidationResult, ProvenanceSource, SignedBidSubmission
from contracts.signing import missing_signing_fields

#: R8: the six provenance sources a store-agent can only mint by calling a tool hook. T-040 pins
#: the hook→source table (`get_product_fact`→`scraped`, `get_live_state`→`pixel_feed`,
#: `get_owner_commitments`→`owner_statement`, `authorize_discount`→`envelope_rule`,
#: `choose_policy_action`→`learned_policy`, `get_network_prior`→`network`).
HOOK_PROVENANCE_SOURCES: frozenset[str] = frozenset(
    {
        ProvenanceSource.scraped.value,
        ProvenanceSource.pixel_feed.value,
        ProvenanceSource.owner_statement.value,
        ProvenanceSource.envelope_rule.value,
        ProvenanceSource.learned_policy.value,
        ProvenanceSource.network.value,
    }
)

#: The only source no hook produces — an assertion the seller made in free text.
NON_HOOK_PROVENANCE_SOURCES: frozenset[str] = frozenset({ProvenanceSource.seller_asserted.value})

#: The claim-bearing sites inside a `Bid` that are NOT `bid.claims`, spelled the way the reason
#: strings name them. `Claim` and `Discount` are the only objects in the protocol schema carrying
#: a `provenance` block, and the only ones reachable from a `Bid` are `bid.claims[i]`,
#: `bid.offer.commitments[i]` and `bid.offer.discount` — `Bid` forbids extra keys, so that list is
#: closed. Add a site to the schema and it must be added here, or R8 stops covering it.
OFFER_COMMITMENTS_SITE = "offer.commitments"
OFFER_DISCOUNT_SITE = "offer.discount"

#: The two doors. `validate_bid` refuses anything else rather than guessing.
HOSTED_PATH = "hosted"
EXTERNAL_PATH = "external"
BID_PATHS: frozenset[str] = frozenset({HOSTED_PATH, EXTERNAL_PATH})

# Reason codes. Stable strings: the exchange logs them and the seller-facing rejection quotes
# them, so renaming one is a contract change, not a refactor.
REASON_UNKNOWN_PATH = "unknown_path"
REASON_SCHEMA_INVALID = "schema_invalid"
REASON_CLAIM_WITHOUT_PROVENANCE = "claim_without_provenance"
REASON_CLAIM_PROVENANCE_EMPTY_SOURCE = "claim_provenance_empty_source"
REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE = "claim_provenance_unknown_source"
REASON_HOSTED_NON_HOOK_PROVENANCE = "hosted_non_hook_provenance"
#: An assertable source at a claim-bearing site the verification queue has no address for.
#: `unverified_claim_indexes` names positions in `bid.claims`; a `seller_asserted` claim that
#: lives in `offer.commitments` or on `offer.discount` cannot be pointed at through it, so the
#: external path refuses it here instead of admitting something nobody will ever verify.
REASON_UNVERIFIABLE_CLAIM_SITE = "unverifiable_claim_site"
REASON_OFFER_EXPIRED = "offer_expired"
REASON_OFFER_EXPIRY_MISSING = "offer_expiry_missing"
REASON_OFFER_EXPIRY_UNPARSEABLE = "offer_expiry_unparseable"
REASON_STORE_BLACKLISTED = "store_blacklisted"
REASON_TRUST_SNAPSHOT_UNAVAILABLE = "trust_snapshot_unavailable"
REASON_SIGNING_ENVELOPE_INCOMPLETE = "signing_envelope_incomplete"
REASON_SIGNATURE_MISSING = "signature_missing"


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read `key` off a mapping or an object, without caring which it is."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_plain(bid: Any) -> Mapping[str, Any]:
    for attr in ("model_dump", "dict"):
        fn = getattr(bid, attr, None)
        if callable(fn):
            dumped = fn()
            if isinstance(dumped, Mapping):
                return dumped
    if isinstance(bid, Mapping):
        return bid
    return {}


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an RFC-3339 instant, a `Z`-suffixed instant, or epoch seconds. `None` if unreadable.

    Naive values are read as UTC. A boundary that treated a missing timezone as local time would
    make expiry depend on where the process happens to run, which is not a property a contract
    can have.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _source_verdict(
    holder: Any, path: str, label: str, *, addressable: bool
) -> tuple[list[str], bool]:
    """Judge ONE provenance-bearing object. Returns (reasons, needs_verification).

    `holder` is anything carrying a `provenance` block — a `Claim` from `bid.claims` or from
    `offer.commitments`, or the `Discount` on the offer. They are judged by the same table
    because they carry the same evidence: R8 is a rule about where a statement came from, and
    it does not become a different rule because the statement was written in a different field.

    `label` is how the reason names the site. It is the bare integer index for `bid.claims`,
    because that spelling is published — `hosted_non_hook_provenance:1:seller_asserted` is
    asserted verbatim by both language suites and read by the exchange's rejection log. Every
    other site gets a dotted/bracketed path (`offer.commitments[0]`, `offer.discount`), the
    same breadcrumb shape `store-agent/hooks/provenance.py` already prints for these fields.

    `addressable` is R18's admit-and-flag, and it is available at `bid.claims` ONLY. See
    `_offer_claim_reasons` for why.
    """
    provenance = _get(holder, "provenance")
    if provenance is None:
        return [f"{REASON_CLAIM_WITHOUT_PROVENANCE}:{label}"], False

    raw_source = _get(provenance, "source")
    source = str(getattr(raw_source, "value", raw_source) or "").strip()
    if not source:
        return [f"{REASON_CLAIM_PROVENANCE_EMPTY_SOURCE}:{label}"], False

    if source in HOOK_PROVENANCE_SOURCES:
        return [], False
    if source in NON_HOOK_PROVENANCE_SOURCES:
        if path == HOSTED_PATH:
            # R8/S5. A hosted agent cannot mint this source through any hook, so its presence
            # means the statement did not come from one.
            return [f"{REASON_HOSTED_NON_HOOK_PROVENANCE}:{label}:{source}"], False
        if addressable:
            # R18. Admitted, but it goes to verification before it is shown as fact.
            return [], True
        return [f"{REASON_UNVERIFIABLE_CLAIM_SITE}:{label}:{source}"], False

    return [f"{REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE}:{label}:{source}"], False


def _claim_provenance_reasons(
    claims: Any, path: str, *, site: str | None = None
) -> tuple[list[str], list[int]]:
    """Per-claim provenance verdicts. Returns (reasons, indexes of claims needing verification).

    `site` is `None` for `bid.claims` — the published, addressable list — and the dotted path of
    the containing field for any other list of claims. Only the `bid.claims` walk can return
    indexes, because `unverified_claim_indexes` means positions in `bid.claims` and nothing else.
    """
    reasons: list[str] = []
    unverified: list[int] = []

    if claims is None:
        return reasons, unverified
    if isinstance(claims, (str, bytes)) or not isinstance(claims, Iterable):
        return [REASON_SCHEMA_INVALID], unverified

    for index, claim in enumerate(claims):
        label = str(index) if site is None else f"{site}[{index}]"
        claim_reasons, needs_verification = _source_verdict(
            claim, path, label, addressable=site is None
        )
        reasons.extend(claim_reasons)
        if needs_verification:
            unverified.append(index)

    return reasons, unverified


def _offer_claim_reasons(offer: Any, path: str) -> list[str]:
    """R8 at the OTHER claim-bearing sites: `offer.commitments` and `offer.discount`.

    **The offer is inside the bid boundary.** `_claim_provenance_reasons` used to be handed
    `bid.claims` and nothing else, so the whole exclusivity property was defeated by MOVING the
    payload: the identical `seller_asserted` claim, relocated into `offer.commitments`, turned a
    rejection into `ok=True, reasons=[]` — with an unauthorised 25% discount riding along on the
    same offer. Same claim, same source, same bid; only the field moved. These are the only two
    other sites: `Claim` and `Discount` are the sole objects in the protocol schema that carry a
    `provenance` block, and `Bid` forbids extra keys, so nothing else reachable from a bid can.

    A non-hook source here is REFUSED on both paths, not flagged, and that asymmetry with
    `bid.claims` is deliberate. R18 does not mean "admitted"; it means "admitted *and routed to
    verification*", and the only handle the boundary hands the verification queue is
    `unverified_claim_indexes`, which is a published `array<integer>` addressing `bid.claims`.
    Renumbering it to cover offer sites would silently redirect the queue at the wrong claims,
    and admitting a claim the queue has no address for would create a worse hole than the one
    this walk closes — an assertion nobody is ever asked to verify. So an external agent may
    still assert freely; it must do so in `bid.claims`, which is the channel with an address,
    and `unverifiable_claim_site` says exactly that. When `packages/verification` exists and a
    path-qualified channel is designed for it, this refusal can become a flag; until then the
    fail-closed direction is the honest one.
    """
    if offer is None:
        return []

    reasons, _ = _claim_provenance_reasons(
        _get(offer, "commitments"), path, site=OFFER_COMMITMENTS_SITE
    )

    # The discount is judged only when there IS one — most offers carry none, and `discount` is
    # nullable by schema. But a discount that is PRESENT and carries no provenance is refused
    # like any other unprovenanced statement: it is the field that actually moves money, and
    # leaving "no provenance at all" unjudged would reopen this very exploit one step further
    # down — drop the block instead of relabelling it, and the 25% discount walks again.
    discount = _get(offer, "discount")
    if discount is not None:
        discount_reasons, _ = _source_verdict(
            discount, path, OFFER_DISCOUNT_SITE, addressable=False
        )
        reasons.extend(discount_reasons)

    return reasons


def _expiry_reasons(offer: Any, now: datetime) -> list[str]:
    if offer is None:
        return [REASON_OFFER_EXPIRY_MISSING]
    raw = _get(offer, "expires_at")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        # Fail closed. An offer with no stated expiry is an offer nobody can price the risk of.
        return [REASON_OFFER_EXPIRY_MISSING]
    expires_at = parse_timestamp(raw)
    if expires_at is None:
        return [REASON_OFFER_EXPIRY_UNPARSEABLE]
    return [REASON_OFFER_EXPIRED] if expires_at <= now else []


def _eligibility_reasons(store_id: Any, trust_snapshot: Any) -> list[str]:
    """R12, fail-closed: blacklisted denies, and an unavailable read denies the same way."""
    if not isinstance(trust_snapshot, Mapping):
        return [REASON_TRUST_SNAPSHOT_UNAVAILABLE]
    try:
        row = trust_snapshot.get(store_id)
    except TypeError:
        # A malformed `store_id` — a dict or a list where a string belongs — is unhashable, and
        # letting the lookup raise would turn a rejectable bid into a crash at the public
        # boundary. It is not a store we know about, so it is denied like any other unknown one.
        return [f"{REASON_TRUST_SNAPSHOT_UNAVAILABLE}:{store_id!r}"]
    if row is None:
        return [f"{REASON_TRUST_SNAPSHOT_UNAVAILABLE}:{store_id}"]
    # Truthy, not `is True`: a snapshot row that spells the flag `1` or `"yes"` is still a
    # blacklisted store, and R12 says the fail-closed direction is the one to take on doubt. The
    # TypeScript peer reads it the same way, for the same reason.
    if bool(_get(row, "blacklisted", False)):
        return [f"{REASON_STORE_BLACKLISTED}:{store_id}"]
    return []


def _signing_envelope_reasons(bid: Any) -> list[str]:
    """D52: the five envelope fields plus a `signature`, or the submission is refused.

    Read off the raw submission, not off a validated `Bid`: `Bid` carries no envelope by design,
    so a schema check against it can never see these fields. That is precisely how an unsigned
    external submission used to come back `ok=True, reasons=[]`.
    """
    payload = _as_plain(bid)
    reasons = [
        f"{REASON_SIGNING_ENVELOPE_INCOMPLETE}:{field}" for field in missing_signing_fields(payload)
    ]
    signature = payload.get("signature") if isinstance(payload, Mapping) else None
    if not isinstance(signature, str) or not signature.strip():
        reasons.append(REASON_SIGNATURE_MISSING)
    return reasons


def validate_bid(
    bid: Any,
    *,
    path: str,
    trust_snapshot: Mapping[str, Any],
    now: datetime | str | float | None = None,
    require_signing_envelope: bool = False,
) -> BidValidationResult:
    """Decide whether `bid` may enter the auction through `path`.

    **This function does NOT check the signing envelope unless you ask it to.** It judges the
    R8/R18/S5 table — provenance, expiry, eligibility, schema — and its schema check is against
    `Bid`, which carries no `signer_id`, `key_id`, `issued_at`, `nonce` or `signature`. A wire
    submission with none of those comes back `ok=True` from the default call. For the real Tier-2
    door use `validate_external_submission`, which is this function with
    `require_signing_envelope=True`.

    Args:
        bid: the bid, as a mapping or as any object exposing the `Bid` fields.
        path: exactly `"hosted"` or `"external"`. Anything else is refused, not guessed at.
        trust_snapshot: `{store_id: {"store_id":…, "score": float, "blacklisted": bool}}`.
            A store with no row is treated as an unavailable eligibility read and denied (R12).
        now: the instant expiry is judged against. Defaults to the current UTC time; pass it
            explicitly to keep a caller deterministic.
        require_signing_envelope: when true, D52's five envelope fields and a non-empty
            `signature` are required, each gap reported as its own reason. Off by default because
            a hosted Tier-1 agent holds no key and has nothing to sign with, and because
            `validate_bid` is also used to judge already-extracted `Bid` objects that never
            carried an envelope.

    Returns:
        `BidValidationResult` — `ok`, the `path` it was judged on, `reasons` (non-empty exactly
        when `ok` is false), `requires_verification`, and `unverified_claim_indexes` so the
        verification queue does not have to re-derive which claims to look at.
    """
    evaluated_at = parse_timestamp(now) or datetime.now(UTC)

    if path not in BID_PATHS:
        return BidValidationResult(
            ok=False,
            path=str(path),
            reasons=[f"{REASON_UNKNOWN_PATH}:{path}"],
            requires_verification=False,
            unverified_claim_indexes=[],
        )

    reasons: list[str] = []

    # 1. Schema validity. A bid that is not this object is refused on every path, before any
    #    field of it is interpreted. The model depends on what the caller says it is holding:
    #    `Bid` forbids extra keys, so validating a real D52 submission against it would report
    #    the four envelope fields as schema violations — the mirror image of admitting a bid
    #    that has none of them.
    model = SignedBidSubmission if require_signing_envelope else Bid
    try:
        model.model_validate(_as_plain(bid))
    except ValidationError as exc:
        reasons.append(
            f"{REASON_SCHEMA_INVALID}:"
            + ";".join(
                ".".join(str(part) for part in error["loc"]) or "<root>"
                for error in exc.errors()[:8]
            )
        )
    except Exception:  # noqa: BLE001 - anything unparseable is simply not a Bid
        reasons.append(REASON_SCHEMA_INVALID)

    # 2. Provenance, at EVERY claim-bearing site. Path-sensitive: this is the whole of R8/R18.
    #    `bid.claims` is not the only place a claim can be written down — the Offer is inside the
    #    bid boundary and carries `commitments` and a provenance-stamped `discount` — and a walk
    #    that covers one site is not an exclusivity property, it is a naming convention.
    offer = _get(bid, "offer")
    claim_reasons, unverified = _claim_provenance_reasons(_get(bid, "claims"), path)
    reasons.extend(claim_reasons)
    reasons.extend(_offer_claim_reasons(offer, path))

    # 3. Offer expiry and 4. seller eligibility — path-insensitive.
    reasons.extend(_expiry_reasons(offer, evaluated_at))
    reasons.extend(_eligibility_reasons(_get(bid, "store_id"), trust_snapshot))

    # 5. D52's signing envelope, when the caller is the external door rather than a component
    #    judging an already-extracted `Bid`.
    if require_signing_envelope:
        reasons.extend(_signing_envelope_reasons(bid))

    ok = not reasons
    return BidValidationResult(
        ok=ok,
        path=path,
        reasons=reasons,
        # A rejected bid is not "admitted pending verification": there is nothing to verify.
        requires_verification=bool(ok and unverified),
        unverified_claim_indexes=unverified if ok else [],
    )


def validate_external_submission(
    submission: Any,
    *,
    trust_snapshot: Mapping[str, Any],
    now: datetime | str | float | None = None,
) -> BidValidationResult:
    """The Tier-2 door: everything `validate_bid` judges, plus D52's signing envelope.

    This is the function an exchange should call on a body arriving at
    `POST /v1/auctions/{auction_id}/bids`. It refuses an unsigned submission with the same
    finality as an expired offer or a blacklisted store — before extraction and before
    verification — and, like `validate_bid`, it never raises.
    """
    return validate_bid(
        submission,
        path=EXTERNAL_PATH,
        trust_snapshot=trust_snapshot,
        now=now,
        require_signing_envelope=True,
    )


__all__ = [
    "BID_PATHS",
    "EXTERNAL_PATH",
    "HOOK_PROVENANCE_SOURCES",
    "HOSTED_PATH",
    "NON_HOOK_PROVENANCE_SOURCES",
    "OFFER_COMMITMENTS_SITE",
    "OFFER_DISCOUNT_SITE",
    "REASON_CLAIM_PROVENANCE_EMPTY_SOURCE",
    "REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE",
    "REASON_CLAIM_WITHOUT_PROVENANCE",
    "REASON_HOSTED_NON_HOOK_PROVENANCE",
    "REASON_OFFER_EXPIRED",
    "REASON_OFFER_EXPIRY_MISSING",
    "REASON_OFFER_EXPIRY_UNPARSEABLE",
    "REASON_SCHEMA_INVALID",
    "REASON_SIGNATURE_MISSING",
    "REASON_SIGNING_ENVELOPE_INCOMPLETE",
    "REASON_STORE_BLACKLISTED",
    "REASON_TRUST_SNAPSHOT_UNAVAILABLE",
    "REASON_UNKNOWN_PATH",
    "REASON_UNVERIFIABLE_CLAIM_SITE",
    "parse_timestamp",
    "validate_bid",
    "validate_external_submission",
]
