"""T-014 acceptance 2: prompt assembly puts the store context BEFORE the dynamic tail.

There is no frozen acceptance test for this criterion, so this file is the only proof it
ever gets, and it is written to actually fail if the order were reversed.

Why the order is the whole point: Anthropic's prompt cache only ever matches a **prefix**.
The cached span runs from the first byte of the request to a ``cache_control`` breakpoint,
so it can only be reused when every byte before that breakpoint is identical to last time.
Put the per-request text first and the prefix is unique on every call — the assembled
prompt still contains the same words, the model still answers correctly, and nothing is
ever a cache hit. That failure is invisible in output; it shows up only in latency and
spend. Hence: assert on the boundary, not on the text.

The discriminating test is :func:`test_the_boundary_check_actually_fails_on_reversed_order`,
which runs the *same* property check against a deliberately reversed assembler and
requires it to fail. Without that, a "cache boundary test" could be one that passes either
way — which is not a test.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from packages.llm import (
    CACHE_CONTROL,
    SECTION_SEPARATOR,
    CachedPrompt,
    PromptAssemblyError,
    assemble_prompt,
)

STORE_CONTEXT = (
    "STORE CONTEXT\n"
    "store: bean-cellar (store-1)\n"
    "envelope v3: floor 18.00 USD; max discount 15%; free shipping over 40.00 USD\n"
    "tools: get_product_fact, get_live_state, authorize_discount"
)
TAIL_ONE = "BUYER REQUEST\nquote 2 bags of the Ethiopia Guji"
TAIL_TWO = "BUYER REQUEST\ndo you ship to Maine, and how fast?"


# --------------------------------------------------------------------------------------
# the property, expressed once so it can be run against a WRONG assembler too
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Assembled:
    """The minimum surface the boundary property needs: full text + cacheable prefix."""

    text: str
    cacheable_prefix: str


def _correct(static: str, dynamic: str) -> _Assembled:
    prompt = assemble_prompt(static, dynamic)
    return _Assembled(text=prompt.text, cacheable_prefix=prompt.cacheable_prefix)


def _reversed_order(static: str, dynamic: str) -> _Assembled:
    """A deliberately WRONG assembler: the dynamic tail leads, so no prefix is reusable."""
    return _Assembled(
        text=dynamic + SECTION_SEPARATOR + static,
        cacheable_prefix=dynamic + SECTION_SEPARATOR,
    )


def _cache_prefix_is_reusable(assembler) -> bool:
    """True when two requests against the same store share the whole static prefix.

    This is the operative definition of "cacheable": the bytes the two requests have in
    common, counted from byte zero, must cover the entire static store context — and both
    requests must claim the same cacheable prefix. (The shared run may be *longer* than
    that prefix when two tails happen to start alike; that is a coincidence, not a
    contract, so the check is `startswith`.)
    """
    first = assembler(STORE_CONTEXT, TAIL_ONE)
    second = assembler(STORE_CONTEXT, TAIL_TWO)
    shared = os.path.commonprefix([first.text, second.text])
    return (
        STORE_CONTEXT in first.cacheable_prefix
        and first.cacheable_prefix == second.cacheable_prefix
        and shared.startswith(first.cacheable_prefix)
    )


def test_the_static_store_context_is_the_shared_prefix_of_every_request() -> None:
    """Acceptance 2, stated as the cache actually works: the prefix is reusable."""
    assert _cache_prefix_is_reusable(_correct) is True


def test_the_shared_run_is_exactly_the_static_half_when_the_tails_differ_at_byte_one() -> None:
    """With tails that have nothing in common, the shared prefix IS the cacheable prefix."""
    first = assemble_prompt(STORE_CONTEXT, "alpha request")
    second = assemble_prompt(STORE_CONTEXT, "beta request")
    shared = os.path.commonprefix([first.text, second.text])
    assert shared == first.cacheable_prefix == second.cacheable_prefix
    assert shared.startswith(STORE_CONTEXT)


def test_the_boundary_check_actually_fails_on_reversed_order() -> None:
    """The discriminator. Same property, dynamic-first assembler: it must NOT hold.

    If this ever passes, the test above is vacuous and proves nothing.
    """
    assert _cache_prefix_is_reusable(_reversed_order) is False


# --------------------------------------------------------------------------------------
# the boundary invariants themselves
# --------------------------------------------------------------------------------------


def test_static_context_precedes_the_dynamic_tail_in_the_assembled_text() -> None:
    prompt = assemble_prompt(STORE_CONTEXT, TAIL_ONE)
    text = prompt.text
    assert text.index(STORE_CONTEXT) < text.index(TAIL_ONE)
    assert text.startswith(STORE_CONTEXT)
    assert text.endswith(TAIL_ONE)


def test_the_cache_boundary_splits_the_text_exactly() -> None:
    prompt = assemble_prompt(STORE_CONTEXT, TAIL_ONE)
    assert prompt.text[: prompt.cache_boundary] == prompt.cacheable_prefix
    assert prompt.text[prompt.cache_boundary :] == prompt.dynamic_tail == TAIL_ONE
    assert prompt.cache_boundary == len(prompt.cacheable_prefix)


def test_nothing_dynamic_leaks_in_front_of_the_boundary() -> None:
    prompt = assemble_prompt(STORE_CONTEXT, TAIL_ONE)
    assert TAIL_ONE not in prompt.cacheable_prefix
    assert "Ethiopia Guji" not in prompt.cacheable_prefix


def test_reusing_a_prompt_for_the_next_turn_keeps_the_prefix_byte_identical() -> None:
    first = assemble_prompt(STORE_CONTEXT, TAIL_ONE)
    second = first.with_dynamic(TAIL_TWO)
    assert second.cacheable_prefix == first.cacheable_prefix
    assert second.prefix_digest == first.prefix_digest
    assert second.dynamic_tail == TAIL_TWO


def test_a_different_store_gets_a_different_prefix_digest() -> None:
    """The digest has to discriminate, or asserting on it would mean nothing."""
    mine = assemble_prompt(STORE_CONTEXT, TAIL_ONE)
    other = assemble_prompt(STORE_CONTEXT.replace("store-1", "store-2"), TAIL_ONE)
    assert other.prefix_digest != mine.prefix_digest


def test_sections_are_joined_in_the_order_given() -> None:
    prompt = assemble_prompt(["POLICY", "CATALOG"], ["HISTORY", "QUESTION"])
    assert prompt.text == "POLICY\n\nCATALOG\n\nHISTORY\n\nQUESTION"
    assert prompt.cacheable_prefix == "POLICY\n\nCATALOG\n\n"


# --------------------------------------------------------------------------------------
# the wire form: cache_control marks the boundary the API is told about
# --------------------------------------------------------------------------------------


def test_cache_control_sits_on_the_last_static_block_and_nothing_dynamic_precedes_it() -> None:
    prompt = assemble_prompt(STORE_CONTEXT, TAIL_ONE)
    blocks = prompt.to_system_blocks()
    assert blocks[-1]["cache_control"] == CACHE_CONTROL
    assert all(TAIL_ONE not in block["text"] for block in blocks)
    assert STORE_CONTEXT in blocks[-1]["text"]

    messages = prompt.to_messages()
    assert len(messages) == 1 and messages[0]["role"] == "user"
    user_text = messages[0]["content"][0]["text"]
    assert user_text == TAIL_ONE
    assert STORE_CONTEXT not in user_text, (
        "the static context must not ride along in the user turn: it would sit AFTER the "
        "cache breakpoint and be re-sent uncached on every request"
    )


def test_cache_control_can_be_turned_off_for_a_one_shot_call() -> None:
    blocks = assemble_prompt(STORE_CONTEXT, TAIL_ONE).to_system_blocks(cache=False)
    assert "cache_control" not in blocks[0]


def test_mutating_a_returned_block_cannot_corrupt_the_shared_constant() -> None:
    blocks = assemble_prompt(STORE_CONTEXT, TAIL_ONE).to_system_blocks()
    blocks[0]["cache_control"]["type"] = "tampered"
    assert CACHE_CONTROL == {"type": "ephemeral"}


# --------------------------------------------------------------------------------------
# failure modes
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("empty", ["", "   ", [], [""]])
def test_an_empty_static_context_is_refused(empty) -> None:
    """No static context means no cacheable prefix — the exact bug this module prevents."""
    with pytest.raises(PromptAssemblyError):
        assemble_prompt(empty, TAIL_ONE)


def test_a_prompt_with_no_dynamic_tail_has_no_user_turn_to_send() -> None:
    prompt = assemble_prompt(STORE_CONTEXT)
    assert prompt.text == STORE_CONTEXT
    assert prompt.cache_boundary == len(STORE_CONTEXT)
    with pytest.raises(PromptAssemblyError):
        prompt.to_messages()


def test_cached_prompt_is_frozen_and_stringifies_to_its_text() -> None:
    prompt = assemble_prompt(STORE_CONTEXT, TAIL_ONE)
    assert str(prompt) == prompt.text
    with pytest.raises(Exception):
        prompt.static_context = "rewritten"  # type: ignore[misc]
    assert isinstance(prompt, CachedPrompt)
