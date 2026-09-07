"""The writer seam itself: what the render path builds, how often, and what it keeps.

Two properties, and the second one is a defect this file was written to catch rather than a
nicety.

**The seam is live.** Under D20's default provider ``POST /buyer/shortlist/render`` really does
consult a client — it is not a code path that only exists when a test injects one. The reply it
gets (``double:<role>:<hex>``) is not prose, fails the screen, and the shopper is served the
deterministic assembly, which is the whole of what "offline determinism" buys.

**The seam keeps nothing per request.** ``llm.doubles.DeterministicLLM`` is a *recording*
double: every ``complete`` appends a ``KeyedLLMCall`` carrying the full prompt to ``self.calls``,
and nothing trims it. The first draft of ``buyer_svc.pitch.writer`` memoised the default client,
so a served buyer service would have grown one prompt transcript per SLOT of every shortlist it
ever rendered, for the life of the process — invisible to every other test in this directory,
because a leak has no assertion. Only the LIVE client is memoised now (it accumulates nothing
and owns an expensive connection pool); the double is rebuilt per render and collected with the
transcript.
"""

from __future__ import annotations

import gc
import importlib
from typing import Any

import pytest
from fastapi.testclient import TestClient
from llm.doubles import DeterministicLLM

from .test_shortlist_pitch import (
    AUCTION,
    SHOPPER_A_INTENT,
    SHOPPER_A_PROFILE,
    _agented_slot,
    _scraped_slot,
)

RENDER = "/buyer/shortlist/render"


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


def _body() -> dict[str, Any]:
    return {
        "shortlist": {"auction_id": AUCTION, "slots": [_agented_slot(), _scraped_slot()]},
        "intent": SHOPPER_A_INTENT,
        "profile": SHOPPER_A_PROFILE,
    }


def test_the_default_provider_really_is_consulted(writer_module):
    """If this ever reads ``None``, every "a case was written" claim in this directory is
    about a seam nothing drives in a deployment."""
    built = writer_module.pitch_writer()
    assert built is not None
    assert isinstance(built, DeterministicLLM)
    assert built.model == "double:buyer"


def test_the_offline_double_is_not_kept_between_renders(writer_module):
    """The leak gate. Two calls must not hand back one accumulating object."""
    first = writer_module.pitch_writer()
    second = writer_module.pitch_writer()
    assert first is not second, (
        "the offline double is memoised, so its `calls` list — one full prompt transcript per "
        "slot — grows for the life of the process on a served route"
    )
    assert second.calls == []


def test_a_served_render_leaves_no_transcript_behind(client, writer_module):
    """End to end: five shortlists rendered, and the seam is holding nothing afterwards."""
    for _ in range(5):
        assert client.post(RENDER, json=_body()).status_code == 200
    gc.collect()
    assert writer_module.pitch_writer().calls == []
    assert writer_module._live is None, "an offline double was memoised as if it were live"


def test_an_installed_override_wins_and_is_taken_back_out(writer_module):
    sentinel = DeterministicLLM(role="buyer")
    writer_module.set_pitch_writer(sentinel)
    assert writer_module.pitch_writer() is sentinel
    writer_module.set_pitch_writer(None)
    assert writer_module.pitch_writer() is not sentinel


def test_the_live_client_is_built_with_the_render_paths_own_timeout(writer_module):
    """The live path's wall-clock bound is STATED, and it is not the package default of 60s.

    Built directly rather than through ``pitch_writer`` so the process is not left holding a
    memoised live client, and with an explicit ``env`` so nothing is read off ``os.environ``.
    """
    llm_config = importlib.import_module("llm.config")
    live = writer_module.build_pitch_writer({"LLM_PROVIDER": "anthropic"})
    assert live is not None
    assert live.timeout == writer_module.PITCH_TIMEOUT_SECONDS
    assert live.timeout < llm_config.DEFAULT_TIMEOUT_SECONDS
    # Building it opened nothing: `AnthropicLLM` imports no SDK and makes no request until
    # `complete` is called, which is why a test with no API key can construct one at all.
    assert live.model == llm_config.DEFAULT_BUYER_MODEL


def test_a_service_that_cannot_build_a_writer_still_serves_every_slot(
    client, writer_module, monkeypatch
):
    """Rule 2 at the seam: no writer is a degraded case, never a failed request."""
    monkeypatch.setattr(writer_module, "build_pitch_writer", lambda env=None: None)
    response = client.post(RENDER, json=_body())
    assert response.status_code == 200, response.text
    served = response.json()["slots"]
    assert len(served) == 2
    for slot in served:
        assert slot["pitch"]["platform_case"].strip()
        assert slot["pitch"]["platform_case_source"] == "assembled"


def test_the_unbuildable_writer_warns_once_and_not_once_per_request(writer_module, caplog):
    """A misconfigured provider must not write a WARNING line per served shortlist."""
    monkey = importlib.import_module("buyer_svc.pitch.writer")
    monkey._warned = False
    try:
        with caplog.at_level("WARNING", logger=writer_module.__name__):
            for _ in range(4):
                writer_module.build_pitch_writer({"LLM_PROVIDER": "not-a-provider"})
        emitted = [r for r in caplog.records if r.name == writer_module.__name__]
        assert len(emitted) == 1, [r.getMessage() for r in emitted]
    finally:
        monkey._warned = False
