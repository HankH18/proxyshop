"""Eligibility filters — everything that decides whether a candidate is scored at all.

R19 is explicit that a hard constraint is a **filter**, never a weighted term, and R12 is
explicit that a blacklist read **fails closed**. Both rules say the same thing about order:
a candidate that the filters cannot positively clear is excluded *before* the published
formula runs, so no `rank_score` exists for it at all. A number that existed but was ignored
would be a number some later reader could sort by; there is no such number here.

The six filters, and why each one denies rather than discounts:

* **Blacklist (R12).** A blacklisted store may not participate at any price. A store whose
  status cannot be read — no row in the snapshot, or a flag that is not a boolean — is
  denied on the same footing: an unreadable answer is not a permissive answer.
* **Expiry.** An offer whose `expires_at` has passed against the caller-supplied `now` is
  not an offer. `now` is passed in rather than read from the clock so a shortlist is
  reproducible from its inputs alone.
* **Checkout domain (C10/D22).** The offer's checkout URL must be on the seller's registered
  `store_domain`. The comparison is `checkout.domain.is_on_domain`, imported rather than
  restated — that module is where the release-blocker rule lives, and a security boundary
  with two implementations is a boundary with two answers.
* **Hard constraints (R19).** A constraint is satisfied by a `verified` supporting claim and
  by nothing else. Ambiguous, unsupported and contradicted evidence are all *absent*
  evidence as far as this filter is concerned — they are dropped before the constraint is
  decided, so an unproven claim can never carry a candidate through.

* **Organic relevance (D55).** A row the PLATFORM manufactured — a ``fallback``, where the
  store never bid and the exchange stood its list price up in its place — must be about what
  the shopper asked, judged by :func:`organic_relevance_reason` against the platform's own
  crawled identity of the product. It is the one filter here that is about the QUESTION rather
  than about the store, and the only one that never touches a row a store actually bid: see
  that function for the four conditions that must all hold before it refuses anything.

* **The buyer's budget.** A ``price_usd`` bound (``lte``/``gte``) is decided against the
  OFFER'S OWN PRICE by :func:`budget_reasons`, and is the one filter here that is not about
  evidence at all. See its docstring for why it cannot be an R19 hard constraint: a verified
  ``price_usd`` claim describes the store's CATALOGUE, and measured through ``POST /auctions``
  a store whose catalogue price of 40.00 verified bid **180.00 under a 50.00 ceiling and was
  shortlisted**. An offer's price is asserted by the bidder and nothing could verify it, so it
  is decided structurally and never counts towards ``verified_hard_fit_count``.

Deciding the constraint itself is delegated to
:class:`~exchange.retrieval.criteria.HardCriterion`, which is where the op vocabulary and
the undecidable-is-never-satisfied rule already live. Restating either here would be a
second answer to a question that has one.

WHOSE "verified" (ESC-020)
--------------------------
This module used to answer that question with `claim["status"]` — a field on a document the
BIDDER wrote, which the published `Claim` does not declare (`additionalProperties: false`)
and which nothing on the auction path validated. A store wrote the string and satisfied any
hard constraint it liked; two identical stores, one adding it, and the liar took the whole
shortlist while the honest one was excluded `hard_constraint_unsatisfied`. It moved
`verified_hard_fit_count` too, which is the first published tie-break (D13).

So the status is now read through :func:`~exchange.ranking.attestation.attested_status`, which
returns a verdict only when this exchange's own MAC over the claim holds. A store-supplied
`status`, and a store-supplied `exchange_verification` block, are both read by nothing. The
producer of real verdicts is :mod:`exchange.ranking.verification`, which runs
:func:`claim_verification.verify` against the exchange's catalog snapshot — so an exchange
that holds no catalog snapshot satisfies no hard constraint and shortlists nobody, the same
direction an exchange with no trust snapshot already fails in.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from claim_verification.statuses import DECIDED_STATUSES

from ..checkout.codes import UnusableOffer, expiry_epoch
from ..checkout.domain import is_on_domain
from ..retrieval.criteria import (
    BUDGET_FIELDS,
    BUDGET_OPS,
    HardCriterion,
    MalformedIntent,
    is_budget_bound,
)

# `NAMING_MODIFIERS`, `query_noun_phrases` and `shopper_named_the_product` are DEFINED there
# and re-exported here (see `__all__`), so every existing `from exchange.ranking.filters import
# ...` still resolves. They moved down because `exchange.retrieval.roster` needs the same
# keep-rule at ROSTER time — before a shop's one slot is staked on a product — and `retrieval`
# may not import `ranking`: this module already imports `..retrieval.relevance`, and the
# reverse edge would be an inverted layer AND an import cycle. See `identity_off_topic`.
from ..retrieval.relevance import (
    NAMING_MODIFIERS,
    TopicalRelevance,
    identity_off_topic,
    query_noun_phrases,
    shopper_named_the_product,
)
from .attestation import ATTESTATION_FIELD, attested_status
from .reasons import (
    REASON_BLACKLIST_UNREADABLE,
    REASON_BLACKLISTED,
    REASON_ELIGIBILITY_DENIED,
    REASON_EXPIRED,
    REASON_HARD_CONSTRAINT,
    REASON_MALFORMED,
    REASON_OFF_DOMAIN,
    REASON_OFF_TOPIC_ORGANIC,
    REASON_OVER_BUDGET,
    REASON_PRICE_UNREADABLE,
    REASON_UNDECIDABLE_INTENT,
    REASON_UNEVIDENCED_CONSTRAINT,
)

#: The one claim status that counts as supporting evidence (R19).
VERIFIED = "verified"

_MISSING = object()


def read(obj: Any, name: str, default: Any = None) -> Any:
    """Read `name` off a mapping or an object. Shape-agnostic: the callers of this package
    hand it plain dicts today and pydantic records tomorrow, and neither spelling changes
    what the rules mean."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    value = getattr(obj, name, _MISSING)
    return default if value is _MISSING else value


def _enum_value(raw: Any) -> Any:
    """Unwrap an enum member to its value, leaving everything else alone."""
    return getattr(raw, "value", raw)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------------
# R12 — the blacklist, and the unreadable blacklist
# ---------------------------------------------------------------------------------
def blacklist_reason(store_id: str, trust_snapshot: Any) -> str | None:
    """The reason this store may not participate, or `None` when it may.

    Three denials, deliberately collapsed into one function so no caller can disagree with
    another about which of them is fatal:

    * the snapshot has no row for the store   -> unreadable, denies;
    * the row's `blacklisted` is not a bool   -> unreadable, denies (this is where `None`
      lands, which is the shape a failed upstream read actually arrives in);
    * the row's `blacklisted` is `True`       -> blacklisted, denies.
    """
    row = trust_row(store_id, trust_snapshot)
    if row is None:
        return (
            f"{REASON_BLACKLIST_UNREADABLE}: no trust snapshot row for {store_id!r}, so its "
            f"blacklist status could not be established; failing closed (R12)"
        )
    flag = read(row, "blacklisted", _MISSING)
    if not isinstance(flag, bool):
        return (
            f"{REASON_BLACKLIST_UNREADABLE}: {store_id!r} has a blacklist flag of "
            f"{'<absent>' if flag is _MISSING else repr(flag)}, which is not a readable "
            f"yes/no; failing closed (R12)"
        )
    if flag:
        return f"{REASON_BLACKLISTED}: {store_id!r} is blacklisted and may not participate (R12)"
    return None


