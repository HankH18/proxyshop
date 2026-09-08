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
    MAX_HOST_LENGTH,
    PERMALINK_TEMPLATE,
    CartPermalink,
    PermalinkError,
    build_permalink,
    host_matches,
    parse_permalink,
)
from shopify_stub.testing import NON_BARE_HOSTS

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


# ---------------------------------------------------------------------------------------
# The builder's host validation, and the port the parser used to swallow
# ---------------------------------------------------------------------------------------

# The host table below is :data:`shopify_stub.testing.NON_BARE_HOSTS`, imported rather than
# copied. This file used to hold its own eleven-row copy while that module's comment said two
# test modules shared its table; only ``test_stub_domain_guard.py`` did. The seven rows the
# copy was missing — ``scheme``, ``path``, ``empty`` and the four named
# :data:`~shopify_stub.testing.RESPONSE_SPLITTING_HOSTS` — were never put to
# :func:`~shopify_stub.permalink.build_permalink` at all. Measured before unifying: the builder
# already refuses all eighteen, so this closed a coverage gap rather than a bug, and a
# nineteenth row added for the guard is put to the builder too.
#
# The two ``userinfo`` rows are the ones that cost something: they render a *live* checkout
# link whose real host is ``attacker.tld`` while a two-substring check (``"://"`` and ``"/"``)
# reports success, contradicting the builder's own docstring.
#
# These are plain ``#`` comments, not ``#:`` ones, deliberately. They documented a dict this
# file used to define; that dict is gone, so as ``#:`` they documented the NEXT assignment
# instead, and the only thing between them and :data:`REFUSALS` was one blank line a formatter
# is free to remove. A comment about a table has no assignment to attach to — a plain ``#``
# says so, and cannot be silently re-attached.

#: Every message :func:`~shopify_stub.permalink._assert_bare_host` can refuse with, in the
#: order its three guards run: emptiness, the
#: :data:`~shopify_stub.permalink.MAX_HOST_LENGTH` ceiling, then the DNS allow-list. Held as a
#: tuple so a row can be checked to have reached exactly ONE of them — a match fragment shared
#: by two of these would pass every row while distinguishing none.
REFUSALS: tuple[str, ...] = (
    "shop_domain must not be empty",
    f"shop_domain must be at most {MAX_HOST_LENGTH} characters",
    "shop_domain must be a bare host",
)


def expected_refusal(domain: str) -> str:
    """Which of :data:`REFUSALS` ``build_permalink`` owes ``domain``, from the guard ORDER.

    Two rules run BEFORE the bare-host rule, so ``match="bare host"`` for every row was wrong
    for any row that cannot reach it, and loosening the match to accept either message would
    have graded neither. Restating the order here is what lets each row be graded against the
    guard it actually reaches: add ``"overlong": "a" * 254 + ".com"`` to
    :data:`~shopify_stub.testing.NON_BARE_HOSTS` — the natural row for
    ``test_stub_domain_guard.py``, which shares this table — and it is graded against the
    length ceiling here instead of failing on a ``bare host`` it never reaches.
    """
    if not domain:
        return REFUSALS[0]
    if len(domain) > MAX_HOST_LENGTH:
        return REFUSALS[1]
    return REFUSALS[2]


@pytest.mark.parametrize(("label", "domain"), sorted(NON_BARE_HOSTS.items()))
def test_build_refuses_a_shop_domain_that_is_not_a_bare_dns_name(label: str, domain: str) -> None:
    """One allow-list, one error, for every character that is not part of a DNS name.

    The check this replaces tested for two substrings and therefore passed ``@``, ``\\``,
    ``:``, ``?`` and ``#``. ``?`` was the tell: it did eventually fail, but as *"permalink
    path must start with /cart/"* — a host bug reported as a path bug, in a different
    function, one call later.

    Every row of the shared table is driven, ``empty`` included, and each is held to the ONE
    refusal :func:`expected_refusal` says it is owed.
    """
    expected = expected_refusal(domain)
    with pytest.raises(PermalinkError) as raised:
        build_permalink(shop_domain=domain, variant_id=1, code=CODE)
    message = str(raised.value)
    assert [refusal for refusal in REFUSALS if refusal in message] == [expected], (
        f"{label!r} must be refused by exactly one guard, and by {expected!r}; got {message!r}"
    )


def test_build_never_emits_a_link_whose_real_host_is_somebody_else() -> None:
    """The defect in one assertion: the builder must not *succeed* at building an attack.

    ``https://good.example.com@attacker.tld/cart/1:1`` is a well-formed URL whose host is
    ``attacker.tld``; everything before the ``@`` is userinfo and is ignored by every client.
    Building it and returning it is worse than crashing, because the caller has no reason to
    look.
    """
    from urllib.parse import urlsplit

    hostile = "good.example.com@attacker.tld"
    assert urlsplit(f"https://{hostile}/cart/1:1").hostname == "attacker.tld", (
        "premise of this test: the userinfo form really does relocate the host"
    )
    with pytest.raises(PermalinkError, match="bare host"):
        build_permalink(shop_domain=hostile, variant_id=1, code=CODE)


