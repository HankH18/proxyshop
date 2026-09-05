"""Two defects that stop a DEPLOYED exchange from shortlisting anybody.

Run them on their own::

    PROXYSHOP_WORKER=6 .venv/bin/python -m pytest \\
        apps/exchange/tests/test_composition_catalog_and_refusals.py -q

Same rule as ``test_composition_root.py``, and for the same reason: **the only thing this
module calls to build the app is ``create_app()``.** No HTTP test here wires the app it is
testing. Everything the exchange needs it reads for itself out of the deployment document,
which is what a person running the service does.

The two defects
---------------
**1. No deployment document could supply a catalog snapshot.** ``composition.py`` named no
catalog key and no catalog collaborator — every occurrence of the word in it was ``catalogue``,
about clusters — so ``ranking.serving.catalog_of`` kept its
``NoCatalogSnapshots`` default in every deployment there is. With no catalog the verifier is
never run, every claim comes back ``unsupported``, R19 refuses to let an unsupported claim
satisfy a hard constraint, and an intent carrying any must-have is answered with an empty
shortlist. A real shopper sentence always yields at least a price constraint, so a deployed
exchange shortlisted nobody; the green demonstrations in this tree are the ones whose
fixtures happen to state no must-have, which is why it went unnoticed.

**2. A store's real refusal was reported as silence.** ``BidRequest`` requires a ``profile``
and the solicitor coerced a missing one to ``{}``, which is not a ``BuyerProfile``; the store
agent answered ``422`` and the exchange reported ``no_response``. The same line reported the
contract's own ``204`` decline that way too. A schema violation the exchange itself caused, a
store that declined and a store that was switched off were one indistinguishable fact.

Every assertion here carries its own CONTROL — the same request, one input removed — because
an assertion whose failure mode nobody has seen is worth very little. The controls are
parameters rather than prose, so they run on every invocation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from contracts import BidRequest
from exchange.accept.offer import use_registered_domains
from exchange.composition import (
    ENV_DEPLOYMENT,
    ENV_DEPLOYMENT_JSON,
    DeploymentConfigurationError,
    parse_deployment,
    solicitation_profile,
)
from exchange.main import create_app
from exchange.ranking.verification import MAX_CATALOG_PRODUCTS
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from proxyshop_support.asgi_server import serve

REQUEST_TIMEOUT_SECONDS = 30.0

PRODUCT = "prod-1"

#: The two sellers this deployment registers, and the one attribute the shopper insists on.
STORES: tuple[dict[str, Any], ...] = (
    {"store_id": "s1", "domain": "s1.example.com", "list_price": 100.0, "unit_price": 88.0},
    {"store_id": "s2", "domain": "s2.example.com", "list_price": 120.0, "unit_price": 111.0},
)

#: The catalogue reading each store's claim is graded against. It AGREES with what the agents
#: below claim, so an honest bid verifies; the point of the test is not the comparison, it is
#: that the comparison happens at all.
CATALOGUE_CAPACITY = 35

#: An intent with a REAL hard constraint. ``test_composition_root.py``'s ``INTENT`` states
#: ``hard_constraints: []`` on purpose — "this buyer stated no must-haves" — and that is
#: exactly the case in which the missing catalog is invisible.
CONSTRAINED_INTENT: dict[str, Any] = {
    "intent_id": "intent-hard-1",
    "cluster_id": "cluster-1",
    "query": "a 35 litre bag, at least 30 litres",
    "hard_constraints": [{"field": "capacity_l", "op": "gte", "value": 30}],
    "preferences": [],
    "created_at": "2026-01-01T00:00:00Z",
    "schema_version": "1.0.0",
}


# =====================================================================================
# The market: real store agents on a real port
# =====================================================================================
def _bid(row: dict[str, Any], auction_id: str) -> dict[str, Any]:
    """One published ``Bid``, carrying the claim the shopper's must-have is decided on."""
    return {
        "auction_id": auction_id,
        "store_id": row["store_id"],
        "offer": {
            "product_ref": PRODUCT,
            "unit_price": row["unit_price"],
            "currency": "USD",
            "commitments": [],
            "total_price": row["unit_price"],
            "expires_at": "2999-01-01T00:00:00Z",
            "checkout_url": f"https://{row['domain']}/cart/44352913:1",
        },
        # No `status` on the claim, and its absence is the point: what decides the constraint
        # is the verdict the EXCHANGE reaches against its own catalogue.
        "claims": [
            {
                "key": "capacity_l",
                "value": CATALOGUE_CAPACITY,
                "provenance": {
                    "source": "owner_statement",
                    "ref": "ref:capacity_l",
                    "authority_rank": 1,
                },
            }
        ],
        "message": None,
        "agent_version": "store-agent-double/1.0.0",
        "signature": None,
        "schema_version": "1.0.0",
    }


