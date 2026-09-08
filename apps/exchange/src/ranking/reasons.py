"""The exclusion-reason vocabulary, published once so two filters cannot phrase the same
denial two ways.

An exclusion reason is not a log line. It is the only record of *why* a candidate never
reached the formula, and a buyer-facing exclusion label is built from it, so the token that
names the filter is part of the contract rather than incidental prose. Each constant below
carries the filter's name as its prefix; the free text after the colon is the detail, and
readers match on the prefix.
"""

from __future__ import annotations

#: R12 — the trust snapshot says this store is blacklisted.
REASON_BLACKLISTED = "blacklisted_store"

#: R12 — the blacklist flag could not be read (no row, or a non-boolean value). Failing
#: closed means an unreadable read denies, exactly as a positive blacklist would.
REASON_BLACKLIST_UNREADABLE = "blacklist_unreadable"

#: R12 — an injected `SellerEligibility` source denied, or could not answer.
REASON_ELIGIBILITY_DENIED = "blacklist_source_denied"

#: The offer's own expiry has passed against the caller-supplied `now`.
REASON_EXPIRED = "expired_offer"

#: C10/D22 — the offer's checkout URL points somewhere other than the seller's registered
#: domain, compared host-for-host.
REASON_OFF_DOMAIN = "off_domain_checkout"

#: R19 — a hard constraint is not satisfied by a `verified` supporting claim.
REASON_HARD_CONSTRAINT = "hard_constraint_unsatisfied"

#: The intent's own constraint list could not be read. An intent this module cannot decide
#: excludes every candidate rather than admitting them all.
REASON_UNDECIDABLE_INTENT = "undecidable_hard_constraint"

#: The candidate record itself is malformed (no bid id, no offer, no store).
REASON_MALFORMED = "malformed_candidate"

#: The buyer stated a price bound and THIS BID'S OWN PRICE does not meet it.
#:
#: Deliberately not :data:`REASON_HARD_CONSTRAINT`, and the distinction is the whole reason
#: this constant exists. R19 decides a hard constraint on a *verified supporting claim*, and a
#: verified `price_usd` claim describes the store's CATALOGUE. What the buyer's ceiling is
#: about is the BID — and the two are different numbers, so a store whose catalogue price of
#: 40.00 verified could bid 180.00 against a 50.00 ceiling and be shortlisted. An offer's
#: price is asserted by the bidder and nothing could verify it, so it is decided structurally,
#: against the offer this exchange holds, and it never counts towards
#: `verified_hard_fit_count` (D13's first tie-break).
REASON_OVER_BUDGET = "offer_price_outside_budget"

#: A price bound was stated and this offer carries no readable price to compare against it —
#: or the bound itself is not a finite amount. An unreadable price denies on the same footing
#: an unreadable blacklist flag does (R12): a bid whose price cannot be compared must not pass
#: a price filter by being uncomparable.
REASON_PRICE_UNREADABLE = "offer_price_unreadable"

#: The PLATFORM manufactured this row and its own crawl says the product is not about what the
#: shopper asked (:mod:`exchange.retrieval.relevance`).
#:
#: It fires on ORGANIC rows only — a candidate ``collect_bids`` marked ``fallback``, where the
#: store never bid and the exchange stood a list-price offer up in its place. Measured on the
#: served route before it existed: ``"a walnut coffee table for the lounge"`` was answered with
#: four slots of liver supplements, every one of them a fallback, every one of them a product
#: the platform picked and wrote the pitch for.
#:
#: **Never on a sponsored row**, and the asymmetry is D55's rather than a softening. A store
#: that BID was solicited because the platform assigned this intent to a cluster that store
#: pursues, chose which of its own products to put forward, and made its case in its own voice
#: against a message the platform adversarially checks — it is accountable for the row and its
#: trust record moves on it. A fallback row has no such author: the platform picked the product
#: and wrote the pitch, so the platform's own crawl is the only thing that can vouch for it.
REASON_OFF_TOPIC_ORGANIC = "organic_result_off_topic"

#: NOT an exclusion reason, and deliberately outside :data:`EXCLUSION_REASON_PREFIXES`: it
#: names a constraint this auction SET ASIDE rather than a candidate it refused. It is
#: published on the ranking result (``relaxed_constraints``) and never on a row's
#: ``exclusion_reasons``, because the two say opposite things about the same candidate.
REASON_UNEVIDENCED_CONSTRAINT = "hard_constraint_unevidenced_by_every_candidate"

#: Every reason prefix this package can emit.
EXCLUSION_REASON_PREFIXES: tuple[str, ...] = (
    REASON_BLACKLISTED,
    REASON_BLACKLIST_UNREADABLE,
    REASON_ELIGIBILITY_DENIED,
    REASON_EXPIRED,
    REASON_OFF_DOMAIN,
    REASON_HARD_CONSTRAINT,
    REASON_UNDECIDABLE_INTENT,
    REASON_MALFORMED,
    REASON_OVER_BUDGET,
    REASON_PRICE_UNREADABLE,
    REASON_OFF_TOPIC_ORGANIC,
)

__all__ = [
    "EXCLUSION_REASON_PREFIXES",
    "REASON_BLACKLISTED",
    "REASON_BLACKLIST_UNREADABLE",
    "REASON_ELIGIBILITY_DENIED",
    "REASON_EXPIRED",
    "REASON_HARD_CONSTRAINT",
    "REASON_MALFORMED",
    "REASON_OFF_DOMAIN",
    "REASON_OFF_TOPIC_ORGANIC",
    "REASON_OVER_BUDGET",
    "REASON_PRICE_UNREADABLE",
    "REASON_UNDECIDABLE_INTENT",
    "REASON_UNEVIDENCED_CONSTRAINT",
]
