"""The dual-path bid boundary — every cell of the R8/R18/S5 table, and the fail-closed edges.

Every rejection here has a POSITIVE CONTROL beside it: the identical bid, changed only in the one
respect under test, must be admitted. A boundary that rejected everything would satisfy every
rejection assertion in this file and be worthless, so each one is paired.
"""

from __future__ import annotations

from typing import Any

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


# ---------------------------------------------------------------------------------------------
# T-135: the exclusivity property must not be defeated by MOVING the claim.
#
# `_claim_provenance_reasons` walked `bid.claims` and nothing else. But the Offer is INSIDE the
# bid boundary and carries claim material of its own: `offer.commitments` is a list of claims,
# and `offer.discount` is stamped with a `provenance` block exactly like a claim is. So a
# store-agent that put its seller-asserted claim in `offer.commitments` instead of `bid.claims`
# — or stamped `seller_asserted` on the discount that prices the offer — walked straight past
# R8. Same claim, same source, same bid; only the field moved.
#
# Measured before the fix, on the hosted path:
#     claim in bid.claims        -> ok=False ['hosted_non_hook_provenance:1:seller_asserted']
#     the SAME claim in the offer-> ok=True  []                      (with a 25% discount on it)
#
# The store-agent's own hook guard closes this for a Tier-1 seller, but the exchange does not
# hold the seller's ToolHooks facade and can never call it. `validate_bid` is the only door the
# exchange can run, so if the walk is not exhaustive here, it is not enforced anywhere.
# ---------------------------------------------------------------------------------------------


def _smuggling_offer(**overrides):
    """An offer carrying the relocated claim and the discount it was smuggled in to justify."""
    payload = dict(
        commitments=[make_claim("spf", 30, dict(ASSERTED_PROVENANCE))],
        discount={"type": "percentage", "value": 25.0, "provenance": dict(ASSERTED_PROVENANCE)},
    )
    payload.update(overrides)
    return make_offer(**payload)


def test_a_seller_asserted_claim_relocated_into_the_offer_is_still_refused() -> None:
    """THE exploit. A hosted bid whose only hook-clean field is `bid.claims`."""
    smuggled = make_bid(
        claims=[make_claim("free_returns", "30 days", dict(HOOK_PROVENANCE))],
        offer=_smuggling_offer(),
    )
    result = check(smuggled, HOSTED_PATH)
    assert result.ok is False, (
        "a hosted bid carrying a seller_asserted claim in offer.commitments and an unauthorised "
        "25% discount was ADMITTED — moving the claim out of bid.claims defeated R8 entirely"
    )
    assert any(reason.startswith("hosted_non_hook_provenance") for reason in result.reasons), (
        "the refusal must be the PROVENANCE refusal, not an incidental schema complaint: "
        f"got {list(result.reasons)}"
    )

    # Positive control: the identical bid with hook provenance everywhere is admitted, so this
    # is not "reject every offer that has commitments".
    control = make_bid(
        claims=[make_claim("free_returns", "30 days", dict(HOOK_PROVENANCE))],
        offer=make_offer(
            commitments=[make_claim("spf", 30, dict(HOOK_PROVENANCE))],
            discount={
                "type": "percentage",
                "value": 25.0,
                "provenance": dict(HOOK_PROVENANCE),
            },
        ),
    )
    assert check(control, HOSTED_PATH).ok is True, check(control, HOSTED_PATH).reasons


def test_an_offer_commitment_is_walked_on_its_own() -> None:
    """Isolate the site: the commitment alone rejects, with the discount left hook-clean."""
    bid = make_bid(offer=make_offer(commitments=[make_claim("spf", 30, dict(ASSERTED_PROVENANCE))]))
    result = check(bid, HOSTED_PATH)
    assert result.ok is False, f"offer.commitments is not walked: {list(result.reasons)}"
    assert any(reason.startswith("hosted_non_hook_provenance") for reason in result.reasons)

    control = make_bid(offer=make_offer(commitments=[make_claim("spf", 30, dict(HOOK_PROVENANCE))]))
    assert check(control, HOSTED_PATH).ok is True


def test_the_offer_discount_provenance_is_walked_on_its_own() -> None:
    """The THIRD claim-bearing site, and the one that actually moves money.

    `offer.discount` is not a claim in the `bid.claims` sense, but it carries the same
    `provenance` block, and `seller_asserted` on it means exactly what it means anywhere else:
    no hook minted this. A 25% discount the seller simply asserted is the payload the whole
    R8 exclusivity property exists to stop.
    """
    bid = make_bid(
        offer=make_offer(
            discount={
                "type": "percentage",
                "value": 25.0,
                "provenance": dict(ASSERTED_PROVENANCE),
            }
        )
    )
    result = check(bid, HOSTED_PATH)
    assert result.ok is False, f"offer.discount.provenance is not walked: {list(result.reasons)}"
    assert any(reason.startswith("hosted_non_hook_provenance") for reason in result.reasons)

    # Control: the same discount, hook-minted, is admitted. The refusal is about the SOURCE.
    assert check(make_bid(), HOSTED_PATH).ok is True


