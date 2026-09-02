"""A body size ceiling for the routes anybody on the internet can reach.

``POST /pixel/collect`` needs no credential at all and ``POST /webhooks/shopify/{topic}``
reads its body **before** the signature can be checked — the signature is computed over
those bytes, so there is no order in which authentication comes first. Both therefore used
to buffer an unbounded body into memory on an anonymous request, which is a denial of
service that costs the sender one connection.

The ceiling is enforced by *streaming*, not by trusting ``Content-Length``: a sender who
declares 10 bytes and writes 100 MB is exactly the sender this guards against. The declared
length is still checked first, because refusing before reading anything is cheaper for both
sides when the sender is honest.

:data:`MAX_REQUEST_BODY_BYTES` is far above anything real. A Shopify order webhook with a
hundred line items is tens of kilobytes; a web-pixel beacon is a few hundred bytes.
"""

from __future__ import annotations

from fastapi import Request

#: The ceiling, in bytes, for a body on an unauthenticated route.
MAX_REQUEST_BODY_BYTES = 1 << 20  # 1 MiB


class BodyTooLarge(ValueError):
    """The request body exceeded :data:`MAX_REQUEST_BODY_BYTES`."""


async def read_capped_body(request: Request, *, limit: int = MAX_REQUEST_BODY_BYTES) -> bytes:
    """The request body, or :class:`BodyTooLarge` before ``limit`` bytes are exceeded.

    Reads from the stream and abandons it the moment the running total passes ``limit``, so
    an oversized body is never fully buffered — which is the whole point; a check applied to
    ``await request.body()`` has already paid the cost it was meant to avoid.

    Raises:
        BodyTooLarge: the declared or actual length exceeds ``limit``.
    """
    # A malformed Content-Length is not itself a reason to refuse: the stream loop below
    # bounds the read regardless, and the server has already framed the request.
    declared: int | None
    try:
        declared = int(request.headers.get("content-length", ""))
    except ValueError:
        declared = None
    if declared is not None and declared > limit:
        raise BodyTooLarge(f"declared body of {declared} bytes exceeds {limit}")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise BodyTooLarge(f"body exceeded {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
