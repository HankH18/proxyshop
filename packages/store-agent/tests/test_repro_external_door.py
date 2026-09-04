"""Reproduction gates for the Tier-2 external door (`store_agent.external.door`).

Every test here asserts the behaviour that SHOULD hold and therefore fails against the tree as
it stands. Each carries `xfail(strict=True)` so an ordinary run reports `xfailed` and stays
green, the ticket's gate (`pytest <file> -q --runxfail -k <name>`) reports a real failure, and
the marker cannot survive the fix: once the defect is closed the test XPASSes, which a strict
xfail turns into a failure, and whoever fixed it has to delete the marker.

Seven tickets, one root shape behind three of them: `receive_bid` accepts any `Mapping` and
re-reads it instead of snapshotting it once at entry.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

import pytest

SIGNER = "store-external-1"
KEY_ID = "key-2026-01"
KEY = "gate-secret-0001"

ISSUED_AT = "2026-01-01T00:00:00Z"
NOW = "2026-01-01T00:00:05Z"
DEADLINE = "2026-01-01T00:05:00Z"
FAR_FUTURE = "2999-01-01T00:00:00Z"

#: A signed submission issued six years before `NOW`. The default freshness window refuses it,
#: which is what makes it the right probe for a window that has stopped refusing anything.
LONG_STALE = "2020-01-01T00:00:00Z"

HONEST_PRICE = 89.0
ATTACKER_PRICE = 1.0


def _keyring() -> dict[str, dict[str, str]]:
    return {SIGNER: {KEY_ID: KEY}}


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "auction_id": "auc-gate-1",
        "store_id": SIGNER,
        "offer": {
            "product_ref": "gate-prod-1",
            "unit_price": HONEST_PRICE,
            "total_price": HONEST_PRICE,
            "discount": None,
            "commitments": [],
            "expires_at": FAR_FUTURE,
        },
        "claims": [],
        "message": "Warmest merino mid-layer on the market.",
        "agent_version": "ext-0.1.0",
        "schema_version": "1",
        "signer_id": SIGNER,
        "key_id": KEY_ID,
        "issued_at": ISSUED_AT,
        "nonce": "nonce-gate-0001",
    }
    payload.update(overrides)
    return payload


class _Queue:
    """Records what the door hands to the next stage."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    def __call__(self, item: Any) -> None:
        self.items.append(item)

    @property
    def count(self) -> int:
        return len(self.items)


class _FieldLiar(Mapping):
    """A `Mapping` that answers truthfully about one key until read `swap_after`, then lies.

    `receive_bid` accepts ANY `Mapping` — that is its signature and its documented contract — and
    does not snapshot the one it is handed. The envelope is therefore read several separate
    times, and a mapping that changes its answer between two of those reads makes the value that
    was VERIFIED and the value that was ACTED ON two different things. A plain `dict` answers
    every read identically and so can never show the difference.
    """

    def __init__(self, base: Mapping[str, Any], field: str, swap_after: int, lie: Any) -> None:
        self._base = dict(base)
        self._field = field
        self._swap_after = swap_after
        self._lie = lie
        self.reads = 0

    def __getitem__(self, key: str) -> Any:
        return self._base[key]

    def __iter__(self):
        return iter(self._base)

    def __len__(self) -> int:
        return len(self._base)

    def get(self, key: str, default: Any = None) -> Any:
        if key == self._field:
            self.reads += 1
            if self.reads >= self._swap_after:
                return self._lie
        return self._base.get(key, default)


class _BodyLiar(Mapping):
    """A `Mapping` whose OFFER changes after the Nth whole-mapping copy.

    `receive_bid` builds `dict(payload)` twice — once for the shared Tier-2 boundary that decides,
    and once inside `_work_item` for the thing that is queued. `dict(m)` reads a mapping through
    `__iter__`/`__getitem__` rather than `.get`, so this is the read path the body travels, and
    counting copies is what separates "the body that was validated" from "the body that was
    enqueued".
    """

    def __init__(self, base: Mapping[str, Any], swap_after: int) -> None:
        self._base = dict(base)
        self._swap_after = swap_after
        self.copies = 0

    def __getitem__(self, key: str) -> Any:
        if key == "offer" and self.copies >= self._swap_after:
            return {
                **self._base["offer"],
                "unit_price": ATTACKER_PRICE,
                "total_price": ATTACKER_PRICE,
            }
        return self._base[key]

    def __iter__(self):
        self.copies += 1
        return iter(self._base)

    def __len__(self) -> int:
        return len(self._base)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


