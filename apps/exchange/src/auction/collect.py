"""Fan-out collection: turn a roster plus whatever came back into one entry per store (R10).

R10 has two halves and this module is the second one. The first half — *asking* — is I/O and
lives above, in :mod:`apps.exchange.src.orchestration`. What is left here is pure and
deterministic: given the roster we asked, the responses that arrived, and the deadline they
had to beat, produce **one entry per rostered store**, never more and never fewer.

A fallback is produced by one of these, and the entry records **which**:

=========================================  =========================================
a Tier-0 store                             has no bidding agent to answer at all
a Tier-1 store that stayed silent          it was asked and nothing ever came back
a store still answering at the close       the window shut on a reply in flight (R10)
a store the fan-out had no worker for      the exchange had no capacity to ask it
a response stamped after the deadline      a late bid is not a bid (R10)
a reply carrying no ``bid``                the store answered with nothing to rank
a reply with no arrival stamp              the exchange's own stamp never got applied
a reply whose stamp will not parse         undatable, therefore uncertifiable
=========================================  =========================================

The last three are *not* lateness and must not be reported as it. They used to be: all
four rejections shared the single label ``response_after_deadline``, so a store that
answered well inside the window with a malformed payload was recorded — and would be
reported back to its operator — as slow. See :data:`FALLBACK_REASONS`.

Rows two through four are the same lesson applied to *absence*, and they used to be one
label. ``no_response`` meant all three, so a store that answered in 4.5 s
against a 3.0 s window was recorded identically to a container that was switched off, and
identically to a store the exchange never dialled because its worker pool was full. Measured
on a live hosted deployment: four store agents answered ``200 OK`` to every solicitation, and
the auction recorded ``hosted bids=0  fallback reasons=['no_response']`` — the demo probe's
own diagnostic then told the operator "no agent is running", which was false. That is why
:data:`RESPONSE_TIMED_OUT_REASON` and :data:`FAN_OUT_CAPACITY_REASON` exist and why the
fan-out mints them: only the fan-out knows which futures were still pending when the wait
ended and which stores it never submitted at all, and it used to throw both facts away.

The late-bid rule is the one worth being blunt about: the deadline is enforced on the
response's ``received_at``, so a bid that arrives at any price after the deadline is
discarded and its store falls back to its **list price**. An implementation that merely
sorted by arrival, or that trusted the last response to win, would let a slow store bid
1.00 after seeing everyone else and take every auction.

That makes ``received_at`` a **privileged field**, and this module is pure: it cannot tell
where the value came from. The stamp must therefore be applied by the exchange, upstream, in
:func:`apps.exchange.src.auction.fanout._stamped`, which overwrites whatever the bidder
sent. Nothing here re-checks that, so a caller assembling ``responses`` by hand is asserting
the stamps are its own — hand this function a store's raw reply and the deadline becomes
advisory.

``collect_bids`` is a pure function of three positional arguments and takes **no eligibility
argument** — D54 is explicit that the R12 gate lives in the orchestration layer above,
which hands this function an already-eligible roster. Putting a gate here would make the
function deny whenever no port was injected, which is the wrong default for a pure helper
and the wrong layer for a system guarantee.


The discount wall (T-177)
-------------------------

There is one thing this function *does* judge about a bid's content, and it is here because
this is the only place in the exchange that holds both halves of the comparison: the store's
answer, and **the roster row it was asked from**. A rostered row carries the product's
``list_price`` and, when the caller supplied one, a ``max_discount_pct`` — the deepest discount
the exchange is told is authorized on that product. It is *told*: C3/S7 forbids the exchange from
ever reading a merchant's `Envelope` itself, so this number arrives with the roster and is only as
trustworthy as the roster is. See the last section here and `RosterEntry` in ``routes.py``.

A bid that DECLARES a discount is making a claim about authorization, and until this the only
wall checking that claim ran inside our own store-agent runtime, on the emitting side. A Tier-2
store does not run our runtime. Measured on the boundary before this: a bid declaring 20% and
charging 15.00 for a product the exchange lists at 100.00 came back ``ok=True, reasons=[]``, and
so did the same bid declaring 85% — the depth bounded the price and the bid chose the depth.
:func:`contracts.boundary.price_reasons` is that wall; this module runs it, and a bid that fails
it is replaced by the store's list-price fallback with ``fallback_reason`` naming why.

Silence is not an exemption
---------------------------

The wall used to run only on offers DECLARING a non-zero discount, and that handed the emitter the
switch again one spelling over. Measured through ``POST /auctions`` before this change, on a roster
row ``{product_ref: 'prod-1', list_price: 100.0, max_discount_pct: 20.0}`` answered by a bid with
**no** ``discount`` key at ``unit_price: 0.0``::

    HTTP 201  entries=[{store_id: 's1', fallback: false, unit_price: 0.0, fallback_reason: null}]

The same bid and the same row handed straight to the boundary
(:func:`contracts.boundary.price_reasons`) come back ``['price_under_declared_depth:offer.unit_price']``.
The refusal already existed; this module simply declined to ask for it. An undeclared price under
list IS a discount — one that entered the bid through no hook at all — so the question asked here is
no longer "did the offer declare one" but **"does the exchange hold a statement to judge it
against"**, and there are three ways it does:

1. **The offer declares a discount.** Always judged: it is asserting an authorization, and an
   assertion is checkable on its own terms.
2. **The roster row states a ``max_discount_pct``.** Judged whether or not a discount is declared.
   An implicit depth is not a different animal from a declared one — 15.00 against a 100.00 list
   under a 20% cap is an 85% discount however the paperwork is spelled — and a row that names the
   authorized depth is exactly the statement needed to say so.
3. **Neither of those, and the offer gives the product away.** Judged. A non-positive price for a
   product the roster prices above zero is not an aggressive bid, it is a free item, and it is
   refused *whatever the request body claims the cap is* — see :func:`_priced_at_nothing`.
4. **Neither of the first two, and the offer's price is positive but under the floor.** Judged, and
   for exactly the same reason as case 3, which is why the two were one function until T-223
   measured the gap between them: the free item was tested for with an EXACT equality at zero, so
   the answer to it was to write ``0.001`` instead of ``0.0`` and be admitted at HTTP 201 as a
   rankable bid for a product listed at 100.00. A thousandth of a cent is not an undercut, it is
   the absence of a price wearing a positive sign. See :func:`_below_the_price_floor`.
5. **The price is not a number the exchange can read.** Judged on every row, with no roster term in
   the question at all. This one is not about pricing policy: ``float("cheap")`` raises, and the
   exception escaped this function, ``solicit_bids`` and the route as an unauthenticated HTTP 500
   whenever the caller wrote ``list_price: 0.0`` and switched case 3 off. See
   :func:`_price_is_unreadable`.

Cases 3, 4 and 5 are the ones that do not consult ``max_discount_pct``, and that is deliberate:
every other relation in the price walk is an inequality against the authorized depth, and the
depth arrives on an unauthenticated request body, so a wall built only out of those has a setting
that turns it off.

Everything else — an undeclared undercut on a row that states no authorized depth — is admitted,
and that is not a gap left open by preference. **R10 forces it.**
``.swarm-loop/acceptance/test_e3_exchange.py::test_every_store_is_represented_including_silent_and_tier0``
rosters ``{"store_id": "store-r1", "tier": 1, "product_ref": "product-1", "list_price": 120.0}``
with no ``max_discount_pct``, answers it with ``{"product_ref": "product-1", "unit_price": 80.0,
"total_price": 80.0}`` carrying no ``discount`` at all, and then asserts ``fallback is False`` and
``_unit_price(...) == 80.0``. That is a 33.3% *undeclared* undercut the frozen suite requires the
exchange to keep, and with no authorized depth on the row there is no line the exchange can draw
between an aggressive auction bid and an unauthorized discount.

What R10 does **not** require is that 0.00 be admitted. No case in any frozen acceptance suite
offers a zero or negative price — ``grep -rn 'unit_price": 0' .swarm-loop/acceptance/`` is empty —
so case 3 closes the free item without touching a single frozen assertion. An earlier pass read the
80.00 case as covering 0.00 as well and pinned all three prices as admitted; the frozen text does
not say that, and the pin was wider than the constraint.

It does not require 0.001 either, and case 4 is bounded by the same reading. The lowest price any
frozen or standing assertion holds as admitted on a 100.00 row is **1.00**
(``test_auction_price_wall.py::test_r10_still_admits_an_undeclared_undercut_where_nothing_is_authorized``),
so the floor's ceiling is 1% of list; :data:`PRICE_FLOOR_FRACTION` sits an order of magnitude
under that. The floor is a threshold, never an equality — an equality is what a bid steps over by
adding a digit.

Where the cap comes from, and where it does not
-----------------------------------------------

``max_discount_pct`` reaches this function on the roster, and the roster reaches the exchange on an
**unauthenticated ``POST /auctions`` request body** (``RosterEntry`` in ``routes.py``; the exchange
has no authentication of any kind — ``git grep -nE "Depends|api_key|Authorization" apps/exchange/src``
is empty). Whatever the caller says the cap is, is the cap — and the same is true of ``list_price``,
so the trust problem is the whole roster, not this one field.

The obvious repair — read the merchant's approved envelope — is **forbidden here, deliberately**.
``protocol.schema.json``'s `Envelope` says it "NEVER crosses into the exchange (C3/S7): it is sealed
state", and the C3 contract in ``.importlinter`` enforces exactly that by forbidding ``exchange``
from importing ``merchant_svc.envelope``. (That contract is cited by its C3 name rather than spelled
out, because the frozen C3/S7 acceptance check scans string literals in this package and a docstring
quoting the rule's full name trips the rule itself.) So an authoritative cap needs a
*derived-authorization port* — the shape R12's eligibility already uses, where the exchange consults
a versioned ``SellerEligibility`` interface rather than the trust ledger — and that port does not
exist in this codebase yet. Case 3 above is the part that does not wait on it: it holds at
``max_discount_pct: 100`` and on a row with no cap at all.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from contracts.boundary import (
    HOSTED_PATH,
    MAX_DISCOUNT_ROSTER_KEY,
    REASON_CLAIM_PROVENANCE_EMPTY_SOURCE,
    REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE,
    REASON_CLAIM_VALUE_UNWALKABLE,
    REASON_CLAIM_WITHOUT_PROVENANCE,
    REASON_CODE_UNUSABLE,
    REASON_DISCOUNT_OVER_AUTHORIZED_DEPTH,
    REASON_HOSTED_NON_HOOK_PROVENANCE,
    REASON_OFFER_EXPIRED,
    REASON_OFFER_EXPIRY_MISSING,
    REASON_OFFER_EXPIRY_UNPARSEABLE,
    REASON_PRICE_UNDER_DECLARED_DEPTH,
    REASON_PRICE_UNRECONCILABLE,
    REASON_SCHEMA_INVALID,
    REASON_STORE_BLACKLISTED,
    REASON_TRUST_SNAPSHOT_UNAVAILABLE,
    REASON_UNKNOWN_PATH,
    REASON_UNVERIFIABLE_CLAIM_SITE,
    REASON_UNVERIFIED_DISCOUNT_AUTHORISATION,
    ROSTER_PRICE_BELOW_FLOOR,
    price_floor,
    price_reasons,
    validate_bid,
)
from contracts.boundary import MINIMUM_PAYABLE_AMOUNT as _MINIMUM_PAYABLE_AMOUNT
from contracts.boundary import PRICE_FLOOR_FRACTION as _PRICE_FLOOR_FRACTION

from .state import AUCTION_TTL_SECONDS

__all__ = [
    "BOUNDARY_REASONS_ANSWERED_ELSEWHERE",
    "BidEntry",
    "FALLBACK_OFFER_TTL_SECONDS",
    "FALLBACK_REASONS",
    "FAN_OUT_CAPACITY_REASON",
    "FAN_OUT_MINTED_REASONS",
    "ILLEGIBLE_OFFER_REASON",
    "MALFORMED_RESPONSE_REASONS",
    "MAX_REFUSAL_DETAIL_LENGTH",
    "MINIMUM_PAYABLE_AMOUNT",
    "NOT_ASKED_FIELD",
    "NO_AGENT_DETAIL",
    "NO_AGENT_FIELD",
    "NO_AGENT_REASON",
    "PRICE_BELOW_FLOOR_REASON",
    "PRICE_FLOOR_FRACTION",
    "PROVENANCE_REASON_PREFIXES",
    "REFUSAL_FIELD",
    "RESPONSE_TIMED_OUT_REASON",
    "STORE_DECLINED_REASON",
    "STORE_REFUSED_REASON",
    "TIMED_OUT_FIELD",
    "UNDISCLOSED_REFUSAL_DETAIL",
    "UNPROVENANCED_CLAIM_REASON",
    "UNRECONCILABLE_PRICE_REASON",
    "collect_bids",
    "fallback_expires_at",
    "fallback_reason_family",
    "refusal_reason",
]

#: How long past the auction's close a manufactured list-price offer stays live (R10).
#:
#: The auction's OWN TTL rather than a number invented here. DESIGN pins ``auction:{id}`` at
#: fifteen minutes (:data:`~apps.exchange.src.auction.state.AUCTION_TTL_SECONDS`) and
#: ``ranking.serving.ShortlistStore`` uses the same duration, so a fallback lives about as long
#: as the shortlist that advertises it and the record that explains it.
#:
#: **About**, not exactly, and the first draft of this comment said "forgets on the same clock",
#: which is measurably false in BOTH directions.  The two are anchored at different instants: an
#: offer expires at ``deadline + TTL`` while the shortlist expires at ``closed_at + TTL``, and
#: ``closed_at`` is a second clock reading taken after the fan-out returns.  A fan-out that
#: finishes early leaves ``closed_at < deadline`` and the offer outlives the shortlist;  one that
#: overruns its window leaves ``closed_at > deadline`` and the offer dies first.  Measured:
#: ``window=10.0`` with instant replies gave the offer ``+9.998s`` of life past the shortlist,
#: and ``window=2.0`` against a 5-second solicitor gave ``-0.013s``.  The skew is bounded by the
#: bid window — seconds against fifteen minutes — so it changes nothing about which offers are
#: shown;  it is written down because "the same clock" is the kind of claim a later reader would
#: build on.
FALLBACK_OFFER_TTL_SECONDS: float = float(AUCTION_TTL_SECONDS)

#: Why an entry ended up at list price. Recorded on the entry so a downstream reader (the
#: ranker, a loss report, an operator) never has to guess between "nobody home" and "too
#: late" — they are different store behaviours with different consequences.
#:
#: The three *malformed* reasons used to be collapsed into ``response_after_deadline``,
#: which told an operator a lie: a store that answered inside the window with a broken
#: payload was reported as slow. Those are different faults with different owners — one is
#: the store's network, the others are the store's serializer or the transport between us —
#: and only one of them is evidence about latency. A loss report built on the collapsed
#: label would blame the wrong thing, and a store arguing it answered in time would be
#: right.
#: The store answered, in time, with a well-formed bid — and the discount it declared does not
#: reconcile against the roster row it was asked from (T-177). A seventh reason rather than a
#: shrug: this is a *policy* refusal, not a transport fault and not latency, and an operator
#: reading a loss report must not see "you were slow" when what happened is "you declared a
#: discount the exchange has no authorization for". The entry keeps the store's real
#: :attr:`BidEntry.price_reasons` alongside it so the rejection can be quoted back verbatim.
UNRECONCILABLE_PRICE_REASON = "bid_price_unreconcilable"

#: The boundary's verdict on an offer it cannot read as a record AT ALL — `"offer": []`,
#: `"offer": "free"`, no `offer` key. :func:`contracts.boundary.price_reasons` answers those with
#: an empty list, because a boundary that cannot find an offer has nothing to say ABOUT one; and
#: an empty list means "no refusal", so the store was admitted — at :attr:`BidEntry.unit_price`'s
#: `float(offer.get("unit_price", 0.0))` default of **0.00**. Measured before this existed, on a
#: rostered row listing at 100.00::
#:
#:     offer=[]  ->  fallback=False  unit_price=0.0  price_reasons=[]
#:
#: which is the same free item as a 0.00 bid, reached by sending no price at all instead of a
#: cheap one. The exchange does not get to read the boundary's silence as an admission, so an
#: illegible offer is named here and refused. Spelled in the boundary's own vocabulary because
#: that is what `BidEntry.price_reasons` carries.
ILLEGIBLE_OFFER_REASON = f"{REASON_PRICE_UNRECONCILABLE}:offer:illegible"

#: The floor's three constants, **re-exported from the shared boundary rather than defined here**
#: (T-250). They were defined here, with the arithmetic, because the boundary's own floor was an
#: exact equality at zero and this door had to close the gap in front of it. The boundary now
#: carries the threshold itself, so a second copy of the number would be a second thing to keep in
#: step — and a floor that differs between the shared boundary and the door in front of it is the
#: same defect the threshold replaced, wearing a different number. The names stay, so that a caller
#: or a test importing them from `exchange.auction.collect` still resolves; only the definitions
#: moved. See :func:`contracts.boundary.price_floor` for both halves and for the R10 ceiling that
#: bounds :data:`~contracts.boundary.PRICE_FLOOR_FRACTION` from above.
MINIMUM_PAYABLE_AMOUNT = _MINIMUM_PAYABLE_AMOUNT
PRICE_FLOOR_FRACTION = _PRICE_FLOOR_FRACTION
PRICE_BELOW_FLOOR_REASON = ROSTER_PRICE_BELOW_FLOOR

#: The store ANSWERED, and its answer was a refusal. Two words, because the store agent
#: contract makes them two different acts and an operator has to tell them apart:
#:
#: ``store_declined``
#:     the published ``204`` — the contract's own word for "I choose not to bid" — carrying
#:     its ``x-proxyshop-decline-reason`` as the detail when the agent stated one.
#: ``store_refused``
#:     any other status the exchange cannot read a bid out of, carrying that status as the
#:     detail. A ``422`` here means the SOLICITATION was rejected, which is the exchange's own
#:     fault and not the store's, and is precisely the case that used to be invisible.
#:
#: Before these existed the solicitor mapped every non-200 to ``None`` and ``collect_bids``
#: labelled the store ``no_response``, so a store that declined, a store that rejected a
#: malformed request and a store that was switched off were one indistinguishable fact.
#: Measured on this tree, one real store agent, ``POST /auctions`` with no ``profile``::
#:
#:     agent, profile={} -> 422 {"detail":[{"type":"missing","loc":["body","profile",
#:                               "pseudonym"],"msg":"Field required","input":{}}, ...]}
#:     entries -> [{"store_id": "store-alpha", "fallback": true,
#:                  "fallback_reason": "no_response"}]
#:
#: A refusal the buyer cannot see is worse than an error.
STORE_DECLINED_REASON = "store_declined"
STORE_REFUSED_REASON = "store_refused"

#: R8/S5a. The store answered, in time, with a well-formed bid — and a claim in it carries no
#: provenance the hosted path admits.
#:
#: A seventh named refusal for the same reason the price one is a seventh rather than a shrug:
#: this is a *provenance* refusal, not a transport fault, not latency and not an unauthorized
#: depth, and an operator reading "you were slow" when what happened is "you stated a fact and
#: could not say where it came from" is being told the wrong thing about their own agent.
#:
#: **Measured before this existed** (``POST /auctions``, one store, list price 200, varying only
#: the claim's provenance)::
#:
#:     owner_statement (control)                        -> real bid, [store-confirmed], 0.545
#:     {} / source:"" / seller_asserted / null / absent -> real bid, SHORTLISTED,        0.545
#:
#: The bid was admitted, scored identically to the honest one and merely relabelled
#: ``[unverified]`` — which is a rendering choice, not a boundary. ``validate_bid`` had no call
#: site anywhere in ``apps/exchange/src``; every occurrence there was a comment about its own
#: absence, and every real call was a test.
UNPROVENANCED_CLAIM_REASON = "bid_claim_unprovenanced"

#: The refusal families this door ENFORCES out of :func:`contracts.boundary.validate_bid`'s
#: verdict: R8/R18/S5's provenance walk, at every claim-bearing site inside the bid.
#:
#: This clause is kept and the others are not, and the reason is evidence rather than taste.
#: The provenance question is decidable from the bid ALONE — no roster, no snapshot, no clock —
#: so this call site can answer it completely. Every other clause needs evidence this function
#: does not hold, or is already answered by named machinery on the same served path; see
#: :data:`BOUNDARY_REASONS_ANSWERED_ELSEWHERE`, which names each one and where.
PROVENANCE_REASON_PREFIXES: frozenset[str] = frozenset(
    {
        REASON_CLAIM_WITHOUT_PROVENANCE,
        REASON_CLAIM_PROVENANCE_EMPTY_SOURCE,
        REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE,
        REASON_HOSTED_NON_HOOK_PROVENANCE,
        REASON_UNVERIFIABLE_CLAIM_SITE,
        REASON_CLAIM_VALUE_UNWALKABLE,
        REASON_UNVERIFIED_DISCOUNT_AUTHORISATION,
    }
)

#: The refusal families this door deliberately does NOT act on, each with the thing that
#: answers it instead. Written down as data rather than as prose because
#: ``test_hosted_bid_boundary.py`` asserts that this set plus
#: :data:`PROVENANCE_REASON_PREFIXES` covers every ``REASON_*`` the shared boundary publishes —
#: so a family added to ``packages/contracts`` turns a test red instead of being dropped on the
#: floor by an allow-list nobody remembered to widen.
#:
#: ``price_*`` / ``discount_over_authorized_depth``
#:     :func:`_price_refusal`, three lines above the boundary call, runs the SAME walk with the
#:     roster row as evidence and its own R10 abstention (:func:`_is_judged`) on top. Acting on
#:     the copy inside ``validate_bid``'s verdict would report one bad price twice, under two
#:     different fallback reasons — the mislabelling ``FALLBACK_REASONS`` was split apart to end.
#: ``offer_expired`` / ``offer_expiry_missing`` / ``offer_expiry_unparseable``
#:     :func:`exchange.ranking.filters.expiry_reason`, at ranking, against the auction's CLOSE
#:     rather than against its deadline. Judging expiry here would judge it at a different
#:     instant from the one the shortlist is built at, and the two would disagree about exactly
#:     the offers that expire inside the fan-out.
#: ``store_blacklisted`` / ``trust_snapshot_unavailable``
#:     R12's three gates — :func:`exchange.orchestration.solicit_bids` before solicitation,
#:     :func:`exchange.ranking.filters.blacklist_reason` before ranking, and the accept path
#:     before checkout. ``collect_bids`` is handed an ALREADY-ELIGIBLE roster (D54) and holds no
#:     snapshot, so a verdict from here would be manufactured from the roster it was given —
#:     a tautology wearing a trust verdict's name.
#: ``schema_invalid``
#:     **Nothing.** This is a real, open hole and it is named here rather than implied: measured
#:     over this repo's own suite, 511 of 558 bids reaching this function fail
#:     ``Bid.model_validate`` — overwhelmingly on ``agent_version``/``schema_version``, which the
#:     hand-written agent doubles in ``apps/exchange/tests`` do not send. Enforcing it here is a
#:     second refusal with a false-positive class two orders of magnitude larger than this one's,
#:     and it needs its own fixture corpus before it can be turned on. ``ranking/serving.py``'s
#:     ``_slot_discount`` documents the consequence that survives: a store-written ``discount``
#:     block reaches the published shortlist and is validated there, per field, rather than here.
#: D52's two request-signing reasons
#:     **Deliberately absent from this set, from this module's imports, and from its prose as
#:     identifiers — and that absence is C3, not an oversight.** They are answered by the
#:     external door (``exchange.external_bids.routes`` ->
#:     ``store_agent.external.door.receive_bid`` -> ``validate_external_submission``): a hosted
#:     Tier-1 agent holds no key and has nothing to sign with, so ``validate_bid`` does not
#:     judge them here by default. They are not NAMED here because the frozen C3/S7 import lint
#:     (``.swarm-loop/acceptance/test_spec_criteria.py``) forbids ``apps/exchange`` importing
#:     any name containing ``envelope`` — a merchant's economic envelope is a surface this
#:     service may not read, and the lint is a name check that cannot tell that homonym from
#:     D52's cryptographic one. Importing them cost a red acceptance run, which is the lint
#:     doing its job on a false positive; the right answer is that the exchange does not need
#:     the names, not that the check should be taught an exception. The partition gate in
#:     ``test_hosted_bid_boundary.py`` accounts for both, read off the contract module by
#:     attribute, so the totality guarantee below is unaffected.
#: ``unknown_path``
#:     Unreachable: :data:`contracts.boundary.HOSTED_PATH` is a constant, not a caller's word.
#: ``code_unusable``
#:     ``contracts.boundary.minted_code_reasons``, at the mint — a code is not a bid field and
#:     ``validate_bid`` never emits this one.
BOUNDARY_REASONS_ANSWERED_ELSEWHERE: frozenset[str] = frozenset(
    {
        REASON_PRICE_UNRECONCILABLE,
        REASON_PRICE_UNDER_DECLARED_DEPTH,
        REASON_DISCOUNT_OVER_AUTHORIZED_DEPTH,
        REASON_OFFER_EXPIRED,
        REASON_OFFER_EXPIRY_MISSING,
        REASON_OFFER_EXPIRY_UNPARSEABLE,
        REASON_STORE_BLACKLISTED,
        REASON_TRUST_SNAPSHOT_UNAVAILABLE,
        REASON_SCHEMA_INVALID,
        REASON_UNKNOWN_PATH,
        REASON_CODE_UNUSABLE,
    }
)

#: What :func:`_provenance_refusal` passes for ``trust_snapshot``.
#:
#: An empty mapping, and it is not a stand-in for a snapshot: it makes the boundary's
#: eligibility clause answer ``trust_snapshot_unavailable`` for every store, which is then
#: dropped by :data:`PROVENANCE_REASON_PREFIXES`. Passing a table BUILT FROM THE ROSTER would be
#: worse than passing nothing — it would answer "eligible" with the roster's own existence and
#: make R12's clause a tautology that reads, in a verdict object, exactly like a trust check
#: that ran. The empty mapping is the honest spelling of "this call site is not the trust gate".
_NO_TRUST_EVIDENCE: Mapping[str, Any] = {}

#: Where the exchange's own solicitor writes that refusal on the response it hands back.
#:
#: The EXCHANGE writes this field, never a store: the solicitor puts the store's reply under
#: ``bid`` and everything beside it is the exchange's own record, the same rule ``received_at``
#: and ``store_id`` are stamped under (:func:`~.fanout._stamped`). :func:`_unusable_because`
#: re-normalises whatever it finds here anyway, so a solicitor that wrote something else — or a
#: store that reached the field through one — still cannot put unbounded text in the answer.
REFUSAL_FIELD = "exchange_refusal"

#: The most characters of refusal DETAIL that reach the response, and what replaces one that
#: cannot be rendered.
#:
#: The detail on a decline is a header the STORE chose, on an unauthenticated path, and it is
#: echoed once per rostered store in a ``201`` body — the same shape of lever
#: :data:`~exchange.auction.routes.MAX_IDENTIFIER_LENGTH` and
#: :data:`~exchange.auction.routes.MAX_EXCLUSION_REASONS_PER_BID` already bound. An allowlist
#: rather than an encodability check, for the reason ``store-agent``'s own header guard records:
#: screening for what raises lets through exactly the characters that make the value illegal.
MAX_REFUSAL_DETAIL_LENGTH = 64
UNDISCLOSED_REFUSAL_DETAIL = "undisclosed"

_REFUSAL_DETAIL_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


def refusal_reason(family: str, detail: Any = None) -> str:
    """One bounded fallback reason in the ``family:detail`` spelling, or the bare family.

    ``family`` is a word out of :data:`FALLBACK_REASONS` and is what a reader groups on;
    ``detail`` is the specific thing that happened and is what a reader acts on. They are one
    string because ``AuctionEntryOut.fallback_reason`` is the only per-store channel the
    published response has, and widening that model is pinned by a test that is not wrong.

    A detail that is empty, over-long, or spells anything outside the allowlist becomes
    :data:`UNDISCLOSED_REFUSAL_DETAIL` rather than being dropped: an absent detail and an
    unrenderable one are different facts, and neither is a reason to lose the family.
    """
    token = str(family).strip()
    if detail is None:
        return token
    rendered = str(detail).strip()
    if not rendered:
        return token
    if len(rendered) > MAX_REFUSAL_DETAIL_LENGTH or set(rendered) - _REFUSAL_DETAIL_CHARACTERS:
        rendered = UNDISCLOSED_REFUSAL_DETAIL
    return f"{token}:{rendered}"


def fallback_reason_family(reason: Any) -> str | None:
    """The :data:`FALLBACK_REASONS` word a recorded reason belongs to, or ``None``.

    Every reason this module records is either a bare vocabulary word or one of those words
    followed by ``:`` and a detail, so this is what a reader groups on — a loss report counting
    ``store_refused:422`` and ``store_refused:503`` separately is counting HTTP statuses, not
    store behaviours.
    """
    if reason is None:
        return None
    return str(reason).split(":", 1)[0] or None


#: **The store was solicited and was still answering when the window shut.**
#:
#: Distinct from ``no_response`` (asked, and nothing ever came back at all) and from
#: ``response_after_deadline`` (a reply that DID arrive, carrying a stamp past the close). The
#: middle case had no word, so it borrowed ``no_response``'s — and that made a slow store
#: indistinguishable from a dead one on the one path a live auction actually runs.
#:
#: ``response_after_deadline`` cannot cover it, and the reason is structural rather than a
#: matter of taste: :func:`~.fanout.parallel_fan_out` *abandons* a future that is still
#: pending at the close (``if not future.done(): continue``), so no response object for that
#: store ever reaches :func:`collect_bids` and there is nothing for the deadline comparison in
#: :func:`_unusable_because` to judge. The late-stamp branch is reachable only for a reply
#: that landed inside the wait and dated itself past the deadline. Hence a reason minted by
#: the fan-out, from what only the fan-out knows.
#:
#: Measured, hosted, with a real model writing each store's pitch: 24 samples across 4 live
#: agents answered in 1.97 s – 4.73 s against a 3.0 s window. Every agent logged ``200 OK``;
#: every entry read ``no_response``; the market silently served list prices only.
RESPONSE_TIMED_OUT_REASON = "response_timed_out"

#: **The exchange never asked, because it had no worker free to ask with.**
#:
#: :class:`~.fanout.BoundedFanOutPool` admits without blocking and answers ``None`` when every
#: worker in the process is held by a store that has not answered; the fan-out's own per-call
#: ``max_workers`` cap ends the roster the same way. Both are R10's bounded degradation and
#: both are the EXCHANGE's condition, not the store's — so reporting them as ``no_response``
#: tells an operator to go and restart a healthy agent. Named separately for exactly the
#: reason :data:`STORE_DECLINED_REASON` and :data:`STORE_REFUSED_REASON` are.
#:
#: It does not contradict ``solicited`` on the auction's own record: that list is what the R12
#: gate cleared to ask (``orchestration.solicitation``), decided before the fan-out runs. This
#: reason is what happened to one of those stores afterwards.
FAN_OUT_CAPACITY_REASON = "fan_out_capacity_exhausted"

#: Where the fan-out writes those two verdicts on a response IT mints for a store that
#: produced none.
#:
#: The same rule :data:`REFUSAL_FIELD` and ``received_at`` are written under, and here it is
#: load-bearing in a new direction: these fields are the exchange's own account of its own
#: behaviour, and a bidder that could set one would be *labelling itself* — a store answering
#: too late could send ``exchange_timed_out`` and have its lateness recorded as the exchange's
#: capacity problem, or a store with a bad payload could relabel it as a timeout. So
#: :func:`~.fanout._stamped` DELETES both from anything a store sent, and the only mappings
#: carrying them are the ones the fan-out builds itself, keyed by the store it ASKED.
TIMED_OUT_FIELD = "exchange_timed_out"
NOT_ASKED_FIELD = "exchange_not_asked"

#: The third marker, written by the SOLICITATION gate rather than by the fan-out: *this
#: exchange holds no bidding agent for this store, so nobody was dialled.*
#:
#: It exists because the exchange was making a false statement about a shop that had done
#: nothing. The deployment document gives four of its ten sellers a ``bid_endpoint`` and six
#: none; ``HttpBidSolicitor.solicit`` answers ``None`` for the six without opening a socket
#: ("a Tier-0 store, or one the registry holds no agent for, is represented at list price
#: rather than asked a question nobody is home to hear") — and the six were then reported to
#: the shopper as ``no_response``, which the buyer's own panel glosses as "switched off or too
#: slow to reach". Measured on the deployed droplet: ``solicited`` named all six stores, and
#: ``bulksupplements.com`` and ``nutricost.com`` reached the buyer's screen as shops that had
#: been asked and had stayed silent. Neither had been asked. In D55 terms that is the platform
#: putting words in an ORGANIC result's mouth: a scraped shop carried at its catalogue price
#: has declined nothing, and saying otherwise is the one thing the organic half must not do.
#:
#: Written under the same rule as the two above and DELETED by :func:`~.fanout._stamped` for
#: the same reason: it is the exchange's account of its own deployment, so a store that could
#: set it would be excusing its own silence.
NO_AGENT_FIELD = "exchange_holds_no_agent"

#: What :data:`NO_AGENT_FIELD` is reported as: the ``tier_0_no_agent`` FAMILY, with the
#: exchange's own detail naming which of the two conditions it was.
#:
#: **Why that family and not a thirteenth word.** The two are the same fact about the shop —
#: *there is no bidding agent for this exchange to ask* — and the buyer's gloss for the family
#: already says exactly that, without mentioning a tier: "means that store has no bidding agent
#: for the exchange to ask. It is on the roster from its catalogue alone, so the exchange
#: represented it at its list price without anybody having declined anything." True of a
#: catalogue-only merchant and true of a shop this deployment holds no endpoint for. What
#: differs is WHOSE decision it was — the merchant's tier, or this exchange's deployment
#: document — and that is what the detail carries, which is what ``refusal_reason`` details are
#: for.
#:
#: **And a thirteenth word is a change to files this repair may not touch.**
#: ``test_prose_counts_match_the_code`` grades the written-out count of this tuple in five
#: places, three of them under ``packages/contracts`` (``protocol.schema.json`` and the two
#: files generated from it), and the buyer's ``REASON_GLOSSES`` is a
#: ``Record<FallbackReasonFamily, …>`` that a new family would need a sentence in. A reason
#: word that reached the shopper as an unglossed machine string would be a worse answer than
#: an accurate family with an accurate detail. If the distinction is later judged to deserve
#: its own word, the schema sentence, the two generated mirrors and the buyer gloss are what
#: it costs, and this constant is the one place to change.
NO_AGENT_DETAIL = "no_bid_endpoint"

#: The whole reason string those two compose to, spelled ONCE.
#:
#: Two readers need to recognise it exactly — :func:`_unusable_because`, which writes it, and
#: :func:`~exchange.auction.routes.market_summary`, which counts it so an operator can tell a
#: catalogue-only MERCHANT from a store this DEPLOYMENT forgot to wire. Every aggregate in the
#: system groups fallback reasons by family (:func:`fallback_reason_family`), so without a count
#: keyed on the whole string the detail exists on the entry and nowhere a report can see it.
NO_AGENT_REASON = f"tier_0_no_agent:{NO_AGENT_DETAIL}"

FALLBACK_REASONS: tuple[str, ...] = (
    "tier_0_no_agent",
    "no_response",
    RESPONSE_TIMED_OUT_REASON,
    FAN_OUT_CAPACITY_REASON,
    "response_after_deadline",
    "response_carried_no_bid",
    "response_not_stamped",
    "arrival_stamp_unparseable",
    UNRECONCILABLE_PRICE_REASON,
    UNPROVENANCED_CLAIM_REASON,
    STORE_DECLINED_REASON,
    STORE_REFUSED_REASON,
)

#: The subset of :data:`FALLBACK_REASONS` the FAN-OUT mints — "no response object for this
#: store exists, and the exchange's own record of the run is what says why" — as opposed to
#: every other reason here, which is a verdict read off something a store actually sent.
#:
#: Named as a pair, exactly as :data:`MALFORMED_RESPONSE_REASONS` is, because the pair is the
#: property worth pinning: these two and only these two may be produced with no reply in hand,
#: and neither of them may ever be reachable from a store's payload. ``test_w6_hardening``
#: asserts the same partition for the malformed set; the sibling assertion for this one lives
#: in ``test_bid_window_and_market_summary``.
FAN_OUT_MINTED_REASONS: tuple[str, ...] = (
    RESPONSE_TIMED_OUT_REASON,
    FAN_OUT_CAPACITY_REASON,
)

#: The subset of :data:`FALLBACK_REASONS` that means "a reply arrived, and we could not use
#: it" — as opposed to "it arrived too late" or "nothing arrived at all".
MALFORMED_RESPONSE_REASONS: tuple[str, ...] = (
    "response_carried_no_bid",
    "response_not_stamped",
    "arrival_stamp_unparseable",
)


@dataclass
class BidEntry:
    """One rostered store's representation in an auction.

    ``bid`` is always present and always carries an ``offer``: a fallback is a real,
    rankable, list-price offer, not a hole. ``fallback`` says which kind it is.
    """

    store_id: str
    tier: int
    fallback: bool
    bid: dict[str, Any]
    received_at: float | None = None
    fallback_reason: str | None = None
    claims: list[Any] = field(default_factory=list)
    #: The boundary's own reason strings when this entry fell back because its declared discount
    #: did not reconcile (``fallback_reason == UNRECONCILABLE_PRICE_REASON``). Empty otherwise.
    #: Kept because ``fallback_reason`` is a fixed vocabulary a loss report aggregates on, while
    #: *which* relation the bid broke is what the store's operator actually needs told.
    price_reasons: list[str] = field(default_factory=list)
    #: The shared boundary's own reason strings when this entry fell back because a claim in it
    #: carried no provenance the hosted path admits (``fallback_reason ==
    #: UNPROVENANCED_CLAIM_REASON``). Empty otherwise.
    #:
    #: A SECOND field rather than more strings in ``price_reasons``, for the reason
    #: ``FALLBACK_REASONS`` was split apart in the first place: ``price_reasons`` is documented
    #: and asserted as the price wall's verdict, and a reader who found
    #: ``claim_without_provenance:offer.discount`` there would reasonably conclude the exchange
    #: had decided something about the price. It did not; it decided something about the
    #: paperwork. The two walls refuse different things and are quoted back separately.
    boundary_reasons: list[str] = field(default_factory=list)
    #: What the ROSTER lists this store's product at, as a finite positive number, or ``None``
    #: where the roster states no price the exchange can read.
    #:
    #: Carried on the entry because it is the one input the published ``price_value`` feature
    #: needs and the only place it exists: ``price_value = clamp((list_price - total_price) /
    #: list_price, 0, 1)`` (DESIGN.md:127), and ``list_price`` is on the roster row, not on the
    #: bid. Without it every served candidate reached the ranker with no ``price_value`` at
    #: all, the feature took its neutral value on every request, and the published formula's
    #: offer-value term did nothing — measured over a real socket, three stores bidding
    #: 90/100/90 against a roster listing 100/200/300 came back with bit-identical scores, and
    #: inverting every list price changed neither a score nor the shortlist.
    #:
    #: **The roster's, never the bid's.** A store cannot state what its own product lists at
    #: any more than it can state its own ``store_domain``: the list price is the other half of
    #: the comparison the buyer is being shown, so a bidder that supplied both halves would be
    #: publishing its own discount. It is read here with :func:`_number` — the same read the
    #: price wall does — so an absent, zero, negative or unreadable row states no price rather
    #: than a free one, exactly as :func:`_list_price_bid` treats the same three spellings.
    list_price: float | None = None

    @property
    def offer(self) -> dict[str, Any]:
        offer = self.bid.get("offer")
        return offer if isinstance(offer, dict) else {}

    @property
    def unit_price(self) -> float:
        """What this entry offers to charge, or ``inf`` where there is no price to read.

        ``float(self.offer.get("unit_price", 0.0))`` — the read this replaced — answered **0.00**
        for an offer stating no price at all, and 0.00 is not a neutral default on a price: it is
        the cheapest number there is, so an absent price won every ranking it reached instead of
        losing them (T-277). ``inf`` is the same absence in the fail-CLOSED direction — a price
        nothing can pay — and it is the direction the rest of this module already fails in.
        Nothing downstream is asked to order on it either: ``ranking._price_of`` drops a
        non-finite price from the published tie-break rather than comparing against it, and
        ``routes._entries_out`` reads the offer's own field rather than this property.

        A price the exchange cannot READ is the same absence, and used to be worse than one:
        ``float("cheap")`` raised ``ValueError`` from this property, out of the middle of an
        auction every other store was bidding in (T-224). It cannot arrive here on an admitted
        bid — :func:`_price_is_unreadable` refuses one — and this property no longer depends on
        that staying true.
        """
        priced = _number(self.offer.get("unit_price"))
        return math.inf if priced is None else priced


def fallback_expires_at(deadline: float) -> str | None:
    """When a manufactured list-price offer stops being live, as an ISO-8601 UTC instant.

    **R10's second half runs through this function.** A fallback used to carry no ``expires_at``
    at all, and ``ranking.filters.expiry_reason`` fails closed on an absent one — "the offer
    carries no expires_at, so it cannot be shown to be live" — so every fallback the exchange
    manufactured for itself was excluded before it could be ranked. R10 does not only say a
    silent store is *represented*; it says it "can still reach the shortlist", and an offer that
    cannot be shown to be live reaches no shortlist.

    Two numbers decide the answer and **this exchange computed both of them**: the auction's own
    close (``deadline``, struck by the route as ``opened_at + window``) and the auction's own TTL
    (:data:`FALLBACK_OFFER_TTL_SECONDS`). Nothing here reads a field a store sent — the store
    sent nothing, which is the entire reason a fallback exists, so anything it had published
    would be the wrong evidence even if it had published something.

    ISO-8601 rather than a float epoch because that is the spelling the published ``Offer``
    declares (``str | None``, ``format: date-time``); ``checkout.codes.expiry_epoch`` reads both,
    and T-182 is the measurement of what happens when the two halves of this system disagree
    about which one they mean.

    Returns ``None`` — which reads downstream exactly as the absent field always did, i.e. the
    offer is excluded — when the deadline is not a finite instant this function can render. An
    auction whose close cannot be dated cannot date the offers it closes over, and a fallback
    that guessed an expiry off an unreadable clock would be a live offer built on nothing.
    """
    seconds = _number(deadline)
    if seconds is None:
        return None
    try:
        expires = datetime.fromtimestamp(seconds + FALLBACK_OFFER_TTL_SECONDS, tz=UTC)
    except (OSError, OverflowError, ValueError):
        # A deadline far enough out of range that no calendar can render it. Undatable, so
        # unexpirable, so not shown — the same direction the absent field already failed in.
        return None
    return expires.isoformat().replace("+00:00", "Z")


def _list_price_bid(
    entry: Mapping[str, Any], auction_id: str | None, deadline: float
) -> dict[str, Any]:
    """The catalog-derived fallback offer for a store that did not (or cannot) bid.

    The read is :func:`_number`, not a bare ``float()``, and that is T-224's shape one row over.
    ``float(entry.get("list_price", 0.0))`` raises on ``list_price: "cheap"`` — from *here*, on the
    fallback path, so a silent store on an unreadable row took the whole auction down the same way
    an unreadable *bid* price did. An unreadable list price is therefore treated exactly as a
    MISSING one: as a roster that states no price the exchange can charge, which mints no offer
    at all. ``RosterEntry.list_price`` refuses both at the door.

    ``expires_at`` is the exchange's own answer, derived from ``deadline`` — see
    :func:`fallback_expires_at` for why it is R10's second half and why it can only come from
    here. The offer's OTHER missing half, a checkout destination, cannot be answered in this
    module and deliberately is not faked: the trustworthy source for that is the platform's
    seller registry, which this pure function has no handle on and must not be given one. It is
    resolved one layer out, in ``ranking.candidates``, where the registry lookup already happens
    for every candidate.

    **A row the exchange cannot price above zero mints no price and no expiry** (T-277).
    ``list_price 0.0`` is a caller stating, legibly, that the product is free, and minting an offer
    from that statement published a rankable 0.00 with no bid involved at all — the free item
    ``RosterEntry.list_price``'s ``Field(gt=0.0)`` closed at the HTTP door, still live one caller
    down. ``orchestration/solicitation.py``, ``services/sim/src/runner.py`` and the frozen
    ``test_e3_exchange.py`` all call :func:`collect_bids` directly, and this module's own docstring
    says a repair living only in a request model is one a second caller does not get. The door's
    reasoning is the reasoning here: *a caller that cannot price a product cannot auction it*. So
    the store is still REPRESENTED — R10 is not negotiable and the entry is built either way — but
    what it is represented by is not an offer: no price for a ranking to prefer, and no
    ``expires_at``, which ``ranking.filters.expiry_reason`` already fails closed on. A negative
    list price goes the same way, for the same reason and one sign further.

    **ABSENT and UNREADABLE list prices go the same way, and that is T-277's second spelling.**
    They used to keep a historical ``0.0``, which is the identical free item reached by omitting
    the field or by writing junk into it instead of writing the zero: ``_number`` answers ``None``
    for all three, and the branch above then minted a live, rankable 0.00 for a store that never
    bid. ``RosterEntry.list_price``'s own docstring already measures the absent case — ``POST
    /auctions`` with ``{"store_id": "s1", "tier": 1, "product_ref": "prod-1"}`` and no solicitor
    gave ``HTTP 201, entries=[{fallback: true, unit_price: 0.0}]`` — and
    ``test_repro_verifier_findings.py::test_t273_an_offer_stating_only_a_unit_price_is_not_turned
    _into_a_rankable_zero`` already refuses "a rankable 0.00 offer minted from a roster row that
    carries no list_price at all" one caller down. The door refuses all three spellings
    (``Field(gt=0.0, allow_inf_nan=False)``); the library callers this docstring names do not get
    that door, which is the whole reason the rule lives here. The two assertions that spelled the
    old value — in ``test_repro_untrusted_roster.py`` — recorded what the defect produced, not a
    contract anything depends on, and they are updated with this change; see the comments at each.

    The question this branch asks is therefore "can the exchange name a price it could charge for
    this product", not "did the caller write a zero", and there is exactly one answer for every
    way of failing it.
    """
    listed = _number(entry.get("list_price"))
    offer: dict[str, Any] = {
        "product_ref": entry.get("product_ref"),
        "currency": entry.get("currency", "USD"),
        # R10: a fallback is a real, rankable offer, so it has to be able to show it is
        # live. This instant is the auction's, never a store's.
        "expires_at": fallback_expires_at(deadline),
    }
    # The variant the cart permalink is built from. It is on the ROW — stated by the caller,
    # or written there by `retrieval.roster` off the same observed offer the row's price came
    # from — and this function's whole job is to carry the row onto an offer.
    #
    # OMITTED WHEN ABSENT, never defaulted, and the published `Offer.variant_ref` docstring is
    # the reason in words: "a fallback offer minted from a roster row names no variant at all.
    # Absent means 'the bid did not name one', never 'the default variant'." The reading that
    # broke checkout was `... or 1` two modules downstream, and writing a `1` here would move
    # it rather than end it.
    variant_ref = entry.get("variant_ref")
    if isinstance(variant_ref, str) and variant_ref.strip():
        offer["variant_ref"] = variant_ref.strip()
    if listed is None or listed <= 0.0:
        # The roster does not price this product at anything the exchange could charge — it
        # priced it at nothing, or it priced it in a way nothing can read, or it did not price
        # it at all. There is no offer to mint, so none is: an unpriced, undated entry is
        # refused by every filter downstream, where a 0.00 would have been preferred by every
        # one of them.
        offer["expires_at"] = None
    else:
        offer["unit_price"] = listed
        offer["total_price"] = listed
    return {
        "auction_id": auction_id,
        "store_id": str(entry["store_id"]),
        "offer": offer,
        # R10/R18/R19: a fallback carries no asserted claims. It is catalog data, so it can
        # never be the evidence that satisfies a hard constraint.
        "claims": [],
        "fallback": True,
    }


def _declares_a_discount(offer: Any) -> bool:
    """Does this offer CLAIM a discount — i.e. assert an authorization someone had to grant?

    ``True`` for a stated non-zero depth, and also for one this function cannot read: a
    ``discount`` block whose ``value`` is ``"85"`` or ``null`` is a claim made illegibly, and
    reading past it would let a bid disable the wall by making its own paperwork unreadable. The
    boundary names that case ``price_unreconcilable:offer.discount:depth_not_a_number`` rather
    than guessing, which is why it must be handed the offer rather than skipped.

    ``False`` only for an offer with no ``discount`` at all, or one declaring exactly zero — a
    discount that takes nothing off the price asserts no authorization and needs none. It is
    **not** on its own a reason to skip the wall; see :func:`_is_judged`.
    """
    discount = offer.get("discount") if isinstance(offer, Mapping) else None
    if discount is None:
        return False
    value = discount.get("value") if isinstance(discount, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    return float(value) != 0.0


def _number(value: Any) -> float | None:
    """``value`` as a real number, or ``None`` — the same read the boundary does.

    Deliberately no coercion and no ``bool``: ``"0"`` is a string a seller wrote, not a price, and
    ``True`` is not one dollar. NaN and ±inf are excluded because every comparison against them is
    silently false, which is the fail-OPEN direction on a wall. This is
    :func:`contracts.boundary._finite_number`'s predicate, restated rather than imported because
    that one is private; the two must agree, and the tests below pin the cases where it matters.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _states_an_authorized_depth(rostered: Mapping[str, Any]) -> bool:
    """Does this roster row make any statement at all about how deep a discount is authorized?

    Presence, not readability: a row carrying ``max_discount_pct: "20"`` or ``max_discount_pct:
    500`` has made a statement the exchange cannot read, and the boundary refuses that
    (``unreadable_authorized_depth``) rather than treating it as silence. Reading past an
    unreadable cap here would let a roster disarm the wall by writing garbage into the field that
    turns it on — the same "make the paperwork illegible" move :func:`_declares_a_discount` refuses
    on the bid's side.
    """
    try:
        return rostered.get(MAX_DISCOUNT_ROSTER_KEY) is not None
    except Exception:  # noqa: BLE001 - a hostile roster row is a row that states nothing
        return False


