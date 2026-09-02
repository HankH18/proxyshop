"""The system half is part of a call's identity — in the double AND on the wire.

Every one of these tests exists because its absence let a defect through. The double used
to key on the user turn alone, so an inverted prompt contract replayed the old reviewed
answer and five downstream tickets would have tested a whole class of contract regressions
into permanent green. The client used to merge a per-call system in FRONT of the store
context, destroying the cache prefix the package exists to protect. Neither had a test,
and both were invisible in output.
"""

from __future__ import annotations

import json
import socket

import pytest

from packages.llm import (
    AnthropicLLM,
    CachedPrompt,
    DeterministicLLM,
    KeyedLLMCall,
    LLMCall,
    PromptAssemblyError,
    RecordedLLM,
    UnrecordedPromptError,
    assemble_prompt,
    canonical_system_key,
    compose_request,
    load_recording_file,
    system_key_blocks,
    system_key_text,
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
    # The requirement asserted below — the key is exactly the blocks the client sent,
    # paired with exactly the user turn it sent — has never changed. Only the way a block
    # LIST is spelled as one key has. It was "\n\n"-joined (W1-24: a blank line inside a
    # block forged a boundary), then NUL-joined (T-104: JSON writes NUL as an escape, so a
    # fixture could carry one), and is now the block texts themselves — no character at
    # all, so nothing a block contains can forge a boundary. The tuple is written out here
    # rather than computed by product code, so this stays an independent check.
    sent_blocks = tuple(block["text"] for block in recorder.last["system"])
    sent_user = recorder.last["messages"][0]["content"][0]["text"]
    assert sent_blocks == ("STORE CONTEXT\nfloor 18.00", "turn 3 of 9")

    assert wire_key(prompt, per_call) == (sent_blocks, sent_user)
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

    # The system half of a key is the block list itself: "" for none, the text verbatim
    # for one, the tuple of texts for several. Spelled out here rather than imported, so
    # this is a check on the product and not a restatement of it. It replaces a
    # `SEPARATOR.join(expected_blocks)` that encoded the same requirement — "the key is
    # exactly the blocks the client sent" — back when a key flattened them into a string.
    expected_system: object
    if not expected_blocks:
        expected_system = ""
    elif len(expected_blocks) == 1:
        expected_system = expected_blocks[0]
    else:
        expected_system = tuple(expected_blocks)

    key = wire_key(prompt, system)
    assert key == (expected_system, expected_user)

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


# ======================================================================================
# T-104 / W2-06: there is no separator, so there is no character to collide on
# ======================================================================================

#: Characters a "this one cannot appear in prompt text" argument has been made for, or
#: could plausibly be made for next. Each is fed to
#: :func:`test_no_character_can_be_the_system_block_separator` as the text a single system
#: block embeds; under any delimiter-based key, the entry matching that key's delimiter
#: collides and cross-replays. Under a key that is the block list, none of them can.
CANDIDATE_SEPARATORS = [
    pytest.param("\n\n", id="blank-line (the W1-24 separator)"),
    pytest.param("\x00", id="NUL (the T-104 separator)"),
    pytest.param("\x1e", id="ascii record separator"),
    pytest.param("\x1f", id="ascii unit separator"),
    pytest.param("␞", id="unicode symbol for record separator"),
    pytest.param("￾", id="a noncharacter"),
    pytest.param("|", id="a plain pipe"),
    pytest.param("<<<SYSTEM-BLOCK>>>", id="a long improbable marker"),
]


@pytest.mark.parametrize("candidate", CANDIDATE_SEPARATORS)
def test_no_character_can_be_the_system_block_separator(no_network, candidate) -> None:
    """The generalisation of W1-24 and T-104, which were the same bug twice.

    Both fixes picked a rarer character; both times the collision was still reachable,
    because *every* join of a list of strings into one string is non-injective — the
    argument "this character cannot appear in prompt text" is a claim about the corpus,
    not about the encoding, and prompt text comes from JSON fixtures and templates that
    can carry anything. ``"\\n\\n"`` fell to a blank line inside a block; NUL fell to
    ``"\\u0000"`` in a fixture (T-104's objective).

    So this test does not name the separator. It asserts the property that makes naming
    one unnecessary: a request of ONE system block whose text embeds ``candidate`` is a
    different call from a request of TWO blocks, whatever ``candidate`` is — different
    bytes, different cache structure — and the double must not answer either with the
    other's reviewed reply.
    """
    one_block = CachedPrompt("A" + candidate + "B", "q")
    two_blocks = CachedPrompt("A", "q")

    # These really are different requests, not two spellings of one.
    assert compose_request(one_block)[0] != compose_request(two_blocks, "B")[0]
    assert wire_key(one_block) != wire_key(two_blocks, "B")

    # ...and neither recording reaches the other call, in either direction.
    for recorded, recorded_key, other, other_system in (
        ("the ONE-block reply", wire_key(one_block), two_blocks, "B"),
        ("the TWO-block reply", wire_key(two_blocks, "B"), one_block, None),
    ):
        double = RecordedLLM({recorded_key: recorded})
        with pytest.raises(UnrecordedPromptError):
            double.complete(other, system=other_system)

    # The recording that WAS made is still reachable, so this is not strictness by
    # breaking the lookup.
    assert RecordedLLM({wire_key(one_block): "reviewed"}).complete(one_block) == "reviewed"
    assert (
        RecordedLLM({wire_key(two_blocks, "B"): "reviewed"}).complete(two_blocks, system="B")
        == "reviewed"
    )


def test_a_fixture_can_carry_a_nul_and_still_not_capture_a_two_block_call(
    no_network, tmp_path
) -> None:
    """T-104's objective, end to end: JSON permits an escaped NUL and the loader takes it.

    The NUL defence was "NUL cannot appear in prompt text that came from a JSON fixture".
    It can: ``"\\u0000"`` is valid JSON, and :func:`llm.recordings.load_recording_file`
    has no reason to reject it (nor should it — a contract's bytes are the author's
    business). This is the whole live path: hand-authored file -> loader -> double ->
    a two-block call that must NOT be answered by it.
    """
    fixture = tmp_path / "nul_contract.json"
    fixture.write_text(
        json.dumps(
            {
                "provenance": {
                    "source": "https://docs.anthropic.com/",
                    "doc_version": "2026-02",
                    "authored_by": "test",
                    "authored_at": "2026-02-14",
                    "capture": "hand-authored for T-104",
                },
                "system": "A\x00B",
                "recordings": {"q": "reviewed against the ONE-block contract"},
            }
        ),
        encoding="utf-8",
    )
    assert "\\u0000" in fixture.read_text(encoding="utf-8"), "JSON escapes it rather than refusing"

    keyed, _ = load_recording_file(fixture)
    assert list(keyed) == [("A\x00B", "q")], "one block whose text happens to contain a NUL"

    double = RecordedLLM(keyed)
    assert double.complete(CachedPrompt("A\x00B", "q")) == "reviewed against the ONE-block contract"
    with pytest.raises(UnrecordedPromptError):
        double.complete(CachedPrompt("A", "q"), system="B")


def test_the_system_half_of_a_key_is_the_block_list_itself(no_network) -> None:
    """Acceptance 1, stated as the shape: block texts as a tuple, never a joined string.

    The two degenerate arities collapse to a plain ``str`` — that is what keeps the frozen
    ``("", prompt)`` path and the obvious ``(system, prompt)`` fixture spelling intact —
    and the collapse is injective only because a system block is never empty, which
    :func:`test_a_system_block_is_never_empty_so_the_collapse_stays_injective` pins.
    """
    assert wire_key("q") == ("", "q")
    assert wire_key(CachedPrompt("A", "q")) == ("A", "q")
    assert wire_key(CachedPrompt("A", "q"), "B") == (("A", "B"), "q")

    system_half = wire_key(CachedPrompt("A", "q"), "B")[0]
    assert isinstance(system_half, tuple)
    assert not isinstance(system_half, str), (
        "a joined string is exactly what T-104 forbids: it is where a collision lives"
    )


def test_a_system_block_is_never_empty_so_the_collapse_stays_injective() -> None:
    """The precondition for ``[]`` -> ``""`` and ``["X"]`` -> ``"X"`` being unambiguous.

    If ``to_system_blocks`` could emit an empty block, a one-block call would key on
    ``""`` — the zero-block key — and the collapse would reintroduce a collision at the
    one arity nobody would think to test.
    """
    for prompt, extra in (
        (CachedPrompt("", "q"), None),
        (CachedPrompt("", "q"), ""),
        (CachedPrompt("A", "q"), ""),
        (CachedPrompt("A", "q"), "B"),
    ):
        blocks = prompt.to_system_blocks(extra=extra)
        assert all(block["text"] for block in blocks), blocks


def test_every_distinct_block_list_gets_a_distinct_key() -> None:
    """Injectivity over a corpus built out of the very characters a join would use.

    A delimiter-based key is a function from a list of strings to one string, so some two
    of these lists must share a value under any such key; the tuple form has no collisions
    by construction. Empty block texts are excluded because the composer never emits one.
    """
    pieces = ["A", "B", "A\x00B", "A\n\nB", "AB", "A|B", "\x00", "\n\n"]
    lists = [[]] + [[x] for x in pieces] + [[x, y] for x in pieces for y in pieces]

    keys = {}
    for blocks in lists:
        key = canonical_system_key(blocks)
        assert key not in keys, f"{blocks!r} collides with {keys.get(key)!r} on {key!r}"
        keys[key] = blocks
    assert len(keys) == len(lists)


def test_a_recording_key_can_name_a_multi_block_call_without_any_separator(no_network) -> None:
    """A hand-written recording for a two-block call is spelled as the two block texts.

    Before the tuple key there was no way to write one except by knowing the separator and
    embedding it, which is the same thing as writing the colliding key.
    """
    two_blocks = CachedPrompt("STORE ENVELOPE", "quote me")
    double = RecordedLLM({(("STORE ENVELOPE", "turn 3 of 9"), "quote me"): "reviewed"})
    assert double.complete(two_blocks, system="turn 3 of 9") == "reviewed"

    # A one-block key holding the joined text is a DIFFERENT call and must not answer it.
    joined = RecordedLLM({("STORE ENVELOPE\n\nturn 3 of 9", "quote me"): "reviewed"})
    with pytest.raises(UnrecordedPromptError):
        joined.complete(two_blocks, system="turn 3 of 9")

    # A one-element tuple is the same one block as the bare string, so both spellings of
    # a single-block recording reach the same call.
    assert (
        RecordedLLM({(("STORE ENVELOPE",), "quote me"): "reviewed"}).complete(
            two_blocks.with_dynamic("quote me")
        )
        == "reviewed"
    )

    # A non-string block is refused rather than silently stringified into a key that no
    # composed call could ever reach.
    with pytest.raises(TypeError):
        RecordedLLM({((1, 2), "quote me"): "reviewed"})


def test_the_recorded_call_and_the_miss_message_still_read_as_text(no_network) -> None:
    """The key is structured; the human-facing copies of it are not, and must stay flat.

    ``LLMCall.system`` is what a downstream ticket asserts on and what a log prints, so a
    tuple leaking into it would break every such assertion. It is a rendering, never an
    identity — which is why the two calls below record the same text and still key apart.
    """
    double = DeterministicLLM(role="store_agent")
    double.complete(CachedPrompt("A", "q"), system="B")
    assert double.calls[-1].system == "A\n\nB"
    assert isinstance(double.calls[-1].system, str)

    assert system_key_text("") == ""
    assert system_key_text("A") == "A"
    assert system_key_text(("A", "B")) == "A\n\nB"

    # The miss message prints the same flat rendering rather than a tuple repr.
    with pytest.raises(UnrecordedPromptError) as excinfo:
        RecordedLLM({}).complete(CachedPrompt("A", "q"), system="B")
    assert "'A\\n\\nB'" in str(excinfo.value)


def test_the_deterministic_double_also_tells_the_block_structures_apart(no_network) -> None:
    """Its one claim is "changing the contract changes the answer"; a join would break it.

    The reply is a hash, so flattening the blocks first would hand a two-block call the
    reply belonging to the one-block call whose text joins to the same string.
    """
    one_block = CachedPrompt("A\n\nB", "q")
    two_blocks = CachedPrompt("A", "q")
    assert DeterministicLLM().complete(one_block) != DeterministicLLM().complete(
        two_blocks, system="B"
    )

    # ...and the no-system and single-block replies are unchanged, which is where the
    # documented byte-for-byte parity with the frozen LLMDouble lives.
    from proxyshop_support.llm_double import LLMDouble

    assert DeterministicLLM().complete("a prompt") == LLMDouble().complete("a prompt")
    assert DeterministicLLM().complete("q", system="A") == DeterministicLLM().complete(
        CachedPrompt("A", "q")
    )


# --------------------------------------------------------------------------------------
# T-118 (b): canonical_system_key enforces its OWN injectivity precondition
# --------------------------------------------------------------------------------------


def test_canonical_system_key_refuses_the_empty_block_it_cannot_encode() -> None:
    """The single-element collapse is injective only if a block is never empty.

    ``canonical_system_key([x]) -> x`` is what lets a one-block recording be spelled the
    obvious way, but it means ``[""]`` lands on ``""`` — the key for NO system blocks at
    all. ``to_system_blocks`` never emits an empty block, so nothing *composed* collides;
    a direct caller (a hand-written recording key, a consumer building blocks itself) was
    not covered by that, and got a silent alias instead of an error.
    """
    # The three spellings of "no system contract" are untouched: they mean zero blocks.
    assert canonical_system_key(None) == ""
    assert canonical_system_key("") == ""
    assert canonical_system_key(()) == ""
    assert canonical_system_key([]) == ""

    # One EMPTY block is not "no blocks", and there is no key that can say so.
    for empty_block in ([""], ("",), ["", "B"], ("A", ""), ("A", "", "B")):
        with pytest.raises(PromptAssemblyError) as excinfo:
            canonical_system_key(empty_block)
        assert "empty" in str(excinfo.value)

    # The keys that do exist stay exactly as they were.
    assert canonical_system_key(["A"]) == "A"
    assert canonical_system_key(["A", "B"]) == ("A", "B")


def test_a_recording_key_cannot_alias_the_no_system_call_with_an_empty_block() -> None:
    """The collision, at the surface where it would actually have hurt.

    ``RecordedLLM({(("",), "p"): reply})`` used to normalise to ``("", "p")``, so a call
    passing NO system contract replayed a reply authored for a call that carried one.
    """
    with pytest.raises(PromptAssemblyError):
        RecordedLLM({(("",), "p"): "reply reviewed for a one-empty-block contract"})

    # The plain-string path — what the frozen acceptance suite uses — is unaffected: it
    # still keys on exactly ("", prompt), and ("", prompt) is still writable as a tuple.
    assert wire_key("p") == ("", "p")
    assert RecordedLLM({"p": "no contract"}).complete("p") == "no contract"
    assert RecordedLLM({("", "p"): "no contract"}).complete("p") == "no contract"


def test_composed_calls_never_reach_the_empty_block_guard() -> None:
    """The guard must be unreachable through assembly, or it would break real calls."""
    assert wire_key(CachedPrompt("", "q")) == ("", "q")
    assert wire_key(CachedPrompt("", "q"), "") == ("", "q")
    assert wire_key("q", "") == ("", "q")
    assert wire_key(CachedPrompt("A", "q")) == ("A", "q")
    assert wire_key(CachedPrompt("A", "q"), "B") == (("A", "B"), "q")
    assert DeterministicLLM().complete(CachedPrompt("", "q"), system="")


# --------------------------------------------------------------------------------------
# T-118 (a): the OBSERVABLE recording surface must not flatten the block structure
# --------------------------------------------------------------------------------------

# The pair that collides under any join. Genuinely different requests — two system blocks
# versus one — whose `system_key_text` renderings are byte-identical.
TWO_BLOCK = (CachedPrompt("A", "q"), "B")
ONE_BLOCK = (CachedPrompt("A\n\nB", "q"), None)


def test_the_recorded_call_tells_two_block_structures_apart(no_network) -> None:
    """The defect: two different calls recorded byte-identical `LLMCall`s.

    `wire_key` has kept them apart since T-104, but `.calls` — the surface a test actually
    asserts on — flattened the key back into one joined string, so a test asking "did the
    per-call system arrive as its own block?" could not tell and would pass either way.
    """
    for double in (DeterministicLLM(), RecordedLLM({})):
        recorded = []
        for prompt, system in (TWO_BLOCK, ONE_BLOCK):
            try:
                double.complete(prompt, system=system)
            except UnrecordedPromptError:
                pass  # RecordedLLM({}) misses; the call is recorded before the lookup
            recorded.append(double.calls[-1])
        two, one = recorded

        # The rendering is still the join, unchanged and still a str (downstream tickets
        # and the frozen LLMDouble both read it) — so it still cannot separate them.
        assert two.system == one.system == "A\n\nB"
        assert isinstance(two.system, str)

        # The structure can, and the whole call record no longer compares equal.
        assert two.system_key == ("A", "B")
        assert one.system_key == "A\n\nB"
        assert two.system_blocks == ("A", "B")
        assert one.system_blocks == ("A\n\nB",)
        assert two != one, "two different calls must not present as the same record"


def test_the_recorded_key_is_the_key_the_lookup_actually_used(no_network) -> None:
    """Anti-drift: the record is derived from the lookup key, not composed a second time.

    A record built independently could disagree with the key — which is exactly the class
    of defect `wire_key` was centralised to remove — and then `.calls` would be evidence
    about a call that never happened.
    """
    for prompt, system in (TWO_BLOCK, ONE_BLOCK, ("plain prompt", None), ("p", "one block")):
        double = DeterministicLLM()
        double.complete(prompt, system=system)
        call = double.calls[-1]
        expected_key, expected_prompt = wire_key(prompt, system)
        assert call.system_key == expected_key
        assert call.prompt == expected_prompt
        # ...and `.system` is that key rendered, never a separately assembled string.
        assert call.system == (system_key_text(expected_key) or None)
        assert call.system_blocks == system_key_blocks(expected_key)
        assert canonical_system_key(call.system_blocks) == call.system_key


def test_the_call_record_keeps_the_frozen_doubles_four_fields(no_network) -> None:
    """The structure is added BESIDE `LLMCall`, never folded into `system`.

    `LLMCall` mirrors the orchestrator-frozen `proxyshop_support.llm_double.LLMCall`
    field for field, so a fifth field there would break positional parity. The extra one
    lives on the subclass the doubles actually record.
    """
    import dataclasses

    from proxyshop_support.llm_double import LLMCall as FrozenCall

    assert [f.name for f in dataclasses.fields(LLMCall)] == [
        f.name for f in dataclasses.fields(FrozenCall)
    ]
    assert [f.name for f in dataclasses.fields(KeyedLLMCall)] == [
        f.name for f in dataclasses.fields(FrozenCall)
    ] + ["system_key"]

    double = DeterministicLLM()
    double.complete("p")
    call = double.calls[-1]
    assert isinstance(call, KeyedLLMCall)
    assert isinstance(call, LLMCall), "everything reading .calls as LLMCalls keeps working"

    # A call with no system contract: `.system` stays None, exactly as the frozen double.
    assert call.system is None
    assert call.system_key == ""
    assert call.system_blocks == ()


def test_the_contract_miss_message_never_prints_two_identical_contracts(no_network) -> None:
    """The same defect at the error surface, where it read as a bug in the double.

    A two-block call missing against a one-block recording whose text joins identically is
    a real and correct miss — but the message announced "a DIFFERENT system contract" and
    then printed the same string twice, which is the most confusing thing it could say.
    """
    joined = RecordedLLM({("STORE ENVELOPE\n\nturn 3 of 9", "quote me"): "reviewed"})
    with pytest.raises(UnrecordedPromptError) as excinfo:
        joined.complete(CachedPrompt("STORE ENVELOPE", "quote me"), system="turn 3 of 9")
    message = str(excinfo.value)
    assert "DIFFERENT" in message

    # Compare what each line SAYS about the contract, not the "sent:"/"recorded:" labels.
    sent, recorded = (
        line.split(":", 1)[1].strip() for line in message.splitlines() if line.startswith("system ")
    )
    assert sent != recorded, f"the message printed the same contract twice:\n{message}"
    assert "2 separate blocks" in sent
    assert "('STORE ENVELOPE', 'turn 3 of 9')" in sent
    assert "separate blocks" not in recorded, "one block has nothing to disambiguate"

    # The reverse miss — a one-block call against a two-block recording — reads too.
    two = RecordedLLM({(("STORE ENVELOPE", "turn 3 of 9"), "quote me"): "reviewed"})
    with pytest.raises(UnrecordedPromptError) as excinfo:
        two.complete(CachedPrompt("STORE ENVELOPE\n\nturn 3 of 9", "quote me"))
    lines = [line for line in str(excinfo.value).splitlines() if line.startswith("system ")]
    assert lines[0] != lines[1]
    assert "2 separate blocks" in lines[1]

    # A single-block miss is unchanged: no structure noise where there is no ambiguity.
    with pytest.raises(UnrecordedPromptError) as excinfo:
        RecordedLLM({}).complete("nothing recorded", system="one block")
    assert "separate blocks" not in str(excinfo.value)
    assert "'one block'" in str(excinfo.value)
