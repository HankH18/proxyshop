"""The hash a merchant's written approval is bound to.

R6 requires activation to rest on a *recorded written approval artifact*, and an artifact is
only worth recording if it names what was approved. :func:`approval_digest` is that name: a
SHA-256 over a canonical rendering of the terms the merchant read.

Two decisions are load-bearing and are stated here rather than left to be inferred:

**``activation`` is not part of the digest.** The merchant approves *terms* — the walls, the
budget, the clusters, the promises — not the lifecycle state the document happens to be in
when they sign. Hashing the state as well would mean the recorded artifact stopped verifying
against its own envelope the instant activation flipped it to ``active``, which is exactly
when an auditor most needs to check it. ``version`` **is** in the digest, and that is what
binds an approval to one specific document: edit the envelope and the approval on file no
longer authorizes anything.

**The rendering is canonical, not incidental.** Numbers are coerced (``20`` and ``20.0`` are
the same wall), lists whose order carries no meaning are sorted, and keys whose value is
``None`` are dropped, so an envelope, its ``to_dict()`` and its JSON round trip all hash the
same. Two *different* sets of terms still hash differently — sorting a multiset is injective.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from merchant_svc.envelope.model import TERM_FIELDS, EnvelopeInvalid, as_document

#: The prefix every digest carries, so a stored hash says what produced it.
DIGEST_ALGORITHM = "sha256"


def approved_terms(envelope: Any) -> dict[str, Any]:
    """The canonical, JSON-able projection of ``envelope`` that an approval covers.

    Args:
        envelope: an :class:`~merchant_svc.envelope.model.Envelope`, a ``contracts.Envelope``,
            or any mapping carrying the seven :data:`~merchant_svc.envelope.model.TERM_FIELDS`.

    Returns:
        The seven approved-terms fields, canonicalized.

    Raises:
        EnvelopeInvalid: a term field is missing or is not the shape a term field has. Fail
            closed: hashing a partial document would mint a digest that looks like an
            approval of terms nobody wrote down.
    """
    document = as_document(envelope)
    missing = [field for field in TERM_FIELDS if document.get(field) is None]
    if missing:
        raise EnvelopeInvalid(
            f"cannot take an approval digest over a partial envelope; missing: {missing}"
        )

    return {
        "store_id": str(document["store_id"]),
        "version": int(document["version"]),
        "floors": sorted(
            (
                {
                    "product_ref": _optional_str(floor.get("product_ref")),
                    "min_price": float(floor["min_price"]),
                }
                for floor in _sequence(document["floors"], "floors")
            ),
            key=_stable_key,
        ),
        "max_discount_pct": float(document["max_discount_pct"]),
        "budget_cap": float(document["budget_cap"]),
        # A cluster list is a set of things to pursue; the same set written in another order
        # is the same agreement, and an approval must not stop verifying because a dashboard
        # re-sorted a multi-select.
        "pursue_clusters": sorted(
            str(cluster) for cluster in _sequence(document["pursue_clusters"], "pursue_clusters")
        ),
        "standing_commitments": sorted(
            (
                _canonical(claim)
                for claim in _sequence(document["standing_commitments"], "standing_commitments")
            ),
            key=_stable_key,
        ),
    }


def canonical_text(envelope: Any) -> str:
    """The exact text :func:`approval_digest` hashes. Useful in a refusal message."""
    return json.dumps(
        approved_terms(envelope),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def approval_digest(envelope: Any) -> str:
    """The hash of ``envelope``'s approved terms, as ``"sha256:<hex>"``.

    Args:
        envelope: the envelope whose terms an approval would cover.

    Returns:
        A non-empty, stable, algorithm-tagged digest.

    Raises:
        EnvelopeInvalid: ``envelope`` does not carry a full set of approved terms.
    """
    payload = canonical_text(envelope).encode("utf-8")
    return f"{DIGEST_ALGORITHM}:{hashlib.sha256(payload).hexdigest()}"


def approval_covers(envelope: Any, envelope_hash: str) -> bool:
    """``True`` when ``envelope_hash`` is the digest of ``envelope``'s approved terms.

    Compared with :func:`hmac.compare_digest`, so a caller cannot learn the expected digest
    one character at a time by timing the refusal.
    """
    if not isinstance(envelope_hash, str) or not envelope_hash.strip():
        return False
    return hmac.compare_digest(approval_digest(envelope), envelope_hash.strip())


def _sequence(value: Any, field: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping):
        raise EnvelopeInvalid(
            f"envelope field {field!r} must be a list, got {type(value).__name__}"
        )
    try:
        return list(value)
    except TypeError as exc:
        raise EnvelopeInvalid(
            f"envelope field {field!r} must be a list, got {type(value).__name__}"
        ) from exc


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _canonical(value: Any) -> Any:
    """``value`` with mappings key-sorted and ``None`` entries dropped.

    Dropping nulls is what makes ``approval_digest(envelope)`` and
    ``approval_digest(envelope.to_dict())`` agree with the same document read back out of
    JSON, where an optional Claim field may be absent rather than explicitly null.
    """
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if item is not None
        }
    if isinstance(value, (str, bytes)) or value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        return [_canonical(item) for item in value]
    except TypeError:  # pragma: no cover - freeze() refuses anything that gets here
        return value


def _stable_key(value: Any) -> str:
    """A total order over JSON documents, so ``sorted`` never raises on mixed types."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
