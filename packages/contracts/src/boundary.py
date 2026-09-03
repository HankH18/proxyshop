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
  number at all as the price. See `_price_reasons`, including what it deliberately cannot know;
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
#: only carry a list price the catalog actually published. **This is the only list price the
#: boundary can ever see**: it holds no catalog, no envelope and no hook ledger, and inventing a
#: lookup it cannot perform would be worse than saying so.
LIST_PRICE_CLAIM_KEY = "list_price"

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


def _price_reasons(bid: Any, offer: Any) -> list[str]:
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
      ``unit_price >= list_price * (100 - declared_pct) / 100``, written in the same
      ``(100 - pct) / 100`` form the store-agent's grant path uses so the wall that grants and
      the wall that checks cannot round differently and call the difference fraud.
    * `total_price` against the offer's own `unit_price` —
      ``total_price >= unit_price * (100 - declared_pct) / 100``. This one needs no catalog at
      all and is **quantity-safe**: `Offer` carries no quantity, but any quantity is at least
      one, so a total can only ever be LARGER than one discounted unit. The bound therefore
      holds whichever way `total_price` is read, which is why it can be enforced while the
      exact quantity semantics are still undecided. Applied only when a NON-ZERO depth is
      declared: at zero it degenerates into "a total is never under a unit price", which is a
      claim about quantity rather than about a discount and is not this wall's subject.

    Both are ONE-SIDED. A price *above* what the depth prices out at takes less off than was
    declared; there is nothing there for a wall about authorization to refuse, and refusing it
    would turn every rounding-up into an outage.

    **What this cannot do, said plainly.** When the bid carries no `list_price` claim, there is
    no number for the declared depth to be a percentage of and the first relation ABSTAINS — it
    reports nothing rather than guessing at a catalog it cannot read. That abstention is
    deliberate and it is a real gap: an external store that simply omits its list price is
    measured only by the second relation. Closing it needs a list price the exchange supplies
    from its own roster, which is a signature change and a different ticket; refusing every
    discounted offer that omits the claim would instead refuse most honest bids, which is not
    fail-closed, it is closed.
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

    listed, list_reasons = _carried_list_price(bid, record)
    reasons.extend(list_reasons)
    if listed is not None and unit_price + PRICE_RECONCILIATION_TOLERANCE < (
        listed * (100.0 - depth) / 100.0
    ):
        reasons.append(f"{REASON_PRICE_UNDER_DECLARED_DEPTH}:{OFFER_UNIT_PRICE_SITE}")

    # `depth > 0` only. At zero there is no declared depth to make true, and `total >= unit`
    # would no longer be a statement about a discount — it would be an assertion that a total is
    # never under a unit price, which conflates quantity with discount and is not this wall's
    # subject. The list-price relation above still applies at zero, because a price under LIST
    # with no declared discount is a discount that entered the bid through no hook at all.
    if depth > 0.0 and total_price is not None and total_price >= 0.0:
        if total_price + PRICE_RECONCILIATION_TOLERANCE < unit_price * (100.0 - depth) / 100.0:
            reasons.append(f"{REASON_PRICE_UNDER_DECLARED_DEPTH}:{OFFER_TOTAL_PRICE_SITE}")

    return reasons


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
    #    lived on the emitting side, where a store not running our runtime never meets it.
    reasons.extend(_price_reasons(bid, offer))

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
) -> BidValidationResult:
    """The Tier-2 door: everything `validate_bid` judges, plus D52's signing envelope.

    This is the function an exchange should call on a body arriving at
    `POST /v1/auctions/{auction_id}/bids`. It refuses an unsigned submission with the same
    finality as an expired offer or a blacklisted store — before extraction and before
    verification — and, like `validate_bid`, it never raises.
    """
    return validate_bid(
        submission,
        path=EXTERNAL_PATH,
        trust_snapshot=trust_snapshot,
        now=now,
        require_signing_envelope=True,
    )


__all__ = [
    "BID_PATHS",
    "EXTERNAL_PATH",
    "HOOK_PROVENANCE_SOURCES",
    "HOSTED_PATH",
    "LIST_PRICE_CLAIM_KEY",
    "NON_HOOK_PROVENANCE_SOURCES",
    "OFFER_COMMITMENTS_SITE",
    "OFFER_DISCOUNT_SITE",
    "OFFER_TOTAL_PRICE_SITE",
    "OFFER_UNIT_PRICE_SITE",
    "PERCENTAGE_DISCOUNT_TYPES",
    "PRICE_RECONCILIATION_TOLERANCE",
    "REASON_CLAIM_PROVENANCE_EMPTY_SOURCE",
    "REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE",
    "REASON_CLAIM_WITHOUT_PROVENANCE",
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
    "parse_timestamp",
    "validate_bid",
    "validate_external_submission",
]
