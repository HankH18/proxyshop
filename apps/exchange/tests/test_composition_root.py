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
``test_the_composition_root_never_overwrites_wiring_a_deployment_already_chose`` and by
``test_a_deployment_states_where_its_trust_service_answers``, and
``exchange.auction.routes._machine`` by
``test_an_exchange_nobody_configured_still_writes_its_transitions_at_the_trust_service``. All
three are unit tests of those functions and none of them issues a request. Every HTTP test in
this file goes through the ``deployed`` fixture or :func:`served_exchange`, and neither wires
anything.

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
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
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
                # The ranking's own verdict. Only an ELIGIBLE candidate is recorded — a
                # candidate the filters refused must not be buyable. See the sibling test.
                "eligible": True,
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
            {"bid_id": "auction-x:s1", "store_id": "s1", "offer": {}, "eligible": True},
            {"bid_id": "auction-x:s2", "store_id": "s2", "offer": {}, "eligible": True},
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


@pytest.mark.parametrize("blacklisted", [True, False], ids=["blacklisted", "honest"])
def test_a_store_the_ranking_excluded_cannot_be_bought(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unwired: None,
    agent_url: str,
    blacklisted: bool,
) -> None:
    """The bid book holds what the ranking admitted, and an excluded store is not buyable.

    This is the assertion whose absence let a real hole through. The first version of the bid
    recording wrote EVERY collected candidate and justified it by claiming the accept path
    re-runs the checks that excluded them. It does not: ``accept/gate.py::accept_offer``
    re-reads the injected ``SellerEligibility`` source and nothing else, and the trust snapshot
    is a separate read by design. Measured over HTTP against this exact deployment before the
    fix — s1 marked ``"blacklisted": true``, excluded ``blacklisted_store … (R12)``::

        accept 'auction-2d54…:s1' -> 200 {"code": "PSX-FPWZHVZD",
                                          "permalink_url": "https://s1.example.com/cart/1:1?…"}

    Parametrized against the honest case on purpose: without it, "the blacklisted store is
    refused" is satisfied by an exchange that refuses everybody, which is what the code did
    before any of this work and is not the property being asserted.
    """
    document = _deployment_document(agent_url)
    for row in document["trust_snapshot"]["stores"].values():
        if row["store_id"] == "s1":
            row["blacklisted"] = blacklisted
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv(ENV_DEPLOYMENT, str(path))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

    with served_exchange() as client:
        body = _open_an_auction(client)
        ranked = {row["store_id"] for row in body["ranked"]}
        excluded = {row["store_id"]: row["bid_ref"] for row in body["excluded"]}

        if blacklisted:
            assert "s1" not in ranked, "a blacklisted store was ranked"
            assert "s1" in excluded, body
            answer = client.post(
                f"/auctions/{body['auction_id']}/accept", json={"bid_ref": excluded["s1"]}
            )
            assert answer.status_code == 409, (
                f"a store the ranking excluded was accepted {answer.status_code}: {answer.text}"
            )
            assert "code" not in answer.json(), answer.json()
        else:
            # The control. s1 is rankable in this arm, so a 409 here would mean the exchange
            # refuses everyone and the arm above proves nothing.
            assert "s1" in ranked, body["excluded"]
            ref = next(row["bid_ref"] for row in body["ranked"] if row["store_id"] == "s1")
            answer = client.post(f"/auctions/{body['auction_id']}/accept", json={"bid_ref": ref})
            assert answer.status_code == 200, answer.text
            assert answer.json()["code"].startswith("PSX-")


def test_a_deployment_document_that_will_not_parse_is_a_503_and_never_a_500(
    monkeypatch: pytest.MonkeyPatch, unwired: None
) -> None:
    """Deeply nested JSON raises ``RecursionError``, which is not a ``ValueError``.

    ``read_deployment`` caught only ``ValueError``, so this escaped ``DeploymentConfiguration
    Error`` entirely and surfaced as an unauthenticated **HTTP 500** on both served POST
    routes — on every request, because a failed configuration is deliberately not cached. The
    repo carries an open ticket for exactly this class (``test_t270_no_field_of_any_request_
    can_produce_a_5xx``); this is the same defect one door over, and it is closed here.
    """
    monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)
    monkeypatch.setenv(ENV_DEPLOYMENT_JSON, "[" * 100_000 + "]" * 100_000)

    with served_exchange() as client:
        response = client.post("/auctions", json={"intent": INTENT, "roster": []})

    assert response.status_code == 503, f"{response.status_code}: {response.text[:300]}"
    assert "not valid JSON" in response.json()["detail"]


