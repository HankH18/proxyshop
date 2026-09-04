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

import random
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest


def _common_prefix(values: Sequence[str]) -> str:
    """The longest literal every value starts with — the shape a fail-open keys on."""
    if not values:
        return ""
    shared = values[0]
    for value in values[1:]:
        while not value.startswith(shared):
            shared = shared[:-1]
            if not shared:
                return ""
    return shared


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


def _trust_snapshot_for(payload: Any) -> dict[str, Any]:
    """The eligibility read the CALLER holds, which every gate below has to be given.

    Added with the T-233 fix, and it is load-bearing rather than cosmetic. The door used to
    mint this row itself out of the submitter's own `store_id` whenever the argument was
    omitted, which is the defect: an eligibility verdict manufactured from the thing being
    judged. Now an absent snapshot is an empty snapshot and the shared boundary refuses the
    submission `trust_snapshot_unavailable` (R12).

    Every probe in this file that has to reach a LATER gate must therefore supply it, and three
    of them are silently hollowed out without it. Measured against the fixed door, admissions
    with the argument vs without:

        T-229 body-liar sweep            6 of 8    ->   0 of 8
        T-281 nested-offer-liar sweep   70 of 79   ->   0 of 79
        T-241 identity-liar sweep       48 of 66   ->   0 of 66

    Each is shaped `if not receipt.accepted: continue`, so at zero admissions the loop asserts
    nothing at all and the test still reports green. A gate probe that cannot reach its gate is
    not a passing test, it is an absent one — which is why all three now carry an explicit
    arming assertion rather than trusting this argument to stay correct.

    Two probes that supply it are NOT in that list, and the distinction is measured, not
    assumed. T-231's window probe refuses `freshness_window_invalid` / `issued_at_stale` either
    way, because freshness is gate 4a and the boundary is gate 5; it was never hollowed. T-232's
    replay counter passes no `nonce_store` at all, so it admits 0 of 3 with the argument and 0
    of 3 without — its `admitted <= 1` is trivially true in both cases, which is a pre-existing
    weakness of that probe and not something this change caused or fixed. Both supply the
    argument anyway so neither depends on gate ORDERING staying what it is today.

    Keyed on the honest payload's `store_id` — the id the caller believes it is dealing with —
    because that is the key the shared boundary looks up, and because several payloads below
    are deliberate liars whose own answers must not be allowed to steer the fixture.
    """
    store_id = payload.get("store_id") if isinstance(payload, Mapping) else None
    if not isinstance(store_id, str) or not store_id:
        return {}
    return {store_id: {"store_id": store_id, "score": 0.9, "blacklisted": False}}


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

    admitted = 0
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
            trust_snapshot=_trust_snapshot_for(payload),
        )
        if not receipt.accepted:
            continue
        admitted += 1
        assert queue.count == 1, f"swap_after={swap_after}: one admission is one enqueue"
        enqueued = queue.items[0]["submission"]["offer"]["unit_price"]
        assert enqueued == HONEST_PRICE, (
            f"swap_after={swap_after}: the door admitted a submission priced at {HONEST_PRICE} "
            f"and enqueued one priced at {enqueued}. A receipt saying accepted=True reasons=() "
            f"is a claim about the document that was checked, and the next stage receives a "
            f"different document"
        )

    # ARMING. `if not receipt.accepted: continue` makes this whole sweep silently optional: if
    # every payload is refused, the loop asserts NOTHING and the test still reports green. That
    # is one edit away at all times — measured, dropping the `trust_snapshot=` above takes this
    # from 6 admissions of 8 to 0 of 8 with nothing turning red. The sweep has to prove it ran.
    assert admitted, (
        "not one of the 8 body-liar payloads was admitted, so every assertion in this sweep was "
        "skipped and it proved nothing. The probe has stopped reaching the gate it is about — "
        "check what is refusing before this test's subject (payload aliasing) is ever reached"
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
        trust_snapshot=_trust_snapshot_for(payload),
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
    nested_admitted = 0
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
            trust_snapshot=_trust_snapshot_for(fresh),
        )
        if not nested.accepted:
            continue
        nested_admitted += 1
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

    # ARMING — see the identical note in `test_the_body_that_was_validated_is_the_body_that_is
    # _enqueued`. Measured: dropping the `trust_snapshot=` above takes this sweep from 70
    # admissions of 79 to 0 of 79, and the test still reports green.
    assert nested_admitted, (
        "not one of the 79 nested-offer-liar payloads was admitted, so every assertion in this "
        "sweep was skipped and it proved nothing about what reaches the queue"
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

    admitted_by_field: dict[str, int] = {}
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
                trust_snapshot=_trust_snapshot_for(payload),
            )
            if not receipt.accepted:
                continue
            admitted_by_field[field] = admitted_by_field.get(field, 0) + 1
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

    # ARMING, per field rather than in total. `if not receipt.accepted: continue` makes each
    # field's slice silently optional, and a global count would let five fields carry a sixth
    # that never gets admitted once. Measured: dropping the `trust_snapshot=` above takes this
    # sweep from 48 admissions of 66 to 0 of 66 with nothing turning red.
    unexercised = [f for f in _IDENTITY_FIELDS if not admitted_by_field.get(f)]
    assert not unexercised, (
        f"no payload lying about {unexercised} was ever admitted, so this sweep asserted nothing "
        f"about {'those fields' if len(unexercised) > 1 else 'that field'}. Admissions per field: "
        f"{ {f: admitted_by_field.get(f, 0) for f in _IDENTITY_FIELDS} }. A gate refusing before "
        f"the work item is built means this test is no longer measuring the work item"
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
        # `setdefault`, not a keyword before `**kwargs`: a case that wants to pass its own
        # `trust_snapshot=` must be able to, rather than dying on a duplicate-keyword TypeError.
        # Measured: the eligibility gate sits AFTER the blacklist and window gates, so supplying
        # a row changes none of these three refusal reasons today — they stay
        # `freshness_window_invalid`, `store_blacklisted` and `signing_envelope_uncanonicalizable`
        # either way. It is here so that stays true by construction rather than by gate
        # ordering: each case has to be refused on ITS OWN hazard.
        kwargs.setdefault("trust_snapshot", _trust_snapshot_for(payload))
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
        # Measured: the window-validation gate refuses `freshness_window_invalid` BEFORE the
        # eligibility gate is reached, so this probe reports the same reasons with or without a
        # row. The row is supplied anyway so the probe never depends on that gate ordering, and
        # `setdefault` keeps a caller free to override it.
        kwargs.setdefault("trust_snapshot", _trust_snapshot_for(stale))
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
            trust_snapshot=_trust_snapshot_for(payload),
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


# xfail marker removed with the T-233 fix: an absent `trust_snapshot` is now the empty snapshot
# rather than a row minted from the submitter's own `store_id`, so omission and `{}` reach the
# same refusal and this test XPASSes — which `strict=True` would turn into a failure.
#
# justify-test-edit. The marker is the only existing test machinery this ticket touched, and
# removing it is the marker's designed lifecycle rather than a weakening — this file's own
# header says so: "once the defect is closed the test XPASSes, which a strict xfail turns into
# a failure, and whoever fixed it has to delete the marker." Would this test still be wrong if
# I reverted my change? No — it would be RIGHT, and it would FAIL, which is exactly what a
# reproduction gate is for; its two original assertions are unchanged in meaning and still
# fail against the pre-fix door. Nothing was weakened to get green: no assertion was modified
# or deleted anywhere in this change, and the two assertions added below (an arming control,
# and the `blacklist=`-only half of the same defect) both make this gate strictly harder.


def _claim(key: str, value: str, source: str = "seller_asserted") -> dict[str, Any]:
    """One claim. `source` matters: R8 admits `seller_asserted` in `bid.claims` and flags it for
    verification, but refuses it at `offer.commitments`, which only a tool hook can mint
    (`get_owner_commitments` → `owner_statement`)."""
    return {
        "key": key,
        "value": value,
        "provenance": {
            "source": source,
            "ref": f"pitch:gate-1#{key}",
            "observed_at": ISSUED_AT,
            "authority_rank": 1,
        },
    }


#: Payload shapes this property is asserted over. The property is universal — it is about an
#: ARGUMENT, not about any bid — so probing it with one payload is a hole rather than a shortcut,
#: and a measured one. Patching `_synthetic_trust_snapshot` to return `{}` only for a claimless
#: bid turns a single-payload probe green while leaving the fail-open intact for every bid a real
#: store actually submits; run against that patch, the parametrized probe below still reports the
#: defect. The shapes vary what such a key-off could plausibly key ON — how many `claims` the bid
#: carries (none, one, three), whether the offer carries `commitments`, and whether `store_id` is
#: the signer itself or a separate shopfront — while every one of them stays a VALID bid, which
#: the arming assertion below enforces so a shape can never go quiet by becoming unbuildable.
_T233_SHAPES: dict[str, Any] = {
    "an empty claims list": lambda: _payload(),
    "one seller-asserted claim": lambda: _payload(claims=[_claim("material", "merino wool")]),
    "three claims": lambda: _payload(
        claims=[
            _claim("material", "merino wool"),
            _claim("origin", "New Zealand"),
            _claim("care", "machine washable"),
        ],
    ),
    "a committed offer": lambda: _payload(
        claims=[_claim("material", "merino wool")],
        offer={
            "product_ref": "gate-prod-1",
            "unit_price": HONEST_PRICE,
            "total_price": HONEST_PRICE,
            "discount": None,
            "commitments": [_claim("returns", "60 days, free", source="owner_statement")],
            "expires_at": FAR_FUTURE,
        },
    ),
    "a shopfront store id that is not the signer": lambda: _payload(
        store_id="store-shopfront-7",
        claims=[_claim("material", "merino wool")],
    ),
}


@pytest.mark.parametrize("shape", sorted(_T233_SHAPES), ids=lambda s: s.replace(" ", "-"))
def test_omitting_the_eligibility_inputs_is_not_more_permissive_than_passing_empty_ones(
    shape: str,
) -> None:
    """Omission is the case that happens by accident, so it must not be the permissive one.

    Measured on this tree BEFORE the fix, same signed payload, same door:

    * `receive_bid(payload, ...)` with neither `trust_snapshot=` nor `blacklist=` →
      ``accepted=True, reasons=()``
    * `receive_bid(payload, ..., trust_snapshot={})` →
      ``accepted=False, reasons=('trust_snapshot_unavailable:store-external-1',)``

    The explicit empty value failed closed and the omitted one failed OPEN, which is backwards.
    The door reached `_synthetic_trust_snapshot`, which minted the single row the shared boundary
    was about to look up and filled it with ``blacklisted: False`` — a verdict the caller never
    gave, keyed on an id out of the submission being judged. That function's own docstring was
    honest about it ("it is not a source of trust, it is the absence of one written down
    honestly"), and an absent eligibility read is exactly what R12 says to deny on.

    The property: a caller who said nothing about eligibility must not get a more permissive
    answer than a caller who said "I have no eligibility data". It is a property of the
    ARGUMENT and therefore holds for every bid, which is why it is parametrized — see
    `_T233_SHAPES` for the single-payload hole that closes.

    Each case arms itself first: with a real snapshot the same bid must be ADMITTED, so a
    refusal below is the eligibility gate answering and not the bid being unbuildable.
    """
    from store_agent.external import NonceStore, receive_bid, sign_bid

    payload = _T233_SHAPES[shape]()
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

    supplied = _receive(trust_snapshot=_trust_snapshot_for(payload))
    assert supplied.accepted is True, (
        f"arming {shape!r}: with a real eligibility row this bid must be admitted, or the "
        f"refusals below prove nothing about the eligibility argument: {supplied!r}"
    )

    empty = _receive(trust_snapshot={})
    assert empty.accepted is False, (
        f"{shape}: control — an explicitly empty trust snapshot is an unavailable eligibility "
        f"read and must be refused (R12): {empty!r}"
    )

    omitted = _receive()
    assert omitted.accepted is False, (
        f"{shape}: a submission judged with NO eligibility input at all was admitted, while the "
        f"same submission judged with an explicitly empty snapshot was refused {empty.reasons!r}."
        f" Omission is the case that happens by accident, and it is the more permissive of the "
        f"two: {omitted!r}"
    )

    # The other half of the same defect: `blacklist=` was the input the minted row claimed to be
    # adapting, so a caller who passes only a blacklist must not be let through either. A
    # blacklist refuses named ids; it never establishes that a store is eligible.
    blacklist_only = _receive(blacklist=[])
    assert blacklist_only.accepted is False, (
        f"{shape}: passing only blacklist=[] admitted a submission that an empty trust snapshot "
        f"refuses. A blacklist is a narrower input than an eligibility snapshot and does not "
        f"stand in for one: {blacklist_only!r}"
    )


#: How many bids the property below draws per seed.
_DRAWS_PER_SEED = 60


def _property_seeds(pinned: int) -> tuple[int, int]:
    """One PINNED seed and one drawn fresh on every run. The property runs over both.

    Back-ported verbatim in shape from `packages/contracts/tests/test_repro_open_tickets.py`,
    where it exists for a measured reason rather than a stylistic one. The pinned seed is what
    makes a red run reproducible; the unpinned one closes the hole the pinned seed leaves, and
    that hole is what defeated THIS gate. With a single constant seed the 60 "drawn" bids are a
    constant TABLE, so any field every row of that table happens to share is a viable key for a
    fail-open: an adversarial review keyed the T-233 defect on `offer.expires_at` (every drawn
    offer expired in 2027-2999 against `now=2026`) and on a `store_id` prefix (every drawn id
    began with the literal `store-`), and this file came back byte-identical to baseline both
    times with the fail-open fully alive for a realistic bid. No enumeration of payloads closes
    that — the defence is a table the patch has not seen. Every failure message names its seed,
    so a red run from the unpinned half is reproduced by pinning the seed it printed.
    """
    return (pinned, random.SystemRandom().randrange(2**32))


#: `NOW` as a datetime, so a drawn `expires_at` is placed RELATIVE to the door's clock instead
#: of in a band chosen by hand. Pinned against the `NOW` string itself in the generator's own
#: regression test — a constant that drifted from `NOW` would quietly move every drawn offer
#: back into the far future this generator exists to leave.
NOW_DT = datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)

