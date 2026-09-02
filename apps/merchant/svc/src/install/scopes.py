"""The OAuth scopes this app asks a merchant for — and the ones it refuses to ask for.

SPEC C5 is the whole of the policy: *"GraphQL Admin only; web pixel app extension is the
only checkout observation path; **no protected-customer-data scopes**; ``read_orders``
(60-day window) is sufficient."* Three scopes carry the entire merchant integration:

============================ ===========================================================
scope                        what stops working without it
============================ ===========================================================
``read_orders``              order reconciliation (R4). The ``orders`` query and the
                             three order webhooks all read through it.
``write_discounts``          ``discountCodeBasicCreate`` — T-052 mints one single-use
                             code per accepted offer.
``write_pixels``             ``webPixelCreate`` — the web pixel extension this module
                             installs.
============================ ===========================================================

**A declared divergence from real Shopify, stated rather than discovered.** Shopify's own
documentation pairs ``write_pixels`` with ``read_customer_events`` for a web-pixel
extension. ``read_customer_events`` is a *protected customer data* scope, so C5 forbids it
and :data:`PROTECTED_CUSTOMER_DATA_SCOPES` lists it among the refusals. This app therefore
requests a scope set that a real Shopify install may reject or degrade, and that trade is
deliberate: the collector (T-051) accepts join keys only and never customer data, so a
scope granting customer-event access would be authority this system has no use for. The
offline stub does not enforce scopes at all, so nothing here is *proven* against Shopify —
SPEC A4 says as much.
"""

from __future__ import annotations

from collections.abc import Iterable

#: The scopes the authorize URL asks for. Order is stable so the generated URL is stable.
REQUIRED_SCOPES: tuple[str, ...] = ("read_orders", "write_discounts", "write_pixels")

#: Shopify's protected-customer-data scopes. C5 forbids every one of them; asking for any
#: is a refusal here rather than a request Shopify gets to decline.
PROTECTED_CUSTOMER_DATA_SCOPES: frozenset[str] = frozenset(
    {
        "read_all_orders",
        "read_customer_events",
        "read_customer_merge",
        "read_customer_payment_methods",
        "read_customers",
        "read_marketing_events",
        "write_customer_merge",
        "write_customers",
    }
)


class ProtectedScopeRequested(ValueError):
    """A caller asked for a scope SPEC C5 forbids this app from ever holding."""

    def __init__(self, offending: Iterable[str]) -> None:
        self.offending: tuple[str, ...] = tuple(sorted(offending))
        super().__init__(
            "SPEC C5 forbids protected-customer-data scopes; refused: " + ", ".join(self.offending)
        )


#: Shopify writes a granted scope set as one comma-joined string — the ``scope`` field of
#: the token-exchange response is ``"read_orders,write_pixels"``, not a list. A scope entry
#: is therefore split on commas before it is checked, or the whole string is treated as one
#: unrecognised scope name and every protected scope inside it goes unseen.
SCOPE_SEPARATOR = ","


def normalize_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    """Lower-case, strip, split and de-duplicate a scope list, preserving first-seen order.

    Args:
        scopes: an iterable of scope names. Each entry may itself be a comma-joined set,
            which is the spelling Shopify's own token response uses.

    Raises:
        TypeError: ``scopes`` is a bare ``str`` or ``bytes``.

    A bare ``str`` is refused rather than accepted, because a ``str`` *is* an
    ``Iterable[str]`` — of single characters. ``normalize_scopes("read_customers")`` used to
    return ``('r', 'e', 'a', 'd', …)``, none of which is a protected scope name, so the C5
    check passed and the forbidden scope was admitted. The one input a caller is most
    likely to pass by mistake was the one input that silently disarmed the guard.
    """
    if isinstance(scopes, str | bytes):
        raise TypeError(
            "scopes must be an iterable of scope names, not a bare "
            f"{type(scopes).__name__}: a string iterates as single characters, none of "
            "which is a scope name, so every protected scope in it would go undetected. "
            f"Pass [{scopes!r}] or split it on {SCOPE_SEPARATOR!r}."
        )
    seen: dict[str, None] = {}
    for scope in scopes:
        for part in str(scope).split(SCOPE_SEPARATOR):
            cleaned = part.strip().lower()
            if cleaned:
                seen.setdefault(cleaned, None)
    return tuple(seen)


def unauthorized_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    """The protected-customer-data scopes present in ``scopes``, sorted."""
    return tuple(sorted(set(normalize_scopes(scopes)) & PROTECTED_CUSTOMER_DATA_SCOPES))


def assert_scopes_allowed(scopes: Iterable[str]) -> tuple[str, ...]:
    """Return the normalized scope list, or raise :class:`ProtectedScopeRequested`.

    This is the only place the C5 rule is enforced, and it is enforced on the *argument*
    rather than on :data:`REQUIRED_SCOPES`: a check against a module constant can never
    refuse anything.

    **What this guard does and does not currently cover.** Its two callers,
    :func:`~merchant_svc.install.oauth.authorize_url` and
    :func:`~merchant_svc.install.flow.install`, both accept a caller-supplied list, and
    both are reached from ``routes.py`` with :data:`REQUIRED_SCOPES` — the module constant.
    So on the deployed HTTP path this refuses nothing today; it refuses real input only
    from a library caller, which is what the tests exercise. It is also the *requested*
    scope set that is checked. Nothing here inspects the set Shopify actually **granted**
    (the ``scope`` field of the token-exchange response), so a shop whose grant differs
    from the request is not detected — stated because the gap is real, not covered.

    Raises:
        ProtectedScopeRequested: any protected-customer-data scope was requested.
        TypeError: ``scopes`` is a bare ``str`` or ``bytes``.
        ValueError: the scope list is empty.
    """
    normalized = normalize_scopes(scopes)
    if not normalized:
        raise ValueError("at least one scope must be requested")
    offending = unauthorized_scopes(normalized)
    if offending:
        raise ProtectedScopeRequested(offending)
    return normalized
