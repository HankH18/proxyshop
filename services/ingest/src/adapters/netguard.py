"""SSRF guard for the crawler (T-020, SPEC C10).

The rule the whole module exists to enforce: **an ingest fetch may only ever reach a
publicly-routable address on a host the caller allow-listed.** Everything else — loopback,
RFC1918, link-local (including the ``169.254.169.254`` cloud-metadata endpoint), CGNAT,
multicast, reserved space, IPv4-mapped and 6to4-wrapped forms of any of those, obfuscated
numeric literals, names that *resolve* into private space, and redirect chains that start
public and land private — is refused.

Three design decisions carry that guarantee, and each one exists because the obvious
implementation is bypassable:

1. **Resolve, then decide, then pin.** A name is checked against the addresses the resolver
   actually returned, not against how it looks. :func:`fetch_verdict` returns those
   addresses so the transport can connect to *them* rather than resolving a second time;
   a second resolution is a DNS-rebinding window (TOCTOU) and this module refuses to open
   one. See :mod:`ingest.adapters.transport`, which never calls the resolver itself.

2. **Obfuscated literals are refused outright, not decoded and re-judged.** ``0x7f000001``,
   ``2130706433``, ``0177.0.0.1`` and ``127.1`` are all ``127.0.0.1`` to ``inet_aton``,
   which is what a libc resolver hands the kernel — but ``ipaddress.ip_address`` rejects
   every one of them, so a guard built on the strict parser sees "not an IP literal, must
   be a hostname" and hands the string to DNS, where libc quietly parses it as the
   loopback address it always was. :func:`decode_numeric_ipv4` implements the permissive
   ``inet_aton`` grammar so the guard can *recognise* those forms, and any host whose
   decoded value does not round-trip to its canonical dotted-quad string is refused on
   sight. No legitimate storefront is addressed as ``http://2130706433/``.

3. **The blocked set is written out explicitly, not delegated to ``is_global``.** The
   stdlib's classification of several ranges (100.64/10, ``64:ff9b::/96``, IPv4-mapped
   IPv6) has moved between Python releases; ``224.0.0.1`` reports ``is_global is True`` on
   3.12. A guard whose meaning depends on the interpreter's patch level is not a guard, so
   the CIDR list below is the authority and ``is_global`` is only an extra veto on top.

Public surface::

    is_fetch_allowed(url) -> bool                                   # frozen, 1 positional
    is_redirect_chain_allowed(chain, allowed_hosts, max_hops) -> bool  # frozen, 3 positional
    fetch_verdict(url, *, policy=None) -> FetchVerdict               # the reasoned form
    FetchPolicy, FetchVerdict, FetchRefused, host_matches_allowlist
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urlsplit

__all__ = [
    "BLOCKED_IPV4_NETWORKS",
    "BLOCKED_IPV6_NETWORKS",
    "DENIED_HOST_SUFFIXES",
    "DENIED_HOSTS",
    "FetchPolicy",
    "FetchRefused",
    "FetchVerdict",
    "IPAddress",
    "address_refusal",
    "decode_numeric_ipv4",
    "fetch_verdict",
    "host_matches_allowlist",
    "is_fetch_allowed",
    "is_redirect_chain_allowed",
    "normalise_host",
    "resolve_host",
    "safe_split",
]

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# --------------------------------------------------------------------------------------
# The blocked address space, written out. See module docstring, decision 3.
# --------------------------------------------------------------------------------------

BLOCKED_IPV4_NETWORKS: tuple[str, ...] = (
    "0.0.0.0/8",  # "this network"; 0.0.0.0 itself is the local host on most stacks
    "10.0.0.0/8",  # RFC1918
    "100.64.0.0/10",  # RFC6598 CGNAT — reaches the carrier's kit, not the internet
    "127.0.0.0/8",  # loopback
    "169.254.0.0/16",  # link-local, incl. 169.254.169.254 cloud metadata
    "172.16.0.0/12",  # RFC1918
    "192.0.0.0/24",  # IETF protocol assignments
    "192.0.2.0/24",  # TEST-NET-1
    "192.88.99.0/24",  # 6to4 relay anycast
    "192.168.0.0/16",  # RFC1918
    "198.18.0.0/15",  # benchmarking
    "198.51.100.0/24",  # TEST-NET-2
    "203.0.113.0/24",  # TEST-NET-3
    "224.0.0.0/4",  # multicast
    "240.0.0.0/4",  # reserved, incl. 255.255.255.255
)

BLOCKED_IPV6_NETWORKS: tuple[str, ...] = (
    "::/128",  # unspecified
    "::1/128",  # loopback
    "::ffff:0:0/96",  # IPv4-mapped — never legitimate in a crawl target
    "::/96",  # deprecated IPv4-compatible
    "64:ff9b::/96",  # NAT64 well-known prefix
    "64:ff9b:1::/48",  # NAT64 local-use prefix
    "100::/64",  # discard-only
    "2001::/23",  # IETF protocol assignments (incl. Teredo 2001::/32)
    "2001:db8::/32",  # documentation
    "2002::/16",  # 6to4 — wraps an arbitrary IPv4, including private space
    "5f00::/16",  # segment routing
    "fc00::/7",  # unique local
    "fe80::/10",  # link-local
    "ff00::/8",  # multicast
)

_BLOCKED_V4 = tuple(ipaddress.ip_network(c) for c in BLOCKED_IPV4_NETWORKS)
_BLOCKED_V6 = tuple(ipaddress.ip_network(c) for c in BLOCKED_IPV6_NETWORKS)

#: Names that must never be resolved at all. ``localhost`` is the important one: a
#: resolver under an attacker's control (or a test double) can map it anywhere, so the
#: guard refuses it by *name* and never gets as far as trusting an answer about it.
DENIED_HOSTS: frozenset[str] = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})

#: Suffixes that address private naming scopes (mDNS, split-horizon corporate DNS, Tor).
DENIED_HOST_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".lan",
    ".corp",
    ".private",
    ".home.arpa",
    ".onion",
    ".invalid",
)


class FetchRefused(RuntimeError):
    """A fetch target was refused by the SSRF guard.

    Attributes:
        url: the target that was refused.
        reason: a short machine-ish token naming the rule that refused it.
    """

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"refused {url!r}: {reason}")
        self.url = url
        self.reason = reason


@dataclass(frozen=True)
class FetchPolicy:
    """What this crawler is permitted to reach.

    The defaults are the production posture: public addresses only, on the two ports a
    storefront actually serves. Loosening any field is a deliberate, explicit act — there
    is no environment variable and no "test mode" flag inside the guard, because a guard
    with a global backdoor is a guard whose default nobody can rely on. Tests that need
    to reach an in-process server on loopback construct a policy naming ``127.0.0.0/8``
    and pass it in, so the loosening is visible at the call site.

    Attributes:
        allowed_schemes: URL schemes that may be fetched at all.
        allowed_ports: ports that may be connected to; ``None`` means "any port".
        extra_allowed_networks: CIDRs that override the blocked set. Checked *before* the
            block list, so ``("127.0.0.0/8",)`` re-admits loopback and nothing else.
        allow_userinfo: whether ``http://user:pw@host/`` is tolerated. Off by default —
            userinfo is the classic way to make a URL *look* like it targets one host
            while the connect goes somewhere else.
        allow_numeric_host_forms: whether obfuscated numeric IPv4 literals are tolerated.
            Off by default; see module docstring, decision 2.
        max_addresses: cap on how many resolved addresses are considered, so a hostile
            resolver cannot hand back an unbounded answer set.
    """

    allowed_schemes: frozenset[str] = frozenset({"http", "https"})
    allowed_ports: frozenset[int] | None = frozenset({80, 443})
    extra_allowed_networks: tuple[str, ...] = ()
    allow_userinfo: bool = False
    allow_numeric_host_forms: bool = False
    max_addresses: int = 8

    def permitted_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return tuple(ipaddress.ip_network(c) for c in self.extra_allowed_networks)


DEFAULT_POLICY = FetchPolicy()


@dataclass(frozen=True)
class FetchVerdict:
    """The reasoned result of checking one URL.

    ``addresses`` is the whole point of returning a structure rather than a bool: it is
    the set the guard actually judged, and the transport must connect to one of *these*
    rather than resolving again. See module docstring, decision 1.
    """

    url: str
    allowed: bool
    reason: str = "ok"
    host: str = ""
    port: int = 0
    scheme: str = ""
    addresses: tuple[IPAddress, ...] = field(default=())

    def raise_if_refused(self) -> FetchVerdict:
        if not self.allowed:
            raise FetchRefused(self.url, self.reason)
        return self


# --------------------------------------------------------------------------------------
# Host parsing
# --------------------------------------------------------------------------------------


def normalise_host(host: str) -> str:
    """Lower-case, strip brackets, strip the root-label dot, IDNA-encode.

    The trailing dot matters: ``store.example.com.`` is the same name to DNS but a
    different string to an allow-list matched with ``==``, which is exactly the shape of
    bypass this normalisation closes. IDNA encoding closes the homograph equivalent —
    a Cyrillic-``е`` ``exаmple.com`` normalises to its punycode, which does not equal the
    ASCII allow-list entry.
    """
    bare = str(host).strip().strip("[]").rstrip(".").lower()
    if not bare:
        return ""
    if bare.isascii():
        return bare
    try:
        return bare.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        # Un-encodable name: hand back something that cannot match any ASCII allow-list
        # entry and will fail hostname-syntax validation below.
        return bare


def _parse_numeric_part(part: str) -> int | None:
    """One ``inet_aton`` component: decimal, ``0``-prefixed octal, or ``0x`` hex."""
    if not part:
        return None
    low = part.lower()
    try:
        if low.startswith("0x"):
            return int(low[2:], 16) if len(low) > 2 else None
        if low.startswith("0") and len(low) > 1:
            return int(low[1:], 8)
        return int(low, 10)
    except ValueError:
        return None


def decode_numeric_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Decode a host under the permissive ``inet_aton`` grammar, or return ``None``.

    Accepts one to four dot-separated parts, each decimal/octal/hex, with the final part
    absorbing all remaining bytes — i.e. exactly what libc does, and therefore exactly
    what a URL like ``http://0177.0.0.1/`` or ``http://2130706433/`` really addresses.
    Returns ``None`` for anything that is not such a literal (every real hostname).
    """
    text = str(host).strip()
    if not text or ":" in text:
        return None
    parts = text.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    values: list[int] = []
    for part in parts:
        value = _parse_numeric_part(part)
        if value is None or value < 0:
            return None
        values.append(value)
    leading, last = values[:-1], values[-1]
    if any(v > 0xFF for v in leading):
        return None
    if last > (256 ** (4 - len(leading))) - 1:
        return None
    packed = 0
    for value in leading:
        packed = (packed << 8) | value
    packed = (packed << (8 * (4 - len(leading)))) | last
    try:
        return ipaddress.IPv4Address(packed)
    except ValueError:
        return None


