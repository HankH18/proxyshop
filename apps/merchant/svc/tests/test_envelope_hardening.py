"""Positive tests for the envelope repairs made in cycle 20 (T-243, T-248, T-239, T-246).

``test_repro_open_tickets.py`` holds the *reproduction* gates — each one written to fail
against the defect and to stop failing when it is closed. This file holds the other half:
what the repaired code is now supposed to do, asserted directly, so a future change that
satisfies a reproduction gate vacuously still has to answer for the behaviour.

**Nothing here mutates process-global state.** Every probe builds its own
:class:`~merchant_svc.envelope.store.EnvelopeVersions`; ``ENVELOPES`` is read for identity
only, never written, for the same reason the reproduction file gives.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from merchant_svc.envelope.digest import approval_digest
from merchant_svc.envelope.model import ACTIVE, KILLED, SHADOW, ApprovalRejected
from merchant_svc.envelope.store import EnvelopeVersions
from merchant_svc.envelope.versions import activate_envelope, edit_envelope


def _envelope(store_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "store_id": store_id,
        "version": 1,
        "floors": [],
        "max_discount_pct": 10.0,
        "budget_cap": 100.0,
        "pursue_clusters": [],
        "standing_commitments": [],
        "activation": SHADOW,
    }
    body.update(overrides)
    return body


def _approval(envelope: Any, **overrides: Any) -> dict[str, Any]:
    artifact = {
        "approver": "owner@store.example",
        "approved_at": "2026-09-05T10:00:00+00:00",
        "envelope_hash": approval_digest(envelope),
    }
    artifact.update(overrides)
    return artifact


# ======================================================================================
# T-248 — record() is the door, so the approval rule is enforced at the door
# ======================================================================================
def test_record_refuses_an_envelope_that_asserts_its_own_activation() -> None:
    """The whole point: `active` is granted by an artifact, never claimed by a document."""
    versions = EnvelopeVersions()
    with pytest.raises(ApprovalRejected):
        versions.record(_envelope("s-claim", activation=ACTIVE))
    # Refused means refused: nothing was filed, so the store is not merely "not live" — it
    # has no history at all and `current` still raises.
    assert versions.stores() == (), "a refused record still appended to the history"


def test_record_refuses_an_approval_bound_to_a_different_version() -> None:
    """The edit-after-approve substitution, arriving through ``record`` instead of activate.

    v1 is approved and live. v2 changes the discount ceiling. Carrying v1's artifact onto v2
    and filing that directly is exactly what ``activate_envelope`` refuses, and ``record``
    must refuse it too or the polite entry point is the only thing enforcing R6.
    """
    versions = EnvelopeVersions()
    first = versions.record(_envelope("s-swap"))
    live = versions.record(activate_envelope(first, _approval(first)))
    assert live.is_live

    edited = edit_envelope(live, {"max_discount_pct": 99.0})
    forged = edited.with_activation(ACTIVE, live.approval)
    with pytest.raises(ApprovalRejected):
        versions.record(forged)
    assert versions.current("s-swap").max_discount_pct == 10.0, (
        "a 99% ceiling was filed live under an approval taken over a 10% one"
    )


def test_the_polite_entry_points_still_work_end_to_end() -> None:
    """Control. A guard that refused everything would satisfy the gate and break the product.

    put -> shadow, activate -> live, edit -> back to shadow, kill -> killed, and every one of
    those goes through the tightened ``record``.
    """
    versions = EnvelopeVersions()
    stored = versions.put("s-happy", _envelope("s-happy", activation=ACTIVE))
    assert stored.activation == SHADOW, "put() honoured a body's activation claim"

    live = versions.activate("s-happy", _approval(stored))
    assert live.is_live and versions.is_live("s-happy")

    edited = versions.put("s-happy", _envelope("s-happy", max_discount_pct=5.0))
    assert edited.activation == SHADOW, "an edit left the store live under a stale approval"
    assert not versions.is_live("s-happy")

    versions.activate("s-happy", _approval(edited))
    assert versions.kill("s-happy").activation == KILLED
    assert not versions.is_live("s-happy")


def test_kill_still_works_on_a_live_envelope() -> None:
    """The kill switch takes no artifact, and the new guard must not start demanding one.

    ``kill_envelope`` keeps the approval that was on file — it is the record of what the
    store *was* running — and moves the state to ``killed``, so the guard's ACTIVE-only
    condition is what keeps a paperwork check off the one control that must always work.
    """
    versions = EnvelopeVersions()
    first = versions.record(_envelope("s-kill"))
    versions.record(activate_envelope(first, _approval(first)))
    killed = versions.kill("s-kill")
    assert killed.activation == KILLED
    assert killed.approval is not None, "the record of what the store was running was dropped"


# ======================================================================================
# T-243 — one file, one module, whichever spelling reaches it
# ======================================================================================
@pytest.mark.parametrize(
    "module",
    ["", ".model", ".store", ".digest", ".versions", ".frozen"],
)
def test_both_spellings_of_every_envelope_module_are_one_object(module: str) -> None:
    short = importlib.import_module(f"merchant_svc.envelope{module}")
    long_ = importlib.import_module(f"apps.merchant.svc.src.envelope{module}")
    assert short is long_, (
        f"merchant_svc.envelope{module} and apps.merchant.svc.src.envelope{module} are two "
        "module objects over one file"
    )


def test_a_kill_through_one_spelling_is_visible_through_the_other() -> None:
    """The consequence the identity is FOR, asserted rather than inferred.

    This is the failure the split made possible: the merchant kills a store through the
    FastAPI app's spelling and the acceptance suite's spelling still reports it live. It uses
    a locally built store recorded through one spelling and read through the other's class,
    so it never writes the module-level ``ENVELOPES``.
    """
    short = importlib.import_module("merchant_svc.envelope.store")
    long_ = importlib.import_module("apps.merchant.svc.src.envelope.store")

    versions = short.EnvelopeVersions()
    first = versions.record(_envelope("s-two-spellings"))
    versions.record(activate_envelope(first, _approval(first)))
    assert long_.EnvelopeVersions.is_live(versions, "s-two-spellings") is True

    versions.kill("s-two-spellings")
    assert long_.EnvelopeVersions.is_live(versions, "s-two-spellings") is False

    # The error classes are the other half: `except UnknownStore` written against one
    # spelling has to catch what the other raises, or a promised 409 becomes a 500.
    with pytest.raises(long_.UnknownStore):
        versions.current("s-never-seen")
