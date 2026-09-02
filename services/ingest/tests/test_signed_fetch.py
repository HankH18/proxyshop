"""T-020 — the signed fetch adapter, its SSRF guard, its robots obedience and its budgets.

Run::

    PROXYSHOP_WORKER=<n> ./.venv/bin/python -m pytest services/ingest/tests/test_signed_fetch.py -q

Three habits shape this file, all of them reactions to the specific way guards fail:

**Every guard test names a concrete blocked input AND a concrete allowed one.** A guard
that refuses everything passes any test that only checks refusals, and three lanes in this
project have shipped exactly that. :data:`SSRF_TABLE` therefore pairs each attack class
with a control that must still be admitted, and the parametrised test asserts both
directions from the same call.

**No guard is allowed to be propped up by a weaker rule beside it.** ``http://127.0.0.1:8080/``
is refused by the default policy's port rule *and* by its address rule, so a test that only
checks the boolean cannot tell whether the address rule works at all. The tests that matter
therefore run with the port rule switched off, so the address rule is the only thing left
standing — and assert the *reason*, not just the answer.

**Budgets are tested against a server that is actually trying to break them.** The
storefront in ``_fixtures_storefront.py`` serves a gzip bomb, an unbounded body, a redirect
loop, a redirect into cloud metadata and a stalling stream. A budget is a claim about
hostile input, so it is measured against hostile input.
"""

from __future__ import annotations

import ipaddress
import socket

import pytest
from ingest.adapters import (
    USER_AGENT,
    BudgetExceeded,
    CatalogAdapter,
    CatalogRequest,
    CrawlBudget,
    CrawlLedger,
    FetchPolicy,
    FetchRefused,
    SafeHTTPClient,
    SignedFetchAdapter,
    address_refusal,
    apply_upserts,
    content_hash,
    decode_numeric_ipv4,
    fetch_verdict,
    is_fetch_allowed,
    is_redirect_chain_allowed,
    may_fetch,
    robots_verdict_for_status,
    user_agent_token,
)

# NOTE: the storefront fixtures (`storefront`, `locked_storefront`, `storefront_factory`)
# come from `_fixtures_storefront.py`, which this directory's conftest auto-loads. They are
# requested as fixtures rather than imported: under pytest's `importlib` import mode a test
# module cannot import a sibling helper by name, so everything a test needs from that module
# reaches it through the yielded `StorefrontStub` (its `.password`, `.products`, `.requests`).

# The policy under which the in-process storefront on 127.0.0.1:<ephemeral> is reachable.
# Written out at every call site rather than hidden in a fixture: re-admitting loopback is
# exactly the loosening that must never happen by accident, so it stays visible.
LOOPBACK = FetchPolicy(allowed_ports=None, extra_allowed_networks=("127.0.0.0/8",))

# The address rule with the port rule switched off, so nothing else can carry a refusal.
ANY_PORT = FetchPolicy(allowed_ports=None)


def _fake_dns(
    monkeypatch, mapping: dict[str, str], default: str = "93.184.216.34"
) -> dict[str, int]:
    """Resolve names from ``mapping`` (or to ``default``); pass IP literals through.

    Returns a call counter keyed by hostname, so a test can assert how many times a name
    was resolved — which is how the TOCTOU/rebinding guarantee is measured.
    """
    calls: dict[str, int] = {}

    def _resolve(host: str) -> list[str]:
        bare = str(host).strip("[]")
        try:
            ipaddress.ip_address(bare)
            return [bare]
        except ValueError:
            pass
        calls[bare] = calls.get(bare, 0) + 1
        value = mapping.get(bare, default)
        return list(value) if isinstance(value, list) else [value]

    def _getaddrinfo(host, port=None, *args, **kwargs):
        out = []
        for ip in _resolve(host):
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            out.append((family, socket.SOCK_STREAM, 6, "", (ip, int(port or 0))))
        return out

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    return calls