_LABEL_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")


def _is_valid_hostname(host: str) -> bool:
    """LDH syntax, and a final label that is not all digits.

    The numeric-TLD rule is a guard, not pedantry: ``2130706433`` and ``1.2.3.4.5`` are
    not names, and letting them through to the resolver is how a libc ``inet_aton``
    fallback turns "hostname" back into "loopback literal".
    """
    if not host or len(host) > 253:
        return False
    labels = host.split(".")
    if any(not label or len(label) > 63 for label in labels):
        return False
    for label in labels:
        if not set(label) <= _LABEL_OK:
            return False
        if label.startswith("-") or label.endswith("-"):
            return False
    return not labels[-1].isdigit()


def _is_denied_name(host: str) -> bool:
    if host in DENIED_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in DENIED_HOST_SUFFIXES)


# --------------------------------------------------------------------------------------
# Address classification
# --------------------------------------------------------------------------------------


def _unwrap(address: IPAddress) -> tuple[IPAddress, ...]:
    """Every address an IPv6 form actually reaches, including the one it embeds.

    ``::ffff:10.0.0.5`` and ``2002:0a00:0005::`` both reach ``10.0.0.5``. Judging only the
    outer form would admit both.
    """
    reached: list[IPAddress] = [address]
    if isinstance(address, ipaddress.IPv6Address):
        for embedded in (address.ipv4_mapped, address.sixtofour):
            if embedded is not None:
                reached.append(embedded)
        teredo = address.teredo
        if teredo is not None:
            reached.extend(teredo)
    return tuple(reached)


