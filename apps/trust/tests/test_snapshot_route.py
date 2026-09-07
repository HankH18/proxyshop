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

import contextlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from proxyshop_support.asgi_server import serve

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


# ======================================================================================
# ``as_of`` at the door — graded over a REAL uvicorn server
#
# ``TestClient`` cannot grade any of this. It never hands a response header to an HTTP
# encoder, so the two worst outcomes below — a ``UnicodeEncodeError`` 500 and a connection
# dropped with no response at all — do not happen in-process. Everything in this section
# therefore runs against ``proxyshop_support.asgi_server.serve``, the same real loopback
# server ``test_events_poison_rows.py`` uses, for the same reason.
#
# Measured on the code as it shipped, ``GET /snapshot``, unauthenticated, one query
# parameter written straight into ``response.headers["X-Trust-As-Of"]``:
#
#   with no stores injected:
#     non-Latin-1 ``as_of``             -> HTTP 500
#     ``as_of`` carrying CR/LF or NUL   -> CONNECTION DROPPED, no status at all
#     an unparseable ``as_of``          -> HTTP 200, echoed VERBATIM into the header
#   with one store injected:
#     non-Latin-1 / CR / LF / garbage   -> HTTP 500 — ``trust.scoring._parse_instant``
#                                          raises a bare ``ValueError`` and this handler
#                                          catches only ``EventServiceError``
#     NUL                               -> CONNECTION DROPPED (the NUL survives the parse)
#
# Three distinct defects in one line, all reachable by an anonymous caller: a 5xx the
# caller chooses, a dropped connection (worse than a 5xx — the client cannot tell a refusal
# from a network fault), and caller-controlled bytes in a response header, which is the
# shape of header injection.
# ======================================================================================

#: Characters no response header value may carry: the C0 controls (CR, LF and NUL among
#: them), DEL, and the C1 controls. Spelled out HERE rather than imported from the route,
#: so this gate stays a independent statement of what is illegal and cannot be relaxed by
#: relaxing the module it grades.
_ILLEGAL_HEADER_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

#: ``as_of`` values outside Latin-1 — Cyrillic, an emoji, CJK. HTTP header values are
#: Latin-1 on the wire and this route builds ``X-Trust-As-Of`` from this parameter, so each
#: raised ``UnicodeEncodeError`` inside Starlette's header assignment: a 500 chosen by an
#: unauthenticated caller.
NON_LATIN_1_AS_OF: tuple[str, ...] = ("заказ-1", "2026-02-01T00:00:00Z🚀", "註文")

#: ``as_of`` values that ARE Latin-1 encodable and are still not legal header values. Each
#: was measured against a real uvicorn server as a dropped connection —
#: ``httpx.RemoteProtocolError("Server disconnected without sending a response")`` — which
#: is strictly worse than a 500: the caller cannot tell a refusal from a network fault.
CONTROL_CHARACTER_AS_OF: tuple[str, ...] = (
    "2026-02-01T00:00:00Z\r\nX-Injected: 1",
    "2026\nX-Injected: 1",
    "2026-02-01T00:00:00Z\x00",
)

#: The half of the above that ``trust.scoring._parse_instant`` **accepts**: it ``strip()``s
#: trailing whitespace (so a trailing CRLF parses) and CPython's ``fromisoformat`` parses
#: straight through a trailing NUL. So "is it a parseable instant?" does NOT imply "can a
#: header carry it", and neither does "is it Latin-1 encodable". Both of these reached the
#: header on the shipped code with the scorer perfectly happy, and both dropped the
#: connection. This tuple is why the control-character screen is a separate, load-bearing
#: check rather than a redundant one.
PARSEABLE_BUT_UNRENDERABLE_AS_OF: tuple[str, ...] = (
    "2026-02-01T00:00:00Z\r\n",
    "2026-02-01T00:00:00Z\x00",
)

#: An ``as_of`` that is renderable and is not an instant. Measured: served ``200`` with this
#: string echoed verbatim into ``X-Trust-As-Of`` when no store was present, and ``500`` when
#: one was. The distinctive prefix is what the no-echo assertion searches the response for.
UNPARSEABLE_AS_OF = "not-an-instant-" + "z" * 64

