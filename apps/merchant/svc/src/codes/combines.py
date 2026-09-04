"""Reading the shop's discount-combination policy, and deciding *before* anything is minted.

R3 asks for a conflicting ``combinesWith`` configuration to be caught before a permalink is
handed out, and the ordering is the whole requirement. A conflict found afterwards is not a
detection, it is an apology: the buyer has already been sent to a cart, a single-use code
already exists in the merchant's Shopify account, and the price they were promised is not
the price they will be charged. So this module runs first, on its own Admin API call, and
:func:`combines_with_verdict` answers one of three things — never two.

**Three answers, not two.** ``CLEAR`` and ``CONFLICT`` are the easy ones. ``UNKNOWN`` is the
one that earns its place: an Admin surface that does not answer this query (the offline
``services/shopify-stub`` is one — it implements four root fields and refuses the rest with
``undefinedField``) tells us nothing about the shop's automatic discounts, and "nothing" is
not "no conflict". The caller refuses on ``UNKNOWN`` too, but with a *different*, retryable
refusal, so an operator can tell "this shop's discounts genuinely clash" apart from "we
could not ask".

**Truthiness is not usable here and that is measured, not defensive.** The frozen
acceptance suite drives this code with an auto-vivifying response double: every key lookup
that misses returns a fresh non-empty mapping, so ``if response["hasActiveAutomaticDiscount"]``
is ``True`` for a shop that has none. Real GraphQL clients have the milder version of the
same problem — an absent field and a ``false`` field are both falsy. Every read below is
therefore *strict*: a boolean must be a ``bool``, a list of discount nodes must be a
``list``, and anything else reads as "not stated" rather than as a value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "AUTOMATIC_DISCOUNT_LIST_KEYS",
    "SHOP_DISCOUNT_CONFIGURATION",
    "CombinesWithPolicy",
    "CombinesWithVerdict",
    "DiscountClass",
    "ShopDiscountConfiguration",
    "combines_with_verdict",
    "read_shop_discount_configuration",
]

#: The Admin GraphQL document this module sends. Written the way a real caller writes one —
#: a named operation with typed variables and an explicit selection set — because the shape
#: a stub is *sent* is the only part of an offline run that carries over to production.
SHOP_DISCOUNT_CONFIGURATION = """
query ShopDiscountConfiguration($first: Int!) {
  automaticDiscountNodes(first: $first, query: "status:active") {
    nodes {
      id
      automaticDiscount {
        __typename
        ... on DiscountAutomaticBasic {
          title
          status
          combinesWith { orderDiscounts productDiscounts shippingDiscounts }
        }
        ... on DiscountAutomaticBxgy {
          title
          status
          combinesWith { orderDiscounts productDiscounts shippingDiscounts }
        }
      }
    }
  }
}
"""

#: How many automatic discounts to ask for. A shop with more than this has bigger problems
#: than one offer's code, and the page is a cap on the work, not a claim of completeness.
AUTOMATIC_DISCOUNT_PAGE = 50

#: Keys under which a list of automatic-discount nodes legitimately arrives. Shopify nests
#: the same list differently per discount type and per API version, so it is found by name
#: rather than by a path that exactly one response shape satisfies.
AUTOMATIC_DISCOUNT_LIST_KEYS: frozenset[str] = frozenset(
    {"automaticDiscounts", "automatic_discounts", "automaticDiscountNodes", "discountNodes"}
)

#: Keys under which the shop's own "is anything automatic running" flag arrives.
_HAS_AUTOMATIC_KEYS: frozenset[str] = frozenset(
    {"hasActiveAutomaticDiscount", "has_active_automatic_discount"}
)

_COMBINES_WITH_KEYS: frozenset[str] = frozenset({"combinesWith", "combines_with"})

#: How deep, and how wide, the response walk goes. A response is data from outside this
#: process; an unbounded walk over it is a denial of service with extra steps.
_MAX_DEPTH = 10
_MAX_NODES = 2048


class ShopConfigurationUnavailable(RuntimeError):
    """The shop's discount configuration could not be read, so no conflict claim is possible.

    Deliberately NOT a subclass of the conflict error. "These discounts clash" and "we could
    not ask whether they clash" are different operational facts with different fixes, and a
    single exception type would erase the difference at exactly the moment somebody is
    trying to work out why an offer stopped converting.
    """


class DiscountClass(StrEnum):
    """Which of Shopify's combination flags governs the code we are about to mint.

    A basic code discount that names specific variants is a **product** discount; one that
    applies to the whole order is an **order** discount. They are governed by different
    ``combinesWith`` booleans, so the class is decided from the offer rather than assumed.
    """

    PRODUCT = "product"
    ORDER = "order"


class CombinesWithVerdict(StrEnum):
    """The three answers, kept distinct all the way to the caller."""

    CLEAR = "clear"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CombinesWithPolicy:
    """Shopify's three ``DiscountCombinesWith`` booleans, with "not stated" preserved.

    ``None`` is a third value on purpose: an absent flag is not a ``False`` flag, and
    collapsing the two is how a shop that never answered the question ends up recorded as
    one that refused to combine.
    """

    order_discounts: bool | None = None
    product_discounts: bool | None = None
    shipping_discounts: bool | None = None

    def for_class(self, discount_class: DiscountClass) -> bool | None:
        """The flag that governs ``discount_class``, or ``None`` when it was not stated."""
        if discount_class is DiscountClass.PRODUCT:
            return self.product_discounts
        return self.order_discounts

    @classmethod
    def from_wire(cls, payload: Any) -> CombinesWithPolicy | None:
        """Parse Shopify's camelCase object, keeping a non-boolean as "not stated"."""
        if not isinstance(payload, Mapping):
            return None
        return cls(
            order_discounts=_strict_bool(payload.get("orderDiscounts")),
            product_discounts=_strict_bool(payload.get("productDiscounts")),
            shipping_discounts=_strict_bool(payload.get("shippingDiscounts")),
        )