def trust_row(store_id: str, trust_snapshot: Any) -> Any | None:
    """One store's row out of the snapshot, or `None` when there is not one."""
    if trust_snapshot is None:
        return None
    if isinstance(trust_snapshot, Mapping):
        return trust_snapshot.get(store_id)
    if isinstance(trust_snapshot, Sequence) and not isinstance(trust_snapshot, (str, bytes)):
        for row in trust_snapshot:
            if str(read(row, "store_id", "")) == store_id:
                return row
        return None
    return getattr(trust_snapshot, store_id, None)


def eligibility_source_reason(source: Any, store_id: str) -> str | None:
    """The reason an injected `SellerEligibility` source denies this store, or `None`.

    The source is OPTIONAL. `rank()` derives the blacklist from the trust snapshot it is
    handed — that is the published four-argument surface every caller uses — and this hook
    exists so a deployment that also has a live eligibility service can hand it in without
    changing that surface. When it is handed in, every unhappy path denies: an unsupported
    interface version, a raised read, an empty answer and an unrecognised status are all
    `UNAVAILABLE`, and `UNAVAILABLE` is a denial (R12).
    """
    if source is None:
        return None
    # Imported here rather than at module scope: the eligibility port is only consulted when
    # a caller opts in, and an optional collaborator should not be an unconditional import.
    from ..eligibility import read_eligibility, speaks_supported_interface

    if not speaks_supported_interface(source):
        declared = getattr(source, "interface_version", None)
        return (
            f"{REASON_ELIGIBILITY_DENIED}: the eligibility source declares interface version "
            f"{declared!r}, which this exchange does not speak; failing closed (R12)"
        )
    decision = read_eligibility(source, store_id)
    if decision.eligible:
        return None
    return f"{REASON_ELIGIBILITY_DENIED}: {decision.reason or decision.status}"


# ---------------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------------
def expiry_reason(offer: Any, now: float) -> str | None:
    """The reason this offer is too old to rank, or `None` when it is live.

    The instant is read by :func:`~exchange.checkout.codes.expiry_epoch` — imported, not
    restated, for the same reason `domain_reason` imports `is_on_domain`. That function is
    where T-182 already ended this exact contradiction one file over: `contracts.Offer`
    types `expires_at` as `str | None` with `format: date-time` and `validate_bid` parses it
    with `contracts.parse_timestamp`, while THIS filter parsed it with bare `float()`. So
    the only expiry spelling the schema permits was the one the ranker refused, and it
    refused it as `expired_offer` — measured:
    `expiry_reason({"expires_at": "2030-01-01T00:00:00Z"}, ...)` came back
    "not a readable instant", so every bid a real hosted store agent produces
    (`store_agent.runtime.context.offer_expires_at` returns `str | None`) was excluded from
    every shortlist. Nothing saw it because every fixture in this tree, the frozen suite
    included, uses a float epoch.

    Fail-closed hid it rather than excusing it: an eligibility gate that refuses every
    conformant offer is not a strict gate, it is a shortlist that is always empty.

    **`now` is the instant the offer had to be STANDING AT, and the comparison is inclusive
    of it.** An offer whose last valid instant is exactly `now` stood for every moment up to
    `now`, so it is live here rather than expired. That one character used to be `<=`, and
    with it this filter voided every honest bid in an auction whose fan-out overran:
    :meth:`store_agent.runtime.context.AuctionContext.offer_expires_at` stamps an offer with
    the auction's own `respond_by` when the merchant's context states no expiry — which, when
    this was measured, every store context in `deploy/demo` did (they now state a validity
    WINDOW, which is a separate repair and does not retire this one: a merchant may still state
    nothing, and a store that does must not be voided for it) — and the served path judged that
    stamp against a
    clock read taken AFTER the fan-out returned. Measured on the deployed droplet, 2 runs in
    12::

        expired_offer: the offer expired at 1788872951.113, which is not after the
        caller-supplied now=1788872951.1345184

    Twenty-one milliseconds, and the shopper's shortlist held nothing but the exchange's own
    list-price stand-ins — which survive because `auction.collect.fallback_expires_at` gives
    them `deadline + 900 s`. Passing the right `now` is half the repair and lives in
    :func:`~exchange.ranking.serving.rank_auction`, which caps it at the auction's published
    deadline; this half is the boundary, because a cap at the deadline is worth nothing while
    an offer stamped AT the deadline still reads as dead.

    **Deliberately different from `contracts.boundary`'s `<=`, and the two are asking
    different questions.** The boundary asks "is this offer live at the instant I am
    validating it", a point question about a bid arriving now. This filter asks "did this
    offer stand through the auction it was solicited for", an interval question decided after
    that auction is over — and an offer covering the closed interval up to the close covered
    the auction. A bid admitted here on the boundary instant is still refused at accept time
    if a shopper clicks it later: `checkout.validity.window_reason` opens the window only on
    `expires_at > now`, pre-mint, and answers 409 rather than minting a code.

    Nothing above this line moves. An absent `expires_at` and a non-finite one still fail
    closed, and they have to: the accept path refuses neither — measured, an offer with no
    expiry mints a code against the flat 48-hour ceiling and a NaN one mints a live `PSX-`
    code — so this filter is the only gate in the system that requires an offer to say when
    it stops standing.
    """
    raw = read(offer, "expires_at", _MISSING)
    if raw is _MISSING or raw is None:
        return (
            f"{REASON_EXPIRED}: the offer carries no expires_at, so it cannot be shown to be "
            f"live; failing closed"
        )
    try:
        expires_at = expiry_epoch(raw)
    except (UnusableOffer, TypeError, ValueError):
        return f"{REASON_EXPIRED}: expires_at {raw!r} is not a readable instant; failing closed"
    if not math.isfinite(expires_at):
        # NaN in particular: EVERY comparison against it is False, so a plain `expires_at <
        # now` test says "not expired" and a NaN-expiry offer walks straight into a shortlist
        # slot. An instant that is not a finite number is not an instant.
        return (
            f"{REASON_EXPIRED}: expires_at {raw!r} is not a finite instant, so the offer "
            f"cannot be shown to be live; failing closed"
        )
    if expires_at < now:
        return (
            f"{REASON_EXPIRED}: the offer expired at {expires_at!r}, which is before the "
            f"instant it had to be standing at, now={now!r}"
        )
    return None