def test_a_slow_store_agent_cannot_hold_a_fan_out_worker_open(agent_url: str) -> None:
    """httpx's timeout resets on every chunk, so a drip is not a timeout.

    Measured before the wall-clock deadline, against an agent sending one chunked byte every
    0.5s with the solicitor's httpx timeout at 1.0s: ``solicit()`` returned after **21.12s**.
    At that rate one hostile agent pins one of ``MAX_FAN_OUT_WORKERS = 32`` process-wide
    workers for around 36 hours, and 32 of them end all outbound bidding — the residual
    ``BoundedFanOutPool`` names in its own docstring, and this solicitor is the first thing in
    the tree that could drive it.
    """
    import time as _time

    from exchange.composition import HttpBidSolicitor
    from fastapi.responses import StreamingResponse

    slow = FastAPI(title="a store agent that answers one byte at a time")

    @slow.post("/v1/bid-requests")
    def door(body: dict[str, Any]) -> StreamingResponse:
        def drip() -> Iterator[bytes]:
            for _ in range(10_000):
                _time.sleep(0.05)
                yield b" "

        return StreamingResponse(drip(), media_type="application/json")

    with serve(slow) as url:
        solicitor = HttpBidSolicitor({"slow": f"{url}/v1/bid-requests"}, timeout=1.0)
        bound = solicitor.for_auction(auction_id="a1", intent={}, profile={}, respond_by=0.0)
        started = _time.monotonic()
        assert bound.solicit({"store_id": "slow"}) is None
        elapsed = _time.monotonic() - started

    from exchange.composition import MAX_SOLICIT_WALL_CLOCK_SECONDS

    assert elapsed < MAX_SOLICIT_WALL_CLOCK_SECONDS + 5.0, (
        f"the drip held one fan-out worker for {elapsed:.2f}s against a "
        f"{MAX_SOLICIT_WALL_CLOCK_SECONDS}s wall-clock ceiling"
    )


# =====================================================================================
# The two halves meeting: the REAL store agent, over HTTP, into the real exchange
# =====================================================================================
#: The store the shipped approved-envelope fixture describes, and the domain the PLATFORM
#: would hold for it. Both sides of every on-domain check read this one value, which is the
#: only honest arrangement — a deployment populates the seller registry and the merchant's
#: store context from one seller record.
REAL_STORE_ID = "store-alpha"
REAL_STORE_DOMAIN = "store-alpha.example.com"
REAL_CLUSTER = "cluster-warm-layers"
REAL_PRODUCT = "prod-cap"


