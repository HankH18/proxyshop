"""The cart permalink: the pinned template, and the exact-host rule behind blocker S8-3.

The hostile hosts below are not invented for this file — they are the five the frozen
release-blocker test uses, reproduced here so the stub's parser is held to the same bar as
the exchange validator that will consume it. Each defeats a different lazy matcher:

============================================ ====================================
Host                                         Defeats
============================================ ====================================
``rival.example.com``                        nothing — the base case
``evil-store-a.example.com.attacker.tld``    ``in`` / substring
``evil-store-a.example.com``                 ``endswith``
``store-a.example.com@attacker.tld``         ``startswith`` on the raw URL
``checkout.store-a.example.com``             ``endswith``, and any wildcard rule
============================================ ====================================
"""

from __future__ import annotations

import pytest
from shopify_stub.permalink import (
    PERMALINK_TEMPLATE,
    CartPermalink,
    PermalinkError,
    build_permalink,
    host_matches,
    parse_permalink,
)

SELLER_DOMAIN = "store-a.example.com"
RIVAL_DOMAIN = "rival.example.com"
CODE = "PSX-ABCDEFGH"

HOSTILE_URLS = {
    "plain other domain": f"https://{RIVAL_DOMAIN}/cart/1:1?discount={CODE}",
    "suffix spoof": f"https://evil-{SELLER_DOMAIN}.attacker.tld/cart/1:1?discount={CODE}",
    "glued suffix spoof": f"https://evil-{SELLER_DOMAIN}/cart/1:1?discount={CODE}",
    "userinfo spoof": f"https://{SELLER_DOMAIN}@attacker.tld/cart/1:1?discount={CODE}",
    "subdomain": f"https://checkout.{SELLER_DOMAIN}/cart/1:1?discount={CODE}",
}


def test_template_is_the_pinned_one() -> None:
    """The literal D22 fixes, so a drift shows up here and not in three tickets at once."""
    assert PERMALINK_TEMPLATE == (
        "https://{shop_domain}/cart/{variant_id}:{quantity}?discount={code}"
    )


def test_build_renders_the_template() -> None:
    assert build_permalink(shop_domain=SELLER_DOMAIN, variant_id=44352913, code=CODE) == (
        f"https://{SELLER_DOMAIN}/cart/44352913:1?discount={CODE}"
    )
    assert build_permalink(shop_domain=SELLER_DOMAIN, variant_id="7", quantity=3) == (
        f"https://{SELLER_DOMAIN}/cart/7:3"
    )


def test_build_rejects_a_shop_domain_that_is_not_a_bare_host() -> None:
    """A scheme or path in ``shop_domain`` would produce a URL with two schemes in it."""
    with pytest.raises(PermalinkError):
        build_permalink(shop_domain=f"https://{SELLER_DOMAIN}", variant_id=1)
    with pytest.raises(PermalinkError):
        build_permalink(shop_domain=f"{SELLER_DOMAIN}/cart", variant_id=1)
    with pytest.raises(PermalinkError):
        build_permalink(shop_domain="", variant_id=1)


def test_build_rejects_bad_variants_and_quantities() -> None:
    with pytest.raises(PermalinkError):
        build_permalink(shop_domain=SELLER_DOMAIN, variant_id="abc")
    with pytest.raises(PermalinkError):
        build_permalink(shop_domain=SELLER_DOMAIN, variant_id=1, quantity=0)


def test_parse_round_trips() -> None:
    url = build_permalink(shop_domain=SELLER_DOMAIN, variant_id=44352913, quantity=2, code=CODE)
    parsed = parse_permalink(url)
    assert parsed == CartPermalink(
        shop_domain=SELLER_DOMAIN, variant_id="44352913", quantity=2, code=CODE
    )
    assert parsed.to_url() == url


def test_parse_accepts_a_permalink_with_no_discount() -> None:
    """The ``discount`` parameter is optional; its absence is not an error."""
    assert parse_permalink(f"https://{SELLER_DOMAIN}/cart/1:1").code is None


@pytest.mark.parametrize(
    "bad",
    [
        f"http://{SELLER_DOMAIN}/cart/1:1",  # not https
        f"https://{SELLER_DOMAIN}/checkout/1:1",  # wrong path
        f"https://{SELLER_DOMAIN}/cart/1:1/extra",  # extra segment
        f"https://{SELLER_DOMAIN}/cart/11",  # no quantity separator
        f"https://{SELLER_DOMAIN}/cart/abc:1",  # non-numeric variant
        f"https://{SELLER_DOMAIN}/cart/1:x",  # non-numeric quantity
        f"https://{SELLER_DOMAIN}/cart/1:0",  # zero quantity
        f"https://{SELLER_DOMAIN}/cart/1:1?discount=A&discount=B",  # two codes
        "https:///cart/1:1",  # no host
    ],
)
def test_parse_rejects_structurally_broken_links(bad: str) -> None:
    """Structural breakage raises. That is *different* from an invalid discount code.

    An invalid code parses fine and is then silently ignored at redemption (acceptance 2);
    a malformed URL is a bug in whoever built the link and must be loud.
    """
    with pytest.raises(PermalinkError):
        parse_permalink(bad)


def test_host_matches_the_registered_domain_exactly() -> None:
    url = build_permalink(shop_domain=SELLER_DOMAIN, variant_id=1, code=CODE)
    assert host_matches(url, SELLER_DOMAIN)
    assert host_matches(url, SELLER_DOMAIN.upper()), "DNS names are case-insensitive"
    assert host_matches(url, f"{SELLER_DOMAIN}."), "a trailing root dot is the same name"


@pytest.mark.parametrize(("label", "url"), sorted(HOSTILE_URLS.items()))
def test_hostile_hosts_are_refused(label: str, url: str) -> None:
    """S8-3: exact host match, no wildcards, no substring, no suffix."""
    assert not host_matches(url, SELLER_DOMAIN), label


def test_host_match_is_not_reflexive_on_a_malformed_url() -> None:
    """A malformed URL is neither a match nor a mismatch — it raises.

    Collapsing it to ``False`` would report "wrong domain" for a link that has no domain at
    all, sending whoever debugs it to the wrong place.
    """
    with pytest.raises(PermalinkError):
        host_matches("not-a-url", SELLER_DOMAIN)


def test_empty_seller_domain_never_matches() -> None:
    """A missing registered domain must fail closed, not match everything."""
    url = build_permalink(shop_domain=SELLER_DOMAIN, variant_id=1)
    assert not host_matches(url, "")
    assert not host_matches(url, "   ")