#: An ``as_of`` that IS a parseable instant and is 221 characters long — ``fromisoformat``
#: takes any number of fractional digits. Parseability alone therefore bounds nothing, and a
#: caller who can choose an unbounded header value can choose the size of every response
#: this route serves.
OVERLONG_AS_OF = "2026-01-01T00:00:00." + "0" * 200 + "Z"

#: Both source populations, because the shipped failure MODE differed between them: with no
#: stores the scorer is never called, so garbage sailed through to the header; with one it
#: was called and raised the uncaught ``ValueError``.
LIVE_STORE_SETS: tuple[tuple[str, list[dict[str, Any]]], ...] = (
    ("no-stores", []),
    (
        "one-store",
        [
            _store(
                "store-1",
                "co-1",
                [
                    {
                        "dim": "price_honored",
                        "type": "verified",
                        "observed_at": "2026-01-01T00:00:00Z",
                    }
                ],
            )
        ],
    ),
)

_STORE_SET_IDS = [name for name, _ in LIVE_STORE_SETS]
_STORE_SET_VALUES = [stores for _, stores in LIVE_STORE_SETS]


@contextlib.contextmanager
def _live_client(stores: Any) -> Iterator[httpx.Client]:
    """A real HTTP client against a real loopback uvicorn server serving ``stores``.

    Port 0 through ``proxyshop_support.asgi_server.serve`` (D40), so nothing here pins a
    port and the pytest socket guard's loopback allowance covers it.
    """
    from trust.main import create_app
    from trust.scoring import Blacklist

    app = create_app()
    app.state.snapshot_stores = stores
    app.state.snapshot_blacklist = Blacklist()
    with serve(app) as base_url, httpx.Client(base_url=base_url, timeout=60.0) as client:
        yield client


def _get_snapshot(client: httpx.Client, as_of: str | None) -> httpx.Response:
    """One ``GET /snapshot``, with ``as_of`` percent-encoded onto the query string.

    Percent-encoded rather than handed to ``params=``: the bytes on the wire must be legal
    whatever the parameter carries, so that what is being graded is the APPLICATION's
    handling of a control character and never the client's refusal to send one.

    A dropped connection is turned into a named assertion failure rather than an error,
    because that is the pre-fix behaviour this section exists to close and an
    ``httpx.RemoteProtocolError`` traceback does not say so.
    """
    url = "/snapshot" if as_of is None else f"/snapshot?as_of={quote(as_of, safe='')}"
    try:
        return client.get(url)
    except httpx.RemoteProtocolError as exc:  # pragma: no cover - the pre-fix behaviour
        raise AssertionError(
            f"the server dropped the connection with no response at all for as_of="
            f"{as_of!r} ({exc}). A caller-chosen value reached a response header that "
            f"could not carry it; the caller cannot tell this from a network fault."
        ) from exc


def _assert_refused(response: httpx.Response, as_of: str) -> dict[str, Any]:
    """The one refusal shape: a clean 4xx, naming ``as_of``, with no header emitted."""
    assert 400 <= response.status_code < 500, (
        f"as_of={as_of!r} was answered {response.status_code}, not a 4xx. A value chosen "
        f"by an anonymous caller must never decide this service's 5xx rate."
    )
    assert response.status_code == 422, response.text
    assert "X-Trust-As-Of" not in response.headers, (
        f"as_of={as_of!r} was refused and the header was emitted anyway: "
        f"{response.headers.get('X-Trust-As-Of')!r}"
    )
    detail = response.json()["detail"]
    assert detail["field"] == "as_of", detail
    assert detail["error"], detail
    return dict(detail)


def _assert_headers_are_renderable(response: httpx.Response, as_of: str | None) -> None:
    """Every header on this response is a value HTTP can actually carry.

    The class check, not the instance one: it walks the whole response rather than
    ``X-Trust-As-Of`` alone, so a future header built from a new caller-derived value is
    graded by this test the day it is added.
    """
    for name, value in response.headers.items():
        assert not _ILLEGAL_HEADER_CHARACTERS.search(value), (
            f"header {name!r} carries a control character for as_of={as_of!r}: {value!r}"
        )
        value.encode("latin-1")


