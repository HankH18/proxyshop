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
