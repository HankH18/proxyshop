"""The exchange, driven as it BOOTS: no test-side ``configure_*`` call anywhere.

Run it on its own::

    PROXYSHOP_WORKER=3 .venv/bin/python -m pytest \\
        apps/exchange/tests/test_composition_root.py -q

Why this file exists, and what makes it different from every other green test here
----------------------------------------------------------------------------------
The exchange's components are real, individually correct and heavily tested. Nothing joined
them. ``exchange.main.create_app()`` boots an app whose five collaborators are all at their
fail-closed defaults, and the measured result — on the app exactly as ``uvicorn
exchange.main:app`` serves it — was::

    POST /auctions -> 201 {"solicited": [], "entries": [], "ranked": [],
                           "shortlist": {"slots": []},
                           "denied": [{"store_id": "s1", "status": "unavailable", ...}]}
    POST /auctions/{id}/accept -> 409 {"denial_reason": "unknown_bid: ... nothing to accept"}

Every green demonstration in this repository supplies the join whose absence is the defect.
``e2e/test_s1_flow.py`` passes 27 assertions, and ``e2e/support/s1/flow.py:369`` calls
``configure_auctions(app, machine=..., solicitor=..., eligibility=...)`` itself before the
first request; ``_t294_corpus`` in ``test_repro_open_tickets.py`` calls both
``configure_auctions`` and ``configure_accept``. A test that wires the app it is testing is
measuring the wiring it wrote — so the one property this file asserts, and the reason it is a
separate file rather than three cases added to an existing one, is:

    **the only thing this module calls to build the app is ``create_app()``.**

No test here that drives a request calls a ``configure_*`` function. Everything the app needs,
it reads for itself out of the deployment document the composition root is pointed at — which
is exactly what a person running the service does, and is the thing that was missing.

Stated exactly, because an overclaiming docstring is how the next reader stops looking:
``configure_exchange`` — the composition root's OWN function, not one of the three route
seams — is called directly by
``test_the_composition_root_never_overwrites_wiring_a_deployment_already_chose``, which is a
unit test of that function and issues no request. Every HTTP test in this file goes through
the ``deployed`` fixture or :func:`served_exchange`, and neither wires anything.

What is real here
-----------------
* the app: ``exchange.main.create_app()``, mounting the routers it globs at import;
* the store agent: a real ASGI service on a real loopback port (D40, port 0), answering the
  published ``POST /v1/bid-requests`` door over HTTP, reached by the exchange's own
  ``HttpBidSolicitor``. It is a stand-in for ``packages/store-agent`` — deliberately, because
  the offer fields this file needs (``checkout_url``, ``expires_at``) are landing in that
  package in parallel, and an acceptance test for the *exchange's* composition root must not
  be able to fail for the store agent's reasons;
* the eligibility gate, the trust-snapshot read, the registered-domain check, the published
  ranking, the shortlist, the bid book, the R12 re-read on accept, and the mint — all of them
  the product's own, reached over HTTP.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from exchange.accept.offer import use_registered_domains
from exchange.composition import ENV_DEPLOYMENT, ENV_DEPLOYMENT_JSON
from exchange.main import create_app
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from proxyshop_support.asgi_server import serve

#: How long a request to the served exchange may take. Generous against the auction's own
#: 3-second bid window plus two loopback hops, and finite so a wedged server fails the test
#: instead of hanging the suite.
REQUEST_TIMEOUT_SECONDS = 30.0

#: The two sellers this deployment registers. The domains are the PLATFORM's statement about
#: which host each store checks out on; the bid endpoints are where its agent answers.
STORES: tuple[dict[str, Any], ...] = (
    {"store_id": "s1", "domain": "s1.example.com", "list_price": 100.0, "unit_price": 88.0},
    {"store_id": "s2", "domain": "s2.example.com", "list_price": 120.0, "unit_price": 111.0},
)

#: A published-shaped ``Intent``. ``hard_constraints`` is present and empty on purpose: the
#: schema requires the field (``protocol.schema.json`` lists it in ``Intent.required``), and
#: an intent that OMITS it excludes every candidate ``undecidable_hard_constraint`` — the
#: exchange declines to tell an unconstrained request from an unread one. `[]` is the
#: "this buyer stated no must-haves" answer, which is the one this run is about.
INTENT: dict[str, Any] = {
    "intent_id": "intent-composition-1",
    "cluster_id": "cluster-1",
    "query": "a demonstration purchase",
    "hard_constraints": [],
    "preferences": [],
    "created_at": "2026-01-01T00:00:00Z",
    "schema_version": "1.0.0",
}


def _store_agent_app() -> FastAPI:
    """A real ASGI store agent: one published ``POST /v1/bid-requests`` door.

    It answers the published ``Bid`` shape, including the two offer fields a bid needs to
    survive the exchange's own filters — a future ``expires_at`` and a ``checkout_url`` on the
    host the platform registered for that store. It reads the ``BidRequest``'s ``auction_id``
    off the body, so a solicitor that failed to send one produces a bid the exchange can see
    is wrong rather than a silently different scenario.
    """
    app = FastAPI(title="store-agent-double")

    def door_for(row: dict[str, Any]) -> Any:
        # A closure, never a defaulted parameter: FastAPI reads a defaulted `dict` argument as
        # a SECOND embedded body field, so `def door(body, row=row)` answers 422 to a
        # well-formed BidRequest and every store reads as silent. Measured, and it cost a run.
        def door(body: dict[str, Any]) -> JSONResponse:
            return JSONResponse(
                status_code=200,
                content={
                    "auction_id": str(body.get("auction_id") or ""),
                    "store_id": row["store_id"],
                    "offer": {
                        "product_ref": "prod-1",
                        "unit_price": row["unit_price"],
                        "currency": "USD",
                        "commitments": [],
                        "total_price": row["unit_price"],
                        "expires_at": "2999-01-01T00:00:00Z",
                        "checkout_url": f"https://{row['domain']}/cart/44352913:1",
                    },
                    "claims": [],
                    "message": None,
                    "agent_version": "store-agent-double/1.0.0",
                    "signature": None,
                    "schema_version": "1.0.0",
                },
            )

        return door

    # Each store gets its own path, which is how the registry tells them apart.
    for row in STORES:
        app.post(f"/{row['store_id']}/v1/bid-requests")(door_for(dict(row)))
    return app


def _deployment_document(agent_base_url: str) -> dict[str, Any]:
    """The document a person writes to deploy this exchange."""
    return {
        "sellers": [
            {
                "store_id": row["store_id"],
                "eligibility": "eligible",
                "registered_domain": row["domain"],
                "bid_endpoint": f"{agent_base_url}/{row['store_id']}/v1/bid-requests",
            }
            for row in STORES
        ],
        # The trust service's projection. Independent of the seller registry above by design:
        # R12's eligibility and the ranking's blacklist read are two different questions, and
        # a composition root that manufactured one from the other would be inventing an answer
        # the trust service never gave.
        "trust_snapshot": {
            "stores": {
                row["store_id"]: {
                    "store_id": row["store_id"],
                    "blacklisted": False,
                    "score": 0.8,
                }
                for row in STORES
            }
        },
        "checkout_mode": "redirect",
    }


@pytest.fixture
def unwired() -> Iterator[None]:
    """Leave the process-wide registered-domain seam exactly as this file found it.

    ``configure_accept(registered_domains=...)`` — which the composition root reaches through
    — also turns a module-level seam, so a file that wires an app and does not restore it
    changes what every later test in the process reads. Same fixture, same reason, as
    ``test_accept_routes.py``'s.
    """
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


@pytest.fixture(scope="module")
def agent_url() -> Iterator[str]:
    """The store agents, on a real loopback port (D40: port 0, reported back)."""
    with serve(_store_agent_app()) as url:
        yield url


@contextmanager
def served_exchange() -> Iterator[httpx.Client]:
    """The exchange as a **served process**, reached over a real TCP socket.

    ``TestClient`` would satisfy the letter of this file: it drives the same ASGI callable and
    would exercise the same composition root. It is not what is used, because in-process ASGI
    transport is exactly the boundary at which this class of defect hides — the app object is
    handed to the test rather than started, so nothing between ``create_app()`` and a served
    request is ever executed. ``proxyshop_support.asgi_server.serve`` starts a real
    ``uvicorn.Server`` on an OS-assigned port (D40), which is what ``apps/merchant`` already
    does for its own deployable and what the exchange had no test doing at all.

    The environment is read at request time by the composition root, so a caller sets it
    before entering this block and the served app picks it up.
    """
    with serve(create_app()) as url:
        with httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            yield client


@pytest.fixture
def deployed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unwired: None, agent_url: str
) -> Iterator[httpx.Client]:
    """A client onto the real served app, configured the way a deployment configures it.

    The ONLY things this fixture does are: write the deployment document, point the
    environment at it, and start ``create_app()``.
    """
    document = tmp_path / "deployment.json"
    document.write_text(json.dumps(_deployment_document(agent_url), indent=2), encoding="utf-8")
    monkeypatch.setenv(ENV_DEPLOYMENT, str(document))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

    with served_exchange() as client:
        yield client


def _open_an_auction(client: httpx.Client) -> dict[str, Any]:
    response = client.post(
        "/auctions",
        json={
            "intent": INTENT,
            "profile": {"pseudonym": "psn-composition-1", "buckets": {}},
            "roster": [
                {
                    "store_id": row["store_id"],
                    "tier": 1,
                    "product_ref": "prod-1",
                    "list_price": row["list_price"],
                    "max_discount_pct": 20.0,
                }
                for row in STORES
            ],
        },
    )
    assert response.status_code == 201, f"POST /auctions -> {response.status_code}: {response.text}"
    return response.json()


# =====================================================================================
# The acceptance criteria
# =====================================================================================
def test_a_booted_exchange_ranks_the_bids_it_solicited(deployed: httpx.Client) -> None:
    """``POST /auctions`` answers with a non-empty ``ranked`` AND a non-empty shortlist.

    Both, because they fail independently. ``ranked`` is empty when a candidate is excluded by
    any of the eligibility filters — the blacklist read, the offer's expiry, the checkout
    domain — and ``shortlist.slots`` is what a buyer is actually shown. An assertion on only
    the first would pass on a ranking that reached no buyer.

    The failure message names every exclusion reason, because the six ways this can come back
    empty are six different missing collaborators and "0 ranked" tells an operator none of them
    apart.
    """
    body = _open_an_auction(deployed)

    assert body["denied"] == [], (
        f"the R12 gate denied a rostered store, so no bid was ever solicited: {body['denied']}"
    )
    assert [entry["store_id"] for entry in body["entries"]] == [row["store_id"] for row in STORES]
    assert [entry["fallback"] for entry in body["entries"]] == [False, False], (
        "a store answered with no usable bid, so the exchange manufactured its list-price "
        f"fallback instead — the solicitor never reached the agent: {body['entries']}"
    )

    excluded = {row["bid_ref"]: row["exclusion_reasons"] for row in body["excluded"]}
    assert body["ranked"], f"nothing was ranked; the filters excluded {excluded}"
    assert body["shortlist"]["slots"], (
        f"the ranking produced {len(body['ranked'])} row(s) and no shortlist slot; "
        f"exclusions were {excluded}"
    )
    assert len(body["ranked"]) == len(STORES)
    assert len(body["shortlist"]["slots"]) == len(STORES)

    # The cheaper store leads: with four of the five published features absent on a served
    # candidate, the score is a function of trust alone, so equal-trust stores tie and D13's
    # published tie-breaks decide — `price` before `bid_id`.
    assert body["ranked"][0]["store_id"] == "s1"


def test_a_booted_exchange_accepts_its_own_shortlists_top_bid(deployed: httpx.Client) -> None:
    """The shortlist's own top ``bid_ref`` is accepted, and answers with a real code.

    This is the half that ``POST /auctions`` used to drop on the floor: it collected a
    ``BidEntry`` list, rendered it into ``entries`` and kept none of it, so
    ``app.state.auction_bids`` stayed on ``NoRecordedBids`` and every accept of a bid the
    exchange had itself just published was refused ``unknown_bid`` (T-294).

    Asserted as an HTTP 200 carrying a code and a permalink rather than as "the denial is not
    ``unknown_bid``", because a refusal's code word is one relabelling away from anything, and
    a discount code is not.
    """
    body = _open_an_auction(deployed)
    top = body["shortlist"]["slots"][0]

    response = deployed.post(
        f"/auctions/{body['auction_id']}/accept", json={"bid_ref": top["bid_ref"]}
    )
    assert response.status_code == 200, (
        f"accepting the shortlist's own top bid {top['bid_ref']!r} was refused "
        f"{response.status_code}: {response.text}"
    )
    payload = response.json()

    assert payload["code"], f"a 200 with no discount code: {payload}"
    assert payload["code"].startswith("PSX-"), payload["code"]
    assert payload["permalink_url"].startswith("https://s1.example.com/cart/"), payload
    assert f"discount={payload['code']}" in payload["permalink_url"], (
        "the permalink the buyer is handed does not carry the code that was minted for it: "
        f"{payload}"
    )

    # The auction really moved, in the store the machine writes to — not only on the object
    # `accept()` was handed (T-158).
    state = deployed.get(f"/auctions/{body['auction_id']}").json()
    assert state["state"] == "accepted"
    assert state["accepted_bid_ref"] == top["bid_ref"]


def test_a_booted_exchange_refuses_a_bid_it_never_published(deployed: httpx.Client) -> None:
    """A recorded bid book must be able to say no, or it is a rubber stamp.

    Without this, "the exchange records its bids" and "the exchange accepts any reference it
    is handed" are indistinguishable, and the 200 above proves nothing.
    """
    body = _open_an_auction(deployed)

    response = deployed.post(
        f"/auctions/{body['auction_id']}/accept", json={"bid_ref": "bid-never-published"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["denial_reason"].startswith("unknown_bid:"), response.json()

    # And the auction is still acceptable afterwards: a refusal must not spend the field.
    top = body["shortlist"]["slots"][0]
    accepted = deployed.post(
        f"/auctions/{body['auction_id']}/accept", json={"bid_ref": top["bid_ref"]}
    )
    assert accepted.status_code == 200, accepted.text


def test_one_auction_yields_one_discount_code(deployed: httpx.Client) -> None:
    """A second accept on the same auction mints no second code (R3/A5)."""
    body = _open_an_auction(deployed)
    slots = body["shortlist"]["slots"]

    first = deployed.post(
        f"/auctions/{body['auction_id']}/accept", json={"bid_ref": slots[0]["bid_ref"]}
    )
    assert first.status_code == 200, first.text

    second = deployed.post(
        f"/auctions/{body['auction_id']}/accept", json={"bid_ref": slots[1]["bid_ref"]}
    )
    assert second.status_code == 409, second.text
    assert "code" not in second.json(), second.json()


def test_every_ranked_bid_is_one_the_auction_can_be_asked_to_accept(
    deployed: httpx.Client,
) -> None:
    """Not only the top slot: every reference this auction published resolves.

    The single-slot assertion above is satisfied by a book holding exactly one row, which is
    indistinguishable from a book that happens to key on whatever the shortlist put first.
    Each ref is tried on its OWN auction — one accept per auction — because a second accept is
    refused ``already_accepted`` for reasons that have nothing to do with the bid book.
    """
    refs = [row["bid_ref"] for row in _open_an_auction(deployed)["ranked"]]
    assert len(refs) == len(STORES), f"the auction published {len(refs)} refs"

    for ref in refs:
        store_id = ref.split(":", 1)[1]
        body = _open_an_auction(deployed)
        matching = [row["bid_ref"] for row in body["ranked"] if row["store_id"] == store_id]
        assert matching, f"{ref} names a store this auction did not rank"
        response = deployed.post(
            f"/auctions/{body['auction_id']}/accept", json={"bid_ref": matching[0]}
        )
        assert response.status_code == 200, (
            f"a bid the exchange published in its own `ranked` list was refused "
            f"{response.status_code}: {response.text}"
        )


def test_the_reference_a_store_minted_for_its_own_bid_also_resolves(
    deployed: httpx.Client,
) -> None:
    """A buyer's agent is told a bid's reference by the store that made it.

    ``BidEntry.bid['bid_id']`` carries that reference through ``collect_bids``, and the
    exchange publishes only its OWN minted spelling — so an accept presenting the store's
    reference was refused ``unknown_bid`` for a bid the auction really did collect. Both
    spellings now resolve, and a colliding one is dropped rather than honoured.

    The store agents in this file mint no reference of their own, so this drives the recording
    function directly against an entry that does. It is the one assertion here that is not an
    HTTP round trip, and it is worth having as itself rather than folded into one.
    """
    from exchange.auction.routes import collected_bid_records

    class _Entry:
        store_id = "s1"
        bid = {"bid_id": "bid-from-the-store-9f2c", "offer": {"unit_price": 88.0}}

    records = collected_bid_records(
        [
            {
                "bid_id": "auction-x:s1",
                "store_id": "s1",
                "store_domain": "s1.example.com",
                "offer": {"unit_price": 88.0},
            }
        ],
        [_Entry()],
    )
    refs = [record["bid_id"] for record in records]
    assert refs == ["auction-x:s1", "bid-from-the-store-9f2c"]
    # The alias carries the PLATFORM's domain, never the bid's own claim about itself (T-169).
    assert {record["store_domain"] for record in records} == {"s1.example.com"}


def test_a_reference_two_stores_both_claim_is_honoured_for_neither(
    deployed: httpx.Client,
) -> None:
    """``_find_bid`` returns the FIRST match, so a colliding alias is a lever.

    Two stores in one auction both minting ``bid-collide`` would otherwise let whichever was
    rostered first decide which offer the other's reference accepts. Neither is recorded; both
    stores keep the exchange's own minted reference, which no bidder can choose.
    """
    from exchange.auction.routes import collected_bid_records

    class _Entry:
        def __init__(self, store_id: str) -> None:
            self.store_id = store_id
            self.bid = {"bid_id": "bid-collide", "offer": {}}

    records = collected_bid_records(
        [
            {"bid_id": "auction-x:s1", "store_id": "s1", "offer": {}},
            {"bid_id": "auction-x:s2", "store_id": "s2", "offer": {}},
        ],
        [_Entry("s1"), _Entry("s2")],
    )
    assert [record["bid_id"] for record in records] == ["auction-x:s1", "auction-x:s2"]


# =====================================================================================
# The composition root itself — configured, unconfigured, and misconfigured
# =====================================================================================
def test_an_unconfigured_exchange_still_refuses_everything(
    monkeypatch: pytest.MonkeyPatch, unwired: None
) -> None:
    """No deployment configured must mean EXACTLY today's fail-closed behaviour.

    This is the property the composition root may not break. An exchange nobody has connected
    to a seller registry, a trust service or a store agent has to deny every store rather than
    quietly admit any — R12's rule applied to the deployment — and a composition root that
    invented a permissive default to make a demo work would have moved the defect rather than
    fixed it.
    """
    monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

    with served_exchange() as client:
        body = _open_an_auction(client)

    assert [denial["status"] for denial in body["denied"]] == ["unavailable"] * len(STORES)
    assert body["entries"] == []
    assert body["ranked"] == []
    assert body["shortlist"]["slots"] == []


def test_an_inline_deployment_document_configures_the_same_exchange(
    monkeypatch: pytest.MonkeyPatch, unwired: None, agent_url: str
) -> None:
    """A container may set the document instead of mounting it."""
    monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)
    monkeypatch.setenv(ENV_DEPLOYMENT_JSON, json.dumps(_deployment_document(agent_url)))
    with served_exchange() as client:
        body = _open_an_auction(client)

    assert body["ranked"], body["excluded"]
    assert body["shortlist"]["slots"]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        pytest.param(
            {"sellers": [{"store_id": "s1", "registered_domain": "s1.example.com"}]},
            "states no 'eligibility'",
            id="eligibility-omitted",
        ),
        pytest.param(
            {"sellers": [{"store_id": "s1", "eligibility": "probably-fine"}]},
            "is not one of",
            id="eligibility-unrecognised",
        ),
        pytest.param(
            {
                "sellers": [
                    {
                        "store_id": "s1",
                        "eligibility": "eligible",
                        "registered_domain": "https://s1.example.com",
                    }
                ]
            },
            "must be a bare host",
            id="domain-carries-a-scheme",
        ),
        pytest.param(
            {"trust_snapshot": {"s1": {"blacklisted": 0}}},
            "unreadable blacklist",
            id="blacklist-flag-is-not-a-bool",
        ),
        pytest.param(
            {"checkout_mode": "definitely-real"},
            "no registered provider",
            id="unregistered-checkout-mode",
        ),
    ],
)
def test_a_misconfigured_deployment_is_a_503_that_names_the_problem(
    monkeypatch: pytest.MonkeyPatch,
    unwired: None,
    mutation: dict[str, Any],
    expected: str,
) -> None:
    """Every one of these mutations produces an EMPTY SHORTLIST if it is tolerated.

    That is the whole argument for refusing them here. A registered domain written with a
    scheme matches no checkout URL, so every candidate is excluded ``off_domain_checkout``; a
    blacklist flag written as ``0`` reads as *unreadable*, which denies; an unrecognised
    eligibility word denies. All three produce a 201 with an empty shortlist and an exclusion
    reason that reads like a policy decision about the stores rather than a typo in a file.

    So the document is validated at the door, and the answer is a 503 naming the row — the
    same status this service already gives for an unregistered ``CHECKOUT_MODE``, because a
    misconfigured deployment is not a decision about the buyer.
    """
    document = _deployment_document("http://127.0.0.1:1")
    document.update(mutation)
    monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)
    monkeypatch.setenv(ENV_DEPLOYMENT_JSON, json.dumps(document))

    with served_exchange() as client:
        response = client.post("/auctions", json={"intent": INTENT, "roster": []})

    assert response.status_code == 503, f"{response.status_code}: {response.text}"
    assert expected in response.json()["detail"], response.json()["detail"]


def test_a_deployment_file_that_does_not_exist_is_refused_rather_than_ignored(
    monkeypatch: pytest.MonkeyPatch, unwired: None, tmp_path: Path
) -> None:
    """A named-but-missing registry is a misconfiguration, not an unconfigured exchange.

    Answering it fail-closed would be indistinguishable from the correct behaviour for an
    exchange nobody configured — which is precisely how a deployment ends up serving empty
    shortlists for a week without anybody knowing the path was wrong.
    """
    monkeypatch.setenv(ENV_DEPLOYMENT, str(tmp_path / "not-here.json"))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

    with served_exchange() as client:
        response = client.post("/auctions", json={"intent": INTENT, "roster": []})

    assert response.status_code == 503, response.text
    assert "could not be read" in response.json()["detail"]


def test_the_composition_root_never_overwrites_wiring_a_deployment_already_chose(
    monkeypatch: pytest.MonkeyPatch, unwired: None
) -> None:
    """Anything already on ``app.state`` outranks the document.

    ``ensure_configured`` runs at the top of a request, which is *after* any caller that built
    the app has had its say. If it overwrote what it found, a deployment that wires its own
    Postgres-backed bid book or its own live eligibility client would silently lose it to a
    file, and the seam would be worse than no seam.
    """
    from exchange.composition import configure_exchange, read_deployment

    monkeypatch.setenv(ENV_DEPLOYMENT_JSON, json.dumps(_deployment_document("http://127.0.0.1:1")))
    monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)
    deployment = read_deployment()
    assert deployment is not None

    app = create_app()
    chosen = object()
    app.state.seller_eligibility = chosen
    app.state.trust_snapshot = chosen

    bound = configure_exchange(app, deployment)

    assert app.state.seller_eligibility is chosen
    assert app.state.trust_snapshot is chosen
    assert "seller_eligibility" not in bound
    assert "trust_snapshot" not in bound
    # …and it did bind the rest.
    assert "ranking_registered_domains" in bound
    assert "registered_domains" in bound
    assert "bid_solicitor" in bound


def test_a_store_agent_cannot_spend_the_exchanges_memory_on_one_reply() -> None:
    """An oversized reply is refused while it is still on the wire, not after it is in RAM.

    The store agent is a third party, the auction path is unauthenticated and the container is
    capped at 256 MiB, so the size of a bid is the size of an attack. Measured with
    ``response.json()`` and a 64 MiB reply: peak RSS 115.9 -> 464.6 MiB, answer ACCEPTED. The
    agent here STREAMS its filler so nothing but the solicitor holds it, and the assertion is
    that the reply is refused — which sends the store to its list-price fallback (R10) and
    leaves the auction intact.
    """
    from exchange.composition import MAX_BID_RESPONSE_BYTES, HttpBidSolicitor
    from fastapi.responses import StreamingResponse

    chunk = b"x" * (64 * 1024)
    oversized = (MAX_BID_RESPONSE_BYTES // len(chunk)) + 4

    agent = FastAPI(title="a store agent that will not stop talking")

    @agent.post("/v1/bid-requests")
    def door(body: dict[str, Any]) -> StreamingResponse:
        def stream() -> Iterator[bytes]:
            yield b'{"store_id":"s1","claims":[],"offer":{"unit_price":1.0,"product_ref":"'
            for _ in range(oversized):
                yield chunk
            yield b'"}}'

        return StreamingResponse(stream(), media_type="application/json")

    @agent.post("/small/v1/bid-requests")
    def polite(body: dict[str, Any]) -> JSONResponse:
        return JSONResponse(
            status_code=200,
            content={"store_id": "s1", "claims": [], "offer": {"unit_price": 1.0}},
        )

    with serve(agent) as url:
        solicitor = HttpBidSolicitor(
            {"loud": f"{url}/v1/bid-requests", "polite": f"{url}/small/v1/bid-requests"}
        )
        bound = solicitor.for_auction(auction_id="a1", intent={}, profile={}, respond_by=0.0)
        assert bound.solicit({"store_id": "loud"}) is None
        # The control: the same client, the same auction, a reply inside the bound. Without
        # this the assertion above is satisfied by a solicitor that answers None to everything.
        assert bound.solicit({"store_id": "polite"}) is not None
