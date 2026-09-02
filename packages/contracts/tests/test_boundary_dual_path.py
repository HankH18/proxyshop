"""The dual-path bid boundary — every cell of the R8/R18/S5 table, and the fail-closed edges.

Every rejection here has a POSITIVE CONTROL beside it: the identical bid, changed only in the one
respect under test, must be admitted. A boundary that rejected everything would satisfy every
rejection assertion in this file and be worthless, so each one is paired.
"""

from __future__ import annotations

import pytest

from packages.contracts import (
    EXTERNAL_PATH,
    HOOK_PROVENANCE_SOURCES,
    HOSTED_PATH,
    NON_HOOK_PROVENANCE_SOURCES,
    parse_timestamp,
    validate_bid,
)
from packages.contracts.tests._fixtures_protocol import (
    ASSERTED_PROVENANCE,
    HOOK_PROVENANCE,
    LONG_EXPIRED,
    NOT_EXPIRED,
    make_bid,
    make_claim,
    make_offer,
    make_snapshot_table,
)

NOW = "2026-06-01T00:00:00Z"
BOTH_PATHS = (HOSTED_PATH, EXTERNAL_PATH)


_DEFAULT = object()


def check(bid, path, snapshot=_DEFAULT, now=NOW):
    # A sentinel, not `None`: `None` is itself a case under test (an unavailable snapshot read),
    # and a `None`-means-default helper would quietly turn that test into the happy path.
    table = make_snapshot_table() if snapshot is _DEFAULT else snapshot
    return validate_bid(bid, path=path, trust_snapshot=table, now=now)


# --- R8 / R18: the path-sensitive half ----------------------------------------------------


def test_hosted_bid_with_a_seller_asserted_claim_is_rejected() -> None:
    result = check(make_bid(claims=[make_claim("spf", 30, dict(ASSERTED_PROVENANCE))]), HOSTED_PATH)
    assert result.ok is False
    assert result.reasons, "a rejection must say why"
    assert any("hosted_non_hook_provenance" in reason for reason in result.reasons)

    control = check(make_bid(claims=[make_claim("spf", 30, dict(HOOK_PROVENANCE))]), HOSTED_PATH)
    assert control.ok is True, control.reasons


def test_external_bid_with_the_same_claim_is_admitted_and_flagged() -> None:
    result = check(
        make_bid(claims=[make_claim("spf", 30, dict(ASSERTED_PROVENANCE))]), EXTERNAL_PATH
    )
    assert result.ok is True, result.reasons
    assert result.requires_verification is True
    assert result.unverified_claim_indexes == [0], (
        "the verification queue must be told WHICH claim to look at, not just that one exists"
    )

    control = check(make_bid(claims=[make_claim("spf", 30, dict(HOOK_PROVENANCE))]), EXTERNAL_PATH)
    assert control.ok is True, control.reasons
    assert control.requires_verification is False, (
        "the flag must track the claim's provenance, not the path alone"
    )


def test_only_the_flagged_claims_are_reported_for_verification() -> None:
    bid = make_bid(
        claims=[
            make_claim("free_returns", "30 days", dict(HOOK_PROVENANCE)),
            make_claim("spf", 30, dict(ASSERTED_PROVENANCE)),
            make_claim("vegan", True, dict(HOOK_PROVENANCE)),
            make_claim("material", "merino", dict(ASSERTED_PROVENANCE)),
        ]
    )
    result = check(bid, EXTERNAL_PATH)
    assert result.ok is True, result.reasons
    assert result.unverified_claim_indexes == [1, 3]


@pytest.mark.parametrize("source", sorted(HOOK_PROVENANCE_SOURCES))
def test_every_hook_provenance_source_is_admitted_on_both_paths(source: str) -> None:
    """R8 rejects NON-HOOK provenance, not "anything that is not owner_statement"."""
    bid = make_bid(claims=[make_claim("spf", 30, {**HOOK_PROVENANCE, "source": source})])
    for path in BOTH_PATHS:
        result = check(bid, path)
        assert result.ok is True, (path, source, result.reasons)
        assert result.requires_verification is False


