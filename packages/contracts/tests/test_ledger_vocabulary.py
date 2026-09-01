"""The ledger event vocabulary (C11/D24) and the pixel↔webhook join.

C11 freezes the kind vocabulary JOINTLY across the redirect path and the Shopify checkout path:
both emit the same kinds, so a proof written against one holds for the other. The set is asserted
exactly — extra kinds are as much a violation as missing ones, because a kind only one path emits
is a kind the other path's proof cannot cover.
"""

from __future__ import annotations

import pytest

from packages.contracts import (
    JOINABLE_KINDS,
    LEDGER_EVENT_KINDS,
    LEDGER_JOIN_KEYS,
    LEDGER_PAYLOAD_SHAPES,
    LedgerEvent,
    LedgerEventKind,
    join_key_view,
    validate_ledger_payload,
)

FROZEN_KINDS = {
    # DESIGN §Interfaces LedgerEvent enum
    "bid_placed",
    "shown",
    "accepted",
    "code_created",
    "checkout_redirect",
    "checkout_pixel",
    "order_paid",
    "order_fulfilled",
    "refund",
    "feedback",
    "reconciled",
    "claim_verified",
    "policy_event",
    # extended exactly once, in T-010, per D24
    "auction_opened",
    "auction_closed",
    "offer_integrity",
    "blacklisted",
    "blacklist_expired",
}


def test_the_kind_vocabulary_is_exactly_the_frozen_set() -> None:
    assert {member.value for member in LedgerEventKind} == FROZEN_KINDS
    assert LEDGER_EVENT_KINDS == FROZEN_KINDS
    assert len(FROZEN_KINDS) == 18


def test_the_enum_is_exported_under_both_accepted_names() -> None:
    import packages.contracts as contracts

    assert contracts.LedgerEventKindEnum is contracts.LedgerEventKind


def test_the_kind_annotation_on_the_model_is_the_same_vocabulary() -> None:
    """A reader that goes through `LedgerEvent.model_fields` must see the same 18 kinds."""
    annotation = LedgerEvent.model_fields["kind"].annotation
    assert {member.value for member in annotation} == FROZEN_KINDS


@pytest.mark.parametrize("kind", sorted(FROZEN_KINDS))
def test_every_frozen_kind_constructs_a_ledger_event(kind: str) -> None:
    event = LedgerEvent.model_validate(
        {"event_id": "ev-1", "ts": "2026-01-01T00:00:00Z", "kind": kind, "payload": {}}
    )
    assert event.kind == kind


def test_a_kind_outside_the_vocabulary_is_rejected() -> None:
    with pytest.raises(Exception):
        LedgerEvent.model_validate(
            {"event_id": "ev-1", "ts": "2026-01-01T00:00:00Z", "kind": "invented", "payload": {}}
        )


def test_every_kind_has_a_published_payload_shape() -> None:
    assert set(LEDGER_PAYLOAD_SHAPES) == FROZEN_KINDS


# --- D24: the pixel↔webhook join ----------------------------------------------------------


def test_the_join_keys_are_the_pinned_snake_case_names() -> None:
    assert tuple(LEDGER_JOIN_KEYS) == ("checkout_token", "order_ref", "client_id", "discount_code")


def test_both_checkout_paths_are_joinable() -> None:
    assert JOINABLE_KINDS == {"checkout_pixel", "order_paid"}


def test_the_join_view_reconciles_the_spellings_the_two_paths_actually_send() -> None:
    """The pixel is Shopify-shaped (`clientId`), the webhook carries `order_id`, and `order_ref`
    is a top-level LedgerEvent field. One reader reconciles all three so T-051 and T-061 do not
    each invent a key name."""
    pixel = {
        "event_id": "ev-pixel",
        "ts": "2026-01-01T00:00:00Z",
        "kind": "checkout_pixel",
        "order_ref": "ord-1",
        "payload": {
            "clientId": "cid-1",
            "checkout_token": "ck-1",
            "total_price": 44.1,
            "discountCode": "PS-ABC123",
        },
    }
    webhook = {
        "event_id": "ev-paid",
        "ts": "2026-01-01T00:10:00Z",
        "kind": "order_paid",
        "order_ref": "ord-1",
        "payload": {
            "checkout_token": "ck-1",
            "order_id": "ord-1",
            "total_price": 44.1,
            "discount_code": "PS-ABC123",
        },
    }
    pixel_view = join_key_view(pixel)
    webhook_view = join_key_view(webhook)

    assert pixel_view["checkout_token"] == webhook_view["checkout_token"] == "ck-1"
    assert pixel_view["order_ref"] == webhook_view["order_ref"] == "ord-1"
    assert pixel_view["client_id"] == "cid-1"
    assert pixel_view["discount_code"] == webhook_view["discount_code"] == "PS-ABC123"
    assert set(pixel_view) == set(LEDGER_JOIN_KEYS)


def test_the_join_view_reports_a_missing_key_rather_than_omitting_it() -> None:
    """A short dict hides WHICH key is missing, which is the only thing the caller needs to know."""
    view = join_key_view(
        {"event_id": "ev", "ts": "2026-01-01T00:00:00Z", "kind": "order_paid", "payload": {}}
    )
    assert set(view) == set(LEDGER_JOIN_KEYS)
    assert all(value is None for value in view.values())


def test_the_join_view_reads_a_model_as_well_as_a_mapping() -> None:
    event = LedgerEvent.model_validate(
        {
            "event_id": "ev-paid",
            "ts": "2026-01-01T00:00:00Z",
            "kind": "order_paid",
            "order_ref": "ord-1",
            "payload": {"checkout_token": "ck-1"},
        }
    )
    assert join_key_view(event)["checkout_token"] == "ck-1"


# --- payload validation at the producing boundary ------------------------------------------


def test_a_joinable_payload_without_a_checkout_token_is_reported() -> None:
    problems = validate_ledger_payload("checkout_pixel", {"clientId": "cid-1"})
    assert problems and "checkout_token" in problems[0]
    assert validate_ledger_payload("checkout_pixel", {"checkout_token": "ck-1"}) == []


def test_a_payload_missing_a_published_key_is_reported() -> None:
    assert validate_ledger_payload("feedback", {"matched_pitch": False, "reason": "late"}) == []
    assert validate_ledger_payload("feedback", {"matched_pitch": False}) != []


def test_extra_payload_keys_are_allowed() -> None:
    """The ledger records what happened. A vendor body carries fields we did not ask for, and
    refusing to record the event because of them would make the ledger lossy."""
    assert (
        validate_ledger_payload(
            "feedback", {"matched_pitch": False, "reason": "late", "vendor_extra": {"a": [1]}}
        )
        == []
    )


def test_an_unknown_kind_is_reported() -> None:
    assert validate_ledger_payload("invented", {}) != []


def test_a_non_mapping_payload_is_reported() -> None:
    assert validate_ledger_payload("feedback", ["not", "a", "mapping"]) != []


def test_payload_validation_is_not_wired_into_the_model() -> None:
    """Deliberate: the stored event must stay lossless even when its body is odd. The check
    belongs at the producing boundary, where a malformed body can still be refused."""
    LedgerEvent.model_validate(
        {
            "event_id": "ev-1",
            "ts": "2026-01-01T00:00:00Z",
            "kind": "checkout_pixel",
            "payload": {"nothing": "that a join could use"},
        }
    )
