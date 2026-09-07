"""Gates on the signed external bid door's own boundary (`exchange.external_bids.routes`).

T-244 shipped the door and asked one question of it: does a served request REACH
``store_agent.external.door.receive_bid``. That is the question its gate measures and it is
not the question this file asks. Reaching the door is worth nothing if the request that
arrives there can be aimed at the wrong auction, counted twice, or made arbitrarily large —
each of those defeats a rule the door itself enforces, from OUTSIDE the door, where the
door's own tests cannot see it.

Every node here was written red against the door as first merged, and each one names the
witness it was red on.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any

import pytest
from exchange.external_bids.routes import configure_external_bids
from exchange.main import create_app
from exchange.ranking.serving import configure_ranking
from fastapi.testclient import TestClient
from store_agent.external import sign_bid

KEY = "external-secret-key-0001"
KEY_ID = "key-2026-01"
STORE = "store-external-1"
PRODUCT = "ext-prod-1"

#: Quoted in the recursion gate's failure message; imported lazily elsewhere.
from exchange.external_bids.routes import MAX_SUBMISSION_BYTES as MAX_BODY  # noqa: E402

#: The two auctions every reconciliation witness needs: one whose terms REFUSE the bid and one
#: whose terms would admit it. The attack is to sign for the first and post to the second.
STRICT = "auction-strict"
LOOSE = "auction-loose"
EXPIRED = "auction-expired"
OPEN = "auction-open"


class _Record:
    """The two fields `_auction_terms` reads off an `AuctionRecord`, and nothing else."""

    def __init__(self, *, roster: list[dict[str, Any]], deadline: float | None) -> None:
        self.roster = roster
        self.deadline = deadline


class _StubMachine:
    """An auction store with exactly the auctions a witness needs.

    A stub rather than four real ``POST /auctions`` calls because these gates turn on the
    auction's TERMS — a zero-depth roster, a deadline already past — and driving the real
    route to produce those states would test the auction route's clamping, not this door's
    reconciliation. ``_auction_terms`` reads ``.roster`` and ``.deadline`` off whatever
    ``machine.get`` returns, and that seam is what is exercised here.
    """

    def __init__(self, records: dict[str, _Record]) -> None:
        self._records = records

    def get(self, auction_id: str) -> _Record:
        return self._records[auction_id]


class _Queue:
    def __init__(self) -> None:
        self.items: list[Any] = []

    def enqueue(self, item: Any) -> None:
        self.items.append(item)


def _roster(max_discount_pct: float) -> list[dict[str, Any]]:
    return [
        {
            "store_id": STORE,
            "product_ref": PRODUCT,
            "list_price": 100.0,
            "max_discount_pct": max_discount_pct,
        }
    ]


def _app(*, records: dict[str, _Record] | None = None, wire_nonces: bool = False) -> Any:
    """A fully wired exchange whose external door will ADMIT a correct submission.

    Everything the door refuses on when unwired — the keyring, the trust snapshot — is
    supplied, so that a refusal observed in these tests is the one the test is about rather
    than a deployment that admits nothing.
    """
    app = create_app()
    configure_ranking(
        app, trust_snapshot={STORE: {"store_id": STORE, "score": 0.9, "blacklisted": False}}
    )
    queue = _Queue()
    configure_external_bids(app, keyring={STORE: {KEY_ID: KEY}}, queue=queue)
    app.state.auction_machine = _StubMachine(records or {})
    if wire_nonces:
        from store_agent.external.nonces import NonceStore  # noqa: PLC0415

        configure_external_bids(app, nonces=NonceStore())
    app.state.test_queue = queue
    return app


def _payload(auction_id: str, *, nonce: str, unit_price: float = 90.0, **over: Any) -> dict:
    body = {
        "auction_id": auction_id,
        "store_id": STORE,
        "offer": {
            "product_ref": PRODUCT,
            "unit_price": unit_price,
            "total_price": unit_price,
            "discount": None,
            "commitments": [],
            "expires_at": "2999-01-01T00:00:00Z",
        },
        "claims": [],
        "message": "m",
        "agent_version": "ext-0.1.0",
        "schema_version": "1",
        "signer_id": STORE,
        "key_id": KEY_ID,
        "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "nonce": nonce,
    }
    body.update(over)
    return body


def _submit(client: TestClient, path_auction_id: str, payload: dict) -> Any:
    return client.post(
        f"/v1/auctions/{path_auction_id}/bids",
        json=payload,
        headers={"X-ProxyShop-Signature": sign_bid(payload, KEY)},
    )


def test_the_door_admits_a_correct_submission() -> None:
    """The arming control. Every refusal asserted below is worth nothing without this.

    A door that answered 400 to everything would satisfy each negative gate in this file by
    accident, so the positive case is asserted first and in its own name: a correctly signed
    bid, on its own auction's path, inside the auction's declared depth, is ADMITTED and
    queued for verification.
    """
    future = time.time() + 300
    app = _app(records={LOOSE: _Record(roster=_roster(50.0), deadline=future)})
    client = TestClient(app, raise_server_exceptions=False)

    response = _submit(client, LOOSE, _payload(LOOSE, nonce="armed-1"))

    assert response.status_code == 202, (
        f"the door refused a correct submission with {response.status_code} "
        f"{response.text[:300]}; every refusal gate in this file would then pass vacuously"
    )
    assert len(app.state.test_queue.items) == 1, (
        "an admitted submission queued no verification work item, so 'admitted' here does not "
        "mean what the contract says it means"
    )


def test_a_signed_bid_cannot_be_admitted_on_another_auctions_path() -> None:
    """The URL must not be able to overrule the auction the seller actually signed for.

    ``submit_external_bid`` reads the path parameter only to look up the auction's TERMS and
    then hands the door the payload's own ``auction_id``. So the terms a submission is judged
    against are chosen by the URL while the identity it is judged AS is chosen by the body,
    and a seller who does not like one auction's terms can borrow another's by changing the
    address — without re-signing anything, because the signature covers the body and the body
    is unchanged.

    Two witnesses, each a rule the door enforces and this bypasses. Both were measured against
    the door as first merged:

    * THE PRICE WALL. A bid signed for a zero-depth auction is refused
      ``price_under_declared_depth:offer.unit_price`` on its own path, and the byte-identical
      signed body was ADMITTED 202 on a 50%-depth auction's path — and queued carrying the
      ORIGINAL auction_id, so the work item names an auction whose terms never admitted it.
    * THE AUCTION DEADLINE. A bid for an auction whose deadline has passed is refused
      ``after_auction_deadline`` on its own path and was ADMITTED 202 on an open auction's
      path. That gate is one of the six the frozen E4 suite drives through ``receive_bid``,
      which is what makes this worse than a bug in a new route: T-244 exists to stop
      ``e4_store_agent_passing`` counting capabilities no request performs, and a door that
      lets the URL retarget the deadline check counts one that a request performs WRONGLY.

    The fix is available without any new input: ``auction_id`` is inside the signed envelope,
    so the door's own copy can be compared to the path. The mismatch is decidable from two
    values the caller supplied and reveals nothing about which auctions exist, so refusing it
    before the door leaks nothing and spends no nonce.
    """
    now = time.time()
    app = _app(
        records={
            STRICT: _Record(roster=_roster(0.0), deadline=now + 300),
            LOOSE: _Record(roster=_roster(50.0), deadline=now + 300),
            EXPIRED: _Record(roster=_roster(50.0), deadline=now - 300),
            OPEN: _Record(roster=_roster(50.0), deadline=now + 300),
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    # -- witness one: the price wall -------------------------------------------------------
    priced = _payload(STRICT, nonce="cross-price-1", unit_price=50.0)
    own = _submit(client, STRICT, priced)
    assert own.status_code == 400, (
        f"the price-wall witness is stale: a 50.0 bid on a zero-depth auction answered "
        f"{own.status_code}, so the cross-post below would prove nothing"
    )
    assert any("price" in reason for reason in own.json()["reasons"]), (
        f"the price-wall witness did not refuse on price: {own.json()['reasons']}"
    )

    borrowed = _submit(client, LOOSE, priced)
    assert borrowed.status_code == 400, (
        "a bid SIGNED for "
        f"{STRICT} was answered {borrowed.status_code} on {LOOSE}'s path. The signature covers "
        "auction_id and the path disagreed with it, so the auction whose terms judged this bid "
        f"is not the auction the seller signed for: {borrowed.text[:300]}"
    )
    assert not app.state.test_queue.items, (
        "a bid admitted on a borrowed path was queued for verification; the work item names "
        "the auction from the payload, so the record is of an auction whose terms never "
        "admitted it"
    )

    # -- witness two: the auction deadline -------------------------------------------------
    late = _payload(EXPIRED, nonce="cross-deadline-1")
    own_late = _submit(client, EXPIRED, late)
    assert own_late.status_code == 400, (
        f"the deadline witness is stale: a bid on an auction whose deadline passed answered "
        f"{own_late.status_code}"
    )
    assert "after_auction_deadline" in own_late.json()["reasons"], (
        f"the deadline witness did not refuse on the deadline: {own_late.json()['reasons']}"
    )

    borrowed_late = _submit(client, OPEN, late)
    assert borrowed_late.status_code == 400, (
        f"a bid signed for the CLOSED auction {EXPIRED} was answered "
        f"{borrowed_late.status_code} on the open auction {OPEN}'s path, so the auction-"
        "deadline gate — one of the six the E4 metric counts — is bypassed by aiming the URL "
        f"somewhere else: {borrowed_late.text[:300]}"
    )


def test_the_replay_memory_is_created_once_under_concurrent_first_requests() -> None:
    """The lazy nonce store must not be racy, or the replay gate has a hole at process start.

    ``NonceStore.consume`` is atomic and its docstring says why (T-234). This route reopened
    the same race ONE LEVEL ABOVE that lock: ``_nonce_store`` did an unguarded
    read-check-write on ``app.state``, so two first requests arriving together could each see
    no store, each build one, and each spend the same nonce in a DIFFERENT store. The lock
    below the hole cannot help; both threads hold a different lock.

    **This is invisible at the default switch interval, and that is the point rather than an
    excuse.** Measured on the door as first merged, 60 trials of 8 concurrent identical
    submissions against a fresh app: 60/60 admissions at the default interval — perfectly
    clean — and 79-80 admissions at ``sys.setswitchinterval(1e-6)``, with 17-19 trials
    admitting one nonce two or three times and queueing it as many. That is the same protocol
    and the same conclusion the ``NonceStore.consume`` docstring reaches about T-234: the
    5 ms GIL switch interval is "a scheduler accident, not a defence, and it goes live the
    moment there is a concurrent HTTP caller".

    The store is deliberately NOT pre-wired here: ``configure_external_bids(nonces=...)`` skips
    the lazy path entirely, so a gate that wired it would be a gate that never runs the code
    under test.
    """
    trials = 60
    workers = 8
    deadline = time.time() + 300
    admissions = 0
    duplicated_trials = 0

    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for trial in range(trials):
            app = _app(records={LOOSE: _Record(roster=_roster(50.0), deadline=deadline)})
            payload = _payload(LOOSE, nonce=f"race-{trial}")
            signature = sign_bid(payload, KEY)
            statuses: list[int] = []
            barrier = threading.Barrier(workers)

            def submit_once(barrier: threading.Barrier = barrier, app: Any = app) -> None:
                client = TestClient(app, raise_server_exceptions=False)
                barrier.wait()
                response = client.post(
                    f"/v1/auctions/{LOOSE}/bids",
                    json=payload,
                    headers={"X-ProxyShop-Signature": signature},
                )
                statuses.append(response.status_code)

            threads = [threading.Thread(target=submit_once) for _ in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            admitted = sum(1 for status in statuses if status == 202)
            admissions += admitted
            if admitted > 1:
                duplicated_trials += 1
            assert len(statuses) == workers, (
                f"trial {trial} recorded {len(statuses)} of {workers} responses; the race "
                "harness is not measuring what it claims to"
            )
    finally:
        sys.setswitchinterval(previous_interval)

    assert admissions == trials, (
        f"{admissions} admissions across {trials} trials of {workers} concurrent identical "
        f"submissions (expected exactly {trials} — one per nonce); {duplicated_trials} trial(s) "
        "spent one nonce more than once. The replay memory is built lazily by an unguarded "
        "read-check-write on app.state, so concurrent first requests each get their own store "
        "and the lock inside NonceStore guards nothing they share."
    )


def test_an_identifier_the_door_retains_is_bounded() -> None:
    """A nonce is retained for the auction's lifetime, so its length cannot be the caller's.

    The admitted path stores ``(signer_id, nonce)`` in a replay memory with no eviction, so
    every byte an anonymous submitter puts in either field is kept. Measured on the door as
    first merged: a 200 KB ``nonce`` was ADMITTED 202 and retained.

    The bound is not invented here. ``MAX_IDENTIFIER_LENGTH = 128`` is already what this
    package means by "an identifier a caller chose" — ``auction/routes.py:294`` applies it to
    ``store_id`` and ``product_ref``, and ``policy/routes.py:444`` imports that same constant
    rather than restating it. This door simply missed it.

    ``signer_id``, ``key_id`` and the path's ``auction_id`` are asserted alongside ``nonce``
    because all four are caller-chosen strings this route either retains or interpolates.
    """
    from exchange.auction.routes import MAX_IDENTIFIER_LENGTH

    deadline = time.time() + 300
    oversized = "n" * (200 * 1024)

    app = _app(records={LOOSE: _Record(roster=_roster(50.0), deadline=deadline)})
    client = TestClient(app, raise_server_exceptions=False)
    response = _submit(client, LOOSE, _payload(LOOSE, nonce=oversized))
    assert response.status_code == 400, (
        f"a {len(oversized)}-byte nonce was answered {response.status_code}. It is retained in "
        "an eviction-free replay memory, so its size is the caller's choice of how much of "
        f"this process to keep: {response.text[:200]}"
    )
    assert not app.state.test_queue.items, (
        "an unbounded nonce was queued for verification, so the oversized identifier is now in "
        "the durable record as well as in memory"
    )

    long_but_legal = "n" * MAX_IDENTIFIER_LENGTH
    app_ok = _app(records={LOOSE: _Record(roster=_roster(50.0), deadline=deadline)})
    ok = _submit(
        TestClient(app_ok, raise_server_exceptions=False),
        LOOSE,
        _payload(LOOSE, nonce=long_but_legal),
    )
    assert ok.status_code == 202, (
        f"a nonce of exactly MAX_IDENTIFIER_LENGTH ({MAX_IDENTIFIER_LENGTH}) was refused "
        f"{ok.status_code}; the bound is off by one or is stricter than the package's own: "
        f"{ok.text[:200]}"
    )

    for field in ("signer_id", "key_id"):
        app_field = _app(records={LOOSE: _Record(roster=_roster(50.0), deadline=deadline)})
        refused = _submit(
            TestClient(app_field, raise_server_exceptions=False),
            LOOSE,
            _payload(LOOSE, nonce=f"bounded-{field}", **{field: "x" * (200 * 1024)}),
        )
        assert refused.status_code == 400, (
            f"a 200 KB {field} was answered {refused.status_code}; it is carried into the "
            "refusal record and the queued work item"
        )

    # 2000 rather than 200 KB: httpx refuses to SEND a 200 KB URL (`InvalidURL: URL too long`),
    # so the oversized-path case has to be a length a real client can actually put on the wire
    # and a real server would forward. 2000 is far past the 128-char ceiling and far inside the
    # ~8 KB request-line limit nginx and uvicorn impose.
    huge_path = "a" * 2000
    app_path = _app(records={LOOSE: _Record(roster=_roster(50.0), deadline=deadline)})
    on_path = _submit(
        TestClient(app_path, raise_server_exceptions=False), huge_path, _payload(LOOSE, nonce="p-1")
    )
    assert on_path.status_code == 400, (
        f"a {len(huge_path)}-character auction_id in the URL was answered {on_path.status_code}"
    )


def test_an_oversized_body_is_refused_without_being_buffered() -> None:
    """ "Refused unread" has to mean unread, or the cap bounds the refusal and not the memory.

    The door's own comment claims a body over the ceiling is "refused unread", but the check
    ran AFTER ``await request.body()`` — which reads whatever arrives before anything can
    object. Measured on the door as first merged: an 8 MiB body was fully buffered and then
    refused, so the ceiling cost the caller nothing and cost the process everything.

    The twin this module names, ``policy/routes.py``'s ``_bounded_body``, streams and counts,
    and its docstring is where the distinction is written down. This asserts the MECHANISM
    rather than the status code: a buffering implementation must be red even though it answers
    400 exactly like a streaming one.

    **Driven as a raw ASGI call, not through ``TestClient``, and that is forced rather than
    fussy.** ``starlette/testclient.py:307`` does ``body = request.read()`` — the test client
    buffers the whole request before it ever invokes the app — so a generator handed to
    ``client.post`` is drained by the CLIENT and counting its chunks measures httpx, not the
    door. The count that means something is how many ``http.request`` messages the
    application pulls from ``receive``, which is what a real server would still be holding.
    """
    import asyncio  # noqa: PLC0415 - only this node drives the app at the ASGI boundary

    from exchange.external_bids.routes import MAX_SUBMISSION_BYTES

    chunk = b"x" * (64 * 1024)
    total_chunks = (8 * 1024 * 1024) // len(chunk)
    app = _app(records={LOOSE: _Record(roster=_roster(50.0), deadline=time.time() + 300)})

    async def drive() -> tuple[int, list[dict[str, Any]]]:
        pulled = 0
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            nonlocal pulled
            if pulled >= total_chunks:
                return {"type": "http.disconnect"}
            pulled += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": pulled < total_chunks,
            }

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"/v1/auctions/{LOOSE}/bids",
            "raw_path": f"/v1/auctions/{LOOSE}/bids".encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"transfer-encoding", b"chunked"),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        await app(scope, receive, send)
        return pulled, sent

    pulled, sent = asyncio.run(drive())

    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert statuses == [400], (
        f"an {total_chunks * len(chunk)}-byte body was answered {statuses}, not [400]"
    )
    ceiling_chunks = (MAX_SUBMISSION_BYTES // len(chunk)) + 2
    assert pulled <= ceiling_chunks, (
        f"the app pulled {pulled} of {total_chunks} chunks "
        f"({pulled * len(chunk)} bytes) before refusing a body whose ceiling is "
        f"{MAX_SUBMISSION_BYTES} bytes. It is buffering the whole body and rejecting it "
        "afterwards, which bounds the refusal and not the memory — the thing "
        "`policy/routes.py:_bounded_body` exists to avoid."
    )


@pytest.mark.parametrize("placement", ["keyring", "signing_key", "secret"])
def test_a_caller_supplied_signing_key_is_never_honoured(placement: str) -> None:
    """Regression cover for the property the T-244 review probed by hand.

    The key a submission is verified against is deployment state. A body that carries its own
    is verified against the CONFIGURED table anyway, so a caller who signs with a key they
    invented is refused ``signature_invalid`` rather than admitted.
    """
    app = _app(records={LOOSE: _Record(roster=_roster(50.0), deadline=time.time() + 300)})
    client = TestClient(app, raise_server_exceptions=False)
    attacker = "attacker-chosen-key"
    payload = _payload(LOOSE, nonce=f"forged-{placement}")

    response = client.post(
        f"/v1/auctions/{LOOSE}/bids",
        json={
            "payload": payload,
            "signature": sign_bid(payload, attacker),
            placement: {STORE: {KEY_ID: attacker}},
        },
    )

    assert response.status_code == 400, (
        f"a submission carrying its own signing key at {placement!r} was answered "
        f"{response.status_code}; the door verified against a key the caller chose"
    )
    assert "signature_invalid" in response.json()["reasons"], (
        f"expected signature_invalid, got {response.json()['reasons']}"
    )


def test_no_body_this_door_accepts_can_make_it_answer_5xx() -> None:
    """T-270's property applies to the door T-244 built, and it was violated there.

    ``_submission_of`` reads the body by hand — which is what keeps a pydantic request model,
    and therefore T-270's echoing 422 renderer, off this route — and in doing so it opted out
    of the blanket body-parse guard FastAPI gives every route that DOES declare a model. It
    then caught only ``(UnicodeDecodeError, ValueError)``. ``json.loads`` on a deeply nested
    document raises ``RecursionError``, which is a ``RuntimeError`` and not a ``ValueError``,
    so it escaped the ASGI app entirely.

    Measured on the door as merged at 696850d, against a bare ``create_app()`` with NO
    signature, NO keyring and no wiring of any kind — reachable in every deployment::

        POST /v1/auctions/a/bids   b"[" * 9994 + b"]" * 9994   ->  500
        RecursionError: maximum recursion depth exceeded while decoding a JSON array

    19,988 bytes is 7.6% of this door's 256 KiB ceiling, so the size cap never fires; 9,994 was
    the smallest depth that did it. Nested objects do it too. Every sibling route on the same
    app answers 400 to the identical body, because they kept the guard this one hand-rolled
    past.

    The twin this module's docstring names has the missing clause and has had it all along —
    ``policy/routes.py``'s ``_json_object`` catches ``RecursionError`` and refuses with "the
    body nests deeper than this door will parse". ``_submission_of`` copied that function's
    ``parse_constant`` half and dropped its ``RecursionError`` half.

    The T-270 gate cannot see this: its corpus builds ``POST /auctions`` bodies only, plants
    non-finite literals, and has no nesting case — so it is armed, green, and blind to this
    route. That is why the property is re-asserted here, against THIS door, rather than
    assumed to be covered.
    """
    client = TestClient(create_app(), raise_server_exceptions=False)

    for label, raw in (
        ("nested arrays", b"[" * 50000 + b"]" * 50000),
        ("nested objects", b'{"a":' * 50000 + b"1" + b"}" * 50000),
        ("the measured 9994-deep witness", b"[" * 9994 + b"]" * 9994),
    ):
        response = client.post(
            "/v1/auctions/a/bids", content=raw, headers={"content-type": "application/json"}
        )
        assert response.status_code < 500, (
            f"{label} ({len(raw)} bytes, well inside the {MAX_BODY} ceiling) made an "
            f"UNAUTHENTICATED request answer {response.status_code}. Nothing a caller can "
            "write may produce a 5xx, and this door reads its own body, so the guard is its "
            f"own to keep: {response.text[:200]}"
        )
        assert response.status_code == 400, (
            f"{label} answered {response.status_code}; the published refusal for a body this "
            "door will not parse is a 400 BidValidationResult"
        )


def test_a_client_that_disconnects_mid_upload_does_not_500() -> None:
    """A caller hanging up while the body is still arriving is a fact of the internet, not a bug.

    ``request.stream()`` raises ``starlette.requests.ClientDisconnect`` when the server
    delivers ``http.disconnect``, and ``_bounded_body`` was wrapped only in
    ``except _UnreadableBody``. Measured at the ASGI boundary on the door as merged at
    696850d, sending one partial ``http.request`` and then ``http.disconnect``::

        /v1/auctions/{id}/bids   -> 500, ClientDisconnect escaped the app
        /auctions                -> 400
        /auctions/{id}/accept    -> 400
        /internal/outcomes       -> 503

    Same root cause as the recursion gate above: reading the body by hand dropped FastAPI's
    guard and did not replace it. This is asserted at the ASGI boundary because that is the
    only place a disconnect can be delivered — ``TestClient`` has no way to express one.
    """
    import asyncio  # noqa: PLC0415 - only the ASGI-boundary nodes need it

    app = _app(records={LOOSE: _Record(roster=_roster(50.0), deadline=time.time() + 300)})

    async def drive() -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"type": "http.request", "body": b'{"a":', "more_body": True},
            {"type": "http.disconnect"},
        ]
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": f"/v1/auctions/{LOOSE}/bids",
                "raw_path": f"/v1/auctions/{LOOSE}/bids".encode(),
                "root_path": "",
                "query_string": b"",
                "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
            },
            receive,
            send,
        )
        return sent

    sent = asyncio.run(drive())
    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert statuses and statuses[0] < 500, (
        f"a mid-upload disconnect answered {statuses}; ClientDisconnect escaped the route "
        "instead of being refused the way every sibling route on this app refuses it"
    )


def test_an_overflowing_exponent_is_refused_on_the_way_in() -> None:
    """``1e400`` is legal JSON and becomes ``inf``; this door claimed to refuse it and did not.

    The module docstring says the bytes are parsed "with the non-finite constants refused on
    the way IN, exactly as ``policy/routes.py`` does". Measured on the door as merged at
    696850d, that was true of the LITERALS and false of the exponents::

        exchange.external_bids.routes._submission_of(b'{"a":1e400}')  -> {'a': inf}
        exchange.policy.routes._json_object(b'{"a":1e400}')           -> refused, not finite

    ``parse_constant`` fires only on the bare tokens ``NaN``/``Infinity``/``-Infinity``.
    ``1e400`` is a perfectly ordinary RFC-8259 number that overflows to ``inf`` during
    conversion, and catching it needs ``parse_float``, which the twin has and this door did
    not. Nothing downstream was admitting an ``inf`` at the time — ``canonical_signing_bytes``
    refused every placement one layer down — so this was a documented-vs-actual mismatch on
    precisely the T-270 property rather than a live 500, and it is gated so it cannot become
    one when the canonicalizer changes.
    """
    from exchange.external_bids.routes import _submission_of, _UnreadableBody

    for raw in (b'{"a":1e400}', b'{"a":-1e400}', b'{"a":[1e999]}', b'{"a":{"b":2.5e400}}'):
        with pytest.raises(_UnreadableBody):
            _submission_of(raw)

    # And the finite neighbours must still parse, or the refusal is just a broken parser.
    assert _submission_of(b'{"a":1e308}') == {"a": 1e308}
    assert _submission_of(b'{"a":0.5,"b":-3}') == {"a": 0.5, "b": -3}


# =====================================================================================
# Gaps an adversarial review found in the gates ABOVE, not in the route.
#
# Every node below was added because a mutation of shipped production code left the whole
# file green. They are additions: no assertion above was changed, because none of them was
# wrong — they were narrow, and a narrow gate that reads as a broad one is how a defect gets
# reintroduced under a green suite.
# =====================================================================================


def test_an_oversized_store_id_is_bounded_and_not_echoed() -> None:
    """`store_id` is bounded, and nothing above noticed when the bound was removed.

    Deleting ``"store_id"`` from ``BOUNDED_IDENTIFIERS`` — a bound this door added
    deliberately — left all eleven earlier nodes GREEN, because none of them ever submitted
    an oversized one.

    The harm is real and the status code never changes, so no status-only assertion could
    catch it. `store_id` is interpolated verbatim into ``trust_snapshot_unavailable:<store_id>``
    and published in ``reasons``. Measured with the bound removed and `signer_id` held short so
    the surviving bounds could not mask it: a 204,800-character `store_id` produced a 400 whose
    body was **204,932 bytes**, with ``reasons[0]`` 204,827 characters long. With the bound in
    place the same request is a 400 of 159 bytes.

    So this asserts the RESPONSE SIZE as well as the reason code — the refusal must not carry
    the thing it is refusing.
    """
    app = _app(records={LOOSE: _Record(roster=_roster(50.0), deadline=time.time() + 300)})
    client = TestClient(app, raise_server_exceptions=False)

    payload = _payload(LOOSE, nonce="store-1", store_id="s" * 204800)
    response = _submit(client, LOOSE, payload)

    assert response.status_code == 400, f"answered {response.status_code}"
    assert response.json()["reasons"] == [
        "malformed_submission:identifier_exceeds_maximum_length"
    ], f"refused for the wrong reason: {response.json()['reasons']}"
    assert len(response.content) < 1024, (
        f"the refusal is {len(response.content)} bytes; it is echoing the oversized store_id "
        "back rather than refusing it, which is the harm the bound exists to prevent"
    )


def test_configuring_the_replay_memory_survives_a_concurrent_first_request() -> None:
    """The lock on `configure_external_bids` is real, and nothing above graded it.

    `_nonce_store` takes ``_NONCE_STORE_LOCK`` around its lazy creation, and
    `configure_external_bids` was changed to write under the SAME lock. Removing that `with`
    left all eleven earlier nodes green: the concurrency node races first REQUESTS against
    each other and never races a request against the operator wiring the store, which is the
    only window this lock closes.

    Driven deterministically rather than by timing luck: a request is parked inside
    ``NonceStore.__init__`` (so it is mid-creation, holding the lock when the fix is present),
    the operator's ``configure_external_bids`` call is made from another thread, and only then
    is the parked request released. Whoever ends up in ``app.state`` is then a fact about the
    lock, not about the scheduler.

    With the lock, the configure call waits and its store wins. Without it, the configure call
    lands in the check-then-write window and the lazily-created store overwrites the
    operator's, which is dropped on the floor — no replay hole in this ordering, but a
    deployment that wired a durable replay memory silently does not have one.
    """
    import store_agent.external.nonces as nonces_module  # noqa: PLC0415
    from exchange.external_bids.routes import configure_external_bids  # noqa: PLC0415

    app = _app(records={LOOSE: _Record(roster=_roster(50.0), deadline=time.time() + 300)})
    assert getattr(app.state, "external_bid_nonces", None) is None, (
        "this node needs the LAZY path; the app already has a replay memory"
    )

    operator_store = nonces_module.NonceStore()  # built before the patch below
    entered = threading.Event()
    release = threading.Event()
    real_init = nonces_module.NonceStore.__init__

    def parked_init(self: Any) -> None:
        real_init(self)
        entered.set()
        release.wait(10)

    nonces_module.NonceStore.__init__ = parked_init  # type: ignore[method-assign]
    try:
        payload = _payload(LOOSE, nonce="cfg-race-1")
        signature = sign_bid(payload, KEY)

        def submit() -> None:
            TestClient(app, raise_server_exceptions=False).post(
                f"/v1/auctions/{LOOSE}/bids",
                json=payload,
                headers={"X-ProxyShop-Signature": signature},
            )

        requester = threading.Thread(target=submit)
        requester.start()
        assert entered.wait(10), "the request never reached NonceStore.__init__"

        configurer = threading.Thread(
            target=lambda: configure_external_bids(app, nonces=operator_store)
        )
        configurer.start()
        time.sleep(0.05)  # let it reach (and, with the fix, block on) the lock
        release.set()
        configurer.join(10)
        requester.join(10)
    finally:
        nonces_module.NonceStore.__init__ = real_init  # type: ignore[method-assign]
        release.set()

    assert app.state.external_bid_nonces is operator_store, (
        "the operator's replay memory was discarded by a concurrent first request: "
        "configure_external_bids wrote app.state outside the lock _nonce_store takes, so the "
        "lazily-created store overwrote it"
    )


def test_a_body_that_is_json_but_not_an_object_is_refused_not_500() -> None:
    """The 5xx node above is named for a universal property and graded on one family.

    ``test_no_body_this_door_accepts_can_make_it_answer_5xx`` carries three witnesses and all
    three are deep nesting, so it proves the ``RecursionError`` clause and nothing else.
    Measured: removing the ``if not isinstance(parsed, dict)`` refusal from ``_submission_of``
    produces LIVE 500s on ``[1,2,3]``, ``"hello"`` and ``42`` — ``_payload_and_signature``
    calls ``.get`` on a list, a str, an int — and every one of the eleven nodes stayed green,
    because each witness dies in ``RecursionError`` long before the ``isinstance`` check.

    A JSON document that is not an object is the other way an anonymous body reaches that
    line, so it gets its own witnesses here.
    """
    client = TestClient(create_app(), raise_server_exceptions=False)

    for raw in (b"[1,2,3]", b'"hello"', b"42", b"true", b"null", b"[]", b"[[1]]", b"-0.5"):
        response = client.post(
            "/v1/auctions/a/bids", content=raw, headers={"content-type": "application/json"}
        )
        assert response.status_code == 400, (
            f"body {raw!r} answered {response.status_code}; a JSON document that is not an "
            "object must be refused, not handed to code that assumes a mapping: "
            f"{response.text[:200]}"
        )
        assert response.json()["reasons"] == ["malformed_submission:body_is_not_a_json_object"], (
            f"body {raw!r} refused for the wrong reason: {response.json()['reasons']}"
        )


def test_an_oversized_path_auction_id_is_refused_for_being_oversized() -> None:
    """The existing path-length witness passes for the wrong reason.

    ``test_an_identifier_the_door_retains_is_bounded`` posts a 2000-character path with a
    payload signed for ``LOOSE``, so ``_reconciled`` answers ``_PATH_MISMATCH`` first and the
    node's ``status_code == 400`` holds whether or not the length bound exists. Measured:
    deleting ``if len(path_auction_id) > ceiling`` leaves all eleven nodes green.

    Here the signed ``auction_id`` IS the oversized path, so reconciliation passes and the
    only thing left that can refuse it is the length — and the reason code is asserted, not
    just the status.
    """
    app = _app(records={LOOSE: _Record(roster=_roster(50.0), deadline=time.time() + 300)})
    client = TestClient(app, raise_server_exceptions=False)

    huge = "a" * 2000
    response = _submit(client, huge, _payload(huge, nonce="path-len-1"))

    assert response.status_code == 400, f"answered {response.status_code}"
    assert response.json()["reasons"] == [
        "malformed_submission:identifier_exceeds_maximum_length"
    ], (
        "an oversized path auction_id that MATCHES the signed one was refused for some other "
        f"reason, so the length bound is not what stopped it: {response.json()['reasons']}"
    )


def test_the_bare_non_finite_tokens_are_refused() -> None:
    """``parse_constant`` has no witness above; only ``parse_float`` does.

    ``test_an_overflowing_exponent_is_refused_on_the_way_in`` uses ``1e400`` and friends, all
    of which are caught by ``parse_float``. Measured: deleting
    ``parse_constant=_refuse_constant`` leaves all eleven nodes green. The bare literals are a
    different code path in ``json`` and this door refuses both, so both are graded.
    """
    from exchange.external_bids.routes import _submission_of, _UnreadableBody  # noqa: PLC0415

    for raw in (
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":-Infinity}',
        b'{"a":[NaN]}',
        b'{"a":{"b":Infinity}}',
    ):
        with pytest.raises(_UnreadableBody):
            _submission_of(raw)


def test_the_submission_ceiling_is_the_value_this_door_documents() -> None:
    """The streaming node cannot detect a WRONG ceiling, only a wrong mechanism.

    ``test_an_oversized_body_is_refused_without_being_buffered`` derives its tolerance from
    ``MAX_SUBMISSION_BYTES`` itself, so the constant and the assertion move together.
    Measured: setting the ceiling to 256 MiB leaves that node green while the app buffers the
    full 8 MiB body — ``pulled = 128 of 128``, both of its assertions satisfied.

    Pinning the value is the missing half. 256 KiB is the documented bound: a signed bid is a
    few kilobytes, and the door snapshots whatever it is handed.
    """
    from exchange.external_bids.routes import MAX_SUBMISSION_BYTES  # noqa: PLC0415

    assert MAX_SUBMISSION_BYTES == 256 * 1024, (
        f"the ceiling is {MAX_SUBMISSION_BYTES}; the streaming gate's tolerance is derived "
        "from this constant, so a change here silently widens that gate too"
    )


def test_a_lone_surrogate_cannot_reach_the_response_renderer() -> None:
    """Starlette cannot encode a lone surrogate, and caller text reaches its renderer.

    ``JSONResponse.render`` does ``ensure_ascii=False`` then ``.encode("utf-8")``, and a lone
    surrogate raises ``UnicodeEncodeError`` there — an unauthenticated 500 in the response
    renderer, the T-270 shape, reached by echoing a caller's own value. A caller writes one as
    an ordinary ``\\uXXXX`` escape and ``json.loads`` accepts it into a normal ``str``.

    This was NOT live: ``canonical_signing_bytes`` refuses a lone surrogate in any value and
    any key one layer down. It is closed anyway and graded here, because "safe because
    something in another package refuses it first" is the reasoning this door has already had
    to retract twice — once for the overflowing exponent, once for the recursion limit.
    """
    from exchange.external_bids.routes import _rejected, _renderable  # noqa: PLC0415

    assert _renderable("x\ud800y") == "x?y"
    assert _renderable("store-1") == "store-1"

    # The refusal path must be encodable even when the reason quotes caller-chosen text.
    body = _rejected(["schema_invalid:claims.0.k\ud800"]).body
    assert b"\\ud800" not in body and body, "the refusal body still carries a lone surrogate"

    app = _app(records={LOOSE: _Record(roster=_roster(50.0), deadline=time.time() + 300)})
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"/v1/auctions/{LOOSE}/bids",
        content=b'{"auction_id":"' + LOOSE.encode() + b'","store_id":"\\ud800"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code < 500, (
        f"a lone surrogate in the body answered {response.status_code}"
    )


def test_two_stores_rostered_on_one_product_do_not_share_a_price_wall() -> None:
    """An honest seller must not be refused by ANOTHER store's declared depth.

    ``_auction_terms`` keys ``list_prices`` by ``product_ref``, and a roster is a list of
    stores asked about the same product — so before this gate the last row on the roster
    silently decided the price wall for every submitter. Red on the door as merged, and found
    by driving the real route rather than by reading it: the composition root's own two-store
    roster (``s1`` at 100.00, ``s2`` at 120.00, both ``prod-1``, 20% of depth) refused ``s1``'s
    correctly signed 88.00 with ``price_under_declared_depth:offer.unit_price``, because 88.00
    is under ``s2``'s floor of 96.00.

    Both directions are asserted, because a filter that simply found nothing would also make
    the first assertion pass: the submitting store's OWN wall must still refuse a bid under
    its own floor.
    """
    future = time.time() + 300
    roster = [
        {"store_id": STORE, "product_ref": PRODUCT, "list_price": 100.0, "max_discount_pct": 20.0},
        {
            "store_id": "store-external-2",
            "product_ref": PRODUCT,
            "list_price": 120.0,
            "max_discount_pct": 20.0,
        },
    ]
    app = _app(records={OPEN: _Record(roster=roster, deadline=future)})
    client = TestClient(app, raise_server_exceptions=False)

    admitted = _submit(client, OPEN, _payload(OPEN, nonce="two-store-1", unit_price=88.0))
    assert admitted.status_code == 202, (
        f"a bid inside its OWN store's declared depth (100.00 less 20%) was refused "
        f"{admitted.status_code}: {admitted.text[:300]}"
    )
    assert len(app.state.test_queue.items) == 1, app.state.test_queue.items

    # The positive control: this store's own wall is still enforced, so the filter did not
    # simply switch the price gate off.
    refused = _submit(client, OPEN, _payload(OPEN, nonce="two-store-2", unit_price=50.0))
    assert refused.status_code == 400, refused.text[:300]
    assert any("price" in reason for reason in refused.json()["reasons"]), refused.json()
    assert len(app.state.test_queue.items) == 1, "a refused submission was queued"


def test_the_door_runs_the_composition_root_and_a_broken_keyring_is_a_503(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """This route takes the deployment hook its three siblings take, and answers 503 like them.

    Red before R8: ``submit_external_bid`` read ``app.state`` for its keyring, its queue and
    its auction machine and never ran the composition root that binds them, so an exchange
    whose first request was a bid submission was configured by nobody — and a deployment
    naming an unreadable keyring got a silent ``unknown_signing_key`` for every seller instead
    of being told its file was wrong.

    A 503 rather than a 400 because "each rejection is final": a 400 from this door tells an
    honest submitter their bid is permanently refused, and an operator's broken file is not a
    fact about their bid.
    """
    import json as _json

    document = {
        "sellers": [{"store_id": STORE, "eligibility": "eligible"}],
        "external_bid_keyring_file": str(tmp_path / "never-mounted.json"),
    }
    monkeypatch.setenv("EXCHANGE_DEPLOYMENT_JSON", _json.dumps(document))
    monkeypatch.delenv("EXCHANGE_DEPLOYMENT", raising=False)

    app = create_app()
    configure_ranking(
        app, trust_snapshot={STORE: {"store_id": STORE, "score": 0.9, "blacklisted": False}}
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = _submit(client, OPEN, _payload(OPEN, nonce="hook-1"))

    assert response.status_code == 503, (
        f"the door did not run the composition root, or answered a misconfigured deployment "
        f"as a rejection: {response.status_code} {response.text[:300]}"
    )
    detail = response.json()["detail"]
    assert "never-mounted.json" in detail and "could not be read" in detail, detail


def test_a_full_verification_queue_refuses_rather_than_displacing_an_admitted_bid() -> None:
    """The backlog has a ceiling, and reaching it costs the exchange nothing it already took.

    The obvious bound — ``LTRIM`` to the last N — silently drops the OLDEST work items, which
    are submissions whose nonce is already spent and which therefore cannot be resubmitted
    byte-identically: the exchange would have admitted a bid, told the seller 202, and then
    thrown the only record of it away. So the queue refuses instead, the door answers
    ``verification_queue_unavailable``, and what was already admitted stays.

    Driven through the real route against a real Redis, with a ceiling of one, because the
    property is about what the datastore holds after the second submission.
    """
    from exchange.external_bids.verification_queue import RedisVerificationQueue

    from proxyshop_support.redis_client import worker_redis

    key = "exchange:external-bid-verification:test-full"
    redis = worker_redis()
    redis.delete(key)
    queue = RedisVerificationQueue(key, max_items=1)
    future = time.time() + 300
    app = _app(records={OPEN: _Record(roster=_roster(50.0), deadline=future)})
    configure_external_bids(app, queue=queue)
    client = TestClient(app, raise_server_exceptions=False)

    try:
        first = _submit(client, OPEN, _payload(OPEN, nonce="full-1"))
        assert first.status_code == 202, first.text[:300]
        assert queue.depth() == 1, queue.depth()

        second = _submit(client, OPEN, _payload(OPEN, nonce="full-2"))
        assert second.status_code == 400, second.text[:300]
        assert "verification_queue_unavailable" in second.json()["reasons"], second.json()
        assert queue.depth() == 1, "the ceiling displaced a work item that was already admitted"
        assert json.loads(redis.lrange(key, 0, -1)[0])["nonce"] == "full-1", (
            "the retained item is not the one that was admitted first"
        )
    finally:
        redis.delete(key)


# =====================================================================================
# The queue's CONSUMER — R8/R18, and it did not exist
# =====================================================================================
#
# Measured on this tree before this section: no ``LPOP``/``BRPOP`` call site anywhere in the
# repository, no ``[project.scripts]`` in any member ``pyproject.toml``, no lifespan or startup
# hook in ``exchange.main``, and one process in the exchange image (``uvicorn
# exchange.main:app``). So an admitted Tier-2 bid queued durably and NOTHING performed R8's
# "routed to claim extraction + verification"; at :data:`MAX_QUEUED_WORK_ITEMS` the whole
# external channel starts refusing correctly signed bids.
#
# A background drainer cannot exist in that process model, and a NEW served route cannot be the
# answer either: ``test_repro_open_tickets.py::test_t312_the_exchange_serves_exactly_the_
# operations_its_contract_publishes`` requires the served surface and
# ``packages/contracts/openapi/exchange.openapi.json`` to agree in BOTH directions, so a new
# operation is a contracts change. What is left, and what these gates drive, is the one served
# operation the exchange already publishes for this channel: the door itself drains a bounded
# batch on the request path, after an ADMISSION (so the work is not anonymously triggerable),
# and every verdict it reaches is announced to the exchange's ledger as ``claim_verified``.

DRAIN_STORE = STORE
DRAIN_PRODUCT = PRODUCT

#: A pitch and its lie, differing in one word. What the door has to do with them is decompose,
#: grade against the exchange's OWN snapshot, and record the answer.
DRAIN_HONEST_PITCH = "A heat exchange boiler with a 9 bar pump and a two-year warranty."
DRAIN_LIAR_PITCH = "A heat exchange boiler with a 9 bar pump and a five-year warranty."

#: The routing that lets the exchange announce a verdict at all. ``claim_dimensions_of``
#: defaults to ``None`` and announces nothing, deliberately (the table is human-approved ground
#: truth the exchange's image does not ship), so a deployment that has not wired it drains
#: NOTHING rather than consuming work items whose verdicts have nowhere to go.
DRAIN_DIMENSIONS = {
    "warranty": "catalog_claim_accuracy",
    "specifications": "catalog_claim_accuracy",
}


class _DrainableQueue:
    """An in-memory stand-in for :class:`RedisVerificationQueue`, list semantics and all."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    def enqueue(self, item: Any) -> None:
        self.items.append(item)

    def pop(self) -> Any | None:
        return self.items.pop(0) if self.items else None

    def depth(self) -> int:
        return len(self.items)


