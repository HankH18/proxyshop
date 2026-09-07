"""The returning shopper population — what a simulated buyer answers, and why (R14).

R14 is the one signal in the whole trust apparatus that is **not the store talking about
itself**. Everything else the engine grades is the store's catalogue, the store's prose, or
the transaction record the store's own checkout produced. A routed buyer's answer to "did it
match the pitch?" is the outside opinion, and it is the reason this module exists: the route
that collects it has been built and served for some time and *nobody has ever called it*,
because a real prompt needs a shopper who comes back days after delivery.

This module is the population that comes back. It holds the decisions and none of the I/O:
:mod:`seed.population` drives the real HTTP routes with what is decided here.

The one property that decides whether this helps or hurts
---------------------------------------------------------
**Simulated feedback must be consequential, not decorative.** If these shoppers answered at
random, or uniformly positively, ``feedback_match`` would become noise with a confident-looking
Beta behind it: a store that over-promised would score the same as one that delivered, and every
downstream thing built on trust would be measuring nothing while looking precise. So every
answer here is derived from **what actually happened to that order**, and "what happened" is
read off the platform's own reconciler rather than invented here:

=========================  =================================================================
reconciled field           what the shopper saw
=========================  =================================================================
``fulfilment_missing``     nothing ever shipped
``price_honored``          promised 100, charged 130 (``price_comparable`` says it could be
                           compared at all — a webhook with no total is a gap, not a lie)
``discount_honored``       advertised 25% off, the minted code applied 5%
``shipped_on_time``        promised dispatch in 1 day, dispatched in 21
=========================  =================================================================

:func:`outcome_choice` is that table, and it is the whole of the mapping. There is no second
notion of "the order went well" anywhere in this package.

Noise, deliberately, and stated
-------------------------------
A population where every answer perfectly encodes the outcome is its own kind of lie — it makes
the trust engine look like a measurement instrument rather than an aggregate of opinions, and it
lets one observation carry a certainty no single shopper's opinion deserves. Two knobs, both
configurable, both defaulted here with the argument for the number written beside it:
:data:`DEFAULT_RESPONSE_RATE` and :data:`DEFAULT_NOISE_RATE`.

Deterministic (S4 / C9)
-----------------------
Every draw is a sha256-derived stream keyed on ``(seed, purpose, order_ref)`` — the construction
:mod:`sim.dishonest` and ``fixtures.generator`` already use — so one order's answer does not
depend on how many orders were decided before it, and the same seed reproduces the same
population exactly. No clock, no ``random`` module state, no I/O in this file at all.

Labelled as simulated (rule 3)
-------------------------------
These are not customers. Every identifier this module mints carries :data:`SIMULATED_PREFIX`,
and ``order_ref`` is copied verbatim onto the append-only ledger event the route writes, so a
trust posture built partly from synthetic feedback stays inspectable as such long after this
process has exited. :func:`is_simulated_reference` is the check, and
``seed.population`` asserts it on every event it sends and every event it gets back.
"""

from __future__ import annotations

__all__ = [
    "CHOICE_FORGIVING",
    "CHOICE_HARSH",
    "DEFAULT_NOISE_RATE",
    "DEFAULT_PROMISED_DISPATCH_DAYS",
    "DEFAULT_RESPONSE_RATE",
    "KEEPS_PROMISES",
    "OVER_PROMISES",
    "SIMULATED_BUYER_PREFIX",
    "SIMULATED_ORDER_PREFIX",
    "SIMULATED_PREFIX",
    "DeliveryProfile",
    "ShopperAnswer",
    "ShopperPolicy",
    "ShopperPolicyError",
    "answer_for",
    "delivered_outcome",
    "delivery_profile",
    "is_simulated_reference",
    "outcome_choice",
    "promised_dispatch_days",
    "shopper_rng",
]

import hashlib
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# --- what a simulated record is called ------------------------------------------------
#: Every identifier this population mints starts here. It reaches the append-only ledger on
#: the ``order_ref`` field of a ``feedback`` event and is never rewritten downstream, which is
#: what makes "this posture is partly synthetic" a fact an auditor can establish from the
#: record itself rather than from a README nobody kept.
SIMULATED_PREFIX = "sim"

#: The order reference prefix. Shaped to survive the feedback route's ``REFERENCE_PATTERN``
#: (``[A-Za-z0-9_.:#/-]``) — no spaces and no ``@``, so it can be neither a sentence nor an
#: email address.
SIMULATED_ORDER_PREFIX = f"{SIMULATED_PREFIX}-fb-"

