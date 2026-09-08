"""``GET /buyer/store-window`` — the served reader for ``app.buyer_accounts`` (T-142).

Every test here drives the **mounted route** through :func:`buyer_svc.main.create_app`, never
:func:`~buyer_svc.window.routes.read_store_window` directly. That is the whole lesson of the
ticket this file closes: ``publish_profile`` was written, unit-tested and correct for months
while no request could reach it, and a suite that called the function would have been green
throughout.

What is doubled and what is not
-------------------------------
The **connection** is doubled; the route is not. A recorder installed over the handler could be
satisfied by a route that returns a constant, so what these tests assert is row-shaped: the
statement the route sends, and rows coming back out of a cursor the route had to open.
:func:`~buyer_svc.window.routes.set_window_connection` is the deployment seam that makes that
possible without a live Postgres; the ``docker``-marked tests at the bottom use a real one.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
from collections.abc import Iterator
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

#: The served window package. Nothing under here may know what a seeded row is.
WINDOW_SRC = REPO_ROOT / "apps" / "buyer" / "svc" / "src" / "window"

#: The migration carrying the constraint the marker rests on.
PROVENANCE_MIGRATION = REPO_ROOT / "db" / "migrations" / "0005_buyer_accounts_provenance.sql"

#: A token table with two stores in it, so ``_store_for``'s loop has more than one row to walk.
TOKENS = {
    "store-brightbean": "wtok-brightbean-3f9c2a71d0e64b58",
    "store-gaiaherbs": "wtok-gaiaherbs-71bd0c4ea9f21836",
}

#: Five rows in one class, two in another, one alone. At the default floor of 2 the singleton
#: is the only thing withheld; at 3 the pair goes with it. Written out rather than generated,
#: because the arithmetic of the floor is the thing under test and a generator would have to
#: repeat it to know what to expect.
_COHORT_A = {
    "budget_band": "50-100",
    "category_affinity": ["trail-runners", "wool-socks"],
    "frequency_tier": "regular",
    "region": "US-OR",
    "first_time": False,
}
_COHORT_B = {
    "budget_band": "1000+",
    "category_affinity": ["camera-lenses"],
    "frequency_tier": "occasional",
    "region": "GB",
    "first_time": False,
}
_ALONE = {
    "budget_band": "0-50",
    "category_affinity": ["board-games"],
    "frequency_tier": "frequent",
    "region": "DE-BE",
    "first_time": False,
}


def _rows() -> list[tuple[str, dict[str, Any], str]]:
    rows: list[tuple[str, dict[str, Any], str]] = []
    for index in range(5):
        rows.append((f"psn-seed-{index:024x}", dict(_COHORT_A), "seed"))
    for index in range(2):
        rows.append((f"psn-{index:032x}", dict(_COHORT_B), "live"))
    rows.append((f"psn-{99:032x}", dict(_ALONE), "live"))
    return sorted(rows)


class _Cursor:
    """Records statements and answers the one the route sends."""

    def __init__(self, log: list[str], rows: list[tuple[str, dict[str, Any], str]]) -> None:
        self._log = log
        self._rows = rows

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def execute(self, statement: Any, params: Any = None) -> _Cursor:
        self._log.append(str(statement))
        return self

    def fetchall(self) -> list[tuple[str, dict[str, Any], str]]:
        return list(self._rows)

    def close(self) -> None:
        return None


class _Connection:
    """A stand-in for the app-role connection, and nothing more."""

    closed = False

    def __init__(self, rows: list[tuple[str, dict[str, Any], str]] | None = None) -> None:
        self.statements: list[str] = []
        self.rows = _rows() if rows is None else rows

    def cursor(self, *_a: Any, **_k: Any) -> _Cursor:
        return _Cursor(self.statements, self.rows)


class _Exploding(_Connection):
    """A connection whose cursor raises — the "the window could not be read" case."""

    def cursor(self, *_a: Any, **_k: Any) -> Any:
        raise RuntimeError("the database went away mid-read")


@pytest.fixture
def tokens_file(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Point the route at a real ``{store_id: token}`` file on disk."""
    from buyer_svc.window.routes import WINDOW_TOKENS_ENV

    path = tmp_path / "window-tokens.json"
    path.write_text(json.dumps(TOKENS), encoding="utf-8")
    monkeypatch.setenv(WINDOW_TOKENS_ENV, str(path))
    return path


@pytest.fixture
def window_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The real application, with the window's process-wide connection slot cleaned up after.

    The slot is module state by design (one connection per process, not one per request), so
    it is restored here rather than left for the next test to inherit — the same discipline
    ``conftest._process_wide_wiring_seams`` applies to the exchange's seams.
    """
    from buyer_svc.main import create_app
    from buyer_svc.window import routes as window_routes

    monkeypatch.delenv("PROXYSHOP_BUYER_STORE_WINDOW_K", raising=False)
    try:
        yield create_app()
    finally:
        window_routes.set_window_connection(None)


def _client(app: Any) -> Any:
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


def _get(app: Any, **headers: str) -> Any:
    from buyer_svc.window.routes import STORE_WINDOW_PATH

    return _client(app).get(f"/buyer{STORE_WINDOW_PATH}", headers=headers)


# ======================================================================================
# the door
# ======================================================================================


def test_the_window_route_is_mounted_and_reachable(window_app: Any) -> None:
    """The route is on the served app, not merely defined in a module.

    T-142's first half was code with no door for months. This asserts the door exists before
    anything below asserts what is behind it: every other test in this file would report
    "unauthorized" for a route that was never mounted, which reads exactly like a refusal.
    """
    from buyer_svc.window.routes import STORE_WINDOW_PATH

    # Read from the OpenAPI document rather than by walking `app.routes`. The installed
    # FastAPI wraps an included router in an opaque `_IncludedRouter` with no `path`, so a
    # walk of the top level finds four docs routes and nothing else — a sweep that would have
    # reported "not mounted" for every feature in this service, including the ones that work.
    # The document is also what a caller actually sees.
    served = set(window_app.openapi()["paths"])
    assert f"/buyer{STORE_WINDOW_PATH}" in served, (
        f"the window router is not mounted; the app serves {sorted(served)}. "
        "buyer_svc.main.create_app globs `*/routes.py` and skips a module exporting no "
        "`router`, so a half-landed feature 404s rather than failing to boot"
    )


def test_an_unconfigured_deployment_serves_the_window_to_nobody(
    window_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No token table -> 503, and the refusal names the variable that would fix it.

    The direction is the point. An empty expected value compared against an empty supplied one
    is a route that opens itself the moment the environment is incomplete, which is how a
    window onto every buyer in the system ends up unauthenticated in a staging deployment.
    """
    from buyer_svc.window.routes import WINDOW_TOKENS_ENV

    monkeypatch.delenv(WINDOW_TOKENS_ENV, raising=False)
    response = _get(window_app, Authorization="Bearer anything-at-all")
    assert response.status_code == 503, response.text
    assert response.json()["error"] == "store-window-not-configured"
    assert WINDOW_TOKENS_ENV in response.json()["detail"]