def _no_sockets(monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise AssertionError("the SSRF guard must decide without opening a connection")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


# ======================================================================================
# 1. The SSRF rejection table — what does only this guard reject?
# ======================================================================================

#: ``(class, blocked, allowed)``. The third column is the control: it is an input of the
#: same *shape* that must still be admitted, so a guard that has simply stopped saying yes
#: cannot pass this table.
SSRF_TABLE: list[tuple[str, str, str]] = [
    ("ipv4 loopback", "http://127.0.0.1/", "http://93.184.216.34/"),
    ("ipv4 loopback high port", "http://127.0.0.1:8080/admin", "http://93.184.216.34:8080/admin"),
    (
        "cloud metadata (link-local)",
        "http://169.254.169.254/latest/meta-data/",
        "http://93.184.216.34/latest/meta-data/",
    ),
    ("rfc1918 10/8", "http://10.0.0.5/internal", "http://11.0.0.5/internal"),
    ("rfc1918 172.16/12", "http://172.16.31.9/", "http://172.32.31.9/"),
    ("rfc1918 192.168/16", "http://192.168.1.1/", "http://192.169.1.1/"),
    ("cgnat 100.64/10", "http://100.64.0.1/", "http://100.128.0.1/"),
    ("unspecified 0.0.0.0", "http://0.0.0.0/", "http://1.0.0.1/"),
    ("broadcast", "http://255.255.255.255/", "http://223.255.255.255/"),
    ("multicast", "http://224.0.0.1/", "http://223.0.0.1/"),
    ("ipv6 loopback", "http://[::1]/", "http://[2606:4700::1111]/"),
    ("ipv6 unique-local", "http://[fd00::1]/", "http://[2001:4860:4860::8888]/"),
    ("ipv6 link-local", "http://[fe80::1]/", "http://[2620:fe::fe]/"),
    ("ipv4-mapped ipv6", "http://[::ffff:127.0.0.1]/", "http://[2606:4700::1111]/"),
    (
        "ipv4-mapped public is still refused",
        "http://[::ffff:93.184.216.34]/",
        "http://93.184.216.34/",
    ),
    ("6to4-wrapped private", "http://[2002:0a00:0005::]/", "http://[2606:4700::1111]/"),
    ("name for loopback", "http://localhost:80/", "http://store.example.com/"),
    ("name under .internal", "http://api.internal/", "http://api.example.com/"),
    ("decimal ip literal", "http://2130706433/", "http://93.184.216.34/"),
    ("octal ip literal", "http://0177.0.0.1/", "http://93.184.216.34/"),
    ("hex ip literal", "http://0x7f000001/", "http://93.184.216.34/"),
    ("short-form ip literal", "http://127.1/", "http://93.184.216.34/"),
    ("non-http scheme", "file:///etc/passwd", "http://93.184.216.34/"),
    ("gopher scheme", "gopher://93.184.216.34:70/", "http://93.184.216.34/"),
    ("userinfo smuggling", "http://store.example.com@127.0.0.1/", "http://store.example.com/"),
]


@pytest.mark.parametrize(
    ("label", "blocked", "allowed"), SSRF_TABLE, ids=[row[0] for row in SSRF_TABLE]
)
def test_ssrf_rejection_table(monkeypatch, label, blocked, allowed):
    """Each attack class is refused, and a same-shaped legitimate target is still admitted."""
    _fake_dns(monkeypatch, {})
    _no_sockets(monkeypatch)
    policy = ANY_PORT  # the port rule is off, so only the address/name/scheme rules answer

    assert fetch_verdict(blocked, policy=policy).allowed is False, (
        f"{label}: SSRF guard admitted {blocked}"
    )
    assert fetch_verdict(allowed, policy=policy).allowed is True, (
        f"{label}: guard refused the control {allowed} — it may be refusing everything "
        f"(reason: {fetch_verdict(allowed, policy=policy).reason})"
    )


def test_private_targets_are_refused_by_the_address_rule_not_by_the_port_rule():
    """The two frozen cases that a port allow-list could refuse on its own must not need it.

    ``http://127.0.0.1:8080/admin`` and ``http://localhost:5432/`` are both refused by the
    default policy — but so is any URL on a non-standard port, so passing the frozen test
    proves nothing about the address rule. With ports unrestricted, the refusal has to come
    from the address and name rules, and the asserted reason says which.
    """
    loopback = fetch_verdict("http://127.0.0.1:8080/admin", policy=ANY_PORT)
    assert loopback.allowed is False
    assert loopback.reason.startswith("blocked-network:127.0.0.1"), loopback.reason

    name = fetch_verdict("http://localhost:5432/", policy=ANY_PORT)
    assert name.allowed is False
    assert name.reason == "denied-name:localhost", name.reason

    # And the port rule really is off, or the assertions above prove nothing.
    assert fetch_verdict("http://93.184.216.34:8080/", policy=ANY_PORT).allowed is True


def test_localhost_is_refused_by_name_even_when_dns_says_it_is_public():
    """A resolver cannot talk the guard into ``localhost``.

    Under a resolver that maps every name to a public address — which is precisely what a
    hostile or compromised DNS server does — a guard that only judges resolved addresses
    admits ``localhost``. This one refuses the name before it ever asks.
    """
    with pytest.MonkeyPatch.context() as mp:
        _fake_dns(mp, {"localhost": "93.184.216.34"})
        _no_sockets(mp)
        assert is_fetch_allowed("http://localhost/") is False
        assert fetch_verdict("http://localhost/", policy=ANY_PORT).reason == "denied-name:localhost"


def test_dns_rebinding_is_refused_because_the_guard_resolves_before_deciding(monkeypatch):
    """An ordinary-looking name that resolves into RFC1918 space is refused."""
    _fake_dns(monkeypatch, {"looks-fine.example.net": "10.0.0.5"})
    _no_sockets(monkeypatch)
    assert is_fetch_allowed("https://looks-fine.example.net/products.json") is False
    assert is_fetch_allowed("https://ordinary.example.net/products.json") is True


def test_one_private_address_in_a_round_robin_answer_refuses_the_whole_host(monkeypatch):
    """A partially-private answer set is a refusal: the *next* connect could pick the bad one."""
    _fake_dns(
        monkeypatch,
        {
            "mixed.example.com": ["93.184.216.34", "169.254.169.254"],
            "clean.example.com": ["93.184.216.34", "93.184.216.35"],
        },
    )
    _no_sockets(monkeypatch)
    assert is_fetch_allowed("https://mixed.example.com/") is False
    assert is_fetch_allowed("https://clean.example.com/") is True


def test_a_name_that_does_not_resolve_is_refused_rather_than_assumed_public(monkeypatch):
    def _explode(host, port=None, *args, **kwargs):
        raise OSError("NXDOMAIN")

    monkeypatch.setattr(socket, "getaddrinfo", _explode)
    _no_sockets(monkeypatch)
    verdict = fetch_verdict("https://nowhere.example.com/")
    assert verdict.allowed is False
    assert verdict.reason.startswith("unresolvable:")


@pytest.mark.parametrize(
    ("host", "decoded"),
    [
        ("2130706433", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),
        ("0x7f000001", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("0xa000005", "10.0.0.5"),
        ("2852039166", "169.254.169.254"),
    ],
)
def test_obfuscated_numeric_literals_decode_to_what_libc_would_dial(host, decoded):
    """The decoder recognises exactly the forms ``ipaddress.ip_address`` refuses to parse.

    This is the fact the guard's second decision rests on: each of these is rejected by the
    strict parser (so a naive guard calls it "a hostname") while ``inet_aton`` reads it as
    a private address (so the kernel dials one).
    """
    with pytest.raises(ValueError):
        ipaddress.ip_address(host)
    assert str(decode_numeric_ipv4(host)) == decoded


def test_ordinary_hostnames_are_not_mistaken_for_numeric_literals():
    """The permissive decoder must not swallow real names, or the control column dies."""
    for host in ("store.example.com", "shop.myshopify.com", "a1.example", "9lives.example.net"):
        assert decode_numeric_ipv4(host) is None


def test_the_guard_opens_no_socket_and_reads_no_body(monkeypatch):
    """The decision is made from DNS alone; nothing is dialled to find out."""
    _fake_dns(monkeypatch, {})
    _no_sockets(monkeypatch)
    assert is_fetch_allowed("https://store.example.com/products.json") is True
    assert is_fetch_allowed("http://10.0.0.1/") is False


def test_address_refusal_can_be_relaxed_only_by_an_explicit_network_grant():
    """Loopback is refused by default and admitted only when a policy names its CIDR."""
    loopback = ipaddress.ip_address("127.0.0.1")
    assert address_refusal(loopback) is not None
    assert address_refusal(loopback, FetchPolicy(extra_allowed_networks=("127.0.0.0/8",))) is None
    # The grant is narrow: naming loopback does not admit link-local metadata.
    metadata = ipaddress.ip_address("169.254.169.254")
    assert (
        address_refusal(metadata, FetchPolicy(extra_allowed_networks=("127.0.0.0/8",))) is not None
    )


# ======================================================================================
# 2. Redirect chains
# ======================================================================================


def test_redirect_chain_allowlist_matching_is_exact_or_proper_subdomain(monkeypatch):
    _fake_dns(monkeypatch, {})
    _no_sockets(monkeypatch)
    allowed = ["store.example.com"]
    start = "https://store.example.com/products.json"

    assert is_redirect_chain_allowed([start], allowed, 5) is True
    assert is_redirect_chain_allowed([start, "https://cdn.store.example.com/x"], allowed, 5) is True
    # Prefix spoof, suffix spoof, and a sibling label — none is a subdomain of the allowed host.
    for spoof in (
        "https://store.example.com.attacker.tld/collect",
        "https://evilstore.example.com/collect",
        "https://xstore.example.com/collect",
        "https://example.com/collect",
    ):
        assert is_redirect_chain_allowed([start, spoof], allowed, 5) is False, spoof


def test_a_redirect_chain_that_repeats_a_url_is_refused(monkeypatch):
    """A loop inside the hop budget still makes no progress, so it is not tolerated."""
    _fake_dns(monkeypatch, {})
    _no_sockets(monkeypatch)
    start = "https://store.example.com/a"
    assert (
        is_redirect_chain_allowed(
            [start, "https://store.example.com/b", start], ["store.example.com"], 5
        )
        is False
    )


def test_the_hop_bound_is_inclusive_and_an_empty_chain_is_refused(monkeypatch):
    _fake_dns(monkeypatch, {})
    _no_sockets(monkeypatch)
    hosts = ["store.example.com"]
    chain = [f"https://store.example.com/hop/{i}" for i in range(4)]  # 3 redirects
    assert is_redirect_chain_allowed(chain, hosts, 3) is True
    assert is_redirect_chain_allowed(chain, hosts, 2) is False
    assert is_redirect_chain_allowed([], hosts, 5) is False


def test_a_trailing_dot_or_odd_case_does_not_defeat_the_allowlist(monkeypatch):
    _fake_dns(monkeypatch, {})
    _no_sockets(monkeypatch)
    start = "https://store.example.com/a"
    assert (
        is_redirect_chain_allowed([start, "https://STORE.Example.COM./b"], ["store.example.com"], 5)
        is True
    )
    assert (
        is_redirect_chain_allowed(
            [start, "https://store.example.com.evil.tld./b"], ["store.example.com"], 5
        )
        is False
    )


# ======================================================================================
# 3. Robots and crawler identity (C6)
# ======================================================================================


def test_the_crawler_identifies_itself_and_impersonates_no_browser():
    lowered = USER_AGENT.lower()
    for token in ("mozilla", "applewebkit", "chrome", "safari", "gecko", "edg/"):
        assert token not in lowered, f"{USER_AGENT!r} impersonates a browser"
    assert "bot" in lowered and "http" in lowered, "agent must name a bot and a contact URL"
    assert user_agent_token(USER_AGENT) == "ProxyShopBot"


def test_robots_disallow_is_honoured_with_longest_match_precedence():
    ua = USER_AGENT
    robots = "User-agent: *\nDisallow: /admin/\nDisallow: /cart\nAllow: /\n"
    assert may_fetch(robots, "https://store.example.com/products.json", ua) is True
    assert may_fetch(robots, "https://store.example.com/admin/orders", ua) is False
    assert may_fetch(robots, "https://store.example.com/cart", ua) is False
    assert may_fetch("", "https://store.example.com/anything", ua) is True


def test_a_group_naming_our_token_beats_the_permissive_wildcard():
    ua = USER_AGENT
    targeted = f"User-agent: {user_agent_token(ua)}\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    assert may_fetch(targeted, "https://store.example.com/products.json", ua) is False
    # A group naming somebody else does not apply to us.
    other = "User-agent: SomeoneElseBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    assert may_fetch(other, "https://store.example.com/products.json", ua) is True


def test_wildcards_and_end_anchors_in_robots_are_honoured():
    ua = USER_AGENT
    robots = "User-agent: *\nDisallow: /*.json$\nAllow: /\n"
    assert may_fetch(robots, "https://store.example.com/products.json", ua) is False
    assert may_fetch(robots, "https://store.example.com/products.jsonl", ua) is True


def test_unreachable_robots_disallows_but_a_404_allows():
    """RFC 9309 §2.3.1: absent means no rules; unreachable means do not crawl."""
    assert robots_verdict_for_status(200) == "use"
    assert robots_verdict_for_status(404) == "allow-all"
    assert robots_verdict_for_status(410) == "allow-all"
    assert robots_verdict_for_status(500) == "disallow-all"
    assert robots_verdict_for_status(503) == "disallow-all"
    assert robots_verdict_for_status(403) == "disallow-all"
    assert robots_verdict_for_status(None) == "disallow-all"


def test_unknown_robots_directives_are_ignored_but_a_parser_failure_fails_closed(monkeypatch):
    """RFC 9309 says ignore lines you do not understand; a parser that *breaks* is different.

    Garbage alongside real rules must not silently discard the real rules, and a parser
    that raises must not be read as "no rules, crawl freely".
    """
    url = "https://store.example.com/admin/orders"
    noisy = "Sitemap: https://store.example.com/sitemap.xml\nGobbledegook: yes\nUser-agent: *\nDisallow: /admin/\n"
    assert may_fetch(noisy, url, USER_AGENT) is False
    assert may_fetch(noisy, "https://store.example.com/products.json", USER_AGENT) is True

    import protego

    def _boom(*args, **kwargs):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(protego.Protego, "parse", staticmethod(_boom))
    assert may_fetch("User-agent: *\nAllow: /\n", url, USER_AGENT) is False


# ======================================================================================
# 4. Budgets — deterministic unit level
# ======================================================================================


def test_the_time_budget_is_measured_on_an_injectable_monotonic_clock():
    now = [100.0]
    ledger = CrawlLedger(CrawlBudget(max_seconds=5.0), monotonic=lambda: now[0])
    ledger.check_time()
    now[0] = 104.9
    ledger.check_time()
    now[0] = 105.1
    with pytest.raises(BudgetExceeded) as exc:
        ledger.check_time()
    assert exc.value.resource == "time"


def test_a_page_is_charged_before_it_is_sent_so_a_hung_request_still_costs():
    ledger = CrawlLedger(CrawlBudget(max_pages=2))
    ledger.charge_page()
    ledger.charge_page()
    with pytest.raises(BudgetExceeded) as exc:
        ledger.charge_page()
    assert exc.value.resource == "pages"
    assert ledger.pages == 2


def test_the_depth_budget_refuses_the_enqueue_rather_than_the_fetch():
    ledger = CrawlLedger(CrawlBudget(max_depth=1))
    assert ledger.may_enqueue(0) is True
    assert ledger.may_enqueue(1) is True
    assert ledger.may_enqueue(2) is False
    with pytest.raises(BudgetExceeded) as exc:
        ledger.charge_depth(2)
    assert exc.value.resource == "depth"


def test_byte_budgets_separate_the_whole_crawl_from_one_response():
    ledger = CrawlLedger(CrawlBudget(max_bytes=1000, max_response_bytes=400))
    ledger.charge_bytes(300, response_total=300)
    with pytest.raises(BudgetExceeded) as exc:
        ledger.charge_bytes(200, response_total=500)
    assert exc.value.resource == "response_bytes"

    # The whole-crawl budget accumulates across responses, so two well-behaved responses
    # can still exhaust it between them.
    fresh = CrawlLedger(CrawlBudget(max_bytes=1000, max_response_bytes=10_000))
    fresh.charge_bytes(400, response_total=400)
    fresh.charge_bytes(400, response_total=400)
    with pytest.raises(BudgetExceeded) as exc:
        fresh.charge_bytes(400, response_total=400)
    assert exc.value.resource == "bytes"


def test_a_negative_budget_is_rejected_at_construction():
    with pytest.raises(ValueError):
        CrawlBudget(max_pages=-1)


# ======================================================================================
# 5. The transport against a hostile storefront
# ======================================================================================


def _client() -> SafeHTTPClient:
    return SafeHTTPClient(policy=LOOPBACK)


def test_the_transport_connects_to_the_address_the_guard_vetted_and_resolves_once(
    monkeypatch, storefront
):
    """The TOCTOU/rebinding guarantee, measured rather than asserted in prose.

    The fake resolver answers ``flaky.example.com`` with loopback the **first** time and
    with cloud-metadata every time after. A transport that re-resolves the hostname when it
    dials would connect to ``169.254.169.254``; this one pins the vetted address, so the
    name is resolved exactly once and the connection lands on the storefront.
    """
    base_url, stub = storefront
    port = int(base_url.rsplit(":", 1)[1])
    answers = iter(["127.0.0.1"])

    calls: dict[str, int] = {}

    def _getaddrinfo(host, port_=None, *args, **kwargs):
        bare = str(host).strip("[]")
        try:
            ipaddress.ip_address(bare)
            ip = bare
        except ValueError:
            calls[bare] = calls.get(bare, 0) + 1
            ip = next(answers, "169.254.169.254")
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, int(port_ or 0)))]

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)

    result = _client().fetch(f"http://flaky.example.com:{port}/robots.txt")
    assert result.status == 200
    assert result.connected_to == "127.0.0.1"
    assert calls["flaky.example.com"] == 1, (
        f"the hostname was resolved {calls['flaky.example.com']} times — a second "
        "resolution is a DNS-rebinding window"
    )


