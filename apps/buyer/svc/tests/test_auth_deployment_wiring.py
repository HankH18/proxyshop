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


# =======================================================================================
# T-361 — the login gesture could not complete in ANY deployment
#
# ``build_auth_service`` decided the vault, the account directory and the publisher, and
# never once decided where the link goes: ``build_auth_service().deliver is magic_link._drop``
# was True on every production path. ``_drop`` is deliberately inert — "a service booted
# without a mail transport should fail to log buyers in, not publish bearer credentials to
# stdout" — so the route answered 202 Accepted, the buyer waited for a mail that was thrown
# away in-process, and nothing anywhere reported a problem. T-141 was recorded CLOSED as
# "REFUTED"; the code says otherwise, which is what makes this worth a gate rather than a
# comment.
#
# Two halves, and both are the fix:
#   * a deployment that HAS configured a transport really mails the token, and the link the
#     buyer clicks really opens a session;
#   * a deployment that has NOT configured one refuses the route out loud instead of
#     accepting a login it cannot deliver.
# =======================================================================================

TRANSPORT_ENVS = (
    "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_URL",
    "PROXYSHOP_BUYER_MAGIC_LINK_SENDER",
    "PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL",
)


class _SMTPSession:
    """One connection to the fake MTA. Records instead of sending."""

    def __init__(self, mailbox: _Mailbox) -> None:
        self._mailbox = mailbox
        self.started_tls = False
        self.logged_in: tuple[str, str] | None = None

    def __enter__(self) -> _SMTPSession:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def starttls(self, *_a: Any, **_k: Any) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def send_message(self, message: Any) -> None:
        self._mailbox.sent.append(message)

    def quit(self) -> None:
        return None


class _Mailbox:
    """A stand-in MTA: records every connection and every message handed to it."""

    def __init__(self) -> None:
        self.opened: list[tuple[str, int]] = []
        self.timeouts: list[Any] = []
        self.sessions: list[_SMTPSession] = []
        self.sent: list[Any] = []

    def transport(self, host: str, port: int, *_a: Any, timeout: Any = None, **_k: Any) -> Any:
        self.opened.append((host, port))
        self.timeouts.append(timeout)
        session = _SMTPSession(self)
        self.sessions.append(session)
        return session

    def link(self) -> str:
        assert len(self.sent) == 1, f"{len(self.sent)} messages were sent"
        body = self.sent[0].get_content()
        for word in body.split():
            if word.startswith("http"):
                return word.rstrip(".,")
        raise AssertionError(f"no link in the delivered mail:\n{body}")

    def token(self) -> str:
        from urllib.parse import parse_qs, urlsplit

        found = parse_qs(urlsplit(self.link()).query).get("token")
        assert found, f"the delivered link carries no token: {self.link()}"
        return found[0]


#: The variable that SELECTS a transport, kept out of ``TRANSPORT_ENVS`` because that tuple
#: is unpacked as the three mail variables. It is cleared by ``no_transport`` all the same:
#: a shell (or a devstack left running in the same session) that exports it would otherwise
#: decide what "no transport configured" means for every test below.
TRANSPORT_SELECT_ENV = "PROXYSHOP_BUYER_MAGIC_LINK_TRANSPORT"


@pytest.fixture
def no_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """No mail transport configured — the state every deployment has actually been in."""
    for name in (*TRANSPORT_ENVS, TRANSPORT_SELECT_ENV):
        monkeypatch.delenv(name, raising=False)


def test_t361_the_production_wiring_no_longer_resolves_to_the_drop_transport(
    clean_env: None, no_transport: None, fresh_process_state: Any
) -> None:
    """The finding verbatim: ``build_auth_service().deliver is magic_link._drop``.

    ``_drop`` swallowing the token is the correct DEFAULT for a bare ``MagicLinkAuth()`` — a
    unit test does not want a mail sent. It is not a wiring decision, and the deployment
    builder making no wiring decision at all is how the whole feature ended up unreachable in
    production while every test passed.
    """
    from buyer_svc.auth import magic_link as magic_link_mod
    from buyer_svc.auth.delivery import MagicLinkUndeliverable

    routes_mod = fresh_process_state
    service = routes_mod.build_auth_service()

    assert service.deliver is not magic_link_mod._drop, (
        "the production login service still delivers to _drop: every magic link this "
        "deployment issues is discarded in-process and no buyer can ever log in"
    )
    with pytest.raises(MagicLinkUndeliverable):
        service.request_login(DANA["email"])


