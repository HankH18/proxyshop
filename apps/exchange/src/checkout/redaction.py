"""Keeping a live discount out of prose (T-215) — the primitives, in one place.

**Why this module exists at all, and why it is not in ``provider.py`` where it was born.**
T-215 has now been fixed three times, and the first two fixes shared one shape: the
redaction ran at the *boundary* — ``OrphanedCheckoutCode.__init__`` — over a message that
had already been built somewhere else. A boundary redactor can only remove what it can
name, and it holds exactly two things: the code, and the URLs the exception carries as
values. Everything else in that message was assembled deep inside another module out of
merchant-controlled fields the boundary never sees.

That is how the same bug arrived three times:

===  ===================================  ====================================================
#    where the code was spelled           what defeated the fix in force at the time
===  ===================================  ====================================================
1    ``?discount=`` in the permalink      the boundary replaced three literal spellings of the
                                          code; the merchant lower-cased its own permalink and
                                          the code and the URL are independent fields
2    the permalink's path/query, any      the boundary's blacklist was case-sensitive and
     encoding                             encoding-blind; fixed by reducing the URL
                                          STRUCTURALLY (:func:`redact_url`) instead
3    the permalink's **host**             ``assert_on_domain`` builds a SECOND, independently
                                          normalised copy of the host (``host.lower()``) into
                                          its reason. The boundary replaces whole URL values,
                                          so it never saw that copy — and the copy is
                                          lower-cased, so the case-sensitive literal layer
                                          could not match it either.
===  ===================================  ====================================================

So the shape is not "the query", "the path" or "the host". **The shape is: prose built out
of a merchant-controlled value, anywhere, by anyone, after a code exists.** The durable fix
is therefore not a fourth blacklist at the boundary. It is that *the site that builds the
prose* renders every merchant-controlled value through :func:`safe_token` (or
:func:`redact_url` for a whole URL) at the moment it builds it, while it still knows which
value is merchant-controlled — a thing the boundary can never know.

These primitives live here, below both ``domain`` and ``provider``, so that any module that
formats a merchant value can reach them without an import cycle. ``provider`` re-exports
:func:`code_fingerprint`, :func:`redact_url` and :func:`redact_code` under their historical
names; nothing that imported them has to change.

**Everything here is TOTAL.** Not "should not raise" — cannot. The input is a string a
merchant chose, and pass 2 shipped a :func:`redact_url` that read ``urlsplit(...).port``
outside its own ``try``: a permalink like ``https://attacker.tld:notaport/x`` raised
``ValueError`` straight out of ``CheckoutProvider.checkout``. That did not merely leak (the
``ValueError``'s own text quotes the offending port, so a code spelled *there* was published
verbatim) — it destroyed T-202, because the exception that carried the orphaned code was
never constructed, so no ``code_created`` event was emitted and the live discount became
unrevocable. A sanitiser that raises on hostile input is worse than no sanitiser, and every
public function here is wrapped to fail **closed** rather than to fail.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from urllib.parse import quote, unquote, urlsplit

__all__ = [
    "code_fingerprint",
    "recoverable_spellings",
    "redact_code",
    "redact_url",
    "safe_token",
    "spells_code",
]

#: How many derived forms of one string :func:`spells_code` will explore before giving up.
#: A merchant can hand us kilobytes; the transformations below are near-idempotent so the
#: reachable set is tiny in practice, and this only bounds the pathological case.
_MAX_FORMS = 64


def code_fingerprint(code: str) -> str:
    """A stable, non-reversible handle for a discount code, safe to put in prose (T-215).

    A refusal message has to be debuggable — an operator reading a ``policy_event`` must be
    able to say *which* code was orphaned — and it must not be the place the code itself is
    published. The fingerprint is the join: the same code always produces the same handle, so
    the redacted prose and the ``code_created`` event that holds the real code can be
    matched to each other, while the handle alone buys an attacker nothing.

    Truncated to 12 hex characters deliberately: this is a correlation token within one
    ledger, not a commitment, and a full digest in every message is noise.
    """
    try:
        text = str(code or "")
    except Exception:  # pragma: no cover - a merchant object with a hostile __str__
        return "code:unknown"
    if not text:
        return "code:none"
    return "code:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


# ---------------------------------------------------------------------------------------
# The recovery model — what a reader of a string can turn it back INTO
# ---------------------------------------------------------------------------------------
def _percent_decoded(text: str) -> str | None:
    try:
        decoded = unquote(text)
    except Exception:  # pragma: no cover - unquote does not raise on str, but do not bet
        return None
    return decoded if decoded != text else None


def _plus_decoded(text: str) -> str | None:
    """``+`` is a space in ``application/x-www-form-urlencoded``, which a query string is."""
    return text.replace("+", " ") if "+" in text else None


def _unicode_unescaped(text: str) -> str | None:
    r"""``PSX`` is ``PSX`` to anything that reads the string as a literal.

    JSON logs, Python ``repr`` of a non-ASCII string, and a merchant that simply writes the
    escapes out all produce this form, and it is one ``json.loads``/``unicode_escape`` away
    from the code.
    """
    if "\\" not in text:
        return None
    try:
        decoded = text.encode("latin-1", "backslashreplace").decode("unicode_escape", "replace")
    except Exception:
        return None
    return decoded if decoded != text else None


#: An ACE ("punycode") hostname label as it appears inside a sentence: the prefix plus the
#: characters a DNS label may hold, stopping at the dot, quote or space that ends it.
_ACE_LABEL = re.compile(r"xn--[A-Za-z0-9-]+", re.IGNORECASE)


def _punycode_decoded(text: str) -> str | None:
    """Decode ``xn--`` labels — the one host encoding pass 2 named as an open residual.

    ``https://xn--<punycode>.attacker.tld`` is a hostname a registrar will sell and a browser
    will render as the Unicode string it encodes, so a code spelled there is recoverable by
    the ordinary act of looking at the URL bar. ``urlsplit().hostname`` hands back the ACE
    form, so a case-folded substring search over the raw host misses it.
    """
    if "xn--" not in text.casefold():
        return None

    def decode(match: re.Match[str]) -> str:
        try:
            return match.group(0)[4:].encode("ascii", "replace").decode("punycode")
        except Exception:
            return match.group(0)

    # Matched by REGEX rather than by splitting on ".": this runs over free prose, not over
    # a bare hostname, and `offer expires_at 'xn--psx--42-bkf.example.com' is neither…`
    # splits into tokens that no longer begin with the ACE prefix. Measured: a split-based
    # version of this passed its unit test and missed the code inside a refusal message.
    out = _ACE_LABEL.sub(decode, text)
    return out if out != text else None


_DECODERS = (_percent_decoded, _plus_decoded, _unicode_unescaped, _punycode_decoded)


def recoverable_spellings(text: str) -> list[str]:
    """Every form of ``text`` reachable by the decodings a reader can apply for free.

    Case-folding is applied to every member, so the result is what a *case-insensitive*
    reader sees. This repo's own Shopify stub redeems with
    ``candidate.strip().upper() == self.code.upper()``
    (``services/shopify-stub/src/codes.py``), so ``summer10-live`` in a log **is**
    ``SUMMER10-LIVE`` and nothing that compares bytes may be trusted here.

    Bounded by :data:`_MAX_FORMS`. Being wrong in the *narrow* direction is what shipped
    twice, so new decodings belong here rather than at a call site.
    """
    try:
        start = str(text)
    except Exception:  # pragma: no cover
        return []
    seen: dict[str, None] = {}
    frontier = [start]
    while frontier and len(seen) < _MAX_FORMS:
        candidate = frontier.pop()
        folded = candidate.casefold()
        if folded in seen:
            continue
        seen[folded] = None
        for decode in _DECODERS:
            try:
                nxt = decode(candidate)
            except Exception:  # pragma: no cover - the decoders guard themselves
                nxt = None
            if nxt is not None and nxt != candidate:
                frontier.append(nxt)
    return list(seen)


def spells_code(text: str, code: str) -> bool:
    """Whether a reader of ``text`` could recover something the merchant would redeem.

    **This is a detector used to fail CLOSED, not a scrubber**, and the distinction is the
    whole reason it is allowed to exist after two blacklists failed. A scrubber that misses
    a spelling *publishes* the code; a detector that misses one merely fails to suppress a
    value that was going to be published anyway, and every value it *does* flag is dropped
    whole rather than patched. It is only ever applied to a value the caller has already
    decided is merchant-controlled, and the alternative to applying it is publishing that
    value unexamined.

    The residual is stated rather than papered over: a Unicode-homoglyph spelling (Cyrillic
    ``Р`` for ``P``) is not normalised here and would survive. Closing that would need a
    confusables table; what closed the previously-stated punycode residual is
    :func:`_punycode_decoded`.
    """
    try:
        needle = str(code or "").casefold()
        if not needle:
            return False
        return any(needle in form for form in recoverable_spellings(text))
    except Exception:  # pragma: no cover - defence in depth; a detector must not raise
        return True  # unsure means REDACT


def safe_token(value: object, code: str, *, label: str = "value") -> str:
    """Render one merchant-controlled value into prose, or drop it if it could spell ``code``.

    This is the function every prose-building site is supposed to call, and calling it is
    what makes the fix structural rather than a fourth blacklist: the decision is taken
    where the value's provenance is known. ``assert_on_domain`` knows that
    ``urlsplit(url).hostname`` came off a merchant reply; ``OrphanedCheckoutCode`` staring at
    the finished sentence does not, and cannot.

    Returns the value unchanged when it cannot spell the code — the diagnostic is the point,
    and a refusal that names no host is useless to an operator — and a fingerprinted
    placeholder when it can.
    """
    try:
        raw = "" if value is None else str(value)
    except Exception:  # pragma: no cover
        return f"<unrenderable-{label}:{code_fingerprint(code)}>"
    if not raw:
        return raw
    try:
        if code and spells_code(raw, str(code)):
            return f"<redacted-{label}:{code_fingerprint(code)}>"
    except Exception:  # pragma: no cover - fail closed, never fail
        return f"<redacted-{label}:{code_fingerprint(code)}>"
    return raw


def _reduce_url(raw: str, code: str, handle: str) -> str:
    """The body of :func:`redact_url`. Every ``urlsplit`` accessor is guarded here."""
    parts = urlsplit(raw)
    # `.hostname` and `.port` are PROPERTIES that parse lazily, and `.port` raises
    # `ValueError` on a non-numeric or out-of-range port — `https://attacker.tld:notaport/`.
    # Pass 2 read it outside the guard. See this module's header for what that cost.
    host = parts.hostname
    if not host:
        # `javascript:PSX-…`, `data:…`, a bare fragment: no host means no diagnostic worth
        # keeping, and the whole opaque remainder is merchant-controlled. Drop it.
        return f"<hostless-url:{handle}>"
    if spells_code(host, code):
        return f"<redacted-host-url:{handle}>"
    try:
        port = parts.port
    except ValueError:
        # A port that is not a port is not a diagnostic — and it is merchant-controlled
        # free text, so `:PSX-LIVE01` would publish the code if it were kept. Drop it and
        # keep the host, which is the part an operator acts on.
        port = None
    scheme = safe_token(parts.scheme.lower(), code, label="scheme") or "?"
    suffix = f":{port}" if port else ""
    # No trailing slash and no space: the result must be re-reducible to ITSELF, so that
    # redacting an already-redacted message is a no-op rather than a slow corruption.
    return f"{scheme}://{host.lower()}{suffix}/<redacted:{handle}>"


def redact_url(url: str, code: str = "") -> str:
    """Reduce a URL to the only part of it that is a diagnostic: its scheme and host.

    **This is the structural half of the redaction and the reason T-215's first attempt was
    not a fix.** That attempt scrubbed three literal spellings of the code out of the
    message — ``raw``, ``quote(raw, safe="")``, ``quote(raw)``. But the code and the
    ``permalink_url`` are two INDEPENDENT fields off the merchant's reply
    (``providers.py`` validates the shape of neither), so nothing requires them to agree on
    spelling. A merchant that lower-cases its own permalink — ordinary CDN and
    link-building behaviour, and this repo's own Shopify stub matches codes
    case-insensitively on redemption (``services/shopify-stub/src/codes.py``) — defeats the
    blacklist with no hostile intent at all. Percent-encoding each character defeats it
    deliberately. Case-folding and percent-decoding the blacklist would only move the bar:
    the next encoding wins again.

    So the URL is not searched for the code. It is **parsed, and everything that could
    carry a code is discarded**: userinfo, path, query, fragment and a non-numeric port all
    go, because a discount can be spelled in any of them (``/discount/CODE``,
    ``?discount=CODE``, ``#CODE``, ``:CODE``) and the reduction must not depend on knowing
    which. What is kept is the scheme and the host — which is precisely the diagnostic the
    message exists to report, since the refusal *is* "this host is not the registered one".

    The retained host is the ONE merchant-controlled value that survives, so it is guarded
    by :func:`spells_code` and dropped when it is unsafe. A value that cannot be parsed at
    all is dropped whole. **This function never raises**; see the module header.
    """
    try:
        raw = "" if url is None else str(url)
    except Exception:  # pragma: no cover
        return f"<unreducible-url:{code_fingerprint(code)}>"
    if not raw:
        return raw
    handle = code_fingerprint(code) if code else "code:n/a"
    try:
        return _reduce_url(raw, str(code or ""), handle)
    except ValueError:
        return f"<unparsable-url:{handle}>"
    except Exception:
        # Fail CLOSED on anything unforeseen. The caller is holding a merchant's reply and
        # is one frame from a persisted event; there is no failure mode here that is better
        # than dropping the URL.
        return f"<unreducible-url:{handle}>"


def _redact_layers(text: str, raw: str, urls: Sequence[str]) -> str:
    """The body of :func:`redact_code`."""
    # Layer 1 — exact values, not guesses about spelling. Longest first for the same reason
    # as below: one URL may be a prefix of another.
    values: set[str] = set()
    for candidate in urls:
        try:
            values.add("" if candidate is None else str(candidate))
        except Exception:  # pragma: no cover
            continue
    for url in sorted({v for v in values if v}, key=len, reverse=True):
        reduced = redact_url(url, raw)
        if reduced == url:
            continue
        for spelling in (url, repr(url)[1:-1]):
            if spelling:
                text = text.replace(spelling, reduced)

    if not raw:
        return text

    # Layer 2 — the code spelled out in prose this package built.
    handle = f"<redacted {code_fingerprint(raw)}>"
    # Longest first — a shorter spelling that is a prefix of a longer one must not eat it —
    # and CASE-INSENSITIVE, which pass 2's version was not. `str.replace` could not match
    # `summer10-live` against a code of `SUMMER10-LIVE`, and this repo's own Shopify stub
    # redeems by `.upper()`, so the lower-cased spelling is the code. Regex alternation is
    # ordered left-to-right, so the sort below still does the longest-first work.
    spellings = [
        s for s in sorted({raw, quote(raw, safe=""), quote(raw)}, key=len, reverse=True) if s
    ]
    if not spellings:
        return text
    return re.sub("|".join(re.escape(s) for s in spellings), handle, text, flags=re.IGNORECASE)


def redact_code(message: str, code: str, *, urls: Sequence[str] = ()) -> str:
    """Strip a live discount out of ``message`` while leaving the diagnostic standing.

    Two layers, and the ORDER of importance is the opposite of the order they were written:

    1. **Structural (``urls``).** Every URL the caller *holds as a value* — the orphan's
       ``permalink_url``, the offer's ``checkout_url`` — is replaced by its
       :func:`redact_url` reduction. This cannot miss *for that value*, because it never
       asks how the URL spells the code: it replaces an exact string the caller already has
       with a form built only from that string's scheme and host. Both the raw spelling and
       the ``repr``-escaped spelling are replaced, since ``assert_on_domain`` embeds the URL
       as ``{url!r}`` and ``repr`` escapes backslashes and quotes.

    2. **Literal spellings of the code (the old behaviour, kept as a second layer).** The
       code also reaches a message spelled out on its own — ``the discount code 'PSX-…'
       was ALREADY minted`` — with no URL involved, and layer 1 says nothing about that.
       This layer is a blacklist and is only sound for the case it was written for: prose
       *this package* formats, where the spelling is ``orphan.code`` itself. It is
       case-insensitive since pass 3 — ``str.replace`` could not match ``summer10-live``
       against ``SUMMER10-LIVE`` — but it is still a blacklist and is not, and must never
       again be treated as, the defence against a merchant-controlled string.

    **What this function is NOT, and the correction pass 3 makes.** It is a boundary
    redactor, and a boundary redactor is structurally incapable of removing a merchant value
    it was not handed. Pass 2 leaned on it as the last line of defence and lost: the host
    that ``assert_on_domain`` normalises into its own reason is a *second copy*, not a
    substring of any URL this function holds, and it is lower-cased so layer 2 could not
    match it either. The defence is now at the sites that BUILD prose — see
    :func:`safe_token` — and these two layers are the backstop, not the plan.

    The replacement is unconditional — no minimum length, no word boundary. A merchant that
    answers with a one-character code would have that character scrubbed everywhere in the
    message, which is ugly; a message mangled by a pathological merchant is a strictly better
    outcome than a live discount published into the event stream, so the ugly case wins.

    **Never raises.** If the redaction machinery itself fails, the MESSAGE is dropped —
    returning it unredacted would be failing open, which is the one outcome worse than
    losing a diagnostic.
    """
    try:
        text = str(message)
        raw = str(code or "")
    except Exception:  # pragma: no cover
        return f"<unredactable-message:{code_fingerprint(code)}>"
    try:
        return _redact_layers(text, raw, tuple(urls or ()))
    except Exception:  # pragma: no cover - fail closed: drop the prose, keep the join
        return f"<unredactable-message:{code_fingerprint(raw)}>"