# =============================================================================================
# T-229 — the validated body is not the enqueued body
# =============================================================================================


# xfail marker removed with the T-229 fix: `receive_bid` now snapshots the caller's mapping
# once (`_snapshot`, door.py) and every gate, the signature check and the work item read that
# snapshot, so this test XPASSes and `strict=True` would fail the run if the marker stayed.
def test_the_body_that_was_validated_is_the_body_that_is_enqueued() -> None:
    """A correctly signed bid for 89.00 must never be enqueued as 1.00.

    `receive_bid` reads the submission out of the caller's mapping several separate times: the
    canonicalizer reads it, the signature check re-canonicalizes and reads it again, the shared
    Tier-2 boundary is handed `dict(payload)`, and then `_work_item` builds a SECOND `dict(payload)`
    for the queue. Nothing binds those two copies together, so a mapping that answers differently
    on the later read has the boundary approve one document and the queue receive another.

    Measured on this tree at `swap_after` 4 and 5: `accepted=True`, `reasons=()`, and the queued
    work item's `submission.offer.unit_price` is 1.0 where the signed and validated one is 89.0.
    The receipt is indistinguishable from an honest admission, and the next stage — claim
    extraction — has no way to know it is not looking at what the door checked.

    The property is stated without reference to any gate: whatever mapping a submission arrives
    in, what reaches the queue is what was validated. The fix named by the reporting lane is one
    line — `payload = dict(payload)` at entry, inside a try/except because `receive_bid` is
    documented never to raise.
    """
    from store_agent.external import NonceStore, receive_bid, sign_bid

    payload = _payload()
    signature = sign_bid(payload, KEY)

    for swap_after in range(1, 9):
        queue = _Queue()
        receipt = receive_bid(
            _BodyLiar(payload, swap_after),
            signature,
            _keyring(),
            queue=queue,
            nonce_store=NonceStore(),
            now=NOW,
            auction_deadline=DEADLINE,
        )
        if not receipt.accepted:
            continue
        assert queue.count == 1, f"swap_after={swap_after}: one admission is one enqueue"
        enqueued = queue.items[0]["submission"]["offer"]["unit_price"]
        assert enqueued == HONEST_PRICE, (
            f"swap_after={swap_after}: the door admitted a submission priced at {HONEST_PRICE} "
            f"and enqueued one priced at {enqueued}. A receipt saying accepted=True reasons=() "
            f"is a claim about the document that was checked, and the next stage receives a "
            f"different document"
        )


class _NestedOfferLiar(Mapping):
    """A hostile `Mapping` sitting at `payload["offer"]` rather than at the root.

    `_BodyLiar` above lies at the TOP level, which a shallow `dict(payload)` is enough to defeat.
    This is the same trick one level down, and it is what a shallow snapshot does NOT defeat: the
    top-level copy keeps every value by reference, so the door validates this object and then
    hands the identical object to the queue.
    """

    def __init__(self, base: Mapping[str, Any], lie_after: int) -> None:
        self._base = dict(base)
        self._lie_after = lie_after
        self.reads = 0

    def _value(self, key: str) -> Any:
        if key in ("unit_price", "total_price") and self.reads >= self._lie_after:
            return ATTACKER_PRICE
        return self._base[key]

    def __getitem__(self, key: str) -> Any:
        self.reads += 1
        return self._value(key)

    def __iter__(self):
        return iter(self._base)

    def __len__(self) -> int:
        return len(self._base)

    def get(self, key: str, default: Any = None) -> Any:
        self.reads += 1
        try:
            return self._value(key)
        except KeyError:
            return default