#: The pseudonym prefix. R5 says stores never receive buyer identity; a simulated buyer has no
#: identity to leak, and saying so in the pseudonym is cheaper than explaining it later.
SIMULATED_BUYER_PREFIX = f"{SIMULATED_PREFIX}-buyer-"


def is_simulated_reference(value: Any) -> bool:
    """Whether ``value`` is an identifier this population minted.

    Used as an assertion rather than as a filter: :mod:`seed.population` refuses to send a
    feedback submission whose ``order_ref`` does not answer ``True`` here, so a future edit
    that drops the prefix fails the run instead of quietly writing unlabelled synthetic
    sentiment onto a ledger nobody can clean.
    """
    return isinstance(value, str) and value.startswith(SIMULATED_ORDER_PREFIX)


# --- the two knobs, and the argument for each default ---------------------------------
#: The fraction of prompted shoppers who answer at all.
#:
#: **Why 0.55.** R14's prompt is ONE single-select question, offered only to a buyer the network
#: itself routed, in a surface the network controls. That is not an emailed CSAT survey (the
#: well-known 5–15% band) and it is not a store-side review request; it is a one-tap in-app
#: prompt, which lands far higher. 0.55 sits deliberately in the middle of that band: high
#: enough that a bounded run moves ``feedback_match`` out of its Beta(2,2) prior, low enough
#: that "some shoppers never answer" is a visible property of the transcript rather than a
#: footnote. It is a guess, which is exactly why it is configurable — ``--response-rate 0.15``
#: reproduces the emailed-survey world, and the separation between a store that delivers and one
#: that does not takes longer rather than disappearing. That is the property worth showing.
DEFAULT_RESPONSE_RATE = 0.55

#: The fraction of *responding* shoppers whose answer does NOT track the outcome.
#:
#: **Why 0.15.** Real shoppers are inconsistent: some are harsh about a fine order, some
#: forgiving about a bad one, and the same objective outcome draws different verdicts from
#: different people. A population without this term would make the trust engine look more
#: precise than it is and would let a single observation carry a certainty no single opinion
#: deserves. 0.15 keeps the outcome the dominant term by a wide margin — an order that really
#: was overcharged still draws a complaint 85% of the time — while making a perfectly-encoding
#: population impossible. It is applied SYMMETRICALLY (the same rate in both directions), so
#: noise cannot silently bias a store up or down; see :func:`answer_for`.
DEFAULT_NOISE_RATE = 0.15

#: What a harsh shopper says about an order that was fine. It has to be an answer whose
#: ``matched_pitch`` differs from the truthful one — noise the trust engine cannot see is not
#: noise, it is decoration.
CHOICE_HARSH = "not_as_described"

#: What a forgiving shopper says about an order that was not fine. The mirror of
#: :data:`CHOICE_HARSH`, and the reason the noise term is symmetric.
CHOICE_FORGIVING = "yes_as_described"


class ShopperPolicyError(ValueError):
    """A population nobody could defend: a rate outside ``[0, 1]``, or not a number."""


def _rate(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShopperPolicyError(f"{name} must be a number in [0, 1], got {type(value).__name__}")
    rate = float(value)
    if not 0.0 <= rate <= 1.0:
        raise ShopperPolicyError(f"{name} must be in [0, 1], got {rate}")
    return rate


@dataclass(frozen=True, slots=True)
class ShopperPolicy:
    """How this population behaves, as one object that can be printed in a report.

    Both fields are on the record for the same reason the seed is: a trust posture built partly
    from synthetic feedback is only interpretable next to the population that produced it, and a
    reader who cannot see the response rate cannot tell a quiet store from a well-behaved one.
    """

    response_rate: float = DEFAULT_RESPONSE_RATE
    noise_rate: float = DEFAULT_NOISE_RATE

    def __post_init__(self) -> None:
        object.__setattr__(self, "response_rate", _rate(self.response_rate, "response_rate"))
        object.__setattr__(self, "noise_rate", _rate(self.noise_rate, "noise_rate"))

    def to_json(self) -> dict[str, Any]:
        return {"response_rate": self.response_rate, "noise_rate": self.noise_rate}


@dataclass(frozen=True, slots=True)
class ShopperAnswer:
    """One shopper's decision about one order.

    ``outcome_choice`` is what the reconciled verdict says happened; ``choice`` is what the
    shopper actually said. They differ exactly when ``noisy`` is true, which is what lets the
    report state how much of the signal is opinion rather than measurement.
    """

    order_ref: str
    responded: bool
    outcome_choice: str
    choice: str | None
    noisy: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "order_ref": self.order_ref,
            "responded": self.responded,
            "outcome_choice": self.outcome_choice,
            "choice": self.choice,
            "noisy": self.noisy,
            "simulated": True,
        }