@pytest.mark.parametrize("stores", _STORE_SET_VALUES, ids=_STORE_SET_IDS)
@pytest.mark.parametrize("as_of", NON_LATIN_1_AS_OF)
def test_a_non_latin_1_as_of_is_refused_and_is_never_a_500(stores: Any, as_of: str) -> None:
    """Measured 500 on the shipped code, from a query string, with no credential.

    ``X-Trust-As-Of`` is built from this parameter and header values are Latin-1 on the
    wire, so Starlette's header assignment raised ``UnicodeEncodeError`` *after* the
    snapshot had been computed. A caller who can choose the status class can choose this
    service's error rate.
    """
    with _live_client(stores) as client:
        response = _get_snapshot(client, as_of)
    _assert_refused(response, as_of)


@pytest.mark.parametrize("stores", _STORE_SET_VALUES, ids=_STORE_SET_IDS)
@pytest.mark.parametrize("as_of", CONTROL_CHARACTER_AS_OF)
def test_an_as_of_carrying_a_control_character_never_drops_the_connection(
    stores: Any, as_of: str
) -> None:
    """The worst of the three: no status at all, chosen by an anonymous caller.

    CR/LF in a header value is a response split, and uvicorn answers such a response by
    closing the connection — the client sees ``RemoteProtocolError`` and cannot distinguish
    a refusal from a dropped network. ``_get_snapshot`` converts that into a failure with
    this test's name on it.
    """
    with _live_client(stores) as client:
        response = _get_snapshot(client, as_of)
    _assert_refused(response, as_of)


@pytest.mark.parametrize("stores", _STORE_SET_VALUES, ids=_STORE_SET_IDS)
@pytest.mark.parametrize("as_of", PARSEABLE_BUT_UNRENDERABLE_AS_OF)
def test_an_as_of_the_scorer_accepts_is_still_refused_when_a_header_cannot_carry_it(
    stores: Any, as_of: str
) -> None:
    """A parse check alone does not close this, and neither does an encodability check.

    Both values here are Latin-1 encodable AND parse cleanly through
    ``trust.scoring._parse_instant`` (it strips trailing whitespace; ``fromisoformat``
    parses through a trailing NUL), and both dropped the connection on the shipped code.
    The screen that catches them is the control-character one, and this is the test that
    says so — if the fix is "reject what does not parse", this stays red.
    """
    from trust.scoring.engine import _parse_instant

    assert _parse_instant(as_of) is not None, (
        f"{as_of!r} no longer parses, so this test no longer grades what it claims to: "
        f"the point is a value the SCORER accepts and a HEADER cannot carry"
    )
    as_of.encode("latin-1")

    with _live_client(stores) as client:
        response = _get_snapshot(client, as_of)
    _assert_refused(response, as_of)


@pytest.mark.parametrize("stores", _STORE_SET_VALUES, ids=_STORE_SET_IDS)
def test_an_unparseable_as_of_is_refused_and_never_echoed_back(stores: Any) -> None:
    """The 200-with-garbage half and the bare-``ValueError`` 500 half, in one gate.

    With no store the scorer was never called and the string was echoed verbatim into
    ``X-Trust-As-Of``; with one store ``_parse_instant`` raised a ``ValueError`` the handler
    does not catch and the caller got a 500. Both are the same defect — a value that is not
    an instant is not a header worth emitting — so both are refused here, and the refusal
    must not repeat the caller's bytes back to it in any form.
    """
    with _live_client(stores) as client:
        response = _get_snapshot(client, UNPARSEABLE_AS_OF)
    _assert_refused(response, UNPARSEABLE_AS_OF)

    marker = UNPARSEABLE_AS_OF[:20]
    assert marker not in response.text, (
        f"the refusal echoes the offending value back to the caller: {response.text}"
    )
    for name, value in response.headers.items():
        assert marker not in value, f"header {name!r} echoes the offending value: {value!r}"


