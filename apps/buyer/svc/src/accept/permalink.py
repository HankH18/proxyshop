"""Is the exchange's permalink something a browser may be sent to? (T-072, R3/D22/C10)

R3 says the buyer **follows** the exchange's permalink and never mints a checkout URL of
its own. Following is a redirect, so this module treats it as one: everything below is about
what the buyer's browser will actually connect to, never about what the URL looks like.

The rule is one line — **the host must equal the expected domain by exact string equality,
lower-cased** — and every weaker comparison is defeated by a domain an attacker can register
today:

=====================  ===========================================  =====================
shape                  example against ``store-x.example.com``      defeats
=====================  ===========================================  =====================
rival domain           ``https://evil.example/cart/…``              nothing — the easy one
suffix spoof           ``https://store-x.example.com.evil.tld/…``   ``in`` / ``endswith``
glued prefix           ``https://evil-store-x.example.com/…``       ``in`` / substring
userinfo spoof         ``https://store-x.example.com@evil.tld/…``   ``startswith`` on the raw URL
subdomain              ``https://checkout.store-x.example.com/…``   ``endswith``
scheme swap            ``javascript:…`` / ``data:…``                a host check alone
protocol-relative      ``//evil.example/cart``                      a "starts with https" check
control characters     ``https://store-x.example.com\\n@evil.tld``   comparing the RAW text
=====================  ===========================================  =====================

``urlsplit().hostname`` is what makes the userinfo spoof harmless: it returns the host
*after* the ``@``, which is the host a browser connects to, and lower-cases it. The control
character row is the one that is easy to miss — WHATWG requires tab, CR and LF to be
**stripped** from a URL before parsing and CPython's ``urlsplit`` does exactly that, so a
raw-text check and the parse can legitimately disagree about where the URL points. Such a
URL is refused outright rather than parsed, because "the two readings differ" is never a
condition a redirect should resolve by picking one.

Why this is not `exchange.checkout.domain.assert_on_domain`
-----------------------------------------------------------
The exchange has that guard, T-081 verified it catches three spoofs, and it is the
authoritative one — it runs **before** a permalink is handed out and it has the registered
domain from the bid. This module is not a replacement for it and does not duplicate its
redaction machinery. It exists because the buyer→exchange boundary is an HTTP seam: the
buyer is the process that performs the redirect, it is the last code between a response body
and a browser, and "the other side already checked" is not something the side doing the
redirecting can verify. Importing ``exchange.checkout`` from ``buyer_svc`` would turn that
HTTP seam into a Python one and make the buyer's guard fail exactly when the exchange it is
guarding against is the thing that is wrong.

The expected domain is **optional**, and that is deliberate rather than a gap. The frozen
``Shortlist``/``ShortlistSlot`` contract carries no store domain
(``packages/contracts/schemas/protocol.schema.json``: ``slot``, ``bid_ref``, ``fit_score``,
``trust_summary``, ``provenance_labels``), so on today's data a buyer usually has no
domain to compare against; when a caller does have one — a slot enriched with
``store_domain``, or a composition root that knows it — it is enforced exactly. What is
*never* used as the expected domain is the slot's own ``checkout_url``: R3's whole point is
that the slot's URL is not authority for anything, and a spoofed slot would then certify a
spoofed permalink.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .errors import NoPermalinkReturned, OffDomainPermalink, UnsafePermalink

__all__ = [
    "ALLOWED_PERMALINK_SCHEMES",
    "EMPTY_AUTHORITY",
    "REASON_ABSENT",
    "REASON_OFF_DOMAIN",
    "REASON_UNSAFE",
    "STRIPPED_BY_URLSPLIT",
    "permalink_host",
    "reason_unsafe",
    "verify_permalink",
]

#: Refusal codes. :func:`verify_permalink` picks its exception class from the CODE rather
#: than by matching prose in the message — an earlier revision of this file grepped the
#: sentence for "is not the store domain", which turns every reword of a diagnostic into a
#: silent change of which exception a caller sees.
REASON_ABSENT = "absent"
REASON_OFF_DOMAIN = "off_domain"
REASON_UNSAFE = "unsafe"

#: A checkout URL a browser can actually be redirected to. Matches the exchange's
#: ``exchange.checkout.domain.ALLOWED_CHECKOUT_SCHEMES``.
ALLOWED_PERMALINK_SCHEMES = frozenset({"https", "http"})

#: ``scheme:///…`` — an empty authority. The browser's URL parser and ``urlsplit`` read this
#: shape differently (see :func:`_judge`), so it is refused by both halves of this system.
EMPTY_AUTHORITY = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:///")

#: The characters CPython's ``urlsplit`` removes from a URL before parsing it (WHATWG). A
#: URL containing any of them parses to something other than what it reads as, so it is
#: refused rather than parsed.
STRIPPED_BY_URLSPLIT = frozenset({"\t", "\r", "\n"})


def _as_text(value: object) -> str:
    """``str(value)`` for a value from across the seam, or a placeholder. **Cannot raise.**

    Nothing coerces the exchange's ``permalink_url`` to ``str`` on the way in, so this is
    handed arbitrary objects. An unrenderable URL is a refusal, never an exception out of
    the refusal path.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - an unrenderable URL is simply not a URL
        return "<unrenderable-url>"