# --- the deterministic streams ---------------------------------------------------------
def shopper_rng(seed: int, *parts: object) -> random.Random:
    """A deterministic stream for one decision.

    sha256-derived rather than ``Random(tuple)``, following :func:`sim.dishonest._rng` and
    ``fixtures.generator``: ``Random(str)`` is stable in CPython but not contractually so, and
    hashing the parts keeps each order's stream independent of how many orders were decided
    before it. That independence is what makes "adding one shopper does not change another
    shopper's answer" true rather than merely hoped for — and it is what a bisecting reader
    needs when a transcript diff has to mean something.
    """
    material = ":".join(str(part) for part in (seed, *parts))
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


# --- what happened to the order --------------------------------------------------------
def outcome_choice(reconciled: Mapping[str, Any]) -> str:
    """The answer the reconciled verdict justifies, before any noise is applied.

    ``reconciled`` is the ``payload`` of a ``reconciled`` event as
    ``trust.reconcile.reconciled_event`` builds it — the platform's own measurement of the
    order, not a parallel notion of "went well" invented here.

    The order of the tests is the substance:

    * **nothing arrived** outranks everything. A buyer with no parcel is not grading a pitch.
    * **a broken money promise** outranks lateness. An order that was both overcharged and late
      says ``not_as_described``, because the pitch was broken on price; lateness is a different
      dimension (``shipped_on_time``) graded from fulfilment stamps rather than from an opinion,
      and folding it into ``matched_pitch`` would charge a catalogue-accuracy penalty for a
      courier's bad week.
    * **late but accurate** is ``as_described_but_late``, whose ``matched_pitch`` is **True** —
      the option table says so in as many words, and this function does not get a second opinion.

    ``comparable`` is always read alongside the verdict. A ``price_honored: False`` on an
    *incomparable* field means "the record did not say", not "the store overcharged", and a
    shopper who complained about that would be complaining about a malformed webhook.

    ``wrong_item`` is in the served option table and this population never chooses it. That is
    deliberate and is a limit worth stating rather than papering over: the reconciler carries
    ``product_ref`` from the accepted offer and never observes what was actually in the box, so
    nothing in this system measures item identity. A population that emitted ``wrong_item``
    would be inventing a fact no component checked.
    """
    if bool(reconciled.get("fulfilment_missing")):
        return "never_arrived"
    if bool(reconciled.get("price_comparable")) and not bool(reconciled.get("price_honored")):
        return "not_as_described"
    if bool(reconciled.get("discount_comparable")) and not bool(reconciled.get("discount_honored")):
        return "not_as_described"
    if bool(reconciled.get("delivery_comparable")) and not bool(reconciled.get("shipped_on_time")):
        return "as_described_but_late"
    return "yes_as_described"


def _served_choice_ids(prompt: Mapping[str, Any]) -> tuple[str, ...]:
    options = prompt.get("options")
    if not isinstance(options, (list, tuple)) or not options:
        raise ShopperPolicyError(
            "the served prompt offered no options, so there is nothing a shopper could answer"
        )
    ids: list[str] = []
    for option in options:
        if not isinstance(option, Mapping) or not str(option.get("id") or "").strip():
            raise ShopperPolicyError(f"the served prompt carries an unusable option: {option!r}")
        ids.append(str(option["id"]))
    return tuple(ids)


