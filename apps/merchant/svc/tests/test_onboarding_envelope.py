"""T-053 — the envelope an interview produces, the approval that activates it, its versions.

What the green here is evidence **of**:

* the fixture in ``fixtures/interviews/`` really is a DESIGN ``Envelope`` (validated against
  ``contracts.Envelope``, not eyeballed), and the interview reproduces it byte for byte;
* activation is refused by *every* way an approval artifact can be wrong, with a positive
  control beside each family so a refusal cannot be a broken call site in disguise;
* a new version is never live and never carries the previous version's approval — the
  approve-then-edit escalation is closed, not merely undocumented;
* "immutable" is structural: an old version shares no mutable object with a new one, so
  there is nothing to mutate through.

What it is **not** evidence of: durable storage. ``EnvelopeVersions`` is process-local, in
the same way ``merchant_svc.collector.PIXEL_INBOX`` is; DESIGN puts the real history in
``sealed.envelopes``. Losing it can only stop a store bidding, never start one.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import contracts
import httpx
import pytest
from merchant_svc.envelope import (
    ACTIVE,
    KILLED,
    SHADOW,
    ApprovalArtifact,
    ApprovalRejected,
    Envelope,
    EnvelopeEditRefused,
    EnvelopeInvalid,
    EnvelopeVersions,
    StoreMismatch,
    UnknownStore,
    VersionWentBackwards,
    approval_digest,
)
from merchant_svc.onboarding import activate, edit, envelope_from_transcript, kill
from merchant_svc.onboarding.interview import TranscriptRejected

_APPROVED_AT = "2026-01-05T09:30:00+00:00"


def _approval(envelope: Any, **overrides: Any) -> dict[str, Any]:
    """A complete, envelope-bound written approval, before any override is applied."""
    artifact = {
        "approver": "Dana Okonkwo, owner",
        "approved_at": _APPROVED_AT,
        "envelope_hash": approval_digest(envelope),
    }
    artifact.update(overrides)
    return artifact


# --------------------------------------------------------------------------------------
# The fixture is a real Envelope, and the interview reproduces it
# --------------------------------------------------------------------------------------
def test_the_golden_fixture_validates_as_a_design_envelope(
    onboarding_golden: dict[str, Any],
) -> None:
    """``contracts.Envelope`` forbids extra keys, so this catches a typo the eye would not."""
    validated = contracts.Envelope.model_validate(onboarding_golden)
    assert validated.activation is contracts.EnvelopeActivation.shadow
    for commitment in validated.standing_commitments:
        assert commitment.provenance.source is contracts.ProvenanceSource.owner_statement


def test_the_interview_reproduces_the_golden_envelope(
    onboarding_transcript: Any, onboarding_golden: dict[str, Any]
) -> None:
    produced = envelope_from_transcript(onboarding_transcript).to_dict()
    for key, expected in onboarding_golden.items():
        assert produced[key] == expected, key


def test_the_interview_is_deterministic(onboarding_transcript: Any) -> None:
    first = envelope_from_transcript(onboarding_transcript)
    second = envelope_from_transcript(copy.deepcopy(onboarding_transcript))
    assert first.to_dict() == second.to_dict()
    assert approval_digest(first) == approval_digest(second)


def test_an_interview_cannot_activate_anything_a_merchant_says_it_should(
    onboarding_transcript: Any,
) -> None:
    """R7. The interview's last question is an acknowledgement, never a switch."""
    eager = copy.deepcopy(onboarding_transcript)
    for turn in eager["turns"]:
        if turn.get("text", "").startswith("Understood"):
            turn["text"] = "Yes — switch it live right now, activate it, make it active."
    assert envelope_from_transcript(eager).activation == SHADOW


def test_an_interview_that_skipped_a_question_produces_no_envelope(
    onboarding_transcript: Any,
) -> None:
    """A wall nobody was asked about would otherwise be filed as "no wall"."""
    maimed = copy.deepcopy(onboarding_transcript)
    maimed["turns"] = [turn for turn in maimed["turns"] if turn.get("question") != "budget_cap"]
    maimed["turns"] = [
        turn for turn in maimed["turns"] if turn.get("text") != "Let's cap it at $500 a month."
    ]
    with pytest.raises(TranscriptRejected, match="never covered"):
        envelope_from_transcript(maimed)