#: Word stock for drawn identifiers, deliberately heterogeneous. The generator this replaced
#: spelled every id with a constant literal prefix — `store-`, `auc-`, `prod-`, `nonce-`,
#: `ext-` — and a fail-open keyed on `store_id.startswith("store-")` therefore survived all 60
#: of its draws. A prefix every draw shares is a key every draw misses.
_WORDS = (
    "north", "kettle", "acme", "vega", "lumen", "orchid", "basalt", "tundra", "quill", "amber",
    "corvid", "delta", "fern", "gable", "harbor", "ingot", "juniper", "krill", "larch", "moss",
    "nimbus", "opal", "pelican", "quarry", "rowan", "sable", "thistle", "umber", "vellum", "wren",
)  # fmt: skip

#: Separators, so not even the punctuation between the words is constant. `""` is included:
#: a run of concatenated words is a perfectly ordinary store id and shares no separator at all.
_SEPARATORS = ("-", "_", ".", "", "~", "+")


def _drawn_token(rng: random.Random) -> str:
    """An identifier sharing no prefix, separator, case or length with the next one drawn."""
    words = [rng.choice(_WORDS) for _ in range(rng.randrange(1, 4))]
    if rng.random() < 0.45:
        words.insert(rng.randrange(len(words) + 1), str(rng.randrange(10**7)))
    token = rng.choice(_SEPARATORS).join(words)
    case = rng.randrange(3)
    return token.upper() if case == 1 else (token.capitalize() if case == 2 else token)


