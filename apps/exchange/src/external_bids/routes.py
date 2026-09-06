"""``POST /v1/auctions/{auction_id}/bids`` — the signed external door (T-244, D52).

`store_agent.external.door.receive_bid` had 28 callers and every one was a test. The six
gates the frozen E4 suite drives through it — envelope completeness, key selection, signature
verification, two-sided freshness, replay, the price wall — all graded a function no running
process reached, so ``e4_store_agent_passing`` counted a signed-external-bid capability that
nothing performed. This module is the request that arrives.

WHY THE ROUTE IS IN THE EXCHANGE AND THE GUARD IS IN THE STORE-AGENT PACKAGE. The two are
different directions on the same arrow, and ``store_agent/solicitation/serving.py`` says so:
solicitation is the exchange ASKING a store for a bid (``POST /v1/bid-requests``, served by
the store agent, unsigned), whereas this is an external Tier-2 seller SUBMITTING one, which
lands on the exchange and must be signed. Mounting this on the store agent would publish an
anonymous door on the wrong side of the arrow — and would contradict the store agent's own
contract, which declares exactly one operation.

What this door does NOT do, and each of these is load-bearing
--------------------------------------------------------------

**It never takes the keyring from the request.** A submission arrives with a ``signer_id``
and a ``key_id``; the SECRET those name is deployment state and is read only from
``configure_external_bids``. This is worth stating because a body carrying its own
``keyring`` is the obvious shape for a caller to try, and honouring it would let anyone
sign anything with a key they chose — it would turn the door into a formality. Such a key is
read and discarded.

**It declares no pydantic request model.** FastAPI renders a request-validation failure by
echoing the offending INPUT, and starlette serialises that with ``allow_nan=False``, so a
body carrying ``NaN``, ``Infinity`` or an overflowing exponent turns a 422 into an
unauthenticated 500 — that is T-270, which was open against this very app. Declaring a body
model on a brand-new route would have minted a fresh instance of it. The bytes are read and
parsed by hand instead, with the non-finite constants refused on the way IN, exactly as
``policy/routes.py`` does for ``POST /internal/outcomes``; that module's comment calls this
block its twin, and this is the twin.

**It does not check that the auction exists before consulting the door.** That ordering is
the whole security posture, not an oversight. ``receive_bid`` is documented "never raises"
and answers a hostile submission with a refusal, so it is the thing that must decide; a
404 taken first would mean an unknown ``auction_id`` skipped signature verification,
freshness and replay entirely, and would let a caller map which auctions exist by watching
which ones answer differently. The auction is looked up only to SUPPLY the door with the
deadline and the roster's list prices, and a lookup that fails hands the door nothing —
which the door treats as ``price_unreconcilable`` / ``auction_deadline_unparseable`` and
refuses. Fail closed, and refuse for a stated reason.

**Its dependencies default to refusing.** No keyring configured means no submission can
name a key, so every submission is refused ``unknown_signing_key``. That is deliberate: an
exchange nobody has handed a keyring to should admit nothing, the same rule
``NoRegisteredDomains`` applies in ``accept/routes.py``.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from store_agent.external.door import DEFAULT_FRESHNESS_WINDOW_SECONDS, receive_bid

from ..ranking.serving import trust_snapshot_of

__all__ = [
    "MAX_SUBMISSION_BYTES",
    "SIGNATURE_HEADER",
    "configure_external_bids",
    "router",
]

router = APIRouter(tags=["external-bids"])

#: The header the published contract carries the signature in.
SIGNATURE_HEADER = "X-ProxyShop-Signature"

#: A submission larger than this is refused unread. The door walks and snapshots whatever it
#: is handed, so an unbounded body is an unbounded walk on an unauthenticated route; a signed
#: bid is a few kilobytes. This is a bound on THIS door only and is not an attempt at the
#: repo-wide body cap that T-355 tracks for `/auctions` and `/accept`.
MAX_SUBMISSION_BYTES = 256 * 1024

#: The refusal `path` published in a `BidValidationResult`. The contract's own example uses
#: `"external"`, which is the dual-path boundary's name for this side.
REFUSAL_PATH = "external"

_UNREADABLE_BODY = "malformed_submission:body_is_not_a_json_object"
_OVERSIZED_BODY = "malformed_submission:body_exceeds_maximum_size"
_NON_FINITE_NUMBER = "malformed_submission:body_carries_a_non_finite_number"


class _UnreadableBody(ValueError):
    """A body no strict JSON client could have written."""


def configure_external_bids(
    app: Any,
    *,
    keyring: Any | None = None,
    nonces: Any | None = None,
    queue: Any | None = None,
    freshness_window_seconds: float | None = None,
) -> None:
    """Wire this door's dependencies. Anything omitted keeps what is already there.

    ``keyring`` is the ``{signer_id: {key_id: secret}}`` table the door selects a secret
    from. There is no default and no fallback: an exchange that was handed none refuses every
    submission ``unknown_signing_key``.

    ``nonces`` is the replay memory. Omitting it lets :func:`_nonce_store` install a process
    -local :class:`~store_agent.external.nonces.NonceStore` on first use, the way
    ``auction/routes.py`` installs its bid book — a deployment that needs replay memory to
    outlive one process hands its own over here.

    ``queue`` receives the verification work item for an ADMITTED submission. Admitted is not
    trusted: the seller-asserted claims are queued rather than believed (R18).
    """
    if keyring is not None:
        app.state.external_bid_keyring = keyring
    if nonces is not None:
        app.state.external_bid_nonces = nonces
    if queue is not None:
        app.state.external_bid_queue = queue
    if freshness_window_seconds is not None:
        app.state.external_bid_freshness_seconds = float(freshness_window_seconds)


def _refuse_constant(token: str) -> Any:
    raise _UnreadableBody(_NON_FINITE_NUMBER)


def _submission_of(raw: bytes) -> dict[str, Any]:
    """The JSON object ``raw`` spells, with non-finite numbers refused rather than admitted.

    ``json.loads`` accepts ``NaN``/``Infinity``/``1e400``. Letting one through would put a
    value no strict client can re-read into a signed submission, a queued work item and any
    response that echoes it — the T-270 failure, on a door that has a refusal for it.
    """
    try:
        parsed = json.loads(raw.decode("utf-8"), parse_constant=_refuse_constant)
    except _UnreadableBody:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise _UnreadableBody(_UNREADABLE_BODY) from exc
    if not isinstance(parsed, dict):
        raise _UnreadableBody(_UNREADABLE_BODY)
    return parsed


def _payload_and_signature(body: dict[str, Any], header: str) -> tuple[Any, str]:
    """The submission and the signature presented for it.

    The contract's shape is the submission itself as the body with the signature in
    :data:`SIGNATURE_HEADER`. A nested ``{"payload": …, "signature": …}`` envelope is also
    read, because that is the shape every in-repo caller of ``receive_bid`` uses and a door
    that only understood one of the two would be a door the platform's own clients cannot
    reach. Both spellings converge here.

    A ``keyring`` presented anywhere in the body is IGNORED — see the module docstring.
    """
    inner = body.get("payload")
    if isinstance(inner, dict):
        payload: Any = inner
        signature = body.get("signature")
    else:
        payload = body
        signature = body.get("signature")
    if header:
        signature = header
    return payload, "" if signature is None else str(signature)


def _keyring(request: Request) -> Any:
    """The deployment's signing table. Absent means 'admit nothing', not 'admit anything'."""
    return getattr(request.app.state, "external_bid_keyring", None) or {}