def _priced_at_nothing(offer: Any, rostered: Mapping[str, Any]) -> bool:
    """Is this offer giving away a product the roster prices above zero?

    The floor under everything else in :func:`_is_judged`, and the only one of its three cases that
    survives a caller-supplied cap of 100. A store that answers 0.00 — or -5.00, or ``"free"``, or
    ``null`` — for a product the exchange was asked to auction at 100.00 is not undercutting
    anybody; there is no auction semantics under which the winning consideration is nothing. R10
    protects the store that bids aggressively, and no frozen case anywhere asks the exchange to
    rank a zero.

    ``total_price`` counts too: a 0.00 total on an 80.00 unit is the same free item wearing the
    other field. And an *unreadable* price is a priced-at-nothing as well, because
    :attr:`BidEntry.unit_price` would go on to call ``float()`` on it — so this is also the reason a
    store answering ``unit_price: "cheap"`` degrades to its list price instead of raising
    ``ValueError`` out of the middle of an auction every other store is bidding in.

    **That last sentence used to hold only where the roster prices the product above zero, and
    this function was the reason why** (T-224). It returns ``False`` when ``listed <= 0``, so on a
    row carrying ``list_price: 0.0`` the offer was never judged at all and ``float("cheap")`` did
    raise, out of the middle of the auction, as an unauthenticated HTTP 500 — the protection was
    conditional on a number the *caller* supplies, and ``list_price: 0.0`` was an accepted roster
    value. That is closed twice over now and NEITHER repair is here: the row is refused at the
    door (``RosterEntry.list_price`` is ``Field(gt=0.0)``), and an unreadable price is judged on
    every row whatever the roster says, by :func:`_price_is_unreadable`. This function keeps its
    original subject — the free item — rather than growing a second one.

    ``False`` when the roster itself prices the product at zero or cannot price it: there is then
    no free item to detect *by this rule*, because there is no price to be under. A row that names
    no ``list_price`` is handled by the boundary's own ``list_price_unavailable`` refusal on the
    paths that reach it, and an unreadable offer price on such a row is handled by
    :func:`_price_is_unreadable`.

    ``True`` only at or below zero, and it is only HALF the floor: a positive price that is still
    not a price — 0.001, or 1e-09 — is :func:`_below_the_price_floor`'s subject (T-223). The two
    are kept apart because they report differently: this one lets the boundary name the price
    ``:not_positive`` or ``:negative`` in its own vocabulary, and that one has a name of its own.
    """
    stated = rostered.get("list_price")
    listed = _number(stated)
    if listed is None and stated is not None:
        # PRESENT BUT UNREADABLE is a caller asserting something the exchange cannot read, and it
        # is not the same statement as naming no price at all. Collapsing the two is what let a
        # rung-2 verifier mint a free item: `list_price: 1e400` is legal JSON, `inf > 0.0` is True
        # so `Field(gt=0.0)` admitted it, `_number` then excluded it as non-finite, and BOTH price
        # guards took this early-out — so the entire wall switched off on the row, while the
        # catalog fallback minted a rankable 0.00 that was the CHEAPEST offer in the auction and
        # therefore WON. Refusing here keeps the wall on without changing what price is minted:
        # the row reports through the boundary's existing `:not_positive` vocabulary rather than
        # being ranked. Presence-not-readability is `_states_an_authorized_depth`'s own rule,
        # applied to the field on the other side of the same wall.
        return True
    if listed is None or listed <= 0.0:
        return False
    if not isinstance(offer, Mapping):
        return True
    for site in ("unit_price", "total_price"):
        priced = _number(offer.get(site))
        if priced is None or priced <= 0.0:
            return True
    return False


