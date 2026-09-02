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
    SYSTEM_BLOCK_SEPARATOR,
    AnthropicLLM,
    CachedPrompt,
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
    # W1-24: this join used to be a literal "\n\n" (SECTION_SEPARATOR). The requirement
    # asserted below — the key is the canonical join of exactly the blocks the client sent,
    # paired with exactly the user turn it sent — is unchanged; only the separator moved.
    # "\n\n" can occur inside a block's own text, so it made two genuinely different calls
    # canonicalise to one key: CachedPrompt("A") + system="B" (two blocks) and
    # CachedPrompt("A\n\nB") (one block) both became "A\n\nB", and RecordedLLM then replayed
    # the reply reviewed for the other one. SYSTEM_BLOCK_SEPARATOR is NUL, which cannot.
    sent_system = SYSTEM_BLOCK_SEPARATOR.join(block["text"] for block in recorder.last["system"])
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


# ======================================================================================
# W1-15: ONE composer, over every input shape — not just the one that had a test
# ======================================================================================

CUSTOM_SEPARATOR = "\n---\n"

#: ``(label, prompt, system, expected_system_blocks, expected_user_turn)`` — the full
#: cross-product of {CachedPrompt, bare string} x {system, no system} x {default, custom
#: separator}. Exactly one of these eight shapes (CachedPrompt + system + default
#: separator) had coverage, and the bare-string branch composed its request independently
#: of ``wire_key``: measured against 9a1ea2f, stripping the halves in the client's string
#: branch left the whole suite green while making a recording unreachable through the very
#: client it was recorded for.
#:
#: The expectations are written as LITERALS on purpose. Asserting only that the client and
#: ``wire_key`` agree would stay green under any change applied to the shared composer —
#: the sabotage this file exists to catch.
COMPOSITION_SHAPES = [
    (
        "cached-prompt + system",
        CachedPrompt("  STORE CONTEXT  ", "  BUYER\nquote me  "),
        "  turn 3 of 9  ",
        ["  STORE CONTEXT  ", "  turn 3 of 9  "],
        "  BUYER\nquote me  ",
    ),
    (
        "cached-prompt, no system",
        CachedPrompt("  STORE CONTEXT  ", "  BUYER\nquote me  "),
        None,
        ["  STORE CONTEXT  "],
        "  BUYER\nquote me  ",
    ),
    (
        "cached-prompt + system, custom separator",
        CachedPrompt("  STORE CONTEXT  ", "  BUYER\nquote me  ", CUSTOM_SEPARATOR),
        "  turn 3 of 9  ",
        ["  STORE CONTEXT  ", "  turn 3 of 9  "],
        "  BUYER\nquote me  ",
    ),
    (
        "cached-prompt, no system, custom separator",
        CachedPrompt("  STORE CONTEXT  ", "  BUYER\nquote me  ", CUSTOM_SEPARATOR),
        None,
        ["  STORE CONTEXT  "],
        "  BUYER\nquote me  ",
    ),
    (
        "bare string + system",
        "  BUYER\nquote me  ",
        "  turn 3 of 9  ",
        ["  turn 3 of 9  "],
        "  BUYER\nquote me  ",
    ),
    (
        "bare string, no system",
        "  BUYER\nquote me  ",
        None,
        [],
        "  BUYER\nquote me  ",
    ),
    (
        "bare string + empty system",
        "  BUYER\nquote me  ",
        "",
        [],
        "  BUYER\nquote me  ",
    ),
    (
        "cached-prompt with an empty static context",
        CachedPrompt("", "  BUYER\nquote me  "),
        None,
        [],
        "  BUYER\nquote me  ",
    ),
]