def _nonce_store(request: Request) -> Any:
    """The replay memory, created on first use exactly as ``auction/routes.py``'s bid book is.

    The door refuses ``replay_memory_unavailable`` when handed nothing, so the alternative to
    creating one here is a door that refuses every submission for a reason the operator did
    not choose.
    """
    from store_agent.external.nonces import NonceStore  # noqa: PLC0415 - optional dependency

    store = getattr(request.app.state, "external_bid_nonces", None)
    if store is None:
        store = NonceStore()
        request.app.state.external_bid_nonces = store
    return store


def _auction_terms(request: Request, auction_id: str) -> tuple[Any, Any]:
    """``(deadline, list_prices)`` for ``auction_id``, or ``(None, None)`` if it is not known.

    Best effort ON PURPOSE. Everything this returns is an input the door VALIDATES AGAINST,
    never one it trusts, so failing to find it can only make the door stricter: no deadline
    is ``auction_deadline_unparseable`` and no roster is
    ``price_unreconcilable:offer.unit_price:list_price_unavailable``. Raising here instead
    would answer an unknown auction before the signature was ever checked.
    """
    machine = getattr(request.app.state, "auction_machine", None)
    if machine is None:
        return None, None
    try:
        record = machine.get(auction_id)
    except Exception:  # noqa: BLE001 - an unreadable auction is 'no terms', never a 500
        return None, None
    prices: dict[Any, Any] = {}
    for row in getattr(record, "roster", None) or ():
        if not isinstance(row, dict):
            continue
        ref = row.get("product_ref")
        if not isinstance(ref, str) or not ref:
            continue
        prices[ref] = {
            "list_price": row.get("list_price"),
            "max_discount_pct": row.get("max_discount_pct"),
        }
    return getattr(record, "deadline", None), (prices or None)


