"""The OAuth half of the install: authorize, verify the callback, exchange for a token.

Two things here are load-bearing and easy to get subtly wrong.

**Offline, not online.** The authorize URL deliberately carries no ``grant_options[]``
parameter. That single omission is what makes Shopify mint an *offline* token — one that
belongs to the shop rather than to the staff member who clicked Install — and it is the
difference between an install that keeps reconciling orders at 03:00 and one that stops
working when a session expires. :func:`authorize_url` refuses to build a per-user URL at
all; there is no flag for it.

**The callback is signed, and the signature is the only thing that makes it trustworthy.**
Everything in the redirect Shopify sends back is attacker-controllable — including
``shop``, which is then used as an API host. :func:`verify_callback_hmac` recomputes the
digest over every parameter except ``hmac`` itself, sorted, and
:func:`read_callback` refuses a callback whose ``shop`` is not a bare ``.myshopify.com``
host even when the signature checks out.

**What is not proven offline.** ``services/shopify-stub`` implements no ``/admin/oauth/*``
route — the stub is the Admin API surface, not the authorization server — so
:func:`exchange_code` is exercised against a transport double that asserts the request
shape, and never against a real token endpoint. SPEC A4 already says genuine Shopify
conformance is unprovable here; this is one of the places that bites.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

import httpx
from merchant_svc.install.scopes import REQUIRED_SCOPES, assert_scopes_allowed
from merchant_svc.install.shop import InvalidShopDomain, normalize_shop_domain
from merchant_svc.install.signatures import secure_equals, signature_bytes

AUTHORIZE_PATH = "/admin/oauth/authorize"
ACCESS_TOKEN_PATH = "/admin/oauth/access_token"

#: Parameters excluded from the callback HMAC computation. ``signature`` is the legacy
#: app-proxy digest and is excluded by the same Shopify rule that excludes ``hmac``.
_HMAC_EXCLUDED = frozenset({"hmac", "signature"})


class OAuthCallbackRejected(ValueError):
    """The install callback did not survive verification and is not being acted on."""


@dataclass(frozen=True)
class OAuthCallback:
    """A verified install callback."""

    shop_domain: str
    code: str
    state: str = ""


def new_state() -> str:
    """A fresh anti-forgery ``state`` nonce for an authorize redirect."""
    return secrets.token_urlsafe(24)


#: How long an issued install state stays redeemable. Long enough for a merchant to read
#: Shopify's grant screen, short enough that a leaked authorize URL is not a standing
#: install ticket.
INSTALL_STATE_TTL_SECONDS = 600

#: Version tag on the signed state, so a future format change is a clean refusal of the old
#: one rather than a silent misparse.
_STATE_VERSION = "s1"

#: How far ahead of this process another replica's clock may be before its states are
#: refused. Removing the pending-state map was justified by making the flow multi-replica
#: (see :func:`issue_install_state`); rejecting every state whose issue time is one second
#: in the future would have handed that back, since two hosts are never exactly in step.
INSTALL_STATE_CLOCK_SKEW_SECONDS = 120


class InstallStateRejected(OAuthCallbackRejected):
    """The ``state`` on a callback was not one this app issued, or is no longer valid."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def issue_install_state(shop_domain: str, *, secret: str, now: datetime | None = None) -> str:
    """Mint a ``state`` that carries its own shop and issue time, signed by the app secret.

    The state used to be a bare nonce, with the shop it belonged to held in a process-local
    ``{state: shop}`` map bounded at 256 entries and evicted FIFO. ``GET /install`` is
    unauthenticated, so 256 anonymous requests flushed every merchant who was mid-install
    and answered their callback ``unknown-state`` — a denial of service costing nothing to
    mount. The same map also made the flow single-replica: an install started on one process
    could not be finished on another.

    Signing the state removes the shared slot rather than enlarging it. There is nothing to
    evict, nothing to synchronise between replicas, and the shop binding is now *verified*
    on the way back in rather than looked up.

    Raises:
        InvalidShopDomain: the shop is not a bare ``.myshopify.com`` host.
        ValueError: ``secret`` is blank — an unsigned state is not a state.
    """
    shop = normalize_shop_domain(shop_domain)
    if not secret:
        raise ValueError("an install state cannot be issued without the app's client secret")
    issued = int((now or datetime.now(UTC)).timestamp())
    body = _b64url(f"{_STATE_VERSION}:{issued}:{secrets.token_urlsafe(16)}:{shop}".encode())
    digest = hmac.new(signature_bytes(secret), signature_bytes(body), hashlib.sha256).digest()
    return f"{body}.{_b64url(digest)}"


