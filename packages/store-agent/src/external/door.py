"""The Tier-2 door: `receive_bid` (D52, R8/R18/C10).

This is the function a body arriving at `POST /v1/auctions/{auction_id}/bids` goes through
before anything else in the system has seen it. Everything it enforces is enforced **before the
queue is touched**, because a door that enqueues and then decides has already handed the
untrusted item to the next stage — and the next stage is claim extraction, which is exactly the
thing an unauthenticated submitter would like to reach.

Six gates, in this order, and the order is the design:

1. **Shape.** A submission that is not a mapping, and a signature that is not a non-empty
   string, are refused before a field of either is read.
2. **Envelope completeness**, via `contracts.signing.canonical_signing_bytes`, which raises on
   a missing `signer_id` / `key_id` / `issued_at` / `nonce` / `schema_version`. The
   canonicalizer is the gate rather than a private re-reading of the same rule, so there is one
   definition of "complete" and both sides read it.
3. **Authentication.** `key_id` *selects* one secret out of the signer's keyring; the door
   never tries the signer's other live keys. Key trial defeats revocation — a retired key would
   keep working for as long as the signer holds any live key at all.
4. **Freshness and the auction deadline**, which are two different checks. A submission one
   second old that arrives after the auction closed is fresh and still too late; a
   thirty-day-old submission to an auction that never closes is inside the deadline and still
   stale. Neither check can stand in for the other.
5. **The shared Tier-2 boundary** — `contracts.boundary.validate_external_submission` — for
   schema, provenance, price reconciliation, offer expiry and seller eligibility. This door
   does not re-read those rules; the exchange and this module must agree about them, and the
   only way two doors agree is by being one function.
6. **Replay.** The nonce is checked before, and consumed after, everything above. A submission
   that fails any gate must not burn its own nonce: an attacker who could spend a victim's
   nonce with a deliberately malformed copy would have a denial-of-service against the honest
   submission that follows.

Only then is the work item enqueued, and it is enqueued **exactly once**, marked
`verified=False`. Admitted is not trusted: R18's whole point is that an external bid enters as
a set of claims someone still has to check.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from contracts.boundary import parse_timestamp, validate_external_submission
from contracts.signing import canonical_signing_bytes, keyring_secret, payload_hash

from .nonces import NonceStore
from .signatures import verify_signature

__all__ = ["DEFAULT_FRESHNESS_WINDOW_SECONDS", "ExternalBidReceipt", "receive_bid"]

#: How far either side of `now` an `issued_at` may sit, in **seconds**, when the caller does not
#: say. Five minutes is the interval every comparable signed-request scheme settles on: long
#: enough that an honestly-skewed clock and a slow network still get in, short enough that a
#: captured submission is worthless long before anyone could replay it to a second endpoint.
#:
#: The window is **two-sided**. A submission issued in the future is not "extra fresh" — it is
#: either a broken clock or a signer pre-minting submissions to widen their own replay window,
#: and an unbounded forward tolerance makes the freshness check decorative in exactly the
#: direction an attacker chooses.
DEFAULT_FRESHNESS_WINDOW_SECONDS: float = 300.0

#: The `verification_status` an admitted external bid carries into the queue. It is never
#: `verified`: this door authenticates the *submitter*, and says nothing about whether the
#: claims are true.
UNVERIFIED_STATUS = "unverified"

REASON_MALFORMED_SUBMISSION = "malformed_submission"
REASON_SIGNATURE_MISSING = "signature_missing"
REASON_ENVELOPE_UNCANONICALIZABLE = "signing_envelope_uncanonicalizable"
REASON_UNKNOWN_SIGNING_KEY = "unknown_signing_key"
REASON_SIGNATURE_INVALID = "signature_invalid"
REASON_ISSUED_AT_UNPARSEABLE = "issued_at_unparseable"
REASON_STALE_SUBMISSION = "issued_at_stale"
REASON_FUTURE_DATED_SUBMISSION = "issued_at_future_dated"
REASON_AFTER_AUCTION_DEADLINE = "after_auction_deadline"
REASON_STORE_BLACKLISTED = "store_blacklisted"
REASON_REPLAYED_NONCE = "replayed_nonce"
REASON_QUEUE_UNAVAILABLE = "verification_queue_unavailable"


@dataclass(frozen=True)
class ExternalBidReceipt:
    """What the door answers. `accepted` is the whole verdict; everything else is diagnosis.

    `verified` is `False` on an accepted submission and stays `False` — there is no value of
    this field the door is able to set to `True`, because verifying a claim is somebody else's
    job and happens after the queue. A receipt is not evidence about the bid's contents.
    """

    accepted: bool
    verified: bool = False
    verification_status: str = UNVERIFIED_STATUS
    reasons: tuple[str, ...] = ()
    signer_id: str | None = None
    nonce: str | None = None
    unverified_claim_indexes: tuple[int, ...] = field(default=())

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.accepted


def _refuse(*reasons: str, payload: Any = None) -> ExternalBidReceipt:
    """A rejection receipt. Never carries `accepted=True` by any path."""
    signer_id = payload.get("signer_id") if isinstance(payload, Mapping) else None
    nonce = payload.get("nonce") if isinstance(payload, Mapping) else None
    return ExternalBidReceipt(
        accepted=False,
        verified=False,
        verification_status="rejected",
        reasons=tuple(str(reason) for reason in reasons),
        signer_id=signer_id if isinstance(signer_id, str) else None,
        nonce=nonce if isinstance(nonce, str) else None,
    )


def _blacklisted(payload: Mapping[str, Any], blacklist: Iterable[Any] | None) -> bool:
    """Whether this submission names a store or a signer the caller has blocked.

    **Both ids are checked, not just `store_id`.** `signer_id` and `store_id` are separate
    fields precisely so one seller can submit for several stores, and a blacklist that only
    read `store_id` would let a blocked *signer* keep submitting by naming a store that was
    never individually blocked. Blocking an actor has to block the actor.
    """
    if blacklist is None:
        return False
    if isinstance(blacklist, str):
        blocked = [blacklist]
    else:
        try:
            blocked = list(blacklist)
        except TypeError:  # a non-iterable blacklist is not a licence to admit everyone
            return True
    # Fail closed on a blacklist we cannot read (R12). An entry that is not a store/signer id —
    # a dict where a string belongs, half-decoded JSON, a `None` from a nullable column — means
    # the operator asked us to block SOMETHING and we cannot tell what. Ignoring it would turn a
    # malformed eligibility input into permission, which is the direction this project's
    # boundary never takes on doubt. Compared with `==` rather than set membership for the same
    # reason: `unhashable in {...}` raises, and a blacklist that crashes blocks nobody.
    if any(not isinstance(entry, str) for entry in blocked):
        return True
    return any(
        payload.get(field_name) == entry
        for field_name in ("store_id", "signer_id")
        for entry in blocked
    )


def _synthetic_trust_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The `trust_snapshot` mapping `validate_external_submission` needs, from a `blacklist` list.

    The frozen public surface hands this door a `blacklist: list[str]`, while the shared
    boundary takes a snapshot MAPPING and treats a store with *no row* as an unavailable
    eligibility read — which it denies (R12, fail closed). Passing `{}` would therefore reject
    every submission, so the adapter mints the one row the boundary is about to look up.

    That row is deliberately thin: it asserts only "this store is not blacklisted", which is
    exactly what a caller passing a bare `blacklist` has told us, and nothing about the store's
    score. **A caller holding a real snapshot should pass `trust_snapshot=` instead** — this
    function is only reached when nobody did, and it is not a source of trust, it is the
    absence of one written down honestly. The blacklist itself is enforced before this is
    called, so a blocked store never reaches a row that says it is fine.
    """
    store_id = payload.get("store_id")
    if not isinstance(store_id, str) or not store_id:
        # No store id to key a row on. Returning an empty snapshot lets the boundary refuse it
        # as an unavailable read rather than having this adapter invent a verdict.
        return {}
    return {store_id: {"store_id": store_id, "score": None, "blacklisted": False}}