def _drain_snapshot(store_id: str) -> dict[str, Any]:
    return {
        "snapshot_id": f"snap-{store_id}",
        "captured_at": "2026-01-01T00:00:00Z",
        "store_id": store_id,
        "products": [
            {
                "product_ref": DRAIN_PRODUCT,
                "canonical_name": DRAIN_PRODUCT,
                "evidence_ref": f"snap-{store_id}#{DRAIN_PRODUCT}",
                "attributes": {
                    "boiler_type": {"value": "heat exchange"},
                    "pump_pressure_bar": {"value": 9},
                    "warranty_months": {"value": 24},
                },
            }
        ],
    }


def _drain_app(*, wire_catalog: bool = True, wire_dimensions: bool = True) -> Any:
    """The door, wired the way a deployment that can actually check a claim is wired."""
    from exchange.auction.ledger import InMemoryLedgerSink, LedgerRecorder
    from exchange.ranking.verification import StaticCatalogSnapshots

    future = time.time() + 300
    app = _app(records={OPEN: _Record(roster=_roster(50.0), deadline=future)})
    queue = _DrainableQueue()
    configure_external_bids(app, queue=queue)
    from store_agent.external.nonces import NonceStore  # noqa: PLC0415

    configure_external_bids(app, nonces=NonceStore())
    configure_ranking(
        app,
        catalog=(
            StaticCatalogSnapshots({DRAIN_STORE: _drain_snapshot(DRAIN_STORE)})
            if wire_catalog
            else None
        ),
        claim_dimensions=DRAIN_DIMENSIONS if wire_dimensions else None,
    )
    sink = InMemoryLedgerSink()
    app.state.auction_machine.ledger = LedgerRecorder(sink)
    app.state.test_queue = queue
    app.state.test_sink = sink
    return app


