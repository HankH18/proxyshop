"""Two unauthenticated 5xx paths on ``apps/trust/src/events/routes.py``, and the rows they left.

Both were **measured against a real uvicorn server on the code as it shipped**, and every
test below was written red against that code. What makes both of them worse than a bad
status code is the ledger's schema: ``ledger.commerce_events`` carries a
``BEFORE UPDATE OR DELETE ... ENABLE ALWAYS`` trigger, so a row admitted here is a row
nobody can evict. A refusal that arrives after the append is not a refusal.

DEFEAT 1 -- 148 anonymous bytes permanently brick ``GET /events/replay?snapshots=true``
---------------------------------------------------------------------------------------
``replay?snapshots=true`` projects every event whose payload names a ``dim`` and a ``type``
into a trust observation (``trust.ledger.replay.observations_from_events``) and folds it
with ``trust.scoring.score``. Both vocabularies are closed and both RAISE rather than drop.
Nothing on the write path checked either. Measured::

    POST /events {"event_id": "e2", "ts": "2026-01-01T00:00:00.000Z", "kind": "feedback",
                  "store_id": "s-1",
                  "payload": {"dim": "price_honored", "type": "positive"}}
        -> 201, store.length 0 -> 1                                  (148 bytes on the wire)
    GET  /events/replay?snapshots=true&as_of=2026-01-01T00:00:00.000Z
        -> 500 "Internal Server Error"   (trust.scoring.engine.UnknownObservationType,
                                          escaping ledger_replay uncaught)

Three more variants of the same class, all measured ``201`` then ``500``:

===============================================================  ========================
``payload {"dim": "not-a-dim", "type": "verified"}``             ``UnknownTrustDimension``
``payload {..., "type": "verified", "weight": 5.0}``             ``InvalidObservationWeight``
``payload {..., "observed_at": "not-a-date"}``                   ``ValueError``
===============================================================  ========================

DEFEAT 2 -- a non-Latin-1 ``event_id`` is a 500 AFTER the row has committed
---------------------------------------------------------------------------
``post_event`` reports the created event in ``Location: /events/{event_id}``, and header
values are Latin-1 on the wire. Measured::

    POST /events  event_id "заказ-1"  -> 500, store.length 0 -> 1
        (UnicodeEncodeError from `response.headers["Location"] = f"/events/{...}"`)

Same for CJK ``"註文-1"`` and for an emoji. ``GET /events`` afterwards listed all three, and
a retry of the same id 500ed too -- the idempotent-replay path sets the same header, so the
caller could neither succeed nor make the row go away. ``"café-1"`` (U+00E9, inside Latin-1)
returned ``201``: the boundary was exactly the header codec.

Its sibling is worse than a 500. An ``event_id`` carrying ``"\\r\\n"``, a bare ``"\\n"`` or
``"\\x00"`` is *encodable* and is not a legal header value, so uvicorn dropped the
connection with no response at all (``httpx.RemoteProtocolError: Server disconnected
without sending a response``) -- and the row still committed. The caller got no status to
retry against and the ledger kept the event.

What these tests grade
----------------------
The write-path guards (a payload the replay could not interpret, and an identifier a
response header could not carry) are graded through a **real loopback uvicorn server**, and
each one is graded on two things: the status, and ``store.length``. A clean 422 over a
ledger that kept the row would be the pre-fix behaviour wearing a better status code.

The read-path guard is graded over a ledger that ALREADY holds a poison row. Such a row
cannot be POSTed any more, so the fixture seals it straight into the store through
``apps.trust.src.events.store.append`` -- bypassing the HTTP door exactly as a deployment
that took these writes before the guard existed is stuck with them. That is the honest
construction: the row exists, it cannot be deleted, and the reader still owes an answer.

Nothing here hardcodes a vocabulary: the six dimensions, the weight table and the
identifier ceiling are imported from the modules that declare them, so a change to any of
them moves these gates with it. No wall clock either -- :data:`AS_OF` is fixed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from apps.trust.src.events import InMemoryEventStore, append
from apps.trust.src.events.service import create_events_app
from apps.trust.src.scoring import OBSERVATION_WEIGHTS, PRIOR_ALPHA, TRUST_DIMENSIONS
from proxyshop_support.asgi_server import serve

#: A fixed instant. Nothing in this file compares against the wall clock.
AS_OF = "2026-01-01T00:00:00.000Z"

#: The exact 148-byte body that was measured to return ``201`` and then brick every
#: subsequent snapshot replay. ``type`` is the defect: ``"positive"`` is the *polarity* of an
#: observation type, not an observation type, and the scorer's weight table has no entry for
#: it -- so it raises rather than scoring a dishonest store as a clean one.
POISON_POST: dict[str, Any] = {
    "event_id": "e2",
    "ts": AS_OF,
    "kind": "feedback",
    "store_id": "s-1",
    "payload": {"dim": "price_honored", "type": "positive"},
}

#: Every class of payload the trust replay cannot interpret, paired with the field the
#: refusal has to name. All four reach ``trust.scoring.score`` and all four were unchecked.
POISON_PAYLOADS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("type", {"dim": "price_honored", "type": "positive"}),
    ("dim", {"dim": "not-a-dim", "type": "verified"}),
    ("weight", {"dim": "price_honored", "type": "verified", "weight": 5.0}),
    ("observed_at", {"dim": "price_honored", "type": "verified", "observed_at": "not-a-date"}),
)

#: Identifiers outside Latin-1: Cyrillic, CJK, an emoji. Each was measured as a ``500``
#: *after* the append committed.
NON_LATIN_1_IDS: tuple[str, ...] = ("заказ-1", "註文-1", "🚀-1")

#: Identifiers that are Latin-1 encodable and are still not legal header values. Spelled as
#: escapes rather than written into this file as raw bytes: a literal CR in a source file is
#: invisible in review and gets normalised by editors.
CONTROL_CHARACTER_IDS: tuple[str, ...] = ("ev\r\nX-Injected: 1", "ev\nb", "ev\x00b")


# ======================================================================================
# helpers
# ======================================================================================
def _event(event_id: str = "ev-1", **extra: Any) -> dict[str, Any]:
    """A plain, replayable ``LedgerEvent`` body, ready to POST."""
    event: dict[str, Any] = {
        "event_id": event_id,
        "ts": AS_OF,
        "kind": "feedback",
        "store_id": "s-1",
        "payload": {"n": 1},
    }
    event.update(extra)
    return event


@contextlib.contextmanager
def _client_for(store: Any) -> Iterator[httpx.Client]:
    """A real HTTP client against a real loopback server serving ``store``.

    A real server and not ``TestClient``: both defects below live in what uvicorn does with
    a response header the app handed it, and an in-process test double never encodes one.
    """
    with (
        serve(create_events_app(store)) as base_url,
        httpx.Client(base_url=base_url, timeout=60.0) as client,
    ):
        yield client


def _asgi_post(
    app: Any, body: bytes, headers: list[tuple[bytes, bytes]]
) -> tuple[int, dict[str, Any]]:
    """One ``POST /events`` driven straight at the ASGI app, with RAW header bytes.

    Needed for exactly one case: the ``Idempotency-Key`` guard. A header value carrying a
    control character never survives the wire -- measured, uvicorn's own parser answers
    ``400 "Invalid HTTP request received."`` before the application is entered, and httpx
    will not even encode one -- so the HTTP path cannot reach the app-level guard. It is
    still worth having and worth grading: a proxy or a different parser in front of this
    service decides what bytes arrive, and starlette decodes header bytes as Latin-1 with no
    opinion about control characters.
    """
    sent: list[dict[str, Any]] = []

    async def run() -> None:
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/events",
                "raw_path": b"/events",
                "query_string": b"",
                "root_path": "",
                "headers": [(b"host", b"testserver"), *headers],
                "client": ("127.0.0.1", 5000),
                "server": ("testserver", 80),
            },
            receive,
            send,
        )

    asyncio.run(run())

    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert starts, "the app produced no response at all"
    payload = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    return int(starts[0]["status"]), json.loads(payload or b"{}")


def _poisoned_store() -> tuple[InMemoryEventStore, int]:
    """A ledger that ALREADY holds an un-replayable row, and that row's ``seq``.

    Sealed straight in through ``apps.trust.src.events.store.append`` rather than POSTed:
    the door refuses this event now, and the point of the read-path gate is the ledger of a
    deployment that took the write before the door existed. The ``ENABLE ALWAYS`` trigger
    means such a row cannot be deleted, so the reader has to answer over it.
    """
    store = InMemoryEventStore()
    outcome = append(
        store,
        {
            "event_id": "poison-1",
            "ts": AS_OF,
            "kind": "feedback",
            "store_id": "s-1",
            "payload": {"dim": "price_honored", "type": "positive"},
        },
    )
    assert store.length == 1, "the fixture did not seal its poison row into the ledger"
    return store, int(outcome.seq)


# ======================================================================================
# DEFEAT 1 -- the write path refuses a payload the replay could not interpret
# ======================================================================================
def test_the_148_byte_poison_post_is_refused_and_nothing_is_appended() -> None:
    """The measured request, byte for byte: ``201`` then a permanent ``500`` on replay."""
    assert len(json.dumps(POISON_POST).encode("utf-8")) == 148, (
        "the measured body is 148 bytes; this constant has drifted from the measurement"
    )

    store = InMemoryEventStore()
    with _client_for(store) as client:
        response = client.post("/events", json=POISON_POST)

    assert 400 <= response.status_code < 500, (
        f"the poison POST answered {response.status_code}; a refusal on an unauthenticated "
        f"door must be a clean 4xx"
    )
    detail = response.json()["detail"]
    assert detail["error"] == "unreplayable_payload", f"unexpected refusal: {detail}"
    assert detail["field"] == "type", (
        f"the refusal blamed {detail['field']!r}; `type` is what the scorer has no weight for"
    )
    assert store.length == 0, (
        "the poison event was appended to an append-only ledger -- there is no eviction to "
        "fall back on, so every later snapshot replay is a 500 forever"
    )
    assert len(response.content) < 4_096, f"the refusal was {len(response.content):,} bytes long"
    assert "positive" not in response.text, (
        "the refusal echoed the caller's payload back; a payload field is caller-controlled "
        "and may be tens of kilobytes"
    )


@pytest.mark.parametrize(("field", "payload"), POISON_PAYLOADS, ids=[f for f, _ in POISON_PAYLOADS])
def test_every_poison_class_is_a_4xx_naming_the_field_the_scorer_choked_on(
    field: str, payload: dict[str, Any]
) -> None:
    """Four fields reach the scorer -- ``dim``, ``type``, ``weight``, ``observed_at`` -- and
    every one of them was measured ``201`` at the door and ``500`` at the reader."""
    store = InMemoryEventStore()
    with _client_for(store) as client:
        response = client.post("/events", json=_event(payload=payload))

    assert 400 <= response.status_code < 500, (
        f"a payload whose {field!r} the scorer cannot interpret answered {response.status_code}"
    )
    detail = response.json()["detail"]
    assert detail["error"] == "unreplayable_payload", f"unexpected refusal: {detail}"
    assert detail["field"] == field, (
        f"the refusal blamed {detail['field']!r} for a payload whose defect is {field!r}; a "
        f"caller cannot fix a field the server will not name"
    )
    assert store.length == 0, f"a payload with an un-replayable {field!r} reached the ledger"


@pytest.mark.parametrize(
    ("field", "vocabulary"),
    [("dim", sorted(TRUST_DIMENSIONS)), ("type", sorted(OBSERVATION_WEIGHTS))],
)
def test_a_closed_vocabulary_refusal_publishes_the_vocabulary_it_closed(
    field: str, vocabulary: list[str]
) -> None:
    """Both closed vocabularies are server-owned, so quoting them costs nothing and is the
    only thing that makes the refusal actionable."""
    payload = dict(next(body for name, body in POISON_PAYLOADS if name == field))

    store = InMemoryEventStore()
    with _client_for(store) as client:
        response = client.post("/events", json=_event(payload=payload))

    detail = response.json()["detail"]
    assert detail.get("permitted") == vocabulary, (
        f"the {field!r} refusal published {detail.get('permitted')!r}, not the vocabulary "
        f"the scorer actually reads ({vocabulary!r})"
    )


def test_an_honest_feedback_event_is_still_accepted_and_still_replays() -> None:
    """The positive control, and the one that decides whether the guard is a guard or a wall.

    The payload also satisfies ``contracts.ledger.validate_ledger_payload``'s published
    ``feedback`` shape (``matched_pitch``, ``reason``) -- which is deliberately NOT what the
    door checks (extra keys are allowed there by design, so a shape-valid payload can still
    carry a ``dim`` nobody weighted), but a positive control that failed the published shape
    would be grading the wrong event.
    """
    store = InMemoryEventStore()
    honest = _event(
        event_id="ev-honest",
        payload={
            "matched_pitch": True,
            "reason": "delivered as pitched",
            "dim": "price_honored",
            "type": "verified",
        },
    )

    with _client_for(store) as client:
        posted = client.post("/events", json=honest)
        replayed = client.get("/events/replay", params={"snapshots": "true", "as_of": AS_OF})

    assert posted.status_code == 201, f"an honest feedback event was refused: {posted.text}"
    assert store.length == 1, "the accepted event is not in the ledger"
    assert replayed.status_code == 200, (
        f"replaying a ledger of one honest event answered {replayed.status_code}: "
        f"{replayed.text[:400]}"
    )
    snapshot = replayed.json()["snapshots"]["s-1"]
    assert isinstance(snapshot.get("score"), float), (
        f"the replayed snapshot carries no score: {snapshot!r}"
    )
    assert (
        snapshot["dims"]["price_honored"]["alpha"] == PRIOR_ALPHA + OBSERVATION_WEIGHTS["verified"]
    ), "a `verified` observation did not move the dimension it named"


def test_an_admissible_relative_weight_is_accepted_and_survives_into_the_replay() -> None:
    """``weight`` is refused at ``5.0`` and must NOT be refused at ``0.25``.

    R14's per-observation discount is a real channel -- ``trust.feedback`` computes exactly
    this number -- so a guard that refused every ``weight`` would close the defect by
    breaking the feature. The assertion is on the arithmetic and not on the status code: a
    weight that were dropped between the door and the scorer would still return ``201`` and
    would still replay, at full strength, which is precisely the T-206 defect.
    """
    store = InMemoryEventStore()
    weighted = _event(
        event_id="ev-weighted",
        store_id="s-weighted",
        payload={
            "matched_pitch": True,
            "reason": "buyer's own return contradicts this report",
            "dim": "price_honored",
            "type": "verified",
            "weight": 0.25,
        },
    )

    with _client_for(store) as client:
        posted = client.post("/events", json=weighted)
        replayed = client.get("/events/replay", params={"snapshots": "true", "as_of": AS_OF})

    assert posted.status_code == 201, f"an admissible weight of 0.25 was refused: {posted.text}"
    assert replayed.status_code == 200, f"the weighted event did not replay: {replayed.text[:400]}"

    dimension = replayed.json()["snapshots"]["s-weighted"]["dims"]["price_honored"]
    expected_alpha = PRIOR_ALPHA + OBSERVATION_WEIGHTS["verified"] * 0.25
    assert dimension["alpha"] == expected_alpha, (
        f"alpha replayed as {dimension['alpha']} against an expected {expected_alpha}: the "
        f"0.25 weight did not survive the door, so one discounted report counted as a whole "
        f"honest one"
    )


# ======================================================================================
# DEFEAT 2 -- the write path refuses an identifier a response header could not carry
# ======================================================================================
@pytest.mark.parametrize("event_id", NON_LATIN_1_IDS)
def test_a_non_latin_1_event_id_is_refused_and_the_ledger_never_holds_it(event_id: str) -> None:
    """The one that matters most: the pre-fix ``500`` told the caller the write had failed
    while the ledger kept the row, and the retry 500ed on the same header."""
    store = InMemoryEventStore()
    assert store.length == 0, "the fixture store did not start empty"

    with _client_for(store) as client:
        response = client.post("/events", json=_event(event_id=event_id))
        listed = client.get("/events").json()

    assert 400 <= response.status_code < 500, (
        f"a non-Latin-1 event_id answered {response.status_code}; the measured pre-fix "
        f"answer was a 500 raised out of the Location header AFTER the append committed"
    )
    detail = response.json()["detail"]
    assert detail["error"] == "identifier_not_renderable", f"unexpected refusal: {detail}"
    assert detail["field"] == "event_id", f"the refusal blamed {detail['field']!r}"
    assert detail["reason"] == "not_latin_1", (
        f"the refusal called this {detail['reason']!r}; the boundary measured is exactly the "
        f"Latin-1 header codec"
    )
    assert store.length == 0, (
        "the row committed anyway -- which is the whole defect: a caller told the write "
        "failed, and an un-evictable row in the ledger"
    )
    assert store.get(event_id) is None, "the ledger is holding an id it told the caller it refused"
    assert listed["count"] == 0, f"GET /events lists {listed['count']} event(s) after a refusal"
    assert event_id not in response.text, "the refusal echoed the identifier back"


@pytest.mark.parametrize("event_id", CONTROL_CHARACTER_IDS)
def test_an_event_id_carrying_a_control_character_gets_a_response_at_all(event_id: str) -> None:
    """Worse than a 500: uvicorn dropped the connection with no status, and the row committed.

    ``httpx.RemoteProtocolError("Server disconnected without sending a response")`` is what
    the caller measured -- not a status code to retry against, not a body to read, and an
    un-evictable event in the ledger regardless. So the first thing asserted here is that
    there IS a response.
    """
    store = InMemoryEventStore()

    with _client_for(store) as client:
        try:
            response = client.post("/events", json=_event(event_id=event_id))
        except httpx.RemoteProtocolError as exc:  # pragma: no cover - the pre-fix behaviour
            pytest.fail(
                f"the server dropped the connection instead of answering: {exc}. The caller "
                f"gets no status at all, and the measured pre-fix ledger kept the row anyway"
            )

    assert 400 <= response.status_code < 500, (
        f"a control-charactered event_id answered {response.status_code}"
    )
    detail = response.json()["detail"]
    assert detail["error"] == "identifier_not_renderable", f"unexpected refusal: {detail}"
    assert detail["reason"] == "control_character", (
        f"the refusal called this {detail['reason']!r}; a CR/LF in a header value is a "
        f"response split, which is a different problem from a codec"
    )
    assert store.length == 0, "a control-charactered event_id reached the append-only ledger"
    assert "X-Injected" not in response.text, (
        "the refusal echoed the caller's control characters into this process's own response"
    )


def test_a_latin_1_event_id_is_still_accepted_and_still_gets_its_location_header() -> None:
    """``"café-1"`` (U+00E9) measured ``201`` before the fix, so it must measure ``201`` after.

    The boundary is the header codec and nothing wider: a guard that refused every non-ASCII
    identifier would be a different, smaller service.
    """
    store = InMemoryEventStore()

    with _client_for(store) as client:
        accented = client.post("/events", json=_event(event_id="café-1"))
        ascii_only = client.post("/events", json=_event(event_id="ev-plain"))

    assert accented.status_code == 201, f"a Latin-1 event_id was refused: {accented.text}"
    assert accented.headers["Location"] == "/events/café-1", (
        f"Location came back as {accented.headers['Location']!r}; the header this route "
        f"builds is the whole reason the character set is bounded"
    )
    assert ascii_only.status_code == 201, f"a plain ASCII event_id was refused: {ascii_only.text}"
    assert ascii_only.headers["Location"] == "/events/ev-plain"
    assert store.length == 2, f"the ledger holds {store.length} of the 2 accepted events"


@pytest.mark.parametrize("field", ["event_id", "auction_id", "store_id", "order_ref"])
def test_the_character_guard_covers_every_caller_chosen_identifier(field: str) -> None:
    """All four are chosen by the caller, indexed by the ledger, and permanent once admitted.

    Only ``event_id`` reaches a header today. The guard that would matter is the one already
    in place before somebody interpolates ``store_id`` into one -- and by then the rows are
    un-evictable.
    """
    from apps.trust.src.events.routes import MAX_IDENTIFIER_LENGTH

    for value, reason in (
        ("заказ-1", "not_latin_1"),
        ("bad\r\nX-Injected: 1", "control_character"),
    ):
        assert len(value) <= MAX_IDENTIFIER_LENGTH, (
            "this identifier is over the length ceiling, so the LENGTH guard would refuse it "
            "and the character guard would never run"
        )
        store = InMemoryEventStore()
        with _client_for(store) as client:
            response = client.post("/events", json=_event(**{field: value}))

        assert 400 <= response.status_code < 500, (
            f"an unrenderable {field} answered {response.status_code}"
        )
        detail = response.json()["detail"]
        assert detail["error"] == "identifier_not_renderable", f"unexpected refusal: {detail}"
        assert detail["field"] == field, (
            f"an unrenderable {field} was refused as {detail['field']!r}"
        )
        assert detail["reason"] == reason, f"{field}={value!r} was called {detail['reason']!r}"
        assert store.length == 0, f"an unrenderable {field} reached the ledger"


def test_the_character_guard_covers_the_idempotency_key_header() -> None:
    """The fifth caller-chosen identifier, and the only one that arrives as a header.

    Driven at the ASGI app with raw header bytes: see :func:`_asgi_post` for why the wire
    cannot deliver these (uvicorn's parser refuses them first, which is itself a clean 4xx --
    graded below).
    """
    store = InMemoryEventStore()
    body = json.dumps(_event(event_id="ev-1")).encode("utf-8")

    status, payload = _asgi_post(
        create_events_app(store),
        body,
        [(b"content-type", b"application/json"), (b"idempotency-key", b"ev-1\x01x")],
    )

    assert 400 <= status < 500, f"a control-charactered Idempotency-Key answered {status}"
    detail = payload["detail"]
    assert detail["error"] == "identifier_not_renderable", f"unexpected refusal: {detail}"
    assert detail["field"] == "Idempotency-Key", f"the refusal blamed {detail['field']!r}"
    assert detail["reason"] == "control_character", f"called {detail['reason']!r}"
    assert store.length == 0, "a control-charactered Idempotency-Key reached the ledger"


def test_a_control_charactered_idempotency_key_never_reaches_the_ledger_over_the_wire() -> None:
    """The same header over real HTTP: refused by the parser, and still nothing appended.

    Measured: uvicorn answers ``400 "Invalid HTTP request received."`` without entering the
    application. That is a clean 4xx and an empty ledger, which is the property being graded
    -- not which layer produced it.
    """
    store = InMemoryEventStore()

    with _client_for(store) as client:
        try:
            response = client.post(
                "/events", json=_event(event_id="ev-1"), headers={"Idempotency-Key": "ev-1\x01x"}
            )
        except httpx.HTTPError as exc:  # pragma: no cover - httpx may refuse to encode it
            pytest.fail(f"the client could not even send the request: {type(exc).__name__}: {exc}")

    assert 400 <= response.status_code < 500, (
        f"a control-charactered Idempotency-Key answered {response.status_code} over the wire"
    )
    assert store.length == 0, "the ledger holds an event whose Idempotency-Key was refused"


# ======================================================================================
# The read path -- a ledger that ALREADY holds a poison row
# ======================================================================================
def test_a_ledger_that_already_holds_a_poison_row_answers_a_4xx_naming_the_row() -> None:
    """The rows written before the door existed cannot be deleted, so the reader owes an answer.

    A ``500`` here is the pre-fix behaviour: an unauthenticated caller gets "Internal Server
    Error" with nothing naming the row, forever, and no operator reading it can tell a broken
    scorer from one bad event.
    """
    store, poison_seq = _poisoned_store()

    with _client_for(store) as client:
        response = client.get("/events/replay", params={"snapshots": "true", "as_of": AS_OF})

    assert 400 <= response.status_code < 500, (
        f"a snapshot replay over a poisoned ledger answered {response.status_code}: "
        f"{response.text[:400]}"
    )
    detail = response.json()["detail"]
    assert detail["error"] == "unreplayable_ledger", f"unexpected refusal: {detail}"
    assert detail["seq"] == poison_seq, (
        f"the refusal named seq {detail['seq']!r} for a poison row at seq {poison_seq}; an "
        f"answer that cannot name the row leaves the operator no way to find it"
    )
    assert detail["event_id"] == "poison-1", f"the refusal named event {detail['event_id']!r}"
    assert detail["field"] == "type", (
        f"the refusal blamed {detail['field']!r}; the row's defect is its observation `type`"
    )


@pytest.mark.parametrize(("field", "payload"), POISON_PAYLOADS, ids=[f for f, _ in POISON_PAYLOADS])
def test_no_pre_existing_poison_row_of_any_class_makes_the_reader_a_5xx(
    field: str, payload: dict[str, Any]
) -> None:
    """Every class of stored poison, graded on the ONE property the read path owes: not a 5xx.

    Deliberately weaker than the gate above, and deliberately separate from it. That one
    asserts the refusal NAMES the row, which it can only do by re-running the write path's
    predicate -- so it goes red when the *write* guard is stubbed as well, and its evidence
    is therefore shared. This one asserts only that the caller gets a bounded 4xx answer
    instead of ``Internal Server Error``, which is a property of the read path's `except`
    clause alone. Stub that clause and all four of these go red; stub anything else and they
    stay green.

    All four classes, because the read path catches ``(LookupError, ValueError)`` and the
    four defects arrive as three different exception types across both branches of that
    tuple -- a narrower catch would still pass a single-class gate.
    """
    store = InMemoryEventStore()
    append(
        store,
        {
            "event_id": f"poison-{field}",
            "ts": AS_OF,
            "kind": "feedback",
            "store_id": "s-1",
            "payload": payload,
        },
    )
    assert store.length == 1, "the fixture did not seal its poison row into the ledger"

    with _client_for(store) as client:
        response = client.get("/events/replay", params={"snapshots": "true", "as_of": AS_OF})

    assert response.status_code < 500, (
        f"a stored payload whose {field!r} the scorer cannot read answered "
        f"{response.status_code}. The row cannot be deleted -- the ledger's ENABLE ALWAYS "
        f"trigger refuses UPDATE and DELETE -- so a 5xx here is permanent, and permanent "
        f"'Internal Server Error' is indistinguishable from the service being down"
    )
    assert response.status_code == 422, f"unexpected status: {response.status_code}"
    assert response.json()["detail"]["error"] == "unreplayable_ledger", response.text[:300]
    assert len(response.content) < 4_096, (
        f"the refusal was {len(response.content):,} bytes; a diagnosis is not a licence to "
        f"echo the stored payload back"
    )


def test_verification_stays_available_over_a_ledger_that_cannot_be_snapshotted() -> None:
    """One un-replayable row must not take the evidence endpoint down.

    ``snapshots=false`` is the S3 evidence path -- links, anchor and the recomputed stream
    hash -- and none of it goes anywhere near the scorer. A poison row that made the whole
    endpoint unavailable would turn 148 bytes into "this ledger can no longer be verified",
    which is exactly the state an attacker who had just tampered with it would like.
    """
    store, _ = _poisoned_store()

    with _client_for(store) as client:
        verified = client.get("/events/replay", params={"snapshots": "false"})
        chain = client.get("/events/verify")
        listed = client.get("/events")
        one = client.get("/events/poison-1")
        head = client.get("/events/head")

    assert verified.status_code == 200, (
        f"snapshots=false answered {verified.status_code} over a poisoned ledger: "
        f"{verified.text[:300]}"
    )
    assert verified.json()["ok"] is True, "the chain itself is intact; the payload is what is not"
    assert chain.status_code == 200, f"GET /events/verify answered {chain.status_code}"
    assert listed.status_code == 200, f"GET /events answered {listed.status_code}"
    assert listed.json()["count"] == 1, "the poison row is in the ledger and must still be listed"
    assert one.status_code == 200, f"GET /events/poison-1 answered {one.status_code}"
    assert head.status_code == 200, f"GET /events/head answered {head.status_code}"


# ======================================================================================
# The meta-gate
# ======================================================================================
def test_no_malformed_input_this_router_admits_can_produce_a_5xx() -> None:
    """The claim ``routes.py`` now states: *no input a caller can send may produce a 5xx.*

    Driven as one loop over every malformed body above, against one server, checking both
    doors: the POST itself, and the ``replay?snapshots=true`` that a poison row was measured
    to brick afterwards. A 5xx on either is the defect, whichever request produced it.
    """
    bodies: list[dict[str, Any]] = [POISON_POST]
    bodies += [
        _event(event_id=f"p-{index}", payload=payload)
        for index, (_, payload) in enumerate(POISON_PAYLOADS)
    ]
    bodies += [_event(event_id=event_id) for event_id in NON_LATIN_1_IDS]
    bodies += [_event(event_id=event_id) for event_id in CONTROL_CHARACTER_IDS]
    bodies += [
        _event(store_id="заказ-1"),
        _event(auction_id="a\r\nX-Injected: 1"),
        _event(order_ref="o\x00-1"),
        # NaN fails BOTH of `relative_observation_weight`'s comparisons, which is how it
        # would otherwise reach a served score as a NaN with nothing naming the observation.
        # Serialised by hand because httpx refuses to encode it (`allow_nan=False`), which is
        # a client-side courtesy no attacker has to extend.
        _event(payload={"dim": "price_honored", "type": "verified", "weight": float("nan")}),
        _event(payload={"dim": "price_honored", "type": "verified", "weight": "1.0"}),
        _event(payload={"dim": 7, "type": "verified"}),
        _event(payload={"dim": "price_honored", "type": None, "observed_at": ""}),
    ]

    store = InMemoryEventStore()
    with _client_for(store) as client:
        for body in bodies:
            raw = json.dumps(body).encode("utf-8")
            try:
                response = client.post(
                    "/events", content=raw, headers={"content-type": "application/json"}
                )
            except httpx.RemoteProtocolError as exc:  # pragma: no cover - the pre-fix behaviour
                pytest.fail(f"the server dropped the connection on {raw[:200]!r}: {exc}")
            assert response.status_code < 500, (
                f"POST /events answered {response.status_code} for {raw[:200]!r}; no input a "
                f"caller can send may produce a 5xx on an unauthenticated door"
            )
            replayed = client.get("/events/replay", params={"snapshots": "true", "as_of": AS_OF})
            assert replayed.status_code < 500, (
                f"replay?snapshots=true answered {replayed.status_code} after POSTing "
                f"{raw[:200]!r} -- one admitted row bricked the reader"
            )