def _rejected(reasons: list[str], *, indexes: list[int] | None = None) -> JSONResponse:
    """The published ``BidValidationResult``. ``reasons`` is never empty when ``ok`` is false.

    "A refusal that will not say why is indistinguishable from a bug", as the schema puts it,
    and the exchange has to log a reason code back to the seller.
    """
    return JSONResponse(
        status_code=400,
        content={
            "ok": False,
            "path": REFUSAL_PATH,
            "reasons": reasons or ["malformed_submission"],
            "requires_verification": False,
            "unverified_claim_indexes": list(indexes or ()),
        },
    )


@router.post(
    "/v1/auctions/{auction_id}/bids",
    operation_id="submitExternalBid",
    summary="The signed external door: a Tier-2 seller submits a Bid.",
    status_code=202,
    responses={
        202: {"description": "AcceptedForVerification. Admitted is not trusted (R18)."},
        400: {"description": "Rejected. Each rejection is final."},
    },
)
async def submit_external_bid(auction_id: str, request: Request) -> Response:
    """Judge one signed external submission and admit it for verification, or refuse it.

    Every decision is ``receive_bid``'s. This function reads the wire, hands the door the
    deployment state it needs, and renders the receipt; it contains no rule of its own about
    who may bid, and that is the point — the rules the E4 suite grades are now the rules a
    request actually meets.
    """
    raw = await request.body()
    if len(raw) > MAX_SUBMISSION_BYTES:
        return _rejected([_OVERSIZED_BODY])
    try:
        body = _submission_of(raw)
    except _UnreadableBody as exc:
        return _rejected([str(exc)])

    payload, signature = _payload_and_signature(body, request.headers.get(SIGNATURE_HEADER, ""))
    deadline, list_prices = _auction_terms(request, auction_id)

    receipt = receive_bid(
        payload,
        signature,
        _keyring(request),
        queue=getattr(request.app.state, "external_bid_queue", None),
        nonce_store=_nonce_store(request),
        auction_deadline=deadline,
        trust_snapshot=trust_snapshot_of(request.app),
        list_prices=list_prices,
        freshness_window_seconds=_freshness(request),
    )

    if not getattr(receipt, "accepted", False):
        return _rejected(
            [str(reason) for reason in getattr(receipt, "reasons", ()) or ()],
            indexes=[int(i) for i in getattr(receipt, "unverified_claim_indexes", ()) or ()],
        )
    return JSONResponse(
        status_code=202,
        content={
            "accepted": True,
            "verification_status": str(getattr(receipt, "verification_status", "unverified")),
            "bid_ref": _bid_ref_of(payload),
        },
    )


def _freshness(request: Request) -> float:
    """The configured freshness window, falling back to the door's own published default.

    Named explicitly rather than splatted in as ``**kwargs``: the door validates this value
    itself and refuses ``freshness_window_invalid`` for a bad one, so there is nothing to gain
    from hiding whether it was passed.
    """
    window = getattr(request.app.state, "external_bid_freshness_seconds", None)
    if window is None:
        return DEFAULT_FRESHNESS_WINDOW_SECONDS
    try:
        return float(window)
    except (TypeError, ValueError):
        return DEFAULT_FRESHNESS_WINDOW_SECONDS


def _bid_ref_of(payload: Any) -> str | None:
    """The submission's own reference, when it carried one the contract can publish."""
    if not isinstance(payload, dict):
        return None
    for key in ("bid_ref", "bid_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None
