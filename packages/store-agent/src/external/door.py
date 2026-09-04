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

import math
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
#: The caller gave no replay memory. Fail closed rather than mint a per-call one (T-232): a
#: store that dies with the call remembers nothing, so the door would have no replay defence
#: and the receipt of a replay would be byte-identical to a real admission.
REASON_REPLAY_MEMORY_UNAVAILABLE = "replay_memory_unavailable"
#: `freshness_window_seconds` is not a finite, non-negative number, so it is not a window
#: (T-231/T-230). NaN passes `float()` and loses every comparison, `inf` and `10**400` say
#: "never stale", and a negative window says "always stale" — none of them is a policy this
#: door may silently substitute a default for.
REASON_FRESHNESS_WINDOW_INVALID = "freshness_window_invalid"
#: The door itself failed. `receive_bid` is documented **Never raises** and is the anonymous
#: external entry point, so an unhandled error must become a refusal rather than a 500 (T-230).
REASON_DOOR_FAILED_CLOSED = "door_failed_closed"


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
    """A rejection receipt. Never carries `accepted=True` by any path, and **never raises**.

    `payload` is read only to fill `signer_id`/`nonce` into the receipt for the rejection log,
    and that read is guarded (T-230). This is the REFUSAL path: it exists for hostile input, so
    it is the one place that must be safe on a mapping whose `.get` raises. Every refusal after
    the door has snapshotted the submission passes the snapshot — a plain dict this module
    built — so in practice nothing hostile reaches here at all; the guard is what makes that
    true by construction rather than by the current arrangement of the call sites.
    """
    try:
        signer_id = payload.get("signer_id") if isinstance(payload, Mapping) else None
    except Exception:  # noqa: BLE001 - diagnosis is worth nothing next to a total refusal path
        signer_id = None
    try:
        nonce = payload.get("nonce") if isinstance(payload, Mapping) else None
    except Exception:  # noqa: BLE001
        nonce = None
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
        except Exception:  # noqa: BLE001 - a blacklist we cannot read is not a licence to admit
            # Every failure to read the operator's block list means the same thing, so they are
            # all answered the same way. It used to catch `TypeError` only, which covered the
            # non-iterable case and let an `__iter__` that raised anything else — a feed
            # answering with half-decoded JSON raises `ValueError`, a broken DB cursor raises
            # its own driver error — propagate straight out of the eligibility check and out of
            # `receive_bid`, which is documented never to raise (T-230).
            return True
    # Fail closed on a blacklist we cannot read (R12). An entry that is not a store/signer id —
    # a dict where a string belongs, half-decoded JSON, a `None` from a nullable column — means
    # the operator asked us to block SOMETHING and we cannot tell what. Ignoring it would turn a
    # malformed eligibility input into permission, which is the direction this project's
    # boundary never takes on doubt. Compared with `==` rather than set membership for the same
    # reason: `unhashable in {...}` raises, and a blacklist that crashes blocks nobody.
    if any(not isinstance(entry, str) for entry in blocked):
        return True
    try:
        return any(
            payload.get(field_name) == entry
            for field_name in ("store_id", "signer_id")
            for entry in blocked
        )
    except Exception:  # noqa: BLE001 - an id whose __eq__ raises is an id we cannot clear
        return True


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


def _snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """One read of the caller's mapping, into a plain `dict`. Everything after reads this.

    `receive_bid` accepts ANY `Mapping` — that is its signature and its published contract — and
    it used to read the caller's object over and over: `missing_signing_fields` read it,
    `canonical_signing_bytes` read it, the door's own envelope guard read it, `verify_signature`
    re-canonicalized and read it again, the shared boundary was handed `dict(payload)`, and
    `_work_item` built a SECOND `dict(payload)` for the queue. Nothing bound those reads
    together, so a mapping that answered differently between two of them made the value that was
    VERIFIED and the value that was ENQUEUED two different things (T-229, T-241): a correctly
    signed bid for 89.00 was enqueued as 1.00 with `accepted=True, reasons=()`, and every one of
    the work item's six identity fields could be swapped for an attacker's — `payload_hash`
    covers only the bid BODY, so for `issued_at`, `nonce`, `signer_id` and `key_id` the enqueued
    item stayed internally consistent and nothing downstream could tell.

    Read through `.get`, which is the accessor the door and the canonicalizer both use, so the
    snapshot REPLACES a read the door was going to do rather than adding a third one.

    Raising is the correct answer to a mapping that cannot be read: the caller of this function
    turns it into a refusal, which is what a submission we cannot even copy deserves.
    """
    return {key: payload.get(key) for key in list(payload)}


