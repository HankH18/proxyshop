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

**It does not check that the auction EXISTS before consulting the door.** That ordering is
the whole security posture, not an oversight. ``receive_bid`` is documented "never raises"
and answers a hostile submission with a refusal, so it is the thing that must decide; a
404 taken first would mean an unknown ``auction_id`` skipped signature verification,
freshness and replay entirely, and would let a caller map which auctions exist by watching
which ones answer differently. The auction is looked up only to SUPPLY the door with the
deadline and the roster's list prices, and a lookup that fails hands the door nothing —
which the door treats as ``price_unreconcilable`` / ``auction_deadline_unparseable`` and
refuses. Fail closed, and refuse for a stated reason.

**But it DOES check three things before the door, and the distinction is exactly the one
above.** The size of the body, the length of each caller-chosen identifier, and whether the
URL agrees with the signed ``auction_id`` are all decidable from what this ONE caller sent,
so none of them can be used to learn anything about the platform's state, and each closes a
hole that would otherwise defeat a rule the door itself enforces:

* an unbounded body is an unbounded walk before any gate runs (:func:`_bounded_body`);
* an unbounded ``nonce`` is retained forever in a replay memory with no eviction
  (:func:`_oversized_identifier`);
* an unreconciled path lets the URL choose which auction's TERMS judge a submission while
  the body chooses which auction it IS — which bypassed the auction-deadline and price-wall
  gates outright (:func:`_reconciled`).

The first version of this module had none of the three. All were found by an adversarial
review of the merged door rather than by the T-244 gate, which asks only whether the door is
REACHED; each now has its own gate in ``apps/exchange/tests/test_external_bids_door.py``.

**Its dependencies default to refusing.** No keyring configured means no submission can
name a key, so every submission is refused ``unknown_signing_key``. That is deliberate: an
exchange nobody has handed a keyring to should admit nothing, the same rule
``NoRegisteredDomains`` applies in ``accept/routes.py``.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from store_agent.external.door import DEFAULT_FRESHNESS_WINDOW_SECONDS, receive_bid

from ..ranking.serving import trust_snapshot_of

__all__ = [
    "MAX_SUBMISSION_BYTES",
    "SIGNATURE_HEADER",
    "configure_external_bids",
    "identifier_ceiling",
    "router",
]

router = APIRouter(tags=["external-bids"])

#: The header the published contract carries the signature in.
SIGNATURE_HEADER = "X-ProxyShop-Signature"

#: A submission larger than this is refused, and refused while it is still ARRIVING — see
#: :func:`_bounded_body`. The door walks and snapshots whatever it is handed, so an unbounded
#: body is an unbounded walk on an unauthenticated route; a signed bid is a few kilobytes.
#: This is a bound on THIS door only and is not an attempt at the repo-wide body cap that
#: T-355 tracks for `/auctions` and `/accept`.
MAX_SUBMISSION_BYTES = 256 * 1024

#: The refusal `path` published in a `BidValidationResult`. The contract's own example uses
#: `"external"`, which is the dual-path boundary's name for this side.
REFUSAL_PATH = "external"

#: The caller-chosen strings this door retains or interpolates, and therefore bounds.
#: `nonce` and `signer_id` are kept in an eviction-free replay memory for the auction's
#: lifetime, `key_id` reaches the queued work item, and `auction_id` arrives in the URL.
BOUNDED_IDENTIFIERS = ("signer_id", "key_id", "nonce")

_UNREADABLE_BODY = "malformed_submission:body_is_not_a_json_object"
_OVERSIZED_BODY = "malformed_submission:body_exceeds_maximum_size"
_NON_FINITE_NUMBER = "malformed_submission:body_carries_a_non_finite_number"
_PATH_MISMATCH = "malformed_submission:auction_id_does_not_match_the_path"
_OVERSIZED_IDENTIFIER = "malformed_submission:identifier_exceeds_maximum_length"

