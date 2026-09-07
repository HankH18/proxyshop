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
``test_an_exchange_nobody_configured_still_writes_its_transitions_at_the_trust_service``, and
``exchange.eligibility.trust_backed.TrustBackedSellerEligibility`` /
``exchange.composition.HttpTrustSnapshot`` by the last two tests in the file. All of them are
unit tests of those objects and none of them issues a request to the exchange. Every HTTP test
in this file goes through the ``deployed`` fixture or :func:`served_exchange`, and neither
wires anything.

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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from exchange.accept.offer import use_registered_domains
from exchange.composition import ENV_DEPLOYMENT, ENV_DEPLOYMENT_JSON
from exchange.main import create_app
from fastapi import FastAPI, Request, Response
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


# =====================================================================================
# R12 is read from the TRUST SERVICE, not from a deterministic double (T-303 b)
#
# The finding: `configure_auctions(app, eligibility=...)` had no product caller, and there
# was no trust-backed `SellerEligibility` anywhere in the tree to give it — not an unwired
# one, none. So `auction/routes.py::_eligibility` fell through to a `StaticSellerEligibility`
# built with NO ROWS, and the served exchange answered `static-eligibility: <store> is
# unavailable` for every store alive. That is not "consulted trust and refused". It is
# "asked nobody", wearing a sentence that reads like a verdict.
#
# The two halves are asserted separately on purpose, because the one-line way to satisfy
# either alone breaks the other: a source that ADMITS by default satisfies "an honest store
# is solicited" and fails the fail-closed half, and a source that DENIES everything satisfies
# the fail-closed half and leaves the exchange asking nobody exactly as before.
# =====================================================================================
#: A price for the approved roster's rows. `list_price` is required by the route's own model.
MANIFEST_LIST_PRICE = 19.99


def _manifest_roster() -> tuple[list[dict[str, Any]], str, str]:
    """The approved fixture roster, and the store the MANIFEST itself calls dishonest.

    Read out of ``fixtures/manifest.json`` through its own loader rather than hand-written
    here: the roster, the business identities and which store is the adversary are ground
    truth a human approved (SPEC A3), and a corpus this file invented would be a corpus
    written by the same mind that chose the wall.
    """
    from fixtures.manifest import load_manifest

    manifest = load_manifest()
    stores = [dict(row) for row in manifest["stores"]]
    dishonest = manifest["dishonest_store"]
    return stores, str(dishonest["store_id"]), str(dishonest["business_identity"])


def _trust_service(stores: list[dict[str, Any]], blacklisted_identity: str) -> FastAPI:
    """A REAL ``trust.main:create_app()`` serving ``GET /snapshot`` over the approved roster.

    One substitution, and it is a datastore rather than a behaviour: ``snapshot_stores`` and
    ``snapshot_blacklist`` on ``app.state``, the two seams
    ``trust.snapshot.routes.stores_for`` / ``blacklist_for`` resolve first, so this needs no
    Postgres. The route, the projection to the published ``TrustSnapshot`` property set, the
    scoring and the identity-bound blacklist resolution are the trust service's own — which is
    the point: what the exchange reads here is what a deployed trust service serves.

    The blacklisting is bound to BUSINESS IDENTITY, not to ``store_id``, because that is what
    ``trust.scoring.is_blacklisted`` resolves and it is the whole reason the registry is
    identity-bound: a delisted operator must not return under a fresh store id.
    """
    from trust.events import InMemoryEventStore
    from trust.main import create_app as create_trust
    from trust.scoring import Blacklist

    registry = Blacklist()
    registry.add(
        business_identity=blacklisted_identity,
        reason_code="trust_score_below_threshold",
        status="active",
    )
    app = create_trust()
    app.state.snapshot_stores = [
        {
            "store_id": row["store_id"],
            "business_identity": row["business_identity"],
            "observations": [],
        }
        for row in stores
    ]
    app.state.snapshot_blacklist = registry
    # The SAME substitution `test_a_served_auction_lands_in_a_real_trust_services_chained_ledger`
    # makes, and for the same reason: this deployment's `trust_url` names one trust service for
    # both doors, so a service that could serve `/snapshot` but 503 on `/events` would report a
    # ledger fault and make the "honest traffic is silent" half of that test's property untrue
    # here for a reason that has nothing to do with eligibility.
    app.state.event_store = InMemoryEventStore()
    return app