# ---------------------------------------------------------------------------------
# C10 / D22 — the checkout URL never leaves the registered seller domain
# ---------------------------------------------------------------------------------
def domain_reason(candidate: Any, offer: Any) -> str | None:
    """The reason this offer's checkout URL is refused, or `None` when it is on-domain.

    The comparison itself is :func:`~exchange.checkout.domain.is_on_domain` — imported, not
    restated. C10/D22 is a release-blocker boundary, and a boundary with two implementations
    is a boundary with two answers: this filter's first draft compared hosts itself and had
    already drifted from the published rule in two ways (it admitted a scheme-relative
    ``//host/path`` URL that no browser can be redirected to, and it refused the rooted
    ``host.`` spelling that the published rule normalises). Neither disagreement was visible
    to any test. The import is what makes the next drift impossible rather than merely
    unlikely.

    What this function still owns is what "absent" means HERE. `checkout.domain` deliberately
    leaves that to its caller, because a list-price fallback bid (R10) arrived with no
    checkout URL at all and refusing it as a spoof would have refused every fallback the
    exchange built for itself. For ranking the answer is deny anyway: a candidate whose
    checkout destination cannot be established is one the buyer cannot be sent to, and
    admitting it would put an unreachable offer in a shortlist slot.

    That deny is why the fallback's URL is supplied UPSTREAM rather than excused here.
    `ranking.candidates.candidate_from_entry` now gives a fallback entry's offer the checkout
    URL the platform's registered-domain lookup implies, before this filter ever sees the
    candidate — so a fallback for a registered store reaches this function with a URL and is
    judged on the same comparison as every other bid, which is what lets a silent store reach
    the shortlist at all (R10). A fallback for a store the registry holds no domain for is
    still completed with nothing and is still denied here, and that is the fail-closed
    direction: no trusted domain means nothing to compare a destination against.
    """
    registered = read(candidate, "store_domain", None) or read(candidate, "domain", None)
    url = read(offer, "checkout_url", None) or read(candidate, "checkout_url", None)
    if not registered:
        return (
            f"{REASON_OFF_DOMAIN}: the candidate names no registered seller domain, so its "
            f"checkout URL cannot be shown to be on-domain; failing closed (C10)"
        )
    if not url or not isinstance(url, str) or not url.strip():
        return (
            f"{REASON_OFF_DOMAIN}: the offer carries no usable checkout URL to compare "
            f"against the registered domain {registered!r}; failing closed (C10)"
        )
    if not is_on_domain(url, str(registered)):
        return (
            f"{REASON_OFF_DOMAIN}: the checkout URL {url!r} is not on the registered seller "
            f"domain {registered!r}; hosts are compared by exact equality (C10/D22)"
        )
    return None


# ---------------------------------------------------------------------------------
# R19 — hard constraints, decided against verified evidence only
# ---------------------------------------------------------------------------------
def verified_attributes(claims: Any, *, store_id: Any = None) -> list[dict[str, Any]]:
    """The candidate's `verified` claims, projected into the attribute shape
    :meth:`HardCriterion.decide` reads.

    Anything that is not `verified` is simply not here. That is the whole of R19's evidence
    rule: an ambiguous, unsupported or contradicted claim is not weaker support for a hard
    constraint, it is no support at all, and a constraint with no support is undecidable —
    which :class:`HardCriterion` already refuses to count as satisfied.

    "Verified" means THIS EXCHANGE said so. The verdict is read out of the claim's attested
    :data:`~exchange.ranking.attestation.ATTESTATION_FIELD` block and never out of a `status`
    the claim's author wrote, so a bidder gains nothing by writing either one (ESC-020). A
    claim carrying no readable exchange verdict is unverified — which is not a claim about
    the seller's honesty, it is the plain fact that nothing checked it.

    `store_id` is the candidate's EXCHANGE-ATTRIBUTED store — `collect_bids` stamps it over
    whatever the payload claimed — so a verdict attested for one store cannot be presented on
    behalf of another.
    """
    attributes: list[dict[str, Any]] = []
    for claim in claims or ():
        key = read(claim, "key", None)
        if key is None:
            continue
        value = read(claim, "value", None)
        unit = read(claim, "unit", None)
        status = attested_status(
            read(claim, ATTESTATION_FIELD, None),
            key=key,
            value=value,
            unit=unit,
            subject=store_id,
        )
        if str(_enum_value(status)) != VERIFIED:
            continue
        attribute: dict[str, Any] = {
            "key": str(key),
            "value_string": None,
            "value_number": None,
            "value_bool": None,
            "unit": unit,
        }
        # bool before number: `isinstance(True, int)` is True, so a bool tested as a number
        # would be filed under value_number and never match an `eq` on a bool.
        if isinstance(value, bool):
            attribute["value_bool"] = value
        elif _is_number(value):
            attribute["value_number"] = float(value)
        elif value is not None:
            attribute["value_string"] = str(value)
        attributes.append(attribute)
    return attributes


def decided_attributes(claims: Any, *, store_id: Any = None) -> list[dict[str, Any]]:
    """Every attribute key this exchange actually DECIDED for a candidate — the keys whose
    claims carry an attested `verified` or `contradicted` verdict.

    Wider than :func:`verified_attributes` and narrower than "every key a claim names", and
    all three answer different questions. `verified` is the only thing that SATISFIES a hard
    constraint (R19), and that rule is untouched here. This one answers: **could this exchange
    decide the key for anybody?** — which is the question :func:`unanswerable_criteria` is
    asking, and a claim's VERDICT is the only honest answer to it.

    The hostile case that makes `contradicted` belong here:

    * a store whose claim this exchange's own verifier **contradicted** carries no verified
      reading, but the exchange plainly decided the key — the question was answered and the
      answer was no. Reading that as "nobody could answer" would hand the waiver of the
      constraint to exactly the candidate caught failing it, so a contradicted claim keeps the
      constraint a filter and keeps its author excluded by it.

    **Why the undecided verdicts are excluded, which is the defect this replaced.** This used
    to return every key any claim NAMED, on the argument that the widest reading was safe
    because "the only thing this set can do is PREVENT a relaxation" and "the shortlist it
    empties is the one it would have been in". The second half of that is false, and the
    measurement is on the served ``POST /auctions``: relaxation is decided ONCE for the whole
    auction, not per candidate. A store making a TRUTHFUL claim on a key no catalogue this
    exchange holds declares gets `ambiguous` — nothing could check it — and under the old rule
    that `ambiguous` suppressed the relaxation for the auction, so the buyer's must-have stayed
    a filter no candidate could pass and **every store's shortlist came back empty, including
    the stores that claimed nothing at all**. One honest sentence from one shop emptied the
    page for the whole market.

    `ambiguous` and `unsupported` are, in this exact sense, evidence FOR unanswerability
    rather than against it: they are this exchange saying it looked and could not decide.
    Counting them as "the question was answerable" inverts what they mean.

    **ESC-020 is closed harder by this, not more loosely.** A claim whose attestation is
    forged, absent or unreadable carries no verdict (:func:`~.attestation.attested_status`
    returns ``None``), so it is not here — and therefore a bidder's own writing cannot move
    the relaxation decision in EITHER direction. It cannot suppress a relaxation the auction
    was entitled to, and it cannot manufacture one either: the outcome is identical to the one
    the auction would have reached had that candidate said nothing. Under the old rule an
    unattested string was load-bearing, which is the thing ESC-020 exists to forbid.

    `store_id` is the candidate's EXCHANGE-ATTRIBUTED store, for the same reason
    :func:`verified_attributes` takes it: a verdict attested for one store may not be presented
    on behalf of another.
    """
    seen: list[dict[str, Any]] = []
    for claim in claims or ():
        key = read(claim, "key", None)
        if key is None:
            continue
        status = attested_status(
            read(claim, ATTESTATION_FIELD, None),
            key=key,
            value=read(claim, "value", None),
            unit=read(claim, "unit", None),
            subject=store_id,
        )
        if str(_enum_value(status)) not in DECIDED_STATUSES:
            continue
        seen.append({"key": str(key)})
    return seen


