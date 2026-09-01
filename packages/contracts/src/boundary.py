"""The dual-path bid boundary (R8 / R18 / S5).

Two doors lead into the auction and they admit different things:

* **hosted** — a Tier-1 agent running on our own runtime. Every claim it can make had to come out
  of a tool hook (`get_product_fact`, `get_owner_commitments`, `authorize_discount`, …), and each
  hook stamps its own provenance source. So a hosted bid carrying a `seller_asserted` claim is
  not a policy question: it is evidence that something bypassed the hooks, and it is rejected.
* **external** — a Tier-2 self-hosted agent behind the signed door. It CAN assert claims freely,
  so `seller_asserted` is admitted — and flagged, so the claim reaches `packages/verification`
  before it is ever shown to a buyer as fact (R18). Admitted is not the same as trusted.

Four conditions reject on BOTH paths, because none of them is about who is speaking:

* a claim with no `provenance` key at all, or with an empty `source`;
* an offer whose `expires_at` has passed — or is missing, which fails closed;
* a store the trust snapshot marks blacklisted, or has no row for at all (R12: an unavailable
  eligibility read denies exactly like a positive one);
* a bid that is not schema-valid.

`validate_bid` never raises on bad input. A boundary that threw would make "reject" and "crash"
indistinguishable to the caller and would give the exchange nothing to log back to the seller;
every refusal comes back as `ok=False` with at least one machine-readable reason.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from contracts.protocol import Bid, BidValidationResult, ProvenanceSource

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
REASON_OFFER_EXPIRED = "offer_expired"
REASON_OFFER_EXPIRY_MISSING = "offer_expiry_missing"
REASON_OFFER_EXPIRY_UNPARSEABLE = "offer_expiry_unparseable"
REASON_STORE_BLACKLISTED = "store_blacklisted"
REASON_TRUST_SNAPSHOT_UNAVAILABLE = "trust_snapshot_unavailable"


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


def _claim_provenance_reasons(claims: Any, path: str) -> tuple[list[str], list[int]]:
    """Per-claim provenance verdicts. Returns (reasons, indexes of claims needing verification)."""
    reasons: list[str] = []
    unverified: list[int] = []

    if claims is None:
        return reasons, unverified
    if isinstance(claims, (str, bytes)) or not isinstance(claims, Iterable):
        return [REASON_SCHEMA_INVALID], unverified

    for index, claim in enumerate(claims):
        provenance = _get(claim, "provenance")
        if provenance is None:
            reasons.append(f"{REASON_CLAIM_WITHOUT_PROVENANCE}:{index}")
            continue

        raw_source = _get(provenance, "source")
        source = str(getattr(raw_source, "value", raw_source) or "").strip()
        if not source:
            reasons.append(f"{REASON_CLAIM_PROVENANCE_EMPTY_SOURCE}:{index}")
            continue

        if source in HOOK_PROVENANCE_SOURCES:
            continue
        if source in NON_HOOK_PROVENANCE_SOURCES:
            if path == HOSTED_PATH:
                # R8/S5. A hosted agent cannot mint this source through any hook, so its presence
                # means the claim did not come from one.
                reasons.append(f"{REASON_HOSTED_NON_HOOK_PROVENANCE}:{index}:{source}")
            else:
                # R18. Admitted, but it goes to verification before it is shown as fact.
                unverified.append(index)
            continue

        reasons.append(f"{REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE}:{index}:{source}")

    return reasons, unverified


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


def validate_bid(
    bid: Any,
    *,
    path: str,
    trust_snapshot: Mapping[str, Any],
    now: datetime | str | float | None = None,
) -> BidValidationResult:
    """Decide whether `bid` may enter the auction through `path`.

    Args:
        bid: the bid, as a mapping or as any object exposing the `Bid` fields.
        path: exactly `"hosted"` or `"external"`. Anything else is refused, not guessed at.
        trust_snapshot: `{store_id: {"store_id":…, "score": float, "blacklisted": bool}}`.
            A store with no row is treated as an unavailable eligibility read and denied (R12).
        now: the instant expiry is judged against. Defaults to the current UTC time; pass it
            explicitly to keep a caller deterministic.

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
    #    field of it is interpreted.
    try:
        Bid.model_validate(_as_plain(bid))
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

    # 2. Provenance, per claim. Path-sensitive: this is the whole of R8/R18.
    claim_reasons, unverified = _claim_provenance_reasons(_get(bid, "claims"), path)
    reasons.extend(claim_reasons)

    # 3. Offer expiry and 4. seller eligibility — path-insensitive.
    reasons.extend(_expiry_reasons(_get(bid, "offer"), evaluated_at))
    reasons.extend(_eligibility_reasons(_get(bid, "store_id"), trust_snapshot))

    ok = not reasons
    return BidValidationResult(
        ok=ok,
        path=path,
        reasons=reasons,
        # A rejected bid is not "admitted pending verification": there is nothing to verify.
        requires_verification=bool(ok and unverified),
        unverified_claim_indexes=unverified if ok else [],
    )


__all__ = [
    "BID_PATHS",
    "EXTERNAL_PATH",
    "HOOK_PROVENANCE_SOURCES",
    "HOSTED_PATH",
    "NON_HOOK_PROVENANCE_SOURCES",
    "REASON_CLAIM_PROVENANCE_EMPTY_SOURCE",
    "REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE",
    "REASON_CLAIM_WITHOUT_PROVENANCE",
    "REASON_HOSTED_NON_HOOK_PROVENANCE",
    "REASON_OFFER_EXPIRED",
    "REASON_OFFER_EXPIRY_MISSING",
    "REASON_OFFER_EXPIRY_UNPARSEABLE",
    "REASON_SCHEMA_INVALID",
    "REASON_STORE_BLACKLISTED",
    "REASON_TRUST_SNAPSHOT_UNAVAILABLE",
    "REASON_UNKNOWN_PATH",
    "parse_timestamp",
    "validate_bid",
]
