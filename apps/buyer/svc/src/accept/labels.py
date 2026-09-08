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

PRODUCT, PRICE and COMMITMENTS
------------------------------
R2 asks one slot to show five things, and for a long time this module carried two of them.
``contracts.protocol.ShortlistSlot`` now publishes ``product``, ``price`` and
``commitments``, and :class:`LabelledSlot` carries all three through to the screen — because
a shopper choosing between four stores is choosing on what the thing is and what it costs,
and a slot that arrives with a price and leaves without one is this hop deciding they may
not see it.

**Absence is the ordinary case and it is never a zero.** An R10 fallback bid promises
nothing and a roster row with no readable list price prices nothing, so all three fields are
``None`` when the exchange sent nothing readable — never ``0.0``, which is the cheapest
number there is and would win every comparison a shopper makes, and never ``[]``, which
reads as "this store committed to nothing" rather than "the exchange sent no commitments".
The readable/unreadable split here is deliberately the *same* one
``exchange.ranking.serving`` applies when it publishes the fields, so a slot that made it
through that producer is never nulled again here: the round trip is lossless for honest
traffic, and the checks exist for the shortlist a *caller* posts to ``/render``, which is an
arbitrary ``dict`` this package does not own.

Commitments get a label EACH. The slot's ``provenance_labels`` are one aggregate line, and a
shopper deciding whether to believe "free returns" needs the label for that promise rather
than for the slot. It is derived here — from :func:`provenance_label`, i.e. from the same
``contracts.labels`` table the exchange labels slots with, so there is still exactly one
source→label answer in the tree. That is not the D30 exception: D30 is about the *slot's*
``provenance_labels``, which are still rendered verbatim and never re-derived.
"""

from __future__ import annotations

import math
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
    "LabelledCommitment",
    "LabelledSlot",
    "commitment_label",
    "label_slot",
    "labels_for_claims",
    "provenance_label",
    "render_shortlist",
    "slot_commitments",
    "slot_fallback",
    "slot_fallback_reason",
    "slot_labels",
    "slot_price",
    "slot_product",
    "slot_rows",
    "slot_store_domain",
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
class LabelledCommitment:
    """One promise a store made, and how well evidenced it is (R2).

    ``label`` is the buyer's only signal of what has been checked, so it is beside the
    promise rather than aggregated with the slot's. ``value`` is the store's own — a bool, a
    number, a string, whatever the claim carried — and is reduced to plain data rather than
    coerced to text, so a renderer can tell ``True`` from the string ``"true"``.
    """

    key: str
    value: Any
    unit: str | None
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "unit": self.unit, "label": self.label}


def commitment_label(claim: Any) -> str:
    """The buyer-facing provenance label for ONE commitment.

    :func:`provenance_label` for a source the tree publishes a label for. A commitment whose
    provenance is missing, or whose source is not one of the seven pinned in
    ``ProvenanceSource``, reads as :data:`LABEL_UNVERIFIED` — the promise is still shown,
    and it is shown as something nobody has checked.

    The alternative, dropping it the way :func:`labels_for_claims` drops it from the
    *aggregate*, is wrong here for a reason worth stating: an aggregate that omits an
    unreadable claim is merely incomplete, but a commitment list that omits one hides a
    promise the store made. Never a default of "store-confirmed", which is the failure R2
    exists to prevent.
    """
    provenance = read(claim, "provenance", None)
    if provenance is None:
        return LABEL_UNVERIFIED
    try:
        return provenance_label(provenance)
    except UnknownProvenanceSource:
        return LABEL_UNVERIFIED


def _finite(value: Any) -> float | None:
    """``value`` as a finite float, or ``None``.

    ``bool`` is excluded before the conversion because ``True`` is not one dollar, and NaN is
    excluded after it because NaN compares false against every bound a caller might apply.
    The same two rules ``exchange.ranking.serving._finite`` applies when it publishes the
    price, so what that producer served reads back identically here.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def slot_store_domain(slot: Any) -> str | None:
    """The platform's registered domain for this slot's store, or ``None``. Never ``""``.

    The one job here is to make ABSENCE a value with a name. ``ShortlistSlot.store_domain``
    is optional and nullable, so a slot can arrive with the key missing, with ``null``, or —
    from a producer written before ``minLength: 1`` was published — with ``""``. All three
    say the same thing, *the platform holds no registered domain for this store*, and they
    are collapsed here into the one spelling the rest of this package tests against.

    Why ``""`` is not allowed to survive: it is a string, so `if domain:` is the only thing
    that separates it from a real host, and every caller that forgot that check silently got
    "no host constraint" instead of a refusal. That is not hypothetical — it is exactly how
    :func:`buyer_svc.accept.handoff.accept` came to run its redirect guard with scheme and
    host-presence only on every slot of every deployment. ``None`` cannot be mistaken for a
    domain by a template, a comparison or a type checker.

    It is deliberately NOT read from ``checkout_url``: R3 makes that value authority for
    nothing, and deriving the expected host from the URL being checked would let a spoofed
    slot certify its own spoofed permalink.
    """
    domain = text(read(slot, "store_domain", None))
    return domain or None