def read_criteria(intent: Any) -> tuple[list[HardCriterion], str | None]:
    """The intent's hard constraints as decidable criteria, plus the reason they could not
    be read.

    An intent this module cannot parse denies every candidate rather than admitting them
    all: "I could not evaluate the constraint" and "the constraint is satisfied" must never
    be the same outcome (R19).

    That is why an ABSENT `hard_constraints` is a denial while an EMPTY one is not. They look
    alike and they are opposites: a buyer who asked for nothing mandatory has an empty list,
    while an intent with no such field at all is not a record this module knows how to read,
    and treating it as "no constraints" would silently admit every candidate for every
    unrecognised intent shape. An empty list, being a readable answer, admits.
    """

    def refused(detail: str) -> tuple[list[HardCriterion], str]:
        return [], (
            f"{REASON_UNDECIDABLE_INTENT}: {detail}; a constraint this exchange cannot decide "
            f"excludes rather than counting as satisfied (R19)"
        )

    raw = read(intent, "hard_constraints", _MISSING)
    if raw is _MISSING or raw is None:
        return refused(
            f"the intent {type(intent).__name__} carries no readable hard_constraints, so "
            f"this exchange cannot tell an unconstrained request from an unread one"
        )
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        return refused(f"hard_constraints {raw!r} is not a list of constraints")
    try:
        entries = list(raw)
    except Exception as exc:  # a constraint list that cannot even be walked
        return refused(f"hard_constraints could not be read ({type(exc).__name__}: {exc})")

    criteria: list[HardCriterion] = []
    for entry in entries:
        try:
            criteria.append(HardCriterion.from_mapping(entry))
        except MalformedIntent as exc:
            return refused(str(exc))
        except Exception as exc:
            # `from_mapping` promises MalformedIntent for a constraint it understands to be
            # wrong; anything else reaching here is a shape it did not anticipate, and an
            # unanticipated shape is still undecidable rather than satisfied.
            return refused(f"constraint {entry!r} could not be read ({type(exc).__name__}: {exc})")
    return criteria, None


def hard_constraint_reasons(
    claims: Any, criteria: Sequence[HardCriterion], *, store_id: Any = None
) -> tuple[list[str], int]:
    """`(reasons, verified_hard_fit_count)` for one candidate.

    `verified_hard_fit_count` is the number of hard constraints this candidate meets on
    verified evidence. It is the first published tie-breaker (D13), which is why it is
    counted here rather than recomputed by the sorter — and why the evidence behind it has to
    be the exchange's own (ESC-020): a tie-break a bidder can set is a tie-break a bidder
    wins.

    **A price BOUND is not decided here**, and that is the same rule stated once more rather
    than an exception to it. A verified `price_usd` claim is evidence about the store's
    CATALOGUE, and the buyer's ceiling is about the BID — so deciding the bound on the claim
    answered a question nobody asked, and a store whose catalogue price of 40.00 verified bid
    180.00 under a 50.00 ceiling and was shortlisted for it. :func:`budget_reasons` decides
    the bound against the offer instead, and skipping it here is what keeps it out of
    `verified_hard_fit_count`: the price a bidder names is a number the bidder chose, and D13's
    first tie-break may not be one of those.
    """
    attributes = verified_attributes(claims, store_id=store_id)
    reasons: list[str] = []
    satisfied = 0
    for criterion in criteria:
        if is_budget_bound(criterion):
            continue
        verdict = criterion.decide(attributes)
        if verdict.satisfied:
            satisfied += 1
            continue
        reasons.append(
            f"{REASON_HARD_CONSTRAINT}: {verdict.reason} — only a verified supporting claim "
            f"satisfies a hard constraint (R19)"
        )
    return reasons, satisfied


# ---------------------------------------------------------------------------------
# The buyer's budget, decided against the price BID
# ---------------------------------------------------------------------------------
# `BUDGET_FIELDS`, `BUDGET_OPS` and `is_budget_bound` are re-exported from
# `..retrieval.criteria`, which is where they are DEFINED — imported at the top of this module
# and named here so this section still reads as the one place the budget vocabulary lives.
#
# They moved down a layer because RETRIEVAL needs the same predicate: a money bound must not be
# pushed into the catalogue query and must not be re-decided against product attributes, and
# both of those decisions happen in `criteria.py`, which this module imports. The dependency
# only runs one way (`ranking` -> `retrieval`), so the definition has to live at the bottom of
# it or there would be two lists of price spellings that could drift apart — and a bound
# exempted in one place and not the other is exactly the defect that emptied every
# graph-rostered shortlist that stated a ceiling.


#: The offer fields that state a price, in the order :func:`offer_price` prefers them.
#:
#: The first two are the published ``Offer``'s own (``protocol.schema.json``) and are the two
#: :func:`~exchange.ranking.serving.shortlist_price` copies onto the slot. ``price`` is not a
#: published field and no bid can carry one; it is here because callers inside this tree hand
#: :func:`budget_reasons` plain mappings, and a mapping stating only ``price`` must be JUDGED
#: rather than counted unreadable.
PRICE_FIELDS: tuple[str, ...] = ("total_price", "unit_price", "price")


def offer_prices(offer: Any) -> list[tuple[str, float]]:
    """EVERY finite price this offer states, as ``(field, amount)`` in :data:`PRICE_FIELDS` order.

    :func:`offer_price` answers "what would this cost" and has to pick one number.
    :func:`budget_reasons` is asking a different question — "is there a number here the buyer
    said they could not pay" — and picking one number is what made that question answerable
    two different ways.
    """
    prices: list[tuple[str, float]] = []
    for name in PRICE_FIELDS:
        raw = read(offer, name, None)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price):
            prices.append((name, price))
    return prices