def address_refusal(address: IPAddress, policy: FetchPolicy = DEFAULT_POLICY) -> str | None:
    """``None`` if this address may be fetched, else a short reason it may not."""
    permitted = policy.permitted_networks()
    for candidate in _unwrap(address):
        if any(candidate.version == net.version and candidate in net for net in permitted):
            continue
        blocked = _BLOCKED_V4 if candidate.version == 4 else _BLOCKED_V6
        for net in blocked:
            if candidate in net:
                return f"blocked-network:{candidate}:{net}"
        if not candidate.is_global:
            return f"non-global-address:{candidate}"
    return None


def resolve_host(
    host: str, port: int, policy: FetchPolicy = DEFAULT_POLICY
) -> tuple[IPAddress, ...]:
    """Every address ``host`` resolves to, deduplicated and order-preserved.

    Raises:
        FetchRefused: the name does not resolve, or resolves to nothing usable.
    """
    try:
        infos = socket.getaddrinfo(host, port or None, type=socket.SOCK_STREAM)
    except OSError as exc:  # NXDOMAIN, SERVFAIL, no resolver at all
        raise FetchRefused(host, f"unresolvable:{exc}") from exc
    addresses: list[IPAddress] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        except ValueError:
            continue
        if address not in addresses:
            addresses.append(address)
        if len(addresses) >= policy.max_addresses:
            break
    if not addresses:
        raise FetchRefused(host, "unresolvable:no-addresses")
    return tuple(addresses)