def _slot_identity(product: Any) -> dict[str, Any] | None:
    """The PLATFORM's own crawled name for this product, or ``None`` (D55).

    Forwarded, never invented, and that distinction is the whole reason this function is allowed
    to exist beside a docstring that says *nothing here invents a title*. This package still
    owns no catalogue and still resolves no reference. What it renders is a name the EXCHANGE
    published, out of the platform's own crawl, under a key that names the snapshot it came from
    — and the ``source`` is carried through precisely so a screen can say whose name it is
    showing rather than presenting it as the shop's.

    ``None`` unless BOTH required fields read as non-empty strings. ``title`` alone would be a
    name with no provenance, which is the thing this package refuses to display; ``source``
    alone is not a name at all. A title that arrives blank or as a non-string is absent, not
    ``""`` — the same rule :func:`slot_store_domain` keeps, for the same reason: an empty string
    is what a template renders as a present-but-blank value.
    """
    identity = read(product, "identity", None)
    if identity is None:
        return None
    title = text(read(identity, "title", None))
    source = text(read(identity, "source", None))
    if not title or not source:
        return None
    return {
        "title": title,
        "brand": text(read(identity, "brand", None)) or None,
        "source": source,
        "observed_at": text(read(identity, "observed_at", None)) or None,
    }


def slot_product(slot: Any) -> dict[str, Any] | None:
    """R2's PRODUCT for one slot: WHICH catalogue thing this slot is offering.

    ``product_ref`` and ``variant_ref`` are references and this package invents no title of its
    own for them — it owns no catalogue and resolves nothing. What changed is that it is no
    longer handed only references: ``identity`` is a name the PLATFORM crawled and the exchange
    published, forwarded here with the snapshot it came from attached (:func:`_slot_identity`).
    Before it existed, a shopper was shown two opaque refs and asked to choose between them.

    ``None`` when the slot names no readable ``product_ref``: an R10 fallback minted from a
    roster row that named none carries ``{"product_ref": None}``, and publishing that would put
    the word ``null`` on a screen where the product goes. A slot with a ref but no identity is
    still published — the ref is what the accept path resolves against, and losing it because
    the platform has not crawled the product would be the worse failure.
    """
    product = read(slot, "product", None)
    if product is None:
        return None
    product_ref = text(read(product, "product_ref", None))
    if not product_ref:
        return None
    rendered: dict[str, Any] = {"product_ref": product_ref}
    variant_ref = text(read(product, "variant_ref", None))
    rendered["variant_ref"] = variant_ref or None
    rendered["identity"] = _slot_identity(product)
    return rendered


def _slot_discount(price: Any) -> Any:
    """The published discount, or ``None``. A STATED depth, never an entitlement (D22).

    Forwarded whole rather than rebuilt, because it is the exchange's published ``Discount``
    and re-spelling it here would drop whatever a later schema adds to it. What is checked is
    only that it is readable *as* one: a discount with no type, or with a value that is not a
    finite number, is not shown at all rather than shown as ``undefined% off``.
    """
    discount = read(price, "discount", None)
    if discount is None:
        return None
    if not text(read(discount, "type", None)):
        return None
    if _finite(read(discount, "value", None)) is None:
        return None
    rendered = plain(discount)
    return rendered if isinstance(rendered, dict) else None


