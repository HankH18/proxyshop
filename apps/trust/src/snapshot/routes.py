"""``GET /snapshot`` — the TrustSnapshot the exchange is specified to cache (T-064 acc. 3).

T-261. ``build_snapshot`` was built and graded long before this file existed, and there was
no way to ASK for its output: ``apps/trust/src/main.py`` discovers routers by globbing
``<feature>/routes.py`` and this package had none, so ``/snapshot`` was **published** in
``packages/contracts/openapi/trust.openapi.json`` (and pinned in
``contracts.openapi.PINNED_ROUTES``) and served by nothing. The comment on
:data:`~.builder.SNAPSHOT_VERSION` — "what the exchange client caches on and refreshes
against" — described a client with nothing to talk to.

This is the SERVER half only. The exchange-side client, its cache and the version-bump
refresh live in ``apps/exchange`` and are that lane's to build; a served route is the
precondition for all three, which is why it is worth landing on its own.

What the body is, and why it is not ``build_snapshot``'s envelope
-----------------------------------------------------------------
The published response is ``store_id -> TrustSnapshot`` — a bare mapping, not
``{version, stores, delistings, ...}``. And ``TrustSnapshot`` (and ``TrustDimensionState``
inside it) are declared ``additionalProperties: false``, so the entries ``store_entry``
produces cannot be served verbatim: they carry ``business_identity``, ``episodes``,
``observations``, ``decided_observations``, ``as_of``, and per-dimension ``coverage`` /
``observations`` / ``decided`` / ``evidence``. Every one of those would be a schema error at
the door. :func:`published_entry` projects an entry down to exactly the published property
set, so what is served validates against the document that advertises it.

``business_identity`` is dropped for a second reason that is not schema bookkeeping: the
blacklist is identity-bound precisely so a delisted operator cannot return under a fresh
``store_id``, and publishing the identity key to every consumer of this endpoint would hand
out the join that makes that binding worth having.

Where the version goes
----------------------
The thing the exchange is specified to cache on is :data:`~.builder.SNAPSHOT_VERSION`, and
the published body has nowhere to put it — the mapping's values are ``TrustSnapshot``\\ s and
nothing else. It is served as ``ETag`` (so a conditional GET is available to any client that
wants one) and, spelled out, as ``X-Trust-Snapshot-Version``, alongside
``X-Trust-Score-Version`` and ``X-Trust-As-Of``. A client that caches on the ETag and
refetches when it changes gets the "refreshes on a version bump" behaviour without inventing
a body shape the contract does not declare. ``snapshot_version`` also appears on every entry,
which the published ``TrustSnapshot`` DOES admit.

No such client exists yet — ``git grep SNAPSHOT_VERSION -- . ':(exclude)apps/trust'`` returns
nothing. What lands here is the cache KEY, served; the caching and the refresh are acceptance
3 itself and are still to be built in ``apps/exchange``.

Where the data comes from
-------------------------
Three tables, read through ONE per-request connection borrowed from
:func:`trust.claims.routes.connection_for` — deliberately, so this route and
``/claims/verifications`` resolve one DSN between them rather than two. ``trust_rw``'s grant
set is what makes this possible: full DML on ``ledger`` and READ-ONLY on ``app``, which is
exactly SELECT on ``app.sellers`` and ``app.seller_blacklist`` plus SELECT on
``ledger.trust_observations``.

Both halves are injectable on ``app.state`` (``snapshot_stores`` / ``snapshot_blacklist``),
the same shape ``/events`` uses for ``event_store``, so the route can be driven without a
database and so a deployment can point it at a different source without editing this file.

``as_of`` is a query parameter and defaults to the serve instant. ``build_snapshot`` refuses
to read a clock itself and says why — a snapshot computed against ``now()`` cannot be told
from a stale one — so the instant is chosen HERE, once, stamped into the response, and
carried into every score. A caller replaying a past decision passes the instant it was taken
at and gets that decision back rather than today's.

Why ``as_of`` is screened at the door
-------------------------------------
That parameter is chosen by an anonymous caller — this route takes no credential — and it
is written into the ``X-Trust-As-Of`` response header. Unscreened, it was three defects in
one line, all measured against a real uvicorn server (a ``TestClient`` shows none of them:
it never hands a header to an HTTP encoder):

* **a 5xx the caller picks.** A value outside Latin-1 raised ``UnicodeEncodeError`` inside
  Starlette's header assignment, *after* the whole snapshot had been computed — 500.
* **a dropped connection, which is worse.** A value carrying CR, LF or NUL is Latin-1
  encodable and is not a legal header value: uvicorn closed the connection with no status
  at all, so the caller could not tell a refusal from a network fault.
* **caller-controlled bytes in a response header**, which is the shape of header injection.
  With no stores present the scorer was never reached, so an unparseable ``as_of`` came
  back ``200`` with the string echoed verbatim into the header; with one store present the
  same value became a 500, because ``trust.scoring._parse_instant`` raises a bare
  ``ValueError`` and this handler caught only ``EventServiceError``.

:func:`validated_as_of` closes all three in the same place and in the fail-closed
direction: a value that is not a bounded, renderable, parseable instant is refused with a
422 that names the problem and never repeats the value. A header is emitted only for a
value this process has already proved it can render.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, NoReturn

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ..events.errors import EventServiceError, StoreUnavailable
from ..scoring import (
    BLACKLIST_STATUSES,
    Blacklist,
    UnknownObservationType,
    UnknownTrustDimension,
)

# The scorer's OWN parser, imported private-and-deliberately: the door and the scorer must
# agree on what an instant is, and two parsers written to the same spec is exactly how a
# value gets admitted here and then raises there — which is the 500 this door exists to
# close. One function cannot drift from itself.
from ..scoring.engine import _parse_instant
from .builder import SNAPSHOT_VERSION, build_snapshot

__all__ = [
    "MAX_AS_OF_LENGTH",
    "PUBLISHED_DIMENSION_FIELDS",
    "PUBLISHED_SNAPSHOT_FIELDS",
    "UNRENDERABLE_HEADER_CHARACTERS",
    "blacklist_for",
    "published_entry",
    "router",
    "stores_for",
    "validated_as_of",
]

router = APIRouter(tags=["trust"])

#: The longest ``as_of`` this door admits. An RFC-3339 instant with microsecond precision
#: and a numeric offset is 32 characters, so 64 is twice what any caller needs.
#:
#: Not redundant with the parse check: ``datetime.fromisoformat`` accepts ANY number of
#: fractional-second digits, so ``"2026-01-01T00:00:00." + "0" * 200 + "Z"`` is a perfectly
#: good instant — measured — and this value is written into a response header. A caller who
#: can choose an unbounded header value chooses the size of every response served.
MAX_AS_OF_LENGTH = 64

#: Characters a response header value cannot carry: the C0 controls (CR, LF and NUL among
#: them), DEL, and the C1 controls.
#:
#: This screen is redundant with NEITHER of the other two, and both halves were measured
#: against a real uvicorn server on the code as it shipped:
#:
#: * Latin-1 encodability does not imply legality. ``"2026-02-01T00:00:00Z\r\n"`` encodes
#:   fine and is a response split; uvicorn answered such a response by CLOSING THE
#:   CONNECTION with no status at all, which is worse than a 500 — the caller cannot tell a
#:   refusal from a network fault.
#: * Parseability does not imply legality either. ``_parse_instant`` ``strip()``\\ s trailing
#:   whitespace, so that same CRLF value parses; and CPython's ``fromisoformat`` parses
#:   straight through a trailing NUL, so ``"2026-02-01T00:00:00Z\x00"`` was a perfectly good
#:   instant to the scorer and a dropped connection on the wire.
#:
#: The same lesson ``trust.events.routes.UNRENDERABLE_IDENTIFIER_CHARACTERS`` records for
#: caller-chosen identifiers, reached independently here on a different parameter.
UNRENDERABLE_HEADER_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _refuse_as_of(reason: str, why: str) -> NoReturn:
    """Refuse ``as_of`` with a 4xx that names the problem and never repeats the value.

    The value is caller-chosen, unbounded, and in every case that reaches here carries
    something this process could not render. Quoting it back would put the caller's own
    bytes on the wire in a different field — which is the defect this door closes, not a
    helpful error message. The ``reason`` code is server-owned vocabulary and says which of
    the four screens refused, which is what a caller can actually act on.
    """
    raise HTTPException(
        422,
        {
            "error": "as_of_not_servable",
            "field": "as_of",
            "reason": reason,
            "message": (
                f"the as_of query parameter {why}. It must be one RFC-3339 instant of at "
                f"most {MAX_AS_OF_LENGTH} characters, Latin-1 encodable and free of control "
                f"characters: it is the instant every score is decayed against and it is "
                f"reported back in the X-Trust-As-Of response header. The offending value "
                f"is deliberately not repeated here."
            ),
        },
    )


def validated_as_of(as_of: str | None) -> str:
    """The instant this request is served against, or a 422 saying why it is not one.

    Four screens, in this order, and the order is load-bearing:

    1. **length**, so nothing longer than a timestamp is examined or emitted at all;
    2. **control characters**, before anything tries to render or parse the value — this is
       the screen that closes the dropped connection, and it catches values the other three
       all accept (see :data:`UNRENDERABLE_HEADER_CHARACTERS`);
    3. **Latin-1**, the header codec on the wire, which is where the ``UnicodeEncodeError``
       500 came from;
    4. **parseability**, through the scorer's own ``_parse_instant``, so a value that is not
       an instant is refused HERE with a 422 instead of raising a bare ``ValueError`` deep
       inside ``build_snapshot`` and surfacing as a 500.

    Absent or empty is not a refusal: ``as_of`` is optional and documented to default to the
    serve instant, so ``None`` and a value that parses to "no instant" both yield the serve
    instant exactly as before. Everything else is returned as the caller spelled it — this
    route's contract is that a replayed decision is stamped with the instant it was replayed
    at — and by the time it is returned this process has proved it can both render and score
    that spelling.

    The one normalisation is surrounding whitespace, and it is not cosmetic: HTTP does not
    consider leading or trailing OWS part of a field value, so an unstripped instant is a
    header the client reads back DIFFERENTLY from the one this handler set — measured,
    ``" 2026-02-01T00:00:00Z "`` came back trimmed. ``_parse_instant`` strips identically,
    so stripping here keeps what is scored, what is set and what is read the same string.
    """
    if as_of is None:
        return _instant(None)
    if len(as_of) > MAX_AS_OF_LENGTH:
        _refuse_as_of(
            "too_long",
            f"is {len(as_of)} characters, over the {MAX_AS_OF_LENGTH}-character ceiling",
        )
    if UNRENDERABLE_HEADER_CHARACTERS.search(as_of):
        _refuse_as_of(
            "control_character",
            "carries a control character (C0, DEL or C1). A header value cannot hold one: "
            "CR/LF is a response split, and uvicorn answers such a response by closing the "
            "connection with no status at all",
        )
    try:
        as_of.encode("latin-1")
    except UnicodeEncodeError:
        _refuse_as_of(
            "not_latin_1",
            "is outside Latin-1. HTTP header values are Latin-1 on the wire, so such a "
            "value raised UnicodeEncodeError after the whole snapshot had been computed",
        )
    normalized = as_of.strip()
    try:
        parsed = _parse_instant(normalized)
    except ValueError:
        _refuse_as_of(
            "not_an_instant",
            "is not an RFC-3339 instant. Decay is a function of recorded time (D17/S3), so "
            "an unscoreable instant is refused rather than served as a header",
        )
    return normalized if parsed is not None else _instant(None)


#: The property names ``TrustSnapshot`` declares. It is ``additionalProperties: false``, so
#: this is a whitelist and not a preference — anything else served here is a schema error
#: against the document that publishes this route.
PUBLISHED_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "store_id",
    "score",
    "confidence",
    "effective_sample_size",
    "score_version",
    "snapshot_version",
    "computed_through_event",
    "low_data",
    "dims",
    "blacklisted",
)

#: The same, for ``TrustDimensionState``. ``score()`` additionally reports ``coverage``,
#: ``observations``, ``decided`` and ``evidence`` per dimension; they are trust-engine
#: internals and the schema admits none of them.
PUBLISHED_DIMENSION_FIELDS: tuple[str, ...] = ("alpha", "beta", "decayed_at")

#: Read the stores in one pass, newest observation last. ``app.sellers`` is the roster: a
#: seller with no observations still gets a snapshot, and it is the ``low_data`` one, which
#: is the entry the exchange most needs to be told about. A LEFT JOIN rather than a join on
#: ``trust_observations`` alone, because the latter would silently omit exactly those stores.
_STORES_SQL = """
select
  s.store_id,
  s.business_identity,
  o.dim,
  o.observation_type,
  o.weight,
  o.observed_at
