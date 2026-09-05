"""``POST /internal/outcomes`` — the door the bandit learns through (T-266, D31/D32, R16).

``packages/contracts/openapi/exchange.openapi.json`` has published this operation
(``operationId: recordOutcome``, "Trust reports an outcome back to the exchange for the bandit
update") since the contract was written, and until this module existed the exchange answered it
with a 404. :mod:`exchange.policy.bandit` — the Thompson sampler, its fail-closed R12 eligibility
rule, the exploration floor and the water-filling loop — was complete, tested and lint-enforced,
and :func:`~.bandit.update` had no production caller at all. R16's loop was open at both ends:
nothing fed the posterior and nothing could.

This module closes the *feeding* end, and it closes it by actually calling
:func:`~.bandit.update` rather than by accepting a body and discarding it. A door that swallows
its input is the same defect as a door that does not exist; it is merely harder to measure.

What this module owns, and what it deliberately does not
--------------------------------------------------------

**It decides what a "conversion" is, and the decision is written down here because it is a
decision.** :func:`~.bandit.update` folds Bernoulli outcomes — ``{store_id, cluster_id,
converted}`` — while the published payload is a ``TrustEventPayload``: a ``LedgerEvent``, a
``TrustDimension`` and a signed ``delta``. Nothing in that object is named ``converted``, so the
mapping is a judgement:

* ``delta > 0`` is a win, ``delta < 0`` is a loss;
* ``delta == 0`` (and a delta that is not a real number) is **not an outcome at all** and is
  folded into nothing — the same rule ``update`` applies to a record with no ``converted`` flag,
  for the same reason: "an outcome with no result is not an outcome".

The alternative was to key on ``event.kind`` — ``order_paid`` a win, ``refund`` a loss — and it
was rejected on measurement rather than taste. Of the eighteen kinds in
``contracts.LedgerEventKind`` exactly two are transactional, so kind-keying would discard the
other sixteen and leave the bandit unable to see a store that ships late, misstates a price or
is reported by every buyer and is simply never refunded. ``delta`` is the *trust service's own
verdict*, computed by the service that owns the question; reading its sign is the exchange
deferring to it, while re-deriving a verdict from ``kind`` would be the exchange having a second
opinion about trust — which D53 puts inside one trust system on purpose.

**An outcome with no cluster is refused, not bucketed.** ``PseudonymousContext.cluster_id`` is
optional in the protocol, and the bandit's whole routing rule is that "an outcome in
``cluster-1`` cannot move ``cluster-2``'s exposure". Inventing a shared bucket — the way
:data:`~exchange.ranking.FALLBACK_AUCTION_ID` invents an auction id — would pool unrelated
clusters into one posterior, which is worse than refusing: an auction id is cosmetic, a cluster
id is the key the model is indexed by. So a schema-valid payload carrying no cluster is a 400,
and it says so.

**What is persisted, said plainly: nothing.** The posteriors live in
:class:`InMemoryBanditPosteriors`, a process-local book reached through
:attr:`app.state.bandit_posteriors` — the same injected-port shape
:attr:`app.state.auction_bids` uses in ``accept/routes.py``. They are lost on restart, they are
not shared between replicas, and two uvicorn workers behind one load balancer keep two different
models. D26 asks for posteriors in Redis and this is not that; it is the seam Redis plugs into
(:func:`configure_outcomes`). The cost of the gap is bounded and worth stating exactly: the
bandit adjusts **exposure and exploration only** — it never touches rank, price or eligibility —
so a lost posterior costs exploration accuracy, never money and never a wrong shortlist.

**What still has no consumer, and this module does not pretend otherwise.**
:func:`~.bandit.exposure` has no production call site: nothing on the served path reads the state
this door writes. That is the *other* half of R16's loop and it belongs to whoever wires exposure
into candidate selection; it is reported here rather than fixed here, because wiring exposure
into the served auction would change what a buyer is shown, and this ticket is about a published
door that answered 404.

**The route is unauthenticated, and its name does not change that.** ``/internal/`` is a naming
convention; this service has no authentication of any kind (``git grep -nE
"Depends|api_key|Authorization" apps/exchange/src`` is empty). So the book is bounded in both
directions an anonymous caller can push on — :data:`DEFAULT_BANDIT_STORES` pairs of roster and
:data:`DEFAULT_MAX_OUTCOME_BYTES` of body — and every refusal is a decision this module makes on
purpose rather than an exception escaping it. Nothing here can answer 500: the handler is a total
wrapper over :func:`_record_outcome` in the same shape ``store_agent.external.door.receive_bid``
wraps ``_receive_bid``, and it fails **closed** — an error nobody anticipated answers 503 ("this
exchange did not record it"), never 204, because 204 is the word "Recorded." and a door that says
that without recording is worse than one that 404s.
"""