def test_the_snapshot_is_deep_so_a_nested_value_cannot_be_swapped_or_mutated() -> None:
    """The T-229 property holds at every level, not only at the root of the submission.

    T-229's fix snapshots the caller's mapping at entry. `dict(payload)` — the fix the ticket's
    own reproduction note suggested — is SHALLOW: it copies the top level and keeps every value
    by reference, so `offer` and `claims` stay the caller's own objects and remain shared between
    the document the boundary validated and the document `_work_item` enqueues. The defect T-229
    describes then survives one level down, which is where a bid's price actually lives.

    Two ways in, both measured against a shallow snapshot:

    1. **No hostile machinery at all.** A submitter passing an ordinary nested `dict` keeps its
       reference and mutates `offer["unit_price"]` AFTER `receive_bid` has returned
       `accepted=True`. The queued work item changes underneath the verification worker, which
       has not dequeued it yet.
    2. **T-229's own sentence, one level down.** A `Mapping` at `payload["offer"]` that answers
       honestly while the door reads it and lies afterwards produced `accepted=True, reasons=()`
       with the door validating 89.00 and the queue receiving 1.00 — and the sweep above stayed
       green throughout, because its liar only lies at the top level.

    A nested non-`dict` `Mapping` was also being enqueued BY IDENTITY, so the worker would run
    the submitter's code on dequeue.
    """
    from store_agent.external import NonceStore, receive_bid, sign_bid

    # 1. aliasing: what is queued must not be the caller's object
    payload = _payload()
    signature = sign_bid(payload, KEY)
    queue = _Queue()
    receipt = receive_bid(
        payload,
        signature,
        _keyring(),
        queue=queue,
        nonce_store=NonceStore(),
        now=NOW,
        auction_deadline=DEADLINE,
    )
    assert receipt.accepted is True, f"control: this bid must be admitted: {receipt!r}"
    assert queue.count == 1
    queued_offer = queue.items[0]["submission"]["offer"]
    assert queued_offer is not payload["offer"], (
        "the queued work item shares the caller's nested offer object, so the submitter can "
        "still change the price after the door has admitted it"
    )
    payload["offer"]["unit_price"] = ATTACKER_PRICE
    assert queued_offer["unit_price"] == HONEST_PRICE, (
        f"the submitter mutated its own offer dict after receive_bid returned accepted=True and "
        f"the queued work item now reads {queued_offer['unit_price']}. The door validated "
        f"{HONEST_PRICE}"
    )

    # 2. the T-229 threat model, nested
    for lie_after in range(1, 80):
        fresh = _payload()
        fresh_signature = sign_bid(fresh, KEY)
        fresh["offer"] = _NestedOfferLiar(fresh["offer"], lie_after)
        nested_queue = _Queue()
        nested = receive_bid(
            fresh,
            fresh_signature,
            _keyring(),
            queue=nested_queue,
            nonce_store=NonceStore(),
            now=NOW,
            auction_deadline=DEADLINE,
        )
        if not nested.accepted:
            continue
        assert nested_queue.count == 1
        enqueued = nested_queue.items[0]["submission"]["offer"]
        assert isinstance(enqueued, dict), (
            f"lie_after={lie_after}: the work item carries the submitter's own Mapping object, "
            f"not a copy — the verification worker runs its code on dequeue"
        )
        assert enqueued["unit_price"] == HONEST_PRICE, (
            f"lie_after={lie_after}: the door admitted a submission it validated at "
            f"{HONEST_PRICE} and enqueued one priced at {enqueued['unit_price']}. This is T-229 "
            f"one level down, where the price actually lives"
        )


# =============================================================================================
# T-241 — the same defect widened: all six identity fields, re-read after the decision
# =============================================================================================

_IDENTITY_FIELDS = ("auction_id", "store_id", "signer_id", "key_id", "issued_at", "nonce")