def _market_app() -> FastAPI:
    """Store agents that bid, and three that refuse in the ways the contract allows.

    **Every door here declares its body as the pinned ``BidRequest``**, which is the one
    property that makes this module a control on the solicitation rather than a rehearsal of
    it. ``packages/store-agent``'s real ``answer_bid_request`` declares exactly that, so a
    solicitation carrying ``profile: {}`` is answered ``422`` here for the same reason and by
    the same model. A double that took ``dict[str, Any]`` would accept a body no deployed
    store agent accepts, and the profile repair would have no failing case anywhere.
    """
    app = FastAPI(title="store-agent-doubles")

    def door_for(row: dict[str, Any]) -> Any:
        # A closure, never a defaulted parameter: FastAPI reads a defaulted `dict` argument as
        # a second embedded body field, so `def door(body, row=row)` answers 422 to a
        # well-formed BidRequest and every store reads as refusing for the wrong reason.
        def door(bid_request: BidRequest) -> JSONResponse:
            return JSONResponse(status_code=200, content=_bid(row, bid_request.auction_id))

        return door

    for row in STORES:
        app.post(f"/{row['store_id']}/v1/bid-requests")(door_for(dict(row)))

    @app.post("/decliner/v1/bid-requests")
    def declines(bid_request: BidRequest) -> Response:
        """The contract's own decline: 204, no body, the reason in the published header."""
        return Response(
            status_code=204, headers={"x-proxyshop-decline-reason": "no_matching_product"}
        )

    @app.post("/shouter/v1/bid-requests")
    def shouts(bid_request: BidRequest) -> Response:
        """A decline whose reason is a lever: 4 KiB of it, on an unauthenticated path."""
        return Response(status_code=204, headers={"x-proxyshop-decline-reason": "z" * 4096})

    @app.post("/refuser/v1/bid-requests")
    def refuses(bid_request: BidRequest) -> JSONResponse:
        """A store that rejects a solicitation it could in fact read — a store-side refusal."""
        return JSONResponse(status_code=422, content={"detail": "this store is not bidding"})

    @app.post("/misreporter/v1/bid-requests")
    def misreports(bid_request: BidRequest) -> JSONResponse:
        """A 503 that also sends the DECLINE header, to claim it chose not to bid."""
        return JSONResponse(
            status_code=503,
            content={"detail": "down"},
            headers={"x-proxyshop-decline-reason": "no_matching_product"},
        )

    return app


def _catalog_document() -> dict[str, Any]:
    return {
        row["store_id"]: {
            "snapshot_id": f"snap-{row['store_id']}",
            "captured_at": "2026-01-01T00:00:00Z",
            "products": [
                {
                    "product_ref": PRODUCT,
                    "canonical_name": PRODUCT,
                    "evidence_ref": f"snap-{row['store_id']}#{PRODUCT}",
                    "attributes": {"capacity_l": {"value": CATALOGUE_CAPACITY}},
                }
            ],
        }
        for row in STORES
    }