def test_a_floor_answer_that_parses_to_nothing_is_an_error_not_an_absent_floor(
    onboarding_transcript: Any,
) -> None:
    maimed = copy.deepcopy(onboarding_transcript)
    for turn in maimed["turns"]:
        if turn.get("text", "").startswith("Ninety-five dollars"):
            turn["text"] = "you know, whatever feels right on the day"
    with pytest.raises(TranscriptRejected, match="neither a price nor a refusal"):
        envelope_from_transcript(maimed)


def test_a_commitment_typed_outside_the_published_vocabulary_is_refused(
    onboarding_transcript: Any,
) -> None:
    """D53's claim-type vocabulary is closed; an unmapped type must not reach trust."""
    maimed = copy.deepcopy(onboarding_transcript)
    for turn in maimed["turns"]:
        if turn.get("commitment_key") == "free_returns":
            turn["claim_type"] = "vibes"
    with pytest.raises(TranscriptRejected, match="not one of"):
        envelope_from_transcript(maimed)


# --------------------------------------------------------------------------------------
# The digest an approval is bound to
# --------------------------------------------------------------------------------------
def test_the_digest_is_the_same_however_the_envelope_is_written_down(
    onboarding_envelope: Envelope,
) -> None:
    """An envelope, its dict, and the same dict through JSON all hash identically."""
    expected = approval_digest(onboarding_envelope)
    assert approval_digest(onboarding_envelope.to_dict()) == expected
    assert approval_digest(json.loads(json.dumps(onboarding_envelope.to_dict()))) == expected
    assert approval_digest(onboarding_envelope.to_contract()) == expected


def test_the_digest_is_algorithm_tagged_and_not_empty(onboarding_envelope: Envelope) -> None:
    digest = approval_digest(onboarding_envelope)
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64


@pytest.mark.parametrize(
    "change",
    [
        {"max_discount_pct": 21.0},
        {"budget_cap": 501.0},
        {"store_id": "somebody-else"},
        {"version": 2},
        {"pursue_clusters": ["cluster-warm-layers"]},
        {"floors": [{"product_ref": None, "min_price": 39.0}]},
        {"standing_commitments": []},
    ],
)
def test_changing_any_approved_term_changes_the_digest(
    onboarding_envelope: Envelope, change: dict[str, Any]
) -> None:
    altered = dict(onboarding_envelope.to_dict(), **change)
    assert approval_digest(altered) != approval_digest(onboarding_envelope)


def test_activation_is_not_part_of_the_digest(onboarding_envelope: Envelope) -> None:
    """The merchant signs the terms, not the lifecycle state.

    If activation were hashed, the recorded artifact would stop verifying against its own
    envelope the moment it went live — exactly when an auditor needs to check it.
    """
    live = activate(onboarding_envelope, _approval(onboarding_envelope))
    assert approval_digest(live) == approval_digest(onboarding_envelope)
    assert approval_digest(kill(live)) == approval_digest(onboarding_envelope)


def test_reordering_a_list_is_not_a_change_of_terms(onboarding_envelope: Envelope) -> None:
    document = onboarding_envelope.to_dict()
    reordered = dict(
        document,
        floors=list(reversed(document["floors"])),
        pursue_clusters=list(reversed(document["pursue_clusters"])),
        standing_commitments=list(reversed(document["standing_commitments"])),
    )
    assert approval_digest(reordered) == approval_digest(onboarding_envelope)


def test_a_partial_envelope_has_no_digest(onboarding_envelope: Envelope) -> None:
    """Fail closed: hashing a half-document would mint an approval of terms nobody wrote."""
    partial = onboarding_envelope.to_dict()
    del partial["max_discount_pct"]
    with pytest.raises(EnvelopeInvalid, match="partial envelope"):
        approval_digest(partial)


# --------------------------------------------------------------------------------------
# Activation
# --------------------------------------------------------------------------------------
def test_a_complete_written_approval_activates_the_envelope(
    onboarding_envelope: Envelope,
) -> None:
    live = activate(onboarding_envelope, _approval(onboarding_envelope))
    assert live.activation == ACTIVE
    assert live.is_live is True
    assert live.version == onboarding_envelope.version, "activation is not a new version"
    assert live.approval is not None
    assert live.approval.approver == "Dana Okonkwo, owner"
    assert live.approval.envelope_hash == approval_digest(onboarding_envelope)


