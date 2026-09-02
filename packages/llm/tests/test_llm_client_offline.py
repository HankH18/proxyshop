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
    MAX_TOKENS_ENV_VAR,
    RESERVED_REQUEST_FIELDS,
    AnthropicLLM,
    DeterministicLLM,
    EmptyReplyError,
    MissingApiKeyError,
    ModelOverrideError,
    ProviderNotConfiguredError,
    RecordedLLM,
    TruncatedReplyError,
    UnknownRoleError,
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
    """Re-import from scratch under blocked sockets — and leave NOTHING behind.

    The cleanup is as load-bearing as the assertion. `importlib.import_module` rebinds the
    `llm` **attribute** on the `packages` namespace package as well as filling
    `sys.modules`, and restoring only `sys.modules` left the two disagreeing for the rest
    of the session: measured, `sys.modules["packages.llm"].RecordedLLM` was no longer
    `packages.llm.RecordedLLM`, so a later module's `except UnrecordedPromptError` could
    silently miss. A whole-repo run is one session, and five tickets import this package.
    """
    import packages

    saved = {
        name: module
        for name, module in sys.modules.items()
        if name.startswith("llm") or name.startswith("packages.llm")
    }
    saved_attr = getattr(packages, "llm", None)
    for name in saved:
        del sys.modules[name]
    try:
        module = importlib.import_module("packages.llm")
        assert module.RecordedLLM({"p": "r"}).complete("p") == "r"
        assert module.resolve_model("buyer")
    finally:
        for name in [
            n
            for n in sys.modules
            if n.startswith("packages.llm") or n == "llm" or n.startswith("llm.")
        ]:
            del sys.modules[name]
        sys.modules.update(saved)
        if saved_attr is not None:
            packages.llm = saved_attr

    assert sys.modules["packages.llm"] is packages.llm
    assert packages.llm.RecordedLLM is RecordedLLM, "the re-import poisoned the session"


# --------------------------------------------------------------------------------------
# 3. the live client is built on first use, not on construction
# --------------------------------------------------------------------------------------


def _fake_sdk(
    recorder: dict, *, stop_reason: str = "end_turn", text: str = "fake reply"
) -> types.ModuleType:
    """A stand-in `anthropic` module that records what it was asked to do."""

    class _Messages:
        def create(self, **kwargs):
            recorder["request"] = kwargs
            content = [{"type": "text", "text": text}] if text else []
            return {"content": content, "stop_reason": stop_reason}

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


# --------------------------------------------------------------------------------------
# what the wrapper owns, and what a 200 with no usable reply does
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["model", "messages"])
def test_a_per_call_keyword_cannot_overwrite_a_field_the_wrapper_owns(no_network, field) -> None:
    """C4 again: `complete(prompt, model=...)` walks past config AND the frozen AST scan.

    The scan can only see string literals inside this package; a model id a caller passes
    in is invisible to it, so the guard has to be here.
    """
    recorder: dict = {}
    fake = _fake_sdk(recorder).Anthropic()  # type: ignore[attr-defined]
    client = AnthropicLLM("buyer", model="env-resolved", client=fake)
    with pytest.raises(ModelOverrideError) as excinfo:
        client.complete("hi", **{field: "smuggled"})
    assert field in str(excinfo.value)
    assert recorder == {"constructor": {}} or "request" not in recorder, (
        "the request must not have been sent at all"
    )
    assert client.complete("hi") == "fake reply"
    assert recorder["request"]["model"] == "env-resolved"


def test_system_cannot_reach_the_request_as_a_raw_keyword(no_network) -> None:
    """`system` is a named parameter, so it is composed — never passed through blind."""
    recorder: dict = {}
    fake = _fake_sdk(recorder).Anthropic()  # type: ignore[attr-defined]
    AnthropicLLM("buyer", model="m", client=fake).complete("hi", system="be terse")
    assert recorder["request"]["system"] == [
        {"type": "text", "text": "be terse", "cache_control": {"type": "ephemeral"}}
    ]
    assert "system" in RESERVED_REQUEST_FIELDS


