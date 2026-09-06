"""``GET /snapshot`` — the served TrustSnapshot the exchange caches (T-064 acceptance 3).

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
``X-Trust-Score-Version`` and ``X-Trust-As-Of``. A client caches on the ETag and refetches
when it changes, which is the "refreshes on a version bump" behaviour without inventing a
body shape the contract does not declare. ``snapshot_version`` also appears on every entry,
which the published ``TrustSnapshot`` DOES admit.

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
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ..events.errors import EventServiceError, StoreUnavailable
from ..scoring import BLACKLIST_STATUSES, Blacklist
from .builder import SNAPSHOT_VERSION, build_snapshot

__all__ = [
    "PUBLISHED_DIMENSION_FIELDS",
    "PUBLISHED_SNAPSHOT_FIELDS",
    "blacklist_for",
    "published_entry",
    "router",
    "stores_for",
]

router = APIRouter(tags=["trust"])

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
    moment = as_of or _instant(None)
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

    snapshot = build_snapshot(stores, blacklist=blacklist, as_of=moment)
    response.headers["ETag"] = f'"{snapshot["version"]}"'
    response.headers["X-Trust-Snapshot-Version"] = str(snapshot["version"])
    response.headers["X-Trust-Score-Version"] = str(snapshot["score_version"])
    response.headers["X-Trust-As-Of"] = str(snapshot["as_of"])
    response.headers["Cache-Control"] = "no-cache"
    return {store_id: published_entry(entry) for store_id, entry in snapshot["stores"].items()}