@pytest.mark.parametrize("stores", _STORE_SET_VALUES, ids=_STORE_SET_IDS)
def test_a_parseable_but_unbounded_as_of_cannot_become_an_unbounded_header(
    stores: Any,
) -> None:
    """``fromisoformat`` accepts any number of fractional digits, so parsing bounds nothing.

    The ceiling is imported rather than restated, so raising it moves this gate with it —
    and the second assertion keeps "raise the ceiling" from being a way to make this pass.
    """
    from trust.snapshot.routes import MAX_AS_OF_LENGTH

    assert MAX_AS_OF_LENGTH <= 128, (
        f"the as_of ceiling is {MAX_AS_OF_LENGTH}; an RFC-3339 instant is ~35 characters "
        f"and this value ends up in a response header"
    )
    assert len(OVERLONG_AS_OF) > MAX_AS_OF_LENGTH

    with _live_client(stores) as client:
        response = _get_snapshot(client, OVERLONG_AS_OF)
    _assert_refused(response, OVERLONG_AS_OF)


@pytest.mark.parametrize("stores", _STORE_SET_VALUES, ids=_STORE_SET_IDS)
def test_no_response_header_this_route_serves_can_carry_an_illegal_value(stores: Any) -> None:
    """Header safety as a CLASS: every header, every input, refused or served.

    Five headers are written by this handler — ``ETag``,
    ``X-Trust-Snapshot-Version``, ``X-Trust-Score-Version``, ``X-Trust-As-Of`` and
    ``Cache-Control`` — and the first three plus the last come from module constants while
    only ``X-Trust-As-Of`` is caller-derived. That is a fact about today's code, so it is
    asserted rather than assumed: the constants are checked directly, and the whole response
    is walked for every hostile input so a newly caller-derived header is caught here.
    """
    from trust.scoring import SCORE_VERSION
    from trust.snapshot import SNAPSHOT_VERSION

    for constant in (SNAPSHOT_VERSION, SCORE_VERSION):
        assert not _ILLEGAL_HEADER_CHARACTERS.search(constant), constant
        constant.encode("latin-1")

    probes: tuple[str | None, ...] = (
        None,
        AS_OF,
        UNPARSEABLE_AS_OF,
        OVERLONG_AS_OF,
        *NON_LATIN_1_AS_OF,
        *CONTROL_CHARACTER_AS_OF,
        *PARSEABLE_BUT_UNRENDERABLE_AS_OF,
    )
    with _live_client(stores) as client:
        for as_of in probes:
            _assert_headers_are_renderable(_get_snapshot(client, as_of), as_of)


@pytest.mark.parametrize("stores", _STORE_SET_VALUES, ids=_STORE_SET_IDS)
def test_a_valid_as_of_still_serves_its_instant_in_the_header(stores: Any) -> None:
    """Positive control. The fix is worthless if it refuses the values the route is for.

    Over the real socket, not ``TestClient``, so this also proves the served header
    survives an actual HTTP encode.
    """
    with _live_client(stores) as client:
        response = _get_snapshot(client, AS_OF)
    assert response.status_code == 200, response.text
    assert response.headers["X-Trust-As-Of"] == AS_OF
    assert response.headers["ETag"], response.headers
    assert sorted(response.json()) == sorted(store["store_id"] for store in stores)


@pytest.mark.parametrize("stores", _STORE_SET_VALUES, ids=_STORE_SET_IDS)
def test_omitting_as_of_still_serves_the_serve_instant(stores: Any) -> None:
    """Positive control, second half: the default path is untouched.

    ``as_of`` is optional and defaults to the serve instant, and a door that refused a
    missing value would break every client that does not replay a past decision. The header
    must still be there and must still be an instant the scorer would accept.
    """
    from trust.scoring.engine import _parse_instant

    with _live_client(stores) as client:
        response = _get_snapshot(client, None)
    assert response.status_code == 200, response.text
    served = response.headers["X-Trust-As-Of"]
    assert served.endswith("Z"), served
    assert _parse_instant(served) is not None, served
    assert sorted(response.json()) == sorted(store["store_id"] for store in stores)