def answer_for(
    prompt: Mapping[str, Any],
    reconciled: Mapping[str, Any],
    policy: ShopperPolicy,
    seed: int,
    order_ref: str,
) -> ShopperAnswer:
    """What this shopper does about this order, given the prompt the service actually served.

    ``prompt`` is the body ``POST /buyer/feedback/prompt`` returned, and the chosen option is
    checked against **its** option list rather than against a copy of the vocabulary kept here.
    That is the point of taking it as an argument: if the served option table ever changes, this
    population fails loudly on the run instead of posting a choice the route will refuse — and a
    second copy of the option ids in this file would be the drift D30 exists to prevent.

    Two independent draws, both keyed on ``order_ref``:

    * **respond** — ``response_rate`` of prompted shoppers answer at all. A shopper who does not
      answer produces no ledger event and no observation; silence is not a positive review.
    * **noise** — ``noise_rate`` of those who answer say the opposite of what the outcome
      justifies. Symmetric: an outcome-positive order becomes :data:`CHOICE_HARSH` and an
      outcome-negative one becomes :data:`CHOICE_FORGIVING`, so the term cannot bias a store in
      one direction.

    Separate streams, so changing the response rate does not reshuffle which shoppers are harsh.
    """
    served = _served_choice_ids(prompt)
    truthful = outcome_choice(reconciled)
    if truthful not in served:
        raise ShopperPolicyError(
            f"the outcome justifies the answer {truthful!r}, which the served prompt does not "
            f"offer (it offers {list(served)}). The population will not substitute a different "
            "answer for the one the evidence supports."
        )

    if shopper_rng(seed, "respond", order_ref).random() >= policy.response_rate:
        return ShopperAnswer(order_ref, False, truthful, None, False)

    noisy = shopper_rng(seed, "noise", order_ref).random() < policy.noise_rate
    if not noisy:
        return ShopperAnswer(order_ref, True, truthful, truthful, False)

    flipped = CHOICE_HARSH if truthful in _POSITIVE_CHOICES else CHOICE_FORGIVING
    if flipped not in served:
        raise ShopperPolicyError(
            f"the noise term needs {flipped!r} and the served prompt does not offer it"
        )
    return ShopperAnswer(order_ref, True, truthful, flipped, True)


#: The served choices whose ``matched_pitch`` is True. Kept here — and only here — because the
#: noise term has to know which direction it is flipping. ``as_described_but_late`` is in this
#: set on purpose: the option's own label says the pitch WAS matched and the parcel was late.
_POSITIVE_CHOICES = frozenset({"yes_as_described", "as_described_but_late"})


# --- how a store behaves after the sale -------------------------------------------------
@dataclass(frozen=True, slots=True)
class DeliveryProfile:
    """What a store does with the promise it made, expressed as a rate rather than a label.

    A rate and not a boolean, because a store that breaks every promise and a store that keeps
    every one are both easier to separate than reality is, and a demonstration that only works
    on absolutes is a demonstration of nothing. The separation these produce comes from the
    RATE at which promises are kept, which is what a trust engine is for.
    """

    name: str
    breach_rate: float

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "breach_rate": self.breach_rate}


#: A store that means it. 5% of its orders still go wrong — a courier misses a day, a price
#: changes mid-checkout — because a store with a literally perfect record would let the
#: population separate two stores on a difference no real market ever offers.
KEEPS_PROMISES = DeliveryProfile("keeps_promises", 0.05)

#: A store whose pitch is better than its fulfilment. 80% and not 100% for the same reason: the
#: point being demonstrated is that a store which pitches well and delivers badly ends up ranked
#: below one that does both, and that has to hold when the bad store sometimes gets it right.
OVER_PROMISES = DeliveryProfile("over_promises", 0.80)


def delivery_profile(store: Mapping[str, Any]) -> DeliveryProfile:
    """Which profile the approved roster gives this store.

    Read out of ``fixtures/manifest.json``'s own ``honest`` flag, never decided here, for the
    reason :mod:`sim.dishonest` gives at length: "the store that delivers badly is caught" is a
    circular claim when the simulator picks which store that is. The manifest's ``aggressive``
    persona corroborates the flag claim by claim for ``store-brightbean`` — ``unit_price``
    ``18.50`` noted "checkout charges 22.50", ``dispatch_window`` "ships within 24 hours" marked
    ``truthful: false`` — so the profile below is a reading of an approved document rather than
    a second opinion about it.
    """
    return KEEPS_PROMISES if bool(store.get("honest", True)) else OVER_PROMISES


#: The dispatch promise a store makes when the approved document states none for it. Three days
#: is the fixture intent's own "shipped this week" read conservatively; it is a promise the
#: population makes on the store's behalf and is labelled as such wherever it is reported.
DEFAULT_PROMISED_DISPATCH_DAYS = 3.0

#: Reads a dispatch promise out of an approved persona claim. Strict on purpose: it matches a
#: leading count and a unit and nothing else, so "ships within 24 hours" and "ships within 2
#: business days" are promises and "ships soon" is not one. A parser that guessed would put a
#: number the human never approved into the promise the store is then graded against.
_DISPATCH_PATTERN = re.compile(
    r"(?P<count>\d+(?:\.\d+)?)\s*(?P<unit>hours?|days?|business\s+days?)", re.IGNORECASE
)


