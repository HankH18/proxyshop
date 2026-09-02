"""T-011: the D16 hash chain -- canonicalisation, sealing, verification, and Postgres.

Owned by T-011. Acceptance 3 is the subject of this file: *commerce_events enforces
idempotency_key uniqueness and valid prev_hash; replay reproduces the stream hash.*

Three layers, in order:

* **Canonical form.** RFC-8785 JCS is what makes two readers agree on one digest. The tests
  here are about the ways ``json.dumps(sort_keys=True)`` would have been subtly wrong.
* **The chain in memory.** ``seal_event`` / ``verify_chain`` -- including the exact assertion
  T-060's frozen test makes (``broken_at == 2`` for a payload mutated at index 2), asserted
  here so T-011 finds a regression in its own library rather than leaving it for T-060.
* **The chain in Postgres.** Idempotency, the link constraint, append-only, no forks, and
  the property everything else rests on: a row that has been through ``timestamptz`` and
  ``jsonb`` still hashes to what was written.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import sys
import types
from typing import Any

import psycopg
import pytest

from apps.trust.src.ledger import (
    GENESIS_HASH,
    CanonicalisationError,
    ChainIntegrityError,
    LedgerError,
    append_event,
    canonical_bytes,
    canonical_event,
    canonical_json,
    chain_events,
    chain_head,
    compute_event_hash,
    db_stream_hash,
    head_hash,
    observations_from_events,
    read_events,
    replay,
    rfc3339_ms,
    seal_event,
    stream_hash,
    verify_chain,
    verify_chain_in_db,
)

AS_OF = "2026-01-01T00:00:00Z"

DIMS = (
    "price_honored",
    "discount_honored",
    "shipped_on_time",
    "not_returned",
    "feedback_match",
    "catalog_claim_accuracy",
)


def _admin_dsn(connection: Any) -> str:
    """An admin DSN for the database ``connection`` is already on.

    D41: no hard-coded ports anywhere in test code -- the DSN is derived from the worker
    index and the live connection's own database name.
    """
    from proxyshop_support.postgres import role_dsn
    from proxyshop_support.worker import worker_id

    return role_dsn("admin", worker_id(), database=connection.info.dbname)


def observation_event(index: int, store_id: str = "s-1", dim: str = "price_honored") -> dict:
    """A ``claim_verified`` LedgerEvent carrying exactly one trust observation."""
    return {
        "event_id": f"ev-{index}",
        "ts": AS_OF,
        "kind": "claim_verified",
        "store_id": store_id,
        "payload": {"dim": dim, "type": "verified", "observed_at": AS_OF},
    }


# =======================================================================================
# Canonical form (RFC-8785 JCS, D16)
# =======================================================================================


def test_canonical_json_sorts_keys_and_emits_no_whitespace() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert canonical_json({"a": [1, {"z": 0, "y": 1}]}) == '{"a":[1,{"y":1,"z":0}]}'
    assert " " not in canonical_json({"a": {"b": [1, 2, 3]}})


def test_canonical_json_is_independent_of_insertion_order_and_a_json_round_trip() -> None:
    """The property that makes the digest stable across processes and storage layers."""
    first = {"kind": "accepted", "payload": {"total": 12.5, "code": "X"}, "event_id": "ev-1"}
    second = {"event_id": "ev-1", "payload": {"code": "X", "total": 12.5}, "kind": "accepted"}
    assert canonical_json(first) == canonical_json(second)
    assert canonical_json(json.loads(json.dumps(first))) == canonical_json(first)


#: ECMAScript ``String(x)`` for every value below, taken from ``node`` and pinned as
#: literals. These are the *external* oracle: nothing here is computed by the module under
#: test, so a change to the serialiser cannot quietly move the expectation with it.
#:
#: Every row from ``1e-7`` down was WRONG before this table existed. ``repr`` switches to
#: exponential notation at ``1e-4`` and ECMAScript switches at ``1e-7``, so ``1e-5`` hashed
#: as ``"1e-05"`` where RFC-8785 requires ``"0.00001"`` -- and any Go, Rust or JavaScript
#: verifier would have reported tampering on an untouched event.
ES6_NUMBER_FORMS = [
    (1.0, "1"),
    (-0.0, "0"),
    (0.0, "0"),
    (2.5, "2.5"),
    (0.1, "0.1"),
    (100.0, "100"),
    (-2.5, "-2.5"),
    (123456789.123, "123456789.123"),
    (1e-3, "0.001"),
    (1e-4, "0.0001"),  # repr() goes exponential here; ECMAScript does not
    (1e-5, "0.00001"),
    (1.5e-5, "0.000015"),
    (1e-6, "0.000001"),
    (1e-7, "1e-7"),  # ...and here is where ECMAScript finally does
    (-2.5e-7, "-2.5e-7"),
    (5e-324, "5e-324"),
    (1e20, "100000000000000000000"),
    (1e21, "1e+21"),
    (1e23, "1e+23"),
    (6.02214076e23, "6.02214076e+23"),
    (1.7976931348623157e308, "1.7976931348623157e+308"),
    (float(2**64), "18446744073709552000"),
]


@pytest.mark.parametrize(("value", "expected"), ES6_NUMBER_FORMS)
def test_canonical_json_matches_ecmascript_number_to_string(value: float, expected: str) -> None:
    """RFC-8785 §3.2.2.3: a JSON number is serialised by ECMAScript ``Number::toString``."""
    assert canonical_json(value) == expected


def test_canonical_json_uses_the_ecmascript_number_form() -> None:
    """The integral cases, and the contrast with ``json.dumps`` that motivates the module."""
    assert canonical_json(10) == "10"
    assert canonical_json(True) == "true"  # bool before int: `True == 1` in Python
    assert canonical_json({"n": 1.0}) == '{"n":1}'
    assert json.dumps(1.0) == "1.0", "the contrast this test exists for has gone away"
    assert json.dumps(1e-5) == "1e-05", "repr()'s exponential threshold has moved"


def test_integers_that_are_not_doubles_are_refused_rather_than_hashed_two_ways() -> None:
    """A JSON number is a double (RFC-8785 §3.1), and ``10**23`` is not one.

    ``10**23`` as a Python ``int`` renders as 24 digits; the same JSON text read back by any
    parser -- or by Postgres ``jsonb`` -- becomes the float ``1e23`` and renders as
    ``"1e+23"``. Two digests for one value. Refused loudly instead, because a ledger whose
    hash depends on which parser last touched the payload is not a ledger.

    This assertion used to read ``for value in (MAX_SAFE_INTEGER + 1, ...)`` and so claimed
    that *every* integer above ``2**53`` is refused. That claim was wrong: RFC-8785 §3.1
    says "expressible as IEEE 754 double-precision", not "below the safe-integer bound", and
    ``10**16``, ``2**53 + 2``, ``2**63`` and ``2**64`` are all exactly doubles. The old bound
    refused a bid quantity that the contracts canonicaliser had already signed and verified,
    which made a valid event unrecordable. The refusal is right; the boundary was not, so it
    is narrowed here to non-representable values and the exact doubles get their own case
    below. See ``test_exact_doubles_above_the_safe_range_are_canonicalised``.
    """
    assert canonical_json(2**53) == "9007199254740992"
    assert canonical_json(-(2**53)) == "-9007199254740992"
    # Every one of these falls strictly between two adjacent doubles, or beyond `DBL_MAX`.
    # `2**200 + 1` is here rather than `2**200`: the power of two IS a double, and that is
    # exactly the distinction the old magnitude bound could not draw.
    for value in (2**53 + 1, 2**54 - 1, 10**23, 2**200 + 1, -(10**30), 10**400):
        with pytest.raises(CanonicalisationError) as raised:
            canonical_json(value)
        assert "IEEE-754 double" in str(raised.value)
    # `10**400` overflows `float()` outright; the OverflowError must not leak to the caller.
    with pytest.raises(CanonicalisationError):
        canonical_json({"n": 10**400})


def test_exact_doubles_above_the_safe_range_are_canonicalised() -> None:
    """RFC-8785 §3.1 admits any integer expressible as an IEEE-754 double, not just small ones.

    The rendering is ECMAScript's ``Number::toString`` of the *double*, which above ``2**53``
    is not the integer's decimal expansion: ``String(2**64)`` in JavaScript is
    ``"18446744073709552000"``, because the shortest digit string that round-trips to that
    double is 17 significant digits. Emitting ``str(2**64)`` instead would disagree with
    every conforming JCS implementation.
    """
    assert canonical_json(10**16) == "10000000000000000"
    assert canonical_json(2**53 + 2) == "9007199254740994"
    assert canonical_json(2**63) == "9223372036854776000"
    assert canonical_json(2**64) == "18446744073709552000"
    assert canonical_json(-(10**16)) == "-10000000000000000"
    # Past 1e21 ES6 switches to exponential, and a huge exact double takes that branch.
    assert canonical_json(10**21) == "1e+21"
    assert canonical_json(2**200) == "1.6069380442589903e+60"
    assert canonical_json({"quantity": 10**16}) == '{"quantity":10000000000000000}'
    # The int and the float spell the same double, so they must hash identically.
    assert canonical_json(10**16) == canonical_json(1e16)
    big_int = observation_event(1) | {"payload": {"q": 10**16}}
    big_float = observation_event(1) | {"payload": {"q": 1e16}}
    assert compute_event_hash(GENESIS_HASH, big_int) == compute_event_hash(GENESIS_HASH, big_float)


def test_lone_surrogates_terminate_with_a_canonicalisation_error() -> None:
    """RFC-8785 §3.2.2.2: unpaired surrogates MUST terminate a compliant implementation.

    ``json.loads('{"\\ud800": 1}')`` hands Python a string UTF-8 cannot encode, so there are
    no canonical bytes to hash. This used to escape as a bare ``UnicodeEncodeError`` -- from
    ``str.encode`` inside the key sort for a key, and from ``canonical_bytes`` for a value --
    so a caller guarding the append path with ``except CanonicalisationError`` caught neither.
    """
    for value in ("\ud800", "a\udfffb", "\ud83d"):  # the last is a lone high surrogate
        with pytest.raises(CanonicalisationError, match="lone surrogate"):
            canonical_json(value)
        with pytest.raises(CanonicalisationError, match="lone surrogate"):
            canonical_json({value: 1})
        with pytest.raises(CanonicalisationError, match="lone surrogate"):
            canonical_json({"k": [value]})
        with pytest.raises(CanonicalisationError, match="lone surrogate"):
            canonical_bytes({"k": value})
    # A *paired* surrogate is an ordinary astral character and must still canonicalise.
    assert canonical_json("\U0001f600") == '"\U0001f600"'
    with pytest.raises(CanonicalisationError, match="lone surrogate"):
        compute_event_hash(GENESIS_HASH, observation_event(1) | {"payload": {"note": "\ud800"}})


def test_the_rfc_8785_worked_example_reproduces_byte_for_byte() -> None:
    """The specification's own §3.2.4 sample: input on the left, RFC text on the right.

    The strongest oracle available, because neither side of it comes from this codebase. It
    exercises the exponential branch (``1e+30``, ``1e-27``), the shortest-round-trip branch
    (``333333333.3333333``), trailing-zero removal (``4.50`` -> ``4.5``), the leading-zero
    branch (``0.002``), control-character escaping, and non-ASCII passthrough at once.
    """
    sample = {
        "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 0.000000000000000000000000001],
        "string": '€$\nA\'B"\\\\"/',
        "literals": [None, True, False],
    }
    assert canonical_json(sample) == (
        '{"literals":[null,true,false],'
        '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )


def test_canonical_json_sorts_by_utf16_code_unit_not_code_point() -> None:
    """JCS §3.2.3. The two orders differ above the BMP, and Python's default is the wrong one.

    ``"\\uffff"`` is one UTF-16 code unit ``FFFF``; ``"\\U0001f600"`` is the surrogate pair
    ``D83D DE00``. By code point the emoji is larger; by UTF-16 code unit it is smaller.
    """
    keys = {"￿": 1, "\U0001f600": 2}
    assert canonical_json(keys) == '{"\U0001f600":2,"￿":1}'
    assert json.dumps(keys, sort_keys=True, ensure_ascii=False) == ('{"￿": 1, "\U0001f600": 2}'), (
        "sort_keys=True no longer differs here; re-derive why this test exists before editing"
    )


def test_canonical_json_leaves_non_ascii_literal_and_escapes_controls() -> None:
    assert canonical_json("café") == '"café"'
    assert canonical_json("a\nb\tc") == '"a\\nb\\tc"'
    assert canonical_json("\x00") == '"\\u0000"'
    assert canonical_json('he said "hi"\\') == '"he said \\"hi\\"\\\\"'


def test_canonical_json_refuses_values_with_no_canonical_form() -> None:
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(CanonicalisationError):
            canonical_json(value)
    with pytest.raises(CanonicalisationError):
        canonical_json({1: "int key"})
    with pytest.raises(CanonicalisationError):
        canonical_json(object())


def test_rfc3339_ms_normalises_every_spelling_of_one_instant() -> None:
    """D16: UTC RFC-3339 at millisecond precision, whatever came in."""
    expected = "2026-01-01T00:00:00.000Z"
    assert rfc3339_ms("2026-01-01T00:00:00Z") == expected
    assert rfc3339_ms("2026-01-01T00:00:00.000Z") == expected
    assert rfc3339_ms("2026-01-01T01:00:00+01:00") == expected
    assert rfc3339_ms("2025-12-31T19:00:00-05:00") == expected
    assert rfc3339_ms(dt.datetime(2026, 1, 1, tzinfo=dt.UTC)) == expected
    assert rfc3339_ms(dt.datetime(2026, 1, 1)) == expected  # naive is read as UTC
    with pytest.raises(CanonicalisationError):
        rfc3339_ms("yesterday")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # .123456 is useless as evidence -- truncation and rounding agree on it. Every value
        # below is chosen so they DISAGREE, which is what makes this a test of the
        # truncation the module says is load-bearing rather than of "some ms conversion".
        ("2026-01-01T00:00:00.123900Z", "2026-01-01T00:00:00.123Z"),
        ("2026-01-01T00:00:00.999999Z", "2026-01-01T00:00:00.999Z"),
        ("2026-01-01T00:00:00.000999Z", "2026-01-01T00:00:00.000Z"),
        ("2026-01-01T00:00:00.9995Z", "2026-01-01T00:00:00.999Z"),
    ],
)
def test_sub_millisecond_digits_truncate_and_never_round(value: str, expected: str) -> None:
    """Rounding ``.9999`` up to ``1.000`` would change the second, and with it the digest.

    A value that has been through a ``timestamptz`` column must canonicalise to the string
    it did on the way in, so the direction of the conversion is part of the hash contract.
    """
    assert rfc3339_ms(value) == expected
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert rfc3339_ms(parsed) == expected


def test_canonical_event_drops_chain_fields_and_absent_optionals() -> None:
    """An event cannot commit to its own digest, and ``None`` is not a value."""
    event = observation_event(1)
    sealed = seal_event(event)
    assert canonical_event(sealed) == canonical_event(event)
    assert canonical_event({**event, "order_ref": None}) == canonical_event(event)
    assert canonical_event({**event, "seq": 7}) == canonical_event(event)
    with pytest.raises(CanonicalisationError):
        canonical_event({"ts": AS_OF, "payload": {}})  # no event_id, no kind


#: The golden event, its canonical form and its digest, all three pinned as **literals**.
#:
#: Without a literal on the right-hand side, every digest assertion in this file computes
#: its expectation with the same two functions it is testing -- so a change to
#: canonicalisation silently rewrites every hash in the system and the whole suite stays
#: green. These constants are what make canonicalisation drift a failure. Regenerating them
#: to match new behaviour is exactly the move they exist to prevent: if they go red, either
#: the change is a breaking change to every stored chain, or it is a bug.
#:
#: ``rate`` is deliberately ``1e-5``: it renders ``0.00001`` under ECMAScript and ``1e-05``
#: under ``repr``, so this constant also pins the fix for that.
GOLDEN_EVENT = {
    "event_id": "ev-golden",
    "ts": "2026-01-01T00:00:00.000Z",
    "kind": "order_paid",
    "store_id": "s-1",
    "order_ref": "o-1",
    "payload": {"total_price": 120.5, "quantity": 3, "flag": True, "note": "café", "rate": 1e-5},
}
GOLDEN_CANONICAL = (
    '{"event_id":"ev-golden","kind":"order_paid","order_ref":"o-1",'
    '"payload":{"flag":true,"note":"café","quantity":3,"rate":0.00001,"total_price":120.5},'
    '"store_id":"s-1","ts":"2026-01-01T00:00:00.000Z"}'
)
GOLDEN_DIGEST = "923fdbb87acce0fba37cfac3b9b6948381bee7b05fe67adc84a58e8851ddf9b3"
GOLDEN_STREAM_HASH = "d3a701c0ee62d7a478e97e028198b7a39ab7f9a8ebdb464252c9fe50e8694591"


def test_the_golden_event_canonicalises_and_hashes_to_its_pinned_constants() -> None:
    """The one assertion in this file whose expected values are not recomputed.

    ``hashlib`` is stdlib and the canonical string is a literal, so the middle line ties
    :data:`GOLDEN_DIGEST` to :data:`GOLDEN_CANONICAL` without touching the module under
    test at all.
    """
    import hashlib

    assert canonical_json(canonical_event(GOLDEN_EVENT)) == GOLDEN_CANONICAL
    assert (
        hashlib.sha256(GENESIS_HASH.encode("ascii") + GOLDEN_CANONICAL.encode("utf-8")).hexdigest()
        == GOLDEN_DIGEST
    )
    assert compute_event_hash(GENESIS_HASH, GOLDEN_EVENT) == GOLDEN_DIGEST


def test_the_golden_stream_hash_is_pinned() -> None:
    """A three-event chain's identity, as a literal. Catches drift in the fold, not just one hash."""
    events = [
        {"event_id": f"g-{i}", "ts": "2026-01-01T00:00:00.000Z", "kind": "shown", "payload": {}}
        for i in range(3)
    ]
    assert stream_hash(chain_events(events)) == GOLDEN_STREAM_HASH
    assert stream_hash(events) == GOLDEN_STREAM_HASH, (
        "stream_hash must be recomputed from content, so sealing the events first cannot change it"
    )


