"""The grounding corpus: everything a shopper's follow-up question may be answered FROM.

This module answers the only hard question in the follow-up feature — *what is an answer
allowed to be made of* — and it answers it by construction rather than by instruction. It
builds one flat, typed, attributed set of evidence items out of an auction the PLATFORM
holds, and nothing else in this package can reach a fact that did not come through here.

D55, spelled out for a chat answer
==================================
A scraped shop is an **organic** result carrying a pitch the platform wrote. An in-network
shop is **sponsored**: it buys the right to make its case in its own voice. The shortlist
already keeps those apart — ``product.identity`` is the platform's crawled name with its
``source`` and ``observed_at``, and ``message`` is the shop's own prose — and a follow-up
answer is the easiest place in the whole product to blur them, because the shopper asks one
question and gets one paragraph back.

So this module sorts every fact into one of **three voices**, and they are not
interchangeable:

:data:`VOICE_PLATFORM`
    The platform crawled it, the exchange published it, or the trust engine computed it.
    The platform authored these and can defend them. An answer may state them flatly.
:data:`VOICE_SHOP_CLAIM`
    A commitment the shop published, which the platform **recorded with its provenance** —
    the source, the reference, when it was observed, and the authority rank. The platform
    can say *that the shop claims it* and can say how well checked the claim is. It may not
    say the thing is true. So every one of these carries its provenance in
    :attr:`Evidence.attribution`, and :func:`buyer_svc.chat.answering.assemble` renders it
    as an attributed claim, never as a fact.
:data:`VOICE_SHOP`
    The shop's own prose — ``Bid.message``, written by that store's advocate. It is
    **never** an ``Evidence`` and never reaches an answer's sentences at all. It is carried
    on :attr:`SlotEvidence.shop_message`, whole and unedited, for the page to show beside
    the answer under the shop's own name. Restating it as the platform's answer is the
    laundering the two-voice split exists to prevent, and
    :func:`buyer_svc.chat.answering.screen_reasons` refuses a reply that reproduces a run of
    it even though the writer is never shown one.

Where the material comes from, and why the browser supplies none of it
======================================================================
:func:`corpus_for` takes the **exchange's own** live shortlist for an auction plus this
service's recorded ``ranked`` rows for it. ``buyer_svc.chat.routes`` fetches both itself
from the exchange client; the shopper's page sends an auction id and a question and nothing
more. That is deliberate and it is the difference between this route and
``POST /buyer/shortlist/render``, which takes a shortlist off the wire: a render is a
*display* of what the caller already has, while an answer is the platform **asserting**
something, and a page that could post its own slots could make the platform assert anything.
Nothing a shopper types can become a fact here, because there is no parameter it could
arrive in.

What is deliberately not built from
===================================
No clock, no environment, no randomness, no socket, and no catalogue. Two identical
auctions produce byte-identical corpora, which is what makes the served answer reproducible
and the screen in :mod:`buyer_svc.chat.answering` meaningful.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..accept._reading import read, text
from ..pitch.material import humanised, match_tokens, numeric_tokens, render_number
from ..pitch.writing import store_pitch_of

__all__ = [
    "MAX_EVIDENCE_PER_SLOT",
    "MAX_SLOTS",
    "TOPIC_COMMITMENT",
    "TOPIC_IDENTITY",
    "TOPIC_PRICE",
    "TOPIC_PROVENANCE",
    "TOPIC_RANKING",
    "TOPIC_SOURCING",
    "TOPIC_TRUST",
    "TOPICS",
    "VOICE_PLATFORM",
    "VOICE_SHOP",
    "VOICE_SHOP_CLAIM",
    "Corpus",
    "Evidence",
    "SlotEvidence",
    "corpus_for",
]

# ==============================================================================================
# vocabulary
# ==============================================================================================

#: The platform's own: crawled, published or computed by it. An answer may state these.
VOICE_PLATFORM = "platform"
#: The shop's published commitment, recorded by the platform WITH its provenance. An answer
#: may attribute these to the shop; it may not assert them.
VOICE_SHOP_CLAIM = "shop_claim"
#: The shop's own prose. Never an :class:`Evidence`; carried whole on the slot and quoted.
VOICE_SHOP = "shop"

#: Which family of platform-held facts an item belongs to. A question word that names one of
#: these (see ``buyer_svc.chat.answering.TOPIC_WORDS``) has named something the platform
#: holds, which is what separates an answerable question from a refusal.
TOPIC_IDENTITY = "identity"
TOPIC_PRICE = "price"
TOPIC_TRUST = "trust"
TOPIC_COMMITMENT = "commitment"
TOPIC_RANKING = "ranking"
TOPIC_PROVENANCE = "provenance"
TOPIC_SOURCING = "sourcing"

#: Every topic, in the order an assembled answer says them. Stable, so two identical
#: auctions render identically.
TOPICS: tuple[str, ...] = (
    TOPIC_IDENTITY,
    TOPIC_PRICE,
    TOPIC_COMMITMENT,
    TOPIC_TRUST,
    TOPIC_PROVENANCE,
    TOPIC_RANKING,
    TOPIC_SOURCING,
)

#: The most slots one corpus covers. R2 caps a shortlist at four; a longer one is truncated
#: rather than refused, because a shopper asking a question about the screen in front of them
#: should not be told the market answered too well.
MAX_SLOTS = 8

#: The most evidence items one slot contributes. A slot is a handful of published fields; a
#: hundred is a caller-shaped document, and a prompt built from one says nothing.
MAX_EVIDENCE_PER_SLOT = 40

#: Longest rendered evidence value. Longer is DROPPED rather than truncated — half a
#: commitment is a different commitment, exactly as ``pitch.material`` has it.
MAX_VALUE_CHARS = 120


# ==============================================================================================
# one fact
# ==============================================================================================


@dataclass(frozen=True, slots=True)
class Evidence:
    """One thing the platform holds about one slot, with where it got it.

    Attributes:
        slot: R2's slot name — ``fit``, ``value``, ``reliability``, ``specialist``.
        bid_ref: which bid this is evidence about.
        store_domain: the shop the slot is for, as the exchange named it.
        voice: :data:`VOICE_PLATFORM` or :data:`VOICE_SHOP_CLAIM`. Never
            :data:`VOICE_SHOP` — the shop's prose is not evidence and is not here.
        topic: which family of held facts this is, one of :data:`TOPICS`.
        key: what the fact is about, in English (``brand``, ``unit price``, ``free returns``).
        value: the platform's own rendering of it.
        attribution: where it came from, as a clause an answer can print verbatim. This is
            the field that makes a shop's claim readable AS a shop's claim.
        observed_at: when the platform observed it, when it knows. ``""`` when it does not,
            which is a different answer from a guessed timestamp.
    """

    slot: str
    bid_ref: str
    store_domain: str
    voice: str
    topic: str
    key: str
    value: str
    attribution: str
    observed_at: str = ""

    def line(self) -> str:
        """The item as one prompt line, with its voice and its attribution attached."""
        when = f", observed {self.observed_at}" if self.observed_at else ""
        return (
            f"- [{self.slot}] {self.voice} | {self.topic} | {self.key} = {self.value} "
            f"({self.attribution}{when})"
        )

    def sentence(self) -> str:
        """The item as the platform says it out loud, in the deterministic answer.

        A shop's claim is ALWAYS attributed here, in the same string as the value, so there
        is no rendering path on which a claim reaches a shopper looking like a checked fact.
        """
        if self.voice == VOICE_SHOP_CLAIM:
            return f"{self.key}: {self.value} — {self.attribution}"
        return f"{self.key}: {self.value}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "bid_ref": self.bid_ref,
            "store_domain": self.store_domain,
            "voice": self.voice,
            "topic": self.topic,
            "key": self.key,
            "value": self.value,
            "attribution": self.attribution,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class SlotEvidence:
    """Everything the platform holds about one shortlist slot, plus the shop's own words.

    ``facts`` and ``shop_message`` are separate attributes rather than one list, and that
    separation is the D55 mechanism rather than a formatting choice: there is no code path
    in :mod:`buyer_svc.chat.answering` that can put ``shop_message`` into a sentence,
    because the assembly walks ``facts`` and the prompt is built from ``facts``.
    """

    slot: str
    bid_ref: str
    store_domain: str
    title: str
    brand: str = ""
    facts: tuple[Evidence, ...] = ()
    shop_message: str | None = None

    def named(self) -> str:
        """How an answer names this slot, in the platform's own crawled words.

        The shop is named alongside the title because two shops on one shortlist really do
        crawl to near-identical titles — measured on the demo market, ``Milk Thistle
        Gummies`` at gaiaherbs.com sat two rows above ``Milk Thistle`` at paradiseherbs.com
        — and an answer that named only the title would have attributed one shop's price to
        the other in a shopper's eyes.
        """
        if self.title and self.store_domain:
            return f"{self.title} at {self.store_domain}"
        return self.title or self.store_domain or self.slot

    @property
    def vocabulary(self) -> frozenset[str]:
        found: set[str] = set()
        for item in self.facts:
            found |= match_tokens(item.key) | match_tokens(item.value)
        return frozenset(found)

    @property
    def handles(self) -> frozenset[str]:
        """Words that NAME this slot: its crawled title, its brand and its shop's domain.

        Used to decide which slot a question is about. Two deliberate exclusions:

        * the shop's own ``message``, because a shopper may not address a slot by prose the
          platform did not write; and
        * the R2 slot name. ``value`` and ``reliability`` are slot names *and* ordinary
          English about price and trust, so selecting on them would answer "is the
          reliability score any good?" about one card out of four.
        """
        return frozenset(
            match_tokens(self.title) | match_tokens(self.brand) | match_tokens(self.store_domain)
        )


@dataclass(frozen=True, slots=True)
class Corpus:
    """One auction's whole answerable material.

    Attributes:
        auction_id: the auction this is about.
        slots: one :class:`SlotEvidence` per shortlist slot, in the exchange's order. This
            module never reorders them — the shortlist's order is the exchange's published
            formula and an answer that reordered it would be arguing with the ranking.
        ranking_recorded: whether this service still holds the recorded ``ranked`` rows. When
            it does not, the ranking topic has no evidence and an answer about *why* one slot
            beat another says so rather than guessing.
    """

    auction_id: str = ""
    slots: tuple[SlotEvidence, ...] = ()
    ranking_recorded: bool = False

    @property
    def facts(self) -> tuple[Evidence, ...]:
        return tuple(item for slot in self.slots for item in slot.facts)

    @property
    def numbers(self) -> frozenset[str]:
        """Every number the platform holds here — the whole of what an answer may quote."""
        found: set[str] = set()
        for item in self.facts:
            found |= numeric_tokens(item.value)
        return frozenset(found)

    @property
    def vocabulary(self) -> frozenset[str]:
        """Every matchable word in the evidence, plus the words that name the slots."""
        found: set[str] = set()
        for slot in self.slots:
            found |= slot.vocabulary | slot.handles
        return frozenset(found)

    @property
    def topics_held(self) -> tuple[str, ...]:
        """Which families this corpus actually has evidence in, in :data:`TOPICS` order."""
        present = {item.topic for item in self.facts}
        return tuple(topic for topic in TOPICS if topic in present)

    @property
    def shop_messages(self) -> tuple[SlotEvidence, ...]:
        """The slots carrying a shop-authored message, for the page to quote."""
        return tuple(slot for slot in self.slots if slot.shop_message)


# ==============================================================================================
# building it
# ==============================================================================================


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _money(value: Any, currency: str) -> str:
    amount = _finite(value)
    if amount is None:
        return ""
    rendered = f"{amount:.2f}"
    return f"{rendered} {currency}" if currency else rendered


def _percent(value: Any) -> str:
    """A 0..1 score as ``77%``. A unit conversion of the platform's own number, not a fact."""
    number = _finite(value)
    if number is None or not 0.0 <= number <= 1.0:
        return ""
    return f"{number * 100:.0f}%"