# --------------------------------------------------------------------------------------
# The two frozen predicates
# --------------------------------------------------------------------------------------


def safe_split(url: object) -> SplitResult | None:
    """``urlsplit`` that answers ``None`` instead of raising on a URL that will not parse.

    ``urlsplit("http://[")`` raises ``ValueError: Invalid IPv6 URL``. Every URL this package
    handles is either a merchant's or a caller's, and one of them — a redirect ``Location``
    header — is chosen by the very party the guard exists to defend against. A parse that
    raises turns a refusal into a traceback, so nothing in the fetch path may call ``urlsplit``
    bare; call this instead and treat ``None`` as ``unparseable-url``.
    """
    try:
        return urlsplit(str(url or ""))
    except ValueError:
        return None


def fetch_verdict(url: str, *, policy: FetchPolicy | None = None) -> FetchVerdict:
    """Decide whether ``url`` may be fetched, and say why not when it may not.

    Opens no socket. Resolves names through :func:`socket.getaddrinfo` and judges the
    answer; the resolved addresses ride along on the verdict so the caller can pin them.
    """
    active = policy or DEFAULT_POLICY
    refuse = lambda reason: FetchVerdict(url, False, reason)  # noqa: E731

    try:
        split = urlsplit(str(url).strip())
    except ValueError as exc:
        return refuse(f"unparseable-url:{exc}")

    scheme = (split.scheme or "").lower()
    if scheme not in active.allowed_schemes:
        return refuse(f"scheme-not-allowed:{scheme or '(none)'}")

    if not active.allow_userinfo and ("@" in (split.netloc or "")):
        return refuse("userinfo-in-url")

    try:
        raw_host = split.hostname
        port = split.port
    except ValueError as exc:  # malformed port, e.g. "http://h:notaport/"
        return refuse(f"unparseable-authority:{exc}")

    if not raw_host:
        return refuse("no-host")
    host = normalise_host(raw_host)
    if not host:
        return refuse("no-host")

    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if not 0 < effective_port < 65536:
        return refuse(f"port-out-of-range:{effective_port}")
    if active.allowed_ports is not None and effective_port not in active.allowed_ports:
        return refuse(f"port-not-allowed:{effective_port}")

    if _is_denied_name(host):
        return refuse(f"denied-name:{host}")

    # An IP literal the strict parser accepts: judge it directly, never resolve it.
    literal: IPAddress | None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is None:
        # Not a canonical literal. Before believing it is a name, check whether libc
        # would read it as an address anyway (decision 2 in the module docstring).
        numeric = decode_numeric_ipv4(host)
        if numeric is not None:
            if not active.allow_numeric_host_forms:
                return refuse(f"obfuscated-ip-literal:{host}->{numeric}")
            literal = numeric

    if literal is not None:
        reason = address_refusal(literal, active)
        if reason:
            return refuse(reason)
        return FetchVerdict(url, True, "ok", host, effective_port, scheme, (literal,))

    if not _is_valid_hostname(host):
        return refuse(f"invalid-hostname:{host}")

    try:
        addresses = resolve_host(host, effective_port, active)
    except FetchRefused as exc:
        return refuse(exc.reason)

    # Every answer must be acceptable. One private address in a round-robin set is enough
    # to make the *next* connect land in private space, so a partial pass is a fail.
    for address in addresses:
        reason = address_refusal(address, active)
        if reason:
            return refuse(reason)

    return FetchVerdict(url, True, "ok", host, effective_port, scheme, addresses)