def slot_price(slot: Any) -> dict[str, Any] | None:
    """R2's PRICE for one slot: what this store is asking, and until when.

    ``None`` unless BOTH prices read as finite numbers. That is the contract's own rule and
    its reason is a shopper's: a slot showing a unit price with no total invites comparing
    two different quantities as if they were the same offer, and a slot whose missing price
    defaulted to ``0`` would beat every real one.
    """
    price = read(slot, "price", None)
    if price is None:
        return None
    unit_price = _finite(read(price, "unit_price", None))
    total_price = _finite(read(price, "total_price", None))
    if unit_price is None or total_price is None:
        return None
    rendered: dict[str, Any] = {"unit_price": unit_price, "total_price": total_price}
    rendered["currency"] = text(read(price, "currency", None)) or None
    rendered["discount"] = _slot_discount(price)
    rendered["expires_at"] = text(read(price, "expires_at", None)) or None
    return rendered


def slot_fallback(slot: Any) -> bool | None:
    """WHOSE PRICE this slot is showing: ``False`` a store's bid, ``True`` the exchange's
    stand-in, ``None`` a producer that did not say (R10/D55).

    Three states and not two, deliberately. ``ShortlistSlot.fallback`` is optional and nullable,
    so a shortlist written before the field existed carries no answer at all — and a screen that
    read that as ``False`` would present a price nobody quoted as a quote, which is the exact
    defect the field was added to close. ``None`` must render as *unknown provenance*.

    Anything that is not a real ``bool`` is ``None`` rather than coerced. ``bool("false")`` is
    ``True``, and a slot arriving with a string here is a producer this package cannot identify;
    guessing which way it meant is how a stand-in becomes a quote.
    """
    value = read(slot, "fallback", None)
    return value if isinstance(value, bool) else None


def slot_fallback_reason(slot: Any) -> str | None:
    """WHY the exchange stood in, or ``None``.

    ``None`` whenever :func:`slot_fallback` is not ``True``: there is no reason to give for a bid
    that arrived, and a reason carried beside a real quote would be read as one. The vocabulary
    is the exchange's own (``exchange.auction.collect.FALLBACK_REASONS``) and is forwarded as the
    token it is — the sentence a shopper reads belongs to the screen, which is the only layer
    that knows how much of it to say.
    """
    if slot_fallback(slot) is not True:
        return None
    return text(read(slot, "fallback_reason", None)) or None


