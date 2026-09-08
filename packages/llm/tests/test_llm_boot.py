"""The one line that tells an operator which voice a container actually has (D55, D20).

The defect this module answers is not that the double is the default — that is D20 and it is
right. It is that the default is INVISIBLE: a deployment configured for a live model, running
an image with no SDK in it, answers 200 with a deterministic template and reports healthy.
These tests pin the three ways that happens, that each one is reported with its own repair,
and that the key is never in the line.
"""

from __future__ import annotations

import logging

import pytest

from llm import boot
from llm.boot import (
    STATUS_LIVE,
    STATUS_TEMPLATED,
    describe_llm_runtime,
    forget_llm_runtime_reports,
    log_llm_runtime,
    sdk_installed,
)
from llm.config import (
    API_KEY_ENV_VAR,
    DEFAULT_INTERVIEW_MODEL,
    DEFAULT_STORE_AGENT_MODEL,
    PROVIDER_ANTHROPIC,
    PROVIDER_ENV_VAR,
    ROLE_BUYER,
    ROLE_STORE_AGENT,
)
from llm.errors import UnknownRoleError

#: A key-shaped string that is not a key. Never printed by anything under test — the point of
#: the assertions below is that it does not reach the line.
FAKE_KEY = "sk-ant-not-a-real-key-0000000000"

LIVE_ENV = {PROVIDER_ENV_VAR: PROVIDER_ANTHROPIC, API_KEY_ENV_VAR: FAKE_KEY}


@pytest.fixture(autouse=True)
def _forget() -> None:
    """`log_llm_runtime` suppresses a repeated line for the life of the process."""
    forget_llm_runtime_reports()


# =====================================================================================
# describe_llm_runtime — the three ways a container ends up on templates
# =====================================================================================


def test_the_default_deployment_is_reported_as_templated_and_says_how_to_change_it() -> None:
    state = describe_llm_runtime(ROLE_STORE_AGENT, env={})

    assert state.live is False
    assert state.status == STATUS_TEMPLATED
    assert state.provider == "double"
    assert state.model == DEFAULT_STORE_AGENT_MODEL
    assert state.api_key_present is False
    assert state.reason is not None
    # The repair is IN the line, because an operator reading it is not holding D20 in their head.
    assert f"{PROVIDER_ENV_VAR}={PROVIDER_ANTHROPIC}" in state.reason
    assert API_KEY_ENV_VAR in state.reason


def test_a_missing_sdk_is_reported_as_the_reason_and_names_the_pip_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect that made this module necessary: configured for a model, cannot reach one.

    `AnthropicLLM.__init__` does no I/O and imports no SDK (D20), so this state is completely
    silent until the first `complete()` — at which point the caller's `except` answers with a
    template. Reported here instead.

    The probe is stubbed rather than skipped-around: the dev venv installs `anthropic`, so a
    test that only ran where it does not would be a test that ran nowhere, for the one state
    that was true of EVERY shipped image.
    """
    monkeypatch.setattr(boot, "sdk_installed", lambda *_a, **_k: False)
    state = boot.describe_llm_runtime(ROLE_STORE_AGENT, env=LIVE_ENV)

    assert state.sdk_installed is False
    assert state.live is False
    assert state.reason is not None
    assert "NOT installed" in state.reason
    assert "pip layer" in state.reason
    assert "sdk=MISSING" in state.line()


def test_a_selected_provider_with_no_key_is_reported_as_the_reason() -> None:
    state = describe_llm_runtime(ROLE_BUYER, env={PROVIDER_ENV_VAR: PROVIDER_ANTHROPIC})

    assert state.live is False
    assert state.api_key_present is False
    assert state.reason is not None
    assert API_KEY_ENV_VAR in state.reason


@pytest.mark.parametrize(
    ("installed", "env", "expected_live"),
    [
        (True, LIVE_ENV, True),
        (False, LIVE_ENV, False),
        (True, {PROVIDER_ENV_VAR: PROVIDER_ANTHROPIC}, False),
        (True, {API_KEY_ENV_VAR: FAKE_KEY}, False),
        (False, {}, False),
    ],
)
def test_provider_plus_sdk_plus_key_is_the_only_state_reported_live(
    monkeypatch: pytest.MonkeyPatch,
    installed: bool,
    env: dict[str, str],
    expected_live: bool,
) -> None:
    """All three, or it is not live. The point of the module is that two of three is silent."""
    monkeypatch.setattr(boot, "sdk_installed", lambda *_a, **_k: installed)
    state = boot.describe_llm_runtime(ROLE_STORE_AGENT, env=env)

    assert state.live is expected_live
    assert state.status == (STATUS_LIVE if expected_live else STATUS_TEMPLATED)
    if expected_live:
        assert state.reason is None
        assert "a real model writes" in state.line()
    else:
        assert state.reason


def test_the_reported_sdk_flag_is_the_real_probe_and_not_a_constant() -> None:
    """The parametrised case above stubs the probe; this one pins that the unstubbed
    function is what `describe_llm_runtime` actually consults."""
    assert describe_llm_runtime(ROLE_STORE_AGENT, env=LIVE_ENV).sdk_installed is sdk_installed()


def test_an_unimplemented_provider_is_reported_rather_than_swallowed() -> None:
    """`resolve_provider` refuses a typo instead of falling back, which is right — and would
    be the one deployment mistake with no line anywhere if this function let it raise."""
    state = describe_llm_runtime(ROLE_BUYER, env={PROVIDER_ENV_VAR: "antropic"})

    assert state.live is False
    assert state.provider == "antropic"
    assert state.reason is not None
    assert "antropic" in state.reason


def test_an_unknown_role_raises_rather_than_being_reported_as_templated() -> None:
    """A typo'd role is a caller bug. Rendering it as a plausible "templated" line would hide
    it behind exactly the sentence an operator is trained to accept."""
    with pytest.raises(UnknownRoleError):
        describe_llm_runtime("store-agent", env=LIVE_ENV)


def test_the_configured_model_is_reported_even_on_the_double_path() -> None:
    """A wrong model id is worth seeing BEFORE the provider is switched, not after.

    The override is spelled from :data:`llm.config.DEFAULT_INTERVIEW_MODEL` rather than as a
    literal because `test_no_model_id_is_written_down_outside_a_module_level_default_table`
    AST-scans this tree for `claude-…` constants — C4 makes model ids config, not code, and
    that scan does not exempt tests.
    """
    state = describe_llm_runtime(ROLE_BUYER, env={"BUYER_MODEL": DEFAULT_INTERVIEW_MODEL})

    assert state.model == DEFAULT_INTERVIEW_MODEL
    assert state.model != DEFAULT_STORE_AGENT_MODEL, "otherwise the override proves nothing"
    assert state.live is False


# =====================================================================================
# The line itself
# =====================================================================================


def test_the_api_key_is_never_in_the_line_only_whether_there_is_one() -> None:
    state = describe_llm_runtime(ROLE_STORE_AGENT, env=LIVE_ENV)
    line = state.line()

    assert FAKE_KEY not in line
    assert FAKE_KEY[:12] not in line
    assert "api_key=present" in line


def test_the_line_is_greppable_and_names_every_input_to_the_decision() -> None:
    line = describe_llm_runtime(ROLE_STORE_AGENT, env={}).line()

    assert line.startswith(f"llm[{ROLE_STORE_AGENT}] {STATUS_TEMPLATED}:")
    for field in ("provider=", "model=", "sdk=", "api_key="):
        assert field in line, field
    assert "\n" not in line, "one line, so one grep finds it in a container log"


def test_a_missing_sdk_is_shouted_in_the_line_not_spelled_like_the_healthy_case() -> None:
    """`sdk=installed` and `sdk=MISSING` differ by more than a word, on purpose: this is the
    field that was false in every image and it must not scan as normal."""
    state = describe_llm_runtime(ROLE_STORE_AGENT, env=LIVE_ENV)
    expected = "sdk=installed" if state.sdk_installed else "sdk=MISSING"

    assert expected in state.line()


# =====================================================================================
# log_llm_runtime — level carries the meaning, and it does not repeat
# =====================================================================================


def test_a_templated_process_logs_a_warning_and_a_live_one_logs_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A deployment that keeps only WARNING still sees the state that costs it the product."""
    with caplog.at_level(logging.INFO, logger="llm.boot"):
        state = log_llm_runtime(ROLE_STORE_AGENT, env={})

    assert state.live is False
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert caplog.records[0].getMessage() == state.line()