def test_a_reply_cut_off_at_max_tokens_raises_instead_of_returning_a_fragment(
    no_network,
) -> None:
    """DEFAULT_MAX_TOKENS against extraction-shaped work truncates JSON mid-document.

    Returning the fragment gives the consumer a JSONDecodeError at a column that names
    nothing; this names max_tokens, the env var, and hands back the partial text.
    """
    recorder: dict = {}
    fake = _fake_sdk(recorder, stop_reason="max_tokens", text='{"claims": [{"key": "roa')
    monkey = fake.Anthropic()  # type: ignore[attr-defined]
    with pytest.raises(TruncatedReplyError) as excinfo:
        AnthropicLLM("extract", model="m", client=monkey).complete("pitch", max_tokens=16)
    message = str(excinfo.value)
    assert "max_tokens=16" in message
    assert MAX_TOKENS_ENV_VAR in message
    assert excinfo.value.partial == '{"claims": [{"key": "roa'


@pytest.mark.parametrize("stop_reason", ["refusal", "tool_use"])
def test_a_200_with_no_text_block_raises_instead_of_returning_an_empty_string(
    no_network, stop_reason
) -> None:
    """A store agent answering a buyer with "" and no exception is the failure here.

    The store-agent fixtures advertise three provenance tools, so `tool_use` is reachable
    and this wrapper deliberately runs no tool loop.
    """
    recorder: dict = {}
    fake = _fake_sdk(recorder, stop_reason=stop_reason, text="")
    with pytest.raises(EmptyReplyError) as excinfo:
        AnthropicLLM("store_agent", model="m", client=fake.Anthropic()).complete("hi")  # type: ignore[attr-defined]
    assert excinfo.value.stop_reason == stop_reason
    assert stop_reason in str(excinfo.value)


def test_a_normal_reply_is_unaffected_by_the_stop_reason_checks(no_network) -> None:
    recorder: dict = {}
    fake = _fake_sdk(recorder, stop_reason="end_turn", text="a real answer")
    assert AnthropicLLM("buyer", model="m", client=fake.Anthropic()).complete("hi") == (  # type: ignore[attr-defined]
        "a real answer"
    )


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
    assert client.complete("anything").startswith("double:buyer:")


@pytest.mark.parametrize("bad_role", ["store-agent", "storeagent", "Buyer", ""])
def test_build_llm_validates_the_role_on_the_OFFLINE_path_too(
    monkeypatch, no_network, bad_role
) -> None:
    """`store-agent` is the directory name, and it used to work offline.

    Validation lived only where the live client resolved a model, so the typo raised
    exclusively under LLM_PROVIDER=anthropic — and every test in this repo runs offline
    (D3), which means the headline feature was never exercised through the seam consumers
    actually use.
    """
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    with pytest.raises(UnknownRoleError) as excinfo:
        build_llm(bad_role)
    assert "store_agent" in str(excinfo.value)

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    with pytest.raises(UnknownRoleError):
        build_llm(bad_role)


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


# --------------------------------------------------------------------------------------
# the cache policy is stated by the caller, not inferred from another argument's type
# --------------------------------------------------------------------------------------


def test_the_string_path_documents_system_as_the_static_context_and_caches_it(
    no_network,
) -> None:
    """On the string path there is no other static half, so `system` IS the static context.

    That is why it carries the breakpoint by default — and why the default is wrong for a
    system string that varies per call, which `cache_system=False` is for.
    """
    recorder: dict = {}
    fake = _fake_sdk(recorder).Anthropic()  # type: ignore[attr-defined]
    AnthropicLLM("buyer", model="m", client=fake).complete("hi", system="STORE POLICY v3")
    assert recorder["request"]["system"] == [
        {"type": "text", "text": "STORE POLICY v3", "cache_control": {"type": "ephemeral"}}
    ]