@pytest.mark.parametrize("source", sorted(NON_HOOK_PROVENANCE_SOURCES))
def test_every_non_hook_source_splits_the_two_paths(source: str) -> None:
    bid = make_bid(claims=[make_claim("spf", 30, {**ASSERTED_PROVENANCE, "source": source})])
    assert check(bid, HOSTED_PATH).ok is False
    external = check(bid, EXTERNAL_PATH)
    assert external.ok is True and external.requires_verification is True


# --- S5: the path-insensitive half --------------------------------------------------------


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_claim_with_no_provenance_key_is_rejected(path: str) -> None:
    result = check(make_bid(claims=[make_claim("spf", 30, None)]), path)
    assert result.ok is False
    assert result.reasons


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_claim_with_an_empty_provenance_source_is_rejected(path: str) -> None:
    result = check(
        make_bid(claims=[make_claim("spf", 30, {**HOOK_PROVENANCE, "source": ""})]), path
    )
    assert result.ok is False
    assert result.reasons


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_an_unknown_provenance_source_is_rejected(path: str) -> None:
    """A source outside the closed enum is not a new kind of evidence; it is a malformed bid."""
    bid = make_bid(claims=[make_claim("spf", 30, {**HOOK_PROVENANCE, "source": "vibes"})])
    assert check(bid, path).ok is False


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_an_expired_offer_is_rejected(path: str) -> None:
    expired = make_bid(offer=make_offer(expires_at=LONG_EXPIRED))
    result = check(expired, path)
    assert result.ok is False
    assert any("offer_expired" in reason for reason in result.reasons)

    live = make_bid(offer=make_offer(expires_at=NOT_EXPIRED))
    assert check(live, path).ok is True


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_an_offer_expiring_exactly_now_is_rejected(path: str) -> None:
    """The boundary is closed at `now`: an offer whose last valid instant has arrived is over."""
    assert check(make_bid(offer=make_offer(expires_at=NOW)), path).ok is False


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_an_offer_with_no_expiry_fails_closed(path: str) -> None:
    """An offer with no stated expiry is one nobody can price the risk of — deny, do not assume."""
    result = check(make_bid(offer=make_offer(expires_at=None)), path)
    assert result.ok is False
    assert any("offer_expiry_missing" in reason for reason in result.reasons)


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_blacklisted_store_is_rejected(path: str) -> None:
    result = check(make_bid(store_id="store-bad"), path)
    assert result.ok is False
    assert any("store_blacklisted" in reason for reason in result.reasons)
    assert check(make_bid(store_id="store-1"), path).ok is True


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_store_absent_from_the_snapshot_fails_closed(path: str) -> None:
    """R12: an UNAVAILABLE eligibility read denies exactly like a positive blacklist hit."""
    result = check(make_bid(store_id="store-unknown"), path)
    assert result.ok is False
    assert any("trust_snapshot_unavailable" in reason for reason in result.reasons)


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_an_unusable_trust_snapshot_fails_closed(path: str) -> None:
    for snapshot in ({}, None, "not-a-snapshot"):
        assert check(make_bid(), path, snapshot=snapshot).ok is False


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_schema_invalid_bid_is_rejected(path: str) -> None:
    bid = make_bid()
    del bid["agent_version"]
    result = check(bid, path)
    assert result.ok is False
    assert any("schema_invalid" in reason for reason in result.reasons)


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_bid_with_a_smuggled_field_is_rejected(path: str) -> None:
    assert check({**make_bid(), "network_fee": 0.0}, path).ok is False


# --- the boundary's own contract ----------------------------------------------------------


