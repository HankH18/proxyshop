"""R2: what a shortlist slot's provenance says to the buyer (T-072).

SPEC R2 fixes exactly two buyer-facing label strings — ``"store-confirmed"`` for evidence
the store stands behind, ``"from their website"`` for evidence observed on its public site.
A ``seller_asserted`` claim gets **neither**: it is a statement with no evidence behind it,
so it surfaces R18's verification badge (``"unverified"`` until a ``VerificationResult``
exists) instead of being dressed as provenance.

**The table is not here.** ``contracts.labels`` publishes it, and D30 puts it there so that
the exchange (T-032, which *produces* ``Shortlist.slots[].provenance_labels``) and this
package (which *renders* them) cannot drift into two answers. Restating the six-source map
in this file would be exactly the second copy that decision exists to prevent, so
:func:`provenance_label` is a thin adapter over :func:`contracts.labels.buyer_label` that
does one thing of its own: it converts that function's ``KeyError`` for an unpinned source
into an :class:`~buyer_svc.accept.errors.UnknownProvenanceSource`, because a renderer
catching ``KeyError`` around a label lookup would also swallow a genuine bug in its own
dictionary handling.

Rendering, and why the exchange's labels win
--------------------------------------------
D30: *the exchange supplies the buyer-facing labels and the buyer app renders what it was
given.* :func:`slot_labels` therefore returns the slot's own ``provenance_labels`` verbatim
whenever the exchange sent any, and derives labels from the slot's claims **only** when it
sent none — a shortlist assembled by something older than D30, or a slot handed to the
renderer straight out of a bid. Which of the two happened is not guessed at afterwards:
:class:`LabelledSlot` carries :attr:`~LabelledSlot.labels_source`, so a screen, a log line
or a test can tell "the exchange said this" from "we worked it out" without comparing
strings.

An empty result is never returned. A slot with no provenance at all reads to a buyer as a
slot with nothing to hide, which is the opposite of the truth, so it renders as
``("unverified",)`` — the same answer ``exchange.ranking.shortlist.provenance_labels``
gives for the same input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from contracts.labels import (
    BUYER_PROVENANCE_LABELS,
    LABEL_FROM_THEIR_WEBSITE,
    LABEL_STORE_CONFIRMED,
    LABEL_UNVERIFIED,
    PROVENANCE_BUYER_LABELS,
    buyer_label,
)

from ._reading import plain, read, text
from .errors import UnknownProvenanceSource

__all__ = [
    "BUYER_PROVENANCE_LABELS",
    "LABELS_ABSENT",
    "LABELS_DERIVED",
    "LABELS_SUPPLIED",
    "LABEL_FROM_THEIR_WEBSITE",
    "LABEL_STORE_CONFIRMED",
    "LABEL_UNVERIFIED",
    "PROVENANCE_BUYER_LABELS",
    "LabelledSlot",
    "label_slot",
    "labels_for_claims",
    "provenance_label",
    "render_shortlist",
    "slot_labels",
]

#: :attr:`LabelledSlot.labels_source` — the exchange sent these labels and we printed them.
LABELS_SUPPLIED = "exchange"

#: The exchange sent none, so they were derived from the slot's own claims' provenance.
LABELS_DERIVED = "derived"

#: Neither labels nor readable claims. The slot renders as `unverified`, honestly.
LABELS_ABSENT = "absent"


def provenance_label(provenance: Any) -> str:
    """The buyer-facing label for one ``Provenance`` (R2/D30).

    Accepts anything that carries a source: a ``contracts.Provenance``, its ``dict``, a
    ``ProvenanceSource`` member, or the bare string.

    Raises:
        UnknownProvenanceSource: the source is not one of the seven pinned in
            ``contracts.protocol.ProvenanceSource``. Deliberately not a default — a source
            nobody published a label for, rendered as "store-confirmed" because a ``.get``
            fell through, is precisely the failure R2 exists to prevent.
    """
    try:
        return buyer_label(provenance)
    except KeyError as exc:
        raise UnknownProvenanceSource(
            f"no buyer-facing label is published for this provenance "
            f"({exc.args[0] if exc.args else provenance!r}); the buyer shows nothing rather "
            f"than guessing at evidence it cannot name"
        ) from None


def labels_for_claims(claims: Any) -> tuple[str, ...]:
    """Derive the labels for a sequence of claims from each claim's provenance.

    The fallback path only — see this module's docstring. A claim with no provenance, and a
    claim whose source is not pinned, are both skipped rather than guessed at; if that
    leaves nothing, the caller gets ``()`` and decides what "no provenance at all" renders
    as (:func:`slot_labels` renders it as ``unverified``).
    """
    labels: list[str] = []
    if isinstance(claims, (str, bytes)) or claims is None:
        return ()
    try:
        iterator = iter(claims)
    except TypeError:
        return ()
    for claim in iterator:
        provenance = read(claim, "provenance", None)
        if provenance is None:
            continue
        try:
            label = provenance_label(provenance)
        except UnknownProvenanceSource:
            continue
        if label not in labels:
            labels.append(label)
    return tuple(sorted(labels))


@dataclass(frozen=True, slots=True)
class LabelledSlot:
    """One shortlist slot, ready to render (R2).

    ``labels`` is what the buyer sees. ``labels_source`` is how it got there — one of
    :data:`LABELS_SUPPLIED`, :data:`LABELS_DERIVED`, :data:`LABELS_ABSENT` — and it exists
    so "the exchange said this" is distinguishable from "we worked it out" without
    re-deriving anything.

    ``checkout_url`` is deliberately absent from this type. A slot may carry one; R3 says
    the buyer never follows it, so it is not carried into the object a screen renders from
    and there is nothing for a template to link to by accident.
    """

    slot: str
    bid_ref: str
    auction_id: str
    fit_score: float
    labels: tuple[str, ...]
    labels_source: str
    trust_summary: dict[str, Any] = field(default_factory=dict)
    store_domain: str = ""

    def to_dict(self) -> dict[str, Any]:
        """The published, buyer-facing projection of this slot."""
        return {
            "slot": self.slot,
            "bid_ref": self.bid_ref,
            "auction_id": self.auction_id,
            "fit_score": self.fit_score,
            "provenance_labels": list(self.labels),
            "labels_source": self.labels_source,
            "trust_summary": dict(self.trust_summary),
            "store_domain": self.store_domain,
        }

    def __getitem__(self, key: str) -> Any:
        """Subscriptable as well as attribute-addressed, like T-071's ``ClarifyOutcome``."""
        try:
            return self.to_dict()[key]
        except KeyError:
            raise KeyError(key) from None