# xfail marker removed with the T-241 fix: `_work_item` now takes the door's own snapshot
# (typed `dict`, not `Mapping`) and its six identity fields and its queued body are two views of
# that one dict, so this test XPASSes and `strict=True` would fail the run if the marker stayed.
def test_every_identity_field_on_the_work_item_is_the_one_that_was_signed() -> None:
    """The work item's six identity fields must be the values the signature covered.

    `_work_item` (door.py:191-206) reads `auction_id`, `store_id`, `signer_id`, `key_id`,
    `issued_at` and `nonce` off the payload a second time, AFTER every gate has passed. T-228
    hardened the three the door itself acts on; the six the work item carries were left as a
    later, separately-unvalidated read.

    The sharp part is the nonce. `payload_hash` covers only the bid BODY, so for `issued_at`,
    `nonce`, `signer_id` and `key_id` the enqueued item is internally consistent — a downstream
    worker that re-hashes the submission cannot detect the swap. The door spends the honest nonce
    in its replay memory and enqueues the attacker's nonce as the documented at-least-once
    IDEMPOTENCY KEY, so the replay memory and the downstream dedup key disagree about which
    submission this is.

    Measured on this tree: at `swap_after=6` (5 for `auction_id`) every one of the six is
    admitted with the attacker's value on the work item and `accepted=True, reasons=()` on the
    receipt.
    """
    from store_agent.external import NonceStore, receive_bid, sign_bid

    payload = _payload()
    signature = sign_bid(payload, KEY)

    for field in _IDENTITY_FIELDS:
        lie = f"attacker-{field}"
        for swap_after in range(1, 12):
            queue = _Queue()
            store = NonceStore()
            receipt = receive_bid(
                _FieldLiar(payload, field, swap_after, lie),
                signature,
                _keyring(),
                queue=queue,
                nonce_store=store,
                now=NOW,
                auction_deadline=DEADLINE,
            )
            if not receipt.accepted:
                continue
            assert queue.count == 1
            item = queue.items[0]
            assert item[field] == payload[field], (
                f"{field} swap_after={swap_after}: the door admitted a submission signed with "
                f"{field}={payload[field]!r} and enqueued a work item naming {item[field]!r}. "
                f"The replay memory holds {payload['nonce']!r}"
                f" (seen={store.seen(SIGNER, payload['nonce'])}) while the work item's "
                f"idempotency key is {item['nonce']!r} — the two disagree about which submission "
                f"this is, and payload_hash covers only the body so nothing downstream can tell"
            )


# =============================================================================================
# T-230 — "Never raises", and three inputs that make it raise
# =============================================================================================


