"""Store-context assembly for one auction, laid out for prompt caching.

Two jobs, and they are the same job seen from two ends.

**Reading the context.** The advocate is handed a store context (envelope, catalog, pixel feed,
learned policy, network priors) and a `BidRequest`, in whichever shape the caller has them:
plain dicts from a fixture, pydantic models from the wire, or a mixture. Everything downstream
asks this module instead of re-deriving "is it a model or a dict" at each site.

**Ordering it.** DESIGN pins store-agent prompts as *static-context-first for prompt caching*,
which is a statement about ORDER: a cache prefix is only reusable while the bytes in front of
the changing part do not move. :meth:`AuctionContext.cache_layout` publishes that order as
three tiers — what is stable for the store, what is stable for the run, and what is new for
this request — so the prompt-building tickets share one layout instead of each inventing one,
and so the boundary between "cacheable" and "not" is a thing that can be asserted.

The bid path itself calls no LLM and reads no clock: prices come from the catalog and claims
come from the hooks (R8/S4). The layout exists for the pitch/rationale work that sits beside
the bid, not underneath it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from contracts.signing import canonical_json

#: The envelope key carrying the cold-start intro discount, as a percentage depth.
#:
#: DESIGN's cold-start rule is "list price, envelope standing commitments, intro discount rule
#: if the envelope defines one". `contracts.Envelope` pins no such field and forbids extras, so
#: an intro rule can only arrive on the raw context mapping the merchant service hands over —
#: which is where this reads it from. Absent (the usual case, and the fixture case) means no
#: intro rule and therefore no discount. The depth still goes through `authorize_discount`
#: like every other: naming a rule in an envelope is not the same as clearing the walls.
INTRO_DISCOUNT_KEY = "intro_discount_pct"

#: The store-context key stating how long an offer this agent makes stands, as an ISO instant.
#:
#: `contracts.boundary.validate_bid` refuses a bid whose offer states no expiry at all —
#: "an offer with no stated expiry is an offer nobody can price the risk of" — so this is not
#: optional decoration, it is what makes a bid admissible. It arrives on the context because an
#: expiry is a MERCHANT decision and because computing one would mean reading a clock, which
#: ends byte-identical reproduction (S4). When the context states none, the advocate falls back
#: to the request's own `respond_by`; see :meth:`AuctionContext.offer_expires_at`.
OFFER_EXPIRES_AT_KEY = "offer_expires_at"


def as_mapping(value: Any, what: str) -> dict[str, Any]:
    """Read a mapping out of a dict or a pydantic model, so fixtures and models behave alike."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)
    raise TypeError(f"{what} must be a mapping or a pydantic model, got {type(value).__name__}")


def as_sequence(value: Any) -> list[Any]:
    """A list of entries, treating a bare mapping as a one-entry sequence."""
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return [value]
    return list(value)