from app.sellers as s
left join ledger.trust_observations as o on o.store_id = s.store_id
order by s.store_id, o.observed_at
"""

#: One live row per identity is what ``seller_blacklist_one_live_entry_idx`` guarantees for
#: the blocking statuses; ``expired`` rows are read too so a lapsed listing is still visible
#: to the expiry path in :mod:`.delisting` rather than vanishing from the registry.
_BLACKLIST_SQL = """
select business_identity, reason_code, status, expires_at
from app.seller_blacklist
order by business_identity, created_at
"""


def _instant(value: Any) -> str:
    """An RFC-3339 UTC instant with millisecond precision, as the ledger spells them."""
    moment = value if isinstance(value, datetime) else datetime.now(tz=UTC)
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def published_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """One ``store_entry`` projected onto the published ``TrustSnapshot`` property set.

    A projection and never a rename: every key kept is spelled exactly as the schema spells
    it, and a key the entry does not carry is simply absent rather than filled with a
    placeholder — ``confidence`` and ``low_data`` are nullable in the schema, but "we did not
    compute it" and "we computed null" are different claims and only the first one is true.

    ``snapshot_version`` is added here because the entry has no reason to know it and the
    schema declares it: it is the value the exchange client caches on, so serving it inside
    each entry as well as in the headers means a cached entry carries its own provenance.
    """
    published: dict[str, Any] = {}
    for field in PUBLISHED_SNAPSHOT_FIELDS:
        if field == "dims":
            dims = entry.get("dims") or {}
            published["dims"] = {
                name: {
                    key: state[key]
                    for key in PUBLISHED_DIMENSION_FIELDS
                    if isinstance(state, Mapping) and key in state
                }
                for name, state in dims.items()
            }
        elif field == "snapshot_version":
            published["snapshot_version"] = SNAPSHOT_VERSION
        elif field in entry:
            published[field] = entry[field]
    return published


def _row_value(row: Any, index: int, name: str) -> Any:
    """One column, whether the driver hands back tuples or mappings."""
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


def _stores_from_rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Fold ``(store, observation)`` rows into one record per store.

    A LEFT JOIN gives a store with no observations exactly one row whose observation columns
    are all NULL; that row contributes the store and no observation, which is what makes such
    a store come back flagged ``low_data`` instead of missing.
    """
    stores: dict[str, dict[str, Any]] = {}
    for row in rows:
        store_id = _row_value(row, 0, "store_id")
        if store_id is None:
            continue
        record = stores.setdefault(
            str(store_id),
            {
                "store_id": str(store_id),
                "business_identity": _row_value(row, 1, "business_identity"),
                "observations": [],
            },
        )
        dim = _row_value(row, 2, "dim")
        observation_type = _row_value(row, 3, "observation_type")
        if dim is None or observation_type is None:
            continue
        observed_at = _row_value(row, 5, "observed_at")
        record["observations"].append(
            {
                "dim": str(dim),
                # `trust.scoring.score` reads `type`; the column is `observation_type`. The
                # mapping is here rather than in an alias in the SQL so the query still reads
                # like the table it queries.
                "type": str(observation_type),
                "weight": _row_value(row, 4, "weight"),
                "observed_at": _instant(observed_at)
                if isinstance(observed_at, datetime)
                else observed_at,
            }
        )
    return [stores[key] for key in sorted(stores)]


