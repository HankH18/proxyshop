"""The served solicitation door, and the two offer fields without which a bid is unusable.

Three properties, and every one of them was FALSE on the tree this file was written against:

1. **The agent serves the door its contract publishes.** ``POST /v1/bid-requests`` — measured
   before: ``create_app().state.mounted_routers == []`` and ``app.openapi()['paths'] == {}``
   against a contract declaring one operation. The set-equality gate for that lives in
   ``test_repro_open_tickets.py`` (T-309); what is graded HERE is the door's *behaviour* — that
   it answers a realistic solicitation with a real bid, that a decline is a 204 naming its
   reason, and that an unconfigured process declines rather than inventing a store.

2. **Every offer the agent produces carries a ``checkout_url``.** Nothing in
   ``packages/store-agent/src`` wrote the field. ``apps/exchange/src/ranking`` drops every
   candidate whose offer states none, so a hand-wired exchange with real eligibility, a real
   trust snapshot and a real domain registry answered ``ranked: []`` — every store excluded
   ``off_domain_checkout`` (C10). Injecting only that field turned 0 ranked into 2.

3. **Every offer carries an ``expires_at``.** Already true on the bid path when this file was
   written, and asserted anyway: it is half of "an offer a buyer could actually complete", and
   an assertion that grades only the half that was broken stops grading the moment the other
   half breaks.

**The assertion is armed.** :func:`assert_completable` is the only thing standing between a
green run and a silently field-less offer, so it is itself under test:
``test_the_completability_assertion_is_load_bearing`` takes a real bid, drops one field at a
time, and requires the helper to RAISE. A sweep that cannot fail is the failure mode this repo
has shipped three times; this one is shown failing before it is trusted.

**On-domain is checked with the platform's own function**, ``exchange.checkout.domain
.is_on_domain``, not with a re-implementation. The property the agent owes is not "the URL looks
plausible" — it is "the door that refuses off-domain checkouts accepts this one", and the only
way two doors agree is by being one function.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from contracts import Bid, BidRequest
from contracts.boundary import parse_timestamp
from exchange.checkout.domain import is_on_domain
from fastapi.testclient import TestClient
from store_agent.main import create_app
from store_agent.runtime import Decline, DeclineReason, bid, is_decline, store_domain_host
from store_agent.solicitation import (
    CONTEXT_ENV,
    DECLINE_REASON_HEADER,
    DOMAIN_ENV,
    UNCONFIGURED_REASON,
    StoreContextError,
    configure_solicitation,
    load_context_from_env,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ENVELOPE_FIXTURE = REPO_ROOT / "fixtures" / "envelopes" / "store-alpha.approved.json"

CLUSTER = "cluster-warm-layers"
STORE_ID = "store-alpha"

#: The domain the *platform* would hold for this store. Stated here once and used on both sides
#: of every on-domain assertion, which is the only honest arrangement: the agent is given it as
#: configuration and the check is run against the same value, exactly as a deployment where the
#: merchant service and the seller registry are populated from one seller record.
STORE_DOMAIN = "store-alpha.example.com"

#: A variant id, because a cart permalink is variant-scoped (D25) and the shipped envelope
#: fixture carries none. Kept out of the fixture itself — that file is an approved artifact.
VARIANT_REF = "44352913"


def _fixture() -> dict[str, Any]:
    return json.loads(ENVELOPE_FIXTURE.read_text(encoding="utf-8"))


def context(*, domain: Any = STORE_DOMAIN, variant: bool = False, **overrides: Any) -> dict:
    """A store context in the shape the merchant service hands over."""
    fixture = _fixture()
    catalog = fixture["catalog"]
    if variant:
        catalog = {
            ref: ({**row, "variant_ref": VARIANT_REF} if ref == "prod-cap" else row)
            for ref, row in catalog.items()
        }
    built: dict[str, Any] = {
        "store_id": STORE_ID,
        "envelope": fixture["envelope"],
        "catalog": catalog,
        "live_state": {
            "prod-cap": {"in_stock": True, "units_left": 7},
            "prod-floor": {"in_stock": True, "units_left": 3},
        },
        "learned_policy": None,
        "network_priors": {CLUSTER: {"depth_buckets": [0.0, 0.05, 0.1, 0.15, 0.2]}},
    }
    if domain is not None:
        built["store_domain"] = domain
    built.update(overrides)
    return built


def request_body(auction_id: str = "auc-0001", cluster_id: str = CLUSTER, **over: Any) -> dict:
    """A `BidRequest` as JSON, matching the example in the published store-agent contract."""
    body: dict[str, Any] = {
        "auction_id": auction_id,
        "intent": {
            "intent_id": "int-0001",
            "cluster_id": cluster_id,
            "query": "a warm mid-layer for cold commutes",
            "category": "outerwear",
            "hard_constraints": [{"field": "material", "op": "eq", "value": "merino wool"}],
            "preferences": [{"field": "price", "direction": "minimize", "weight": 1.0}],
            "ship_to": "US-CA",
            "currency": "USD",
            "budget_band": "50-150",
            "created_at": "2026-01-01T00:00:00Z",
            "schema_version": "1.0.0",
        },
        "profile": {
            "pseudonym": "psn-0001",
            "buckets": {
                "budget_band": "50-150",
                "category_affinity": ["outerwear"],
                "frequency_tier": "occasional",
                "region": "US-CA",
                "first_time": False,
            },
        },
        "respond_by": "2999-01-01T00:00:00Z",
    }
    body.update(over)
    return body


def answered(ctx: dict[str, Any], body: dict[str, Any] | None = None) -> Bid:
    """A bid that must BE a bid. A decline here is a failure to report, never a silent skip."""
    answer = bid(body or request_body(), ctx)
    assert not is_decline(answer), f"expected a bid, got {answer}"
    return answer


# =============================================================================================
# The assertion everything below leans on, and the control that arms it.
# =============================================================================================


def assert_completable(offer: Any, *, domain: str) -> None:
    """Raise unless this offer is one a buyer could actually be sent to a checkout for.

    Takes a mapping or the `Offer` model, because it is used on both a library answer and a
    parsed HTTP response body and the two must be graded identically.
    """
    fields = offer if isinstance(offer, dict) else offer.model_dump(mode="json")

    url = fields.get("checkout_url")
    assert url, (
        f"the offer states no checkout_url ({url!r}); every candidate without one is dropped "
        f"from the shortlist by apps/exchange/src/ranking, which is how a fully-built agent "
        f"bid its way to `ranked: []`"
    )
    assert is_on_domain(url, domain), (
        f"checkout_url {url!r} is not on the seller's registered domain {domain!r} by the "
        f"platform's own C10 check (exchange.checkout.domain.is_on_domain)"
    )

    expires_at = fields.get("expires_at")
    assert expires_at, (
        f"the offer states no expires_at ({expires_at!r}); contracts.boundary.validate_bid "
        f"refuses an offer nobody can price the risk of"
    )
    assert parse_timestamp(expires_at) is not None, (
        f"expires_at {expires_at!r} is not an instant the boundary can read"
    )


@pytest.mark.parametrize("dropped", ["checkout_url", "expires_at"])
@pytest.mark.parametrize("how", ["removed", "null", "empty"])
def test_the_completability_assertion_is_load_bearing(dropped: str, how: str) -> None:
    """The control. Drop a field from a real offer and the helper above must go RED.

    Six cases, because there are three ways a field goes missing — the key deleted, the key
    present and `null` (which is what `Offer` serialises an unset field to), and the key present
    and empty — and a helper that caught only the first would pass a bid whose `checkout_url` is
    `None`, which is exactly the shape this whole file exists to refuse.
    """
    offer = answered(context()).offer.model_dump(mode="json")
    assert_completable(offer, domain=STORE_DOMAIN)  # the unmutated original is accepted

    mutated = copy.deepcopy(offer)
    if how == "removed":
        del mutated[dropped]
    else:
        mutated[dropped] = None if how == "null" else ""

    with pytest.raises(AssertionError, match=dropped):
        assert_completable(mutated, domain=STORE_DOMAIN)


def test_the_off_domain_half_of_the_assertion_is_load_bearing() -> None:
    """And a URL on somebody else's host is refused, so "present" is not the whole check."""
    offer = answered(context()).offer.model_dump(mode="json")
    offer["checkout_url"] = "https://attacker.tld/cart/1:1"
    with pytest.raises(AssertionError, match="registered domain"):
        assert_completable(offer, domain=STORE_DOMAIN)