def offer_price(offer: Any) -> float | None:
    """What this offer would actually cost, or `None` when there is not a finite number.

    Total before unit, because the total is what the buyer pays and the two differ whenever a
    bid is for more than one of something. There is no quantity field to reconcile them with —
    the published `Offer` forbids one (`additionalProperties: false`) — so the total is read
    as given rather than reconstructed.

    ``total_price`` is ALREADY POST-DISCOUNT: a `Discount` is known at BID time and the accept
    path only mints the redemption code against it (`checkout/protocol.py`), so there is no
    later reduction to thread through here.

    A non-finite price is not a price, and an offer stating none is `None` here — which the
    price tie-break reads as "sort last".

    **This is the RANKING number and it is deliberately not the wall's.** The tie-break sorts
    by what the buyer pays, which is one number; :func:`budget_reasons` judges every number the
    offer states, because the slot publishes every number the offer states. Collapsing the two
    questions into this one function is what let a bid stating ``unit_price 500.00`` and
    ``total_price 19.99`` clear a 25.00 ceiling on the total and then publish the 500.00 —
    see that function's "THE WALL AND THE SLOT" section for the measurement.
    """
    for _name, price in offer_prices(offer):
        return price
    return None


def _bound_text(criterion: HardCriterion) -> str:
    return f"{criterion.field!r} {criterion.op} {criterion.value!r}"


def budget_reasons(offer: Any, criteria: Sequence[HardCriterion]) -> list[str]:
    """Why this offer's own price fails the buyer's budget, if it does.

    **The defect this closes, measured over ``POST /auctions``.** With ``price_usd`` declared
    in this exchange's catalogue snapshot — so the constraint was *decided* rather than set
    aside — a store whose catalogue price is 40.00, claiming ``price_usd: 40`` and carrying
    this exchange's own attestation on that claim, **bid 180.00 under a ``price_usd lte 50``
    ceiling and took a shortlist slot**. R19 was satisfied and the answer was still wrong,
    because a verified claim describes the CATALOGUE and the ceiling is about the BID. The
    shipped demo has the same shape: ``apps/buyer/devstack/demo-market.json`` states
    ``price_usd lte 80`` against a catalogue price of 78.00, and the bid could be anything.

    **Why this is a structural filter and not a hard constraint.** A bid-asserted price is not
    a verified supporting fact and *nothing could verify it* — there is no catalogue entry for
    "what I will charge you today". So the bound is decided here, from the offer this exchange
    holds, and :func:`hard_constraint_reasons` no longer grades it against claims at all.
    Grading it there was the confusion; leaving it there as well would ALSO feed
    ``verified_hard_fit_count``, which is D13's first published tie-break, and a tie-break a
    bidder can set is a tie-break a bidder wins.

    **No readable price DENIES.** The alternative — admitting a bid whose price cannot be
    compared — would make "my price is unreadable" the cheapest bid in the auction, and R12
    already settled which way an unreadable answer fails.

    **THE WALL AND THE SLOT HAVE TO MEAN THE SAME NUMBER, so every stated price is judged.**
    The bound is applied to EVERY finite price the offer states (:func:`offer_prices`), not to
    the one :func:`offer_price` picks. Measured over ``POST /auctions`` before this, with a
    roster row at ``list_price 25.00``, no ``max_discount_pct`` and a bid of
    ``{"unit_price": 500.0, "total_price": 19.99}`` under ``price_usd lte 25``::

        excluded: []
        slot:  {"slot": "fit", "fallback": false,
                "price": {"unit_price": 500.0, "total_price": 19.99, "currency": "USD"}}

    19.99 cleared the ceiling on the total, and the 500.00 went to the shopper, because
    ``ranking.serving.shortlist_price`` copies BOTH fields onto the slot verbatim and the
    surfaces read them differently. That is the worst direction for this filter to be wrong
    in: it exists to make a ceiling mean something, and it put a number above the ceiling on
    screen.

    **Why judge both, rather than switch the wall to "the rendered field".** There is no such
    field — the readers of one slot disagree, and the disagreement is worst in exactly this
    case. ``apps/buyer/app/shortlist/ShortlistView.tsx::priceLine`` leads with ``total_price``
    and appends ``"<unit_price> each"`` *only when the two differ*, so the shopper reads
    "USD 19.99 — USD 500.00 each" — the over-ceiling number, rendered, because the fields
    disagree. ``apps/buyer/app/learning/LearningPage.tsx`` prints ``unit_price`` alone, and
    ``docs/driving-the-stack.md`` tells a reader driving the API by hand that "the price is
    ``price.unit_price``". Nothing binds a surface to either field: the published
    ``ShortlistPrice`` requires the two together and this exchange publishes both. So "the
    wall reads what the display renders" is a rule about another module's current choice and
    would be silently wrong again the next time a surface changes. What is invariant is that a
    ceiling is a promise about every price this exchange puts in front of the shopper, and the
    offer states them all. Making the SLOT render only the judged number is the other
    direction and it is worse: it would delete a field the contract requires, across
    ``apps/buyer/``, to hide a number the bid really did state.

    This is a REFINEMENT, not a new class of refusal, and no honest bid can feel it: with no
    quantity field on the published ``Offer`` there is no bid for which a total is legitimately
    SMALLER than a unit price, so the only bids this newly refuses are the malformed ones. A
    genuine multi-unit bid — ``unit 10.00, total 20.00`` — is still judged on its total, which
    is what it was judged on before.

    **OPEN EXPOSURE — the bound is decided against the STORE'S OWN NUMBER, and the exchange is
    holding a crawled one it never looks at.** R19 says unverified data cannot satisfy a hard
    constraint. A bid-stated price is unverified data by this docstring's own argument, and the
    exchange holds the platform's crawled price for the same product on the same auction, at
    ``auction.collect.BidEntry.list_price``. Nothing compares them. Measured over
    ``POST /auctions``, roster row ``{list_price: 180.0}`` with no ``max_discount_pct``,
    ``price_usd lte 25``, one hosted bid of 24.00::

        slots=1  price.unit_price=24.00  fallback=false  excluded=[]

    Whether that is exploitable turns on one caller-supplied field:

    * **row states no ``max_discount_pct``** — ``auction.collect._price_refusal`` ABSTAINS, by
      design and by its own docstring ("an undeclared undercut on a row that states no
      authorized depth, which R10 requires the exchange to keep"), so the bid is admitted at
      whatever it says. **This is the shape the GRAPH path produces**:
      ``retrieval.roster.SolicitedShop.as_roster_row`` writes ``store_id``, ``tier``,
      ``product_ref``, ``list_price``, ``currency`` and ``variant_ref`` and no cap at all.
    * **row states ``max_discount_pct: N``** — the same 24.00 bid is refused
      ``bid_price_unreconcilable`` and the store is represented at 180.00, which this function
      then refuses as over the ceiling. Measured alongside the above. The demo's stated roster
      (``deploy/demo/buyer-roster.json``) carries 12-20% on its four bidding stores, so the
      demo path is the guarded one and the exchange's own graph roster is not.

    The four shipped store agents decline rather than send an under-ceiling lie — they fold the
    buyer's field through ``PRICE_CONSTRAINT_FIELDS`` and look the ceiling up against their own
    ``LIST_PRICE_KEY``. That is the AGENT's manners and not this exchange's wall.

    **Why it is not closed here, rather than closed badly here.** The crawled price is not
    reachable from this frame. ``ranking.candidates.CANDIDATE_FIELDS`` is ``(bid_id, store_id,
    store_domain, offer, claims, message, fallback, fallback_reason)`` — no roster field — and
    ``ranking.rank()`` is not given the ``BidEntry`` list at all; the list price reaches the
    ranker only as a positional sideband into ``features.attach_features``, one frame up in
    ``serving.rank_auction``. Closing it means either widening a projection R11 deliberately
    keeps narrow so a bidder cannot write a published feature into its own reply, or threading
    a new argument through ``rank()``. And on the STATED path the number would not be worth
    reaching: ``auction.routes.RosterEntry.list_price`` is caller-supplied on an
    unauthenticated endpoint, so a ceiling decided against it is a ceiling the CALLER sets —
    ``BidEntry.list_price`` mixes crawled and caller-asserted provenance with nothing recording
    which. The honest repair is a provenance flag on that field plus a wall in
    ``auction/collect.py`` that stops treating "no declared cap" as "no bound", and both are
    outside this function. ``test_ranking_budget_ceiling.py``'s section 6 pins the two
    measurements above so this cannot regress into something worse unnoticed.

    **Currency is reported and not gated on**, and neither is the criterion's ``unit``.
    ``Offer.currency`` is optional and bidder-set, so refusing a mismatch would refuse every
    honest bid that omits it, and *accepting* a mismatch costs a bidder nothing it could
    exploit: the comparison is numeric either way, so relabelling a 180.00 offer as ``JPY``
    does not get it under a 50.00 ceiling. Converting between currencies is a different
    capability with a rate source behind it, and this exchange has neither.

    Returns:
        One reason per bound this offer misses — naming every stated price that missed it —
        or a single unreadable-price reason naming every bound it could not be judged
        against. Empty when the buyer stated no budget.
    """
    bounds = [criterion for criterion in criteria if is_budget_bound(criterion)]
    if not bounds:
        return []

    prices = offer_prices(offer)
    if not prices:
        stated = ", ".join(_bound_text(criterion) for criterion in bounds)
        return [
            f"{REASON_PRICE_UNREADABLE}: the buyer stated {stated} and this offer carries no "
            f"readable price — total_price {read(offer, 'total_price', None)!r}, unit_price "
            f"{read(offer, 'unit_price', None)!r} — so the bound could not be applied to it; a "
            f"bid whose price cannot be compared does not pass a price filter by being "
            f"uncomparable"
        ]

    currency = read(offer, "currency", None)
    reasons: list[str] = []
    for criterion in bounds:
        bound = float(criterion.value)
        if not math.isfinite(bound):
            # `HardCriterion` requires a NUMERIC bound for lte/gte and NaN is a number, so
            # `price_usd lte NaN` is constructible — and `price > nan` is False, which would
            # make an unreadable ceiling the one ceiling every bid clears. JSON's own parser
            # accepts the literal, so this is reachable from the door rather than theoretical.
            reasons.append(
                f"{REASON_PRICE_UNREADABLE}: the buyer's bound {_bound_text(criterion)} is not a "
                f"finite amount, so no price could be compared against it; an undecidable bound "
                f"excludes rather than admitting everybody (R19)"
            )
            continue
        if criterion.op == "lte":
            missed = [(name, price) for name, price in prices if price > bound]
            side = f"above the buyer's ceiling ({_bound_text(criterion)})"
        elif criterion.op == "gte":
            missed = [(name, price) for name, price in prices if price < bound]
            side = f"below the buyer's floor ({_bound_text(criterion)})"
        else:
            continue
        if not missed:
            continue
        # The FIELD is named only when this offer states more than one distinct price, and
        # then every missed field is named rather than the first. An offer whose prices agree
        # — every honest bid, and every fallback `_list_price_bid` mints — reads exactly as it
        # read before every stated price was judged, because naming a field there tells a
        # merchant nothing they do not already know. When the prices DISAGREE the field is the
        # whole content of the refusal, and naming one of two missed fields would be a partial
        # explanation presented as a complete one.
        suffix = f" {currency}" if currency else ""
        priced = (
            f"{missed[0][1]}{suffix}"
            if len({price for _name, price in prices}) == 1
            else ", ".join(f"{name} {price}{suffix}" for name, price in missed)
        )
        reasons.append(
            f"{REASON_OVER_BUDGET}: this bid's own price of {priced} is {side} — a price bound "
            f"is decided against the PRICE BID, not against the catalogue price a store claims "
            f"for itself, and a bid the buyer cannot afford is not a shortlist answer. Every "
            f"price the offer states is judged, because the slot publishes every price the "
            f"offer states and the shopper is shown one of them"
        )
    return reasons


