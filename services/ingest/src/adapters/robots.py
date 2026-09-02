"""Crawler identity and robots.txt obedience (T-020, SPEC C6).

C6 requires an *identified* bot. That is two obligations, not one:

* **Say who you are.** :data:`USER_AGENT` names the product, its version and a contact URL,
  and deliberately contains none of the tokens a browser sends. Impersonating a browser to
  slip past bot detection is the behaviour C6 forbids, so the acceptance suite greps the
  agent string for ``mozilla``/``applewebkit``/``chrome``/``safari``/``gecko``/``edg/``.
* **Obey what the site tells you.** :func:`may_fetch` answers robots.txt for a URL under
  our own product token, honouring the RFC 9309 rule that a group naming our token wins
  outright over the ``*`` group — even when the ``*`` group is more permissive.

Parsing is delegated to ``protego``, which is the parser Scrapy ships and implements RFC
9309 longest-match precedence, wildcards (``*``), end-anchors (``$``) and percent-decoding.
Hand-rolling that grammar is a well-known source of "the guard allowed a disallowed path"
bugs, and the dependency is already pinned in the root manifest.

Failure posture, which is where robots handling usually goes wrong:

============================  ==========================================================
robots.txt could not be…      what this module does
============================  ==========================================================
…parsed (garbage bytes)       refuse the fetch (:func:`may_fetch` returns ``False``)
…fetched, 4xx / 404           allow the crawl — RFC 9309 §2.3.1.3, "unavailable" = allow
…fetched, 5xx or transport    refuse the crawl — §2.3.1.4, "unreachable" = disallow all
============================  ==========================================================

The asymmetry is deliberate: a 404 is the site saying "no rules", a 503 is the site saying
nothing at all, and treating silence as consent is how a crawler ends up hammering a host
that was trying to shed load.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "PRODUCT_TOKEN",
    "USER_AGENT",
    "crawl_delay",
    "may_fetch",
    "robots_url",
    "robots_verdict_for_status",
    "user_agent_token",
]

#: How this crawler identifies itself. Product token, version, and a contact URL so an
#: operator who sees us in their logs can find out what we are and ask us to stop.
USER_AGENT: str = "ProxyShopBot/1.0 (+https://proxyshop.example/bot; ingest@proxyshop.example)"


def user_agent_token(user_agent: str) -> str:
    """The leading product token of a User-Agent string.

    ``"ProxyShopBot/1.0 (+https://…)"`` -> ``"ProxyShopBot"``. This is the name a site
    operator writes in a ``User-agent:`` line to address us specifically, so it is what
    robots group-matching is done against.
    """
    return re.split(r"[/\s]", str(user_agent).strip())[0]


#: Our token, as it should appear in a ``User-agent:`` line addressed to us.
PRODUCT_TOKEN: str = user_agent_token(USER_AGENT)


def robots_url(url: str) -> str:
    """The robots.txt that governs ``url``.

    Robots policy is per **origin** (scheme + host + port), not per path and not per
    registrable domain: ``https://shop.example.com/`` and ``https://example.com/`` have
    separate robots files, and so do the same host on ports 80 and 443.
    """
    split = urlsplit(str(url))
    return urlunsplit((split.scheme, split.netloc, "/robots.txt", "", ""))


def may_fetch(robots_txt: str, url: str, user_agent: str = USER_AGENT) -> bool:
    """True when ``robots_txt`` permits ``user_agent`` to fetch ``url``.

    Args:
        robots_txt: the literal body of the origin's robots.txt. Empty text means the file
            was present and empty, which permits everything.
        url: the absolute URL being considered.
        user_agent: the full agent string; its product token does the group matching.

    Returns:
        ``True`` to proceed, ``False`` to skip this URL.

    A robots file this cannot parse returns ``False``. Being wrongly polite costs us one
    page; being wrongly rude costs the relationship the whole adapter depends on.
    """
    text = robots_txt if isinstance(robots_txt, str) else ""
    if not text.strip():
        return True
    try:
        from protego import Protego

        parsed = Protego.parse(text)
        return bool(parsed.can_fetch(str(url), user_agent_token(user_agent)))
    except Exception:
        return False


def crawl_delay(robots_txt: str, user_agent: str = USER_AGENT) -> float | None:
    """The ``Crawl-delay`` our token is asked to honour, in seconds, if any."""
    if not (robots_txt or "").strip():
        return None
    try:
        from protego import Protego

        delay = Protego.parse(robots_txt).crawl_delay(user_agent_token(user_agent))
    except Exception:
        return None
    return None if delay is None else float(delay)


def robots_verdict_for_status(status: int | None) -> str:
    """Map a robots.txt fetch outcome to a crawl posture (RFC 9309 §2.3.1).

    Args:
        status: the HTTP status robots.txt answered with, or ``None`` if the request never
            produced a response (DNS failure, refused connection, timeout).

    Returns:
        ``"use"`` — parse the body and obey it.
        ``"allow-all"`` — the file is genuinely absent; crawl normally.
        ``"disallow-all"`` — the server is unwell or forbidding; do not crawl at all.
    """
    if status is None:
        return "disallow-all"
    if 200 <= status < 300:
        return "use"
    if status in (401, 403):
        return "disallow-all"
    if 400 <= status < 500:
        return "allow-all"
    return "disallow-all"
