"""The external door's signing envelope, and the one canonical form the signature covers (D52).

Two separate things live here and it matters that they stay separate:

**The envelope is a property of a SUBMISSION, not of a `Bid`.** `signer_id`, `key_id`,
`issued_at`, `nonce` and `schema_version` are required on every bid that crosses the public
boundary at `POST /v1/auctions/{auction_id}/bids`. They are *not* fields of `Bid`: a hosted
Tier-1 agent answering `POST /v1/bid-requests` never crosses that boundary and holds no key, so
requiring them on `Bid` itself would demand a signature from something that has nothing to sign
with. The submission shape is `SignedBidSubmission` — `Bid` ∪ `SigningEnvelope`, all five
required, no optional-field mode and no legacy-tolerant variant.

**The canonical bytes are defined once, here, and called by both sides.** `sign_bid` and
`receive_bid` (T-044, `packages.store_agent.src.external`) must produce and check the *same*
bytes; two implementations of a canonicalizer is two protocols. This module owns the single
implementation and T-044 re-exports it, because `packages.store_agent.src.external` is the import
path the frozen suite binds to and physically relocating those functions voids the freeze.

The signed input covers `auction_id`, `signer_id`, `store_id`, `issued_at`, `nonce`, `key_id`,
`schema_version` and `payload_hash(payload)`. Changing any one of them changes the bytes, so a
signature cannot be lifted onto a different auction, signer, store, instant, key or body.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from contracts.protocol import SignedBidSubmission, SigningEnvelope

#: D52: the five fields required on every external submission. Order is the DESIGN order.
REQUIRED_SIGNING_FIELDS: tuple[str, ...] = (
    "signer_id",
    "key_id",
    "issued_at",
    "nonce",
    "schema_version",
)

#: The fields the signature covers, in the order DESIGN §Interfaces lists them. `payload_hash`
#: is computed, not read off the payload.
SIGNED_FIELDS: tuple[str, ...] = (
    "auction_id",
    "signer_id",
    "store_id",
    "issued_at",
    "nonce",
    "key_id",
    "schema_version",
)

#: Keys excluded from the body digest: the envelope rides in the canonical bytes in its own
#: right, and `signature` cannot cover itself.
_NON_BODY_KEYS: frozenset[str] = frozenset(
    {"signer_id", "key_id", "issued_at", "nonce", "signature"}
)

#: Prefix on `payload_hash` output, so a digest is self-describing if the algorithm ever moves.
PAYLOAD_HASH_ALGORITHM = "sha256"


def canonical_json(value: Any) -> str:
    """RFC-8785-style canonical JSON: sorted keys, no insignificant whitespace, UTF-8 text.

    Deterministic for a given *content*, which is the only property the signature needs:
    reordering a mapping or round-tripping the payload through JSON must not change the bytes.
    Scoped to signing — the ledger's hash chain has its own canonicalizer in
    `apps/trust/src/ledger` (D16) and neither borrows the other's.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def payload_hash(payload: Mapping[str, Any]) -> str:
    """A digest over the bid BODY — everything except the envelope and the signature itself.

    Stable across a JSON round trip and across mapping insertion order, and it changes whenever
    any part of the offer or the claims changes. That is what makes the signature cover the
    thing the seller is actually promising, rather than only the metadata around it.
    """
    if not isinstance(payload, Mapping):
        raise TypeError(f"payload_hash expects a mapping, got {type(payload).__name__}")
    body = {key: value for key, value in payload.items() if key not in _NON_BODY_KEYS}
    digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    return f"{PAYLOAD_HASH_ALGORITHM}:{digest}"


def missing_signing_fields(payload: Mapping[str, Any]) -> list[str]:
    """Which of the five required envelope fields are absent or empty. Empty list means complete."""
    if not isinstance(payload, Mapping):
        return list(REQUIRED_SIGNING_FIELDS)
    missing = []
    for field in REQUIRED_SIGNING_FIELDS:
        value = payload.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field)
    return missing


def canonical_signing_bytes(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes a submission's `signature` covers.

    Raises `ValueError` when the envelope is incomplete: refusing to produce signing input for a
    submission that cannot legally exist is better than producing bytes nobody can verify.
    """
    if not isinstance(payload, Mapping):
        raise TypeError(
            f"canonical_signing_bytes expects a mapping, got {type(payload).__name__}"
        )

    missing = missing_signing_fields(payload)
    if missing:
        raise ValueError(
            "cannot canonicalize a submission with an incomplete signing envelope; "
            f"missing {missing} (D52: all of {list(REQUIRED_SIGNING_FIELDS)} are required)"
        )
    if payload.get("auction_id") in (None, "") or payload.get("store_id") in (None, ""):
        raise ValueError(
            "cannot canonicalize a submission without auction_id and store_id — both are "
            "covered fields, so a signature that omitted them could be lifted across auctions"
        )

    covered: dict[str, Any] = {field: payload.get(field) for field in SIGNED_FIELDS}
    covered["payload_hash"] = payload_hash(payload)
    return canonical_json(covered).encode("utf-8")


def envelope_of(payload: Mapping[str, Any]) -> SigningEnvelope:
    """Lift the five envelope fields out of a submission. Raises if any is missing."""
    return SigningEnvelope.model_validate(
        {field: payload.get(field) for field in REQUIRED_SIGNING_FIELDS}
    )


def keyring_secret(
    keyring: Mapping[str, Mapping[str, str]], signer_id: str, key_id: str
) -> str | None:
    """Look up `(signer_id, key_id)` in the D52 keyring. `None` when the pair is unknown.

    The keyring is nested `{signer_id: {key_id: secret}}` and the OUTER key is the signer, not
    the store. Two signers may legitimately use the same `key_id` string, so a lookup keyed on
    `key_id` alone is wrong; and an unknown pair never falls back to another key of that signer,
    because falling back is what makes a revoked key still work.
    """
    if not isinstance(keyring, Mapping):
        return None
    signer_keys = keyring.get(signer_id)
    if not isinstance(signer_keys, Mapping):
        return None
    secret = signer_keys.get(key_id)
    return secret if isinstance(secret, str) and secret else None


def signing_envelope_errors(payload: Mapping[str, Any]) -> Sequence[str]:
    """Human-readable reasons a submission's envelope is unacceptable. Empty means acceptable."""
    missing = missing_signing_fields(payload)
    return [f"missing required signing field: {field}" for field in missing]


__all__ = [
    "PAYLOAD_HASH_ALGORITHM",
    "REQUIRED_SIGNING_FIELDS",
    "SIGNED_FIELDS",
    "SignedBidSubmission",
    "SigningEnvelope",
    "canonical_json",
    "canonical_signing_bytes",
    "envelope_of",
    "keyring_secret",
    "missing_signing_fields",
    "payload_hash",
    "signing_envelope_errors",
]
