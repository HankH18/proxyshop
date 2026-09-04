"""The D22 cart permalink, built in exactly one place.

D22 pins the shape, and the stub parser (``services/shopify-stub/src/permalink.py``), the
exchange builder (``apps/exchange/src/checkout/codes.build_cart_permalink``) and this one
must agree character for character or a buyer follows a URL nobody can reconcile::

    https://{shop_domain}/cart/{variant_id}:{quantity}?discount={code}

The host is the store's own registered ``.myshopify.com`` domain, resolved by
:func:`~merchant_svc.codes.offer.shop_domain_for` from the *store*, never from the offer —
see that function for why a host taken from a bidder's JSON is an open redirect.

An order-level discount names no variant, and Shopify has no cart permalink for one. That
case gets the shop's cart with the code pre-applied (``/cart?discount=…``), which is the
closest thing that actually works, rather than a ``/cart/None:1`` URL that 404s.
"""

from __future__ import annotations

from urllib.parse import quote

__all__ = ["build_cart_permalink"]


def build_cart_permalink(
    *,
    shop_domain: str,
    code: str,
    variant_id: str | None = None,
    quantity: int = 1,
) -> str:
    """Build the D22 cart permalink for a code that is already minted.

    Args:
        shop_domain: the store's own bare ``<name>.myshopify.com`` host, already validated.
        code: the minted discount code, pre-applied by the ``discount`` query parameter.
        variant_id: the numeric variant the offer is for, or ``None`` for an order-level
            discount.
        quantity: how many of that variant the cart starts with.

    Returns:
        An absolute ``https://`` URL. Every interpolated segment is percent-encoded, so a
        code or a variant carrying a ``?``, a ``#`` or a ``/`` cannot restructure the URL.
    """
    discount = quote(str(code), safe="")
    if variant_id is None:
        return f"https://{shop_domain}/cart?discount={discount}"
    return (
        f"https://{shop_domain}/cart/{quote(str(variant_id), safe='')}:{int(quantity)}"
        f"?discount={discount}"
    )