from __future__ import annotations

import json
import math
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from contracts.protocol import TrustEventPayload
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response

from .. import describe_exception, redact_addresses
from .bandit import BanditState, initial_state, update

__all__ = [
    "DEFAULT_BANDIT_CLUSTERS",
    "DEFAULT_BANDIT_STORES",
    "DEFAULT_MAX_OUTCOME_BYTES",
    "InMemoryBanditPosteriors",
    "configure_outcomes",
    "router",
]

router = APIRouter(tags=["policy"])

#: The most a body may weigh before it is refused unread.
#:
#: ``TrustEventPayload`` is a small object — five fields and a ledger event — but
#: ``LedgerEvent.payload`` is an OPEN mapping by design, so the one field that has no schema is
#: the one an anonymous caller controls the size of. 64 KiB is roughly three hundred times the
#: published example and still far under the 256 MiB this service runs in
#: (``apps/exchange/compose.yaml``). Counted while STREAMING, never by trusting
#: ``Content-Length``: a chunked body sends no length, and a length a caller writes is a claim.
DEFAULT_MAX_OUTCOME_BYTES = 64 * 1024

#: How many stores and clusters one process's bandit state covers at once.
#:
#: A cap at all, because a ``(cluster, store)`` pair is created by *the caller naming one* and
#: this route is unauthenticated: without a bound, a loop posting fresh ids is a memory leak with
#: a public handle on it — the same hazard :class:`~..accept.routes.InMemoryAuctionBids` and
#: :class:`~..ranking.serving.ShortlistStore` are bounded against, and the same eviction rule
#: (oldest touched first).
#:
#: These numbers also bound CPU, which matters more here than in either sibling: the state is
#: re-seeded from the trust snapshot on every recorded outcome (see
#: :meth:`InMemoryBanditPosteriors.record` for why), so one request costs
#: ``stores x clusters`` posterior objects. 256 x 32 is ~8k — well under a millisecond — while
#: 4096 x 512 would be two million and would make this door a CPU amplifier anyone could point
#: at the service.
DEFAULT_BANDIT_STORES = 256
DEFAULT_BANDIT_CLUSTERS = 32

#: The exploration floor new state is seeded with. Zero — the same default
#: :func:`~.bandit.initial_state` applies — because the floor is a *policy* an operator sets, and
#: a door that invented one would be this module deciding how much of every cluster goes to
#: exploration on the strength of nobody having said.
DEFAULT_EXPLORATION_FLOOR = 0.0

#: Refusal codes. Prose is safe to publish (nothing here interpolates a caller's value except
#: field NAMES, and those are swept by :func:`~..safe_text.redact_addresses` and truncated).
REASON_BODY_TOO_LARGE = "body_too_large"
REASON_BODY_NOT_JSON = "body_not_json"
REASON_BODY_NOT_AN_OBJECT = "body_not_a_json_object"
REASON_SCHEMA_INVALID = "outcome_schema_invalid"
REASON_NO_CLUSTER = "outcome_carries_no_cluster"

#: How much of a field path a refusal may quote, and how many. Field paths come from pydantic's
#: ``loc``, and under ``extra="forbid"`` a ``loc`` is a KEY THE CALLER WROTE — so it is bounded
#: and redacted like any other echoed value.
_MAX_REPORTED_FIELDS = 8
_MAX_REPORTED_FIELD_CHARS = 64


