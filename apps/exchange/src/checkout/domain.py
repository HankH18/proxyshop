"""The registered-domain check (D22, C10, S8 release blocker 3).

One rule: the URL's **host** must equal the seller's registered domain, by exact string
equality after the host is lower-cased. Not `in`, not `startswith`, not `endswith`, not "the
registered domain is a suffix". Every weaker comparison is defeated by a URL an attacker can
register today, and the five shapes below are the ones that defeat each of them:

=====================  ===========================================  ====================
shape                  example against ``seller.example.com``       defeats
=====================  ===========================================  ====================
rival domain           ``https://attacker.tld/cart/…``              nothing — the easy one
suffix spoof           ``https://evil-seller.example.com.evil.tld`` ``in`` / substring
glued suffix spoof     ``https://evil-seller.example.com/…``        ``in`` / ``endswith``
userinfo spoof         ``https://seller.example.com@evil.tld/…``    ``startswith`` on the raw URL
subdomain              ``https://checkout.seller.example.com/…``    ``endswith``
=====================  ===========================================  ====================

``urlsplit().hostname`` is what makes the userinfo spoof harmless: it returns the host after
the ``@``, which is the host a browser will actually connect to, and it lower-cases it.
Comparing the *raw URL* — the mistake this module exists to prevent — sees
``https://seller.example.com@…`` and believes it.

The scheme is checked too. A ``javascript:`` or ``data:`` "checkout URL" has no host at all,
so a host comparison alone would have to decide what ``None == "seller.example.com"`` means;
here it is refused explicitly, with a reason, instead.
"""

from __future__ import annotations

from urllib.parse import urlsplit

__all__ = ["ALLOWED_CHECKOUT_SCHEMES", "OffDomainCheckout", "assert_on_domain", "is_on_domain"]

#: A checkout URL a browser can actually be redirected to.
ALLOWED_CHECKOUT_SCHEMES = frozenset({"https", "http"})


class OffDomainCheckout(ValueError):
    """A checkout URL whose host is not the seller's registered domain (C10, D22)."""


def _reason(url: str, registered_domain: str) -> str | None:
    """Return why ``url`` is off-domain, or ``None`` when it is on-domain."""
    if not registered_domain or not str(registered_domain).strip():
        return "the seller has no registered domain to compare against"
    if not url or not str(url).strip():
        return "the offer carries no checkout_url"

    try:
        parts = urlsplit(str(url))
    except ValueError as exc:
        return f"checkout_url is not a parsable URL ({exc})"

    if parts.scheme.lower() not in ALLOWED_CHECKOUT_SCHEMES:
        return (
            f"checkout_url scheme {parts.scheme!r} is not one a checkout can be redirected "
            f"to (allowed: {sorted(ALLOWED_CHECKOUT_SCHEMES)})"
        )

    try:
        host = parts.hostname
    except ValueError as exc:
        return f"checkout_url has no parsable host ({exc})"

    if not host:
        return "checkout_url carries no host"

    expected = str(registered_domain).strip().lower().rstrip(".")
    actual = host.lower().rstrip(".")
    if actual != expected:
        return (
            f"checkout_url host {actual!r} is not the registered seller domain "
            f"{expected!r} — hosts are compared by exact equality (D22/C10)"
        )
    return None


def is_on_domain(url: str, registered_domain: str) -> bool:
    """True when ``url``'s host is exactly ``registered_domain``."""
    return _reason(url, registered_domain) is None


def assert_on_domain(url: str, registered_domain: str, *, what: str = "checkout_url") -> None:
    """Raise :class:`OffDomainCheckout` unless ``url``'s host is the registered domain."""
    reason = _reason(url, registered_domain)
    if reason is not None:
        raise OffDomainCheckout(f"{what}: {reason} (url={url!r})")
