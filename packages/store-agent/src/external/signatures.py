"""HMAC over the one canonical form (D52) — the half of the external door a *seller* runs.

`sign_bid` is the reference signer. It exists so the frozen suite, the reference personas and
any Tier-2 seller can produce a submission this door will accept without reimplementing the
canonicalizer, and so the signature is reproducible from published material alone: the secret,
and `contracts.signing.canonical_signing_bytes`. Nothing private to this module enters the
digest — no salt, no field ordering of our own, no version prefix. A third party holding the
contract and the key can recompute every byte.

**The message is `canonical_signing_bytes(payload)` and nothing else.** Signing anything
derived from it (a re-serialization, a lower-cased copy, the bytes plus a separator) would make
the signer and the verifier agree only for as long as both derivations stay in step, which is
the failure this ticket exists to avoid.

**Refusing to sign is a feature.** `canonical_signing_bytes` raises when the envelope is
incomplete, and this function does not catch that. Bytes for a submission that cannot legally
exist are bytes nobody can verify, and handing a seller a signature for an unsignable payload
teaches them the door accepted something it will reject.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

from contracts.signing import canonical_signing_bytes

__all__ = ["sign_bid", "signature_bytes", "verify_signature"]

#: The digest the envelope is signed with. Named rather than inlined because both sides read it.
SIGNATURE_DIGEST = hashlib.sha256


def signature_bytes(value: object) -> bytes:
    """The bytes of any value that has to become bytes without raising. Never raises.

    Used for the secret AND for the attacker-supplied signature, because a digest comparison is
    only as total as its least total input. `hmac.compare_digest` accepts `str` only when *both*
    sides are ASCII-only and raises `TypeError` otherwise — "both sides are ASCII" is an
    assumption about the attacker, not a property of the code, and one non-ASCII byte would turn
    "this signature did not verify" into an unhandled exception where a rejection belongs.

    `surrogatepass` is what makes the promise true: a lone surrogate is the one thing a plain
    utf-8 encode refuses, and lone surrogates are exactly what a lenient decode produces. The
    same reasoning is already load-bearing in `merchant_svc.install.signatures`; it is
    reimplemented here rather than imported because `store_agent` importing an app package
    would be a new architectural edge for three lines of encoding.
    """
    text = value if isinstance(value, str) else str(value)
    return text.encode("utf-8", "surrogatepass")


def sign_bid(payload: Mapping[str, Any], secret: object) -> str:
    """HMAC-SHA256 over `canonical_signing_bytes(payload)`, hex-encoded.

    Args:
        payload: the submission — `Bid` fields plus the five-field signing envelope. The
            `signature` key, if present, is excluded from the body digest by
            `contracts.signing.payload_hash`, so signing an already-signed payload is stable.
        secret: the shared secret for `(payload["signer_id"], payload["key_id"])`.

    Returns:
        The lowercase hex digest, the spelling the wire carries.

    Raises:
        contracts.signing.CanonicalisationError: the envelope is incomplete, the payload is not
            a mapping, or it carries a value that cannot be canonicalized. Deliberately not
            caught: see the module docstring.
    """
    return hmac.new(
        signature_bytes(secret), canonical_signing_bytes(payload), SIGNATURE_DIGEST
    ).hexdigest()


def verify_signature(payload: Mapping[str, Any], signature: object, secret: object) -> bool:
    """Whether `signature` is the signature `secret` produces over `payload`. Never raises.

    Compared in constant time on **bytes**. Length still leaks — it does in
    `hmac.compare_digest` too — but a digest's length is public and the content comparison does
    not depend on how far the two agree.

    A signature that is not a non-empty string is `False` before anything is computed. `None`,
    an integer and `""` are all "no signature was presented", and the door must answer that the
    same way it answers a wrong one: with a refusal, not a `TypeError` from an unauthenticated
    caller.
    """
    if not isinstance(signature, str) or not signature.strip():
        return False
    try:
        expected = sign_bid(payload, secret)
    except Exception:  # noqa: BLE001 - an unsignable payload cannot have a valid signature
        return False
    return hmac.compare_digest(signature_bytes(expected), signature_bytes(signature))