# ---------------------------------------------------------------------------------------------
# The CROSS-LANGUAGE parity table for the claim-bearing sites.
#
# `boundary.ts` is the second implementation of this door, and two doors that admit different
# bids are worse than one door with a hole — the seller picks whichever one lets the bid through.
# So the verdicts below are asserted VERBATIM here and, case for case and string for string, in
# `boundary.test.ts::T-135 parity`. Changing one side alone turns the other side red.
#
# `schema_invalid` reasons are filtered out of the comparison and only there: pydantic spells a
# location `offer.commitments.0.provenance` and ajv spells it its own way, and that text was
# never a cross-language contract. Every provenance reason IS.
# ---------------------------------------------------------------------------------------------

PARITY_TABLE: dict[str, dict] = {
    "claims_seller_asserted/hosted": {
        "ok": False,
        "reasons": ["hosted_non_hook_provenance:0:seller_asserted"],
        "requires_verification": False,
        "unverified_claim_indexes": [],
    },
    "claims_seller_asserted/external": {
        "ok": True,
        "reasons": [],
        "requires_verification": True,
        # R18 still routes a bid.claims assertion to verification, by index. Untouched.
        "unverified_claim_indexes": [0],
    },
    "offer_commitment_seller_asserted/hosted": {
        "ok": False,
        "reasons": ["hosted_non_hook_provenance:offer.commitments[0]:seller_asserted"],
        "requires_verification": False,
        "unverified_claim_indexes": [],
    },
    "offer_commitment_seller_asserted/external": {
        "ok": False,
        # NOT flagged: `unverified_claim_indexes` addresses `bid.claims`, so there is no handle
        # to hand the verification queue for this site. Refused, with the site named.
        "reasons": ["unverifiable_claim_site:offer.commitments[0]:seller_asserted"],
        "requires_verification": False,
        "unverified_claim_indexes": [],
    },
    "offer_commitment_unknown_source/hosted": {
        "ok": False,
        "reasons": ["claim_provenance_unknown_source:offer.commitments[0]:vibes"],
        "requires_verification": False,
        "unverified_claim_indexes": [],
    },
    "offer_commitment_without_provenance/hosted": {
        "ok": False,
        "reasons": ["claim_without_provenance:offer.commitments[0]"],
        "requires_verification": False,
        "unverified_claim_indexes": [],
    },
    "offer_discount_seller_asserted/hosted": {
        "ok": False,
        "reasons": ["hosted_non_hook_provenance:offer.discount:seller_asserted"],
        "requires_verification": False,
        "unverified_claim_indexes": [],
    },
    "offer_discount_seller_asserted/external": {
        "ok": False,
        "reasons": ["unverifiable_claim_site:offer.discount:seller_asserted"],
        "requires_verification": False,
        "unverified_claim_indexes": [],
    },
    "offer_discount_without_provenance/hosted": {
        "ok": False,
        # `Discount.provenance` is optional by schema, so this payload is schema-VALID. It is
        # still refused: the discount is the field that moves money, and "no provenance at all"
        # is not a weaker version of `seller_asserted`, it is the same statement with the label
        # torn off. Leaving it unjudged would reopen the relocation exploit one step further
        # down — drop the block instead of relabelling it and the 25% walks again.
        "reasons": ["claim_without_provenance:offer.discount"],
        "requires_verification": False,
        "unverified_claim_indexes": [],
    },
    "offer_without_a_discount/hosted": {
        "ok": True,
        "reasons": [],
        "requires_verification": False,
        "unverified_claim_indexes": [],
    },
    "all_hook_provenanced/hosted": {
        "ok": True,
        "reasons": [],
        "requires_verification": False,
        "unverified_claim_indexes": [],
    },
    "all_hook_provenanced/external": {
        "ok": True,
        "reasons": [],
        "requires_verification": False,
        "unverified_claim_indexes": [],
    },
}