def _document(agent_base_url: str, *, with_catalog: bool) -> dict[str, Any]:
    """The document a person writes to deploy this exchange."""
    document: dict[str, Any] = {
        "sellers": [
            {
                "store_id": row["store_id"],
                "eligibility": "eligible",
                "registered_domain": row["domain"],
                "bid_endpoint": f"{agent_base_url}/{row['store_id']}/v1/bid-requests",
            }
            for row in STORES
        ],
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
    if with_catalog:
        document["catalog"] = _catalog_document()
    return document


@pytest.fixture
def unwired() -> Iterator[None]:
    """Leave the process-wide registered-domain seam exactly as this file found it."""
    previous = use_registered_domains(None)
    try:
        yield
    finally:
        use_registered_domains(previous)


@pytest.fixture(scope="module")
def agent_url() -> Iterator[str]:
    with serve(_market_app()) as url:
        yield url


@contextmanager
def served_exchange() -> Iterator[httpx.Client]:
    """The exchange as a served process, on a real TCP socket, wired by nothing."""
    with serve(create_app()) as url:
        with httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            yield client


def _roster(rows: tuple[dict[str, Any], ...] = STORES) -> list[dict[str, Any]]:
    return [
        {
            "store_id": row["store_id"],
            "tier": 1,
            "product_ref": PRODUCT,
            "list_price": row["list_price"],
            "max_discount_pct": 20.0,
        }
        for row in rows
    ]


# =====================================================================================
# 1. The catalog key: a hard-constrained intent produces a shortlist
# =====================================================================================
@pytest.mark.parametrize(
    "with_catalog", [True, False], ids=["catalog-stated", "control-catalog-omitted"]
)
def test_a_deployed_exchange_shortlists_a_hard_constrained_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unwired: None,
    agent_url: str,
    with_catalog: bool,
) -> None:
    """The same request, the same market, differing only in one document key.

    The ``control-catalog-omitted`` case is not decoration: it is the measurement this whole
    change exists for, and it runs on every invocation. Delete the ``catalog`` handling from
    ``composition.py`` and the first case reports exactly what the second one asserts —
    ``ranked: []`` and every candidate excluded ``hard_constraint_unsatisfied``, on a ``201``
    that looks like a policy decision about the shopper's must-have.
    """
    document = tmp_path / "deployment.json"
    document.write_text(
        json.dumps(_document(agent_url, with_catalog=with_catalog), indent=2), encoding="utf-8"
    )
    monkeypatch.setenv(ENV_DEPLOYMENT, str(document))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

    with served_exchange() as client:
        response = client.post(
            "/auctions",
            json={
                "intent": CONSTRAINED_INTENT,
                "profile": {"pseudonym": "psn-hard-1", "buckets": {}},
                "roster": _roster(),
            },
        )

    assert response.status_code == 201, f"{response.status_code}: {response.text}"
    body = response.json()

    # True either way, and asserted either way: the stores really did bid. Without this the
    # empty shortlist below could be a solicitation that never landed rather than a claim
    # that was never checked, and the two have different fixes.
    assert body["denied"] == [], body["denied"]
    assert [entry["fallback"] for entry in body["entries"]] == [False, False], body["entries"]

    excluded = {row["store_id"]: row["exclusion_reasons"] for row in body["excluded"]}
    if with_catalog:
        assert body["ranked"], f"nothing was ranked; the filters excluded {excluded}"
        assert len(body["shortlist"]["slots"]) == len(STORES), (
            f"the ranking produced {len(body['ranked'])} row(s) and "
            f"{len(body['shortlist']['slots'])} shortlist slot(s); exclusions were {excluded}"
        )
        assert excluded == {}, excluded
        return

    # The control. This is the measured behaviour of every deployment of this exchange before
    # the `catalog` key existed, and it is what the case above must not be able to report.
    assert body["ranked"] == [], body["ranked"]
    assert body["shortlist"]["slots"] == [], body["shortlist"]
    assert set(excluded) == {row["store_id"] for row in STORES}, excluded
    for store_id, reasons in excluded.items():
        assert any("hard_constraint_unsatisfied" in reason for reason in reasons), (
            f"{store_id} was excluded for something other than the unsatisfiable constraint, "
            f"so this control is measuring the wrong hole: {reasons}"
        )