def _work_item(
    payload: Mapping[str, Any],
    signature: str,
    *,
    digest: str,
    unverified_claim_indexes: Sequence[int],
) -> dict[str, Any]:
    """The plain-dict work item handed to claim extraction and verification.

    A dict rather than a model on purpose: the queue is a transport, the next stage re-validates
    whatever it is given, and a shared object would make the door and the worker redeploy
    together. It carries the whole submission — the claims are the thing being queued for
    verification, so dropping them would leave the worker re-fetching what the door already had.

    The `nonce` rides at the top level as the idempotency key: the queue is at-least-once, and
    extraction plus verification must be able to recognise a redelivery of work it has already
    done without re-parsing the submission to find the key.
    """
    submission = dict(payload)
    submission["signature"] = signature
    return {
        "kind": "external_bid_verification",
        "auction_id": payload.get("auction_id"),
        "store_id": payload.get("store_id"),
        "signer_id": payload.get("signer_id"),
        "key_id": payload.get("key_id"),
        "issued_at": payload.get("issued_at"),
        "nonce": payload.get("nonce"),
        "payload_hash": digest,
        "verified": False,
        "verification_status": UNVERIFIED_STATUS,
        "unverified_claim_indexes": list(unverified_claim_indexes),
        "submission": submission,
    }


