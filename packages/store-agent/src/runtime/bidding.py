"""The hosted advocate's bid path: ``BidRequest -> Bid | Decline`` (R8, R10, S4, S5).

What this module is allowed to use, stated as rules rather than as prose, because every one of
them is a criterion something else grades:

**The catalog and the hooks, and nothing else.** Every fact that reaches the bid arrives as a
`Claim` a tool hook emitted — `get_product_fact`, `get_live_state`, `get_owner_commitments`,
`choose_policy_action`, `authorize_discount`. The runtime never builds a `Claim`; `hooks.lint`
refuses one built anywhere on this path, and `enforce_bid_provenance` refuses one at the
boundary. Those two are the same rule enforced statically and dynamically.

**No LLM anywhere near a price.** `unit_price` is the catalog list price, moved only by a depth
`authorize_discount` granted. A model DOES now write the pitch that travels beside the bid —
:mod:`.pitch`, the advocate a shop buys by joining (D55) — and it never writes, and is never
shown, the number in it: the pitch's material is filtered by provenance source, so the
`envelope_rule` grant and the `learned_policy` action, the only two claims carrying a number the
envelope governs, are structurally outside what the writer can see (S4).

**No clock, no RNG and no environment.** `observed_at` comes from the evidence, or from an
explicit `as_of`, or from `UNKNOWN_OBSERVED_AT` — all three inside the hooks. There is no
`datetime.now()`, no `random`, no `uuid`, no `os.environ` and no set iteration on this path,
because "two calls on identical inputs are byte-identical" is a frozen criterion and each of
those ends it quietly. That is also why the pitch's LLM client is *injected* by the composition
root rather than resolved from `LLM_PROVIDER` here: an environment read on the bid path is an
ambient input, whatever it is spelled.

**One `ToolHooks` per auction.** Constructing the facade opens its first bid, so a runtime that
builds one per auction is correct with no ceremony. A facade REUSED across auctions must have
`start_bid()` called between them — the harness cannot detect a new auction on its own — or a
grant obtained in one auction stays admissible in later ones. :func:`bid` therefore builds its
own facade when it is handed none, and when it IS handed one it makes exactly one bid with it
and leaves the lifecycle to the caller that owns it.

**The whole bid goes through the boundary, never `bid.claims`.** See :func:`bid`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from contracts import SCHEMA_VERSION, Bid, Claim, Discount, Offer
from contracts.signing import canonical_json

from ..hooks import (
    CLAIM_SCOPE_SEPARATOR,
    Denied,
    HookInputError,
    HookProvenanceError,
    ToolHooks,
    enforce_bid_provenance,
)
from .context import AuctionContext, as_number, assemble_context, satisfies
from .decline import Decline, DeclineReason
from .pitch import compose_pitch

#: Stamped on every bid this runtime emits, and read downstream as "which advocate built this".
#: Tracks the `proxyshop-store-agent` distribution version.
AGENT_VERSION = "store-agent/0.0.0"

#: The catalog key carrying a product's list price. The one number the cold-start bid is made of.
LIST_PRICE_KEY = "list_price"

#: The pixel-feed key that says a product cannot be sold right now.
IN_STOCK_KEY = "in_stock"

#: The catalog keys that may carry a product's variant id, in priority order.
#:
#: Read **structurally**, off the catalog entry, and not through `get_product_fact` — for the
#: same reason `product_ref` is read off the catalog's own keys rather than asked for as a fact.
#: A variant id identifies the listing a permalink points at; it asserts nothing about the
#: product, so it is not a claim, and minting one for it would put a `Claim` in the hook log and
#: in the bid that no acceptance criterion asked for.
VARIANT_REF_KEYS: tuple[str, ...] = ("variant_ref", "variant_id")

#: `Discount.type` for a percentage depth — the only form a tool hook can authorize.
PERCENTAGE = "percentage"

#: Prefix on a bid-offer id, so a bare string in a log says what it is.
OFFER_ID_PREFIX = "bidoffer"

#: How many hex characters of the digest the id carries. 32 is 128 bits — far past any
#: collision a single exchange will see, and short enough to read in a log line.
OFFER_ID_LENGTH = 32


def offer_id(auction_id: str, store_id: str, product_ref: str) -> str:
    """A deterministic, INJECTIVE id for the offer this bid carries.

    Injective is the whole requirement, and the reason this is a digest rather than the obvious
    `f"{auction}:{store}:{product}"`. Colons are legal in every one of those three fields, so
    the readable form is ambiguous: store ``a`` offering ``b:c`` and store ``a:b`` offering
    ``c`` render the same string, and `app.offers.offer_id` is a PRIMARY KEY — two stores in one
    auction would collide on insert. This package already refuses to build an ambiguous
    identifier elsewhere (`hooks.provenance.scoped_ref` raises rather than mint one), and the
    same argument applies here.

    Hashed over RFC 8785 canonical JSON of the three fields as a LIST, so the separator problem
    cannot come back through the serialization: the encoding is unambiguous by construction.
    No clock and no counter, so the id is a pure function of the bid it names (S4).
    """
    material = canonical_json([str(auction_id), str(store_id), str(product_ref)])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{OFFER_ID_PREFIX}:{digest[:OFFER_ID_LENGTH]}"


@dataclass(frozen=True)
class _Candidate:
    """A catalog product that could carry this bid, with the evidence that qualified it."""

    product_ref: str
    list_price: float
    facts: tuple[Claim, ...]
    live: tuple[Claim, ...]
    #: The catalog's own variant id for this product, when it names one. Carried because a cart
    #: permalink is variant-scoped (D25) and because `contracts.Offer.variant_ref` is what the
    #: checkout path reads before it can mint one. `None` when the catalog names none, which is
    #: the fixture case and stays `None` on the offer.
    variant_ref: str | None = None

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
        if not product_ref:
            # `Offer.product_ref` forbids the empty string, and a provenance `ref` that names no
            # product cites nothing. Rejecting it here answers; building the Offer would raise.
            rejected[product_ref] = (UNPRICED, "the catalog key is empty, so nothing cites it")
            continue
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
                variant_ref=_variant_ref(ctx.catalog.get(product_ref)),
            )
        )
    candidates.sort(key=lambda candidate: candidate.order)
    return candidates, rejected


def _variant_ref(listing: Any) -> str | None:
    """The catalog entry's variant id as a string, or `None` when it names none.

    Defensive about the shape for the same reason the rest of this module is: the catalog is
    whatever the merchant service handed over. A value that is not a scalar — a list of variants,
    a nested mapping — is `None` here rather than a `str(...)` of a container, because a
    permalink built on ``"['a', 'b']"`` points at nothing.
    """
    if not isinstance(listing, Mapping):
        return None
    for key in VARIANT_REF_KEYS:
        value = listing.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if not isinstance(value, (str, int, float)):
            continue
        text = str(value).strip()
        if text:
            return text
    return None


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


def _authorized(
    hooks: Any, store_id: str, product_ref: str, requested: float
) -> tuple[float, Claim | None]:
    """Ask the envelope for `requested`, and return ``(granted depth, the grant)``.

    A :class:`Denied` is a VALUE, looked at rather than swallowed: it is falsy and typed
    precisely so this branch has to exist. A refusal fails closed to list price — a bid without
    the discount is still a bid, and the facade's `call_log` already records the denial and its
    reason, which is the audit trail S5 asks for.

    Zero is never asked. `authorize_discount(ref, 0.0)` would happily mint a grant, because no
    discount is inside every wall; a grant for nothing is still a grant in the ledger, and the
    bid would carry an authorization it does not spend.
    """
    if requested <= 0.0 or not citable_as_a_grant(store_id):
        return 0.0, None
    granted = hooks.authorize_discount(product_ref, requested)
    if isinstance(granted, Denied) or not granted:
        return 0.0, None
    depth = as_number(granted.value)
    if depth is None or depth <= 0.0:
        return 0.0, None
    return depth, granted


def citable_as_a_grant(store_id: str) -> bool:
    """Whether a grant minted for this store could be matched back to its product.

    `hooks.provenance.scoped_ref` builds a grant's ref as ``<rule>@<product>`` and REFUSES —
    loudly, by raising — to build one whose rule half already contains that separator, because
    `claim_is_scoped_to` could never read it back. The rule half embeds `store_id`, so a store
    called ``store@alpha`` can never hold an authorization.

    Asked here, before the hook is called, rather than caught afterwards. Catching the
    `ValueError` would also swallow the unrelated ones `authorize_discount` raises on a
    malformed envelope — a `max_discount_pct` of ``"lots"`` — and those must stay visible as a
    decline that names them, not become a silent absence of discount.

    A store in this state still bids: list price needs no authorization. It simply never gets a
    discount, which is the same fail-closed answer a :class:`Denied` produces.
    """
    return CLAIM_SCOPE_SEPARATOR not in str(store_id)


def _discount(grant: Claim | None, depth: float) -> Discount | None:
    """The offer's discount, citing the grant that authorized it. `None` when there is no grant.

    **The provenance is the grant's own, copied — never one built here.** A `Discount` is not a
    `Claim`: it has no `key`, so it has no ledger identity and cannot be checked against the
    ledger directly. What makes it admissible is the `authorized_discount_pct` grant beside it
    in `bid.claims`, and stamping the grant's provenance onto it is that citation made explicit
    — same `source` (`envelope_rule`), same `ref` (the rule scoped to this product), same
    `observed_at`, same `authority_rank`.

    `contracts.boundary.validate_bid` requires it. It walks `bid.claims`, `offer.commitments`
    AND `offer.discount`, and refuses a discount that is PRESENT WITH NO PROVENANCE AT ALL —
    otherwise an attacker drops the provenance block instead of relabelling it and the discount
    walks through. This runtime is the first producer whose bids reach that door, so it is the
    first code that can satisfy it.

    Copied deep rather than aliased: sharing one `Provenance` instance between the grant and the
    discount means editing either edits the other, and "the discount cites the grant" must not
    become "editing the discount silently rewrites the grant's citation".

    Nothing here is minted. `mint_provenance` would produce an equally valid block, but a
    *separately minted* one is a second assertion about where the authorization came from, and
    two assertions can disagree. The runtime never builds a `Claim`; by the same argument it
    never builds this either — it repeats what the hook already said.
    """
    if grant is None:
        return None
    return Discount(type=PERCENTAGE, value=depth, provenance=grant.provenance.model_copy(deep=True))


def _priced(list_price: float, depth: float) -> float:
    """The price after a granted depth.

    Spelled `list * (100 - pct) / 100` rather than `list * (1 - pct/100)` — the first keeps whole
    percentages exact and the second does not — and identically to `ToolHooks._evaluate_discount`,
    because the boundary checks this number against the same floor that arithmetic cleared. Two
    spellings would be two chances to land a cent apart and have an honest bid refused.
    """
    return list_price * (100.0 - depth) / 100.0


def bid(
    request: Any, context: Any, *, hooks: Any | None = None, llm: Any | None = None
) -> Bid | Decline:
    """Answer one `BidRequest` for one store: a `Bid`, or a :class:`Decline` carrying a reason.

    Args:
        request: the `BidRequest` — a mapping or the pydantic model.
        context: the store context (`store_id`, `envelope`, `catalog`, `live_state`,
            `learned_policy`, `network_priors`), mapping or model.
        hooks: the tool-hook facade. Built from `context` when omitted, which is the correct
            default: one facade per auction needs no `start_bid()` ceremony. Pass one to observe
            what the runtime asked for — `hooks.call_log` and `hooks.emitted_claims` are the S5
            audit trail — and, if you REUSE it, call `hooks.start_bid()` between auctions.
        llm: the copywriter that writes this store's pitch onto `Bid.message` — the advocate a
            shop buys by joining (D55). Anything exposing ``complete(prompt) -> str``.
            **Injected, never resolved here**, because resolving one means reading `LLM_PROVIDER`
            and this module may not touch the environment: the bid path is asserted clock-free,
            RNG-free and environment-free by an AST scan over every file under `runtime/`. The
            composition root owns the choice — see
            :func:`store_agent.solicitation.copywriter.pitch_client` — and with none injected the
            pitch is :func:`~.pitch.fallback_pitch`, which is deterministic and needs no model.

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

    **Answering is not optional.** A malformed store context — a catalog entry that is not a
    mapping, an envelope floor whose `min_price` is a string, a commitment with no `key`, a
    list price that is NaN — used to leave this function as a `TypeError`, a `ValueError` or a
    `pydantic.ValidationError`, thrown into whoever solicited the bid. None of those is a
    better outcome than a decline that names the problem: the merchant service owns the shape
    of the context, and the exchange asked a question that deserves an answer. So the assembly
    runs inside :func:`_answered`, which converts exactly the "this input is not what it claims
    to be" family into :attr:`DeclineReason.unusable_store_context` with the original message
    attached, and lets everything else — including a `HookProvenanceError`, which is handled on
    its own terms below — propagate.
    """
    try:
        return _assemble(request, context, hooks, llm)
    except _UNUSABLE_INPUT as exc:
        return _unanswerable(request, context, exc)


#: The exception family that means "the input is not the shape it claims to be", as opposed to
#: "this runtime has a bug". `pydantic.ValidationError` and `contracts.signing`'s
#: `CanonicalisationError` are both `ValueError` subclasses, so all four arrive through these
#: two names. `HookProvenanceError` is a `RuntimeError` and is deliberately NOT here: a claim
#: the boundary refuses is answered on its own terms, with its own reason.
_UNUSABLE_INPUT = (HookInputError, TypeError, ValueError)


def _unanswerable(request: Any, context: Any, exc: Exception) -> Decline:
    """A decline for a context this runtime could not read, built without trusting that context.

    Deliberately re-reads the two identifiers defensively rather than through
    `assemble_context`: the reason we are here may be that assembling threw, and a fallback
    that can fail is not a fallback.
    """

    def _field(source: Any, name: str) -> str:
        try:
            value = source.get(name) if hasattr(source, "get") else getattr(source, name, "")
        except Exception:  # noqa: BLE001 - the input is already known to be malformed
            return ""
        return str(value) if value else ""

    return Decline(
        auction_id=_field(request, "auction_id"),
        store_id=_field(context, "store_id"),
        reason=DeclineReason.unusable_store_context,
        detail=f"{type(exc).__name__}: {exc}",
        agent_version=AGENT_VERSION,
        schema_version=SCHEMA_VERSION,
    )


def _assemble(
    request: Any, context: Any, hooks: Any | None, llm: Any | None = None
) -> Bid | Decline:
    """The bid path proper. See :func:`bid`, which is this function plus the input guard."""
    ctx = assemble_context(request, context)
    if hooks is None:
        hooks = ToolHooks(context)

    if not ctx.auction_id or not ctx.store_id:
        return _decline(
            ctx,
            DeclineReason.unidentified_request,
            f"a bid must name both the auction and the store it answers for; got "
            f"auction_id={ctx.auction_id!r} store_id={ctx.store_id!r}",
        )

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

    expires_at = ctx.offer_expires_at
    if not expires_at:
        return _decline(
            ctx,
            DeclineReason.unstatable_offer_expiry,
            "neither the store context's `offer_expires_at` nor the request's `respond_by` is a "
            "readable instant, and an offer with no readable expiry is one the exchange refuses",
        )

    chosen = candidates[0]
    commitments = hooks.get_owner_commitments(ctx.cluster_id)
    action = hooks.choose_policy_action(
        {"cluster_id": ctx.cluster_id, "product_ref": chosen.product_ref}
    )
    depth, grant = _authorized(
        hooks, ctx.store_id, chosen.product_ref, _requested_depth(ctx, action)
    )
    unit_price = _priced(chosen.list_price, depth)

    offer = Offer(
        bid_offer_id=offer_id(ctx.auction_id, ctx.store_id, chosen.product_ref),
        product_ref=chosen.product_ref,
        variant_ref=chosen.variant_ref,
        unit_price=unit_price,
        currency=ctx.currency,
        discount=_discount(grant, depth),
        commitments=list(commitments),
        total_price=unit_price,
        # Not optional in practice: `contracts.boundary.validate_bid` refuses an offer that
        # states no expiry, because an offer nobody can price the risk of is not an offer. It
        # comes off the context (or, failing that, off the auction's own `respond_by`) rather
        # than out of a `now() + ttl`, which would be the one clock read on this path.
        expires_at=expires_at,
        # The other field an offer is unusable without, and it was written by nothing in this
        # package until T-309/T-311. `apps/exchange/src/ranking` drops from the shortlist every
        # candidate whose offer states no `checkout_url` — measured with the exchange wired by
        # hand against real eligibility, a real trust snapshot and a real domain registry:
        # `POST /auctions` answered `ranked: []`, every store excluded `off_domain_checkout`,
        # and injecting only this field turned that into 2 ranked and 2 shortlist slots. It is
        # `None` exactly when the merchant's context states no registered domain, which is the
        # legal R10 fallback shape rather than a spoof — `checkout/domain.py` refuses an
        # off-domain URL, never an absent one.
        checkout_url=ctx.checkout_url_for(chosen.product_ref, chosen.variant_ref),
    )
    claims = [*chosen.facts, *chosen.live, action, *([grant] if grant is not None else [])]
    assembled = Bid(
        auction_id=ctx.auction_id,
        store_id=ctx.store_id,
        offer=offer,
        claims=claims,
        # The advocate's own voice (D55). Composed from `claims` and the offer's `commitments` —
        # the material this bid already carries and nothing else — so every sentence in the pitch
        # has a provenance-tagged `Claim` beside it in the same bid for R18 to check against.
        # `compose_pitch` cannot raise and never sees a price; see `runtime.pitch`. `None` is a
        # perfectly good answer and leaves the bid exactly as it was before this feature existed.
        message=compose_pitch(
            ctx, [*claims, *offer.commitments], offer_ref=offer.bid_offer_id, llm=llm
        ),
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
    "VARIANT_REF_KEYS",
    "bid",
    "citable_as_a_grant",
    "offer_id",
]