def test_an_unknown_path_is_refused_rather_than_guessed_at() -> None:
    for path in ("HOSTED", "internal", "", "hosted "):
        result = validate_bid(make_bid(), path=path, trust_snapshot=make_snapshot_table(), now=NOW)
        assert result.ok is False
        assert any("unknown_path" in reason for reason in result.reasons)


def test_the_boundary_never_raises_on_malformed_input() -> None:
    """A boundary that threw would make "reject" and "crash" indistinguishable to the caller."""
    for bid in (None, "not-a-bid", 42, [], {"claims": "not-a-list"}):
        result = validate_bid(bid, path=HOSTED_PATH, trust_snapshot=make_snapshot_table(), now=NOW)
        assert result.ok is False
        assert result.reasons


def test_a_rejected_bid_is_never_flagged_for_verification() -> None:
    """Rejected is not "admitted pending verification": there is nothing left to verify."""
    rejected = check(
        make_bid(
            claims=[make_claim("spf", 30, dict(ASSERTED_PROVENANCE))],
            store_id="store-bad",
        ),
        EXTERNAL_PATH,
    )
    assert rejected.ok is False
    assert rejected.requires_verification is False
    assert rejected.unverified_claim_indexes == []


def test_an_admitted_bid_carries_no_reasons() -> None:
    result = check(make_bid(), HOSTED_PATH)
    assert result.ok is True
    assert list(result.reasons) == []


def test_the_result_is_serializable_for_the_rejection_the_seller_sees() -> None:
    dumped = check(make_bid(store_id="store-bad"), EXTERNAL_PATH).model_dump()
    assert dumped["ok"] is False
    assert dumped["path"] == EXTERNAL_PATH
    assert isinstance(dumped["reasons"], list) and dumped["reasons"]


def test_validate_bid_accepts_a_model_as_well_as_a_mapping() -> None:
    from packages.contracts import Bid

    model = Bid.model_validate(make_bid())
    assert (
        validate_bid(model, path=HOSTED_PATH, trust_snapshot=make_snapshot_table(), now=NOW).ok
        is True
    )


# --- timestamp handling -------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00",
        1767225600.0,
    ],
)
def test_parse_timestamp_reads_every_spelling_the_system_produces(value) -> None:
    assert parse_timestamp(value) is not None


def test_parse_timestamp_treats_a_naive_instant_as_utc() -> None:
    """Otherwise expiry would depend on the host's timezone, which is not a property a contract has."""
    assert parse_timestamp("2026-01-01T00:00:00") == parse_timestamp("2026-01-01T00:00:00Z")


@pytest.mark.parametrize("value", [None, "", "  ", "not-a-date", True, {}])
def test_parse_timestamp_refuses_what_it_cannot_read(value) -> None:
    assert parse_timestamp(value) is None


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_an_unparseable_expiry_fails_closed(path: str) -> None:
    result = check(make_bid(offer=make_offer(expires_at="soon")), path)
    assert result.ok is False
    assert any("offer_expiry" in reason for reason in result.reasons)


# ---------------------------------------------------------------------------------------------
# Reason codes are part of the contract, not decoration.
#
# The exchange logs them and the seller-facing rejection quotes them, so the STRING matters, not
# only the boolean beside it. Without these, the whole per-claim provenance branch could be
# deleted and every other test in this file would still pass — the schema check would reject the
# same payloads for a different reason, and nobody would learn that the boundary had stopped
# explaining itself.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_missing_provenance_is_reported_by_its_own_reason_code(path: str) -> None:
    result = check(make_bid(claims=[make_claim("spf", 30, None)]), path)
    assert any(reason.startswith("claim_without_provenance:0") for reason in result.reasons), (
        f"expected a claim_without_provenance reason naming claim 0, got {list(result.reasons)}"
    )


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_an_empty_provenance_source_is_reported_by_its_own_reason_code(path: str) -> None:
    bid = make_bid(claims=[make_claim("spf", 30, {**HOOK_PROVENANCE, "source": ""})])
    result = check(bid, path)
    assert any(reason.startswith("claim_provenance_empty_source:0") for reason in result.reasons), (
        f"expected a claim_provenance_empty_source reason, got {list(result.reasons)}"
    )


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_an_unknown_provenance_source_is_reported_by_its_own_reason_code(path: str) -> None:
    bid = make_bid(claims=[make_claim("spf", 30, {**HOOK_PROVENANCE, "source": "vibes"})])
    result = check(bid, path)
    assert any(
        reason.startswith("claim_provenance_unknown_source:0:vibes") for reason in result.reasons
    ), (
        f"expected a claim_provenance_unknown_source reason naming 'vibes', got {list(result.reasons)}"
    )


