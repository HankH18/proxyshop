"""The cart permalink — build, parse, and host validation. D22 / release blocker S8-3.

D22 pins one template, used identically by this stub's parser (T-013), the merchant builder
(T-052) and the exchange validator (T-033)::

    https://{shop_domain}/cart/{variant_id}:{quantity}?discount={code}

and one validation rule: the permalink **host** is compared to ``app.sellers.domain`` by
**exact match**. No subdomain wildcards. That is release blocker S8-3, and the reason it is
a blocker is that a wildcard match lets ``evil.merchant.example.com`` pass a check written
for ``merchant.example.com`` — an attacker-controlled checkout destination wearing a
trusted store's name.

This module is intentionally dependency-free (stdlib only) so T-052 and T-033 can be held
to the same parse without importing a web framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlsplit

#: The one template. Written out so a reader can diff it against D22 by eye.
PERMALINK_TEMPLATE = "https://{shop_domain}/cart/{variant_id}:{quantity}?discount={code}"


class PermalinkError(ValueError):
    """The string is not a cart permalink of the pinned shape."""


@dataclass(frozen=True)
class CartPermalink:
    """A parsed cart permalink.

    Attributes:
        shop_domain: the URL's host, lower-cased, port stripped.
        variant_id: the numeric variant id as a string (Shopify's are 64-bit; keeping the
            string avoids a lossy int round-trip in JSON).
        quantity: the requested quantity.
        code: the ``discount`` query parameter, or ``None`` when absent.
    """

    shop_domain: str
    variant_id: str
    quantity: int
    code: str | None

    def to_url(self) -> str:
        """Render back to the pinned template."""
        return build_permalink(
            shop_domain=self.shop_domain,
            variant_id=self.variant_id,
            quantity=self.quantity,
            code=self.code,
        )


def build_permalink(
    *,
    shop_domain: str,
    variant_id: str | int,
    quantity: int = 1,
    code: str | None = None,
) -> str:
    """Render the D22 template.

    Args:
        shop_domain: the store's host, e.g. ``"demo-store.myshopify.com"``. A scheme or a
            path here is a caller bug and raises rather than producing a URL with two
            schemes in it.
        variant_id: Shopify variant id.
        quantity: units of that variant. Must be >= 1.
        code: the discount code, or ``None`` to omit the query string entirely.

    Returns:
        ``https://{shop_domain}/cart/{variant_id}:{quantity}?discount={code}``.

    Raises:
        PermalinkError: the arguments cannot produce a well-formed permalink.
    """
    if "://" in shop_domain or "/" in shop_domain:
        raise PermalinkError(f"shop_domain must be a bare host, got {shop_domain!r}")
    if not shop_domain:
        raise PermalinkError("shop_domain must not be empty")
    if quantity < 1:
        raise PermalinkError(f"quantity must be >= 1, got {quantity}")
    variant = str(variant_id)
    if not variant.isdigit():
        raise PermalinkError(f"variant_id must be numeric, got {variant!r}")
    base = f"https://{shop_domain}/cart/{variant}:{quantity}"
    if code is None:
        return base
    return f"{base}?discount={quote(code, safe='')}"


def parse_permalink(url: str) -> CartPermalink:
    """Parse a cart permalink of the pinned shape.

    Args:
        url: the full URL.

    Returns:
        The parsed :class:`CartPermalink`.

    Raises:
        PermalinkError: wrong scheme, wrong path shape, non-numeric variant, unparseable
            quantity, or a repeated ``discount`` parameter. Every one of these is a
            *structural* failure of the link, which is different from an invalid discount
            code — an invalid code parses fine and is then silently ignored at redemption
            (acceptance criterion 2).
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise PermalinkError(f"permalink must be https, got {parts.scheme!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise PermalinkError("permalink has no host")
    path = parts.path
    if not path.startswith("/cart/"):
        raise PermalinkError(f"permalink path must start with /cart/, got {path!r}")
    remainder = path[len("/cart/") :]
    if "/" in remainder:
        raise PermalinkError(f"permalink path has extra segments: {path!r}")
    if ":" not in remainder:
        raise PermalinkError(f"permalink must be /cart/<variant>:<quantity>, got {path!r}")
    variant_id, _, quantity_text = remainder.partition(":")
    if not variant_id.isdigit():
        raise PermalinkError(f"variant id must be numeric, got {variant_id!r}")
    if not quantity_text.isdigit():
        raise PermalinkError(f"quantity must be numeric, got {quantity_text!r}")
    quantity = int(quantity_text)
    if quantity < 1:
        raise PermalinkError("quantity must be >= 1")
    query = parse_qs(parts.query, keep_blank_values=True)
    codes = query.get("discount", [])
    if len(codes) > 1:
        raise PermalinkError("permalink carries more than one discount parameter")
    code = codes[0] if codes else None
    return CartPermalink(
        shop_domain=host,
        variant_id=variant_id,
        quantity=quantity,
        code=code or None,
    )


def host_matches(permalink_url: str, seller_domain: str) -> bool:
    """S8-3: exact host match against ``app.sellers.domain``. No wildcards.

    Case is normalised (DNS is case-insensitive) and a trailing dot is stripped, because
    ``example.com.`` and ``example.com`` are the same name — accepting one and rejecting the
    other would be a correctness bug, not a security property. Everything else is compared
    literally, so a subdomain, a suffix, a homograph or a port-bearing host all fail.

    Args:
        permalink_url: the full permalink.
        seller_domain: the value of ``app.sellers.domain`` for the store the offer belongs
            to.

    Returns:
        ``True`` only when the permalink's host is exactly that domain.

    Raises:
        PermalinkError: ``permalink_url`` is not parseable as a permalink. A malformed URL
            is never "matching" and never "not matching" — it is a different failure, and
            collapsing it into ``False`` hides link bugs behind domain-mismatch messages.
    """
    parsed = parse_permalink(permalink_url)
    expected = seller_domain.strip().lower().rstrip(".")
    actual = parsed.shop_domain.rstrip(".")
    if not expected:
        return False
    return actual == expected
