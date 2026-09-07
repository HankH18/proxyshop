"""Where the population is allowed to point, and why the default is not a convenience.

This package is a tool that **manufactures trust signal**. It posts synthetic buyer sentiment
through a real, unauthenticated route, and that sentiment becomes a row on an append-only ledger
which moves a store's published posture and therefore its eligibility to be solicited at all. A
ledger has no delete. So the failure mode here is not "the run errors out" — it is "a few
thousand fabricated opinions about real merchants, permanently, because a base URL was left in
an environment variable".

Two rules, and the second is the one that matters:

1. **The default is loopback and nothing else.** No environment variable, no config file and no
   discovered service address can move it. A default that could be moved from outside is a
   default that will be.
2. **A non-local host is refused unless the caller passed an explicit flag saying so.** Not a
   warning, not a prompt — a refusal, raised before the first byte leaves. The flag exists
   because there is a legitimate use (an operator driving a disposable demo stack on another
   host) and it is spelled ``--allow-remote`` so it appears in the shell history of whoever ran
   it.

What "local" means here is decided **without DNS**. A hostname is checked against a literal
allowlist and IP literals are checked with :mod:`ipaddress`; a name is never resolved to see
where it points. Resolving would be worse in three separate ways: it is a network call in a
package that must run offline (D3/C9), the answer can change between the check and the request
(a DNS rebind), and a name that resolves to a loopback address today is still a name somebody
else controls tomorrow. ``localhost`` is admitted as a literal because it is the one name the
platform itself resolves specially and the one this repo's own socket guard already allows.
"""

from __future__ import annotations

__all__ = [
    "LOCAL_HOSTNAMES",
    "PERMITTED_SCHEMES",
    "UnsafeTarget",
    "describe_target",
    "is_local_host",
    "require_local_target",
]

import ipaddress
from urllib.parse import urlsplit

#: The hostnames admitted as local without resolving anything. Literals only — see the module
#: docstring on why a name is never looked up. This is the same set the repository's own pytest
#: socket guard permits (``--allow-hosts=127.0.0.1,localhost,::1``), so a target this module
#: accepts is a target the test session can actually reach.
LOCAL_HOSTNAMES = frozenset({"localhost", "localhost."})

#: The schemes a base URL may use. ``file:``, ``unix:`` and the rest are refused because the
#: guard below reasons about hosts, and a scheme with no host would slip past a host check by
#: having nothing to check.
PERMITTED_SCHEMES = frozenset({"http", "https"})


class UnsafeTarget(ValueError):
    """The population was pointed somewhere it must not manufacture trust signal."""


def _host_of(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme.lower() not in PERMITTED_SCHEMES:
        raise UnsafeTarget(
            f"{url!r} is not an http(s) URL. This tool posts synthetic buyer sentiment onto an "
            f"append-only ledger; it addresses services over HTTP and refuses anything else "
            f"rather than reasoning about a scheme with no host to check."
        )
    if parts.username or parts.password:
        raise UnsafeTarget(
            f"{url!r} carries credentials in the URL. A tool that fabricates reputation must not "
            "also carry a credential that could make the fabrication authoritative."
        )
    host = (parts.hostname or "").strip()
    if not host:
        raise UnsafeTarget(f"{url!r} names no host, so there is nothing to check it against")
    return host


def is_local_host(host: str) -> bool:
    """Whether ``host`` is loopback, decided from the literal and never from DNS.

    An IP literal is checked with :mod:`ipaddress` — which covers the whole of ``127.0.0.0/8``
    and ``::1``, so ``127.0.0.2`` is local and ``127.0.0.1.example.com`` is not — and any other
    name must be in :data:`LOCAL_HOSTNAMES` exactly. Nothing here performs a lookup, so this
    function cannot be made to answer differently by anything outside the process.
    """
    name = host.strip().strip("[]").lower()
    if name in LOCAL_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def require_local_target(url: str, *, allow_remote: bool = False, what: str = "target") -> str:
    """Return ``url`` normalised, or refuse to let this population point at it.

    Args:
        url: the base URL of a service this population will drive.
        allow_remote: the caller's explicit statement that a non-local host is intended. This is
            the ``--allow-remote`` flag and nothing else sets it; there is deliberately no
            environment variable, because the whole value of the flag is that it appears in the
            command somebody typed.
        what: what the URL is, for the refusal message ("buyer service", "trust service").

    Raises:
        UnsafeTarget: the URL is unusable, or names a non-local host and ``allow_remote`` is
            False.

    A blank ``url`` is a refusal rather than a default. The caller that has nothing to say should
    be using :mod:`seed.local_stack`, which stands up its own services on loopback and hands back
    their real addresses; silently substituting an address here would be exactly the convenience
    default this module exists to remove.
    """
    text = str(url or "").strip()
    if not text:
        raise UnsafeTarget(
            f"no {what} URL was given. This tool has no fallback address on purpose — a "
            "convenience default for a thing that manufactures reputation is a footgun."
        )
    host = _host_of(text)
    if not is_local_host(host) and not allow_remote:
        raise UnsafeTarget(
            f"refusing to drive the {what} at {text!r}: {host!r} is not a loopback host. This "
            f"tool writes synthetic buyer feedback onto an append-only ledger that moves a "
            f"store's published trust posture, and there is no delete. Pass --allow-remote if "
            f"you really mean to point a feedback population at that host."
        )
    return text.rstrip("/")


def describe_target(url: str) -> str:
    """A one-line description of a target, for the report. Names whether it is loopback."""
    try:
        host = _host_of(url)
    except UnsafeTarget:
        return f"{url} (unusable)"
    return f"{url} ({'loopback' if is_local_host(host) else 'REMOTE'})"