def _claim_verdicts(sink: Any) -> list[tuple[str, str, str]]:
    return [
        (
            str(event["store_id"]),
            str(event["payload"]["claim_type"]),
            str(event["payload"]["status"]),
        )
        for event in sink.events
        if event.get("kind") == "claim_verified"
    ]


def test_an_admitted_external_bid_is_verified_inside_the_request_that_admitted_it() -> None:
    """R8's "routed to claim extraction + verification", performed rather than promised.

    RED before this change on two counts: the queue had no ``pop`` and the door had no drain,
    so ``queue.depth()`` only ever grew and no ``claim_verified`` event was ever produced from
    this channel. The pitch text is the witness — ``warranty_months`` appears in NO claim the
    seller submitted; the exchange read it out of the prose and graded it against its own
    catalogue.
    """
    app = _drain_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = _submit(
        client,
        OPEN,
        _payload(OPEN, nonce="drain-1", message=DRAIN_HONEST_PITCH),
    )
    assert response.status_code == 202, response.text[:400]

    verdicts = _claim_verdicts(app.state.test_sink)
    assert ("store-external-1", "warranty", "verified") in verdicts, verdicts
    assert app.state.test_queue.depth() == 0, "the work item was queued and never consumed"


def test_a_lie_in_an_admitted_pitch_is_recorded_as_contradicted() -> None:
    """The same request, the same store, one word changed in the prose."""
    app = _drain_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = _submit(client, OPEN, _payload(OPEN, nonce="drain-2", message=DRAIN_LIAR_PITCH))
    assert response.status_code == 202, response.text[:400]

    verdicts = _claim_verdicts(app.state.test_sink)
    assert ("store-external-1", "warranty", "contradicted") in verdicts, verdicts
    # Charged for the sentence it lied in, not for its pitch.
    assert ("store-external-1", "specifications", "verified") in verdicts, verdicts


