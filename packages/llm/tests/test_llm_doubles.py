"""T-014 acceptance 3: the doubles replay deterministically, with no network anywhere.

The frozen acceptance suite covers the headline behaviour (`RecordedLLM(dict).complete()`
under monkeypatched sockets). This file covers what the frozen test does not: replay from
the committed fixtures (D21), the exact failure mode on an unrecorded prompt, the
`CachedPrompt` lookup path, call recording, and the fact that the double cannot be
influenced by a caller mutating the dict it was built from.
"""

from __future__ import annotations

import json
import socket

import pytest

from packages.llm import (
    CachedPrompt,
    DeterministicLLM,
    RecordedLLM,
    UnrecordedPromptError,
    assemble_prompt,
    available_recordings,
    load_recording,
)

RECORDINGS = {
    "summarize the envelope": "floors respected; max discount 15%",
    "classify the intent": "cluster-serum",
}


class _NetworkBlocked(RuntimeError):
    """Raised by the socket stubs below; never by product code."""


@pytest.fixture
def no_network(monkeypatch):
    """Every socket entry point raises. Mirrors the frozen acceptance suite's guard."""

    def boom(*_args, **_kwargs):
        raise _NetworkBlocked("the test disabled the network; product code reached for it")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    return boom


# --------------------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------------------


def test_replay_is_deterministic_across_calls_and_instances(no_network) -> None:
    double = RecordedLLM(RECORDINGS)
    replies = {double.complete("summarize the envelope") for _ in range(25)}
    assert replies == {"floors respected; max discount 15%"}
    assert RecordedLLM(RECORDINGS).complete("summarize the envelope") == replies.pop()
    assert double.complete("classify the intent") == "cluster-serum"


def test_answering_order_does_not_change_the_answers(no_network) -> None:
    """No queue, no cursor: replies cannot depend on what was asked before."""
    forwards = RecordedLLM(RECORDINGS)
    backwards = RecordedLLM(RECORDINGS)
    a = [forwards.complete(prompt) for prompt in RECORDINGS]
    b = [backwards.complete(prompt) for prompt in reversed(list(RECORDINGS))]
    assert a == list(reversed(b))


def test_the_double_owns_its_table_and_a_caller_cannot_mutate_the_replies() -> None:
    table = dict(RECORDINGS)
    double = RecordedLLM(table)
    table["summarize the envelope"] = "TAMPERED"
    table["a new prompt"] = "smuggled in"
    assert double.complete("summarize the envelope") == RECORDINGS["summarize the envelope"]
    with pytest.raises(UnrecordedPromptError):
        double.complete("a new prompt")
    with pytest.raises(TypeError):
        double.recordings["x"] = "y"  # read-only view


# --------------------------------------------------------------------------------------
# the strict miss
# --------------------------------------------------------------------------------------


def test_an_unrecorded_prompt_raises_a_lookup_error_not_a_network_error(no_network) -> None:
    double = RecordedLLM(RECORDINGS)
    with pytest.raises(UnrecordedPromptError) as excinfo:
        double.complete("a prompt that was never recorded")
    error = excinfo.value
    assert isinstance(error, KeyError), "callers catch KeyError; keep that working"
    assert not isinstance(error, _NetworkBlocked)
    assert error.prompt == "a prompt that was never recorded"
    message = str(error)
    assert "a prompt that was never recorded" in message
    assert not message.startswith("'"), "KeyError repr-quoting would mangle this message"


def test_the_miss_message_names_the_closest_recorded_prompt() -> None:
    """Five tickets will hit this error; it has to say what to do about it."""
    double = RecordedLLM(RECORDINGS, name="store_agent_envelope")
    with pytest.raises(UnrecordedPromptError) as excinfo:
        double.complete("summarize the envelop")  # one character short
    message = str(excinfo.value)
    assert "store_agent_envelope" in message
    assert "summarize the envelope" in message, "the near-miss must be suggested"