def is_fetch_allowed(url: str) -> bool:
    """True when ``url`` is a publicly-routable http(s) target this crawler may reach.

    The frozen one-argument form (SPEC C10). Use :func:`fetch_verdict` when the reason or
    the resolved addresses are wanted.
    """
    return bool(fetch_verdict(url).allowed)


def host_matches_allowlist(host: str, allowed_hosts: Iterable[str]) -> bool:
    """Exact host match, or a *proper* subdomain of an allow-listed host.

    Written out rather than expressed as ``in``/``startswith``/``endswith`` because each
    of those admits a spoof: ``store.example.com.attacker.tld`` starts with the allowed
    host, and ``evilstore.example.com`` ends with it. Only ``host == allowed`` or
    ``host`` ending in ``"." + allowed`` is a real match.
    """
    target = normalise_host(host)
    if not target:
        return False
    for entry in allowed_hosts:
        allowed = normalise_host(entry)
        if not allowed:
            continue
        if target == allowed or target.endswith("." + allowed):
            return True
    return False


def is_redirect_chain_allowed(
    chain: Sequence[str],
    allowed_hosts: Sequence[str],
    max_hops: int,
    *,
    policy: FetchPolicy | None = None,
) -> bool:
    """True when a redirect chain stayed short, on-host and publicly routable.

    Args:
        chain: every URL visited, starting with the original request.
        allowed_hosts: hosts the chain may touch (subdomains of these are permitted).
        max_hops: how many *redirects* are tolerated, i.e. ``len(chain) - 1``.

    A chain fails if it is empty, exceeds ``max_hops``, revisits a URL (a redirect loop
    burns budget without making progress), leaves the allow-list, or contains a hop the
    SSRF guard refuses — which is what stops a chain that starts on a public storefront
    and ends at ``169.254.169.254``.
    """
    hops = list(chain)
    if not hops:
        return False
    if max_hops < 0 or len(hops) - 1 > max_hops:
        return False

    seen: set[str] = set()
    for url in hops:
        verdict = fetch_verdict(url, policy=policy)
        if not verdict.allowed:
            return False
        if not host_matches_allowlist(verdict.host, allowed_hosts):
            return False
        key = str(url).strip().lower()
        if key in seen:
            return False
        seen.add(key)
    return True