# xfail marker removed with the T-230 fix: the overflowing window is refused by
# `_freshness_window`, `_blacklisted` fails closed on any unreadable blacklist, `_refuse` no
# longer re-reads the payload it is refusing, and `receive_bid` is now a total wrapper around
# `_receive_bid`, so this test XPASSes and `strict=True` would fail the run if the marker stayed.
def test_the_door_never_raises_on_the_three_inputs_that_make_it_raise() -> None:
    """This is the signed external-bid entry point; an exception here is a 500, not a refusal.

    Three inputs, all measured against a fully valid signed payload on this tree:

    1. ``freshness_window_seconds=10**400`` — `float()` raises `OverflowError`, and the handler
       at door.py:305-308 catches `(TypeError, ValueError)` only.
    2. A blacklist whose ``__iter__`` raises `ValueError` — `_blacklisted` at door.py:131-134
       catches `TypeError` only, so the exception propagates out of an eligibility check that is
       otherwise carefully fail-closed.
    3. A `Mapping` whose ``.get`` raises. This one is structural rather than a missing `except`:
       `_refuse` (door.py:106-107) RE-READS the very payload it is refusing, to fill `signer_id`
       and `nonce` into the rejection receipt. So the error path is not safe on exactly the
       hostile input the error path exists for, and no amount of hardening in the happy path
       fixes it.

    The suite already grades "the door refuses hostile input rather than raising" for nine other
    shapes (`test_the_door_refuses_hostile_input_rather_than_raising`). These three are the same
    property, on inputs nothing currently covers.
    """
    from store_agent.external import NonceStore, receive_bid, sign_bid

    payload = _payload()
    signature = sign_bid(payload, KEY)

    def _call(**kwargs: Any) -> Any:
        return receive_bid(
            kwargs.pop("payload", payload),
            signature,
            _keyring(),
            queue=_Queue(),
            nonce_store=NonceStore(),
            now=NOW,
            auction_deadline=DEADLINE,
            **kwargs,
        )

    class _UnreadableBlacklist:
        def __iter__(self):
            raise ValueError("the eligibility feed answered with half-decoded JSON")

    class _ExplodingMapping(Mapping):
        """A payload whose reads start failing partway through, error path included."""

        def __init__(self, base: Mapping[str, Any], after: int) -> None:
            self._base = dict(base)
            self._after = after
            self.reads = 0

        def __getitem__(self, key: str) -> Any:
            return self._base[key]

        def __iter__(self):
            return iter(self._base)

        def __len__(self) -> int:
            return len(self._base)

        def get(self, key: str, default: Any = None) -> Any:
            self.reads += 1
            if self.reads > self._after:
                raise ValueError("hostile mapping")
            return self._base.get(key, default)

    cases: list[tuple[str, Any]] = [
        ("an overflowing freshness window", lambda: _call(freshness_window_seconds=10**400)),
        ("a blacklist that cannot be read", lambda: _call(blacklist=_UnreadableBlacklist())),
        (
            "a payload whose .get raises, refusal path included",
            lambda: _call(payload=_ExplodingMapping(payload, 3)),
        ),
    ]
    for case, call in cases:
        try:
            receipt = call()
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"{case}: receive_bid is documented 'Never raises' and this is the anonymous "
                f"external entry point, so {type(exc).__name__}: {exc} is a 500 handed to an "
                f"unauthenticated submitter rather than a refusal"
            ) from exc
        assert receipt.accepted is not True, f"{case}: must refuse, not admit"


# =============================================================================================
# T-231 — a non-finite freshness window makes the replay window unbounded
# =============================================================================================


# xfail marker removed with the T-231 fix: a `freshness_window_seconds` that is not a finite,
# non-negative number now refuses the submission instead of passing `float()` intact, so this
# test XPASSes and `strict=True` would fail the run if the marker stayed.
def test_a_non_finite_freshness_window_does_not_disable_the_freshness_gates() -> None:
    """A six-year-stale bid must stay refused whatever the window says.

    The two freshness comparisons are ``age > window`` and ``-age > window``. `float('nan')`
    passes the `(TypeError, ValueError)` guard intact, and every comparison against NaN evaluates
    False — so both gates pass and the door's replay window becomes unbounded. `inf` does the
    same thing honestly. A negative window is the third non-sensical value and is the one that
    happens to behave, because `age > -1` is True.

    Measured on this tree: the same signed bid, issued 2020-01-01 and judged at 2026-01-01, is
    refused `issued_at_stale` at the default window and ACCEPTED with either `nan` or `inf`.

    Not payload-reachable today — this is a caller-supplied policy number — but it is the one
    knob that makes the replay window infinite, and a YAML or env config value is exactly how a
    NaN arrives. The fix is to reject non-finite and negative windows explicitly rather than
    letting `float()` be the whole validation.
    """
    from store_agent.external import NonceStore, receive_bid, sign_bid

    stale = _payload(issued_at=LONG_STALE)
    signature = sign_bid(stale, KEY)

    def _receive(**kwargs: Any) -> Any:
        return receive_bid(
            stale,
            signature,
            _keyring(),
            queue=_Queue(),
            nonce_store=NonceStore(),
            now=NOW,
            auction_deadline=FAR_FUTURE,
            **kwargs,
        )

    control = _receive()
    assert control.accepted is False, (
        f"control: a six-year-old submission must be refused at the default window, or nothing "
        f"below proves anything: {control!r}"
    )

    for label, window in (("nan", float("nan")), ("inf", float("inf"))):
        receipt = _receive(freshness_window_seconds=window)
        assert receipt.accepted is False, (
            f"freshness_window_seconds={label} admitted a six-year-stale submission: a window "
            f"that is not a finite non-negative number is not a window, and accepting it turns "
            f"the replay defence off without saying so ({receipt!r})"
        )


