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
