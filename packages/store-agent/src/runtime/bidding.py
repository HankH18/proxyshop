"""The hosted advocate's bid path: ``BidRequest -> Bid | Decline`` (R8, R10, S4, S5).

What this module is allowed to use, stated as rules rather than as prose, because every one of
them is a criterion something else grades:

**The catalog and the hooks, and nothing else.** Every fact that reaches the bid arrives as a
`Claim` a tool hook emitted — `get_product_fact`, `get_live_state`, `get_owner_commitments`,
`choose_policy_action`, `authorize_discount`. The runtime never builds a `Claim`; `hooks.lint`
refuses one built anywhere on this path, and `enforce_bid_provenance` refuses one at the
boundary. Those two are the same rule enforced statically and dynamically.

**No LLM anywhere near a price.** `unit_price` is the catalog list price, moved only by a depth
`authorize_discount` granted. A model may one day write the pitch that travels beside the bid;
it will never write the number in it (S4).

**No clock and no RNG.** `observed_at` comes from the evidence, or from an explicit `as_of`, or
from `UNKNOWN_OBSERVED_AT` — all three inside the hooks. There is no `datetime.now()`, no
`random`, no `uuid` and no set iteration on this path, because "two calls on identical inputs
are byte-identical" is a frozen criterion and each of those four ends it quietly.

**One `ToolHooks` per auction.** Constructing the facade opens its first bid, so a runtime that
builds one per auction is correct with no ceremony. A facade REUSED across auctions must have
`start_bid()` called between them — the harness cannot detect a new auction on its own — or a
grant obtained in one auction stays admissible in later ones. :func:`bid` therefore builds its
own facade when it is handed none, and when it IS handed one it makes exactly one bid with it
and leaves the lifecycle to the caller that owns it.

**The whole bid goes through the boundary, never `bid.claims`.** See :func:`bid`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from contracts import SCHEMA_VERSION, Bid, Claim, Discount, Offer

from ..hooks import (
    Denied,
    HookInputError,
    HookProvenanceError,
    ToolHooks,
    enforce_bid_provenance,
)
from .context import AuctionContext, as_number, assemble_context, satisfies
from .decline import Decline, DeclineReason

#: Stamped on every bid this runtime emits, and read downstream as "which advocate built this".
#: Tracks the `proxyshop-store-agent` distribution version.
AGENT_VERSION = "store-agent/0.0.0"

#: The catalog key carrying a product's list price. The one number the cold-start bid is made of.
LIST_PRICE_KEY = "list_price"

#: The pixel-feed key that says a product cannot be sold right now.
IN_STOCK_KEY = "in_stock"

#: `Discount.type` for a percentage depth — the only form a tool hook can authorize.
PERCENTAGE = "percentage"


@dataclass(frozen=True)
class _Candidate:
    """A catalog product that could carry this bid, with the evidence that qualified it."""

    product_ref: str
    list_price: float
    facts: tuple[Claim, ...]
    live: tuple[Claim, ...]

    @property
    def order(self) -> tuple[float, str]:
        """The deterministic selection key: cheapest first, then product_ref.

        The tie-break is not decoration. Two products at one price are ordered by a `dict` whose
        insertion order is whatever the catalog loader happened to produce, and a bid that
        depended on it would be reproducible only by accident.
        """
        return (self.list_price, self.product_ref)


def _out_of_stock(live: Sequence[Claim]) -> bool:
    """Whether the pixel feed says this product cannot be sold.

    Only an explicit `in_stock: false` counts. The pixel is lossy by documented design, so
    silence about a product is not evidence against it — "the feed has said nothing" and "it is
    out of stock" are different facts and only one of them is a reason not to bid.
    """
    for claim in live:
        if str(claim.key) == IN_STOCK_KEY and claim.value is False:
            return True
    return False


#: Why a catalog product cannot carry this bid. Recorded per product as ``(kind, detail)`` and
#: mapped to a `DeclineReason` by :func:`_no_candidate_reason` — a kind rather than a sentence,
#: so the decline reason is decided by a value the rejecting branch chose and never by matching
#: text against the message it happened to write.
UNPRICED = "unpriced"
UNMATCHED = "unmatched"
UNAVAILABLE = "unavailable"


def _gather(ctx: AuctionContext, hooks: Any) -> tuple[list[_Candidate], dict[str, tuple[str, str]]]:
    """Every catalog product that can carry this bid, and why each of the others cannot.

    Catalog order is `sorted()` rather than the mapping's own, so the hook call log — the S5
    audit trail — reads the same on two runs even when the catalog arrived in a different order.

    A hook raising :class:`HookInputError` is an ANSWER here: "the catalog says nothing about
    this key for this product". A product whose material is unknown does not satisfy a hard
    constraint on material, and R19 is explicit that unverified data cannot satisfy one. So the
    exception disqualifies the candidate; it never becomes a default value.
    """
    candidates: list[_Candidate] = []
    rejected: dict[str, tuple[str, str]] = {}
    for product_ref in sorted(ctx.catalog):
        try:
            price_claim = hooks.get_product_fact(product_ref, LIST_PRICE_KEY)
        except HookInputError:
            rejected[product_ref] = (UNPRICED, f"the catalog carries no {LIST_PRICE_KEY}")
            continue
        price = as_number(price_claim.value)
        if price is None or price < 0.0:
            rejected[product_ref] = (
                UNPRICED,
                f"{LIST_PRICE_KEY} {price_claim.value!r} is not a price",
            )
            continue

        facts: list[Claim] = [price_claim]
        failure = ""
        for constraint in ctx.hard_constraints:
            try:
                fact = hooks.get_product_fact(product_ref, constraint.field)
            except HookInputError:
                failure = f"no evidence for hard constraint {constraint.field!r}"
                break
            facts.append(fact)
            if not satisfies(constraint.op, fact.value, constraint.value):
                failure = (
                    f"{constraint.field}={fact.value!r} fails {constraint.op} {constraint.value!r}"
                )
                break
        if failure:
            rejected[product_ref] = (UNMATCHED, failure)
            continue

        live = hooks.get_live_state(product_ref)
        if _out_of_stock(live):
            rejected[product_ref] = (UNAVAILABLE, "the pixel feed reports it out of stock")
            continue
        candidates.append(
            _Candidate(
                product_ref=product_ref,
                list_price=price,
                facts=tuple(facts),
                live=tuple(live),
            )
        )
    candidates.sort(key=lambda candidate: candidate.order)
    return candidates, rejected


def _no_candidate_reason(
    ctx: AuctionContext, rejected: dict[str, tuple[str, str]]
) -> DeclineReason:
    """Which condition to report when nothing can carry the bid.

    Ordered by how far each product got: one that satisfied the intent and then failed on stock
    says more than "nothing matched", and both say more than "nothing was priced".
    """
    if not ctx.catalog:
        return DeclineReason.no_priced_product
    kinds = {kind for kind, _detail in rejected.values()}
    if UNAVAILABLE in kinds:
        return DeclineReason.no_available_product
    if UNMATCHED in kinds:
        return DeclineReason.no_matching_product
    return DeclineReason.no_priced_product


def _detail(rejected: dict[str, tuple[str, str]]) -> str:
    return "; ".join(f"{ref}: {detail}" for ref, (_kind, detail) in sorted(rejected.items()))


def _requested_depth(ctx: AuctionContext, action: Claim) -> float:
    """The discount depth to ASK the envelope for. Asking is not being granted.

    Cold (R10): the envelope's intro rule if it defines one, and otherwise nothing — that is the
    whole of "list price + standing commitments + intro rule".

    Warm: the depth the store's own learned policy chose, read off hook 5's `policy_action`
    claim rather than out of the policy mapping. The hook has already re-asked the envelope
    about that depth and zeroed it if the walls refuse, so a policy that has learned to want 25%
    against a 20% cap arrives here as 0.0 and the bid goes out at list price.
    """
    if ctx.is_cold:
        return ctx.intro_discount_pct
    value = action.value
    depth = as_number(value.get("discount_pct")) if isinstance(value, dict) else None
    return depth if depth is not None and depth > 0.0 else 0.0


def _authorized(hooks: Any, product_ref: str, requested: float) -> tuple[float, Claim | None]:
    """Ask the envelope for `requested`, and return ``(granted depth, the grant)``.

    A :class:`Denied` is a VALUE, looked at rather than swallowed: it is falsy and typed
    precisely so this branch has to exist. A refusal fails closed to list price — a bid without
    the discount is still a bid, and the facade's `call_log` already records the denial and its
    reason, which is the audit trail S5 asks for.

    Zero is never asked. `authorize_discount(ref, 0.0)` would happily mint a grant, because no
    discount is inside every wall; a grant for nothing is still a grant in the ledger, and the
    bid would carry an authorization it does not spend.
    """
    if requested <= 0.0:
        return 0.0, None
    granted = hooks.authorize_discount(product_ref, requested)
    if isinstance(granted, Denied) or not granted:
        return 0.0, None
    depth = as_number(granted.value)
    if depth is None or depth <= 0.0:
        return 0.0, None
    return depth, granted


def _priced(list_price: float, depth: float) -> float:
    """The price after a granted depth.

    Spelled `list * (100 - pct) / 100` rather than `list * (1 - pct/100)` — the first keeps whole
    percentages exact and the second does not — and identically to `ToolHooks._evaluate_discount`,
    because the boundary checks this number against the same floor that arithmetic cleared. Two
    spellings would be two chances to land a cent apart and have an honest bid refused.
    """
    return list_price * (100.0 - depth) / 100.0


def bid(request: Any, context: Any, *, hooks: Any | None = None) -> Bid | Decline:
    """Answer one `BidRequest` for one store: a `Bid`, or a :class:`Decline` carrying a reason.

    Args:
        request: the `BidRequest` — a mapping or the pydantic model.
        context: the store context (`store_id`, `envelope`, `catalog`, `live_state`,
            `learned_policy`, `network_priors`), mapping or model.
        hooks: the tool-hook facade. Built from `context` when omitted, which is the correct
            default: one facade per auction needs no `start_bid()` ceremony. Pass one to observe
            what the runtime asked for — `hooks.call_log` and `hooks.emitted_claims` are the S5
            audit trail — and, if you REUSE it, call `hooks.start_bid()` between auctions.

    The last step is the one this function exists to get right::

        enforce_bid_provenance(assembled, hooks)   # the WHOLE Bid, never `assembled.claims`

    `enforce_hook_provenance` checks exactly the material it is handed. Handing it `bid.claims`
    leaves the offer outside the boundary — its own `commitments`, its `discount` and the price
    it states are all fields of the `Offer`, and `collect_claim_material` reaches them only from
    the `Bid` root. That is not hypothetical: an unauthorised 25% once reached a bid whose every
    top-level claim was genuine, because the guard was shown the claim list. Passing the whole
    bid is what closes it, and `enforce_bid_provenance` additionally reads `product_ref` off
    `bid.offer` so the scope argument cannot be forgotten.

    A refusal here means the runtime assembled something it cannot prove — a defect in THIS
    module, not a business condition — so it becomes a decline carrying the boundary's own
    message rather than a bid. A hosted agent that cannot trace its own claims must not emit
    them.
    """
    ctx = assemble_context(request, context)
    if hooks is None:
        hooks = ToolHooks(context)

    if not ctx.pursues(ctx.cluster_id):
        return _decline(
            ctx,
            DeclineReason.cluster_not_pursued,
            f"cluster {ctx.cluster_id!r} is not in the envelope's pursue_clusters "
            f"{list(ctx.pursued_clusters)}",
        )

    candidates, rejected = _gather(ctx, hooks)
    if not candidates:
        return _decline(ctx, _no_candidate_reason(ctx, rejected), _detail(rejected))

    chosen = candidates[0]
    commitments = hooks.get_owner_commitments(ctx.cluster_id)
    action = hooks.choose_policy_action(
        {"cluster_id": ctx.cluster_id, "product_ref": chosen.product_ref}
    )
    depth, grant = _authorized(hooks, chosen.product_ref, _requested_depth(ctx, action))
    unit_price = _priced(chosen.list_price, depth)

    offer = Offer(
        bid_offer_id=f"bidoffer:{ctx.auction_id}:{ctx.store_id}:{chosen.product_ref}",
        product_ref=chosen.product_ref,
        unit_price=unit_price,
        currency=ctx.currency,
        # No `provenance` on the discount, deliberately. A `Discount` is not a `Claim` — it has
        # no key, so it has no ledger identity — and the frozen S5 criterion reads every node
        # carrying a `provenance` as a claim that must trace to a hook emission. A discount
        # wearing one would be a claim no hook emitted. What authorizes it is the grant beside
        # it in `claims`, which is exactly what the boundary matches it against.
        discount=Discount(type=PERCENTAGE, value=depth) if grant is not None else None,
        commitments=list(commitments),
        total_price=unit_price,
        # Not optional in practice: `contracts.boundary.validate_bid` refuses an offer that
        # states no expiry, because an offer nobody can price the risk of is not an offer. It
        # comes off the context (or, failing that, off the auction's own `respond_by`) rather
        # than out of a `now() + ttl`, which would be the one clock read on this path.
        expires_at=ctx.offer_expires_at,
    )
    assembled = Bid(
        auction_id=ctx.auction_id,
        store_id=ctx.store_id,
        offer=offer,
        claims=[*chosen.facts, *chosen.live, action, *([grant] if grant is not None else [])],
        agent_version=AGENT_VERSION,
        schema_version=SCHEMA_VERSION,
    )

    try:
        enforce_bid_provenance(assembled, hooks)
    except HookProvenanceError as refusal:
        return _decline(ctx, DeclineReason.provenance_refused, str(refusal))
    return assembled


def _decline(ctx: AuctionContext, reason: DeclineReason, detail: str) -> Decline:
    return Decline(
        auction_id=ctx.auction_id,
        store_id=ctx.store_id,
        reason=reason,
        detail=detail,
        agent_version=AGENT_VERSION,
        schema_version=SCHEMA_VERSION,
    )


__all__ = [
    "AGENT_VERSION",
    "IN_STOCK_KEY",
    "LIST_PRICE_KEY",
    "PERCENTAGE",
    "bid",
]