# =============================================================================================
# T-232 — the default nonce store silently disables replay protection
# =============================================================================================


# xfail marker removed with the T-232 fix: an absent `nonce_store` is now refused
# `replay_memory_unavailable` instead of being given a throwaway store that remembers nothing,
# so this test XPASSes and `strict=True` would fail the run if the marker stayed.
def test_a_door_with_no_injected_nonce_store_does_not_admit_the_same_bid_twice() -> None:
    """Replay defence must not be something a caller can switch off by forgetting an argument.

    `receive_bid` builds `NonceStore()` when none is passed, and that store dies with the call —
    so nothing is remembered between submissions. Measured on this tree: the identical signed
    bid, submitted three times with no `nonce_store=` argument, was accepted all three times and
    enqueued three times.

    It is documented in the docstring, and that is not sufficient: the receipt of a replayed bid
    is byte-identical to the receipt of a real admission, so the failure is invisible from the
    outside. This is the one gate whose absence cannot be noticed by watching the door work.

    The correct behaviour is either of the two fixes the ticket names — make `nonce_store`
    required, or fail closed when it is absent — and both make this test pass: a second
    submission of the same signed bytes must not be admitted.
    """
    from store_agent.external import receive_bid, sign_bid

    payload = _payload()
    signature = sign_bid(payload, KEY)

    admitted = 0
    enqueued = 0
    for _ in range(3):
        queue = _Queue()
        receipt = receive_bid(
            payload,
            signature,
            _keyring(),
            queue=queue,
            now=NOW,
            auction_deadline=DEADLINE,
        )
        admitted += 1 if receipt.accepted else 0
        enqueued += queue.count

    assert admitted <= 1, (
        f"the identical signed bid was admitted {admitted} times with no nonce_store= argument. "
        f"A door whose replay memory is created per call has no replay memory, and the receipt "
        f"of a replay is indistinguishable from a real acceptance"
    )
    assert enqueued <= 1, f"and it reached the verification queue {enqueued} times"


# =============================================================================================
# T-233 — the omitted eligibility argument is more permissive than the empty one
# =============================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-233: with neither trust_snapshot= nor blacklist=, _synthetic_trust_snapshot mints a "
        "row asserting the store is not blacklisted, so omission fails OPEN while an explicit "
        "empty snapshot fails closed; remove this marker with the fix"
    ),
)
def test_omitting_the_eligibility_inputs_is_not_more_permissive_than_passing_empty_ones() -> None:
    """Omission is the case that happens by accident, so it must not be the permissive one.

    Measured on this tree, same signed payload, same door:

    * `receive_bid(payload, ...)` with neither `trust_snapshot=` nor `blacklist=` →
      ``accepted=True, reasons=()``
    * `receive_bid(payload, ..., trust_snapshot={})` →
      ``accepted=False, reasons=('trust_snapshot_unavailable:store-external-1',)``

    The explicit empty value fails closed and the omitted one fails open, which is backwards. The
    door reaches `_synthetic_trust_snapshot`, which mints the single row the shared boundary is
    about to look up and fills it with ``blacklisted: False`` — a verdict the caller never gave.
    The docstring is honest about this ("it is not a source of trust, it is the absence of one
    written down honestly"), and an absent eligibility read is exactly what R12 says to deny on.

    The property: a caller who said nothing about eligibility must not get a more permissive
    answer than a caller who said "I have no eligibility data".
    """
    from store_agent.external import NonceStore, receive_bid, sign_bid

    payload = _payload()
    signature = sign_bid(payload, KEY)

    def _receive(**kwargs: Any) -> Any:
        return receive_bid(
            payload,
            signature,
            _keyring(),
            queue=_Queue(),
            nonce_store=NonceStore(),
            now=NOW,
            auction_deadline=DEADLINE,
            **kwargs,
        )

    empty = _receive(trust_snapshot={})
    assert empty.accepted is False, (
        f"control: an explicitly empty trust snapshot is an unavailable eligibility read and "
        f"must be refused (R12): {empty!r}"
    )

    omitted = _receive()
    assert omitted.accepted is False, (
        f"a submission judged with NO eligibility input at all was admitted, while the same "
        f"submission judged with an explicitly empty snapshot was refused "
        f"{empty.reasons!r}. Omission is the case that happens by accident, and it is the more "
        f"permissive of the two: {omitted!r}"
    )