def test_cache_system_false_stops_a_per_call_system_writing_an_unreadable_cache_entry(
    no_network,
) -> None:
    """W1-12: the same `system=` keyword meant "cache this" or "do not" depending on the
    runtime type of a DIFFERENT argument — a CachedPrompt prompt made it uncached, a
    string prompt made the identical text cached. A system string that varies per call
    then wrote a fresh cache entry on every turn that no later turn could ever read.

    The policy is now the caller's to state.
    """
    recorder: dict = {}
    fake = _fake_sdk(recorder).Anthropic()  # type: ignore[attr-defined]
    client = AnthropicLLM("buyer", model="m", client=fake)

    for turn in range(1, 4):
        client.complete("hi", system=f"turn {turn} of 9", cache_system=False)
        assert recorder["request"]["system"] == [{"type": "text", "text": f"turn {turn} of 9"}]
        assert "cache_control" not in recorder["request"]["system"][0]


def test_cache_system_false_also_lifts_the_breakpoint_off_a_cached_prompt(no_network) -> None:
    """Same meaning on both paths: it is the breakpoint on the STATIC block."""
    recorder: dict = {}
    fake = _fake_sdk(recorder).Anthropic()  # type: ignore[attr-defined]
    prompt = assemble_prompt("STORE CONTEXT", "quote me")
    client = AnthropicLLM("store_agent", model="m", client=fake)

    client.complete(prompt)
    assert recorder["request"]["system"][0]["cache_control"] == {"type": "ephemeral"}

    client.complete(prompt, cache_system=False)
    assert recorder["request"]["system"] == [{"type": "text", "text": "STORE CONTEXT"}]


def test_cache_system_is_a_named_parameter_and_never_reaches_the_request(no_network) -> None:
    recorder: dict = {}
    fake = _fake_sdk(recorder).Anthropic()  # type: ignore[attr-defined]
    AnthropicLLM("buyer", model="m", client=fake).complete("hi", system="s", cache_system=False)
    assert "cache_system" not in recorder["request"]


# --------------------------------------------------------------------------------------
# the reserved-field guard: which entries can actually fire it (EXTRA-1)
# --------------------------------------------------------------------------------------

#: The reserved fields a caller CAN smuggle in through `**kwargs`. `system` cannot be one
#: of them — `complete` binds it as a named keyword-only parameter — so parametrizing the
#: guard test over the whole constant would silently include a case that proves nothing.
REACHABLE_RESERVED_FIELDS = sorted(RESERVED_REQUEST_FIELDS - {"system"})


@pytest.mark.parametrize("field", REACHABLE_RESERVED_FIELDS)
def test_the_reserved_field_guard_is_live_for_every_field_that_can_reach_kwargs(
    no_network, field
) -> None:
    """Deleting the guard must turn this red — which `"system" in RESERVED_REQUEST_FIELDS`
    could never do, because that is true by construction and stays true with no guard at
    all.

    Parametrized over the reachable fields rather than a hard-coded pair, so a field added
    to the constant is covered here the moment it is added.
    """
    recorder: dict = {}
    fake = _fake_sdk(recorder).Anthropic()  # type: ignore[attr-defined]
    client = AnthropicLLM("buyer", model="env-resolved", client=fake)

    with pytest.raises(ModelOverrideError) as excinfo:
        client.complete("hi", **{field: "smuggled"})
    assert field in str(excinfo.value)
    assert "request" not in recorder, "the request must not have been sent at all"


def test_system_is_listed_in_the_reserved_fields_for_documentation_only(no_network) -> None:
    """`system` is in the constant and the guard can NEVER fire for it.

    `complete` declares `system` as a keyword-only NAMED parameter, so it is bound there
    and cannot land in `**kwargs`. The constant entry is documentation; the signature is
    the enforcement. Asserting `"system" in RESERVED_REQUEST_FIELDS` proves nothing about
    the guard, so this asserts the mechanism that actually excludes it — and that the
    keyword is composed rather than passed through blind.
    """
    import inspect

    parameter = inspect.signature(AnthropicLLM.complete).parameters["system"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
        "if `system` ever stops being a named parameter, the guard becomes load-bearing "
        "and this test should be replaced by a ModelOverrideError one"
    )

    recorder: dict = {}
    fake = _fake_sdk(recorder).Anthropic()  # type: ignore[attr-defined]
    AnthropicLLM("buyer", model="m", client=fake).complete("hi", system="be terse")
    assert recorder["request"]["system"][0]["text"] == "be terse"