def _below_the_price_floor(offer: Any, rostered: Mapping[str, Any]) -> bool:
    """Is this offer's price positive and still not a price? — T-223's defect, T-250's port.

    :func:`_priced_at_nothing` is the floor at exactly zero. The boundary's own used to be written
    ``priced == 0.0`` — an EXACT equality, so the answer to it was to write ``0.001`` instead of
    ``0.0``. Measured through ``POST /auctions`` before either fix, on a roster row
    ``{list_price: 100.0, max_discount_pct: 100.0}``::

        offer(0.001, 0.001)              ->  HTTP 201  fallback=false  unit_price=0.001
        offer(0.001, 0.001, depth=100.0) ->  HTTP 201  fallback=false  unit_price=0.001

    and on a row stating no cap at all, ``offer(1e-09, 1e-09)`` came back the same way — a
    99.9999999% undercut ranked as the winning consideration for a product the exchange's own
    roster prices at 100.00. Every OTHER relation in the price walk is an inequality against the
    authorized depth, so a caller-supplied ``max_discount_pct: 100`` makes all of them true at
    once; an equality at zero is the one relation no depth can satisfy, and it is defeated by
    adding a thousandth.

    **T-223 closed that here, at the exchange door only; T-250 moved the threshold into
    :func:`contracts.boundary.price_reasons` itself**, where the TypeScript peer and every other
    consumer of the shared boundary reach it too. So this function no longer REPORTS anything —
    reporting it here as well would name one bad price twice, which is the mislabelling
    :data:`FALLBACK_REASONS` was split apart to end. What is left is the one job the boundary
    cannot do from where it sits: answering :func:`_is_judged`, which decides whether the boundary
    is consulted about this offer *at all*. R10 requires an undeclared undercut on an
    unauthorized row to be ADMITTED, so silence is normally an abstention here — and a price under
    the floor is the case where it must not be.

    The threshold comes from :func:`contracts.boundary.price_floor` rather than from a copy kept
    in this module, so the door and the boundary behind it cannot drift to two different floors.

    ``False``, as :func:`_priced_at_nothing` is, when the roster cannot price the product above
    zero: there is no proportion to take of a list price that is missing, zero or unreadable. The
    row that reaches that state through the exchange's own door is refused before it gets here —
    ``RosterEntry.list_price`` is ``Field(gt=0.0)``.
    """
    listed = _number(rostered.get("list_price"))
    if listed is None or listed <= 0.0 or not isinstance(offer, Mapping):
        return False
    floor = price_floor(listed)
    for key in ("unit_price", "total_price"):
        priced = _number(offer.get(key))
        if priced is not None and 0.0 < priced < floor:
            return True
    return False