def test_nothing_is_drained_when_a_verdict_would_have_nowhere_to_go() -> None:
    """A drain that cannot RECORD is not a drain, it is deletion.

    ``claim_dimensions_of`` defaults to ``None`` — the exchange announces nothing until a
    deployment hands it the approved routing — so a deployment in that state must leave the
    backlog alone rather than consume admitted bids into silence.
    """
    app = _drain_app(wire_dimensions=False)
    client = TestClient(app, raise_server_exceptions=False)

    response = _submit(client, OPEN, _payload(OPEN, nonce="drain-3", message=DRAIN_HONEST_PITCH))
    assert response.status_code == 202, response.text[:400]
    assert _claim_verdicts(app.state.test_sink) == []
    assert app.state.test_queue.depth() == 1, "an unrecordable verdict consumed the work item"


def test_the_drain_is_bounded_and_a_backlog_shrinks_rather_than_grows() -> None:
    """One request adds one item and consumes up to :data:`DRAIN_BATCH_SIZE` of them."""
    from exchange.external_bids.draining import DRAIN_BATCH_SIZE

    app = _drain_app()
    queue = app.state.test_queue
    client = TestClient(app, raise_server_exceptions=False)

    backlog = 3 * DRAIN_BATCH_SIZE
    for n in range(backlog):
        queue.enqueue(
            {
                "kind": "external_bid_verification",
                "auction_id": OPEN,
                "store_id": DRAIN_STORE,
                "nonce": f"old-{n}",
                "submission": {
                    "store_id": DRAIN_STORE,
                    "offer": {"product_ref": DRAIN_PRODUCT},
                    "claims": [],
                    "message": DRAIN_HONEST_PITCH,
                },
            }
        )

    before = queue.depth()
    response = _submit(client, OPEN, _payload(OPEN, nonce="drain-4", message=DRAIN_HONEST_PITCH))
    assert response.status_code == 202, response.text[:400]
    after = queue.depth()

    assert after < before, "the backlog did not shrink"
    # +1 for the submission this request admitted, -DRAIN_BATCH_SIZE for what it consumed.
    assert after == before + 1 - DRAIN_BATCH_SIZE, (before, after, DRAIN_BATCH_SIZE)
    assert DRAIN_BATCH_SIZE > 1, "a batch of one can never overtake its own producer"


