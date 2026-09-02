"""Constant-time comparison of a computed digest against an attacker-supplied one.

:func:`hmac.compare_digest` accepts ``str`` arguments only when **both** are ASCII-only; it
raises ``TypeError`` otherwise. Every value this app compares a digest against arrives from
the network — an ``X-Shopify-Hmac-Sha256`` header, an ``hmac`` query parameter — so "both
sides are ASCII" is an assumption about the attacker, not a property of the code. One
non-ASCII byte in either turns "the signature did not verify" into an unhandled exception,
which the HTTP layer answers with a 500 rather than the hard 401 a bad signature must get.

So the comparison happens on **bytes**, where ``compare_digest`` has no such rule, and the
encoding cannot itself raise: ASGI hands header values back as latin-1 text and query
strings can carry replacement characters, so anything from ``U+0000`` to ``U+10FFFF``
(lone surrogates included) has to encode to *something* rather than blow up.

Length still leaks — it does in :func:`hmac.compare_digest` too — but the digest length is
public, and the content comparison is constant time.
"""

from __future__ import annotations

import hmac

__all__ = ["secure_equals", "signature_bytes"]


def signature_bytes(value: object) -> bytes:
    """The comparable bytes of a signature-shaped value. Never raises.

    ``surrogatepass`` is what makes that promise true: a lone surrogate is the one thing a
    plain ``utf-8`` encode refuses, and a value decoded from bytes by a lenient codec can
    contain one.
    """
    text = value if isinstance(value, str) else str(value)
    return text.encode("utf-8", "surrogatepass")


def secure_equals(expected: object, supplied: object) -> bool:
    """``True`` when the two digests match, compared in constant time.

    Args:
        expected: the digest this app computed.
        supplied: the digest that arrived from the network, in any encoding, of any length.

    Returns:
        Whether they are equal. A value that is not a valid digest at all — wrong length,
        wrong alphabet, non-ASCII bytes — is ``False``, never an exception.
    """
    return hmac.compare_digest(signature_bytes(expected), signature_bytes(supplied))