def parity_bid(name: str) -> dict:
    """The payload for one parity case. Mirrored by `parityBid` in `boundary.test.ts`."""
    if name == "claims_seller_asserted":
        return make_bid(claims=[make_claim("spf", 30, dict(ASSERTED_PROVENANCE))])
    if name == "offer_commitment_seller_asserted":
        return make_bid(
            offer=make_offer(commitments=[make_claim("spf", 30, dict(ASSERTED_PROVENANCE))])
        )
    if name == "offer_commitment_unknown_source":
        return make_bid(
            offer=make_offer(
                commitments=[make_claim("spf", 30, {**HOOK_PROVENANCE, "source": "vibes"})]
            )
        )
    if name == "offer_commitment_without_provenance":
        return make_bid(offer=make_offer(commitments=[make_claim("spf", 30, None)]))
    if name == "offer_discount_seller_asserted":
        return make_bid(
            offer=make_offer(
                discount={
                    "type": "percentage",
                    "value": 25.0,
                    "provenance": dict(ASSERTED_PROVENANCE),
                }
            )
        )
    if name == "offer_discount_without_provenance":
        return make_bid(offer=make_offer(discount={"type": "percentage", "value": 25.0}))
    if name == "offer_without_a_discount":
        return make_bid(offer=make_offer(discount=None))
    if name == "all_hook_provenanced":
        return make_bid(
            offer=make_offer(commitments=[make_claim("spf", 30, dict(HOOK_PROVENANCE))])
        )
    raise AssertionError(f"unknown parity case {name!r}")


@pytest.mark.parametrize("case", sorted(PARITY_TABLE))
def test_the_claim_site_verdicts_match_the_typescript_peer(case: str) -> None:
    name, _, path = case.rpartition("/")
    expected = PARITY_TABLE[case]
    result = check(parity_bid(name), path)

    provenance_reasons = [r for r in result.reasons if not r.startswith("schema_invalid")]
    assert provenance_reasons == expected["reasons"], case
    assert result.ok is expected["ok"], (case, list(result.reasons))
    assert result.requires_verification is expected["requires_verification"], case
    assert list(result.unverified_claim_indexes) == expected["unverified_claim_indexes"], case


def test_the_parity_table_covers_every_claim_bearing_site_on_both_paths() -> None:
    """Guards the parametrization: a table someone quietly emptied would register zero cases,
    which pytest reports as neither a pass nor a failure and nobody reads."""
    from contracts import boundary

    assert len(PARITY_TABLE) == 12
    sites = {case.split("/")[0] for case in PARITY_TABLE}
    assert {
        "claims_seller_asserted",
        "offer_commitment_seller_asserted",
        "offer_discount_seller_asserted",
    } <= sites
    for path in BOTH_PATHS:
        assert any(case.endswith(f"/{path}") for case in PARITY_TABLE), path

    # The site labels the reasons are built from are the contract the table pins.
    assert boundary.OFFER_COMMITMENTS_SITE == "offer.commitments"
    assert boundary.OFFER_DISCOUNT_SITE == "offer.discount"
    assert boundary.REASON_UNVERIFIABLE_CLAIM_SITE == "unverifiable_claim_site"


def test_the_walk_reaches_the_offer_through_a_pydantic_model_too() -> None:
    """`validate_bid` accepts a `Bid` instance, not only a mapping, and the offer sites must be
    walked through attribute access exactly as they are through `dict.get`. A walk that only
    worked on wire dicts would be blind to every caller holding an extracted model."""
    from packages.contracts import Bid

    model = Bid.model_validate(
        make_bid(offer=make_offer(commitments=[make_claim("spf", 30, dict(ASSERTED_PROVENANCE))]))
    )
    result = validate_bid(model, path=HOSTED_PATH, trust_snapshot=make_snapshot_table(), now=NOW)
    assert result.ok is False, "the offer walk does not survive attribute access"
    assert "hosted_non_hook_provenance:offer.commitments[0]:seller_asserted" in list(result.reasons)

    clean = Bid.model_validate(make_bid())
    assert (
        validate_bid(clean, path=HOSTED_PATH, trust_snapshot=make_snapshot_table(), now=NOW).ok
        is True
    )


HOSTILE_OFFER_SHAPES: dict[str, dict] = {
    "commitments as a mapping": make_offer(
        commitments={"0": make_claim("x", 1, dict(ASSERTED_PROVENANCE))}
    ),
    "commitments as a string": make_offer(commitments="free_returns"),
    "discount as a list": make_offer(
        discount=[{"type": "percentage", "value": 25.0, "provenance": dict(ASSERTED_PROVENANCE)}]
    ),
    "discount as a bare number": make_offer(discount=25.0),
    "discount with a null provenance": make_offer(
        discount={"type": "percentage", "value": 25.0, "provenance": None}
    ),
    "discount with an empty provenance source": make_offer(
        discount={
            "type": "percentage",
            "value": 25.0,
            "provenance": {**HOOK_PROVENANCE, "source": ""},
        }
    ),
    "discount with an unknown provenance source": make_offer(
        discount={
            "type": "percentage",
            "value": 25.0,
            "provenance": {**HOOK_PROVENANCE, "source": "vibes"},
        }
    ),
}