def test_activation_leaves_the_envelope_it_was_handed_in_shadow(
    onboarding_envelope: Envelope,
) -> None:
    before = onboarding_envelope.to_dict()
    activate(onboarding_envelope, _approval(onboarding_envelope))
    assert onboarding_envelope.to_dict() == before


@pytest.mark.parametrize(
    ("label", "artifact"),
    [
        ("no artifact at all", None),
        ("an artifact that is not a document", "I approve"),
        ("no approver", {"approved_at": _APPROVED_AT, "envelope_hash": "sha256:x"}),
        (
            "a blank approver",
            {"approver": "   ", "approved_at": _APPROVED_AT, "envelope_hash": "s"},
        ),
        ("no timestamp", {"approver": "Dana", "envelope_hash": "sha256:x"}),
        (
            "an unparseable timestamp",
            {"approver": "Dana", "approved_at": "last tuesday", "envelope_hash": "s"},
        ),
        (
            "a timestamp with no timezone",
            {"approver": "Dana", "approved_at": "2026-01-05T09:30:00", "envelope_hash": "s"},
        ),
        ("no binding hash", {"approver": "Dana", "approved_at": _APPROVED_AT}),
        (
            "a field nothing checks",
            {
                "approver": "Dana",
                "approved_at": _APPROVED_AT,
                "envelope_hash": "s",
                "scope": "everything",
            },
        ),
    ],
)
def test_an_incomplete_approval_never_activates(
    onboarding_envelope: Envelope, label: str, artifact: Any
) -> None:
    with pytest.raises(ApprovalRejected):
        activate(onboarding_envelope, artifact)
    assert onboarding_envelope.activation == SHADOW, label


def test_an_approval_bound_to_a_different_envelope_never_activates(
    onboarding_envelope: Envelope,
) -> None:
    other = Envelope.from_obj(dict(onboarding_envelope.to_dict(), max_discount_pct=99.0))
    with pytest.raises(ApprovalRejected, match="only activates the exact terms"):
        activate(onboarding_envelope, _approval(other))


def test_an_approval_of_the_previous_version_does_not_activate_the_next_one(
    onboarding_envelope: Envelope,
) -> None:
    """The edit-versus-approve race, closed by the version being inside the digest."""
    stale = _approval(onboarding_envelope)
    v2 = edit(onboarding_envelope, {"max_discount_pct": 40.0})
    with pytest.raises(ApprovalRejected):
        activate(v2, stale)


def test_a_killed_envelope_is_not_reactivated_by_an_approval(
    onboarding_envelope: Envelope,
) -> None:
    killed = kill(onboarding_envelope)
    assert killed.activation == KILLED
    with pytest.raises(ApprovalRejected, match="has been killed"):
        activate(killed, _approval(killed))


def test_an_approval_timestamp_is_recorded_in_utc(onboarding_envelope: Envelope) -> None:
    live = activate(
        onboarding_envelope,
        _approval(onboarding_envelope, approved_at="2026-01-05T10:30:00+01:00"),
    )
    assert live.approval is not None
    assert live.approval.approved_at == "2026-01-05T09:30:00+00:00"


def test_the_approval_artifact_survives_a_json_round_trip(onboarding_envelope: Envelope) -> None:
    live = activate(onboarding_envelope, _approval(onboarding_envelope))
    reloaded = Envelope.from_obj(json.loads(json.dumps(live.to_dict())))
    assert reloaded.approval == live.approval
    assert reloaded.activation == ACTIVE


# --------------------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------------------
def test_an_edit_mints_the_next_version_and_leaves_the_previous_one_alone(
    onboarding_envelope: Envelope,
) -> None:
    before = onboarding_envelope.to_dict()
    v2 = edit(onboarding_envelope, {"max_discount_pct": 15.0})
    assert v2.version == onboarding_envelope.version + 1
    assert v2.max_discount_pct == 15.0
    assert onboarding_envelope.to_dict() == before