def test_whitespace_is_significant_because_the_lookup_is_exact() -> None:
    double = RecordedLLM(RECORDINGS)
    with pytest.raises(UnrecordedPromptError):
        double.complete(" summarize the envelope")


def test_recordings_must_be_a_mapping_of_strings() -> None:
    with pytest.raises(TypeError):
        RecordedLLM([("prompt", "reply")])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RecordedLLM({"prompt": {"not": "a string"}})  # type: ignore[dict-item]


# --------------------------------------------------------------------------------------
# prompt objects and call recording
# --------------------------------------------------------------------------------------


def test_a_cached_prompt_looks_up_by_its_assembled_text(no_network) -> None:
    """The recording key is the string a live call would have sent — static half first."""
    prompt = assemble_prompt("STORE CONTEXT\nfloor 18.00", "BUYER\nquote me")
    double = RecordedLLM({prompt.text: "recorded reply"})
    assert double.complete(prompt) == "recorded reply"
    assert double.last_prompt == prompt.text
    assert double.last_prompt.startswith("STORE CONTEXT")


def test_calls_are_recorded_so_tests_can_assert_on_prompt_assembly(no_network) -> None:
    double = RecordedLLM(RECORDINGS, role="store_agent")
    double.complete("classify the intent", system="be terse", temperature=0)
    call = double.calls[-1]
    assert call.prompt == "classify the intent"
    assert call.role == "store_agent"
    assert call.system == "be terse"
    assert call.kwargs == {"temperature": 0}
    assert double.prompts() == ["classify the intent"]
    double.reset()
    assert double.calls == []
    assert double.complete("classify the intent") == "cluster-serum", "reset keeps replays"


def test_complete_json_parses_a_recorded_json_reply(no_network) -> None:
    double = RecordedLLM({"ask": '{"ok": true}'})
    assert double.complete_json("ask") == {"ok": True}
    broken = RecordedLLM({"ask": "not json"})
    with pytest.raises(json.JSONDecodeError):
        broken.complete_json("ask")


# --------------------------------------------------------------------------------------
# replay straight from the committed fixtures (D21)
# --------------------------------------------------------------------------------------


def test_every_committed_fixture_replays_every_prompt_it_records(no_network) -> None:
    names = available_recordings()
    assert names, "no recorded fixtures are committed; D21 acceptance 3 needs at least one"
    for name in names:
        table = load_recording(name)
        double = RecordedLLM.from_fixture(name)
        for prompt, expected in table.items():
            assert double.complete(prompt) == expected
            assert RecordedLLM.from_fixture(name).complete(prompt) == expected


def test_a_fixture_double_still_refuses_an_unrecorded_prompt(no_network) -> None:
    double = RecordedLLM.from_fixture("buyer_intent")
    with pytest.raises(UnrecordedPromptError):
        double.complete("what is the airspeed velocity of an unladen swallow?")


# --------------------------------------------------------------------------------------
# the non-strict double
# --------------------------------------------------------------------------------------


def test_deterministic_double_answers_anything_stably_and_offline(no_network) -> None:
    double = DeterministicLLM(role="buyer")
    first = double.complete("anything at all")
    assert first == DeterministicLLM(role="buyer").complete("anything at all")
    assert first.startswith("double:buyer:")
    assert double.complete("something else") != first


def test_deterministic_replies_match_the_shared_runtime_double() -> None:
    """proxyshop_support.LLMDouble is the fixture other lanes get; the shapes must agree."""
    from proxyshop_support.llm_double import LLMDouble

    assert DeterministicLLM.deterministic("buyer", "hello") == LLMDouble.deterministic(
        "buyer", "hello"
    )


def test_deterministic_double_accepts_a_fixed_default(no_network) -> None:
    double = DeterministicLLM(role="extract", default='{"claims": []}')
    assert double.complete_json(CachedPrompt("STATIC", "TAIL")) == {"claims": []}
    assert double.last_prompt == "STATIC\n\nTAIL"