@pytest.mark.parametrize("name", sorted(HOSTILE_OFFER_SHAPES))
@pytest.mark.parametrize("path", BOTH_PATHS)
def test_the_offer_walk_refuses_hostile_shapes_rather_than_raising(name: str, path: str) -> None:
    """The relocation exploit's next move is a MALFORMED relocation — put the claim somewhere the
    walk has to guess at. Every one of these is refused on both paths, and none of them raises.

    Mirrored in `boundary.test.ts`. Only `ok` is compared across the two languages here, not the
    reason text: pydantic enumerates a mapping's keys where ajv calls it a shape error, and that
    spelling was never a cross-language contract. "Is this bid admitted" is, and it is the only
    thing an attacker cares about.
    """
    result = check(make_bid(offer=HOSTILE_OFFER_SHAPES[name]), path)
    assert result.ok is False, f"{name}/{path} was admitted"
    assert result.reasons


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_the_offer_shapes_that_are_legitimately_quiet_still_admit(path: str) -> None:
    """The positive controls, so the block above is not "reject every offer"."""
    for offer in (
        make_offer(discount=None),
        make_offer(commitments=[]),
        make_offer(commitments=[make_claim("x", 1, dict(HOOK_PROVENANCE))]),
        make_offer(),
    ):
        result = check(make_bid(offer=offer), path)
        assert result.ok is True, (offer, path, list(result.reasons))


# ---------------------------------------------------------------------------------------------
# T-177 — the price wall, on the door the exchange actually runs.
#
# The wall reconciling a bid's stated price against its declared depth was built on the EMITTING
# side (`store-agent/hooks/provenance.py`). Measured through the real door on the clean tree:
#
#     hosted bid, genuine hook-minted 20% grant, declares 20%, charges 15.00 on a 100.00 list
#     validate_bid(bid, path="hosted")  ->  ok=True, reasons=[]
#
# So the wall protected only bids our own runtime produced, and a Tier-2 store not running our
# runtime walked past it. These pin the same arithmetic on the validating side.
# ---------------------------------------------------------------------------------------------


def priced_offer(unit: Any, total: Any, depth: Any = 20.0, kind: str = "percentage") -> dict:
    """An offer that states a price and declares a depth, hook-provenanced throughout."""
    return make_offer(
        unit_price=unit,
        total_price=total,
        discount={"type": kind, "value": depth, "provenance": dict(HOOK_PROVENANCE)},
    )