@dataclass(frozen=True)
class ShopDiscountConfiguration:
    """What one Admin API answer told us about a shop's existing discounts.

    Attributes:
        combines_with: the combination policy the shop's discounts declare, or ``None``
            when the answer stated none.
        automatic_discounts: an identifier per active automatic discount found.
        automatic_discounts_known: whether the answer actually carried the list. An empty
            tuple with this ``False`` means "we did not see one", not "there are none".
        has_active_automatic_discount: the shop's own summary flag, when it stated one.
        source: where this came from, for the refusal message and the ledger.
    """

    combines_with: CombinesWithPolicy | None = None
    automatic_discounts: tuple[str, ...] = ()
    automatic_discounts_known: bool = False
    has_active_automatic_discount: bool | None = None
    source: str = "admin-api"

    @property
    def has_automatic_discount(self) -> bool | None:
        """``True``/``False`` when the answer settles it, ``None`` when it does not."""
        if self.automatic_discounts:
            return True
        if self.has_active_automatic_discount is not None:
            return self.has_active_automatic_discount
        if self.automatic_discounts_known:
            return False
        return None

    def describe(self) -> dict[str, Any]:
        """A JSON-able summary, for a refusal message and for the ledger."""
        return {
            "source": self.source,
            "combines_with": None
            if self.combines_with is None
            else {
                "orderDiscounts": self.combines_with.order_discounts,
                "productDiscounts": self.combines_with.product_discounts,
                "shippingDiscounts": self.combines_with.shipping_discounts,
            },
            "active_automatic_discounts": list(self.automatic_discounts),
            "has_active_automatic_discount": self.has_active_automatic_discount,
        }


def _strict_bool(value: Any) -> bool | None:
    """``value`` when it really is a boolean, else ``None``. Never a truthiness test."""
    return value if isinstance(value, bool) else None


def _strict_list(value: Any) -> list[Any] | None:
    """``value`` when it really is a sequence of nodes, else ``None``.

    A ``Mapping`` is excluded explicitly: the acceptance suite's auto-vivifying double
    answers every missing key with a mapping, and a mapping is not a list of discounts
    however much it is willing to be indexed.
    """
    if isinstance(value, Mapping) or isinstance(value, (str, bytes)):
        return None
    if isinstance(value, Sequence):
        return list(value)
    return None


def _walk(response: Any) -> list[tuple[str, Any]]:
    """Every ``(key, value)`` pair reachable in a response, breadth-first and bounded.

    Only *real* items are visited — ``Mapping.items()`` never triggers the acceptance
    double's auto-vivification — so this terminates on the doubles and on real responses
    alike, and the bounds cap what an unfriendly response can cost.
    """
    pairs: list[tuple[str, Any]] = []
    frontier: list[tuple[Any, int]] = [(response, 0)]
    seen = 0
    while frontier and seen < _MAX_NODES:
        node, depth = frontier.pop(0)
        seen += 1
        if depth >= _MAX_DEPTH:
            continue
        if isinstance(node, Mapping):
            for key, value in node.items():
                pairs.append((str(key), value))
                frontier.append((value, depth + 1))
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            for value in node:
                frontier.append((value, depth + 1))
    return pairs