def test_the_digest_is_sha256_of_prev_hash_concatenated_with_the_canonical_body() -> None:
    """D16's formula: the *structure*, over an arbitrary event.

    The concatenation order and the prev-hash dependence are what this checks; the golden
    constants above are what pin the canonical bytes.
    """
    import hashlib

    event = observation_event(1)
    body = canonical_json(canonical_event(event))
    assert (
        compute_event_hash(GENESIS_HASH, event)
        == hashlib.sha256(GENESIS_HASH.encode("ascii") + body.encode("utf-8")).hexdigest()
    )
    other = "a" * 64
    assert (
        compute_event_hash(other, event)
        == hashlib.sha256(other.encode("ascii") + body.encode("utf-8")).hexdigest()
    )
    assert compute_event_hash(other, event) != compute_event_hash(GENESIS_HASH, event)


# =======================================================================================
# The chain, in memory
# =======================================================================================


def test_seal_event_does_not_mutate_its_input() -> None:
    event = observation_event(1)
    before = copy.deepcopy(event)
    sealed = seal_event(event)
    assert event == before
    assert sealed["prev_hash"] == GENESIS_HASH
    assert sealed["event_hash"] != GENESIS_HASH


def test_verify_chain_accepts_a_clean_stream() -> None:
    sealed = chain_events(observation_event(i, dim=DIMS[i % len(DIMS)]) for i in range(5))
    result = verify_chain(sealed)
    assert result["ok"] is True
    assert result["broken_at"] is None
    assert result["head_hash"] == chain_head(sealed) == sealed[-1]["event_hash"]


def test_verify_chain_reports_the_index_of_a_tampered_event() -> None:
    """The exact assertion T-060's frozen test makes, proved here against this library.

    T-060 owns ``apps/trust/src/events`` and may not write this package, so if the verifier
    were wrong the failure would land on a ticket that cannot fix it.
    """
    sealed = chain_events(observation_event(i, dim=DIMS[i % len(DIMS)]) for i in range(5))
    tampered = copy.deepcopy(sealed)
    tampered[2]["payload"]["type"] = "contradicted"

    broken = verify_chain(tampered)
    assert broken["ok"] is False
    assert broken["broken_at"] == 2
    assert broken["reason"] == "tampered"
    assert verify_chain(sealed)["ok"] is True, "the clean stream must still verify"


@pytest.mark.parametrize("index", [0, 1, 4])
def test_verify_chain_finds_tampering_at_any_position(index: int) -> None:
    sealed = chain_events(observation_event(i) for i in range(5))
    tampered = copy.deepcopy(sealed)
    tampered[index]["store_id"] = "s-impostor"
    assert verify_chain(tampered)["broken_at"] == index


def test_verify_chain_detects_a_reordered_or_truncated_stream() -> None:
    """Rewriting the sequence breaks the *links*, even though every event is untouched."""
    sealed = chain_events(observation_event(i) for i in range(5))

    reordered = [sealed[0], sealed[2], sealed[1], sealed[3], sealed[4]]
    assert verify_chain(reordered) == {
        "ok": False,
        "broken_at": 1,
        "reason": "broken_link",
        "head_hash": sealed[0]["event_hash"],
        "verified": 1,
    }

    dropped = sealed[:2] + sealed[3:]
    result = verify_chain(dropped)
    assert result["ok"] is False and result["broken_at"] == 2
    assert result["reason"] == "broken_link"


def test_verify_chain_refuses_an_unsealed_stream_rather_than_passing_it() -> None:
    """An unverifiable stream is not a verified one.

    This is the contract T-060's ``append`` has to meet: stamp the chain fields with
    ``seal_event``. A store that keeps only a running head hash leaves the verifier nothing
    to check, and the honest answer to "is this stream intact?" is then "unknown", which
    here is reported as not-ok rather than silently as ok.
    """
    raw = [observation_event(i) for i in range(3)]
    assert verify_chain(raw) == {
        "ok": False,
        "broken_at": 0,
        "reason": "unsealed",
        "head_hash": GENESIS_HASH,
        "verified": 0,
    }
    # CONTRACT CHANGED -- justify-test-edit, wave-1 verification finding 8.
    #
    # This block asserted `ok: True, reason: None`, on the strength of an inline comment
    # ("...but an empty stream is intact") written in this ticket's own test commit
    # (edb1845). Nothing in SPEC.md or DESIGN.md ever said it; it was a self-asserted
    # contract, and it is a fail-open. `read_events` returns `[]` for reasons that have
    # nothing to do with integrity -- a bad `after_seq`, a missing SELECT grant, the wrong
    # database, a store that has not loaded -- and every one of them then read as a healthy
    # chain. Truncation-to-empty read as a healthy chain too. "Nothing to check" and "checked
    # and intact" must not be the same answer, so an empty stream is now reported
    # `ok: False, reason: "empty"` unless the caller explicitly says empty is expected
    # (`allow_empty=True`, or `expected_length=0`). The two permitted forms are asserted
    # immediately below, so this is a change of contract rather than a loss of coverage.
    assert verify_chain([]) == {
        "ok": False,
        "broken_at": 0,
        "reason": "empty",
        "head_hash": GENESIS_HASH,
        "verified": 0,
    }
    assert verify_chain([], allow_empty=True) == {
        "ok": True,
        "broken_at": None,
        "reason": None,
        "head_hash": GENESIS_HASH,
        "verified": 0,
    }
    assert verify_chain([], expected_length=0)["ok"] is True


def test_verify_chain_returns_the_same_keys_whatever_the_outcome() -> None:
    """A result whose key set depends on the answer makes every caller branch first.

    T-060 and T-062 both read this dict; ``result["verified"]`` must not raise ``KeyError``
    precisely when the chain is broken.
    """
    sealed = chain_events(observation_event(i) for i in range(3))
    tampered = copy.deepcopy(sealed)
    tampered[1]["store_id"] = "s-other"
    expected = {"ok", "broken_at", "reason", "head_hash", "verified"}
    assert set(verify_chain(sealed)) == expected
    assert set(verify_chain(tampered)) == expected
    assert set(verify_chain([])) == expected
    assert set(verify_chain([observation_event(0)])) == expected
    assert verify_chain(tampered)["verified"] == 1


def test_stream_hash_commits_to_every_event_in_order() -> None:
    """The "event-stream hash" of acceptance 3: it moves for any change to any event."""
    sealed = chain_events(observation_event(i) for i in range(4))
    baseline = stream_hash(sealed)
    assert baseline == chain_head(sealed)
    assert stream_hash([]) == GENESIS_HASH

    assert stream_hash(chain_events(observation_event(i) for i in range(3))) != baseline
    reordered = chain_events(
        [observation_event(0), observation_event(2), observation_event(1), observation_event(3)]
    )
    assert stream_hash(reordered) != baseline, "the stream hash must depend on order"


def test_stream_hash_is_recomputed_from_content_not_read_off_the_last_row() -> None:
    """Otherwise "replay reproduces the stream hash" compares a column to itself.

    ``stream_hash`` used to return ``chain_head``, which reads the last event's stored
    ``event_hash`` verbatim. Every side of the acceptance-3 assertion then came from the
    same column, so it held against any self-consistent forgery -- rewrite the events AND
    their digests and the "replay" still matched.

    Recomputed, the digest depends on the events' *content*, so a forged stream whose stored
    digests agree with each other still fails against a head recorded at write time.
    """
    sealed = chain_events(observation_event(i) for i in range(4))
    genuine = stream_hash(sealed)

    # A forgery: every event rewritten, then re-sealed so it is internally perfect.
    forged = chain_events({**observation_event(i), "store_id": "s-impostor"} for i in range(4))
    assert verify_chain(forged)["ok"] is True, "the forgery is internally self-consistent"
    assert chain_head(forged) == forged[-1]["event_hash"]
    assert stream_hash(forged) != genuine, (
        "a wholesale-rewritten stream must not reproduce the original stream hash"
    )

    # And it ignores the stored digests entirely: corrupt them and the value is unchanged.
    corrupted = copy.deepcopy(sealed)
    for event in corrupted:
        event["event_hash"] = "f" * 64
        event["prev_hash"] = "e" * 64
    assert stream_hash(corrupted) == genuine


def test_chain_head_refuses_an_unsealed_tail_instead_of_silently_skipping_it() -> None:
    """``chain_head`` is what T-060's ``append`` links the next event behind.

    It used to skip any event carrying no ``event_hash``, so appending an unsealed event and
    then asking for the head returned the head of the sealed prefix -- and the next append
    linked behind the wrong event, in a store with no ``char(64) NOT NULL`` column to make
    that impossible. T-060's frozen acceptance is "appending a new event must change
    head_hash", which that silently broke.
    """
    sealed = chain_events(observation_event(i) for i in range(3))
    assert chain_head(sealed) == sealed[-1]["event_hash"]
    assert chain_head([]) == GENESIS_HASH

    with pytest.raises(ChainIntegrityError) as raised:
        chain_head([*sealed, observation_event(9)])
    assert "never sealed" in str(raised.value)


# =======================================================================================
# The chain, in Postgres -- acceptance 3
# =======================================================================================


@pytest.mark.docker
def test_append_event_chains_from_genesis(ledger_clean) -> None:
    connection = ledger_clean
    assert head_hash(connection) == GENESIS_HASH

    first = append_event(connection, observation_event(0))
    assert first.inserted is True
    assert first.event["prev_hash"] == GENESIS_HASH
    assert first.event["seq"] == 1

    second = append_event(connection, observation_event(1))
    assert second.event["prev_hash"] == first.event["event_hash"]
    assert head_hash(connection) == second.event["event_hash"] == second.head_hash
    assert verify_chain_in_db(connection)["ok"] is True


@pytest.mark.docker
def test_exact_double_integers_survive_the_jsonb_round_trip_identically(ledger_clean) -> None:
    """The claim the refusal rule rests on, finally measured rather than asserted.

    ``_serialise_number`` refuses a non-double integer on the grounds that it "would hash one
    way as a Python int and another way once Postgres ``jsonb`` has read it back". That
    argument only licenses refusing *non*-doubles if the doubles themselves do survive, and
    until this test nothing checked. They do: ``jsonb`` stores a JSON number as ``numeric``,
    which is arbitrary-precision, so the exact integer comes back and re-hashes to the same
    digest -- which is what makes ``verify_chain_in_db`` pass over a payload carrying one.
    """
    connection = ledger_clean
    payload = {"q": 10**16, "big": 2**63, "small": 2**53 - 1, "f": 1e16}
    written = append_event(connection, observation_event(0) | {"payload": payload})
    assert written.inserted is True

    (stored,) = read_events(connection)
    assert stored["payload"] == payload, "jsonb changed a value the hash commits to"
    assert stored["payload"]["big"] == 2**63  # not 9223372036854776000
    assert compute_event_hash(stored["prev_hash"], stored) == stored["event_hash"]
    assert verify_chain_in_db(connection)["ok"] is True

    # And the other half of the rule: a non-double never reaches the database at all.
    with pytest.raises(CanonicalisationError):
        append_event(connection, observation_event(1) | {"payload": {"q": 10**23}})
    assert len(read_events(connection)) == 1


@pytest.mark.docker
def test_duplicate_event_id_is_a_no_op(ledger_clean) -> None:
    """Acceptance 3, first half. D16: ``idempotency_key`` IS ``event_id``.

    Length and head hash both have to stay put -- a second row would advance the chain even
    if the duplicate were otherwise harmless, and the head hash is what every downstream
    consumer compares against.
    """
    connection = ledger_clean
    original = append_event(connection, observation_event(0))
    head_before = head_hash(connection)

    again = append_event(connection, copy.deepcopy(observation_event(0)))
    assert again.inserted is False
    assert again.event["event_hash"] == original.event["event_hash"]
    assert again.event["seq"] == original.event["seq"]
    assert head_hash(connection) == head_before
    assert len(read_events(connection)) == 1

    # ...and the store is not simply refusing everything.
    fresh = append_event(connection, observation_event(1))
    assert fresh.inserted is True
    assert head_hash(connection) != head_before
    assert len(read_events(connection)) == 2

    # The constraint is real, not just a Python-side check.
    with pytest.raises(psycopg.errors.UniqueViolation) as raised:
        with connection.cursor() as cur:
            cur.execute(
                "insert into ledger.commerce_events "
                "(idempotency_key, kind, occurred_at, prev_hash, event_hash) "
                "values ('ev-0', 'shown', date_trunc('milliseconds', now()), %s, %s)",
                (head_hash(connection), "f" * 64),
            )
    assert "idempotency_key" in str(raised.value)


@pytest.mark.docker
def test_prev_hash_must_link_to_the_chain_tail(ledger_clean) -> None:
    """Acceptance 3, second half: "valid prev_hash", enforced by the database.

    Enforced server-side on purpose. A Python-only rule protects the chain from the library
    and from nothing else; the trigger protects it from psql, from a future service, and
    from a writer that skipped the advisory lock.
    """
    connection = ledger_clean
    append_event(connection, observation_event(0))

    for bad_prev in (GENESIS_HASH, "a" * 64):
        with pytest.raises(psycopg.errors.IntegrityError) as raised:
            with connection.cursor() as cur:
                cur.execute(
                    "insert into ledger.commerce_events "
                    "(idempotency_key, kind, occurred_at, prev_hash, event_hash) "
                    "values (%s, 'shown', date_trunc('milliseconds', now()), %s, %s)",
                    (f"ev-bad-{bad_prev[:4]}", bad_prev, "b" * 64),
                )
        assert "does not link to the chain tail" in str(raised.value)

    # A non-hex digest is refused by the format CHECK. (The link check is a BEFORE INSERT
    # trigger and therefore runs *first*, so the malformed value under test here has to be
    # the one the trigger does not look at.)
    with pytest.raises(psycopg.errors.CheckViolation) as check:
        with connection.cursor() as cur:
            cur.execute(
                "insert into ledger.commerce_events "
                "(idempotency_key, kind, occurred_at, prev_hash, event_hash) "
                "values ('ev-bad-hex', 'shown', date_trunc('milliseconds', now()), %s, %s)",
                (head_hash(connection), "NOT A HASH".ljust(64, "z")),
            )
    assert "event_hash_hex" in str(check.value)
    assert verify_chain_in_db(connection)["ok"] is True
    assert len(read_events(connection)) == 1