def test_the_real_store_agent_and_the_real_exchange_complete_a_purchase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unwired: None
) -> None:
    """Both deployables, both booted, one loopback socket between them, one discount code.

    Everything above this line uses a store-agent double, deliberately: an acceptance test for
    the exchange's composition root must not be able to fail for the store agent's reasons.
    This one is the opposite test and it is the one that proves the system, because the two
    halves of this defect were fixed in two different packages by two different lanes:

    * the store agent had a bidding runtime nothing served, so no HTTP door existed to knock
      on and every offer it built carried no ``checkout_url``;
    * the exchange had no outbound client, no seller registry, no trust snapshot and no bid
      book, so it solicited nobody and could accept nothing.

    Either half alone still produces an empty shortlist. What is asserted here is the join:
    ``store_agent.main.create_app()`` serving its published ``POST /v1/bid-requests`` on a real
    port, ``exchange.main.create_app()`` serving on another, the exchange's own
    ``HttpBidSolicitor`` dialling the first from inside the fan-out, and a buyer walking away
    with a code that resolves on the host the platform registered.

    Neither app is wired by this test. The store agent reads ``STORE_AGENT_CONTEXT`` and the
    exchange reads ``EXCHANGE_DEPLOYMENT``, which is what a person deploying them does.
    """
    from store_agent.main import create_app as create_store_agent

    envelope_fixture = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "fixtures"
            / "envelopes"
            / "store-alpha.approved.json"
        ).read_text(encoding="utf-8")
    )
    context = {
        "store_id": REAL_STORE_ID,
        "envelope": envelope_fixture["envelope"],
        "catalog": envelope_fixture["catalog"],
        "live_state": {REAL_PRODUCT: {"in_stock": True, "units_left": 7}},
        "learned_policy": None,
        "network_priors": {REAL_CLUSTER: {"depth_buckets": [0.0, 0.05, 0.1, 0.15, 0.2]}},
    }
    context_path = tmp_path / "store-context.json"
    context_path.write_text(json.dumps(context), encoding="utf-8")
    monkeypatch.setenv("STORE_AGENT_CONTEXT", str(context_path))
    monkeypatch.setenv("STORE_AGENT_STORE_DOMAIN", REAL_STORE_DOMAIN)

    with serve(create_store_agent()) as agent_url:
        document = {
            "sellers": [
                {
                    "store_id": REAL_STORE_ID,
                    "eligibility": "eligible",
                    "registered_domain": REAL_STORE_DOMAIN,
                    "bid_endpoint": f"{agent_url}/v1/bid-requests",
                }
            ],
            "trust_snapshot": {
                "stores": {
                    REAL_STORE_ID: {
                        "store_id": REAL_STORE_ID,
                        "blacklisted": False,
                        "score": 0.8,
                    }
                }
            },
            "checkout_mode": "redirect",
        }
        deployment = tmp_path / "deployment.json"
        deployment.write_text(json.dumps(document), encoding="utf-8")
        monkeypatch.setenv(ENV_DEPLOYMENT, str(deployment))
        monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

        with served_exchange() as client:
            opened = client.post(
                "/auctions",
                json={
                    "intent": {**INTENT, "cluster_id": REAL_CLUSTER},
                    "profile": {"pseudonym": "psn-cross-lane", "buckets": {}},
                    "roster": [
                        {
                            "store_id": REAL_STORE_ID,
                            "tier": 1,
                            # The catalogue's own list price and the envelope's own cap.
                            # They are the PLATFORM's half of the T-177 price wall, and a
                            # roster that states either one differently makes the store's
                            # honest bid read as an unauthorized discount: measured, a
                            # `list_price: 120.0` row turned a real bid into
                            # `fallback_reason: 'bid_price_unreconcilable'`.
                            "product_ref": REAL_PRODUCT,
                            "list_price": 100.0,
                            "max_discount_pct": 20.0,
                        }
                    ],
                },
            )
            assert opened.status_code == 201, opened.text
            body = opened.json()

            # The agent really answered: a fallback here means the solicitation never landed.
            assert body["solicited"] == [REAL_STORE_ID], body
            assert body["entries"] and body["entries"][0]["fallback"] is False, (
                "the exchange manufactured a list-price fallback, so the real agent was never "
                f"reached or refused to bid: {body['entries']}"
            )
            assert body["ranked"], f"nothing ranked; exclusions were {body['excluded']}"
            assert body["shortlist"]["slots"], body

            top = body["shortlist"]["slots"][0]
            accepted = client.post(
                f"/auctions/{body['auction_id']}/accept", json={"bid_ref": top["bid_ref"]}
            )

    assert accepted.status_code == 200, accepted.text
    payload = accepted.json()
    assert payload["code"].startswith("PSX-"), payload
    assert payload["permalink_url"].startswith(f"https://{REAL_STORE_DOMAIN}/cart/"), payload
    assert f"discount={payload['code']}" in payload["permalink_url"], payload


