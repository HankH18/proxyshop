"""T-014 acceptance 1: role -> model resolution from env, with documented defaults (C4).

The frozen acceptance suite asserts the same contract, but it is not run by this ticket's
verify command (`pytest packages/llm -q`) — so the contract is re-asserted here, including
an independent implementation of the "no model id outside a DEFAULT table" AST scan. If
this file and the frozen suite ever disagree, the frozen suite is right.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from packages.llm import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_BY_ROLE,
    DEFAULT_PROVIDER,
    KNOWN_ROLES,
    PROVIDER_ANTHROPIC,
    PROVIDER_DOUBLE,
    PROVIDER_ENV_VAR,
    ROLE_ENV_VARS,
    ProviderNotConfiguredError,
    UnknownRoleError,
    default_model,
    model_env_var,
    resolve_api_key,
    resolve_max_tokens,
    resolve_model,
    resolve_provider,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
LLM_ROOT = REPO_ROOT / "packages" / "llm"

#: The prefix every Anthropic model id starts with. It lives in a module-level ``DEFAULT``
#: constant for the same reason the real ids do: the acceptance suite's AST scan forbids a
#: ``"claude-…"`` literal anywhere else under ``packages/llm``, and this test file is
#: scanned too.
DEFAULT_MODEL_ID_PREFIX = "claude-"

#: The exact, frozen role -> env var mapping. Spelled out again rather than imported, so a
#: rename of `ROLE_ENV_VARS` in the source cannot silently redefine the contract.
EXPECTED_ROLE_ENV_VARS = {
    "buyer": "BUYER_MODEL",
    "store_agent": "STORE_AGENT_MODEL",
    "interview": "INTERVIEW_MODEL",
    "extract": "EXTRACT_MODEL",
}


def _env_example() -> dict[str, str]:
    """``.env.example`` parsed into a dict — the file that documents the defaults."""
    path = REPO_ROOT / ".env.example"
    assert path.is_file(), f"{path} is missing; it is what documents the model defaults"
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def test_role_env_var_mapping_is_exactly_the_four_frozen_names() -> None:
    assert dict(ROLE_ENV_VARS) == EXPECTED_ROLE_ENV_VARS
    assert set(KNOWN_ROLES) == set(EXPECTED_ROLE_ENV_VARS)


@pytest.mark.parametrize(("role", "env_var"), sorted(EXPECTED_ROLE_ENV_VARS.items()))
def test_a_set_env_var_is_returned_verbatim(monkeypatch, role: str, env_var: str) -> None:
    sentinel = f"model-sentinel/{role}/with spaces and a slash"
    monkeypatch.setenv(env_var, sentinel)
    assert resolve_model(role) == sentinel


@pytest.mark.parametrize(("role", "env_var"), sorted(EXPECTED_ROLE_ENV_VARS.items()))
def test_an_unset_env_var_falls_back_to_the_documented_default(
    monkeypatch, role: str, env_var: str
) -> None:
    monkeypatch.delenv(env_var, raising=False)
    resolved = resolve_model(role)
    assert resolved == DEFAULT_MODEL_BY_ROLE[role] == default_model(role)
    assert resolved.startswith(DEFAULT_MODEL_ID_PREFIX), resolved


@pytest.mark.parametrize(("role", "env_var"), sorted(EXPECTED_ROLE_ENV_VARS.items()))
def test_a_blank_env_var_is_treated_as_unset(monkeypatch, role: str, env_var: str) -> None:
    """`BUYER_MODEL=` in a .env is a typo, not a request to send an empty model id."""
    monkeypatch.setenv(env_var, "   ")
    assert resolve_model(role) == DEFAULT_MODEL_BY_ROLE[role]


def test_the_documented_defaults_are_the_values_env_example_ships() -> None:
    """The defaults are what actually runs: a worktree has no .env (it is gitignored)."""
    documented = _env_example()
    for role, env_var in EXPECTED_ROLE_ENV_VARS.items():
        assert env_var in documented, f".env.example no longer documents {env_var}"
        assert DEFAULT_MODEL_BY_ROLE[role] == documented[env_var], (
            f"the default for {role!r} drifted from .env.example's {env_var}"
        )


def test_the_four_roles_do_not_all_share_one_model() -> None:
    """DESIGN pins opus-class for the interview and haiku-class for extraction."""
    assert DEFAULT_MODEL_BY_ROLE["interview"] != DEFAULT_MODEL_BY_ROLE["buyer"]
    assert DEFAULT_MODEL_BY_ROLE["extract"] != DEFAULT_MODEL_BY_ROLE["buyer"]


def test_resolve_model_reads_an_injected_mapping_without_touching_the_process_env(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BUYER_MODEL", "from-the-process")
    assert resolve_model("buyer", {"BUYER_MODEL": "from-the-mapping"}) == "from-the-mapping"
    assert resolve_model("buyer", {}) == DEFAULT_MODEL_BY_ROLE["buyer"]
    assert resolve_model("buyer") == "from-the-process"


def test_an_unknown_role_fails_loudly_and_says_which_roles_exist() -> None:
    with pytest.raises(UnknownRoleError) as excinfo:
        resolve_model("storeagent")
    message = str(excinfo.value)
    assert "storeagent" in message
    for role in EXPECTED_ROLE_ENV_VARS:
        assert role in message
    # Catchable as a plain KeyError by callers that do not import our errors.
    assert isinstance(excinfo.value, KeyError)
    with pytest.raises(UnknownRoleError):
        model_env_var("nope")
    with pytest.raises(UnknownRoleError):
        default_model("nope")


def test_provider_defaults_to_the_double_and_rejects_a_typo(monkeypatch) -> None:
    """D20: the offline double is the default; only an explicit opt-in selects live."""
    monkeypatch.delenv(PROVIDER_ENV_VAR, raising=False)
    assert resolve_provider() == DEFAULT_PROVIDER == PROVIDER_DOUBLE

    monkeypatch.setenv(PROVIDER_ENV_VAR, "  ANTHROPIC ")
    assert resolve_provider() == PROVIDER_ANTHROPIC

    monkeypatch.setenv(PROVIDER_ENV_VAR, "antropic")
    with pytest.raises(ProviderNotConfiguredError) as excinfo:
        resolve_provider()
    assert "antropic" in str(excinfo.value), "a typo must not silently fall back to a double"


def test_api_key_and_limits_resolve_from_env_with_safe_defaults() -> None:
    assert resolve_api_key({}) is None
    assert resolve_api_key({"ANTHROPIC_API_KEY": "   "}) is None
    assert resolve_api_key({"ANTHROPIC_API_KEY": "sk-test"}) == "sk-test"

    assert resolve_max_tokens({}) == DEFAULT_MAX_TOKENS
    assert resolve_max_tokens({"LLM_MAX_TOKENS": "64"}) == 64
    for bad in ("not-a-number", "0", "-5"):
        with pytest.raises(ProviderNotConfiguredError):
            resolve_max_tokens({"LLM_MAX_TOKENS": bad})


def test_no_model_id_is_written_down_outside_a_module_level_default_table() -> None:
    """C4, re-implemented here so `pytest packages/llm -q` enforces it on its own.

    A model id inline at a call site is the failure this guards: it cannot be changed by
    configuration, so the env vars above would be a lie for that one call.
    """
    offenders: list[str] = []
    scanned = 0
    for path in sorted(LLM_ROOT.rglob("*.py")):
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed: set[int] = set()
        for node in tree.body:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            value = getattr(node, "value", None)
            if value is not None and any("DEFAULT" in name.upper() for name in names):
                allowed.update(id(sub) for sub in ast.walk(value))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith(DEFAULT_MODEL_ID_PREFIX)
                and id(node) not in allowed
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} {node.value}")
    assert scanned >= 5, "the scan found almost no files; the layout moved"
    assert offenders == [], f"hard-coded model ids outside a defaults table: {offenders}"