def test_a_shopper_who_states_a_must_have_can_actually_buy_what_they_are_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unwired: None, agent_url: str
) -> None:
    """The shortlist is not the finish line — the whole journey, on one hard constraint.

    Asserted separately from the shortlist because they fail for different reasons and the
    accept path re-decides things the ranking did not: it re-reads R12 and checks the checkout
    host against the platform registry. A shortlist nobody can buy from would satisfy every
    other assertion in this file.

    ``test_composition_root.py`` already drives an accept end to end — on an intent stating
    NO must-have, which is the one case in which none of this module's subject matter is
    engaged.
    """
    document = tmp_path / "deployment.json"
    document.write_text(
        json.dumps(_document(agent_url, with_catalog=True), indent=2), encoding="utf-8"
    )
    monkeypatch.setenv(ENV_DEPLOYMENT, str(document))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

    with served_exchange() as client:
        opened = client.post(
            "/auctions",
            json={
                "intent": CONSTRAINED_INTENT,
                "profile": {"pseudonym": "psn-buy-1", "buckets": {}},
                "roster": _roster(),
            },
        )
        assert opened.status_code == 201, f"{opened.status_code}: {opened.text}"
        body = opened.json()
        assert body["shortlist"]["slots"], f"nothing to buy; exclusions were {body['excluded']}"

        top = body["shortlist"]["slots"][0]
        accepted = client.post(
            f"/auctions/{body['auction_id']}/accept", json={"bid_ref": top["bid_ref"]}
        )

    assert accepted.status_code == 200, f"{accepted.status_code}: {accepted.text}"
    payload = accepted.json()
    assert payload["code"].startswith("PSX-"), payload
    assert payload["permalink_url"].startswith(f"https://{STORES[0]['domain']}/cart/"), payload
    assert f"discount={payload['code']}" in payload["permalink_url"], payload


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        pytest.param({"catalog": []}, "must be a JSON object keyed by store_id", id="not-a-map"),
        pytest.param({"catalog": {"s1": 7}}, "must be a JSON object", id="row-not-a-map"),
        pytest.param(
            {"catalog": {"s1": {"products": [{"product_ref": PRODUCT}]}}},
            "states no 'snapshot_id'",
            id="no-snapshot-id",
        ),
        pytest.param(
            {"catalog": {"s1": {"snapshot_id": "snap-s1", "products": "everything"}}},
            "it must be a JSON array",
            id="products-not-an-array",
        ),
        pytest.param(
            {"catalog": {"s1": {"snapshot_id": "snap-s1", "products": []}}},
            "holds no products",
            id="no-products",
        ),
        pytest.param(
            {
                "catalog": {
                    "s1": {"snapshot_id": "snap-s1", "products": [{"canonical_name": "a bag"}]}
                }
            },
            "states no 'product_ref'",
            id="product-names-no-ref",
        ),
        pytest.param(
            {
                "catalog": {
                    "s1": {
                        "snapshot_id": "snap-s1",
                        "products": [
                            {"product_ref": f"p{n}"} for n in range(MAX_CATALOG_PRODUCTS + 1)
                        ],
                    }
                }
            },
            f"this exchange reads at most {MAX_CATALOG_PRODUCTS} per store",
            id="over-the-product-cap",
        ),
        # A name that is PRESENT and falsy, and one that is present and not a name at all.
        # `str(row.get(field) or "")` reads both as absent, which tells the operator to add a
        # line that is already there — and lets `true` through as the ref "True".
        pytest.param(
            {"catalog": {"s1": {"snapshot_id": "snap-s1", "products": [{"product_ref": 0}]}}},
            "states product_ref=0, which is not a name",
            id="product-ref-is-a-falsy-number",
        ),
        pytest.param(
            {"catalog": {"s1": {"snapshot_id": "snap-s1", "products": [{"product_ref": True}]}}},
            "states product_ref=True, which is not a name",
            id="product-ref-is-a-boolean",
        ),
        pytest.param(
            {"catalog": {"s1": {"snapshot_id": ["a"], "products": [{"product_ref": PRODUCT}]}}},
            "states snapshot_id=['a'], which is not a name",
            id="snapshot-id-is-a-list",
        ),
    ],
)
def test_a_malformed_catalog_is_a_503_that_names_the_offending_row(
    monkeypatch: pytest.MonkeyPatch,
    unwired: None,
    mutation: dict[str, Any],
    expected: str,
) -> None:
    """Refused loudly, because the silent version of each rule is an empty shortlist.

    Every row here degrades, if it is not refused, to the SAME symptom: a store whose claims
    come back ``unsupported`` and which is therefore excluded from every constrained auction,
    with nothing in the ``201`` pointing at the document. A 503 naming the row is the whole
    difference between a five-minute fix and a day of reading exclusion reasons.
    """
    document = _document("http://127.0.0.1:1", with_catalog=False)
    document.update(mutation)
    monkeypatch.delenv(ENV_DEPLOYMENT, raising=False)
    monkeypatch.setenv(ENV_DEPLOYMENT_JSON, json.dumps(document))

    with served_exchange() as client:
        response = client.post("/auctions", json={"intent": CONSTRAINED_INTENT, "roster": []})

    assert response.status_code == 503, f"{response.status_code}: {response.text}"
    detail = response.json()["detail"]
    assert expected in detail, detail
    # It names the STORE, not merely the rule — a message saying "a product row is wrong"
    # about a hundred-store document has told the operator almost nothing. Only when there IS
    # a store to name: a `catalog` that is not an object keyed by store has no row to point at,
    # and asserting one there would be asserting a sentence the parser cannot honestly write.
    if isinstance(mutation["catalog"], dict):
        assert "'s1'" in detail, detail


def test_a_document_that_states_no_catalog_leaves_the_seam_exactly_as_it_was(
    unwired: None,
) -> None:
    """The property the new key may not break: nothing configured is today's behaviour."""
    from exchange.ranking.serving import catalog_of
    from exchange.ranking.verification import NoCatalogSnapshots

    deployment = parse_deployment(_document("http://127.0.0.1:1", with_catalog=False), source="x")
    assert deployment.catalog is None

    app = create_app()
    from exchange.composition import configure_exchange  # noqa: PLC0415

    bound = configure_exchange(app, deployment)
    assert "ranking_catalog" not in bound, bound
    assert isinstance(catalog_of(app), NoCatalogSnapshots)


def test_an_explicitly_empty_catalog_is_a_statement_and_is_bound(unwired: None) -> None:
    """``"catalog": {}`` and no ``catalog`` key are the same behaviour, different statements.

    The same distinction ``intent_clusters`` draws. Binding the empty source records that a
    person decided this exchange holds no snapshot for anybody, instead of leaving the seam
    looking unwired to the next reader of ``app.state``.
    """
    from exchange.composition import configure_exchange  # noqa: PLC0415
    from exchange.ranking.serving import catalog_of
    from exchange.ranking.verification import StaticCatalogSnapshots

    document = _document("http://127.0.0.1:1", with_catalog=False)
    document["catalog"] = {}
    deployment = parse_deployment(document, source="x")
    assert deployment.catalog == {}

    app = create_app()
    bound = configure_exchange(app, deployment)
    assert "ranking_catalog" in bound, bound
    catalog = catalog_of(app)
    assert isinstance(catalog, StaticCatalogSnapshots)
    assert len(catalog) == 0