def read_install_state(
    state: str,
    *,
    secret: str,
    now: datetime | None = None,
    ttl_seconds: int = INSTALL_STATE_TTL_SECONDS,
    clock_skew_seconds: int = INSTALL_STATE_CLOCK_SKEW_SECONDS,
) -> str:
    """Verify a ``state`` and return the shop it was issued for.

    Every failure is the same exception on purpose: a forged state, a tampered one, an
    expired one and a malformed one are all "not a state this app issued", and telling them
    apart is a signal handed to whoever is guessing.

    Raises:
        InstallStateRejected: the state is not one this app issued, or has expired.
    """
    if not secret:
        raise InstallStateRejected("no client secret is configured; no state can be verified")
    body, _, supplied = str(state).partition(".")
    if not body or not supplied:
        raise InstallStateRejected("the callback state is not in the issued format")
    expected = hmac.new(signature_bytes(secret), signature_bytes(body), hashlib.sha256).digest()
    if not secure_equals(_b64url(expected), supplied):
        raise InstallStateRejected("the callback state did not verify")
    try:
        version, issued_text, _nonce, shop = _unb64url(body).decode("utf-8").split(":", 3)
    except Exception as exc:  # noqa: BLE001 - an attacker-chosen input may not raise a 500
        # A signed state cannot get here with a body this app did not write, so this is
        # defensive rather than reachable; it stays a refusal because the alternative is a
        # 500 on an attacker-chosen input.
        raise InstallStateRejected("the callback state is unreadable") from exc
    if version != _STATE_VERSION:
        raise InstallStateRejected("the callback state is from an older format")
    try:
        issued = int(issued_text)
    except ValueError as exc:
        raise InstallStateRejected("the callback state carries no issue time") from exc
    age = int((now or datetime.now(UTC)).timestamp()) - issued
    if age > ttl_seconds:
        raise InstallStateRejected(f"the callback state expired {age}s after it was issued")
    if age < -clock_skew_seconds:
        # Bounded, not unbounded: a state dated far in the future is not a clock, it is
        # somebody trying to mint one that never expires.
        raise InstallStateRejected(
            f"the callback state is dated {-age}s in the future, beyond the "
            f"{clock_skew_seconds}s skew allowance"
        )
    try:
        return normalize_shop_domain(shop)
    except InvalidShopDomain as exc:
        raise InstallStateRejected("the callback state names an unusable shop") from exc


def authorize_url(
    shop_domain: str,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: Iterable[str] = REQUIRED_SCOPES,
) -> str:
    """Build the Shopify authorize URL that starts an **offline**-token install.

    Raises:
        InvalidShopDomain: the shop is not a bare ``.myshopify.com`` host.
        ProtectedScopeRequested: a scope C5 forbids was requested.
        ValueError: ``client_id``, ``redirect_uri`` or ``state`` is blank.
    """
    shop = normalize_shop_domain(shop_domain)
    granted = assert_scopes_allowed(scopes)
    for name, value in (
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("state", state),
    ):
        if not value:
            raise ValueError(f"{name} is required to build an authorize URL")
    query = urlencode(
        {
            "client_id": client_id,
            "scope": ",".join(granted),
            "redirect_uri": redirect_uri,
            "state": state,
        },
        quote_via=quote,
    )
    return f"https://{shop}{AUTHORIZE_PATH}?{query}"