@pytest.mark.docker
def test_the_chain_cannot_fork(ledger_clean) -> None:
    """Two rows sharing a ``prev_hash`` is a fork, and it is refused twice over.

    The trigger catches it first, because a fork always re-uses a link that is no longer the
    tail. The ``UNIQUE (prev_hash)`` constraint is the layer underneath, and it is asserted
    independently -- with the trigger disabled -- because "belt and braces" is only true if
    somebody has checked the braces.
    """
    connection = ledger_clean
    first = append_event(connection, observation_event(0))
    append_event(connection, observation_event(1))

    with pytest.raises(psycopg.errors.IntegrityError) as raised:
        with connection.cursor() as cur:
            cur.execute(
                "insert into ledger.commerce_events "
                "(idempotency_key, kind, occurred_at, prev_hash, event_hash) "
                "values ('ev-fork', 'shown', date_trunc('milliseconds', now()), %s, %s)",
                (first.event["event_hash"], "d" * 64),
            )
    assert "does not link to the chain tail" in str(raised.value)

    with connection.cursor() as cur:
        cur.execute(
            "alter table ledger.commerce_events disable trigger commerce_events_chain_guard_trigger"
        )
    try:
        with pytest.raises(psycopg.errors.UniqueViolation) as unique:
            with connection.cursor() as cur:
                cur.execute(
                    "insert into ledger.commerce_events "
                    "(idempotency_key, kind, occurred_at, prev_hash, event_hash) "
                    "values ('ev-fork', 'shown', date_trunc('milliseconds', now()), %s, %s)",
                    (first.event["event_hash"], "d" * 64),
                )
        assert "prev_hash" in str(unique.value)
    finally:
        # ENABLE ALWAYS, not ENABLE. `ledger_clean` hands back the session-scoped autocommit
        # admin connection, so this teardown is permanent for the rest of the run -- and
        # plain `ENABLE TRIGGER` puts the trigger back at tgenabled = 'O', NOT at the 'A'
        # the migration installed (T-114). Measured: with `enable trigger` here, the link
        # check spent every test after this one skippable by
        # `SET session_replication_role = 'replica'`, and
        # `test_every_ledger_integrity_trigger_is_installed_enable_always` below is what
        # caught it. Postgres has no "restore whatever it was" spelling; the state has to
        # be named.
        with connection.cursor() as cur:
            cur.execute(
                "alter table ledger.commerce_events enable always trigger "
                "commerce_events_chain_guard_trigger"
            )
    assert verify_chain_in_db(connection)["ok"] is True


@pytest.mark.docker
def test_commerce_events_is_append_only(ledger_clean) -> None:
    connection = ledger_clean
    append_event(connection, observation_event(0))
    for statement in (
        "update ledger.commerce_events set kind = 'shown'",
        "delete from ledger.commerce_events",
    ):
        with pytest.raises(psycopg.errors.IntegrityError) as raised:
            with connection.cursor() as cur:
                cur.execute(statement)
        assert "append-only" in str(raised.value)
    assert len(read_events(connection)) == 1


@pytest.mark.docker
def test_an_unknown_event_kind_is_refused(ledger_clean) -> None:
    """C11/D24 froze the vocabulary; the column enforces it."""
    connection = ledger_clean
    with pytest.raises(psycopg.errors.CheckViolation):
        append_event(connection, {**observation_event(0), "kind": "invented_kind"})
    for kind in ("bid_placed", "auction_opened", "blacklist_expired", "offer_integrity"):
        result = append_event(connection, {**observation_event(0), "event_id": kind, "kind": kind})
        assert result.inserted is True


@pytest.mark.docker
def test_a_round_trip_through_postgres_preserves_the_digest(ledger_clean) -> None:
    """The property everything else rests on.

    ``ts`` goes through ``timestamptz`` and the payload through ``jsonb``, and both come
    back re-rendered by the server. If either renormalised, the stored ``event_hash`` would
    no longer match the row it is on and the chain would fail verification with no tampering
    anywhere -- so the round trip is asserted over a payload holding every JSON shape.
    """
    connection = ledger_clean
    event = {
        "event_id": "ev-roundtrip",
        "ts": "2026-03-04T05:06:07.089Z",
        "kind": "order_paid",
        "auction_id": "auction-1",
        "store_id": "s-1",
        "order_ref": "o-1",
        "payload": {
            "checkout_token": "ck-1",
            "total_price": 120.5,
            "quantity": 3,
            "zero": 0,
            "negative": -2.25,
            "flag": True,
            "absent": None,
            "note": "café — ünicode",
            "discountApplications": [{"type": "percentage", "value": 10}],
            "nested": {"b": [1, 2, {"c": "d"}], "a": {}},
        },
    }
    written = append_event(connection, event)
    [row] = read_events(connection)

    assert row["event_hash"] == written.event["event_hash"]
    assert compute_event_hash(GENESIS_HASH, row) == row["event_hash"], (
        "the row read back out of Postgres no longer hashes to its stored digest"
    )
    assert canonical_event(row) == canonical_event(event)
    assert row["ts"] == "2026-03-04T05:06:07.089Z"
    assert verify_chain_in_db(connection)["ok"] is True


@pytest.mark.docker
def test_a_payload_that_jsonb_renormalises_is_refused_at_write_time(ledger_clean) -> None:
    """Loud at the moment of the mistake, rather than as a mystery verification failure.

    ``jsonb`` stores numbers as ``numeric``, so ``1e100`` comes back as a 101-digit integer
    and canonicalises differently. That would corrupt the chain silently.
    """
    connection = ledger_clean
    with pytest.raises(LedgerError) as raised:
        append_event(
            connection,
            {**observation_event(0), "payload": {"huge": 1e100}},
        )
    assert "jsonb round trip" in str(raised.value)
    assert read_events(connection) == [], "the failed append must leave nothing behind"


@pytest.mark.docker
def test_replay_from_postgres_reproduces_the_stream_hash(ledger_clean) -> None:
    """Acceptance 3, third clause: "replay reproduces the stream hash".

    The stream is written, read back from genesis, and re-verified: the head recomputed from
    what the database returns is the head that was recorded at write time, event for event.
    """
    connection = ledger_clean
    written = [append_event(connection, observation_event(i, dim=DIMS[i % 6])) for i in range(8)]
    recorded_head = written[-1].head_hash

    replayed = read_events(connection)
    assert [row["event_id"] for row in replayed] == [f"ev-{i}" for i in range(8)]
    assert stream_hash(replayed) == recorded_head == db_stream_hash(connection)
    assert verify_chain(replayed)["ok"] is True

    # An incremental read resumes the same chain rather than starting a new one.
    tail = read_events(connection, after_seq=5)
    assert [row["seq"] for row in tail] == [6, 7, 8]
    assert tail[0]["prev_hash"] == replayed[4]["event_hash"]


@pytest.mark.docker
def test_appending_under_the_advisory_lock_keeps_one_linear_chain(ledger_clean) -> None:
    """A second writer waits for the tail rather than racing it (D16).

    Both connections append; the chain that results is linear and verifies, and the second
    writer's ``prev_hash`` is the first writer's ``event_hash`` -- not the tail it saw when
    it started.
    """
    from proxyshop_support.postgres import role_dsn
    from proxyshop_support.worker import worker_id

    connection = ledger_clean
    first = append_event(connection, observation_event(0))

    dsn = role_dsn("admin", worker_id(), database=connection.info.dbname)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as other:
        second = append_event(other, observation_event(1))
    assert second.event["prev_hash"] == first.event["event_hash"]

    third = append_event(connection, observation_event(2))
    assert third.event["prev_hash"] == second.event["event_hash"]
    assert verify_chain_in_db(connection)["ok"] is True
    assert [row["seq"] for row in read_events(connection)] == [1, 2, 3]


@pytest.mark.docker
def test_the_writer_leaves_no_advisory_lock_behind(ledger_clean) -> None:
    """``pg_advisory_xact_lock`` is transaction-scoped, so the commit releases it.

    A session-scoped lock here would be held for the life of the connection and would
    deadlock the next writer on a pooled connection.
    """
    from apps.trust.src.ledger.store import CHAIN_LOCK_KEY

    connection = ledger_clean
    append_event(connection, observation_event(0))
    with connection.cursor() as cur:
        cur.execute(
            "select count(*) from pg_locks where locktype = 'advisory' and objid = %s "
            "  and database = (select oid from pg_database where datname = current_database())",
            (CHAIN_LOCK_KEY,),
        )
        assert cur.fetchone() == (0,)


@pytest.mark.docker
def test_a_malformed_event_is_rejected_before_it_reaches_the_database(ledger_clean) -> None:
    connection = ledger_clean
    with pytest.raises(LedgerError):
        append_event(connection, {"event_id": "ev-x", "kind": "shown"})  # no ts
    with pytest.raises(CanonicalisationError):
        append_event(connection, {"ts": AS_OF, "kind": "shown", "payload": {}})  # no event_id
    assert read_events(connection) == []


# =======================================================================================
# The replay seam (D49) -- projection here, every number in T-062
# =======================================================================================


def test_observations_from_events_projects_only_observation_payloads() -> None:
    """A ledger concern: what an event *carries*. Nothing here decides a score."""
    events = [
        observation_event(0, store_id="s-1", dim="price_honored"),
        {"event_id": "ev-a", "ts": AS_OF, "kind": "accepted", "store_id": "s-1", "payload": {}},
        observation_event(1, store_id="s-2", dim="catalog_claim_accuracy"),
        {"event_id": "ev-b", "ts": AS_OF, "kind": "auction_opened", "payload": {"x": 1}},
    ]
    assert observations_from_events(events) == [
        {"store_id": "s-1", "dim": "price_honored", "type": "verified", "observed_at": AS_OF},
        {
            "store_id": "s-2",
            "dim": "catalog_claim_accuracy",
            "type": "verified",
            "observed_at": AS_OF,
        },
    ]


def test_observations_preserve_stream_order_and_fall_back_to_the_event_timestamp() -> None:
    """Order is load-bearing: decay is a function of the recorded sequence (D17/S3)."""
    events = [
        {
            "event_id": "ev-0",
            "ts": "2026-01-02T00:00:00Z",
            "kind": "claim_verified",
            "store_id": "s-1",
            "payload": {"dim": "not_returned", "type": "mismatch_return"},
        },
        observation_event(1, dim="feedback_match"),
    ]
    projected = observations_from_events(events)
    assert [row["dim"] for row in projected] == ["not_returned", "feedback_match"]
    assert projected[0]["observed_at"] == "2026-01-02T00:00:00Z"


def test_replay_hands_every_number_to_the_scorer_grouped_by_store(monkeypatch) -> None:
    """D49: the scoring lives in T-062's files; this module is only the seam.

    The scorer is stubbed rather than imported, which is the point -- if ``replay`` computed
    anything itself, the stub's recorded arguments would not be the whole input and the
    returned object would not be the whole output.
    """
    calls: list[tuple[list[dict], Any]] = []

    def fake_score(observations, *, as_of):
        calls.append((list(observations), as_of))
        return {"score": 0.5, "score_version": "stub-1", "observations": len(observations)}

    stub = types.ModuleType("trust.scoring")
    stub.score = fake_score  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "trust.scoring", stub)

    events = [
        observation_event(0, store_id="s-1", dim="price_honored"),
        observation_event(1, store_id="s-2", dim="shipped_on_time"),
        observation_event(2, store_id="s-1", dim="feedback_match"),
    ]
    result = replay(events, as_of=AS_OF)

    assert set(result) == {"s-1", "s-2"}
    assert result["s-1"]["observations"] == 2
    assert result["s-2"]["observations"] == 1
    assert result["s-1"]["score_version"] == "stub-1"
    assert [as_of for _, as_of in calls] == [AS_OF, AS_OF]
    assert [row["dim"] for row in calls[0][0]] == ["price_honored", "feedback_match"]


def test_replay_names_the_scoring_ticket_when_the_scorer_is_absent(monkeypatch) -> None:
    """An unbuilt scorer must be a loud failure, never an empty mapping.

    ``{}`` compares equal to nothing and would read as a pass in whichever test asked for it.

    The absence is forced by pointing the lookup at a module that will never exist, rather
    than by relying on ``apps.trust.src.scoring`` being empty. Relying on that would have
    made this test go red the day T-062 landed -- a test scheduled to fail on somebody
    else's success is worse than no test.
    """
    import importlib

    replay_module = importlib.import_module("apps.trust.src.ledger.replay")

    monkeypatch.setattr(replay_module, "SCORING_MODULES", ("trust.scoring_does_not_exist",))
    with pytest.raises(ModuleNotFoundError) as raised:
        replay_module.replay([observation_event(0)], as_of=AS_OF)
    message = str(raised.value)
    assert "T-062" in message
    assert "score(observations" in message


def test_replay_reports_a_scorer_that_exists_but_exports_no_score(monkeypatch) -> None:
    """The half-built case: the module imports, the entry point is not there yet."""
    import importlib

    replay_module = importlib.import_module("apps.trust.src.ledger.replay")

    stub = types.ModuleType("trust.scoring_half_built")
    monkeypatch.setitem(sys.modules, "trust.scoring_half_built", stub)
    monkeypatch.setattr(replay_module, "SCORING_MODULES", ("trust.scoring_half_built",))
    with pytest.raises(ModuleNotFoundError) as raised:
        replay_module.replay([observation_event(0)], as_of=AS_OF)
    assert "exports no `score`" in str(raised.value)


@pytest.mark.docker
def test_tail_truncation_is_detected_by_the_stored_anchor(ledger_clean) -> None:
    """The failure a hash chain cannot see on its own.

    Delete the last events and the survivors verify **perfectly** -- a truncated chain's
    digest is a valid chain digest, and there is nothing in the links to say how many there
    should have been. ``ledger.chain_head`` is that something, and this is the test that
    makes it load-bearing rather than decorative.
    """
    connection = ledger_clean
    for index in range(6):
        append_event(connection, observation_event(index))
    intact = verify_chain_in_db(connection)
    assert intact["ok"] is True and intact["anchor_ok"] is True
    assert intact["anchor"]["length"] == 6
    assert intact["recomputed"] == intact["anchor"]["head_hash"]

    # Cut the tail off. DELETE is refused by the append-only trigger, which is the point --
    # so the only way to stage this is to disable it, exactly as an attacker with owner
    # rights would. Everything happens in a transaction that is always rolled back.
    with psycopg.connect(_admin_dsn(connection), connect_timeout=5) as probe:
        try:
            with probe.cursor() as cur:
                cur.execute(
                    "alter table ledger.commerce_events disable trigger "
                    "commerce_events_append_only_trigger"
                )
                cur.execute("delete from ledger.commerce_events where seq > 3")
                cur.execute("select count(*) from ledger.commerce_events")
                assert cur.fetchone() == (3,)

                survivors = read_events(probe)
                assert verify_chain(survivors)["ok"] is True, (
                    "the surviving prefix is internally flawless -- that is the whole problem"
                )
                truncated = verify_chain_in_db(probe)
                assert truncated["ok"] is False
                assert truncated["reason"] == "truncated"
                assert truncated["anchor_ok"] is False
                assert truncated["anchor"]["length"] == 6
        finally:
            probe.rollback()

    assert verify_chain_in_db(connection)["ok"] is True, "the rollback restored the chain"


@pytest.mark.docker
def test_truncate_resets_the_anchor_so_empty_table_means_empty_chain(ledger_clean) -> None:
    """TRUNCATE is the sanctioned owner-only reset, and it takes the anchor with it."""
    connection = ledger_clean
    append_event(connection, observation_event(0))
    from apps.trust.src.ledger import chain_anchor

    assert chain_anchor(connection)["length"] == 1
    with connection.cursor() as cur:
        cur.execute("truncate table ledger.commerce_events restart identity cascade")
    anchor = chain_anchor(connection)
    assert (anchor["length"], anchor["head_hash"]) == (0, GENESIS_HASH)
    assert verify_chain_in_db(connection)["ok"] is True


@pytest.mark.docker
def test_the_append_only_role_can_actually_append(ledger_clean, ledger_roles) -> None:
    """``app`` holds SELECT+INSERT on the ledger and nothing else. That has to be enough.

    It was not: the writer read the tail with ``SELECT ... FOR UPDATE``, which Postgres
    gates on the **UPDATE** privilege. The one role the grant model documents as the
    ledger's appender could not append, and the denial arrived as a bare
    ``permission denied for table commerce_events`` naming neither ``FOR UPDATE`` nor the
    privilege it wanted.
    """
    connection = ledger_roles.connection("app")
    connection.autocommit = True
    try:
        result = append_event(connection, observation_event(0))
        assert result.inserted is True
        assert read_events(connection)[0]["event_id"] == "ev-0"
        # ...and it still holds no UPDATE, so this is not a test that quietly widened a grant.
        with connection.cursor() as cur:
            cur.execute("select has_table_privilege('app', 'ledger.commerce_events', 'update')")
            assert cur.fetchone() == (False,)
    finally:
        connection.autocommit = False


