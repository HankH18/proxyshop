"""`claim_id` — a content hash that survives re-extraction (D25).

A claim has no natural key. Extract the same pitch twice and you get two claim records that mean
the same thing; without a stable identity, T-065's idempotency criterion is unsatisfiable and
`VerificationResult.claim_ref` points at whichever row happened to be written last.

So `claim_id` is a content hash over `(pitch_ref, key, canonicalized value, claim_type)`. All
four matter:

* `pitch_ref` scopes the claim to the text it came from, so two stores asserting "free returns"
  are two claims with two verification outcomes, not one shared row;
* `key` and `claim_type` distinguish "the 30-day return policy" from "the 30-day warranty";
* the value is CANONICALIZED before hashing, so `30` and `30.0`, and two dicts written in
  different key orders, hash the same. Re-extraction that produces an equal value must produce an
  equal id or the whole point is lost.

Provenance is deliberately NOT in the hash. The same claim re-observed from a fresher snapshot is
the same claim; folding provenance in would mint a new id every crawl and defeat idempotency.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

#: Prefix on the returned id, so a bare string is self-describing in a log or a database column.
CLAIM_ID_PREFIX = "claim"

#: Algorithm the content hash uses. Part of the id, so a future change is visible per row.
CLAIM_ID_ALGORITHM = "sha256"


def canonicalize_value(value: Any) -> Any:
    """Normalize a claim value so equal meanings hash equally.

    Integral floats collapse to ints (`30.0` -> `30`), strings are stripped and case-folded,
    mappings are sorted by key, sequences keep their order (a list of ingredients is not a set).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        return value.strip().casefold()
    if isinstance(value, Mapping):
        return {
            str(k): canonicalize_value(v)
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [canonicalize_value(item) for item in value]
    return value


def claim_id(
    *,
    pitch_ref: str | None,
    key: str,
    value: Any,
    claim_type: Any = None,
) -> str:
    """The stable content hash for a claim. Same content in, same id out, on every extraction."""
    material = {
        "pitch_ref": (pitch_ref or "").strip(),
        "key": (key or "").strip().casefold(),
        "value": canonicalize_value(value),
        "claim_type": str(getattr(claim_type, "value", claim_type) or ""),
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"{CLAIM_ID_PREFIX}:{CLAIM_ID_ALGORITHM}:{digest}"


def claim_id_for(claim: Any, *, pitch_ref: str | None = None) -> str:
    """`claim_id` for an existing `Claim` (or claim-shaped mapping).

    `pitch_ref` falls back to the claim's own `source_span.pitch_ref` when not given explicitly.
    """
    if isinstance(claim, Mapping):
        read = claim.get
    else:

        def read(name: str, default: Any = None) -> Any:  # type: ignore[misc]
            return getattr(claim, name, default)

    span = read("source_span") or {}
    span_ref = (
        span.get("pitch_ref") if isinstance(span, Mapping) else getattr(span, "pitch_ref", None)
    )
    return claim_id(
        pitch_ref=pitch_ref if pitch_ref is not None else span_ref,
        key=str(read("key") or ""),
        value=read("value"),
        claim_type=read("claim_type"),
    )


__all__ = [
    "CLAIM_ID_ALGORITHM",
    "CLAIM_ID_PREFIX",
    "canonicalize_value",
    "claim_id",
    "claim_id_for",
]
