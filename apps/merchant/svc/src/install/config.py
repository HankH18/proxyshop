"""Where the merchant app's identity and public URLs come from.

Nothing here is read at import time. Every value is resolved when it is *used*, so a test
that sets an environment variable after the module is imported still gets the value it
set, and two tests in one session cannot leak configuration into each other.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

#: The Admin API version this app pins. Matches the version ``services/shopify-stub``
#: serves; any other version is a 404 there, exactly as it is against Shopify.
DEFAULT_API_VERSION = "2026-07"

#: The app's own public origin, used to build the webhook callback URLs and the pixel's
#: collector URL. Deliberately a ``.example`` host: it is not resolvable, so a
#: misconfigured deployment fails loudly instead of quietly talking to somebody else.
DEFAULT_APP_URL = "https://merchant.proxyshop.example"

ENV_APP_URL = "MERCHANT_APP_URL"
ENV_API_KEY = "SHOPIFY_API_KEY"
ENV_API_SECRET = "SHOPIFY_API_SECRET"
ENV_API_VERSION = "SHOPIFY_API_VERSION"
#: Points the Admin client at the offline stub instead of ``https://<shop>`` (C9).
ENV_ADMIN_BASE_URL = "SHOPIFY_STUB_URL"

#: The path the web pixel beacons to, and the paths the order webhooks are delivered to.
COLLECTOR_PATH = "/pixel/collect"
WEBHOOK_PATH_PREFIX = "/webhooks/shopify"


@dataclass(frozen=True)
class AppConfig:
    """The merchant app's identity, as the OAuth and webhook code needs it."""

    app_url: str
    api_key: str
    api_secret: str
    api_version: str


def app_config() -> AppConfig:
    """Read :class:`AppConfig` from the environment, with documented defaults."""
    return AppConfig(
        app_url=(os.environ.get(ENV_APP_URL) or DEFAULT_APP_URL).rstrip("/"),
        api_key=os.environ.get(ENV_API_KEY, ""),
        api_secret=os.environ.get(ENV_API_SECRET, ""),
        api_version=os.environ.get(ENV_API_VERSION) or DEFAULT_API_VERSION,
    )


def admin_base_url(shop_domain: str) -> str:
    """The origin the Admin GraphQL client posts to for ``shop_domain``.

    ``SHOPIFY_STUB_URL`` wins when it is set, which is how every offline run (C9) reaches
    ``services/shopify-stub`` instead of a Shopify host that does not exist here.
    """
    override = os.environ.get(ENV_ADMIN_BASE_URL)
    if override:
        return override.rstrip("/")
    return f"https://{shop_domain}"


def collector_url(app_url: str | None = None) -> str:
    """The URL the web pixel POSTs checkout events to (T-051's collector)."""
    base = (app_url or app_config().app_url).rstrip("/")
    return f"{base}{COLLECTOR_PATH}"


def webhook_callback_url(topic: str, app_url: str | None = None) -> str:
    """The URL Shopify delivers ``topic`` to, e.g. ``…/webhooks/shopify/orders/paid``."""
    base = (app_url or app_config().app_url).rstrip("/")
    return f"{base}{WEBHOOK_PATH_PREFIX}/{topic}"


def web_pixel_settings(
    shop_domain: str,
    *,
    collector: str | None = None,
    app_url: str | None = None,
    api_version: str | None = None,
) -> dict[str, Any]:
    """The ``WebPixelInput.settings`` payload ``webPixelCreate`` is called with.

    The web pixel extension reads these at run time; they are the only configuration it
    gets. ``collectorUrl`` is the one that must be right — a pixel pointed at the wrong
    origin reports nothing and looks exactly like a pixel that was never installed.

    ``collector`` and ``app_url`` are two ways of naming that destination and are resolved
    in that order: ``collector`` is the whole URL and wins outright; otherwise ``app_url``
    is the origin :func:`collector_url` hangs :data:`COLLECTOR_PATH` off; otherwise the
    configured default. Neither is ever dropped. A caller-supplied destination that is
    silently ignored produces exactly the failure above — a pixel that installs cleanly,
    reports to somebody else's origin, and looks like a shop that simply never checks out.

    Deliberately absent: anything identifying a shopper. C5 grants this app no
    protected-customer-data scope and the collector rejects every PII field (T-051), so a
    setting that carried one would be authority the system has decided not to hold.
    """
    return {
        "collectorUrl": collector or collector_url(app_url),
        "shopDomain": shop_domain,
        "apiVersion": api_version or app_config().api_version,
    }