def _open_a_roster_auction(client: httpx.Client, stores: list[dict[str, Any]]) -> dict[str, Any]:
    response = client.post(
        "/auctions",
        json={
            "intent": INTENT,
            "profile": {"pseudonym": "psn-t303", "buckets": {}},
            "roster": [
                {"store_id": row["store_id"], "tier": 1, "list_price": MANIFEST_LIST_PRICE}
                for row in stores
            ],
        },
    )
    assert response.status_code == 201, f"POST /auctions -> {response.status_code}: {response.text}"
    return response.json()


def test_a_booted_exchange_reads_r12_from_a_real_trust_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unwired: None, caplog: pytest.LogCaptureFixture
) -> None:
    """HONEST TRAFFIC through the new wall: a real trust service, and a store it has delisted.

    The exchange is the served one — ``create_app()`` plus a deployment document, no test-side
    ``configure_auctions`` — and the trust service on the other end is
    ``trust.main:create_app()`` on its own loopback port, answering the published
    ``GET /snapshot`` over the approved fixture roster.

    Both halves are asserted in the SAME run, which is what makes it a discrimination rather
    than a mood: the store the trust registry delists must be refused, and the stores it holds
    nothing against must be solicited. A source that refuses everybody fails the second, and a
    source that admits everybody fails the first.

    The log assertion is the other half of the fault channel: honest traffic through a trust
    service that is answering must be SILENT. A fault report that fires when everything works
    is a fault report an operator learns to ignore.
    """
    stores, dishonest_id, dishonest_identity = _manifest_roster()
    honest = {row["store_id"] for row in stores} - {dishonest_id}
    assert honest, "the approved roster has no honest store, so this cannot discriminate"

    with serve(_trust_service(stores, dishonest_identity)) as trust_url:
        # NO `sellers` key: this deployment states where trust answers and nothing about who
        # is eligible, which is the arrangement that makes trust's verdict the R12 answer.
        path = tmp_path / "deployment.json"
        path.write_text(json.dumps({"trust_url": trust_url}), encoding="utf-8")
        monkeypatch.setenv(ENV_DEPLOYMENT, str(path))
        monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

        with caplog.at_level(logging.DEBUG, logger="exchange.composition"):
            with served_exchange() as client:
                body = _open_a_roster_auction(client, stores)

    denials = {row["store_id"]: row for row in body["denied"]}
    solicited = set(body["solicited"])

    assert dishonest_id in denials, (
        f"the store the trust registry delisted was not refused. denied={sorted(denials)}, "
        f"solicited={sorted(solicited)}"
    )
    assert denials[dishonest_id]["status"] == "blacklisted", denials[dishonest_id]
    assert "trust-eligibility" in denials[dishonest_id]["reason"], denials[dishonest_id]
    assert dishonest_id not in solicited, "a delisted store was still asked to bid"

    assert honest <= solicited, (
        f"the served exchange did not solicit {sorted(honest - solicited)}, which the trust "
        f"service holds nothing against. denied={[(k, v['reason']) for k, v in denials.items()]}"
    )
    assert not any("static-eligibility" in row["reason"] for row in body["denied"]), body["denied"]

    faults = [
        record
        for record in caplog.records
        if record.name == "exchange.composition" and record.levelno >= logging.WARNING
    ]
    assert faults == [], (
        f"honest traffic against a trust service that is answering still reported a fault: "
        f"{[record.getMessage() for record in faults]}"
    )