def test_a_redirect_into_cloud_metadata_is_refused_mid_chain(storefront):
    """A chain that starts on an approved host and tries to end on 169.254 is cut.

    The asserted reason is the *address* rule, not the allow-list. Both would refuse this
    hop, and a test that accepted either could not tell whether the SSRF guard runs on
    redirect targets at all.
    """
    base_url, _ = storefront
    with pytest.raises(FetchRefused) as exc:
        _client().fetch(f"{base_url}/redirect/metadata")
    assert exc.value.reason.startswith("blocked-network:169.254.169.254"), exc.value.reason


def test_a_redirect_to_an_allow_listed_host_that_resolves_privately_is_still_refused(
    monkeypatch, storefront
):
    """The hop is on the allow-list, so only the per-hop SSRF re-check can refuse it.

    This is the case the allow-list cannot cover and the initial check cannot see: the
    first hop is fine, the redirect target is a host the caller explicitly approved, and
    the *only* thing wrong with it is where it resolves. If the guard ran once at the start
    instead of on every hop, this would sail through to an internal address.
    """
    base_url, stub = storefront
    stub.redirect_target = "http://internal.example.com/secrets"
    _fake_dns(monkeypatch, {"internal.example.com": "10.0.0.5"})

    with pytest.raises(FetchRefused) as exc:
        _client().fetch(
            f"{base_url}/redirect/custom",
            allowed_hosts=("127.0.0.1", "internal.example.com"),
        )
    assert exc.value.reason.startswith("blocked-network:10.0.0.5"), exc.value.reason

    # Control: the same allow-listed host, resolving publicly, is followed rather than
    # refused — so the refusal above is about the address, not about the host name.
    stub.redirect_target = f"{base_url}/robots.txt"
    result = _client().fetch(
        f"{base_url}/redirect/custom", allowed_hosts=("127.0.0.1", "internal.example.com")
    )
    assert result.status == 200
    assert len(result.redirect_chain) == 2


