"""A hostile redirect target cannot escape the fetch guard as an exception (T-238).

The repro gate next door asserts the one property the ticket is about: a ``Location`` of
``http://[`` produces a ``FetchRefused`` rather than a ``ValueError``. These tests pin the
rest of the contract around that repair, which a single gate cannot:

* the refusal speaks the vocabulary the guard already uses for an entry URL that will not
  parse (``unparseable-url:…``) rather than inventing a second one;
* every *other* redirect behaviour is unchanged — relative Locations still resolve, loops are
  still detected on the resolved string, and a redirect into blocked address space is still
  refused for the network reason and not for a parse reason;
* the exception does not escape ``fetch_catalog``, whose docstring promises it "never raises
  for an ordinary crawl outcome" and which catches only ``(FetchRefused, TransportError)``.

That last one is the reason this is a ticket rather than a two-line patch: before the fix the
``ValueError`` went straight through the adapter, so a store could abort a crawl mid-way with
four characters in a header.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import pytest
from ingest.adapters import (
    CatalogRequest,
    FetchPolicy,
    FetchRefused,
    SafeHTTPClient,
    SignedFetchAdapter,
)
from ingest.adapters.netguard import fetch_verdict, safe_split

from proxyshop_support.asgi_server import serve

#: Loopback is not publicly routable, so the default posture refuses an in-process server.
LOOPBACK = FetchPolicy(allowed_ports=None, extra_allowed_networks=("127.0.0.0/8",))

#: The four characters. ``urlsplit`` reads the ``[`` as the start of an IPv6 literal and
#: raises when it never closes.
MALFORMED = "http://["


def _client() -> SafeHTTPClient:
    return SafeHTTPClient(policy=LOOPBACK)


class _AlwaysRedirects:
    """A store that answers every request with a 302 to ``location``.

    Raw ASGI for the same reason the repro file's ``_TwoFacedStore`` is: the property under
    test is a wire-level one, and the shared ``StorefrontStub`` cannot serve a malformed
    ``Location`` on the catalog path.
    """

    def __init__(self, location: str) -> None:
        self.location = location
        self.paths: list[str] = []

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        while True:
            message = await receive()
            if not message.get("more_body"):
                break

        self.paths.append(scope["path"])
        await send(
            {
                "type": "http.response.start",
                "status": 302,
                "headers": [
                    (b"content-type", b"text/plain"),
                    (b"location", self.location.encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})


# ---------------------------------------------------------------------------------------
# the guarded parse itself
# ---------------------------------------------------------------------------------------


def test_the_bare_parse_this_guard_replaces_really_does_raise() -> None:
    """Arming control. Everything below is worthless if ``urlsplit`` stopped raising.

    Python's own behaviour is the premise of the whole ticket, so it is asserted rather than
    assumed — a future interpreter that returned a ``SplitResult`` for ``http://[`` would make
    every test in this file pass while measuring nothing at all.
    """
    with pytest.raises(ValueError, match="Invalid IPv6 URL"):
        urlsplit(MALFORMED)


def test_safe_split_answers_none_where_urlsplit_raises() -> None:
    assert safe_split(MALFORMED) is None
    assert safe_split("https://store.example.com/products.json") is not None
    assert safe_split(None) is not None, "an empty URL parses to an empty result, not to None"


def test_the_entry_url_refusal_already_speaks_this_vocabulary() -> None:
    """The reason the redirect guard reuses. Quoted here so the two cannot drift apart."""
    verdict = fetch_verdict(MALFORMED, policy=LOOPBACK)
    assert not verdict.allowed
    assert verdict.reason.startswith("unparseable-url:"), verdict.reason


# ---------------------------------------------------------------------------------------
# the redirect path
# ---------------------------------------------------------------------------------------


def test_a_malformed_location_is_refused_with_the_unparseable_url_reason(
    storefront: Any,
) -> None:
    """A refusal a caller cannot classify is barely better than a traceback."""
    base_url, stub = storefront
    stub.redirect_target = MALFORMED

    with pytest.raises(FetchRefused) as raised:
        _client().fetch(f"{base_url}/redirect/custom")

    assert raised.value.reason.startswith("unparseable-url:"), raised.value.reason
    assert raised.value.url == MALFORMED, (
        "the refusal names the URL it was raised for, and the offending one is the Location "
        f"header, not the page that served it: {raised.value.url!r}"
    )


def test_a_relative_location_still_resolves_against_the_current_url(storefront: Any) -> None:
    """The guard must not change what a *parseable* join produces.

    The redirect-loop check keys on the resolved string and the allow-list check reads its
    host, so a join that returned anything different here would move behaviour the T-020
    suite asserts on — which is precisely why this repair was a ticket.
    """
    base_url, stub = storefront
    stub.redirect_target = "/robots.txt"

    result = _client().fetch(f"{base_url}/redirect/custom")

    assert result.status == 200
    assert result.final_url == f"{base_url}/robots.txt", result.final_url
    assert result.redirect_chain == (f"{base_url}/redirect/custom", f"{base_url}/robots.txt")


def test_a_malformed_location_does_not_escape_fetch_catalog() -> None:
    """``fetch_catalog`` never raises for an ordinary crawl outcome — including this one.

    ``SignedFetchAdapter`` catches ``(FetchRefused, TransportError)`` around every fetch, so
    before the repair the ``ValueError`` went straight through it and aborted the crawl. Now
    it is one more refused resource, reported in ``warnings`` with the catalog gathered so far.
    """
    store = _AlwaysRedirects(MALFORMED)
    with serve(store) as hostile_url:
        snapshot = SignedFetchAdapter().fetch_catalog(
            CatalogRequest(
                store_id="store-1",
                base_url=hostile_url,
                policy=LOOPBACK,
                fetch_product_pages=False,
            )
        )

    assert store.paths, "the crawl never reached the hostile store"
    assert snapshot.products == ()
    assert any("unparseable-url" in warning for warning in snapshot.warnings), (
        f"the refused redirect was not reported as a warning: {snapshot.warnings}"
    )


def test_a_redirect_into_blocked_space_is_still_refused_for_the_network_reason(
    storefront: Any,
) -> None:
    """The guard must keep refusing what it refused before, and for the same stated reason.

    A repair that answered ``unparseable-url`` for everything would pass the ticket's gate and
    destroy the diagnosis that makes an SSRF refusal auditable.
    """
    base_url, stub = storefront
    stub.redirect_target = "http://169.254.169.254/latest/meta-data/"

    with pytest.raises(FetchRefused) as raised:
        _client().fetch(f"{base_url}/redirect/custom")

    assert raised.value.reason.startswith("blocked-network:169.254.169.254"), raised.value.reason
