"""D20: nothing constructs an Anthropic client — or imports the SDK — at import time.

This is the decision with the largest blast radius in the package. Five other tickets
import `packages.llm`; their suites run with no API key and no network (D3). A client built
at import time, or even a top-level `import anthropic` that later tries to read a key,
takes every one of those suites down at collection.

So the laziness is asserted three ways here:

1. in a **child process**, with `socket` broken *before* the package is imported at all,
   and `sys.modules` inspected afterwards for `anthropic`;
2. in-process, by dropping the package from `sys.modules` and re-importing it with the
   sockets monkeypatched;
3. by constructing :class:`AnthropicLLM` under the same broken sockets and checking that
   nothing was built until a call was actually made.
"""

from __future__ import annotations

import importlib
import os
import socket
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from packages.llm import (
    AnthropicLLM,
    DeterministicLLM,
    LLMClient,
    MissingApiKeyError,
    ProviderNotConfiguredError,
    RecordedLLM,
    assemble_prompt,
    build_llm,
    response_text,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

CHILD_PROGRAM = textwrap.dedent(
    """
    import socket, sys

    def boom(*args, **kwargs):
        raise RuntimeError("NETWORK TOUCHED: the package reached for a socket on import")

    # Break the network BEFORE the package is imported.
    socket.socket = boom
    socket.create_connection = boom
    socket.getaddrinfo = boom

    import packages.llm
    import llm.client

    assert "anthropic" not in sys.modules, "the anthropic SDK was imported at import time"

    client = packages.llm.AnthropicLLM("buyer")
    assert "anthropic" not in sys.modules, "constructing AnthropicLLM imported the SDK"

    double = packages.llm.RecordedLLM({"p": "r"})
    assert double.complete("p") == "r"

    print("OK", client.model, packages.llm.resolve_model("extract"))
    """
)


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


# --------------------------------------------------------------------------------------
# 1. child process: sockets broken before the first import
# --------------------------------------------------------------------------------------


def test_a_fresh_interpreter_imports_the_package_without_the_sdk_or_a_socket() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), str(REPO_ROOT / ".pkgroot"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    proc = subprocess.run(
        [sys.executable, "-c", CHILD_PROGRAM],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr}"
    assert proc.stdout.startswith("OK "), proc.stdout


# --------------------------------------------------------------------------------------
# 2. in-process: re-import with the sockets already broken
# --------------------------------------------------------------------------------------


def test_reimporting_the_package_with_broken_sockets_still_works(no_network) -> None:
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "llm" or name.startswith("llm.") or name == "packages.llm"
    }
    for name in saved:
        del sys.modules[name]
    try:
        module = importlib.import_module("packages.llm")
        assert module.RecordedLLM({"p": "r"}).complete("p") == "r"
        assert module.resolve_model("buyer")
    finally:
        sys.modules.update(saved)


# --------------------------------------------------------------------------------------
# 3. the live client is built on first use, not on construction
# --------------------------------------------------------------------------------------


def _fake_sdk(recorder: dict) -> types.ModuleType:
    """A stand-in `anthropic` module that records what it was asked to do."""

    class _Messages:
        def create(self, **kwargs):
            recorder["request"] = kwargs
            return {"content": [{"type": "text", "text": "fake reply"}]}

    class _Anthropic:
        def __init__(self, **kwargs):
            recorder["constructor"] = kwargs
            self.messages = _Messages()

    module = types.ModuleType("anthropic")
    module.Anthropic = _Anthropic  # type: ignore[attr-defined]
    return module


def test_constructing_the_live_client_object_touches_nothing(monkeypatch, no_network) -> None:
    monkeypatch.setenv("STORE_AGENT_MODEL", "sentinel-store-model")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = AnthropicLLM("store_agent")
    assert client.model == "sentinel-store-model"
    assert "not built" in repr(client)


def test_a_live_call_without_a_key_fails_legibly_instead_of_hanging(
    monkeypatch, no_network
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError) as excinfo:
        AnthropicLLM("buyer").complete("hello")
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_the_live_call_sends_the_cache_boundary_it_was_given(monkeypatch, no_network) -> None:
    recorder: dict = {}
    monkeypatch.setitem(sys.modules, "anthropic", _fake_sdk(recorder))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("STORE_AGENT_MODEL", "sentinel-store-model")

    prompt = assemble_prompt("STORE CONTEXT\nfloor 18.00", "BUYER\nquote me")
    reply = AnthropicLLM("store_agent").complete(prompt, max_tokens=32)

    assert reply == "fake reply"
    assert recorder["constructor"]["api_key"] == "sk-test-not-a-real-key"
    request = recorder["request"]
    assert request["model"] == "sentinel-store-model"
    assert request["max_tokens"] == 32
    assert request["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "BUYER" not in request["system"][-1]["text"], "the dynamic tail must not be cached"
    assert request["messages"][0]["content"][0]["text"] == "BUYER\nquote me"


def test_an_injected_client_is_used_as_is(no_network) -> None:
    recorder: dict = {}
    fake = _fake_sdk(recorder).Anthropic()  # type: ignore[attr-defined]
    client = AnthropicLLM("buyer", model="sentinel", client=fake)
    assert client.complete("hi", system="be terse") == "fake reply"
    assert recorder["request"]["system"][0]["text"] == "be terse"
    assert recorder["request"]["messages"][0]["content"][0]["text"] == "hi"


def test_a_bare_string_prompt_sends_no_system_block_at_all(no_network) -> None:
    """No static half means nothing to cache; sending an empty system block would be junk."""
    recorder: dict = {}
    fake = _fake_sdk(recorder).Anthropic()  # type: ignore[attr-defined]
    assert AnthropicLLM("buyer", model="sentinel", client=fake).complete("just this") == (
        "fake reply"
    )
    assert "system" not in recorder["request"]
    assert recorder["request"]["messages"][0]["content"][0]["text"] == "just this"


def test_response_text_reads_objects_and_dicts_and_ignores_non_text_blocks() -> None:
    class _Block:
        def __init__(self, type_: str, text: str) -> None:
            self.type = type_
            self.text = text

    class _Response:
        content = [_Block("text", "one "), _Block("tool_use", "ignored"), _Block("text", "two")]

    assert response_text(_Response()) == "one two"
    assert response_text({"content": [{"type": "text", "text": "hi"}]}) == "hi"
    assert response_text({}) == ""


# --------------------------------------------------------------------------------------
# provider dispatch (D20)
# --------------------------------------------------------------------------------------


def test_build_llm_defaults_to_the_double(monkeypatch, no_network) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    client = build_llm("buyer")
    assert isinstance(client, DeterministicLLM)
    assert isinstance(client, LLMClient)
    assert client.complete("anything").startswith("double:buyer:")


def test_build_llm_uses_recordings_when_it_is_given_them(monkeypatch, no_network) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    client = build_llm("extract", recordings={"p": "r"})
    assert isinstance(client, RecordedLLM)
    assert client.complete("p") == "r"


def test_build_llm_only_reaches_for_the_live_client_on_an_explicit_opt_in(
    monkeypatch, no_network
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    client = build_llm("interview")
    assert isinstance(client, AnthropicLLM)
    assert "not built" in repr(client), "still lazy: selecting the provider builds nothing"

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(ProviderNotConfiguredError):
        build_llm("interview")