# ---------------------------------------------------------------------------------
# The question nobody in the auction could be asked
# ---------------------------------------------------------------------------------
def unanswerable_criteria(
    candidates: Sequence[Any],
    criteria: Sequence[HardCriterion],
    network_attributes: Sequence[Mapping[str, Any]] | None,
) -> list[HardCriterion]:
    """The criteria this exchange could not decide for ANY candidate, however honest.

    R19 makes a hard constraint an eligibility filter decided on verified evidence, and
    :func:`hard_constraint_reasons` applies that per candidate, correctly. What no single
    candidate can be asked is whether the constraint was *answerable at all* — and that is a
    different question with a different answer:

    * a constraint this exchange can decide is a **filter**. It narrows. The candidates that
      fail it — including the ones caught contradicting it — are worse answers to the buyer's
      question, and excluding them is the whole point;
    * a constraint it can decide for nobody narrows nothing. Every candidate is excluded on
      it, so what it produces is not a strict shortlist but an empty one wearing a filter's
      name, and the shopper is told "no stores matched" when the truth is "nobody could be
      asked the question you asked".

    TWO facts have to be absent before a constraint is called unanswerable, and either one on
    its own keeps it a filter. **Both are things THIS EXCHANGE knows** — what its catalogues
    declare and what its verifier decided — and neither is a string a bidder can write. That
    is the property this function lost and has regained: the relaxation is decided once for
    the whole auction, so an input a single participant controls is an input that participant
    can use against every other participant.

    ``network_attributes``
        the attributes the catalogue snapshots THIS EXCHANGE holds for this auction's stores
        declare (:func:`~exchange.ranking.verification.declared_attributes`). ``None`` means
        the exchange cannot say — an unwired catalog, a catalog service that is down — and
        then NOTHING is unanswerable: an exchange that can verify nothing has discovered that
        it is misconfigured, not that the buyer's must-have is meaningless, and ESC-020
        already fixed which way that fails (shortlist nobody).
    ``decided_attributes``
        the keys this exchange actually DECIDED for some candidate — `verified` or
        `contradicted`. Not "the keys somebody named": a claim graded `ambiguous` or
        `unsupported` is this exchange saying it looked and could not decide, which is
        evidence FOR unanswerability rather than against it. See that function for the
        measurement that made the wider reading untenable — a single honest claim on an
        undecidable key used to empty the shortlist for every store in the auction.

    The catalogue half is what makes this decidable here and nowhere else. A buyer service
    holds no candidates and no snapshots, so the best it could do is guess from a catalogue
    CONFIG file — and measured through ``POST /auctions`` on the S1 roster, that guess named
    the wrong constraints in both directions: ``list_price``, ``boiler_type`` and
    ``roast_level`` are all declared by ``fixtures/catalog/coffee.json`` and all produced 0
    slots, exactly as the ``brew_method`` it refused did.

    A PRICE BOUND IS NEVER NAMED HERE, whatever the catalogue and the claims say, and that is
    the third fact rather than a fourth exception. Answerability asks "could this exchange
    decide the constraint for anybody?", and for a ``price_usd`` ceiling the answer is always
    yes: the exchange is holding every bid, and :func:`budget_reasons` decides the bound
    against the offer's own price without consulting a catalogue or a claim at all. Reading
    answerability off the catalogue for it would be reading the wrong evidence — and the cost
    of that is not academic. An auction where EVERY bid is over budget leaves nothing eligible,
    which is precisely the input :func:`~exchange.ranking.rank`'s relaxation path acts on; a
    ceiling named here would then be set aside and **every bid just excluded would be
    re-admitted**, in exactly the case the budget filter exists for. Driven in
    ``apps/exchange/tests/test_ranking_budget_ceiling.py``.

    The relaxation itself is untouched: a constraint the exchange really cannot decide for
    anybody — no catalogue declares it, no store claimed it — is still named and still set
    aside, price bound beside it or not.

    This function only NAMES them. Whether naming one changes an outcome is
    :func:`~exchange.ranking.rank`'s decision, and it makes it only when the shortlist would
    otherwise be empty.
    """
    if not criteria or not network_attributes:
        return []
    # ONE flat list for the whole auction, which is what makes the verdict rule load-bearing
    # rather than fastidious: whatever goes in here decides the relaxation for EVERY candidate,
    # including the ones that said nothing. `decided_attributes` is what keeps the entries in
    # it to facts this exchange established.
    seen: list[Mapping[str, Any]] = list(network_attributes)
    for candidate in candidates:
        seen.extend(
            decided_attributes(
                read(candidate, "claims", None),
                store_id=read(candidate, "store_id", None),
            )
        )
    return [
        criterion
        for criterion in criteria
        if not is_budget_bound(criterion) and not criterion.is_evidenced_by(seen)
    ]