@dataclass
class _Builder:
    """Accumulates one slot's evidence. A plain sink so each ``_read_*`` stays a statement."""

    slot: str
    bid_ref: str
    store_domain: str
    items: list[Evidence] = field(default_factory=list)

    def add(
        self,
        *,
        voice: str,
        topic: str,
        key: str,
        value: str,
        attribution: str,
        observed_at: str = "",
    ) -> None:
        """Keep one item, or drop it. A blank key or value is not evidence of anything."""
        key, value = key.strip(), value.strip()
        if not key or not value or len(value) > MAX_VALUE_CHARS:
            return
        if len(self.items) >= MAX_EVIDENCE_PER_SLOT:
            return
        self.items.append(
            Evidence(
                slot=self.slot,
                bid_ref=self.bid_ref,
                store_domain=self.store_domain,
                voice=voice,
                topic=topic,
                key=key,
                value=value,
                attribution=attribution,
                observed_at=observed_at,
            )
        )


def _read_identity(builder: _Builder, product: Any) -> tuple[str, str]:
    """The platform's crawled name for the thing, with the crawl that saw it.

    Returns ``(title, brand)``, which is how an answer names the slot and how a shopper
    addresses it by name. ``product.identity`` is the
    PLATFORM's voice by construction — ``apps/buyer/app/shortlist/shortlist.ts`` documents
    it as "a product name a person can read, in the PLATFORM's voice (D55)" — and the
    ``source``/``observed_at`` pair travels with it here for the same reason it travels
    onto the card: a name with no crawl behind it is a name a shopper cannot distrust.
    """
    identity = read(product, "identity", None)
    source = text(read(identity, "source", ""))
    observed = text(read(identity, "observed_at", ""))
    crawl = f"the platform's crawl {source}" if source else "the platform's catalogue"
    title = text(read(identity, "title", ""))
    brand = text(read(identity, "brand", ""))
    builder.add(
        voice=VOICE_PLATFORM,
        topic=TOPIC_IDENTITY,
        key="product",
        value=title,
        attribution=crawl,
        observed_at=observed,
    )
    builder.add(
        voice=VOICE_PLATFORM,
        topic=TOPIC_IDENTITY,
        key="brand",
        value=brand,
        attribution=crawl,
        observed_at=observed,
    )
    builder.add(
        voice=VOICE_PLATFORM,
        topic=TOPIC_IDENTITY,
        key="shop",
        value=builder.store_domain,
        attribution="the exchange's record of who bid",
    )
    return title, brand