def test_editing_a_live_envelope_produces_an_unapproved_shadow_version(
    onboarding_envelope: Envelope,
) -> None:
    """The escalation this whole module exists to stop.

    Approve a 20 % cap, edit the cap to 99 %, and if ``active`` were carried forward the
    store would be bidding at 99 % under an approval nobody gave for those terms.
    """
    live = activate(onboarding_envelope, _approval(onboarding_envelope))
    assert live.is_live is True

    escalated = edit(live, {"max_discount_pct": 99.0})
    assert escalated.max_discount_pct == 99.0
    assert escalated.activation == SHADOW
    assert escalated.is_live is False
    assert escalated.approval is None, "the previous version's approval must not travel"
    assert live.activation == ACTIVE, "editing must not disturb the version it edited"


def test_editing_a_killed_envelope_does_not_quietly_revive_it(
    onboarding_envelope: Envelope,
) -> None:
    revived = edit(kill(onboarding_envelope), {"budget_cap": 10.0})
    assert revived.activation == KILLED
    assert revived.is_live is False


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 99},
        {"activation": "active"},
        {"store_id": "somebody-else"},
        {"approval": {"approver": "me"}},
        {"max_discount_pct": 10.0, "activation": "active"},
        {"nonsense": 1},
    ],
)
def test_an_edit_may_not_set_a_field_that_is_not_a_term(
    onboarding_envelope: Envelope, changes: dict[str, Any]
) -> None:
    with pytest.raises(EnvelopeEditRefused, match="not editable"):
        edit(onboarding_envelope, changes)


def test_an_edit_that_changes_nothing_is_refused(onboarding_envelope: Envelope) -> None:
    with pytest.raises(EnvelopeEditRefused):
        edit(onboarding_envelope, {})


def test_an_edit_to_an_impossible_value_is_refused(onboarding_envelope: Envelope) -> None:
    with pytest.raises(EnvelopeInvalid):
        edit(onboarding_envelope, {"max_discount_pct": 101.0})
    with pytest.raises(EnvelopeInvalid):
        edit(onboarding_envelope, {"floors": [{"product_ref": None, "min_price": -1.0}]})


def test_versions_climb_across_a_chain_of_edits(onboarding_envelope: Envelope) -> None:
    versions = [onboarding_envelope]
    for cap in (10.0, 11.0, 12.0):
        versions.append(edit(versions[-1], {"max_discount_pct": cap}))
    assert [v.version for v in versions] == [1, 2, 3, 4]
    assert [v.max_discount_pct for v in versions] == [20.0, 10.0, 11.0, 12.0]


# --------------------------------------------------------------------------------------
# "Immutable" means there is nothing to mutate
# --------------------------------------------------------------------------------------
def test_mutating_what_to_dict_handed_back_does_not_reach_the_envelope(
    onboarding_envelope: Envelope,
) -> None:
    document = onboarding_envelope.to_dict()
    document["floors"][0]["min_price"] = 0.01
    document["standing_commitments"][0]["provenance"]["source"] = "seller_asserted"
    document["pursue_clusters"].append("cluster-anything")
    assert onboarding_envelope.to_dict()["floors"][0]["min_price"] == 40.0
    assert (
        onboarding_envelope.to_dict()["standing_commitments"][0]["provenance"]["source"]
        == "owner_statement"
    )
    assert "cluster-anything" not in onboarding_envelope.pursue_clusters


def test_two_versions_share_no_mutable_object(onboarding_envelope: Envelope) -> None:
    """The classic false immutability: v2 keeps v1's list and an edit reaches back."""
    v2 = edit(onboarding_envelope, {"max_discount_pct": 15.0})
    for floor in v2.floors:
        with pytest.raises(TypeError):
            floor["min_price"] = 0.0  # type: ignore[index]
    assert v2.floors is not onboarding_envelope.floors or isinstance(v2.floors, tuple)
    assert onboarding_envelope.to_dict()["floors"][0]["min_price"] == 40.0


def test_an_envelope_field_cannot_be_reassigned(onboarding_envelope: Envelope) -> None:
    with pytest.raises(Exception):
        onboarding_envelope.activation = ACTIVE  # type: ignore[misc]