def unanswerable_reason(criterion: HardCriterion) -> str:
    """Why one criterion was set aside, in the words a shopper's agent can repeat.

    It names the constraint, says plainly that it was NOT applied, and says why — because a
    must-have that quietly stops being a must-have is the failure this whole path exists to
    avoid. The shortlist beside it is unfiltered on this attribute and the reader is told so.
    """
    return (
        f"{REASON_UNEVIDENCED_CONSTRAINT}: no catalogue this exchange holds for the stores in "
        f"this auction declares an attribute called {criterion.field!r}, and no store's claim "
        f"about it could be checked either way, so {criterion.field!r} {criterion.op} "
        f"{criterion.value!r} could not be decided "
        f"for anybody — it could not narrow the shortlist, only empty it. It was NOT applied: "
        f"the slots below are unfiltered on {criterion.field!r}, and no store below has been "
        f"shown to meet it."
    )


# ---------------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------------
def organic_relevance_reason(
    candidate: Any,
    *,
    query_text: str,
    identity: Any,
    relevance: TopicalRelevance,
) -> str | None:
    """Why this ORGANIC row is not about what the shopper asked, or ``None``.

    The sixth filter, and the only one that is about the shopper's question rather than about
    the store. It answers a defect a green ranking could not see: a vector index returns its
    top ``k`` whatever is in it, so a catalogue with nothing on the subject produces a full
    shortlist rather than an empty one. Measured through ``POST /auctions`` before this
    existed — ``"a walnut coffee table for the lounge"``, four slots, four liver supplements.

    FOUR conditions, all of which must hold before anything is refused. Each is the
    silent-on-honest-traffic direction of this gate, and each is here rather than left to fall
    out of the arithmetic. **Only the first is executed in this module**: 2-4 are
    :func:`~exchange.retrieval.relevance.identity_off_topic`, because
    :func:`~exchange.retrieval.roster._solicited` has to ask the identical question one layer
    earlier — before a shop's one roster row is staked on a product this would then refuse —
    and a copy of them there would be a second rule that drifts from this one. They are still
    documented here, where the gate is:

    1. **The candidate is a FALLBACK.** ``entry.fallback`` is ``collect_bids``' own verdict for
       a store that did not bid: the exchange stood a list-price offer up in its place, chose
       the product off the roster row and wrote the pitch itself. A row a store BID is
       untouched — see :data:`~exchange.ranking.reasons.REASON_OFF_TOPIC_ORGANIC` for why the
       asymmetry is D55's and not a softening. Measured on the demo roster: the three queries
       whose stores bid (``"milk thistle"``, ``"milk thistle silymarin liver support extract
       under $50"``, ``"liver support supplement"``) keep all four sponsored slots under this
       filter, because it never looks at them.
    2. **The exchange holds a crawled identity for this store's product.** ``identity`` is
       :func:`~exchange.ranking.verification.catalog_identity`'s answer, absent for a store this
       exchange's catalogue never named. Absent means unchecked, and an unchecked product is
       kept: an exchange whose catalogue is unwired would otherwise refuse every organic row in
       every auction, which is a misconfiguration reported as "this catalogue serves nothing".
    3. **The shopper did not ask for the product by name.** See
       :func:`shopper_named_the_product` — when every content word of the platform's crawled
       TITLE is in the query AND the query's opening noun phrase is that name, the shopper
       asked for this thing by the name the platform gave it, and no word-count threshold may
       overrule that. Measured on the shipped catalogue, this is what stops the gate refusing
       104 of its own 3,086 products for queries carrying their titles verbatim. The second
       half of that condition is what keeps it from being the one-shared-word rule in
       disguise: without it, ``"cast iron skillet for camping"`` was answered with ``Iron+``
       on the served route.
    4. **The relevance rule could decide.** See
       :meth:`~exchange.retrieval.relevance.TopicalRelevance.judge` — a query with no content
       words, or an identity that folds to none, answers ``about=True, decidable=False`` and
       nothing is refused on it.

    ONE RULE NAME, TWO SURFACES, and it is written down here rather than left to be discovered
    from the audit trail. This layer judges :func:`~exchange.retrieval.relevance.identity_surface`
    — ``title`` and ``brand``, which is all :func:`~.verification.catalog_identity` returns.
    The retrieval layer judges :func:`~exchange.retrieval.relevance.candidate_surface`, which is
    the canonical name, brand, categories, ingredients and attribute keys the graph holds, and
    hands ``judge`` the product's observed variant names beside it
    (:func:`~exchange.retrieval.relevance.variant_surface`) — a surface this layer holds none
    of, because ``catalog_identity`` carries no variants. Both record the verdict under one
    name, ``content-word-agreement/2``, so a reader of an exclusion reason cannot tell which
    surface produced it, and the two CAN disagree: a product retrieval kept on a category word,
    or on a walnut its variants name and its title does not, can be refused here on its title
    alone.

    Measured rather than asserted, on the recorded corpus (3,093 products, ten storefronts) with
    every store's whole catalogue in the snapshot: over 24 in-corpus queries through the graph
    roster, 89 shortlist slots, this layer refused **one** of them (``"nmn supplement"``), and
    over the same 24 queries through a stated roster it refused nothing that retrieval had
    vouched for. The gap was small because the graph that run read carried no ``Ingredient`` or
    ``AttributeValue`` nodes at all (measured: 0 and 0) — categories were the only component
    ``candidate_surface`` added.

    **THE SECOND HALF OF THAT IS NO LONGER TRUE OF A FRESH CRAWL, so the 89-slot measurement
    above is now a reading taken under conditions a re-crawl changes.** ``build_upserts`` now
    emits attribute ops from each entry's ``options[]`` block — measured over the nineteen
    recorded storefronts, 21,667 readings under 147 keys on 3,443 of 4,903 products — so
    ``candidate_surface`` gains both the key words and the value words for any product loaded
    since. The value words were largely there already through ``variant_surface`` (a Shopify
    variant title IS the option combination joined by " / "); the KEY words are the new arm and
    they are the risk, because several are ordinary query words that name nothing on their own:
    ``color``, ``size``, ``style``, ``type``, ``base``, ``side``, ``length``, ``product``. A
    query for "coffee table base" can now match a product whose only tie is an option NAMED
    ``base``. That arm is unmeasured — it needs a graph loaded from the new crawl, which is a
    re-load rather than a re-run — and it is written down here rather than left to be
    discovered as a widened shortlist. Variant names
    are the other component the retrieval layer now has and this one does not, and on this
    corpus they widen nothing either: measured over 37 honest and 30 off-corpus queries through
    the real index against a private Neo4j holding it (3,093 ``Product``, 9,667 ``Variant``),
    the variant arm moved not one retrieval row in either direction, because a supplement's
    variants are its form and its count. A corpus of furniture is where it moves rows, and that
    is where this gap would open. The fix if it ever matters is to judge both layers on one
    surface, not to loosen this one: a title-only refusal of a product the platform's own
    retrieval vouched for is this layer overturning a decision made with more evidence.

    It is also, on the graph route, mostly a formality: every row there was already judged by
    the retrieval layer on the fuller surface before it reached a roster at all.

    Args:
        candidate: the projected candidate, carrying ``fallback``.
        query_text: the shopper's own words, off the intent.
        identity: the PLATFORM's crawled identity for this store's rostered product, or
            ``None``. Never the store's message, pitch or claims.
        relevance: the rule to decide with.

    Returns:
        A ``REASON_OFF_TOPIC_ORGANIC``-prefixed reason, or ``None`` when this row stands.
    """
    if not bool(read(candidate, "fallback", False)):
        return None
    # Conditions 2-4 are `identity_off_topic`, and they are asked THERE rather than spelled
    # again here because `exchange.retrieval.roster` asks the identical question one layer
    # earlier — see that function. Condition 1 stays here: `fallback` is a fact about a BID,
    # which the retrieval layer holds none of.
    verdict = identity_off_topic(query_text, identity, relevance)
    if verdict is None:
        return None
    return f"{REASON_OFF_TOPIC_ORGANIC}: {verdict.detail}"