def _read_price(builder: _Builder, price: Any) -> None:
    """The exchange's published offer. Its own reading, so it carries no store's badge."""
    if not isinstance(price, Mapping):
        return
    where = "the exchange's published offer for this auction"
    currency = text(price.get("currency"))
    unit = _money(price.get("unit_price"), currency)
    total = _money(price.get("total_price"), currency)
    if total and unit == total:
        builder.add(
            voice=VOICE_PLATFORM, topic=TOPIC_PRICE, key="price", value=total, attribution=where
        )
    else:
        builder.add(
            voice=VOICE_PLATFORM, topic=TOPIC_PRICE, key="unit price", value=unit, attribution=where
        )
        builder.add(
            voice=VOICE_PLATFORM,
            topic=TOPIC_PRICE,
            key="total price",
            value=total,
            attribution=where,
        )
    discount = price.get("discount")
    if isinstance(discount, Mapping):
        depth = render_number(discount.get("value"))
        kind = text(discount.get("type")).casefold()
        if depth:
            builder.add(
                voice=VOICE_PLATFORM,
                topic=TOPIC_PRICE,
                key="discount",
                value=f"{depth}% off" if kind in {"percent", "percentage"} else f"{depth} off",
                attribution=where,
            )
    expires = text(price.get("expires_at"))
    if expires:
        # The date, not the instant: the seconds put four more numbers into the material for
        # a writer to quote and a shopper reads "held until the 8th".
        builder.add(
            voice=VOICE_PLATFORM,
            topic=TOPIC_PRICE,
            key="offer held until",
            value=expires.split("T")[0],
            attribution=where,
        )


