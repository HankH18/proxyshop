"""Provenance → buyer-facing label, and provenance → authority rank.

Both are published HERE, once, because two tickets read each of them and a second copy would be
a second answer. D30 puts the label map in this package precisely so the exchange (T-032, the
producer of `Shortlist.slots[].provenance_labels`) and the buyer app (T-072, the renderer) cannot
drift apart; T-072 renders what the exchange supplied and never re-derives it.

SPEC R2 fixes exactly two buyer-facing label strings, `"store-confirmed"` and
`"from their website"`. A seller-asserted claim gets NEITHER: it has no provenance the buyer
should treat as evidence, so it surfaces the R18 verification badge instead. The string it
carries here contains `"unverified"` so a renderer that shows it verbatim still tells the truth.

WHERE THIS DISAGREES WITH D30, AND WHY
--------------------------------------
D30's prose maps `pixel_feed` to `"from their website"` and leaves `learned_policy` and `network`
unlabelled. The frozen acceptance suite maps `pixel_feed`, `learned_policy` and `network` to
`"store-confirmed"` and only `scraped` to `"from their website"`
(`.swarm-loop/acceptance/test_e7_buyer.py:524-540`, an exact-equality assertion T-072 must
satisfy). The suite is the authority over the prose, so the table below is the suite's. The
disagreement is reported rather than silently reconciled — see this ticket's completion report.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from contracts.protocol import ProvenanceSource

#: R2 label for evidence the store itself stands behind.
LABEL_STORE_CONFIRMED = "store-confirmed"

#: R2 label for evidence observed on the store's public site rather than asserted by it.
LABEL_FROM_THEIR_WEBSITE = "from their website"

#: Not an R2 provenance label. A seller-asserted claim surfaces the R18 verification badge; this
#: string is what a renderer shows before a `VerificationResult` exists for the claim.
LABEL_UNVERIFIED = "unverified"

#: The two R2 label strings, and only those two.
BUYER_PROVENANCE_LABELS: frozenset[str] = frozenset(
    {LABEL_STORE_CONFIRMED, LABEL_FROM_THEIR_WEBSITE}
)

#: source -> buyer-facing label. Total over `ProvenanceSource`: every source has an answer, so a
#: renderer never has to invent one for a value it did not expect.
PROVENANCE_BUYER_LABELS: MappingProxyType[str, str] = MappingProxyType(
    {
        ProvenanceSource.owner_statement.value: LABEL_STORE_CONFIRMED,
        ProvenanceSource.envelope_rule.value: LABEL_STORE_CONFIRMED,
        ProvenanceSource.learned_policy.value: LABEL_STORE_CONFIRMED,
        ProvenanceSource.pixel_feed.value: LABEL_STORE_CONFIRMED,
        ProvenanceSource.network.value: LABEL_STORE_CONFIRMED,
        ProvenanceSource.scraped.value: LABEL_FROM_THEIR_WEBSITE,
        ProvenanceSource.seller_asserted.value: LABEL_UNVERIFIED,
    }
)

#: D30: `authority_rank` semantics, pinned so two hooks cannot number the same kind of evidence
#: differently. 1 is the most authoritative and larger is weaker. An owner statement and an
#: envelope rule are the store speaking for itself; a pixel feed is the store's own installed app
#: observing a live checkout; network and learned-policy priors are the platform's inference;
#: scraped text is a third-party reading of a public page; a seller assertion is unevidenced
#: until a `VerificationResult` says otherwise.
PROVENANCE_AUTHORITY_RANK: MappingProxyType[str, int] = MappingProxyType(
    {
        ProvenanceSource.owner_statement.value: 1,
        ProvenanceSource.envelope_rule.value: 1,
        ProvenanceSource.pixel_feed.value: 2,
        ProvenanceSource.network.value: 3,
        ProvenanceSource.learned_policy.value: 3,
        ProvenanceSource.scraped.value: 4,
        ProvenanceSource.seller_asserted.value: 5,
    }
)


def _source_of(provenance: Any) -> str:
    """Read a provenance source off a mapping, a model, an enum member or a bare string."""
    if isinstance(provenance, str):
        raw: Any = provenance
    else:
        try:
            raw = provenance["source"]  # type: ignore[index]
        except Exception:
            raw = getattr(provenance, "source", None)
    return str(getattr(raw, "value", raw) or "")


def buyer_label(provenance: Any) -> str:
    """The buyer-facing label for a `Provenance`, a mapping, or a bare source string.

    Raises `KeyError` for a source outside the pinned enum rather than returning a default: an
    unknown provenance that silently rendered as "store-confirmed" would be the exact failure
    R2 exists to prevent.
    """
    source = _source_of(provenance)
    try:
        return PROVENANCE_BUYER_LABELS[source]
    except KeyError:
        raise KeyError(
            f"no buyer label is published for provenance source {source!r}; the pinned sources "
            f"are {sorted(PROVENANCE_BUYER_LABELS)}"
        ) from None


def canonical_authority_rank(provenance: Any) -> int:
    """The canonical `authority_rank` for a provenance source (D30). 1 is most authoritative."""
    source = _source_of(provenance)
    try:
        return PROVENANCE_AUTHORITY_RANK[source]
    except KeyError:
        raise KeyError(f"no authority rank is published for provenance source {source!r}") from None


__all__ = [
    "BUYER_PROVENANCE_LABELS",
    "LABEL_FROM_THEIR_WEBSITE",
    "LABEL_STORE_CONFIRMED",
    "LABEL_UNVERIFIED",
    "PROVENANCE_AUTHORITY_RANK",
    "PROVENANCE_BUYER_LABELS",
    "buyer_label",
    "canonical_authority_rank",
]
