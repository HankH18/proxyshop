"""The URL is seller-controlled input, and this file is the refusal surface for it.

Every case here is a URL a hostile store could put on a bid. The platform fetches it
unattended, so the interesting assertion in almost every test is that **nothing was fetched**
— and, in the served cases, that the refusal is recorded with a sentence rather than swallowed.

The rule: a page is fetched only when its host is the domain the PLATFORM registered for that
store, or a proper subdomain of it. Not the ``store_domain`` on the slot, which the store
wrote; ``exchange/checkout/sellers.py`` records what happened the last time a store supplied
both halves of its own check.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from buyer_svc.livecheck import (
    LiveCheckTarget,
    NoProductPages,
    NoRegisteredDomains,
    StaticProductPages,
    StaticRegisteredDomains,
    TargetRefused,
    domain_matches,
    normalise_domain,
    target_for,
    usable_page_url,
)
from buyer_svc.main import create_app
from fastapi.testclient import TestClient

from apps.buyer.svc.tests._fixtures_livecheck import (
    CORPUS,
    STORE_DOMAIN,
    STORE_ID,
    page_url,
    shortlist_body,
)

DOMAINS = StaticRegisteredDomains({STORE_ID: STORE_DOMAIN})


# ==============================================================================================
# Host matching
# ==============================================================================================


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("www.gaiaherbs.com", id="exact"),
        pytest.param("WWW.GaiaHerbs.COM", id="case"),
        pytest.param("www.gaiaherbs.com.", id="root-label-dot"),
        pytest.param("shop.www.gaiaherbs.com", id="proper-subdomain"),
    ],
)
def test_hosts_that_are_the_registered_domain(host: str) -> None:
    assert domain_matches(host, STORE_DOMAIN)


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("www.gaiaherbs.com.attacker.tld", id="suffix-append"),
        pytest.param("evilwww.gaiaherbs.com", id="prefix-glue"),
        pytest.param("gaiaherbs.com", id="parent-is-not-a-subdomain"),
        pytest.param("attacker.tld", id="unrelated"),
        pytest.param("", id="empty"),
    ],
)
def test_hosts_that_are_not(host: str) -> None:
    """Each of these defeats one of ``in`` / ``startswith`` / ``endswith``.

    ``www.gaiaherbs.com.attacker.tld`` starts with the registered domain and
    ``evilwww.gaiaherbs.com`` ends with it, which is why the rule is written out as
    ``host == allowed or host.endswith("." + allowed)`` and nothing else.
    """
    assert not domain_matches(host, STORE_DOMAIN)


def test_a_registered_domain_written_as_a_url_still_matches() -> None:
    """Operators write ``https://store.example.com/`` as often as the bare host.

    A registry entry that failed to match because of a scheme would refuse every check for a
    correctly-registered store, and a silent refusal is indistinguishable from a store nobody
    registered — which is the failure mode that hides.
    """
    assert normalise_domain("https://WWW.GaiaHerbs.com:443/collections/all") == "www.gaiaherbs.com"
    assert domain_matches("www.gaiaherbs.com", "https://www.gaiaherbs.com/")


# ==============================================================================================
# The URL a store may put on a bid
# ==============================================================================================


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param("http://www.gaiaherbs.com/p", "scheme", id="plaintext-http"),
        pytest.param("file:///etc/passwd", "scheme", id="file"),
        pytest.param("data:text/html,<b>", "scheme", id="data"),
        pytest.param(
            "https://www.gaiaherbs.com@attacker.tld/p", "userinfo", id="userinfo-lookalike"
        ),
        pytest.param("https://www.gaiaherbs.com:8080/p", "port", id="non-standard-port"),
        pytest.param("https://attacker.tld/p", "not the domain", id="off-domain"),
        pytest.param("https://169.254.169.254/latest/meta-data/", "not the domain", id="metadata"),
        pytest.param("https://localhost/p", "not the domain", id="loopback-by-name"),
        pytest.param("https://[/p", "does not parse", id="unparseable"),
        pytest.param("https://www.gaiaherbs.com/" + "a" * 3000, "characters", id="too-long"),
        pytest.param("", "no product page url", id="empty"),
    ],
)
def test_a_url_a_store_could_choose_is_refused_with_a_reason(url: str, expected: str) -> None:
    accepted, why = usable_page_url(url, STORE_DOMAIN)
    assert accepted == ""
    assert expected in why, why


def test_the_one_url_shape_that_is_accepted() -> None:
    url = "https://shop.www.gaiaherbs.com/products/reflux-relief?variant=1"
    accepted, why = usable_page_url(url, STORE_DOMAIN)
    assert accepted == url and why == ""


def test_a_store_the_platform_never_registered_can_name_no_url_at_all() -> None:
    """Fail closed on the DEPLOYMENT, not only on a lookup.

    ``NoRegisteredDomains`` is the default, and with it every URL — including one on a real
    storefront — is refused, because the platform has no basis for saying the host belongs to
    the store that named it.
    """
    accepted, why = usable_page_url(page_url("agrees.html"), None)
    assert accepted == "" and "no registered domain" in why


# ==============================================================================================
# Resolving a slot to a target
# ==============================================================================================


def _slot(**overrides: Any) -> dict[str, Any]:
    slot = dict(shortlist_body()["slots"][0])
    slot.update(overrides)
    return slot


def test_a_registered_store_with_a_url_on_its_own_domain_resolves() -> None:
    resolved = target_for(_slot(), auction_id="auc-1", registered_domains=DOMAINS)
    assert isinstance(resolved, LiveCheckTarget)
    assert resolved.url == page_url("agrees.html")
    assert resolved.asking_price == 29.97
    assert resolved.currency == "USD"


def test_the_stores_own_store_domain_field_is_never_consulted() -> None:
    """A store that rewrites ``store_domain`` to match its hostile url gets nothing.

    This is the exact shape ``exchange/checkout/sellers.py`` documents: a store that supplies
    both halves of its own check passes it. Here the second half comes from the registry and
    the slot's field is not read at all.
    """
    resolved = target_for(
        _slot(store_domain="attacker.tld", product_url="https://attacker.tld/p"),
        registered_domains=DOMAINS,
    )
    assert isinstance(resolved, TargetRefused)
    assert "not the domain the platform registered" in resolved.reason


def test_a_slot_with_no_product_is_refused_rather_than_guessed_at() -> None:
    resolved = target_for(_slot(product=None), registered_domains=DOMAINS)
    assert isinstance(resolved, TargetRefused)
    assert "names no product" in resolved.reason


def test_with_no_url_anywhere_the_platform_declines_rather_than_deriving_one() -> None:
    """``product_ref`` is ``prod_<hash>``, not a handle — a derived path is a 404 for everyone.

    Measured on this tree: ``ingest.adapters.mapping.product_id_for`` mints
    ``prod_<stable_id(...)>``. Guessing ``/products/{product_ref}`` would 404 for every store
    on every check, and under "absence is not guilt" a 404 is a silent no-verdict — a feature
    that looks wired and decides nothing, forever. So it declines, loudly.
    """
    slot = _slot()
    slot.pop("product_url")
    resolved = target_for(slot, registered_domains=DOMAINS, product_pages=NoProductPages())
    assert isinstance(resolved, TargetRefused)
    assert "names a live page" in resolved.reason


def test_the_platforms_own_page_table_supplies_the_url_when_the_bid_does_not() -> None:
    slot = _slot()
    slot.pop("product_url")
    pages = StaticProductPages({STORE_ID: {"prod_gaia_reflux": page_url("agrees.html")}})
    resolved = target_for(slot, registered_domains=DOMAINS, product_pages=pages)
    assert isinstance(resolved, LiveCheckTarget)
    assert resolved.url == page_url("agrees.html")


def test_even_the_platforms_own_table_is_checked_against_the_registry() -> None:
    """A transcription mistake in a deployment document must not become an SSRF.

    ``StaticProductPages`` is written by an operator, and an operator can paste the wrong
    host. The registry check is applied to the platform's own table for the same reason it is
    applied to the store's url.
    """
    slot = _slot()
    slot.pop("product_url")
    pages = StaticProductPages({f"{STORE_ID}/prod_gaia_reflux": "https://169.254.169.254/"})
    resolved = target_for(slot, registered_domains=DOMAINS, product_pages=pages)
    assert isinstance(resolved, TargetRefused)
    assert "not the domain the platform registered" in resolved.reason


def test_a_registry_that_raises_refuses_rather_than_escaping() -> None:
    class Exploding:
        def domain_for(self, store_id: str) -> str:
            raise RuntimeError("registry is down")

    resolved = target_for(_slot(), registered_domains=Exploding())
    assert isinstance(resolved, TargetRefused)
    assert "could not be consulted" in resolved.reason


def test_the_default_registry_refuses_everybody() -> None:
    assert NoRegisteredDomains().domain_for(STORE_ID) is None
    resolved = target_for(_slot(), registered_domains=NoRegisteredDomains())
    assert isinstance(resolved, TargetRefused)


# ==============================================================================================
# Served: a hostile url reaches the fetcher zero times
# ==============================================================================================


@pytest.mark.parametrize(
    "hostile",
    [
        "https://attacker.tld/products/x",
        "https://169.254.169.254/latest/meta-data/",
        "http://www.gaiaherbs.com/products/x",
        "https://www.gaiaherbs.com@attacker.tld/products/x",
    ],
)
def test_a_hostile_url_on_a_bid_is_never_fetched_by_the_served_route(
    monkeypatch: pytest.MonkeyPatch, hostile: str
) -> None:
    monkeypatch.setenv(
        "BUYER_DEPLOYMENT_JSON",
        json.dumps(
            {
                "exchange_url": "http://exchange.invalid",
                "registered_domains": {STORE_ID: STORE_DOMAIN},
                "live_page_fetcher": f"recorded:{CORPUS}",
            }
        ),
    )
    client = TestClient(create_app())
    assert client.get("/buyer/livecheck/none").status_code == 200
    fetcher = client.app.state.live_page_fetcher

    body = shortlist_body(product_url=hostile)
    assert client.post("/buyer/shortlist/render", json={"shortlist": body}).status_code == 200
    ran = client.post("/buyer/livecheck/run")
    assert ran.status_code == 200
    assert ran.json()["checked"] == 0
    assert fetcher.requested == []

    served = client.get("/buyer/livecheck/auc-live-1").json()
    assert served["records"] == []
    assert len(served["refused"]) == 1
    assert "url from the store's own bid" in served["refused"][0]["reason"]