# =====================================================================================
# The ledger seam (T-150) — the eighth collaborator, and the only one bound by DEFAULT
# =====================================================================================
def test_an_exchange_nobody_configured_still_writes_its_transitions_at_the_trust_service() -> None:
    """The sink an unconfigured app installs on first use is a producer into the trust ledger.

    ``AuctionStateMachine()`` with no sink is what shipped, and its ``LedgerRecorder`` builds
    an ``InMemoryLedgerSink`` — a list discarded with the app — so the hash chaining, once-only
    landing and replay in ``apps/trust/src/events`` graded a stream no served request produced
    (T-150). It has to be the DEFAULT and not a configuration key, because ``create_app()``
    with nothing set is what ``docker compose up`` starts: ``apps/exchange/compose.yaml``
    forwards exactly two variables for this module and neither has a value there, so a producer
    reachable only through configuration is a producer no deployment in this repository reaches.

    That the write actually LEAVES the process is measured in ``test_repro_ledger_gates.py``
    ``::test_t150_a_served_auction_puts_its_transitions_in_the_trust_ledger``, which serves an
    auction with every trust event store's ``append`` and every outbound ``httpx`` request
    watched. What is asserted here is the composition root's half: which sink the route's lazy
    default takes, and from where.
    """
    from exchange.auction.routes import _machine
    from exchange.composition import (
        DEFAULT_TRUST_URL,
        TRUST_EVENTS_PATH,
        HttpTrustLedgerSink,
        default_ledger_sink,
    )

    assert isinstance(default_ledger_sink(), HttpTrustLedgerSink)
    assert default_ledger_sink({}).url == f"{DEFAULT_TRUST_URL}{TRUST_EVENTS_PATH}"
    assert default_ledger_sink({"TRUST_URL": "http://ledger:9/"}).url == "http://ledger:9/events"

    # …and it is the sink the served route installs. `_machine` reads one attribute off the
    # request, so a stand-in carrying the real app is the whole of what it needs — and using
    # the real app is what makes this an assertion about `create_app()`'s wiring.
    app = create_app()
    machine = _machine(SimpleNamespace(app=app))
    assert machine is app.state.auction_machine
    sink = machine.ledger.sink
    assert isinstance(sink, HttpTrustLedgerSink), (
        f"an unconfigured exchange installed a {type(sink).__name__} as its ledger sink. "
        f"Whatever else that is, it does not leave the process"
    )
    assert sink.url == f"{DEFAULT_TRUST_URL}{TRUST_EVENTS_PATH}", sink.url
    # The in-process readback three `test_auction.py` nodes assert on is still there, because
    # the sink SUBCLASSES the stub rather than replacing it.
    assert sink.kinds == [] and sink.for_auction("a-1") == []


def test_a_deployment_states_where_its_trust_service_answers(
    monkeypatch: pytest.MonkeyPatch, unwired: None
) -> None:
    """``trust_url`` in the document outranks the default, and binds a whole machine.

    The document as well as an environment variable, because ``apps/exchange/compose.yaml``
    forwards no variable it does not name and the two it names are ``EXCHANGE_DEPLOYMENT`` and
    ``EXCHANGE_DEPLOYMENT_JSON`` — so in the shipped container the document is the only knob
    that reaches this module.
    """
    from exchange.composition import (
        DEFAULT_TRUST_URL,
        TRUST_EVENTS_PATH,
        HttpTrustLedgerSink,
        configure_exchange,
        read_deployment,
    )

    document = {**_deployment_document("http://127.0.0.1:1"), "trust_url": "http://ledger:9/"}
    monkeypatch.setenv(ENV_DEPLOYMENT_JSON, json.dumps(document))
    monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)
    deployment = read_deployment()
    assert deployment is not None
    assert deployment.trust_url == "http://ledger:9/"

    app = create_app()
    bound = configure_exchange(app, deployment)
    assert "auction_machine" in bound, bound
    sink = app.state.auction_machine.ledger.sink
    assert isinstance(sink, HttpTrustLedgerSink)
    # One trailing slash in the document must not become `//events`: the path is appended in
    # exactly one place so no two callers can disagree about it.
    assert sink.url == "http://ledger:9/events", sink.url

    # …and it never overwrites a machine a caller already chose, like every other seam here.
    chosen = create_app()
    chosen.state.auction_machine = object()
    assert "auction_machine" not in configure_exchange(chosen, deployment)

    # A document that states no `trust_url` still binds the seam — the key says WHERE, never
    # WHETHER. That is what stops `accept/routes.py`'s own bare `AuctionStateMachine()` default
    # from being installed by an app whose first auction request is an accept.
    monkeypatch.setenv(ENV_DEPLOYMENT_JSON, json.dumps(_deployment_document("http://127.0.0.1:1")))
    silent_document = read_deployment()
    assert silent_document is not None and silent_document.trust_url is None

    silent = create_app()
    assert "auction_machine" in configure_exchange(silent, silent_document)
    assert silent.state.auction_machine.ledger.sink.url == f"{DEFAULT_TRUST_URL}{TRUST_EVENTS_PATH}"