def callback_signing_bytes(params: Mapping[str, str]) -> bytes:
    """The exact bytes Shopify's callback HMAC is computed over.

    Every parameter except ``hmac``/``signature``, sorted by name, joined as
    ``key=value`` with ``&``. No percent-encoding: Shopify signs the decoded values.
    """
    pairs = sorted(
        (str(key), str(value)) for key, value in params.items() if key not in _HMAC_EXCLUDED
    )
    # signature_bytes, not .encode("utf-8"): every key and value here came off the wire,
    # and one that cannot be encoded must produce a digest that fails to match, not a 500.
    return signature_bytes("&".join(f"{key}={value}" for key, value in pairs))


def verify_callback_hmac(params: Mapping[str, str], secret: str) -> bool:
    """Constant-time check of the callback's ``hmac`` parameter.

    The comparison goes through :func:`~merchant_svc.install.signatures.secure_equals`
    because ``hmac`` is a query parameter — fully attacker-chosen, and free to carry a
    character ``hmac.compare_digest`` refuses to compare as ``str``. A malformed signature
    is a refusal (``False``), never an exception the route answers with a 500.
    """
    supplied = params.get("hmac")
    if not supplied or not secret:
        return False
    expected = hmac.new(
        signature_bytes(secret), callback_signing_bytes(params), hashlib.sha256
    ).hexdigest()
    return secure_equals(expected, supplied)


def sign_callback(params: Mapping[str, str], secret: str) -> str:
    """The ``hmac`` value Shopify would attach to ``params``.

    Exported because a receiver and a signer that disagree about the canonical bytes is
    exactly the bug :func:`verify_callback_hmac` exists to catch, and one implementation of
    those bytes is the only way they cannot drift.
    """
    return hmac.new(
        signature_bytes(secret), callback_signing_bytes(params), hashlib.sha256
    ).hexdigest()


def read_callback(
    params: Mapping[str, str],
    *,
    secret: str,
    expected_state: str | None = None,
) -> OAuthCallback:
    """Verify an install callback and return its usable parts.

    Raises:
        OAuthCallbackRejected: bad signature, wrong/missing ``state``, missing ``code``,
            or a ``shop`` that is not a bare ``.myshopify.com`` host.
    """
    if not verify_callback_hmac(params, secret):
        raise OAuthCallbackRejected("the callback signature did not verify")
    if expected_state is not None and str(params.get("state", "")) != expected_state:
        raise OAuthCallbackRejected("the callback state does not match the one issued")
    code = str(params.get("code", ""))
    if not code:
        raise OAuthCallbackRejected("the callback carries no authorization code")
    try:
        shop = normalize_shop_domain(str(params.get("shop", "")))
    except InvalidShopDomain as exc:
        raise OAuthCallbackRejected(f"the callback names an unusable shop: {exc}") from exc
    return OAuthCallback(shop_domain=shop, code=code, state=str(params.get("state", "")))


def exchange_code(
    shop_domain: str,
    code: str,
    *,
    client_id: str,
    client_secret: str,
    base_url: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> str:
    """POST the authorization code to Shopify and return the offline access token.

    Args:
        shop_domain: the shop being installed on.
        code: the ``code`` parameter from the verified callback.
        client_id: the app's API key.
        client_secret: the app's API secret.
        base_url: origin override (the token endpoint is on the shop's own host).
        client: an ``httpx.Client`` to borrow.
        timeout: request timeout in seconds.

    Raises:
        OAuthCallbackRejected: the token endpoint refused, or answered without a token.
    """
    shop = normalize_shop_domain(shop_domain)
    origin = (base_url or f"https://{shop}").rstrip("/")
    owned = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        response = http.post(
            f"{origin}{ACCESS_TOKEN_PATH}",
            json={"client_id": client_id, "client_secret": client_secret, "code": code},
            headers={"Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise OAuthCallbackRejected(
                f"token exchange for {shop} failed with HTTP {response.status_code}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise OAuthCallbackRejected(
                f"token exchange for {shop} returned a non-JSON body"
            ) from exc
        token = body.get("access_token") if isinstance(body, Mapping) else None
        if not isinstance(token, str) or not token:
            raise OAuthCallbackRejected(f"token exchange for {shop} returned no access_token")
        return token
    finally:
        if owned:
            http.close()
