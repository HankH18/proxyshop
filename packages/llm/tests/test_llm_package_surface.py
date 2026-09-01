"""The public import surface: one class per name, whatever path a consumer guesses.

Five tickets import this package. The flat ``src/`` layout (D42) plus the ``.pkgroot``
symlink means the same file is reachable under more than one module name, and Python will
happily execute it twice — handing two tickets two *different* ``RecordedLLM`` classes and
two different ``UnrecordedPromptError`` types, so one ticket's ``except`` clause silently
misses the other's raise. These tests pin that down.
"""

from __future__ import annotations

import importlib

import packages.llm as public

CANONICAL = {
    "RecordedLLM": "llm.doubles",
    "DeterministicLLM": "llm.doubles",
    "CachedPrompt": "llm.prompting",
    "UnrecordedPromptError": "llm.errors",
    "AnthropicLLM": "llm.client",
}


def test_the_documented_entry_point_imports() -> None:
    """Exactly what the frozen acceptance suite and every dependent ticket writes."""
    from packages.llm import RecordedLLM, resolve_model

    assert callable(resolve_model)
    assert RecordedLLM({"p": "r"}).complete("p") == "r"


def test_every_exported_name_is_present() -> None:
    assert public.__all__
    missing = [name for name in public.__all__ if not hasattr(public, name)]
    assert missing == [], missing


def test_the_flat_namespace_and_the_packages_namespace_share_one_class() -> None:
    for name, module_name in CANONICAL.items():
        module = importlib.import_module(module_name)
        assert getattr(public, name) is getattr(module, name), name


def test_guessing_a_submodule_path_cannot_produce_a_second_copy() -> None:
    """`packages.llm.src.doubles` is a path a dependent ticket may plausibly guess."""
    for dotted in ("packages.llm.doubles", "packages.llm.src.doubles", "llm.doubles"):
        module = importlib.import_module(dotted)
        assert module.RecordedLLM is public.RecordedLLM, dotted
        assert module.UnrecordedPromptError is public.UnrecordedPromptError, dotted


def test_an_error_raised_through_one_path_is_caught_through_another() -> None:
    from llm.errors import UnrecordedPromptError as ViaFlat
    from packages.llm import RecordedLLM as ViaPackages

    try:
        ViaPackages({"p": "r"}).complete("unrecorded")
    except ViaFlat as error:
        assert error.prompt == "unrecorded"
    else:  # pragma: no cover - the raise is asserted elsewhere too
        raise AssertionError("the double did not raise")