class _BodyRefused(Exception):
    """A body this door will not read further, carrying the code it is refused under."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# =====================================================================================
# The posterior book — where the bandit's state lives
# =====================================================================================
class InMemoryBanditPosteriors:
    """A process-local Beta-Bernoulli state, grown one recorded outcome at a time.

    Bounded in both dimensions a caller can push on. Beyond the caps the least recently touched
    store or cluster is dropped, oldest first, exactly as the two sibling in-process stores in
    this app evict — and never the pair this call just named, which is moved to the newest end
    before any eviction runs.

    **The state is re-seeded from the trust snapshot on every call, and that is deliberate rather
    than lazy.** :func:`~.bandit.initial_state` is what reads R12: it decides, fail-closed, which
    stores are ``blacklisted`` and which are ``low_data``. Seeding once and never looking again
    would leave a store blacklisted after its first outcome still marked eligible in this state
    for as long as the process lives, which is the wrong direction for a rule whose whole point is
    that "we could not tell" denies. Re-seeding costs ``stores x clusters`` posteriors per
    request; :data:`DEFAULT_BANDIT_STORES` and :data:`DEFAULT_BANDIT_CLUSTERS` are what keep that
    number small, and they are the reason those caps are as low as they are.

    Learning survives the re-seed: every posterior the previous state already held is carried
    over, and only genuinely new ``(cluster, store)`` pairs take a fresh trust-seeded prior.
    """

    def __init__(
        self,
        *,
        max_stores: int = DEFAULT_BANDIT_STORES,
        max_clusters: int = DEFAULT_BANDIT_CLUSTERS,
        exploration_floor: float = DEFAULT_EXPLORATION_FLOOR,
    ) -> None:
        self.max_stores = max(1, int(max_stores))
        self.max_clusters = max(1, int(max_clusters))
        # Clamped rather than validated: `initial_state` raises outside [0, 1], and a raise here
        # would turn a deployment's typo into a 503 on every outcome instead of a floor of 0 or 1.
        floor = float(exploration_floor)
        self.exploration_floor = 0.0 if not math.isfinite(floor) else min(1.0, max(0.0, floor))
        self._state: BanditState | None = None
        self._stores: OrderedDict[str, None] = OrderedDict()
        self._clusters: OrderedDict[str, None] = OrderedDict()

    def state(self) -> BanditState | None:
        """The current state, or ``None`` before the first outcome has been recorded."""
        return self._state

    def record(
        self,
        store_id: str,
        cluster_id: str,
        converted: bool,
        *,
        trust_snapshot: Any = None,
    ) -> BanditState:
        """Fold one outcome into this process's state and return the state it produced."""
        store = str(store_id)
        cluster = str(cluster_id)

        self._stores.pop(store, None)
        self._stores[store] = None
        self._clusters.pop(cluster, None)
        self._clusters[cluster] = None
        while len(self._stores) > self.max_stores:
            self._stores.popitem(last=False)
        while len(self._clusters) > self.max_clusters:
            self._clusters.popitem(last=False)

        # A snapshot that is not a mapping is no snapshot. `initial_state` would raise on it;
        # an empty one denies every store instead, which is R12's own direction (T-312's sibling
        # accessor `ranking.serving.trust_snapshot_of` defaults to exactly this).
        snapshot: Mapping[str, Any] = trust_snapshot if isinstance(trust_snapshot, Mapping) else {}
        seeded = initial_state(
            tuple(self._stores),
            tuple(self._clusters),
            snapshot,
            {"exploration_floor": self.exploration_floor},
        )

        previous = self._state
        if previous is not None:
            for cluster_key, per_store in seeded.posteriors.items():
                carried = previous.posteriors.get(cluster_key, {})
                for store_key in list(per_store):
                    kept = carried.get(store_key)
                    if kept is not None:
                        per_store[store_key] = kept

        self._state = update(
            seeded,
            [{"store_id": store, "cluster_id": cluster, "converted": converted}],
        )
        return self._state

    def __len__(self) -> int:
        return len(self._stores) * len(self._clusters)


# =====================================================================================
# Wiring
# =====================================================================================
def configure_outcomes(app: FastAPI, *, posteriors: Any | None = None) -> None:
    """Wire this door's dependencies. Anything omitted keeps what is already there.

    ``posteriors`` is the seam D26's Redis-backed model plugs into. It has to expose
    ``record(store_id, cluster_id, converted, *, trust_snapshot=...)``; anything else is a
    misconfigured deployment and is answered 503 rather than dressed up as a decision about the
    caller's outcome.
    """
    if posteriors is not None:
        app.state.bandit_posteriors = posteriors


def _posteriors(request: Request) -> Any:
    """This app's posterior book, created on first use.

    The default is the real bounded book rather than a null object, and that asymmetry with
    :class:`~..accept.routes.NoRecordedBids` is on purpose: refusing to *record* is not a safety
    property. A bid store that knows nothing refuses an accept, which protects a buyer's money; a
    posterior book that knows nothing only makes exploration worse, and shipping a door whose
    default discards its input is the defect this module exists to close.
    """
    book = getattr(request.app.state, "bandit_posteriors", None)
    if book is None:
        book = InMemoryBanditPosteriors()
        request.app.state.bandit_posteriors = book
    return book


