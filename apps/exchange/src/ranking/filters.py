"""Eligibility filters — everything that decides whether a candidate is scored at all.

R19 is explicit that a hard constraint is a **filter**, never a weighted term, and R12 is
explicit that a blacklist read **fails closed**. Both rules say the same thing about order:
a candidate that the filters cannot positively clear is excluded *before* the published
formula runs, so no `rank_score` exists for it at all. A number that existed but was ignored
would be a number some later reader could sort by; there is no such number here.

The five filters, and why each one denies rather than discounts:

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
from ingest.graph.model import slug

from ..checkout.codes import UnusableOffer, expiry_epoch
from ..checkout.domain import is_on_domain
from ..retrieval.criteria import HardCriterion, MalformedIntent
from .attestation import ATTESTATION_FIELD, attested_status
from .reasons import (
    REASON_BLACKLIST_UNREADABLE,
    REASON_BLACKLISTED,
    REASON_ELIGIBILITY_DENIED,
    REASON_EXPIRED,
    REASON_HARD_CONSTRAINT,
    REASON_MALFORMED,
    REASON_OFF_DOMAIN,
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
    the auction's own `respond_by` when the merchant's context states no expiry (no store
    context in `deploy/demo` states one), and the served path judged that stamp against a
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
#: The constraint fields that state the buyer's budget in money, folded through the same
#: :func:`slug` that :attr:`HardCriterion.canonical_field` folds with — so ``price_usd``,
#: ``Price USD`` and ``price-usd`` are one field here and not three.
#:
#: ``budget_band`` is deliberately absent, and it is the obvious wrong answer. The band is a
#: LOSSY PROJECTION minted *from* the parsed number
#: (``apps/buyer/svc/src/intent/extraction.py`` turns a ceiling into one of a handful of
#: buckets), so binding the wall to it would judge a bid against a bucket the shopper never
#: said — and against a bound the buyer service, not the buyer, chose.
BUDGET_FIELDS: frozenset[str] = frozenset({slug("price_usd")})

#: The ops that state a money BOUND. ``eq``, ``in`` and ``contains`` on a price field are not
#: budgets — "exactly $40" is a description of a product, not a ceiling — and they keep
#: whatever meaning they already had, decided against claims like any other constraint.
BUDGET_OPS: tuple[str, ...] = ("lte", "gte")


def is_budget_bound(criterion: HardCriterion) -> bool:
    """Is this criterion the buyer's stated budget, rather than a claim about a product?"""
    return criterion.canonical_field in BUDGET_FIELDS and criterion.op in BUDGET_OPS


def offer_price(offer: Any) -> float | None:
    """What this offer would actually cost, or `None` when there is not a finite number.

    Total before unit, because the total is what the buyer pays and the two differ whenever a
    bid is for more than one of something. There is no quantity field to reconcile them with —
    the published `Offer` forbids one (`additionalProperties: false`) — so the total is read
    as given rather than reconstructed.

    ``total_price`` is ALREADY POST-DISCOUNT: a `Discount` is known at BID time and the accept
    path only mints the redemption code against it (`checkout/protocol.py`), so there is no
    later reduction to thread through here.

    A non-finite price is not a price. It is `None` to both callers, and they disagree about
    nothing: the price tie-break sorts an unreadable price LAST, and :func:`budget_reasons`
    refuses it outright.
    """
    for name in ("total_price", "unit_price", "price"):
        raw = read(offer, name, None)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price):
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

    **Currency is reported and not gated on**, and neither is the criterion's ``unit``.
    ``Offer.currency`` is optional and bidder-set, so refusing a mismatch would refuse every
    honest bid that omits it, and *accepting* a mismatch costs a bidder nothing it could
    exploit: the comparison is numeric either way, so relabelling a 180.00 offer as ``JPY``
    does not get it under a 50.00 ceiling. Converting between currencies is a different
    capability with a rate source behind it, and this exchange has neither.

    Returns:
        One reason per bound this offer misses, or a single unreadable-price reason naming
        every bound it could not be judged against. Empty when the buyer stated no budget.
    """
    bounds = [criterion for criterion in criteria if is_budget_bound(criterion)]
    if not bounds:
        return []

    price = offer_price(offer)
    if price is None:
        stated = ", ".join(_bound_text(criterion) for criterion in bounds)
        return [
            f"{REASON_PRICE_UNREADABLE}: the buyer stated {stated} and this offer carries no "
            f"readable price — total_price {read(offer, 'total_price', None)!r}, unit_price "
            f"{read(offer, 'unit_price', None)!r} — so the bound could not be applied to it; a "
            f"bid whose price cannot be compared does not pass a price filter by being "
            f"uncomparable"
        ]

    currency = read(offer, "currency", None)
    priced = f"{price} {currency}" if currency else f"{price}"
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
        if criterion.op == "lte" and price > bound:
            side = f"above the buyer's ceiling ({_bound_text(criterion)})"
        elif criterion.op == "gte" and price < bound:
            side = f"below the buyer's floor ({_bound_text(criterion)})"
        else:
            continue
        reasons.append(
            f"{REASON_OVER_BUDGET}: this bid's own price of {priced} is {side} — a price bound "
            f"is decided against the PRICE BID, not against the catalogue price a store claims "
            f"for itself, and a bid the buyer cannot afford is not a shortlist answer"
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
    "offer_price",
    "read",
    "read_criteria",
    "trust_row",
    "unanswerable_criteria",
    "unanswerable_reason",
    "verified_attributes",
]