def test_a_trust_url_that_is_not_an_http_url_is_refused_by_the_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused at parse, because the alternative failure is invisible by design.

    Every other malformed key in this document produces an empty shortlist that at least looks
    like a policy decision. This one is worse: a sink pointed at nonsense swallows its own
    transport failure — deliberately, so a trust service that is down cannot fail an auction —
    so a typo here would leave a perfectly healthy exchange writing its audit trail nowhere,
    with nothing in any response to say so.
    """
    from exchange.composition import DeploymentConfigurationError, read_deployment

    document = {**_deployment_document("http://127.0.0.1:1"), "trust_url": "trust:8084"}
    monkeypatch.setenv(ENV_DEPLOYMENT_JSON, json.dumps(document))
    monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)

    with pytest.raises(DeploymentConfigurationError) as raised:
        read_deployment()
    assert "trust_url" in str(raised.value) and "http(s)" in str(raised.value)


def test_a_trust_service_that_is_down_costs_the_auction_nothing_and_is_not_silent() -> None:
    """The three properties that make writing to another service on the request path safe.

    * **The transition still stands.** ``LedgerRecorder``'s own rule — losing an audit record
      is bad, failing a live auction because the audit sink hiccuped is worse — now has to
      survive a network, so the sink swallows its own transport failure.
    * **It is not swallowed into ``LedgerRecorder.failures``.** That list is unbounded and this
      runs on the unauthenticated ``POST /auctions``, so a trust service down for an hour would
      be a memory leak anybody can drive by posting in a loop. The record is a bounded ring
      instead, event ids and reasons only, never a payload.
    * **The in-process readback is untouched**, which is what makes the ring a diagnosis rather
      than a hole: what the auction recorded is still readable when trust is not.
    """
    from exchange.auction.ledger import LedgerRecorder
    from exchange.composition import MAX_UNDELIVERED_LEDGER_EVENTS, HttpTrustLedgerSink

    class _DeadTrust:
        def post(self, url: str, **kwargs: Any) -> Any:
            raise httpx.ConnectError("nodename nor servname provided, or not known")

    sink = HttpTrustLedgerSink("http://trust.invalid/events")
    sink._client = _DeadTrust()
    recorder = LedgerRecorder(sink)

    recorded = recorder.record("auction_opened", auction_id="a-1", payload={"roster_size": 0})

    assert recorder.failures == [], (
        f"a transport failure reached LedgerRecorder.failures, an unbounded list on an "
        f"unauthenticated path: {recorder.failures}"
    )
    assert sink.kinds == ["auction_opened"], sink.kinds
    assert sink.delivered == 0
    assert [event_id for event_id, _ in sink.undelivered] == [recorded["event_id"]]
    assert [reason for _, reason in sink.undelivered] == [
        "ConnectError: nodename nor servname provided, or not known"
    ], list(sink.undelivered)

    # The ring is a ring. Driven past its bound it holds the newest, and does not grow.
    assert sink.undelivered.maxlen == MAX_UNDELIVERED_LEDGER_EVENTS
    for index in range(MAX_UNDELIVERED_LEDGER_EVENTS + 10):
        recorder.record("auction_closed", auction_id=f"a-{index}", payload={})
    assert len(sink.undelivered) == MAX_UNDELIVERED_LEDGER_EVENTS
    assert recorder.failures == []


def test_a_trust_service_that_refuses_an_event_is_recorded_rather_than_counted_as_landed() -> None:
    """A 4xx/5xx is not delivery — and ``409``, which is once-only landing working, is not either.

    Kept distinguishable on purpose: an operator reading ``delivered`` wants the number of
    events the chained ledger actually took, and a door answering "I already have it" took none,
    however healthy that is.
    """
    from exchange.composition import HttpTrustLedgerSink

    class _Refusing:
        def __init__(self, status: int) -> None:
            self.status = status

        def post(self, url: str, **kwargs: Any) -> Any:
            return httpx.Response(self.status, request=httpx.Request("POST", url))

    for status in (409, 422, 503):
        sink = HttpTrustLedgerSink("http://trust.invalid/events")
        sink._client = _Refusing(status)
        sink.emit({"event_id": f"e-{status}", "kind": "auction_opened"})
        assert sink.delivered == 0, status
        assert [reason for _, reason in sink.undelivered] == [f"trust answered HTTP {status}"]

    landed = HttpTrustLedgerSink("http://trust.invalid/events")
    landed._client = _Refusing(201)
    landed.emit({"event_id": "e-ok", "kind": "auction_opened"})
    assert landed.delivered == 1 and not landed.undelivered


def test_an_unreachable_trust_service_changes_nothing_a_buyer_or_a_store_can_see(
    deployed: httpx.Client,
) -> None:
    """HONEST TRAFFIC. The whole purchase, with the new outbound write failing every time.

    This is the case neither a red-before gate nor an adversarial verifier catches, because
    both are pointed at the defect: the repair adds an outbound call to the request path of
    every auction and every accept, and there is no trust service anywhere in this suite — so
    **every one of those calls fails**, on a DNS name that does not resolve. The property is
    that neither a buyer nor a store can tell.

    Driven through the same served process on a real socket, the same real store agents over
    loopback and the same deployment document the rest of this file uses — not a stub — and
    carried all the way to a minted, chargeable discount code, because the ledger writes are
    spread across ``auction_opened``, ``auction_closed`` and ``accepted`` and a mint that
    survived the first two would prove nothing about the third.
    """
    body = _open_an_auction(deployed)

    assert body["solicited"] == [row["store_id"] for row in STORES], body
    assert body["entries"] and all(entry["fallback"] is False for entry in body["entries"]), (
        f"a store fell back to its list price, so a solicitation did not land: {body['entries']}"
    )
    assert body["denied"] == [], body["denied"]
    assert body["ranked"], f"nothing ranked; exclusions were {body['excluded']}"
    assert body["shortlist"]["slots"], body

    top = body["shortlist"]["slots"][0]
    accepted = deployed.post(
        f"/auctions/{body['auction_id']}/accept", json={"bid_ref": top["bid_ref"]}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["code"].startswith("PSX-"), accepted.json()


# =====================================================================================
# The ledger writer is shared machinery, and it reports a condition rather than an event
# =====================================================================================
def test_the_ledger_writer_is_shared_machinery_rather_than_this_services_private_client() -> None:
    """The exchange's sink is built ON the shared writer; it does not carry a copy of one.

    T-150 shipped the FIRST cross-process ledger writer in this repository as a private class
    in ONE service's composition root. Nothing else here POSTs to the trust service:
    ``trust.events.append`` is in-process, the exchange's ``InMemoryLedgerSink`` is a list
    discarded with the app, and merchant's ``HANDOFF`` ring says in its own docstring that it
    exists because "E6 is not deployed yet". So the mechanism lives in
    ``proxyshop_support.trust_ledger`` now — where ``redis_client``, ``postgres``,
    ``asgi_server`` and ``logging_config`` already live, and which merchant's and buyer's
    ``main.py`` already import — and the exchange's sink is the composition of the two halves
    that are genuinely the exchange's: the in-process readback, and that writer.

    ``issubclass`` both ways is the assertion that a *copy* cannot satisfy. The endpoint
    resolver is asserted to be the same function OBJECT for the same reason: two spellings of
    "append ``/events`` to the base URL" is how two callers end up disagreeing about it.
    """
    from exchange.auction.ledger import InMemoryLedgerSink
    from exchange.composition import HttpTrustLedgerSink, trust_events_url

    from proxyshop_support import trust_ledger

    assert issubclass(HttpTrustLedgerSink, trust_ledger.TrustLedgerPublisher), (
        "the exchange's trust sink is not built on the shared writer, so adopting the "
        "mechanism from merchant or buyer means copying it"
    )
    assert issubclass(HttpTrustLedgerSink, InMemoryLedgerSink), (
        "the in-process readback three test_auction.py nodes assert on is gone"
    )
    assert trust_events_url is trust_ledger.trust_events_url
    assert HttpTrustLedgerSink("http://ledger:9/events").status()["url"] == "http://ledger:9/events"


def test_a_trust_service_that_stops_taking_events_is_a_reported_fault_not_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JOB 2. The per-event warning is gone, and what replaced it says more, not less.

    T-150 shipped warn-and-continue: one ``WARNING`` per sink, saying that the auction stands
    and the transition is not in the chained ledger, and then counters. That is the posture of
    a service that has not decided whether its audit trail is required. It is decided now — the
    trust ledger is standing architecture — so a required write that is not happening is a
    fault, reported at ``ERROR``, on the two moments the CONDITION changes:

    * five failing publishes are one fault, not five lines and not a per-event warning;
    * recovery is reported, with the number of events that were lost. The old shape reported
      the start of an outage once per process and its end never;
    * a second outage after a recovery is reported again. The old shape was silent forever
      after its one line, which is what "counted, not logged" quietly costs.

    And the condition stays readable after the line scrolls: ``status()`` is the mapping an
    operator or a health route reads, and it never carries a payload.
    """
    from exchange.composition import HttpTrustLedgerSink

    class _Transport:
        def __init__(self) -> None:
            self.up = False

        def post(self, url: str, **kwargs: Any) -> Any:
            if not self.up:
                raise httpx.ConnectError("nodename nor servname provided, or not known")
            return httpx.Response(201, request=httpx.Request("POST", url))

    transport = _Transport()
    sink = HttpTrustLedgerSink("http://trust.invalid/events")
    sink._client = transport

    with caplog.at_level(logging.DEBUG, logger="exchange.composition"):
        for index in range(5):
            sink.emit({"event_id": f"e-{index}", "kind": "auction_opened"})
        outage = [record for record in caplog.records if record.name == "exchange.composition"]
        assert [record.levelname for record in outage] == ["ERROR"], (
            f"five undeliverable events produced {[r.levelname for r in outage]}. A WARNING "
            f"here is the old warn-and-continue posture; more than one line is per-event noise "
            f"on the auction path"
        )
        assert sink.delivering is False

        caplog.clear()
        transport.up = True
        sink.emit({"event_id": "e-back", "kind": "auction_closed"})
        recovery = [record for record in caplog.records if record.name == "exchange.composition"]
        assert [record.levelname for record in recovery] == ["INFO"], recovery
        assert "5 event(s)" in recovery[0].getMessage(), recovery[0].getMessage()

        caplog.clear()
        transport.up = False
        sink.emit({"event_id": "e-again", "kind": "auction_closed"})
        again = [record for record in caplog.records if record.name == "exchange.composition"]
        assert [record.levelname for record in again] == ["ERROR"], (
            "a trust service that failed, recovered and failed again said nothing the second "
            "time — the hole a one-shot report leaves"
        )

    # The in-process record is untouched by any of it, which is what makes the ring and the
    # status a diagnosis rather than a hole.
    assert sink.kinds == ["auction_opened"] * 5 + ["auction_closed"] * 2
    assert sink.status() == {
        "url": "http://trust.invalid/events",
        "delivering": False,
        "delivered": 1,
        "lost": 6,
        "last_failure": "ConnectError: nodename nor servname provided, or not known",
    }


