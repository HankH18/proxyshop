"""``GET /snapshot`` — the served TrustSnapshot (T-261 / T-064 acceptance 3).

Two layers, and the second is the one that matters. The injected-source tests grade the
SHAPE — that what is served validates against the ``TrustSnapshot`` the published document
advertises, which is what a client can rely on. The docker-marked tests grade the SOURCE —
that the route really reads ``app.sellers``, ``ledger.trust_observations`` and
``app.seller_blacklist`` as ``trust_rw``, which is the half an injected fixture cannot prove
and the half that breaks in a deployment.

Both layers grade the SERVER half, and only that. T-064's acceptance 3 is "the exchange
client caches the trust snapshot and refreshes it on a version bump"; nothing in this file
exercises a client, a cache or a refresh, because none exists —
``git grep SNAPSHOT_VERSION -- . ':(exclude)apps/trust'`` returns nothing. Those three live
in ``apps/exchange`` and are that lane's to build.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]
AS_OF = "2026-02-01T00:00:00Z"


def _protocol_schema() -> dict[str, Any]:
    return json.loads(
        (REPO_ROOT / "packages" / "contracts" / "schemas" / "protocol.schema.json").read_text(
            encoding="utf-8"
        )
    )


def _validate_trust_snapshot(entry: Any) -> None:
    """Validate one served entry against the published ``TrustSnapshot`` definition.

    Validated against the WHOLE document with a ``$ref`` into it, rather than against the
    lifted subschema, so the ``$ref``\\ s to ``TrustDims`` and ``TrustDimensionState`` — and
    their ``additionalProperties: false`` — are actually resolved. Lifting the definition out
    and validating that alone silently passes any ``dims`` at all, which is the half of this
    contract most likely to drift.
    """
    import jsonschema

    document = _protocol_schema()
    validator = jsonschema.Draft202012Validator(
        {"$ref": "#/$defs/TrustSnapshot", **{"$defs": document["$defs"]}}
    )
    errors = sorted(validator.iter_errors(entry), key=lambda error: list(error.path))
    assert errors == [], (
        f"the served entry does not validate against the published TrustSnapshot: "
        f"{[f'{list(error.path)}: {error.message}' for error in errors]}"
    )


def _store(store_id: str, identity: str, observations: list[dict[str, Any]]) -> dict[str, Any]:
    return {"store_id": store_id, "business_identity": identity, "observations": observations}


def _client(stores: Any, blacklist: Any) -> TestClient:
    from trust.main import create_app

    app = create_app()
    app.state.snapshot_stores = stores
    app.state.snapshot_blacklist = blacklist
    return TestClient(app)


def test_the_published_snapshot_route_is_mounted() -> None:
    """T-261's own subject: ``/snapshot`` is published AND served.

    ``main.py`` is frozen and discovers routers by globbing ``<feature>/routes.py``, so the
    only way this passes is that ``apps/trust/src/snapshot/routes.py`` exists and exports a
    module-level ``router``.
    """
    from trust.main import create_app

    app = create_app()
    assert "/snapshot" in app.openapi()["paths"], sorted(app.openapi()["paths"])
    assert "trust.snapshot.routes" in app.state.mounted_routers, app.state.mounted_routers


def test_every_served_entry_validates_against_the_published_trust_snapshot() -> None:
    """The body is what the document says it is, ``additionalProperties: false`` included.

    ``store_entry`` carries ``business_identity``, ``episodes``, ``observations``,
    ``decided_observations`` and ``as_of``, and each dimension carries ``coverage``,
    ``observations``, ``decided`` and ``evidence``. ``TrustSnapshot`` and
    ``TrustDimensionState`` both forbid extra properties, so serving an entry verbatim is a
    schema violation on nine counts. This is the test that would catch that.
    """
    from trust.scoring import Blacklist

    stores = [
        _store(
            "store-1",
            "co-1",
            [{"dim": "price_honored", "type": "verified", "observed_at": "2026-01-01T00:00:00Z"}],
        ),
        _store("store-quiet", "co-2", []),
    ]
    response = _client(stores, Blacklist()).get("/snapshot", params={"as_of": AS_OF})
    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(body) == ["store-1", "store-quiet"], body

    for store_id, entry in body.items():
        _validate_trust_snapshot(entry)
        assert entry["store_id"] == store_id
        assert "business_identity" not in entry, (
            "the identity the blacklist is keyed by is served to every consumer of this "
            "endpoint, which hands out the join the identity binding exists to withhold"
        )


def test_a_store_with_no_observations_is_served_and_flagged_low_data() -> None:
    """The entry the exchange most needs is the one a join on observations would drop.

    ``low_data`` exists because an unknown store and a genuinely mixed one both score ~0.5.
    A store with no observations at all must therefore appear in the body carrying the flag,
    not be absent from it — absence is read by the exchange as an unavailable eligibility
    read and denied outright (R12), which is a different decision from "we have not seen
    this store yet".
    """
    from trust.scoring import Blacklist

    response = _client([_store("store-quiet", "co-2", [])], Blacklist()).get(
        "/snapshot", params={"as_of": AS_OF}
    )
    assert response.status_code == 200, response.text
    entry = response.json()["store-quiet"]
    assert entry["low_data"] is True, entry
    assert set(entry["dims"]) == {
        "price_honored",
        "discount_honored",
        "shipped_on_time",
        "not_returned",
        "feedback_match",
        "catalog_claim_accuracy",
    }, "a five-dimension snapshot is the D53 regression, served"


def test_a_listed_store_is_served_blacklisted() -> None:
    """``blacklisted`` is the registry's answer, resolved through the identity binding.

    Keyed on ``business_identity`` and not ``store_id``: the same operator under a fresh
    store id must still read blacklisted, which is the whole point of R12's identity binding
    and is what this asserts by listing the IDENTITY and reading the flag off the STORE.
    """
    from trust.scoring import Blacklist

    blacklist = Blacklist()
    blacklist.add(business_identity="co-bad", reason_code="manual_review", status="active")
    stores = [
        _store("store-reregistered", "co-bad", []),
        _store("store-clean", "co-ok", []),
    ]
    body = _client(stores, blacklist).get("/snapshot", params={"as_of": AS_OF}).json()
    assert body["store-reregistered"]["blacklisted"] is True, body
    assert body["store-clean"]["blacklisted"] is False, body


def test_the_response_carries_the_version_the_exchange_caches_on() -> None:
    """T-064 acceptance 3's cache KEY, served where the published body has no room for it.

    The published response is ``store_id -> TrustSnapshot`` and nothing else, so the envelope's
    ``version`` has nowhere to go in the body. It is served as ``ETag`` and spelled out in
    ``X-Trust-Snapshot-Version``.

    Despite this test's name, nothing on the exchange side caches on it: no such client exists,
    and this asserts only that the key is SERVED. A client that caches on the ETag and refetches
    when it changes is what would deliver acceptance 3's refresh, and building it is
    ``apps/exchange``'s work, not this route's.
    """
    from trust.scoring import Blacklist
    from trust.snapshot import SNAPSHOT_VERSION

    response = _client([_store("store-1", "co-1", [])], Blacklist()).get(
        "/snapshot", params={"as_of": AS_OF}
    )
    assert response.headers["X-Trust-Snapshot-Version"] == SNAPSHOT_VERSION
    assert response.headers["ETag"] == f'"{SNAPSHOT_VERSION}"'
    assert response.headers["X-Trust-As-Of"] == AS_OF, (
        "the instant the scores were decayed against is not reported, so a client cannot "
        "tell a fresh snapshot from one served against a different clock"
    )
    assert response.json()["store-1"]["snapshot_version"] == SNAPSHOT_VERSION


def test_as_of_is_the_callers_instant_and_not_a_clock() -> None:
    """A decision replayed at its own instant returns that decision, not today's.

    ``build_snapshot`` refuses to read a clock and says why; the route chooses the instant
    once and stamps it. Decay is monotone in elapsed time, so the same observation read a
    year later has strictly less evidence behind it — which is the observable consequence
    that proves ``as_of`` reached the scorer rather than being accepted and dropped.
    """
    from trust.scoring import Blacklist

    stores = [
        _store(
            "store-1",
            "co-1",
            [{"dim": "price_honored", "type": "verified", "observed_at": "2026-01-01T00:00:00Z"}],
        )
    ]
    client = _client(stores, Blacklist())
    near = client.get("/snapshot", params={"as_of": "2026-01-02T00:00:00Z"}).json()["store-1"]
    far = client.get("/snapshot", params={"as_of": "2027-01-01T00:00:00Z"}).json()["store-1"]
    assert near["dims"]["price_honored"]["alpha"] > far["dims"]["price_honored"]["alpha"], (
        f"the same observation decayed identically a year apart ({near['dims']}, "
        f"{far['dims']}); as_of is not reaching the scorer"
    )
    assert near["score"] != far["score"]


def test_an_unreadable_source_is_a_503_and_never_an_empty_snapshot() -> None:
    """Fail closed, and fail VISIBLY. An empty body is a valid answer meaning "no stores".

    The exchange denies a store with no row here (R12). So serving ``{}`` when the datastore
    is down tells it to deny every store, for a reason it cannot distinguish from the
    truthful one — the outage becomes indistinguishable from a real, empty roster.
    """

    def _down() -> Any:
        from trust.events.errors import StoreUnavailable

        raise StoreUnavailable("no database to read from")

    from trust.scoring import Blacklist

    response = _client(_down, Blacklist()).get("/snapshot")
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"] == "store_unavailable", response.text


@pytest.mark.docker
def test_the_route_reads_the_real_tables_as_trust_rw(
    ledger_clean: Any,
    pg_admin: Any,
    worker_database: str,
) -> None:
    """The half an injected source cannot prove: the default reads the real database.

    Seeds one seller with one observation and one blacklisted sibling, boots the app with
    NOTHING injected on ``app.state``, and reads the snapshot back. The connection this goes
    through is the ledger writer's own — ``trust_rw`` — so this also grades that the grant
    set D5 gives this service actually covers ``SELECT`` on ``app.sellers``,
    ``app.seller_blacklist`` and ``ledger.trust_observations``. It does not today by
    accident: ``0004_object_grants.sql`` gives ``trust_rw`` read-only on ``app``, and a route
    that needed a write there could not be served by this service at all.
    """
    from trust.main import create_app

    from proxyshop_support.postgres import role_dsn

    with pg_admin.cursor() as cursor:
        cursor.execute("delete from ledger.trust_observations")
        cursor.execute("delete from app.seller_blacklist")
        cursor.execute("delete from app.sellers")
        cursor.execute(
            "insert into app.sellers (store_id, domain, business_identity, tier) values "
            "('store-live', 'live.example', 'co-live', 'hosted'), "
            "('store-listed', 'listed.example', 'co-listed', 'hosted')"
        )
        cursor.execute(
            "insert into ledger.trust_observations "
            "(store_id, dim, observation_type, weight, observed_at) values "
            "('store-live', 'price_honored', 'verified', 1.0, '2026-01-01T00:00:00Z')"
        )
        cursor.execute(
            "insert into app.seller_blacklist "
            "(business_identity, reason_code, source, status, starts_at) values "
            "('co-listed', 'manual_review', 'human', 'active', '2026-01-01T00:00:00Z')"
        )

    app = create_app()
    # The DSN the route's connection is opened against, injected the way a deployment sets
    # it — the environment — rather than by handing the route a connection, so the resolution
    # path is the shipped one.
    app.state.ledger_connection = None
    import os

    previous = os.environ.get("PROXYSHOP_LEDGER_DSN")
    os.environ["PROXYSHOP_LEDGER_DSN"] = role_dsn("trust_rw", database=worker_database)
    try:
        response = TestClient(app).get("/snapshot", params={"as_of": AS_OF})
    finally:
        if previous is None:
            os.environ.pop("PROXYSHOP_LEDGER_DSN", None)
        else:
            os.environ["PROXYSHOP_LEDGER_DSN"] = previous

    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(body) == ["store-listed", "store-live"], body
    _validate_trust_snapshot(body["store-live"])
    assert body["store-listed"]["blacklisted"] is True, (
        f"the listed seller came back unlisted, so app.seller_blacklist was not read: {body}"
    )
    assert body["store-live"]["blacklisted"] is False, body
    assert body["store-live"]["dims"]["price_honored"]["alpha"] > 2.0, (
        f"the seeded observation did not reach the score (prior alpha is 2.0): {body}"
    )
    assert body["store-listed"]["dims"]["price_honored"]["alpha"] == 2.0, (
        "a store with no observations did not come back on the untouched prior, so rows are "
        "reaching the wrong store"
    )