def _trust_snapshot(request: Request) -> Any:
    """The same ``{store_id: row}`` the auction and ranking doors read, off the same key.

    Imported here rather than at module scope: ``ranking.serving`` is a sibling feature and this
    is a collaborator lookup, not a dependency of the policy package — which the frozen
    acceptance suite imports as a library.
    """
    from ..ranking.serving import trust_snapshot_of  # noqa: PLC0415 - sibling feature

    return trust_snapshot_of(request.app)


def _bind_the_deployment(request: Request) -> None:
    """Run the composition root once for this app. See ``auction/routes.py``'s twin.

    Taken here because an outcome can arrive at a process that never served the auction it
    describes, and that process would otherwise seed every posterior against an empty trust
    snapshot while the process next to it used the deployment's real one.
    """
    from ..composition import DeploymentConfigurationError, ensure_configured  # noqa: PLC0415

    try:
        ensure_configured(request.app)
    except DeploymentConfigurationError as exc:
        raise HTTPException(status_code=503, detail=redact_addresses(exc)) from exc


# =====================================================================================
# Reading the body without handing a caller's values to the error renderer
# =====================================================================================
#
# This door declares no pydantic request model, and that is a security decision rather than a
# style one. FastAPI renders a request-validation failure by echoing the offending INPUT, and
# starlette serialises that with ``allow_nan=False`` — so a body carrying ``NaN``, ``Infinity``
# or an overflowing exponent such as ``1e400`` (all of which ``json.loads`` accepts) turns a 422
# into an unhandled ``ValueError`` and an HTTP 500 on an unauthenticated POST. That is T-270,
# which is open against this very app. Declaring a body model here would have added a second
# instance of it on a brand-new route; reading the bytes and validating them by hand does not.
#
# The twin of this block lives in ``external_bids/routes.py``. It is duplicated rather than
# shared for the reason ``_bind_the_deployment`` is duplicated between ``auction/routes.py`` and
# ``accept/routes.py``: each feature package owns its own door, and a thirty-line helper is not
# worth a new cross-feature import edge.


class _NonFiniteNumber(ValueError):
    """A JSON number no strict client can read: ``NaN``, ``Infinity``, or an overflow."""


def _refuse_json_constant(token: str) -> Any:
    raise _NonFiniteNumber(f"{token} is not valid JSON; a strict client cannot read this body")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _NonFiniteNumber(f"the number {text!r} is not finite")
    return value