def _price_is_unreadable(offer: Any) -> bool:
    """Would ``float()`` raise on what this store called a price? — T-224's own defect.

    Deliberately a question about the OFFER alone, with no roster term in it, and that is the
    whole repair. :func:`_priced_at_nothing` already catches an unreadable price and degrades the
    store to its list price instead of raising — but only ``if listed > 0``, so the protection was
    switched on and off by a number the *caller* writes in an unauthenticated request body. On a
    row carrying ``list_price: 0.0`` nothing judged the offer, ``BidEntry.unit_price`` and the
    route's own ``_entries_out`` went on to call ``float("cheap")``, and the ``ValueError``
    escaped ``collect_bids``, ``solicit_bids`` and the route. Measured through ``POST /auctions``::

        roster=[{..., "list_price": 0.0}]  offer(unit_price="cheap")  ->  HTTP 500

    Store-shaped data is never trusted anywhere else in this module — the unparseable *arrival
    stamp* in :func:`_unusable_because` was fixed for precisely this reason — and no roster value
    a caller writes may switch that off. A price the exchange cannot read is therefore judged on
    every row, and the boundary answers it ``:not_a_number`` and falls the store back.

    ``True`` for an offer that is not a record at all, for the same reason: the walk below would
    read its prices off ``{}`` and find the ``0.0`` default. That case is named
    :data:`ILLEGIBLE_OFFER_REASON` once it reaches :func:`_price_refusal`.

    This adds nothing on a row that prices its product above zero — :func:`_priced_at_nothing`
    already returns ``True`` for every case here — so it changes behaviour only on the rows whose
    list price is missing, zero or itself unreadable, which is exactly T-224's surface.

    **STATED-BUT-UNREADABLE, not merely "not a number I can read off ``.get``"** (T-273). The
    question is about what the store WROTE, and ``_number(None)`` is ``None`` for a key that was
    never sent — which is not the same statement as a key sent unreadably. Reading the two as one
    made an offer of ``{"unit_price": 80.0}`` unreadable on account of the ``total_price`` it
    never claimed to state, so on a roster row carrying no ``list_price`` the exchange judged an
    honest 80.00 bid, the boundary refused it for a fault of the ROSTER's
    (``offer.unit_price:unreadable_roster_list_price`` — an offer site named for a roster-side
    cause, the double-reporting :data:`FALLBACK_REASONS` was split apart to end), and the store
    fell back to a list price the row does not carry: **a rankable 0.00, which wins every ranking
    there is.** The wall against a free item minted one. So a price the offer does not state is
    not a price the exchange cannot read, and it is presence-not-readability again —
    :func:`_states_an_authorized_depth`'s own rule, and :func:`_priced_at_nothing`'s, applied to
    the third field on the same wall.

    An offer stating NO price at all is still unreadable, and for the reason directly above: the
    walk below would find :attr:`BidEntry.unit_price`'s ``0.0`` default and rank it. "States no
    price" and "is not a record at all" are the same fact about an offer, and neither is a bid.
    """
    if not isinstance(offer, Mapping):
        return True
    stated = [offer[site] for site in ("unit_price", "total_price") if site in offer]
    if not stated:
        return True
    return any(_number(price) is None for price in stated)


