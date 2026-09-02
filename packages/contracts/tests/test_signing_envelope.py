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


def test_an_integer_beyond_javascript_s_exact_range_is_signed_as_the_double_it_becomes() -> None:
    """The other side reads JSON numbers as doubles. Signing the exact integer would mean the two
    ends canonicalize different values out of identical bytes."""
    assert canonical_json({"n": 12345678901234567890}) == '{"n":12345678901234567000}'
    assert canonical_json({"n": 2**53 - 1}) == '{"n":9007199254740991}'


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

    # The one remaining asymmetry, and it points the SAFE way: `SigningEnvelope` spells its rule
    # `min_length=1`, so `"   "` satisfies pydantic, while `missing_signing_fields` strips and
    # calls it missing. The strict gate is the one `canonical_signing_bytes` is on, so a
    # whitespace-only nonce cannot be signed either way.
    whitespace = make_submission(nonce="   ")
    assert missing_signing_fields(whitespace) == ["nonce"]
    assert envelope_of(whitespace).nonce == "   "
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
