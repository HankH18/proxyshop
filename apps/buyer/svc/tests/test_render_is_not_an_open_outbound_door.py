"""``POST /buyer/shortlist/render`` is a door onto the platform's outbound HTTP. Bound it.

**Not SSRF.** ``buyer_svc.livecheck.targets.usable_page_url`` already decides WHERE a fetch may
go — https, port 443, no userinfo, the registered domain or a proper subdomain of it — and
``tests/test_livecheck_targets.py`` drives every one of those refusals through this same route.
What containment does not decide is *who* may point the platform, *how often*, or *at what*.
Before this file: anybody on the internet could make this service issue outbound HTTP to any of
the nineteen registered merchant domains, at a path of their own choosing, as many times as
they liked, without ever saying who they were. The merchant sees traffic from the platform.

The three bounds asserted here, and what each one closes:

1. **A credential is checked when one is sent.** A header that is present and is not a live
   session is a 401 that reaches no merchant — ``buyer_svc.feedback.routes``' posture, because
   a bad credential that fell through to "anonymous" would be a way to *choose* which
   allowance to spend.
2. **Dedup.** One captured request body replayed in a loop is the cheapest amplifier there is,
   and the second read of a page inside the window answers a question the first one answered.
3. **A per-``(caller, merchant)`` budget**, for the caller who defeats dedup by varying the
   query string. Every caller with no session shares ONE bucket, so the ceiling is a ceiling on
   the anonymous internet rather than a ceiling per attacker.

What is deliberately NOT asserted, because it would be false: that an anonymous render is
refused. Every shipped buyer surface renders a shortlist without a session
(``apps/buyer/app/journey/wire.ts`` sends ``content-type`` and nothing else), and the demo
deployment cannot issue one at all (``GET /buyer/auth/sign-in`` answers ``offered: false``
where no MTA is configured). A required credential here would 401 every real shopper. The
session gates what a render may COST a merchant, not whether it renders — and
``test_a_shortlist_still_renders_for_a_shopper_with_no_session`` is the guard on that.

Every case drives the two SERVED routes — ``POST /buyer/shortlist/render`` to enqueue and
``POST /buyer/livecheck/run`` to drain — and asserts on the URL list the FETCHER was handed. A
helper's return value is not evidence that a door is shut.

**No socket is opened here** (D3/C9). The fetcher is a counting wrapper around
``RecordedPageFetcher``, which reads the offline corpus in ``tests/livepages/``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from buyer_svc.livecheck import MAX_FETCHES_PER_ORIGIN
from buyer_svc.livecheck.fetching import FetchedPage, RecordedPageFetcher
from buyer_svc.livecheck.routes import LIVE_PAGE_FETCHER_ATTR
from buyer_svc.main import create_app
from fastapi.testclient import TestClient

from apps.buyer.svc.tests._fixtures_livecheck import (
    CORPUS,
    STORE_DOMAIN,
    STORE_ID,
    page_url,
    shortlist_body,
)

RENDER = "/buyer/shortlist/render"
RUN = "/buyer/livecheck/run"
SESSION_HEADER = "X-Buyer-Session"


class CountingFetcher:
    """Every URL this deployment was asked to read, in order. Reads the offline corpus."""

    def __init__(self) -> None:
        self._inner = RecordedPageFetcher(CORPUS)
        self.urls: list[str] = []

    def fetch(self, url: str) -> FetchedPage:
        self.urls.append(url)
        return self._inner.fetch(url)


def _document(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "exchange_url": "http://exchange.invalid",
        "registered_domains": {STORE_ID: STORE_DOMAIN},
        "live_page_fetcher": f"recorded:{CORPUS}",
    }
    body.update(overrides)
    return body


@pytest.fixture()
def fetcher() -> CountingFetcher:
    return CountingFetcher()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, fetcher: CountingFetcher) -> Any:
    monkeypatch.setenv("BUYER_DEPLOYMENT_JSON", json.dumps(_document()))
    app = create_app()
    # `buyer_svc.composition` never overwrites what is already on `app.state`, so this wins
    # over the document's own `recorded:` fetcher and counts what it is asked for.
    setattr(app.state, LIVE_PAGE_FETCHER_ATTR, fetcher)
    return TestClient(app)


@pytest.fixture()
def signed_in() -> Any:
    """A live buyer session, minted with NO mail transport of any kind.

    Which is the point as well as the convenience: ``MagicLinkAuth``'s ``deliver`` seam is the
    whole of the transport, so a session can be issued in a process that could not send an
    email if it wanted to. The service is installed through ``set_auth_service`` — the test
    seam ``buyer_svc.auth.routes`` documents — and removed again, because it is a process
    global and a session left standing would authenticate another module's test.
    """
    from buyer_svc.auth import MagicLinkAuth
    from buyer_svc.auth.routes import set_auth_service

    tokens: list[str] = []
    auth = MagicLinkAuth(deliver=lambda email, token, expires_at: tokens.append(token))
    set_auth_service(auth)
    auth.request_login("dana.reyes@example.com")
    session = auth.redeem(tokens[-1])
    try:
        yield {SESSION_HEADER: session.session_id}
    finally:
        set_auth_service(None)


def _render(client: Any, body: dict[str, Any], **kwargs: Any) -> Any:
    return client.post(RENDER, json={"shortlist": body}, **kwargs)


def _run(client: Any) -> dict[str, Any]:
    response = client.post(RUN)
    assert response.status_code == 200, response.text
    return response.json()


def _on_the_merchant(fetcher: CountingFetcher) -> list[str]:
    return [url for url in fetcher.urls if STORE_DOMAIN in url]


# ==============================================================================================
# The control: this file can see a fetch at all
# ==============================================================================================


def test_the_route_still_reads_a_merchant_page_when_it_is_allowed_to(
    client: Any, fetcher: CountingFetcher
) -> None:
    """Armed, and stated first.

    Every other case in this file asserts that a fetch did NOT happen, and a suite of those is
    green against a service that fetches nothing at all — a broken fixture, an unmounted
    router, a fetcher nobody bound. This is the case that fails if the evidence path is dead.
    """
    assert _render(client, shortlist_body(fixture="agrees.html")).status_code == 200
    _run(client)
    assert _on_the_merchant(fetcher) == [page_url("agrees.html")]


# ==============================================================================================
# 1. A credential is checked when one is sent
# ==============================================================================================


def test_a_credential_that_is_not_a_live_session_is_refused_and_reaches_no_merchant(
    client: Any, fetcher: CountingFetcher
) -> None:
    """401, and nothing is queued — not a 200 that quietly treats the caller as anonymous.

    A caller who sends a credential is telling the service who they are. Ignoring a bad one
    would mean the budget key is chosen by the attacker in exactly the case the attacker
    controls: send garbage, be billed to the anonymous bucket, or send garbage on somebody
    else's behalf and spend theirs.
    """
    response = _render(
        client,
        shortlist_body(fixture="agrees.html"),
        headers={SESSION_HEADER: "not-a-session"},
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "no live buyer session"

    _run(client)
    assert fetcher.urls == [], f"a refused caller still cost the merchant a read: {fetcher.urls}"


def test_a_shortlist_still_renders_for_a_shopper_with_no_session(
    client: Any, fetcher: CountingFetcher
) -> None:
    """The no-regression guard, and the reason auth here is optional rather than required.

    Nothing in this repository logs a buyer in before they reach a shortlist, and the shipped
    demo cannot log anyone in at all. A render with no header must therefore still be a 200
    carrying every labelled slot and its case — the credential decides what the render may cost
    a merchant, never whether the shopper sees their shortlist.
    """
    response = _render(client, shortlist_body(fixture="agrees.html"))
    assert response.status_code == 200, response.text

    [slot] = response.json()["slots"]
    assert slot["bid_ref"] == "bid-gaia-1"
    assert slot["store_domain"] == STORE_DOMAIN
    assert slot["provenance_labels"], "a slot rendered without a session lost its labels"
    assert slot["pitch"] is not None, "a slot rendered without a session lost its case"


# ==============================================================================================
# 2. Dedup
# ==============================================================================================


def test_the_same_render_repeated_does_not_re_issue_the_same_fetch(
    client: Any, fetcher: CountingFetcher
) -> None:
    """One captured body, sent in a loop: the cheapest amplifier, and the emptiest one."""
    body = shortlist_body(fixture="agrees.html")
    for _ in range(5):
        assert _render(client, body).status_code == 200
        _run(client)

    assert _on_the_merchant(fetcher) == [page_url("agrees.html")], (
        "the same shortlist rendered five times was read from the merchant "
        f"{len(_on_the_merchant(fetcher))} times: {fetcher.urls}"
    )


def test_a_page_the_platform_declined_to_re_read_says_so_on_the_record(client: Any) -> None:
    """Refused, not dropped. "We did not read this page" is served with its reason.

    The same rule the hostile-URL refusals follow: a screen that showed an unchecked slot and a
    checked one identically would be claiming evidence the platform does not have. So a budget
    refusal lands on the ledger beside them and comes back from
    ``GET /buyer/livecheck/{auction_id}``.
    """
    body = shortlist_body(fixture="agrees.html")
    assert _render(client, body).status_code == 200
    assert _render(client, body).status_code == 200

    served = client.get("/buyer/livecheck/auc-live-1")
    assert served.status_code == 200, served.text
    [refusal] = served.json()["refused"]
    assert refusal["store_id"] == STORE_ID
    assert refusal["bid_ref"] == "bid-gaia-1"
    assert "does not read it again" in refusal["reason"]


# ==============================================================================================
# 3. A per-(caller, merchant) budget
# ==============================================================================================


def test_the_whole_anonymous_internet_shares_one_budget_at_each_merchant(
    client: Any, fetcher: CountingFetcher
) -> None:
    """Vary the query string and dedup has nothing to match on. The budget still does.

    Forty distinct pages on one registered domain, rendered by a caller who never identifies
    themselves. Every one of them passes ``usable_page_url``; that is what makes this the
    interesting case rather than an SSRF one.
    """
    for index in range(40):
        body = shortlist_body(
            product_url=f"{page_url('agrees.html')}&cache-buster={index}",
            auction_id=f"auc-flood-{index}",
        )
        assert _render(client, body).status_code == 200
        _run(client)

    read = _on_the_merchant(fetcher)
    assert read, "the flood read nothing at all, so this case is not evidence of a bound"
    assert len(read) == MAX_FETCHES_PER_ORIGIN, (
        f"one anonymous caller drove {len(read)} reads at {STORE_DOMAIN} against a budget of "
        f"{MAX_FETCHES_PER_ORIGIN}"
    )


def test_a_signed_in_shopper_is_not_billed_to_the_anonymous_allowance(
    client: Any, fetcher: CountingFetcher, signed_in: dict[str, str]
) -> None:
    """The pseudonym is the budget's key, so a flood cannot spend a real buyer's allowance.

    An anonymous caller empties the merchant's anonymous budget first. A shopper holding a live
    session then renders one shortlist and their page is still read — from their own bucket.
    """
    for index in range(MAX_FETCHES_PER_ORIGIN + 8):
        body = shortlist_body(
            product_url=f"{page_url('agrees.html')}&flood={index}",
            auction_id=f"auc-flood-{index}",
        )
        assert _render(client, body).status_code == 200
    _run(client)
    spent = len(_on_the_merchant(fetcher))
    assert spent == MAX_FETCHES_PER_ORIGIN, f"the anonymous budget did not bind: {spent} reads"

    mine = shortlist_body(product_url=f"{page_url('agrees.html')}&mine=1", auction_id="auc-mine")
    assert _render(client, mine, headers=signed_in).status_code == 200
    _run(client)

    assert len(_on_the_merchant(fetcher)) == spent + 1, (
        "a signed-in shopper's page went unread because an anonymous flood had spent a budget "
        "that is not theirs"
    )

    # ...and the identity does not leak the other way. The caller travels from the route's
    # dependency to the handler through a `ContextVar` (see `buyer_svc.accept.routes._CALLER`),
    # which is a request-scoped channel only because an ASGI request is served in its own task
    # with its own copy of the context. If it were process-scoped instead, THIS render — which
    # carries no credential — would be billed to the pseudonym the previous one established,
    # find that bucket unspent, and be read. It is an R5 claim, so it is driven rather than
    # assumed.
    after = shortlist_body(product_url=f"{page_url('agrees.html')}&after=1", auction_id="auc-after")
    assert _render(client, after).status_code == 200
    _run(client)
    assert len(_on_the_merchant(fetcher)) == spent + 1, (
        "an anonymous render was served out of the previous caller's allowance, so the caller "
        "is leaking between requests"
    )
