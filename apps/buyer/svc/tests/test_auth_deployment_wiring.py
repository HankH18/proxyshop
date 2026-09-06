"""T-140 and T-142 — what ``build_auth_service`` actually decides.

Two findings from the same root: the deployment builder wired the vault and nothing else.

* **T-140.** ``accounts=`` was never passed, so every service the process built got its own
  brand-new empty ``InMemoryAccountDirectory``. The only writer of a buyer record anywhere in
  the tree was ``redeem``'s ``upsert(email, {"email": email})``, so a record could never
  carry anything the login gesture already knew and ``GET /buyer/profile`` served the same
  information-free buckets to every buyer alive.
* **T-142.** ``publish_profile`` — the only writer of ``app.buyer_accounts``, "the
  store-visible working set" — had zero production call sites, and the buyer service opened
  no ``app``-role connection at all, while ``apps/buyer/compose.yaml`` had been handing it
  ``PROXYSHOP_PG_DSN_APP`` all along.

No datastore is touched here. The app connection is a recorder, and the sentinel DSN points
at a port nothing listens on and names a database that does not exist, so a doubled
``psycopg.connect`` that failed to take could not reach a live cluster either.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

SENTINEL_APP_DSN = "postgresql://app:pw@127.0.0.1:1/proxyshop_wiring_sentinel"

DANA = {
    "email": "dana.reyes@example.com",
    "region": "US-OR",
    "orders": [
        {"order_ref": "ord-1", "total": 240.0, "category": "camera-lenses"},
        {"order_ref": "ord-2", "total": 74.0, "category": "camera-lenses"},
        {"order_ref": "ord-3", "total": 128.0, "category": "hiking-boots"},
    ],
}


class _Cursor:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self._log = log

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, statement: Any, params: Any = None, **_kw: Any) -> _Cursor:
        self._log.append((str(statement), params))
        return self


class _Connection:
    """An ``app``-role connection stand-in that records instead of sending."""

    def __init__(self, log: list[tuple[str, Any]], opened: list[Any], dsn: Any) -> None:
        self._log = log
        opened.append(dsn)

    def cursor(self, *_a: Any, **_k: Any) -> _Cursor:
        return _Cursor(self._log)

    def close(self) -> None:
        return None


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No DSN and no worker count from the runner's shell decides these readings."""
    import os

    from buyer_svc.auth.routes import WORKER_COUNT_ENVS

    for name in list(os.environ):
        if name.startswith("PROXYSHOP_PG_DSN") or name in WORKER_COUNT_ENVS:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PROXYSHOP_BUYER_K_ANONYMITY", raising=False)


@pytest.fixture
def fresh_process_state():
    """Reset the two process-wide singletons around each test, whatever it does to them."""
    from buyer_svc.auth import routes as routes_mod

    previous_service = routes_mod._service
    previous_accounts = routes_mod._accounts
    routes_mod.set_auth_service(None)
    routes_mod.set_account_directory(None)
    try:
        yield routes_mod
    finally:
        routes_mod.set_auth_service(previous_service)
        routes_mod.set_account_directory(previous_accounts)


# =======================================================================================
# T-140 — the deployment builder decides where buyer records come from
# =======================================================================================


def test_every_service_the_process_builds_shares_one_account_directory(
    clean_env: None, fresh_process_state: Any
) -> None:
    """The finding, stated as a property: a rebuild must not forget the buyers.

    ``build_auth_service`` is what the running app calls, and calling it twice is what a
    restart does to the wiring. A record written through the first service's directory has
    to be there for the second, or the only thing a served profile can reflect is the address
    the login already had.
    """
    routes_mod = fresh_process_state

    first = routes_mod.build_auth_service()
    first.accounts.upsert(DANA["email"], DANA)

    second = routes_mod.build_auth_service()
    held = second.accounts.get(DANA["email"])
    assert held is not None, "a rebuilt service could not see a record the first one held"
    assert held["region"] == "US-OR"
    assert len(held["orders"]) == 3
    assert second.accounts is first.accounts


def test_a_deployment_can_install_its_own_account_directory(
    clean_env: None, fresh_process_state: Any
) -> None:
    """The seam a durable directory lands through, and the reason this is not a global dict.

    The in-memory default does not survive a real process restart. What must survive is the
    *ability to replace it* without editing the builder — otherwise the next ticket has to
    re-open this file to make buyer records durable.
    """
    from buyer_svc.auth import InMemoryAccountDirectory

    routes_mod = fresh_process_state
    installed = InMemoryAccountDirectory({DANA["email"]: DANA})
    routes_mod.set_account_directory(installed)

    service = routes_mod.build_auth_service()
    assert service.accounts is installed
    assert service.accounts.get(DANA["email"])["region"] == "US-OR"