@pytest.mark.docker
def test_append_event_waits_for_the_chain_lock(ledger_clean) -> None:
    """The advisory lock's actual purpose, exercised.

    Deleting ``pg_advisory_xact_lock`` from the writer used to kill zero tests: the
    "concurrency" test appended strictly sequentially, so the tail row already existed and
    the row lock alone sufficed. Here a second connection holds the chain lock and the
    writer must **block** on it -- which fails deterministically if the lock is not taken.
    """
    import threading

    from apps.trust.src.ledger.store import CHAIN_LOCK_KEY

    connection = ledger_clean
    dsn = _admin_dsn(connection)
    finished = threading.Event()
    outcome: dict[str, Any] = {}

    holder = psycopg.connect(dsn, connect_timeout=5)  # transactional: the lock persists
    try:
        with holder.cursor() as cur:
            cur.execute("select pg_advisory_xact_lock(%s)", (CHAIN_LOCK_KEY,))

        def worker() -> None:
            try:
                with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as writer:
                    outcome["result"] = append_event(writer, observation_event(0))
            except Exception as exc:  # pragma: no cover - reported through the assertion
                outcome["error"] = exc
            finally:
                finished.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert not finished.wait(2.0), (
            "append_event completed while another session held the chain lock, so it never "
            "took the lock -- concurrent appends are unserialised"
        )
        holder.rollback()  # release
        assert finished.wait(20.0), "append_event never completed after the lock was released"
        thread.join(timeout=5)
    finally:
        holder.close()

    assert "error" not in outcome, outcome.get("error")
    assert outcome["result"].inserted is True
    assert verify_chain_in_db(connection)["ok"] is True


@pytest.mark.docker
def test_an_event_without_a_payload_is_writable(ledger_clean) -> None:
    """``auction_opened`` and friends plausibly carry nothing.

    This used to be refused, by the jsonb round-trip guard, with an error blaming floating
    point: the hashed body omitted ``payload`` while the stored row carried the column
    default ``{}``.
    """
    connection = ledger_clean
    result = append_event(
        connection, {"event_id": "ev-bare", "ts": AS_OF, "kind": "auction_opened"}
    )
    assert result.inserted is True
    assert result.event["payload"] == {}
    assert verify_chain_in_db(connection)["ok"] is True
    # An explicit empty payload is the same event, so it is idempotent against the above.
    again = append_event(
        connection, {"event_id": "ev-bare", "ts": AS_OF, "kind": "auction_opened", "payload": {}}
    )
    assert again.inserted is False


@pytest.mark.docker
def test_reusing_an_event_id_for_different_content_is_refused(ledger_clean) -> None:
    """Idempotency must not become silent data loss.

    Matching on ``event_id`` alone and returning the stored row gave a success-shaped result
    for a write that never happened, with nothing for the caller to notice.
    """
    connection = ledger_clean
    append_event(connection, observation_event(0, store_id="s-1"))
    with pytest.raises(LedgerError) as raised:
        append_event(connection, observation_event(0, store_id="s-impostor"))
    assert "DIFFERENT content" in str(raised.value)
    assert len(read_events(connection)) == 1
    assert read_events(connection)[0]["store_id"] == "s-1"


@pytest.mark.docker
def test_append_refuses_a_connection_with_an_open_transaction_unless_told_to_join(
    ledger_clean,
) -> None:
    """``connection.transaction()`` opens a SAVEPOINT when a transaction is already running.

    The chain lock is transaction-scoped, so joining silently would hold the single global
    ledger lock until the *caller's* commit -- every other writer blocked, for a span the
    ledger does not control. Opt-in, and named.
    """
    connection = ledger_clean
    with psycopg.connect(_admin_dsn(connection), connect_timeout=5) as caller:
        with caller.cursor() as cur:
            cur.execute("select 1")  # a transaction is now open
        with pytest.raises(LedgerError) as raised:
            append_event(caller, observation_event(0))
        assert "join_open_transaction" in str(raised.value)

        result = append_event(caller, observation_event(0), join_open_transaction=True)
        assert result.inserted is True
        caller.rollback()  # ...and the append is part of the caller's transaction

    assert read_events(connection) == [], "the append must roll back with its caller"


def test_the_ledger_verifier_is_not_the_claim_verifier() -> None:
    """``verify_chain`` (ledger) and ``packages.verification.verify`` (T-065) are different.

    Asserted because the two names are one word apart and merging them would silently make
    a chain assertion into a claim assertion.
    """
    from apps.trust.src import ledger

    assert ledger.verify_chain.__module__.endswith("ledger.chain")
    assert not hasattr(ledger, "verify")


# =======================================================================================
# Wave-1 verification findings 2, 3, 5 and 8 -- regression guards for defects that shipped
# with a 110-green gate. Each one was re-verified by re-applying the exact sabotage.
# =======================================================================================


def _refused(runner, role: str, sql: str, params=None) -> Exception:
    """Assert ``sql`` is refused for ``role`` **by a trigger**, and return the exception.

    The sibling of ``ledger_roles.denied``, which asserts a *privilege* denial. Some of the
    anchor's rules cannot be expressed as a grant -- ``trust_rw`` and ``app`` must keep
    UPDATE on ``ledger.chain_head`` because the append trigger runs as them -- so those are
    enforced by ``ledger.chain_head_guard()`` and arrive as an integrity violation instead.
    Rolls the role connection back before returning (CF-4).
    """
    connection = runner.connection(role)
    try:
        with connection.cursor() as cur:
            cur.execute(sql, params)
    except psycopg.errors.IntegrityConstraintViolation as exc:
        return exc
    except psycopg.Error as exc:
        raise AssertionError(
            f"as {role!r}, {sql!r} failed with {type(exc).__name__} rather than an integrity "
            f"violation from the anchor guard: {exc}"
        ) from exc
    else:
        raise AssertionError(
            f"as {role!r}, {sql!r} SUCCEEDED. The chain's anchor is the only mechanism that "
            f"can detect tail truncation, and a role that can rewrite it has defeated the "
            f"mechanism rather than tripped it."
        )
    finally:
        connection.rollback()


@pytest.mark.docker
def test_the_anchor_is_not_rewritable_by_the_two_roles_that_append(
    ledger_clean, ledger_roles
) -> None:
    """Finding 2, attacks (B) and the grant half of (C) and (D).

    ``ledger.chain_head`` is the ONLY mechanism that can detect tail truncation, so the roles
    it constrains must not be able to re-author it. All three were ALLOWED before this guard,
    proven live:

    * ``UPDATE chain_head SET length = 99`` -- a flawless ledger then permanently
      self-reports ``{'ok': False, 'reason': 'truncated'}``. Unfalsifiable repudiation: the
      ledger accuses itself and no one can clear it.
    * ``DELETE FROM chain_head`` -- appends then kept succeeding with no anchor at all.
    * re-INSERT a forged anchor -- a truncated chain then verified ``{'ok': True}``.

    The append path itself keeps working; that is asserted at the end, because a grant model
    that closes a hole by breaking the writer has not closed anything.
    """
    connection = ledger_clean
    for index in range(3):
        append_event(connection, observation_event(index))

    forged_insert = (
        "insert into ledger.chain_head (chain, head_hash, length, last_seq) "
        "values ('commerce_events', repeat('a', 64), 3, 3)"
    )
    for role in ("trust_rw", "app"):
        # (C) and (D): gone at the GRANT level. There is no legitimate INSERT or DELETE on
        # this table for either role -- the seed row is the migration's, and the reset is an
        # UPDATE issued by the AFTER TRUNCATE trigger.
        ledger_roles.denied(role, "delete from ledger.chain_head")
        ledger_roles.denied(role, forged_insert)
        ledger_roles.denied(role, "truncate table ledger.chain_head")

        # (B): UPDATE they must keep, because the append trigger runs as them -- so the
        # bound is the trigger, not the grant.
        assert "advances by exactly one" in str(
            _refused(ledger_roles, role, "update ledger.chain_head set length = 99")
        )
        assert "monotonic" in str(
            _refused(
                ledger_roles,
                role,
                "update ledger.chain_head set length = length + 1, last_seq = 0",
            )
        )
        assert "not the event_hash stored at seq" in str(
            _refused(
                ledger_roles,
                role,
                "update ledger.chain_head set head_hash = repeat('b', 64), "
                "length = length + 1, last_seq = last_seq + 1",
            )
        )
        assert "still holds rows" in str(
            _refused(
                ledger_roles,
                role,
                "update ledger.chain_head set head_hash = repeat('0', 64), length = 0, "
                "last_seq = 0",
            )
        )
        # ...and it can still read the anchor it is supposed to maintain.
        assert ledger_roles.fetch(role, "select length from ledger.chain_head")[0] == (3,)

    # The writer still writes. This is the assertion that keeps the fix honest.
    appender = ledger_roles.connection("app")
    appender.autocommit = True
    try:
        assert append_event(appender, observation_event(3)).inserted is True
    finally:
        appender.autocommit = False
    assert chain_anchor_of(connection)["length"] == 4
    assert verify_chain_in_db(connection)["ok"] is True


def chain_anchor_of(connection):
    """``chain_anchor`` under a name that cannot shadow another ticket's fixture."""
    from apps.trust.src.ledger import chain_anchor

    return chain_anchor(connection)


@pytest.mark.docker
def test_an_append_that_cannot_advance_the_anchor_is_refused_rather_than_silent(
    ledger_clean,
) -> None:
    """Finding 2, attack (C), and the reporting half of it.

    ``commerce_events_advance_anchor()`` never checked ``FOUND``: with the anchor row gone
    its ``UPDATE ... WHERE chain = 'commerce_events'`` matched zero rows and returned NULL,
    so appends kept committing UNANCHORED -- ``inserted = True``, no error, and the one
    mechanism that can detect truncation quietly not running. ``verify_chain_in_db`` then
    *raised* instead of returning its documented result, so a caller writing
    ``verify_chain_in_db(conn)["ok"]`` got an exception on the one path that matters most.

    Staged the way an owner-level attacker would: disable the guard, delete, re-enable.
    Everything happens in a transaction that is always rolled back.
    """
    connection = ledger_clean
    for index in range(3):
        append_event(connection, observation_event(index))

    with psycopg.connect(_admin_dsn(connection), connect_timeout=5) as probe:
        try:
            with probe.cursor() as cur:
                cur.execute(
                    "alter table ledger.chain_head disable trigger chain_head_guard_trigger"
                )
                cur.execute("delete from ledger.chain_head")
                # ALWAYS, not the plain spelling (T-127). Written under T-011, when the
                # migration installed this trigger at the CREATE TRIGGER default
                # tgenabled = 'O'; T-114 moved it to 'A', and Postgres has no "put it back
                # to whatever it was" -- the state has to be named. Rolled back here, so
                # the plain spelling was latent rather than live; naming the wrong state in
                # a teardown is a hazard whether or not this particular transaction commits.
                cur.execute(
                    "alter table ledger.chain_head enable always trigger chain_head_guard_trigger"
                )
                cur.execute("select count(*) from ledger.chain_head")
                assert cur.fetchone() == (0,)

            with pytest.raises(psycopg.errors.IntegrityConstraintViolation) as unanchored:
                append_event(probe, observation_event(3), join_open_transaction=True)
            assert "could not advance the anchor" in str(unanchored.value)
            assert len(read_events(probe)) == 3, "the unanchored append must not have landed"

            # ...and the verifier REPORTS the missing anchor rather than raising on it.
            missing = verify_chain_in_db(probe)
            assert missing["ok"] is False
            assert missing["reason"] == "anchor_missing"
            assert missing["anchor"] is None and missing["anchor_ok"] is False
            assert set(missing) >= {"ok", "broken_at", "reason", "head_hash", "verified"}

            # (D) anchor laundering: re-INSERT an anchor recomputed over the survivors.
            # Before the guard this returned {'ok': True, 'anchor_ok': True} over a chain
            # whose tail had been cut off.
            try:
                with probe.transaction():
                    with probe.cursor() as cur:
                        cur.execute(
                            "insert into ledger.chain_head "
                            "  (chain, head_hash, length, last_seq) "
                            "values ('commerce_events', %s, %s, 3)",
                            (db_stream_hash(probe), len(read_events(probe))),
                        )
            except psycopg.errors.IntegrityConstraintViolation as exc:
                assert "genesis seed row" in str(exc)
            else:
                raise AssertionError(
                    "a laundered anchor was accepted: any prior truncation can now be made "
                    "to verify clean"
                )
        finally:
            probe.rollback()

    assert verify_chain_in_db(connection)["ok"] is True, "the rollback restored the anchor"


@pytest.mark.docker
def test_a_length_only_divergence_is_caught_although_the_head_hash_still_matches(
    ledger_clean,
) -> None:
    """Finding 5: the truncation detector's load-bearing half, isolated.

    ``anchor_ok`` is ``recomputed == anchor.head_hash AND len(events) == anchor.length``, and
    the single truncation test moves head and length TOGETHER, so it cannot tell the two
    clauses apart -- deleting the length clause left the gate at 110 green. The head clause
    is the redundant one: a chain whose links all verify necessarily folds to its own last
    stored digest, so it passes on any prefix that was cut. Only the ROW COUNT catches a
    divergence the links cannot see.

    Here the head half is asserted to AGREE, so the length half is the only thing that can
    fail. Re-apply the sabotage and this goes red on its own.
    """
    connection = ledger_clean
    for index in range(6):
        append_event(connection, observation_event(index))

    with psycopg.connect(_admin_dsn(connection), connect_timeout=5) as probe:
        try:
            with probe.cursor() as cur:
                cur.execute(
                    "alter table ledger.chain_head disable trigger chain_head_guard_trigger"
                )
                cur.execute("update ledger.chain_head set length = length + 1")
                # ALWAYS, not the plain spelling -- see the note at the identical teardown
                # in the attack-(C) test above (T-127).
                cur.execute(
                    "alter table ledger.chain_head enable always trigger chain_head_guard_trigger"
                )

            events = read_events(probe)
            assert verify_chain(events)["ok"] is True, "every link still verifies"
            result = verify_chain_in_db(probe)
            assert result["recomputed"] == result["anchor"]["head_hash"], (
                "the head half of anchor_ok AGREES here -- so only the length half can be "
                "what fails, which is the whole point of this test"
            )
            assert result["anchor"]["length"] == len(events) + 1
            assert result["ok"] is False
            assert result["anchor_ok"] is False
            assert result["reason"] == "truncated"
        finally:
            probe.rollback()

    assert verify_chain_in_db(connection)["ok"] is True


def test_verify_chain_detects_a_digest_that_differs_only_in_its_tail() -> None:
    """Finding 3: a 32-bit hash comparison shipped 100% green.

    Replacing ``compute_event_hash(prev, event) != str(stored_hash)`` with
    ``[:8] != str(stored_hash)[:8]`` -- 256 bits of integrity cut to 32 -- left the gate at
    110 passed. Every existing tamper test mutates event CONTENT, which changes the digest
    from character 0, so nothing ever presented a hash differing only in its tail.

    Under that sabotage the corrupted event's prefix matches, index 2 passes, ``prev``
    becomes the corrupted digest, and the break surfaces at index 3 as ``broken_link`` -- so
    both assertions below fail. That is what makes this a regression test rather than a
    restatement.
    """
    sealed = chain_events(observation_event(i, dim=DIMS[i % len(DIMS)]) for i in range(5))
    corrupted = copy.deepcopy(sealed)
    genuine = str(corrupted[2]["event_hash"])
    corrupted[2]["event_hash"] = genuine[:-1] + ("0" if genuine[-1] != "0" else "1")

    assert corrupted[2]["event_hash"][:8] == genuine[:8], "the first 32 bits are IDENTICAL"
    assert corrupted[2]["event_hash"] != genuine
    result = verify_chain(corrupted)
    assert result["ok"] is False
    assert result["broken_at"] == 2
    assert result["reason"] == "tampered"


@pytest.mark.parametrize("position", [8, 16, 32, 48, 63])
def test_verify_chain_compares_every_character_of_the_digest(position: int) -> None:
    """The same defect at every prefix length a truncated comparison might have used."""
    sealed = chain_events(observation_event(i) for i in range(4))
    corrupted = copy.deepcopy(sealed)
    genuine = str(corrupted[1]["event_hash"])
    flipped = "0" if genuine[position] != "0" else "1"
    corrupted[1]["event_hash"] = genuine[:position] + flipped + genuine[position + 1 :]
    assert corrupted[1]["event_hash"] != genuine
    result = verify_chain(corrupted)
    assert (result["broken_at"], result["reason"]) == (1, "tampered")


