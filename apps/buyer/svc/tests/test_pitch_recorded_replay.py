"""Rule 1, the strict half: a REVIEWED recording replays, and a changed contract MISSES loudly.

``tests/fixtures/buyer_pitch.json`` is a hand-authored, human-reviewed fixture (D21 — nothing
in this tree captures a live response, because no credentials exist here, D3). It carries a
provenance header, the CASE CONTRACT it was authored against, and one reviewed reply keyed on
the exact user turn ``buyer_svc.pitch.case_prompt`` builds for one request.

:class:`llm.doubles.RecordedLLM` keys on the ``(system, prompt)`` **pair**, and that is the
whole reason this file exists. A double that keyed on the user half alone would keep replaying
the same reviewed answer after ``PLATFORM_CONTRACT`` was rewritten — so a rule removed from the
contract ("assert nothing that is not in the CHECKED FACTS block", say) would turn into
permanent green in every test that goes through this seam. Here the same edit is an
``UnrecordedPromptError``, and the render falls back to the deterministic assembly rather than
serving prose reviewed against a contract that no longer exists.

Both halves are driven through the **served route**, because "the replay works" and "the served
shortlist shows what the replay produced" are different claims.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from llm.doubles import RecordedLLM
from llm.errors import UnrecordedPromptError
from llm.recordings import REQUIRED_PROVENANCE_FIELDS, load_recording_file

from .test_shortlist_pitch import (
    AUCTION,
    SHOPPER_A_INTENT,
    SHOPPER_A_PROFILE,
    _agented_slot,
)

RENDER = "/buyer/shortlist/render"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "buyer_pitch.json"


@pytest.fixture()
def client():
    main = importlib.import_module("buyer_svc.main")
    with TestClient(main.create_app()) as test_client:
        yield test_client


@pytest.fixture()
def recorded():
    """The reviewed fixture as a strict, offline double, installed on the writer seam."""
    writer = importlib.import_module("buyer_svc.pitch.writer")
    table, _ = load_recording_file(FIXTURE)
    double = RecordedLLM(table, role="buyer", name="buyer_pitch")
    writer.set_pitch_writer(double)
    try:
        yield double
    finally:
        writer.set_pitch_writer(None)


def _body() -> dict[str, Any]:
    return {
        "shortlist": {"auction_id": AUCTION, "slots": [_agented_slot()]},
        "intent": SHOPPER_A_INTENT,
        "profile": SHOPPER_A_PROFILE,
    }


def test_the_fixture_is_reviewed_and_declares_where_it_came_from():
    """D21: an undocumented fixture may not quietly become a test's ground truth."""
    table, provenance = load_recording_file(FIXTURE)
    assert table
    for field in REQUIRED_PROVENANCE_FIELDS:
        assert str(provenance.get(field, "")).strip(), field
    # The contract it was authored against is THIS tree's contract, byte for byte.
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    pitch = importlib.import_module("buyer_svc.pitch")
    assert document["system"] == pitch.PLATFORM_CONTRACT


def test_the_reviewed_reply_replays_into_the_served_shortlist(client, recorded):
    """A hit: the model's own words reach the shopper, in the platform's voice."""
    response = client.post(RENDER, json=_body())
    assert response.status_code == 200, response.text
    pitch = response.json()["slots"][0]["pitch"]
    assert pitch["platform_case_source"] == "written"
    assert pitch["platform_case"] == (
        "Free returns here are store-confirmed, dispatch is inside 2 days, and the price is "
        "78.00 USD with 10% off if you take it before 2026-09-06."
    )
    assert len(recorded.calls) == 1
    # The store's own message rode in on the same slot and is still its own, unedited.
    assert pitch["store_pitch"] is not None
    assert pitch["store_pitch"] not in pitch["platform_case"]


def test_replaying_twice_serves_the_same_bytes(client, recorded):
    first = client.post(RENDER, json=_body())
    second = client.post(RENDER, json=_body())
    assert first.content == second.content


def test_a_changed_contract_misses_loudly_instead_of_replaying_a_stale_answer(
    client, recorded, monkeypatch
):
    """The one that makes the seam worth having.

    One byte of the CASE CONTRACT changes and the reviewed reply stops matching — the double
    raises rather than answering. The shopper still gets a case (rule 2), it is the
    deterministic assembly, and it is NOT the stale reviewed prose.
    """
    writing = importlib.import_module("buyer_svc.pitch.writing")
    monkeypatch.setattr(
        writing, "PLATFORM_CONTRACT", writing.PLATFORM_CONTRACT + "\n7. And one more rule."
    )

    response = client.post(RENDER, json=_body())
    assert response.status_code == 200, response.text
    pitch = response.json()["slots"][0]["pitch"]
    assert pitch["platform_case_source"] == "assembled"
    assert "dispatch is inside 2 days" not in pitch["platform_case"]
    assert pitch["platform_case"].strip()


def test_the_miss_is_an_unrecorded_prompt_error_and_not_a_silent_fallthrough(recorded):
    """Named directly, so "it fell back" cannot be confused with "it replayed something else"."""
    pitch = importlib.import_module("buyer_svc.pitch")
    from llm.prompting import assemble_prompt

    with pytest.raises(UnrecordedPromptError):
        recorded.complete(
            assemble_prompt(pitch.PLATFORM_CONTRACT + " (edited)", "CASE REQUEST\nnothing here")
        )