def test_an_exchange_says_where_its_audit_trail_goes_at_the_moment_it_binds_the_seam(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, unwired: None
) -> None:
    """JOB 2, the other half: the configuration question is answered at wiring time.

    "Which trust service is this process writing to, and did anybody choose it" is a
    configuration question, and the old shape could only answer it from a runtime warning that
    fired when something had already gone wrong. It is stated once now, when the seam is
    bound, where an operator reading a start-up log is looking — and at ``INFO``, not
    ``WARNING``, because a deployment that states no ``trust_url`` has not made a mistake: the
    default is the compose service name, which is the address that is correct in the
    deployment this repository ships.
    """
    from exchange.composition import (
        DEFAULT_TRUST_URL,
        configure_exchange,
        default_ledger_sink,
        read_deployment,
    )

    def bound_lines() -> list[logging.LogRecord]:
        return [record for record in caplog.records if record.name == "exchange.composition"]

    with caplog.at_level(logging.DEBUG, logger="exchange.composition"):
        caplog.clear()
        default_ledger_sink({})
        silent = bound_lines()
        assert [record.levelname for record in silent] == ["INFO"], silent
        assert DEFAULT_TRUST_URL in silent[0].getMessage(), silent[0].getMessage()
        assert "default" in silent[0].getMessage(), silent[0].getMessage()

        caplog.clear()
        default_ledger_sink({"TRUST_URL": "http://ledger:9"})
        from_environment = bound_lines()
        assert [record.levelname for record in from_environment] == ["INFO"]
        assert "http://ledger:9/events" in from_environment[0].getMessage()
        assert "TRUST_URL" in from_environment[0].getMessage()

        caplog.clear()
        document = {**_deployment_document("http://127.0.0.1:1"), "trust_url": "http://stated:9"}
        monkeypatch.setenv(ENV_DEPLOYMENT_JSON, json.dumps(document))
        monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)
        deployment = read_deployment()
        assert deployment is not None
        configure_exchange(create_app(), deployment)
        stated = [record for record in bound_lines() if "trust" in record.getMessage()]
        assert [record.levelname for record in stated] == ["INFO"], stated
        assert "http://stated:9/events" in stated[0].getMessage()
        assert "deployment document" in stated[0].getMessage()


