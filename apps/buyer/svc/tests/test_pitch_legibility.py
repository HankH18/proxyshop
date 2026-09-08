"""A buyer container serving nothing but `assemble()` must not look like one writing prose.

`compose_case` never raises — a model outage may never cost a shopper a slot (rule 2), and
that is right. It also never *said* anything, so a deployment configured for
`LLM_PROVIDER=anthropic` on an image with no `anthropic` distribution in it served the
deterministic assembly on every slot of every shortlist, at 200, with a healthy container and
a config that claimed otherwise.

Two lines close it, and these tests drive both through the seam the served route uses:
the state, where the writer is chosen, and the failure, where it happens.
"""

from __future__ import annotations

import importlib
import logging

import pytest
from fastapi.testclient import TestClient
from llm.boot import forget_llm_runtime_reports

from .test_shortlist_pitch import (
    AUCTION,
    SHOPPER_A_INTENT,
    SHOPPER_A_PROFILE,
    _agented_slot,
    _scraped_slot,
)

RENDER = "/buyer/shortlist/render"
BOOT_LOGGER = "llm.boot"
WRITING_LOGGER = "buyer_svc.pitch.writing"

#: Key-shaped, not a key. Asserted ABSENT from every line these tests capture.
FAKE_KEY = "sk-ant-not-a-real-key-0000000000"


@pytest.fixture(autouse=True)
def _forget() -> None:
    forget_llm_runtime_reports()


@pytest.fixture()
def writer_module():
    module = importlib.import_module("buyer_svc.pitch.writer")
    module.set_pitch_writer(None)
    try:
        yield module
    finally:
        module.set_pitch_writer(None)


@pytest.fixture()
def client():
    main = importlib.import_module("buyer_svc.main")
    with TestClient(main.create_app()) as test_client:
        yield test_client


def _body() -> dict[str, object]:
    return {
        "shortlist": {"auction_id": AUCTION, "slots": [_agented_slot(), _scraped_slot()]},
        "intent": SHOPPER_A_INTENT,
        "profile": SHOPPER_A_PROFILE,
    }


class Exploding:
    """Fails the way a live client fails: on the call, not on construction."""

    def complete(self, prompt: object, **kwargs: object) -> str:
        raise RuntimeError("the provider said no")


def test_the_default_deployment_says_out_loud_that_the_cases_are_assembled(
    writer_module,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=BOOT_LOGGER):
        assert writer_module.build_pitch_writer({}) is not None

    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert "llm[buyer] TEMPLATED" in caplog.records[0].getMessage()


def test_a_deployment_configured_for_a_model_it_cannot_reach_says_so(
    writer_module,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped defect. `build_pitch_writer` SUCCEEDS — `AnthropicLLM.__init__` does no I/O
    and imports no SDK — so nothing was wrong until the first `complete()` three frames down,
    where `compose_case`'s `except` answered with the template."""
    from llm import boot

    monkeypatch.setattr(boot, "sdk_installed", lambda *_a, **_k: False)
    env = {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": FAKE_KEY}

    with caplog.at_level(logging.INFO, logger=BOOT_LOGGER):
        assert writer_module.build_pitch_writer(env) is not None

    emitted = "\n".join(record.getMessage() for record in caplog.records)
    assert "llm[buyer] TEMPLATED" in emitted
    assert "sdk=MISSING" in emitted
    assert FAKE_KEY not in emitted


def test_a_fully_configured_deployment_reports_the_platform_case_is_written(
    writer_module,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm import boot

    monkeypatch.setattr(boot, "sdk_installed", lambda *_a, **_k: True)
    env = {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": FAKE_KEY}

    with caplog.at_level(logging.INFO, logger=BOOT_LOGGER):
        assert writer_module.build_pitch_writer(env) is not None

    assert [record.levelno for record in caplog.records] == [logging.INFO]
    line = caplog.records[0].getMessage()
    assert "llm[buyer] LIVE" in line
    assert FAKE_KEY not in line


def test_the_served_route_reports_its_state_once_and_not_once_per_shortlist(
    writer_module,
    client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D20's default double is rebuilt per render (so its transcript is collected), which puts
    `build_pitch_writer` on the served path — an unguarded line would be one WARNING per
    shortlist for the life of the container."""
    with caplog.at_level(logging.INFO, logger=BOOT_LOGGER):
        for _ in range(3):
            assert client.post(RENDER, json=_body()).status_code == 200

    boot_lines = [r.getMessage() for r in caplog.records if "llm[buyer]" in r.getMessage()]
    assert len(boot_lines) == 1, boot_lines


def test_a_writer_that_throws_is_logged_before_the_assembly_absorbs_it(
    writer_module,
    client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule 2 is kept — every slot is served — and the reason is no longer invisible."""
    writer_module.set_pitch_writer(Exploding())

    with caplog.at_level(logging.WARNING, logger=WRITING_LOGGER):
        response = client.post(RENDER, json=_body())

    assert response.status_code == 200
    slots = response.json()["slots"]
    assert all(slot["pitch"]["platform_case_source"] == "assembled" for slot in slots)

    warnings = [r.getMessage() for r in caplog.records if "case writer failed" in r.getMessage()]
    assert warnings, "a total writer outage left no trace at all before this line existed"
    for message in warnings:
        assert "RuntimeError" in message, "the exception TYPE is what an operator needs"
        assert "the provider said no" not in message, (
            "the provider's own message is not logged: on an auth failure it is the string "
            "in this path most likely to carry a credential"
        )


def test_the_default_render_logs_no_writer_failure_at_all(
    writer_module,
    client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The offline double answers `double:buyer:<hex>`, which the SCREEN refuses — a refusal,
    not a failure. Logging it as one would make the healthy default look like an outage."""
    with caplog.at_level(logging.WARNING, logger=WRITING_LOGGER):
        assert client.post(RENDER, json=_body()).status_code == 200

    assert [r for r in caplog.records if "case writer failed" in r.getMessage()] == []