def slot_commitments(slot: Any) -> list[LabelledCommitment] | None:
    """R2's COMMITMENTS for one slot: what this store promises beside the price.

    ``None`` — not ``[]`` — when the exchange sent none, because an empty list on a screen
    reads as "this store committed to nothing" and the honest answer is "the exchange sent
    nothing here". A fallback bid is exactly that case and it is the common one.

    A commitment with no readable ``key`` is dropped, and only that one: there is nothing a
    shopper could read in it, and dropping its neighbours because of it would hide promises
    that were fine. Everything that survives carries :func:`commitment_label`.
    """
    raw = read(slot, "commitments", None)
    if raw is None or isinstance(raw, (str, bytes)):
        return None
    try:
        rows = list(raw)
    except TypeError:
        return None
    commitments: list[LabelledCommitment] = []
    for row in rows:
        key = text(read(row, "key", None))
        if not key:
            continue
        commitments.append(
            LabelledCommitment(
                key=key,
                value=plain(read(row, "value", None)),
                unit=text(read(row, "unit", None)) or None,
                label=commitment_label(row),
            )
        )
    return commitments or None


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
    #: The PLATFORM's registered domain for this store, or ``None`` — never ``""``.
    #:
    #: ``None`` is the only spelling of "the exchange published no registered domain for this
    #: store", and it is a different statement from a domain: it means the checkout permalink
    #: this slot leads to **cannot be pinned to a named host** (R3/D22/C10). It used to be
    #: ``""``, which reads identically to a domain that is present and blank, and that is
    #: precisely how the anti-spoofing cross-check came to be skipped on every slot of every
    #: deployment without anything saying so. ``""`` on the way in is normalised to ``None``
    #: by :func:`slot_store_domain` so the two spellings cannot both exist downstream, and
    #: ``protocol.schema.json`` now declares ``minLength: 1`` so a producer cannot emit one.
    store_domain: str | None = None
    #: WHICH catalogue thing (:func:`slot_product`), or ``None``. A reference, not a title.
    product: dict[str, Any] | None = None
    #: What the store is asking (:func:`slot_price`), or ``None``. Never a zero.
    price: dict[str, Any] | None = None
    #: What it promises beside the price (:func:`slot_commitments`), or ``None`` — not ``[]``.
    commitments: tuple[LabelledCommitment, ...] | None = None
    #: WHOSE PRICE this is (:func:`slot_fallback`). ``False`` a bid, ``True`` a stand-in,
    #: ``None`` a producer that did not say — three states, and a screen must not collapse them.
    fallback: bool | None = None
    #: WHY the exchange stood in (:func:`slot_fallback_reason`), or ``None``.
    fallback_reason: str | None = None

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
            "product": dict(self.product) if self.product is not None else None,
            "price": dict(self.price) if self.price is not None else None,
            # `None` and `[]` are different answers and stay different all the way to the
            # screen: nothing sent, versus a store that committed to nothing.
            "commitments": (
                [commitment.to_dict() for commitment in self.commitments]
                if self.commitments is not None
                else None
            ),
            "fallback": self.fallback,
            "fallback_reason": self.fallback_reason,
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
    commitments = slot_commitments(slot)
    return LabelledSlot(
        slot=text(read(slot, "slot", "")),
        bid_ref=text(read(slot, "bid_ref", "")),
        auction_id=text(read(slot, "auction_id", "")) or text(auction_id),
        fit_score=fit_score,
        labels=labels,
        labels_source=source,
        trust_summary=summary if isinstance(summary, dict) else {},
        store_domain=slot_store_domain(slot),
        product=slot_product(slot),
        price=slot_price(slot),
        commitments=None if commitments is None else tuple(commitments),
        fallback=slot_fallback(slot),
        fallback_reason=slot_fallback_reason(slot),
    )


def slot_rows(shortlist: Any) -> list[Any]:
    """The slots of a ``Shortlist`` (or a bare sequence of slots), **as the caller sent them**.

    Exported because a second consumer needs the same rows in the same order:
    :func:`buyer_svc.pitch.pitches_for` reads each slot's ``message`` — the shop-authored
    pitch that ``LabelledSlot`` deliberately does not carry — and its result is zipped with
    :func:`render_shortlist`'s by POSITION. Two copies of this extraction could disagree
    about how many rows there are, and a pitch that drifted one slot sideways would attach
    one store's promises to another store's price. One function, both callers.

    ``[]`` for anything that is not a readable sequence of slots, which is the same answer
    :func:`render_shortlist` has always given for one.
    """
    slots = read(shortlist, "slots", None)
    if slots is None:
        slots = shortlist
    if slots is None or isinstance(slots, (str, bytes)):
        return []
    try:
        return list(slots)
    except TypeError:
        return []


def render_shortlist(shortlist: Any, *, derive: bool = True) -> list[LabelledSlot]:
    """Every slot of a ``Shortlist`` (or a bare sequence of slots), labelled for display.

    The shortlist's ``auction_id`` is pushed down onto each slot: the contract carries it
    once at the top and :func:`~buyer_svc.accept.handoff.accept` needs it per slot, and
    making the caller re-attach it is how a slot ends up accepted against the wrong auction.
    """
    auction_id = text(read(shortlist, "auction_id", ""))
    return [label_slot(slot, auction_id=auction_id, derive=derive) for slot in slot_rows(shortlist)]