def test_a_served_auction_lands_in_a_real_trust_services_chained_ledger(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unwired: None,
    agent_url: str,
) -> None:
    """HONEST TRAFFIC through the moved wall — a real purchase into a real trust service.

    Everything else in this file measures the unreachable case, because there is no trust
    service in this suite. That is exactly half the property, and it is the half a refactor of
    the delivery mechanism cannot break by accident: a writer that posted nothing at all would
    pass it. So this one serves ``trust.main:create_app()`` on its own loopback port, names it
    in the exchange's deployment document, drives the same auction and the same accept the
    rest of this file drives — real store agents, real sockets, a real minted code — and then
    reads the transitions back **out of the trust service over HTTP**.

    One substitution, and it is a datastore rather than a behaviour: ``InMemoryEventStore`` on
    ``app.state.event_store``, the seam ``trust.events.routes.store_for`` resolves first, so
    this needs no Postgres. The append path, the hash chaining and the verification are the
    trust service's own — which is why ``/events/verify`` is asserted here rather than a
    count. It is the same substitution ``proxyshop_demo`` makes for the same reason.

    The log assertion is the other half of JOB 2: honest traffic through the new fault channel
    must be SILENT. A fault report that also fires when everything is working is a fault report
    an operator learns to ignore.
    """
    from trust.events import InMemoryEventStore
    from trust.main import create_app as create_trust

    trust_app = create_trust()
    trust_app.state.event_store = InMemoryEventStore()

    with serve(trust_app) as trust_url:
        document = {**_deployment_document(agent_url), "trust_url": trust_url}
        path = tmp_path / "deployment.json"
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        monkeypatch.setenv(ENV_DEPLOYMENT, str(path))
        monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

        exchange_app = create_app()
        with caplog.at_level(logging.DEBUG, logger="exchange.composition"):
            with serve(exchange_app) as exchange_url:
                with httpx.Client(base_url=exchange_url, timeout=REQUEST_TIMEOUT_SECONDS) as buyer:
                    body = _open_an_auction(buyer)
                    assert body["shortlist"]["slots"], body
                    top = body["shortlist"]["slots"][0]
                    accepted = buyer.post(
                        f"/auctions/{body['auction_id']}/accept", json={"bid_ref": top["bid_ref"]}
                    )
                    assert accepted.status_code == 200, accepted.text
                    assert accepted.json()["code"].startswith("PSX-"), accepted.json()

            faults = [
                record
                for record in caplog.records
                if record.name == "exchange.composition" and record.levelno >= logging.WARNING
            ]
            assert faults == [], (
                f"honest traffic into a trust service that is answering still reported a "
                f"delivery fault: {[record.getMessage() for record in faults]}"
            )

        with httpx.Client(base_url=trust_url, timeout=REQUEST_TIMEOUT_SECONDS) as reader:
            page = reader.get("/events")
            assert page.status_code == 200, page.text
            chain = page.json()
            report = reader.get("/events/verify")
            assert report.status_code == 200, report.text

    kinds = [event["kind"] for event in chain["events"]]
    assert {"auction_opened", "auction_closed", "accepted"} <= set(kinds), (
        f"a served auction and its accept reached a REACHABLE trust service with {kinds}; the "
        f"transitions the exchange recorded are not all in the chained ledger"
    )
    assert chain["is_chain"] is True, chain
    assert report.json()["ok"] is True, report.json()
    assert {event["auction_id"] for event in chain["events"]} == {body["auction_id"]}, chain

    sink = exchange_app.state.auction_machine.ledger.sink
    assert sink.status() == {
        "url": f"{trust_url}/events",
        "delivering": True,
        "delivered": len(chain["events"]),
        "lost": 0,
        "last_failure": None,
    }
    assert sink.kinds == kinds, (sink.kinds, kinds)