def promised_dispatch_days(manifest: Mapping[str, Any], store_id: str) -> float:
    """The dispatch window this store promises, in days, from its approved persona.

    Falls back to :data:`DEFAULT_PROMISED_DISPATCH_DAYS` for a store the manifest gives no
    persona — which is most of the roster — and never invents a promise for a store whose
    persona states one it cannot parse: an unparseable ``dispatch_window`` raises rather than
    silently becoming the default, because the default would then be graded as if a human had
    approved it.
    """
    personas = manifest.get("personas")
    if not isinstance(personas, Mapping):
        return DEFAULT_PROMISED_DISPATCH_DAYS
    for persona in personas.values():
        if not isinstance(persona, Mapping) or str(persona.get("store_id") or "") != store_id:
            continue
        for claim in persona.get("scripted_claims") or ():
            if not isinstance(claim, Mapping) or claim.get("claim_type") != "dispatch_window":
                continue
            text = str(claim.get("value") or "")
            match = _DISPATCH_PATTERN.search(text)
            if match is None:
                raise ShopperPolicyError(
                    f"the approved persona for {store_id!r} promises {text!r}, which states no "
                    "dispatch window this population can grade. Refusing to substitute the "
                    "default: a store must not be graded against a promise no human approved."
                )
            count = float(match.group("count"))
            unit = match.group("unit").lower()
            return count / 24.0 if unit.startswith("hour") else count
    return DEFAULT_PROMISED_DISPATCH_DAYS


#: How far above the promised total an over-promising checkout charges. The same band
#: :func:`sim.dishonest._incidentals` draws its ``gap_ratio`` from, and for the same reason: the
#: manifest fixes the DIRECTION of the lie (``honest: false``, "checkout charges 22.50" against
#: a pitched 18.50 — 21.6% over) and the seed fixes the magnitude. The approved note sits inside
#: this band, which is the point of choosing it rather than a rounder one.
PRICE_GAP_BAND = (0.10, 0.45)

#: How many days past the promised window an over-promising store dispatches. Again the band
#: :func:`sim.dishonest._incidentals` uses, so the two adversaries in this package draw their
#: magnitudes from one published range rather than from two that can drift apart.
DAYS_LATE_BAND = (1, 9)

#: What fraction of its promised window a store that keeps promises actually takes. Never 1.0:
#: a store that dispatches at exactly the promised instant on every order is a store whose
#: ``shipped_on_time`` verdict is decided by a floating-point comparison.
ON_TIME_BAND = (0.15, 0.85)

#: How much of the advertised discount an over-promising checkout really applies. The manifest's
#: aggressive persona is explicit — 25% advertised, "the minted code applies 5%", i.e. a fifth —
#: and this band is centred on that reading rather than replacing it.
DISCOUNT_APPLIED_BAND = (0.10, 0.35)


def delivered_outcome(
    profile: DeliveryProfile,
    promise: Mapping[str, Any],
    seed: int,
    order_ref: str,
) -> dict[str, Any]:
    """What the merchant actually did with one order, as plain data.

    ``promise`` is the accepted offer's own numbers — ``total_price``, the advertised
    ``discount_percentage`` and ``delivery_estimate_days`` — so what is graded is the promise the
    market really made in this run, not a promise reconstructed afterwards.

    Returns ``{"breached", "charged_total", "applied_discount_percentage", "dispatch_days"}``.
    A breach is decided ONCE per order, from ``profile.breach_rate``, and then applies to every
    graded field of that order: a store having a bad day charges more *and* ships late, which is
    what makes the resulting complaint one complaint rather than three independent coin flips
    that happen to land together.
    """
    rng = shopper_rng(seed, "delivery", order_ref)
    breached = rng.random() < profile.breach_rate
    promised_total = float(promise.get("total_price") or 0.0)
    promised_days = float(promise.get("delivery_estimate_days") or 0.0)
    promised_discount = promise.get("discount_percentage")

    if breached:
        gap = rng.uniform(*PRICE_GAP_BAND)
        charged = round(promised_total * (1.0 + gap), 2)
        dispatch = promised_days + float(rng.randint(*DAYS_LATE_BAND))
        applied = (
            round(float(promised_discount) * rng.uniform(*DISCOUNT_APPLIED_BAND), 2)
            if promised_discount is not None
            else None
        )
    else:
        charged = round(promised_total, 2)
        dispatch = round(promised_days * rng.uniform(*ON_TIME_BAND), 4)
        applied = float(promised_discount) if promised_discount is not None else None

    return {
        "breached": breached,
        "charged_total": charged,
        "applied_discount_percentage": applied,
        "dispatch_days": dispatch,
    }
