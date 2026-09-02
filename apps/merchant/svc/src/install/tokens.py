"""Offline access tokens, one per shop.

Shopify issues two kinds of Admin API token and the difference is the whole reason this
module exists. An **online** token (``shpua_…``) is bound to the staff member who happened
to click *Install* and expires with their session; an **offline** token (``shpat_…``,
``shpca_…``) belongs to the shop and keeps working when nobody is logged in. Everything
this app does after the install — reconciling an ``orders/paid`` webhook, minting a
discount code for an offer accepted at 03:00 — happens with no human present, so an online
token would work in the install test and fail in production the first quiet night.

:meth:`InMemoryOfflineTokenStore.save` therefore refuses an online token outright rather
than storing one that will expire.

Durability is out of scope for T-050 and is stated rather than implied: this store lives in
the process. The seam is :class:`OfflineTokenStore`, so a Postgres-backed implementation
replaces it without touching a caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from merchant_svc.install.shop import normalize_shop_domain

#: Token prefix Shopify uses for a per-user (online) token. Anything with this prefix is
#: refused: it dies with the staff session that created it.
ONLINE_TOKEN_PREFIX = "shpua_"


class OnlineTokenRefused(ValueError):
    """An online (per-user) token was offered where an offline token is required."""


class ShopNotInstalled(LookupError):
    """No offline token is stored for this shop; the app is not installed on it."""


@dataclass(frozen=True)
class OfflineToken:
    """One shop's offline Admin API credential."""

    shop_domain: str
    access_token: str
    scopes: tuple[str, ...]
    issued_at: datetime

    def redacted(self) -> str:
        """The token with its secret elided, safe to log."""
        head = self.access_token[:6]
        return f"{head}…({len(self.access_token)} chars)"


@runtime_checkable
class OfflineTokenStore(Protocol):
    """The seam a durable implementation replaces."""

    def save(
        self,
        shop_domain: str,
        access_token: str,
        *,
        scopes: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> OfflineToken: ...

    def get(self, shop_domain: str) -> OfflineToken | None: ...

    def require(self, shop_domain: str) -> OfflineToken: ...

    def forget(self, shop_domain: str) -> bool: ...

    def shops(self) -> tuple[str, ...]: ...


class InMemoryOfflineTokenStore:
    """Process-local ``{shop_domain: OfflineToken}``.

    Keyed by the *normalized* shop domain, so ``Acceptance-Store.myshopify.com`` and
    ``acceptance-store.myshopify.com`` are one shop rather than two half-installed ones.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, OfflineToken] = {}

    def save(
        self,
        shop_domain: str,
        access_token: str,
        *,
        scopes: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> OfflineToken:
        """Store ``access_token`` for ``shop_domain``, replacing any earlier one.

        Raises:
            InvalidShopDomain: the shop is not a bare ``.myshopify.com`` host.
            OnlineTokenRefused: the token is a per-user token.
            ValueError: the token is blank.
        """
        shop = normalize_shop_domain(shop_domain)
        if not isinstance(access_token, str) or not access_token.strip():
            raise ValueError(f"refusing to store a blank access token for {shop}")
        token = access_token.strip()
        if token.startswith(ONLINE_TOKEN_PREFIX):
            raise OnlineTokenRefused(
                f"{shop} was offered a per-user (online) token; this app needs an offline "
                f"token, because every Admin call it makes happens with no staff session"
            )
        record = OfflineToken(
            shop_domain=shop,
            access_token=token,
            scopes=tuple(scopes),
            issued_at=now or datetime.now(UTC),
        )
        self._tokens[shop] = record
        return record

    def get(self, shop_domain: str) -> OfflineToken | None:
        """The stored token for a shop, or ``None``."""
        try:
            shop = normalize_shop_domain(shop_domain)
        except ValueError:
            return None
        return self._tokens.get(shop)

    def require(self, shop_domain: str) -> OfflineToken:
        """The stored token for a shop.

        Raises:
            ShopNotInstalled: nothing is stored for it.
        """
        token = self.get(shop_domain)
        if token is None:
            raise ShopNotInstalled(f"the app is not installed on {shop_domain!r}")
        return token

    def forget(self, shop_domain: str) -> bool:
        """Drop a shop's token (app uninstalled). ``True`` when one was there."""
        token = self.get(shop_domain)
        if token is None:
            return False
        del self._tokens[token.shop_domain]
        return True

    def shops(self) -> tuple[str, ...]:
        """Every shop with a stored offline token, sorted."""
        return tuple(sorted(self._tokens))


#: The process-wide default store. A caller may inject its own into
#: :func:`~merchant_svc.install.flow.install`; this one is what the HTTP routes use.
TOKENS = InMemoryOfflineTokenStore()
