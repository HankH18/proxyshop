"""The GraphQL Admin API surface — exactly four root fields, and no fifth.

SPEC C5 says *"GraphQL Admin only"*, and the whole repo names exactly two operations by
name: ``discountCodeBasicCreate`` and ``webPixelCreate``. Webhook subscription is named only
by its topics, so the stub implements the real mutation that registers them,
``webhookSubscriptionCreate``. The ``orders`` query is named as a "subset" with no fields
specified anywhere, so the subset is defined here.

============================== ============ =======================================
Root field                     Kind         Why the stub has it
============================== ============ =======================================
``discountCodeBasicCreate``    mutation     T-052 mints every offer's code with it.
``orders``                     query        The read side of the ``read_orders`` scope.
``webPixelCreate``             mutation     T-050's install step.
``webhookSubscriptionCreate``  mutation     Registers the three delivery topics.
============================== ============ =======================================

Anything else answers with Shopify's ``undefinedField`` error rather than a plausible
success. That refusal is load-bearing: a stub that silently returns ``{"data": {}}`` for an
unimplemented operation lets a consumer ship a call that no one has ever exercised.

**Declared limitation.** The stub does not honour the selection set — it returns the whole
documented node shape whatever fields were asked for. The response is therefore a *superset*
of a narrowed real response, never a different shape, and the contract tests assert exactly
that relation. Implementing selection filtering would need a schema, and there is no GraphQL
engine in this repo's dependency manifest.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from shopify_stub.codes import CombinesWith, DiscountCode
from shopify_stub.graphql_lite import GraphQLSyntaxError, parse_operation
from shopify_stub.orders import iso, orders_connection
from shopify_stub.state import (
    StubState,
    WebhookSubscription,
    WebhookTopic,
    WebPixel,
)

#: The root fields the stub answers. Everything else is ``undefinedField``.
SUPPORTED_QUERIES = frozenset({"orders"})
SUPPORTED_MUTATIONS = frozenset(
    {"discountCodeBasicCreate", "webPixelCreate", "webhookSubscriptionCreate"}
)

#: Topic enum spellings Shopify's ``WebhookSubscriptionTopic`` uses, mapped to the
#: slash-separated names the delivery headers and this system's config use.
TOPIC_ENUM_TO_NAME = {
    "ORDERS_PAID": WebhookTopic.ORDERS_PAID,
    "ORDERS_FULFILLED": WebhookTopic.ORDERS_FULFILLED,
    "REFUNDS_CREATE": WebhookTopic.REFUNDS_CREATE,
}


class GraphQLHTTPError(Exception):
    """A failure that Shopify answers at the HTTP layer, not inside ``errors``.

    The one that matters here is authentication: a bad or missing access token gets a
    **401** whose body is ``{"errors": "<string>"}`` — a bare string, not the list of error
    objects a GraphQL-level failure produces. Consumers that parse ``errors`` as a list
    crash on this exact response in production, so the stub reproduces it faithfully.
    """

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.body = body


def throttle_extensions(requested: int = 11, actual: int = 11) -> dict[str, Any]:
    """The ``extensions.cost`` block Shopify attaches to every Admin GraphQL response.

    Values are plausible rather than computed — the stub does not model query cost. It is
    present because a client that reads ``throttleStatus`` to pace itself must find the
    field, and a client that finds it missing usually crashes rather than degrading.
    """
    return {
        "cost": {
            "requestedQueryCost": requested,
            "actualQueryCost": actual,
            "throttleStatus": {
                "maximumAvailable": 2000.0,
                "currentlyAvailable": 2000.0 - actual,
                "restoreRate": 100.0,
            },
        }
    }


def undefined_field_error(field_name: str, parent_type: str) -> dict[str, Any]:
    """Shopify's response to a root field that does not exist on the schema."""
    return {
        "errors": [
            {
                "message": f"Field '{field_name}' doesn't exist on type '{parent_type}'",
                "locations": [{"line": 1, "column": 1}],
                "path": ["query", field_name],
                "extensions": {
                    "code": "undefinedField",
                    "typeName": parent_type,
                    "fieldName": field_name,
                },
            }
        ]
    }


