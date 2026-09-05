"""The dual-path bid boundary (R8 / R18 / S5).

Two doors lead into the auction and they admit different things:

* **hosted** — a Tier-1 agent running on our own runtime. Every claim it can make had to come out
  of a tool hook (`get_product_fact`, `get_owner_commitments`, `authorize_discount`, …), and each
  hook stamps its own provenance source. So a hosted bid carrying a `seller_asserted` claim is
  not a policy question: it is evidence that something bypassed the hooks, and it is rejected.
* **external** — a Tier-2 self-hosted agent behind the signed door. It CAN assert claims freely,
  so `seller_asserted` is admitted — and flagged, so the claim reaches `packages/verification`
  before it is ever shown to a buyer as fact (R18). Admitted is not the same as trusted.

**A claim is judged by where it CAME FROM, never by which field it was written in.** The Offer is
inside the bid boundary and carries claim material of its own — `offer.commitments` is a list of
`Claim`, and `offer.discount` is stamped with the same `provenance` block a claim is — so all
three sites are walked. A boundary that inspected only `bid.claims` did not have an exclusivity
property; it had a naming convention, and moving the claim one field over defeated it outright.
The external path's admit-and-flag applies at `bid.claims` alone, because that is the only site
`unverified_claim_indexes` can address; a `seller_asserted` source in the offer is refused with
`unverifiable_claim_site` rather than admitted with nobody assigned to check it.

Five conditions reject on BOTH paths, because none of them is about who is speaking:

* a claim with no `provenance` key at all, or with an empty `source`;
* an offer that charges less than the discount depth it declares prices out at (T-177) — the
  arithmetic, not the paperwork: a genuine hook-minted 20% grant is not a licence to state any
  number at all as the price. See `_price_reasons`, including what it deliberately cannot know.
  The list price that relation needs comes from the bid's own `list_price` claim, and — when
  the caller passes `list_prices` — from the EXCHANGE'S OWN ROSTER, which is the only version of
  this wall an emitter cannot disarm by staying silent;
* an offer that declares a depth DEEPER than the caller authorizes for that product. A depth is
  not authorization, it is a number the bid wrote down, and a wall that bounds the price by it
  bounds nothing: write 85 instead of 20 and 15.00 prices out fine on a 100.00 product. So a
  roster row may name the deepest discount the exchange authorizes (`max_discount_pct`, the
  policy `Envelope`'s own spelling), the declared depth is refused above it, and the price is
  measured against the AUTHORIZED depth rather than the declared one. A roster that prices a
  product but authorizes no depth for it fails closed at zero — see `_authorized_depth`;
* an offer whose `expires_at` has passed — or is missing, which fails closed;
* a store the trust snapshot marks blacklisted, or has no row for at all (R12: an unavailable
  eligibility read denies exactly like a positive one);
* a bid that is not schema-valid.

`validate_bid` never raises on bad input. A boundary that threw would make "reject" and "crash"
indistinguishable to the caller and would give the exchange nothing to log back to the seller;
every refusal comes back as `ok=False` with at least one machine-readable reason.

**The signing envelope is a FIFTH condition, and it applies to the external path only.** D52:
a submission missing any of `signer_id`, `key_id`, `issued_at`, `nonce`, `schema_version` — or
its `signature` — is rejected at the exchange boundary, before extraction and before verification,
with the same finality as a bad signature. `validate_bid` validates the bid against `Bid`, which
by design carries no envelope, so it cannot see those fields and does not check them by default:
its verdict is the R8/R18/S5 table and nothing more. Use `validate_external_submission` — or
`validate_bid(..., require_signing_envelope=True)` — for the real Tier-2 door. Calling plain
`validate_bid(bid, path="external", ...)` on a wire submission admits an UNSIGNED bid.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from contracts.protocol import Bid, BidValidationResult, ProvenanceSource, SignedBidSubmission
from contracts.signing import missing_signing_fields

#: R8: the six provenance sources a store-agent can only mint by calling a tool hook. T-040 pins
#: the hook→source table (`get_product_fact`→`scraped`, `get_live_state`→`pixel_feed`,
#: `get_owner_commitments`→`owner_statement`, `authorize_discount`→`envelope_rule`,
#: `choose_policy_action`→`learned_policy`, `get_network_prior`→`network`).
HOOK_PROVENANCE_SOURCES: frozenset[str] = frozenset(
    {
        ProvenanceSource.scraped.value,
        ProvenanceSource.pixel_feed.value,
        ProvenanceSource.owner_statement.value,
        ProvenanceSource.envelope_rule.value,
        ProvenanceSource.learned_policy.value,
        ProvenanceSource.network.value,
    }
)

#: The only source no hook produces — an assertion the seller made in free text.
NON_HOOK_PROVENANCE_SOURCES: frozenset[str] = frozenset({ProvenanceSource.seller_asserted.value})

#: The claim-bearing sites inside a `Bid` that are NOT `bid.claims`, spelled the way the reason
#: strings name them. `Claim` and `Discount` are the only objects in the protocol schema carrying
#: a `provenance` block, and the only ones reachable from a `Bid` are `bid.claims[i]`,
#: `bid.offer.commitments[i]` and `bid.offer.discount` — `Bid` forbids extra keys, so that list is
#: closed. Add a site to the schema and it must be added here, or R8 stops covering it.
OFFER_COMMITMENTS_SITE = "offer.commitments"
OFFER_DISCOUNT_SITE = "offer.discount"

#: The two priced sites on an offer, spelled the way the price reasons name them.
OFFER_UNIT_PRICE_SITE = "offer.unit_price"
OFFER_TOTAL_PRICE_SITE = "offer.total_price"

#: `Discount.type` spellings that mean "`value` is a percentage depth". A depth is the only form
#: this boundary can reconcile: an amount off cannot be compared with a price without re-deriving
#: what the offer means, and a boundary that re-derived prices would be deciding rather than
#: checking. The same three spellings the store-agent's emitting wall accepts.
PERCENTAGE_DISCOUNT_TYPES: frozenset[str] = frozenset({"percentage", "percent", "pct"})

#: The claim key under which a bid carries the list price its discount is a percentage OF.
#: `get_product_fact(product_ref, "list_price")` is the hook that mints it, so a hosted bid can
#: only carry a list price the catalog actually published. It is the only list price the boundary
#: can see when the caller supplies no roster — the boundary holds no catalog of its own, and
#: inventing a lookup it cannot perform would be worse than saying so. It is also the SAME key a
#: `list_prices` roster row may spell its number under, so one name means one thing on both sides.
LIST_PRICE_CLAIM_KEY = "list_price"

#: The `list_prices` roster row key naming the deepest percentage discount the caller AUTHORIZES
#: for a product. Deliberately the policy `Envelope`'s own spelling — `max_discount_pct`, the
#: field `authorize_discount` refuses to mint past on the emitting side, the column
#: `db/migrations/0003_sealed_vault_app_tables.sql` stores it in, and the number the merchant
#: actually approved. One name means one thing on both sides of the wall, so an exchange holding
#: an approved envelope can hand this door its cap without translating it into a shape this door
#: invented. There is NO claim by this name: a cap a bid could carry would be a cap the bid
#: chooses, which is the whole defect this closes.
MAX_DISCOUNT_ROSTER_KEY = "max_discount_pct"

#: Slack when reconciling a stated price against the price its declared depth prices out at, as
#: an absolute amount of currency rather than a float epsilon. Money is quoted to the cent, so
#: 19.99 less an honest 15% is 16.9915 and the honest rounded price sits a fraction of a cent
#: under the exact one; a wall tightened to the float would refuse almost every real product.
#: One cent is far below any discount a merchant could feel. Identical to the store-agent's
#: `PRICE_RECONCILIATION_TOLERANCE`, deliberately: the emitting wall and the validating wall
#: must not disagree about what 20% off 100.00 comes to.
PRICE_RECONCILIATION_TOLERANCE = 0.01

#: The two doors. `validate_bid` refuses anything else rather than guessing.
HOSTED_PATH = "hosted"
EXTERNAL_PATH = "external"
BID_PATHS: frozenset[str] = frozenset({HOSTED_PATH, EXTERNAL_PATH})

# Reason codes. Stable strings: the exchange logs them and the seller-facing rejection quotes
# them, so renaming one is a contract change, not a refactor.
REASON_UNKNOWN_PATH = "unknown_path"
REASON_SCHEMA_INVALID = "schema_invalid"
REASON_CLAIM_WITHOUT_PROVENANCE = "claim_without_provenance"
REASON_CLAIM_PROVENANCE_EMPTY_SOURCE = "claim_provenance_empty_source"
REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE = "claim_provenance_unknown_source"
REASON_HOSTED_NON_HOOK_PROVENANCE = "hosted_non_hook_provenance"
#: An assertable source at a claim-bearing site the verification queue has no address for.
#: `unverified_claim_indexes` names positions in `bid.claims`; a `seller_asserted` claim that
#: lives in `offer.commitments` or on `offer.discount` cannot be pointed at through it, so the
#: external path refuses it here instead of admitting something nobody will ever verify.
REASON_UNVERIFIABLE_CLAIM_SITE = "unverifiable_claim_site"
REASON_OFFER_EXPIRED = "offer_expired"
REASON_OFFER_EXPIRY_MISSING = "offer_expiry_missing"
REASON_OFFER_EXPIRY_UNPARSEABLE = "offer_expiry_unparseable"
REASON_STORE_BLACKLISTED = "store_blacklisted"
REASON_TRUST_SNAPSHOT_UNAVAILABLE = "trust_snapshot_unavailable"
REASON_SIGNING_ENVELOPE_INCOMPLETE = "signing_envelope_incomplete"
REASON_SIGNATURE_MISSING = "signature_missing"
#: The offer charges less than the depth it declares licenses. One-sided: charging MORE than the
#: depth prices out at spends less of the envelope than was declared, which is a question about
#: what the buyer is told, not about what the merchant authorised.
REASON_PRICE_UNDER_DECLARED_DEPTH = "price_under_declared_depth"
#: The depth, the price, or the list price is a number this boundary cannot read, so the offer's
#: arithmetic cannot be checked at all. Fail-closed, exactly like an unavailable eligibility read.
REASON_PRICE_UNRECONCILABLE = "price_unreconcilable"
#: The offer declares a depth deeper than the caller authorizes for that product. Distinct from
#: `price_under_declared_depth` on purpose: that one says the offer is priced under its own
#: paperwork, this one says the paperwork itself was never granted. A seller reading its
#: rejection log needs to be able to tell "your arithmetic is wrong" from "you awarded yourself
#: a discount nobody approved", because only one of those is fixable by repricing.
REASON_DISCOUNT_OVER_AUTHORIZED_DEPTH = "discount_over_authorized_depth"

#: `price_unreconcilable` suffixes for the exchange-supplied roster, named rather than spelled
#: inline so a caller can match on them without pattern-matching a sentence. A roster is EVIDENCE
#: like any other, so every way of failing to read one is its own refusal.
#:
#: * `list_price_unavailable` — the caller passed a roster and it does not price this product.
#:   A caller that supplies a roster is asserting it can price what it admits, so a product it
#:   cannot price is an unavailable read (R12's shape), not a licence to fall back to the
#:   claim-only abstention. Falling back would hand an attacker one unknown `product_ref` as a
#:   way round the entire wall.
#: * `unreadable_roster_list_price` — the row exists and is not a non-negative finite number.
#: * `list_price_contradicts_roster` — the bid CARRIES a list price and the roster names a
#:   different one. The carried claim is minted by `get_product_fact`, so it can only be a number
#:   the catalog published; disagreeing with the catalog is evidence it came from somewhere else.
#: * `authorized_depth_unavailable` — the caller passed a roster, the offer declares a non-zero
#:   depth, and nothing the caller supplied says that depth was authorized. Fail-closed for the
#:   same reason as the two above: the alternative is to accept the bid's own number as its own
#:   authorization, which is the T-177 asymmetry itself. The refused bid is not stranded — a
#:   price at or above the list price needs no authorization and is still admitted.
#: * `unreadable_authorized_depth` — a cap was supplied and is not a finite number in 0..100.
#: * `not_positive` — the roster prices this product ABOVE zero and the offer charges nothing for
#:   it. Alone among the suffixes here it is not a statement about reading the roster; it is the
#:   one refusal in the whole price walk that no declared depth and no authorized cap can talk its
#:   way out of. See the floor in `_price_reasons`.
#: * `below_price_floor` — the same floor one keystroke over: the offer's price clears zero and
#:   nothing else. Kept apart from `not_positive` deliberately, because they say different things
#:   to the operator reading a rejection: `not_positive` is "this is not a number a price can be"
#:   and this is "this is a number, and it is not enough of one". A price that is zero, negative
#:   or unreadable keeps the boundary's own name for that and never also collects this one — one
#:   bad number reported under two names is a mislabelling, not a second finding.
ROSTER_LIST_PRICE_UNAVAILABLE = "list_price_unavailable"
ROSTER_LIST_PRICE_UNREADABLE = "unreadable_roster_list_price"
ROSTER_LIST_PRICE_CONTRADICTED = "list_price_contradicts_roster"
ROSTER_MAX_DISCOUNT_UNAVAILABLE = "authorized_depth_unavailable"
ROSTER_MAX_DISCOUNT_UNREADABLE = "unreadable_authorized_depth"
ROSTER_PRICE_NOT_POSITIVE = "not_positive"
ROSTER_PRICE_BELOW_FLOOR = "below_price_floor"

#: The smallest amount of money a price is allowed to name — one minor unit of the currency the
#: exchange prices in. A number under this is not a cheap price, it is a price nobody can pay: no
#: settlement rail moves 0.001, and 1e-09 is nine orders of magnitude under the smallest coin
#: there is. It is the ABSOLUTE half of :func:`price_floor` and it is what makes the floor bite on
#: a cheap product, where a percentage of the list price is itself a fraction of a cent.
MINIMUM_PAYABLE_AMOUNT = 0.01

#: ...and the PROPORTIONAL half, as a fraction of the roster row's own list price, which is what
#: makes the floor bite on an expensive one: 0.02 on a product listed at 1,000,000.00 is a payable
#: amount and still not a price for it.
#:
#: **Why 0.1% and not something stricter.** The frozen R10 contract pins how far this may go.
#: ``apps/exchange/tests/test_auction_price_wall.py``'s
#: ``test_r10_still_admits_an_undeclared_undercut_where_nothing_is_authorized`` asserts that
#: 99.00, 80.00 **and 1.00** are all admitted on a row listing at 100.00 with no authorized depth,
#: so any floor at or above 1.00 on that row breaks a standing assertion — the ceiling on this
#: constant is therefore 1%, and 0.1% takes it with an order of magnitude of margin while still
#: refusing the 0.001 and the 1e-09 that were measured through the real door. Everything between
#: the floor and the roster's own cap remains the caller's word.
PRICE_FLOOR_FRACTION = 0.001


def price_floor(listed: float) -> float:
    """The lowest number that is still a price for a product the roster lists at `listed`.

    Both halves at once — `max` of the proportional floor and one minor currency unit — because
    each covers the case the other misses. On a 100.00 product the proportional half dominates
    (0.10); on a 0.50 product the absolute half does (0.01, where 0.1% would be five thousandths
    of a cent and would refuse nothing). The absolute half is dropped, not clamped, where the
    roster itself lists the product below one minor unit: a catalog genuinely pricing something at
    0.005 is not describing a giveaway, and a floor above its own list price would refuse every
    bid on it — closed rather than fail-closed, which is the failure mode this wall's positive
    controls exist to catch.

    Public because it is the SHARED number. The exchange's own door
    (`apps/exchange/src/auction/collect.py`) reaches the floor a second time, to decide whether it
    holds anything to judge an undeclared bid against at all, and it calls THIS function to get
    the threshold rather than keeping a copy of the arithmetic — two implementations of a floor
    are two things to keep in step, and a floor that differs between the shared boundary and the
    door in front of it is the same defect this one replaced, wearing a different number.
    """
    floor = listed * PRICE_FLOOR_FRACTION
    if listed >= MINIMUM_PAYABLE_AMOUNT:
        floor = max(floor, MINIMUM_PAYABLE_AMOUNT)
    return floor


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read `key` off a mapping or an object, without caring which it is — and never raise.

    The read itself is arbitrary caller code: `Mapping.get` on a subclass, a property, a
    `__getattr__` trap. An object whose attribute access raises turned "this bid is refused"
    into a 500 at the public boundary, which is the observable this module exists to keep
    distinct — the same reason `_eligibility_reasons` catches the unhashable-`store_id`
    `TypeError` rather than letting the lookup escape. A field that cannot be read is a field
    the bid did not state, and the walls above fail closed on exactly that.
    """
    try:
        if isinstance(obj, Mapping):
            return obj.get(key, default)
        return getattr(obj, key, default)
    except Exception:  # noqa: BLE001 - a read that raises is a field the bid did not state
        return default