def _judge(url: object, expected_domain: str = "") -> tuple[str, str] | None:
    """``(code, why)`` when ``url`` may not be followed, else ``None``. Cannot raise."""
    text = _as_text(url)
    if not text.strip():
        return REASON_ABSENT, "the exchange returned no permalink"
    if text != text.strip():
        # Surrounding whitespace has to be either preserved (and then the URL a browser
        # gets is not the URL that was checked) or trimmed (and then R3's "unmodified" is
        # already broken). Neither is acceptable, so it is refused instead.
        return (
            REASON_UNSAFE,
            "the permalink is padded with whitespace, so it cannot be followed unmodified",
        )
    found = sorted(STRIPPED_BY_URLSPLIT & set(text))
    if found:
        return (
            REASON_UNSAFE,
            f"the permalink contains {[repr(c) for c in found]}, which URL parsing strips "
            f"and which therefore makes the text and the destination disagree",
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        return REASON_UNSAFE, "the permalink contains control characters"
    if EMPTY_AUTHORITY.match(text):
        # MEASURED, and stated here rather than left to fall out of the host check, because
        # this is a shape the two halves of this system read DIFFERENTLY. `urlsplit` reads
        # `https:///cart/1:1` as having no host; the browser's WHATWG parser skips the extra
        # slash and resolves it to the host `cart` (`new URL('https:///cart/1:1').hostname
        # === 'cart'`, measured on this worktree's node). Both refuse it now, and both refuse
        # it for the same stated reason instead of agreeing by accident. A real cart
        # permalink never has an empty authority.
        return (
            REASON_UNSAFE,
            "the permalink's authority section is empty, and URL parsers disagree about "
            "where that points",
        )

    try:
        parts = urlsplit(text)
    except ValueError as exc:
        return REASON_UNSAFE, f"the permalink is not a parsable URL ({exc})"

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_PERMALINK_SCHEMES:
        # `urlsplit` accepts any `[A-Za-z][A-Za-z0-9+.-]*` as a scheme, and an EMPTY one is
        # what a protocol-relative `//evil.example/cart` produces — which is why the scheme
        # is checked against a set rather than merely inspected for `javascript`.
        return (
            REASON_UNSAFE,
            f"permalink scheme {parts.scheme!r} is not one a checkout can be redirected to "
            f"(allowed: {sorted(ALLOWED_PERMALINK_SCHEMES)})",
        )

    try:
        host = parts.hostname
    except ValueError as exc:
        return REASON_UNSAFE, f"the permalink has no parsable host ({exc})"
    if not host:
        return REASON_UNSAFE, "the permalink carries no host"

    if str(expected_domain or "").strip():
        expected = str(expected_domain).strip().lower().rstrip(".")
        actual = host.lower().rstrip(".")
        if actual != expected:
            return (
                REASON_OFF_DOMAIN,
                f"permalink host {actual!r} is not the store domain the buyer was shown "
                f"({expected!r}) — hosts are compared by exact equality (D22/C10)",
            )
    return None


def reason_unsafe(url: object, expected_domain: str = "") -> str | None:
    """Why ``url`` may not be followed, or ``None`` when it may be. Cannot raise.

    ``expected_domain`` empty means "no host constraint" — the scheme, host-presence,
    whitespace and control-character rules still apply.
    """
    verdict = _judge(url, expected_domain)
    return None if verdict is None else verdict[1]


def permalink_host(url: object) -> str:
    """The host a browser would connect to for ``url``, or ``""``. Cannot raise."""
    try:
        return (urlsplit(_as_text(url)).hostname or "").lower()
    except ValueError:
        return ""


def verify_permalink(url: object, expected_domain: str = "", *, what: str = "permalink") -> str:
    """Return ``url`` **unchanged** as a ``str``, or refuse.

    Unchanged is the contract, not an implementation detail: R3 is satisfied only if what
    the buyer follows is byte-for-byte what the exchange returned, so this function
    normalises nothing — it decides, and the value it hands back is the value it was given.

    Raises:
        NoPermalinkReturned: there is no URL here at all.
        OffDomainPermalink: the host is not ``expected_domain``.
        UnsafePermalink: every other refusal (scheme, missing host, control characters).
    """
    text = _as_text(url)
    verdict = _judge(url, expected_domain)
    if verdict is None:
        return text
    code, reason = verdict
    if code == REASON_ABSENT:
        raise NoPermalinkReturned(
            f"{what}: {reason}. R3 forbids the buyer minting a checkout URL of its own, so "
            f"there is nowhere to send this buyer."
        )
    if code == REASON_OFF_DOMAIN:
        raise OffDomainPermalink(f"{what}: {reason} (url={text!r})")
    raise UnsafePermalink(f"{what}: {reason} (url={text!r})")