def test_a_redirect_loop_is_refused_rather_than_followed(storefront):
    base_url, _ = storefront
    with pytest.raises(FetchRefused) as exc:
        _client().fetch(f"{base_url}/redirect/loop")
    assert exc.value.reason == "redirect-loop"


def test_a_long_redirect_chain_exhausts_the_redirect_budget(storefront):
    base_url, _ = storefront
    ledger = CrawlLedger(CrawlBudget(max_redirects=3, max_pages=100))
    with pytest.raises(BudgetExceeded) as exc:
        _client().fetch(f"{base_url}/redirect/chain/0", ledger=ledger)
    assert exc.value.resource == "redirects"


def test_a_gzip_bomb_is_refused_without_being_inflated(storefront):
    """8 MiB of zeros arrive as a few KiB; the ceiling is on what comes *out* of the inflater."""
    base_url, _ = storefront
    budget = CrawlBudget(max_response_bytes=1024 * 1024, max_decompressed_bytes=64 * 1024)
    ledger = CrawlLedger(budget)
    with pytest.raises(BudgetExceeded) as exc:
        _client().fetch(f"{base_url}/bomb.json", ledger=ledger)
    assert exc.value.resource == "decompressed_bytes"
    # The wire bytes were tiny — proof the refusal came from the inflated size, and that
    # the 8 MiB was never materialised.
    assert ledger.bytes_downloaded < 1024 * 1024
    assert ledger.bytes_decompressed <= budget.max_decompressed_bytes + 1