def test_t361_a_deployment_with_no_transport_refuses_the_login_instead_of_accepting_it(
    clean_env: None, no_transport: None, fresh_process_state: Any
) -> None:
    """Fail closed at the door. A 202 for a mail nobody will send is the defect, not the fix.

    The buyer is told the link is on its way, waits, and re-requests — spending a real budget
    against a service that structurally cannot answer. The refusal has to be loud enough for
    an operator to act on and must still not put a token on the wire.
    """
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    routes_mod = fresh_process_state
    routes_mod.set_auth_service(routes_mod.build_auth_service())
    client = TestClient(create_app(), raise_server_exceptions=False)

    answered = client.post("/buyer/auth/magic-link", json={"email": DANA["email"]})

    assert answered.status_code == 503, (
        f"a login with no mail transport configured was answered {answered.status_code}; the "
        "buyer is waiting for a link this deployment threw away"
    )
    assert "transport" in answered.text.casefold(), (
        f"the refusal says nothing an operator could act on: {answered.text!r}"
    )
    # Still not an oracle and still not a leak: no address, and nothing token-shaped.
    for fragment in ("dana", "reyes"):
        assert fragment not in answered.text.casefold(), answered.text


def test_t361_a_configured_transport_mails_the_token_and_the_mailed_link_opens_a_session(
    clean_env: None, no_transport: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole login gesture over the wiring a deployment really gets. This is the headline.

    Nothing here overrides ``deliver``: the transport under test is the one
    ``build_auth_service`` chose from the environment, and the token that opens the session is
    the one that came out of the delivered mail rather than out of a test double's list. That
    is the difference between "the feature is tested" and "the feature works in production" —
    every existing magic-link test injects its own ``deliver`` and is blind to this by
    construction.
    """
    import smtplib

    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    routes_mod = fresh_process_state
    mailbox = _Mailbox()
    monkeypatch.setattr(smtplib, "SMTP", mailbox.transport)
    monkeypatch.setenv("PROXYSHOP_BUYER_MAGIC_LINK_SMTP_URL", "smtp://mail.example.net:2525")
    monkeypatch.setenv("PROXYSHOP_BUYER_MAGIC_LINK_SENDER", "no-reply@proxyshop.example")
    monkeypatch.setenv(
        "PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL", "https://buyer.proxyshop.example/auth/callback"
    )

    routes_mod.set_auth_service(routes_mod.build_auth_service())
    client = TestClient(create_app(), raise_server_exceptions=False)

    requested = client.post("/buyer/auth/magic-link", json={"email": DANA["email"]})
    assert requested.status_code == 202, requested.text

    assert mailbox.opened == [("mail.example.net", 2525)], (
        f"the configured MTA was not the one contacted: {mailbox.opened!r}"
    )
    assert mailbox.timeouts and all(mailbox.timeouts), (
        "the SMTP connection was opened with no timeout, so one unreachable MTA parks a "
        "threadpool worker for as long as the OS lets it"
    )
    assert len(mailbox.sent) == 1, f"{len(mailbox.sent)} messages reached the MTA"
    message = mailbox.sent[0]
    assert message["To"] == DANA["email"]
    assert message["From"] == "no-reply@proxyshop.example"
    assert mailbox.link().startswith("https://buyer.proxyshop.example/auth/callback?"), (
        f"the mailed link does not point at the configured front door: {mailbox.link()}"
    )

    token = mailbox.token()
    assert token not in requested.text, (
        "the token is on the HTTP response as well as in the mail; the mailbox is no longer "
        "the credential"
    )

    session = client.post("/buyer/auth/session", json={"token": token})
    assert session.status_code == 201, (
        f"the token out of the delivered mail did not open a session ({session.status_code}): "
        f"{session.text}"
    )
    assert session.json()["pseudonym"].startswith("psn-")

    # Single use, exactly as every other path: the mailed link is not a reusable credential.
    assert client.post("/buyer/auth/session", json={"token": token}).status_code == 401


def test_t361_a_half_configured_transport_refuses_to_boot_rather_than_guessing(
    clean_env: None, no_transport: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator who configured the MTA and forgot the rest gets an error, not a silence.

    Every case here is a deployment that meant to send mail. Silently falling back to "no
    transport" would put it in exactly the state T-361 is about while looking configured, so
    a half-set transport is a boot failure — the same shape as
    :class:`ProcessLocalStateUnsafe`, which already refuses a configuration this service's
    state model cannot survive.
    """
    from buyer_svc.auth.delivery import MagicLinkTransportMisconfigured

    routes_mod = fresh_process_state
    smtp_env, sender_env, base_env = TRANSPORT_ENVS
    complete = {
        smtp_env: "smtp://mail.example.net:2525",
        sender_env: "no-reply@proxyshop.example",
        base_env: "https://buyer.proxyshop.example/auth/callback",
    }
    broken = [
        ({sender_env: None}, sender_env),
        ({base_env: None}, base_env),
        ({sender_env: "not-an-address"}, sender_env),
        ({base_env: "buyer.example/callback"}, base_env),
        ({smtp_env: "http://mail.example.net"}, smtp_env),
        ({smtp_env: "smtp://"}, smtp_env),
    ]

    for override, named in broken:
        for name, value in {**complete, **override}.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        with pytest.raises(MagicLinkTransportMisconfigured) as raised:
            routes_mod.build_auth_service()
        assert named in str(raised.value), (
            f"the boot failure for {override!r} does not name {named}: {raised.value}"
        )


# =======================================================================================
# The console transport — the third state, and the reason it is not a hole in the second
#
# The demo dead-ended. A person could open the devstack UI, type a query and answer the
# clarifying questions, and then stop: `Journey.tsx` keeps the confirm button OUT of the
# document until there is a session (`gateOnSignIn`), a session comes only from redeeming a
# mailed link, and no deployment in this tree could mail one — a workstation has no MTA, so
# `build_magic_link_delivery` returned `_undeliverable` and the route answered 503.
#
# The fix is NOT to make the unconfigured case print the token. `_drop`'s docstring is right
# and stays right: a service booted WITHOUT a transport must fail to log buyers in rather
# than publish bearer credentials to stdout. What was missing is a transport an operator can
# DELIBERATELY choose, and the tests here are all about that distinction:
#
#   * absence still fails closed, and prints nothing (the first two);
#   * the console is reachable only by naming it exactly (the third);
#   * having named it, the link really is printed and really opens a session (the fourth);
#   * and choosing it says so, loudly, at boot (the fifth).
# =======================================================================================

CONSOLE_BASE_URL = "http://127.0.0.1:8100/"


@pytest.fixture
def known_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make the next minted magic-link token a value this test can search output for.

    The service keeps only ``token_fingerprint(token)``, so a test cannot ask it what it
    issued — which is correct of the service and unhelpful here. Deciding the token in
    advance is what turns "no token appears to have been printed" into "THIS credential was
    not printed anywhere".
    """
    import secrets

    from buyer_svc.auth import magic_link as magic_link_mod

    token = "test-token-must-never-reach-stdout"
    monkeypatch.setattr(secrets, "token_urlsafe", lambda _bytes: token)
    assert magic_link_mod.secrets is secrets, "the module no longer mints through `secrets`"
    return token


def test_an_unconfigured_deployment_still_refuses_and_still_prints_no_token(
    clean_env: None,
    no_transport: None,
    fresh_process_state: Any,
    known_token: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Adding a third state must not soften the default. Unset is still 503, still silent.

    This is the regression that matters most about the console transport: the failure mode
    of "a dev convenience nobody selected" is a service that prints live sign-in tokens into
    a log aggregator because somebody forgot a variable. Nothing is set here, and both halves
    are asserted — the refusal on the wire, and the absence of the credential on stdout.
    """
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    routes_mod = fresh_process_state
    routes_mod.set_auth_service(routes_mod.build_auth_service())
    client = TestClient(create_app(), raise_server_exceptions=False)

    answered = client.post("/buyer/auth/magic-link", json={"email": DANA["email"]})
    assert answered.status_code == 503, answered.text

    printed = capsys.readouterr()
    stream = printed.out + printed.err
    assert known_token not in stream, (
        "a deployment with NO transport configured printed the sign-in token to its output; "
        "the fail-closed default has been weakened into a credential leak"
    )
    assert "token=" not in stream, f"something link-shaped reached the output: {stream!r}"
    assert known_token not in answered.text, answered.text


def test_the_console_transport_cannot_be_reached_by_accident(
    clean_env: None, no_transport: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo, a near miss, or a blank is a refusal — never a quiet promotion to stdout.

    Two different refusals, and the difference is whether the operator said anything. A value
    that is not a transport is a BOOT FAILURE that names what would have been accepted: it is
    a statement this service cannot honour, and honouring it as "the default" would mean a
    deployment reading `PROXYSHOP_BUYER_MAGIC_LINK_TRANSPORT=consoel` in its own manifest
    while mailing nothing and refusing every login. A blank is not a statement at all, so it
    falls through to the fail-closed default — which refuses too, just not by raising.
    """
    from buyer_svc.auth.delivery import MagicLinkTransportMisconfigured, MagicLinkUndeliverable

    routes_mod = fresh_process_state
    monkeypatch.setenv("PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL", CONSOLE_BASE_URL)

    for stated in ("Console", "CONSOLE", "consol", "console!", "stdout", "print", "true", "1"):
        monkeypatch.setenv(TRANSPORT_SELECT_ENV, stated)
        with pytest.raises(MagicLinkTransportMisconfigured) as raised:
            routes_mod.build_auth_service()
        assert TRANSPORT_SELECT_ENV in str(raised.value), (
            f"the refusal of {stated!r} does not name the variable: {raised.value}"
        )
        assert "console" in str(raised.value), (
            f"the refusal of {stated!r} does not say what it would have accepted: {raised.value}"
        )

    for blank in ("", "   "):
        monkeypatch.setenv(TRANSPORT_SELECT_ENV, blank)
        service = routes_mod.build_auth_service()
        with pytest.raises(MagicLinkUndeliverable):
            service.request_login(DANA["email"])


def test_the_console_transport_refuses_to_boot_without_the_origin_it_would_link_to(
    clean_env: None, no_transport: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A printed link with no front door is the same outage as a mailed one with none.

    The SPA reads its token off `?token=` at its own origin root, so the base URL is not
    decoration: without it there is nothing to print but a bare token, and with a wrong one
    the operator clicks into a 404. Refused at boot, in the same shape as the half-configured
    mail transport above.
    """
    from buyer_svc.auth.delivery import MagicLinkTransportMisconfigured

    routes_mod = fresh_process_state
    monkeypatch.setenv(TRANSPORT_SELECT_ENV, "console")

    for value in (None, "127.0.0.1:8100", "/auth/callback"):
        if value is None:
            monkeypatch.delenv("PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL", raising=False)
        else:
            monkeypatch.setenv("PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL", value)
        with pytest.raises(MagicLinkTransportMisconfigured) as raised:
            routes_mod.build_auth_service()
        assert "PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL" in str(raised.value), raised.value


def test_stating_smtp_with_no_mta_is_a_boot_failure_rather_than_a_silent_503(
    clean_env: None, no_transport: None, fresh_process_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator who named a transport gets told when there is nothing behind it.

    Unset means "nothing stated", and nothing stated is the deliberate 503. `smtp` means "I
    intend to mail", and a deployment that intends to mail and cannot is exactly the state
    T-361 is about — so it stops the process instead of serving refusals that look like a
    policy decision.
    """
    from buyer_svc.auth.delivery import MagicLinkTransportMisconfigured

    routes_mod = fresh_process_state
    monkeypatch.setenv(TRANSPORT_SELECT_ENV, "smtp")

    with pytest.raises(MagicLinkTransportMisconfigured) as raised:
        routes_mod.build_auth_service()
    assert "PROXYSHOP_BUYER_MAGIC_LINK_SMTP_URL" in str(raised.value), raised.value


def test_the_console_transport_announces_itself_at_boot(
    clean_env: None,
    no_transport: None,
    fresh_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Choosing it is a decision somebody can be held to, so the process says so out loud.

    An operator reading this service's start-up must be able to tell that sign-in tokens are
    going to stdout — that is the difference between a deliberate local convenience and a
    credential leak discovered later in a log file. Asserted on stdout rather than on the
    logger because the launcher this exists for configures uvicorn's loggers and not this
    package's, so a `_log.warning` alone is frequently swallowed.
    """
    routes_mod = fresh_process_state
    monkeypatch.setenv(TRANSPORT_SELECT_ENV, "console")
    monkeypatch.setenv("PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL", CONSOLE_BASE_URL)

    routes_mod.build_auth_service()

    announced = capsys.readouterr().out.casefold()
    assert announced.strip(), "the console transport was selected and said nothing at all"
    for fragment in (TRANSPORT_SELECT_ENV.casefold(), "console", "stdout", "credential"):
        assert fragment in announced, (
            f"the boot announcement never says {fragment!r}, so an operator reading the log "
            f"cannot tell this process is publishing sign-in tokens:\n{announced}"
        )


def test_the_console_transport_prints_a_link_that_opens_a_session(
    clean_env: None,
    no_transport: None,
    fresh_process_state: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The headline, and the whole reason this exists: the demo journey can be completed.

    Nothing here injects a `deliver`. The transport under test is the one
    `build_auth_service` chose from the environment, the token is scraped out of what the
    process actually printed — the same text a developer reads off their terminal — and it is
    then spent against the running app. That is what makes this evidence that the browser
    journey works rather than that the function was called.
    """
    from urllib.parse import parse_qs, urlsplit

    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    routes_mod = fresh_process_state
    monkeypatch.setenv(TRANSPORT_SELECT_ENV, "console")
    monkeypatch.setenv("PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL", CONSOLE_BASE_URL)

    routes_mod.set_auth_service(routes_mod.build_auth_service())
    client = TestClient(create_app(), raise_server_exceptions=False)
    capsys.readouterr()  # drop the boot announcement; the next read is the delivery alone

    requested = client.post("/buyer/auth/magic-link", json={"email": DANA["email"]})
    assert requested.status_code == 202, requested.text

    printed = capsys.readouterr().out
    links = [
        word.rstrip(".,")
        for word in printed.split()
        if word.startswith(CONSOLE_BASE_URL) and "token=" in word
    ]
    assert len(links) == 1, f"expected exactly one printed sign-in link, got {links!r}:\n{printed}"
    found = parse_qs(urlsplit(links[0]).query).get("token")
    assert found, f"the printed link carries no token: {links[0]}"
    token = found[0]

    assert token not in requested.text, (
        "the token is on the HTTP response as well as in the terminal; the console is no "
        "longer the only place the credential appears"
    )

    session = client.post("/buyer/auth/session", json={"token": token})
    assert session.status_code == 201, (
        f"the token printed to the console did not open a session ({session.status_code}): "
        f"{session.text}"
    )
    assert session.json()["pseudonym"].startswith("psn-")
    # Single use, exactly as the mailed one: printing it does not make it a reusable password.
    assert client.post("/buyer/auth/session", json={"token": token}).status_code == 401
