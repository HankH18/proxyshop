"""The guarded HTTP transport the signed fetcher runs on (T-020, SPEC C10).

Everything the crawler puts on the wire goes through :class:`SafeHTTPClient`, and the class
exists because the ordinary way to write this — hand a URL to a high-level HTTP client and
let it follow redirects — cannot be made safe from the outside. Four things have to happen
*inside* the request loop:

**1. Connect to the address the guard actually judged.**
:func:`~ingest.adapters.netguard.fetch_verdict` resolves a name and vets every address it
resolved to. If the transport then hands the *name* to the socket layer, the name gets
resolved a second time and the second answer is the one that gets connected to — a DNS
rebinding window (a TOCTOU bug) that a short TTL turns into a reliable exploit. So this
transport never resolves anything. It takes the addresses off the verdict and pins one onto
the connection, keeping the hostname for the ``Host`` header and for TLS SNI and certificate
validation, so the connection is both correct and provably to a vetted address.

**2. Walk redirects by hand.** Auto-following is the single most common SSRF hole: the guard
approves ``https://store.example.com/products.json`` and the client cheerfully follows a
``302`` to ``http://169.254.169.254/latest/meta-data/``. Here every hop is a fresh
:func:`fetch_verdict` plus a fresh allow-list check plus a charge against the budget, and a
repeated URL ends the chain rather than looping.

**3. Meter the body while reading it, never from its headers.** See
:mod:`ingest.adapters.budgets`.

**4. Keep credentials on the host that issued them.** The storefront-password cookie is
bound to the host that set it and is not replayed across a redirect to anywhere else.

Requests are also *signed* when a key is configured: an HMAC over canonical request bytes,
alongside the identified User-Agent, so a cooperating merchant can verify a crawl really
came from us rather than from something wearing our agent string.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ssl
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlunsplit

from .budgets import BudgetExceeded, CrawlLedger
from .hashing import content_hash, snapshot_ref
from .netguard import (
    FetchPolicy,
    FetchRefused,
    IPAddress,
    fetch_verdict,
    host_matches_allowlist,
    normalise_host,
    safe_split,
)
from .robots import USER_AGENT

__all__ = [
    "REDIRECT_STATUSES",
    "HTTPResult",
    "RequestSigner",
    "SafeHTTPClient",
    "TransportError",
]

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CHUNK = 64 * 1024


class TransportError(RuntimeError):
    """A request could not be completed for a reason that is not a refusal or a budget."""


@dataclass(frozen=True)
class HTTPResult:
    """One completed request, body already read and metered."""

    url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    redirect_chain: tuple[str, ...] = ()
    content_hash: str = ""
    snapshot_ref: str = ""
    bytes_downloaded: int = 0
    connected_to: str = ""

    @property
    def media_type(self) -> str:
        return str(self.headers.get("content-type", "")).split(";", 1)[0].strip().lower()

    def text(self, fallback: str = "utf-8") -> str:
        """Body as text, decoded per the response charset, never raising on bad bytes."""
        charset = fallback
        raw = str(self.headers.get("content-type", ""))
        if "charset=" in raw:
            charset = raw.split("charset=", 1)[1].split(";", 1)[0].strip().strip('"') or fallback
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode(fallback, errors="replace")


@dataclass(frozen=True)
class RequestSigner:
    """HMAC signer for outbound crawl requests — the "signed" in *signed fetch*.

    Signs the canonical bytes ``method\\nhost\\npath\\nissued_at\\nnonce\\nsha256(body)``.
    The host and path are inside the signature specifically so a captured signature cannot
    be replayed against a different endpoint, and the nonce and timestamp are inside it so
    it cannot be replayed at all beyond the merchant's freshness window.
    """

    signer_id: str
    secret: bytes
    key_id: str = "k1"
    header_prefix: str = "X-ProxyShop"

    def canonical_bytes(
        self, method: str, host: str, path: str, issued_at: str, nonce: str, body: bytes
    ) -> bytes:
        digest = hashlib.sha256(body or b"").hexdigest()
        return "\n".join([method.upper(), host.lower(), path, issued_at, nonce, digest]).encode(
            "utf-8"
        )

    def headers(
        self, method: str, host: str, path: str, issued_at: str, nonce: str, body: bytes
    ) -> dict[str, str]:
        signature = hmac.new(
            self.secret,
            self.canonical_bytes(method, host, path, issued_at, nonce, body),
            hashlib.sha256,
        ).hexdigest()
        p = self.header_prefix
        return {
            f"{p}-Signer": self.signer_id,
            f"{p}-Key-Id": self.key_id,
            f"{p}-Issued-At": issued_at,
            f"{p}-Nonce": nonce,
            f"{p}-Signature": signature,
        }


@dataclass
class _CookieJar:
    """Cookies, bound to the host that set them. Deliberately not a general cookie store."""

    by_host: dict[str, dict[str, str]] = field(default_factory=dict)

    def store(self, host: str, set_cookie_headers: Sequence[str]) -> None:
        jar = self.by_host.setdefault(host.lower(), {})
        for raw in set_cookie_headers:
            pair = raw.split(";", 1)[0].strip()
            if "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            name = name.strip()
            if name:
                jar[name] = value.strip()

    def header_for(self, host: str) -> str:
        jar = self.by_host.get(host.lower(), {})
        return "; ".join(f"{k}={v}" for k, v in jar.items())


class SafeHTTPClient:
    """An HTTP client that will only ever reach vetted addresses on allow-listed hosts.

    Args:
        policy: SSRF posture. Defaults to public-only.
        user_agent: the identified agent string sent on every request.
        signer: optional request signer.
        clock: returns ``(issued_at_iso, nonce)`` for signing; injectable for determinism.
        ssl_context: TLS context; defaults to the platform trust store with verification on.
    """

    def __init__(
        self,
        *,
        policy: FetchPolicy | None = None,
        user_agent: str = USER_AGENT,
        signer: RequestSigner | None = None,
        clock=None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.policy = policy or FetchPolicy()
        self.user_agent = user_agent
        self.signer = signer
        self._clock = clock
        self._ssl_context = ssl_context
        self.cookies = _CookieJar()

    # -- connection ----------------------------------------------------------------------

    def _open(self, host: str, port: int, scheme: str, address: IPAddress, timeout: float):
        """A connection to ``address`` that still believes it is talking to ``host``."""
        pinned = str(address)
        if scheme == "https":
            context = self._ssl_context or ssl.create_default_context()
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, port, timeout=timeout, context=context
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)

        original = conn._create_connection  # type: ignore[attr-defined]

        def _pinned(address_pair, connect_timeout=timeout, source_address=None):
            # Ignore the hostname http.client would resolve; use the vetted address.
            return original((pinned, address_pair[1]), connect_timeout, source_address)

        conn._create_connection = _pinned  # type: ignore[attr-defined]
        return conn, pinned

    # -- body ----------------------------------------------------------------------------

    @staticmethod
    def _decompressor(encoding: str):
        enc = (encoding or "").strip().lower()
        if enc in ("gzip", "x-gzip"):
            return zlib.decompressobj(16 + zlib.MAX_WBITS)
        if enc == "deflate":
            return zlib.decompressobj()
        return None

    def _read_body(self, response, ledger: CrawlLedger) -> tuple[bytes, int]:
        """Stream the body, metering wire bytes and inflated bytes as they arrive.

        Returns:
            ``(decoded_body, wire_bytes)``.

        Raises:
            BudgetExceeded: the response outgrew the wire, decompressed or time budget.
        """
        budget = ledger.budget
        declared = response.getheader("Content-Length")
        if declared is not None:
            try:
                if int(declared) > budget.max_response_bytes:
                    raise BudgetExceeded("response_bytes", budget.max_response_bytes, int(declared))
            except ValueError:
                pass  # a lying or absent length changes nothing: the meter below decides

        inflater = self._decompressor(response.getheader("Content-Encoding") or "")
        chunks: list[bytes] = []
        wire_total = 0
        out_total = 0

        # `read1` rather than `read`: `read(n)` blocks until it has all n bytes or hits EOF,
        # so against a server that dribbles 1 KiB at a time the time budget would not be
        # consulted again until the whole response had arrived — the socket timeout would
        # end up being the only real limit, and it resets on every packet. `read1` returns
        # whatever has arrived, which is what makes the per-chunk `check_time()` below an
        # actual deadline rather than decoration.
        read = getattr(response, "read1", None) or response.read

        while True:
            ledger.check_time()
            chunk = read(_CHUNK)
            if not chunk:
                break
            wire_total += len(chunk)
            ledger.charge_bytes(len(chunk), response_total=wire_total)

            if inflater is None:
                out_total += len(chunk)
                ledger.charge_decompressed(len(chunk), response_total=out_total)
                chunks.append(chunk)
                continue

            # Gzip bomb defence: never ask the inflater for more than one byte past the
            # ceiling, so the ceiling is enforced by *not materialising* the excess rather
            # than by measuring it after the fact.
            pending = chunk
            while pending:
                allowed = budget.max_decompressed_bytes - out_total + 1
                if allowed <= 0:
                    raise BudgetExceeded(
                        "decompressed_bytes", budget.max_decompressed_bytes, out_total
                    )
                try:
                    piece = inflater.decompress(pending, allowed)
                except zlib.error as exc:
                    raise TransportError(f"malformed Content-Encoding body: {exc}") from exc
                out_total += len(piece)
                ledger.charge_decompressed(len(piece), response_total=out_total)
                chunks.append(piece)
                tail = inflater.unconsumed_tail
                if not piece and tail == pending:
                    break  # no progress possible; stop rather than spin
                pending = tail

        return b"".join(chunks), wire_total

    # -- request -------------------------------------------------------------------------

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        allowed_hosts: Sequence[str] = (),
        ledger: CrawlLedger | None = None,
    ) -> HTTPResult:
        """Fetch ``url``, following redirects by hand under guard and budget.

        Args:
            url: absolute http(s) URL.
            method: HTTP method.
            body: request body; sent only on the first hop (a redirect drops it, per the
                method-rewrite rules below).
            headers: extra request headers.
            allowed_hosts: hosts the chain may touch. The starting host is added
                automatically; a redirect anywhere else is refused.
            ledger: the crawl's budget ledger. One is created if omitted.

        Raises:
            FetchRefused: a hop failed the SSRF guard or left the allow-list.
            BudgetExceeded: pages, time, bytes or redirects ran out.
            TransportError: the server spoke something that is not HTTP.
        """
        book = ledger or CrawlLedger()
        # The allow-list is derived from the URL's *text*, never from a resolution: the
        # loop below resolves each hop exactly once, and a second lookup of the same name
        # would be the DNS-rebinding window this transport exists to close. When the caller
        # names hosts explicitly that list is used as given — including for the first hop,
        # so "fetch this URL but only if it is on host X" is expressible.
        # `safe_split`, not `urlsplit`: a caller can hand this method a URL that will not
        # parse, and the refusal for one belongs to `fetch_verdict` below — which answers
        # `unparseable-url:…` — not to a ValueError out of the allow-list derivation.
        entry = safe_split(url)
        origin = normalise_host((entry.hostname if entry is not None else "") or "")
        allow = list(allowed_hosts) if allowed_hosts else [origin]

        current = url
        current_method = method.upper()
        current_body = body
        chain: list[str] = []
        seen: set[str] = set()

        while True:
            verdict = fetch_verdict(current, policy=self.policy).raise_if_refused()
            if not host_matches_allowlist(verdict.host, allow):
                raise FetchRefused(current, f"host-not-allow-listed:{verdict.host}")

            key = current.strip().lower()
            if key in seen:
                raise FetchRefused(current, "redirect-loop")
            seen.add(key)
            chain.append(current)
            book.charge_redirect(len(chain) - 1)
            book.charge_page()

            # `current` has just been through `fetch_verdict`, which refuses what will not
            # parse, so this cannot be None today. It is still not spelled `urlsplit`: the
            # invariant lives in another function, and a second bare parse in the fetch loop
            # is exactly the shape of the defect this method already carries once.
            split = safe_split(current)
            if split is None:  # pragma: no cover - fetch_verdict refuses these first
                raise FetchRefused(current, f"unparseable-url:{current!r}")
            path = urlunsplit(("", "", split.path or "/", split.query, ""))
            timeout = book.request_timeout()
            conn, pinned = self._open(
                verdict.host, verdict.port, verdict.scheme, verdict.addresses[0], timeout
            )

            try:
                request_headers = self._headers(
                    current_method, verdict.host, path, current_body, headers
                )
                try:
                    conn.request(current_method, path, body=current_body, headers=request_headers)
                    response = conn.getresponse()
                except (OSError, http.client.HTTPException) as exc:
                    raise TransportError(f"{current}: {exc}") from exc

                status = int(response.status)
                response_headers = {k.lower(): v for k, v in response.getheaders()}
                set_cookies = [v for k, v in response.getheaders() if k.lower() == "set-cookie"]
                if set_cookies:
                    self.cookies.store(verdict.host, set_cookies)

                if status in REDIRECT_STATUSES and response_headers.get("location"):
                    response.read()  # drain so the socket closes cleanly
                    location = response_headers["location"].strip()
                    # The redirect target is the ONE URL in a crawl the hostile party writes,
                    # and `urljoin` parses it with the same `urlsplit` that raises on
                    # `http://[`. Bare, that turns this guard's refusal into a ValueError
                    # escaping `fetch` — and `SignedFetchAdapter` catches only
                    # (FetchRefused, TransportError), so it escapes `fetch_catalog` too,
                    # whose contract is that it never raises for an ordinary crawl outcome.
                    # A target that will not parse is refused in the guard's own vocabulary,
                    # the same `unparseable-url:` reason `fetch_verdict` answers for an entry
                    # URL. A target that DOES parse joins exactly as before.
                    try:
                        target = urljoin(current, location)
                    except ValueError as exc:
                        raise FetchRefused(location, f"unparseable-url:{exc}") from exc
                    if status == 303 or (
                        status in (301, 302) and current_method not in ("GET", "HEAD")
                    ):
                        current_method, current_body = "GET", None
                    current = target
                    continue

                payload, wire = self._read_body(response, book)
            finally:
                conn.close()

            digest = content_hash(payload)
            return HTTPResult(
                url=chain[0],
                final_url=current,
                status=status,
                headers=response_headers,
                body=payload,
                redirect_chain=tuple(chain),
                content_hash=digest,
                snapshot_ref=snapshot_ref(current, digest),
                bytes_downloaded=wire,
                connected_to=pinned,
            )

    def _headers(
        self,
        method: str,
        host: str,
        path: str,
        body: bytes | None,
        extra: Mapping[str, str] | None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        }
        cookie = self.cookies.header_for(host)
        if cookie:
            headers["Cookie"] = cookie
        if body is not None:
            headers["Content-Length"] = str(len(body))
        headers.update(dict(extra or {}))
        if self.signer is not None:
            issued_at, nonce = self._stamp()
            headers.update(self.signer.headers(method, host, path, issued_at, nonce, body or b""))
        return headers

    def _stamp(self) -> tuple[str, str]:
        if self._clock is not None:
            return self._clock()
        import secrets
        from datetime import UTC, datetime

        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), secrets.token_hex(16)