def _tier(rostered: Mapping[str, Any]) -> int:
    """This row's tier, fail-closed — T-224's shape a third time, corrected by T-272.

    ``int(rostered.get("tier", 1))`` raises on ``tier: "one"`` (``ValueError``) and on
    ``tier: inf`` (``OverflowError``), out of the middle of :func:`collect_bids` and therefore out
    of the auction. A tier is a claim about whether a store has a bidding agent at all, so a claim
    the exchange cannot read is answered the way every other unreadable claim in this module is:
    it does not establish one. That is tier 0 — represented, at list price, never dropped — which
    is R10's own degradation rather than a hole.

    **Unreadable and "spelled differently" are not the same claim, and reading the roster with**
    :func:`_number` **collapsed them** (T-272). ``_number`` excludes ``str`` on purpose, and the
    reason it gives is about PRICES: ``"0"`` is a string a seller wrote, not an amount of money. A
    tier is neither a price nor the seller's — it is the caller's own roster row, and every caller
    that assembles one out of JSON, a CSV column or a database driver produces ``tier: "2"``. That
    read as tier 0, "no bidding agent", so the store's answer was never looked at and its
    legitimate bid was replaced by the catalog price. Measured on this module::

        roster tier "2", store bids 80.00  ->  fallback=True  tier_0_no_agent  unit_price=100.00
        roster tier  2 , store bids 80.00  ->  fallback=False                  unit_price= 80.00

    One pair of quotes, and the buyer pays twenty dollars more. The line this replaced,
    ``int(rostered.get("tier", 1))``, answered 2. Nothing reaches it through ``POST /auctions``
    — ``RosterEntry.tier`` is an ``int``, so pydantic coerces first — which is exactly why it
    survived: it is reachable only by the library callers this module's docstring names, and a
    repair that lives in a request model is one they do not get.

    So a tier SPELLED as a number is read as that number, and everything :func:`_number` refuses
    for a reason stays refused whichever side of the quotes it is written on: ``"one"``
    establishes no tier, and neither do ``"inf"``, ``"nan"``, ``True`` or ``[1]``.
    """
    stated = rostered.get("tier", 1)
    if isinstance(stated, str):
        try:
            stated = float(stated)
        except (TypeError, ValueError):
            # A tier nobody can read is not a tier. Fail closed, exactly as before.
            return 0
    number = _number(stated)
    return 0 if number is None else int(number)