def _node_identifier(node: Any) -> str:
    """A stable, non-secret name for one automatic discount, for the refusal and the ledger."""
    if isinstance(node, Mapping):
        for key in ("id", "title", "handle", "__typename"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        inner = node.get("automaticDiscount")
        if isinstance(inner, Mapping):
            return _node_identifier(inner)
    return "automatic-discount"


def parse_shop_discount_configuration(
    response: Any, *, source: str = "admin-api"
) -> ShopDiscountConfiguration:
    """Read a shop's combination policy and active automatic discounts out of one answer."""
    pairs = _walk(response)

    policy: CombinesWithPolicy | None = None
    for key, value in pairs:
        if key in _COMBINES_WITH_KEYS:
            parsed = CombinesWithPolicy.from_wire(value)
            if parsed is not None:
                policy = parsed
                break

    has_flag: bool | None = None
    for key, value in pairs:
        if key in _HAS_AUTOMATIC_KEYS:
            has_flag = _strict_bool(value)
            if has_flag is not None:
                break

    discounts: list[str] = []
    known = False
    for key, value in pairs:
        if key not in AUTOMATIC_DISCOUNT_LIST_KEYS:
            continue
        nodes = _strict_list(value)
        if nodes is None:
            # `automaticDiscountNodes` is a connection object; its `nodes` list is a
            # separate pair this same walk will visit, so nothing is lost by skipping it.
            continue
        known = True
        discounts.extend(_node_identifier(node) for node in nodes)
        break

    return ShopDiscountConfiguration(
        combines_with=policy,
        automatic_discounts=tuple(discounts),
        automatic_discounts_known=known,
        has_active_automatic_discount=has_flag,
        source=source,
    )


def _data_block(response: Any) -> Any:
    """The GraphQL ``data`` block, whichever layer the client hands back.

    ``merchant_svc.install.admin.AdminGraphQLClient.execute`` already unwraps ``data``;
    other clients (and the acceptance suite's double) return the whole envelope. Membership
    is tested with ``in`` rather than ``.get`` on purpose — the acceptance double's ``get``
    invents a value for any key, so asking it politely would always succeed.
    """
    if isinstance(response, Mapping) and "data" in response:
        inner = response["data"]
        if isinstance(inner, Mapping):
            return inner
    return response


def read_shop_discount_configuration(
    client: Any, *, first: int = AUTOMATIC_DISCOUNT_PAGE
) -> ShopDiscountConfiguration:
    """Ask the Admin API what discounts are already running on this shop.

    Raises:
        ShopConfigurationUnavailable: the client refused, raised, or answered something
            that states neither a combination policy nor a list of automatic discounts.
            Refusing here rather than defaulting to "no conflict" is the fail-closed half
            of R3: an unread configuration must never be reported as a clear one.
    """
    execute = getattr(client, "execute", None)
    if not callable(execute):
        raise ShopConfigurationUnavailable(
            f"{type(client).__name__} exposes no `execute`, so the shop's discount "
            f"configuration cannot be read before minting"
        )
    try:
        response = execute(SHOP_DISCOUNT_CONFIGURATION, {"first": int(first)})
    except Exception as exc:  # noqa: BLE001 - every client failure is the same refusal
        raise ShopConfigurationUnavailable(
            f"the Admin API did not answer the shop discount configuration query: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    config = parse_shop_discount_configuration(_data_block(response))
    if config.combines_with is None and not config.automatic_discounts_known:
        raise ShopConfigurationUnavailable(
            "the Admin API answer carried neither a combinesWith policy nor a list of "
            "automatic discounts, so whether this offer's code would combine is unknown"
        )
    return config


def combines_with_verdict(
    config: ShopDiscountConfiguration, discount_class: DiscountClass
) -> CombinesWithVerdict:
    """Whether a code of ``discount_class`` may be minted against this configuration.

    The rule, in the order it is applied:

    1. No stated policy at all -> ``UNKNOWN``.
    2. The governing flag is ``True`` -> ``CLEAR``. The shop's discounts combine with ours;
       whether any are running does not matter.
    3. The governing flag is not stated -> ``UNKNOWN``.
    4. The governing flag is ``False`` and at least one automatic discount is running ->
       ``CONFLICT``. Whichever of the two applies, the buyer will not pay the price the
       offer promised.
    5. The governing flag is ``False`` and we positively know none is running -> ``CLEAR``.
       A refusal to combine with something that does not exist refuses nothing.
    6. Otherwise -> ``UNKNOWN``.
    """
    policy = config.combines_with
    if policy is None:
        return CombinesWithVerdict.UNKNOWN
    flag = policy.for_class(discount_class)
    if flag is True:
        return CombinesWithVerdict.CLEAR
    if flag is None:
        return CombinesWithVerdict.UNKNOWN
    running = config.has_automatic_discount
    if running is True:
        return CombinesWithVerdict.CONFLICT
    if running is False:
        return CombinesWithVerdict.CLEAR
    return CombinesWithVerdict.UNKNOWN