def _drawn_expires_at(rng: random.Random) -> str:
    """An offer expiry spanning REALISTIC values around `now`, not only the far future.

    This is the fix for the measured blindness, so the shape of the distribution is the point.
    The old generator drew the year uniformly from 2027-2999 against `now=2026`, which means no
    draw it could ever produce resembled a bid a store actually submits — a live auction's offer
    expires in minutes or days, not in three centuries. A fail-open keyed on
    `offer.expires_at >= "2027"` was therefore invisible to 60 of 60 draws while staying alive
    for every real bid. Most of the mass now sits inside the day; the far-future tail is kept so
    the shapes the old table covered are not LOST, only outnumbered.

    The one hard constraint: the shared boundary refuses `offer_expired` when
    `expires_at <= now`, so every draw must land strictly after `NOW_DT` or the arming assertion
    would start failing for a reason that has nothing to do with eligibility.
    """
    band = rng.random()
    if band < 0.55:  # the realistic band: this offer expires within the hour or the day
        delta = timedelta(seconds=rng.randrange(5, 86_400))
    elif band < 0.80:  # weeks to a year out
        delta = timedelta(days=rng.randrange(1, 366))
    elif band < 0.93:  # one to ten years
        delta = timedelta(days=rng.randrange(366, 3653))
    else:  # the far future the old table lived in, kept as a tail rather than as the whole thing
        delta = timedelta(days=rng.randrange(3653, 355_000))
    return (NOW_DT + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _drawn_claim(rng: random.Random, *, source: str) -> dict[str, Any]:
    """One claim whose key, value, ref and `observed_at` are all drawn.

    Deliberately NOT `_claim`: that helper stamps `observed_at=ISSUED_AT` and
    `ref="pitch:gate-1#<key>"`, both constants, and a constant inside a "randomized" bid is a
    key. `source` is held to the caller's choice because R8 admits `seller_asserted` only in
    `bid.claims` and only `owner_statement` at `offer.commitments`; a drawn source would make
    the bid unbuildable rather than harder to game.
    """
    observed = NOW_DT - timedelta(seconds=rng.randrange(0, 240))
    return {
        "key": _drawn_token(rng),
        "value": f"{_drawn_token(rng)} {rng.randrange(10**4)}",
        "provenance": {
            "source": source,
            "ref": f"{_drawn_token(rng)}:{rng.randrange(10**6)}#{_drawn_token(rng)}",
            "observed_at": observed.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "authority_rank": rng.randrange(1, 5),
        },
    }


def _randomized_bid(rng: random.Random) -> dict[str, Any]:
    """A valid, signable bid whose every caller-visible field is drawn rather than fixed.

    `signer_id`/`key_id` are held to the keyring because a bid nobody can authenticate never
    reaches the eligibility gate at all, and `schema_version` is held to `"1"` because the
    envelope has no other version. `offer.discount` stays `None`: a present discount engages
    R8's provenance rules at `offer.discount` AND T-177's depth/price consistency check, and a
    drawn one would fail the ARMING assertion for reasons unrelated to eligibility — that is an
    honest limit of this generator and is recorded here rather than left to be rediscovered.
    Everything else varies, prefixes and separators included.
    """
    unit_price = round(rng.uniform(0.5, 50_000.0), 2)
    issued = NOW_DT - timedelta(seconds=rng.randrange(0, 240))
    claims = [_drawn_claim(rng, source="seller_asserted") for _ in range(rng.randrange(0, 4))]
    commitments = [_drawn_claim(rng, source="owner_statement") for _ in range(rng.randrange(0, 2))]
    return {
        "auction_id": _drawn_token(rng),
        "store_id": _drawn_token(rng),
        "offer": {
            "product_ref": _drawn_token(rng),
            "unit_price": unit_price,
            "total_price": unit_price,
            "discount": None,
            "commitments": commitments,
            "expires_at": _drawn_expires_at(rng),
        },
        "claims": claims,
        "message": " ".join(_drawn_token(rng) for _ in range(rng.randrange(1, 12))),
        "agent_version": f"{_drawn_token(rng)}/{rng.randrange(99)}.{rng.randrange(99)}",
        "schema_version": "1",
        "signer_id": SIGNER,
        "key_id": KEY_ID,
        "issued_at": issued.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nonce": _drawn_token(rng),
    }


def test_the_absent_eligibility_argument_is_indistinguishable_from_the_empty_one_for_any_bid():
    """T-233 stated as the property it actually is, over bids nobody chose.

    `_T233_SHAPES` above hardens the probe against a fix that keys its fail-open on `claims`,
    which is the one an author reaches for first. It does NOT harden it against keying on
    anything else, and that hole is measured rather than theoretical: an adversarial review of
    this very change wrote four patches that keep the fail-open alive for realistic bids and
    still take the five shapes to `5 passed` — keyed on `offer.expires_at >= "2100"` (every
    shape uses `FAR_FUTURE`), on a `store_id` allowlist, on `auction_id == "auc-gate-1"`, and on
    `nonce == "nonce-gate-0001"`. Every shape is built from one `_payload()` fixture, so every
    field they share is a viable key, and no enumeration of shapes can close that — adding a
    sixth shape just moves the key.

    The durable form is not more payloads, it is the property itself: `trust_snapshot=None` must
    be INDISTINGUISHABLE from `trust_snapshot={}` — same verdict, same reasons — for a bid the
    test author did not choose. A fail-open that keys on any payload field is then caught by the
    draws that miss its key, and one that keys on nothing is caught by all of them.

    Reasons are compared, not just `accepted`. Two refusals for different reasons are two
    different behaviours, and "absent" collapsing to some OTHER refusal would be a new defect
    wearing this one's passing grade.
    """
    from store_agent.external import NonceStore, receive_bid, sign_bid

    def _draw_once(rng: random.Random, seed: int, draw: int) -> str:
        payload = _randomized_bid(rng)
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

        supplied = _receive(trust_snapshot=_trust_snapshot_for(payload))
        assert supplied.accepted is True, (
            f"seed {seed} draw {draw}: with a real eligibility row this randomized bid must be "
            f"admitted, or the comparison below is between two refusals that have nothing to do "
            f"with eligibility. payload={payload!r} receipt={supplied!r}"
        )

        omitted = _receive()
        empty = _receive(trust_snapshot={})
        assert (omitted.accepted, tuple(omitted.reasons or ())) == (
            empty.accepted,
            tuple(empty.reasons or ()),
        ), (
            f"seed {seed} draw {draw}: omitting trust_snapshot gave "
            f"{(omitted.accepted, tuple(omitted.reasons or ()))} where passing an explicit empty "
            f"snapshot gave {(empty.accepted, tuple(empty.reasons or ()))}. The absent argument "
            f"must be the empty one for EVERY bid, not for the ones this file happens to name — "
            f"a default that reads any part of the submission to decide how permissive to be is "
            f"the T-233 defect with a different key. Reproduce with random.Random({seed}) and "
            f"take draw {draw}. payload={payload!r}"
        )
        assert omitted.accepted is False, (
            f"seed {seed} draw {draw}: a submission judged with no eligibility input at all was "
            f"admitted. payload={payload!r} receipt={omitted!r}"
        )
        return repr(payload)

    armed = 0
    distinct: set[str] = set()
    for seed in _property_seeds(20260904):
        rng = random.Random(seed)
        for draw in range(_DRAWS_PER_SEED):
            distinct.add(_draw_once(rng, seed, draw))
            armed += 1

    # A LITERAL 120, never `2 * _DRAWS_PER_SEED`: a guard written in terms of the constant it is
    # guarding compares the constant against itself, which is the same tautology as a loop that
    # iterates zero cases and reports success.
    assert _DRAWS_PER_SEED >= 60, (
        f"_DRAWS_PER_SEED shrank to {_DRAWS_PER_SEED}; this property is sized at 60 draws per "
        f"seed and the guards below are written against a literal 120"
    )
    assert armed == 120, (
        f"only {armed} of 120 draws were built, armed and compared. A loop that silently "
        f"iterates fewer cases than it claims is how three sweeps in this repo went QUIET rather "
        f"than red (6->0 of 8, 70->0 of 79, 48->0 of 66)"
    )
    assert len(distinct) == 120, (
        f"the generator produced {len(distinct)} distinct bids across 120 draws; a property "
        f"asserted over one repeated bid is a single-payload probe wearing a loop"
    )


def test_the_t233_generator_is_not_a_constant_far_future_table() -> None:
    """The generator's own gate: the three properties that make the property above able to see.

    Every assertion here is a regression pin on a MEASURED blindness, not a style preference.
    The generator this replaced drew `offer.expires_at` from `randrange(2027, 3000)` against
    `now=2026` and spelled every id with a constant literal prefix, and against that generator a
    fail-open keyed on `offer.expires_at` and one keyed on `store_id.startswith("store-")` both
    came back byte-identical to baseline — the gate went quiet rather than red while the T-233
    defect was live for a realistic bid. Amendment 17 names `offer.expires_at` as one of the four
    gaming keys that sank this gate's predecessor.

    Revert any part of that widening and this test fails, which is the point: the next rewrite
    cannot re-open the hole by tidying the generator back into a constant table.
    """
    # The seeds. `isinstance(drawn, int)` alone proves nothing — it is satisfied by a
    # `_property_seeds` de-randomized to a constant pair, which is the one property its docstring
    # calls load-bearing. Two calls must disagree on the drawn half; collision odds are 2**-32.
    first_pinned, first_drawn = _property_seeds(20260904)
    second_pinned, second_drawn = _property_seeds(20260904)
    assert first_pinned == second_pinned == 20260904, (
        f"_property_seeds must return the pinned seed it was given, got "
        f"{(first_pinned, second_pinned)!r}"
    )
    assert first_drawn != second_drawn, (
        f"_property_seeds returned the same 'drawn' seed twice ({first_drawn}), so the property "
        f"is running over a constant table after all — which is precisely the hole the unpinned "
        f"seed exists to close, and two measured fail-open keys walk through it"
    )

    # `NOW_DT` must BE `NOW`, or every "realistic" band below is measured against a clock the
    # door does not use and the widening is cosmetic.
    assert NOW_DT.strftime("%Y-%m-%dT%H:%M:%SZ") == NOW, (
        f"NOW_DT ({NOW_DT!r}) has drifted from NOW ({NOW!r}); the drawn expiries are then placed "
        f"relative to the wrong clock"
    )

    rng = random.Random(20260904)
    bids = [_randomized_bid(rng) for _ in range(_DRAWS_PER_SEED)]
    assert len(bids) == _DRAWS_PER_SEED >= 60, "the sample itself must be armed"

    expiries = [b["offer"]["expires_at"] for b in bids]
    assert all(e > NOW for e in expiries), (
        f"an offer expiring at or before now is refused `offer_expired` by the shared boundary, "
        f"so it would break the property's ARMING assertion rather than harden it: "
        f"{sorted(e for e in expiries if e <= NOW)[:5]}"
    )
    # The blindness itself, stated as a number. The old generator scored 0 here for 60 of 60.
    within_a_day = [e for e in expiries if e < "2026-01-02"]
    assert len(within_a_day) >= 15, (
        f"only {len(within_a_day)} of {len(expiries)} drawn offers expire within a day of "
        f"now={NOW}. The generator this replaced scored ZERO — every draw expired in 2027-2999 — "
        f"and a fail-open keyed on `offer.expires_at` was invisible to all 60 of them while "
        f"staying alive for the near-term expiry a live auction actually carries"
    )
    assert len({e[:4] for e in expiries}) >= 5, (
        f"the drawn expiry years are {sorted({e[:4] for e in expiries})}; a distribution narrow "
        f"enough to enumerate is a distribution a patch can key on"
    )

    # No constant prefix on any drawn identifier. `store-`, `auc-`, `prod-`, `nonce-` and `ext-`
    # were all constants of the old generator, and a prefix every draw shares is a key every
    # draw misses.
    for field, values in (
        ("store_id", [b["store_id"] for b in bids]),
        ("auction_id", [b["auction_id"] for b in bids]),
        ("offer.product_ref", [b["offer"]["product_ref"] for b in bids]),
        ("nonce", [b["nonce"] for b in bids]),
        ("agent_version", [b["agent_version"] for b in bids]),
        ("message", [b["message"] for b in bids]),
    ):
        shared = _common_prefix(values)
        assert len(shared) <= 1, (
            f"every drawn `{field}` starts with {shared!r}. That literal is a key: a fix that "
            f"keeps the fail-open alive for everything NOT matching it passes all "
            f"{len(values)} draws — which is exactly how `store-` defeated this gate"
        )
        assert len(set(values)) >= len(values) - 5, (
            f"`{field}` took only {len(set(values))} distinct values across {len(values)} draws; "
            f"a field that repeats is a field a patch can enumerate"
        )

    # `issued_at` is a timestamp, so a shared prefix is inevitable and meaningless — what matters
    # is that it is not the single frozen `ISSUED_AT` constant the old generator stamped on every
    # draw, which was itself a viable key.
    issued = [b["issued_at"] for b in bids]
    assert len(set(issued)) >= 20, (
        f"`issued_at` took only {len(set(issued))} distinct values across {len(issued)} draws "
        f"(the generator this replaced stamped one constant on all of them): {sorted(set(issued))}"
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
