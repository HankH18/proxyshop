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

**"Absent" and "off-domain" are different conditions, and this module only answers the
second.** :func:`is_on_domain` is a question about a URL, so an empty URL is not on-domain —
there is no host to be on the domain. That is *not* the same as "this offer must be refused":
a list-price fallback offer (R10, built by ``collect_bids`` for every Tier-0 and silent
store) carries no ``checkout_url`` at all, and treating its absence as a spoof refused every
fallback bid the exchange had manufactured for itself. The caller decides — see
``CheckoutProvider.checkout``, which validates the offer's URL only when the offer has one
and validates the *provider's* permalink unconditionally.

**Every merchant-controlled value this module puts in a message goes through
``redaction.safe_token`` or ``redaction.redact_url``, and that is T-215 pass 3.** The reason
strings below are built from four things a merchant chose — the URL, its parsed host, its
scheme, and the text of a ``urlsplit`` ``ValueError`` (which quotes the offending fragment) —
and after a code has been minted, prose from here is carried on ``OrphanedCheckoutCode``
into a persisted ``policy_event`` and into the client-visible ``denial_reason``. Two earlier
fixes redacted at *that* boundary instead, and both were defeated by exactly this module:
the boundary can only remove values it was handed, and ``host.lower()`` here is an
independent second copy of the host that no URL value it holds contains. The ``secret``
parameter is how the decision is taken where the provenance is known — see
``redaction.safe_token``.

``secret`` is empty on the **pre-mint** call and that is not an oversight: the port checks
the offer's ``checkout_url`` before ``mint`` runs, so at that point no code exists anywhere
and there is nothing to keep out of the message. It is passed on the **post-mint** permalink
check, which is the one refusal that happens with a live discount behind it.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from .redaction import redact_url, safe_token

__all__ = ["ALLOWED_CHECKOUT_SCHEMES", "OffDomainCheckout", "assert_on_domain", "is_on_domain"]

#: A checkout URL a browser can actually be redirected to.
ALLOWED_CHECKOUT_SCHEMES = frozenset({"https", "http"})


class OffDomainCheckout(ValueError):
    """A checkout URL whose host is not the seller's registered domain (C10, D22)."""


def _as_text(value: object) -> str:
    """``str(value)`` for a value a merchant supplied, or a placeholder. **Cannot raise.**

    Nothing coerces ``MintedCheckout.permalink_url`` or an offer's ``checkout_url`` to
    ``str`` on the way in, so both of the values this module is handed can be arbitrary
    objects. Measured on this tree: a provider returning a ``permalink_url`` whose
    ``__str__`` raises made :func:`_reason` raise out of ``CheckoutProvider.checkout``, so
    the post-mint host check died instead of refusing — no ``OrphanedCheckoutCode``, no
    ``code_created`` event, and a live discount nobody could revoke. An unreadable URL is a
    refusal, never an exception out of the refusal path.
    """
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return "<unrenderable-url>"