def test_an_envelope_deep_copy_is_a_faithful_copy(onboarding_envelope: Envelope) -> None:
    assert copy.deepcopy(onboarding_envelope).to_dict() == onboarding_envelope.to_dict()


# --------------------------------------------------------------------------------------
# "Live" is declared, never inferred
# --------------------------------------------------------------------------------------
def test_an_envelope_with_no_activation_is_not_an_envelope(
    onboarding_envelope: Envelope,
) -> None:
    """The defect this asserts against: a store whose envelope forgot a key bidding anyway."""
    document = onboarding_envelope.to_dict()
    del document["activation"]
    with pytest.raises(EnvelopeInvalid):
        Envelope.from_obj(document)
    for nonsense in ("", "ACTIVE", "live", "whenever", None, True):
        with pytest.raises(EnvelopeInvalid):
            Envelope.from_obj(dict(document, activation=nonsense))


def test_a_store_with_no_envelope_is_not_live() -> None:
    assert EnvelopeVersions().is_live("never-onboarded") is False


# --------------------------------------------------------------------------------------
# The versioned history
# --------------------------------------------------------------------------------------
def test_the_history_keeps_every_state_and_never_rewrites_one(
    onboarding_envelope: Envelope, onboarding_store: EnvelopeVersions
) -> None:
    store_id = onboarding_envelope.store_id
    onboarding_store.record(onboarding_envelope)
    onboarding_store.activate(store_id, _approval(onboarding_envelope))
    onboarding_store.kill(store_id)

    history = onboarding_store.history(store_id)
    assert [state.activation for state in history] == [SHADOW, ACTIVE, KILLED]
    assert [state.version for state in history] == [1, 1, 1]
    assert onboarding_store.current(store_id).activation == KILLED
    assert onboarding_store.is_live(store_id) is False


def test_a_version_that_goes_backwards_is_refused(
    onboarding_envelope: Envelope, onboarding_store: EnvelopeVersions
) -> None:
    """A stale writer reinstating replaced limits is a silent rollback of the merchant."""
    onboarding_store.record(edit(onboarding_envelope, {"max_discount_pct": 5.0}))
    with pytest.raises(VersionWentBackwards):
        onboarding_store.record(onboarding_envelope)


def test_an_unknown_store_has_no_current_envelope(onboarding_store: EnvelopeVersions) -> None:
    with pytest.raises(UnknownStore):
        onboarding_store.current("never-onboarded")


def test_putting_terms_for_another_store_is_refused(
    onboarding_envelope: Envelope, onboarding_store: EnvelopeVersions
) -> None:
    with pytest.raises(StoreMismatch):
        onboarding_store.put("somebody-else", onboarding_envelope)


def test_a_put_never_honours_the_submitted_version_or_activation(
    onboarding_envelope: Envelope, onboarding_store: EnvelopeVersions
) -> None:
    store_id = onboarding_envelope.store_id
    onboarding_store.record(onboarding_envelope)
    stored = onboarding_store.put(
        store_id,
        dict(onboarding_envelope.to_dict(), version=99, activation=ACTIVE, max_discount_pct=5.0),
    )
    assert stored.version == 2, "the version is derived from the history, not submitted"
    assert stored.activation == SHADOW, "new terms have not been approved"
    assert stored.max_discount_pct == 5.0


def test_the_store_activates_the_head_not_whatever_the_caller_is_holding(
    onboarding_envelope: Envelope, onboarding_store: EnvelopeVersions
) -> None:
    """An approval collected against v1 must not go live against v2's terms."""
    store_id = onboarding_envelope.store_id
    onboarding_store.record(onboarding_envelope)
    stale = _approval(onboarding_envelope)
    onboarding_store.put(store_id, dict(onboarding_envelope.to_dict(), max_discount_pct=95.0))

    with pytest.raises(ApprovalRejected):
        onboarding_store.activate(store_id, stale)
    assert onboarding_store.is_live(store_id) is False
    assert onboarding_store.current(store_id).max_discount_pct == 95.0

    fresh = _approval(onboarding_store.current(store_id))
    assert onboarding_store.activate(store_id, fresh).activation == ACTIVE
    assert onboarding_store.is_live(store_id) is True


