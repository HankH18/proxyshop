"""Shop-domain handling: the app talks to ``<name>.myshopify.com`` and to nothing else.

Every Shopify surface this app touches is addressed by the shop's ``.myshopify.com`` host —
the Admin GraphQL endpoint, the OAuth authorize redirect, the cart permalink. A host that
is not a bare DNS name is the classic way that becomes an open redirect: the userinfo form
``good.example.com@attacker.tld`` renders as the merchant's own store in a log line and
resolves to the attacker's server in a browser.

The rule here is the same one ``shopify_stub.permalink`` applies to the stub's own
configured domain, which is why an install can never point at a host the stub itself would
refuse to be.
"""

from __future__ import annotations

import re

#: Every shop this app can be installed on lives under this suffix.
MYSHOPIFY_SUFFIX = ".myshopify.com"

#: One DNS label: alphanumerics and inner hyphens, 1-63 characters.
_LABEL = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class InvalidShopDomain(ValueError):
    """The shop domain is not a bare ``<name>.myshopify.com`` host."""


def normalize_shop_domain(raw: str) -> str:
    """Return the lower-cased bare shop host, or raise.

    Args:
        raw: the candidate host, e.g. ``"Acceptance-Store.myshopify.com"``.

    Returns:
        The lower-cased host.

    Raises:
        InvalidShopDomain: the value carries a scheme, port, path, query, fragment,
            userinfo, whitespace, a control byte, an empty label, or does not end in
            ``.myshopify.com``.
    """
    if not isinstance(raw, str):
        raise InvalidShopDomain(f"shop domain must be a string, got {type(raw).__name__}")
    # Control bytes are refused, never stripped. A trailing LF on a host is a
    # response-splitting payload, and quietly removing it turns an attack into a value the
    # rest of the system treats as ordinary — the same rule `shopify_stub.permalink`
    # applies to the stub's own configured domain. Only spaces are forgiven.
    if any((ord(char) < 0x21 and char != " ") or ord(char) == 0x7F for char in raw):
        raise InvalidShopDomain(f"shop domain carries a control character: {raw!r}")
    candidate = raw.strip().lower()
    if not candidate:
        raise InvalidShopDomain("shop domain is empty")
    if any(char.isspace() for char in candidate):
        raise InvalidShopDomain(f"shop domain carries whitespace: {raw!r}")
    for forbidden in ("://", "@", "/", "?", "#", ":", "\\", "\r", "\n"):
        if forbidden in candidate:
            raise InvalidShopDomain(
                f"shop domain must be a bare host, not {raw!r} (contains {forbidden!r})"
            )
    if not candidate.endswith(MYSHOPIFY_SUFFIX):
        raise InvalidShopDomain(f"shop domain must end in {MYSHOPIFY_SUFFIX}, got {raw!r}")
    labels = candidate.split(".")
    if len(labels) < 3 or not all(_LABEL.match(label) for label in labels):
        raise InvalidShopDomain(f"shop domain is not a well-formed host: {raw!r}")
    return candidate


def is_shop_domain(raw: str) -> bool:
    """``True`` when :func:`normalize_shop_domain` would accept ``raw``."""
    try:
        normalize_shop_domain(raw)
    except InvalidShopDomain:
        return False
    return True
