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

The material is serialized with `contracts.signing.canonical_json` — RFC 8785 — and NOT with
`json.dumps(sort_keys=True)`. They are different serializations, and both differences are
reachable from a real claim value:

* `sort_keys` orders by CODE POINT, JCS by UTF-16 CODE UNIT, so `{"😀": 1, "\\uffff": 2}` comes
  out `{"\\uffff":2,"😀":1}` one way and `{"😀":1,"\\uffff":2}` the other;
* `json.dumps` writes numbers with `repr`, so `{"rate": 1e-5}` becomes `{"rate":1e-05}` where
  ECMAScript — and therefore JCS, and therefore any JS peer — writes `{"rate":0.00001}`.

A JS peer computing a claim id disagreed on both, and this module was a FOURTH independent
hashing definition in a system whose decisions (D16, D52) say there should be one.

**Adopting JCS re-identifies every existing claim.** The bytes change wherever a claim value
carries a non-ASCII key or a number `repr` and ECMAScript spell differently, so every id derived
from such a value moves. Nothing has stored one yet — this lands before any consumer does, and
there is deliberately no migration path, because there is nothing to migrate.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from contracts.signing import canonical_json

#: Prefix on the returned id, so a bare string is self-describing in a log or a database column.
CLAIM_ID_PREFIX = "claim"

#: Algorithm the content hash uses. Part of the id, so a future change is visible per row.
CLAIM_ID_ALGORITHM = "sha256"


def canonicalize_value(value: Any) -> Any:
    """Normalize a claim value so equal meanings hash equally.

    Integral floats collapse to ints (`30.0` -> `30`), strings are stripped and case-folded,
    mappings are sorted by key, sequences keep their order (a list of ingredients is not a set),
    and sets are SORTED — an unordered collection has no wire order to preserve, and hashing one
    in iteration order would make `claim_id` depend on `PYTHONHASHSEED`, so the same claim would
    get a different id in every process. That is exactly the idempotency this module exists to
    provide, so it is not left to whatever order the set happens to iterate in.
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
    if isinstance(value, (set, frozenset)):
        return sorted((canonicalize_value(item) for item in value), key=repr)
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
    # `canonical_json` refuses anything it cannot render as JSON rather than falling back to
    # `repr`, and `repr` of anything unordered or memory-addressed is not stable across
    # processes. A claim whose value cannot be canonicalized has no stable id, and saying so is
    # better than minting one that silently changes on the next run. It also refuses an integer
    # with no exact double (RFC 8785 §3.1) — a JS peer could not state that value, so the two
    # could not agree on an id for it either.
    encoded = canonical_json(material).encode("utf-8")
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