def test_a_stored_observation_the_scorer_refuses_is_a_503_and_not_a_bare_traceback() -> None:
    """The other reachable ``ValueError``: the DATA, once ``as_of`` is screened at the door.

    ``trust.scoring.score`` raises a bare ``ValueError`` on an unparseable ``observed_at``,
    and rows in ``ledger.trust_observations`` are written from ``POST /events`` — so the
    value is caller-influenced even though this request did not carry it. The handler caught
    only ``EventServiceError``, so such a row was an unhandled exception.

    503 and never an empty body, for the reason the route already documents: the exchange
    reads a missing row as an unavailable eligibility read and denies (R12), so ``{}`` for
    "one row is poison" would silently deny every store. The refusal names the class of
    problem and not the stored value — that value came from a caller too.
    """
    poisoned = [
        _store(
            "store-1",
            "co-1",
            [{"dim": "price_honored", "type": "verified", "observed_at": "not-a-date"}],
        )
    ]
    with _live_client(poisoned) as client:
        response = _get_snapshot(client, AS_OF)
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["error"], detail
    assert "not-a-date" not in response.text, response.text


#: Every legitimate RFC-3339 spelling the scorer accepts, plus the two "no instant" ones.
#: Measured over a real socket: each is served ``200`` with a header the wire carries. This
#: is the door's admit set, and it is graded for the same reason the refusals are — a screen
#: that also refuses the offset form, the lowercase ``z`` or microsecond precision would
#: break real clients while every hostile test above stayed green.
ADMITTED_AS_OF: tuple[str, ...] = (
    "2026-02-01T00:00:00Z",
    "2026-02-01t00:00:00z",
    "2026-02-01T00:00:00+00:00",
    "2026-02-01T00:00:00.123456+05:30",
    "2026-02-01 00:00:00+00:00",
    " 2026-02-01T00:00:00Z ",
    "2026-02-01",
    "",
    "   ",
)


@pytest.mark.parametrize("as_of", ADMITTED_AS_OF)
def test_every_instant_the_scorer_accepts_is_still_served_with_a_header(as_of: str) -> None:
    """The admit set, over the wire. A fail-closed door that refuses real clients is a bug.

    The empty and whitespace-only cases are the documented default and not a refusal:
    ``as_of`` is optional, so "no instant" yields the serve instant exactly as it did before
    this door existed.
    """
    from trust.scoring.engine import _parse_instant

    with _live_client([]) as client:
        response = _get_snapshot(client, as_of)
    assert response.status_code == 200, response.text
    served = response.headers["X-Trust-As-Of"]
    _assert_headers_are_renderable(response, as_of)
    assert _parse_instant(served) is not None, served
    if as_of.strip():
        # ``as_of.strip()`` and not ``as_of``: HTTP does not treat surrounding OWS as part
        # of a field value, so a padded instant comes back trimmed however the app spells
        # it. The door normalises for exactly that reason — what is scored, what is set and
        # what is read must be one string — and this is the assertion that says so.
        assert served == as_of.strip(), (
            f"the caller's instant was rewritten to {served!r}; a replayed decision is "
            f"stamped with the instant it was replayed at"
        )


# ======================================================================================
# T-303 (b) — the fold on the read path: a sealed delisting becomes a served `blacklisted`
#
# `blacklist_for` is where the ledger's delisting decisions meet the registry the snapshot is
# resolved against. Before this, `app.seller_blacklist` had no writer anywhere in the tree —
# `db/migrations/0004` grants `trust_rw` INSERT on it and nothing used the grant — so a
# delisting the trust engine sealed into the hash chain never became a row and the exchange
# went on being told the store was fine.
#
# The injected layer below grades the FOLD; the docker layer grades the WRITER, which an
# injected fixture cannot prove and which is the half that breaks in a deployment.
# ======================================================================================

_FOLD_STORES = [
    _store(
        "store-caught",
        "co-caught",
        [{"dim": "price_honored", "type": "verified", "observed_at": "2026-01-01T00:00:00Z"}],
    ),
    _store("store-honest", "co-honest", []),
]