# =============================================================================================
# T-234 — NonceStore.consume is a check-then-set with a function call in the gap
# =============================================================================================


# xfail marker removed with the T-234 fix: `NonceStore.consume` now parses the retention
# BEFORE the critical section and does the membership test and the store under one lock, so
# this test XPASSes and `strict=True` would fail the run if the marker stayed.
def test_only_one_of_two_racing_callers_can_spend_the_same_nonce(monkeypatch) -> None:
    """ "Spend this nonce" must be atomic, or it is not a replay defence.

    ``consume`` reads::

        key = self._key(signer_id, nonce)
        if key in self._consumed:      # the check
            return False
        self._consumed[key] = parse_timestamp(retain_until)   # ... and only now the set

    `parse_timestamp` is a full function call sitting in the gap between the two, so the window
    is not a single bytecode. The reporting lane measured it as not reproducible at the default
    5ms GIL switch interval (0/300 direct trials) and reproducible 25/300 at
    ``sys.setswitchinterval(1e-6)`` — a real defect currently masked by the scheduler, which
    goes live the moment there is a concurrent HTTP caller.

    This test does not gamble on the scheduler. It parks the FIRST caller inside the gap the
    ticket names — by making that one call to `parse_timestamp` wait — and then runs the second
    caller while the first is parked. Both fixes the defect admits pass it: storing the key
    before parsing makes the second caller see it, and a lock makes the second caller wait (the
    park is bounded, so a lock resolves rather than deadlocks).

    The cross-process half of the ticket is NOT graded here and remains open: `app.bid_nonces`
    (db/migrations/0003_*.sql, UNIQUE (signer_id, nonce)) is referenced only by a docstring and
    two schema tests, so across two uvicorn workers there is no replay defence at all, atomic or
    not.
    """
    from store_agent.external import NonceStore
    from store_agent.external import nonces as nonces_module

    real_parse = nonces_module.parse_timestamp
    reached_the_gap = threading.Event()
    second_caller_done = threading.Event()
    entries = {"count": 0}
    counter_lock = threading.Lock()

    def parked_parse(value: Any) -> Any:
        with counter_lock:
            entries["count"] += 1
            first = entries["count"] == 1
        if first:
            reached_the_gap.set()
            # Bounded, so a lock-based fix resolves here instead of deadlocking.
            second_caller_done.wait(2.0)
        return real_parse(value)

    monkeypatch.setattr(nonces_module, "parse_timestamp", parked_parse)

    store = NonceStore()
    results: list[bool] = []

    def first_caller() -> None:
        results.append(store.consume(SIGNER, "nonce-race-0001", DEADLINE))

    thread = threading.Thread(target=first_caller, name="first-caller")
    thread.start()
    assert reached_the_gap.wait(5.0), (
        "the first consume never reached the gap between the membership test and the store, so "
        "this test measured nothing"
    )

    second = store.consume(SIGNER, "nonce-race-0001", DEADLINE)
    second_caller_done.set()
    thread.join(5.0)
    assert not thread.is_alive(), "the first consume never returned"

    results.append(second)
    assert results.count(True) == 1, (
        f"both callers were told they spent the same (signer_id, nonce): {results}. `consume` "
        f"is the replay defence, and a check-then-set with a function call between the two "
        f"halves is not one — two concurrent submissions of the identical signed bytes are both "
        f"admitted"
    )
    assert len(store) == 1, "and the store holds one entry either way, so the count cannot show it"