def list_price_claim(value: Any = 100.0) -> dict:
    """The list price a bid CARRIES — `get_product_fact(product_ref, "list_price")` mints it."""
    return make_claim("list_price", value, dict(HOOK_PROVENANCE))


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_bid_may_not_charge_more_off_than_the_depth_it_declares(path: str) -> None:
    """The T-177 reproduction, verbatim, through `validate_bid`.

    Every piece of paperwork on this bid is genuine: a hook-minted 20% authorization, a
    hook-minted list price of 100.00, a hook-provenanced 20% discount on the offer. It charges
    15.00. An authorized 20% prices out at 80.00, so 65 currency units of unauthorised discount
    sat INSIDE every wall this boundary had — the depth is a description of a price, and nothing
    here had ever made the description true.
    """
    bid = make_bid(
        claims=[list_price_claim(100.0), make_claim("authorized_discount_pct", 20.0)],
        offer=priced_offer(15.0, 15.0),
    )
    result = check(bid, path)
    assert result.ok is False, "a 20% grant licensed an 85% discount"
    assert "price_under_declared_depth:offer.unit_price" in list(result.reasons), result.reasons

    # Control: the SAME bid at the price that depth actually prices out at is admitted. The
    # refusal is about the arithmetic, not about carrying a list price or a discount at all.
    honest = make_bid(
        claims=[list_price_claim(100.0), make_claim("authorized_discount_pct", 20.0)],
        offer=priced_offer(80.0, 80.0),
    )
    assert check(honest, path).ok is True, check(honest, path).reasons


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_the_list_price_is_read_from_offer_commitments_too(path: str) -> None:
    """The relocation move, applied to the evidence instead of to the claim: move the list price
    out of `bid.claims` and the wall must still find it. `offer.commitments` is `list[Claim]` on
    the object that carries the price, and a walk that read one site would be a naming
    convention again."""
    bid = make_bid(
        claims=[make_claim()],
        offer=make_offer(
            unit_price=15.0,
            total_price=15.0,
            commitments=[list_price_claim(100.0)],
            discount={"type": "percentage", "value": 20.0, "provenance": dict(HOOK_PROVENANCE)},
        ),
    )
    result = check(bid, path)
    assert result.ok is False, "relocating the list price into the offer defeated the price wall"
    assert "price_under_declared_depth:offer.unit_price" in list(result.reasons)


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_the_total_may_not_undercut_the_depth_the_offer_declares(path: str) -> None:
    """The relation that needs NO catalog at all, and the one that still holds while the quantity
    semantics of `total_price` are undecided: `Offer` carries no quantity, but every quantity is
    at least one, so a total can only ever be LARGER than one discounted unit. A 20% discount off
    a stated 100.00 cannot produce a total of 15.00 under any reading of the field."""
    result = check(make_bid(offer=priced_offer(100.0, 15.0)), path)
    assert result.ok is False
    assert "price_under_declared_depth:offer.total_price" in list(result.reasons), result.reasons

    # Control: the honest total for that depth, and a total for a LARGER quantity, both admit.
    for total in (80.0, 240.0):
        assert check(make_bid(offer=priced_offer(100.0, total)), path).ok is True


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_the_price_wall_is_one_sided(path: str) -> None:
    """A price ABOVE what the depth prices out at takes less off than was declared. There is
    nothing there for a wall about authorization to refuse, and refusing it would turn every
    rounding-up into an outage."""
    generous = make_bid(claims=[list_price_claim(100.0)], offer=priced_offer(95.0, 95.0))
    assert check(generous, path).ok is True, check(generous, path).reasons


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_rounded_price_a_fraction_of_a_cent_under_the_exact_one_still_admits(path: str) -> None:
    """19.99 less an honest 15% is 16.9915 and no bid states that: money is quoted to the cent,
    so the honest rounded price sits under the exact one. A wall tightened to the float would
    refuse almost every real product."""
    bid = make_bid(claims=[list_price_claim(19.99)], offer=priced_offer(16.99, 16.99, depth=15.0))
    assert check(bid, path).ok is True, check(bid, path).reasons

    # ...and one cent of slack is all there is: a whole currency unit under still refuses.
    over = make_bid(claims=[list_price_claim(19.99)], offer=priced_offer(15.99, 15.99, depth=15.0))
    assert check(over, path).ok is False


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_depth_the_boundary_cannot_read_is_refused_rather_than_skipped(path: str) -> None:
    """A depth in a form this door cannot reconcile is a refusal, not an abstention. An
    amount-off cannot be compared with a stated price without re-deriving what the offer means,
    and "the wall does not apply to me" is exactly the shape an attacker reaches for next."""
    for offer, needle in (
        (priced_offer(15.0, 15.0, depth=10.0, kind="amount"), "offer.discount:amount"),
        (priced_offer(15.0, 15.0, depth=150.0), "offer.discount:depth_out_of_range"),
        (priced_offer(15.0, 15.0, depth=-20.0), "offer.discount:depth_out_of_range"),
        (priced_offer(15.0, 15.0, depth="20"), "offer.discount:depth_not_a_number"),
        (priced_offer(15.0, 15.0, depth=True), "offer.discount:depth_not_a_number"),
    ):
        result = check(make_bid(claims=[list_price_claim(100.0)], offer=offer), path)
        assert result.ok is False, (offer, path)
        assert any(needle in reason for reason in result.reasons), (needle, list(result.reasons))

    # Control: the same offer with a depth the door CAN read, priced honestly, is admitted.
    ok = check(make_bid(claims=[list_price_claim(100.0)], offer=priced_offer(80.0, 80.0)), path)
    assert ok.ok is True, ok.reasons


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_zero_discount_needs_no_reconciliation_but_still_answers_to_the_list_price(
    path: str,
) -> None:
    """A discount that takes nothing off prices out at the price itself — so a bid declaring 0%
    and charging under its own carried list price is a discount that entered through no hook at
    all, and is refused."""
    under = make_bid(
        claims=[list_price_claim(100.0)],
        offer=priced_offer(60.0, 60.0, depth=0.0),
    )
    assert check(under, path).ok is False
    assert "price_under_declared_depth:offer.unit_price" in list(check(under, path).reasons)

    at_list = make_bid(
        claims=[list_price_claim(100.0)], offer=priced_offer(100.0, 100.0, depth=0.0)
    )
    assert check(at_list, path).ok is True, check(at_list, path).reasons


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_an_illegible_or_contradictory_list_price_is_refused_not_read_past(path: str) -> None:
    """A bid does not get to disable the wall by making its own evidence unreadable, and it does
    not get to pick which wall it is measured against by carrying two list prices."""
    unreadable = make_bid(claims=[list_price_claim("n/a")], offer=priced_offer(15.0, 15.0))
    result = check(unreadable, path)
    assert result.ok is False
    assert any("unreadable_list_price" in reason for reason in result.reasons), result.reasons

    ambiguous = make_bid(
        claims=[list_price_claim(100.0), list_price_claim(120.0)],
        offer=priced_offer(80.0, 80.0),
    )
    result = check(ambiguous, path)
    assert result.ok is False
    assert any("ambiguous_list_price" in reason for reason in result.reasons), result.reasons

    # Control: the same list price stated twice is not a contradiction.
    twice = make_bid(
        claims=[list_price_claim(100.0), list_price_claim(100.0)],
        offer=priced_offer(80.0, 80.0),
    )
    assert check(twice, path).ok is True, check(twice, path).reasons


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_the_wall_abstains_deliberately_when_the_bid_carries_no_list_price(path: str) -> None:
    """THE DOCUMENTED GAP, pinned so it cannot be mistaken for coverage.

    The boundary holds no catalog. A bid that declares a depth and carries no `list_price` claim
    gives the first relation no number to be a percentage OF, and it reports nothing rather than
    inventing a lookup it cannot do. Such a bid is measured by the `total_price` relation alone,
    which is why the offer below — 20% off, charging 15.00, internally consistent — is admitted.

    Closing this needs a list price the EXCHANGE supplies from its own roster — which is now the
    `list_prices` parameter, pinned in `test_boundary_price_roster.py`. This test is what says
    the roster is OPT-IN: with none passed the abstention is still exactly here, unchanged, and
    every assertion below is the one it was written with. Removing the abstention outright rather
    than giving callers a way to close it would not be fail-closed, it would be closed:
    `make_offer()` itself declares 10% and carries no list price, as does every honest bid in
    this suite.
    """
    silent = make_bid(offer=priced_offer(15.0, 15.0))
    result = check(silent, path)
    assert result.ok is True, result.reasons
    assert not any(reason.startswith("price_") for reason in result.reasons)

    # And the moment the bid DOES say what it is discounting from, the same offer refuses.
    assert (
        check(make_bid(claims=[list_price_claim(100.0)], offer=priced_offer(15.0, 15.0)), path).ok
        is False
    )