def exclusion_reasons(
    candidate: Any,
    *,
    criteria: Sequence[HardCriterion],
    intent_reason: str | None,
    trust_snapshot: Any,
    now: float,
    eligibility: Any = None,
) -> tuple[list[str], int]:
    """Every reason this candidate may not be scored, and its verified hard-fit count.

    Every filter runs, rather than the first denial short-circuiting the rest. A candidate
    that is both blacklisted and off-domain has two things wrong with it, and recording one
    of them would make the second invisible to whoever fixes the first.
    """
    reasons: list[str] = []

    bid_id = read(candidate, "bid_id", None)
    store_id = read(candidate, "store_id", None)
    offer = read(candidate, "offer", None)
    if not bid_id:
        reasons.append(f"{REASON_MALFORMED}: the candidate carries no bid_id")
    if not store_id:
        reasons.append(f"{REASON_MALFORMED}: the candidate carries no store_id")
    if offer is None:
        reasons.append(f"{REASON_MALFORMED}: the candidate carries no offer to rank")

    if store_id:
        blacklist = blacklist_reason(str(store_id), trust_snapshot)
        if blacklist is not None:
            reasons.append(blacklist)
        source = eligibility_source_reason(eligibility, str(store_id))
        if source is not None:
            reasons.append(source)

    if offer is not None:
        expiry = expiry_reason(offer, now)
        if expiry is not None:
            reasons.append(expiry)
        domain = domain_reason(candidate, offer)
        if domain is not None:
            reasons.append(domain)
        # The buyer's budget, beside the two other offer-based filters and before scoring. It
        # takes `criteria` as well as the offer because it is the one filter that needs both,
        # and this gate is the only place that holds them together.
        reasons.extend(budget_reasons(offer, criteria))

    if intent_reason is not None:
        reasons.append(intent_reason)
        return reasons, 0

    hard, satisfied = hard_constraint_reasons(
        read(candidate, "claims", None), criteria, store_id=store_id
    )
    reasons.extend(hard)
    return reasons, satisfied


__all__ = [
    "BUDGET_FIELDS",
    "BUDGET_OPS",
    "NAMING_MODIFIERS",
    "VERIFIED",
    "blacklist_reason",
    "budget_reasons",
    "decided_attributes",
    "domain_reason",
    "eligibility_source_reason",
    "exclusion_reasons",
    "expiry_reason",
    "hard_constraint_reasons",
    "is_budget_bound",
    "identity_off_topic",
    "PRICE_FIELDS",
    "offer_price",
    "offer_prices",
    "organic_relevance_reason",
    "query_noun_phrases",
    "read",
    "read_criteria",
    "shopper_named_the_product",
    "trust_row",
    "unanswerable_criteria",
    "unanswerable_reason",
    "verified_attributes",
]