def receive_bid(
    payload: Any,
    signature: Any,
    keyring: Any,
    *,
    queue: Any = None,
    nonce_store: NonceStore | None = None,
    now: Any = None,
    auction_deadline: Any = None,
    blacklist: Iterable[Any] | None = None,
    freshness_window_seconds: float = DEFAULT_FRESHNESS_WINDOW_SECONDS,
    trust_snapshot: Mapping[str, Any] | None = None,
    list_prices: Mapping[Any, Any] | None = None,
    max_discount_pct: float | None = None,
) -> ExternalBidReceipt:
    """Admit or refuse one externally submitted, signed bid. **Never raises.**

    Args:
        payload: the submission — `Bid` fields plus the five-field D52 signing envelope.
        signature: the hex digest the submitter presented, as it arrived off the wire.
        keyring: `{signer_id: {key_id: secret}}`. The nesting is the contract: two signers may
            legitimately use the same `key_id` string, so a flat `{signer_id: secret}` cannot
            express key selection and is not honoured as a shortcut.
        queue: the verification queue. Called once, with the work item, on acceptance only.
        nonce_store: the replay memory. A fresh `NonceStore()` is used when none is injected,
            which remembers nothing across calls — inject a shared one to actually catch replay.
        now: the instant to judge freshness, the deadline and offer expiry against. Pass it to
            keep the door deterministic; it defaults to the current UTC time.
        auction_deadline: the instant the auction closes. A submission arriving after it is
            refused, and an accepted nonce is remembered until it has passed.
        blacklist: store ids and/or signer ids to refuse outright.
        freshness_window_seconds: how far either side of `now` an `issued_at` may sit.
        trust_snapshot: the real eligibility snapshot, when the caller holds one. Preferred over
            `blacklist`; see `_synthetic_trust_snapshot` for what is assumed when it is absent.
        list_prices: the caller's own catalog, forwarded to the price wall. This is the door it
            matters most on — a Tier-2 store never meets the emitting-side wall and gets to
            choose what its bid says about its own catalog.
        max_discount_pct: a caller-wide discount ceiling, forwarded unchanged.

    Returns:
        `ExternalBidReceipt`. `accepted` is `True` only when every gate passed and the work item
        was enqueued; on every refusal it is `False` and the queue was never called.
    """
    if not isinstance(payload, Mapping):
        return _refuse(REASON_MALFORMED_SUBMISSION)
    if not isinstance(signature, str) or not signature.strip():
        # Checked here as well as inside `verify_signature` so the refusal reason distinguishes
        # "presented nothing" from "presented something wrong" in the rejection log.
        return _refuse(REASON_SIGNATURE_MISSING, payload=payload)

    # 2. The envelope must be complete enough to canonicalize. `canonical_signing_bytes` owns
    #    that rule for both sides; asking it is how the door and the signer stay one protocol.
    try:
        canonical_signing_bytes(payload)
        digest = payload_hash(payload)
    except Exception:  # noqa: BLE001 - CanonicalisationError, or anything unsignable at all
        return _refuse(REASON_ENVELOPE_UNCANONICALIZABLE, payload=payload)

    # 2b. The three envelope ids this function goes on to ACT on — the pair that selects the
    #     signing key, and the pair that spends the nonce — read ONCE here and proven to be
    #     strings before either use.
    #
    #     For an ordinary mapping this cannot fail, and gate 2 is why: `canonical_signing_bytes`
    #     refuses unless `missing_signing_fields` found every one of D52's five envelope fields
    #     present, `isinstance(..., str)` and non-blank, and that refusal is the
    #     REASON_ENVELOPE_UNCANONICALIZABLE returned immediately above. This check is not
    #     written for an ordinary mapping. `receive_bid` accepts ANY `Mapping`, and gate 2 read
    #     these fields through `canonical_signing_bytes` — so without this, the values that
    #     reach `keyring_secret` and `NonceStore.consume` are a SECOND, separately-unvalidated
    #     read of a caller-supplied object, and a `get` that does not answer the same way twice
    #     makes the two reads disagree. That matters most at the nonce: `NonceStore._key`
    #     stringifies both halves so a pair that read as `None` on the second read keys the
    #     replay memory at `("None", "None")` — one slot shared by every submission that plays
    #     the same trick, so the first burns it and the rest are refused as replays, and if the
    #     ordering ever changed it would be one slot admitting all of them. Fail closed, once,
    #     on the same reason gate 2 would have given, rather than trusting a re-read.
    signer_id = payload.get("signer_id")
    key_id = payload.get("key_id")
    nonce = payload.get("nonce")
    if not isinstance(signer_id, str) or not isinstance(key_id, str) or not isinstance(nonce, str):
        return _refuse(REASON_ENVELOPE_UNCANONICALIZABLE, payload=payload)

    # 3. Key SELECTION, never key trial.
    secret = keyring_secret(keyring, signer_id, key_id)
    if secret is None:
        return _refuse(REASON_UNKNOWN_SIGNING_KEY, payload=payload)
    if not verify_signature(payload, signature, secret):
        return _refuse(REASON_SIGNATURE_INVALID, payload=payload)

    # 4a. Freshness, two-sided.
    evaluated_at = parse_timestamp(now) or datetime.now(UTC)
    issued_at = parse_timestamp(payload.get("issued_at"))
    if issued_at is None:
        # The field is present and non-blank (gate 2) but is not an instant. It is inside the
        # signed bytes, so this is a well-formed signature over a meaningless claim about time.
        return _refuse(REASON_ISSUED_AT_UNPARSEABLE, payload=payload)
    try:
        window = float(freshness_window_seconds)
    except (TypeError, ValueError):
        window = DEFAULT_FRESHNESS_WINDOW_SECONDS
    age_seconds = (evaluated_at - issued_at).total_seconds()
    if age_seconds > window:
        return _refuse(REASON_STALE_SUBMISSION, payload=payload)
    if -age_seconds > window:
        return _refuse(REASON_FUTURE_DATED_SUBMISSION, payload=payload)

    # 4b. The auction deadline — a different question from freshness. The late submission is
    #     seconds old and still too late; only this check can see that.
    deadline = parse_timestamp(auction_deadline)
    if deadline is not None and evaluated_at > deadline:
        return _refuse(REASON_AFTER_AUCTION_DEADLINE, payload=payload)

    # 5. Eligibility and the shared Tier-2 boundary.
    if _blacklisted(payload, blacklist):
        return _refuse(REASON_STORE_BLACKLISTED, payload=payload)
    submission = dict(payload)
    submission["signature"] = signature
    verdict = validate_external_submission(
        submission,
        trust_snapshot=(
            trust_snapshot if trust_snapshot is not None else _synthetic_trust_snapshot(payload)
        ),
        now=evaluated_at,
        list_prices=list_prices,
        max_discount_pct=max_discount_pct,
    )
    if not verdict.ok:
        return _refuse(*verdict.reasons, payload=payload)

    # The transport is resolved BEFORE the nonce is spent. A queue we cannot write to means this
    # submission is not going to be admitted, and discovering that after consuming the nonce
    # would burn an honest submitter's one-shot key on our own misconfiguration — they would
    # then be unable to retry the identical submission at all.
    enqueue = _resolve_enqueue(queue)
    if enqueue is None:
        return _refuse(REASON_QUEUE_UNAVAILABLE, payload=payload)

    # 6. Replay. Checked last so a submission refused above never spends the nonce it names —
    #    otherwise anyone could burn an honest signer's nonce with a deliberately broken copy.
    store = nonce_store if nonce_store is not None else NonceStore()
    if not store.consume(signer_id, nonce, auction_deadline):
        return _refuse(REASON_REPLAYED_NONCE, payload=payload)

    item = _work_item(
        payload,
        signature,
        digest=digest,
        unverified_claim_indexes=verdict.unverified_claim_indexes or [],
    )
    try:
        enqueue(item)
    except Exception:  # noqa: BLE001 - a queue that raised did not take the item
        # The nonce stays consumed. The submission WAS authenticated and admitted, and
        # re-opening its nonce because the transport threw would hand a replay window to
        # whoever was watching. The submitter retries with a new nonce, exactly as they would
        # on any 5xx.
        return _refuse(REASON_QUEUE_UNAVAILABLE, payload=payload)

    return ExternalBidReceipt(
        accepted=True,
        verified=False,
        verification_status=UNVERIFIED_STATUS,
        reasons=(),
        signer_id=signer_id if isinstance(signer_id, str) else None,
        nonce=nonce if isinstance(nonce, str) else None,
        unverified_claim_indexes=tuple(verdict.unverified_claim_indexes or ()),
    )


def _resolve_enqueue(queue: Any) -> Any:
    """The one-argument callable that writes to `queue`, or `None` when there isn't one.

    A callable queue is called directly; otherwise the first of the usual method spellings the
    object actually has is used. The door does not get to dictate the transport's vocabulary — a
    `queue.put`, a `queue.enqueue` and a bare function are all the same fact, "this went to the
    next stage" — but it does insist on exactly one call, so the alternatives are tried in order
    and the first that exists wins.

    Resolution is separated from the call so the caller can find out that there is no usable
    transport *before* any state is spent on the submission. Returning `None` rather than
    raising keeps `receive_bid` total.
    """
    if queue is None:
        return None
    if callable(queue):
        return queue
    for method_name in ("enqueue", "put", "submit", "send", "append", "write", "record", "log"):
        method = getattr(queue, method_name, None)
        if callable(method):
            return method
    return None