def test_build_accepts_the_hosts_a_real_store_actually_has() -> None:
    """Negative control for the allow-list: it must not reject legitimate names.

    Hyphens inside a label, a single-character label, digits, mixed case and a trailing root
    dot are all legal DNS, and an over-eager host check that refused them would be a worse
    bug than the one it fixed.
    """
    for good in (
        "demo-store.myshopify.com",
        "store-a.example.com",
        "a.co",
        "shop123.example.co.uk",
        "Store-A.Example.COM",
        "store-a.example.com.",
    ):
        assert build_permalink(shop_domain=good, variant_id=1).startswith(f"https://{good}/cart/")


def test_build_rejects_a_host_longer_than_dns_allows() -> None:
    with pytest.raises(PermalinkError, match="253"):
        build_permalink(shop_domain=".".join(["abcdefghij"] * 26), variant_id=1)


@pytest.mark.parametrize(
    "bad_port_url",
    [
        f"https://{SELLER_DOMAIN}:8443/cart/1:1?discount={CODE}",
        f"https://{SELLER_DOMAIN}:443/cart/1:1?discount={CODE}",
        f"https://{SELLER_DOMAIN}:notaport/cart/1:1",
    ],
)
def test_parse_refuses_a_port_bearing_permalink(bad_port_url: str) -> None:
    """D22's template carries no port, so a port is structural breakage, not detail.

    ``urlsplit(...).port`` raises ``ValueError`` — not ``PermalinkError`` — on ``:notaport``,
    which would have escaped every ``pytest.raises(PermalinkError)`` in this file and reached
    a caller as an unrelated exception type. It is translated at the boundary.
    """
    with pytest.raises(PermalinkError):
        parse_permalink(bad_port_url)


def test_a_port_can_never_be_silently_rewritten_to_implicit_443() -> None:
    """The unambiguous half of the port defect: a *lossy* round-trip.

    :class:`CartPermalink` keeps only ``parts.hostname``, which drops the port, so parsing
    ``…:8443`` and re-rendering it used to yield ``https://store-a.example.com/cart/1:1`` —
    the same link pointed at a different listener. A buyer sent there checks out somewhere
    the merchant never published. Refusing the parse is the only answer that cannot lose the
    port, since the template has nowhere to put it.
    """
    with pytest.raises(PermalinkError, match="port"):
        parse_permalink(f"https://{SELLER_DOMAIN}:8443/cart/1:1?discount={CODE}")
    # And the builder cannot manufacture one for the parser to swallow.
    with pytest.raises(PermalinkError, match="bare host"):
        build_permalink(shop_domain=f"{SELLER_DOMAIN}:8443", variant_id=1)


def test_host_matches_does_not_report_a_port_bearing_host_as_the_registered_domain() -> None:
    """``host_matches`` compared ``parts.hostname``, which is the domain with the port cut off.

    So ``https://store-a.example.com:8443/cart/1:1`` matched ``store-a.example.com`` — the
    check said "this is the seller's own store" about a link aimed at a different listener.
    It must not answer ``True``; it now refuses the link outright, which is what
    ``host_matches`` already does for every other malformed permalink.
    """
    with pytest.raises(PermalinkError):
        host_matches(f"https://{SELLER_DOMAIN}:8443/cart/1:1?discount={CODE}", SELLER_DOMAIN)


@pytest.mark.parametrize(
    ("label", "code"),
    [
        ("accented latin", "PSX-CAFÉ"),
        ("precomposed", "PSX-CAFÉ"),
        ("cjk", "PSX-中文"),
        ("emoji", "PSX-\U0001f600"),
        ("a space", "PSX A B"),
        ("a slash", "PSX/AB"),
        ("a question mark", "PSX?AB"),
        ("an ampersand", "PSX&AB"),
    ],
)
def test_a_non_ascii_discount_code_still_builds_and_round_trips(label: str, code: str) -> None:
    """The blast-radius control for T-118 (d)'s tightening of ``_FORBIDDEN_IN_PATH``.

    ``store_url``'s path guard is now an allow-list of printable ASCII. The discount code
    is **not** subject to it: ``build_permalink`` percent-encodes the code with
    ``quote(code, safe='')`` and appends it *after* ``store_url`` has returned, so the guard
    never sees it. Narrowing the path guard must therefore leave every legal code legal —
    and a code is free-form merchant text, so refusing non-ASCII ones would be a real
    regression rather than a tightening.
    """
    url = build_permalink(shop_domain=SELLER_DOMAIN, variant_id=1, code=code)
    assert url.startswith(f"https://{SELLER_DOMAIN}/cart/1:1?discount=")
    assert url.isascii(), "the rendered URL is percent-encoded, so it is ASCII on the wire"
    assert parse_permalink(url).code == code, "and it decodes back to the caller's code"