def _freshness_window(value: Any) -> float | None:
    """`freshness_window_seconds` as a finite, non-negative float — or `None`, meaning refuse.

    `float()` was the whole validation, and it is not enough (T-231, T-230):

    * `float('nan')` passes it, and EVERY comparison against NaN is `False`, so both `age >
      window` and `-age > window` fail and the two-sided freshness gate silently stops
      refusing anything. Measured: a six-year-stale bid, correctly refused at the default
      window, was ACCEPTED with `nan`.
    * `float('inf')` says the same thing honestly, and a negative window says the opposite —
      neither is a window.
    * `float(10**400)` does not return at all, it raises `OverflowError`, and the old handler
      caught `(TypeError, ValueError)` only, so it propagated out of a function documented
      **Never raises**.

    A bad window is REFUSED rather than quietly replaced with `DEFAULT_FRESHNESS_WINDOW_SECONDS`.
    Substituting a policy the caller did not ask for is how an operator ends up believing a
    window is in force that is not, and this is the single knob that decides how long a captured
    submission stays replayable — the one place a silent default is least affordable.
    """
    try:
        window = float(value)
    except Exception:  # noqa: BLE001 - OverflowError, TypeError, ValueError, a hostile __float__
        return None
    if not math.isfinite(window) or window < 0.0:
        return None
    return window