# =============================================================================================
# 1. Every offer the agent produces is completable.
# =============================================================================================


@pytest.mark.parametrize(
    "ctx",
    [
        pytest.param(context(), id="cold-no-learned-policy"),
        pytest.param(context(variant=True), id="cold-variant-scoped-catalog"),
        pytest.param(
            context(
                learned_policy={
                    "version": "policy-v2",
                    "actions": {CLUSTER: {"discount_pct": 10.0, "commitment_keys": []}},
                }
            ),
            id="warm-learned-policy-with-a-granted-discount",
        ),
        pytest.param(
            context(envelope={**_fixture()["envelope"], "intro_discount_pct": 5.0}),
            id="cold-with-the-envelopes-intro-rule",
        ),
        pytest.param(context(domain="https://store-alpha.example.com"), id="domain-with-scheme"),
        pytest.param(context(domain="Store-Alpha.Example.Com."), id="domain-mixed-case-dotted"),
        pytest.param(
            context(domain=None, envelope={**_fixture()["envelope"], "domain": STORE_DOMAIN}),
            id="domain-stated-on-the-envelope",
        ),
    ],
)
def test_every_bid_the_agent_produces_is_one_a_buyer_could_complete(ctx: dict) -> None:
    """Whatever path the offer left by, it names where to buy it and how long it stands."""
    assert_completable(answered(ctx).offer, domain=STORE_DOMAIN)