def as_number(value: Any) -> float | None:
    """A finite number as a float, or `None` when the value is not one.

    `bool` is excluded deliberately: `True` is an `int` in Python, and an availability flag that
    silently became the number 1 would make "in_stock" comparable with a price.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / ±inf
        return None
    return number


@dataclass(frozen=True)
class HardConstraint:
    """One R19 eligibility filter, read off the intent. `field`/`op`/`value`, never weighted."""

    field: str
    op: str
    value: Any


@dataclass(frozen=True)
class AuctionContext:
    """One store's context joined to one `BidRequest`. Immutable, and free of any clock.

    Built by :func:`assemble_context`. Everything on it is derived from the two arguments; there
    is no ambient state, which is what lets two runs on identical inputs produce identical bids
    (S4).
    """

    auction_id: str
    store_id: str
    envelope: Mapping[str, Any]
    catalog: Mapping[str, Mapping[str, Any]]
    live_state: Mapping[str, Mapping[str, Any]]
    learned_policy: Any
    network_priors: Mapping[str, Any]
    intent: Mapping[str, Any]
    profile: Mapping[str, Any]
    respond_by: str
    store_currency: str | None
    stated_offer_expiry: str | None

    @property
    def cluster_id(self) -> str:
        """The intent's cluster, or the empty string when it names none."""
        return str(self.intent.get("cluster_id") or "")

    @property
    def currency(self) -> str | None:
        """The currency the offer is priced in: the store's own, else the one asked for.

        The store's own comes first because a price is the store's assertion. The intent's is
        the fallback rather than the primary, and `None` is a legitimate answer — the protocol
        makes `Offer.currency` optional, and inventing one would be a fact no evidence supports.
        """
        if self.store_currency:
            return self.store_currency
        asked = self.intent.get("currency")
        return str(asked) if asked else None

    @property
    def hard_constraints(self) -> tuple[HardConstraint, ...]:
        """R19's eligibility filters, in the order the intent states them."""
        constraints: list[HardConstraint] = []
        for raw in as_sequence(self.intent.get("hard_constraints")):
            entry = as_mapping(raw, "hard constraint")
            name = str(entry.get("field") or "")
            op = str(getattr(entry.get("op"), "value", entry.get("op")) or "")
            if not name or not op:
                continue
            constraints.append(HardConstraint(field=name, op=op, value=entry.get("value")))
        return tuple(constraints)

    @property
    def pursued_clusters(self) -> tuple[str, ...]:
        return tuple(str(c) for c in as_sequence(self.envelope.get("pursue_clusters")))

    def pursues(self, cluster_id: str) -> bool:
        """Whether the approved envelope told this agent to pursue `cluster_id`.

        Fail-closed on an envelope that lists none, and on an intent that names none. The
        envelope is the merchant's authorization, and "pursue nothing" is a thing a merchant can
        mean; reading an empty list as "pursue everything" would turn the narrowest envelope
        into the widest one.
        """
        return bool(cluster_id) and cluster_id in self.pursued_clusters

    @property
    def intro_discount_pct(self) -> float:
        """The envelope's cold-start intro depth, or 0.0 when it defines no intro rule."""
        depth = as_number(self.envelope.get(INTRO_DISCOUNT_KEY))
        return depth if depth is not None and depth > 0.0 else 0.0

    @property
    def offer_expires_at(self) -> str | None:
        """When an offer made in this auction stops standing. Never computed from a clock.

        The merchant's own :data:`OFFER_EXPIRES_AT_KEY` when the context states one, and
        otherwise the request's `respond_by`: the offer stands at least as long as the auction it
        was solicited for. That fallback is a FLOOR, and a deliberately conservative one — an
        offer that expires exactly when the auction closes is honest about what the agent was
        actually authorized to promise, where an invented "+48h" would be the agent committing
        the merchant to a window nobody approved. A store that wants its offers to outlive the
        auction says so, once, on its context.

        Stated as the ISO instant `contracts.Offer.expires_at` is typed as; `validate_bid` reads
        it with `parse_timestamp` and refuses an offer that states none at all.
        """
        if self.stated_offer_expiry:
            return self.stated_offer_expiry
        return self.respond_by or None

    @property
    def is_cold(self) -> bool:
        """R10: the store has learned nothing yet, so the deterministic default applies."""
        return self.learned_policy is None

    def cache_layout(self) -> tuple[tuple[str, str, str], ...]:
        """``(tier, name, canonical_json)`` blocks, most stable first.

        Three tiers, ordered so a prompt built by concatenating them keeps the longest possible
        unchanged prefix between two calls:

        ``store``
            the merchant's own configuration — identity, approved envelope, catalog. Changes
            when the merchant changes something, which is rarely and deliberately.
        ``session``
            what the platform knows right now — the pixel feed, the learned policy, the network
            priors. Changes between runs, not between the requests inside one.
        ``request``
            this auction: its id, the intent, the buyer's coarse profile. New every time.

        Serialized with RFC 8785 canonical JSON, the same spelling `claim_fingerprint` uses, so
        two logically identical contexts produce byte-identical blocks and a cache hit is a
        property of the content rather than of dict insertion order.
        """
        blocks: list[tuple[str, str, Any]] = [
            ("store", "store_id", self.store_id),
            ("store", "envelope", self.envelope),
            ("store", "catalog", self.catalog),
            ("session", "live_state", self.live_state),
            ("session", "learned_policy", self.learned_policy),
            ("session", "network_priors", self.network_priors),
            ("request", "auction_id", self.auction_id),
            ("request", "intent", self.intent),
            ("request", "profile", self.profile),
        ]
        return tuple((tier, name, canonical_json(value)) for tier, name, value in blocks)


