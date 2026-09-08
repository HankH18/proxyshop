"""A container that has lost the shop's own voice must not look like one that has it (D55).

The advocate is deliberately forgiving: `compose_pitch` catches everything a copywriter can
throw and answers with the deterministic fallback, because a model outage may never cost a
store its bid. That is right, and it made the failure invisible — a deployment configured for
`LLM_PROVIDER=anthropic`, running an image with no `anthropic` distribution in it, answered
`POST /v1/bid-requests` with 200, a template, and nothing in the log to point at.

These tests pin the two lines that close it: the state, said once where the advocate is built,
and the failure, said where it actually happens.
"""

from __future__ import annotations

import logging

import pytest
from llm.boot import forget_llm_runtime_reports
from llm.doubles import DeterministicLLM
from store_agent.solicitation.copywriter import PitchClient, pitch_client

COPYWRITER_LOGGER = "store_agent.solicitation.copywriter"
BOOT_LOGGER = "llm.boot"

#: Key-shaped, not a key. Asserted ABSENT from every line these tests capture.
FAKE_KEY = "sk-ant-not-a-real-key-0000000000"


@pytest.fixture(autouse=True)
def _forget() -> None:
    """The boot line is suppressed after its first identical emission, per process."""
    forget_llm_runtime_reports()


class Exploding:
    """A copywriter that fails the way a live one fails: on the call, not on construction."""

    model = "a-model-id-no-provider-serves"

    def complete(self, prompt: object, **kwargs: object) -> str:
        raise RuntimeError("the provider said no")


def test_the_default_deployment_says_out_loud_that_it_is_serving_templates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The shipped configuration. It is CORRECT (D20) and it must still be visible."""
    with caplog.at_level(logging.INFO, logger=BOOT_LOGGER):
        assert pitch_client({}) is not None

    lines = [record.getMessage() for record in caplog.records]
    assert any("llm[store_agent] TEMPLATED" in line for line in lines), lines
    assert [record.levelno for record in caplog.records] == [logging.WARNING], (
        "a deployment that keeps only WARNING must still see that it lost the shop's voice"
    )


def test_a_deployment_configured_for_a_model_it_cannot_reach_says_so(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact defect: `LLM_PROVIDER=anthropic`, a key set, and no SDK in the image.

    `AnthropicLLM.__init__` does no I/O and imports no SDK, so `pitch_client` SUCCEEDS here
    and returns a client. Nothing at all was wrong until the first `complete()`.
    """
    from llm import boot

    monkeypatch.setattr(boot, "sdk_installed", lambda *_a, **_k: False)
    env = {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": FAKE_KEY}

    with caplog.at_level(logging.INFO, logger=BOOT_LOGGER):
        assert pitch_client(env) is not None, "the client builds; that is the whole problem"

    emitted = "\n".join(record.getMessage() for record in caplog.records)
    assert "llm[store_agent] TEMPLATED" in emitted
    assert "sdk=MISSING" in emitted
    assert FAKE_KEY not in emitted, "a boot line is the easiest place in a system to leak a key"


def test_a_fully_configured_deployment_reports_the_shop_speaks_for_itself(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm import boot

    monkeypatch.setattr(boot, "sdk_installed", lambda *_a, **_k: True)
    env = {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": FAKE_KEY}

    with caplog.at_level(logging.INFO, logger=BOOT_LOGGER):
        assert pitch_client(env) is not None

    assert [record.levelno for record in caplog.records] == [logging.INFO]
    line = caplog.records[0].getMessage()
    assert "llm[store_agent] LIVE" in line
    assert "api_key=present" in line
    assert FAKE_KEY not in line


def test_the_line_is_not_repeated_once_per_solicitation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`solicitation.advocate` caches the runner on `app.state`, so this runs once per
    application — but nothing structural stops a caller building a second advocate, and one
    WARNING per bid for the life of a container is how a true line becomes noise."""
    with caplog.at_level(logging.INFO, logger=BOOT_LOGGER):
        pitch_client({})
        pitch_client({})
        pitch_client({})

    assert len(caplog.records) == 1


def test_a_copywriter_that_throws_is_logged_before_the_fallback_absorbs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`PitchClient` re-raises, `compose_pitch` catches, the store still bids — and now the
    log says the shopper is reading a template rather than the store's own case."""
    client = PitchClient(Exploding())

    with caplog.at_level(logging.WARNING, logger=COPYWRITER_LOGGER):
        with pytest.raises(RuntimeError):
            client.complete("a prompt")

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "RuntimeError" in message, "the exception TYPE is what an operator needs"
    assert "the provider said no" not in message, (
        "the provider's message is not logged: on an auth failure it is the string in this "
        "path most likely to carry a credential"
    )
    assert "fallback pitch" in message


def test_a_working_copywriter_logs_nothing_and_still_forgets_its_calls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning is on the failure path only; the leak fix it sits beside is unchanged."""
    client = PitchClient(DeterministicLLM(role="store_agent"))

    with caplog.at_level(logging.WARNING, logger=COPYWRITER_LOGGER):
        for _ in range(5):
            assert client.complete("a prompt")

    assert caplog.records == []
    assert client.inner.calls == []