def test_the_hosted_rejection_names_the_claim_and_the_source() -> None:
    bid = make_bid(
        claims=[
            make_claim("free_returns", "30 days", dict(HOOK_PROVENANCE)),
            make_claim("spf", 30, dict(ASSERTED_PROVENANCE)),
        ]
    )
    result = check(bid, HOSTED_PATH)
    assert "hosted_non_hook_provenance:1:seller_asserted" in list(result.reasons), (
        f"the rejection must say WHICH claim and WHICH source, got {list(result.reasons)}"
    )


def test_the_reason_vocabulary_is_stable() -> None:
    """These strings cross a network boundary. Renaming one is a contract change."""
    from contracts import boundary

    assert boundary.REASON_UNKNOWN_PATH == "unknown_path"
    assert boundary.REASON_SCHEMA_INVALID == "schema_invalid"
    assert boundary.REASON_CLAIM_WITHOUT_PROVENANCE == "claim_without_provenance"
    assert boundary.REASON_CLAIM_PROVENANCE_EMPTY_SOURCE == "claim_provenance_empty_source"
    assert boundary.REASON_CLAIM_PROVENANCE_UNKNOWN_SOURCE == "claim_provenance_unknown_source"
    assert boundary.REASON_HOSTED_NON_HOOK_PROVENANCE == "hosted_non_hook_provenance"
    assert boundary.REASON_OFFER_EXPIRED == "offer_expired"
    assert boundary.REASON_OFFER_EXPIRY_MISSING == "offer_expiry_missing"
    assert boundary.REASON_OFFER_EXPIRY_UNPARSEABLE == "offer_expiry_unparseable"
    assert boundary.REASON_STORE_BLACKLISTED == "store_blacklisted"
    assert boundary.REASON_TRUST_SNAPSHOT_UNAVAILABLE == "trust_snapshot_unavailable"


def test_the_provenance_source_partition_is_the_whole_closed_enum() -> None:
    """Guards the parametrized tests above: emptying either constant would turn them into SKIPS,
    which pytest reports as neither a pass nor a failure and nobody reads."""
    from packages.contracts import ProvenanceSource

    assert len(HOOK_PROVENANCE_SOURCES) == 6
    assert len(NON_HOOK_PROVENANCE_SOURCES) == 1
    assert HOOK_PROVENANCE_SOURCES.isdisjoint(NON_HOOK_PROVENANCE_SOURCES)
    assert HOOK_PROVENANCE_SOURCES | NON_HOOK_PROVENANCE_SOURCES == {
        member.value for member in ProvenanceSource
    }


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_blacklist_flag_that_is_truthy_but_not_true_still_denies(path: str) -> None:
    """R12 fail-closed. A row spelling the flag `1` or `"yes"` is a blacklisted store, and the
    TypeScript peer reads it the same way — a strict `=== true` there admitted all of these."""
    for flag in (True, 1, "yes", "true", [0]):
        snapshot = {"store-1": {"store_id": "store-1", "score": 0.6, "blacklisted": flag}}
        result = check(make_bid(), path, snapshot=snapshot)
        assert result.ok is False, f"blacklisted={flag!r} was admitted"
        assert any("store_blacklisted" in reason for reason in result.reasons)

    # Control: the falsy spellings still admit, so this is not "reject every row".
    for flag in (False, 0, "", None):
        snapshot = {"store-1": {"store_id": "store-1", "score": 0.6, "blacklisted": flag}}
        assert check(make_bid(), path, snapshot=snapshot).ok is True


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_an_unhashable_store_id_is_rejected_rather_than_raising(path: str) -> None:
    """A malformed wire payload must not crash the public boundary: `{}.get([1])` raises TypeError,
    and a boundary that throws makes "reject" and "500" indistinguishable to the caller."""
    for store_id in ({"a": 1}, ["x"], {1, 2}):
        result = check(make_bid(store_id=store_id), path)
        assert result.ok is False
        assert result.reasons