def test_a_file_that_cannot_be_read_serves_nobody_rather_than_everybody(
    window_app: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named-but-unusable token file is a misconfiguration, and it fails closed."""
    from buyer_svc.window.routes import WINDOW_TOKENS_ENV

    broken = tmp_path / "not-json.json"
    broken.write_text("{this is not json", encoding="utf-8")
    monkeypatch.setenv(WINDOW_TOKENS_ENV, str(broken))
    assert _get(window_app, Authorization="Bearer anything").status_code == 503

    monkeypatch.setenv(WINDOW_TOKENS_ENV, str(tmp_path / "does-not-exist.json"))
    assert _get(window_app, Authorization="Bearer anything").status_code == 503


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="nothing-presented"),
        pytest.param({"Authorization": "Bearer wrong"}, id="a-wrong-token"),
        pytest.param(
            {"Authorization": "Basic wtok-brightbean-3f9c2a71d0e64b58"}, id="wrong-scheme"
        ),
        pytest.param({"Authorization": "Bearer "}, id="an-empty-bearer"),
        # NEAR MISSES. Every case above shares no prefix with a real token, so all of them are
        # satisfied by `str(token).startswith(bearer)` — MEASURED: swapping
        # `hmac.compare_digest` for `startswith` left this whole file green while
        # `Authorization: Bearer w` opened the entire window as `store-brightbean`. The three
        # below are the ones that fail a comparison which is not equality.
        pytest.param({"Authorization": "Bearer w"}, id="a-one-character-prefix"),
        pytest.param(
            {"Authorization": "Bearer wtok-brightbean-3f9c2a71d0e64b5"}, id="one-char-short"
        ),
        pytest.param(
            {"Authorization": "Bearer wtok-brightbean-3f9c2a71d0e64b58x"}, id="one-char-long"
        ),
        pytest.param({"Authorization": "Bearer tok-brightbean-3f9c2a71d0e64b58"}, id="suffix"),
    ],
)
def test_the_window_refuses_every_caller_it_cannot_name(
    window_app: Any, tokens_file: pathlib.Path, headers: dict[str, str]
) -> None:
    """401, and the body echoes nothing that arrived.

    The empty-bearer case is not padding: ``window_tokens`` drops empty tokens from the table
    precisely so that there is no row an empty presented string could compare equal to.
    """
    response = _get(window_app, **headers)
    assert response.status_code == 401, response.text
    assert response.json() == {"error": "unauthorized"}
    assert "wtok" not in response.text, "the refusal echoed the presented bearer"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"Bearer wtok-brightbean-3f9c2a71d0e64b5\xe9", id="one-latin1-byte"),
        pytest.param("Bearer wtok-brightbeän".encode(), id="utf-8-umlaut"),
        pytest.param(b"Bearer \xff\xfe\x00", id="bytes-that-are-not-text-at-all"),
        pytest.param("Bearer \ud800".encode("utf-8", "surrogatepass"), id="a-lone-surrogate"),
        pytest.param(b"Bearer \xe9" * 400, id="long-and-non-ascii"),
    ],
)
def test_a_non_ascii_bearer_is_refused_and_not_a_500(
    window_app: Any, tokens_file: pathlib.Path, raw: bytes
) -> None:
    """One non-ASCII byte in ``Authorization`` must be a 401, never an unhandled 500.

    MEASURED before the fix, on this exact route, from an unauthenticated caller::

        Authorization: Bearer tok-alph\\xe9
        -> 500 Internal Server Error
        TypeError: comparing strings with non-ASCII characters is not supported
          File ".../buyer_svc/window/routes.py", in _store_for
            if hmac.compare_digest(str(token), bearer):

    ``hmac.compare_digest`` takes ``str`` only when BOTH sides are ASCII-only, and the
    presented side comes from the network. This route releases the buyer population, so a
    caller who cannot be named must get the same flat refusal whatever they send, and the
    service must not answer a credential probe with a traceback.

    The header is passed as **bytes**, which is what an HTTP header is: a ``str`` carrying
    ``\\xe9`` never leaves the test client (httpx encodes header values as ASCII and raises),
    so a test written with ``str`` headers cannot reach this defect at all — it fails in the
    client, in the test's own process, and never serves a request.
    """
    from buyer_svc.window.routes import STORE_WINDOW_PATH

    response = _client(window_app).get(f"/buyer{STORE_WINDOW_PATH}", headers={"Authorization": raw})
    assert response.status_code == 401, response.text
    assert response.json() == {"error": "unauthorized"}
    assert "wtok" not in response.text, "the refusal echoed the presented bearer"


def test_a_non_ascii_token_in_the_table_is_dropped_and_the_store_is_named(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A token no bearer could ever equal is refused at load, naming the store and not the token.

    The comparison is on encoded bytes and the presented side has been through ASGI's latin-1
    header decode, so a non-ASCII token cannot match itself over HTTP. Keeping the row would
    leave an operator with a configured store that silently authenticates nobody. The warning
    must name the store id and must NOT name the token, for the reason the 401 body echoes
    nothing: a log line is as much a place a credential lives as a database is.
    """
    import logging as _logging

    from buyer_svc.window.routes import WINDOW_TOKENS_ENV, window_tokens

    path = tmp_path / "tokens.json"
    path.write_text(
        json.dumps({"store-ascii": "wtok-fine", "store-umlaut": "wtok-brightbeän"}),
        encoding="utf-8",
    )
    monkeypatch.setenv(WINDOW_TOKENS_ENV, str(path))

    with caplog.at_level(_logging.WARNING, logger="buyer_svc.window.routes"):
        table = window_tokens()

    assert table == {"store-ascii": "wtok-fine"}, (
        "a non-ASCII token was kept in the table; it can never equal a presented bearer, so "
        "the store it belongs to would be unable to authenticate with no line saying why"
    )
    assert "store-umlaut" in caplog.text, "the dropped row's store was not named"
    assert "brightbeän" not in caplog.text, "the warning put the token itself in the log"


def test_a_buyer_session_is_not_a_window_credential(
    window_app: Any, tokens_file: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REAL, live ``X-Buyer-Session`` gets 401 from this route.

    The authority that must not be widened. ``GET /buyer/profile`` lets a buyer read their own
    row; if that same header opened the window, one buyer's credential would read every
    buyer's. The session below is genuine — minted by the production login path in this
    process — so this is not "an unknown header is ignored", it is "the right credential for
    the wrong door".
    """
    from buyer_svc.auth import routes as auth_routes

    auth_routes.set_auth_service(None)
    try:
        service = auth_routes.build_auth_service()
        auth_routes.set_auth_service(service)
        captured: dict[str, str] = {}
        service.deliver = lambda email, token, expires_at: captured.update({email: token})

        client = _client(window_app)
        assert (
            client.post("/buyer/auth/magic-link", json={"email": "ada@example.com"}).status_code
            == 202
        )
        opened = client.post("/buyer/auth/session", json={"token": captured["ada@example.com"]})
        assert opened.status_code == 201, opened.text
        session_id = opened.json()["session_id"]

        # The header works on the buyer's own route ...
        assert (
            client.get("/buyer/profile", headers={"X-Buyer-Session": session_id}).status_code == 200
        )
        # ... and buys nothing here.
        refused = _get(window_app, **{"X-Buyer-Session": session_id})
        assert refused.status_code == 401, (
            f"a live buyer session opened the store window ({refused.status_code}); a "
            "credential that reads one buyer must not read the population"
        )
    finally:
        auth_routes.set_auth_service(None)


# ======================================================================================
# what comes back
# ======================================================================================


def test_the_served_window_reads_the_table_and_holds_the_release_to_a_floor(
    window_app: Any, tokens_file: pathlib.Path
) -> None:
    """The whole route, end to end: a statement against the table, and a coarsened release."""
    from buyer_svc.window.routes import DEFAULT_WINDOW_FLOOR, set_window_connection

    connection = _Connection()
    set_window_connection(connection)
    response = _get(window_app, Authorization=f"Bearer {TOKENS['store-gaiaherbs']}")

    assert response.status_code == 200, response.text
    assert connection.statements, "the route answered 200 without opening a cursor"
    statement = connection.statements[0].lower()
    assert "app.buyer_accounts" in statement, (
        f"the route read something other than the store-visible table: {statement!r}"
    )

    body = response.json()
    assert body["store_id"] == "store-gaiaherbs", "the store is resolved from the bearer"
    assert body["floor"] == DEFAULT_WINDOW_FLOOR
    # Five in one class and two in another are released; the singleton is not.
    assert len(body["released"]) == 7, body["released"]
    assert body["withheld"] == 1, (
        "the buyer whose bucket combination is unique in this release was served to a store; "
        "uniqueness is a property of the release and no per-row coarsening fixes it"
    )
    assert body["truncated"] is False


def test_the_floor_is_a_deployment_knob_and_cannot_be_turned_below_two(
    window_app: Any, tokens_file: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raising the floor withholds more; asking for 1 does not disable the floor.

    A knob that can be set to "no protection at all" is not a knob, it is an off switch, and
    the population read is the one release where a floor of 1 means nothing is enforced.
    """
    from buyer_svc.window.routes import (
        DEFAULT_WINDOW_FLOOR,
        WINDOW_FLOOR_ENV,
        set_window_connection,
    )

    set_window_connection(_Connection())

    monkeypatch.setenv(WINDOW_FLOOR_ENV, "3")
    raised = _get(window_app, Authorization=f"Bearer {TOKENS['store-gaiaherbs']}").json()
    assert raised["floor"] == 3
    assert len(raised["released"]) == 5, "the class of two survived a floor of three"
    assert raised["withheld"] == 3

    for hostile in ("1", "0", "-4", "banana", ""):
        monkeypatch.setenv(WINDOW_FLOOR_ENV, hostile)
        lowered = _get(window_app, Authorization=f"Bearer {TOKENS['store-gaiaherbs']}").json()
        assert lowered["floor"] == DEFAULT_WINDOW_FLOOR, (
            f"{WINDOW_FLOOR_ENV}={hostile!r} pushed the floor to {lowered['floor']}; the knob "
            "raises the floor and never lowers it"
        )


def test_nothing_but_the_buyer_profile_leaves(window_app: Any, tokens_file: pathlib.Path) -> None:
    """Each released row is ``{pseudonym, buckets, provenance}`` and the buckets are the five.

    ``created_at`` is the field this is really about. It is not a bucket and it is not
    coarsened by anything: a row's creation instant is a handle onto whichever login happened
    at that moment, so it is a join key wearing a timestamp's clothes. The statement does not
    select it, and the response cannot carry it.
    """
    from buyer_svc.profile import BUCKET_KEYS
    from buyer_svc.window.routes import set_window_connection

    connection = _Connection()
    set_window_connection(connection)
    body = _get(window_app, Authorization=f"Bearer {TOKENS['store-brightbean']}").json()

    assert "created_at" not in connection.statements[0].lower(), (
        f"the window's statement selects created_at: {connection.statements[0]!r}"
    )
    for row in body["released"]:
        assert set(row) == {"pseudonym", "buckets", "provenance"}, row
        assert set(row["buckets"]) == set(BUCKET_KEYS), row["buckets"]
    assert "created_at" not in json.dumps(body)


def test_an_unread_window_is_never_served_as_an_empty_one(
    window_app: Any, tokens_file: pathlib.Path
) -> None:
    """A failed read is a 500, not a 200 with no rows.

    A store cannot tell "this network has no buyers" from "this route could not reach its
    database", and serving the second as the first is a false statement about the population.
    """
    from buyer_svc.window.routes import set_window_connection

    set_window_connection(_Exploding())
    response = _get(window_app, Authorization=f"Bearer {TOKENS['store-brightbean']}")
    assert response.status_code == 500, response.text
    assert response.json()["error"] == "store-window-unreadable"


def test_a_database_less_boot_answers_503_rather_than_crashing(
    window_app: Any, tokens_file: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No app DSN and no bound connection -> a refusal that names the variable."""
    from buyer_svc.auth.routes import APP_DSN_ENV
    from buyer_svc.window.routes import set_window_connection

    set_window_connection(None)
    monkeypatch.delenv(APP_DSN_ENV, raising=False)
    response = _get(window_app, Authorization=f"Bearer {TOKENS['store-brightbean']}")
    assert response.status_code == 503, response.text
    assert response.json()["error"] == "store-window-has-no-database"


# ======================================================================================
# seeded and real, distinguishable — from the served response alone
# ======================================================================================


def test_the_column_and_the_key_agree_on_every_released_row(
    window_app: Any, tokens_file: pathlib.Path
) -> None:
    """Two independent witnesses classify each row, and they never disagree.

    The route copies the ``provenance`` column. :func:`apps.buyer.seed.chain.is_seeded_pseudonym`
    reads the PRIMARY KEY and ignores the column. The database's CHECK is what makes them
    agree; this asserts the agreement from the served bytes, which is the only place a store
    can check it.
    """
    from buyer_svc.window.routes import set_window_connection

    from apps.buyer.seed.chain import is_seeded_pseudonym

    set_window_connection(_Connection())
    body = _get(window_app, Authorization=f"Bearer {TOKENS['store-brightbean']}").json()

    disagreements = [
        row
        for row in body["released"]
        if is_seeded_pseudonym(row["pseudonym"]) != (row["provenance"] == "seed")
    ]
    assert not disagreements, (
        f"{len(disagreements)} released row(s) carry a provenance label their pseudonym "
        f"contradicts: {disagreements[:3]}"
    )
    assert {row["provenance"] for row in body["released"]} == {"seed", "live"}, (
        "this fixture holds both kinds on purpose; a response carrying only one would make "
        "the agreement above vacuous"
    )


def test_the_seed_marker_the_database_enforces_is_the_one_python_mints() -> None:
    """The CHECK's literal and :data:`SEED_PSEUDONYM_PREFIX` are the same string.

    The marker lives in two languages, and neither can see the other. A rename on the Python
    side alone leaves a constraint that passes for every row and a reader that classifies every
    row ``'live'`` — a silent, total failure of the one guarantee this table makes.
    """
    from apps.buyer.seed.chain import SEED_PSEUDONYM_PREFIX

    sql = PROVENANCE_MIGRATION.read_text(encoding="utf-8")
    found = re.findall(r"pseudonym LIKE '([^']+)'", sql)
    assert found, (
        f"{PROVENANCE_MIGRATION.name} carries no `pseudonym LIKE '...'` clause, so nothing in "
        "the database ties the provenance label to the key"
    )
    assert set(found) == {f"{SEED_PSEUDONYM_PREFIX}%"}, (
        f"the migration matches {sorted(set(found))} and Python mints "
        f"{SEED_PSEUDONYM_PREFIX!r}; the two spellings have drifted"
    )


def test_no_vault_pseudonym_can_ever_land_in_the_seeded_namespace() -> None:
    """The vault's own generator, drawn many times, never produces a seed-shaped handle.

    Not a probabilistic argument dressed up as a test: ``default_pseudonym`` is
    ``PSEUDONYM_PREFIX + secrets.token_hex(16)`` and ``token_hex`` emits ``0-9a-f`` only, in
    which ``s`` does not appear — so the namespaces are disjoint by alphabet, and the draw
    below is the demonstration rather than the proof. The alphabet claim is asserted too, so
    that swapping the generator for one with a wider alphabet fails here rather than silently
    making every future login forgeable.
    """
    from buyer_svc.vault import PSEUDONYM_PREFIX, default_pseudonym

    from apps.buyer.seed.chain import SEED_PSEUDONYM_PREFIX, is_seeded_pseudonym

    drawn = [default_pseudonym() for _ in range(2000)]
    assert len({*drawn}) == len(drawn), "the vault generator repeated itself"
    assert not [value for value in drawn if is_seeded_pseudonym(value)]
    bodies = "".join(value.removeprefix(PSEUDONYM_PREFIX) for value in drawn)
    assert set(bodies) <= set("0123456789abcdef"), (
        f"the vault generator emitted characters outside hex: {sorted(set(bodies) - set('0123456789abcdef'))}. "
        f"The seed namespace {SEED_PSEUDONYM_PREFIX!r} is reserved by the alphabet, and a "
        "wider one stops reserving it"
    )


# ======================================================================================
# the live switch: the served route has no notion of seeding
# ======================================================================================


def _executable_vocabulary(path: pathlib.Path) -> set[str]:
    """Every identifier, import and non-docstring string literal in ``path``.

    Docstrings and comments are excluded deliberately, and this is the difference between this
    check and the grep ``services/sim/tests/test_seed_store.py`` runs on the feedback package.
    That directory contains no mention of simulation at all, so a grep is exact there. This one
    *documents* seeded rows at length — a reader of this route needs to know why the column
    exists — so the claim being made is narrower and has to be measured more precisely: the
    route's **behaviour** has no notion of seeding. No branch on it, no literal ``'seed'``, no
    import of the producer.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    vocabulary: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                vocabulary.add(node.value)
        elif isinstance(node, ast.Name):
            vocabulary.add(node.id)
        elif isinstance(node, ast.Attribute):
            vocabulary.add(node.attr)
        elif isinstance(node, ast.arg):
            vocabulary.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            vocabulary.add(node.name)
        elif isinstance(node, ast.Import):
            vocabulary.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            vocabulary.add(node.module or "")
            vocabulary.update(alias.name for alias in node.names)
    return vocabulary


def test_the_served_window_has_no_notion_of_seeding() -> None:
    """Nothing the window *executes* mentions seeding, simulation, or the producer package.

    This is what makes "stop loading the corpus" the whole of going live. A single branch on
    the caller — a header, a prefix check, a debug flag — would mean the seeded path and the
    real path are two paths, and everything proven about one would stop being evidence about
    the other. The route selects a column and copies it; the database is what makes the value
    true.
    """
    modules = sorted(WINDOW_SRC.glob("*.py"))
    assert modules, f"no modules under {WINDOW_SRC}; this sweep would pass over an empty set"

    offences: list[str] = []
    for module in modules:
        for word in _executable_vocabulary(module):
            lowered = word.lower()
            if "seed" in lowered or "simulat" in lowered:
                offences.append(f"{module.name}: {word!r}")
    assert not offences, (
        "the served window's executable code knows what a seeded row is: "
        f"{offences}. It must select the provenance column and copy it, with no branch on the "
        "value; db/migrations/0005 is what makes the value trustworthy, not this route"
    )


def test_the_served_service_never_imports_the_seed_producer() -> None:
    """No module under ``apps/buyer/svc/src`` imports :mod:`apps.buyer.seed`.

    The producer is a script beside the service, not a part of it — it is outside the image's
    ``COPY`` set and the import edge runs one way, ``apps.buyer.seed`` -> ``buyer_svc``. An
    import in the other direction would put the generator inside the deployable and make
    "which code is running in production" a question with two answers.
    """
    served = REPO_ROOT / "apps" / "buyer" / "svc" / "src"
    importers: list[str] = []
    for module in sorted(served.rglob("*.py")):
        if "__pycache__" in module.parts:
            continue
        source = module.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a module that will not parse imports nothing
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any(name.startswith("apps.buyer.seed") for name in names):
                importers.append(f"{module.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not importers, f"the served service imports the seed producer at {importers}"


# ======================================================================================
# against a real database — the constraint, and the denial that makes the window safe
# ======================================================================================


@pytest.mark.docker
def test_the_database_refuses_both_forgeries(vault_migrated: str, pg_admin: Any) -> None:
    """A live row cannot be labelled seeded, and a seeded row cannot be labelled live.

    Driven against a real Postgres because the guarantee IS the constraint. A Python-side
    assertion here would be testing the test: the whole argument for putting the marker in the
    key is that no application code has to remember to check it.
    """
    import psycopg
    from buyer_svc.vault import default_pseudonym

    from apps.buyer.seed.chain import SEED_PSEUDONYM_PREFIX

    live = default_pseudonym()
    seeded = f"{SEED_PSEUDONYM_PREFIX}{'0' * 24}"
    try:
        with pg_admin.cursor() as cur:
            cur.execute(
                "insert into app.buyer_accounts (pseudonym, buckets, provenance) "
                "values (%s, '{}'::jsonb, 'live')",
                (live,),
            )
            cur.execute(
                "insert into app.buyer_accounts (pseudonym, buckets, provenance) "
                "values (%s, '{}'::jsonb, 'seed')",
                (seeded,),
            )

        for label, statement, params in [
            (
                "labelling a real buyer's row as seeded",
                "update app.buyer_accounts set provenance = 'seed' where pseudonym = %s",
                (live,),
            ),
            (
                "un-marking a seeded row",
                "update app.buyer_accounts set provenance = 'live' where pseudonym = %s",
                (seeded,),
            ),
            (
                "inserting an unlabelled row into the seeded namespace",
                "insert into app.buyer_accounts (pseudonym, buckets) values (%s, '{}'::jsonb)",
                (f"{SEED_PSEUDONYM_PREFIX}{'1' * 24}",),
            ),
        ]:
            with pytest.raises(psycopg.errors.CheckViolation) as raised:
                with pg_admin.cursor() as cur:
                    cur.execute(statement, params)
            pg_admin.rollback()
            assert "buyer_accounts_provenance_is_the_key" in str(raised.value), label
    finally:
        with pg_admin.cursor() as cur:
            cur.execute(
                "delete from app.buyer_accounts where pseudonym = any(%s)",
                ([live, seeded, f"{SEED_PSEUDONYM_PREFIX}{'1' * 24}"],),
            )


@pytest.mark.docker
def test_the_role_the_window_reads_under_cannot_resolve_a_pseudonym(
    vault_scratch: Any, vault_roles: Any
) -> None:
    """The window's own role reads the buckets and is denied the mapping.

    This is what makes the route's "no identity leaves" claim a property of the grant model
    rather than of the SQL somebody wrote. There is no statement this handler could be edited
    into that returns an email: the join is refused at the schema.
    """
    from buyer_svc.profile import build_profile, publish_profile

    pseudonym = vault_scratch.pseudonym("window")
    profile = build_profile({"region": "US-OR", "orders": []}, pseudonym)

    app_connection = vault_roles.connection("app")
    try:
        publish_profile(app_connection, profile)
        app_connection.commit()
    finally:
        app_connection.rollback()

    served = vault_roles.fetch(
        "app",
        "select pseudonym, buckets, provenance from app.buyer_accounts where pseudonym = %s",
        (pseudonym,),
    )
    assert len(served) == 1, "the window's role cannot read the row it is meant to serve"
    assert served[0][2] == "live", "a row written by the login path is not labelled live"

    vault_roles.denied(
        "app",
        "select email from vault.pseudonym_history where pseudonym = %s",
        (pseudonym,),
    )
    vault_roles.denied(
        "app",
        "select a.pseudonym, v.email from app.buyer_accounts a "
        "join vault.pseudonym_history v using (pseudonym) where a.pseudonym = %s",
        (pseudonym,),
    )


@pytest.mark.docker
def test_the_corpus_loads_into_the_table_and_the_window_serves_it(
    vault_migrated: str, pg_admin: Any
) -> None:
    """The committed corpus reaches ``app.buyer_accounts`` and comes back out of the route.

    The full loop over a real database: load the artefact, read it back through the mounted
    handler's own release path, and check that every seeded row the store is shown is one of
    the rows the chain pins. A corpus that loaded and a window that served would each pass
    alone; only together do they say the demo is real.
    """
    from buyer_svc.window.routes import DEFAULT_WINDOW_FLOOR, release

    from apps.buyer.seed.chain import is_seeded_pseudonym
    from apps.buyer.seed.store import load

    corpus = load()
    pinned = {row["pseudonym"]: row["buckets"] for row in corpus.rows}
    try:
        with pg_admin.cursor() as cur:
            for row in corpus.rows:
                cur.execute(
                    "insert into app.buyer_accounts (pseudonym, buckets, provenance) "
                    "values (%s, %s::jsonb, 'seed') on conflict (pseudonym) do update set "
                    "buckets = excluded.buckets, provenance = 'seed'",
                    (row["pseudonym"], json.dumps(row["buckets"], sort_keys=True)),
                )
            cur.execute(
                "select pseudonym, buckets, provenance from app.buyer_accounts "
                "where provenance = 'seed' order by pseudonym"
            )
            fetched = [
                {"pseudonym": p, "buckets": b, "provenance": v} for p, b, v in cur.fetchall()
            ]

        released, withheld = release(fetched, floor=DEFAULT_WINDOW_FLOOR)
        assert len(released) == len(corpus.rows), (
            f"{withheld} seeded row(s) were withheld at the default floor of "
            f"{DEFAULT_WINDOW_FLOOR}; the corpus's cohorts are sized so that none is"
        )
        for row in released:
            assert is_seeded_pseudonym(row["pseudonym"])
            assert row["pseudonym"] in pinned, (
                f"{row['pseudonym']} was served as seeded and is not in the chain-verified "
                "corpus; the table holds a seeded row this repository did not commit"
            )
            assert row["buckets"] == pinned[row["pseudonym"]]
    finally:
        with pg_admin.cursor() as cur:
            cur.execute("delete from app.buyer_accounts where provenance = 'seed'")


def test_the_window_modules_doctests_are_true() -> None:
    """The examples in the source say what the code does.

    The repo's pytest configuration does not pass ``--doctest-modules``, so a doctest anywhere
    in this tree is prose that nobody executes — and a worked example that has quietly stopped
    being true is worse than none, because it is the first thing a reader trusts.
    ``window_floor``'s examples are the clamping contract in four lines; this runs them.
    """
    import doctest

    from buyer_svc.window import routes as window_routes

    results = doctest.testmod(window_routes, verbose=False)
    assert results.attempted, (
        "no doctest ran, so this test would stay green over a module whose examples were all "
        "deleted"
    )
    assert results.failed == 0, f"{results.failed} of {results.attempted} doctests failed"


# ======================================================================================
# the floor counts what leaves, and only what leaves
# ======================================================================================


def test_the_released_buckets_are_the_ones_the_floor_counted() -> None:
    """A value the class reader normalises away must not travel on the row beside it.

    The defect this pins, MEASURED before the fix: ``equivalence_class`` maps a non-list
    ``category_affinity`` to ``()``, so a row carrying the bare string
    ``"dana-reyes-portland-97205"`` and a row carrying nothing grouped as one class of two —
    and the release then emitted each row's OWN buckets, putting a unique quasi-identifier on
    the wire at floor 2 with ``withheld: 0``. Grouping by one projection and releasing another
    is the whole bug, and it is invisible to any test that only counts rows.
    """
    from buyer_svc.window.routes import release

    base = {
        "budget_band": "50-100",
        "frequency_tier": "regular",
        "region": "US-OR",
        "first_time": False,
    }
    rows = [
        {
            "pseudonym": "psn-aaaa",
            "buckets": {**base, "category_affinity": "dana-reyes-portland-97205"},
            "provenance": "live",
        },
        {"pseudonym": "psn-bbbb", "buckets": dict(base), "provenance": "live"},
    ]
    released, withheld = release(rows, floor=2)

    assert withheld == 0 and len(released) == 2
    leaked = [row for row in released if "dana" in json.dumps(row)]
    assert not leaked, (
        f"a free-text value the floor did not count reached a store: {leaked}. The released "
        "buckets must BE the equivalence class, not a second projection of the same row"
    )
    assert released[0]["buckets"] == released[1]["buckets"], (
        "two rows counted as one anonymity class were released with different buckets, so the "
        f"class size the floor checked was never true of the response: {released}"
    )


def test_a_row_whose_class_cannot_be_computed_is_withheld_not_served_and_not_a_500() -> None:
    """An unclassifiable row removes itself from the window rather than taking it down.

    ``category_affinity: [{"a": 1}]`` makes ``equivalence_class`` return an unhashable tuple.
    Before the guard that was an unhandled ``TypeError`` inside the handler — one malformed row
    turning the whole window into a 500. Withholding is the only safe answer: a row whose
    anonymity cannot be established has not been shown to have any.
    """
    from buyer_svc.window.routes import release

    base = {
        "budget_band": "50-100",
        "frequency_tier": "regular",
        "region": "US-OR",
        "first_time": False,
    }
    rows = [
        {"pseudonym": "psn-aaaa", "buckets": dict(base), "provenance": "live"},
        {"pseudonym": "psn-bbbb", "buckets": dict(base), "provenance": "live"},
        {
            "pseudonym": "psn-cccc",
            "buckets": {**base, "category_affinity": [{"a": 1}]},
            "provenance": "live",
        },
        {"pseudonym": "psn-dddd", "buckets": "not an object at all", "provenance": "live"},
    ]
    released, withheld = release(rows, floor=2)

    assert {row["pseudonym"] for row in released} == {"psn-aaaa", "psn-bbbb"}
    assert withheld == 2, f"expected both unclassifiable rows withheld, got {withheld}"


def test_the_class_tuple_and_the_bucket_keys_are_in_the_same_order() -> None:
    """``release`` rebuilds the released buckets by zipping BUCKET_KEYS onto the class tuple.

    That is only correct while the two agree on order, and nothing in the profile module
    promises they will: ``equivalence_class`` builds its tuple by hand. If the orders ever
    diverge the window would serve every buyer's facets silently transposed — a store would
    read a region as a budget band and nothing would raise. So the agreement is pinned here
    rather than assumed at the call site.
    """
    from buyer_svc.profile import BUCKET_KEYS, equivalence_class

    buckets = {
        "budget_band": "100-250",
        "category_affinity": ["camera-lenses", "tripods"],
        "frequency_tier": "occasional",
        "region": "GB",
        "first_time": False,
    }
    rebuilt = dict(zip(BUCKET_KEYS, equivalence_class(buckets), strict=True))
    rebuilt["category_affinity"] = list(rebuilt["category_affinity"])
    assert rebuilt == buckets, (
        "equivalence_class no longer emits its facets in BUCKET_KEYS order, so "
        "buyer_svc.window.routes.release would transpose them on every released row"
    )


def test_the_bearer_is_compared_against_every_row_and_never_by_prefix() -> None:
    """Two properties of ``_store_for``, and neither was graded before.

    * **Equality, not containment.** ``startswith``, ``in`` and ``==`` all satisfy a suite whose
      only wrong token shares no prefix with a real one. Asserted here directly against a table
      whose tokens are prefixes of each other, which no comparison but equality survives.
    * **Every row is walked.** Stopping at the first match makes response time a function of
      where in the table a token sits, which is a store-ordering oracle. The module docstring
      claims the loop is exhaustive; before this, nothing measured it. The counting mapping
      below fails the day somebody adds an early ``return``.
    """
    from buyer_svc.window.routes import _store_for

    class _CountingTokens(dict):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.walks = 0

        def items(self) -> Any:
            self.walks += 1
            for pair in list(super().items()):
                yield pair
                self.walks += 1

    tokens = _CountingTokens({"store-a": "tok", "store-b": "token", "store-c": "token-and-more"})
    assert _store_for("token", tokens) == "store-b"
    assert tokens.walks == len(tokens) + 1, (
        f"the comparison stopped after {tokens.walks - 1} of {len(tokens)} rows; response time "
        "then says where in the table a presented token sits"
    )

    for near_miss in ("t", "tok-", "toke", "tokenx", "oken", "TOKEN", ""):
        assert _store_for(near_miss, tokens) is None, (
            f"{near_miss!r} matched a token it does not equal; the comparison is not equality"
        )


def test_no_header_a_caller_can_send_changes_what_the_window_serves(
    tokens_file: pathlib.Path,
) -> None:
    """The behavioural twin of the AST sweep, and the half that actually binds.

    ``test_the_served_window_has_no_notion_of_seeding`` matches on spelling, so a branch that
    avoids the words — ``if request.headers.get("x-window-demo") == "off": rows = [r for r in
    rows if r["provenance"] == "live"]`` — is invisible to it. MEASURED: that exact branch left
    every test in this file green.

    This closes it from the outside instead: the same request with an assortment of extra
    headers must produce a byte-identical body, ``as_of`` aside. A route with a second path
    fails here whatever it calls its flag, and a route with no second path cannot fail.
    """
    from buyer_svc.main import create_app
    from buyer_svc.window import routes as window_routes

    app = create_app()
    window_routes.set_window_connection(_Connection())
    try:
        baseline = None
        for extra in (
            {},
            {"X-Window-Demo": "off"},
            {"X-Window-Demo": "on"},
            {"X-Seed": "0"},
            {"X-Provenance": "live"},
            {"X-Buyer-Session": "whatever"},
            {"Accept": "application/json", "X-Debug": "1"},
        ):
            token = TOKENS["store-brightbean"]
            body = _get(app, Authorization=f"Bearer {token}", **extra)
            assert body.status_code == 200, (extra, body.text)
            payload = body.json()
            payload.pop("as_of")
            if baseline is None:
                baseline = payload
            assert payload == baseline, (
                f"the header {extra} changed what the window served. The route must have one "
                "path; a caller-triggered second one means everything proven about the first "
                "stops being evidence about what a store actually receives"
            )
        assert baseline and baseline["released"], "the invariance above was measured over nothing"
    finally:
        window_routes.set_window_connection(None)


def test_a_window_larger_than_the_ceiling_says_so(
    window_app: Any, tokens_file: pathlib.Path
) -> None:
    """``truncated`` is computed, not hard-coded — and the floor over-suppresses, never under.

    Nothing exercised the ceiling before, so ``truncated = False`` unconditionally was a
    green mutation. It matters twice: a store told nothing reads a truncated window as the whole
    population, and the class counts are taken over the rows actually fetched — which can only
    make a class look smaller than it is, so truncation withholds more and never less.
    """
    from buyer_svc.window.routes import MAX_WINDOW_ROWS, set_window_connection

    oversized = [
        (f"psn-{index:032x}", dict(_COHORT_A), "live") for index in range(MAX_WINDOW_ROWS + 1)
    ]
    set_window_connection(_Connection(sorted(oversized)))
    body = _get(window_app, Authorization=f"Bearer {TOKENS['store-brightbean']}").json()

    assert body["truncated"] is True, (
        f"the table held {len(oversized)} rows against a ceiling of {MAX_WINDOW_ROWS} and the "
        "response did not say it was truncated"
    )
    assert len(body["released"]) + body["withheld"] == MAX_WINDOW_ROWS, (
        "the response accounts for more rows than it read"
    )


def test_a_refusal_carries_no_secret_and_no_connection_string(
    window_app: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 401/500/503 bodies name variables, never values.

    A refusal is the most-logged, most-pasted response a route produces — into proxy logs,
    ticket comments, screenshots. Nothing graded what these bodies may carry, so a helpful
    ``detail=f"{APP_DSN_ENV}={dsn}"`` would have shipped a password in every 503.
    """
    from buyer_svc.auth.routes import APP_DSN_ENV
    from buyer_svc.window.routes import WINDOW_TOKENS_ENV, set_window_connection

    secret = "postgresql://app:hunter2@db.internal:5432/proxyshop"
    monkeypatch.setenv(APP_DSN_ENV, secret)

    bodies = []
    monkeypatch.delenv(WINDOW_TOKENS_ENV, raising=False)
    bodies.append(_get(window_app, Authorization="Bearer wtok-brightbean-3f9c2a71d0e64b58"))

    path = tmp_path / "tokens.json"
    path.write_text(json.dumps(TOKENS), encoding="utf-8")
    monkeypatch.setenv(WINDOW_TOKENS_ENV, str(path))
    bodies.append(_get(window_app, Authorization="Bearer nope-not-this-one"))
    set_window_connection(None)
    monkeypatch.delenv(APP_DSN_ENV, raising=False)
    monkeypatch.setenv(APP_DSN_ENV, secret)
    set_window_connection(_Exploding())
    bodies.append(_get(window_app, Authorization=f"Bearer {TOKENS['store-brightbean']}"))

    assert {body.status_code for body in bodies} == {401, 500, 503}, [b.status_code for b in bodies]
    for body in bodies:
        text = body.text
        for forbidden in ("hunter2", "db.internal", secret, "wtok-", "nope-not-this-one"):
            assert forbidden not in text, (
                f"a {body.status_code} refusal carried {forbidden!r}: {text}"
            )


@pytest.mark.docker
def test_the_audit_catches_the_forgery_the_constraint_permits(
    vault_migrated: str, pg_admin: Any
) -> None:
    """Against a real database: the CHECK admits an unpinned seeded row; the audit does not.

    This is the honest edge of the marker, driven rather than argued. ``psn-seed-`` plus any
    twenty-four characters is a legal row — the constraint's whole job is consistency between
    the label and the key, and this row is perfectly consistent. It is simply not one the
    repository committed to, and only a comparison against the corpus can say so.
    """
    from apps.buyer.seed import __main__ as cli
    from apps.buyer.seed.store import load

    corpus = load()
    smuggled = "psn-seed-" + "d" * 24
    dsn = f"postgresql://proxyshop:proxyshop_dev_pw@localhost:5432/{pg_admin.info.dbname}"
    try:
        with pg_admin.cursor() as cur:
            for row in corpus.rows:
                cur.execute(
                    "insert into app.buyer_accounts (pseudonym, buckets, provenance) "
                    "values (%s, %s::jsonb, 'seed') on conflict (pseudonym) do update set "
                    "buckets = excluded.buckets, provenance = 'seed'",
                    (row["pseudonym"], json.dumps(row["buckets"], sort_keys=True)),
                )
        pg_admin.commit()
        assert cli.main(["audit", "--dsn", dsn, "--json"]) == 0, (
            "the committed corpus, loaded exactly, did not audit clean"
        )

        with pg_admin.cursor() as cur:
            cur.execute(
                "insert into app.buyer_accounts (pseudonym, buckets, provenance) "
                "values (%s, '{}'::jsonb, 'seed')",
                (smuggled,),
            )
        pg_admin.commit()
        assert cli.main(["audit", "--dsn", dsn, "--json"]) == 1, (
            f"{smuggled} was inserted, is served as seeded, is in no chain, and the audit "
            "reported the database clean"
        )
    finally:
        with pg_admin.cursor() as cur:
            cur.execute("delete from app.buyer_accounts where provenance = 'seed'")
        pg_admin.commit()