def test_an_unbounded_response_body_is_cut_at_the_byte_budget(storefront):
    base_url, _ = storefront
    ledger = CrawlLedger(CrawlBudget(max_response_bytes=256 * 1024, max_bytes=8 * 1024 * 1024))
    with pytest.raises(BudgetExceeded) as exc:
        _client().fetch(f"{base_url}/endless", ledger=ledger)
    assert exc.value.resource in ("response_bytes", "bytes")
    assert ledger.bytes_downloaded < 2 * 1024 * 1024


def test_a_stalling_response_is_cut_at_the_time_budget(storefront_factory):
    """A server that dribbles must be abandoned *at* the deadline, not after it finishes.

    The wall-clock assertion is the point of this test, not decoration. ``/slow`` emits far
    more data than the budget allows time for, and every individual dribble arrives well
    inside the socket timeout — so the socket timeout never fires and cannot save us. A
    reader that asks for a full buffer (``read``) blocks until the *whole* body has arrived
    and only then notices the deadline: still an exception, still ``resource == "time"``,
    but ten deadlines late, which for a server that never stops dribbling means never. Only a
    reader that takes what has arrived (``read1``) turns the budget into a real deadline,
    and only the elapsed-time assertion can tell the two apart.
    """
    import time

    base_url, stub = storefront_factory()
    stub.slow_chunk_seconds = 0.1
    stub.slow_chunks = 40  # 4s of dribble against a 0.4s budget
    ledger = CrawlLedger(CrawlBudget(max_seconds=0.4, connect_timeout=5.0))

    started = time.monotonic()
    with pytest.raises(BudgetExceeded) as exc:
        _client().fetch(f"{base_url}/slow", ledger=ledger)
    elapsed = time.monotonic() - started

    assert exc.value.resource == "time"
    assert elapsed < 2.0, (
        f"the crawl was abandoned only after {elapsed:.1f}s against a 0.4s budget — the "
        "deadline fired after the body finished arriving, not while it was arriving"
    )