def _blacklist_from_rows(rows: Iterable[Any]) -> Blacklist:
    """Build the in-process registry from ``app.seller_blacklist``.

    A status outside the published vocabulary is kept rather than dropped, recorded through
    ``BlacklistEntry`` directly so ``Blacklist.add``'s write-time gate cannot reject it. That
    is the fail-CLOSED direction and the one :mod:`..scoring.blacklist` documents: a row in a
    state this process has not heard of BLOCKS, where dropping it would quietly release the
    store — which is the single direction R12 forbids.
    """
    from ..scoring import BlacklistEntry

    registry = Blacklist()
    entries: dict[str, Any] = {}
    for row in rows:
        identity = _row_value(row, 0, "business_identity")
        if identity is None:
            continue
        expires_at = _row_value(row, 3, "expires_at")
        entries[str(identity)] = BlacklistEntry(
            business_identity=str(identity),
            reason_code=str(_row_value(row, 1, "reason_code") or "unrecorded"),
            status=str(_row_value(row, 2, "status") or BLACKLIST_STATUSES[0]),
            expires_at=_instant(expires_at) if isinstance(expires_at, datetime) else expires_at,
        )
    for identity, entry in entries.items():
        registry._entries[identity] = entry  # noqa: SLF001 - see the docstring above
    return registry