# --------------------------------------------------------------------------------------
# The HTTP surface
# --------------------------------------------------------------------------------------
async def test_the_envelope_routes_refuse_an_anonymous_caller(
    onboarding_client: httpx.AsyncClient,
    onboarding_admin_token: str,
    onboarding_envelope: Envelope,
    onboarding_store: EnvelopeVersions,
) -> None:
    """An envelope is the store's whole negotiating position. It is not world-readable."""
    store_id = onboarding_envelope.store_id
    onboarding_store.record(onboarding_envelope)

    for response in (
        await onboarding_client.get(f"/stores/{store_id}/envelope"),
        await onboarding_client.put(
            f"/stores/{store_id}/envelope", json=onboarding_envelope.to_dict()
        ),
        await onboarding_client.post(f"/stores/{store_id}/kill"),
    ):
        assert response.status_code == 401, response.text
    assert onboarding_store.current(store_id).activation == SHADOW


async def test_the_envelope_routes_refuse_everybody_until_a_token_is_configured(
    onboarding_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    onboarding_envelope: Envelope,
) -> None:
    monkeypatch.delenv("MERCHANT_ADMIN_TOKEN", raising=False)
    response = await onboarding_client.get(f"/stores/{onboarding_envelope.store_id}/envelope")
    assert response.status_code == 503
    assert response.json()["error"] == "admin-api-not-configured"