def _sealed(kind: str, store_id: str, identity: str, **payload: Any) -> dict[str, Any]:
    """One sealed delisting event, in the shape ``trust.snapshot.delisting`` emits."""
    body = {"store_id": store_id, "reason_code": "trust_score_below_threshold", **payload}
    body.setdefault("business_identity", identity)
    if kind == "blacklisted":
        body.setdefault("source", "trust-score")
        body.setdefault("expires_at", None)
    return {
        "event_id": f"{kind}:{store_id}:{AS_OF}",
        "ts": AS_OF,
        "kind": kind,
        "store_id": store_id,
        "payload": body,
    }


def _served_with_ledger(events: list[dict[str, Any]], registry: Any = None) -> dict[str, Any]:
    """``GET /snapshot`` over :data:`_FOLD_STORES`, with ``events`` sealed in the ledger."""
    from trust.events import InMemoryEventStore, append
    from trust.main import create_app
    from trust.scoring import Blacklist

    store = InMemoryEventStore()
    for event in events:
        append(store, event)

    app = create_app()
    app.state.snapshot_stores = _FOLD_STORES
    app.state.snapshot_blacklist = Blacklist() if registry is None else registry
    app.state.event_store = store
    response = TestClient(app).get("/snapshot", params={"as_of": AS_OF})
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_a_sealed_delisting_reaches_the_served_snapshot() -> None:
    """The missing link, at the door the exchange actually reads.

    The control is the same route, the same stores and the same empty registry with NO event
    sealed — so ``blacklisted: true`` is attributable to the fold and to nothing else. Before
    this fold existed, both halves answered ``false``.
    """
    control = _served_with_ledger([])
    assert control["store-caught"]["blacklisted"] is False, control
    assert control["store-honest"]["blacklisted"] is False, control

    served = _served_with_ledger([_sealed("blacklisted", "store-caught", "co-caught")])
    assert served["store-caught"]["blacklisted"] is True, (
        f"a delisting sealed into the ledger did not reach the served snapshot: {served}"
    )
    assert served["store-honest"]["blacklisted"] is False, (
        f"the fold blacklisted a store no delisting names: {served}"
    )
    _validate_trust_snapshot(served["store-caught"])


def test_a_sealed_expiry_returns_the_store_to_the_served_snapshot() -> None:
    """R12's expiry state, end to end: the listing ends and the store is served again."""
    listed = _served_with_ledger([_sealed("blacklisted", "store-caught", "co-caught")])
    assert listed["store-caught"]["blacklisted"] is True

    released = _served_with_ledger(
        [
            _sealed("blacklisted", "store-caught", "co-caught"),
            _sealed("blacklist_expired", "store-caught", "co-caught"),
        ]
    )
    assert released["store-caught"]["blacklisted"] is False, (
        f"a sealed blacklist_expired did not return the store to service: {released}"
    )


def test_the_served_fold_never_reopens_a_listing_a_human_is_still_deciding() -> None:
    """A sealed ``blacklisted`` must not close an open appeal by re-stamping it ``active``.

    The two ledger kinds cannot express ``under_review`` or ``appealed``; only
    ``app.seller_blacklist`` can, so the fold may add listings and close them and may not
    overwrite the lifecycle state a person put one in.
    """
    from trust.scoring import Blacklist

    registry = Blacklist()
    registry.add(business_identity="co-caught", reason_code="manual_review", status="appealed")
    served = _served_with_ledger(
        [_sealed("blacklisted", "store-caught", "co-caught")], registry=registry
    )
    assert served["store-caught"]["blacklisted"] is True, served
    assert registry.lookup("co-caught").status == "appealed", (
        "the served fold rewrote a listing under appeal; a GET closed a review nobody finished"
    )
    assert registry.lookup("co-caught").reason_code == "manual_review"


def test_an_injected_registry_with_no_ledger_bound_is_served_exactly_as_before() -> None:
    """No ``event_store``, no fold, no attempt to reach a datastore nobody configured.

    This is the shape every injected-source test in this file and in ``e2e/`` uses. A fold
    that reached for a ``PostgresEventStore`` here would turn each of them into a 503.
    """
    from trust.scoring import Blacklist

    registry = Blacklist()
    registry.add(business_identity="co-caught", reason_code="manual_review", status="active")
    response = _client(_FOLD_STORES, registry).get("/snapshot", params={"as_of": AS_OF})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["store-caught"]["blacklisted"] is True, body
    assert body["store-honest"]["blacklisted"] is False, body