def test_the_same_line_is_not_repeated_for_the_life_of_the_process(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The honest call sites are per-request seams — the buyer rebuilds its double per render
    so the transcript is collected — so an unguarded line is one WARNING per shortlist."""
    with caplog.at_level(logging.INFO, logger="llm.boot"):
        log_llm_runtime(ROLE_STORE_AGENT, env={})
        log_llm_runtime(ROLE_STORE_AGENT, env={})
        log_llm_runtime(ROLE_STORE_AGENT, env={})

    assert len(caplog.records) == 1


def test_a_state_change_still_speaks_because_the_guard_is_keyed_on_the_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keying the guard on the role instead would make a reconfigured process permanently mute
    about the reconfiguration — the exact question the line exists to answer."""
    with caplog.at_level(logging.INFO, logger="llm.boot"):
        log_llm_runtime(ROLE_STORE_AGENT, env={})
        log_llm_runtime(ROLE_STORE_AGENT, env={PROVIDER_ENV_VAR: PROVIDER_ANTHROPIC})

    assert len(caplog.records) == 2
    assert caplog.records[0].getMessage() != caplog.records[1].getMessage()


def test_two_roles_each_get_their_own_line(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="llm.boot"):
        log_llm_runtime(ROLE_STORE_AGENT, env={})
        log_llm_runtime(ROLE_BUYER, env={})

    assert len(caplog.records) == 2


def test_once_false_reports_every_time(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="llm.boot"):
        log_llm_runtime(ROLE_BUYER, env={}, once=False)
        log_llm_runtime(ROLE_BUYER, env={}, once=False)

    assert len(caplog.records) == 2


def test_the_key_never_reaches_a_log_record_either(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="llm.boot"):
        log_llm_runtime(ROLE_STORE_AGENT, env=LIVE_ENV)

    emitted = "\n".join(record.getMessage() for record in caplog.records)
    assert FAKE_KEY not in emitted


# =====================================================================================
# The SDK probe
# =====================================================================================


def test_the_sdk_probe_does_not_import_what_it_is_asked_about() -> None:
    """D20's load-bearing property: five packages import this tree and their suites run with
    no key and no network, so describing the runtime may not execute the SDK."""
    import sys

    sys.modules.pop("anthropic", None)
    sdk_installed()

    assert "anthropic" not in sys.modules


def test_the_sdk_probe_answers_false_for_a_name_that_is_not_there() -> None:
    assert sdk_installed("a_distribution_no_image_has_ever_installed") is False


def test_the_sdk_probe_answers_false_rather_than_raising_on_a_malformed_name() -> None:
    """`find_spec("")` raises `ValueError`. "Cannot tell" is reported as "not installed",
    because a boot line that raised would take down the process it was describing."""
    assert sdk_installed("") is False
