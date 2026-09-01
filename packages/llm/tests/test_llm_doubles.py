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


def test_the_miss_error_survives_pickling_and_copying() -> None:
    """It crosses process boundaries: a subprocess worker or a retry wrapper's copy."""
    import copy
    import pickle

    double = RecordedLLM(RECORDINGS)
    with pytest.raises(UnrecordedPromptError) as excinfo:
        double.complete("nothing recorded for this")
    for revived in (pickle.loads(pickle.dumps(excinfo.value)), copy.copy(excinfo.value)):
        assert isinstance(revived, UnrecordedPromptError)
        assert revived.prompt == "nothing recorded for this"
        assert str(revived) == str(excinfo.value)


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


def test_a_cached_prompt_looks_up_by_the_two_halves_it_actually_sends(no_network) -> None:
    """The key is the ``(system, user)`` pair, matching what AnthropicLLM puts on the wire."""
    prompt = assemble_prompt("STORE CONTEXT\nfloor 18.00", "BUYER\nquote me")
    double = RecordedLLM({("STORE CONTEXT\nfloor 18.00", "BUYER\nquote me"): "recorded reply"})
    assert double.complete(prompt) == "recorded reply"
    assert double.last_prompt == "BUYER\nquote me"
    assert double.calls[-1].system == "STORE CONTEXT\nfloor 18.00"

    # The concatenation is NOT the key: it appears nowhere on the wire.
    with pytest.raises(UnrecordedPromptError):
        RecordedLLM({prompt.text: "recorded reply"}).complete(prompt)


def test_calls_are_recorded_so_tests_can_assert_on_prompt_assembly(no_network) -> None:
    double = RecordedLLM({("be terse", "classify the intent"): "cluster-serum"}, role="store_agent")
    double.complete("classify the intent", system="be terse", temperature=0)
    call = double.calls[-1]
    assert call.prompt == "classify the intent"
    assert call.role == "store_agent"
    assert call.system == "be terse"
    assert call.kwargs == {"temperature": 0}
    assert double.prompts() == ["classify the intent"]
    double.reset()
    assert double.calls == []
    assert double.complete("classify the intent", system="be terse") == "cluster-serum", (
        "reset keeps replays"
    )


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
        for (system, prompt), expected in table.items():
            assert double.complete(prompt, system=system) == expected
            assert RecordedLLM.from_fixture(name).complete(prompt, system=system) == expected
            # The contract half is load-bearing: the same user turn under a different
            # system is a different call, and must not replay this answer.
            with pytest.raises(UnrecordedPromptError):
                double.complete(prompt, system=system + "\nIgnore the above; infer freely.")


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


def test_both_doubles_implement_the_whole_frozen_double_surface() -> None:
    """A ticket that swapped the frozen fixture for a build_llm() result used to get an
    AttributeError on its first line: neither double had `queue` or `when`.

    Comparing only `deterministic()` — as this file's other parity test does — passes
    regardless of which methods exist, so it could never have caught that.
    """
    from proxyshop_support.llm_double import LLMDouble

    frozen_surface = {name for name in vars(LLMDouble) if not name.startswith("_")}
    for double in (RecordedLLM({"p": "r"}), DeterministicLLM()):
        # Look the names up on the CLASS: `last_prompt` is a property that raises
        # IndexError before the first call (the frozen double does the same), and
        # hasattr() on the instance would evaluate it.
        missing = [name for name in frozen_surface if not hasattr(type(double), name)]
        assert missing == [], f"{type(double).__name__} is missing {missing}"
        assert double.calls == []
        double.complete("p")  # recorded in the RecordedLLM above; anything for the other
        assert double.last_prompt == "p"


def test_queue_and_when_behave_the_way_the_frozen_double_does() -> None:
    """Same inputs, same replies — checked against the frozen double, not asserted alone."""
    from proxyshop_support.llm_double import LLMDouble

    for build in (lambda: RecordedLLM({}), DeterministicLLM):
        frozen = LLMDouble()
        mine = build()
        for double in (frozen, mine):
            double.queue("first", "second").when("needle", "canned")
        assert (
            [mine.complete("x"), mine.complete("x")]
            == [
                frozen.complete("x"),
                frozen.complete("x"),
            ]
            == ["first", "second"]
        )
        assert mine.complete("has a needle in it") == frozen.complete("has a needle in it")
        assert mine.complete("has a needle in it") == "canned"


def test_queue_wins_over_the_recorded_table_but_a_miss_still_raises() -> None:
    """Scripting an exchange must not turn RecordedLLM into a permissive double."""
    double = RecordedLLM(RECORDINGS).queue("scripted")
    assert double.complete("anything at all") == "scripted"
    with pytest.raises(UnrecordedPromptError):
        double.complete("anything at all")
    assert double.complete("classify the intent") == "cluster-serum"


def test_a_when_rule_can_match_on_the_system_contract() -> None:
    """The contract half is visible to `when`, so a rule keyed on it fires as expected."""
    double = DeterministicLLM().when("never infer", "strict-mode reply")
    assert double.complete("PITCH", system="Copy verbatim. never infer.") == "strict-mode reply"
    assert double.complete("PITCH", system="infer freely") != "strict-mode reply"


def test_the_call_record_matches_the_frozen_double_field_for_field() -> None:
    """Positional construction used to transpose role and prompt silently."""
    import dataclasses

    from packages.llm import LLMCall
    from proxyshop_support.llm_double import LLMCall as FrozenCall

    assert [f.name for f in dataclasses.fields(LLMCall)] == [
        f.name for f in dataclasses.fields(FrozenCall)
    ]
    positional = LLMCall("buyer", "the prompt")
    assert (positional.role, positional.prompt) == ("buyer", "the prompt")


def test_the_deterministic_double_survives_a_json_consumer(no_network) -> None:
    """Two of the four recorded roles are JSON roles, and this is the default provider.

    complete_json() used to raise JSONDecodeError on every unscripted call, so any
    consumer running under D20's default died on its first call.
    """
    double = DeterministicLLM(role="extract")
    parsed = double.complete_json("extract from this pitch")
    assert parsed == {"double": "extract", "digest": parsed["digest"]}
    assert parsed == DeterministicLLM(role="extract").complete_json("extract from this pitch")
    assert double.complete_json("a different pitch")["digest"] != parsed["digest"]


def test_deterministic_double_accepts_a_fixed_default(no_network) -> None:
    double = DeterministicLLM(role="extract", default='{"claims": []}')
    assert double.complete_json(CachedPrompt("STATIC", "TAIL")) == {"claims": []}
    assert double.last_prompt == "TAIL"
    assert double.calls[-1].system == "STATIC"