@pytest.mark.docker
def test_the_fold_writes_the_listing_into_app_seller_blacklist(
    ledger_clean: Any,
    pg_admin: Any,
    worker_database: str,
    worker_index: int,
) -> None:
    """The half an injected fixture cannot prove: the grant nothing used is now used.

    ``db/migrations/0004`` gives ``trust_rw`` ``INSERT, UPDATE, DELETE`` on
    ``app.seller_blacklist`` and, until this fold, no product code anywhere in the tree wrote
    to that table — ``blacklist_for`` was its only reader. So this seeds a seller and a
    delisting sealed through the REAL ledger writer, leaves the blacklist table EMPTY, and
    asks the route for a snapshot: the store must come back listed, and the row must exist
    afterwards, written by the service under its own role.

    The second read is not decoration. It is the idempotency claim at the database: the fold
    runs again over the same chain, and ``seller_blacklist_one_live_entry_idx`` plus
    ``on conflict do nothing`` must leave exactly one row rather than a second listing per
    snapshot read.
    """
    import os

    from trust.events import PostgresEventStore, append
    from trust.main import create_app

    from proxyshop_support.postgres import role_dsn

    with pg_admin.cursor() as cursor:
        cursor.execute("delete from ledger.trust_observations")
        cursor.execute("delete from app.seller_blacklist")
        cursor.execute("delete from app.sellers")
        cursor.execute(
            "insert into app.sellers (store_id, domain, business_identity, tier) values "
            "('store-caught', 'caught.example', 'co-caught', 'hosted'), "
            "('store-windowed', 'windowed.example', 'co-windowed', 'hosted'), "
            "('store-honest', 'honest.example', 'co-honest', 'hosted')"
        )
        cursor.execute(
            "insert into ledger.trust_observations "
            "(store_id, dim, observation_type, weight, observed_at) values "
            "('store-caught', 'price_honored', 'verified', 1.0, '2026-01-01T00:00:00Z')"
        )

    dsn = role_dsn("trust_rw", worker_index, database=worker_database)
    writer = PostgresEventStore(dsn)
    try:
        append(writer, _sealed("blacklisted", "store-caught", "co-caught"))
        # A listing with a WINDOW, and one whose window closes before the instant this fold
        # runs at. `seller_blacklist_window_ordered` requires `expires_at >= starts_at`, so a
        # row stamped `starts_at = now()` would be refused and the listing lost on a schema
        # technicality — which is why the insert floors `starts_at` by `expires_at`.
        append(
            writer,
            _sealed(
                "blacklisted",
                "store-windowed",
                "co-windowed",
                expires_at="2026-03-01T00:00:00Z",
            ),
        )
    finally:
        writer.close()

    with pg_admin.cursor() as cursor:
        cursor.execute("select count(*) from app.seller_blacklist")
        assert cursor.fetchone()[0] == 0, "the blacklist table was seeded; nothing left to write"

    app = create_app()
    app.state.ledger_connection = None
    previous = os.environ.get("PROXYSHOP_LEDGER_DSN")
    os.environ["PROXYSHOP_LEDGER_DSN"] = role_dsn("trust_rw", database=worker_database)
    try:
        client = TestClient(app)
        first = client.get("/snapshot", params={"as_of": AS_OF})
        second = client.get("/snapshot", params={"as_of": AS_OF})
    finally:
        if previous is None:
            os.environ.pop("PROXYSHOP_LEDGER_DSN", None)
        else:
            os.environ["PROXYSHOP_LEDGER_DSN"] = previous

    for response in (first, second):
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["store-caught"]["blacklisted"] is True, (
            f"a delisting sealed into ledger.commerce_events did not reach the served "
            f"snapshot, so the fold did not read the real chain: {body}"
        )
        assert body["store-windowed"]["blacklisted"] is True, (
            f"a delisting whose window has not closed at as_of={AS_OF} is not enforced: {body}"
        )
        assert body["store-honest"]["blacklisted"] is False, body

    with pg_admin.cursor() as cursor:
        cursor.execute(
            "select business_identity, store_id, reason_code, source, status, expires_at "
            "from app.seller_blacklist order by business_identity"
        )
        rows = cursor.fetchall()
    assert len(rows) == 2, (
        f"the fold wrote {len(rows)} rows for two sealed delistings across two reads; the "
        f"registry has to CONVERGE on the chain, not be re-listed on every snapshot: {rows}"
    )
    windowed = rows[1]
    assert windowed[0] == "co-windowed" and windowed[4] == "active", windowed
    assert windowed[5] is not None, (
        f"the listing lost the window the sealed decision carried, so it can never lapse: "
        f"{windowed}"
    )
    identity, store_id, reason_code, source, status, expires_at = rows[0]
    assert (identity, store_id, status) == ("co-caught", "store-caught", "active"), rows[0]
    assert reason_code == "trust_score_below_threshold", rows[0]
    assert source == "trust-score", (
        f"the row does not record WHO decided, so a reviewer cannot tell an automatic "
        f"delisting from a human one: {rows[0]}"
    )
    assert expires_at is None, rows[0]