def test_the_checkout_url_is_the_variant_scoped_cart_permalink() -> None:
    """The D22 shape, verbatim — and the variant, not the product, when the catalog names one."""
    answer = answered(context(variant=True))
    assert answer.offer.variant_ref == VARIANT_REF
    assert answer.offer.checkout_url == f"https://{STORE_DOMAIN}/cart/{VARIANT_REF}:1"


def test_a_catalog_with_no_variant_still_gets_a_url_scoped_to_its_product() -> None:
    """No variant id is not a reason to bid without a checkout URL — that costs the shortlist."""
    answer = answered(context())
    assert answer.offer.variant_ref is None
    assert answer.offer.checkout_url == f"https://{STORE_DOMAIN}/cart/prod-cap:1"


def test_no_discount_code_is_smuggled_into_the_url() -> None:
    """The single-use code is the exchange's to mint at accept time, not the agent's to claim."""
    url = answered(context()).offer.checkout_url or ""
    assert "?" not in url and "discount" not in url, (
        f"the agent appended a query to {url!r}; an agent that writes `?discount=` is asserting "
        f"an authorization nobody granted it"
    )


# =============================================================================================
# 2. What the agent does when it has NOT been told its domain — it must not invent one.
# =============================================================================================


def test_a_context_that_states_no_domain_mints_no_url_rather_than_guessing() -> None:
    """`None` is the R10 fallback shape; a synthesised host would be a fact nothing supports.

    ``apps/exchange/src/checkout/domain.py`` distinguishes an ABSENT checkout_url from an
    off-domain one and refuses only the second, so this is a legal offer — where a guessed
    ``f"{store_id}.example.com"`` would pass the platform's exact-equality check only when two
    independent guesses happened to agree, and would be a spoof the day they did not.
    """
    answer = answered(context(domain=None))
    assert answer.offer.checkout_url is None
    assert STORE_ID not in str(answer.offer.checkout_url or ""), "the agent invented a host"
    # The expiry is NOT contingent on the domain: it comes off the context or the request.
    assert answer.offer.expires_at == "2999-01-01T00:00:00Z"


@pytest.mark.parametrize(
    "stated",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("store-alpha.example.com/cart", id="carries-a-path"),
        pytest.param("store-alpha.example.com:8443", id="carries-a-port"),
        pytest.param("store-alpha.example.com@evil.tld", id="userinfo-spoof"),
        pytest.param("https://store-alpha.example.com/?next=x", id="carries-a-query"),
        pytest.param("[::1]", id="bracketed-ipv6-literal"),
        pytest.param("store alpha.example.com", id="carries-a-space"),
        pytest.param("store-alpha..example.com", id="empty-label"),
        pytest.param("store_alpha.example.com", id="underscore"),
        pytest.param(None, id="unset"),
    ],
)
def test_a_stated_domain_that_is_not_a_bare_host_is_refused_rather_than_trimmed(
    stated: Any,
) -> None:
    """Trimming would publish a URL the merchant did not write while still looking configured.

    The userinfo case is the sharp one: ``urlsplit`` reads ``store-alpha.example.com@evil.tld``
    as userinfo ``store-alpha.example.com`` on host ``evil.tld``, so a reader that took the text
    before the ``@`` would build a checkout URL pointing at the attacker while believing it had
    the seller's own domain.
    """
    assert store_domain_host(stated) is None
    assert answered(context(domain=stated)).offer.checkout_url is None