def test_a_served_profile_reflects_a_record_written_outside_the_login_gesture(
    clean_env: None, fresh_process_state: Any
) -> None:
    """End to end over the app, which is the only place this property is worth anything.

    The history is established through the directory the PRODUCTION builder handed out —
    never as an argument to ``build_profile``, never into a directory this test constructed —
    and then the same address logs in again against a service rebuilt the way a restart
    rebuilds it. The profile that comes back must carry values no function of the address
    could invent.
    """
    from buyer_svc.main import create_app
    from buyer_svc.profile import build_profile
    from fastapi.testclient import TestClient

    routes_mod = fresh_process_state
    tokens: dict[str, str] = {}
    client = TestClient(create_app(), raise_server_exceptions=False)

    def _login() -> tuple[str, dict[str, Any]]:
        assert (
            client.post("/buyer/auth/magic-link", json={"email": DANA["email"]}).status_code == 202
        )
        opened = client.post("/buyer/auth/session", json={"token": tokens[DANA["email"]]})
        assert opened.status_code == 201, opened.text
        session = opened.json()
        read = client.get("/buyer/profile", headers={"X-Buyer-Session": session["session_id"]})
        assert read.status_code == 200, read.text
        return session["pseudonym"], read.json()["buckets"]

    service = routes_mod.build_auth_service()
    routes_mod.set_auth_service(service)
    service.deliver = lambda email, token, expires_at: tokens.__setitem__(email, token)

    # A brand-new buyer: nothing is known, and that is correct.
    _pseudonym, before = _login()

    # The history, written through the production service's own directory.
    service.accounts.upsert(DANA["email"], DANA)

    # The restart.
    rebuilt = routes_mod.build_auth_service()
    routes_mod.set_auth_service(rebuilt)
    rebuilt.deliver = lambda email, token, expires_at: tokens.__setitem__(email, token)

    pseudonym, after = _login()
    assert after != before, (
        "the served profile did not move when the buyer's history did, so it is a function "
        f"of the address rather than of the buyer: {before}"
    )
    assert after == build_profile(DANA, pseudonym).model_dump()["buckets"]
    assert after["region"] == "US-OR"
    assert after["first_time"] is False


def test_the_login_gesture_does_not_overwrite_a_record_it_did_not_write(
    clean_env: None, fresh_process_state: Any
) -> None:
    """Signup and login are one gesture; the signup half must not run for a known buyer.

    ``redeem`` creates a minimal record for an address it has never seen. Now that the
    directory outlives the service, that same branch running on a *second* login would
    silently flatten a real buyer's history back to ``{"email": ...}`` — a populated
    directory turning itself empty one login at a time.
    """
    routes_mod = fresh_process_state
    tokens: list[str] = []

    service = routes_mod.build_auth_service()
    service.deliver = lambda email, token, expires_at: tokens.append(token)
    service.accounts.upsert(DANA["email"], DANA)

    for _ in range(3):
        service.request_login(DANA["email"])
        service.redeem(tokens[-1])

    held = service.accounts.get(DANA["email"])
    assert len(held["orders"]) == 3, f"the login gesture flattened the record: {held}"
    assert held["region"] == "US-OR"


# =======================================================================================
# T-142 — the served profile reaches app.buyer_accounts
# =======================================================================================


def test_no_app_dsn_means_no_connection_and_no_publisher(
    clean_env: None, fresh_process_state: Any
) -> None:
    """A database-less dev boot is unchanged: the profile route still answers from memory."""
    import psycopg

    routes_mod = fresh_process_state
    opened: list[Any] = []

    def _refuse(*args: Any, **kwargs: Any) -> None:
        opened.append((args, kwargs))
        raise AssertionError("a connection was opened with no app DSN configured")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(psycopg, "connect", _refuse)
        service = routes_mod.build_auth_service()
    assert service.publish is None
    assert opened == []


