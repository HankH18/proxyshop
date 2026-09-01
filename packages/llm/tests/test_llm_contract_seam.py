"""The system half is part of a call's identity — in the double AND on the wire.

Every one of these tests exists because its absence let a defect through. The double used
to key on the user turn alone, so an inverted prompt contract replayed the old reviewed
answer and five downstream tickets would have tested a whole class of contract regressions
into permanent green. The client used to merge a per-call system in FRONT of the store
context, destroying the cache prefix the package exists to protect. Neither had a test,
and both were invisible in output.
"""

from __future__ import annotations

import socket

import pytest

from packages.llm import (
    AnthropicLLM,
    RecordedLLM,
    UnrecordedPromptError,
    assemble_prompt,
    wire_key,
)

STRICT_CONTRACT = "EXTRACTION CONTRACT\nCopy values verbatim. Never infer. Cite a span."
LOOSE_CONTRACT = "EXTRACTION CONTRACT\nInfer freely. A span is optional."
PITCH = "PITCH\nOur Guji is a light roast, 12 oz, roasted 3 days ago."
REVIEWED = '{"claims": [{"key": "roast_level", "value": "light", "span": [17, 28]}]}'


class _NetworkBlocked(RuntimeError):
    """Raised by the socket stubs below; never by product code."""


@pytest.fixture
def no_network(monkeypatch):
    def boom(*_args, **_kwargs):
        raise _NetworkBlocked("the test disabled the network; product code reached for it")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    return boom


class _Recorder:
    """A stand-in Anthropic client that keeps the last request. No SDK, no network."""

    def __init__(self, stop_reason: str = "end_turn", text: str = "ok") -> None:
        self.requests: list[dict] = []
        self._stop_reason = stop_reason
        self._text = text
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        content = [{"type": "text", "text": self._text}] if self._text else []
        return {"content": content, "stop_reason": self._stop_reason}

    @property
    def last(self) -> dict:
        return self.requests[-1]


# ======================================================================================
# the double sees the contract
# ======================================================================================


def test_inverting_the_contract_does_not_replay_the_reviewed_answer(no_network) -> None:
    """The defect, stated directly: 'never infer' -> 'infer freely' must not stay green."""
    double = RecordedLLM({(STRICT_CONTRACT, PITCH): REVIEWED})
    assert double.complete(PITCH, system=STRICT_CONTRACT) == REVIEWED

    with pytest.raises(UnrecordedPromptError) as excinfo:
        double.complete(PITCH, system=LOOSE_CONTRACT)
    message = str(excinfo.value)
    assert "DIFFERENT" in message and "system" in message, (
        "the message must name the contract as the thing that changed — this is the miss "
        "a downstream ticket will hit, and 'no recording' alone sends them prompt-hunting"
    )
    assert not isinstance(excinfo.value, _NetworkBlocked)


def test_dropping_the_contract_entirely_is_also_a_miss(no_network) -> None:
    double = RecordedLLM({(STRICT_CONTRACT, PITCH): REVIEWED})
    with pytest.raises(UnrecordedPromptError):
        double.complete(PITCH)


def test_a_recording_with_no_contract_still_works_verbatim(no_network) -> None:
    """The frozen acceptance suite passes a plain dict[str, str]; that must keep working."""
    double = RecordedLLM({"summarize the envelope": "floors respected"})
    assert double.complete("summarize the envelope") == "floors respected"
    assert double.recordings == {("", "summarize the envelope"): "floors respected"}


def test_the_double_keys_on_exactly_what_the_client_sends(no_network) -> None:
    """The anti-drift assertion: one function computes the key, and the client uses it.

    If the client's composition and the double's key were computed separately, they could
    disagree and a recording would be unreachable through the very client it recorded.
    """
    prompt = assemble_prompt("STORE CONTEXT\nfloor 18.00", "BUYER\nquote me")
    per_call = "turn 3 of 9"

    recorder = _Recorder()
    AnthropicLLM("buyer", model="m", client=recorder).complete(prompt, system=per_call)
    sent_system = "\n\n".join(block["text"] for block in recorder.last["system"])
    sent_user = recorder.last["messages"][0]["content"][0]["text"]

    assert wire_key(prompt, per_call) == (sent_system, sent_user)
    double = RecordedLLM({wire_key(prompt, per_call): "recorded"})
    assert double.complete(prompt, system=per_call) == "recorded"


# ======================================================================================
# the per-call system never lands in front of the cached prefix
# ======================================================================================


def test_a_per_call_system_is_appended_after_the_store_context() -> None:
    prompt = assemble_prompt("STORE ENVELOPE\nfloor 18.00; max discount 15%", "quote me")
    recorder = _Recorder()
    AnthropicLLM("store_agent", model="m", client=recorder).complete(prompt, system="turn 1 of 9")

    blocks = recorder.last["system"]
    assert len(blocks) == 2
    assert blocks[0]["text"] == "STORE ENVELOPE\nfloor 18.00; max discount 15%"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["text"] == "turn 1 of 9"
    assert "cache_control" not in blocks[1], (
        "the breakpoint belongs on the stable block; caching a per-call one writes an "
        "entry nothing will ever read"
    )


def test_a_varying_per_call_system_leaves_the_cached_prefix_byte_identical() -> None:
    """The regression itself. Under the old merge, block 0 changed on every turn."""
    prompt = assemble_prompt("STORE ENVELOPE\nfloor 18.00", "quote me")
    recorder = _Recorder()
    client = AnthropicLLM("store_agent", model="m", client=recorder)

    for turn in range(1, 6):
        client.complete(prompt, system=f"turn {turn} of 9 at 2026-02-14T10:0{turn}:00Z")

    cached_blocks = {request["system"][0]["text"] for request in recorder.requests}
    assert cached_blocks == {"STORE ENVELOPE\nfloor 18.00"}, (
        f"the cacheable prefix moved between turns: {sorted(cached_blocks)}"
    )
    assert all("cache_control" in request["system"][0] for request in recorder.requests)
    assert len({request["system"][1]["text"] for request in recorder.requests}) == 5


def test_the_store_context_still_leads_when_there_is_no_per_call_system() -> None:
    prompt = assemble_prompt("STORE ENVELOPE\nfloor 18.00", "quote me")
    recorder = _Recorder()
    AnthropicLLM("store_agent", model="m", client=recorder).complete(prompt)
    assert [block["text"] for block in recorder.last["system"]] == ["STORE ENVELOPE\nfloor 18.00"]