def _answers_about_another_product(offer: Any, rostered: Mapping[str, Any]) -> bool:
    """Does this offer name a product that is NOT the one this row prices? (D58)

    The sixth thing the exchange holds a statement about, and it was the quiet hole. A store
    answering about a different product finds no row in :func:`_price_refusal`'s one-row
    catalogue and is therefore refused — but only if something ELSE had already made the bid
    judgeable. Measured on the served route before this clause existed, one honest hosted agent,
    same bid, same price, differing only in whether the roster row happened to state a
    ``max_discount_pct``::

        roster max_discount_pct: 20  -> refused, bid_price_unreconcilable, represented at 78.00
        roster states none           -> ADMITTED at 39.00, and price_value read 0.15

    The graph-backed roster (:meth:`exchange.retrieval.roster.SolicitedShop.as_roster_row`)
    states no ``max_discount_pct`` at all, so the second row is the one the organic half of the
    market actually gets: a price against a product the exchange never priced, admitted, and
    then credited a full ``price_value`` for a discount nobody gave — ``(78 − 39)/78``, a
    comparison between two different products' prices.

    So the mismatch is judged on its own and the two answers stop depending on an unrelated
    field. The verdict is a REFUSAL
    (:data:`contracts.boundary.ROSTER_LIST_PRICE_UNAVAILABLE`), and the store is represented at
    its rostered list price (R10) — the fail-closed direction, and the one this module has
    always taken for a price it cannot read.

    **What this deliberately does NOT do, because it was built and withdrawn (D58).** It does
    not reach for the exchange's catalogue to price the offered product and admit the bid. That
    hands the BIDDER the ``price_value`` denominator: measured, a store charging 44.00 moved the
    term from 0.0079 to its saturated 0.15 — last place to first, same money — by naming a
    sibling the crawl lists at 5000. Since ``price_value`` is what a counter-proposal would need
    in order to be ranked at all, and the exchange cannot bind ``offer.product_ref`` to what the
    checkout actually sells, a counter-proposal is represented rather than ranked. D58 carries
    the argument and the condition under which that changes.

    Both sides are read for PRESENCE, not readability, and only a row that names a product can
    disagree with an offer: a roster row naming none makes no statement for the offer to
    contradict, and an offer naming none is not "about another product" — it is the unreadable
    offer :func:`_price_is_unreadable` already answers.

    **Both refs are trimmed before they are compared**, and it is not cosmetic:
    ``RosterEntry.product_ref`` has no trim, the exchange solicits with the trimmed ref
    (``composition._solicited_product_ref``) and the agent matches its catalog key with the
    trimmed ref (``store_agent.runtime.context._solicited_ref``) — so comparing the padded
    roster spelling here would refuse a store for answering EXACTLY the question it was asked.
    Measured before this trim existed: roster ``'beanie-merino-01 '``, offer
    ``'beanie-merino-01'``, verdict ``bid_price_unreconcilable``. Whitespace is the only
    normalisation applied; case, unicode form and every other spelling are compared as written,
    because those are different keys to the catalogue too.

    Wrapped, for the reason :func:`_states_an_authorized_depth` is wrapped: this is a read that
    TURNS THE WALL ON, the roster arrives on an unauthenticated request body, and a row whose
    ``get`` or whose ``__str__`` raises would take the exception out through ``collect_bids``
    and end an auction every other store was bidding in. A row nobody can read states nothing,
    so it disagrees with nothing.
    """
    try:
        listed = rostered.get("product_ref")
        if listed is None:
            return False
        offered = offer.get("product_ref") if isinstance(offer, Mapping) else None
        if offered is None:
            return False
        return str(offered).strip() != str(listed).strip()
    except Exception:  # noqa: BLE001 - an unreadable row makes no statement about a product
        return False