# --- the cross-language price table, mirrored in `boundary.test.ts::T-177 price parity` -------

PRICE_PARITY_TABLE: dict[str, dict] = {
    "charges_under_the_carried_list_price": {
        "ok": False,
        "reasons": ["price_under_declared_depth:offer.unit_price"],
    },
    "total_under_the_stated_unit_price": {
        "ok": False,
        "reasons": ["price_under_declared_depth:offer.total_price"],
    },
    "amount_discount": {
        "ok": False,
        "reasons": ["price_unreconcilable:offer.discount:amount"],
    },
    "depth_out_of_range": {
        "ok": False,
        "reasons": ["price_unreconcilable:offer.discount:depth_out_of_range"],
    },
    "ambiguous_list_price": {
        "ok": False,
        "reasons": ["price_unreconcilable:offer.unit_price:ambiguous_list_price"],
    },
    "unreadable_list_price": {
        "ok": False,
        "reasons": ["price_unreconcilable:offer.unit_price:unreadable_list_price"],
    },
    "honest_price": {"ok": True, "reasons": []},
    # The abstention, pinned on BOTH doors: they must be blind to the same thing, or the seller
    # picks the blinder one.
    "no_list_price_carried": {"ok": True, "reasons": []},
}


def price_parity_bid(name: str) -> dict:
    """The payload for one price-parity case. Mirrored by `pricedParityBid` in `boundary.test.ts`."""
    if name == "charges_under_the_carried_list_price":
        return make_bid(claims=[list_price_claim(100.0)], offer=priced_offer(15.0, 15.0))
    if name == "total_under_the_stated_unit_price":
        return make_bid(offer=priced_offer(100.0, 15.0))
    if name == "amount_discount":
        return make_bid(offer=priced_offer(49.0, 44.1, depth=10.0, kind="amount"))
    if name == "depth_out_of_range":
        return make_bid(offer=priced_offer(49.0, 44.1, depth=150.0))
    if name == "ambiguous_list_price":
        return make_bid(
            claims=[list_price_claim(100.0), list_price_claim(120.0)],
            offer=priced_offer(80.0, 80.0),
        )
    if name == "unreadable_list_price":
        return make_bid(claims=[list_price_claim("n/a")], offer=priced_offer(80.0, 80.0))
    if name == "honest_price":
        return make_bid(claims=[list_price_claim(100.0)], offer=priced_offer(80.0, 80.0))
    if name == "no_list_price_carried":
        return make_bid(offer=priced_offer(15.0, 15.0))
    raise AssertionError(f"unknown price parity case {name!r}")


@pytest.mark.parametrize("case", sorted(PRICE_PARITY_TABLE))
def test_the_price_verdicts_match_the_typescript_peer(case: str) -> None:
    expected = PRICE_PARITY_TABLE[case]
    result = check(price_parity_bid(case), HOSTED_PATH)
    priced = [r for r in result.reasons if not r.startswith("schema_invalid")]
    assert priced == expected["reasons"], (case, list(result.reasons))
    assert result.ok is expected["ok"], (case, list(result.reasons))