# --------------------------------------------------------------------------------------
# build_llm refuses a keyword the selected provider would throw away
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"model": "smuggled-model"},
        {"api_key": "sk-not-real"},
        {"timeout": 5},
        {"max_tokens": 32},
        {"utter_nonsense": object()},
        {"model": "smuggled-model", "timeout": 5},
    ],
)
def test_build_llm_refuses_kwargs_the_double_would_silently_discard(
    monkeypatch, no_network, kwargs
) -> None:
    """`build_llm(role, model="…")` under D20's default read as a configured client and
    was a double that had thrown the model away — including outright nonsense."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    with pytest.raises(TypeError) as excinfo:
        build_llm("buyer", **kwargs)
    for name in kwargs:
        assert name in str(excinfo.value), "the error must name the offending keyword"


def test_build_llm_still_forwards_those_kwargs_on_the_path_that_uses_them(
    monkeypatch, no_network
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    client = build_llm("buyer", model="explicit-model", timeout=7.5)
    assert isinstance(client, AnthropicLLM)
    assert client.model == "explicit-model"
    assert client.timeout == 7.5


@pytest.mark.parametrize("case", [str.lower, str.upper, str.title, "  {}  ".format])
@pytest.mark.parametrize(
    ("provider", "expected"), [("anthropic", AnthropicLLM), ("double", DeterministicLLM)]
)
def test_build_llm_normalizes_the_provider_keyword_the_way_the_env_var_is_normalized(
    monkeypatch, no_network, case, provider, expected
) -> None:
    """`resolve_provider` strips and lower-cases `LLM_PROVIDER`; the keyword did not.

    So `LLM_PROVIDER=Anthropic` selected the live client while `provider="Anthropic"`
    raised ProviderNotConfiguredError — the same spelling, two different answers. Every
    spelling the env var accepts, the keyword must accept too.
    """
    spelling = case(provider)

    monkeypatch.setenv("LLM_PROVIDER", spelling)
    assert isinstance(build_llm("buyer"), expected), f"LLM_PROVIDER={spelling!r}"

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert isinstance(build_llm("buyer", provider=spelling), expected), f"provider={spelling!r}"


def test_build_llm_refuses_recordings_it_would_have_to_discard(monkeypatch, no_network) -> None:
    """`recordings=` with a live provider read as offline replay and was a live client."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    with pytest.raises(TypeError) as excinfo:
        build_llm("buyer", provider="anthropic", recordings={"p": "r"})
    assert "recordings" in str(excinfo.value)

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    with pytest.raises(TypeError):
        build_llm("buyer", recordings={"p": "r"})

    # ...and it is still accepted where it is honoured.
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert isinstance(build_llm("buyer", recordings={"p": "r"}), RecordedLLM)


def test_build_llm_reports_an_unknown_provider_before_it_complains_about_keywords(
    no_network,
) -> None:
    with pytest.raises(ProviderNotConfiguredError):
        build_llm("buyer", provider="openai", model="x", recordings={"p": "r"})


def test_every_client_build_llm_can_return_exposes_model(monkeypatch, no_network) -> None:
    """A consumer logging `client.model` must not work live and AttributeError offline."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert build_llm("buyer").model == "double:buyer"
    assert build_llm("buyer", recordings={"p": "r"}).model == "double:buyer"
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("BUYER_MODEL", "sentinel-buyer-model")
    assert build_llm("buyer").model == "sentinel-buyer-model"