def test_a_queue_that_cannot_be_read_never_fails_the_submission() -> None:
    """The audit trail must not be able to refuse a bid the door already admitted."""

    class _Hostile(_DrainableQueue):
        def pop(self) -> Any:
            raise RuntimeError("redis is down")

    app = _drain_app()
    hostile = _Hostile()
    configure_external_bids(app, queue=hostile)
    app.state.test_queue = hostile
    client = TestClient(app, raise_server_exceptions=False)

    response = _submit(client, OPEN, _payload(OPEN, nonce="drain-5", message=DRAIN_HONEST_PITCH))
    assert response.status_code == 202, response.text[:400]
    assert response.json()["accepted"] is True


def test_a_refused_submission_drains_nothing() -> None:
    """The drain is work, so it hangs off an ADMISSION rather than off a request.

    A door that drained before judging would let an anonymous caller with a junk signature
    spend the exchange's CPU on the backlog, once per request, for free.
    """
    app = _drain_app()
    queue = app.state.test_queue
    queue.enqueue(
        {
            "kind": "external_bid_verification",
            "auction_id": OPEN,
            "store_id": DRAIN_STORE,
            "nonce": "old-refused",
            "submission": {
                "store_id": DRAIN_STORE,
                "offer": {"product_ref": DRAIN_PRODUCT},
                "claims": [],
                "message": DRAIN_HONEST_PITCH,
            },
        }
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        f"/v1/auctions/{OPEN}/bids",
        json=_payload(OPEN, nonce="drain-6"),
        headers={"X-ProxyShop-Signature": "00" * 32},
    )
    assert response.status_code == 400, response.text[:300]
    assert queue.depth() == 1, "a refused submission drained the backlog"
    assert _claim_verdicts(app.state.test_sink) == []


