"""The six trust dimensions, and the typed table that routes a claim into one of them.

**Six, and exactly six** (R12 as amended by D53). Five of them grade an *offer-integrity
promise* — something the store said it would do, graded by what the transaction record shows
it did. The sixth, ``catalog_claim_accuracy``, grades a *product fact*: whether what the
pitch said about the goods matches the catalog. A false dairy-free claim is not a price that
was dishonoured, not a parcel that shipped late and not a return that never happened, so it
has no honest home among the five — but it is a dimension of the SAME Beta framework, not a
second trust system. It decays, updates and serves identically; see :mod:`.engine`.

The routing table is ground truth, not policy this engine sets
--------------------------------------------------------------
``claim_type -> dimension`` lives in the human-approved manifest (T-080) precisely so the
trust engine cannot decide for itself which dimension a contradicted claim should penalise.
This module *consumes* it read-only. :data:`PUBLISHED_CLAIM_TYPE_DIMENSIONS` is a
transcription of the same approved table, used only when the manifest is unreachable (the
deployed container does not ship ``fixtures/``); :data:`CLAIM_TYPE_DIMENSIONS_SOURCE` records
which one is live.

**Exhaustive, and loud when it is not.** :func:`claim_dimension` RAISES
:class:`UnmappedClaimType` for an unknown or empty claim type. It never falls back to a
default dimension: silently defaulting is the failure mode the word "exhaustive" exists to
prevent — a claim type nobody mapped would quietly start penalising whichever dimension the
default happened to name, and the manifest would still look correct.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .manifest import manifest_mapping

__all__ = [
    "CATALOG_DIMENSION",
    "CLAIM_TYPE_DIMENSIONS",
    "CLAIM_TYPE_DIMENSIONS_SOURCE",
    "OFFER_INTEGRITY_CLAIM_TYPES",
    "PRODUCT_FACT_CLAIM_TYPES",
    "PUBLISHED_CLAIM_TYPE_DIMENSIONS",
    "TRANSACTION_DIMENSIONS",
    "TRUST_DIMENSIONS",
    "UnknownTrustDimension",
    "UnmappedClaimType",
    "claim_dimension",
    "is_trust_dimension",
    "require_trust_dimension",
]

#: The five dimensions that grade an offer-integrity promise.
TRANSACTION_DIMENSIONS: tuple[str, ...] = (
    "price_honored",
    "discount_honored",
    "shipped_on_time",
    "not_returned",
    "feedback_match",
)

#: The sixth dimension, and the only one that grades a product fact (D53).
CATALOG_DIMENSION = "catalog_claim_accuracy"

#: The complete trust vocabulary. Six, and exactly six.
TRUST_DIMENSIONS: tuple[str, ...] = (*TRANSACTION_DIMENSIONS, CATALOG_DIMENSION)


class UnmappedClaimType(LookupError):
    """A claim type the approved table does not map to a trust dimension.

    Deliberately a :class:`LookupError` and NOT a :class:`KeyError`: a caller's
    ``except KeyError`` around a dictionary read must not swallow "the human-approved routing
    table has no entry for this claim type", which is a ground-truth gap and needs a human,
    not a retry.
    """


class UnknownTrustDimension(LookupError):
    """An observation naming a dimension outside the published six."""


#: The approved ``claim_type -> dimension`` table as published in
#: ``fixtures/manifest.json`` at ``manifest_version`` 1.0.0 / DESIGN "Claim-type -> trust-
#: dimension mapping" (amended by D53). Used only when the manifest itself is unreachable —
#: see :mod:`.manifest` for why that case has to keep working.
PUBLISHED_CLAIM_TYPE_DIMENSIONS: Mapping[str, str] = MappingProxyType(
    {
        # offer-integrity claim types keep the transaction dimension they have always had
        "price": "price_honored",
        "unit_price": "price_honored",
        "total_price": "price_honored",
        "discount": "discount_honored",
        "promo_eligibility": "discount_honored",
        "delivery": "shipped_on_time",
        "shipping_speed": "shipped_on_time",
        "dispatch_window": "shipped_on_time",
        "return_policy": "not_returned",
        "warranty": "not_returned",
        # product-fact claim types land on the dimension that means what they say
        "ingredients": CATALOG_DIMENSION,
        "compatibility": CATALOG_DIMENSION,
        "nutrition": CATALOG_DIMENSION,
        "specifications": CATALOG_DIMENSION,
    }
)


def is_trust_dimension(name: object) -> bool:
    """Whether ``name`` is one of the published six."""
    return isinstance(name, str) and name in TRUST_DIMENSIONS


def _resolve_claim_type_dimensions() -> tuple[dict[str, str], str]:
    """The live routing table and where it came from.

    The manifest wins whenever it is reachable AND every entry it publishes routes to one of
    the six dimensions. A table carrying a seventh dimension name is not "mostly usable" —
    it means ground truth and this engine disagree about the vocabulary, and quietly keeping
    the good half would route the rest of that document's claims to nowhere. In that case the
    published transcription takes over and :data:`CLAIM_TYPE_DIMENSIONS_SOURCE` says so.
    """
    table = manifest_mapping("claim_type_dimensions")
    if table:
        resolved = {str(key): str(value) for key, value in table.items()}
        if resolved and all(is_trust_dimension(value) for value in resolved.values()):
            return resolved, "manifest"
    return dict(PUBLISHED_CLAIM_TYPE_DIMENSIONS), "published-fallback"


_TABLE, CLAIM_TYPE_DIMENSIONS_SOURCE = _resolve_claim_type_dimensions()

#: The live ``claim_type -> dimension`` table. Read-only by construction: the engine consumes
#: this mapping, it does not author it (D18).
CLAIM_TYPE_DIMENSIONS: Mapping[str, str] = MappingProxyType(_TABLE)

#: The two halves of the vocabulary, derived from the live table rather than re-listed.
OFFER_INTEGRITY_CLAIM_TYPES: tuple[str, ...] = tuple(
    sorted(k for k, v in CLAIM_TYPE_DIMENSIONS.items() if v != CATALOG_DIMENSION)
)
PRODUCT_FACT_CLAIM_TYPES: tuple[str, ...] = tuple(
    sorted(k for k, v in CLAIM_TYPE_DIMENSIONS.items() if v == CATALOG_DIMENSION)
)


def claim_dimension(claim_type: object) -> str:
    """The trust dimension a verification outcome on ``claim_type`` belongs to.

    Args:
        claim_type: the typed claim type carried by a ``VerificationResult`` claim.

    Returns:
        Exactly one of :data:`TRUST_DIMENSIONS`.

    Raises:
        UnmappedClaimType: ``claim_type`` is empty, ``None``, or absent from the approved
            table. This is the whole value of an exhaustive mapping — the alternative is a
            silent default onto some dimension, which scores a store for a failure it did
            not have.
    """
    key = "" if claim_type is None else str(claim_type).strip()
    dimension = CLAIM_TYPE_DIMENSIONS.get(key) if key else None
    if dimension is None:
        raise UnmappedClaimType(
            f"claim_type {claim_type!r} is not in the approved claim_type -> dimension table "
            f"(source: {CLAIM_TYPE_DIMENSIONS_SOURCE}; mapped types: "
            f"{sorted(CLAIM_TYPE_DIMENSIONS)}). The table is human-approved ground truth "
            "(T-080/D18) and this engine consumes it read-only, so an unmapped type is a "
            "manifest gap to be approved, never a default this engine may pick."
        )
    return dimension


def require_trust_dimension(name: object) -> str:
    """``name`` as one of the six, or raise :class:`UnknownTrustDimension`."""
    if not is_trust_dimension(name):
        raise UnknownTrustDimension(
            f"{name!r} is not one of the six trust dimensions {list(TRUST_DIMENSIONS)}. "
            "The vocabulary is closed (D53): a seventh dimension is a SPEC change, and an "
            "observation naming one would otherwise vanish from every served snapshot."
        )
    return str(name)
