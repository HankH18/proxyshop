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

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlsplit

#: The one template. Written out so a reader can diff it against D22 by eye.
PERMALINK_TEMPLATE = "https://{shop_domain}/cart/{variant_id}:{quantity}?discount={code}"

#: One DNS label: letters, digits and hyphens, 1-63 characters, no leading or trailing
#: hyphen. Deliberately an allow-list — a deny-list of "the delimiters we thought of" is how
#: ``@``, ``\``, ``:``, ``?`` and ``#`` all walked past the original two-character check.
_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

#: RFC 1035's limit on a fully-qualified name.
MAX_HOST_LENGTH = 253


class PermalinkError(ValueError):
    """The string is not a cart permalink of the pinned shape."""


def _assert_bare_host(shop_domain: str) -> None:
    """Raise unless ``shop_domain`` is a bare DNS name — no scheme, port, path or userinfo.

    **This is the only place that decides what a host may contain**, and it is an allow-list
    for a reason. The check it replaces tested for exactly two substrings, ``"://"`` and
    ``"/"``, which meant ``good.example.com@attacker.tld`` rendered into a *live* checkout
    link whose real host was ``attacker.tld`` while :func:`build_permalink` reported success —
    the builder's own docstring promises the opposite. ``\\``, ``:``, ``?`` and ``#`` got
    through the same gap, and ``?`` in particular failed later and somewhere else, reporting
    "permalink path must start with /cart/" for what is a bad *host*.

    A single trailing root dot is accepted: ``example.com.`` and ``example.com`` are the same
    name, and :func:`host_matches` already treats them as equal, so rejecting one here would
    make a legal name un-buildable and break :meth:`CartPermalink.to_url`.

    Raises:
        PermalinkError: the value is empty, too long, or is not a dotted sequence of DNS
            labels.
    """
    if not shop_domain:
        raise PermalinkError("shop_domain must not be empty")
    candidate = shop_domain[:-1] if shop_domain.endswith(".") else shop_domain
    if len(shop_domain) > MAX_HOST_LENGTH:
        raise PermalinkError(
            f"shop_domain must be at most {MAX_HOST_LENGTH} characters, got {len(shop_domain)}"
        )
    labels = candidate.split(".")
    if not candidate or any(not _LABEL.match(label) for label in labels):
        raise PermalinkError(
            f"shop_domain must be a bare host (dotted DNS labels only), got {shop_domain!r}"
        )


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
        shop_domain: the store's host, e.g. ``"demo-store.myshopify.com"``. Anything that is
            not a bare DNS name — a scheme, a path, a port, userinfo, a query or a fragment —
            is a caller bug and raises (:func:`_assert_bare_host`) rather than rendering a URL
            whose real host is somebody else's.
        variant_id: Shopify variant id.
        quantity: units of that variant. Must be >= 1.
        code: the discount code, or ``None`` to omit the query string entirely.

    Returns:
        ``https://{shop_domain}/cart/{variant_id}:{quantity}?discount={code}``.

    Raises:
        PermalinkError: the arguments cannot produce a well-formed permalink.
    """
    _assert_bare_host(shop_domain)
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
        PermalinkError: wrong scheme, wrong path shape, an explicit port, non-numeric
            variant, unparseable quantity, or a repeated ``discount`` parameter. Every one of
            these is a *structural* failure of the link, which is different from an invalid
            discount code — an invalid code parses fine and is then silently ignored at
            redemption (acceptance criterion 2).

    A port is refused rather than ignored. :class:`CartPermalink` keeps only
    ``parts.hostname``, which **drops** the port, so accepting ``…example.com:8443`` gave a
    parse whose :meth:`CartPermalink.to_url` rendered ``https://…example.com/…`` — a silent
    rewrite of the buyer's checkout destination from :8443 to implicit :443. D22's template
    carries no port, so there is no port to preserve and nothing legitimate to lose.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise PermalinkError(f"permalink must be https, got {parts.scheme!r}")
    try:
        port = parts.port
    except ValueError as exc:  # e.g. "host:notaport" — urllib raises rather than returning
        raise PermalinkError(f"permalink has an unparseable port: {url!r}") from exc
    if port is not None:
        raise PermalinkError(
            f"permalink must not carry a port (D22's template has none), got :{port}"
        )
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
    literally, so a subdomain, a suffix and a homograph all fail.

    A **port-bearing host never reaches this comparison**: :func:`parse_permalink` refuses it
    outright. It has to, because the comparison here reads ``parts.hostname``, which strips
    the port — so ``store.example.com:8443`` would have compared *equal* to
    ``store.example.com`` on a link that sends the buyer somewhere else entirely.

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