def test_the_door_drains_a_real_redis_queue_and_verifies_what_it_pops() -> None:
    """The whole channel over the real datastore: ``RPUSH`` in, ``LPOP`` out, verdicts recorded.

    Every other gate in this section drives an in-memory stand-in, which grades the drain's
    LOGIC and cannot grade the thing that was actually missing: there was no ``LPOP`` call site
    anywhere in this repository, so ``RedisVerificationQueue`` was a producer with no consumer
    and the class that would have to grow one had never been asked to. A duck-typed double
    would have kept that true — this node is the one that pops out of Redis.

    It also grades the DECODING, which the stand-in cannot: :meth:`enqueue` writes JSON and a
    consumer that handed the verifier a string would have found no ``submission`` on it and
    verified nothing, silently and greenly.
    """
    from exchange.auction.ledger import InMemoryLedgerSink, LedgerRecorder
    from exchange.external_bids.verification_queue import RedisVerificationQueue
    from exchange.ranking.verification import StaticCatalogSnapshots

    from proxyshop_support.redis_client import worker_redis

    key = "exchange:external-bid-verification:test-drain"
    redis = worker_redis()
    redis.delete(key)
    queue = RedisVerificationQueue(key)
    future = time.time() + 300
    app = _app(records={OPEN: _Record(roster=_roster(50.0), deadline=future)})
    configure_external_bids(app, queue=queue)
    from store_agent.external.nonces import NonceStore  # noqa: PLC0415

    configure_external_bids(app, nonces=NonceStore())
    configure_ranking(
        app,
        catalog=StaticCatalogSnapshots({DRAIN_STORE: _drain_snapshot(DRAIN_STORE)}),
        claim_dimensions=DRAIN_DIMENSIONS,
    )
    sink = InMemoryLedgerSink()
    app.state.auction_machine.ledger = LedgerRecorder(sink)
    client = TestClient(app, raise_server_exceptions=False)

    try:
        response = _submit(
            client, OPEN, _payload(OPEN, nonce="redis-drain-1", message=DRAIN_LIAR_PITCH)
        )
        assert response.status_code == 202, response.text[:400]

        assert queue.depth() == 0, "the real queue was written to and never read"
        verdicts = _claim_verdicts(sink)
        assert ("store-external-1", "warranty", "contradicted") in verdicts, verdicts
    finally:
        redis.delete(key)