def _is_judged(offer: Any, rostered: Mapping[str, Any]) -> bool:
    """Does the exchange hold a statement this offer's price can be reconciled against?

    Six ways it does, and the module docstring's "Silence is not an exemption" section carries
    the argument for each. In one line: a declared discount is a claim, a rostered
    ``max_discount_pct`` is an authorization, an offer about another product is a price the row
    does not cover, and a free item, a price under the floor and a price that is not a number
    need none of it.

    The last three do not consult ``max_discount_pct`` at all and that is the point of them:
    every other relation in the price walk is an inequality against the authorized depth, and
    the depth arrives on an unauthenticated request body, so a wall built only out of those has
    a setting that turns it off.
    """
    return (
        _declares_a_discount(offer)
        or _states_an_authorized_depth(rostered)
        or _priced_at_nothing(offer, rostered)
        or _price_is_unreadable(offer)
        or _below_the_price_floor(offer, rostered)
        or _answers_about_another_product(offer, rostered)
    )


def _price_refusal(bid: Mapping[str, Any], rostered: Mapping[str, Any]) -> list[str]:
    """The boundary's verdict on this bid's declared discount, against its own roster row.

    The one-row roster is keyed by **the ROSTER's** ``product_ref``, and the boundary looks the
    row up by the **OFFER's**. Keying it by the offer's instead would hand every bid a row
    whatever product it named — the bid would be choosing which catalog entry it is priced
    against, which is the shape of the defect this whole wall exists to close. So a store
    answering about a product it was not asked about finds no row, and an unpriceable product is
    a refusal rather than an abstention
    (:data:`contracts.boundary.ROSTER_LIST_PRICE_UNAVAILABLE`) — the direction an attacker's
    "you've never heard of that ref" would otherwise walk through.

    Empty list when :func:`_is_judged` says the exchange holds nothing to judge this offer
    against — an undeclared undercut on a row that states no authorized depth, which R10 requires
    the exchange to keep. That is the ONLY abstention left; it used to be "any offer that declares
    no discount", which admitted a 0.00 bid on a 100.00 product at the production door.

    The floor's own refusal is NOT appended here any more (T-250). It used to be, because the
    boundary genuinely had nothing to say about a price of 0.001: its floor was an exact equality
    at zero and every other relation it runs is an inequality against a depth the request body
    supplies, so ``price_reasons`` came back ``[]`` under ``max_discount_pct: 100``. The threshold
    now lives in the boundary, which names the refusal itself
    (:data:`contracts.boundary.ROSTER_PRICE_BELOW_FLOOR`), so appending a second copy would report
    one bad price twice. :func:`_below_the_price_floor` still gates :func:`_is_judged` above — that
    is the part the boundary cannot do from where it sits — and the refused store is represented at
    its list price exactly as before.
    """
    offer = bid.get("offer")
    if not _is_judged(offer, rostered):
        return []
    refused = price_reasons(bid, list_prices={rostered.get("product_ref"): rostered})
    if refused or isinstance(offer, Mapping):
        return refused
    # The boundary read no offer here, so it said nothing — and nothing means "no refusal".
    # See :data:`ILLEGIBLE_OFFER_REASON` for why silence must not be an admission.
    return [ILLEGIBLE_OFFER_REASON]


def _provenance_refusal(bid: Mapping[str, Any], deadline: float) -> list[str]:
    """R8/S5a: the shared boundary's provenance verdict on this hosted bid.

    This is ``contracts.boundary.validate_bid``'s first call site in ``apps/exchange/src``.
    Until it existed, S5a — "a claim carrying no provenance record, absent or empty, is rejected
    on every path, hosted and external" — held on the external door alone; the hosted door
    admitted the identical claim, ranked it identically, and relabelled the buyer-facing slot
    ``[unverified]``. See :data:`UNPROVENANCED_CLAIM_REASON` for the measurement.

    **Why this is the right layer, given that the module docstring calls this function pure.**
    The provenance question needs no collaborator: not the roster, not a trust snapshot, not a
    clock. It is decidable from the bytes of the bid, exactly as the price wall's arithmetic is
    decidable from the bid and the roster row — so it belongs where the exchange already holds
    the store's reply and already asks the shared boundary a question about it. Nothing is
    injected and nothing is read from the environment.

    **Why only part of the verdict is acted on.** ``validate_bid`` answers the whole R8/R18/S5
    table in one call, and three of its clauses are already answered on this served path by
    machinery that holds better evidence than this function does. Acting on the copies inside
    this verdict would report one fault twice under two fallback reasons. Each dropped family is
    named, with its real answerer, in :data:`BOUNDARY_REASONS_ANSWERED_ELSEWHERE` — including
    the one that is answered by nothing, which is written down as an open hole rather than
    left to look like a decision.

    Args:
        bid: the store's reply, as it arrived.
        deadline: the auction's close, passed as ``now`` so this stays a pure function of its
            arguments. The instant only feeds the expiry clause, whose reasons are dropped —
            but leaving it to DEFAULT would put a ``datetime.now(UTC)`` read inside a function
            this module's docstring promises is deterministic. Passed as the raw float rather
            than as a ``datetime``: ``contracts.boundary.parse_timestamp`` already reads epoch
            seconds and answers ``None`` for a value no clock can express, where
            ``datetime.fromtimestamp`` would raise ``OverflowError`` out of the middle of
            ``collect_bids`` — the exact class of escape ``_unusable_because`` exists to stop.

    Returns:
        The provenance reasons, in the boundary's own vocabulary, or ``[]``.
    """
    verdict = validate_bid(
        bid,
        path=HOSTED_PATH,
        trust_snapshot=_NO_TRUST_EVIDENCE,
        now=deadline,
    )
    return [
        reason
        for reason in verdict.reasons
        if str(reason).split(":", 1)[0] in PROVENANCE_REASON_PREFIXES
    ]