def test_the_composition_root_never_overwrites_a_catalog_a_deployment_chose(
    unwired: None,
) -> None:
    """A deployment that wired its own catalog keeps it — the rule for all seven keys."""
    from exchange.composition import configure_exchange  # noqa: PLC0415
    from exchange.ranking.serving import configure_ranking  # noqa: PLC0415
    from exchange.ranking.verification import StaticCatalogSnapshots

    chosen = StaticCatalogSnapshots({"s1": {"snapshot_id": "chosen", "products": []}})
    app = create_app()
    configure_ranking(app, catalog=chosen)

    deployment = parse_deployment(_document("http://127.0.0.1:1", with_catalog=True), source="x")
    bound = configure_exchange(app, deployment)

    assert app.state.ranking_catalog is chosen
    assert "ranking_catalog" not in bound, bound


def test_a_catalog_snapshot_the_verifier_cannot_walk_is_refused_before_it_is_deployed() -> None:
    """The document grammar, exercised directly, and its control.

    The control is the second half: the same builder, one rule satisfied, and it parses. A
    refusal test with no such case passes on a parser that refuses everything.
    """
    good = {
        "snapshot_id": "snap-s1",
        "products": [{"product_ref": PRODUCT, "attributes": {"capacity_l": {"value": 35}}}],
    }
    parsed = parse_deployment({"catalog": {"s1": good}}, source="x")
    assert parsed.catalog is not None
    assert parsed.catalog["s1"]["snapshot_id"] == "snap-s1"

    broken = {"snapshot_id": "snap-s1", "products": [{"attributes": {"capacity_l": {"value": 35}}}]}
    with pytest.raises(DeploymentConfigurationError) as raised:
        parse_deployment({"catalog": {"s1": broken}}, source="x")
    assert "product_ref" in str(raised.value), str(raised.value)


# =====================================================================================
# 2. A refusal is reported as itself
# =====================================================================================
def test_the_solicitation_carries_a_profile_the_published_contract_accepts() -> None:
    """``{}`` is not a ``BuyerProfile``, and the exchange used to send exactly that.

    The control is the first assertion: it drives the OLD value through the pinned model and
    shows it is rejected, naming both missing fields. Without it the second assertion would
    pass on a model that accepts anything, and this test would prove nothing.
    """
    from pydantic import ValidationError  # noqa: PLC0415

    def request_with(profile: Any) -> dict[str, Any]:
        return {
            "auction_id": "auction-1",
            "intent": CONSTRAINED_INTENT,
            "profile": profile,
            "respond_by": "2026-01-01T00:00:03.000Z",
        }

    # The control: what the solicitor sent before this repair.
    with pytest.raises(ValidationError) as rejected:
        BidRequest.model_validate(request_with({}))
    missing = {tuple(error["loc"]) for error in rejected.value.errors()}
    assert ("profile", "pseudonym") in missing, missing
    assert ("profile", "buckets") in missing, missing

    # What it sends now, for a buyer who named no profile at all.
    minted = solicitation_profile(None, auction_id="auction-1")
    accepted = BidRequest.model_validate(request_with(minted))
    assert accepted.profile.pseudonym == "anon-auction-1"
    assert accepted.profile.buckets.model_dump(exclude_none=True) == {"category_affinity": []}


def test_a_profile_the_buyer_did_state_is_carried_through_unchanged() -> None:
    """Only the handle is minted. Nothing about the shopper is invented or dropped."""
    stated = {"pseudonym": "psn-real", "buckets": {"budget_band": "mid", "region": "us-west"}}
    assert solicitation_profile(stated, auction_id="auction-1") == stated

    # A pseudonym that is present but unusable is replaced rather than sent — an empty string
    # fails `minLength: 1` and costs the auction every bid — and the buckets still survive.
    repaired = solicitation_profile(
        {"pseudonym": "  ", "buckets": {"region": "eu"}}, auction_id="a"
    )
    assert repaired == {"pseudonym": "anon-a", "buckets": {"region": "eu"}}