def test_the_real_queue_round_trips_a_work_item_as_an_object_not_a_string() -> None:
    """``pop`` is the other half of ``enqueue``, and it must give back what was put in."""
    from exchange.external_bids.verification_queue import RedisVerificationQueue

    from proxyshop_support.redis_client import worker_redis

    key = "exchange:external-bid-verification:test-roundtrip"
    redis = worker_redis()
    redis.delete(key)
    queue = RedisVerificationQueue(key)
    try:
        assert queue.pop() is None, "an empty queue must answer None, not raise or block"
        first = {"kind": "external_bid_verification", "nonce": "a", "submission": {"claims": []}}
        second = {"kind": "external_bid_verification", "nonce": "b", "submission": {"claims": []}}
        queue.enqueue(first)
        queue.enqueue(second)

        # FIFO: `RPUSH` in, `LPOP` out. The head is the submission that has been waiting
        # longest, and its nonce has been spent for longest.
        assert queue.pop() == first
        assert queue.pop() == second
        assert queue.pop() is None
        assert queue.depth() == 0

        # A row nothing in this tree could have written is dropped rather than blocking every
        # consumer behind it.
        redis.rpush(key, "{not json")
        assert queue.pop() is None
        assert queue.depth() == 0
    finally:
        redis.delete(key)