def _commitment_value(row: Any) -> str:
    value = read(row, "value", None)
    unit = text(read(row, "unit", ""))
    if isinstance(value, bool):
        rendered = "yes" if value else "no"
    elif isinstance(value, (int, float)):
        rendered = render_number(value)
    else:
        rendered = text(value)
    if not rendered:
        return ""
    return f"{rendered} {unit}" if unit else rendered


def _read_commitments(builder: _Builder, commitments: Any) -> None:
    """Each published promise, as a SHOP CLAIM with the platform's provenance on it.

    The provenance is not decoration. ``authority_rank`` and ``source`` are how well checked
    the claim is, and they are the difference between "the shop says it takes returns for 30
    days" and "returns are free for 30 days". This builder cannot produce the second
    sentence: :data:`VOICE_SHOP_CLAIM` items render through
    :meth:`Evidence.sentence`, which always prints the attribution.
    """
    if commitments is None or isinstance(commitments, (str, bytes, Mapping)):
        return
    try:
        rows = list(commitments)
    except TypeError:
        return
    for row in rows:
        key = humanised(text(read(row, "key", "")))
        value = _commitment_value(row)
        if not key or not value:
            continue
        provenance = read(row, "provenance", None)
        source = humanised(text(read(provenance, "source", "")))
        rank = render_number(read(provenance, "authority_rank", None))
        checked = f"{builder.store_domain} published this"
        if source:
            checked += f" as {source}"
        if rank:
            checked += f", authority rank {rank}"
        builder.add(
            voice=VOICE_SHOP_CLAIM,
            topic=TOPIC_COMMITMENT,
            key=key,
            value=value,
            attribution=checked,
            observed_at=text(read(provenance, "observed_at", "")),
        )


