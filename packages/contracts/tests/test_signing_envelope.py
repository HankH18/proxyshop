"""The external signing envelope (D52) and the single canonical form the signature covers.

Two things are proven here and they are different claims:

1. **The envelope is required.** A submission missing any one of the five fields fails schema
   validation, and a submission carrying all five validates. Both halves matter — a schema that
   rejected everything would satisfy the first assertion and be useless.
2. **The canonical bytes cover what they promise.** Deterministic, independent of mapping
   insertion order, stable across a JSON round trip, and CHANGED by a mutation to any covered
   field. The last one is what stops a signature being lifted onto another auction, signer, store,
   instant, key or body.

The exact canonical bytes for the reference submission are pinned in `EXPECTED_CANONICAL_BYTES`
and the TypeScript suite pins the same string. That is the only way two independent
implementations of a canonicalizer can be shown to be one protocol.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from pydantic import ValidationError

from packages.contracts import (
    REQUIRED_SIGNING_FIELDS,
    SIGNED_FIELDS,
    SignedBidSubmission,
    SigningEnvelope,
    canonical_json,
    canonical_signing_bytes,
    envelope_of,
    keyring_secret,
    missing_signing_fields,
    payload_hash,
)
from packages.contracts.tests._fixtures_protocol import make_bid, make_submission

#: Pinned byte-for-byte, and pinned identically in `tests/signing.test.ts`. If these two ever
#: disagree, a bid signed by a Node seller stops verifying at a Python exchange.
EXPECTED_CANONICAL_BYTES = (
    '{"auction_id":"auc-0100","issued_at":"2026-01-01T00:00:00Z","key_id":"key-2026-01",'
    '"nonce":"nonce-ext-0001",'
    '"payload_hash":"sha256:3879714cea211d2b69e088a8535f30d00f7f571b8a38a21b0728466159cbd512",'
    '"schema_version":"1.0.0","signer_id":"store-external-1","store_id":"store-external-1"}'
)


# --- D52: the five fields are required ----------------------------------------------------


def test_the_required_signing_fields_are_exactly_the_five_d52_names() -> None:
    assert tuple(REQUIRED_SIGNING_FIELDS) == (
        "signer_id",
        "key_id",
        "issued_at",
        "nonce",
        "schema_version",
    )


def test_a_complete_submission_validates() -> None:
    """The positive control. Without it the rejections below would prove nothing."""
    submission = SignedBidSubmission.model_validate(make_submission())
    assert submission.signer_id == "store-external-1"
    assert submission.key_id == "key-2026-01"


@pytest.mark.parametrize("field", REQUIRED_SIGNING_FIELDS)
def test_a_submission_missing_any_one_required_field_is_invalid(field: str) -> None:
    payload = make_submission()
    del payload[field]
    with pytest.raises(Exception):
        SignedBidSubmission.model_validate(payload)


@pytest.mark.parametrize("field", REQUIRED_SIGNING_FIELDS)
def test_a_submission_with_an_empty_required_field_is_invalid(field: str) -> None:
    """Empty string is not a signer, a key, an instant or a nonce."""
    with pytest.raises(Exception):
        SignedBidSubmission.model_validate(make_submission(**{field: ""}))


def test_missing_signing_fields_names_every_gap_at_once() -> None:
    payload = make_submission()
    del payload["nonce"]
    payload["key_id"] = "  "
    assert set(missing_signing_fields(payload)) == {"nonce", "key_id"}
    assert missing_signing_fields(make_submission()) == []


def test_the_submission_shape_is_exactly_bid_union_signing_envelope() -> None:
    """The composed definition cannot drift away from the two it composes."""
    from packages.contracts import Bid

    assert set(SignedBidSubmission.model_fields) == set(Bid.model_fields) | set(
        SigningEnvelope.model_fields
    )


def test_a_plain_bid_is_not_a_valid_external_submission() -> None:
    with pytest.raises(Exception):
        SignedBidSubmission.model_validate(make_bid())


def test_envelope_of_lifts_the_five_fields_out() -> None:
    envelope = envelope_of(make_submission())
    assert isinstance(envelope, SigningEnvelope)
    assert envelope.nonce == "nonce-ext-0001"
    with pytest.raises(Exception):
        envelope_of(make_bid())


# --- the canonical signing bytes ----------------------------------------------------------


def test_canonical_bytes_are_deterministic() -> None:
    payload = make_submission()
    assert canonical_signing_bytes(payload) == canonical_signing_bytes(payload)


def test_canonical_bytes_ignore_mapping_insertion_order() -> None:
    payload = make_submission()
    reordered = dict(reversed(list(payload.items())))
    assert canonical_signing_bytes(reordered) == canonical_signing_bytes(payload)


def test_canonical_bytes_survive_a_json_round_trip() -> None:
    payload = make_submission()
    assert canonical_signing_bytes(json.loads(json.dumps(payload))) == canonical_signing_bytes(
        payload
    )


def test_canonical_bytes_match_the_pinned_cross_language_fixture() -> None:
    assert canonical_signing_bytes(make_submission()).decode("utf-8") == EXPECTED_CANONICAL_BYTES


def test_canonical_bytes_cover_every_signed_field_verbatim() -> None:
    payload = make_submission()
    text = canonical_signing_bytes(payload).decode("utf-8")
    for field in SIGNED_FIELDS:
        assert str(payload[field]) in text, f"the signing bytes do not cover {field!r}"
    assert payload_hash(payload) in text, "the signature must cover the bid body"


@pytest.mark.parametrize(
    ("label", "mutation"),
    [
        ("auction_id", {"auction_id": "auc-0999"}),
        ("signer_id", {"signer_id": "store-external-2"}),
        ("store_id", {"store_id": "store-external-2"}),
        ("issued_at", {"issued_at": "2026-01-01T00:00:01Z"}),
        ("nonce", {"nonce": "nonce-ext-0002"}),
        ("key_id", {"key_id": "key-2026-07"}),
        ("schema_version", {"schema_version": "2"}),
    ],
)
def test_mutating_any_covered_field_changes_the_bytes(label: str, mutation: dict) -> None:
    base = canonical_signing_bytes(make_submission())
    assert canonical_signing_bytes(make_submission(**mutation)) != base, (
        f"changing {label} must change the signing input — it is a covered field"
    )


def test_mutating_the_offer_body_changes_the_bytes() -> None:
    from packages.contracts.tests._fixtures_protocol import make_offer

    base = canonical_signing_bytes(make_submission())
    mutated = make_submission(offer=make_offer(unit_price=88.0))
    assert canonical_signing_bytes(mutated) != base


def test_an_incomplete_envelope_cannot_be_canonicalized() -> None:
    """Refusing to sign a submission that cannot legally exist beats emitting unverifiable bytes."""
    for field in REQUIRED_SIGNING_FIELDS:
        payload = make_submission()
        del payload[field]
        with pytest.raises(ValueError, match="incomplete signing envelope"):
            canonical_signing_bytes(payload)


def test_a_submission_without_auction_or_store_cannot_be_canonicalized() -> None:
    for field in ("auction_id", "store_id"):
        payload = make_submission(**{field: ""})
        with pytest.raises(ValueError):
            canonical_signing_bytes(payload)


# --- the payload hash ---------------------------------------------------------------------


def test_payload_hash_is_a_real_digest() -> None:
    digest = payload_hash(make_submission())
    assert isinstance(digest, str) and len(digest) >= 32
    assert digest.startswith("sha256:")


def test_payload_hash_is_stable_across_a_json_round_trip_and_key_order() -> None:
    payload = make_submission()
    digest = payload_hash(payload)
    assert payload_hash(json.loads(json.dumps(payload))) == digest
    assert payload_hash(dict(reversed(list(payload.items())))) == digest


def test_payload_hash_changes_when_the_bid_body_changes() -> None:
    from packages.contracts.tests._fixtures_protocol import make_offer

    base = payload_hash(make_submission())
    assert payload_hash(make_submission(offer=make_offer(unit_price=88.0))) != base
    assert payload_hash(make_submission(message="something else entirely")) != base


def test_payload_hash_ignores_the_envelope_and_the_signature() -> None:
    """The body digest covers the promise; the envelope rides in the signing bytes in its own right,
    and a signature cannot cover itself."""
    base = payload_hash(make_submission())
    assert payload_hash(make_submission(nonce="nonce-ext-0002")) == base
    assert payload_hash({**make_submission(), "signature": "sig-whatever"}) == base


def test_canonical_json_writes_integral_floats_the_way_ecmascript_does() -> None:
    """RFC 8785 §3.2.2.3: `89.0` and `89` are the same number. Python's json.dumps disagrees, and
    without the normalization the two language implementations would sign differently."""
    assert canonical_json({"n": 89.0}) == '{"n":89}'
    assert canonical_json({"n": 44.1}) == '{"n":44.1}'
    assert canonical_json({"b": True}) == '{"b":true}', (
        "bool is an int subclass; it must not collapse"
    )
    assert canonical_json({"b": [1.0, {"c": 2.0}]}) == '{"b":[1,{"c":2}]}'


def test_canonical_json_sorts_nested_keys_not_only_the_top_level() -> None:
    assert canonical_json({"b": {"z": 1, "a": 2}, "a": 3}) == '{"a":3,"b":{"a":2,"z":1}}'


# --- the keyring --------------------------------------------------------------------------


def test_the_keyring_is_indexed_by_signer_then_key() -> None:
    keyring = {
        "store-external-1": {"key-2026-01": "secret-1", "key-2026-07": "secret-2"},
        "store-external-2": {"key-2026-01": "secret-3"},
    }
    assert keyring_secret(keyring, "store-external-1", "key-2026-01") == "secret-1"
    assert keyring_secret(keyring, "store-external-1", "key-2026-07") == "secret-2"

    # Two signers may legitimately share a key_id string, so a lookup keyed on key_id alone is
    # wrong — and this pair proves it, because both signers use "key-2026-01".
    assert keyring_secret(keyring, "store-external-2", "key-2026-01") == "secret-3"


def test_an_unknown_key_never_falls_back_to_another_key_of_that_signer() -> None:
    """Falling back is precisely what would make a revoked key still work."""
    keyring = {"store-external-1": {"key-2026-01": "secret-1"}}
    assert keyring_secret(keyring, "store-external-1", "key-2026-07") is None
    assert keyring_secret(keyring, "store-external-9", "key-2026-01") is None
    assert keyring_secret({}, "store-external-1", "key-2026-01") is None


# ---------------------------------------------------------------------------------------------
# Cross-language canonicalization, pinned against a reference implementation.
#
# `tests/canonicalization_corpus.json` was produced by a canonicalizer written straight from
# RFC 8785, with ECMAScript as the authority for number formatting (§3.2.2.3) and member ordering
# (§3.2.3) — because that is what the RFC cites. BOTH language implementations are checked against
# that same file, which is what makes them one implementation rather than two that happen to agree
# on whatever fixtures someone thought to try.
#
# The cases that matter are the ones where Python's obvious answer is WRONG:
#   1e-6   `repr` says "1e-06",  ECMAScript says "0.000001"
#   1e16   `repr` says "1e+16",  ECMAScript says "10000000000000000"
#   "😀"   sorts before "Ｚ" by code point, AFTER it by UTF-16 code unit
# `Offer.unit_price` is an unconstrained `number` and `Claim.value` is unconstrained entirely, so
# every one of these is reachable from a schema-valid bid.
# ---------------------------------------------------------------------------------------------

_CORPUS_PATH = pathlib.Path(__file__).parent / "canonicalization_corpus.json"
_CORPUS = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))


def test_the_corpus_is_present_and_substantial() -> None:
    """Guards the parametrized test below against passing vacuously on an empty file."""
    assert len(_CORPUS) >= 25
    assert all({"input", "expected"} <= set(case) for case in _CORPUS)


@pytest.mark.parametrize("case", _CORPUS, ids=[case["expected"][:48] for case in _CORPUS])
def test_canonical_json_matches_the_rfc_8785_reference(case: dict) -> None:
    assert canonical_json(case["input"]) == case["expected"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (89.0, "89"),
        (44.1, "44.1"),
        (0.0, "0"),
        (-0.0, "0"),
        (1e-6, "0.000001"),
        (1e-7, "1e-7"),
        (0.00001, "0.00001"),
        (0.0001, "0.0001"),
        (1e21, "1e+21"),
        (1e16, "10000000000000000"),
        (1.5e-10, "1.5e-10"),
        (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (-44.1, "-44.1"),
    ],
)
def test_numbers_serialize_the_way_ecmascript_does(value: float, expected: str) -> None:
    """Every one of these is a magnitude where `repr(value)` gives a DIFFERENT string, so a
    canonicalizer built on `repr` would sign differently from the Node peer."""
    assert canonical_json({"n": value}) == f'{{"n":{expected}}}'


def test_an_integer_that_is_not_a_double_is_REFUSED_not_silently_coerced() -> None:
    """RFC 8785 §3.1: "JSON number data MUST be expressible as IEEE 754 double-precision values."

    This assertion replaces one that required the opposite — see the `justify-test-edit` ritual in
    the commit that changed it. It read:

        assert canonical_json({"n": 12345678901234567890}) == '{"n":12345678901234567000}'

    i.e. it REQUIRED the canonicalizer to silently rewrite a non-representable integer as the
    nearest double. That is a defect encoded as a contract: a seller signing
    `{"quantity": 9007199254740993}` produced bytes covering `…992`, the signature verified, and
    the exchange acted on a quantity the signature did not bind. Coercion has to be a refusal.
    """
    for value in (2**53 + 1, 12345678901234567890, 10**400, -(2**53) - 1):
        with pytest.raises(Exception) as excinfo:
            canonical_json({"n": value})
        assert "IEEE-754" in str(excinfo.value) or "double" in str(excinfo.value)

    # Controls, so this is not "refuse every large integer". Each of these IS an exact double and
    # must still canonicalize — the old safe-integer bound was wrong in this direction too.
    assert canonical_json({"n": 2**53 - 1}) == '{"n":9007199254740991}'
    assert canonical_json({"n": 2**53}) == '{"n":9007199254740992}'
    assert canonical_json({"n": 10**16}) == '{"n":10000000000000000}'
    assert canonical_json({"n": 2**63}) == '{"n":9223372036854776000}'
    assert canonical_json({"n": 10**21}) == '{"n":1e+21}'


def test_keys_are_ordered_by_utf16_code_unit_not_code_point() -> None:
    """RFC 8785 §3.2.3. Python's `<` compares code points and disagrees above the BMP, so an
    emoji key would sort to the wrong side and the two implementations would sign differently."""
    assert canonical_json({"Ｚ": 1, "\U0001f600": 2}) == '{"\U0001f600":2,"Ｚ":1}'


def test_a_value_that_cannot_be_canonicalized_is_refused() -> None:
    """Refusing beats stringifying: `repr` of an unordered or memory-addressed object is not
    stable across processes, so the signature would not reproduce."""
    with pytest.raises(TypeError):
        canonical_json({"n": {1, 2, 3}})
    with pytest.raises(TypeError):
        canonical_json({"n": object()})


def test_non_finite_numbers_are_refused() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            canonical_json({"n": value})


# --- F3: the five envelope fields are required BY TYPE, not merely by presence --------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in REQUIRED_SIGNING_FIELDS
        for value in (0, 1, False, True, 12345, [], {}, ["x"], {"a": 1}, 1.5, None)
    ],
)
def test_a_non_string_envelope_field_counts_as_missing(field: str, value: object) -> None:
    """All five are `string` in the schema, and `SigningEnvelope` rejects every one of these
    values. `missing_signing_fields` used to call them present, which meant `nonce: false` — a
    CONSTANT nonce, the thing D52's replay defence exists to make impossible — sailed through.
    `canonical_signing_bytes` calls this function and never `envelope_of`, so the loose gate was
    the only one on the path."""
    payload = make_submission(**{field: value})
    assert field in missing_signing_fields(payload), (
        f"{field}={value!r} was accepted as a present envelope field"
    )
    with pytest.raises(Exception):
        SigningEnvelope.model_validate({f: payload.get(f) for f in REQUIRED_SIGNING_FIELDS})


@pytest.mark.parametrize(
    ("field", "value"),
    [(field, value) for field in REQUIRED_SIGNING_FIELDS for value in (0, False, [], {}, 12345)],
)
def test_a_non_string_envelope_field_cannot_be_canonicalized(field: str, value: object) -> None:
    """The end-to-end consequence: `{"issued_at": 12345}` used to produce signing bytes reading
    `"issued_at":12345`, over a submission `SigningEnvelope` would never have admitted."""
    with pytest.raises(ValueError, match="incomplete signing envelope"):
        canonical_signing_bytes(make_submission(**{field: value}))


def test_the_two_envelope_gates_agree_with_each_other() -> None:
    """`envelope_of` (pydantic) and `missing_signing_fields` are two gates on the same rule. They
    disagreed on every non-string value, and the stricter one was the one nobody was on the path
    of."""
    for value in (0, False, [], {}, 12345, 1.5, None, ""):
        payload = make_submission(nonce=value)
        assert missing_signing_fields(payload) != [], f"nonce={value!r} passed the loose gate"
        with pytest.raises(Exception):
            envelope_of(payload)

    # T-108 closed the last asymmetry. This line used to read
    #     assert envelope_of(whitespace).nonce == "   "
    # which pinned the DEVIATION rather than the rule: `SigningEnvelope` spelled its constraint
    # `min_length=1`, so `"   "` satisfied pydantic while `missing_signing_fields` stripped it and
    # called it missing. That is the very disagreement this test's name denies, recorded as though
    # it were the contract. It pointed the safe way only because `canonical_signing_bytes` happens
    # to sit on the strict gate. The schema now carries `pattern` alongside `minLength`, so both
    # gates refuse a whitespace-only value and the assertion states the rule instead of the gap.
    whitespace = make_submission(nonce="   ")
    assert missing_signing_fields(whitespace) == ["nonce"]
    with pytest.raises(ValidationError):
        envelope_of(whitespace)
    with pytest.raises(ValueError, match="incomplete signing envelope"):
        canonical_signing_bytes(whitespace)

    # Control: a real nonce passes both.
    assert missing_signing_fields(make_submission()) == []
    assert envelope_of(make_submission()).nonce == "nonce-ext-0001"


# --- F2: the keyring lookup is on the public path and must not raise ------------------------


def test_an_unhashable_signer_or_key_is_a_miss_rather_than_a_crash() -> None:
    """`{"signer_id": {"a": 1}, "key_id": "k-1"}` on the public submission route made
    `dict.get` raise `TypeError: unhashable type: 'dict'` — an uncaught 500 from an
    unauthenticated caller where a rejection belongs. `boundary.py` already guards this exact
    hazard on `store_id` for the same reason."""
    keyring = {"store-external-1": {"key-2026-01": "secret-1"}}
    for signer_id, key_id in (
        ({"a": 1}, "key-2026-01"),
        (["x"], "key-2026-01"),
        ("store-external-1", {"a": 1}),
        ("store-external-1", ["x"]),
        ({1, 2}, {3, 4}),
        (None, None),
        (12345, 67890),
    ):
        assert keyring_secret(keyring, signer_id, key_id) is None, (
            f"({signer_id!r}, {key_id!r}) resolved to a secret"
        )

    # Control: the well-typed pair still resolves, so this is not "return None for everything".
    assert keyring_secret(keyring, "store-external-1", "key-2026-01") == "secret-1"


# --- F5: an empty or non-string stored secret is not a key ----------------------------------


def test_a_blank_or_non_string_stored_secret_is_not_a_usable_key() -> None:
    """A keyring row seeded with `""` — a placeholder, a truncated secret, a key cleared but not
    deleted — must read as "no such key". Returning it would hand T-044 an empty HMAC key. The
    TypeScript peer is pinned the same way."""
    keyring = {
        "store-external-1": {
            "key-good": "secret-1",
            "key-empty": "",
            "key-blank": "   ",
            "key-none": None,
            "key-int": 12345,
            "key-list": ["secret-1"],
            "key-bool": True,
        }
    }
    for key_id in ("key-empty", "key-none", "key-int", "key-list", "key-bool"):
        assert keyring_secret(keyring, "store-external-1", key_id) is None, (
            f"{key_id} was treated as a usable HMAC key"
        )

    # Control: the real secret still comes back.
    assert keyring_secret(keyring, "store-external-1", "key-good") == "secret-1"
    # A whitespace-only secret is a real (if silly) string and is returned — the contract is
    # "non-empty string", not "looks like a key". Pinned so the boundary of the rule is explicit.
    assert keyring_secret(keyring, "store-external-1", "key-blank") == "   "


# --- F7: a non-mapping is not an acceptable envelope ----------------------------------------


@pytest.mark.parametrize(
    "payload", ("a raw string", "", ["signer_id", "key_id"], 42, None, True, {1, 2}, b"bytes")
)
def test_a_non_mapping_envelope_is_five_errors_not_zero(payload: object) -> None:
    """`[]` from `signing_envelope_errors` means "acceptable envelope". A caller trusting an
    empty list would admit a bare string or a JSON array as a signed submission."""
    from packages.contracts.src.signing import signing_envelope_errors

    assert len(signing_envelope_errors(payload)) == 5, (
        f"{payload!r} was reported as an acceptable envelope"
    )
    assert missing_signing_fields(payload) == list(REQUIRED_SIGNING_FIELDS)

    # Control: a complete mapping is zero errors.
    assert list(signing_envelope_errors(make_submission())) == []


# --- RFC 8785 §3.1: the number rule is `float(v) == v`, not a safe-integer bound ------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (2**53 - 1, "9007199254740991"),  # the old bound's last accepted value
        (2**53, "9007199254740992"),  # one past it, and still an exact double
        (10**16, "10000000000000000"),  # an exact double; T-011's bound rejects this one
        (2**63, "9223372036854776000"),  # exact double; ES prints the shortest round trip
        (10**21, "1e+21"),
        (-(2**53), "-9007199254740992"),
        (0, "0"),
        (89, "89"),
    ],
)
def test_an_integer_that_is_an_exact_double_is_canonicalized(value: int, expected: str) -> None:
    """`float(v) == v` is the whole predicate. The old `abs(v) <= 2**53-1` bound was wrong in
    BOTH directions — it treats `10**16` and `2**63` as out of range even though every JS engine
    reads them back bit-for-bit, and it silently coerced everything above it."""
    assert canonical_json({"n": value}) == f'{{"n":{expected}}}'


@pytest.mark.parametrize(
    "value",
    [
        2**53 + 1,  # 9007199254740993 — the first integer with no double
        2**53 + 3,
        -(2**53) - 1,
        12345678901234567890,
        123456789012345680000,
        12345678901234567001,
        10**400,  # no double at all: `float()` raises OverflowError
        -(10**400),
    ],
)
def test_an_integer_that_is_not_an_exact_double_is_refused(value: int) -> None:
    """The security case, independent of any cross-language question: a seller signing
    `{"quantity": 9007199254740993}` used to produce bytes covering `…992`. The signature
    verifies and the exchange acts on a quantity the signature does not bind."""
    from packages.contracts import CanonicalisationError

    with pytest.raises(CanonicalisationError):
        canonical_json({"n": value})
    with pytest.raises(CanonicalisationError):
        canonical_json([value])


def test_the_number_rule_is_exactly_float_equality() -> None:
    """Stated as a property rather than a table, so the rule cannot drift to some other bound
    that happens to agree on the sampled values. This is the predicate to hand any peer
    implementation: `float(v) == v`, with `OverflowError` counting as False."""
    from packages.contracts import CanonicalisationError

    interesting = [
        0,
        1,
        -1,
        10**15,
        10**16,
        10**17,
        2**53 - 1,
        2**53,
        2**53 + 1,
        2**53 + 2,
        2**62,
        2**63,
        2**63 + 1,
        10**21,
        10**22,
        10**308,
        10**309,
        10**400,
    ]
    for value in interesting + [-v for v in interesting]:
        try:
            representable = float(value) == value
        except OverflowError:
            representable = False
        if representable:
            canonical_json({"n": value})  # must not raise
        else:
            with pytest.raises(CanonicalisationError):
                canonical_json({"n": value})


# --- one exception type for every canonicalisation failure ----------------------------------


def test_every_canonicalisation_failure_raises_the_one_error() -> None:
    """The module used to raise `TypeError`, `ValueError`, `AttributeError` and `OverflowError`
    depending on which way the input was wrong — including a bare `AttributeError: 'int' object
    has no attribute 'encode'` for a non-string key, a latent 500. A caller writing one `except`
    around the canonicalizer missed four of the five failure modes."""
    from packages.contracts import CanonicalisationError

    failures = {
        "a set": lambda: canonical_json({"n": {1, 2, 3}}),
        "an object": lambda: canonical_json({"n": object()}),
        "a non-string key": lambda: canonical_json({1: 2}),
        "a tuple key": lambda: canonical_json({(1, 2): "x"}),
        "nan": lambda: canonical_json({"n": float("nan")}),
        "inf": lambda: canonical_json({"n": float("inf")}),
        "a non-double integer": lambda: canonical_json({"n": 2**53 + 1}),
        "an integer with no double at all": lambda: canonical_json({"n": 10**400}),
        "payload_hash of a non-mapping": lambda: payload_hash("not a mapping"),
        "canonical_signing_bytes of a non-mapping": lambda: canonical_signing_bytes(["a", "b"]),
        "an incomplete envelope": lambda: canonical_signing_bytes(make_bid()),
    }
    for label, call in failures.items():
        with pytest.raises(CanonicalisationError, match=r".") as excinfo:
            call()
        assert type(excinfo.value) is CanonicalisationError, (
            f"{label} raised {type(excinfo.value).__name__}, not the one canonicalisation error"
        )


def test_the_one_error_still_matches_the_except_clauses_callers_already_wrote() -> None:
    """Narrowing a public exception type is a breaking change. `CanonicalisationError` derives
    from both `ValueError` and `TypeError` so no existing `except` silently stops matching."""
    from packages.contracts import CanonicalisationError

    assert issubclass(CanonicalisationError, ValueError)
    assert issubclass(CanonicalisationError, TypeError)


# --- the corpus itself ----------------------------------------------------------------------


def test_the_corpus_cases_are_distinct() -> None:
    """The file advertised 27 cases and had 26 distinct inputs: entries 3 and 4 were both
    `{"n": 0}`, because the intended `-0` case round-tripped through JSON as `0` when the file
    was written. `-0` was therefore untested in BOTH languages. `len(_CORPUS) >= 25` could not
    see that, which is why nobody noticed."""
    rendered = [json.dumps(case["input"], sort_keys=True) for case in _CORPUS]
    duplicates = {text for text in rendered if rendered.count(text) > 1}
    assert duplicates == set(), f"the corpus repeats these inputs: {duplicates}"
    assert len(_CORPUS) == 27


def test_negative_zero_is_actually_in_the_corpus_and_actually_negative() -> None:
    """Stored as a float literal so `json.loads` preserves the sign bit — as an integer `0` it
    silently became the positive-zero case that was already there."""
    import math

    negative_zeros = [
        case
        for case in _CORPUS
        if isinstance(case["input"].get("n"), float)
        and case["input"]["n"] == 0
        and math.copysign(1, case["input"]["n"]) < 0
    ]
    assert len(negative_zeros) == 1, "the corpus does not contain a real -0 case"
    assert canonical_json(negative_zeros[0]["input"]) == '{"n":0}'


# --- RFC 8785 §3.2.2.2: lone surrogates terminate with an error, in BOTH languages -----------

#: Wire forms that decode to a Python `str` holding an unpaired surrogate. Written as JSON text
#: and parsed, because that is exactly how one arrives — off the wire, through `json.loads`.
_LONE_SURROGATE_WIRE = (
    '"\\ud800"',  # a bare high surrogate
    '"\\udfff"',  # a bare low surrogate
    '"a\\ud83dz"',  # a high surrogate not followed by a low one
    '"\\udc00\\ud800"',  # low then high: the pair in the wrong order
    '"caf\\u00e9 \\udbff"',  # a lone surrogate after perfectly ordinary text
)


@pytest.mark.parametrize("wire", _LONE_SURROGATE_WIRE)
def test_a_lone_surrogate_in_a_string_is_refused(wire: str) -> None:
    """RFC 8785 §3.2.2.2: "occurrences of such data MUST cause a compliant JCS implementation to
    terminate with an appropriate error."

    Before this, Python emitted the surrogate into the output string and only blew up at
    `.encode("utf-8")` with `UnicodeEncodeError`, while the TypeScript peer SUCCEEDED and emitted
    escaped hex. `Claim.value` is unconstrained, so a Node seller could sign a bid the Python
    exchange then crashed on at the public door rather than rejecting."""
    from packages.contracts import CanonicalisationError

    value = json.loads(wire)
    with pytest.raises(CanonicalisationError, match="lone surrogate"):
        canonical_json({"s": value})
    with pytest.raises(CanonicalisationError, match="lone surrogate"):
        canonical_json([value])


@pytest.mark.parametrize("wire", _LONE_SURROGATE_WIRE)
def test_a_lone_surrogate_in_a_KEY_is_refused(wire: str) -> None:
    """Keys go through `_utf16_key`, which passes surrogates through for sorting. A refusal has
    to happen before that, or the same crash reappears one layer down."""
    from packages.contracts import CanonicalisationError

    with pytest.raises(CanonicalisationError, match="lone surrogate"):
        canonical_json({json.loads(wire): 1})


def test_a_lone_surrogate_never_reaches_the_signing_bytes() -> None:
    """The end-to-end shape of it: a rejection, not a `UnicodeEncodeError` at the public door."""
    from packages.contracts import CanonicalisationError

    payload = make_submission(message=json.loads('"\\ud800"'))
    with pytest.raises(CanonicalisationError, match="lone surrogate"):
        canonical_signing_bytes(payload)
    with pytest.raises(CanonicalisationError, match="lone surrogate"):
        payload_hash(payload)


def test_well_formed_astral_characters_are_still_signable() -> None:
    """The control. A real emoji is ONE code point in Python and a surrogate PAIR in JavaScript;
    refusing it would break every bid whose message contains one, and would put the two languages
    right back out of step."""
    assert canonical_json({"s": "\U0001f600"}) == '{"s":"\U0001f600"}'
    assert canonical_json({"\U0001f600": 1, "Ｚ": 2}) == '{"\U0001f600":1,"Ｚ":2}'
    assert canonical_json({"s": "café — “quoted” 😀🎉"}) == '{"s":"café — “quoted” 😀🎉"}'
    # ...and it still round-trips through the wire form the JavaScript peer would send.
    assert canonical_json({"s": json.loads('"\\ud83d\\ude00"')}) == '{"s":"\U0001f600"}'


# --- T-108: the two envelope gates agree on WHITESPACE, not merely on length ------------------


#: Every whitespace-only spelling `str.strip()` collapses to empty, by code point so the file
#: itself carries no raw control characters. `\S` alone would NOT cover U+001C-U+001F: Unicode
#: does not classify those four as whitespace, but Python's `str.strip()` does, which is exactly
#: the gap a `\S` pattern would have left open between the schema and `missing_signing_fields`.
WHITESPACE_ONLY: list[str] = [
    chr(code)
    for code in (
        0x20,
        0x09,
        0x0A,
        0x0B,
        0x0C,
        0x0D,
        0x1C,
        0x1D,
        0x1E,
        0x1F,
        0x85,
        0xA0,
        0x1680,
        0x2000,
        0x2003,
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
    )
] + ["   ", chr(0x20) + chr(0x09) + chr(0x0A), chr(0x09) * 4]


@pytest.mark.parametrize("field", REQUIRED_SIGNING_FIELDS)
@pytest.mark.parametrize(
    "blank", WHITESPACE_ONLY, ids=lambda s: "-".join(f"U+{ord(c):04X}" for c in s)
)
def test_both_envelope_gates_refuse_every_whitespace_only_value(field: str, blank: str) -> None:
    """`SigningEnvelope` used to spell its rule `min_length=1`, which admits any number of spaces;
    `missing_signing_fields` strips and calls the same value missing. Two gates on one rule that
    disagree is one gate, and it is whichever one the caller happens to be standing on."""
    assert blank.strip() == "", "this case is not actually whitespace-only"

    payload = make_submission(**{field: blank})
    assert field in missing_signing_fields(payload), (field, repr(blank))
    with pytest.raises(ValidationError):
        envelope_of(payload)
    with pytest.raises(ValidationError):
        SigningEnvelope.model_validate({name: payload[name] for name in REQUIRED_SIGNING_FIELDS})
    with pytest.raises(ValidationError):
        SignedBidSubmission.model_validate(payload)
    with pytest.raises(ValueError, match="incomplete signing envelope"):
        canonical_signing_bytes(payload)


@pytest.mark.parametrize("field", REQUIRED_SIGNING_FIELDS)
def test_a_value_with_real_content_still_passes_both_gates(field: str) -> None:
    """The control. A pattern that rejected everything would satisfy the test above and break
    every legal submission, including the padded-but-non-empty spellings that are still valid."""
    for value in ("x", " padded ", chr(0x09) + "tabbed", "nonce-ext-0001"):
        if field == "issued_at":
            value = "2026-01-01T00:00:00Z"
        payload = make_submission(**{field: value})
        assert missing_signing_fields(payload) == []
        assert getattr(envelope_of(payload), field) == value
        assert canonical_signing_bytes(payload)


def test_the_schema_states_the_whitespace_rule_where_both_languages_read_it() -> None:
    """The rule has to live in `protocol.schema.json`, not in a Python validator: Ajv compiles the
    same file, so a rule spelled anywhere else is a rule TypeScript does not have."""
    defs = json.loads(
        (
            pathlib.Path(__file__).resolve().parents[1] / "schemas" / "protocol.schema.json"
        ).read_text(encoding="utf-8")
    )["$defs"]
    for name in ("SigningEnvelope", "SignedBidSubmission"):
        for field in REQUIRED_SIGNING_FIELDS:
            spec = defs[name]["properties"][field]
            assert spec.get("minLength") == 1, (name, field)
            assert spec.get("pattern"), f"{name}.{field} has no non-blank pattern"
            # Length alone is what let `"   "` through; the pattern is the part that closes it.
            assert "\\s" in spec["pattern"], (name, field, spec["pattern"])