def _catalog(raw: Any) -> dict[str, dict[str, Any]]:
    return {
        str(key): as_mapping(value, "catalog entry")
        for key, value in as_mapping(raw, "catalog").items()
    }


def assemble_context(request: Any, context: Any) -> AuctionContext:
    """Join a `BidRequest` to a store context. Pure, clock-free, and shape-tolerant.

    Both arguments may be plain mappings or pydantic models; nested members may be either.
    Unknown keys are ignored rather than refused — the context is assembled by the merchant
    service and the runtime is not the schema police for it — but every key this module reads is
    read here, once, so there is exactly one place to look for "what does the advocate use".
    """
    ctx = as_mapping(context, "store context")
    req = as_mapping(request, "bid request")
    envelope = as_mapping(ctx.get("envelope"), "envelope")
    currency = ctx.get("currency")
    stated_expiry = ctx.get(OFFER_EXPIRES_AT_KEY) or envelope.get(OFFER_EXPIRES_AT_KEY)
    return AuctionContext(
        auction_id=str(req.get("auction_id") or ""),
        store_id=str(ctx.get("store_id") or envelope.get("store_id") or ""),
        envelope=envelope,
        catalog=_catalog(ctx.get("catalog")),
        live_state={
            str(key): as_mapping(value, "live-state entry")
            for key, value in as_mapping(ctx.get("live_state"), "live_state").items()
        },
        learned_policy=ctx.get("learned_policy"),
        network_priors=as_mapping(ctx.get("network_priors"), "network_priors"),
        intent=as_mapping(req.get("intent"), "intent"),
        profile=as_mapping(req.get("profile"), "buyer profile"),
        respond_by=str(req.get("respond_by") or ""),
        store_currency=str(currency) if currency else None,
        stated_offer_expiry=str(stated_expiry) if stated_expiry else None,
    )


def normalized(value: Any) -> str:
    """A comparable spelling of an attribute value: stripped and case-folded."""
    return str(value).strip().casefold()


def same_value(observed: Any, wanted: Any) -> bool:
    """Equality for catalog attributes: numeric when both are numbers, textual otherwise."""
    left, right = as_number(observed), as_number(wanted)
    if left is not None and right is not None:
        return left == right
    if isinstance(observed, bool) or isinstance(wanted, bool):
        return bool(observed) is bool(wanted)
    return normalized(observed) == normalized(wanted)


def satisfies(op: str, observed: Any, wanted: Any) -> bool:
    """Whether `observed` satisfies the R19 constraint ``op wanted``.

    An operator this function does not implement returns False — the constraint is NOT
    satisfied — rather than True or a raised error. A hard constraint is an eligibility filter,
    and the failure mode of an unknown filter must be "this product does not qualify", never
    "the filter did not apply".
    """
    if op == "eq":
        return same_value(observed, wanted)
    if op in ("lte", "gte"):
        left, right = as_number(observed), as_number(wanted)
        if left is None or right is None:
            return False
        return left <= right if op == "lte" else left >= right
    if op == "in":
        if isinstance(wanted, (str, bytes)) or not isinstance(wanted, Sequence):
            return same_value(observed, wanted)
        return any(same_value(observed, item) for item in wanted)
    if op == "contains":
        if isinstance(observed, str):
            return normalized(wanted) in normalized(observed)
        if isinstance(observed, Sequence):
            return any(same_value(item, wanted) for item in observed)
        return False
    return False


__all__ = [
    "INTRO_DISCOUNT_KEY",
    "OFFER_EXPIRES_AT_KEY",
    "AuctionContext",
    "HardConstraint",
    "as_mapping",
    "as_number",
    "as_sequence",
    "assemble_context",
    "normalized",
    "same_value",
    "satisfies",
]