def test_an_exchange_whose_trust_service_is_down_denies_every_store_and_says_so_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unwired: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The fail-closed half, at the wall this moved — and the positive control is the test above.

    ``127.0.0.1:1`` is closed, so the exchange reads no snapshot at all. It must then deny
    EVERY store, including the ones the test above proves it solicits when trust answers: an
    exchange that cannot reach trust knows nothing current about anybody, and knowing nothing
    denies (R12). It must also say so — once, on the state change, at ``ERROR`` — and not once
    per store and not once per auction.
    """
    stores, _dishonest_id, _identity = _manifest_roster()
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps({"trust_url": "http://127.0.0.1:1"}), encoding="utf-8")
    monkeypatch.setenv(ENV_DEPLOYMENT, str(path))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

    with caplog.at_level(logging.DEBUG, logger="exchange.composition"):
        with served_exchange() as client:
            first = _open_a_roster_auction(client, stores)
            second = _open_a_roster_auction(client, stores)

    assert first["solicited"] == [], first
    assert {row["status"] for row in first["denied"]} == {"unavailable"}, first["denied"]
    assert len(first["denied"]) == len(stores), first["denied"]
    for row in first["denied"]:
        assert "trust-eligibility" in row["reason"], row
        assert "static-eligibility" not in row["reason"], row
    assert second["solicited"] == [], second

    # The ELIGIBILITY channel only. The ledger writer reports the same unreachable service on
    # the same logger, and it is a separate condition with its own one-line-per-change rule.
    faults = [
        record
        for record in caplog.records
        if record.name == "exchange.composition"
        and record.levelno >= logging.ERROR
        and "eligibility" in record.getMessage()
    ]
    assert len(faults) == 1, (
        f"a trust service that is down should be reported once, on the state change, not "
        f"{len(faults)} times: {[record.getMessage() for record in faults]}"
    )
    assert "127.0.0.1:1/snapshot" in faults[0].getMessage(), faults[0].getMessage()


def test_an_unconfigured_exchange_refuses_by_naming_trust_rather_than_a_double(
    monkeypatch: pytest.MonkeyPatch, unwired: None
) -> None:
    """No deployment at all still denies everything — and now it denies for a readable reason.

    ``test_an_unconfigured_exchange_still_refuses_everything`` pins the BEHAVIOUR and must not
    move. This pins what the behaviour is evidence OF: before T-303 every denial read
    ``static-eligibility: s1 is unavailable``, which is a deterministic double that no
    deployment ever bound saying it has no rows — indistinguishable, from outside, from a
    trust service that had actually been asked. The reason now names the trust service this
    process could not reach, which is the true statement.
    """
    monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

    with served_exchange() as client:
        body = _open_an_auction(client)

    assert [row["status"] for row in body["denied"]] == ["unavailable"] * len(STORES)
    for row in body["denied"]:
        assert "trust-eligibility" in row["reason"], row
        assert "static-eligibility" not in row["reason"], row


def test_the_trust_backed_source_tells_unreachable_from_unknown_from_delisted() -> None:
    """The three states the whole fix turns on, read through the port's own fail-closed reader.

    A direct-call unit test of
    :class:`~exchange.eligibility.trust_backed.TrustBackedSellerEligibility` — one of the
    handful in this file that issues no request; see the module docstring. It is read through
    ``read_eligibility`` rather than by calling ``check`` directly because that is the function
    the three gates actually apply, so what is graded here is what a gate will do.
    """
    from exchange.eligibility import BLACKLISTED, ELIGIBLE, UNAVAILABLE, read_eligibility
    from exchange.eligibility.trust_backed import (
        TrustBackedSellerEligibility,
        TrustSnapshotUnavailable,
    )

    #: `build_snapshot`'s envelope, not the flat mapping, so the unwrap is exercised too.
    document = {
        "version": "trust-snapshot/1.0.0",
        "stores": {
            "honest": {"store_id": "honest", "blacklisted": False, "score": 0.86},
            "delisted": {"store_id": "delisted", "blacklisted": True, "score": 0.07},
            "unreadable": {"store_id": "unreadable", "blacklisted": "false"},
        },
    }

    def unreachable() -> dict[str, Any]:
        raise TrustSnapshotUnavailable("ConnectError: nodename nor servname provided")

    answering = TrustBackedSellerEligibility(document, source="the trust snapshot under test")
    down = TrustBackedSellerEligibility(unreachable, source="the trust snapshot under test")

    assert read_eligibility(answering, "honest").status == ELIGIBLE
    assert read_eligibility(answering, "delisted").status == BLACKLISTED
    # Trust answered and has no opinion. NOT the same state as trust being down, and NOT a
    # reason to admit: this is the rule `trust.openapi.json` publishes on the route itself.
    assert read_eligibility(answering, "never-heard-of").status == UNAVAILABLE
    assert read_eligibility(answering, "unreadable").status == UNAVAILABLE
    # The positive control is the first line: the SAME store id, admitted when trust answers
    # and refused when it does not, so "trust-backed" cannot mean "admits when trust is down".
    assert read_eligibility(down, "honest").status == UNAVAILABLE

    for source, store_id in (
        (answering, "delisted"),
        (answering, "never-heard-of"),
        (down, "honest"),
    ):
        reason = read_eligibility(source, store_id).reason
        assert "trust-eligibility" in reason, reason
        assert "static-eligibility" not in reason, reason


def test_the_exchange_caches_the_trust_snapshot_and_revalidates_on_the_version_it_publishes() -> (
    None
):
    """T-064 acceptance 3, at last: the exchange caches the snapshot and refreshes on a bump.

    A direct-call unit test of :class:`~exchange.composition.HttpTrustSnapshot` against a
    served stub, because the clock has to be injectable and the request count has to be
    observable. The stub answers the two things the real route answers — an ``ETag`` carrying
    the snapshot version, and ``304`` to a conditional ``GET`` naming the current one.

    The cache is a correctness requirement and not an optimisation: ``check`` is asked once per
    rostered store and three times per purchase, and an uncached reader could answer
    differently for two stores in the same auction.
    """
    from exchange.composition import TRUST_SNAPSHOT_REFRESH_SECONDS, HttpTrustSnapshot

    version = {"value": '"trust-snapshot/1.0.0"'}
    rows: dict[str, Any] = {"s1": {"store_id": "s1", "blacklisted": False}}
    conditionals: list[str | None] = []

    stub = FastAPI(title="trust-snapshot-double")

    @stub.get("/snapshot")
    def snapshot(request: Request) -> Response:
        offered = request.headers.get("if-none-match")
        conditionals.append(offered)
        if offered == version["value"]:
            return Response(status_code=304, headers={"etag": version["value"]})
        return JSONResponse(content=dict(rows), headers={"etag": version["value"]})

    clock = {"now": 0.0}
    with serve(stub) as url:
        reader = HttpTrustSnapshot(f"{url}/snapshot", monotonic=lambda: clock["now"])

        assert reader()["s1"]["blacklisted"] is False
        reader()
        reader()
        assert conditionals == [None], (
            f"the snapshot was refetched inside its refresh window: {conditionals}"
        )

        # The version moves, and so does the verdict. The refresh must be a CONDITIONAL get.
        clock["now"] += TRUST_SNAPSHOT_REFRESH_SECONDS + 1.0
        rows["s1"] = {"store_id": "s1", "blacklisted": True}
        version["value"] = '"trust-snapshot/1.0.1"'
        assert reader()["s1"]["blacklisted"] is True
        assert conditionals == [None, '"trust-snapshot/1.0.0"'], conditionals

        # A version that has NOT moved costs a 304 and the cached document stands.
        clock["now"] += TRUST_SNAPSHOT_REFRESH_SECONDS + 1.0
        assert reader()["s1"]["blacklisted"] is True
        assert conditionals == [None, '"trust-snapshot/1.0.0"', '"trust-snapshot/1.0.1"'], (
            conditionals
        )
        assert reader.status()["readable"] is True, reader.status()

    # The service is gone. A snapshot that could not be REVALIDATED is discarded rather than
    # served stale: it is no longer evidence that `s1` is still listed, and R12 denies on
    # "no longer know" exactly as it denies on "never knew".
    clock["now"] += TRUST_SNAPSHOT_REFRESH_SECONDS + 1.0
    with pytest.raises(Exception) as failure:
        reader()
    assert "ConnectError" in str(failure.value) or "Connect" in str(failure.value), failure.value
    assert reader.status()["readable"] is False, reader.status()


# =====================================================================================
# The WHOLE chain, end to end, on the served path (T-303 b)
#
# `test_a_booted_exchange_reads_r12_from_a_real_trust_service` above proves the exchange
# reads R12 from trust. It cannot prove S2, and the reason is the two things that document
# leaves out: it states no `sellers`, so no store agent is ever asked and there is no
# shortlist for a delisted store to be missing from; and it hand-types the registry row
# rather than letting the trust ENGINE decide who is out.
#
# The three properties asserted below are the ones S2 actually names, and each of them was
# measured false before the change that closes them:
#
#   1. a deployment can state its seller registry (bid endpoints, registered domains) AND
#      still read R12 from the trust service. Before: `bind_eligibility`'s first rung took
#      any document with `sellers` and bound `StaticSellerEligibility`, so the only
#      deployment shape that consulted trust was one that solicited nobody;
#   2. the RANKING gate reads the same live snapshot. Before: `app.state.trust_snapshot`
#      was bound only from a literal `trust_snapshot` key in the document, so a deployment
#      that did not type one out ranked against `{}` and excluded EVERY store
#      `blacklist_unreadable` — the fail-closed path firing on honest traffic;
#   3. the store the trust engine delisted disappears from the shortlist while the honest
#      ones remain on it.
# =====================================================================================
#: How far back the scripted evidence is placed. Decay is a function of recorded time
#: (D17/S3) and the served route scores against its own serve instant, so observations
#: written at the manifest's 2026-02 episode dates would be half-lives old by the time this
#: runs and every score would have decayed back toward the neutral prior. The SCRIPT is the
#: manifest's; only its position on the clock is chosen here.
EVIDENCE_WINDOW_DAYS = 30


def _observations(store_id: str, dishonest_id: str, manifest: Mapping[str, Any]) -> list[Any]:
    """One store's trust observations: the manifest's script for the adversary, clean for the rest.

    The dishonest store's rows are ``manifest.dishonest_store.behaviours`` — the approved
    ``(dim, type)`` pairs, replayed once per episode across the published ``episode_budget``,
    which is the schedule ``expected_trust_trajectory`` is graded against. The honest stores
    get ``fulfilled`` on every dimension, which is what "no complaint has ever been filed"
    looks like in this vocabulary.
    """
    from datetime import UTC, datetime, timedelta

    now = datetime.now(tz=UTC)

    def at(episode: int) -> str:
        moment = now - timedelta(days=EVIDENCE_WINDOW_DAYS - episode)
        return moment.isoformat(timespec="seconds").replace("+00:00", "Z")

    budget = int(manifest["episode_budget"])
    if store_id != dishonest_id:
        dims = sorted({str(row["dim"]) for row in manifest["dishonest_store"]["behaviours"]})
        return [
            {"dim": dim, "type": "fulfilled", "observed_at": at(episode)}
            for episode in range(1, budget + 1)
            for dim in dims
        ]
    return [
        {"dim": str(row["dim"]), "type": str(row["type"]), "observed_at": at(episode)}
        for episode in range(1, budget + 1)
        for row in manifest["dishonest_store"]["behaviours"]
    ]


def _scored_roster() -> tuple[list[dict[str, Any]], str]:
    """The approved roster as trust's own store records, carrying the manifest's evidence."""
    from fixtures.manifest import load_manifest

    manifest = load_manifest()
    dishonest_id = str(manifest["dishonest_store"]["store_id"])
    records = [
        {
            "store_id": str(row["store_id"]),
            "business_identity": str(row["business_identity"]),
            "domain": str(row["domain"]),
            "observations": _observations(str(row["store_id"]), dishonest_id, manifest),
        }
        for row in manifest["stores"]
    ]
    return records, dishonest_id


def _registry_the_engine_decided(records: list[dict[str, Any]]) -> tuple[Any, set[str]]:
    """The blacklist registry the trust ENGINE's own delisting decision implies, and who is on it.

    ``trust.snapshot.delisting.delisting_events`` IS the platform delisting a store: it reads
    the scores and the registry and returns the ``blacklisted`` ledger events they imply at
    ``as_of``. Nothing here decides who is out — the manifest's published
    ``blacklist_threshold`` does, through the trust engine, over the manifest's own scripted
    behaviours.

    **The registry row is written HERE because no product code writes it**, and that is the
    one link in this chain still open (see the report on T-303 b). The decision is computed by
    ``build_snapshot`` and published on ``snapshot["delistings"]``, the simulator seals it into
    the hash chain (T-303 a) — and no writer turns it into a row in ``app.seller_blacklist``,
    which is the only thing ``trust.snapshot.routes.blacklist_for`` reads. So the served
    ``GET /snapshot`` reports ``blacklisted: false`` for a store scoring 0.10 against a 0.35
    threshold, measured. This helper is that missing writer, standing in a test, and it is
    deliberately built out of the event's OWN payload rather than from a hand-typed identity:
    when the real writer lands it has to do exactly this.
    """
    from datetime import UTC, datetime

    from trust.scoring import Blacklist
    from trust.snapshot import build_snapshot

    as_of = datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    empty = Blacklist()
    decided = build_snapshot(records, blacklist=empty, as_of=as_of)["delistings"]

    registry = Blacklist()
    delisted: set[str] = set()
    for event in decided:
        if event["kind"] != "blacklisted":
            continue
        payload = event["payload"]
        registry.add(
            business_identity=str(payload["business_identity"]),
            reason_code=str(payload["reason_code"]),
            status="active",
            expires_at=payload.get("expires_at"),
        )
        delisted.add(str(payload["store_id"]))
    return registry, delisted


def _trust_service_over(records: list[dict[str, Any]], registry: Any) -> FastAPI:
    """A real ``trust.main:create_app()`` serving ``GET /snapshot`` over these records."""
    from trust.events import InMemoryEventStore
    from trust.main import create_app as create_trust

    app = create_trust()
    app.state.snapshot_stores = [
        {
            "store_id": row["store_id"],
            "business_identity": row["business_identity"],
            "observations": row["observations"],
        }
        for row in records
    ]
    app.state.snapshot_blacklist = registry
    app.state.event_store = InMemoryEventStore()
    return app


def _roster_agent_app(records: list[dict[str, Any]]) -> FastAPI:
    """A real store agent per rostered store, answering the published bid door."""
    app = FastAPI(title="roster-agent-double")

    def door_for(row: dict[str, Any]) -> Any:
        def door(body: dict[str, Any]) -> JSONResponse:
            return JSONResponse(
                status_code=200,
                content={
                    "auction_id": str(body.get("auction_id") or ""),
                    "store_id": row["store_id"],
                    "offer": {
                        "product_ref": "prod-1",
                        "unit_price": 15.0,
                        "currency": "USD",
                        "commitments": [],
                        "total_price": 15.0,
                        "expires_at": "2999-01-01T00:00:00Z",
                        "checkout_url": f"https://{row['domain']}/cart/44352913:1",
                    },
                    "claims": [],
                    "message": None,
                    "agent_version": "roster-agent-double/1.0.0",
                    "signature": None,
                    "schema_version": "1.0.0",
                },
            )

        return door

    for row in records:
        app.post(f"/{row['store_id']}/v1/bid-requests")(door_for(dict(row)))
    return app


def test_a_store_the_trust_engine_delisted_leaves_the_shortlist_the_others_stay_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unwired: None
) -> None:
    """S2, on the served path: the dishonest store is gone and the honest ones are still there.

    Everything real. A ``trust.main:create_app()`` scoring the manifest's own scripted
    behaviours; the registry carrying the delisting THAT ENGINE decided; store agents on
    loopback answering the published bid door; and ``exchange.main:create_app()`` reading one
    deployment document. No ``configure_*`` call anywhere in this test.

    ``"eligibility": "trust"`` is the word that makes it possible for the document to state a
    seller registry — which it must, or there are no bid endpoints and no shortlist — without
    that registry also becoming the R12 answer.
    """
    records, dishonest_id = _scored_roster()
    registry, delisted = _registry_the_engine_decided(records)
    assert delisted == {dishonest_id}, (
        f"the trust engine delisted {sorted(delisted)}; this test needs it to delist exactly "
        f"the manifest's dishonest store, or it is not discriminating"
    )
    honest = {row["store_id"] for row in records} - delisted

    with serve(_trust_service_over(records, registry)) as trust_url:
        with serve(_roster_agent_app(records)) as agent_url:
            document = {
                "trust_url": trust_url,
                "sellers": [
                    {
                        "store_id": row["store_id"],
                        # The platform states WHERE this store bids and WHICH host it owns,
                        # and leaves who may participate to the trust service.
                        "eligibility": "trust",
                        "registered_domain": row["domain"],
                        "bid_endpoint": f"{agent_url}/{row['store_id']}/v1/bid-requests",
                    }
                    for row in records
                ],
                "checkout_mode": "redirect",
            }
            path = tmp_path / "deployment.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            monkeypatch.setenv(ENV_DEPLOYMENT, str(path))
            monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

            with served_exchange() as client:
                body = _open_a_roster_auction(
                    client, [{"store_id": r["store_id"]} for r in records]
                )
                # The CHECKOUT gate, in the same served run and through the same live source:
                # `accept/routes.py` re-reads `app.state.seller_eligibility` and refuses on
                # anything but ELIGIBLE, so a 200 carrying a real code is that gate consulting
                # trust and admitting. It is the positive control the gate's own denial test
                # (`test_accept_routes.py::test_a_store_blacklisted_after_bidding_gets_no_code_
                # through_the_route`, which wires a static double and has no admitting arm)
                # does not carry. A trust-sourced DENIAL here cannot be produced in one request
                # cycle by construction — gate 1 reads the same source a moment earlier and
                # stops the store before it can bid — so this gate only ever fires on a verdict
                # that changed between solicitation and checkout.
                top = body["shortlist"]["slots"][0]
                accepted = client.post(
                    f"/auctions/{body['auction_id']}/accept", json={"bid_ref": top["bid_ref"]}
                )

    denials = {row["store_id"]: row for row in body["denied"]}
    solicited = set(body["solicited"])
    ranked = {row["store_id"] for row in body["ranked"]}
    # A published `ShortlistSlot` names a `bid_ref`, never a store — the buyer-facing object
    # deliberately does not publish who is behind a slot — so the store is resolved back
    # through `ranked`, which is where the two are joined.
    seller_of = {row["bid_ref"]: row["store_id"] for row in body["ranked"]}
    slots = {seller_of[slot["bid_ref"]] for slot in body["shortlist"]["slots"]}

    # 1. the delisted store, refused at the solicitation gate with a TRUST-sourced reason
    assert dishonest_id in denials, f"denied={sorted(denials)} solicited={sorted(solicited)}"
    assert denials[dishonest_id]["status"] == "blacklisted", denials[dishonest_id]
    assert "trust-eligibility" in denials[dishonest_id]["reason"], denials[dishonest_id]
    assert dishonest_id not in solicited, "a delisted store was still asked to bid"
    assert dishonest_id not in slots, "a delisted store reached the buyer's shortlist"

    # 2. the positive control, in the SAME run: a gate that refuses everyone is not a fix
    assert honest <= solicited, (
        f"the served exchange did not solicit {sorted(honest - solicited)}: "
        f"{[(k, v['reason']) for k, v in denials.items()]}"
    )
    assert honest <= ranked, (
        f"honest stores {sorted(honest - ranked)} were solicited and then dropped by the "
        f"ranking gate: excluded={body['excluded']}"
    )
    assert slots, f"nobody reached the shortlist: ranked={sorted(ranked)}"
    assert slots <= honest, f"the shortlist carries a store trust did not clear: {sorted(slots)}"

    # 3. the checkout gate admits that store too, so all three R12 gates are consulting the
    #    same live trust source and honest traffic reaches a real discount code.
    assert accepted.status_code == 200, (
        f"the checkout gate refused a store the trust service cleared "
        f"{accepted.status_code}: {accepted.text}"
    )
    assert accepted.json()["code"].startswith("PSX-"), accepted.json()
    assert seller_of[top["bid_ref"]] in honest, seller_of[top["bid_ref"]]


def test_the_ranking_gates_live_snapshot_tells_the_same_three_states_apart() -> None:
    """R12's middle gate, in the three states the whole fix turns on.

    A direct-call unit test of :class:`~exchange.composition.LiveTrustSnapshot` — one of the
    handful in this file that issues no request; see the module docstring. It is graded
    through :func:`exchange.ranking.filters.blacklist_reason`, which is the function ``rank()``
    actually applies, so what is asserted here is what the gate will do rather than what the
    mapping returns.

    The three states must not collapse, and the failing one is the reason this class exists:
    a reader that raised would take an exception straight through ``rank()`` and turn a trust
    outage into a 500 on ``POST /auctions``, while a reader that answered "no rows" silently
    would be indistinguishable from a trust service with nothing to say. Both must deny, and
    the delisted store must deny for a DIFFERENT stated reason than the unreadable one.
    """
    from exchange.composition import LiveTrustSnapshot
    from exchange.eligibility.trust_backed import TrustSnapshotUnavailable
    from exchange.ranking.filters import blacklist_reason

    rows = {
        "honest": {"store_id": "honest", "blacklisted": False},
        "delisted": {"store_id": "delisted", "blacklisted": True},
    }

    def unreachable() -> dict[str, Any]:
        raise TrustSnapshotUnavailable("ConnectError: nodename nor servname provided")

    answering = LiveTrustSnapshot(lambda: rows)
    down = LiveTrustSnapshot(unreachable)

    # trust answered and holds nothing against this store -> the ONLY admitting state
    assert blacklist_reason("honest", answering) is None
    # trust answered and has delisted it
    delisted = blacklist_reason("delisted", answering)
    assert delisted is not None and "blacklisted_store" in delisted, delisted
    # trust answered and has no opinion on this store — denied, and NOT as a delisting
    unknown = blacklist_reason("never-heard-of", answering)
    assert unknown is not None and "blacklist_unreadable" in unknown, unknown
    # trust could not be read at all. The positive control is the first line: the SAME store
    # id, admitted when trust answers and refused when it does not.
    unreadable = blacklist_reason("honest", down)
    assert unreadable is not None and "blacklist_unreadable" in unreadable, unreadable
    assert len(down) == 0 and dict(answering) == rows
