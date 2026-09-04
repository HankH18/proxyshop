"""T-044 — the signed external bid door (`store_agent.external`).

This is the ticket's own gate. It is deliberately NOT a copy of the frozen acceptance
suite: it checks the things the frozen suite can only observe indirectly, and it checks
them against the module's real import path in this workspace.

Three obligations it exists to hold:

1. **The canonicalizer is re-exported, never re-implemented.** `canonical_signing_bytes`
   and `payload_hash` must be the *same objects* as `contracts.signing`'s. Two
   canonicalizers is two protocols, and a signer and a verifier that disagree about the
   bytes fail open the moment either one is touched. Identity — not equal output on the
   fixtures this file happens to try — is the only assertion that keeps them one.
2. **Every refusal happens before the queue is touched.** A door that enqueues and then
   decides has already handed the untrusted item to the next stage.
3. **The public surface is exactly five names.** `__all__` is the contract the frozen
   suite binds to; anything else this module grows is private.

Product code is imported inside the test bodies so an unbuilt module is a per-test
failure rather than a collection error.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping

import pytest

EXPECTED_EXPORTS = frozenset(
    {"canonical_signing_bytes", "payload_hash", "sign_bid", "receive_bid", "NonceStore"}
)

SIGNER = "store-external-1"
SIGNER_2 = "store-external-2"
KEY_ID = "key-2026-01"
KEY = "gate-secret-0001"
KEY_ID_ROTATED = "key-2026-07"
KEY_ROTATED = "gate-secret-0002"
KEY_2 = "gate-secret-0003"

ISSUED_AT = "2026-01-01T00:00:00Z"
NOW = "2026-01-01T00:00:05Z"
DEADLINE = "2026-01-01T00:05:00Z"
FAR_FUTURE = "2999-01-01T00:00:00Z"


def _keyring() -> dict:
    return {
        SIGNER: {KEY_ID: KEY, KEY_ID_ROTATED: KEY_ROTATED},
        SIGNER_2: {KEY_ID: KEY_2},
    }


def _provenance(source: str = "seller_asserted") -> dict:
    return {
        "source": source,
        "ref": "pitch:gate-1#material",
        "observed_at": ISSUED_AT,
        "authority_rank": 1,
    }


def _payload(**overrides) -> dict:
    store_id = overrides.pop("store_id", SIGNER)
    unit_price = overrides.pop("unit_price", 89.0)
    payload = {
        "auction_id": "auc-gate-1",
        "store_id": store_id,
        "offer": {
            "product_ref": "gate-prod-1",
            "unit_price": unit_price,
            "total_price": unit_price,
            "discount": None,
            "commitments": [],
            "expires_at": FAR_FUTURE,
        },
        "claims": [{"key": "material", "value": "merino wool", "provenance": _provenance()}],
        "message": "Warmest merino mid-layer on the market.",
        "agent_version": "ext-0.1.0",
        "schema_version": "1",
        "signer_id": store_id,
        "key_id": KEY_ID,
        "issued_at": ISSUED_AT,
        "nonce": "nonce-gate-0001",
    }
    payload.update(overrides)
    return payload


class _Queue:
    """A queue that records, and that fails loudly if the door writes to it twice."""

    def __init__(self) -> None:
        self.items: list = []

    def __call__(self, item) -> None:
        self.items.append(item)

    @property
    def count(self) -> int:
        return len(self.items)


def _trust_snapshot_for(payload) -> dict:
    """The eligibility read this caller holds for the store the payload names.

    Re-baselined with the T-233 fix. These calls used to pass no `trust_snapshot=` at all and
    relied on the door minting one for them out of the submitter's own `store_id` — which is
    what T-233 was: the door manufacturing an eligibility verdict nobody gave it, so that
    forgetting the argument was more permissive than passing `{}`. The row has not disappeared,
    it has moved to the only place that can honestly assert it: the caller. A test that means
    to exercise the signature, the nonce, the deadline or the queue must supply the eligibility
    input a real caller supplies, or it is measuring the defaulting path instead.

    Keyed on `store_id` because that is the key the shared boundary looks up
    (`_eligibility_reasons(_get(bid, "store_id"), trust_snapshot)`), and `signer_id` is checked
    against the snapshot nowhere — the signer/store distinction is the `blacklist`'s job. Reads
    defensively because some callers below hand the door hostile objects on purpose.
    """
    try:
        store_id = payload.get("store_id")
    except Exception:  # noqa: BLE001 - a payload whose .get raises is one we hold no row for
        store_id = None
    if not isinstance(store_id, str) or not store_id:
        return {}
    return {store_id: {"store_id": store_id, "score": 0.9, "blacklisted": False}}


def _receive(payload, signature, **kwargs):
    from store_agent.external import NonceStore, receive_bid

    queue = kwargs.pop("queue", None) or _Queue()
    kwargs.setdefault("nonce_store", NonceStore())
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("auction_deadline", DEADLINE)
    kwargs.setdefault("trust_snapshot", _trust_snapshot_for(payload))
    keyring = kwargs.pop("keyring", None)
    ring = _keyring() if keyring is None else keyring
    try:
        result = receive_bid(payload, signature, ring, queue=queue, **kwargs)
    except Exception as exc:  # noqa: BLE001 - refusing by raising is allowed
        return None, queue, exc
    return result, queue, None


def test_module_exports_exactly_the_five_public_symbols() -> None:
    """The frozen suite binds to five names. Anything else here is private."""
    import store_agent.external as external

    assert set(external.__all__) == EXPECTED_EXPORTS, (
        f"__all__ must be exactly {sorted(EXPECTED_EXPORTS)}, got {sorted(external.__all__)}"
    )
    for name in EXPECTED_EXPORTS:
        assert hasattr(external, name), f"{name} is exported in __all__ but not defined"


def test_canonicalizer_and_payload_hash_are_the_contracts_objects_not_copies() -> None:
    """Re-export, not re-implementation: one canonical form or the protocol has forked."""
    import contracts.signing as contracts_signing
    import store_agent.external as external

    assert external.canonical_signing_bytes is contracts_signing.canonical_signing_bytes, (
        "canonical_signing_bytes must BE contracts.signing's function object — a second "
        "implementation is a second protocol, and the signer and the door would drift apart"
    )
    assert external.payload_hash is contracts_signing.payload_hash, (
        "payload_hash must BE contracts.signing's function object for the same reason"
    )


def test_sign_bid_is_a_deterministic_hmac_over_the_canonical_bytes() -> None:
    """The signature is reproducible from the published canonical form and the secret alone."""
    from store_agent.external import canonical_signing_bytes, sign_bid

    payload = _payload()
    signature = sign_bid(payload, KEY)
    assert isinstance(signature, str) and signature, "sign_bid must return a non-empty string"
    assert sign_bid(payload, KEY) == signature, "signing must be deterministic"

    expected = hmac.new(
        KEY.encode("utf-8"), canonical_signing_bytes(payload), hashlib.sha256
    ).hexdigest()
    assert signature == expected, (
        "the signature must be HMAC-SHA256 over exactly canonical_signing_bytes(payload), so a "
        f"third party can reproduce it from the published contract; got {signature!r}"
    )
    assert sign_bid(payload, "another-secret") != signature, "the secret must change the signature"
    assert sign_bid(_payload(unit_price=1.0), KEY) != signature, (
        "the body must change the signature — otherwise the price is not covered"
    )


def test_sign_bid_refuses_an_incomplete_envelope_rather_than_signing_it() -> None:
    """Producing bytes for a submission that cannot legally exist helps nobody."""
    from contracts.signing import REQUIRED_SIGNING_FIELDS
    from store_agent.external import sign_bid

    for field in REQUIRED_SIGNING_FIELDS:
        broken = _payload()
        del broken[field]
        with pytest.raises(Exception):  # noqa: B017 - CanonicalisationError is a ValueError
            sign_bid(broken, KEY)


def test_a_correctly_signed_bid_is_admitted_unverified_and_queued_once() -> None:
    """Admitted is not trusted: the door marks it unverified and hands it to verification."""
    from store_agent.external import sign_bid

    payload = _payload()
    result, queue, error = _receive(payload, sign_bid(payload, KEY))
    assert error is None, f"a valid submission must not raise: {error!r}"
    assert result.accepted is True, f"a correctly signed bid must be accepted: {result!r}"
    assert result.verified is False, "an external bid is never admitted as verified"
    assert queue.count == 1, f"exactly one enqueue, saw {queue.count}"

    item = json.dumps(queue.items[0], default=lambda o: getattr(o, "__dict__", str(o)))
    assert payload["nonce"] in item, (
        "the queued work item must carry the submission's nonce so extraction and "
        f"verification stay idempotent: {item!r}"
    )
    assert "seller_asserted" in item, (
        f"the queued work item must carry the claims that need verifying: {item!r}"
    )


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("garbage signature", lambda p, s: (p, "not-a-signature")),
        ("absent signature", lambda p, s: (p, None)),
        ("non-string signature", lambda p, s: (p, 12345)),
        ("tampered auction_id", lambda p, s: (dict(p, auction_id="auc-gate-999"), s)),
        ("tampered issued_at", lambda p, s: (dict(p, issued_at="2026-01-01T00:00:01Z"), s)),
        ("tampered nonce", lambda p, s: (dict(p, nonce="nonce-gate-0002"), s)),
    ],
)
def test_the_queue_is_never_touched_by_a_refused_submission(case, mutate) -> None:
    """Rejection always precedes enqueue — for every shape of refusal."""
    from store_agent.external import sign_bid

    payload = _payload()
    mutated, signature = mutate(payload, sign_bid(payload, KEY))
    result, queue, _error = _receive(mutated, signature)
    assert queue.count == 0, f"{case}: the queue must not be written to, saw {queue.count} call(s)"
    assert result is None or result.accepted is not True, f"{case}: must not be accepted"


def test_the_key_id_selects_one_secret_and_the_door_never_tries_the_others() -> None:
    """Key trial defeats revocation: a retired key would keep working while any live key exists."""
    from store_agent.external import sign_bid

    rotated = _payload(key_id=KEY_ID_ROTATED, nonce="nonce-gate-rot")
    good, _queue, _err = _receive(rotated, sign_bid(rotated, KEY_ROTATED))
    assert good.accepted is True, "the signer's rotated key must authenticate"

    bad, queue, _err2 = _receive(rotated, sign_bid(rotated, KEY))
    assert bad is None or bad.accepted is not True, (
        "a bid stamped with the rotated key_id but signed with the OLD secret must reject — "
        "the door must not fall back to the signer's other live keys"
    )
    assert queue.count == 0

    flat, flat_queue, _err3 = _receive(_payload(), sign_bid(_payload(), KEY), keyring={SIGNER: KEY})
    assert flat is None or flat.accepted is not True, (
        "a flat {signer_id: secret} keyring cannot express key selection and must not authenticate"
    )
    assert flat_queue.count == 0


def test_a_nonce_is_single_use_per_signer_and_survives_until_the_deadline_passes() -> None:
    """Replay defence, and the retention window that makes it worth anything."""
    from store_agent.external import NonceStore, sign_bid

    store = NonceStore()
    payload = _payload()
    signature = sign_bid(payload, KEY)

    first, _q1, _e1 = _receive(payload, signature, nonce_store=store)
    assert first.accepted is True
    assert store.seen(SIGNER, payload["nonce"]) is True, (
        "an accepted submission must consume its nonce, and `seen` must return a real bool"
    )

    replay, replay_queue, _e2 = _receive(payload, signature, nonce_store=store)
    assert replay is None or replay.accepted is not True, "a replayed nonce must reject"
    assert replay_queue.count == 0

    assert store.seen(SIGNER_2, payload["nonce"]) is False, (
        "nonce scoping is per signer: the same string from another signer is unseen"
    )

    # `seen` is a pure read — it must not re-arm the entry it just reported.
    store.purge_expired("2026-01-01T00:04:59Z")
    assert store.seen(SIGNER, payload["nonce"]) is True, (
        "a consumed nonce must still be remembered at any moment before the auction deadline"
    )
    store.purge_expired("2026-01-01T00:05:01Z")
    assert store.seen(SIGNER, payload["nonce"]) is False, (
        "retention ends only after the auction deadline has passed"
    )


def test_freshness_is_two_sided_and_the_deadline_is_a_separate_gate() -> None:
    """A stale bid and a future-dated bid both reject; a late arrival rejects on its own check."""
    from store_agent.external import sign_bid

    fresh = _payload()
    signature = sign_bid(fresh, KEY)

    ok, _q, _e = _receive(fresh, signature, now=NOW, auction_deadline=FAR_FUTURE)
    assert ok.accepted is True, "control: +5s must be inside the default window"

    stale, stale_q, _e2 = _receive(
        fresh, signature, now="2026-01-31T00:00:00Z", auction_deadline=FAR_FUTURE
    )
    assert stale is None or stale.accepted is not True, "a 30-day-old submission must reject"
    assert stale_q.count == 0

    future, future_q, _e3 = _receive(
        fresh, signature, now="2025-12-31T00:00:00Z", auction_deadline=FAR_FUTURE
    )
    assert future is None or future.accepted is not True, (
        "a submission issued a day in the FUTURE must reject — skew tolerance is bounded too"
    )
    assert future_q.count == 0

    inside, _q4, _e4 = _receive(
        fresh,
        signature,
        now="2026-01-01T00:02:00Z",
        auction_deadline=FAR_FUTURE,
        freshness_window_seconds=300,
    )
    assert inside.accepted is True, "+120s is inside an explicit 300s window"
    outside, outside_q, _e5 = _receive(
        fresh,
        signature,
        now="2026-01-01T00:10:00Z",
        auction_deadline=FAR_FUTURE,
        freshness_window_seconds=300,
    )
    assert outside is None or outside.accepted is not True, "+600s is outside a 300s window"
    assert outside_q.count == 0

    # The late arrival is only ONE SECOND old, so freshness cannot be what rejects it.
    late = _payload(issued_at=DEADLINE, nonce="nonce-gate-late")
    result, late_q, _e6 = _receive(
        late,
        sign_bid(late, KEY),
        now="2026-01-01T00:05:01Z",
        auction_deadline=DEADLINE,
    )
    assert result is None or result.accepted is not True, (
        "a bid arriving after the auction deadline must reject on the deadline check, which is "
        "distinct from the freshness check this bid passes"
    )
    assert late_q.count == 0


def test_expiry_blacklist_and_an_unregistered_signer_all_reject_before_enqueue() -> None:
    """The door runs the shared Tier-2 boundary, not a private re-reading of it."""
    from store_agent.external import sign_bid

    expired = _payload(nonce="nonce-gate-exp")
    expired["offer"]["expires_at"] = "2000-01-01T00:00:00Z"
    result, queue, _e = _receive(expired, sign_bid(expired, KEY))
    assert result is None or result.accepted is not True, "an expired offer must reject"
    assert queue.count == 0

    good = _payload()
    blocked, blocked_q, _e2 = _receive(good, sign_bid(good, KEY), blacklist=[SIGNER])
    assert blocked is None or blocked.accepted is not True, "a blacklisted store must reject"
    assert blocked_q.count == 0

    stranger = _payload(store_id="store-external-9")
    unknown, unknown_q, _e3 = _receive(stranger, sign_bid(stranger, KEY))
    assert unknown is None or unknown.accepted is not True, (
        "a signer absent from the keyring has no key to check against and must not be admitted"
    )
    assert unknown_q.count == 0


def test_a_blacklisted_signer_cannot_submit_under_an_unblocked_store_id() -> None:
    """Blocking an actor has to block the actor, not one of the names it answers to.

    `signer_id` and `store_id` are separate fields precisely so one seller may submit for
    several stores. A blacklist that only read `store_id` would therefore block a *shopfront*
    and leave the blocked signer submitting through any other shopfront it is registered for —
    which is the one thing an operator reaches for a blacklist to prevent.
    """
    from store_agent.external import sign_bid

    # signer_id stays the blacklisted signer; store_id is a shopfront nobody blocked.
    disguised = dict(_payload(), store_id="store-front-unblocked")
    signature = sign_bid(disguised, KEY)

    control, control_queue, _err = _receive(disguised, signature)
    assert control.accepted is True, (
        "control: with no blacklist this submission must be admitted, or the rejection below "
        f"proves nothing about the blacklist: {control!r}"
    )
    assert control_queue.count == 1

    blocked, blocked_queue, _err2 = _receive(disguised, signature, blacklist=[SIGNER])
    assert blocked is None or blocked.accepted is not True, (
        "a blacklisted SIGNER must reject even when it names a store id that is not blacklisted"
    )
    assert blocked_queue.count == 0


def test_an_unusable_verification_queue_refuses_before_spending_the_nonce() -> None:
    """A transport we cannot write to must not cost the submitter their one-shot nonce.

    The nonce is single-use per signer. If the door consumed it and only then discovered it had
    nowhere to put the work item, an honest submitter's retry of the identical submission would
    be rejected as a replay — our misconfiguration, charged to them, unrecoverably.
    """
    from store_agent.external import NonceStore, receive_bid, sign_bid

    store = NonceStore()
    payload = _payload()
    result = receive_bid(
        payload,
        sign_bid(payload, KEY),
        _keyring(),
        queue=object(),  # no __call__ and none of the known method spellings
        nonce_store=store,
        now=NOW,
        auction_deadline=DEADLINE,
        trust_snapshot=_trust_snapshot_for(payload),
    )
    assert result is None or result.accepted is not True, (
        "a submission that could not be enqueued must not report itself accepted"
    )
    assert store.seen(SIGNER, payload["nonce"]) is False, (
        "the nonce must NOT be consumed when the door never managed to enqueue the bid — "
        "otherwise the submitter cannot retry the same submission"
    )


def test_a_queue_that_raises_is_never_reported_as_a_successful_admission() -> None:
    """`accepted is True` is a claim that the work item reached the next stage."""
    from store_agent.external import NonceStore, receive_bid, sign_bid

    class _ExplodingQueue:
        def __init__(self) -> None:
            self.attempts = 0

        def __call__(self, item) -> None:
            self.attempts += 1
            raise RuntimeError("broker unavailable")

    queue = _ExplodingQueue()
    payload = _payload()
    result = receive_bid(
        payload,
        sign_bid(payload, KEY),
        _keyring(),
        queue=queue,
        nonce_store=NonceStore(),
        now=NOW,
        auction_deadline=DEADLINE,
        trust_snapshot=_trust_snapshot_for(payload),
    )
    assert queue.attempts == 1, "the door must have genuinely attempted the write exactly once"
    assert result is None or result.accepted is not True, (
        "a bid whose enqueue raised must not come back accepted — nothing received it"
    )


@pytest.mark.parametrize(
    ("case", "args"),
    [
        ("payload is None", (None, "sig", {})),
        ("payload is a string", ("not-a-submission", "sig", {})),
        ("payload is a list", ([1, 2, 3], "sig", {})),
        ("keyring is None", (None, "sig", None)),
        ("keyring is a string", (None, "sig", "not-a-keyring")),
        ("keyring is a list", (None, "sig", [SIGNER])),
        ("signature is a dict", (None, {"sig": 1}, None)),
        ("signature is bytes", (None, b"\xff\xfe", None)),
        ("unhashable store_id", ({"store_id": {"a": 1}}, "sig", {})),
    ],
)
def test_the_door_refuses_hostile_input_rather_than_raising(case, args) -> None:
    """Every caller of this function is anonymous, so an exception here is a 500 for free.

    A door that crashes on a malformed body hands an unauthenticated submitter a way to make the
    exchange emit stack traces and burn workers, without ever holding a key. `None` payloads
    below mean "use the real fixture"; the interesting mutation is in the other argument.
    """
    from store_agent.external import NonceStore, receive_bid, sign_bid

    raw_payload, signature, keyring = args
    payload = _payload() if raw_payload is None else raw_payload
    if signature == "sig":
        try:
            signature = sign_bid(_payload(), KEY)
        except Exception:  # noqa: BLE001 - an unsignable fixture still exercises the door
            signature = "unsignable"

    queue = _Queue()
    try:
        result = receive_bid(
            payload,
            signature,
            keyring,
            queue=queue,
            nonce_store=NonceStore(),
            now=NOW,
            auction_deadline=DEADLINE,
            trust_snapshot=_trust_snapshot_for(payload),
        )
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"{case}: receive_bid must refuse hostile input, not raise {type(exc).__name__}: {exc}"
        ) from exc

    assert result is None or result.accepted is not True, f"{case}: must not be accepted"
    assert queue.count == 0, f"{case}: the queue must not be written to"


def test_a_blacklist_the_door_cannot_read_blocks_everything_rather_than_nobody() -> None:
    """Fail closed. An eligibility input we cannot interpret is not permission to admit."""
    from store_agent.external import sign_bid

    payload = _payload()
    signature = sign_bid(payload, KEY)

    for case, blacklist in (
        ("a non-iterable blacklist", 12345),
        ("a blacklist of unhashable entries", [{"store_id": SIGNER}]),
    ):
        result, queue, _err = _receive(payload, signature, blacklist=blacklist)
        assert result is None or result.accepted is not True, (
            f"{case}: an unreadable blacklist must deny, never admit"
        )
        assert queue.count == 0, f"{case}: nothing may be enqueued"


class _EnvelopeLiar(Mapping):
    """A `Mapping` that tells the truth about `field_name` except on read number `lie_on`.

    `receive_bid` accepts ANY `Mapping` — that is its signature and its docstring — and it does
    not snapshot the one it is given. It reads the signing envelope out of that mapping several
    separate times: `contracts.signing.missing_signing_fields` reads it to decide the envelope
    is complete, `canonical_signing_bytes` reads it to build the bytes the signature covers, the
    door reads it to decide which key to select and which nonce to spend, `verify_signature`
    re-canonicalizes and reads it again, and `_work_item` reads it once more on the way to the
    queue. Every one of those is a separate `payload.get` call.

    A plain `dict` answers all of them identically and so can never show the difference between
    "the value that was verified" and "the value that was acted on". This can. Lying on exactly
    ONE read is the sharp instrument: lying on all of them from some point onward is caught by
    `verify_signature`, which re-canonicalizes and no longer matches, so it proves much less
    than it looks like it does.
    """

    def __init__(self, base, field_name, lie_on, lie=None):
        self._base = dict(base)
        self._field = field_name
        self._lie_on = lie_on
        self._lie = lie
        self.reads = 0

    def __getitem__(self, key):
        return self._base[key]

    def __iter__(self):
        return iter(self._base)

    def __len__(self):
        return len(self._base)

    def get(self, key, default=None):
        if key == self._field:
            self.reads += 1
            if self.reads == self._lie_on:
                return self._lie
        return self._base.get(key, default)


def test_one_signed_submission_cannot_be_admitted_twice_by_lying_on_one_envelope_read() -> None:
    """Whatever mapping it arrives in, one set of signed bytes gets in at most ONCE.

    This is the replay guarantee stated without reference to any gate, and it is the one that
    broke. The door used to bind its `nonce` from a re-read of the payload rather than from the
    reading it had just canonicalized and was about to verify. A mapping that answers truthfully
    everywhere except on that single read therefore produced a submission that was correctly
    signed, correctly canonicalized, correctly verified — and then spent the WRONG slot in the
    replay memory, because `NonceStore._key` stringifies both halves, so a `None` spends
    `("store-external-1", "None")` and leaves the real nonce untouched. Measured against the
    pre-fix door: the lying mapping was admitted AND the identical signed bytes, resubmitted
    afterwards as an ordinary `dict`, were admitted a second time. Two admissions, one nonce.

    The sweep runs over every read position rather than the one that happened to be the door's,
    so the test keeps grading the property if the number or order of the reads ever changes.
    A position at which the mapping never lies is honest by construction: it is admitted once
    and its replay is refused, which is the same assertion.
    """
    from store_agent.external import NonceStore, sign_bid

    payload = _payload()
    signature = sign_bid(payload, KEY)

    for lie_on in range(1, 9):
        store = NonceStore()
        queue = _Queue()
        liar = _EnvelopeLiar(payload, "nonce", lie_on=lie_on)

        first, _q, error = _receive(liar, signature, queue=queue, nonce_store=store)
        assert error is None, f"lie_on={lie_on}: the door must refuse, never raise: {error!r}"

        second, _q2, error2 = _receive(payload, signature, queue=queue, nonce_store=store)
        assert error2 is None, f"lie_on={lie_on}: the honest resubmission must not raise"

        admitted = [r.accepted for r in (first, second)].count(True)
        assert admitted <= 1, (
            f"lie_on={lie_on}: the same signed bytes were admitted {admitted} times. A payload "
            f"that lies on one envelope read must not be able to spend a different slot in the "
            f"replay memory than the one it signed"
        )
        assert queue.count == admitted, (
            f"lie_on={lie_on}: {queue.count} enqueue(s) for {admitted} admission(s) — the queue "
            f"and the verdict must agree"
        )
        assert not store.seen(SIGNER, "None"), (
            f"lie_on={lie_on}: the replay memory was keyed off a stringified None. That slot is "
            f"not the nonce that was signed, and it is the same slot for every payload that "
            f"plays this trick"
        )


def test_a_payload_that_lies_about_its_nonce_is_refused_before_the_replay_memory() -> None:
    """The direct reading of the same guard: the lie is caught, and it costs the honest nonce nothing.

    Read order for `nonce` is measured rather than assumed — #1 `missing_signing_fields`,
    #2 `canonical_signing_bytes`, #3 the door's own envelope guard — so read #3 is the one the
    door acts on. Refusing there is the fail-closed direction: the submission never reaches the
    queue, and the nonce it named is left unspent, so the honest submitter can still use it.
    """
    from store_agent.external import NonceStore, sign_bid

    store = NonceStore()
    payload = _payload()
    signature = sign_bid(payload, KEY)

    result, queue, error = _receive(
        _EnvelopeLiar(payload, "nonce", lie_on=3), signature, nonce_store=store
    )

    assert error is None, f"the door must refuse hostile input rather than raise: {error!r}"
    assert result.accepted is False, (
        f"a payload that lies about the nonce the door acts on must not be admitted: {result!r}"
    )
    assert queue.count == 0, f"nothing may be enqueued, saw {queue.count} call(s)"
    assert not store.seen(SIGNER, payload["nonce"]), (
        "a refused submission must not spend the nonce it named"
    )
    assert not store.seen(SIGNER, "None"), (
        "the replay memory must never be keyed off a stringified None"
    )

    honest, honest_queue, _err = _receive(payload, signature, nonce_store=store)
    assert honest.accepted is True, (
        "refusing the liar must cost the honest submitter nothing: the nonce it named was never "
        "spent, so the genuine submission of the same bytes must still be admitted"
    )
    assert honest_queue.count == 1