@pytest.mark.parametrize(
    ("prompt", "system", "expected_blocks", "expected_user"),
    [shape[1:] for shape in COMPOSITION_SHAPES],
    ids=[shape[0] for shape in COMPOSITION_SHAPES],
)
def test_every_prompt_shape_composes_the_same_way_for_the_client_and_the_double(
    no_network, prompt, system, expected_blocks, expected_user
) -> None:
    """One composer, checked against literals, over every shape a consumer can pass.

    Three separate claims, and the literals are what make the first two real:

    1. the client puts exactly ``expected_blocks`` and ``expected_user`` on the wire;
    2. ``wire_key`` — what the doubles look up — reports exactly the same two halves;
    3. a recording keyed on ``wire_key`` is therefore reachable through the client.

    Claim 3 alone cannot catch a change applied to the shared composer, because both
    sides would move together. Claims 1 and 2 can, which is why every expectation carries
    its surrounding whitespace: stripping a half is the measured sabotage.
    """
    recorder = _Recorder()
    AnthropicLLM("buyer", model="m", client=recorder).complete(prompt, system=system)

    sent_blocks = [block["text"] for block in recorder.last.get("system", [])]
    sent_user = recorder.last["messages"][0]["content"][0]["text"]
    assert sent_blocks == expected_blocks
    assert sent_user == expected_user
    assert ("system" in recorder.last) == bool(expected_blocks), (
        "an empty system block list must be omitted from the request, never sent as []"
    )

    key = wire_key(prompt, system)
    assert key == (SYSTEM_BLOCK_SEPARATOR.join(expected_blocks), expected_user)

    double = RecordedLLM({key: "recorded"})
    assert double.complete(prompt, system=system) == "recorded", (
        "a recording keyed on wire_key must be reachable through the client that sends it"
    )


def test_a_bare_string_prompt_with_no_system_still_keys_on_exactly_the_prompt(
    no_network,
) -> None:
    """The frozen acceptance path: ``RecordedLLM({"p": "r"}).complete("p")``.

    Every other shape may move; this one may not. The frozen suite builds a
    ``dict[str, str]`` and calls with a plain string, so the composed key has to stay
    ``("", prompt)`` byte for byte.
    """
    assert wire_key("summarize the envelope") == ("", "summarize the envelope")
    assert wire_key("summarize the envelope", None) == ("", "summarize the envelope")
    assert (
        RecordedLLM({"summarize the envelope": "floors respected"}).complete(
            "summarize the envelope"
        )
        == "floors respected"
    )


def test_two_different_block_structures_do_not_collide_on_one_key(no_network) -> None:
    """W1-24: joining system blocks with "\\n\\n" made two distinct calls one key.

    ``CachedPrompt("A") + system="B"`` is two blocks; ``CachedPrompt("A\\n\\nB")`` is one.
    They are different requests — different cache structure, different bytes on the wire —
    and both used to canonicalise to ``("A\\n\\nB", "q")``, so a double built for one
    replayed its reviewed answer for the other.
    """
    two_blocks = CachedPrompt("A", "q")
    one_block = CachedPrompt("A\n\nB", "q")

    assert wire_key(two_blocks, "B") != wire_key(one_block)

    double = RecordedLLM({wire_key(one_block): "answer for the one-block call"})
    assert double.complete(one_block) == "answer for the one-block call"
    with pytest.raises(UnrecordedPromptError):
        double.complete(two_blocks, system="B")


def test_a_separator_is_a_property_of_the_assembled_text_not_of_the_wire(no_network) -> None:
    """A custom separator changes ``.text`` and the cache prefix — never the request.

    The two halves travel separately, so the separator that joins them for a human reader
    is never sent, and a double keyed on one separator must answer a call made with the
    other.
    """
    default = CachedPrompt("STATIC", "TAIL")
    custom = CachedPrompt("STATIC", "TAIL", CUSTOM_SEPARATOR)
    assert default.text != custom.text
    assert wire_key(default) == wire_key(custom) == ("STATIC", "TAIL")

    recorder = _Recorder()
    client = AnthropicLLM("buyer", model="m", client=recorder)
    client.complete(default)
    first = recorder.last
    client.complete(custom)
    assert recorder.last == first