def slot_labels(slot: Any, *, derive: bool = True) -> tuple[tuple[str, ...], str]:
    """``(labels, labels_source)`` for one slot. D30: what the exchange sent wins.

    ``derive=False`` turns the claims fallback off entirely, so a slot the exchange sent no
    labels for renders as ``unverified`` rather than as something this package worked out.
    That is the strict reading of D30 — *the buyer app renders what it was given* — and it
    is available to a caller that wants it without making the ordinary case useless.
    """
    supplied = read(slot, "provenance_labels", None)
    if supplied is not None and not isinstance(supplied, (str, bytes)):
        try:
            candidates = [text(item) for item in supplied]
        except TypeError:
            candidates = []
        rendered = tuple(dict.fromkeys(label for label in candidates if label))
        if rendered:
            return rendered, LABELS_SUPPLIED
    if derive:
        derived = labels_for_claims(read(slot, "claims", None))
        if derived:
            return derived, LABELS_DERIVED
    return (LABEL_UNVERIFIED,), LABELS_ABSENT


def label_slot(slot: Any, *, auction_id: str = "", derive: bool = True) -> LabelledSlot:
    """One shortlist slot as the thing a screen renders."""
    labels, source = slot_labels(slot, derive=derive)
    raw_score = read(slot, "fit_score", 0.0)
    try:
        fit_score = float(raw_score)
    except (TypeError, ValueError):
        fit_score = 0.0
    summary = plain(read(slot, "trust_summary", None))
    return LabelledSlot(
        slot=text(read(slot, "slot", "")),
        bid_ref=text(read(slot, "bid_ref", "")),
        auction_id=text(read(slot, "auction_id", "")) or text(auction_id),
        fit_score=fit_score,
        labels=labels,
        labels_source=source,
        trust_summary=summary if isinstance(summary, dict) else {},
        store_domain=text(read(slot, "store_domain", "")),
    )


def render_shortlist(shortlist: Any, *, derive: bool = True) -> list[LabelledSlot]:
    """Every slot of a ``Shortlist`` (or a bare sequence of slots), labelled for display.

    The shortlist's ``auction_id`` is pushed down onto each slot: the contract carries it
    once at the top and :func:`~buyer_svc.accept.handoff.accept` needs it per slot, and
    making the caller re-attach it is how a slot ends up accepted against the wrong auction.
    """
    auction_id = text(read(shortlist, "auction_id", ""))
    slots = read(shortlist, "slots", None)
    if slots is None:
        slots = shortlist
    if slots is None or isinstance(slots, (str, bytes)):
        return []
    try:
        iterator = list(slots)
    except TypeError:
        return []
    return [label_slot(slot, auction_id=auction_id, derive=derive) for slot in iterator]