def _read_trust(builder: _Builder, summary: Any) -> None:
    """The trust engine's own snapshot. Never recomputed — R15 makes the served one final."""
    if not isinstance(summary, Mapping):
        return
    where = "the platform's trust snapshot for this shop"
    builder.add(
        voice=VOICE_PLATFORM,
        topic=TOPIC_TRUST,
        key="reliability",
        value=_percent(summary.get("score")),
        attribution=where,
    )
    builder.add(
        voice=VOICE_PLATFORM,
        topic=TOPIC_TRUST,
        key="reliability confidence",
        value=_percent(summary.get("confidence")),
        attribution=where,
    )
    if summary.get("low_data") is True:
        # The one fact in the material that argues AGAINST the candidate. It is here for
        # exactly that reason: an answer about reliability that omitted it would be the
        # platform overselling in its own voice.
        builder.add(
            voice=VOICE_PLATFORM,
            topic=TOPIC_TRUST,
            key="observations",
            value="few so far, so the score is held loosely",
            attribution=where,
        )
    dimensions = summary.get("dimensions")
    if isinstance(dimensions, Sequence) and not isinstance(dimensions, (str, bytes)):
        named = ", ".join(humanised(text(item)) for item in dimensions if text(item))
        builder.add(
            voice=VOICE_PLATFORM,
            topic=TOPIC_TRUST,
            key="reliability graded on",
            value=named,
            attribution=where,
        )
    if summary.get("available") is False:
        builder.add(
            voice=VOICE_PLATFORM,
            topic=TOPIC_TRUST,
            key="reliability",
            value="no snapshot available for this shop",
            attribution=where,
        )


def _read_labels(builder: _Builder, labels: Any) -> None:
    """R2's provenance labels — the badges already on the card, answerable in words."""
    if labels is None or isinstance(labels, (str, bytes, Mapping)):
        return
    try:
        rows = [text(item) for item in labels]
    except TypeError:
        return
    named = ", ".join(row for row in rows if row)
    builder.add(
        voice=VOICE_PLATFORM,
        topic=TOPIC_PROVENANCE,
        key="provenance labels",
        value=named,
        attribution="how well checked each thing on this card is",
    )


def _read_sourcing(builder: _Builder, slot: Any) -> None:
    """Whether this shop actually bid, or whether the platform is showing its crawl.

    The single most under-answered thing on the page. A fallback slot has no advocate, no
    commitments and no message; a shopper who asks "why does this one say so little?"
    deserves the exchange's own reason rather than silence.
    """
    fallback = read(slot, "fallback", None)
    reason = text(read(slot, "fallback_reason", ""))
    if fallback is True:
        value = "no — this shop did not bid, so this is the platform's crawled listing"
        if reason:
            value = f"{value} ({reason})"
        builder.add(
            voice=VOICE_PLATFORM,
            topic=TOPIC_SOURCING,
            key="bid in this auction",
            value=value,
            attribution="the exchange's record of who answered",
        )
    elif fallback is False:
        builder.add(
            voice=VOICE_PLATFORM,
            topic=TOPIC_SOURCING,
            key="bid in this auction",
            value="yes — this shop's own agent answered the solicitation",
            attribution="the exchange's record of who answered",
        )