def _as_plain(bid: Any) -> Mapping[str, Any]:
    for attr in ("model_dump", "dict"):
        try:
            fn = getattr(bid, attr, None)
            if not callable(fn):
                continue
            dumped = fn()
        except Exception:  # noqa: BLE001 - an object that cannot dump itself is not a Bid
            continue
        if isinstance(dumped, Mapping):
            return dumped
    if isinstance(bid, Mapping):
        return bid
    return {}


def _finite_number(value: Any) -> float | None:
    """`value` as a float when it IS a number, else `None`.

    Deliberately no coercion: `"20"` is a string a seller wrote, not a depth, and a boundary that
    parsed it would be inventing a number the bid did not state. `bool` is excluded even though
    Python calls it an `int` — `True` is not a 100% discount — and NaN/±inf are excluded because
    a comparison against them is silently false, which is the fail-OPEN direction on a wall.
    The TypeScript peer's `typeof value === "number" && Number.isFinite(value)` is this same
    predicate, so the two doors read the same set of values as numbers.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _js_truthy(value: Any) -> bool:
    """JavaScript's truthiness, evaluated in Python.

    Not a stylistic choice. `blacklisted` is read as a flag rather than compared to `True`, so
    that a row spelling it `1` or `"yes"` still denies (R12) — but Python and JavaScript disagree
    about the empty container: `bool([])` is `False` and `Boolean([])` is `true`, so a snapshot
    row reading `{"blacklisted": []}` was ADMITTED by this door and REFUSED by the TypeScript
    one. A seller who can shape the snapshot row picks whichever door lets the bid through, and
    two doors that disagree are worse than one door with a hole. JavaScript's rule is the
    fail-closed one of the two — only `false`, `0`, `NaN`, `""`, `null`/`undefined` are falsy,
    and every object, list and mapping is truthy — so it is the one both doors now use.
    """
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        number = float(value)
        return not (number == 0.0 or number != number)
    if isinstance(value, str):
        return value != ""
    return True


def _record(value: Any) -> Any:
    """`value` when it is an object with FIELDS, else `None`.

    The mirror of `readRecord` in `boundary.ts`. A string, a number, a boolean or a list has no
    fields, and `getattr(1, "blacklisted", False)` answers `False` — which is the fail-OPEN
    direction on a wall, and it is exactly how a trust-snapshot row of `1`, `"x"`, `[]`, `True`
    or `3.5` was ADMITTED here while the TypeScript door refused every one of them. Reading a
    field off something that has none is not "the field is absent"; it is "this is not the
    object you thought", and both doors now say so.
    """
    if value is None or isinstance(value, (str, bytes, bool, int, float, list, tuple, set)):
        return None
    return value


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an RFC-3339 instant, a `Z`-suffixed instant, or epoch seconds. `None` if unreadable.

    Naive values are read as UTC. A boundary that treated a missing timezone as local time would
    make expiry depend on where the process happens to run, which is not a property a contract
    can have.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _source_verdict(
    holder: Any, path: str, label: str, *, addressable: bool
) -> tuple[list[str], bool]:
    """Judge ONE provenance-bearing object. Returns (reasons, needs_verification).

    `holder` is anything carrying a `provenance` block — a `Claim` from `bid.claims` or from
    `offer.commitments`, or the `Discount` on the offer. They are judged by the same table
    because they carry the same evidence: R8 is a rule about where a statement came from, and
    it does not become a different rule because the statement was written in a different field.

    `label` is how the reason names the site. It is the bare integer index for `bid.claims`,
    because that spelling is published — `hosted_non_hook_provenance:1:seller_asserted` is
    asserted verbatim by both language suites and read by the exchange's rejection log. Every
    other site gets a dotted/bracketed path (`offer.commitments[0]`, `offer.discount`), the
    same breadcrumb shape `store-agent/hooks/provenance.py` already prints for these fields.

    `addressable` is R18's admit-and-flag, and it is available at `bid.claims` ONLY. See
    `_offer_claim_reasons` for why.
    """
    provenance = _get(holder, "provenance")
    if provenance is None:
        return [f"{REASON_CLAIM_WITHOUT_PROVENANCE}:{label}"], False

    raw_source = _get(provenance, "source")
    source = str(getattr(raw_source, "value", raw_source) or "").strip()
    if not source:
        return [f"{REASON_CLAIM_PROVENANCE_EMPTY_SOURCE}:{label}"], False

    if source in HOOK_PROVENANCE_SOURCES:
        return [], False
    if source in NON_HOOK_PROVENANCE_SOURCES:
        if path == HOSTED_PATH:
            # R8/S5. A hosted agent cannot mint this source through any hook, so its presence
            # means the statement did not come from one.
            return [f"{REASON_HOSTED_NON_HOOK_PROVENANCE}:{label}:{source}"], False
        if addressable:
            # R18. Admitted, but it goes to verification before it is shown as fact.
            return [], True
        return [f"{REASON_UNVERIFIABLE_CLAIM_SITE}:{label}:{source}"], False

    return [f"{REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE}:{label}:{source}"], False


def _claim_provenance_reasons(
    claims: Any, path: str, *, site: str | None = None
) -> tuple[list[str], list[int]]:
    """Per-claim provenance verdicts. Returns (reasons, indexes of claims needing verification).

    `site` is `None` for `bid.claims` — the published, addressable list — and the dotted path of
    the containing field for any other list of claims. Only the `bid.claims` walk can return
    indexes, because `unverified_claim_indexes` means positions in `bid.claims` and nothing else.
    """
    reasons: list[str] = []
    unverified: list[int] = []

    if claims is None:
        return reasons, unverified
    # A SEQUENCE, not any iterable. Two reasons, and both are holes this closed:
    #
    # * a one-shot iterator is CONSUMED by the walk, so the second reader of `bid.claims`
    #   (the price walk, `_carried_list_price`) saw an empty list and its wall silently did
    #   not run. `model_validate` drains it first, so even this walk saw nothing: a bid whose
    #   `claims` was a generator was admitted with every claim in it unexamined.
    # * `Array.isArray` is what the TypeScript peer asks, so a mapping, a set or a generator
    #   is `schema_invalid` there. Accepting them here made the two doors answer differently
    #   about the same payload, which is the one thing a dual-language boundary may not do.
    if isinstance(claims, (str, bytes)) or not isinstance(claims, Sequence):
        return [REASON_SCHEMA_INVALID], unverified

    try:
        walk = list(enumerate(claims))
    except Exception:  # noqa: BLE001 - a list that cannot be read is not a list of claims
        return [REASON_SCHEMA_INVALID], unverified

    for index, claim in walk:
        label = str(index) if site is None else f"{site}[{index}]"
        claim_reasons, needs_verification = _source_verdict(
            claim, path, label, addressable=site is None
        )
        reasons.extend(claim_reasons)
        if needs_verification:
            unverified.append(index)

    return reasons, unverified


def _offer_claim_reasons(offer: Any, path: str) -> list[str]:
    """R8 at the OTHER claim-bearing sites: `offer.commitments` and `offer.discount`.

    **The offer is inside the bid boundary.** `_claim_provenance_reasons` used to be handed
    `bid.claims` and nothing else, so the whole exclusivity property was defeated by MOVING the
    payload: the identical `seller_asserted` claim, relocated into `offer.commitments`, turned a
    rejection into `ok=True, reasons=[]` — with an unauthorised 25% discount riding along on the
    same offer. Same claim, same source, same bid; only the field moved. These are the only two
    other sites: `Claim` and `Discount` are the sole objects in the protocol schema that carry a
    `provenance` block, and `Bid` forbids extra keys, so nothing else reachable from a bid can.

    A non-hook source here is REFUSED on both paths, not flagged, and that asymmetry with
    `bid.claims` is deliberate. R18 does not mean "admitted"; it means "admitted *and routed to
    verification*", and the only handle the boundary hands the verification queue is
    `unverified_claim_indexes`, which is a published `array<integer>` addressing `bid.claims`.
    Renumbering it to cover offer sites would silently redirect the queue at the wrong claims,
    and admitting a claim the queue has no address for would create a worse hole than the one
    this walk closes — an assertion nobody is ever asked to verify. So an external agent may
    still assert freely; it must do so in `bid.claims`, which is the channel with an address,
    and `unverifiable_claim_site` says exactly that. When `packages/verification` exists and a
    path-qualified channel is designed for it, this refusal can become a flag; until then the
    fail-closed direction is the honest one.
    """
    if offer is None:
        return []

    reasons, _ = _claim_provenance_reasons(
        _get(offer, "commitments"), path, site=OFFER_COMMITMENTS_SITE
    )

    # The discount is judged only when there IS one — most offers carry none, and `discount` is
    # nullable by schema. But a discount that is PRESENT and carries no provenance is refused
    # like any other unprovenanced statement: it is the field that actually moves money, and
    # leaving "no provenance at all" unjudged would reopen this very exploit one step further
    # down — drop the block instead of relabelling it, and the 25% discount walks again.
    discount = _get(offer, "discount")
    if discount is not None:
        discount_reasons, _ = _source_verdict(
            discount, path, OFFER_DISCOUNT_SITE, addressable=False
        )
        reasons.extend(discount_reasons)

    return reasons


def _declared_depth(offer: Any) -> tuple[float | None, list[str]]:
    """The percentage depth THIS offer declares, and why it could not be read.

    `(0.0, [])` when the offer declares no discount at all, or declares one of zero: a discount
    that takes nothing off the price needs no authorization and prices out at the price itself.
    `(None, [reason])` when a depth is declared in a form this boundary cannot reconcile — a
    non-numeric value, a `type` that is not a percentage, a depth outside 0–100. Those are
    refusals rather than abstentions: an amount-off cannot be compared with a stated price
    without re-deriving what the offer means, and a boundary that guessed would be deciding
    instead of checking.
    """
    discount = _get(offer, "discount")
    if discount is None:
        return 0.0, []

    depth = _finite_number(_get(discount, "value"))
    if depth is None:
        return None, [f"{REASON_PRICE_UNRECONCILABLE}:{OFFER_DISCOUNT_SITE}:depth_not_a_number"]
    if depth == 0.0:
        return 0.0, []

    kind = _get(discount, "type")
    # The raw spelling reaches the reason string, the way a provenance source does — but only
    # when it IS a string. `str(2.0)` is "2.0" in Python and "2" in JavaScript, and a reason
    # code that differs between the two doors is a parity break dressed up as a diagnostic.
    spelling = kind.strip().lower() if isinstance(kind, str) else "not_a_string"
    if spelling not in PERCENTAGE_DISCOUNT_TYPES:
        return None, [f"{REASON_PRICE_UNRECONCILABLE}:{OFFER_DISCOUNT_SITE}:{spelling}"]
    if depth < 0.0 or depth > 100.0:
        return None, [f"{REASON_PRICE_UNRECONCILABLE}:{OFFER_DISCOUNT_SITE}:depth_out_of_range"]
    return depth, []


def _carried_list_price(bid: Any, offer: Any) -> tuple[float | None, list[str]]:
    """The list price the BID carries, from any `list_price` claim at any claim-bearing site.

    The boundary holds no catalog. `get_product_fact(product_ref, "list_price")` is a real hook
    and `list_price` is a real claim key, so a bid can state the number its own discount is a
    percentage of — and when it does, that number is checkable evidence rather than an oracle
    this door would have to invent.

    Two claims naming two different list prices is refused rather than resolved: an `Offer`
    names exactly one `product_ref`, so exactly one list price is coherent, and picking one of
    them (the smaller? the first?) would let a bid choose which wall it is measured against by
    adding a claim.
    """
    values: list[float] = []
    unreadable = False

    for claims in (_get(bid, "claims"), _get(offer, "commitments")):
        if claims is None or isinstance(claims, (str, bytes)) or not isinstance(claims, Sequence):
            continue
        try:
            walk = list(claims)
        except Exception:  # noqa: BLE001 - handled as `schema_invalid` by the provenance walk
            continue
        for claim in walk:
            key = _get(claim, "key")
            if not isinstance(key, str) or key.strip() != LIST_PRICE_CLAIM_KEY:
                continue
            listed = _finite_number(_get(claim, "value"))
            if listed is None or listed < 0.0:
                # A claim that says "here is the list price" and then does not state a number is
                # not an absent list price; it is an unreadable one, and reading past it would
                # let a bid disable this wall by making its own evidence illegible.
                unreadable = True
                continue
            values.append(listed)

    if unreadable:
        return None, [
            f"{REASON_PRICE_UNRECONCILABLE}:{OFFER_UNIT_PRICE_SITE}:unreadable_list_price"
        ]
    distinct = sorted(set(values))
    if len(distinct) > 1:
        return None, [f"{REASON_PRICE_UNRECONCILABLE}:{OFFER_UNIT_PRICE_SITE}:ambiguous_list_price"]
    if not distinct:
        return None, []
    return distinct[0], []


#: "the roster has no row for this product", distinct from a row that IS `None`. Both are an
#: unavailable read; the sentinel exists so one lookup can serve two readers without repeating it.
_NO_ROW = object()


def _roster_row(offer: Any, list_prices: Any) -> Any:
    """The roster's row for this offer's product, or `_NO_ROW`.

    The lookup is wrapped: `list_prices` is arbitrary caller code and `product_ref` comes off the
    wire, so an unhashable key or a `get` that raises is an ABSENT row — never a 500 at the
    public boundary, the same property `_eligibility_reasons` holds for `store_id`.
    """
    if not isinstance(list_prices, Mapping):
        return _NO_ROW
    product_ref = _get(offer, "product_ref")
    try:
        row = list_prices.get(product_ref)
    except Exception:  # noqa: BLE001 - an unhashable ref or a hostile mapping is an absent row
        return _NO_ROW
    return _NO_ROW if row is None else row


def _authorized_depth(
    offer: Any, list_prices: Any, max_discount_pct: Any
) -> tuple[float | None, list[str]]:
    """The deepest percentage the CALLER authorizes for this product, and why it is unknown.

    **This is the half of T-177 that makes the other half bind.** `_price_reasons` bounds the
    stated price by the depth the offer DECLARES, and a bound whose bound is chosen by the thing
    being bounded is not a bound: measured on the door, a bid charging 15.00 for a product the
    roster prices at 100.00 was admitted with `ok=True, reasons=[]` simply by writing 85 rather
    than 20 into `discount.value`, and at a declared 100% the same bid charged 0.00 for it. The
    catalog's real cap was enforced only on the emitting side — `authorize_discount` denies 25%
    against `prod-cap`'s 20.0 limit — which is exactly the wall a Tier-2 store never meets.

    So the cap is read from the caller, never from the bid, in this order:

    1. the roster row's own `max_discount_pct`, so a per-product envelope beats a global one;
    2. the call's `max_discount_pct`, a caller-wide ceiling for a caller holding one number
       rather than a column;
    3. nothing — and nothing is a REFUSAL, not a default. `(None, [reason])`.

    **There is no abstention for an ABSENT roster, and removing that carve-out is T-306/T-307.**
    This used to open `if list_prices is None: return None, []`, described here as "abstain, the
    legacy contract … the whole of the backward-compatibility story". Measured on this door, that
    made the argument nobody passed MORE permissive than the argument passed empty: a bid charging
    15.00 for a product the exchange prices at 100.00 while declaring 20% — 85% off behind a 20%
    authorization — was `ok=True reasons=()` with `list_prices` omitted and refused with
    `price_unreconcilable:offer.unit_price:list_price_unavailable` under `list_prices={}`. Omission
    is the call a caller makes by FORGETTING, so the accident was the one that paid, and this is
    T-233's shape one argument over — on the money path.

    Worse (T-307), that early return sat BEFORE the cap is read at all, so a caller setting
    `max_discount_pct=0` — "I authorize no discount" — and forgetting the roster had its ceiling
    dropped on the floor with nothing in the verdict saying so. The two arguments together ARE the
    price wall, and omitting one disarmed both rather than failing closed on the missing input.

    So an absent roster is now exactly an empty one, and `None` is not special anywhere in this
    walk: `_roster_row` already answers `_NO_ROW` for anything that is not a `Mapping`, so the
    omitted call and the `{}` call are indistinguishable to the byte. The cost is the DELIBERATE
    behaviour change T-306 asks for — a caller that passes no catalog is now told it cannot price
    what it admits, exactly as an empty catalog is — not a widened refusal nobody chose.

    Fail-closed here costs less than it looks. It bites only an offer that DECLARES a non-zero
    depth (a zero depth authorizes itself: it takes nothing off), and its consequence is that the
    offer is measured against the full list price. A store may still bid at or above list.
    """
    site = OFFER_DISCOUNT_SITE
    row = _roster_row(offer, list_prices)
    raw = None if row is _NO_ROW else _get(_record(row), MAX_DISCOUNT_ROSTER_KEY)
    if raw is None:
        raw = max_discount_pct
    if raw is None:
        return None, [f"{REASON_PRICE_UNRECONCILABLE}:{site}:{ROSTER_MAX_DISCOUNT_UNAVAILABLE}"]

    cap = _finite_number(raw)
    if cap is None or cap < 0.0 or cap > 100.0:
        return None, [f"{REASON_PRICE_UNRECONCILABLE}:{site}:{ROSTER_MAX_DISCOUNT_UNREADABLE}"]
    return cap, []


def _roster_list_price(offer: Any, list_prices: Any) -> tuple[float | None, list[str]]:
    """The list price the EXCHANGE holds for this offer's product, and why it could not be read.

    **There is no abstention for an ABSENT roster** (T-306). This used to open
    `if list_prices is None: return None, []` — "the whole of the backward compatibility story" —
    and that sentence was the fail-open: it made FORGETTING the argument more permissive than
    passing it empty, on the one door where the difference is money. The roster is read the same
    way whether it is absent, `None` or `{}`, so a caller that supplies no catalog is told exactly
    what a caller supplying an empty one is told.

    A roster is evidence, and every way of failing to read it is a refusal:

    * the roster does not price this product — `list_price_unavailable`;
    * it prices it with something that is not a non-negative finite number —
      `unreadable_roster_list_price`.

    Neither degrades back to silence. A caller reaching this door is asserting it can price what
    it admits, and "the roster has never heard of this `product_ref`" is exactly the string an
    attacker would put there if silence still worked — as is the argument an attacker's caller
    never passed at all.

    A row may be the number itself or a record spelling it under `list_price`, so a caller
    holding catalog rows does not have to unwrap them into a shape this door invented. The
    lookup is wrapped: `list_prices` is arbitrary caller code and `product_ref` comes off the
    wire, so an unhashable key or a `get` that raises is an unavailable read — never a 500 at
    the public boundary, the same property `_eligibility_reasons` holds for `store_id`.
    """
    site = OFFER_UNIT_PRICE_SITE
    unavailable = [f"{REASON_PRICE_UNRECONCILABLE}:{site}:{ROSTER_LIST_PRICE_UNAVAILABLE}"]
    row = _roster_row(offer, list_prices)
    if row is _NO_ROW:
        return None, unavailable

    listed = _finite_number(row)
    if listed is None:
        listed = _finite_number(_get(_record(row), LIST_PRICE_CLAIM_KEY))
    if listed is None or listed < 0.0:
        return None, [f"{REASON_PRICE_UNRECONCILABLE}:{site}:{ROSTER_LIST_PRICE_UNREADABLE}"]
    return listed, []


def _price_reasons(
    bid: Any, offer: Any, list_prices: Any = None, max_discount_pct: Any = None
) -> list[str]:
    """Reconcile the price the offer STATES against the depth it DECLARES (T-177).

    A depth is a description of a price, and until this walk existed nothing on the validating
    side made the description true. `prod-cap` lists at 100.00 with a 20% cap, so an honest 20%
    grant makes the honest price 80.00 — and 15.00 was admitted here with `ok=True, reasons=[]`
    while carrying a genuine hook-minted 20% grant, because the only wall comparing what a bid
    *charges* with what it *declares* lived in `store-agent/hooks/provenance.py`. That is the
    EMITTING side: it protects bids our own runtime produced and nothing else, and a Tier-2
    store not running our runtime walked straight past it. This is the same arithmetic on the
    door the exchange actually runs.

    **The boundary holds no hook ledger, no envelope and no catalog, and does not pretend to.**
    It reconciles only what the bid itself asserts, on two independent relations:

    * `unit_price` against the list price the bid CARRIES (`_carried_list_price`) —
      ``unit_price >= list_price * (100 - authorized_pct) / 100``, written in the same
      ``(100 - pct) / 100`` form the store-agent's grant path uses so the wall that grants and
      the wall that checks cannot round differently and call the difference fraud.
    * `total_price` against the offer's own `unit_price` —
      ``total_price >= unit_price * (100 - authorized_pct) / 100``. This one needs no catalog at
      all and is **quantity-safe**: `Offer` carries no quantity, but any quantity is at least
      one, so a total can only ever be LARGER than one discounted unit. The bound therefore
      holds whichever way `total_price` is read, which is why it can be enforced while the
      exact quantity semantics are still undecided. Applied only when a NON-ZERO depth is
      declared: at zero it degenerates into "a total is never under a unit price", which is a
      claim about quantity rather than about a discount and is not this wall's subject.

    Both are ONE-SIDED. A price *above* what the depth prices out at takes less off than was
    declared; there is nothing there for a wall about authorization to refuse, and refusing it
    would turn every rounding-up into an outage.

    Underneath both sits a floor that is not an inequality against the depth at all: when the
    ROSTER prices a product above zero, an offer of 0.00 for it is refused whatever depth is
    declared and whatever cap authorized it (`not_positive`). Every other relation here is
    satisfied by an authorized 100 — `listed * (100 - 100) / 100` is 0.00 — so without the floor
    the deepest cap a caller can write is a free item, which is exactly what was measured through
    `POST /auctions`. It is conditioned on a roster that PRICES the product, so a caller passing no
    roster — or one that does not price this product — never reaches it; that caller is refused one
    relation earlier, by `list_price_unavailable`.

    **Where the list price comes from, and why that was the whole attack.** The first relation
    originally had ONE source of a list price: the `list_price` claim the bid carries. That made
    the wall answer to evidence the emitter controls, and the emitter's counter-move was not to
    forge anything — it was to say nothing. A bid declaring 20%, charging 15.00 on a product the
    exchange lists at 100.00 and carrying no `list_price` claim was admitted `ok=True,
    reasons=[]`: the relation had no number to be a percentage of, so it abstained, and the
    second relation (15.00 total against a 15.00 unit) is satisfied by any self-consistent lie.

    So `list_prices` is the exchange's OWN roster, `{product_ref: 100.0}` or
    `{product_ref: {"list_price": 100.0}}` — the same catalog the auction was opened from. When
    it is passed, it is authoritative, it cannot be silenced by omitting a claim, and a carried
    claim that names a different number is refused rather than reconciled: `get_product_fact`
    mints that claim off the catalog, so a claim disagreeing with the catalog is evidence it came
    from somewhere else, and reading past it would restore the very move this closes one step
    over — inflate the stated list price until 15.00 looks like 20% off.

    **THE ABSENT ROSTER IS THE EMPTY ROSTER (T-306/T-307), and this paragraph used to say the
    opposite.** It read: "With NO roster passed the first relation abstains exactly as it always
    did … it is left in place rather than removed because refusing every discounted offer that
    omits the claim would refuse most honest bids from callers holding no catalog — that is not
    fail-closed, it is closed." That opt-in was documented, deliberate and pinned by tests, and it
    was still the defect: it made the argument a caller forgets MORE permissive than the argument
    a caller passes empty. Measured on this walk before the change — a hook-minted 20% grant, an
    offer charging 15.00 for a product the exchange prices at 100.00, carrying no `list_price`
    claim::

        validate_bid(bid, ...)                   -> ok=True   reasons=()
        validate_bid(bid, ..., list_prices={})   -> ok=False  reasons=(
            'price_unreconcilable:offer.discount:authorized_depth_unavailable',
            'price_unreconcilable:offer.unit_price:list_price_unavailable')

    Nothing about the second call is stricter than the first except that the caller said something.
    A default that reads which arguments were supplied to decide how permissive to be is the T-233
    fail-open with a different key, and this one is on the money path. So `None` is normalised to
    the empty roster at every one of the three sites that could see it — `_authorized_depth`,
    `_roster_list_price`, and the `authorized` fallback below — and `price_reasons(bid)` now
    answers exactly what `price_reasons(bid, list_prices={})` answers, for every bid.

    **What that costs, said plainly, because it is not small.** A caller holding no catalog no
    longer gets the claim-only walk; it gets `price_unreconcilable:offer.unit_price:
    list_price_unavailable` on every offer. That is a deliberate behaviour change, not a widened
    refusal that crept in: a door that cannot price what it admits is not entitled to admit it, and
    the caller that CAN price its products was always supposed to pass the roster — see
    `validate_external_submission`, which calls this "the door it matters most on".

    **The depth these relations use is the AUTHORIZED one, not the declared one** — see
    `_authorized_depth`. Both are written against ``authorized``, and the declared depth is never
    self-granting: it is refused above the cap, and the price is measured against what the caller
    authorized. At an authorized zero the second relation switches off for the same reason it does
    at a declared zero — ``total >= unit`` is a claim about quantity, not about a discount — and
    the first relation, now measuring against the full list price, is the one carrying the refusal.
    """
    record = _record(offer)
    if record is None:
        return []

    reasons: list[str] = []
    depth, depth_reasons = _declared_depth(record)
    reasons.extend(depth_reasons)

    unit_price = _finite_number(_get(record, "unit_price"))
    total_price = _finite_number(_get(record, "total_price"))
    if unit_price is None:
        reasons.append(f"{REASON_PRICE_UNRECONCILABLE}:{OFFER_UNIT_PRICE_SITE}:not_a_number")
    elif unit_price < 0.0:
        reasons.append(f"{REASON_PRICE_UNRECONCILABLE}:{OFFER_UNIT_PRICE_SITE}:negative")
    if total_price is None:
        reasons.append(f"{REASON_PRICE_UNRECONCILABLE}:{OFFER_TOTAL_PRICE_SITE}:not_a_number")
    elif total_price < 0.0:
        reasons.append(f"{REASON_PRICE_UNRECONCILABLE}:{OFFER_TOTAL_PRICE_SITE}:negative")

    if depth is None or unit_price is None or unit_price < 0.0:
        # Nothing arithmetic left to say: the numbers the relations are built out of are already
        # refused above, and restating them as an inequality would report one bad number twice.
        return reasons

    # The depth the offer declared is paperwork; the depth the CALLER authorized is the bound.
    # A zero depth needs no authorization — it takes nothing off — so the cap is not even
    # consulted for one, and a caller with a roster but no envelope keeps the undiscounted half of
    # its traffic behaving exactly as before.
    #
    # T-337's third site. Normalising only `_authorized_depth` and `_roster_list_price` leaves the
    # fail-open ALIVE here, because `authorized = depth if list_prices is None else 0.0` restores
    # the abstention one layer up: the omitted roster hands the offer its own declared depth back
    # as the bound, which is the self-granting cap `_authorized_depth` exists to end. Measured on
    # the T-306 property at seed 20260904, a two-site repair diverges at draw 3 through this line.
    # There is no `list_prices is None` branch left in this walk.
    authorized = depth
    if depth > 0.0:
        cap, cap_reasons = _authorized_depth(record, list_prices, max_discount_pct)
        reasons.extend(cap_reasons)
        if cap is None:
            # A roster that could not authorize this depth, and an unauthorized depth authorizes
            # nothing. An ABSENT roster is one of those rosters now (T-306), not an exemption.
            authorized = 0.0
        else:
            if depth > cap:
                reasons.append(f"{REASON_DISCOUNT_OVER_AUTHORIZED_DEPTH}:{OFFER_DISCOUNT_SITE}")
            # No tolerance on this comparison, and none is wanted: a request exactly AT the cap
            # is authorized (`authorize_discount` grants at `max_discount_pct` and denies above
            # it), and a cent of currency slack has no meaning applied to percentage points.
            authorized = min(depth, cap)
    else:
        # NO depth declared, and a roster to check against — and after T-306 there is ALWAYS a
        # roster to check against, because an absent one is an empty one. The price is still a depth — an
        # implicit one — and 15.00 for a 100.00 product is an 85% discount however the paperwork
        # is spelled. So when the roster STATES what is authorized, the undeclared price is
        # measured against that, exactly as a declared one is. Without this the caller's own cap
        # was unreachable from the silent path and every undercut on a capped row was measured
        # against the FULL list price — which refuses the honest 80.00 under a 20% authorization,
        # a wall that is closed rather than fail-closed.
        #
        # A roster that states no cap is NOT a refusal here, and the cap's own reasons are
        # dropped: `authorized_depth_unavailable` names a depth the offer claimed without
        # authorization, and this offer claimed nothing. `authorized` stays at zero, so an
        # unreadable or absent cap still measures the price against the full list price — the
        # fail-closed direction — it simply does not invent a claim to complain about.
        cap, _ = _authorized_depth(record, list_prices, max_discount_pct)
        if cap is not None:
            authorized = cap

    carried, carried_reasons = _carried_list_price(bid, record)
    reasons.extend(carried_reasons)
    rostered, roster_reasons = _roster_list_price(record, list_prices)
    reasons.extend(roster_reasons)

    # The roster wins when both are readable, and a disagreement between them is its own refusal
    # rather than a tie broken silently: letting the door pick would let a bid choose which wall
    # it is measured against by writing a claim, which is the property `_carried_list_price`
    # refuses two claims to protect.
    listed = rostered if rostered is not None else carried
    if (
        rostered is not None
        and carried is not None
        and abs(rostered - carried) > PRICE_RECONCILIATION_TOLERANCE
    ):
        reasons.append(
            f"{REASON_PRICE_UNRECONCILABLE}:{OFFER_UNIT_PRICE_SITE}:"
            f"{ROSTER_LIST_PRICE_CONTRADICTED}"
        )

    # THE FLOOR — the one relation in this walk that no depth can satisfy. Everything else here
    # is an inequality against `authorized`, so an authorized 100 makes all of them true at once:
    # `listed * (100 - 100) / 100` is 0.00, and a bid charging 0.00 for a 100.00 product is then
    # arithmetically perfect. Measured through the exchange's own `POST /auctions` before this
    # existed — roster row `{list_price: 100.0, max_discount_pct: 100.0}`, an offer declaring 100%
    # at 0.00 — `HTTP 201, entries=[{fallback: false, unit_price: 0.0}]`. That is the free item
    # this ticket is about, and it was minted by writing one number into the request body.
    #
    # A price of nothing is not a deep discount; it is the absence of a price, and no authorization
    # makes a product free. So when the ROSTER prices it above zero, an offer of zero is refused at
    # any depth and under any cap.
    #
    # Deliberately `rostered`, never `listed`: a carried claim must not be able to switch this on,
    # both because the emitter would then choose its own floor and because every no-roster verdict
    # would stop being byte-identical — with no roster, `rostered` is `None` and this is dead code.
    #
    # And it is a THRESHOLD, not an equality. It was written `priced == 0.0`, which has exactly one
    # satisfying value while every other relation in this walk is an inequality against a
    # caller-supplied depth — so the floor was one keystroke wide, and the answer to it was to
    # write `0.001` instead of `0.0`. Measured on this function at HEAD, roster
    # `{'prod-1': 100.0}` with `max_discount_pct=100.0`::
    #
    #     price_reasons(bid(0.001, depth=100.0))  ->  []
    #     price_reasons(bid(1e-09))               ->  []
    #     price_reasons(bid(0.0,   depth=100.0))  ->  [...:not_positive, ...:not_positive]
    #
    # An empty reason list is this boundary saying it has no objection, so the wall refused the
    # absence of a price spelled `0.0` and admitted the same absence spelled `0.001` — a
    # 99.999% undercut ranked as a winning consideration for a product the roster prices at
    # 100.00. `price_floor` is the number and carries the argument for why it cannot be tighter.
    #
    # The two refusals are kept apart and never both reported for one price: `not_positive` is
    # "this is not a number a price can be" (exactly zero — a negative price is already `:negative`
    # above) and `below_price_floor` is "this is a number, and it is not enough of one". One bad
    # number reported under two names is the reporting fault the fallback reasons were split apart
    # to end, which is why the second test is `0.0 < priced` and not `priced < floor` alone.
    if rostered is not None and rostered > 0.0:
        floor = price_floor(rostered)
        for site, priced in (
            (OFFER_UNIT_PRICE_SITE, unit_price),
            (OFFER_TOTAL_PRICE_SITE, total_price),
        ):
            if priced is None:
                continue
            if priced == 0.0:
                reasons.append(f"{REASON_PRICE_UNRECONCILABLE}:{site}:{ROSTER_PRICE_NOT_POSITIVE}")
            elif 0.0 < priced < floor:
                reasons.append(f"{REASON_PRICE_UNRECONCILABLE}:{site}:{ROSTER_PRICE_BELOW_FLOOR}")

    if listed is not None and unit_price + PRICE_RECONCILIATION_TOLERANCE < (
        listed * (100.0 - authorized) / 100.0
    ):
        reasons.append(f"{REASON_PRICE_UNDER_DECLARED_DEPTH}:{OFFER_UNIT_PRICE_SITE}")

    # `depth > 0` only. At zero there is no declared depth to make true, and `total >= unit`
    # would no longer be a statement about a discount — it would be an assertion that a total is
    # never under a unit price, which conflates quantity with discount and is not this wall's
    # subject. The list-price relation above still applies at zero, because a price under LIST
    # with no declared discount is a discount that entered the bid through no hook at all.
    if authorized > 0.0 and total_price is not None and total_price >= 0.0:
        if total_price + PRICE_RECONCILIATION_TOLERANCE < unit_price * (100.0 - authorized) / 100.0:
            reasons.append(f"{REASON_PRICE_UNDER_DECLARED_DEPTH}:{OFFER_TOTAL_PRICE_SITE}")

    return reasons


def price_reasons(
    bid: Any,
    *,
    list_prices: Mapping[Any, Any] | None = None,
    max_discount_pct: float | None = None,
) -> list[str]:
    """The PRICE half of `validate_bid`'s verdict, for a caller holding a catalog but no snapshot.

    `validate_bid` is the whole R8/R18/S5 door and it requires a `trust_snapshot`, because
    eligibility is one of the five conditions it answers. A caller that already ran the R12 gate
    somewhere else — the exchange's own auction collection does, D54 puts that gate one layer up
    — has no snapshot to hand this door and would have to fake one to reach the arithmetic. It
    would then get a `trust_snapshot_unavailable` refusal on every bid, or invent a permissive
    snapshot, which is worse. So the price walk is published on its own.

    It is the SAME function `validate_bid` calls, not a re-implementation: a second copy of this
    arithmetic is a second thing to keep in step with the TypeScript door, and the module already
    carries one cross-language parity table for exactly that reason.

    Returns the reason strings — empty when the offer's prices reconcile with what the caller
    authorized. It never raises.
    """
    return _price_reasons(bid, _get(bid, "offer"), list_prices, max_discount_pct)


def _expiry_reasons(offer: Any, now: datetime) -> list[str]:
    if offer is None:
        return [REASON_OFFER_EXPIRY_MISSING]
    raw = _get(offer, "expires_at")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        # Fail closed. An offer with no stated expiry is an offer nobody can price the risk of.
        return [REASON_OFFER_EXPIRY_MISSING]
    expires_at = parse_timestamp(raw)
    if expires_at is None:
        return [REASON_OFFER_EXPIRY_UNPARSEABLE]
    return [REASON_OFFER_EXPIRED] if expires_at <= now else []


def _eligibility_reasons(store_id: Any, trust_snapshot: Any) -> list[str]:
    """R12, fail-closed: blacklisted denies, and an unavailable read denies the same way."""
    if not isinstance(trust_snapshot, Mapping):
        return [REASON_TRUST_SNAPSHOT_UNAVAILABLE]
    try:
        row = trust_snapshot.get(store_id)
    except TypeError:
        # A malformed `store_id` — a dict or a list where a string belongs — is unhashable, and
        # letting the lookup raise would turn a rejectable bid into a crash at the public
        # boundary. It is not a store we know about, so it is denied like any other unknown one.
        return [f"{REASON_TRUST_SNAPSHOT_UNAVAILABLE}:{store_id!r}"]
    # A row that is not an OBJECT is not a row. `getattr(1, "blacklisted", False)` is `False`,
    # so `{"store-1": 1}` — a snapshot mangled in transit, or half-decoded — read as "present and
    # not blacklisted" and ADMITTED the store, on the one check R12 exists to make fail closed.
    # `readRecord` in the TypeScript peer has always refused these, so this was also the widest
    # remaining ok-divergence between the two doors: five row shapes, both paths.
    if _record(row) is None:
        return [f"{REASON_TRUST_SNAPSHOT_UNAVAILABLE}:{store_id}"]
    # Truthy, not `is True`: a snapshot row that spells the flag `1` or `"yes"` is still a
    # blacklisted store, and R12 says the fail-closed direction is the one to take on doubt.
    # JAVASCRIPT's truthiness specifically — see `_js_truthy`. Python's own would admit
    # `{"blacklisted": []}` that the TypeScript door refuses, and a seller who can shape the
    # snapshot row would simply submit at whichever door says yes.
    if _js_truthy(_get(row, "blacklisted", False)):
        return [f"{REASON_STORE_BLACKLISTED}:{store_id}"]
    return []


def _signing_envelope_reasons(bid: Any) -> list[str]:
    """D52: the five envelope fields plus a `signature`, or the submission is refused.

    Read off the raw submission, not off a validated `Bid`: `Bid` carries no envelope by design,
    so a schema check against it can never see these fields. That is precisely how an unsigned
    external submission used to come back `ok=True, reasons=[]`.
    """
    payload = _as_plain(bid)
    reasons = [
        f"{REASON_SIGNING_ENVELOPE_INCOMPLETE}:{field}" for field in missing_signing_fields(payload)
    ]
    signature = payload.get("signature") if isinstance(payload, Mapping) else None
    if not isinstance(signature, str) or not signature.strip():
        reasons.append(REASON_SIGNATURE_MISSING)
    return reasons


def validate_bid(
    bid: Any,
    *,
    path: str,
    trust_snapshot: Mapping[str, Any],
    now: datetime | str | float | None = None,
    require_signing_envelope: bool = False,
    list_prices: Mapping[Any, Any] | None = None,
    max_discount_pct: float | None = None,
) -> BidValidationResult:
    """Decide whether `bid` may enter the auction through `path`.

    **This function does NOT check the signing envelope unless you ask it to.** It judges the
    R8/R18/S5 table — provenance, expiry, eligibility, schema — and its schema check is against
    `Bid`, which carries no `signer_id`, `key_id`, `issued_at`, `nonce` or `signature`. A wire
    submission with none of those comes back `ok=True` from the default call. For the real Tier-2
    door use `validate_external_submission`, which is this function with
    `require_signing_envelope=True`.

    Args:
        bid: the bid, as a mapping or as any object exposing the `Bid` fields.
        path: exactly `"hosted"` or `"external"`. Anything else is refused, not guessed at.
        trust_snapshot: `{store_id: {"store_id":…, "score": float, "blacklisted": bool}}`.
            A store with no row is treated as an unavailable eligibility read and denied (R12).
        now: the instant expiry is judged against. Defaults to the current UTC time; pass it
            explicitly to keep a caller deterministic.
        require_signing_envelope: when true, D52's five envelope fields and a non-empty
            `signature` are required, each gap reported as its own reason. Off by default because
            a hosted Tier-1 agent holds no key and has nothing to sign with, and because
            `validate_bid` is also used to judge already-extracted `Bid` objects that never
            carried an envelope.
        list_prices: the caller's OWN catalog, `{product_ref: 100.0}` or
            `{product_ref: {"list_price": 100.0}}`. It is what makes the price wall stop depending
            on the bid volunteering what it is discounting from — the one move that defeated it,
            and the only one an emitter did not have to forge anything to make. **A roster is
            evidence, and OMITTING it is not a way to be judged more gently** (T-306): a product
            the roster cannot price refuses with `price_unreconcilable:offer.unit_price:
            list_price_unavailable`, and so does passing no roster at all, because `None` is the
            empty roster here. Pass a roster that covers everything you are willing to admit. A
            row may also name `max_discount_pct` — the deepest discount you authorize for that
            product — and without one, an offer declaring a non-zero depth is refused rather than
            measured against a depth it chose for itself.
        max_discount_pct: a caller-wide ceiling, in percentage points, for a caller holding one
            approved number rather than a per-product column. A roster row's own
            `max_discount_pct` beats it. It is read whether or not a roster is passed (T-307): a
            ceiling that binds only in the presence of a second argument is a ceiling a caller can
            lose by forgetting, and this one used to be dropped on the floor in silence.

    Returns:
        `BidValidationResult` — `ok`, the `path` it was judged on, `reasons` (non-empty exactly
        when `ok` is false), `requires_verification`, and `unverified_claim_indexes` so the
        verification queue does not have to re-derive which claims to look at.
    """
    evaluated_at = parse_timestamp(now) or datetime.now(UTC)

    if path not in BID_PATHS:
        return BidValidationResult(
            ok=False,
            path=str(path),
            reasons=[f"{REASON_UNKNOWN_PATH}:{path}"],
            requires_verification=False,
            unverified_claim_indexes=[],
        )

    reasons: list[str] = []

    # 1. Schema validity. A bid that is not this object is refused on every path, before any
    #    field of it is interpreted. The model depends on what the caller says it is holding:
    #    `Bid` forbids extra keys, so validating a real D52 submission against it would report
    #    the four envelope fields as schema violations — the mirror image of admitting a bid
    #    that has none of them.
    model = SignedBidSubmission if require_signing_envelope else Bid
    try:
        model.model_validate(_as_plain(bid))
    except ValidationError as exc:
        reasons.append(
            f"{REASON_SCHEMA_INVALID}:"
            + ";".join(
                ".".join(str(part) for part in error["loc"]) or "<root>"
                for error in exc.errors()[:8]
            )
        )
    except Exception:  # noqa: BLE001 - anything unparseable is simply not a Bid
        reasons.append(REASON_SCHEMA_INVALID)

    # 2. Provenance, at EVERY claim-bearing site. Path-sensitive: this is the whole of R8/R18.
    #    `bid.claims` is not the only place a claim can be written down — the Offer is inside the
    #    bid boundary and carries `commitments` and a provenance-stamped `discount` — and a walk
    #    that covers one site is not an exclusivity property, it is a naming convention.
    offer = _get(bid, "offer")
    claim_reasons, unverified = _claim_provenance_reasons(_get(bid, "claims"), path)
    reasons.extend(claim_reasons)
    reasons.extend(_offer_claim_reasons(offer, path))

    # 3. The PRICE the offer states, against the depth it declares. Path-insensitive: arithmetic
    #    does not care who is speaking. A genuine hook-minted 20% grant used to license any price
    #    at all here, because the only wall comparing what a bid charges with what it declares
    #    lived on the emitting side, where a store not running our runtime never meets it. The
    #    list price it reconciles against comes from the bid's own claim and, when the caller
    #    supplies one, from the caller's roster — the version of this wall silence cannot disarm.
    reasons.extend(_price_reasons(bid, offer, list_prices, max_discount_pct))

    # 4. Offer expiry and 5. seller eligibility — path-insensitive.
    reasons.extend(_expiry_reasons(offer, evaluated_at))
    reasons.extend(_eligibility_reasons(_get(bid, "store_id"), trust_snapshot))

    # 6. D52's signing envelope, when the caller is the external door rather than a component
    #    judging an already-extracted `Bid`.
    if require_signing_envelope:
        reasons.extend(_signing_envelope_reasons(bid))

    ok = not reasons
    return BidValidationResult(
        ok=ok,
        path=path,
        reasons=reasons,
        # A rejected bid is not "admitted pending verification": there is nothing to verify.
        requires_verification=bool(ok and unverified),
        unverified_claim_indexes=unverified if ok else [],
    )


def validate_external_submission(
    submission: Any,
    *,
    trust_snapshot: Mapping[str, Any],
    now: datetime | str | float | None = None,
    list_prices: Mapping[Any, Any] | None = None,
    max_discount_pct: float | None = None,
) -> BidValidationResult:
    """The Tier-2 door: everything `validate_bid` judges, plus D52's signing envelope.

    This is the function an exchange should call on a body arriving at
    `POST /v1/auctions/{auction_id}/bids`. It refuses an unsigned submission with the same
    finality as an expired offer or a blacklisted store — before extraction and before
    verification — and, like `validate_bid`, it never raises.

    `list_prices` is forwarded unchanged, and this is the door it matters most on: a Tier-2 store
    is precisely the submitter that does not run our runtime, never meets the emitting wall in
    `store-agent/hooks/provenance.py`, and gets to choose what its bid says about its own
    catalog. Pass the roster here or that choice is the only list price anybody checks — and pass
    a cap with it (`max_discount_pct`, per row or per call), or the store also gets to choose how
    deep the discount it awarded itself is allowed to be.
    """
    return validate_bid(
        submission,
        path=EXTERNAL_PATH,
        trust_snapshot=trust_snapshot,
        now=now,
        require_signing_envelope=True,
        list_prices=list_prices,
        max_discount_pct=max_discount_pct,
    )


__all__ = [
    "BID_PATHS",
    "EXTERNAL_PATH",
    "HOOK_PROVENANCE_SOURCES",
    "HOSTED_PATH",
    "LIST_PRICE_CLAIM_KEY",
    "MAX_DISCOUNT_ROSTER_KEY",
    "MINIMUM_PAYABLE_AMOUNT",
    "NON_HOOK_PROVENANCE_SOURCES",
    "OFFER_COMMITMENTS_SITE",
    "OFFER_DISCOUNT_SITE",
    "OFFER_TOTAL_PRICE_SITE",
    "OFFER_UNIT_PRICE_SITE",
    "PERCENTAGE_DISCOUNT_TYPES",
    "PRICE_FLOOR_FRACTION",
    "PRICE_RECONCILIATION_TOLERANCE",
    "REASON_CLAIM_PROVENANCE_EMPTY_SOURCE",
    "REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE",
    "REASON_CLAIM_WITHOUT_PROVENANCE",
    "REASON_DISCOUNT_OVER_AUTHORIZED_DEPTH",
    "REASON_HOSTED_NON_HOOK_PROVENANCE",
    "REASON_OFFER_EXPIRED",
    "REASON_OFFER_EXPIRY_MISSING",
    "REASON_OFFER_EXPIRY_UNPARSEABLE",
    "REASON_PRICE_UNDER_DECLARED_DEPTH",
    "REASON_PRICE_UNRECONCILABLE",
    "REASON_SCHEMA_INVALID",
    "REASON_SIGNATURE_MISSING",
    "REASON_SIGNING_ENVELOPE_INCOMPLETE",
    "REASON_STORE_BLACKLISTED",
    "REASON_TRUST_SNAPSHOT_UNAVAILABLE",
    "REASON_UNKNOWN_PATH",
    "REASON_UNVERIFIABLE_CLAIM_SITE",
    "ROSTER_LIST_PRICE_CONTRADICTED",
    "ROSTER_LIST_PRICE_UNAVAILABLE",
    "ROSTER_LIST_PRICE_UNREADABLE",
    "ROSTER_MAX_DISCOUNT_UNAVAILABLE",
    "ROSTER_MAX_DISCOUNT_UNREADABLE",
    "ROSTER_PRICE_BELOW_FLOOR",
    "ROSTER_PRICE_NOT_POSITIVE",
    "parse_timestamp",
    "price_floor",
    "price_reasons",
    "validate_bid",
    "validate_external_submission",
]