@pytest.mark.parametrize(
    "stated",
    ["store-alpha.example.com", "127.0.0.1", "xn--strae-oqa.example", "sub.store.example.co.uk"],
)
def test_every_domain_the_agent_accepts_round_trips_through_the_platforms_own_check(
    stated: str,
) -> None:
    """Whatever `store_domain_host` accepts must survive being made into a URL and parsed back.

    This is the property, and it is the one an earlier draft broke twice: `[::1]` came back as
    `::1` and built a URL with no host at all, and `a b.com` came back whole and built a URL no
    browser can dial while the platform's comparison still said on-domain.
    """
    host = store_domain_host(stated)
    assert host is not None
    url = answered(context(domain=stated)).offer.checkout_url
    assert url is not None
    assert is_on_domain(url, host), f"{url!r} is not on {host!r} by the platform's own check"


def test_the_domain_the_context_states_beats_the_one_the_envelope_states() -> None:
    """The context is the live join the merchant service assembles; the envelope is the artifact."""
    ctx = context(domain=STORE_DOMAIN, envelope={**_fixture()["envelope"], "domain": "other.tld"})
    assert (answered(ctx).offer.checkout_url or "").startswith(f"https://{STORE_DOMAIN}/")


# =============================================================================================
# 3. The expiry, which no offer may leave without.
# =============================================================================================


def test_an_offer_with_no_readable_expiry_is_a_decline_not_a_bid_without_one() -> None:
    """There is no path out of this runtime that produces an offer with no `expires_at`."""
    answer = bid(request_body(respond_by="not-an-instant"), context())
    assert is_decline(answer)
    assert isinstance(answer, Decline)
    assert answer.reason is DeclineReason.unstatable_offer_expiry


# =============================================================================================
# 4. The served door.
# =============================================================================================


def client_for(ctx: dict[str, Any] | None) -> TestClient:
    app = create_app()
    configure_solicitation(app, context=ctx)
    return TestClient(app)


def test_the_served_door_answers_a_solicitation_with_a_completable_bid() -> None:
    """The whole point of the transport: a real POST, a real 200, a real usable offer."""
    response = client_for(context(variant=True)).post("/v1/bid-requests", json=request_body())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auction_id"] == "auc-0001"
    assert body["store_id"] == STORE_ID
    assert_completable(body["offer"], domain=STORE_DOMAIN)
    # The body is the pinned protocol object, not a look-alike this route shaped by hand.
    assert Bid.model_validate(body).offer.checkout_url == body["offer"]["checkout_url"]


def test_the_served_door_is_the_same_answer_as_the_library_call() -> None:
    """A transport that changed the answer would be a second bidding path nobody graded."""
    ctx = context(variant=True)
    served = client_for(ctx).post("/v1/bid-requests", json=request_body()).json()
    in_process = bid(BidRequest.model_validate(request_body()), ctx)
    assert served == in_process.model_dump(mode="json")


def test_a_decline_is_a_204_that_names_its_reason() -> None:
    """204 is the contract's word for a decline, so the reason travels in a header."""
    response = client_for(context()).post(
        "/v1/bid-requests", json=request_body(cluster_id="cluster-espresso")
    )
    assert response.status_code == 204
    assert response.content == b""
    assert response.headers[DECLINE_REASON_HEADER] == DeclineReason.cluster_not_pursued.value


def test_an_unconfigured_agent_declines_rather_than_inventing_a_store() -> None:
    """Fail closed: an agent with no envelope has no authorization to offer anything."""
    response = client_for(None).post("/v1/bid-requests", json=request_body())
    assert response.status_code == 204
    assert response.headers[DECLINE_REASON_HEADER] == UNCONFIGURED_REASON