def test_verify_chain_needs_a_witness_from_outside_the_stream_to_see_truncation() -> None:
    """Finding 8: what a bare hash chain cannot see, and the parameters that fix it.

    Truncate-the-tail and rewrite-the-whole-stream both leave every link intact, so
    ``verify_chain`` alone reports them ``ok``. That is inherent, not a bug -- but the frozen
    acceptance contract imports ``verify_chain``, not ``verify_chain_in_db``, and in-memory
    consumers have no ``ledger.chain_head`` to consult. ``expected_length`` /
    ``expected_head`` are how they supply the witness.
    """
    sealed = chain_events(observation_event(i) for i in range(5))
    committed_head = chain_head(sealed)

    truncated = sealed[:3]
    assert verify_chain(truncated)["ok"] is True, "the links cannot see it -- that is the point"
    assert verify_chain(truncated, expected_length=5)["ok"] is False
    assert verify_chain(truncated, expected_length=5)["reason"] == "truncated"
    assert verify_chain(truncated, expected_head=committed_head)["reason"] == "head_mismatch"

    rewritten = chain_events({**observation_event(i), "store_id": "s-impostor"} for i in range(5))
    assert verify_chain(rewritten)["ok"] is True, "the forgery is internally self-consistent"
    assert verify_chain(rewritten, expected_head=committed_head)["ok"] is False
    assert verify_chain(rewritten, expected_length=5)["ok"] is True, (
        "length alone cannot see a rewrite -- the head is the half that does"
    )

    assert verify_chain(sealed, expected_length=5, expected_head=committed_head)["ok"] is True


def test_an_empty_stream_is_reported_rather_than_passed() -> None:
    """Finding 8: "nothing to check" and "checked and intact" must not be one answer.

    ``read_events`` returns ``[]`` for reasons that have nothing to do with integrity -- a
    bad ``after_seq``, a missing SELECT grant, the wrong database, a store that has not
    loaded -- and truncation-to-empty returns it too. Every one of those read as a healthy
    chain.
    """
    assert verify_chain([])["ok"] is False
    assert verify_chain([])["reason"] == "empty"
    assert verify_chain([], allow_empty=True)["ok"] is True
    assert verify_chain([], expected_length=0)["ok"] is True
    # "empty" wins over "truncated" when there is nothing at all: it is the more
    # specific fact, and it is the one a caller can act on.
    assert verify_chain([], expected_length=3)["reason"] == "empty"


def test_verify_chain_keeps_the_frozen_call_signature_it_is_imported_under() -> None:
    """26 downstream tickets and the frozen acceptance suite call ``verify_chain(events)``.

    The new arguments are keyword-only, so the one-positional call the frozen contract makes
    (``.swarm-loop/acceptance/test_e6_trust.py``) cannot be broken by adding to them, and the
    five-key result shape is unchanged on every outcome.
    """
    import inspect

    parameters = list(inspect.signature(verify_chain).parameters.values())
    assert parameters[0].name == "events"
    assert parameters[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters[1:]), (
        "a new positional parameter would break every frozen one-positional call site"
    )
    five = {"ok", "broken_at", "reason", "head_hash", "verified"}
    sealed = chain_events(observation_event(i) for i in range(3))
    assert set(verify_chain(sealed)) == five
    assert set(verify_chain([])) == five
    assert set(verify_chain(sealed, expected_length=9)) == five
    assert set(verify_chain(sealed, expected_head="f" * 64)) == five


# =======================================================================================
# T-102 (wave-2 finding W2-04) -- the DELETE arm of ``chain_head_guard_trigger``.
#
# 0002:189 names ``DELETE FROM ledger.chain_head`` as the whole of attack (C): the anchor
# row goes, ``commerce_events_advance_anchor()`` matches zero rows, and appends keep
# committing UNANCHORED. 0004 revokes DELETE from ``trust_rw`` and ``app``; the trigger is
# the half of that defence which does not depend on a grant being right -- and nothing
# graded it. The three tests above go past the arm on both sides: the role test at
# ``test_the_anchor_is_not_rewritable_by_the_two_roles_that_append`` asserts a *privilege*
# denial, which fires before any trigger can run, and the two tests that stage attack (C)
# reach the anchor-less state through ``ALTER TABLE ... DISABLE TRIGGER``, which is the arm
# switched off rather than exercised.
#
# Measured in this tree, worker 13: cutting ``BEFORE INSERT OR UPDATE OR DELETE`` down to
# ``BEFORE INSERT OR UPDATE`` (0002:287) leaves ``pg_trigger.tgtype`` at 23 with the DELETE
# bit clear, makes an owner ``DELETE FROM ledger.chain_head`` succeed with ``rowcount=1``,
# and ``pytest apps/trust -q`` still reports ``138 passed``. Both tests below go red under
# exactly that edit.
# =======================================================================================

#: ``pg_trigger.tgtype`` bits, from ``src/include/catalog/pg_trigger.h``. Postgres exposes
#: no boolean column for "does this trigger have a DELETE arm" -- the arms live in this one
#: bitmask, so the assertion has to be made at the bit.
TRIGGER_TYPE_ROW = 1 << 0
TRIGGER_TYPE_BEFORE = 1 << 1
TRIGGER_TYPE_INSERT = 1 << 2
TRIGGER_TYPE_DELETE = 1 << 3
TRIGGER_TYPE_UPDATE = 1 << 4
TRIGGER_TYPE_TRUNCATE = 1 << 5
TRIGGER_TYPE_INSTEAD = 1 << 6

#: The trigger's own words. Asserted verbatim so that a *grant* denial, a missing table, or
#: any other refusal cannot be mistaken for this arm having fired.
ANCHOR_DELETE_REFUSAL = (
    "ledger.chain_head is the ledger's only truncation detector: DELETE is not permitted"
)


def _anchor_delete_is_refused_by_the_trigger(owner, statement: str) -> None:
    """Assert ``statement`` is refused by ``chain_head_guard()``'s DELETE arm.

    Never commits: the statement is rolled back on both paths, so a run in which the arm is
    missing and the DELETE therefore *succeeds* still leaves the anchor row where it was for
    the tests that follow.
    """
    rowcount: int | None = None
    try:
        with owner.cursor() as cur:
            cur.execute(statement)
            rowcount = cur.rowcount
    except psycopg.errors.IntegrityConstraintViolation as exc:
        assert ANCHOR_DELETE_REFUSAL in str(exc), (
            f"{statement!r} was refused, but not by the anchor guard's DELETE arm: {exc}"
        )
        assert exc.sqlstate == "23000", (
            "the arm raises USING ERRCODE = 'integrity_constraint_violation'; callers "
            f"discriminate on that, and this arrived as {exc.sqlstate}"
        )
    except psycopg.Error as exc:
        raise AssertionError(
            f"{statement!r} failed with {type(exc).__name__} rather than the anchor guard's "
            f"DELETE arm: {exc}. A refusal from somewhere else does not grade this arm."
        ) from exc
    else:
        raise AssertionError(
            f"{statement!r} SUCCEEDED (rowcount={rowcount}). "
            f"Deleting the anchor is attack (C): the advance trigger then matches zero rows "
            f"and every subsequent append commits with nothing able to detect a truncated "
            f"tail. The BEFORE ... OR DELETE arm of chain_head_guard_trigger is gone."
        )
    finally:
        owner.rollback()


def _anchor_delete_succeeds_once_the_arm_is_switched_off(owner, statement: str) -> None:
    """The positive control: the SAME statement on the SAME connection, arm disabled.

    T-118 (f). ``has_table_privilege`` being TRUE is a weaker fact than the prose next to it
    used to claim. It says the privilege check will not be what refuses this statement --
    and for a superuser it says that unconditionally, whatever the table's ACL holds -- but
    it says nothing about what else might. A CHECK constraint, a foreign key, a rule or a
    row-security policy would refuse this DELETE too, and a test that only ever sees the
    statement fail cannot tell any of them apart from the trigger.

    Disabling the arm and watching the identical statement succeed is what closes that gap:
    with ``chain_head_guard_trigger`` off and nothing else changed, the DELETE removes the
    anchor row. So the refusal next door is the trigger's, and the guard is load-bearing
    rather than redundant with something else.

    Everything is rolled back, including the ``ALTER TABLE``, so the arm is back on and the
    anchor is back before the next assertion runs.
    """
    try:
        with owner.cursor() as cur:
            cur.execute("alter table ledger.chain_head disable trigger chain_head_guard_trigger")
            try:
                cur.execute(statement)
            except psycopg.Error as exc:
                raise AssertionError(
                    f"{statement!r} was still refused with chain_head_guard_trigger DISABLED, "
                    f"by {type(exc).__name__}: {exc}. Something other than that trigger is "
                    f"stopping this statement, so the refusal asserted next door is not "
                    f"evidence about the arm -- which is exactly what asserting "
                    f"has_table_privilege alone could never tell you."
                ) from exc
            assert cur.rowcount == 1, (
                f"{statement!r} matched {cur.rowcount} rows with chain_head_guard_trigger "
                f"DISABLED. The refusal asserted elsewhere is then not evidence about the "
                f"trigger -- something other than the guard is what stops this statement."
            )
            cur.execute("select count(*) from ledger.chain_head")
            assert cur.fetchone() == (0,), "the anchor row survived a DELETE that reported 1 row"
    finally:
        owner.rollback()


@pytest.mark.docker
def test_the_anchor_guards_delete_arm_refuses_the_table_owner_as_well(ledger_clean) -> None:
    """W2-04 acceptance 1: the arm fires for the principal the grant layer cannot refuse.

    ``ledger_roles.denied(role, "delete from ledger.chain_head")`` above proves only that
    ``trust_rw`` and ``app`` lack the privilege -- Postgres refuses those before the trigger
    is ever consulted, so that assertion stays green with the DELETE arm deleted. The owner
    is the principal that separates the two layers.

    **What the precondition below is and is not** (T-118 (f)). This connection owns the
    table and is a superuser, so ``has_table_privilege`` is TRUE *by ownership*, and a
    superuser's ``has_table_privilege`` is TRUE for every table in the cluster whatever its
    ACL says. It therefore establishes exactly one thing -- that the privilege check is not
    what will refuse the statement -- and NOT the stronger claim the prose here used to
    make, that the trigger is the only thing left in the way. The discriminator is the
    positive control that follows it: with the arm switched off and nothing else changed,
    the identical statement on the identical connection succeeds. That, plus the verbatim
    message match inside :func:`_anchor_delete_is_refused_by_the_trigger`, is what makes
    this a test of the trigger rather than of "something said no".

    **What the arm does not stop** (T-114 acceptance 4). An owner who first turns the
    trigger off. ``ALTER TABLE ... DISABLE TRIGGER`` is DDL: it takes an ACCESS EXCLUSIVE
    lock and moves ``pg_trigger.tgenabled`` to ``'D'``, where the catalog test below sees
    it. Until T-114 the arm also failed to stop an owner who merely set
    ``session_replication_role = 'replica'`` -- a GUC, invisible to every catalog query
    there is, needing no DDL and leaving no trace. The migration now installs every ledger
    integrity trigger ``ENABLE ALWAYS``; ``test_a_replica_session_replication_role_cannot_
    skip_the_ledger_integrity_triggers`` below drives that, and the catalog test pins it.
    """
    connection = ledger_clean
    for index in range(3):
        append_event(connection, observation_event(index))

    with psycopg.connect(_admin_dsn(connection), connect_timeout=5) as owner:
        try:
            with owner.cursor() as cur:
                cur.execute(
                    "select has_table_privilege(current_user, 'ledger.chain_head', 'DELETE'), "
                    "       pg_has_role(current_user, (select relowner from pg_class "
                    "                                   where oid = 'ledger.chain_head'::regclass), "
                    "                   'USAGE')"
                )
                assert cur.fetchone() == (True, True), (
                    "this connection must be able to DELETE as far as the grant layer is "
                    "concerned, or the test below re-asserts the privilege check instead of "
                    "the trigger"
                )
            owner.rollback()

            # ...and the fact that actually discriminates the two layers (T-118 (f)).
            _anchor_delete_succeeds_once_the_arm_is_switched_off(
                owner, "delete from ledger.chain_head"
            )

            _anchor_delete_is_refused_by_the_trigger(owner, "delete from ledger.chain_head")
            _anchor_delete_is_refused_by_the_trigger(
                owner, "delete from ledger.chain_head where chain = 'commerce_events'"
            )
            # A no-op DELETE touches no row, so the FOR EACH ROW arm never fires. Asserted so
            # the arm is understood to be row-scoped rather than statement-scoped.
            with owner.cursor() as cur:
                cur.execute("delete from ledger.chain_head where chain = 'no_such_chain'")
                assert cur.rowcount == 0
        finally:
            owner.rollback()

    # The anchor is still there, still correct, and the writer still writes: a guard that
    # closed the hole by breaking the append path would not have closed anything.
    assert chain_anchor_of(connection)["length"] == 3
    assert verify_chain_in_db(connection)["ok"] is True
    append_event(connection, observation_event(3))
    assert chain_anchor_of(connection)["length"] == 4
    assert verify_chain_in_db(connection)["ok"] is True


@pytest.mark.docker
def test_the_anchor_guards_delete_arm_refuses_the_genesis_row_of_an_empty_chain(
    ledger_clean,
) -> None:
    """The seed row is not deletable either, and "the chain is empty" is not an exception.

    ``length = 0`` is the one state in which deleting the anchor looks harmless -- there is
    nothing yet to truncate. It is not: the row is what the very next append's
    ``UPDATE ... WHERE chain = 'commerce_events'`` has to find, and without it that append is
    refused outright (see the attack-(C) test above). An arm narrowed to "refuse DELETE only
    when the chain has rows" would leak exactly here.
    """
    connection = ledger_clean
    assert chain_anchor_of(connection)["length"] == 0

    with psycopg.connect(_admin_dsn(connection), connect_timeout=5) as owner:
        try:
            _anchor_delete_is_refused_by_the_trigger(owner, "delete from ledger.chain_head")
        finally:
            owner.rollback()

    assert chain_anchor_of(connection)["head_hash"] == GENESIS_HASH
    assert append_event(connection, observation_event(0)).inserted is True


@pytest.mark.docker
def test_pg_trigger_records_a_delete_arm_for_the_chain_head_guard(ledger_clean) -> None:
    """W2-04 acceptance 2: the arm asserted in the catalog, not only in its behaviour.

    The behavioural test above needs a live DELETE to be refused; this one reads the arm
    straight out of ``pg_trigger``, so it stays a regression test even for a future refactor
    that moves the refusal elsewhere but leaves the trigger declaration behind. The two
    oracles are independent: ``tgtype`` is the bitmask the planner fires from, and
    ``information_schema.triggers`` is a separate view over the same catalog row.
    """
    connection = ledger_clean
    with connection.cursor() as cur:
        cur.execute(
            "select t.tgtype, t.tgenabled, pg_get_triggerdef(t.oid) "
            "  from pg_trigger t "
            "  join pg_class c on c.oid = t.tgrelid "
            "  join pg_namespace n on n.oid = c.relnamespace "
            " where n.nspname = 'ledger' and c.relname = 'chain_head' "
            "   and t.tgname = 'chain_head_guard_trigger'"
        )
        rows = cur.fetchall()
    assert len(rows) == 1, "chain_head_guard_trigger is not installed on ledger.chain_head"
    tgtype, tgenabled, definition = rows[0]

    assert tgtype & TRIGGER_TYPE_DELETE, (
        f"pg_trigger.tgtype = {tgtype} for chain_head_guard_trigger: the DELETE bit "
        f"({TRIGGER_TYPE_DELETE}) is CLEAR, so the guard is never consulted on a DELETE and "
        f"the anchor -- the ledger's only truncation detector -- can simply be removed."
    )
    # ...and the other arms are still there, so this cannot be satisfied by widening the
    # trigger into something that no longer guards INSERT or UPDATE.
    assert tgtype == (
        TRIGGER_TYPE_ROW
        | TRIGGER_TYPE_BEFORE
        | TRIGGER_TYPE_INSERT
        | TRIGGER_TYPE_UPDATE
        | TRIGGER_TYPE_DELETE
    ), f"expected a BEFORE INSERT OR UPDATE OR DELETE ... FOR EACH ROW trigger, got {tgtype}"
    assert not tgtype & TRIGGER_TYPE_TRUNCATE
    assert not tgtype & TRIGGER_TYPE_INSTEAD
    # T-114 acceptance 1. This was `== "O"`, which encoded "enabled, not left disabled by a
    # test" but spelled it as the WEAKER of the two enabled states: 'O' fires in origin and
    # local mode only, so `SET session_replication_role = 'replica'` -- one statement, no
    # DDL, superuser-only and therefore available to exactly the principal this whole block
    # exists to grade -- skipped the arm entirely and `DELETE FROM ledger.chain_head`
    # succeeded with rowcount 1. The assertion that said "enabled" was the assertion that
    # made the one-statement fix red. 'A' is ENABLE ALWAYS: strictly stronger, still fails
    # on 'D' (the regression the original wording was after), and now the only state the
    # migration produces.
    assert tgenabled == "A", (
        f"chain_head_guard_trigger is {tgenabled!r}, not ENABLE ALWAYS. 'D' means a test "
        f"left the anchor disarmed for everything that runs after it; 'O' means a superuser "
        f"can skip the arm with SET session_replication_role = 'replica' and delete the "
        f"ledger's only truncation detector without touching the catalog at all."
    )
    assert "DELETE" in definition and "BEFORE" in definition, definition

    with connection.cursor() as cur:
        cur.execute(
            "select event_manipulation, action_timing, action_orientation "
            "  from information_schema.triggers "
            " where trigger_schema = 'ledger' and trigger_name = 'chain_head_guard_trigger'"
        )
        arms = cur.fetchall()
    assert {row[0] for row in arms} == {"INSERT", "UPDATE", "DELETE"}
    assert {row[1] for row in arms} == {"BEFORE"}
    assert {row[2] for row in arms} == {"ROW"}