def test_the_transport_refuses_a_host_outside_the_allow_list(storefront):
    base_url, _ = storefront
    port = int(base_url.rsplit(":", 1)[1])
    with pytest.raises(FetchRefused) as exc:
        _client().fetch(f"http://127.0.0.1:{port}/", allowed_hosts=("store.example.com",))
    assert exc.value.reason.startswith("host-not-allow-listed")


def test_the_default_policy_refuses_the_loopback_storefront(storefront):
    """The permissive policy these tests use is a deliberate opt-in, not the default."""
    base_url, _ = storefront
    with pytest.raises(FetchRefused):
        SafeHTTPClient().fetch(f"{base_url}/robots.txt")


def test_every_request_carries_the_identified_user_agent(storefront):
    base_url, stub = storefront
    _client().fetch(f"{base_url}/robots.txt")
    assert stub.requests, "the storefront saw no request"
    assert stub.requests[-1][2]["user-agent"] == USER_AGENT


def test_a_signed_request_carries_a_replay_bound_signature(storefront):
    from ingest.adapters import RequestSigner

    base_url, stub = storefront
    signer = RequestSigner(signer_id="proxyshop", secret=b"s3cret", key_id="k1")
    client = SafeHTTPClient(
        policy=LOOPBACK, signer=signer, clock=lambda: ("2026-01-01T00:00:00Z", "n0")
    )
    client.fetch(f"{base_url}/robots.txt")
    headers = stub.requests[-1][2]
    assert headers["x-proxyshop-signer"] == "proxyshop"
    assert headers["x-proxyshop-nonce"] == "n0"
    assert len(headers["x-proxyshop-signature"]) == 64
    # The host and path are inside the signature, so the same bytes signed for another
    # endpoint produce a different value — a captured signature cannot be replayed.
    other = signer.headers("GET", "127.0.0.1", "/products.json", "2026-01-01T00:00:00Z", "n0", b"")
    assert other["X-ProxyShop-Signature"] != headers["x-proxyshop-signature"]


# ======================================================================================
# 6. The adapter end to end
# ======================================================================================