def _read_ranking(builder: _Builder, ranked: Mapping[str, Any] | None) -> None:
    """Why this slot is where it is: the exchange's published components, unedited.

    ``rank_score`` and its components are the exchange's formula, recorded by this service
    when it opened the auction. Nothing here recomputes, normalises or re-weights them: an
    answer about ranking either quotes the published row or says there is none.
    """
    if not isinstance(ranked, Mapping):
        return
    where = "the exchange's published ranking, recorded when this auction ran"
    score = _finite(ranked.get("rank_score"))
    if score is not None:
        builder.add(
            voice=VOICE_PLATFORM,
            topic=TOPIC_RANKING,
            key="rank score",
            value=f"{score:.3f}",
            attribution=where,
        )
    components = ranked.get("components")
    if not isinstance(components, Mapping):
        return
    for name in sorted(str(key) for key in components):
        contribution = _finite(components.get(name))
        if contribution is None:
            continue
        builder.add(
            voice=VOICE_PLATFORM,
            topic=TOPIC_RANKING,
            key=f"{humanised(name)} contribution to the rank",
            value=f"{contribution:.3f}",
            attribution=where,
        )


def _ranked_rows(recorded: Any) -> dict[str, Mapping[str, Any]]:
    """``{bid_ref: row}`` out of this service's recorded ``POST /auctions`` answer.

    Read from the RECORD rather than from the live shortlist because the exchange publishes
    the components exactly once, in the answer it gives when the auction opens.
    ``buyer_svc.auctions.routes`` reads the same field the same way for the same reason.
    """
    response = read(recorded, "response", None)
    rows = read(response, "ranked", None)
    if rows is None or isinstance(rows, (str, bytes, Mapping)):
        return {}
    try:
        listed = list(rows)
    except TypeError:
        return {}
    found: dict[str, Mapping[str, Any]] = {}
    for row in listed:
        if not isinstance(row, Mapping):
            continue
        bid_ref = text(row.get("bid_ref"))
        if bid_ref and bid_ref not in found:
            found[bid_ref] = row
    return found


def corpus_for(shortlist: Any, *, recorded: Any = None) -> Corpus:
    """Build the whole answerable corpus for one auction. Cannot raise.

    ``shortlist`` is the exchange's live ``GET /auctions/{id}/shortlist`` body; ``recorded``
    is this service's kept ``POST /auctions`` answer, or ``None`` when the process that
    opened the auction was a different one. The second is optional and its absence is
    reported on :attr:`Corpus.ranking_recorded` rather than papered over — "we no longer
    hold the ranking" and "the ranking was all zeroes" are different answers to a shopper
    asking why one slot came first.
    """
    auction_id = text(read(shortlist, "auction_id", ""))
    rows = read(shortlist, "slots", None)
    if rows is None or isinstance(rows, (str, bytes, Mapping)):
        listed: list[Any] = []
    else:
        try:
            listed = list(rows)[:MAX_SLOTS]
        except TypeError:
            listed = []

    ranked = _ranked_rows(recorded)
    slots: list[SlotEvidence] = []
    for slot in listed:
        bid_ref = text(read(slot, "bid_ref", ""))
        builder = _Builder(
            slot=text(read(slot, "slot", "")) or "slot",
            bid_ref=bid_ref,
            store_domain=text(read(slot, "store_domain", "")),
        )
        title, brand = _read_identity(builder, read(slot, "product", None))
        _read_price(builder, read(slot, "price", None))
        _read_commitments(builder, read(slot, "commitments", None))
        _read_trust(builder, read(slot, "trust_summary", None))
        _read_labels(builder, read(slot, "provenance_labels", None))
        _read_sourcing(builder, slot)
        _read_ranking(builder, ranked.get(bid_ref))
        slots.append(
            SlotEvidence(
                slot=builder.slot,
                bid_ref=bid_ref,
                store_domain=builder.store_domain,
                title=title,
                brand=brand,
                facts=tuple(builder.items),
                # The shop's own words, VERBATIM or not at all. `store_pitch_of` is the
                # function `POST /buyer/shortlist/render` already carries this field with,
                # so the chat panel and the card quote byte-identical strings.
                shop_message=store_pitch_of(slot),
            )
        )
    return Corpus(auction_id=auction_id, slots=tuple(slots), ranking_recorded=bool(ranked))
