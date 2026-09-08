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

**And until R8 nothing could stop defaulting.** The paragraph above was the whole of this
door's production behaviour: ``configure_external_bids`` had no caller outside this
repository's tests, so a deployed exchange refused EVERY submission ``unknown_signing_key``
and, had it got past that gate, ``verification_queue_unavailable`` — served, correct, and
unusable. The keyring could not simply move into the deployment document, because that
document carries no secret material by design; it names a file instead
(``external_bid_keyring_file``), the composition root reads it, and :func:`_bind_deployment`
below is what makes this route run that composition root at all. A door that reads
``app.state`` for its collaborators and never runs the hook that binds them is a door nobody
can configure, whatever the document says.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from store_agent.external.door import DEFAULT_FRESHNESS_WINDOW_SECONDS, receive_bid

from .. import redact_addresses
from ..ranking.serving import catalog_of, claim_dimensions_of, trust_snapshot_of
from .draining import DRAIN_BATCH_SIZE, drain_verification_queue

__all__ = [
    "MAX_SUBMISSION_BYTES",
    "SIGNATURE_HEADER",
    "configure_external_bids",
    "drain_after_admission",
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

#: The caller-chosen IDENTIFIERS this door bounds, alongside the path's `auction_id`.
#: `nonce` and `signer_id` are kept in a replay memory for the auction's lifetime, `key_id`
#: reaches the queued work item, and `store_id` is interpolated verbatim into refusals such as
#: `trust_snapshot_unavailable:<store_id>` — the package already bounds it on `RosterEntry`
#: (`auction/routes.py:488`) for that reason.
#:
#: WHAT THIS DOES NOT BOUND, said plainly because an earlier version of this comment claimed
#: to cover "the caller-chosen strings this door retains" and did not: `message`,
#: `agent_version`, `schema_version` and the `claims` list are part of the SUBMISSION, which
#: is snapshotted into the queued work item by design. They are bounded only by
#: :data:`MAX_SUBMISSION_BYTES`, and deliberately so — truncating a bid's contents would
#: change what was signed. Measured: a 100 KB `message` is admitted and queues a ~106 KB item.
BOUNDED_IDENTIFIERS = ("signer_id", "key_id", "nonce", "store_id")

_UNREADABLE_BODY = "malformed_submission:body_is_not_a_json_object"
_OVERSIZED_BODY = "malformed_submission:body_exceeds_maximum_size"
_NON_FINITE_NUMBER = "malformed_submission:body_carries_a_non_finite_number"
_PATH_MISMATCH = "malformed_submission:auction_id_does_not_match_the_path"
_OVERSIZED_IDENTIFIER = "malformed_submission:identifier_exceeds_maximum_length"
_DEEPLY_NESTED_BODY = "malformed_submission:body_nests_deeper_than_this_door_will_parse"
_CLIENT_DISCONNECTED = "malformed_submission:the_body_never_finished_arriving"

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

    **That default store used to never forget; it does now, and the fix is not in this file.**
    This paragraph reported an unbounded nonce COUNT — each nonce's LENGTH was bounded by
    :data:`BOUNDED_IDENTIFIERS` but nothing dropped an entry, so 200 admitted bids retained 200
    entries and 74,504 bytes with ``retain_until`` populated and never consulted, because
    :meth:`~store_agent.external.nonces.NonceStore.purge_expired` had no production caller. T-378
    gave it one, and it is the very function this route delegates to:
    ``store_agent.external.door.receive_bid`` sweeps at its replay gate, on the door's own
    ``evaluated_at`` clock, before every consume. The same 200 admitted bids now retain 30
    entries / 6,851 bytes — 30 being exactly the bids still inside the freshness window — and 2
    when auction deadlines are supplied. ``NonceStore`` also refuses at ``MAX_TRACKED_NONCES``
    rather than evicting an entry whose window is still open, which is what keeps the bound from
    becoming a replay hole. A deployment that hands its own ``nonces`` over inherits neither
    guarantee unless its object implements the same port; ``purge_expired`` is part of that port,
    and a store lacking it fails the door **closed** rather than silently skipping the sweep.

    ``queue`` receives the verification work item for an ADMITTED submission. Admitted is not
    trusted: the seller-asserted claims are queued rather than believed (R18).
    """
    if keyring is not None:
        app.state.external_bid_keyring = keyring
    if nonces is not None:
        # Under the SAME lock the lazy path takes. Without it an operator's store could be
        # written into the window between `_nonce_store`'s check and its own write and then
        # silently overwritten by the store it was configuring away — no replay hole, but the
        # deployment's own replay memory would be dropped on the floor.
        with _NONCE_STORE_LOCK:
            app.state.external_bid_nonces = nonces
    if queue is not None:
        app.state.external_bid_queue = queue
    if freshness_window_seconds is not None:
        app.state.external_bid_freshness_seconds = float(freshness_window_seconds)


def _refuse_constant(token: str) -> Any:
    raise _UnreadableBody(_NON_FINITE_NUMBER)


def _finite_float(text: str) -> float:
    """Every JSON float, refused if converting it overflows to an infinity.

    ``parse_constant`` fires only on the bare tokens ``NaN``/``Infinity``/``-Infinity``.
    ``1e400`` is an ordinary RFC-8259 number that becomes ``inf`` during conversion and
    reaches nothing that would object — this door's docstring claimed to refuse it "exactly as
    policy/routes.py does" while admitting it, and the twin has had this half all along.
    """
    value = float(text)
    if value in (float("inf"), float("-inf")):
        raise _UnreadableBody(_NON_FINITE_NUMBER)
    return value


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
    from starlette.requests import ClientDisconnect  # noqa: PLC0415 - only needed here

    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_SUBMISSION_BYTES:
                raise _UnreadableBody(_OVERSIZED_BODY)
            chunks.append(chunk)
    except ClientDisconnect as exc:
        # A caller hanging up mid-upload is a fact of the internet, not a bug. Measured on
        # this door before the catch: one partial `http.request` followed by
        # `http.disconnect` let `ClientDisconnect` escape and the app answered 500.
        #
        # An earlier version of this comment added "and every sibling route on this app
        # already answers it 4xx/5xx-free". That is false and the commit message that
        # shipped it contained the table disproving it: `/auctions` and
        # `/auctions/{id}/accept` answer 400, but `/internal/outcomes` — served by
        # `policy/routes.py`, the twin this module cites throughout — answers 503, which is
        # a 5xx. In a module whose convention is that the comment IS the evidence, a comment
        # contradicted by its own commit is worse than no comment, so it is corrected rather
        # than deleted. The door's behaviour was always the 400 below; only the claim about
        # the neighbours was wrong.
        raise _UnreadableBody(_CLIENT_DISCONNECTED) from exc
    return b"".join(chunks)


def _submission_of(raw: bytes) -> dict[str, Any]:
    """The JSON object ``raw`` spells, with non-finite numbers refused rather than admitted.

    ``json.loads`` accepts ``NaN``/``Infinity``/``1e400``. Letting one through would put a
    value no strict client can re-read into a signed submission, a queued work item and any
    response that echoes it — the T-270 failure, on a door that has a refusal for it.
    """
    try:
        parsed = json.loads(
            raw.decode("utf-8"), parse_constant=_refuse_constant, parse_float=_finite_float
        )
    except _UnreadableBody:
        raise
    except RecursionError as exc:
        # NOT COVERED BY `ValueError`, WHICH IS THE WHOLE DEFECT. `RecursionError` is a
        # `RuntimeError`, so a deeply nested body escaped this function and the ASGI app
        # entirely: measured, `b"[" * 9994 + b"]" * 9994` — 19,988 bytes, 7.6% of this door's
        # ceiling, unauthenticated, no signature needed — answered HTTP 500 while every
        # sibling route on the same app answered 400. Reading the body by hand is what keeps
        # T-270's echoing 422 renderer off this route; it also opted out of the blanket
        # body-parse guard FastAPI gives a route that declares a model, and that guard has to
        # be replaced rather than merely dropped. `policy/routes.py`'s `_json_object` — the
        # twin this module names — has always had this clause.
        raise _UnreadableBody(_DEEPLY_NESTED_BODY) from exc
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


def _bind_deployment(request: Request) -> None:
    """Run the composition root once for this app, before anything reads a collaborator.

    The same request-time start-up hook ``auction/routes.py``, ``accept/routes.py`` and
    ``policy/routes.py`` take, and this route is here because it did not take it: ``main.py``
    is orchestrator-frozen (B6(iii)), so a deployment cannot be composed in ``create_app``, and
    a route that skips the hook reads whatever ``app.state`` happens to hold. For this door
    that meant the keyring, the verification queue and the auction terms were all whatever the
    OTHER routes had bound — so an exchange whose first request was a bid submission refused it
    with no keyring, no queue and no auction machine to read a deadline or a list price from.

    It also fixes an ordering that was invisible while nothing configured this door: the terms
    a submission is judged against (:func:`_auction_terms`) come off ``app.state.auction_machine``,
    which the composition root binds. Without the hook, an exchange serving auctions from a
    shared store still judged external bids against no terms at all.

    A malformed deployment — including a keyring file that is named and cannot be read — is a
    **503 naming the problem**, never a 400. The distinction is the seller's: a 400 from this
    door is a ``BidValidationResult`` and "each rejection is final", so answering an operator's
    broken configuration with one would tell an honest submitter their bid was permanently
    refused. It is not. ``redact_addresses`` for the reason the siblings use it, and the
    composition root's own messages never quote a secret.

    It does not weaken the module's ordering rule, which is that nothing may answer before
    ``receive_bid`` decides. That rule exists so a caller cannot learn which auctions exist by
    watching which ones answer differently — and this answer does not vary with the submission
    at all: the same 503 goes to every caller, including one that sends no body, and it says
    only that this exchange is misconfigured, which ``POST /auctions`` says to the same
    anonymous caller already.
    """
    from ..composition import DeploymentConfigurationError, ensure_configured  # noqa: PLC0415

    try:
        ensure_configured(request.app)
    except DeploymentConfigurationError as exc:
        raise HTTPException(status_code=503, detail=redact_addresses(exc)) from exc


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


def _auction_terms(request: Request, auction_id: str, store_id: Any) -> tuple[Any, Any]:
    """``(deadline, list_prices)`` for ``auction_id``, or ``(None, None)`` if it is not known.

    Best effort ON PURPOSE. Everything this returns is an input the door VALIDATES AGAINST,
    never one it trusts, so failing to find it can only make the door stricter: no deadline
    is ``auction_deadline_unparseable`` and no roster is
    ``price_unreconcilable:offer.unit_price:list_price_unavailable``. Raising here instead
    would answer an unknown auction before the signature was ever checked.

    **THE ROSTER IS READ FOR ONE STORE, AND IT USED NOT TO BE.** ``list_prices`` is keyed by
    ``product_ref``, so a roster on which two stores are asked about the same product — which
    is what a roster IS, and what every roster in this repository's own tests looks like —
    collapsed into one entry, and the LAST row silently decided the price wall for every
    seller. Measured on the served exchange the moment a deployment could reach this door at
    all: an auction rostering ``s1`` at a list price of 100.00 with 20% of depth and ``s2`` at
    120.00, both on ``prod-1``, refused ``s1``'s correctly signed 88.00 —
    ``price_under_declared_depth:offer.unit_price`` — because it was judged against ``s2``'s
    120.00 floor of 96.00. That is an honest seller refused by another store's terms, and it
    is the shape of failure this door must not have: the refusal is final and names the seller.

    So the rows are filtered to the store the SUBMISSION names. A submission whose store is not
    on the roster at all now finds no list price and is refused
    ``price_unreconcilable:offer.unit_price:list_price_unavailable`` rather than being judged
    against a store it is not — fail closed, and for a stated reason. ``store_id`` is compared
    only when it is a string, because it arrives off the wire: the door refuses a non-string
    one itself, and an ``__eq__`` that raises must not escape this function.
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
        if not isinstance(store_id, str) or row.get("store_id") != store_id:
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


def _renderable(text: Any) -> str:
    r"""``text`` with anything the response encoder cannot emit replaced.

    Starlette renders with ``ensure_ascii=False`` and then ``.encode("utf-8")``, and a LONE
    SURROGATE — ``"\ud800"``, which a caller writes as a plain ``\uXXXX`` escape and which
    ``json.loads`` accepts into a perfectly ordinary ``str`` — makes that encode raise
    ``UnicodeEncodeError``. That is an unauthenticated 500 in the response renderer, the same
    SHAPE of defect as T-270 and reached the same way: by echoing a caller's own value.

    Caller-controlled text does reach that renderer, through ``bid_ref`` on the 202 and
    through reason codes such as ``schema_invalid:claims.0.<key>`` on the 400.

    **It is not live today, and it is closed anyway, for the reason the overflowing-exponent
    refusal was closed:** the only thing preventing it is ``canonical_signing_bytes``, which
    refuses a lone surrogate in any value AND any key one layer down. That is a borrowed
    defence in another package, and "safe because something else happens to refuse it first"
    is exactly the reasoning this door has twice had to retract. The cost is one pass over a
    handful of short strings.
    """
    return str(text).encode("utf-8", "replace").decode("utf-8", "replace")


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
            "reasons": [_renderable(reason) for reason in reasons] or ["malformed_submission"],
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
        # NOT a rejection, which is why it is not a `BidValidationResult`: the deployment
        # document (or the keyring file it names) cannot be read, so this exchange has not
        # judged the submission at all and the seller should retry it unchanged.
        #
        # `packages/contracts/openapi/exchange.openapi.json` declares 202 and 400 for this
        # operation and no 503 — as it does for `/auctions` and `/accept`, which have answered
        # 503 to a malformed deployment since the composition root landed. So this entry is a
        # served document that says more than the published one, not less, and the sweep in
        # `test_repro_open_tickets.py` compares OPERATIONS rather than response codes. Making
        # the published contract say it is a change to a file this route does not own.
        503: {"description": "This exchange is misconfigured. The submission was not judged."},
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

    The composition root runs FIRST, before the body is even read: everything below reads a
    collaborator off ``app.state``, and this is the request-time start-up hook that puts them
    there. See :func:`_bind_deployment`.
    """
    _bind_deployment(request)
    try:
        raw = await _bounded_body(request)
        body = _submission_of(raw)
    except _UnreadableBody as exc:
        return _rejected([str(exc)])

    payload, signature = _payload_and_signature(body, x_proxyshop_signature or "")

    refusal = _oversized_identifier(payload, auction_id) or _reconciled(payload, auction_id)
    if refusal is not None:
        return _rejected([refusal])

    # The terms come from the roster row for the store this submission names — see
    # `_auction_terms`. `payload` is whatever arrived, so the store is read defensively;
    # anything that is not a string finds no terms, and the door refuses.
    submitted_store = payload.get("store_id") if isinstance(payload, dict) else None
    deadline, list_prices = _auction_terms(request, auction_id, submitted_store)

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
        # NOTHING is drained on a refusal, and that ordering is a defence rather than a tidy-up:
        # draining is work, and a door that did it before judging would let an anonymous caller
        # with a junk signature spend the exchange's CPU on the backlog once per request.
        return _rejected(
            [str(reason) for reason in getattr(receipt, "reasons", ()) or ()],
            indexes=[int(i) for i in getattr(receipt, "unverified_claim_indexes", ()) or ()],
        )
    drain_after_admission(request)
    return JSONResponse(
        status_code=202,
        content={
            "accepted": True,
            "verification_status": _renderable(
                getattr(receipt, "verification_status", "unverified")
            ),
            "bid_ref": _renderable_ref(_bid_ref_of(payload)),
        },
    )


def drain_after_admission(request: Request) -> Any:
    """Perform R8's "routed to claim extraction + verification" for a bounded batch.

    **This is the queue's only consumer, and it runs here because there is nowhere else.** The
    exchange deployable runs one command — ``uvicorn exchange.main:app`` — and declares no
    console script and no lifespan hook, so a background drainer would be a module nothing
    starts; and a NEW served route would put the exchange's surface out of agreement with
    ``packages/contracts/openapi/exchange.openapi.json``, which
    ``test_t312_the_exchange_serves_exactly_the_operations_its_contract_publishes`` refuses in
    both directions. See :mod:`.draining` for the full argument and for the four rules that
    keep a drain from becoming a deletion.

    Called only after an ADMISSION. One request adds one work item and consumes up to
    :data:`~.draining.DRAIN_BATCH_SIZE`, so the backlog shrinks under load instead of walking
    towards :data:`~.verification_queue.MAX_QUEUED_WORK_ITEMS`, where this channel starts
    refusing correctly signed bids.

    Never raises, and never changes the receipt: the seller has already been judged, and an
    audit trail able to turn a 202 into a 500 would be worse than no audit trail. The collected
    :class:`~.draining.DrainReport` is returned for a caller that wants it (this route's own
    gates do) and is ignored by the response.
    """
    try:
        machine = getattr(request.app.state, "auction_machine", None)
        return drain_verification_queue(
            getattr(request.app.state, "external_bid_queue", None),
            catalog=catalog_of(request.app),
            recorder=getattr(machine, "ledger", None),
            dimensions=claim_dimensions_of(request.app),
            max_items=DRAIN_BATCH_SIZE,
        )
    except Exception:  # noqa: BLE001 - the audit trail must not be able to fail an admitted bid
        return None


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


def _renderable_ref(value: str | None) -> str | None:
    """:func:`_renderable`, but ``None`` stays ``None`` — the contract publishes a nullable."""
    return None if value is None else _renderable(value)


def _bid_ref_of(payload: Any) -> str | None:
    """The submission's own reference, when it carried one the contract can publish."""
    if not isinstance(payload, dict):
        return None
    for key in ("bid_ref", "bid_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None