def stores_for(request: Request) -> list[dict[str, Any]]:
    """The store records this snapshot is built from.

    Resolution order: whatever was injected on ``app.state.snapshot_stores`` (a list, or a
    callable returning one), otherwise ``app.sellers`` LEFT JOIN ``ledger.trust_observations``
    read on this request's connection.
    """
    injected = getattr(request.app.state, "snapshot_stores", None)
    if injected is not None:
        return list(injected() if callable(injected) else injected)

    from ..claims.routes import connection_for

    with connection_for(request) as connection, connection.cursor() as cursor:
        cursor.execute(_STORES_SQL)
        return _stores_from_rows(cursor.fetchall())


def blacklist_for(request: Request) -> Any:
    """The blacklist registry this snapshot resolves ``blacklisted`` against.

    Injectable on ``app.state.snapshot_blacklist``; otherwise read from
    ``app.seller_blacklist``. Never a module-level singleton — :func:`trust.scoring.
    is_blacklisted` takes the registry for exactly this reason.
    """
    injected = getattr(request.app.state, "snapshot_blacklist", None)
    if injected is not None:
        return injected() if callable(injected) else injected

    from ..claims.routes import connection_for

    with connection_for(request) as connection, connection.cursor() as cursor:
        cursor.execute(_BLACKLIST_SQL)
        return _blacklist_from_rows(cursor.fetchall())


