"""The store-visible window, on a served route (T-142).

    GET /buyer/store-window
    Authorization: Bearer <this store's window token>

What was measured before this file existed
==========================================
``app.buyer_accounts`` had been written on every served ``GET /buyer/profile`` since T-142's
first half landed, and **no line of any language in this repository ever read it**. Not the
exchange — which holds the ``SELECT`` grant and has no Postgres client at all — not the trust
engine, not the buyer web app, not a script. ``publish_profile``'s own docstring called the
table "the store-visible working set"; the set had no viewer, and ``app.intents.pseudonym``'s
foreign key onto it was inert because nothing writes ``app.intents`` either.

Who is entitled to read it, and why it is not any of the existing callers
=========================================================================
The entitlement question is already answered, and it is answered by the database rather than
by this file. D5's grant model gives ``SELECT`` on ``app.*`` to ``exchange``, ``trust_rw`` and
``buyer_vault``, and gives ``USAGE`` on ``vault.*`` to ``buyer_vault`` alone.
``apps/buyer/svc/tests/test_auth_vault.py::test_the_store_facing_row_is_readable_while_the_mapping_is_not``
drives exactly that pair against a live database — the auction role reads a buyer's buckets and
gets ``InsufficientPrivilege`` when it asks who that buyer is. **The store side of the network
is entitled to this table.** What it did not have was a door.

Three doors were available and two are wrong:

* **Widen ``X-Buyer-Session``.** ``GET /buyer/profile`` already reads a buyer's own row. Letting
  that header read the whole table would turn "a buyer may see themselves" into "any buyer may
  see every buyer" — a strictly larger authority granted to a credential that exists for a
  smaller one. Refused; this route does not accept a session header at all, and
  ``test_a_buyer_session_is_not_a_window_credential`` pins that a valid one is still a 401.
* **Have the exchange read it.** The exchange is documented as a pure pass-through that never
  looks a buyer up (``apps/exchange/src/composition.py``'s ``solicitation_profile``: "Nothing
  about the BUYER is invented here"), and it opens no database connection. Building that
  consumer would be inventing a cross-service dependency nobody asked for in order to give a
  table a reader.
* **A new, store-scoped door on the service that owns the table.** This file.

Authorisation: the token IS the store id
========================================
The shape is ``exchange.reports.routes``'s, deliberately and almost line for line: a bearer
token table ``{store_id: token}``, **no default**, and a deployment that has configured none
serves nobody (``503``) — because an empty expected value compared against an empty supplied
one is a route that opens itself the moment the environment is incomplete. The comparison is
``hmac.compare_digest`` over every row rather than stopping at the first match, so the response
time says nothing about where in the table a presented token sits.

Unlike the loss report, the window is not *scoped* by the store that reads it: there is one
window and every entitled store sees the same one. The store id is resolved from the bearer all
the same, because a shared token authenticates a caller and says nothing about who it is, and
"who read the window" is the first thing anyone asks after it is read.

What must be coarsened before it leaves: suppression, not generalisation
========================================================================
The rows in the table are already rung 0 of :mod:`buyer_svc.profile`'s ladder — each one is
what ``build_profile`` published for one buyer. That is the right disclosure for **one** buyer
reading their own profile. It is not automatically the right disclosure for a store handed the
whole population at once: a buyer whose bucket combination is unique in the release has been
singled out, and no amount of per-row coarsening fixes that, because uniqueness is a property
of the release and not of the row.

So the release is held to a floor (:data:`DEFAULT_WINDOW_FLOOR`, overridable per deployment),
and rows in an equivalence class smaller than the floor are **withheld**. Not generalised —
and the difference is forced rather than chosen. :func:`~buyer_svc.profile.anonymise_cohort`
generalises by walking each record up a ladder built from its **account**; this route has no
accounts, only the coarse buckets that were published from them, and a ladder cannot be climbed
from a rung. Suppression is the only sound move available to a reader in this position, and
claiming to generalise here would mean inventing a coarser account than the one that produced
the row.

The count of withheld rows is reported. A store that is shown 34 of 42 rows and told so can
reason about what it is looking at; one shown 34 rows and told nothing cannot.

``created_at`` never leaves. It is not a bucket, it is a join key: a timestamp on a row is a
handle onto whichever login happened at that instant, and the table's coarsening says nothing
about it. Only ``{pseudonym, buckets}`` — DESIGN §Data models' ``BuyerProfile``, exactly — plus
the ``provenance`` column, below.

Seeded rows, and why this route does not know what one is
==========================================================
Half of a demonstrable window is manufactured (``apps/buyer/seed-data/store-window/``), and a
store must be told which half. So each row carries the ``provenance`` column verbatim.

This route has **no notion of seeding**. It does not import the seed package, does not know
what ``'seed'`` means, and applies no rule to it: it copies a column, the way
``buyer_svc.feedback`` copies ``order_ref`` onto a ledger event without knowing that a prefix
on it means "simulated". What makes the copied value trustworthy is not this file — it is
``db/migrations/0005_buyer_accounts_provenance.sql``, whose CHECK ties ``provenance`` to the
PRIMARY KEY as an equivalence, so a row whose label and whose key disagree cannot exist.

**What that label is and is not worth**, because the difference decides what a store may
conclude from it. It is proof that no *caller* forged it: nothing reachable over HTTP, a real
buyer's login included, can produce a row that reads as seeded or strip the marker from one
that does. It is **not** proof that a row marked ``'seed'`` is one this repository committed —
the table stores no signature, so anything holding the ``app`` role can insert
``psn-seed-<any 24 characters>`` and this route will serve it as manufactured. Closing that is
a comparison against the committed corpus, ``python -m apps.buyer.seed audit``, which lives
outside this service precisely because the route must not know what a corpus is.

For the same reason, ``apps.buyer.seed.chain.is_seeded_pseudonym`` — which reads the pseudonym
and ignores the column — is not an *independent* witness to the label. It and the column are
two readers of one fact the CHECK forces into agreement, so
``test_the_column_and_the_key_agree_on_every_released_row`` catches this route mixing fields
across rows and nothing else. It is still worth having: it means a reader holding only this
response can classify a row without trusting the label that arrived with it.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth.routes import APP_DSN_ENV
from ..profile import BUCKET_KEYS, equivalence_class

__all__ = [
    "DEFAULT_WINDOW_FLOOR",
    "MAX_WINDOW_ROWS",
    "STORE_WINDOW_PATH",
    "WINDOW_FLOOR_ENV",
    "WINDOW_TOKENS_ENV",
    "StoreWindowRow",
    "StoreWindowView",
    "read_store_window",
    "release",
    "router",
    "set_window_connection",
    "window_floor",
    "window_tokens",
]

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/buyer", tags=["buyer-store-window"])

#: The published path. Named rather than spelled twice, so the tests drive the constant the
#: route registers — a path that agreed with itself only in two string literals is one rename
#: away from a suite passing against a 404.
STORE_WINDOW_PATH = "/store-window"

#: Path to a JSON file holding ``{store_id: token}``. A PATH and never the tokens themselves,
#: for the reason ``exchange.composition.report_tokens_file`` is one: the value gets pasted
#: into tickets, compose files and process listings, and a secret that travels that way is
#: already spent. Unset -> this route serves nobody.
WINDOW_TOKENS_ENV = "PROXYSHOP_BUYER_STORE_WINDOW_TOKENS"

#: The release floor, overridable per deployment.
WINDOW_FLOOR_ENV = "PROXYSHOP_BUYER_STORE_WINDOW_K"

#: The floor when nothing is configured. **Two, and not one.**
#:
#: This is not the knob SPEC §Non-goals pins at 1. That default governs
#: :func:`~buyer_svc.profile.build_profile` — one buyer, reading their own profile, where a
#: floor is meaningless because a release of one is always unique. This is a different release
#: to a different audience: a whole population handed to a store in one response. A floor of 1
#: there is not "the documented default", it is no floor at all on the one read where a floor
#: is the entire protection, so the minimum this route will serve under is 2 and a deployment
#: raises it from there.
DEFAULT_WINDOW_FLOOR = 2

#: Ceiling on rows read in one response. The window is a working set, not an export, and a
#: route whose cost grows with the buyer table is one a single request can use to stall the
#: service. When the table is larger than this the response says so rather than truncating
#: silently — a store that does not know it was truncated will read the window as the whole
#: population, which is a false statement this route would have made.
#:
#: The truncation and the floor interact, and the direction is the safe one, so it is written
#: down rather than left to be re-derived. Class sizes are counted over the rows actually
#: fetched, so a truncated read can only ever see a class as SMALLER than it is — never
#: larger. It therefore over-suppresses (a buyer whose class-mates fell past the cut is
#: withheld) and can never under-suppress. A ceiling that admitted the opposite error would be
#: a privacy bug hiding inside a performance guard.
MAX_WINDOW_ROWS = 5000

#: The columns that leave. `created_at` is deliberately absent; see the module docstring.
_SELECT = (
    "select pseudonym, buckets, provenance from app.buyer_accounts "
    f"order by pseudonym limit {MAX_WINDOW_ROWS + 1}"
)

#: One connection, replaced when the driver reports it closed. Same slot-behind-a-lock shape,
#: and the same reasoning, as ``auth.routes.build_profile_publisher``: `def` endpoints run in a
#: threadpool, so the lock is never held across a `connect` — during an outage that would queue
#: every thread behind one TCP timeout apiece and turn a database blip into a whole-service
#: one.
_slot = threading.Lock()
_held: dict[str, Any] = {"connection": None}


def set_window_connection(connection: Any) -> None:
    """Bind (or clear, with ``None``) the connection this route reads through.

    Exists for the same reason ``auth.routes.set_auth_service`` does: a test needs to drive the
    served route against a connection it controls, and a route that could only be reached with
    a live Postgres would be tested by mocking the thing under test instead.
    """
    with _slot:
        _held["connection"] = connection


def _connection() -> Any | None:
    """The current connection, opening one from the environment if there is none.

    ``None`` when :data:`~buyer_svc.auth.routes.APP_DSN_ENV` is unset, which is a database-less
    dev boot and answers ``503`` rather than crashing.

    The ``app`` role and not a new one. It is what this service already holds, it is what
    already writes this table, and — the load-bearing half — 0001 grants it **no USAGE on
    schema vault**. So this route cannot resolve a pseudonym to an email, and that is a
    property of the grant rather than of the SQL written above: rewriting the statement into a
    join against ``vault.pseudonym_history`` produces ``permission denied for schema vault``,
    not a leak.

    A deployment that wants the window read at an even narrower role than the one that writes
    it — ``exchange`` holds ``SELECT`` on ``app.*`` and no write anywhere — can open that
    connection at startup and hand it to :func:`set_window_connection`; the statement above is
    a ``SELECT`` and asks for nothing more. That is a tightening, not a requirement, and it is
    left to the deployment rather than read from a second variable this service is not handed.
    """
    with _slot:
        existing = _held["connection"]
    if existing is not None and not getattr(existing, "closed", False):
        return existing

    dsn = os.environ.get(APP_DSN_ENV)
    if not dsn:
        return None
    import psycopg  # noqa: PLC0415 - a database-less boot must not need the driver

    fresh = psycopg.connect(dsn, autocommit=True)
    with _slot:
        winner = _held["connection"]
        if winner is None or getattr(winner, "closed", False):
            _held["connection"] = fresh
            return fresh
    # Another thread won the race; close ours rather than leaking it.
    try:
        fresh.close()
    except Exception:  # pragma: no cover - closing a connection nobody used
        _log.debug("closing a raced-away window connection raised", exc_info=True)
    return winner


def window_tokens(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The ``{store_id: token}`` table, or ``{}`` when this deployment has configured none.

    Every refusal below returns ``{}``, which the route turns into a ``503`` naming the
    variable — a named-but-unreadable file is a misconfiguration, and the correct posture for
    one is "this route serves nobody", never "this route serves everybody".

    Empty ids and empty tokens are dropped rather than kept, so there is no row an empty
    presented bearer could compare equal to.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    path = source.get(WINDOW_TOKENS_ENV)
    if not path:
        return {}
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _log.warning("%s names a file that could not be read as JSON", WINDOW_TOKENS_ENV)
        return {}
    if not isinstance(body, Mapping):
        _log.warning("%s does not hold a {store_id: token} object", WINDOW_TOKENS_ENV)
        return {}
    return {str(k): str(v) for k, v in body.items() if str(k) and str(v)}


def window_floor(env: Mapping[str, str] | None = None) -> int:
    """The configured floor, never below :data:`DEFAULT_WINDOW_FLOOR`.

    A value that is not an integer at or above the default is treated as unset. Clamped rather
    than raising, and clamped **upward only**: a typo in an optional knob is not a reason to
    fail a boot, and it is emphatically not a reason to serve the population with no floor.

    >>> window_floor({})
    2
    >>> window_floor({"PROXYSHOP_BUYER_STORE_WINDOW_K": "5"})
    5
    >>> window_floor({"PROXYSHOP_BUYER_STORE_WINDOW_K": "1"})
    2
    >>> window_floor({"PROXYSHOP_BUYER_STORE_WINDOW_K": "banana"})
    2
    """
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get(WINDOW_FLOOR_ENV, "")
    try:
        configured = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_FLOOR
    return max(configured, DEFAULT_WINDOW_FLOOR)


def _classify(buckets: Any) -> tuple[Any, ...] | None:
    """The row's equivalence class, or ``None`` when it has none this route can trust.

    ``None`` is a refusal and its caller withholds the row. Two ways to get one, and both are
    "a row whose anonymity cannot be established", which is the only safe reading:

    * :func:`~buyer_svc.profile.equivalence_class` rejects the buckets outright (not a
      mapping);
    * the class it returns is unhashable, which happens when ``category_affinity`` holds
      objects rather than strings — ``tuple([{"a": 1}])`` cannot be a dict key. Before this
      guard that was an unhandled ``TypeError`` inside the handler, so one malformed row took
      the whole window to a 500 rather than removing itself from it.

    The table admits both: its only bucket constraint is ``jsonb_typeof(buckets) = 'object'``.
    ``publish_profile`` cannot write either today, and that is exactly the argument this module
    refuses to lean on elsewhere — the database is what makes a value true, and the database
    does not check this one.
    """
    try:
        key = equivalence_class(buckets)
        hash(key)
    except (TypeError, ValueError):
        return None
    return key


def release(rows: Sequence[Mapping[str, Any]], *, floor: int) -> tuple[list[dict[str, Any]], int]:
    """``(released, withheld)`` — the rows a store may see, and how many it may not.

    A row is released when its :func:`~buyer_svc.profile.equivalence_class` is shared by at
    least ``floor`` rows in this release. The class is computed by the profile module's own
    function rather than re-derived here, because "how many buyers look like this one" is the
    measurement the floor turns on and a measurement each caller re-derives drifts.

    **The buckets released are rebuilt FROM the class, not copied from the row**, and that is
    the load-bearing line in this function. It used to group by the class and then emit
    ``{key: row["buckets"].get(key) for key in BUCKET_KEYS}``, which is a *different*
    projection — and the two disagree exactly where it is most dangerous.
    ``equivalence_class`` maps a non-list ``category_affinity`` to ``()``, so two rows, one
    carrying the bare string ``"dana-reyes-portland-97205"`` in that field and one carrying
    nothing, grouped as a class of two and were BOTH released at floor 2 with ``withheld: 0``
    — one of them putting a unique quasi-identifier on the wire under a k-anonymity claim.
    MEASURED, before this rewrite. Emitting the class itself makes the grouped value and the
    released value the same object by construction, so no such gap can reopen: whatever the
    floor counted is precisely what leaves, and a value the class reader does not understand
    is normalised away rather than passed through.

    Order is the caller's, which is ``pseudonym`` order from the statement rather than
    insertion order, so where a row sits in the response says nothing about when it was
    written. That is all it buys, and the limit is worth naming: a store that reads the window
    twice can still difference the two and see which pseudonyms are new since its last read.
    What ordering removes is the *free* version of that — a store learning the arrival order of
    every buyer from a single response — and what bounds the rest is the floor, which is why a
    new buyer is invisible here until ``floor - 1`` others look like them.
    """
    keyed = [(_classify(row["buckets"]), row) for row in rows]
    classes: dict[tuple[Any, ...], int] = {}
    for key, _row in keyed:
        if key is not None:
            classes[key] = classes.get(key, 0) + 1

    released: list[dict[str, Any]] = []
    withheld = 0
    for key, row in keyed:
        if key is None or classes[key] < floor:
            withheld += 1
            continue
        # `equivalence_class` returns its five facets in BUCKET_KEYS order; the affinity comes
        # back as a tuple because the class has to be hashable, and JSON has no tuple.
        buckets = dict(zip(BUCKET_KEYS, key, strict=True))
        buckets["category_affinity"] = list(buckets["category_affinity"])
        released.append(
            {
                "pseudonym": row["pseudonym"],
                "buckets": buckets,
                "provenance": row["provenance"],
            }
        )
    return released, withheld


class StoreWindowRow(BaseModel):
    """One buyer as a store may see them. Exactly ``BuyerProfile``, plus where the row came from.

    ``provenance`` is the ``app.buyer_accounts`` column copied verbatim, and its two values are
    the vocabulary the table's own CHECK constraint admits. It is published rather than kept
    internal because a store shown a window that mixes real and manufactured buyers has to be
    able to tell which is which — and because the guarantee behind the label
    (``db/migrations/0005_buyer_accounts_provenance.sql``) is strong enough to be worth
    depending on.
    """

    pseudonym: str
    buckets: dict[str, Any]
    provenance: str


class StoreWindowView(BaseModel):
    """The whole release, and the two numbers a store needs to read it honestly.

    ``withheld`` is published for the reason it is counted: a store shown 34 rows and told
    nothing reads the window as the whole population. Told it is 34 of 42, it can reason about
    what it is looking at, and about how much of the network it is not being shown.
    """

    as_of: str
    store_id: str
    floor: int = Field(description="The k-anonymity floor this release was held to.")
    released: list[StoreWindowRow]
    withheld: int = Field(
        description="Rows suppressed because fewer than `floor` buyers share their buckets."
    )
    truncated: bool = Field(
        description="True when the table holds more rows than one response may carry."
    )


def _problem(status: int, reason: str, **detail: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": reason, **detail})


def _store_for(bearer: str, tokens: Mapping[str, str]) -> str | None:
    """The store whose token this is, or ``None``. Constant-time, and over every row."""
    if not bearer:
        return None
    found: str | None = None
    for store_id, token in tokens.items():
        if hmac.compare_digest(str(token), bearer):
            found = store_id
    return found


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return supplied.strip()


@router.get(
    STORE_WINDOW_PATH,
    response_model=StoreWindowView,
    summary="The pseudonymous buyer window a store may be shown",
)
def read_store_window(request: Request) -> Any:
    """The store-visible window: pseudonyms and coarse buckets, held to a k-anonymity floor.

    No identity, and not because a list of forbidden field names is checked on the way out —
    because the statement reads one table in one schema, and the role it reads under has no
    access to the schema where identity lives. There is no query this handler could be edited
    into that returns an email.
    """
    tokens = window_tokens()
    if not tokens:
        return _problem(
            503,
            "store-window-not-configured",
            detail=(
                f"this deployment holds no store-window token table, so the window serves "
                f"nobody. Point {WINDOW_TOKENS_ENV} at a file holding {{store_id: token}}"
            ),
        )

    store_id = _store_for(_bearer(request), tokens)
    if store_id is None:
        # Names no store and echoes nothing that arrived: a refusal quoting the presented
        # bearer would put it in every proxy log between here and the caller.
        return _problem(401, "unauthorized")

    connection = _connection()
    if connection is None:
        return _problem(
            503,
            "store-window-has-no-database",
            detail=(
                f"{APP_DSN_ENV} is unset, so this process holds no connection to the table the "
                "window is read from"
            ),
        )

    try:
        with connection.cursor() as cur:
            cur.execute(_SELECT)
            fetched = cur.fetchall()
    except Exception:
        # Never a 200 over a window that was not read. A store cannot tell an empty window
        # from an unread one, so an unread one must not be served as empty.
        _log.exception("the store window could not be read from app.buyer_accounts")
        if getattr(connection, "closed", False):
            set_window_connection(None)
        return _problem(500, "store-window-unreadable")

    truncated = len(fetched) > MAX_WINDOW_ROWS
    rows = [
        {
            "pseudonym": pseudonym,
            "buckets": buckets if isinstance(buckets, Mapping) else json.loads(buckets or "{}"),
            "provenance": provenance,
        }
        for pseudonym, buckets, provenance in fetched[:MAX_WINDOW_ROWS]
    ]

    floor = window_floor()
    released, withheld = release(rows, floor=floor)
    _log.info(
        "store window read by %s: %d released, %d withheld at floor %d",
        store_id,
        len(released),
        withheld,
        floor,
    )
    return {
        "as_of": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "store_id": store_id,
        "floor": floor,
        "released": released,
        "withheld": withheld,
        "truncated": truncated,
    }