# =======================================================================================
# T-114 -- the arms are not switchable off by a session GUC, and no DELETE shape escapes
# =======================================================================================
# Two holes in the block above, both measured in this tree (worker 22) before this section
# existed.
#
# 1. `CREATE TRIGGER` installs a trigger at `pg_trigger.tgenabled = 'O'`, and 'O' does not
#    mean "enabled" -- it means "fires in origin and local mode". A session in REPLICA mode
#    skips every 'O' trigger on the table, and `session_replication_role` is a plain `SET`:
#    no DDL, no lock, no catalog change, nothing for the catalog test above to see, and
#    available to any superuser -- which is the table owner, the one principal the arm
#    exists to stop. Verbatim, on a three-event chain:
#
#        SET session_replication_role = 'replica';
#        DELETE FROM ledger.chain_head;      -- SUCCEEDED, rowcount = 1
#
#    and the same switch turns off the append-only arm, the prev_hash link check and the
#    anchor advance with it. `ENABLE ALWAYS` (tgenabled = 'A') is the fix; these are the
#    tests that grade it rather than the catalog row.
#
# 2. The two behavioural tests above drive `delete from ledger.chain_head` and its
#    `where chain = 'commerce_events'` twin. Both are simple-protocol single-table DELETEs
#    from one connection, so an exemption written INSIDE the DELETE branch --
#    `IF current_setting('...') THEN RETURN OLD`, a ctid or subquery shape the author did
#    not think of, a DELETE reached through a CTE or a plpgsql block -- would pass every
#    assertion there is. The matrix below drives ten spellings of "remove the anchor row"
#    in both replication roles; they differ in the ways such an exemption would plausibly
#    key on, and all twenty must arrive at the same arm with the same message.

#: The append-only arm's own words, and the link check's. Asserted verbatim for the same
#: reason ``ANCHOR_DELETE_REFUSAL`` is: any other refusal is not this one.
APPEND_ONLY_DELETE_REFUSAL = "ledger.commerce_events is append-only: DELETE is not permitted"
APPEND_ONLY_UPDATE_REFUSAL = "ledger.commerce_events is append-only: UPDATE is not permitted"
CHAIN_LINK_REFUSAL = "does not link to the chain tail"
ANCHOR_LENGTH_REFUSAL = "advances by exactly one per appended event"

#: Ten ways to say "delete the anchor row". Every one reaches the same FOR EACH ROW arm.
ANCHOR_DELETE_SHAPES: tuple[tuple[str, str], tuple[str, str], ...] = (
    ("bare", "delete from ledger.chain_head"),
    ("by discriminator", "delete from ledger.chain_head where chain = 'commerce_events'"),
    (
        "by ctid",
        "delete from ledger.chain_head where ctid = (select ctid from ledger.chain_head limit 1)",
    ),
    ("by a column no arm reads", "delete from ledger.chain_head where length >= 0"),
    ("only", "delete from only ledger.chain_head"),
    (
        "aliased, with USING",
        "delete from ledger.chain_head as ch "
        "using (select 'commerce_events'::text as c) s where ch.chain = s.c",
    ),
    ("returning", "delete from ledger.chain_head returning chain, head_hash"),
    (
        "inside a data-modifying CTE",
        "with gone as (delete from ledger.chain_head returning chain) select count(*) from gone",
    ),
    (
        "by subquery on itself",
        "delete from ledger.chain_head where chain in (select chain from ledger.chain_head)",
    ),
    ("inside a plpgsql block", "do $$ begin delete from ledger.chain_head; end $$"),
)


def _refused_by_a_ledger_trigger(
    owner,
    statement: str,
    expected: str,
    *,
    params: tuple | None = None,
    replication_role: str = "origin",
    label: str = "",
) -> None:
    """Assert ``statement`` is refused by a ledger integrity trigger, verbatim.

    ``replication_role`` is set inside the same transaction as the statement and read back
    before the statement runs. Reading it back is not decoration: a ``SET`` that silently
    did nothing would make every "still refused in replica mode" assertion here vacuous,
    and a vacuous assertion about a bypass is worse than none.

    Rolls back on every path, so a run in which the arm is missing and the statement
    therefore SUCCEEDS still leaves the ledger where it was for the tests that follow.
    """
    where = f" [{label}]" if label else ""
    rowcount: int | None = None
    try:
        with owner.cursor() as cur:
            cur.execute(f"set local session_replication_role = '{replication_role}'")  # noqa: S608
            cur.execute("show session_replication_role")
            assert cur.fetchone() == (replication_role,), (
                f"session_replication_role did not take{where}: this assertion would have "
                f"proved nothing about the bypass it exists to close"
            )
            cur.execute(statement, params)
            rowcount = cur.rowcount
    except psycopg.errors.IntegrityConstraintViolation as exc:
        assert expected in str(exc), (
            f"{statement!r}{where} was refused in {replication_role} mode, but not by the "
            f"arm this asserts ({expected!r}): {exc}"
        )
        assert exc.sqlstate == "23000", (
            f"the arm raises USING ERRCODE = 'integrity_constraint_violation'; callers "
            f"discriminate on that, and this arrived as {exc.sqlstate}{where}"
        )
    except psycopg.Error as exc:
        raise AssertionError(
            f"{statement!r}{where} failed with {type(exc).__name__} rather than the ledger "
            f"arm that raises {expected!r}: {exc}. A refusal from somewhere else does not "
            f"grade this arm."
        ) from exc
    else:
        raise AssertionError(
            f"{statement!r}{where} SUCCEEDED (rowcount={rowcount}) with "
            f"session_replication_role = '{replication_role}'. The arm did not fire."
        )
    finally:
        owner.rollback()


@pytest.mark.docker
@pytest.mark.parametrize(("label", "statement"), ANCHOR_DELETE_SHAPES)
def test_no_spelling_of_deleting_the_anchor_gets_past_the_guard(
    ledger_clean, label: str, statement: str
) -> None:
    """T-114 acceptance 3: ten statement shapes, both replication roles, one arm.

    The shapes are not decoration. A conditional exemption written inside the DELETE branch
    -- the kind of edit that "fixes" an inconvenient failure -- survives a test that only
    ever sends one statement. It does not survive a bare DELETE, a discriminator predicate,
    a ctid lookup, a predicate on a column no arm reads, ONLY, an alias with USING,
    RETURNING, a data-modifying CTE, a self-referencing subquery and a plpgsql block all
    arriving at the same refusal with the same SQLSTATE.
    """
    connection = ledger_clean
    for index in range(3):
        append_event(connection, observation_event(index))

    with psycopg.connect(_admin_dsn(connection), connect_timeout=5) as owner:
        try:
            for role in ("origin", "replica"):
                _refused_by_a_ledger_trigger(
                    owner,
                    statement,
                    ANCHOR_DELETE_REFUSAL,
                    replication_role=role,
                    label=f"{label} / {role}",
                )
        finally:
            owner.rollback()

    # The chain is untouched and still appendable: a guard that closed the hole by breaking
    # the writer would not have closed anything.
    assert chain_anchor_of(connection)["length"] == 3
    assert verify_chain_in_db(connection)["ok"] is True


@pytest.mark.docker
def test_the_anchor_guard_refuses_a_parameterised_delete_too(ledger_clean) -> None:
    """The extended query protocol is a different code path from the shapes above.

    psycopg sends a literal statement over the simple protocol and a parameterised one over
    Parse/Bind/Execute. Same arm either way -- asserted rather than assumed, because "it
    only fires for literals" is exactly the sort of thing nobody would think to check.
    """
    connection = ledger_clean
    append_event(connection, observation_event(0))
    with psycopg.connect(_admin_dsn(connection), connect_timeout=5) as owner:
        try:
            for role in ("origin", "replica"):
                _refused_by_a_ledger_trigger(
                    owner,
                    "delete from ledger.chain_head where chain = %s",
                    ANCHOR_DELETE_REFUSAL,
                    params=("commerce_events",),
                    replication_role=role,
                    label=f"parameterised / {role}",
                )
        finally:
            owner.rollback()
    assert chain_anchor_of(connection)["length"] == 1


@pytest.mark.docker
def test_a_replica_session_replication_role_cannot_skip_the_ledger_integrity_triggers(
    ledger_clean,
) -> None:
    """T-114 acceptance 2: the GUC bypass, driven arm by arm.

    ``session_replication_role = 'replica'`` was a complete disarm of the ledger's
    integrity layer for anyone who could set it, and only a superuser can -- which is the
    table owner, the principal every arm here exists to stop, and the principal no grant
    reaches. Five triggers, five attacks, and the last one is a positive: the anchor
    ADVANCE trigger has to keep firing in replica mode too, or an append in that mode
    commits unanchored and the ledger loses the ability to detect its own truncated tail.
    """
    connection = ledger_clean
    for index in range(3):
        append_event(connection, observation_event(index))
    with connection.cursor() as cur:
        cur.execute("select seq, event_hash from ledger.commerce_events order by seq desc limit 1")
        tail_seq, tail_hash = cur.fetchone()

    insert_event = (
        "insert into ledger.commerce_events "
        "  (idempotency_key, kind, store_id, occurred_at, payload, prev_hash, event_hash) "
        "values (%s, 'claim_verified', 's-1', %s, %s::jsonb, %s, %s)"
    )

    with psycopg.connect(_admin_dsn(connection), connect_timeout=5) as owner:
        try:
            # (C) the anchor deleted outright -- the ledger's only truncation detector.
            _refused_by_a_ledger_trigger(
                owner,
                "delete from ledger.chain_head",
                ANCHOR_DELETE_REFUSAL,
                replication_role="replica",
                label="chain_head_guard / DELETE",
            )
            # (B) denial-of-integrity: a flawless ledger made to report itself truncated.
            _refused_by_a_ledger_trigger(
                owner,
                "update ledger.chain_head set length = 99 where chain = 'commerce_events'",
                ANCHOR_LENGTH_REFUSAL,
                replication_role="replica",
                label="chain_head_guard / UPDATE",
            )
            # The tail cut off row by row.
            _refused_by_a_ledger_trigger(
                owner,
                "delete from ledger.commerce_events where seq > 1",
                APPEND_ONLY_DELETE_REFUSAL,
                replication_role="replica",
                label="append_only / DELETE",
            )
            # A written event rewritten in place.
            _refused_by_a_ledger_trigger(
                owner,
                "update ledger.commerce_events set payload = '{\"x\": 1}'::jsonb where seq = 1",
                APPEND_ONLY_UPDATE_REFUSAL,
                replication_role="replica",
                label="append_only / UPDATE",
            )
            # A forked chain: an event whose prev_hash links to nothing.
            _refused_by_a_ledger_trigger(
                owner,
                insert_event,
                CHAIN_LINK_REFUSAL,
                params=("ev-forked", AS_OF, "{}", "a" * 64, "b" * 64),
                replication_role="replica",
                label="chain_guard / INSERT",
            )

            # ...and the positive: a well-linked append in replica mode still advances the
            # anchor. With the trigger at 'O' this INSERT succeeded and the anchor did not
            # move -- an unanchored append, which is attack (C) reached from the other side.
            with owner.cursor() as cur:
                cur.execute("set local session_replication_role = 'replica'")
                cur.execute("show session_replication_role")
                assert cur.fetchone() == ("replica",)
                cur.execute(
                    insert_event + " returning seq",
                    ("ev-replica", AS_OF, "{}", tail_hash, "f" * 64),
                )
                # Read the seq back rather than predicting it: `bigserial` is not
                # transactional, so the refused INSERTs above consumed sequence values.
                (new_seq,) = cur.fetchone()
                assert new_seq > tail_seq
                cur.execute(
                    "select head_hash, length, last_seq from ledger.chain_head "
                    " where chain = 'commerce_events'"
                )
                assert cur.fetchone() == ("f" * 64, 4, new_seq), (
                    "an append made with session_replication_role = 'replica' did not move "
                    "the anchor: commerce_events_advance_anchor_trigger was skipped, so the "
                    "chain now has an event the anchor does not commit to and truncating "
                    "back to it is undetectable"
                )
        finally:
            owner.rollback()

    assert chain_anchor_of(connection)["length"] == 3
    assert verify_chain_in_db(connection)["ok"] is True


@pytest.mark.docker
def test_every_ledger_integrity_trigger_is_installed_enable_always(ledger_clean) -> None:
    """T-114 acceptance 1 and 2, at the catalog, for all five arms rather than one.

    The behavioural tests above drive the bypass; this one is the regression guard that
    survives a refactor moving a refusal elsewhere. The trigger NAMES are pinned as a set
    as well as their state, so a sixth trigger added later at the default ``'O'`` fails
    here and has to be a decision rather than an oversight.
    """
    connection = ledger_clean
    with connection.cursor() as cur:
        cur.execute(
            "select ns.nspname || '.' || cl.relname || '.' || t.tgname, t.tgenabled "
            "  from pg_trigger t "
            "  join pg_class cl on cl.oid = t.tgrelid "
            "  join pg_namespace ns on ns.oid = cl.relnamespace "
            " where ns.nspname = 'ledger' and not t.tgisinternal"
        )
        installed = dict(cur.fetchall())

    assert set(installed) == {
        "ledger.chain_head.chain_head_guard_trigger",
        "ledger.commerce_events.commerce_events_advance_anchor_trigger",
        "ledger.commerce_events.commerce_events_append_only_trigger",
        "ledger.commerce_events.commerce_events_chain_guard_trigger",
        "ledger.commerce_events.commerce_events_reset_anchor_trigger",
    }, sorted(installed)

    not_always = sorted(name for name, state in installed.items() if state != "A")
    assert not_always == [], (
        f"{not_always} are not ENABLE ALWAYS. tgenabled 'O' fires in origin and local mode "
        f"only, so `SET session_replication_role = 'replica'` -- one statement, no DDL, no "
        f"catalog change, superuser-only and therefore available to the table owner -- "
        f"skips them. Measured with all five at 'O': DELETE FROM ledger.chain_head "
        f"succeeded with rowcount 1."
    )


@pytest.mark.docker
def test_the_anchor_guard_does_not_depend_on_who_is_connected_or_how(ledger_clean) -> None:
    """The other half of "one fixed statement shape from ONE connection".

    A conditional exemption does not have to key on the statement. ``current_user``,
    ``application_name``, ``pg_backend_pid()``, ``inet_client_addr()`` and any
    ``current_setting('...')`` are all in reach of a plpgsql trigger, and every test in this
    block until now drove the arm from one admin connection with default session settings --
    so an exemption written against connection-level state would have gone straight through
    the whole file. Three connections, three identities, one arm.

    The DELETE arm is unconditional. There is no legitimate delete of the anchor row from
    any session, so there is nothing here that a session can say about itself that should
    change the answer.
    """
    connection = ledger_clean
    for index in range(2):
        append_event(connection, observation_event(index))

    dsn = _admin_dsn(connection)
    identities = (
        ("default", {}, ()),
        ("named application", {"application_name": "proxyshop-t114-probe"}, ()),
        (
            "custom GUCs set",
            {"application_name": "pg_dump"},
            (
                "set local proxyshop.maintenance = 'on'",
                "set local statement_timeout = '30s'",
                "set local search_path to ledger, public",
            ),
        ),
    )

    pids = set()
    for label, kwargs, prelude in identities:
        with psycopg.connect(dsn, connect_timeout=5, **kwargs) as owner:
            try:
                with owner.cursor() as cur:
                    for statement in prelude:
                        cur.execute(statement)
                    cur.execute("select pg_backend_pid()")
                    pids.add(cur.fetchone()[0])
                    cur.execute(
                        "select has_table_privilege(current_user, 'ledger.chain_head', 'DELETE')"
                    )
                    assert cur.fetchone() == (True,), label
                    # The prelude has to survive into the statement's own transaction, so it
                    # is re-run there rather than trusted to persist across the rollback.
                    for statement in prelude:
                        cur.execute(statement)
                    try:
                        cur.execute("delete from ledger.chain_head")
                    except psycopg.errors.IntegrityConstraintViolation as exc:
                        assert ANCHOR_DELETE_REFUSAL in str(exc), f"{label}: {exc}"
                    else:
                        raise AssertionError(
                            f"[{label}] deleting the anchor SUCCEEDED "
                            f"(rowcount={cur.rowcount}). The arm is conditional on something "
                            f"about the session, which is not a distinction it is allowed to "
                            f"make: there is no legitimate DELETE of this row."
                        )
            finally:
                owner.rollback()

    assert len(pids) == 3, f"the three probes shared a backend: {pids}"
    assert chain_anchor_of(connection)["length"] == 2
    assert verify_chain_in_db(connection)["ok"] is True