def test_a_body_that_is_not_a_bid_request_is_refused_by_the_pinned_contract() -> None:
    """422, from `contracts.BidRequest` itself, rather than a decline that hides a caller bug."""
    response = client_for(context()).post("/v1/bid-requests", json={"auction_id": "auc-0002"})
    assert response.status_code == 422


# =============================================================================================
# 5. How a container gets its store, since nothing in `uvicorn store_agent.main:app` can call
#    `configure_solicitation`.
# =============================================================================================


def test_the_context_file_named_by_the_environment_is_what_the_route_bids_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "store-context.json"
    path.write_text(json.dumps(context(variant=True)), encoding="utf-8")
    monkeypatch.setenv(CONTEXT_ENV, str(path))

    # No `configure_solicitation` — exactly what the shipped image does.
    response = TestClient(create_app()).post("/v1/bid-requests", json=request_body())

    assert response.status_code == 200, response.text
    assert_completable(response.json()["offer"], domain=STORE_DOMAIN)


def test_an_approved_envelope_fixture_loads_as_a_context_with_a_domain_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped fixture states its store only inside the envelope, and states no domain.

    Both are filled in from the environment rather than by editing an approved artifact — and
    the result must still be a bid a buyer could complete, which is the whole demo path.
    """
    fixture = _fixture()
    path = tmp_path / "approved.json"
    path.write_text(
        json.dumps(
            {
                "envelope": fixture["envelope"],
                "catalog": fixture["catalog"],
                "live_state": {"prod-cap": {"in_stock": True, "units_left": 7}},
                "network_priors": {CLUSTER: {"depth_buckets": [0.0, 0.05, 0.1]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(CONTEXT_ENV, str(path))
    monkeypatch.setenv(DOMAIN_ENV, STORE_DOMAIN)

    loaded = load_context_from_env()
    assert loaded is not None
    assert loaded["store_id"] == STORE_ID, "the store id was not recovered from the envelope"
    assert_completable(answered(loaded).offer, domain=STORE_DOMAIN)


def test_the_environment_domain_never_overrides_one_the_merchant_stated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "store-context.json"
    path.write_text(json.dumps(context(domain=STORE_DOMAIN)), encoding="utf-8")
    monkeypatch.setenv(CONTEXT_ENV, str(path))
    monkeypatch.setenv(DOMAIN_ENV, "someone-else.example.com")

    loaded = load_context_from_env()
    assert loaded is not None
    assert loaded["store_domain"] == STORE_DOMAIN


@pytest.mark.parametrize(
    ("written", "match"),
    [
        pytest.param(None, "cannot be read", id="missing-file"),
        pytest.param("{not json", "not valid JSON", id="unparseable"),
        pytest.param("[1, 2, 3]", "must hold a JSON object", id="not-an-object"),
    ],
)
def test_a_context_path_that_cannot_be_read_fails_loudly_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, written: str | None, match: str
) -> None:
    """An unreadable path must not look identical to a store that chose not to bid."""
    path = tmp_path / "store-context.json"
    if written is not None:
        path.write_text(written, encoding="utf-8")
    monkeypatch.setenv(CONTEXT_ENV, str(path))

    with pytest.raises(StoreContextError, match=match):
        load_context_from_env()


def test_an_unset_context_variable_is_a_decision_and_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CONTEXT_ENV, raising=False)
    assert load_context_from_env() is None


# =============================================================================================
# 6. The product property the whole lane exists for: a bid this agent serves is one the
#    exchange can actually shortlist.
#
#    Every assertion above is about a FIELD. This one is about the CONSEQUENCE, and it is the
#    one that was measured broken: an exchange wired by hand with real eligibility, a real trust
#    snapshot and a real domain registry answered `ranked: []`, every store excluded
#    `off_domain_checkout … failing closed (C10)`. A field-level test would have gone green the
#    moment the agent wrote *any* string into `checkout_url`; this one goes green only when the
#    string is one the platform's ranker accepts.
#
#    It drives the exchange's own `attest_candidates` + `rank` rather than a local imitation. If
#    those move, this test fails loudly, which is the correct outcome: the property the agent
#    owes is defined by that code and by nothing this package could restate.
# =============================================================================================

PRODUCT_REF = "prod-cap"
AS_OF = "2026-01-01T00:00:00Z"
#: `config["now"]` for `rank()`: 2026-01-01T00:00:00Z as epoch seconds. The offer expires in
#: 2999, so nothing here is near an expiry boundary.
RANK_NOW = 1767225600.0


def catalog_snapshot(ctx: dict[str, Any]) -> dict[str, Any]:
    """The catalogue the exchange grades this store's claims against — the PLATFORM's copy.

    Built the same way ``e2e/support/s1/flow.py`` builds it, from the same rows the agent read.
    That is the whole security argument restated: the store supplies the claim, the exchange
    supplies the evidence, and a divergence is a contradiction rather than a matter of opinion.
    """
    listing = ctx["catalog"][PRODUCT_REF]
    attributes = {
        key: {"value": value}
        for key, value in listing.items()
        if key not in ("product_ref", "variant_ref")
    }
    attributes.update({k: {"value": v} for k, v in ctx["live_state"][PRODUCT_REF].items()})
    return {
        "snapshot_id": f"snap-{STORE_ID}",
        "captured_at": AS_OF,
        "store_id": STORE_ID,
        "products": [
            {
                "product_ref": PRODUCT_REF,
                "canonical_name": PRODUCT_REF,
                "evidence_ref": f"snap-{STORE_ID}#{PRODUCT_REF}",
                "observed_at": AS_OF,
                "attributes": attributes,
                "offer": {
                    "unit_price": listing["list_price"],
                    "currency": "USD",
                    "availability": "in_stock",
                },
            }
        ],
    }


def ranked_by_the_exchange(offer: dict[str, Any], bid_body: dict[str, Any], ctx: dict) -> dict:
    """One auction, one candidate, through the exchange's real attestation and ranker."""
    from exchange.ranking import rank  # noqa: PLC0415 - the exchange is a sibling package
    from exchange.ranking.verification import (  # noqa: PLC0415
        StaticCatalogSnapshots,
        attest_candidates,
    )

    candidate = {
        "bid_id": f"{bid_body['auction_id']}-{STORE_ID}",
        "store_id": STORE_ID,
        # The PLATFORM's registered domain, not the bid's word for it. The agent was configured
        # with the same value, which is what a deployment populated from one seller record looks
        # like — and what makes the host comparison mean something.
        "store_domain": STORE_DOMAIN,
        "tier": 1,
        "network_fee": 0.0,
        "fee_rate": 0.0,
        "envelope_max_discount_pct": 0.0,
        "envelope_budget_cap": 0.0,
        "offer": offer,
        "claims": bid_body["claims"],
    }
    attested = attest_candidates(
        [candidate],
        catalog=StaticCatalogSnapshots({STORE_ID: catalog_snapshot(ctx)}),
        product_refs={STORE_ID: PRODUCT_REF},
    )
    return rank(
        attested,
        request_body()["intent"],
        {STORE_ID: {"store_id": STORE_ID, "score": 0.8, "blacklisted": False}},
        {"now": RANK_NOW, "auction_id": bid_body["auction_id"]},
    )