@router.get(
    "/snapshot",
    operation_id="getAllTrustSnapshots",
    summary="Every store's snapshot, for exchange consumption.",
)
def get_all_trust_snapshots(
    request: Request,
    response: Response,
    as_of: str | None = Query(
        default=None,
        description=(
            "The instant every score is decayed against, RFC-3339. Defaults to the serve "
            "instant. Explicit because a snapshot computed against a clock cannot be told "
            "from a stale one."
        ),
    ),
) -> dict[str, dict[str, Any]]:
    """``store_id -> TrustSnapshot``, exactly as ``validate_bid(..., trust_snapshot=...)`` reads it.

    A store with no row here is treated by the exchange as an UNAVAILABLE eligibility read and
    denied (R12, fail-closed) — never admitted on the grounds that no blacklist entry was
    found. That is why an unreadable datastore is a 503 rather than an empty mapping: an empty
    body is a valid answer meaning "no stores", and serving it for "the database is down"
    would tell the exchange to deny everything for a reason it cannot distinguish from the
    truthful one.

    ``delistings`` — the ``blacklisted`` / ``blacklist_expired`` events this snapshot implies
    — is deliberately NOT served. It is a recommendation to a ledger writer, not part of the
    published ``TrustSnapshot``, and putting it in this body would make a read-only cache
    refresh look like a delisting decision.
    """
    moment = validated_as_of(as_of)
    try:
        stores = stores_for(request)
        blacklist = blacklist_for(request)
    except StoreUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "store_unavailable",
                "message": (
                    f"the trust snapshot could not be read: {exc}. Fail closed — an empty "
                    f"snapshot is a valid answer meaning 'no stores' and would be read as one."
                ),
            },
        ) from exc
    except EventServiceError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "store_unavailable", "message": str(exc)},
        ) from exc

    try:
        snapshot = build_snapshot(stores, blacklist=blacklist, as_of=moment)
    except (ValueError, UnknownObservationType, UnknownTrustDimension) as exc:
        # The scorer raises a BARE ``ValueError`` on an unparseable timestamp, and this
        # handler used to catch only ``EventServiceError`` — so an unparseable ``as_of``
        # was a 500 an anonymous caller could choose. ``moment`` has been through
        # ``validated_as_of``, which parses with this same function, so it cannot be the
        # cause any more; the re-check is what keeps that true if the two ever drift, and
        # it is the belt to the door's braces. A caller-chosen value must never be able to
        # pick this service's status class.
        try:
            _parse_instant(moment)
        except ValueError:
            _refuse_as_of(
                "not_an_instant",
                "is not an RFC-3339 instant and reached the scorer, which refused it",
            )
        # So this is the SOURCE, not the request: a stored row the scorer will not read.
        # 503 and never ``{}`` for the reason this route already documents — the exchange
        # denies a store with no row here (R12), so an empty body for "one row is poison"
        # would silently deny every store. The offending value is not quoted: rows in
        # ``ledger.trust_observations`` are written from ``POST /events``, so it is
        # caller-influenced too, and the scorer's own message interpolates it.
        raise HTTPException(
            status_code=503,
            detail={
                "error": "store_unavailable",
                "message": (
                    "a stored trust observation carries a timestamp, dimension, type or "
                    "weight the scorer refuses, so no snapshot can be computed. Fail "
                    "closed — an empty snapshot is a valid answer meaning 'no stores' and "
                    "would be read as one."
                ),
            },
        ) from exc

    response.headers["ETag"] = f'"{snapshot["version"]}"'
    response.headers["X-Trust-Snapshot-Version"] = str(snapshot["version"])
    response.headers["X-Trust-Score-Version"] = str(snapshot["score_version"])
    response.headers["X-Trust-As-Of"] = str(snapshot["as_of"])
    response.headers["Cache-Control"] = "no-cache"
    return {store_id: published_entry(entry) for store_id, entry in snapshot["stores"].items()}