# =======================================================================================
# T-127 -- the DELETE arm graded as a PROPERTY, and the trigger state named correctly
# =======================================================================================
# T-114 closed the statement-shape half of "one fixed statement from one connection"
# properly: ten spellings of "remove the anchor row" in two replication roles, twenty
# assertions, one arm. It closed the session-state half by ENUMERATING three identities --
# a default connection, one with an application_name, one with three GUCs set. Three is a
# list, not a property, and the hole a list leaves is the whole point of the exercise: an
# exemption keyed on any state nobody listed goes straight through.
#
# Measured in this tree, worker 52, on the migration as it stands. Inserted as the first
# statement of `ledger.chain_head_guard()`'s DELETE branch:
#
#     IF current_setting('proxyshop.anchor_maintenance', true) = 'on' THEN
#       RETURN OLD;
#     END IF;
#
# Then, on the live schema:
#
#     SET LOCAL proxyshop.anchor_maintenance = 'on';
#     DELETE FROM ledger.chain_head;      -- SUCCEEDED, rowcount = 1
#
# and `pytest apps/trust -q` reported `197 passed`. The GUC name is one character-class
# away from the `proxyshop.maintenance` the identity test happens to set, which is exactly
# how much distance an enumeration needs to be useless.
#
# You cannot enumerate session state. `current_user`, `application_name`,
# `pg_backend_pid()`, `inet_client_addr()`, `txid_current()`, `now()` and every
# `current_setting('<anything>')` are all in reach of a plpgsql trigger, and the last of
# those is an unbounded namespace. So the assertion below is not about states at all: it is
# that the DELETE branch CONTAINS NO CODE that could read one. The refusal is the first
# thing the function does and the only thing that branch does.


def _chain_head_guard_body(connection: Any) -> tuple[str, str]:
    """The source of the function ``chain_head_guard_trigger`` actually calls.

    Read from ``pg_proc`` through ``pg_trigger.tgfoid`` rather than by name, so pointing the
    trigger at a differently-named copy does not route around this. Returns
    ``(declare_section, body)``, both with ``--`` comments removed and whitespace collapsed.

    Comment removal is what lets the RAISE carry an annotation without turning this red;
    it cannot hide anything, because a comment does not execute and everything outside one
    still has to match the expected text exactly.
    """
    import re

    with connection.cursor() as cur:
        cur.execute(
            "select p.prosrc, t.tgqual is null, ns.nspname || '.' || p.proname "
            "  from pg_trigger t "
            "  join pg_class cl on cl.oid = t.tgrelid "
            "  join pg_namespace cn on cn.oid = cl.relnamespace "
            "  join pg_proc p on p.oid = t.tgfoid "
            "  join pg_namespace ns on ns.oid = p.pronamespace "
            " where cn.nspname = 'ledger' and cl.relname = 'chain_head' "
            "   and t.tgname = 'chain_head_guard_trigger'"
        )
        rows = cur.fetchall()

    assert len(rows) == 1, "chain_head_guard_trigger is not installed on ledger.chain_head"
    prosrc, has_no_when_clause, proname = rows[0]

    # A WHEN clause is a conditional exemption the function body cannot see: Postgres
    # evaluates it BEFORE calling the trigger function, it may call any function it likes
    # (`current_setting`, `current_user`, ...), and a body that is unconditional inside is
    # simply never reached. Everything below reads the body, so this has to be closed here.
    assert has_no_when_clause, (
        "chain_head_guard_trigger carries a WHEN clause. That is evaluated before the "
        "trigger function runs and can key on any session state there is, so the "
        "unconditional body asserted below would never be consulted."
    )
    assert proname == "ledger.chain_head_guard", proname

    without_comments = re.sub(r"--[^\n]*", " ", prosrc)
    normalised = " ".join(without_comments.split())

    # The split is ANCHORED AT THE START of the source. It used to be
    # `normalised.partition(" BEGIN ")`, which takes the FIRST ` BEGIN ` anywhere in the
    # function -- and that is not the outermost one. A function with no top-level DECLARE
    # section opens with `BEGIN`, which cannot match a delimiter that requires a leading
    # space, so the outer block header was swallowed into the declare half (it came out as
    # the literal 'BEGIN ...', which passes the ":=" check below because there is no
    # initialiser in it) and `body` was read from the NESTED block instead. The whole
    # unconditional DELETE arm can then sit verbatim inside an inner block with the
    # exemption written ahead of it in the OUTER one, and the property passes. Measured on
    # the live catalog, worker 52, in a rolled-back transaction:
    #
    #     declare = "BEGIN IF current_setting('proxyshop.anchor_maintenance', true) = 'on'
    #                AND TG_OP = 'DELETE' THEN RETURN OLD; END IF; DECLARE chain_is_empty
    #                boolean;"
    #     body    = "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'ledger.chain_head is the ..."
    #     SET LOCAL proxyshop.anchor_maintenance = 'on';
    #     DELETE FROM ledger.chain_head;      -- SUCCEEDED, rowcount = 1
    #
    # and the property test PASSED -- the conditional-exemption hole it exists to close.
    # `test_a_nested_block_cannot_hide_the_delete_arm_from_the_property` installs exactly
    # that function and requires this helper to refuse it.
    #
    # So the shape is decided by what the source STARTS with, not by where a delimiter
    # happens to fall, and anything that is neither shape is a failure rather than a guess.
    if normalised.startswith("DECLARE "):
        declare, separator, body = normalised.partition(" BEGIN ")
        assert separator, f"chain_head_guard has a DECLARE and no BEGIN: {normalised!r}"
        assert "BEGIN" not in declare, (
            f"chain_head_guard's declare section appears to contain the word BEGIN, so the "
            f"split above may not have found the OUTERMOST block header and the body below "
            f"may not be the outer body: {declare!r}"
        )
    elif normalised.startswith("BEGIN "):
        # No declare section at all: the body is everything after the outer block header,
        # nested blocks included. A nested `BEGIN` is then part of `body`, where the
        # assertion that the DELETE arm comes FIRST can see it.
        declare, body = "", normalised[len("BEGIN ") :]
    else:
        raise AssertionError(
            f"chain_head_guard's source opens with neither DECLARE nor BEGIN, so this test "
            f"cannot tell its declare section from its body. Refusing rather than guessing: "
            f"a guess is how the outer block came to be read as a declare section in the "
            f"first place. Source: {normalised!r}"
        )
    return declare, body


#: The DELETE branch, whole, as the migration writes it. Built from
#: :data:`ANCHOR_DELETE_REFUSAL` rather than typed a second time, so the message stays one
#: fact: the behavioural tests match it in the exception, this one matches it in the source.
UNCONDITIONAL_DELETE_BRANCH = (
    "IF TG_OP = 'DELETE' THEN "
    "RAISE EXCEPTION '{message}' "
    "USING ERRCODE = 'integrity_constraint_violation'; "
    "END IF;"
).format(message=ANCHOR_DELETE_REFUSAL.replace("'", "''"))


@pytest.mark.docker
def test_the_anchor_guards_delete_arm_is_unconditional_as_a_property(ledger_clean) -> None:
    """T-127 acceptance 1: the arm asserted unconditional, not sampled over three sessions.

    ``test_the_anchor_guard_does_not_depend_on_who_is_connected_or_how`` drives three
    connection identities. Three is a sample. This is the property those three were a
    sample OF, and it is stated once, over the function's own source as the live catalog
    holds it:

        the first thing ``chain_head_guard()`` does is test ``TG_OP = 'DELETE'``, and the
        whole of that branch is the RAISE.

    Everything an exemption could key on -- ``current_user``, ``application_name``,
    ``pg_backend_pid()``, ``inet_client_addr()``, any ``current_setting('...')``, or
    nothing at all -- has to appear as CODE inside that branch or ahead of it, and there is
    room for neither. So this refuses every exemption without knowing what any of them key
    on, which is the difference between a property and a list.

    Read from the live database rather than from ``db/migrations/0002_ledger_tables.sql``
    on purpose: what guards the anchor is the function that is installed, and
    ``CREATE OR REPLACE FUNCTION`` needs no migration. The trigger's own ``WHEN`` clause is
    closed in the helper, because a condition there is never visible in the body at all.
    """
    declare, body = _chain_head_guard_body(ledger_clean)

    assert body.startswith(UNCONDITIONAL_DELETE_BRANCH), (
        "the DELETE arm of ledger.chain_head_guard() is not the unconditional first "
        "statement of the function any more.\n"
        f"expected the body to start with:\n  {UNCONDITIONAL_DELETE_BRANCH}\n"
        f"got:\n  {body[: len(UNCONDITIONAL_DELETE_BRANCH) + 200]}\n"
        "Anything between BEGIN and the RAISE -- a guard clause, an early RETURN, an added "
        "conjunct on the IF, a CASE -- is a session-conditional exemption, and there is no "
        "session state that is allowed to make deleting the anchor legitimate."
    )

    # ...and nothing runs on the way in, either. A DECLARE with an initialiser executes
    # before the first statement of the body, so a `maintenance boolean := current_setting(
    # 'proxyshop.x', true) = 'on'` would run ahead of everything asserted above.
    assert ":=" not in declare and "DEFAULT" not in declare.upper(), (
        f"ledger.chain_head_guard() declares an initialised variable: {declare!r}. That "
        f"expression runs before the DELETE arm, so it can read session state the arm is "
        f"asserted not to read."
    )


#: Exemptions to inject into the DELETE branch, each keyed on something the enumerated
#: identity test cannot cover. ``(label, session prelude, plpgsql condition)``.
DELETE_ARM_EXEMPTIONS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "a custom GUC nobody listed",
        ("set local proxyshop.anchor_maintenance = 'on'",),
        "current_setting('proxyshop.anchor_maintenance', true) = 'on'",
    ),
    (
        "the connected role",
        (),
        "current_user = session_user",
    ),
    (
        "an application_name nobody listed",
        ("set local application_name = 'pg_restore'",),
        "current_setting('application_name') = 'pg_restore'",
    ),
    (
        "the backend pid -- unenumerable by construction",
        (),
        "pg_backend_pid() = pg_backend_pid()",
    ),
    (
        "nothing at all",
        (),
        "true",
    ),
)


def _replace_chain_head_guard_delete_branch(cursor: Any, condition: str) -> None:
    """Rewrite ``ledger.chain_head_guard()``'s DELETE branch to exempt ``condition``.

    Only the DELETE branch changes; every other arm is copied through verbatim, so the
    UPDATE and INSERT rules -- and therefore the rest of this file -- keep working and the
    only thing under test is the exemption.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    migration = (repo_root / "db" / "migrations" / "0002_ledger_tables.sql").read_text()
    start = migration.index("CREATE OR REPLACE FUNCTION ledger.chain_head_guard()")
    end = migration.index("$head_guard$;", start) + len("$head_guard$;")
    definition = migration[start:end]

    original = (
        "  IF TG_OP = 'DELETE' THEN\n"
        "    RAISE EXCEPTION\n"
        "      'ledger.chain_head is the ledger''s only truncation detector: "
        "DELETE is not permitted'\n"
        "      USING ERRCODE = 'integrity_constraint_violation';\n"
        "  END IF;\n"
    )
    assert original in definition, (
        "the DELETE branch is not where this test expects it in "
        "db/migrations/0002_ledger_tables.sql; the injection below would be a no-op, and a "
        "no-op sabotage proves nothing"
    )
    exempted = (
        "  IF TG_OP = 'DELETE' THEN\n"
        f"    IF {condition} THEN\n"
        "      RETURN OLD;\n"
        "    END IF;\n"
        "    RAISE EXCEPTION\n"
        "      'ledger.chain_head is the ledger''s only truncation detector: "
        "DELETE is not permitted'\n"
        "      USING ERRCODE = 'integrity_constraint_violation';\n"
        "  END IF;\n"
    )
    cursor.execute(definition.replace(original, exempted))


@pytest.mark.docker
@pytest.mark.parametrize(("label", "prelude", "condition"), DELETE_ARM_EXEMPTIONS)
def test_an_exemption_injected_into_the_delete_branch_turns_this_file_red(
    ledger_clean, label: str, prelude: tuple[str, ...], condition: str
) -> None:
    """T-127 acceptance 3: the property's blast radius, driven rather than argued.

    A guard that cannot refuse anything is dead code, and the only way to know which kind
    this is, is to hand it something it must refuse. So the exemption is really installed,
    on the live schema, inside a transaction that is always rolled back -- ``CREATE OR
    REPLACE FUNCTION`` is transactional, which is what makes this safe to do in-suite
    rather than by hand in a commit message.

    Two halves, and both are load-bearing:

    * the exemption WORKS. The identical ``DELETE FROM ledger.chain_head`` that is refused
      everywhere else in this file succeeds, rowcount 1, with the anchor gone. Without this
      the test could pass over an injection that changed nothing.
    * the property test REFUSES it. Whatever it keys on -- a GUC name nobody wrote down,
      the connected role, an application_name, the backend pid, or nothing at all -- the
      body no longer starts with the unconditional branch.

    The transaction is rolled back on every path and the function is re-read afterwards, so
    a leak is a failure here rather than a silently disarmed anchor for the rest of the
    session.
    """
    connection = ledger_clean
    for index in range(3):
        append_event(connection, observation_event(index))

    before_declare, before_body = _chain_head_guard_body(connection)

    with psycopg.connect(_admin_dsn(connection), connect_timeout=5) as probe:
        try:
            with probe.cursor() as cur:
                for statement in prelude:
                    cur.execute(statement)
                _replace_chain_head_guard_delete_branch(cur, condition)

                cur.execute("delete from ledger.chain_head")
                assert cur.rowcount == 1, (
                    f"[{label}] the injected exemption did not actually let the DELETE "
                    f"through (rowcount={cur.rowcount}), so this proves nothing about what "
                    f"the property test can refuse."
                )
                cur.execute("select count(*) from ledger.chain_head")
                assert cur.fetchone() == (0,), f"[{label}] the anchor row survived"

            # ...and the property test says no. Called on the same open transaction, where
            # the exemption is the installed definition.
            with pytest.raises(AssertionError) as refused:
                test_the_anchor_guards_delete_arm_is_unconditional_as_a_property(probe)
            assert "not the unconditional first statement" in str(refused.value), (
                f"[{label}] the property test failed for some other reason: {refused.value}"
            )
        finally:
            probe.rollback()

    after_declare, after_body = _chain_head_guard_body(connection)
    assert (after_declare, after_body) == (before_declare, before_body), (
        f"[{label}] the injected exemption OUTLIVED its transaction: the anchor guard is "
        f"now disarmed for every test that runs after this one"
    )
    test_the_anchor_guards_delete_arm_is_unconditional_as_a_property(connection)
    _anchor_delete_is_refused_by_the_trigger(connection, "delete from ledger.chain_head")


# ---------------------------------------------------------------------------------------
# The exemption the FIRST version of this property could not see: a nested block
# ---------------------------------------------------------------------------------------
# Every exemption above is injected INTO the DELETE branch, so the branch itself changes
# and any reading of the body catches it. An exemption does not have to go there. plpgsql
# blocks nest, and the arm can be moved wholesale into an inner one with the exemption
# written ahead of it in the outer block -- the DELETE branch is then byte-identical to the
# migration's, and only the block STRUCTURE has changed.
#
# `_chain_head_guard_body` split the source with `normalised.partition(" BEGIN ")`, which
# takes the first ` BEGIN ` anywhere in the function rather than the outermost one, so it
# read the inner block as the body and the outer one as the declare section. Measured on
# the live catalog with the function below installed: the property test PASSED and
# `DELETE FROM ledger.chain_head` succeeded with rowcount 1.
#
# Both shapes are driven, because the two halves of the old split failed differently: with
# no top-level DECLARE the outer `BEGIN` was swallowed into the declare half, and with one
# kept the outer body was skipped past. The fixed helper anchors the split at the START of
# the source, so a nested block is part of the body and the "the DELETE arm comes FIRST"
# assertion sees whatever precedes it.

#: The exemption written into the OUTER block. Deliberately the same GUC as the first entry
#: of :data:`DELETE_ARM_EXEMPTIONS`: what changes here is where it is written, not what it
#: keys on, so a pass here could not be explained by the condition being an easier one.
NESTED_BLOCK_EXEMPTION = "current_setting('proxyshop.anchor_maintenance', true) = 'on'"


def _chain_head_guard_source() -> tuple[str, str, str]:
    """``ledger.chain_head_guard()`` split into ``(header, declare_section, block)``.

    ``header`` is everything up to and including the opening ``$head_guard$`` dollar quote,
    ``declare_section`` is the top-level ``DECLARE ...`` the migration writes, and ``block``
    is the ``BEGIN ... END;`` that follows it. Read out of the migration so the statements
    inside the block are the real ones and the only thing this file invents is the nesting.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    migration = (repo_root / "db" / "migrations" / "0002_ledger_tables.sql").read_text()
    start = migration.index("CREATE OR REPLACE FUNCTION ledger.chain_head_guard()")
    end = migration.index("$head_guard$;", start)
    opener = "AS $head_guard$"
    header, _, body = migration[start:end].partition(opener)
    declare_section, separator, block = body.partition("BEGIN\n")
    assert declare_section.strip() == "DECLARE\n  chain_is_empty boolean;".strip(), (
        f"ledger.chain_head_guard()'s declare section is not what this test expects in "
        f"db/migrations/0002_ledger_tables.sql, so the nesting below would not be the "
        f"function under test: {declare_section!r}"
    )
    assert separator and block.lstrip().startswith("IF TG_OP = 'DELETE' THEN"), (
        f"the DELETE arm is not the first statement of the migration's block, so this test "
        f"would nest something other than the arm it is about: {block[:120]!r}"
    )
    return header + opener, declare_section, separator + block