def _request(base_url: str, **kwargs) -> CatalogRequest:
    defaults = dict(
        store_id="store-t020",
        base_url=base_url,
        policy=LOOPBACK,
        budget=CrawlBudget(max_pages=60, max_seconds=30.0),
    )
    defaults.update(kwargs)
    return CatalogRequest(**defaults)


def test_the_adapter_satisfies_the_one_catalog_adapter_interface():
    adapter = SignedFetchAdapter()
    assert isinstance(adapter, CatalogAdapter)
    assert callable(adapter.fetch_catalog) and callable(adapter.to_upserts)


def test_the_adapter_ingests_an_open_storefront(storefront):
    """Acceptance 1: products.json + JSON-LD are read and merged into records."""
    base_url, stub = storefront
    snapshot = SignedFetchAdapter().fetch_catalog(_request(base_url))

    assert snapshot.warnings == (), snapshot.warnings
    assert len(snapshot.products) == len(stub.products)
    shoe = next(p for p in snapshot.products if p.canonical_name == "Trail Runner 42")
    assert shoe.brand == "Cascade"
    assert shoe.categories == ("Footwear",)
    assert [v.seller_sku for v in shoe.variants] == ["TR-42-9", "TR-42-10"]
    assert [v.price for v in shoe.variants] == [129.95, 129.95]
    # Availability comes from products.json's bool; currency only exists in the JSON-LD,
    # so a run that failed to parse the product page would show the fallback for one and
    # the wrong value for the other.
    assert [v.availability for v in shoe.variants] == ["in_stock", "out_of_stock"]
    assert {v.currency for v in shoe.variants} == {"USD"}

    # robots.txt was read before any catalog page (C6).
    paths = stub.paths_fetched()
    assert paths[0] == "/robots.txt"
    assert "/products.json" in paths
    assert "/products/trail-runner-42" in paths


def test_the_adapter_ingests_a_password_protected_dev_store(locked_storefront):
    """Acceptance 1 / SPEC A1: the dev-store password unlocks the catalog."""
    base_url, stub = locked_storefront
    snapshot = SignedFetchAdapter().fetch_catalog(
        _request(base_url, storefront_password=stub.password)
    )
    assert snapshot.warnings == (), snapshot.warnings
    assert len(snapshot.products) == len(stub.products)
    assert any(method == "POST" and path == "/password" for method, path, _ in stub.requests)
    # The session cookie the store set was replayed on the catalog request, which is the
    # only reason the catalog was visible at all.
    catalog = next(h for m, p, h in stub.requests if p == "/products.json")
    assert "storefront_digest=" in catalog.get("cookie", "")


def test_without_the_password_the_locked_store_yields_nothing(locked_storefront):
    """The control for the test above: the password is doing the work, not the fixture."""
    base_url, _ = locked_storefront
    snapshot = SignedFetchAdapter().fetch_catalog(_request(base_url))
    assert snapshot.products == ()


def test_a_wrong_password_is_reported_rather_than_silently_ingesting_nothing(locked_storefront):
    base_url, _ = locked_storefront
    snapshot = SignedFetchAdapter().fetch_catalog(_request(base_url, storefront_password="wrong"))
    assert snapshot.products == ()
    assert any("password rejected" in w for w in snapshot.warnings), snapshot.warnings


def test_the_session_cookie_is_not_replayed_to_another_host(locked_storefront):
    """Credentials stay on the host that issued them."""
    base_url, _ = locked_storefront
    client = SafeHTTPClient(policy=LOOPBACK)
    client.cookies.store("127.0.0.1", ["storefront_digest=abc; path=/"])
    assert "storefront_digest" in client.cookies.header_for("127.0.0.1")
    assert client.cookies.header_for("attacker.tld") == ""


def test_robots_disallow_stops_the_catalog_fetch(storefront_factory):
    """Acceptance 2: a robots rule against the catalog endpoint is obeyed."""
    base_url, stub = storefront_factory(
        robots="User-agent: *\nDisallow: /products.json\nAllow: /\n"
    )
    snapshot = SignedFetchAdapter().fetch_catalog(_request(base_url))
    assert snapshot.products == ()
    assert any("robots.txt disallows" in w for w in snapshot.warnings), snapshot.warnings
    assert "/products.json" not in stub.paths_fetched()

    # Control: the same storefront with permissive robots does ingest.
    open_url, _ = storefront_factory()
    assert SignedFetchAdapter().fetch_catalog(_request(open_url)).products