# --- D52: the signing envelope at the external door ----------------------------------------


def _signed(**overrides):
    """A wire submission: `make_bid`'s store and auction, plus the five D52 fields and a
    signature. `make_submission` drops `signature`, which is itself a rejection case here."""
    payload = make_bid()
    payload.update(
        {
            "signer_id": "store-1",
            "key_id": "key-2026-01",
            "issued_at": "2026-06-01T00:00:00Z",
            "nonce": "nonce-0001",
            "schema_version": "1.0.0",
            "signature": "sig-deadbeef",
        }
    )
    payload.update(overrides)
    return payload


def test_validate_bid_alone_does_not_judge_the_signing_envelope() -> None:
    """The documented gap, pinned so it cannot become an accident again.

    `validate_bid` checks the R8/R18/S5 table against `Bid`, which carries no envelope. This
    assertion exists so the docstring's warning is a fact and not a hope — and so that a caller
    reaching for the external door has to reach for `validate_external_submission`.
    """
    from packages.contracts import missing_signing_fields

    unsigned = make_bid()
    assert missing_signing_fields(unsigned) == ["signer_id", "key_id", "issued_at", "nonce"]
    assert check(unsigned, EXTERNAL_PATH).ok is True


def test_an_unsigned_submission_is_rejected_at_the_external_door() -> None:
    """D52: a submission missing any of the five is Rejected at the boundary, before extraction
    and before verification, with the same finality as a bad signature. Before this check, a
    Tier-2 seller POSTing a bid with no signer_id, key_id, issued_at, nonce or signature got
    `ok=True, reasons=[]` from the only boundary function this package exported."""
    from packages.contracts import validate_external_submission

    unsigned = make_bid()  # a plain Bid: four envelope fields absent
    result = validate_external_submission(unsigned, trust_snapshot=make_snapshot_table(), now=NOW)
    assert result.ok is False, "an unsigned external submission was admitted"
    for field in ("signer_id", "key_id", "issued_at", "nonce"):
        assert f"signing_envelope_incomplete:{field}" in result.reasons

    # Control: the identical bid with the envelope on it is admitted, so this is not
    # "reject every external submission".
    control = validate_external_submission(_signed(), trust_snapshot=make_snapshot_table(), now=NOW)
    assert control.ok is True, control.reasons


@pytest.mark.parametrize("field", ("signer_id", "key_id", "issued_at", "nonce", "schema_version"))
def test_the_external_door_rejects_a_submission_missing_any_one_envelope_field(field: str) -> None:
    from packages.contracts import validate_external_submission

    payload = _signed()
    del payload[field]
    result = validate_external_submission(payload, trust_snapshot=make_snapshot_table(), now=NOW)
    assert result.ok is False
    assert any(reason.startswith("signing_envelope_incomplete") for reason in result.reasons), (
        result.reasons
    )


@pytest.mark.parametrize("signature", (None, "", "   ", 12345, [], {"a": 1}))
def test_the_external_door_rejects_a_submission_without_a_usable_signature(signature) -> None:
    """`signature` is the thing the envelope exists to carry. A submission whose signature is
    absent, blank or not even a string cannot be verified, so it is refused here rather than
    handed to T-044 to fail on."""
    from packages.contracts import REASON_SIGNATURE_MISSING, validate_external_submission

    result = validate_external_submission(
        _signed(signature=signature), trust_snapshot=make_snapshot_table(), now=NOW
    )
    assert result.ok is False
    assert REASON_SIGNATURE_MISSING in result.reasons