def _nest_the_chain_head_guard_block(cursor: Any, *, keep_top_level_declare: bool) -> None:
    """Install ``chain_head_guard()`` with its whole block moved into a NESTED one.

    The DELETE arm, the INSERT arm and every UPDATE rule are copied through verbatim -- the
    inner block is the migration's block, unmodified -- so the only difference from the real
    function is the outer block wrapped around it and the exemption written inside that.
    """
    header, declare_section, block = _chain_head_guard_source()
    exemption = (
        f"  IF {NESTED_BLOCK_EXEMPTION} AND TG_OP = 'DELETE' THEN\n    RETURN OLD;\n  END IF;\n"
    )
    if keep_top_level_declare:
        # The declare section stays where it was, so the source still opens with DECLARE;
        # the exemption is the outer block's first statement and the arm is one block down.
        inner = "BEGIN\n" + block + "END;\n"
        nested = f"{declare_section}BEGIN\n{exemption}{inner}END;\n"
    else:
        # No top-level DECLARE at all: the source opens with the outer BEGIN, and the
        # migration's own DECLARE ... BEGIN ... END; becomes the nested block.
        nested = f"\nBEGIN\n{exemption}{declare_section}{block}END;\n"
    cursor.execute(f"{header}{nested}$head_guard$;")


@pytest.mark.docker
@pytest.mark.parametrize(
    "keep_top_level_declare",
    [False, True],
    ids=["no top-level DECLARE", "top-level DECLARE kept"],
)
def test_a_nested_block_cannot_hide_the_delete_arm_from_the_property(
    ledger_clean, keep_top_level_declare: bool
) -> None:
    """The DELETE arm verbatim, one block deeper, with the exemption above it.

    Graded exactly like the injected exemptions next door, and for the same reason: the
    exemption has to really work, or a refusal from the property test proves nothing about
    what it can refuse. So the identical ``DELETE FROM ledger.chain_head`` that is refused
    everywhere else in this file must succeed with rowcount 1 first.

    This is the input that separates "the DELETE branch is unconditional" from "the DELETE
    branch is the first thing the function does". Only the second is a property; the first
    is satisfied by a branch nothing ever reaches.
    """
    connection = ledger_clean
    for index in range(3):
        append_event(connection, observation_event(index))

    before_declare, before_body = _chain_head_guard_body(connection)

    with psycopg.connect(_admin_dsn(connection), connect_timeout=5) as probe:
        try:
            with probe.cursor() as cur:
                _nest_the_chain_head_guard_block(cur, keep_top_level_declare=keep_top_level_declare)

                # The DELETE arm is still there, character for character. If the only thing
                # that changed were the nesting, and the nesting were harmless, this would
                # be refused like every other DELETE in this file.
                _, nested_body = _chain_head_guard_body(probe)
                assert UNCONDITIONAL_DELETE_BRANCH in nested_body, (
                    "the nesting did not preserve the DELETE arm verbatim, so this test is "
                    "grading a different function than the one it claims to"
                )

                cur.execute("set local proxyshop.anchor_maintenance = 'on'")
                cur.execute("delete from ledger.chain_head")
                assert cur.rowcount == 1, (
                    f"the nested-block exemption did not actually let the DELETE through "
                    f"(rowcount={cur.rowcount}), so this proves nothing about what the "
                    f"property test can refuse."
                )
                cur.execute("select count(*) from ledger.chain_head")
                assert cur.fetchone() == (0,), "the anchor row survived"

            with pytest.raises(AssertionError) as refused:
                test_the_anchor_guards_delete_arm_is_unconditional_as_a_property(probe)
            assert "not the unconditional first statement" in str(refused.value), (
                f"the property test failed for some other reason: {refused.value}"
            )
        finally:
            probe.rollback()

    after_declare, after_body = _chain_head_guard_body(connection)
    assert (after_declare, after_body) == (before_declare, before_body), (
        "the nested-block exemption OUTLIVED its transaction: the anchor guard is now "
        "disarmed for every test that runs after this one"
    )
    test_the_anchor_guards_delete_arm_is_unconditional_as_a_property(connection)
    _anchor_delete_is_refused_by_the_trigger(connection, "delete from ledger.chain_head")


class _OneRowCatalog:
    """The smallest thing :func:`_chain_head_guard_body` can read a ``prosrc`` out of.

    Enough of psycopg's connection/cursor shape to answer the one query the helper runs, so
    the SPLIT can be graded without a database. The docker test above proves the same thing
    against the live catalog; this one keeps it graded in an environment where the compose
    stack is unreachable and the ``docker`` mark is skipped.
    """

    def __init__(self, prosrc: str) -> None:
        self._prosrc = prosrc

    def cursor(self) -> Any:
        catalog = self

        class _Cursor:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

            def execute(self, statement: str) -> None:
                assert "pg_trigger" in statement, statement

            def fetchall(self) -> list[tuple[str, bool, str]]:
                return [(catalog._prosrc, True, "ledger.chain_head_guard")]

        return _Cursor()


def test_the_body_split_takes_the_outermost_block_and_not_the_first_one() -> None:
    """The split itself, over four sources, with no database in the way.

    ``partition(" BEGIN ")`` answers the last two of these the same way it answers the
    first two, which is the whole defect: it returns the INNER block as the body.
    """
    real = "DECLARE chain_is_empty boolean; BEGIN IF TG_OP = 'DELETE' THEN RAISE; END IF; END;"
    assert _chain_head_guard_body(_OneRowCatalog(real)) == (
        "DECLARE chain_is_empty boolean;",
        "IF TG_OP = 'DELETE' THEN RAISE; END IF; END;",
    )

    no_declare = "BEGIN IF TG_OP = 'DELETE' THEN RAISE; END IF; END;"
    assert _chain_head_guard_body(_OneRowCatalog(no_declare)) == (
        "",
        "IF TG_OP = 'DELETE' THEN RAISE; END IF; END;",
    )

    # The two nested shapes. The exemption is in the OUTER block, so it must come back as
    # part of the body -- if it comes back as the declare section, or is dropped entirely,
    # the property test above cannot see it.
    nested_without_declare = (
        "BEGIN IF exempt() THEN RETURN OLD; END IF; "
        "DECLARE chain_is_empty boolean; BEGIN IF TG_OP = 'DELETE' THEN RAISE; END IF; END; END;"
    )
    declare, body = _chain_head_guard_body(_OneRowCatalog(nested_without_declare))
    assert declare == ""
    assert body.startswith("IF exempt() THEN RETURN OLD; END IF;"), body

    nested_with_declare = (
        "DECLARE chain_is_empty boolean; BEGIN IF exempt() THEN RETURN OLD; END IF; "
        "BEGIN IF TG_OP = 'DELETE' THEN RAISE; END IF; END; END;"
    )
    declare, body = _chain_head_guard_body(_OneRowCatalog(nested_with_declare))
    assert declare == "DECLARE chain_is_empty boolean;"
    assert body.startswith("IF exempt() THEN RETURN OLD; END IF;"), body

    # Neither shape: refused rather than guessed at.
    with pytest.raises(AssertionError, match="opens with neither DECLARE nor BEGIN"):
        _chain_head_guard_body(_OneRowCatalog("IF TG_OP = 'DELETE' THEN RAISE; END IF;"))


# ---------------------------------------------------------------------------------------
# T-127 acceptance 2 -- the trigger-state spelling, in the source rather than the catalog
# ---------------------------------------------------------------------------------------
# `CREATE TRIGGER` installs at tgenabled = 'O' and the migration moves all five to 'A'.
# Postgres has no "restore whatever it was" spelling, so a teardown that re-enables a
# trigger has to NAME the state, and the plain spelling names the wrong one: it silently
# downgrades the trigger to 'O', where `SET session_replication_role = 'replica'` skips it.
#
# T-114 found that, wrote it up at the teardown in
# `test_a_fork_is_refused_before_the_unique_index_is_reached`, fixed that one site -- and
# left the two in the attack-(C) and length-divergence tests spelled the dangerous way.
# `test_every_ledger_integrity_trigger_is_installed_enable_always` could not catch those:
# both sit inside transactions that are always rolled back, so the wrong state never
# reaches the catalog for a catalog test to see. It is latent, not live, and the thing that
# makes it latent is a `finally` two lines further down. The check that catches it has to
# read the SOURCE, so that is what this one does.


def _sql_statements_that_enable_a_trigger(path: Any) -> list[str]:
    """Every SQL statement in ``path`` that re-enables a trigger, whitespace-collapsed.

    Python is read through :mod:`ast`, so only real string literals are considered and
    ``#`` comments cannot trip it; SQL has its ``--`` comments stripped for the same
    reason. Adjacent-literal concatenation is resolved by the parser, so a statement split
    across source lines arrives here whole.

    A docstring that spells out a statement is scanned like any other literal. That is
    deliberate rather than a limitation: the dangerous spelling should not be written down
    as an example either.
    """
    import ast
    import re

    source = path.read_text()
    if path.suffix == ".py":
        haystacks = [
            " ".join(node.value.split())
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
    else:
        haystacks = [" ".join(re.sub(r"--[^\n]*", " ", source).split())]

    pattern = re.compile(
        r"\balter\s+table\s+[\w.\"]+\s+enable\s+(?:\w+\s+)?trigger\s+[\w.\"]+", re.I
    )
    return [match.group(0) for text in haystacks for match in pattern.finditer(text)]


#: Built by concatenation on purpose: written as one literal, this example would be found
#: by the very scan it is an example for, and this file would fail its own test.
_PLAIN_SPELLING = "alter table ledger.chain_head enable " + "trigger chain_head_guard_trigger"
_ALWAYS_SPELLING = "alter table ledger.chain_head enable always trigger chain_head_guard_trigger"


def _plainly_enabled_triggers(statements: list[str]) -> list[str]:
    """The subset of ``statements`` that enable a trigger without saying ALWAYS."""
    import re

    plain = re.compile(r"\balter\s+table\s+[\w.\"]+\s+enable\s+trigger\b", re.I)
    return [text for text in statements if plain.search(text)]


def test_the_plain_enable_spelling_is_what_this_scan_is_looking_for() -> None:
    """The positive control. A scan that cannot flag anything flags nothing for a reason.

    Two statements that differ in one word: the scan has to separate them, or the test
    below is a test of an empty list.
    """
    assert _plainly_enabled_triggers([_PLAIN_SPELLING]) == [_PLAIN_SPELLING]
    assert _plainly_enabled_triggers([_ALWAYS_SPELLING]) == []
    assert _plainly_enabled_triggers(["alter table ledger.chain_head disable trigger t"]) == []


def test_no_ledger_source_re_enables_a_trigger_without_saying_always() -> None:
    """T-127 acceptance 2: every site, not the two this ticket happened to name.

    Scanning beats fixing two lines, because the next one will be written by someone who
    read the catalog test, saw it green, and reasonably assumed the catalog test was the
    guard. It is not: a teardown inside a rolled-back transaction never reaches the
    catalog.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    migration = repo_root / "db" / "migrations" / "0002_ledger_tables.sql"
    scanned: list[Any] = sorted((repo_root / "apps" / "trust").rglob("*.py"))
    scanned += sorted((repo_root / "db" / "migrations").glob("*.sql"))
    assert scanned, "the scan found no files, so it graded nothing"

    offenders: list[str] = []
    found: list[str] = []
    for path in scanned:
        statements = _sql_statements_that_enable_a_trigger(path)
        found += statements
        offenders += [
            f"{path.relative_to(repo_root)}: {text}"
            for text in _plainly_enabled_triggers(statements)
        ]

    # Liveness. An offender list is only meaningful if the scan is still matching, and the
    # migration is the fixed point to measure that against: it installs exactly the five
    # integrity triggers `test_every_ledger_integrity_trigger_is_installed_enable_always`
    # pins, each with its own ALTER TABLE.
    assert len(_sql_statements_that_enable_a_trigger(migration)) == 5, (
        f"the scan found {len(_sql_statements_that_enable_a_trigger(migration))} "
        f"trigger-enabling statements in {migration.name}, not the five the migration "
        f"writes; it has stopped matching, so an empty offender list means nothing"
    )
    assert len(found) > 5, "no test source enables a trigger at all; the scan graded only SQL"
    assert offenders == [], (
        "these statements re-enable a ledger trigger with the plain spelling, which "
        "restores pg_trigger.tgenabled to 'O' rather than the 'A' the migration installs -- "
        "and at 'O' a single `SET session_replication_role = 'replica'` skips the trigger "
        "entirely:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.docker
def test_a_when_clause_on_the_trigger_is_an_exemption_the_body_cannot_see(ledger_clean) -> None:
    """The other place an exemption can live, and the only assertion that can refuse it.

    ``CREATE TRIGGER ... WHEN (<condition>)`` is evaluated by Postgres *before* the trigger
    function is called, and the condition may read anything a function can --
    ``current_setting``, ``current_user``, ``inet_client_addr()``. An arm whose body is
    unconditional is no defence at all if the body is never reached, and nothing in
    ``prosrc`` records that it was not.

    So this installs exactly that: the same trigger, the same unmodified function, plus a
    ``WHEN`` that a single ``SET LOCAL`` turns off. It is the input that ONLY the
    ``tgqual is null`` assertion can reject -- the source-level property test next door
    passes on it, because the source it reads is untouched.

    Rolled back, and the arm is re-driven afterwards.
    """
    connection = ledger_clean
    for index in range(3):
        append_event(connection, observation_event(index))

    with psycopg.connect(_admin_dsn(connection), connect_timeout=5) as probe:
        try:
            with probe.cursor() as cur:
                cur.execute("set local proxyshop.anchor_maintenance = 'on'")
                cur.execute(
                    "create or replace trigger chain_head_guard_trigger "
                    "  before insert or update or delete on ledger.chain_head "
                    "  for each row "
                    "  when (current_setting('proxyshop.anchor_maintenance', true) "
                    "        is distinct from 'on') "
                    "  execute function ledger.chain_head_guard()"
                )

                cur.execute("delete from ledger.chain_head")
                assert cur.rowcount == 1, (
                    f"the WHEN clause did not actually skip the arm (rowcount="
                    f"{cur.rowcount}); this proves nothing about what tgqual catches"
                )

                # The body is untouched, so the source-level property still holds...
                declare, body = _chain_head_guard_body(connection)
                assert body.startswith(UNCONDITIONAL_DELETE_BRANCH), (
                    "the function body changed; then this test is no longer isolating the "
                    "WHEN clause"
                )

            # ...and the WHEN clause is what has to catch it.
            with pytest.raises(AssertionError) as refused:
                _chain_head_guard_body(probe)
            assert "carries a WHEN clause" in str(refused.value), refused.value
        finally:
            probe.rollback()

    _chain_head_guard_body(connection)
    _anchor_delete_is_refused_by_the_trigger(connection, "delete from ledger.chain_head")