def test_a_store_that_refuses_is_named_rather_than_reported_as_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unwired: None, agent_url: str
) -> None:
    """Five stores, one auction: one bids, four refuse in four different ways.

    Before this repair every refusal read ``fallback_reason: "no_response"`` — measured over
    HTTP on the same market — so a store that declined, a store that rejected the request body
    and a store that was switched off were one fact. The bidder is the control: without it, a
    solicitor that answered "refused" to everything would pass.

    ``shouter`` is the second control, on the other axis. That header is chosen by a
    third-party store on an unauthenticated path and is echoed once per rostered store in a
    ``201``, so what reaches the answer is bounded — and bounded to a NAMED value, because an
    unrenderable reason and an absent one are different facts.

    ``misreporter`` is the third: it sends the DECLINE header on a ``503``. The header is the
    contract's word for a 204 and for nothing else, so an agent that is down cannot describe
    itself as a store that chose not to bid — the two have different owners and different
    fixes, which is the entire point of splitting them.
    """
    document = _document(agent_url, with_catalog=True)
    document["sellers"].extend(
        [
            {
                "store_id": store_id,
                "eligibility": "eligible",
                "registered_domain": f"{store_id}.example.com",
                "bid_endpoint": f"{agent_url}/{store_id}/v1/bid-requests",
            }
            for store_id in ("decliner", "shouter", "refuser", "misreporter")
        ]
    )
    for store_id in ("decliner", "shouter", "refuser", "misreporter"):
        document["trust_snapshot"]["stores"][store_id] = {
            "store_id": store_id,
            "blacklisted": False,
            "score": 0.8,
        }
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    monkeypatch.setenv(ENV_DEPLOYMENT, str(path))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

    roster = _roster((STORES[0],)) + [
        {
            "store_id": store_id,
            "tier": 1,
            "product_ref": PRODUCT,
            "list_price": 130.0,
            "max_discount_pct": 20.0,
        }
        for store_id in ("decliner", "shouter", "refuser", "misreporter")
    ]

    with served_exchange() as client:
        response = client.post(
            "/auctions",
            json={
                "intent": CONSTRAINED_INTENT,
                "profile": {"pseudonym": "psn-refusal-1", "buckets": {}},
                "roster": roster,
            },
        )

    assert response.status_code == 201, f"{response.status_code}: {response.text}"
    reasons = {entry["store_id"]: entry["fallback_reason"] for entry in response.json()["entries"]}

    # The control: a store that really did answer is still not a fallback at all.
    assert reasons["s1"] is None, reasons

    assert reasons["decliner"] == "store_declined:no_matching_product", reasons
    assert reasons["refuser"] == "store_refused:422", reasons
    assert reasons["shouter"] == "store_declined:undisclosed", reasons
    assert reasons["misreporter"] == "store_refused:503", reasons
    assert "no_response" not in set(reasons.values()), (
        f"a store that ANSWERED is still being reported as silent: {reasons}"
    )


def test_a_deployed_exchange_solicits_successfully_when_the_buyer_names_no_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unwired: None, agent_url: str
) -> None:
    """``profile`` is optional on ``POST /auctions`` and required on ``POST /v1/bid-requests``.

    The doubles in this module declare the pinned ``BidRequest``, exactly as
    ``packages/store-agent``'s real door does, so the old coercion fails this case here for
    the same reason and by the same model — measured: reverting `for_auction` to
    ``profile if isinstance(profile, Mapping) else {}`` turns this red and nothing else in
    the file.
    """
    document = tmp_path / "deployment.json"
    document.write_text(
        json.dumps(_document(agent_url, with_catalog=True), indent=2), encoding="utf-8"
    )
    monkeypatch.setenv(ENV_DEPLOYMENT, str(document))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

    with served_exchange() as client:
        response = client.post(
            "/auctions", json={"intent": CONSTRAINED_INTENT, "roster": _roster()}
        )

    assert response.status_code == 201, f"{response.status_code}: {response.text}"
    body = response.json()
    assert [entry["fallback_reason"] for entry in body["entries"]] == [None, None], body["entries"]
    assert len(body["shortlist"]["slots"]) == len(STORES), body["shortlist"]


def test_the_refusal_vocabulary_is_the_one_the_collector_publishes() -> None:
    """Every reason the solicitor can write groups onto a published word.

    Stated as a property rather than as three equalities, because the detail after the colon
    is open by design — an HTTP status, a store's own decline reason — and the FAMILY is what
    a loss report or an operator groups on.
    """
    from exchange.auction import FALLBACK_REASONS  # noqa: PLC0415
    from exchange.auction.collect import (  # noqa: PLC0415
        MAX_REFUSAL_DETAIL_LENGTH,
        STORE_DECLINED_REASON,
        STORE_REFUSED_REASON,
        fallback_reason_family,
        refusal_reason,
    )

    assert STORE_DECLINED_REASON in FALLBACK_REASONS
    assert STORE_REFUSED_REASON in FALLBACK_REASONS
    assert "no_response" in FALLBACK_REASONS  # the word this repair stops over-using

    assert refusal_reason(STORE_REFUSED_REASON, 422) == "store_refused:422"
    assert refusal_reason(STORE_DECLINED_REASON, None) == "store_declined"
    assert refusal_reason(STORE_DECLINED_REASON, "  ") == "store_declined"

    # Header injection and length, both answered by the allowlist rather than by an
    # encodability check — screening for what raises lets through exactly the two characters
    # that make a header illegal.
    assert (
        refusal_reason(STORE_DECLINED_REASON, "a\r\nX-Injected: 1") == "store_declined:undisclosed"
    )
    assert (
        refusal_reason(STORE_DECLINED_REASON, "z" * (MAX_REFUSAL_DETAIL_LENGTH + 1))
        == "store_declined:undisclosed"
    )

    for reason in FALLBACK_REASONS:
        assert fallback_reason_family(reason) == reason
    assert fallback_reason_family("store_refused:503") == STORE_REFUSED_REASON
    assert fallback_reason_family(None) is None