def _unusable_because(response: Mapping[str, Any], deadline: float) -> str | None:
    """Why this response cannot be counted, or ``None`` when it can.

    Every rejection here still fails **closed** — an answer the exchange cannot date is an
    answer it cannot certify arrived in time, so the store falls back to its list price —
    but each one is *named*. An arrival stamp that will not parse is not an exception
    either: this function is fed store-shaped data, and ``float("whenever")`` raises
    ``ValueError``, which used to escape ``collect_bids``, ``solicit_bids`` and the route,
    so one malformed reply took down an auction every other store was bidding in. (``nan``
    already fails the comparison; this makes the string and ``None``-ish cases agree.)

    The seven conditions are kept apart because they are seven different faults:

    the solicitor's refusal        the store ANSWERED and said no — a ``204`` decline or a
                                   status the exchange cannot read a bid out of. Read FIRST,
                                   because every other label below would describe it as an
                                   absence: a refusal carries no ``bid``, so without this it
                                   is ``response_carried_no_bid`` at best and, when the
                                   solicitor answered ``None``, ``no_response`` — a store
                                   that refused reported as a store that was switched off
    ``response_timed_out``          the fan-out's own verdict: this store was asked and was
                                    STILL ANSWERING when the window shut. Not the store's
                                    payload and not a stamp — a mapping the exchange minted
                                    for a future it abandoned, which is the only evidence
                                    that survives an abandoned future
    ``fan_out_capacity_exhausted``  the fan-out's other verdict: the exchange had no worker
                                    free, so this store was never dialled at all
    ``response_carried_no_bid``     a reply with no ``bid`` mapping — the store answered,
                                    but with nothing to rank
    ``response_not_stamped``        no ``received_at`` at all, so the exchange's own stamp
                                    never got applied; a fan-out bug, not a store's latency
    ``arrival_stamp_unparseable``   a stamp that is not a number
    ``response_after_deadline``     a reply that DID arrive and dated itself past the close

    The last one and ``response_timed_out`` are both "too slow" and are still two answers,
    because they are two different observations: one is a reply the exchange holds and can
    quote back, the other is a reply it never received. Collapsing them (as ``no_response``
    silently did) is what made a store answering at 4.5 s against a 3.0 s window read exactly
    like a container nobody had started.

    The refusal is re-normalised through :func:`refusal_reason` rather than trusted verbatim.
    :data:`REFUSAL_FIELD` is written by the exchange's own solicitor, but this is the public
    boundary and it bounds what reaches an answer, exactly as it bounds a store's prices and
    its arrival stamp: a solicitor that wrote 64 KiB there cannot spend it in a ``201`` body.

    The two fan-out markers are read as bare booleans and carry no detail for the same reason:
    they are the exchange's own words about its own run, they are stripped off anything a
    store sent (:func:`~.fanout._stamped`), and there is nothing a caller could add to them
    that this function would be right to repeat.
    """
    refusal = response.get(REFUSAL_FIELD)
    if refusal is not None and str(refusal).strip():
        stated = str(refusal).strip()
        family, _, detail = stated.partition(":")
        if family in FALLBACK_REASONS:
            return refusal_reason(family, detail or None)
        # A family this module does not publish is not half-parsed into one: the WHOLE string
        # becomes the detail, and the allowlist then answers it. Splitting an unrecognised
        # value on its first colon would report `totally_made_up:404` as a `store_refused:404`
        # — a detail the exchange took out of a string it had already decided it could not
        # read, which is a worse answer than saying it could not read it.
        return refusal_reason(STORE_REFUSED_REASON, stated)

    # Before the `bid` check, because a minted marker carries no bid and would otherwise be
    # reported as `response_carried_no_bid` — "the store answered with nothing to rank", of a
    # store that did not answer at all. After the refusal check only because the two cannot
    # co-occur: the fan-out mints a marker exactly when no response object exists, and a
    # refusal IS a response object.
    if response.get(TIMED_OUT_FIELD):
        return RESPONSE_TIMED_OUT_REASON
    if response.get(NOT_ASKED_FIELD):
        return FAN_OUT_CAPACITY_REASON
    if response.get(NO_AGENT_FIELD):
        return NO_AGENT_REASON

    bid = response.get("bid")
    if not isinstance(bid, Mapping):
        return "response_carried_no_bid"
    received_at = response.get("received_at")
    if received_at is None:
        return "response_not_stamped"
    try:
        arrived = float(received_at)
    except (TypeError, ValueError):
        return "arrival_stamp_unparseable"
    if not arrived <= float(deadline):
        return "response_after_deadline"
    return None


def collect_bids(
    roster: Sequence[Mapping[str, Any]],
    responses: Iterable[Mapping[str, Any]],
    now: float,
) -> list[BidEntry]:
    """Represent every rostered store, exactly once, in roster order.

    Args:
        roster: the stores selected for this auction —
            ``{store_id, tier, product_ref, list_price}``, optionally with
            ``max_discount_pct`` — the deepest discount the caller states is authorized on
            that product. Roster order is the output order. A row that names no
            ``max_discount_pct`` authorizes no discount at all: a bid declaring one is
            refused rather than measured against a depth it chose for itself, and falls back
            to the row's list price. A row that DOES name one is the exchange's licence to
            judge an undeclared price too — silence stops being an exemption — and a bid
            giving the product away is refused on either kind of row.
        responses: whatever came back — ``{store_id, received_at, bid}``. Responses for a
            store that is not on the roster are ignored; a store that answered twice keeps
            its **first** on-time answer, so a second, cheaper resubmission cannot displace
            the bid the store actually committed to inside the window.
        now: the fan-out **deadline**, a float epoch. A response with
            ``received_at > now`` is rejected. Named ``now`` because that is the
            parameter name D54 pins; it is the instant the auction closed.

    Returns:
        One :class:`BidEntry` per rostered store, in roster order.
    """
    deadline = float(now)

    # Materialise once: `responses` is an Iterable and is walked twice below (on-time bids,
    # then the late ones we only need in order to label a fallback honestly). A generator
    # would silently look empty the second time.
    arrived = list(responses)

    on_time: dict[str, Mapping[str, Any]] = {}
    # store_id -> why its first unusable reply was unusable. First, not last: a store that
    # sends junk and then a second reply should be reported by what it actually did first,
    # exactly as `on_time` keeps a store's first committed bid.
    rejected: dict[str, str] = {}
    for response in arrived:
        store_id = response.get("store_id")
        if store_id is None:
            continue
        unusable = _unusable_because(response, deadline)
        if unusable is not None:
            rejected.setdefault(str(store_id), unusable)
            continue
        on_time.setdefault(str(store_id), response)

    auction_ids = {
        str(r["bid"]["auction_id"])
        for r in on_time.values()
        if isinstance(r.get("bid"), Mapping) and r["bid"].get("auction_id") is not None
    }
    auction_id = next(iter(auction_ids)) if len(auction_ids) == 1 else None

    entries: list[BidEntry] = []
    for rostered in roster:
        store_id = str(rostered["store_id"])
        tier = _tier(rostered)
        answer = on_time.get(store_id)

        if tier <= 0:
            # Tier-0 is a catalog-only store: there is no agent to answer, so it is
            # represented at list price whatever arrived under its name.
            reason: str | None = "tier_0_no_agent"
        elif answer is None:
            reason = rejected.get(store_id, "no_response")
        else:
            reason = None

        refused: list[str] = []
        if reason is None and answer is not None:
            # T-177. The store answered in time with a well-formed bid; the remaining question
            # is whether the price it CHARGES is one this roster authorizes — declared or not. A
            # bid that fails that is not a bid the exchange may rank: it is a store helping itself
            # to an authorization, whether it wrote the authorization down or stayed quiet about
            # it. So it degrades to its list price like any other unusable answer, with its own
            # reason.
            refused = _price_refusal(dict(answer["bid"]), rostered)
            if refused:
                reason = UNRECONCILABLE_PRICE_REASON

        unprovenanced: list[str] = []
        if reason is None and answer is not None:
            # R8/S5a. The price is one this roster authorizes; the remaining question is whether
            # the store can say where its facts CAME from. A hosted agent's every claim had to
            # come out of a tool hook that stamped its own provenance, so a hosted bid carrying
            # a claim with none is not a policy question — it is evidence that something
            # bypassed the hooks.
            #
            # **Second, not first, and the order is deliberate rather than incidental.** A bid
            # that breaks both walls is one fault to the store's operator, not two, and the
            # price wall is the one this exchange already ran — so keeping it first leaves every
            # existing `bid_price_unreconcilable` verdict spelled exactly as it was, and this
            # refusal names only bids the price wall had nothing against. Refusing in the other
            # order would relabel bids that are already refused today, which is churn in an
            # operator-facing vocabulary bought for nothing: the bid is degraded either way.
            unprovenanced = _provenance_refusal(dict(answer["bid"]), deadline)
            if unprovenanced:
                reason = UNPROVENANCED_CLAIM_REASON

        # Read once, from the ROSTER row, and carried on both branches: the published
        # `price_value` feature is a comparison between what this row lists and what the entry
        # charges, and a fallback is a real rankable offer that has to be comparable too (R10).
        # It reads 0.0 there rather than being absent — an entry priced AT its list price
        # demonstrates no saving, which is the honest answer for a store that never bid.
        #
        # **The denominator is the roster's and stays the roster's (D58).** Feeding it the
        # platform's price for whatever product the BID named was implemented and withdrawn:
        # measured on the served route, a store charging 44.00 moved `price_value` from
        # 0.0079 to its saturated 0.15 — last place to first, same money — by naming a
        # sibling product the crawl lists at 5000. An entry admitted here is always about the
        # rostered product, because `_answers_about_another_product` refuses every other kind.
        listed = _number(rostered.get("list_price"))
        listed = listed if listed is not None and listed > 0.0 else None
        if reason is None and answer is not None:
            bid = dict(answer["bid"])
            entries.append(
                BidEntry(
                    store_id=store_id,
                    tier=tier,
                    fallback=False,
                    bid=bid,
                    received_at=float(answer["received_at"]),
                    claims=list(bid.get("claims") or []),
                    list_price=listed,
                )
            )
        else:
            entries.append(
                BidEntry(
                    store_id=store_id,
                    tier=tier,
                    fallback=True,
                    bid=_list_price_bid(rostered, auction_id, deadline),
                    received_at=None,
                    fallback_reason=reason,
                    price_reasons=refused,
                    boundary_reasons=unprovenanced,
                    list_price=listed,
                )
            )
    return entries
