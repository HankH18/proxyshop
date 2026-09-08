"""``POST /claims/verifications`` driven through the app a deployment actually boots.

WHY THIS FILE EXISTS
--------------------
``proxyshop_support/route_census.py`` enumerates every route this repo serves and asks which
of them any test issues a request at. Before this module the answer for this one route was
*none*: ``/claims/verifications`` was named in four places across three test files and every
mention was a comment or a docstring, which is exactly why the census blanks comments and
bare strings before it looks. The handler was covered only through
:func:`trust.verification.persist_claim_verification`, whose sole caller in the tree is the
handler itself — so nothing anywhere proved that the route was mounted, that its request
model accepted a real payload, that its refusals came back as refusals, or that the
announcement its own summary promises ("and announce it on the ledger") ever reached
``ledger.commerce_events``.

WHAT IS DRIVEN, AND WHAT EACH TEST IS ACTUALLY FOR
--------------------------------------------------
Every docker-marked test below goes through a ``TestClient`` over ``trust.main:create_app()``
against this worker's real Postgres as ``trust_rw`` (the ``loop_client`` fixture), because the
defect class this file closes is "built, unit-tested, reachable by nobody" and a test that
calls the seam directly reproduces it rather than closing it.

The failure modes are not invented. Each is named in the handler's own comments as something
that was *measured* against a live client and is now defended:

* ``trust.scoring``'s vocabulary refusals derive from :class:`LookupError`, **not** from
  :class:`ValueError`, so with ``except ValueError`` alone an unapproved ``claim_type``
  escaped the handler as a 500 — the service reporting a caller's bad payload as its own
  outage. :func:`test_an_unapproved_claim_type_is_the_callers_mistake_not_a_500`.
* ``psycopg.Error`` derives directly from :class:`Exception`, so a CHECK violation, an unknown
  ``catalog_snapshot_id`` (a real FK onto ``ledger.catalog_snapshots``), a malformed uuid and
  a bad provenance value all escaped as 500s — four of five bad payloads. All four are driven
  here, and each also asserts the transaction was **rolled back**, because a refusal that
  leaves half a claim behind is a worse answer than a 500.
* The event id carries every component of the database's own idempotency key, so a legitimate
  re-verification against a new snapshot does not collide with the event it already appended.
  :func:`test_a_snapshot_bump_re_verifies_and_announces_a_second_time` is that regression.

THE ANNOUNCEMENT IS ASSERTED BY PROVENANCE, NOT BY PRESENCE
-----------------------------------------------------------
"a ``claim_verified`` row exists" is satisfied by any test that inserts one. These tests never
write to ``ledger.commerce_events`` — ``ledger_clean`` empties it at setup and the only writer
in the process is the served handler — and
:func:`test_the_announcement_could_only_have_come_from_the_production_emitter` closes the gap
the rest of the way with three properties a hand-written row cannot have:

1. its ``payload`` carries ``dim``, which appears nowhere in the request body and is
   :func:`trust.scoring.claim_dimension` of the claim type — server-derived by construction;
2. its ``idempotency_key`` is the handler's own five-part composition, rebuilt here from
   :data:`trust.claims.routes.CLAIM_VERIFIED_KIND` rather than typed out; and
3. it is a valid link in the hash chain — ``event_hash`` re-derived from the stored row by
   :func:`trust.ledger.compute_event_hash`, and the whole stored chain re-verified against its
   anchor by :func:`trust.ledger.verify_chain_in_db`. A row inserted beside the writer does
   not hash, and cannot be made to without reimplementing the sealer.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest

#: The served path under test. Written once, as code rather than as prose, because the route
#: census reads test files with comments and docstrings blanked out — a route "covered" only
#: by a paragraph about it is the exact state this module was written to end.
VERIFICATIONS = "/claims/verifications"

#: The store, claim and verifier every payload below is about unless it says otherwise.
STORE = "store-northroast"
CLAIM_REF = "claim-price-espresso-1kg"
VERIFIER = "catalog-verifier-1.4.0"

#: When the verification happened. Explicit and millisecond-precise: the scorer decays against
#: it, and ``commerce_events_occurred_at_is_millisecond`` refuses anything finer.
OBSERVED_AT = "2026-02-01T00:00:00.000Z"

#: A claim type that is in neither the approved ``claim_type -> dimension`` table nor
#: ``claims_claim_type_check``. It reaches :func:`trust.scoring.claim_dimension`, which raises
#: ``UnmappedClaimType`` — a ``LookupError``, and the whole reason the handler catches one.
UNAPPROVED_CLAIM_TYPE = "vibes"


def _body(snapshot_id: str, **overrides: Any) -> dict[str, Any]:
    """One complete, valid verification outcome, with ``overrides`` applied on top."""
    body: dict[str, Any] = {
        "store_id": STORE,
        "claim_ref": CLAIM_REF,
        "claim_type": "price",
        "key": "price.value",
        "status": "verified",
        "confidence": 0.9,
        "catalog_snapshot_id": snapshot_id,
        "verifier_version": VERIFIER,
        "observed_at": OBSERVED_AT,
        "evidence_refs": ["evidence:catalog-page-1"],
        "value": {"amount": 19.99, "currency": "EUR"},
        "observed_value": {"amount": 19.99, "currency": "EUR"},
        "provenance_source": "scraped",
        "provenance_ref": "https://northroast.example/p/espresso-1kg",
        "authority_rank": 3,
        "weight": 0.8,
        "source_class": "scraped",
    }
    body.update(overrides)
    return body


@pytest.fixture
def claims_snapshot(ledger_clean: Any, pg_admin: Any) -> Callable[..., str]:
    """Insert one ``ledger.catalog_snapshots`` row and hand back its ``snapshot_id``.

    ``claim_verifications.catalog_snapshot_id`` is a real foreign key onto this table, so a
    verification cannot be recorded against a snapshot nobody crawled. Depends on
    ``ledger_clean`` so the insert happens *after* the per-test truncation rather than being
    swept away by it.
    """

    def make(store_id: str = STORE) -> str:
        snapshot_id = str(uuid.uuid4())
        with pg_admin.cursor() as cursor:
            cursor.execute(
                "insert into ledger.catalog_snapshots "
                "(snapshot_id, store_id, snapshot_ref, content_hash, extractor_version, "
                " observed_at) values (%s, %s, %s, %s, %s, %s)",
                (
                    snapshot_id,
                    store_id,
                    f"snapshot:{snapshot_id}",
                    f"hash-{snapshot_id}",
                    "extractor-1.0.0",
                    OBSERVED_AT,
                ),
            )
        return snapshot_id

    return make


@pytest.fixture
def claims_counts(pg_admin: Any) -> Callable[[], dict[str, int]]:
    """How many rows each table this route writes is holding, read with the admin connection.

    Read out of the tables rather than off the response, because what a route *says* it wrote
    is the thing under test.
    """
    tables = (
        "ledger.claims",
        "ledger.claim_verifications",
        "ledger.verification_evidence_refs",
        "ledger.trust_observations",
        "ledger.trust_scores",
        "ledger.commerce_events",
    )

    def counts() -> dict[str, int]:
        out: dict[str, int] = {}
        with pg_admin.cursor() as cursor:
            for table in tables:
                cursor.execute(f"select count(*) from {table}")  # noqa: S608 - fixed literals
                row = cursor.fetchone()
                out[table] = int(row[0]) if row else 0
        return out

    return counts


@pytest.fixture
def claims_events(pg_admin: Any) -> Callable[[], list[dict[str, Any]]]:
    """Every ``ledger.commerce_events`` row, in chain order, as a dict per row."""

    def rows() -> list[dict[str, Any]]:
        with pg_admin.cursor() as cursor:
            cursor.execute(
                "select seq, idempotency_key, kind, store_id, occurred_at, payload, "
                "prev_hash, event_hash from ledger.commerce_events order by seq"
            )
            names = (
                "seq",
                "idempotency_key",
                "kind",
                "store_id",
                "occurred_at",
                "payload",
                "prev_hash",
                "event_hash",
            )
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    return rows


def _expected_event_id(body: dict[str, Any]) -> str:
    """The event id the handler composes, rebuilt from the request rather than typed out."""
    from trust.claims.routes import CLAIM_VERIFIED_KIND

    return (
        f"{CLAIM_VERIFIED_KIND}:{body['store_id']}:{body['claim_ref']}"
        f":{body['catalog_snapshot_id']}:{body['verifier_version']}"
    )


# ======================================================================================
# Is it served at all? No database needed to answer that, and it is the first question.
# ======================================================================================
def test_the_factory_the_deployment_boots_actually_mounts_the_verifications_route() -> None:
    """``create_app()`` — the same factory ``uvicorn`` reaches — serves POST on this path.

    The route lives in ``apps/trust/src/claims/routes.py`` on a router carrying
    ``prefix="/claims"``, and ``main.create_app()`` mounts it by globbing ``src/*/routes.py``.
    Nothing asserted that the glob, the prefix and the decorator compose to the path the
    published contract advertises; a rename of the ``claims`` directory would have moved the
    served path silently.
    """
    from trust.main import create_app

    app = create_app()
    paths = app.openapi()["paths"]
    assert VERIFICATIONS in paths, (
        f"{VERIFICATIONS} is not on the served app. Mounted routers: "
        f"{getattr(app.state, 'mounted_routers', None)}"
    )
    operation = paths[VERIFICATIONS]
    assert "post" in operation, f"{VERIFICATIONS} is served, but not for POST: {sorted(operation)}"
    assert "201" in operation["post"]["responses"], (
        "the route declares no 201, so its documented 'new outcome' answer is unpublished: "
        f"{sorted(operation['post']['responses'])}"
    )


def test_the_published_contract_carries_the_route_the_app_serves() -> None:
    """``trust.openapi.json`` publishes this operation, so a client can find it.

    The handler's module docstring records the opposite as KNOWN CONTRACT DRIFT — "served and
    published nowhere". It is published now; this holds that closed, from the test directory
    that owns nothing in ``packages/contracts`` and therefore cannot fix it by editing it.
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    contract = json.loads(
        (root / "packages/contracts/openapi/trust.openapi.json").read_text(encoding="utf-8")
    )
    assert VERIFICATIONS in contract["paths"], (
        f"the trust contract does not publish {VERIFICATIONS}, which the app serves: "
        f"{sorted(contract['paths'])}"
    )
    assert "post" in contract["paths"][VERIFICATIONS]


# ======================================================================================
# The refusals a caller can trigger without a database being involved at all.
# ======================================================================================
def test_a_provenance_source_outside_the_vocabulary_is_refused_before_any_write() -> None:
    """A value only ``claims_provenance_source_check`` would refuse comes back 422, named.

    One of the four payloads the handler's comment records as having become a 500. It is a
    422 twice over now — the request model refuses it here, and the broad ``except`` behind it
    would catch the CHECK violation if the model ever stopped. No database: the refusal has to
    happen before the handler runs, and this test proves it does by never providing one.
    """
    from fastapi.testclient import TestClient
    from trust.main import create_app

    client = TestClient(create_app())
    response = client.post(VERIFICATIONS, json=_body(str(uuid.uuid4()), provenance_source="vibes"))

    assert response.status_code == 422, (
        f"an unapproved provenance_source answered {response.status_code}: {response.text}"
    )
    assert "provenance_source" in response.text, (
        "the refusal does not name the field the caller got wrong, which is the whole "
        f"difference between a 422 and a 500: {response.text}"
    )


def test_an_unknown_top_level_field_is_refused_rather_than_dropped() -> None:
    """``extra="forbid"`` means a misspelled field is a refusal, not a silently ignored one.

    ``ClaimVerificationIn`` forwards its dump into the persistence seam as ``**payload``, so a
    tolerated extra field would either vanish or become a ``TypeError`` deep inside the writer.
    """
    from fastapi.testclient import TestClient
    from trust.main import create_app

    client = TestClient(create_app())
    response = client.post(VERIFICATIONS, json=_body(str(uuid.uuid4()), snapshot_version=7))

    assert response.status_code == 422, (
        f"an unknown field answered {response.status_code}: {response.text}"
    )


def test_the_handler_must_pop_store_id_because_the_seam_takes_it_by_keyword() -> None:
    """Why ``payload.pop("store_id", None)`` is load-bearing, demonstrated rather than asserted.

    The handler dumps the request model and forwards it as ``**payload`` while *also* passing
    ``store_id=body.store_id`` explicitly. ``store_id`` is a field of the model, so leaving it
    in the dump is not a harmless duplicate — it is ``TypeError: got multiple values for
    keyword argument 'store_id'`` raised by the call itself, before the writer runs, on the
    happy path. That would land in the handler's broad ``except Exception``, which re-raises
    anything that is not a ``psycopg.Error`` — so every well-formed request would have become
    a 500. This pins the reason the line exists so it cannot be tidied away as redundant.
    """
    from trust.claims.routes import ClaimVerificationIn
    from trust.verification import persist_claim_verification

    dumped = ClaimVerificationIn(**_body(str(uuid.uuid4()))).model_dump(exclude_none=True)
    assert "store_id" in dumped, (
        "store_id has left the request model, so the pop in the handler is now dead code "
        "rather than the thing standing between a valid request and a TypeError"
    )

    with pytest.raises(TypeError, match="store_id"):
        persist_claim_verification(connection=object(), store_id=STORE, **dumped)


# ======================================================================================
# The served happy path, and the replay.
# ======================================================================================
@pytest.mark.docker
def test_a_new_verification_is_recorded_across_all_five_tables_and_announced(
    loop_client: Any,
    claims_snapshot: Callable[..., str],
    claims_counts: Callable[[], dict[str, int]],
    claims_events: Callable[[], list[dict[str, Any]]],
) -> None:
    """201, five tables written, one event appended — the route's whole contract, once.

    Asserted against the tables rather than against the response body, because the response is
    what the route claims and the rows are what it did.
    """
    snapshot_id = claims_snapshot()
    body = _body(snapshot_id)

    response = loop_client.post(VERIFICATIONS, json=body)

    assert response.status_code == 201, (
        f"a well-formed verification answered {response.status_code}: {response.text}"
    )
    answered = response.json()
    assert answered["replayed"] is False
    verification = answered["verification"]
    assert verification["written"] is True
    assert verification["claim_id"] and verification["verification_id"]
    assert verification["dim"] == "price_honored", (
        f"claim_type 'price' routed to {verification['dim']!r}, not the approved dimension"
    )
    assert verification["observation_type"] == "verified"

    counts = claims_counts()
    assert counts == {
        "ledger.claims": 1,
        "ledger.claim_verifications": 1,
        "ledger.verification_evidence_refs": 1,
        "ledger.trust_observations": 1,
        "ledger.trust_scores": 1,
        "ledger.commerce_events": 1,
    }, f"one served verification wrote {counts}"

    events = claims_events()
    assert [event["idempotency_key"] for event in events] == [_expected_event_id(body)]
    assert events[0]["kind"] == "claim_verified"
    assert answered["event"] == {
        "event_id": _expected_event_id(body),
        "kind": "claim_verified",
        "inserted": True,
    }


@pytest.mark.docker
def test_the_verification_row_carries_what_the_request_asked_for(
    ledger_clean: Any,
    loop_client: Any,
    claims_snapshot: Callable[..., str],
    pg_admin: Any,
) -> None:
    """The claim and the verification land with the caller's values, not with defaults.

    Without this, "201 and a row exists" would pass for a writer that recorded a different
    claim: the ``authority_rank``/``provenance_source`` pair in particular decides which of two
    competing claims about one key wins, and it is only ever read back on a later request.
    """
    snapshot_id = claims_snapshot()
    response = loop_client.post(VERIFICATIONS, json=_body(snapshot_id))
    assert response.status_code == 201, response.text

    with pg_admin.cursor() as cursor:
        cursor.execute(
            "select store_id, claim_ref, claim_type, key, value, provenance_source, "
            "provenance_ref, authority_rank from ledger.claims"
        )
        claim = cursor.fetchone()
        cursor.execute(
            "select status, confidence, dim, verifier_version, catalog_snapshot_id, "
            "observed_value from ledger.claim_verifications"
        )
        recorded = cursor.fetchone()
        cursor.execute("select evidence_ref, source_class from ledger.verification_evidence_refs")
        evidence = cursor.fetchall()
        cursor.execute(
            "select store_id, dim, observation_type, weight from ledger.trust_observations"
        )
        observation = cursor.fetchone()
        cursor.execute("select score, confidence from ledger.trust_scores")
        folded = cursor.fetchone()

    assert claim == (
        STORE,
        CLAIM_REF,
        "price",
        "price.value",
        {"amount": 19.99, "currency": "EUR"},
        "scraped",
        "https://northroast.example/p/espresso-1kg",
        3,
    )
    assert recorded is not None
    assert recorded[0] == "verified"
    assert recorded[1] == pytest.approx(0.9)
    assert recorded[2] == "price_honored"
    assert recorded[3] == VERIFIER
    assert str(recorded[4]) == snapshot_id
    assert recorded[5] == {"amount": 19.99, "currency": "EUR"}
    assert [tuple(row) for row in evidence] == [("evidence:catalog-page-1", "scraped")]
    assert observation is not None
    assert observation[0] == STORE
    assert observation[1] == "price_honored"
    assert observation[2] == "verified"
    assert observation[3] == pytest.approx(0.8)

    # ``confidence`` means two different things in one exchange, and this pins which is which
    # so a reader cannot get it backwards. The request's ``confidence`` is the VERIFIER's — it
    # is what lands in ``claim_verifications.confidence`` (asserted above) and what the ledger
    # announcement carries. The response's ``verification.confidence`` is the STORE's folded
    # score confidence, straight off ``ledger.trust_scores``, and is a different number under
    # the same key. Asserted against the row rather than a literal, so it tracks the scorer.
    answered = response.json()["verification"]
    assert folded is not None
    assert answered["score"] == pytest.approx(folded[0])
    assert answered["confidence"] == pytest.approx(folded[1])
    assert answered["confidence"] != pytest.approx(0.9), (
        "the response echoed the verifier confidence the request sent; it is documented as "
        "the store's folded score confidence"
    )


@pytest.mark.docker
def test_a_replay_answers_200_and_writes_nothing_a_second_time(
    ledger_clean: Any,
    loop_client: Any,
    claims_snapshot: Callable[..., str],
    claims_counts: Callable[[], dict[str, int]],
    pg_admin: Any,
) -> None:
    """The docstring's promise — 201 when new, 200 on a replay — with nothing written twice.

    ``ledger.trust_observations`` is the table this actually protects. It has no unique index
    over its real columns, so nothing in the database would stop a second identical fold; the
    only defence is that the writer never emits the second INSERT. A replay that answered 200
    while quietly adding an observation would double-count a store's trust and no constraint
    anywhere would notice.
    """
    snapshot_id = claims_snapshot()
    body = _body(snapshot_id)

    first = loop_client.post(VERIFICATIONS, json=body)
    assert first.status_code == 201, first.text
    after_first = claims_counts()

    with pg_admin.cursor() as cursor:
        cursor.execute("select score, confidence, effective_sample_size from ledger.trust_scores")
        score_before = cursor.fetchone()

    second = loop_client.post(VERIFICATIONS, json=body)

    assert second.status_code == 200, (
        f"a replayed verification answered {second.status_code}, not 200: {second.text}"
    )
    replayed = second.json()
    assert replayed["replayed"] is True
    assert replayed["verification"]["written"] is False
    assert replayed["verification"]["verification_id"] is None, (
        "a replay reported a verification_id, so it wrote a second outcome row"
    )
    assert replayed["verification"]["claim_id"] == first.json()["verification"]["claim_id"]
    assert "event" not in replayed, (
        "a replay announced a second event; the ledger already carries the announcement"
    )

    assert claims_counts() == after_first, (
        f"the replay changed the tables: {after_first} -> {claims_counts()}"
    )
    with pg_admin.cursor() as cursor:
        cursor.execute("select score, confidence, effective_sample_size from ledger.trust_scores")
        assert cursor.fetchone() == score_before, "the replay refolded the store's trust score"


@pytest.mark.docker
def test_a_snapshot_bump_re_verifies_and_announces_a_second_time(
    loop_client: Any,
    claims_snapshot: Callable[..., str],
    claims_counts: Callable[[], dict[str, int]],
    claims_events: Callable[[], list[dict[str, Any]]],
) -> None:
    """The same claim against a NEW catalog snapshot is a new outcome, and a new event.

    The migration says in as many words that "a snapshot bump is a different row and therefore
    re-verifies". The event id has to carry the snapshot for that to work end to end: without
    it, the second call wrote and committed its rows and then hit a 409 appending an event id
    it had already used — rows in the tables that the ledger does not corroborate, which is the
    persist-then-announce order's own failure mode running backwards.
    """
    first_snapshot = claims_snapshot()
    second_snapshot = claims_snapshot()
    assert first_snapshot != second_snapshot

    first = loop_client.post(VERIFICATIONS, json=_body(first_snapshot))
    assert first.status_code == 201, first.text

    second = loop_client.post(VERIFICATIONS, json=_body(second_snapshot))

    assert second.status_code == 201, (
        f"re-verifying against a new snapshot answered {second.status_code}: {second.text}"
    )
    assert second.json()["verification"]["written"] is True
    assert (
        second.json()["verification"]["verification_id"]
        != first.json()["verification"]["verification_id"]
    )

    counts = claims_counts()
    assert counts["ledger.claims"] == 1, "the second verification created a second claim"
    assert counts["ledger.claim_verifications"] == 2
    assert counts["ledger.trust_observations"] == 2
    assert counts["ledger.commerce_events"] == 2

    announced = [event["idempotency_key"] for event in claims_events()]
    assert announced == [
        _expected_event_id(_body(first_snapshot)),
        _expected_event_id(_body(second_snapshot)),
    ], f"the two announcements do not carry their own snapshots: {announced}"


# ======================================================================================
# The announcement, asserted by provenance.
# ======================================================================================
@pytest.mark.docker
def test_the_announcement_could_only_have_come_from_the_production_emitter(
    loop_client: Any,
    claims_snapshot: Callable[..., str],
    claims_events: Callable[[], list[dict[str, Any]]],
    pg_admin: Any,
) -> None:
    """The ``claim_verified`` row is a genuine chain link the handler derived, not a fixture.

    Three properties, none of which a row written beside the emitter can have:

    * ``payload["dim"]`` is not in the request body anywhere. It is
      :func:`trust.scoring.claim_dimension` of the claim type, computed inside the writer, so
      its presence with the right value is evidence the handler ran.
    * ``idempotency_key`` is the handler's five-part composition, rebuilt here from the
      module's own ``CLAIM_VERIFIED_KIND`` rather than typed out.
    * ``event_hash`` re-derives, from the STORED row, through
      :func:`trust.ledger.compute_event_hash` — and the whole chain re-verifies against its
      stored anchor. An inserted row hashes to nothing and breaks both.

    ``verification_id`` is asserted ABSENT from the payload on purpose: it is
    ``gen_random_uuid()``-derived, so an event carrying it would hash differently on every
    write, which makes the announcement unreplayable and any redelivery a 409 rather than the
    no-op D16 requires.
    """
    from trust.ledger import GENESIS_HASH, compute_event_hash, verify_chain_in_db
    from trust.scoring import claim_dimension

    snapshot_id = claims_snapshot()
    body = _body(snapshot_id)
    assert "dim" not in body, "the request already carries a dim, so this proves nothing"

    response = loop_client.post(VERIFICATIONS, json=body)
    assert response.status_code == 201, response.text

    events = claims_events()
    assert len(events) == 1, (
        f"this test appends nothing itself, so the chain must hold exactly the handler's one "
        f"announcement; it holds {len(events)}"
    )
    event = events[0]

    assert event["idempotency_key"] == _expected_event_id(body)
    assert event["kind"] == "claim_verified"
    assert event["store_id"] == STORE

    payload = event["payload"]
    assert payload["dim"] == claim_dimension(body["claim_type"]), (
        f"the announced dim {payload['dim']!r} is not the one the approved claim_type table "
        f"routes {body['claim_type']!r} to"
    )
    assert payload["claim_ref"] == CLAIM_REF
    assert payload["status"] == "verified"
    assert payload["catalog_snapshot_id"] == snapshot_id
    assert payload["verifier_version"] == VERIFIER
    assert payload["confidence"] == pytest.approx(body["confidence"]), (
        "the announcement carries something other than the VERIFIER's confidence; the store's "
        "folded score confidence is a different number and belongs in the response, not here"
    )
    assert "verification_id" not in payload, (
        "a server-generated verification_id is in the announced payload, which makes the "
        "event's content irreproducible and its redelivery a 409 instead of a no-op"
    )

    from trust.ledger import rfc3339_ms

    rebuilt = {
        "event_id": event["idempotency_key"],
        "ts": rfc3339_ms(event["occurred_at"]),
        "kind": event["kind"],
        "store_id": event["store_id"],
        "payload": payload,
    }
    assert event["prev_hash"] == GENESIS_HASH
    assert compute_event_hash(event["prev_hash"], rebuilt) == event["event_hash"], (
        "the stored row does not hash to its own event_hash, so it is not a link this "
        "service's sealer produced"
    )

    report = verify_chain_in_db(pg_admin)
    assert report["ok"] is True, f"the chain the handler appended to does not verify: {report}"


# ======================================================================================
# The refusals that need a database to reach, and the rollback behind each one.
# ======================================================================================
@pytest.mark.docker
def test_an_unapproved_claim_type_is_the_callers_mistake_not_a_500(
    loop_client: Any,
    claims_snapshot: Callable[..., str],
    claims_counts: Callable[[], dict[str, int]],
) -> None:
    """The measured ``LookupError`` escape: an unapproved ``claim_type`` must be 422, not 500.

    ``UnmappedClaimType`` derives from :class:`LookupError` and NOT from :class:`ValueError`,
    deliberately — a caller's ``except KeyError`` must not swallow "the human-approved routing
    table has no entry for this". With ``except ValueError`` alone this request escaped the
    handler and became a 500: the service reporting a bad payload as its own outage.
    """
    snapshot_id = claims_snapshot()

    response = loop_client.post(
        VERIFICATIONS, json=_body(snapshot_id, claim_type=UNAPPROVED_CLAIM_TYPE)
    )

    assert response.status_code == 422, (
        f"an unapproved claim_type answered {response.status_code}, and anything 5xx here is "
        f"this service calling the caller's bad payload its own outage: {response.text}"
    )
    detail = response.json()["detail"]
    assert detail["error"] == "unverifiable_claim"
    assert UNAPPROVED_CLAIM_TYPE in detail["message"], (
        f"the refusal does not say which claim_type was refused: {detail['message']}"
    )

    counts = claims_counts()
    assert counts["ledger.claims"] == 0, "a refused claim_type still wrote a claim row"
    assert counts["ledger.commerce_events"] == 0, "a refused request announced an event"


@pytest.mark.docker
@pytest.mark.parametrize(
    ("label", "overrides", "expected_in_message"),
    [
        # A well-formed uuid that is not in ledger.catalog_snapshots: the FK refuses it.
        ("an unknown catalog snapshot", {"catalog_snapshot_id": str(uuid.uuid4())}, "foreign key"),
        # Not a uuid at all: Postgres refuses the cast before the FK is even consulted.
        ("a malformed snapshot uuid", {"catalog_snapshot_id": "not-a-uuid"}, "uuid"),
        # verification_evidence_refs_source_class_check: a CHECK the request model does not
        # duplicate, so this one genuinely reaches the database and comes back through the
        # broad `except Exception`.
        (
            "a source_class outside the vocabulary",
            {"source_class": "vibes"},
            "verification_evidence_refs_source_class_check",
        ),
    ],
)
def test_a_payload_only_postgres_can_refuse_comes_back_as_a_refusal(
    label: str,
    overrides: dict[str, Any],
    expected_in_message: str,
    loop_client: Any,
    claims_snapshot: Callable[..., str],
    claims_counts: Callable[[], dict[str, int]],
) -> None:
    """``psycopg.Error`` derives from :class:`Exception`, and all three of these were 500s.

    The handler's broad ``except`` is what turns them into 4xx. Each case also asserts the
    transaction was rolled back: ``ledger.claims`` is written FIRST, so a refusal that leaves a
    claim row behind has half-recorded something nobody verified — and, worse, left the
    connection in ``InFailedSqlTransaction`` for whoever holds it next.
    """
    snapshot_id = claims_snapshot()

    response = loop_client.post(VERIFICATIONS, json=_body(snapshot_id, **overrides))

    assert 400 <= response.status_code < 500, (
        f"{label} answered {response.status_code}; a payload the caller controls may not "
        f"produce a 5xx: {response.text}"
    )
    assert response.status_code == 422, (
        f"{label} answered {response.status_code}, not the documented 422: {response.text}"
    )
    detail = response.json()["detail"]
    assert detail["error"] == "unverifiable_claim"
    assert expected_in_message in detail["message"].lower(), (
        f"{label}: the refusal does not name what was wrong, so it is a 422 that reads like a "
        f"shrug: {detail['message']}"
    )

    counts = claims_counts()
    assert counts["ledger.claim_verifications"] == 0
    assert counts["ledger.trust_observations"] == 0
    assert counts["ledger.commerce_events"] == 0, f"{label} announced an event it never recorded"


@pytest.mark.docker
def test_the_connection_survives_a_refusal_and_serves_the_next_request(
    loop_client: Any,
    claims_snapshot: Callable[..., str],
    claims_counts: Callable[[], dict[str, int]],
) -> None:
    """One bad request must not brick the endpoint, which is a measured defect, not a worry.

    An earlier version cached one ``autocommit=False`` connection on ``app.state`` forever, so
    a single failing statement left it in ``InFailedSqlTransaction`` and every later request
    died on a transaction it could not see. The fix is a connection per request plus the
    writer's own rollback; this is the assertion that holds both, from outside.
    """
    snapshot_id = claims_snapshot()

    refused = loop_client.post(VERIFICATIONS, json=_body(snapshot_id, claim_type="vibes"))
    assert refused.status_code == 422, refused.text

    accepted = loop_client.post(VERIFICATIONS, json=_body(snapshot_id))

    assert accepted.status_code == 201, (
        f"the request after a refusal answered {accepted.status_code}; the endpoint did not "
        f"recover from one bad payload: {accepted.text}"
    )
    assert claims_counts()["ledger.commerce_events"] == 1


# ======================================================================================
# store_id: popped from the payload, passed separately.
# ======================================================================================
@pytest.mark.docker
def test_a_store_id_nested_inside_the_claim_value_does_not_reroute_the_row(
    ledger_clean: Any,
    loop_client: Any,
    claims_snapshot: Callable[..., str],
    claims_events: Callable[[], list[dict[str, Any]]],
    pg_admin: Any,
) -> None:
    """The pop is TOP-LEVEL only, and this is what that does and does not mean.

    ``payload.pop("store_id", None)`` removes the model's own field so the explicit
    ``store_id=body.store_id`` argument is not a duplicate keyword. It does not, and must not,
    reach inside ``value`` or ``observed_value`` — those are opaque ``jsonb`` the verifier
    observed, and a writer that rewrote a caller's data because a key inside it happened to be
    called ``store_id`` would be corrupting evidence.

    So a nested ``store_id`` is stored verbatim and steers nothing: the claim, the observation
    and the announcement all land under the top-level store. Asserted rather than assumed,
    because "a field named store_id somewhere in the body decides which store is scored" is
    exactly the shape of a cross-tenant write.
    """
    snapshot_id = claims_snapshot()
    body = _body(
        snapshot_id,
        value={"amount": 19.99, "store_id": "store-somebody-else"},
        observed_value={"amount": 19.99, "store_id": "store-somebody-else"},
    )

    response = loop_client.post(VERIFICATIONS, json=body)

    assert response.status_code == 201, (
        f"a nested store_id made a valid request fail with {response.status_code}: {response.text}"
    )
    with pg_admin.cursor() as cursor:
        cursor.execute("select store_id, value from ledger.claims")
        claims = cursor.fetchall()
        cursor.execute("select distinct store_id from ledger.trust_observations")
        observed_stores = [row[0] for row in cursor.fetchall()]
        cursor.execute("select store_id from ledger.trust_scores")
        scored_stores = [row[0] for row in cursor.fetchall()]

    assert [row[0] for row in claims] == [STORE], (
        f"the claim landed under {[row[0] for row in claims]}, not the top-level store_id"
    )
    assert claims[0][1] == {"amount": 19.99, "store_id": "store-somebody-else"}, (
        "the nested value was rewritten; jsonb the verifier observed is evidence and is "
        "stored verbatim"
    )
    assert observed_stores == [STORE]
    assert scored_stores == [STORE]
    assert [event["store_id"] for event in claims_events()] == [STORE]