def test_the_price_parity_table_is_not_quietly_empty() -> None:
    """Guards the parametrization: an emptied table registers zero cases, which reads as green."""
    from contracts import boundary

    assert len(PRICE_PARITY_TABLE) == 8
    assert boundary.OFFER_UNIT_PRICE_SITE == "offer.unit_price"
    assert boundary.OFFER_TOTAL_PRICE_SITE == "offer.total_price"
    assert boundary.REASON_PRICE_UNDER_DECLARED_DEPTH == "price_under_declared_depth"
    assert boundary.REASON_PRICE_UNRECONCILABLE == "price_unreconcilable"
    assert boundary.LIST_PRICE_CLAIM_KEY == "list_price"
    assert boundary.PRICE_RECONCILIATION_TOLERANCE == 0.01


def test_the_price_walk_survives_attribute_access_and_hostile_offers() -> None:
    """It must work on an extracted `Bid` as well as on a wire dict, and it must never raise."""
    from packages.contracts import Bid

    model = Bid.model_validate(
        make_bid(claims=[list_price_claim(100.0)], offer=priced_offer(15.0, 15.0))
    )
    result = validate_bid(model, path=HOSTED_PATH, trust_snapshot=make_snapshot_table(), now=NOW)
    assert result.ok is False, "the price walk does not survive attribute access"
    assert "price_under_declared_depth:offer.unit_price" in list(result.reasons)

    for offer in (None, "an offer", 42, [1, 2], True, {}, {"unit_price": float("nan")}):
        for path in BOTH_PATHS:
            verdict = check(make_bid(offer=offer), path)
            assert verdict.ok is False
            assert verdict.reasons


# ---------------------------------------------------------------------------------------------
# T-184's Python peer, and the parity narrowings this lane made.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", BOTH_PATHS)
@pytest.mark.parametrize("store_id", ("__proto__", "constructor", "toString", "valueOf"))
def test_a_store_id_naming_a_prototype_member_is_an_unavailable_read(path: str, store_id) -> None:
    """`table[key]` in JavaScript is not a lookup when the key comes off the wire — it walks the
    prototype chain, and `store_id: "__proto__"` returned `Object.prototype`: an object with no
    `blacklisted`, so the TypeScript door ADMITTED the bid with no trust row behind it. Python
    never had the bug (a dict miss is a miss), so this pins the verdict the two doors must share.
    """
    result = check(make_bid(store_id=store_id), path)
    assert result.ok is False, f"store_id={store_id!r} was admitted with no trust row"
    assert any("trust_snapshot_unavailable" in reason for reason in result.reasons)


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_an_empty_container_blacklist_flag_denies_the_way_javascript_reads_it(path: str) -> None:
    """A measured ok-divergence: `{"blacklisted": []}` was ADMITTED here (`bool([])` is False) and
    REFUSED by the TypeScript door (`[]` is truthy), so a seller who can shape the snapshot row
    picked whichever door said yes. JavaScript's truthiness is the fail-closed one of the two,
    so both doors now use it."""
    for flag in ([], {}, (), [0], "0", " "):
        snapshot = {"store-1": {"store_id": "store-1", "score": 0.6, "blacklisted": flag}}
        result = check(make_bid(), path, snapshot=snapshot)
        assert result.ok is False, f"blacklisted={flag!r} was admitted"
        assert any("store_blacklisted" in reason for reason in result.reasons)

    # Control, unchanged: the falsy spellings JavaScript agrees are falsy still admit.
    for flag in (False, 0, 0.0, "", None):
        snapshot = {"store-1": {"store_id": "store-1", "score": 0.6, "blacklisted": flag}}
        assert check(make_bid(), path, snapshot=snapshot).ok is True, flag


# ---------------------------------------------------------------------------------------------
# T-195 — the generated model may not be looser than the schema it is generated from.
# ---------------------------------------------------------------------------------------------


def test_offer_commitments_may_not_be_spelled_null() -> None:
    """`$defs/Offer/properties/commitments` is a NON-nullable array with `default: []`, so ajv
    refuses `commitments: null` — but datamodel-code-generator widened every defaulted field to
    `| None`, and pydantic accepted it. Two halves of one contract disagreeing about a shape is
    bad on its own; worse, that nullable spelling was the ONE shape of `offer.commitments` the
    claim walk skipped, on the field the walk exists to cover.
    """
    import pydantic

    from packages.contracts import Offer

    with pytest.raises(pydantic.ValidationError):
        Offer.model_validate(make_offer(commitments=None))

    for path in BOTH_PATHS:
        result = check(make_bid(offer=make_offer(commitments=None)), path)
        assert result.ok is False, "commitments: null is admitted here and refused by ajv"
        assert any(reason.startswith("schema_invalid") for reason in result.reasons)

    # Controls: the two spellings the schema DOES allow still admit.
    for offer in (
        make_offer(commitments=[]),
        {k: v for k, v in make_offer().items() if k != "commitments"},
    ):
        assert check(make_bid(offer=offer), HOSTED_PATH).ok is True