async def _bounded_body(request: Request, limit: int) -> bytes:
    """The request body, refused the moment it exceeds ``limit`` bytes.

    Streamed and counted rather than buffered through ``Request.body()``: the latter reads
    whatever arrives before anything can object, so a length check afterwards bounds the refusal
    and not the memory.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _BodyRefused(
                REASON_BODY_TOO_LARGE,
                f"the body exceeds this door's {limit}-byte ceiling and is refused unread",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _json_object(raw: bytes) -> dict[str, Any]:
    """``raw`` as a JSON object, or a refusal naming which of the three ways it was not one."""
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            parse_constant=_refuse_json_constant,
            parse_float=_finite_float,
        )
    except _NonFiniteNumber as exc:
        raise _BodyRefused(
            REASON_BODY_NOT_JSON,
            f"{exc}; NaN, Infinity and overflowing exponents are not readable by a strict "
            "JSON client and are refused here rather than echoed back",
        ) from exc
    except RecursionError as exc:
        raise _BodyRefused(
            REASON_BODY_NOT_JSON, "the body nests deeper than this door will parse"
        ) from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise _BodyRefused(REASON_BODY_NOT_JSON, "the body is not UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise _BodyRefused(
            REASON_BODY_NOT_AN_OBJECT,
            f"the body is a JSON {type(parsed).__name__}; TrustEventPayload is an object",
        )
    return parsed


def _reported_fields(exc: Exception) -> str:
    """The field paths a validation failure names — never the values it was handed.

    ``errors()[i]["input"]`` is the caller's own value and is deliberately not read: publishing it
    is how the 422 renderer this module avoids ends up serialising a float nobody can encode.
    A ``loc`` is still caller-influenced under ``extra="forbid"`` (it is the extra key's name), so
    it is truncated, capped in number and swept for process addresses like any other echo.
    """
    errors = getattr(exc, "errors", None)
    rows: list[Any] = []
    if callable(errors):
        try:
            rows = list(errors())
        except Exception:  # noqa: BLE001 - a validator that cannot describe itself names nothing
            rows = []
    fields: list[str] = []
    for row in rows[:_MAX_REPORTED_FIELDS]:
        location = row.get("loc", ()) if isinstance(row, Mapping) else ()
        rendered = ".".join(str(part) for part in location) or "<root>"
        fields.append(redact_addresses(rendered)[:_MAX_REPORTED_FIELD_CHARS])
    return ";".join(fields) or "<root>"


# =====================================================================================
# The route
# =====================================================================================
@router.post(
    "/internal/outcomes",
    status_code=204,
    response_class=Response,
    summary="Trust reports an outcome back to the exchange for the bandit update.",
    responses={
        204: {"description": "Recorded."},
        400: {"description": "The body is not a TrustEventPayload, or names no cluster."},
        503: {"description": "The exchange is misconfigured, or could not record the outcome."},
    },
    # The body is declared REQUIRED and is deliberately NOT re-specified field by field. The
    # published contract already gives it as `protocol.schema.json#/$defs/TrustEventPayload`, and
    # that document is generated from the schema both languages read; a second copy of the shape
    # in this file would be a copy that can drift from the model actually validated against below.
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": (
                "A protocol `TrustEventPayload` (R13). The authoritative schema is "
                "`protocol.schema.json#/$defs/TrustEventPayload`; it is enforced here by "
                "`contracts.protocol.TrustEventPayload`."
            ),
            "content": {"application/json": {"schema": {"type": "object"}}},
        },
    },
)
async def record_outcome(request: Request) -> Response:
    """Fold one trust outcome into this cluster's posterior and answer 204 with no body.

    A total wrapper, in the shape ``store_agent.external.door.receive_bid`` wraps
    ``_receive_bid`` and for the same reason: this is an unauthenticated POST, so an exception
    escaping it is a 500 handed to an anonymous caller — a stack trace, a burnt worker and an
    oracle — where a decision belongs. Naming the hazards one by one inside
    :func:`_record_outcome` is not the same as being total, so the wrapper makes the property
    hold by construction.

    It fails **closed in the honest direction**, which for this door is a 503 rather than a
    refusal: 204 on this route is the word "Recorded.", and answering it after an error nobody
    anticipated would tell the trust service its report landed when it did not. 400 is reserved
    for the cases this module actually diagnosed.
    """
    try:
        return await _record_outcome(request)
    except _BodyRefused as exc:
        raise HTTPException(status_code=400, detail=f"{exc.reason}: {exc.detail}") from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - a door that raises is a door that 500s
        raise HTTPException(
            status_code=503,
            detail=(
                "the exchange did not record this outcome "
                f"({describe_exception(exc)}); it is not folded into any posterior"
            ),
        ) from exc


async def _record_outcome(request: Request) -> Response:
    """The work behind :func:`record_outcome`. Every refusal here is one this module chose."""
    _bind_the_deployment(request)

    body = _json_object(await _bounded_body(request, DEFAULT_MAX_OUTCOME_BYTES))
    try:
        payload = TrustEventPayload.model_validate(body)
    except Exception as exc:  # noqa: BLE001 - anything unparseable is simply not the payload
        raise HTTPException(
            status_code=400,
            detail=(
                f"{REASON_SCHEMA_INVALID}: the body is not a TrustEventPayload "
                f"(fields: {_reported_fields(exc)})"
            ),
        ) from exc

    cluster_id = (payload.pseudonymous_context.cluster_id or "").strip()
    if not cluster_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{REASON_NO_CLUSTER}: pseudonymous_context.cluster_id is absent, and exposure "
                "is decided WITHIN a cluster — an outcome that names none cannot be routed to a "
                "posterior, and pooling it into a shared bucket would move clusters it never "
                "happened in"
            ),
        )

    delta = float(payload.delta)
    if math.isfinite(delta) and delta != 0.0:
        book = _posteriors(request)
        recorder = getattr(book, "record", None)
        if not callable(recorder):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"the wired posterior book of type {type(book).__name__!r} exposes no "
                    "record(store_id, cluster_id, converted) — this exchange cannot learn from "
                    "an outcome it has nowhere to put"
                ),
            )
        recorder(
            payload.store_id,
            cluster_id,
            delta > 0.0,
            trust_snapshot=_trust_snapshot(request),
        )
    # A zero (or unreadable) delta is a well-formed report carrying no result, and `update`'s own
    # rule is that an outcome with no result is not an outcome. It is accepted — the trust
    # service has nothing to retry — and folded into nothing.

    return Response(status_code=204)