def test_a_buyer_stated_profile_the_contract_rejects_is_named_rather_than_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unwired: None, agent_url: str
) -> None:
    """The residual the profile repair deliberately does NOT close, pinned as behaviour.

    ``ProfileBuckets`` is ``extra="forbid"``, and the exchange does not rewrite a profile the
    buyer actually stated — inventing a different one would be answering a question the shopper
    did not ask. So a bucket key the contract does not declare still costs the auction every
    bid. What must never happen again is that it costs them SILENTLY: the caller has to be able
    to see that the profile it sent is what lost the auction.

    The control is the second case: the same request with the undeclared key removed, which
    bids and shortlists. Without it this test would pass on an exchange that refused everything.
    """
    document = tmp_path / "deployment.json"
    document.write_text(
        json.dumps(_document(agent_url, with_catalog=True), indent=2), encoding="utf-8"
    )
    monkeypatch.setenv(ENV_DEPLOYMENT, str(document))
    monkeypatch.delenv(ENV_DEPLOYMENT_JSON, raising=False)

    def entries_for(profile: dict[str, Any]) -> list[dict[str, Any]]:
        with served_exchange() as client:
            response = client.post(
                "/auctions",
                json={"intent": CONSTRAINED_INTENT, "profile": profile, "roster": _roster()},
            )
        assert response.status_code == 201, f"{response.status_code}: {response.text}"
        return response.json()["entries"]

    refused = entries_for({"pseudonym": "psn-1", "buckets": {"totally_unknown": "x"}})
    assert [entry["fallback_reason"] for entry in refused] == [
        "store_refused:422",
        "store_refused:422",
    ], refused

    # The control.
    accepted = entries_for({"pseudonym": "psn-1", "buckets": {"budget_band": "mid"}})
    assert [entry["fallback_reason"] for entry in accepted] == [None, None], accepted


def test_a_solicitor_cannot_spend_the_response_on_a_refusal_it_invented() -> None:
    """``collect_bids`` re-normalises the refusal field; it does not echo it.

    :data:`REFUSAL_FIELD` is written by the exchange's own solicitor, so this is defence in
    depth rather than a hole — but ``collect_bids`` is the PUBLIC boundary and it already
    bounds a store's prices and its arrival stamp. A second caller, or a second solicitor,
    must not be able to put 64 KiB of anything into an unauthenticated ``201``.

    The control is the first case: a well-formed refusal survives unchanged, so this is not
    measuring a function that discards everything.
    """
    from exchange.auction.collect import REFUSAL_FIELD, collect_bids  # noqa: PLC0415

    roster = [{"store_id": "s1", "tier": 1, "product_ref": PRODUCT, "list_price": 10.0}]

    def reason_for(refusal: Any) -> str | None:
        entries = collect_bids(
            roster, [{"store_id": "s1", "received_at": 1.0, REFUSAL_FIELD: refusal}], 2.0
        )
        return entries[0].fallback_reason

    # The control: a refusal the solicitor really writes comes back verbatim.
    assert reason_for("store_declined:no_matching_product") == "store_declined:no_matching_product"

    assert reason_for("store_refused:" + "z" * 65_536) == "store_refused:undisclosed"
    assert reason_for("z" * 65_536) == "store_refused:undisclosed"
    # A family nothing published is not honoured as one, and its tail is not mined for a
    # detail either: the whole string is the detail, and the allowlist answers it.
    assert reason_for("totally_made_up:404") == "store_refused:undisclosed"
    assert reason_for("no_matching_product") == "store_refused:no_matching_product"
    # An empty field is not a refusal at all, and the store is NOT reported as silent either:
    # a response is in hand, it simply carries no bid, which is the label that already existed
    # for exactly that.
    assert reason_for("") == "response_carried_no_bid"