def _reason(url: object, registered_domain: str, *, secret: str = "") -> str | None:
    """Return why ``url`` is off-domain, or ``None`` when it is on-domain.

    ``secret`` is a live discount code that must not be recoverable from the returned prose.
    Every merchant-controlled fragment below is rendered through
    :func:`~.redaction.safe_token`, which drops the fragment whole when it could spell that
    code and returns it untouched otherwise — so the diagnostic survives in the ordinary
    case and fails closed in the hostile one. With ``secret`` empty (the pre-mint call, and
    :func:`is_on_domain`) nothing has been minted and every fragment is rendered as-is,
    which is the behaviour every existing caller already has.
    """
    if not registered_domain or not str(registered_domain).strip():
        return "the seller has no registered domain to compare against"
    # `_as_text` first, then the emptiness test on the TEXT: `if not url` would run the
    # object's own `__bool__`/`__len__`, which is one more merchant-authored hook on the
    # refusal path.
    text = _as_text(url)
    if not text.strip():
        return "the offer carries no checkout_url"

    try:
        parts = urlsplit(text)
    except ValueError as exc:
        # The ValueError's own text QUOTES the offending fragment of the merchant's URL,
        # so it is merchant-controlled prose and gets the same treatment as the URL itself.
        return (
            f"checkout_url is not a parsable URL ({safe_token(exc, secret, label='parse-error')})"
        )

    scheme = parts.scheme
    if scheme.lower() not in ALLOWED_CHECKOUT_SCHEMES:
        # A scheme is merchant-controlled and `urlsplit` accepts any `[A-Za-z][A-Za-z0-9+.-]*`,
        # so `PSX-LIVE-01://…` parses and spells a code here.
        return (
            f"checkout_url scheme {safe_token(scheme, secret, label='scheme')!r} is not one "
            f"a checkout can be redirected to (allowed: {sorted(ALLOWED_CHECKOUT_SCHEMES)})"
        )

    try:
        host = parts.hostname
    except ValueError as exc:
        return f"checkout_url has no parsable host ({safe_token(exc, secret, label='parse-error')})"

    if not host:
        return "checkout_url carries no host"

    expected = str(registered_domain).strip().lower().rstrip(".")
    actual = host.lower().rstrip(".")
    if actual != expected:
        # `actual` is the SECOND, independently normalised copy of the merchant's host that
        # defeated pass 2: a boundary redactor holding the URL as a value never contained
        # this string, and `.lower()` put it out of reach of a case-sensitive literal layer.
        # It is guarded HERE, where it is known to have come off a merchant reply.
        #
        # `expected` is guarded for a reason that is easy to miss and is the whole of the
        # build-site claim. It reads like the PLATFORM's value — and it is, on a call site
        # that wired `CheckoutRequest.registered_domains`. On the DEFAULT unwired path,
        # which is the legacy behaviour the frozen contract pins,
        # `registered_domain_for` returns `request.store_domain`, read straight off the bid
        # — so it is merchant-authored, it is a *third* independently normalised string
        # (`.strip().lower().rstrip(".")`) that no boundary redactor holds as a value, and a
        # store is free to spell the code it is about to mint in it. Measured at the build
        # site: with `secret="PSX-Ω-42"` and a bid claiming `xn--psx--42-bkf.example.com`,
        # this sentence published a recoverable code. Downstream fail-closed layers happened
        # to catch it end to end, which is precisely why it must be guarded here: the claim
        # this module makes is that EVERY merchant-controlled fragment renders through a
        # guard at the point it is built, and a fragment that relies on a later layer is a
        # fragment the next refactor publishes.
        return (
            f"checkout_url host {safe_token(actual, secret, label='host')!r} is not the "
            f"registered seller domain "
            f"{safe_token(expected, secret, label='registered-domain')!r} — hosts are "
            f"compared by exact equality (D22/C10)"
        )
    return None


def is_on_domain(url: object, registered_domain: str) -> bool:
    """True when ``url``'s host is exactly ``registered_domain``."""
    return _reason(url, registered_domain) is None


def assert_on_domain(
    url: object, registered_domain: str, *, what: str = "checkout_url", secret: str = ""
) -> None:
    """Raise :class:`OffDomainCheckout` unless ``url``'s host is the registered domain.

    Pass ``secret`` — the code that has already been minted — on any call that happens
    **after** a provider has issued a real discount. The message this raises is carried into
    a persisted ``policy_event`` and the client-visible ``denial_reason``, and ``secret`` is
    what lets this function keep the code out of it while it still knows which parts of the
    sentence the merchant wrote.
    """
    reason = _reason(url, registered_domain, secret=secret)
    if reason is not None:
        # With a secret in play the URL is reduced to scheme+host HERE rather than left for
        # a downstream replace to find: the reduction is the same one `redact_code`'s
        # structural layer would apply, done at the point the prose is built so that no
        # unreduced spelling of the URL ever exists in a string anyone might copy.
        #
        # `_as_text` first for the same reason `_reason` uses it: `{url!r}` runs the
        # object's `__repr__`, and the object came off a merchant reply.
        text = _as_text(url)
        shown = redact_url(text, secret) if secret else text
        raise OffDomainCheckout(f"{what}: {reason} (url={shown!r})")