#: Guards the lazy creation of the replay memory — see :func:`_nonce_store`.
_NONCE_STORE_LOCK = threading.Lock()


class _UnreadableBody(ValueError):
    """A body no strict JSON client could have written."""


def identifier_ceiling() -> int:
    """The package's bound on a caller-chosen identifier.

    Imported rather than restated for the reason ``policy/routes.py`` gives when it does the
    same: a copy of a number is a number that can drift, and ``MAX_IDENTIFIER_LENGTH`` is
    already what this package means by "a string some caller picked". Deferred to keep this
    module's import head free of a sibling feature.
    """
    from ..auction.routes import MAX_IDENTIFIER_LENGTH  # noqa: PLC0415 - sibling feature

    return MAX_IDENTIFIER_LENGTH


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


async def _bounded_body(request: Request) -> bytes:
    """The submission, refused the moment it crosses :data:`MAX_SUBMISSION_BYTES`.

    STREAMED AND COUNTED, not buffered and then measured. ``await request.body()`` reads
    whatever arrives before anything can object, so a length check afterwards bounds the
    REFUSAL and not the MEMORY — measured on the first version of this door, an 8 MiB body was
    pulled in full (128 of 128 chunks) and only then answered 400, which cost the anonymous
    caller nothing. The twin in ``policy/routes.py`` streams for exactly this reason and its
    docstring is where the distinction is written down; this door's comment claimed "refused
    unread" while doing the opposite.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_SUBMISSION_BYTES:
            raise _UnreadableBody(_OVERSIZED_BODY)
        chunks.append(chunk)
    return b"".join(chunks)


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

    state = request.app.state
    store = getattr(state, "external_bid_nonces", None)
    if store is not None:
        return store
    # THE CREATION IS LOCKED, AND THE LOCK BELOW IT IS NOT ENOUGH. `NonceStore.consume` is
    # atomic (T-234) — but an unguarded read-check-write HERE reopens the same race one level
    # up: two first requests arriving together each see no store, each build one, and each
    # spend the same nonce in a DIFFERENT store, so the lock inside guards nothing they share.
    # Measured on the first version of this door, 60 trials of 8 concurrent identical
    # submissions against a fresh app: 60/60 admissions at the default switch interval and 89
    # at `sys.setswitchinterval(1e-6)`, with 24 trials spending one nonce two or three times.
    # That is precisely the argument `NonceStore.consume`'s own docstring makes about T-234:
    # the 5 ms GIL switch interval is "a scheduler accident, not a defence, and it goes live
    # the moment there is a concurrent HTTP caller".
    with _NONCE_STORE_LOCK:
        store = getattr(state, "external_bid_nonces", None)
        if store is None:
            store = NonceStore()
            state.external_bid_nonces = store
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


def _reconciled(payload: Any, path_auction_id: str) -> str | None:
    """The refusal code when the signed ``auction_id`` is not the one in the URL, else ``None``.

    THE URL MUST NOT BE ABLE TO OVERRULE WHAT THE SELLER SIGNED FOR. The path parameter
    chooses which auction's TERMS a submission is judged against — its deadline and its
    roster's list prices — while ``receive_bid`` judges the submission's IDENTITY from
    ``payload["auction_id"]``. Left unreconciled those are two different auctions, and a
    seller who dislikes one auction's terms can borrow another's by changing the address
    without re-signing anything. Measured on the first version of this door: a bid refused
    ``price_under_declared_depth`` on its own path was admitted 202, byte-identical, on a
    deeper auction's path; and a bid refused ``after_auction_deadline`` was admitted on an
    open auction's path — bypassing one of the six gates the frozen E4 suite counts, on the
    door built to make that metric honest.

    Refused BEFORE the door, which is safe here even though the module's ordering rule is that
    the door decides first. That rule exists so an unknown ``auction_id`` cannot skip signature,
    freshness and replay; this check reveals nothing about which auctions exist, because it
    compares two values the SAME caller supplied. It also spends no nonce on a request that
    was never going to be honoured.

    A payload with no ``auction_id`` — or a non-string one — is NOT refused here. That is the
    door's own ``malformed_submission``, and pre-empting it would replace the door's vocabulary
    with this route's for a case the door already handles.
    """
    if not isinstance(payload, dict):
        return None
    claimed = payload.get("auction_id")
    if not isinstance(claimed, str):
        return None
    return None if claimed == path_auction_id else _PATH_MISMATCH


def _oversized_identifier(payload: Any, path_auction_id: str) -> str | None:
    """The refusal code when a caller-chosen identifier is longer than the package allows.

    ``nonce`` and ``signer_id`` are retained in a replay memory with no eviction for the
    auction's lifetime, so their length is the caller's choice of how much of this process to
    keep — measured on the first version of this door, a 200 KB nonce was admitted 202 and
    retained. ``key_id`` reaches the queued work item and ``auction_id`` arrives in the URL.

    The ceiling is the package's own :data:`~..auction.routes.MAX_IDENTIFIER_LENGTH`, not a
    number invented here. Only the length is judged; every other property of these fields is
    the door's business.
    """
    ceiling = identifier_ceiling()
    if len(path_auction_id) > ceiling:
        return _OVERSIZED_IDENTIFIER
    if not isinstance(payload, dict):
        return None
    for field in BOUNDED_IDENTIFIERS:
        value = payload.get(field)
        if isinstance(value, str) and len(value) > ceiling:
            return _OVERSIZED_IDENTIFIER
    return None


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
    # DECLARED BY HAND BECAUSE THERE IS NO REQUEST MODEL TO INFER IT FROM. Reading the body as
    # bytes is what keeps T-270 off this door, and the cost is that FastAPI has nothing to
    # generate a `requestBody` from — so the served document would promise no body at all while
    # the published contract requires `SignedBidSubmission`. `openapi_extra` closes that gap
    # without putting a pydantic model back on the request, which is the trade the whole module
    # is built around.
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": (
                            "https://proxyshop.dev/schemas/protocol.schema.json"
                            "#/$defs/SignedBidSubmission"
                        )
                    }
                }
            },
        }
    },
)
async def submit_external_bid(
    auction_id: str,
    request: Request,
    # OPTIONAL TO FASTAPI, REQUIRED BY THE DOOR, and the difference is deliberate. Declaring
    # it `Header(...)` would make a missing header a `RequestValidationError` — a 422 rendered
    # by the machinery T-270 is about, in place of the 400 `BidValidationResult` this door's
    # contract publishes for a rejection. The submission is refused either way; refusing it as
    # `signature_missing` is refusing it in the vocabulary the seller was promised.
    x_proxyshop_signature: str | None = Header(
        default=None,
        alias=SIGNATURE_HEADER,
        description="Signature over canonical_signing_bytes(payload).",
    ),
) -> Response:
    """Judge one signed external submission and admit it for verification, or refuse it.

    Every decision about WHETHER A BID IS GOOD is ``receive_bid``'s. What this function keeps
    for itself is exactly the set of checks the door cannot make, because they are about the
    request rather than the submission: how big the body is, how long a caller-chosen
    identifier may be, and whether the URL agrees with what was signed. Each is decidable from
    values this one caller supplied, so none of them leaks anything, and each closes a hole
    that would otherwise defeat a rule the door DOES enforce.
    """
    try:
        raw = await _bounded_body(request)
        body = _submission_of(raw)
    except _UnreadableBody as exc:
        return _rejected([str(exc)])

    payload, signature = _payload_and_signature(body, x_proxyshop_signature or "")

    refusal = _oversized_identifier(payload, auction_id) or _reconciled(payload, auction_id)
    if refusal is not None:
        return _rejected([refusal])

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