def test_a_bid_this_agent_serves_is_one_the_exchange_shortlists() -> None:
    """The consequence, end to end: HTTP 200 -> attested -> ranked -> a slot in the shortlist.

    And the control in the same test, because "1 slot" only means something next to the 0 it
    replaced: the same bid with **only** ``checkout_url`` removed is excluded by name.
    """
    ctx = context(variant=True)
    response = client_for(ctx).post("/v1/bid-requests", json=request_body())
    assert response.status_code == 200, response.text
    served = response.json()

    result = ranked_by_the_exchange(served["offer"], served, ctx)
    excluded_for = result["candidates"][0]["exclusion_reasons"]
    assert len(result["ranked"]) == 1, f"the agent's own bid was not rankable: {excluded_for}"
    assert len(result["shortlist"]["slots"]) == 1
    assert result["shortlist"]["slots"][0]["bid_ref"] == f"auc-0001-{STORE_ID}"

    without = ranked_by_the_exchange(
        {**copy.deepcopy(served["offer"]), "checkout_url": None}, served, ctx
    )
    assert without["ranked"] == []
    assert without["shortlist"]["slots"] == []
    reasons = without["candidates"][0]["exclusion_reasons"]
    assert any(reason.startswith("off_domain_checkout") for reason in reasons), reasons