def parse_error(message: str) -> dict[str, Any]:
    """Shopify's response to a document it cannot parse."""
    return {"errors": [{"message": message, "locations": [{"line": 1, "column": 1}]}]}


def user_error(
    message: str, field_path: list[str], code: str, extra_info: str | None = None
) -> dict[str, Any]:
    """One ``DiscountUserError``.

    Four members, in the order the schema lists them: ``field``, ``message``, ``code`` and
    ``extraInfo``. ``extraInfo`` is easy to omit and is a real member; a consumer that
    destructures the error object positionally or validates it against a schema notices.
    """
    return {
        "field": field_path,
        "message": message,
        "code": code,
        "extraInfo": extra_info,
    }


def execute(
    state: StubState,
    *,
    document: str,
    variables: dict[str, Any] | None,
    access_token: str | None,
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    """Run one Admin GraphQL request.

    Args:
        state: the stub's world.
        document: the ``query`` member of the request body.
        variables: the ``variables`` member.
        access_token: the value of the ``X-Shopify-Access-Token`` header, if any.
        now: the request instant.

    Returns:
        ``(http_status, body)``.
    """
    if access_token != state.config.access_token:
        return 401, {
            "errors": (
                "[API] Invalid API key or access token (unrecognized login or wrong password)"
            )
        }
    try:
        operation = parse_operation(document, variables)
    except GraphQLSyntaxError as exc:
        return 200, parse_error(f"Parse error on {exc}")

    name = operation.field_name
    if operation.operation == "mutation":
        if name not in SUPPORTED_MUTATIONS:
            return 200, undefined_field_error(name, "Mutation")
    elif name not in SUPPORTED_QUERIES:
        return 200, undefined_field_error(name, "QueryRoot")

    arguments = _merge_arguments(operation.arguments, variables or {})
    resolver = {
        "discountCodeBasicCreate": _resolve_discount_code_basic_create,
        "orders": _resolve_orders,
        "webPixelCreate": _resolve_web_pixel_create,
        "webhookSubscriptionCreate": _resolve_webhook_subscription_create,
    }[name]
    try:
        result = resolver(state, arguments, now)
    except GraphQLHTTPError as exc:
        return exc.status_code, exc.body
    except _FieldError as exc:
        return 200, {"errors": [{"message": str(exc)}]}
    return 200, {
        "data": {operation.response_key: result},
        "extensions": throttle_extensions(),
    }


class _FieldError(Exception):
    """A request-level GraphQL error (bad argument), as opposed to a ``userErrors`` entry."""


def _merge_arguments(inline: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
    """Inline arguments win; anything still missing falls back to a same-named variable.

    Callers written against Shopify overwhelmingly pass one variable per argument with the
    same name (``orders(first: $first)``), and the inline parser has already substituted
    those. This fallback additionally supports the shorthand where a client sends only
    ``variables`` and a document whose root field takes no explicit arguments.
    """
    merged = dict(variables)
    merged.update({key: value for key, value in inline.items() if value is not None})
    return merged


# ---------------------------------------------------------------------------------------
# discountCodeBasicCreate
# ---------------------------------------------------------------------------------------


def _resolve_discount_code_basic_create(
    state: StubState, arguments: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Create a basic code discount.

    The single argument is ``basicCodeDiscount: DiscountCodeBasicInput!``. Its
    ``customerGets.value.percentage`` is a **fraction in [0, 1]**, not a 0-100 number — the
    schema says "Value must be between 0.00 - 1.00", and a caller that sends ``10`` for 10%
    would otherwise silently create a 1000% discount. The stub rejects out-of-range values
    with a ``userErrors`` entry rather than clamping them.
    """
    payload = arguments.get("basicCodeDiscount")
    if not isinstance(payload, dict):
        raise _FieldError(
            "Variable $basicCodeDiscount of type DiscountCodeBasicInput! was provided invalid value"
        )
    code = payload.get("code")
    if not isinstance(code, str) or not code:
        return {
            "codeDiscountNode": None,
            "userErrors": [
                user_error(
                    "Code can't be blank",
                    ["basicCodeDiscount", "code"],
                    "BLANK",
                )
            ],
        }
    if state.find_code(code) is not None:
        return {
            "codeDiscountNode": None,
            "userErrors": [
                user_error(
                    "Discount code must be unique",
                    ["basicCodeDiscount", "code"],
                    "TAKEN",
                )
            ],
        }

    starts_at = _parse_datetime(payload.get("startsAt")) or now
    ends_at = _parse_datetime(payload.get("endsAt"))
    if ends_at is not None and ends_at <= starts_at:
        return {
            "codeDiscountNode": None,
            "userErrors": [
                user_error(
                    "End date must be after start date",
                    ["basicCodeDiscount", "endsAt"],
                    "INVALID",
                )
            ],
        }

    usage_limit = payload.get("usageLimit")
    if usage_limit is not None and (not isinstance(usage_limit, int) or usage_limit < 1):
        return {
            "codeDiscountNode": None,
            "userErrors": [
                user_error(
                    "Usage limit must be greater than 0",
                    ["basicCodeDiscount", "usageLimit"],
                    "GREATER_THAN",
                )
            ],
        }

    customer_gets = payload.get("customerGets") or {}
    value = customer_gets.get("value") or {}
    percentage = value.get("percentage")
    discount_amount = (value.get("discountAmount") or {}).get("amount")
    if percentage is None and discount_amount is None:
        return {
            "codeDiscountNode": None,
            "userErrors": [
                user_error(
                    "Customer gets value must be provided",
                    ["basicCodeDiscount", "customerGets", "value"],
                    "INVALID",
                )
            ],
        }
    if percentage is not None and not 0.0 <= float(percentage) <= 1.0:
        return {
            "codeDiscountNode": None,
            "userErrors": [
                user_error(
                    "Percentage value must be between 0.0 and 1.0",
                    ["basicCodeDiscount", "customerGets", "value", "percentage"],
                    "INVALID",
                    extra_info=f"received {percentage}",
                )
            ],
        }

    node_id = state.next_discount_id()
    discount = DiscountCode(
        code=code,
        offer_id=_offer_id_from(payload),
        title=str(payload.get("title") or code),
        starts_at=starts_at,
        ends_at=ends_at,
        usage_limit=usage_limit,
        applies_once_per_customer=bool(payload.get("appliesOncePerCustomer", False)),
        combines_with=CombinesWith.from_wire(payload.get("combinesWith")),
        percentage=float(percentage) if percentage is not None else None,
        fixed_amount=str(discount_amount) if discount_amount is not None else None,
        node_id=f"gid://shopify/DiscountCodeNode/{node_id}",
    )
    state.store_code(discount)
    return {
        "codeDiscountNode": _discount_node(discount),
        "userErrors": [],
    }


def _offer_id_from(payload: dict[str, Any]) -> str | None:
    """Read the offer id out of the mutation's ``tags``.

    D22 stores codes keyed by ``offer_id``, but Shopify's discount input has no such field —
    the only place a caller can put an application-level correlation id is ``tags``. So the
    convention is a ``offer:<offer_id>`` tag, and it is read here, **after** the code has
    already been chosen. The code is never derived from it (D22).
    """
    tags = payload.get("tags")
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("offer:"):
            return tag[len("offer:") :]
    return None


def _discount_node(discount: DiscountCode) -> dict[str, Any]:
    """The ``DiscountCodeNode`` shape the mutation returns."""
    if discount.percentage is not None:
        value: dict[str, Any] = {"percentage": discount.percentage}
    else:
        value = {
            "amount": {
                "amount": str(discount.fixed_amount),
                "currencyCode": discount.currency_code,
            },
            "appliesOnEachItem": False,
        }
    return {
        "id": discount.node_id,
        "codeDiscount": {
            "__typename": "DiscountCodeBasic",
            "title": discount.title,
            "status": "ACTIVE",
            "codes": {"nodes": [{"code": discount.code}]},
            # `codesCount` is a `Count`, which has TWO non-null fields. A fixture carrying
            # only `count` is structurally wrong the moment a caller selects `precision`.
            "codesCount": {"count": 1, "precision": "EXACT"},
            "startsAt": iso(discount.starts_at) if discount.starts_at else None,
            "endsAt": iso(discount.ends_at) if discount.ends_at else None,
            "usageLimit": discount.usage_limit,
            "asyncUsageCount": discount.usage_count,
            "appliesOncePerCustomer": discount.applies_once_per_customer,
            "combinesWith": discount.combines_with.to_wire(),
            "customerGets": {
                "value": value,
                "items": {"__typename": "AllDiscountItems", "allItems": True},
            },
        },
    }


# ---------------------------------------------------------------------------------------
# orders
# ---------------------------------------------------------------------------------------


def _resolve_orders(state: StubState, arguments: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The ``orders`` query subset.

    Supported arguments: ``first``, ``after``, ``reverse`` and a ``query`` search string
    restricted to the two filters this system needs. An unsupported filter is a **request
    error**, not a silently ignored token: Shopify itself rejects unknown filter keys, and a
    stub that ignored them would let a consumer ship a filter that never filtered.
    """
    del now
    first = arguments.get("first")
    if first is None:
        raise _FieldError("Field 'orders' is missing required arguments: first (or last)")
    if not isinstance(first, int) or not 1 <= first <= 250:
        raise _FieldError("Argument 'first' must be between 1 and 250")
    after = arguments.get("after")
    if after is not None and not isinstance(after, str):
        raise _FieldError("Argument 'after' must be a String")
    reverse = bool(arguments.get("reverse", False))
    selected = _apply_search(list(state.orders.values()), arguments.get("query"), state)
    return orders_connection(selected, state, first=first, after=after, reverse=reverse)


#: The two search filters the ``orders`` query supports.
SUPPORTED_FILTERS = ("checkout_token", "discount_code")


def _apply_search(orders: list[Any], search: Any, state: StubState) -> list[Any]:
    """Apply the ``query:`` search string.

    Grammar accepted: whitespace-separated ``key:value`` terms, ANDed. Bare terms and
    unsupported keys raise, because a filter that quietly matches everything is worse than
    no filter at all.
    """
    del state
    if search in (None, ""):
        return orders
    if not isinstance(search, str):
        raise _FieldError("Argument 'query' must be a String")
    selected = orders
    for term in search.split():
        if ":" not in term:
            raise _FieldError(f"Unsupported search term {term!r}")
        key, _, value = term.partition(":")
        if key not in SUPPORTED_FILTERS:
            raise _FieldError(
                f"Unsupported search field {key!r}; this stub supports "
                f"{', '.join(SUPPORTED_FILTERS)}"
            )
        if key == "checkout_token":
            selected = [order for order in selected if order.checkout_token == value]
        else:
            selected = [
                order
                for order in selected
                if order.discount_code and order.discount_code.upper() == value.upper()
            ]
    return selected


# ---------------------------------------------------------------------------------------
# webPixelCreate / webhookSubscriptionCreate
# ---------------------------------------------------------------------------------------


def _resolve_web_pixel_create(
    state: StubState, arguments: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Install the web pixel. ``webPixel: {settings: …}``.

    Installing the pixel turns firing **on**: a store with no pixel installed is the
    non-firing case, and conflating "not installed" with "installed but configured to drop
    everything" is the exact distinction :class:`~shopify_stub.state.PixelMode` exists to
    keep.
    """
    payload = arguments.get("webPixel")
    if not isinstance(payload, dict):
        raise _FieldError("Variable $webPixel of type WebPixelInput! was provided invalid value")
    settings = payload.get("settings")
    if not settings:
        return {
            "webPixel": None,
            "userErrors": [
                {
                    "field": ["webPixel", "settings"],
                    "message": "Settings can't be blank",
                    "code": "BLANK",
                }
            ],
        }
    if isinstance(settings, str):
        # Shopify's WebPixelInput.settings is a JSON *string*.
        try:
            parsed = json.loads(settings)
        except ValueError as exc:
            raise _FieldError(f"settings is not valid JSON: {exc}") from exc
    else:
        parsed = settings
    pixel = WebPixel(id=state.next_web_pixel_id(), settings=parsed, created_at=now)
    state.web_pixels[pixel.id] = pixel
    collector = parsed.get("collectorUrl") if isinstance(parsed, dict) else None
    if isinstance(collector, str) and collector:
        state.pixel_collector_url = collector
    return {
        "webPixel": {
            "id": f"gid://shopify/WebPixel/{pixel.id}",
            # Asymmetric on purpose: `WebPixelInput.settings` is the JSON scalar, and the
            # documented example passes an OBJECT in variables while the response returns a
            # SERIALIZED STRING. A stub that echoed the object back would let a consumer
            # skip the JSON.parse it needs against the real API.
            "settings": json.dumps(parsed, separators=(",", ":")),
        },
        "userErrors": [],
    }


def _resolve_webhook_subscription_create(
    state: StubState, arguments: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Register a webhook subscription.

    ``topic: WebhookSubscriptionTopic!`` and
    ``webhookSubscription: WebhookSubscriptionInput!`` with a ``callbackUrl``. The stub
    accepts only the three topics this system is allowed to subscribe: the frozen install
    contract asserts **exact set equality** on those three, because subscribing
    ``checkouts/*`` would create a second checkout-observation path that C5 forbids.
    """
    topic_raw = arguments.get("topic")
    topic = TOPIC_ENUM_TO_NAME.get(str(topic_raw))
    if topic is None:
        return {
            "webhookSubscription": None,
            "userErrors": [
                {
                    "field": ["topic"],
                    "message": (
                        f"Topic {topic_raw!r} is not supported by this stub; supported "
                        f"topics are {', '.join(sorted(TOPIC_ENUM_TO_NAME))}"
                    ),
                }
            ],
        }
    subscription_input = arguments.get("webhookSubscription") or {}
    # `uri` is the current field; `callbackUrl` is deprecated but still accepted, because a
    # consumer written against an older example must not silently register nothing.
    callback_url = subscription_input.get("uri") or subscription_input.get("callbackUrl")
    if not isinstance(callback_url, str) or not callback_url:
        return {
            "webhookSubscription": None,
            # `UserError` carries ONLY `field` and `message` — no `code`. The web-pixel
            # mutation's error type does have one; copying `code` across is a real mistake
            # this stub declines to teach.
            "userErrors": [
                {
                    "field": ["webhookSubscription", "uri"],
                    "message": "Uri can't be blank",
                }
            ],
        }
    existing = [
        s
        for s in state.subscriptions.values()
        if s.topic == topic and s.callback_url == callback_url
    ]
    if existing:
        # Shopify rejects a duplicate (topic, address) pair rather than creating a second
        # subscription that would double-deliver every event.
        return {
            "webhookSubscription": None,
            "userErrors": [
                {
                    "field": ["webhookSubscription", "uri"],
                    "message": "Address for this topic has already been taken",
                }
            ],
        }
    subscription = WebhookSubscription(
        id=state.next_subscription_id(),
        topic=topic,
        callback_url=callback_url,
        created_at=now,
    )
    state.subscriptions[subscription.id] = subscription
    return {
        "webhookSubscription": {
            "id": f"gid://shopify/WebhookSubscription/{subscription.id}",
            "legacyResourceId": str(subscription.id),
            "topic": str(topic_raw),
            "uri": callback_url,
            "format": subscription_input.get("format", "JSON"),
            "apiVersion": {"handle": state.config.api_version},
            "includeFields": subscription_input.get("includeFields", []),
            "createdAt": iso(now),
            "updatedAt": iso(now),
        },
        "userErrors": [],
    }


def _parse_datetime(raw: Any) -> datetime | None:
    """Parse an ISO-8601 instant, accepting the ``Z`` suffix Shopify sends."""
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        raise _FieldError(f"expected an ISO-8601 DateTime, got {raw!r}")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _FieldError(f"expected an ISO-8601 DateTime, got {raw!r}") from exc


__all__ = [
    "SUPPORTED_MUTATIONS",
    "SUPPORTED_QUERIES",
    "GraphQLHTTPError",
    "execute",
    "throttle_extensions",
]