@pytest.mark.docker
def test_a_sealed_expiry_closes_the_row_the_fold_wrote(
    ledger_clean: Any,
    pg_admin: Any,
    worker_database: str,
    worker_index: int,
) -> None:
    """The other half of the lifecycle, against the real table: the listing is CLOSED.

    Two phases, because that is the sequence a deployment actually runs: the delisting is
    sealed and read (the fold writes an ``active`` row), then the expiry is sealed and read
    (the fold closes that same row). ``status`` moves to ``expired`` rather than the row being
    deleted — an appeal, an auditor and ``reviewed_by`` all read a history, and a blacklist
    you cannot explain is one nobody will maintain.
    """
    import os

    from trust.events import PostgresEventStore, append
    from trust.main import create_app

    from proxyshop_support.postgres import role_dsn

    with pg_admin.cursor() as cursor:
        cursor.execute("delete from ledger.trust_observations")
        cursor.execute("delete from app.seller_blacklist")
        cursor.execute("delete from app.sellers")
        cursor.execute(
            "insert into app.sellers (store_id, domain, business_identity, tier) values "
            "('store-caught', 'caught.example', 'co-caught', 'hosted')"
        )

    dsn = role_dsn("trust_rw", worker_index, database=worker_database)
    app = create_app()
    app.state.ledger_connection = None
    previous = os.environ.get("PROXYSHOP_LEDGER_DSN")
    os.environ["PROXYSHOP_LEDGER_DSN"] = role_dsn("trust_rw", database=worker_database)

    def seal(event: dict[str, Any]) -> None:
        writer = PostgresEventStore(dsn)
        try:
            append(writer, event)
        finally:
            writer.close()

    def statuses() -> list[str]:
        with pg_admin.cursor() as cursor:
            cursor.execute("select status from app.seller_blacklist order by created_at")
            return [row[0] for row in cursor.fetchall()]

    try:
        client = TestClient(app)
        seal(_sealed("blacklisted", "store-caught", "co-caught"))
        listed = client.get("/snapshot", params={"as_of": AS_OF})
        assert listed.status_code == 200, listed.text
        assert listed.json()["store-caught"]["blacklisted"] is True, listed.text
        assert statuses() == ["active"], statuses()

        seal(_sealed("blacklist_expired", "store-caught", "co-caught"))
        released = client.get("/snapshot", params={"as_of": AS_OF})
    finally:
        if previous is None:
            os.environ.pop("PROXYSHOP_LEDGER_DSN", None)
        else:
            os.environ["PROXYSHOP_LEDGER_DSN"] = previous

    assert released.status_code == 200, released.text
    body = released.json()
    assert body["store-caught"]["blacklisted"] is False, (
        f"a sealed blacklist_expired did not return the store to service: {body}"
    )
    assert statuses() == ["expired"], (
        f"the closed listing is not readable as a closed listing: {statuses()}. An expiry "
        f"ends a listing; it does not erase that it happened"
    )
