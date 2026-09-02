"""T-050 — installing the ProxyShop app on a Shopify store.

Public surface, in the order an install uses it::

    from apps.merchant.svc.src.install import REQUIRED_SCOPES, authorize_url, read_callback
    from apps.merchant.svc.src.install import AdminGraphQLClient, install

    url    = authorize_url(shop, client_id=…, redirect_uri=…, state=…)   # offline token
    cb     = read_callback(request.query_params, secret=…)               # signed, or refused
    token  = exchange_code(cb.shop_domain, cb.code, client_id=…, client_secret=…)
    client = AdminGraphQLClient(shop_domain=cb.shop_domain, access_token=token)
    result = install(cb.shop_domain, client, access_token=token)

:func:`install` takes the Admin client as its **second positional argument** and never
builds one, so the same function serves production, the offline ``services/shopify-stub``
run, and the frozen acceptance suite's recording double.

The module namespace is ``merchant_svc.install`` (via ``.pkgroot``); the frozen suite
imports the same package by path as ``apps.merchant.svc.src.install``.
"""

from __future__ import annotations

from merchant_svc.install.admin import (
    ACCESS_TOKEN_HEADER,
    WEB_PIXEL_CREATE,
    WEBHOOK_SUBSCRIPTION_CREATE,
    AdminAPIError,
    AdminGraphQLClient,
)
from merchant_svc.install.config import (
    DEFAULT_API_VERSION,
    DEFAULT_APP_URL,
    AppConfig,
    admin_base_url,
    app_config,
    collector_url,
    web_pixel_settings,
    webhook_callback_url,
)
from merchant_svc.install.flow import (
    InstallFailed,
    InstallResult,
    WebhookRegistration,
    install,
)
from merchant_svc.install.oauth import (
    ACCESS_TOKEN_PATH,
    AUTHORIZE_PATH,
    OAuthCallback,
    OAuthCallbackRejected,
    authorize_url,
    callback_signing_bytes,
    exchange_code,
    new_state,
    read_callback,
    sign_callback,
    verify_callback_hmac,
)
from merchant_svc.install.scopes import (
    PROTECTED_CUSTOMER_DATA_SCOPES,
    REQUIRED_SCOPES,
    ProtectedScopeRequested,
    assert_scopes_allowed,
    normalize_scopes,
    unauthorized_scopes,
)
from merchant_svc.install.shop import (
    MYSHOPIFY_SUFFIX,
    InvalidShopDomain,
    is_shop_domain,
    normalize_shop_domain,
)
from merchant_svc.install.signatures import secure_equals, signature_bytes
from merchant_svc.install.tokens import (
    TOKENS,
    InMemoryOfflineTokenStore,
    OfflineToken,
    OfflineTokenStore,
    OnlineTokenRefused,
    ShopNotInstalled,
)
from merchant_svc.install.webhooks import (
    HEADER_HMAC,
    HEADER_SHOP_DOMAIN,
    HEADER_TOPIC,
    HEADER_WEBHOOK_ID,
    INBOX,
    INBOX_CAPACITY,
    SEEN_CAPACITY,
    TOPIC_ENUM,
    WEBHOOK_TOPICS,
    ForbiddenWebhookTopic,
    ReceivedWebhook,
    WebhookDecision,
    WebhookInbox,
    assert_topics_allowed,
    delivery_digest,
    handle_delivery,
    normalize_topic,
    set_webhook_sink,
    sign,
    verify,
)

__all__ = [
    "ACCESS_TOKEN_HEADER",
    "ACCESS_TOKEN_PATH",
    "AUTHORIZE_PATH",
    "DEFAULT_API_VERSION",
    "DEFAULT_APP_URL",
    "HEADER_HMAC",
    "HEADER_SHOP_DOMAIN",
    "HEADER_TOPIC",
    "HEADER_WEBHOOK_ID",
    "INBOX",
    "INBOX_CAPACITY",
    "MYSHOPIFY_SUFFIX",
    "PROTECTED_CUSTOMER_DATA_SCOPES",
    "REQUIRED_SCOPES",
    "SEEN_CAPACITY",
    "TOKENS",
    "TOPIC_ENUM",
    "WEBHOOK_SUBSCRIPTION_CREATE",
    "WEBHOOK_TOPICS",
    "WEB_PIXEL_CREATE",
    "AdminAPIError",
    "AdminGraphQLClient",
    "AppConfig",
    "ForbiddenWebhookTopic",
    "InMemoryOfflineTokenStore",
    "InstallFailed",
    "InstallResult",
    "InvalidShopDomain",
    "OAuthCallback",
    "OAuthCallbackRejected",
    "OfflineToken",
    "OfflineTokenStore",
    "OnlineTokenRefused",
    "ProtectedScopeRequested",
    "ReceivedWebhook",
    "ShopNotInstalled",
    "WebhookDecision",
    "WebhookInbox",
    "WebhookRegistration",
    "admin_base_url",
    "app_config",
    "assert_scopes_allowed",
    "assert_topics_allowed",
    "authorize_url",
    "callback_signing_bytes",
    "collector_url",
    "delivery_digest",
    "exchange_code",
    "handle_delivery",
    "install",
    "is_shop_domain",
    "new_state",
    "normalize_scopes",
    "normalize_shop_domain",
    "normalize_topic",
    "read_callback",
    "secure_equals",
    "set_webhook_sink",
    "sign",
    "sign_callback",
    "signature_bytes",
    "unauthorized_scopes",
    "verify",
    "verify_callback_hmac",
    "web_pixel_settings",
    "webhook_callback_url",
]