def _work_item(
    submission: dict[str, Any],
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

    **`submission` is the door's own snapshot, not the caller's mapping** (T-229, T-241). The
    six identity fields and the queued body are therefore two views of ONE dict — the same dict
    the signature was verified over, the same dict the shared boundary judged — rather than a
    later, separately-unvalidated read of an object that is free to answer differently. The
    parameter is typed `dict` rather than `Mapping` to say so in the signature: hand this the
    caller's object again and the defect comes straight back.
    """
    body = dict(submission)
    body["signature"] = signature
    return {
        "kind": "external_bid_verification",
        "auction_id": submission.get("auction_id"),
        "store_id": submission.get("store_id"),
        "signer_id": submission.get("signer_id"),
        "key_id": submission.get("key_id"),
        "issued_at": submission.get("issued_at"),
        "nonce": submission.get("nonce"),
        "payload_hash": digest,
        "verified": False,
        "verification_status": UNVERIFIED_STATUS,
        "unverified_claim_indexes": list(unverified_claim_indexes),
        "submission": body,
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
    """Admit or refuse one externally submitted, signed bid. **Never raises** — see below.

    This is a thin total wrapper around `_receive_bid`, and the split is the point. "Never
    raises" is a security property here, not a courtesy: this function is what an anonymous
    `POST /v1/auctions/{auction_id}/bids` reaches, so an exception is a 500 handed to an
    unauthenticated submitter — a stack trace, a burnt worker, and an oracle — where a refusal
    belongs. Every hazard the gates know about is handled by name inside `_receive_bid`; three
    of them were measured escaping it (an overflowing `freshness_window_seconds`, a blacklist
    whose `__iter__` raised, and a payload whose `.get` raised THROUGH THE REFUSAL PATH), and
    naming three is not the same as being total. This wrapper is what makes the docstring true
    by construction rather than by enumeration, and it fails CLOSED: an error the door did not
    anticipate is a refusal, never an admission (T-230).
    """
    try:
        return _receive_bid(
            payload,
            signature,
            keyring,
            queue=queue,
            nonce_store=nonce_store,
            now=now,
            auction_deadline=auction_deadline,
            blacklist=blacklist,
            freshness_window_seconds=freshness_window_seconds,
            trust_snapshot=trust_snapshot,
            list_prices=list_prices,
            max_discount_pct=max_discount_pct,
        )
    except Exception:  # noqa: BLE001 - a door that raises is a door that 500s; fail closed
        return _refuse(REASON_DOOR_FAILED_CLOSED)


def _receive_bid(
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
    """The six gates. Called only by `receive_bid`, which is the total wrapper.

    Args:
        payload: the submission — `Bid` fields plus the five-field D52 signing envelope.
        signature: the hex digest the submitter presented, as it arrived off the wire.
        keyring: `{signer_id: {key_id: secret}}`. The nesting is the contract: two signers may
            legitimately use the same `key_id` string, so a flat `{signer_id: secret}` cannot
            express key selection and is not honoured as a shortcut.
        queue: the verification queue. Called once, with the work item, on acceptance only.
        nonce_store: the replay memory, and it is **required in practice**. A submission judged
            without one is REFUSED `replay_memory_unavailable` (T-232). It used to default to a
            fresh `NonceStore()` per call, which remembers nothing between submissions: the
            identical signed bid was admitted three times running, and the receipt of a replay
            was byte-identical to the receipt of a real admission, so the missing defence was
            invisible from outside. Replay protection must not be something a caller switches
            off by forgetting an argument. It stays keyword-with-a-default rather than
            positional-required so the five-name public surface is unchanged and the failure is
            a refusal at runtime with a reason on it, not a `TypeError` from the entry point.
        now: the instant to judge freshness, the deadline and offer expiry against. Pass it to
            keep the door deterministic; it defaults to the current UTC time.
        auction_deadline: the instant the auction closes. A submission arriving after it is
            refused, and an accepted nonce is remembered until it has passed.
        blacklist: store ids and/or signer ids to refuse outright.
        freshness_window_seconds: how far either side of `now` an `issued_at` may sit. Must be a
            finite, non-negative number; anything else — `nan`, `inf`, `10**400`, a negative,
            `None`, a string — refuses the submission rather than being silently replaced with
            the default (T-231). See `_freshness_window`.
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
    #    This one asks the CALLER'S object, because that is the object whose bytes the submitter
    #    claims to have signed, and a submission that cannot be canonicalized at all is refused
    #    before anything is copied.
    try:
        canonical_signing_bytes(payload)
    except Exception:  # noqa: BLE001 - CanonicalisationError, or anything unsignable at all
        return _refuse(REASON_ENVELOPE_UNCANONICALIZABLE, payload=payload)

    # 2b. THE SNAPSHOT. `payload` — the caller's mapping — is read here for the last time.
    #     Everything below this line reads `submitted`, a plain dict this module owns.
    #
    #     T-228 read the three envelope ids the door itself acts on once, here, and proved them
    #     strings. That closed the two SECURITY decisions and left the rest: `verify_signature`
    #     re-canonicalized the caller's object, the shared boundary was handed `dict(payload)`,
    #     and `_work_item` built a SECOND `dict(payload)` afterwards, so the body that was
    #     VALIDATED and the body that was ENQUEUED were two different reads of an object free to
    #     answer differently between them — measured at 89.00 in, 1.00 out, `accepted=True`,
    #     `reasons=()` (T-229) — and all six of the work item's identity fields could be swapped
    #     the same way (T-241). Hardening three fields could not fix that; only reading once can.
    #
    #     Refusing a mapping we cannot copy is the fail-closed direction, and the refusal is
    #     safe on exactly the input it exists for because `_refuse` no longer re-reads what it
    #     is refusing (T-230).
    try:
        submitted = _snapshot(payload)
    except Exception:  # noqa: BLE001 - a mapping that cannot be read is a malformed submission
        return _refuse(REASON_MALFORMED_SUBMISSION)

    #     Re-anchor the protocol on the snapshot. The bytes that were canonicalized at gate 2
    #     belong to the caller's object; these belong to the document that is actually going to
    #     be judged and queued, and `digest` is the hash the work item carries. If the mapping
    #     answered differently between the two, the snapshot's bytes no longer match the
    #     signature and gate 3 refuses it — the submission is authenticated over precisely the
    #     bytes that reach the queue, which is the property T-229 asks for stated in full.
    try:
        canonical_signing_bytes(submitted)
        digest = payload_hash(submitted)
    except Exception:  # noqa: BLE001
        return _refuse(REASON_ENVELOPE_UNCANONICALIZABLE, payload=submitted)

    #     The three ids this function goes on to ACT on — the pair that selects the signing key,
    #     and the pair that spends the nonce — proven to be strings before either use. It
    #     matters most at the nonce: `NonceStore._key` stringifies both halves, so a `None` here
    #     would key the replay memory at `("None", "None")`, one slot shared by every submission
    #     that plays the same trick.
    signer_id = submitted.get("signer_id")
    key_id = submitted.get("key_id")
    nonce = submitted.get("nonce")
    if not isinstance(signer_id, str) or not isinstance(key_id, str) or not isinstance(nonce, str):
        return _refuse(REASON_ENVELOPE_UNCANONICALIZABLE, payload=submitted)

    # 3. Key SELECTION, never key trial.
    secret = keyring_secret(keyring, signer_id, key_id)
    if secret is None:
        return _refuse(REASON_UNKNOWN_SIGNING_KEY, payload=submitted)
    if not verify_signature(submitted, signature, secret):
        return _refuse(REASON_SIGNATURE_INVALID, payload=submitted)

    # 4a. Freshness, two-sided.
    evaluated_at = parse_timestamp(now) or datetime.now(UTC)
    issued_at = parse_timestamp(submitted.get("issued_at"))
    if issued_at is None:
        # The field is present and non-blank (gate 2) but is not an instant. It is inside the
        # signed bytes, so this is a well-formed signature over a meaningless claim about time.
        return _refuse(REASON_ISSUED_AT_UNPARSEABLE, payload=submitted)
    window = _freshness_window(freshness_window_seconds)
    if window is None:
        return _refuse(REASON_FRESHNESS_WINDOW_INVALID, payload=submitted)
    age_seconds = (evaluated_at - issued_at).total_seconds()
    if age_seconds > window:
        return _refuse(REASON_STALE_SUBMISSION, payload=submitted)
    if -age_seconds > window:
        return _refuse(REASON_FUTURE_DATED_SUBMISSION, payload=submitted)

    # 4b. The auction deadline — a different question from freshness. The late submission is
    #     seconds old and still too late; only this check can see that.
    deadline = parse_timestamp(auction_deadline)
    if deadline is not None and evaluated_at > deadline:
        return _refuse(REASON_AFTER_AUCTION_DEADLINE, payload=submitted)

    # 5. Eligibility and the shared Tier-2 boundary — judged on the snapshot, so the document
    #    the boundary approves is the document the queue receives.
    if _blacklisted(submitted, blacklist):
        return _refuse(REASON_STORE_BLACKLISTED, payload=submitted)
    submission = dict(submitted)
    submission["signature"] = signature
    verdict = validate_external_submission(
        submission,
        trust_snapshot=(
            trust_snapshot if trust_snapshot is not None else _synthetic_trust_snapshot(submitted)
        ),
        now=evaluated_at,
        list_prices=list_prices,
        max_discount_pct=max_discount_pct,
    )
    if not verdict.ok:
        return _refuse(*verdict.reasons, payload=submitted)

    # The transport is resolved BEFORE the nonce is spent. A queue we cannot write to means this
    # submission is not going to be admitted, and discovering that after consuming the nonce
    # would burn an honest submitter's one-shot key on our own misconfiguration — they would
    # then be unable to retry the identical submission at all.
    enqueue = _resolve_enqueue(queue)
    if enqueue is None:
        return _refuse(REASON_QUEUE_UNAVAILABLE, payload=submitted)

    # 6. Replay. Checked last so a submission refused above never spends the nonce it names —
    #    otherwise anyone could burn an honest signer's nonce with a deliberately broken copy.
    #
    #    No injected store, no admission (T-232). This used to mint a fresh `NonceStore()`, one
    #    per call, which is a replay memory that remembers nothing: the identical signed bid was
    #    admitted three times running and the three receipts were indistinguishable from real
    #    admissions. Documenting it was not enough, because this is the one gate whose absence
    #    cannot be noticed by watching the door work — every other missing gate eventually shows
    #    up as something wrong getting in, and this one shows up as nothing at all.
    if nonce_store is None:
        return _refuse(REASON_REPLAY_MEMORY_UNAVAILABLE, payload=submitted)
    if not nonce_store.consume(signer_id, nonce, auction_deadline):
        return _refuse(REASON_REPLAYED_NONCE, payload=submitted)

    item = _work_item(
        submitted,
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
        return _refuse(REASON_QUEUE_UNAVAILABLE, payload=submitted)

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
    try:
        if callable(queue):
            return queue
        for method_name in ("enqueue", "put", "submit", "send", "append", "write", "record", "log"):
            method = getattr(queue, method_name, None)
            if callable(method):
                return method
    except Exception:  # noqa: BLE001 - a transport whose attribute access raises is no transport
        # `getattr` on a caller-supplied object runs the caller's `__getattr__`/descriptors, and
        # `callable()` runs its metaclass. Neither is ours to trust, and "there is no usable
        # transport" is the honest answer to an object that cannot be asked (T-230).
        return None
    return None