def test_a_bid_cannot_spend_the_operators_catalogue_by_carrying_more_claims() -> None:
    """The bidder's factor no longer multiplies the operator's, and no verdict changes.

    ``claims`` arrives from a third-party store agent and the verifier walked the operator's
    product list once per claim, so the two multiplied. Measured over a real socket with ten
    bidding stores, a catalogue at ``MAX_CATALOG_PRODUCTS`` and 9,000 claims per bid:
    ``POST /auctions -> 201 in 34.71s`` before, ``3.67s`` after, against a ``3.30s`` control
    with no catalog configured.

    What is asserted here is the property that makes that safe: the verdicts are IDENTICAL to
    the ones the whole document produces. A bound that changed an answer would be a denial of
    service wearing a fix's name — which is what a cap on the claim COUNT was, and why it is
    not what landed: a cap grades the first N in the bidder's own order, so an honest store
    whose deciding evidence sits at position N+1 loses a constraint it satisfies.
    """
    from exchange.ranking.attestation import ATTESTATION_FIELD  # noqa: PLC0415
    from exchange.ranking.verification import (  # noqa: PLC0415
        StaticCatalogSnapshots,
        attest_candidate_claims,
    )

    # The auction's product is the LAST row, so a narrowing that took the first would answer
    # differently and this test would see it.
    decoys = [
        {"product_ref": f"decoy-{n}", "attributes": {"capacity_l": {"value": 1}}}
        for n in range(200)
    ]
    snapshot = {
        "snapshot_id": "snap-s1",
        "products": [
            *decoys,
            {
                "product_ref": PRODUCT,
                "attributes": {
                    "capacity_l": {"value": CATALOGUE_CAPACITY, "unit": "l"},
                    "colour": {"value": "black"},
                },
            },
        ],
    }
    catalog = StaticCatalogSnapshots({"s1": snapshot})

    # 200 claims — well past any cap a count-based bound would have used — with the one that
    # decides the shopper's must-have at the very end.
    claims = [{"key": "colour", "value": "black"} for _ in range(200)]
    claims.append({"key": "capacity_l", "value": CATALOGUE_CAPACITY})

    attested = attest_candidate_claims(claims, store_id="s1", product_ref=PRODUCT, catalog=catalog)

    assert len(attested) == len(claims), "a presented claim was dropped rather than graded"
    statuses = [row[ATTESTATION_FIELD]["status"] for row in attested]
    assert set(statuses) == {"verified"}, statuses
    # The unit is the EXCHANGE's, read off the narrowed row rather than off the decoys.
    assert attested[-1][ATTESTATION_FIELD]["unit"] == "l", attested[-1][ATTESTATION_FIELD]

    # The control, and the assertion that makes the bound honest: the same claims graded
    # against a snapshot holding ONLY that product answer identically, verdict for verdict.
    one_product = StaticCatalogSnapshots(
        {"s1": {"snapshot_id": "snap-s1", "products": [snapshot["products"][-1]]}}
    )
    alone = attest_candidate_claims(claims, store_id="s1", product_ref=PRODUCT, catalog=one_product)
    assert [row[ATTESTATION_FIELD] for row in alone] == [row[ATTESTATION_FIELD] for row in attested]


def test_a_snapshot_holding_several_products_and_no_named_one_stays_ambiguous() -> None:
    """Narrowing happens only when the AUCTION names a product, and this is why.

    With no ``product_ref``, a snapshot holding several products is the case
    ``claim_verification`` decides ``ambiguous`` — which of them the seller meant is exactly
    what is undecidable. A narrowing that picked one would turn an undecidable claim into a
    verified one, which is the direction R19 exists to refuse.
    """
    from exchange.ranking.attestation import ATTESTATION_FIELD  # noqa: PLC0415
    from exchange.ranking.verification import (  # noqa: PLC0415
        StaticCatalogSnapshots,
        attest_candidate_claims,
    )

    catalog = StaticCatalogSnapshots(
        {
            "s1": {
                "snapshot_id": "snap-s1",
                "products": [
                    {"product_ref": "a", "attributes": {"capacity_l": {"value": 35}}},
                    {"product_ref": "b", "attributes": {"capacity_l": {"value": 35}}},
                ],
            }
        }
    )
    claims = [{"key": "capacity_l", "value": 35}]

    unnamed = attest_candidate_claims(claims, store_id="s1", product_ref=None, catalog=catalog)
    assert unnamed[0][ATTESTATION_FIELD]["status"] == "ambiguous", unnamed[0]

    # The control: naming one resolves it, so the ambiguity above is about the missing ref and
    # not about a catalog this test built wrong.
    named = attest_candidate_claims(claims, store_id="s1", product_ref="b", catalog=catalog)
    assert named[0][ATTESTATION_FIELD]["status"] == "verified", named[0]


def test_the_catalog_a_source_hands_back_cannot_be_rewritten_through_the_copy() -> None:
    """``StaticCatalogSnapshots.snapshots`` is a DEEP copy, because a shallow one is not one.

    The composition root reads this back out of a catalog it has just validated. A one-level
    copy shares the ``products`` list, so a holder of the result could append a product to the
    catalogue the ranker grades claims against.
    """
    from exchange.ranking.verification import StaticCatalogSnapshots  # noqa: PLC0415

    source = StaticCatalogSnapshots(
        {"s1": {"snapshot_id": "snap-s1", "products": [{"product_ref": PRODUCT}]}}
    )
    handed_back = source.snapshots
    handed_back["s1"]["products"].append({"product_ref": "injected"})

    still_held = source.snapshot_for("s1")
    assert still_held is not None
    assert [p["product_ref"] for p in still_held["products"]] == [PRODUCT], still_held