def test_a_served_profile_is_upserted_into_app_buyer_accounts(
    clean_env: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row, not the call. ``publish_profile`` is never doubled here.

    A recorder installed over ``publish_profile`` could only prove "the function was called",
    which a repair that hands it a ``None`` connection satisfies while the table receives
    nothing. So the CONNECTION is the double and the assertions are row-shaped: a statement
    naming ``app.buyer_accounts``, carrying this buyer's pseudonym and this buyer's served
    buckets.
    """
    import psycopg
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    routes_mod = fresh_process_state
    statements: list[tuple[str, Any]] = []
    opened: list[Any] = []

    def _connect(*args: Any, **kwargs: Any) -> _Connection:
        return _Connection(statements, opened, f"args={args!r} kwargs={kwargs!r}")

    monkeypatch.setattr(psycopg, "connect", _connect)
    monkeypatch.setattr(psycopg.Connection, "connect", staticmethod(_connect), raising=False)
    monkeypatch.setenv("PROXYSHOP_PG_DSN_APP", SENTINEL_APP_DSN)

    tokens: dict[str, str] = {}
    client = TestClient(create_app(), raise_server_exceptions=False)
    service = routes_mod.build_auth_service()
    routes_mod.set_auth_service(service)
    service.deliver = lambda email, token, expires_at: tokens.__setitem__(email, token)
    service.accounts.upsert(DANA["email"], DANA)

    assert opened, "no app-role connection was opened while PROXYSHOP_PG_DSN_APP was set"
    assert any(SENTINEL_APP_DSN in str(dsn) for dsn in opened), (
        f"a connection was opened but not from the configured DSN: {opened!r}"
    )

    client.post("/buyer/auth/magic-link", json={"email": DANA["email"]})
    session = client.post("/buyer/auth/session", json={"token": tokens[DANA["email"]]}).json()
    read = client.get("/buyer/profile", headers={"X-Buyer-Session": session["session_id"]})
    assert read.status_code == 200, read.text
    served = read.json()["buckets"]

    writes = [(text, params) for text, params in statements if "buyer_accounts" in text.lower()]
    assert writes, (
        f"the whole production login path ran and no statement naming app.buyer_accounts "
        f"reached a connection ({len(statements)} statements in total)"
    )

    def _decodes_to_the_served_buckets(params: Any) -> bool:
        """Recover the bucket object out of a parameter however psycopg was handed it."""
        for candidate in params if isinstance(params, (list, tuple)) else [params]:
            if isinstance(candidate, dict) and candidate == served:
                return True
            if isinstance(candidate, (str, bytes)):
                try:
                    if json.loads(candidate) == served:
                        return True
                except (ValueError, TypeError):
                    continue
        return False

    matched = [
        params
        for _text, params in writes
        if session["pseudonym"] in repr(params) and _decodes_to_the_served_buckets(params)
    ]
    assert matched, (
        f"no row carried this buyer's own pseudonym and its own served buckets; writes={writes!r}"
    )
    assert "on conflict" in writes[0][0].lower(), (
        "the publish is an INSERT with no upsert, so a returning buyer's row would collide "
        f"rather than update: {writes[0][0]}"
    )


def test_the_row_is_rewritten_when_the_buyers_history_changes(
    clean_env: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publisher that fires once per buyer leaves every store reading a stale row."""
    import psycopg

    routes_mod = fresh_process_state
    statements: list[tuple[str, Any]] = []
    opened: list[Any] = []
    monkeypatch.setattr(
        psycopg,
        "connect",
        lambda *a, **k: _Connection(statements, opened, f"args={a!r} kwargs={k!r}"),
    )
    monkeypatch.setenv("PROXYSHOP_PG_DSN_APP", SENTINEL_APP_DSN)

    tokens: list[str] = []
    service = routes_mod.build_auth_service()
    service.deliver = lambda email, token, expires_at: tokens.append(token)

    service.request_login(DANA["email"])
    first = service.redeem(tokens[-1])
    service.profile_for(first.session_id)
    after_first = len(statements)

    service.accounts.upsert(DANA["email"], DANA)
    service.request_login(DANA["email"])
    second = service.redeem(tokens[-1])
    published = service.profile_for(second.session_id)

    assert len(statements) > after_first, "the second profile read published nothing"
    latest = statements[-1][1]
    assert second.pseudonym in repr(latest)
    assert json.dumps(published.model_dump()["buckets"], sort_keys=True) in repr(latest)


def test_a_publish_failure_is_not_swallowed(
    clean_env: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``app.buyer_accounts`` is what a store reads; a silent failure is a silent divergence.

    Serving the buyer a fresh profile while the store's row stayed stale is a state nothing
    downstream can detect. A deployment that configured the DSN asked for the publish, so a
    failure is allowed to be loud — and the route turns it into a 500 rather than a 200 over
    a row that was never written.
    """
    import psycopg
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    routes_mod = fresh_process_state

    class _Broken(_Connection):
        def cursor(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("app.buyer_accounts is unreachable")

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _Broken([], [], "broken"))
    monkeypatch.setenv("PROXYSHOP_PG_DSN_APP", SENTINEL_APP_DSN)

    tokens: dict[str, str] = {}
    client = TestClient(create_app(), raise_server_exceptions=False)
    service = routes_mod.build_auth_service()
    routes_mod.set_auth_service(service)
    service.deliver = lambda email, token, expires_at: tokens.__setitem__(email, token)

    client.post("/buyer/auth/magic-link", json={"email": DANA["email"]})
    session = client.post("/buyer/auth/session", json={"token": tokens[DANA["email"]]}).json()
    answered = client.get("/buyer/profile", headers={"X-Buyer-Session": session["session_id"]})
    assert answered.status_code == 500, (
        f"a profile whose publish failed was served as if it had reached the store table: "
        f"{answered.status_code} {answered.text}"
    )
    # And the failure must not have carried the buyer out with it.
    for fragment in ("dana", "reyes", "example.com"):
        assert fragment not in answered.text.casefold(), answered.text


def test_publishing_never_runs_on_a_profile_that_failed_the_identity_backstop(
    clean_env: None, fresh_process_state: Any
) -> None:
    """The one thing that must never reach a table every store-facing role can read.

    ``build_profile`` raises ``IdentityLeak`` rather than returning a degraded profile. If
    the publish ran first — or ran in a ``finally`` — the leak would be written into
    ``app.buyer_accounts`` and the refusal would be the thing that published it.
    """
    from buyer_svc.auth import InMemoryAccountDirectory, MagicLinkAuth
    from buyer_svc.profile import IdentityLeak

    leaking = {
        "email": "mallory.quist@example.com",
        "region": "US-OR",
        "orders": [{"order_ref": "o1", "total": 42.0, "category": "mallory-quist"}],
    }
    published: list[Any] = []
    tokens: list[str] = []
    service = MagicLinkAuth(
        accounts=InMemoryAccountDirectory({leaking["email"]: leaking}),
        deliver=lambda email, token, expires_at: tokens.append(token),
        publish=published.append,
    )
    service.request_login(leaking["email"])
    session = service.redeem(tokens[-1])

    with pytest.raises(IdentityLeak):
        service.profile_for(session.session_id)
    assert published == [], f"a leaking profile was published: {published}"


# =======================================================================================
# T-142's other half: a failure must be loud AND survivable. Appended.
# =======================================================================================


class _Cluster:
    """A database that can go away and come back.

    An outage kills every connection already open and refuses new ones; recovery lets NEW
    connections through and never revives an old one. That asymmetry is the whole point and
    it is what a real socket does: a process holding one connection for its lifetime does not
    come back when the database does.
    """

    def __init__(self) -> None:
        self.up = True
        self.statements: list[tuple[str, Any]] = []
        self.opened: list[Any] = []
        self._live: list[Any] = []
        self._closed: list[Any] = []

    def connect(self, *args: Any, **kwargs: Any) -> Any:
        if not self.up:
            raise RuntimeError("could not connect to server: Connection refused")
        connection = _Connection(self.statements, self.opened, f"args={args!r} kwargs={kwargs!r}")
        connection.usable = True
        # `closed` is psycopg's own signal and the publisher reads it to tell a dead socket
        # from a rejected statement: psycopg marks a connection closed when the socket is gone
        # and leaves it open when the server merely said no. Modelling it is what makes the
        # difference between the two failures testable at all.
        connection.closed = 0

        # Per-instance, so a socket that is closed can be told apart from one still held —
        # which is how a leaked connection becomes visible to a test.
        #
        # And closing REALLY closes it. Written first as a bare `self._closed.append(...)`,
        # this made `close()` inert: a test could close a connection another thread was using
        # and that thread would carry on working, so the gate below for exactly that hazard
        # could not fail. A fake whose destructive operation is not destructive measures
        # nothing.
        def _close(connection: Any = connection) -> None:
            self._closed.append(connection)
            connection.usable = False
            connection.closed = 1

        connection.close = _close
        self._live.append(connection)
        return connection

    def outage(self) -> None:
        self.up = False
        for connection in self._live:
            connection.usable = False
            connection.closed = 1
        self._live.clear()

    def recover(self) -> None:
        self.up = True

    def live(self) -> list[Any]:
        """Connections opened since the last outage and not closed since."""
        return [connection for connection in self._live if connection not in self._closed]

    def writes(self) -> list[tuple[str, Any]]:
        return [(text, params) for text, params in self.statements if "buyer_accounts" in text]


def _cursor_honouring_usable(self: Any, *_a: Any, **_k: Any) -> Any:
    if not getattr(self, "usable", True):
        raise RuntimeError("connection is closed / server closed the connection unexpectedly")
    return _Cursor(self._log)


def test_a_transient_database_outage_is_not_permanent_for_the_life_of_the_process(
    clean_env: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both properties at once: the failure is never swallowed, and it is never forever.

    ``build_profile_publisher`` opened ONE connection at build time and opened no other,
    deliberately without a ``try/except`` on a fail-loudly argument. That argument covers the
    first failure and says nothing about the permanent one. MEASURED before the repair, with a
    cluster that goes down and comes back up: ``GET /buyer/profile`` answered
    ``200, 500, [500, 500, 500]`` — every profile read after a single transient blip failed for
    the rest of the process's life, and only a restart cured it. This is a failure mode T-142's
    half of the branch introduced.

    The fix must not be a swallowed publish. ``app.buyer_accounts`` is what a store reads, and
    the commit body's own concern is right: a publish that fails silently leaves every store
    reading a stale row while the buyer is served a fresh one, and nothing downstream can
    detect that. So the outage must still be a 500 while it lasts, and the recovery must be a
    200 over a row that actually reached a cursor.
    """
    import psycopg
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    routes_mod = fresh_process_state
    cluster = _Cluster()
    monkeypatch.setattr(_Connection, "cursor", _cursor_honouring_usable, raising=False)
    monkeypatch.setattr(psycopg, "connect", cluster.connect)
    monkeypatch.setenv("PROXYSHOP_PG_DSN_APP", SENTINEL_APP_DSN)

    tokens: dict[str, str] = {}
    client = TestClient(create_app(), raise_server_exceptions=False)
    service = routes_mod.build_auth_service()
    routes_mod.set_auth_service(service)
    service.deliver = lambda email, token, expires_at: tokens.__setitem__(email, token)
    service.accounts.upsert(DANA["email"], DANA)

    def _login_and_read() -> int:
        client.post("/buyer/auth/magic-link", json={"email": DANA["email"]})
        session = client.post("/buyer/auth/session", json={"token": tokens[DANA["email"]]}).json()
        return client.get(
            "/buyer/profile", headers={"X-Buyer-Session": session["session_id"]}
        ).status_code

    # ARMED: healthy, this really does serve and really does write. Without this the two
    # readings below could both be produced by a route that was broken from the start.
    assert _login_and_read() == 200
    healthy_writes = len(cluster.writes())
    assert healthy_writes, "no row reached a cursor while the database was up"

    cluster.outage()
    during = _login_and_read()
    assert during == 500, (
        f"a profile whose publish could not reach the store table was served as {during}; a "
        "silently stale row is exactly the divergence nothing downstream can detect"
    )
    assert len(cluster.writes()) == healthy_writes, "a write landed during the outage"

    cluster.recover()
    after = [_login_and_read() for _ in range(3)]
    assert after == [200, 200, 200], (
        f"the database came back and GET /buyer/profile did not: {after}. One transient blip "
        "left the publisher holding a connection it can never use again, so every buyer is "
        "answered 500 until somebody restarts the process"
    )
    assert len(cluster.writes()) >= healthy_writes + 3, (
        f"the route answered 200 three times and only {len(cluster.writes()) - healthy_writes} "
        "further rows reached a cursor; recovering by not publishing is the silent divergence "
        "wearing the repair's clothes"
    )


def test_a_publish_that_cannot_be_completed_still_fails_loudly_after_the_retry(
    clean_env: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconnect must not become a way to swallow a publish that never lands.

    The dangerous shape of this repair is "try, reconnect, try again, and carry on regardless"
    — which turns every publish failure into a 200 over a row nobody wrote. A database that is
    down and stays down must therefore still produce a 500, and it must still not carry the
    buyer out with it.
    """
    import psycopg
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    routes_mod = fresh_process_state
    cluster = _Cluster()
    monkeypatch.setattr(_Connection, "cursor", _cursor_honouring_usable, raising=False)
    monkeypatch.setattr(psycopg, "connect", cluster.connect)
    monkeypatch.setenv("PROXYSHOP_PG_DSN_APP", SENTINEL_APP_DSN)

    tokens: dict[str, str] = {}
    client = TestClient(create_app(), raise_server_exceptions=False)
    service = routes_mod.build_auth_service()
    routes_mod.set_auth_service(service)
    service.deliver = lambda email, token, expires_at: tokens.__setitem__(email, token)
    service.accounts.upsert(DANA["email"], DANA)

    cluster.outage()
    for _ in range(3):
        client.post("/buyer/auth/magic-link", json={"email": DANA["email"]})
        session = client.post("/buyer/auth/session", json={"token": tokens[DANA["email"]]}).json()
        answered = client.get("/buyer/profile", headers={"X-Buyer-Session": session["session_id"]})
        assert answered.status_code == 500, (
            f"the publisher reported success while the database was down: "
            f"{answered.status_code} {answered.text}"
        )
        for fragment in ("dana", "reyes", "example.com"):
            assert fragment not in answered.text.casefold(), answered.text
    assert cluster.writes() == [], f"a row landed against a dead cluster: {cluster.writes()!r}"


def test_the_publisher_does_not_open_a_fresh_connection_for_every_profile_read(
    clean_env: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconnecting on failure, not connecting per request.

    A publisher that opened a connection per publish would pass the recovery gate above and
    hand a production deployment a connection storm instead — one TCP+TLS+auth round trip on
    every ``GET /buyer/profile``, against a Postgres role whose connection limit is finite.
    """
    import psycopg

    routes_mod = fresh_process_state
    cluster = _Cluster()
    monkeypatch.setattr(_Connection, "cursor", _cursor_honouring_usable, raising=False)
    monkeypatch.setattr(psycopg, "connect", cluster.connect)
    monkeypatch.setenv("PROXYSHOP_PG_DSN_APP", SENTINEL_APP_DSN)

    tokens: list[str] = []
    service = routes_mod.build_auth_service()
    service.deliver = lambda email, token, expires_at: tokens.append(token)
    service.accounts.upsert(DANA["email"], DANA)

    for _ in range(6):
        service.request_login(DANA["email"])
        session = service.redeem(tokens[-1])
        service.profile_for(session.session_id)

    assert len(cluster.writes()) >= 6, "the sweep published nothing, so it measured nothing"
    assert len(cluster.opened) == 1, (
        f"{len(cluster.opened)} connections were opened for six profile reads against a "
        "healthy database; the publisher reconnects per request rather than on failure"
    )


def test_a_reconnect_does_not_serialise_every_other_profile_read_behind_it(
    clean_env: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconnecting must not turn a publisher outage into a whole-service stall.

    ``def`` endpoints run in a threadpool, so during an outage every thread in that pool is
    inside the publisher at once. If the slot's lock were held across ``psycopg.connect``,
    each of them would wait out the one before it — an OS-length TCP timeout apiece against a
    host that is not answering — and the pool would be starved for routes that have nothing to
    do with profiles. Recovering from a local failure by stalling globally is the same shape of
    bug as the one this branch's limiter repair closes.

    Driven with events rather than sleeps: the first thread is parked *inside* ``connect``, and
    the second must be able to reach ``connect`` while it is parked. A lock held across the
    call makes the second thread unreachable and this fails on the timeout instead of hanging.
    """
    import threading

    import psycopg

    routes_mod = fresh_process_state
    cluster = _Cluster()
    monkeypatch.setattr(_Connection, "cursor", _cursor_honouring_usable, raising=False)
    monkeypatch.setattr(psycopg, "connect", cluster.connect)
    monkeypatch.setenv("PROXYSHOP_PG_DSN_APP", SENTINEL_APP_DSN)

    tokens: list[str] = []
    service = routes_mod.build_auth_service()
    service.deliver = lambda email, token, expires_at: tokens.append(token)
    service.accounts.upsert(DANA["email"], DANA)

    sessions = []
    for _ in range(2):
        service.request_login(DANA["email"])
        sessions.append(service.redeem(tokens[-1]))

    # Kill the connection built at boot, so the next publish must reconnect.
    cluster.outage()
    cluster.recover()

    entered = threading.Semaphore(0)
    release_first = threading.Event()
    reconnects: list[int] = []
    real_connect = cluster.connect

    def _slow_connect(*args: Any, **kwargs: Any) -> Any:
        reconnects.append(1)
        entered.release()
        if len(reconnects) == 1:
            # Park the first reconnect INSIDE connect, the way a dead host would.
            assert release_first.wait(timeout=10), "the test's own event never fired"
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(psycopg, "connect", _slow_connect)

    errors: list[BaseException] = []

    def _publish(index: int) -> None:
        try:
            service.profile_for(sessions[index].session_id)
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_publish, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()

    assert entered.acquire(timeout=5), "no thread reached connect at all"
    reached_while_parked = entered.acquire(timeout=5)
    release_first.set()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive(), "a publish thread never finished"

    assert reached_while_parked, (
        "the second profile read could not even reach psycopg.connect while the first was "
        "parked inside it, so the slot's lock is held across the connect; during an outage "
        "every threadpool thread queues behind one TCP timeout and the whole service stalls"
    )
    assert errors == [], f"a publish raised against a healthy cluster: {errors!r}"
    # And the race did not leak a socket: one connection is held, the loser closed its own.
    assert len(cluster.live()) == 1, (
        f"{len(cluster.live())} connections are still open after a two-thread reconnect race; "
        "the loser leaked its socket instead of closing it"
    )


def test_a_rejected_statement_does_not_throw_away_a_healthy_connection(
    clean_env: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead socket and a rejected statement are different failures. Only one needs a retry.

    A constraint violation, a missing grant, a value the column will not take — the server
    answered, the connection is fine, and reconnecting buys nothing. MEASURED when the retry
    treated every failure as a socket failure: 20 reads of ONE profile whose row cannot be
    written opened 40 connections and closed 40, roughly two per request, for as long as that
    one buyer keeps logging in — against a Postgres role whose ``max_connections`` is finite.

    ``test_the_publisher_does_not_open_a_fresh_connection_for_every_profile_read`` measures
    only the healthy case, which was never the case in doubt. This is the unhealthy one, and
    the failure must still be loud: the row did not land, so the buyer must not be told it did.
    """
    import psycopg

    routes_mod = fresh_process_state
    cluster = _Cluster()

    def _rejecting_cursor(self: Any, *_a: Any, **_k: Any) -> Any:
        if not getattr(self, "usable", True):
            raise RuntimeError("connection is closed / server closed the connection")
        # The server answered, and said no. `closed` stays 0, exactly as psycopg leaves it.
        raise RuntimeError('new row for relation "buyer_accounts" violates check constraint')

    monkeypatch.setattr(_Connection, "cursor", _rejecting_cursor, raising=False)
    monkeypatch.setattr(psycopg, "connect", cluster.connect)
    monkeypatch.setenv("PROXYSHOP_PG_DSN_APP", SENTINEL_APP_DSN)

    tokens: list[str] = []
    service = routes_mod.build_auth_service()
    service.deliver = lambda email, token, expires_at: tokens.append(token)
    service.accounts.upsert(DANA["email"], DANA)
    opened_after_boot = len(cluster.opened)

    failures = 0
    for _ in range(20):
        service.request_login(DANA["email"])
        session = service.redeem(tokens[-1])
        try:
            service.profile_for(session.session_id)
        except Exception:  # noqa: BLE001 - the point is that it DOES raise
            failures += 1

    # ARMED: every publish really did fail, so the connection count below is being measured
    # against twenty genuine failures rather than twenty successes.
    assert failures == 20, f"only {failures} of 20 publishes failed; this measured nothing"

    churn = len(cluster.opened) - opened_after_boot
    assert churn == 0, (
        f"{churn} connections were opened for 20 rejected statements against a connection the "
        "server never closed; a publish failure that is not a connection failure is being "
        "answered with a reconnect"
    )
    assert cluster.live(), "the healthy connection was thrown away over a rejected statement"


def test_one_threads_reconnect_does_not_break_another_threads_publish(
    clean_env: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connection is shared by the whole threadpool, so discarding must not close it.

    During a failover the first statement on the shared connection fails for whoever reaches
    it first, and several threads discover it at once. If a discard closed the socket, one
    thread's cleanup would land on a connection another thread had already replaced and was
    mid-statement on — turning one thread's blip into the other's 500 on a database that is
    back up. Nothing this publisher discards needs closing anyway: it only discards what the
    driver has already marked closed.
    """
    import threading

    import psycopg

    routes_mod = fresh_process_state
    cluster = _Cluster()
    monkeypatch.setattr(_Connection, "cursor", _cursor_honouring_usable, raising=False)
    monkeypatch.setattr(psycopg, "connect", cluster.connect)
    monkeypatch.setenv("PROXYSHOP_PG_DSN_APP", SENTINEL_APP_DSN)

    tokens: list[str] = []
    service = routes_mod.build_auth_service()
    service.deliver = lambda email, token, expires_at: tokens.append(token)
    service.accounts.upsert(DANA["email"], DANA)

    sessions = []
    for _ in range(6):
        service.request_login(DANA["email"])
        sessions.append(service.redeem(tokens[-1]))

    # The failover: the connection every thread is about to use is dead, and the database is
    # already back, so a reconnect succeeds.
    cluster.outage()
    cluster.recover()

    errors: list[BaseException] = []
    barrier = threading.Barrier(6, timeout=10)

    def _publish(index: int) -> None:
        try:
            barrier.wait()
            service.profile_for(sessions[index].session_id)
        except BaseException as exc:  # noqa: BLE001 - collected, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_publish, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive(), "a publish thread never finished"

    assert errors == [], (
        f"{len(errors)} of 6 concurrent publishes failed against a database that was back up: "
        f"{errors!r}. One thread's discard closed the connection another was using"
    )
    assert len(cluster.writes()) >= 6, "the sweep published nothing, so it measured nothing"


def test_a_retry_that_also_fails_is_reported_and_not_swallowed(
    clean_env: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry's OWN failure must reach the caller, and no existing test reached that line.

    ``test_a_publish_that_cannot_be_completed_still_fails_loudly_after_the_retry`` drives a
    cluster that is down and stays down, so the second attempt never happens: the reconnect
    itself raises and that is what the route reports. The retry's ``publish_profile`` is
    therefore never executed there, and a repair that swallowed it — ``except Exception: pass``
    around the second attempt — passed the whole suite. Measured as a surviving mutant.

    This is the ordering that reaches it: the cluster ACCEPTS connections and then drops every
    one of them on the first statement, which is what a half-healthy replica does. First
    attempt fails, reconnect succeeds, second attempt fails. The buyer must be told.
    """
    import psycopg
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    routes_mod = fresh_process_state
    cluster = _Cluster()

    def _resetting_cursor(self: Any, *_a: Any, **_k: Any) -> Any:
        # The server drops the connection instead of answering. `closed` goes to 1, exactly as
        # psycopg marks it, so the publisher correctly reads this as a socket failure and
        # reconnects — and the replacement behaves the same way.
        self.usable = False
        self.closed = 1
        raise RuntimeError("server closed the connection unexpectedly")

    monkeypatch.setattr(_Connection, "cursor", _resetting_cursor, raising=False)
    monkeypatch.setattr(psycopg, "connect", cluster.connect)
    monkeypatch.setenv("PROXYSHOP_PG_DSN_APP", SENTINEL_APP_DSN)

    tokens: dict[str, str] = {}
    client = TestClient(create_app(), raise_server_exceptions=False)
    service = routes_mod.build_auth_service()
    routes_mod.set_auth_service(service)
    service.deliver = lambda email, token, expires_at: tokens.__setitem__(email, token)
    service.accounts.upsert(DANA["email"], DANA)
    opened_before = len(cluster.opened)

    client.post("/buyer/auth/magic-link", json={"email": DANA["email"]})
    session = client.post("/buyer/auth/session", json={"token": tokens[DANA["email"]]}).json()
    answered = client.get("/buyer/profile", headers={"X-Buyer-Session": session["session_id"]})

    # ARMED: the retry really was attempted, so the assertion below is about the retry's
    # failure and not about the first attempt's.
    assert len(cluster.opened) > opened_before, (
        "no reconnect happened, so the second publish attempt never ran and this measured "
        "the same path the down-and-stays-down test already covers"
    )
    assert answered.status_code == 500, (
        f"a profile was served as {answered.status_code} after BOTH publish attempts failed; "
        "the retry's own failure is being swallowed, which leaves every store reading a stale "
        "row while the buyer is handed a fresh one"
    )
    assert cluster.writes() == [], (
        f"a row landed despite both attempts failing: {cluster.writes()!r}"
    )
    for fragment in ("dana", "reyes", "example.com"):
        assert fragment not in answered.text.casefold(), answered.text