def test_the_external_door_still_applies_the_whole_r8_r18_table() -> None:
    """The envelope is an ADDITIONAL condition, not a replacement: a fully signed submission that
    is expired, or from a blacklisted store, still rejects."""
    from packages.contracts import validate_external_submission

    for overrides, needle in (
        ({"offer": make_offer(expires_at=LONG_EXPIRED)}, "offer_expired"),
        ({"store_id": "store-bad"}, "store_blacklisted"),
        ({"claims": [make_claim("spf", 30, None)]}, "claim_without_provenance"),
    ):
        result = validate_external_submission(
            _signed(**overrides), trust_snapshot=make_snapshot_table(), now=NOW
        )
        assert result.ok is False, overrides
        assert any(needle in reason for reason in result.reasons), result.reasons

    # ...and a signed, seller-asserted claim is still admitted and flagged (R18).
    flagged = validate_external_submission(
        _signed(claims=[make_claim("spf", 30, dict(ASSERTED_PROVENANCE))]),
        trust_snapshot=make_snapshot_table(),
        now=NOW,
    )
    assert flagged.ok is True, flagged.reasons
    assert flagged.requires_verification is True


def test_the_external_door_never_raises_on_a_hostile_submission() -> None:
    """The whole point of the boundary: "reject" and "500" must not be the same observable."""
    from packages.contracts import validate_external_submission

    for payload in (None, "a raw string", [1, 2, 3], 42, {}, {"store_id": {"a": 1}}):
        result = validate_external_submission(
            payload, trust_snapshot=make_snapshot_table(), now=NOW
        )
        assert result.ok is False
        assert result.reasons


# --- F4: a stated UTC offset is part of the instant, not decoration -------------------------


def test_a_stated_utc_offset_is_honoured_not_discarded() -> None:
    """`parse_timestamp` defaults NAIVE values to UTC. Replacing the tzinfo on an aware value
    instead — `parsed.replace(tzinfo=UTC)` — reads `...-08:00` as if it were `...Z`, which shifts
    a US-Pacific store's offer by eight hours. No other test in this suite uses a non-zero
    offset, so that mutation passed the whole suite."""
    for stated, equivalent_utc in (
        ("2026-01-01T00:00:00-08:00", "2026-01-01T08:00:00Z"),
        ("2026-01-01T00:00:00+05:30", "2025-12-31T18:30:00Z"),
        ("2026-01-01T12:00:00+00:00", "2026-01-01T12:00:00Z"),
    ):
        assert parse_timestamp(stated) == parse_timestamp(equivalent_utc), (
            f"{stated} and {equivalent_utc} are the same instant"
        )

    # ...and they are NOT the same instant as the naked wall clock, which is what dropping the
    # offset would make them.
    assert parse_timestamp("2026-01-01T00:00:00-08:00") != parse_timestamp("2026-01-01T00:00:00Z")


def test_the_offset_decides_admit_versus_reject_for_an_offer_at_the_edge() -> None:
    """A US-Pacific store's offer expiring at 00:00-08:00 is live at 04:00Z and dead at 09:00Z.
    Discarding the offset rejects it as expired eight hours early."""
    offer = make_offer(expires_at="2026-01-01T00:00:00-08:00")  # == 08:00Z

    live = check(make_bid(offer=offer), EXTERNAL_PATH, now="2026-01-01T04:00:00Z")
    assert live.ok is True, f"a live Pacific offer was rejected as expired: {live.reasons}"

    dead = check(make_bid(offer=offer), EXTERNAL_PATH, now="2026-01-01T09:00:00Z")
    assert dead.ok is False
    assert any("offer_expired" in reason for reason in dead.reasons)