def test_a_robots_rule_naming_our_token_stops_the_crawl(storefront_factory):
    base_url, stub = storefront_factory(
        robots=f"User-agent: {user_agent_token(USER_AGENT)}\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    )
    snapshot = SignedFetchAdapter().fetch_catalog(_request(base_url))
    assert snapshot.products == ()
    assert "/products.json" not in stub.paths_fetched()


def test_an_unreachable_robots_abandons_the_crawl(storefront_factory):
    base_url, stub = storefront_factory(robots_status=503)
    snapshot = SignedFetchAdapter().fetch_catalog(_request(base_url))
    assert snapshot.products == ()
    assert "/products.json" not in stub.paths_fetched()


def test_the_adapter_refuses_a_private_base_url_under_the_default_policy(storefront):
    """The adapter is SSRF-guarded by default; the loopback grant is per-request and explicit."""
    base_url, stub = storefront
    snapshot = SignedFetchAdapter().fetch_catalog(
        CatalogRequest(store_id="s", base_url=base_url)  # default FetchPolicy
    )
    assert snapshot.products == ()
    assert stub.requests == [], "the guard let a request reach a private address"


# ---- change detection (acceptance 3) -------------------------------------------------


def test_unchanged_content_produces_zero_reextraction_work(storefront):
    """Acceptance 3: a second crawl of an unchanged store emits no upserts at all."""
    base_url, stub = storefront
    adapter = SignedFetchAdapter()

    first = adapter.fetch_catalog(_request(base_url))
    first_ops = adapter.to_upserts(first)
    assert first_ops, "the first crawl must produce work"
    assert all(p.changed for p in first.products)

    known = first.hash_index
    second = adapter.fetch_catalog(_request(base_url, known_hashes=known))

    assert [p.changed for p in second.products] == [False] * len(second.products)
    assert second.changed_products == ()
    assert adapter.to_upserts(second) == [], "unchanged content must produce zero upserts"
    assert second.unchanged_urls, "the resources should be reported as unchanged"


def test_changed_content_is_detected_and_re_upserted(storefront):
    """The control: change one price and the work comes back for that product only."""
    base_url, stub = storefront
    adapter = SignedFetchAdapter()
    first = adapter.fetch_catalog(_request(base_url))
    known = first.hash_index

    stub.products[0]["variants"][0]["price"] = "139.95"
    second = adapter.fetch_catalog(_request(base_url, known_hashes=known))

    changed = [p.canonical_name for p in second.changed_products]
    assert changed == ["Trail Runner 42"], changed
    ops = adapter.to_upserts(second)
    assert {op.kind for op in ops} >= {"store", "product", "variant", "offer"}
    assert all(op.kind != "product" or "Merino" not in op.node.canonical_name for op in ops)


def test_the_content_hash_is_stable_against_key_reordering_but_not_against_values(storefront):
    """Change detection must not fire on a re-serialisation, and must fire on a real edit."""
    from ingest.adapters import canonical_json_hash

    a = {"id": 1, "title": "Shoe", "variants": [{"sku": "x", "price": "10.00"}]}
    b = {"variants": [{"price": "10.00", "sku": "x"}], "title": "Shoe", "id": 1}
    c = {"id": 1, "title": "Shoe", "variants": [{"sku": "x", "price": "11.00"}]}
    assert canonical_json_hash(a) == canonical_json_hash(b)
    assert canonical_json_hash(a) != canonical_json_hash(c)
    assert content_hash("x") == content_hash(b"x") != content_hash("y")


# ---- mapping to the graph ------------------------------------------------------------


def test_to_upserts_is_pure_and_emits_dependencies_before_dependents(storefront):
    """``to_upserts`` opens no socket and orders writes so the graph's constraints hold."""
    base_url, _ = storefront
    adapter = SignedFetchAdapter()
    snapshot = adapter.fetch_catalog(_request(base_url))

    with pytest.MonkeyPatch.context() as mp:
        _no_sockets(mp)
        ops = adapter.to_upserts(snapshot)

    kinds = [op.kind for op in ops]
    assert kinds[0] == "store"
    assert kinds.index("product") < kinds.index("variant") < kinds.index("offer")
    assert all(op.source.source_class == "scraped" for op in ops)
    assert all(0.0 <= op.source.confidence <= 1.0 for op in ops)
    assert all(op.source.content_hash and op.source.url for op in ops)
    # Deterministic: the same snapshot maps to the same ops, so a re-crawl of unchanged
    # content cannot churn the graph through ID drift.
    assert adapter.to_upserts(snapshot) == ops


@pytest.mark.docker
@pytest.mark.graph
def test_the_ingested_catalog_lands_in_neo4j_with_complete_provenance(
    storefront, graph_schema_session
):
    """Acceptance 1, all the way through: a crawled storefront becomes a provenanced graph."""
    from ingest.graph import provenance_violations

    base_url, stub = storefront
    expected_products = len(stub.products)
    expected_variants = sum(len(p["variants"]) for p in stub.products)
    adapter = SignedFetchAdapter()
    snapshot = adapter.fetch_catalog(_request(base_url))
    written = apply_upserts(graph_schema_session, adapter.to_upserts(snapshot))
    assert written

    assert provenance_violations(graph_schema_session) == []
    counts = graph_schema_session.run("MATCH (p:Product) RETURN count(p) AS products").single()[
        "products"
    ]
    assert counts == expected_products
    offers = graph_schema_session.run("MATCH (o:Offer) RETURN count(o) AS n").single()["n"]
    assert offers == expected_variants

    # Re-applying an unchanged crawl writes nothing new (acceptance 3, at the graph).
    again = adapter.fetch_catalog(_request(base_url, known_hashes=snapshot.hash_index))
    assert apply_upserts(graph_schema_session, adapter.to_upserts(again)) == []