async def test_reading_an_envelope_returns_the_pinned_document_without_the_approval(
    onboarding_client: httpx.AsyncClient,
    onboarding_admin_token: str,
    onboarding_envelope: Envelope,
    onboarding_store: EnvelopeVersions,
    onboarding_golden: dict[str, Any],
) -> None:
    store_id = onboarding_envelope.store_id
    onboarding_store.record(activate(onboarding_envelope, _approval(onboarding_envelope)))

    response = await onboarding_client.get(
        f"/stores/{store_id}/envelope",
        headers={"authorization": f"Bearer {onboarding_admin_token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "approval" not in body, "the wire document is the pinned eight-field Envelope"
    assert body["activation"] == ACTIVE
    assert body["floors"] == onboarding_golden["floors"]


async def test_reading_an_envelope_for_a_store_with_none_is_a_404(
    onboarding_client: httpx.AsyncClient, onboarding_admin_token: str
) -> None:
    response = await onboarding_client.get(
        "/stores/never-onboarded/envelope",
        headers={"authorization": f"Bearer {onboarding_admin_token}"},
    )
    assert response.status_code == 404


async def test_a_put_asking_to_go_live_without_an_approval_is_stored_in_shadow(
    onboarding_client: httpx.AsyncClient,
    onboarding_admin_token: str,
    onboarding_envelope: Envelope,
    onboarding_store: EnvelopeVersions,
) -> None:
    """The body is the merchant's *request*; only the artifact grants it."""
    store_id = onboarding_envelope.store_id
    onboarding_store.record(onboarding_envelope)

    response = await onboarding_client.put(
        f"/stores/{store_id}/envelope",
        json=dict(onboarding_envelope.to_dict(), activation=ACTIVE, max_discount_pct=95.0),
        headers={"authorization": f"Bearer {onboarding_admin_token}"},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "approval-required"
    assert onboarding_store.is_live(store_id) is False
    assert onboarding_store.current(store_id).activation == SHADOW
    assert onboarding_store.current(store_id).max_discount_pct == 95.0, (
        "the edit itself is saved; only the activation is refused"
    )


async def test_a_put_carrying_a_bound_written_approval_goes_live(
    onboarding_client: httpx.AsyncClient,
    onboarding_admin_token: str,
    onboarding_envelope: Envelope,
    onboarding_store: EnvelopeVersions,
) -> None:
    store_id = onboarding_envelope.store_id
    onboarding_store.record(onboarding_envelope)
    intended = edit(onboarding_envelope, {"max_discount_pct": 12.0})

    response = await onboarding_client.put(
        f"/stores/{store_id}/envelope",
        json=dict(intended.to_dict(), activation=ACTIVE),
        headers={
            "authorization": f"Bearer {onboarding_admin_token}",
            "X-Envelope-Approval": json.dumps(_approval(intended)),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["activation"] == ACTIVE
    assert onboarding_store.is_live(store_id) is True
    assert onboarding_store.current(store_id).version == 2


async def test_a_put_carrying_someone_elses_approval_does_not_go_live(
    onboarding_client: httpx.AsyncClient,
    onboarding_admin_token: str,
    onboarding_envelope: Envelope,
    onboarding_store: EnvelopeVersions,
) -> None:
    store_id = onboarding_envelope.store_id
    onboarding_store.record(onboarding_envelope)
    other = Envelope.from_obj(dict(onboarding_envelope.to_dict(), max_discount_pct=99.0, version=2))

    response = await onboarding_client.put(
        f"/stores/{store_id}/envelope",
        json=dict(onboarding_envelope.to_dict(), activation=ACTIVE, max_discount_pct=12.0),
        headers={
            "authorization": f"Bearer {onboarding_admin_token}",
            "X-Envelope-Approval": json.dumps(_approval(other)),
        },
    )
    assert response.status_code == 403
    assert onboarding_store.is_live(store_id) is False


async def test_a_put_that_is_not_an_envelope_is_a_400(
    onboarding_client: httpx.AsyncClient, onboarding_admin_token: str
) -> None:
    headers = {"authorization": f"Bearer {onboarding_admin_token}"}
    assert (
        await onboarding_client.put("/stores/x/envelope", content=b"{not json", headers=headers)
    ).status_code == 400
    assert (
        await onboarding_client.put("/stores/x/envelope", json=[1, 2, 3], headers=headers)
    ).status_code == 400
    assert (
        await onboarding_client.put("/stores/x/envelope", json={"store_id": "x"}, headers=headers)
    ).status_code == 400


async def test_the_kill_switch_stops_a_live_store_immediately(
    onboarding_client: httpx.AsyncClient,
    onboarding_admin_token: str,
    onboarding_envelope: Envelope,
    onboarding_store: EnvelopeVersions,
) -> None:
    store_id = onboarding_envelope.store_id
    onboarding_store.record(activate(onboarding_envelope, _approval(onboarding_envelope)))
    assert onboarding_store.is_live(store_id) is True

    response = await onboarding_client.post(
        f"/stores/{store_id}/kill",
        headers={"authorization": f"Bearer {onboarding_admin_token}"},
    )
    assert response.status_code == 200
    assert response.json() == {"store_id": store_id, "activation": KILLED}
    assert onboarding_store.is_live(store_id) is False


async def test_killing_a_store_with_no_envelope_is_a_404(
    onboarding_client: httpx.AsyncClient, onboarding_admin_token: str
) -> None:
    response = await onboarding_client.post(
        "/stores/never-onboarded/kill",
        headers={"authorization": f"Bearer {onboarding_admin_token}"},
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------------------
# The operator CLI
# --------------------------------------------------------------------------------------
def test_the_cli_prints_the_envelope_a_transcript_produces(
    capsys: pytest.CaptureFixture[str], onboarding_golden: dict[str, Any]
) -> None:
    from merchant_svc.onboarding.__main__ import main

    from ._fixtures_onboarding import INTERVIEW_FIXTURE  # noqa: TID252 - same test package

    assert main([str(INTERVIEW_FIXTURE)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["store_id"] == onboarding_golden["store_id"]
    assert printed["activation"] == SHADOW


def test_the_cli_refuses_an_unreadable_transcript(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from merchant_svc.onboarding.__main__ import main

    broken = tmp_path / "broken.json"
    broken.write_text(
        json.dumps({"transcript": {"turns": [], "completed_at": "2026-01-01T00:00:00Z"}})
    )
    assert main([str(broken)]) == 1
    assert main([str(tmp_path / "absent.json")]) == 2


def test_the_artifact_helper_and_the_cli_agree_on_what_gets_signed(
    onboarding_envelope: Envelope,
) -> None:
    from merchant_svc.onboarding import approval_artifact_template

    template = approval_artifact_template(onboarding_envelope, "Dana", _APPROVED_AT)
    assert activate(onboarding_envelope, template).activation == ACTIVE
    assert ApprovalArtifact.parse(template).envelope_hash == approval_digest(onboarding_envelope)