def test_the_schema_is_the_thing_the_generated_model_was_made_to_match() -> None:
    """Pins the DIRECTION of the T-195 fix: the schema was already right and the generator was
    wrong, so a future "fix" that makes the schema nullable to match a regenerated model would
    be reopening the hole from the other end."""
    import json
    import pathlib

    from packages.contracts import Offer

    schema = json.loads(
        (
            pathlib.Path(__file__).resolve().parent.parent / "schemas" / "protocol.schema.json"
        ).read_text(encoding="utf-8")
    )
    commitments = schema["$defs"]["Offer"]["properties"]["commitments"]
    assert commitments["type"] == "array", commitments
    assert commitments.get("default") == []

    assert Offer.model_validate(make_offer(commitments=[])).commitments == []


# ---------------------------------------------------------------------------------------------
# Found by this lane's own adversarial pass, after the price wall went in.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_one_shot_iterator_of_claims_is_not_a_list_of_claims(path: str) -> None:
    """A generator in `claims` was walked ONCE and then gone.

    `model_validate` drains it first, so by the time the provenance walk reached it there was
    nothing left to judge — every claim in the bid went unexamined and the bid was admitted. The
    price wall made it worse by adding a SECOND reader of the same field: whichever walk ran
    second saw an empty list. The TypeScript peer asks `Array.isArray` and calls anything else
    `schema_invalid`, so this was also a live ok-divergence. Both doors now say the same thing.
    """
    smuggled = make_bid()
    smuggled["claims"] = iter([make_claim("spf", 30, dict(ASSERTED_PROVENANCE))])
    result = check(smuggled, path)
    assert result.ok is False, "a generator of claims was admitted with its claims unread"
    assert any(reason.startswith("schema_invalid") for reason in result.reasons)

    for shape in ({"0": make_claim()}, {make_claim()["key"]}, iter([])):
        hidden = make_bid()
        hidden["claims"] = shape
        assert check(hidden, path).ok is False, shape

    # ...and the same at the offer's own claim-bearing site.
    for shape in (iter([make_claim()]), {"0": make_claim()}, frozenset({"free_returns"})):
        assert check(make_bid(offer=make_offer(commitments=shape)), path).ok is False, shape

    # Control: a real list, and a tuple of the same claims, are still walked and admitted.
    for shape in ([make_claim()], (make_claim(),)):
        assert check(make_bid(claims=shape), path).ok is True


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_the_boundary_refuses_rather_than_raising_on_an_object_that_fights_back(path: str) -> None:
    """ "Reject" and "500" must not be the same observable — including when the READ itself is
    hostile. An object whose `__getattr__` raises, or whose `__iter__` does, escaped as a
    RuntimeError out of the public boundary."""

    class Exploding:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("boom")

    class UnwalkableClaims(list):
        def __iter__(self) -> Any:
            raise RuntimeError("boom-iter")

    hostile: list[Any] = [
        Exploding(),
        make_bid(offer=Exploding()),
        {**make_bid(), "claims": UnwalkableClaims()},
        make_bid(offer=make_offer(commitments=UnwalkableClaims())),
        iter([1, 2, 3]),
    ]
    for payload in hostile:
        result = check(payload, path)
        assert result.ok is False
        assert result.reasons, "a refusal must still say why"


@pytest.mark.parametrize("path", BOTH_PATHS)
def test_a_trust_snapshot_row_that_is_not_an_object_is_an_unavailable_read(path: str) -> None:
    """R12's widest remaining hole, and the widest measured ok-divergence between the two doors.

    A row that is not an object is not a row: `getattr(1, "blacklisted", False)` answers `False`,
    so `{"store-1": 1}` — a snapshot mangled in transit, or half-decoded — read as "present and
    not blacklisted" and ADMITTED the store, on the one check R12 exists to make fail closed.
    The TypeScript peer's `readRecord` refused every one of these all along; a seller who could
    shape the snapshot row simply submitted at the door that said yes.

    Note this is the ROW, not the snapshot. A non-mapping SNAPSHOT was always refused, which is
    what made the row case easy to mistake for covered.
    """
    for row in (1, "x", [], True, 3.5, 0, "", 0.0, ["blacklisted"], None):
        snapshot = {"store-1": row}
        result = check(make_bid(), path, snapshot=snapshot)
        assert result.ok is False, f"a trust row of {row!r} admitted the store"
        assert any("trust_snapshot_unavailable" in reason for reason in result.reasons)

    # Control: a real row still admits, and a real blacklisted row still denies for its own
    # reason rather than being swept up as unavailable.
    assert check(make_bid(), path, snapshot=make_snapshot_table()).ok is True
    denied = check(make_bid(store_id="store-bad"), path)
    assert any("store_blacklisted" in reason for reason in denied.reasons)
