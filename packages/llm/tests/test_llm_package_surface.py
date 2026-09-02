"""The public import surface: one class per name, whatever path a consumer guesses.

Five tickets import this package. The flat ``src/`` layout (D42) plus the ``.pkgroot``
symlink means the same file is reachable under more than one module name, and Python will
happily execute it twice — handing two tickets two *different* ``RecordedLLM`` classes and
two different ``UnrecordedPromptError`` types, so one ticket's ``except`` clause silently
misses the other's raise. These tests pin that down.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys

import llm as flat
import packages
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


def test_every_submodule_in_src_is_bound_not_just_the_ones_someone_listed() -> None:
    """Discovered with pkgutil, so a module added to src/ later cannot miss the binding.

    A hard-coded list here would agree with a hard-coded list in the package and neither
    would notice a new file.
    """
    names = [info.name for info in pkgutil.iter_modules(flat.__path__)]
    assert len(names) >= 6, names
    for name in names:
        canonical = importlib.import_module(f"llm.{name}")
        for dotted in (f"packages.llm.{name}", f"packages.llm.src.{name}"):
            assert sys.modules.get(dotted) is canonical, dotted
        assert getattr(public, name) is canonical, name
        assert getattr(public.src, name) is canonical, name


def test_a_plain_import_followed_by_attribute_access_works() -> None:
    """`import x.y` then `x.y.Z` needs the ATTRIBUTE, which a sys.modules entry alone
    does not create. This used to raise AttributeError."""
    import packages.llm.doubles  # noqa: F401 - the attribute access below is the test

    assert packages.llm.doubles.RecordedLLM is public.RecordedLLM
    assert packages.llm.src.doubles.RecordedLLM is public.RecordedLLM


def test_the_module_object_and_the_sys_modules_entry_are_the_same_object() -> None:
    """Catches a test elsewhere in this package leaving a re-imported duplicate behind.

    File order puts this after test_llm_client_offline.py, which drops the package from
    sys.modules and re-imports it; when its cleanup was incomplete, the two disagreed for
    the rest of the session and nothing noticed.
    """
    assert sys.modules["packages.llm"] is packages.llm is public
    assert sys.modules["llm"] is flat
    assert public.RecordedLLM is flat.RecordedLLM


def test_an_error_raised_through_one_path_is_caught_through_another() -> None:
    from llm.errors import UnrecordedPromptError as ViaFlat
    from packages.llm import RecordedLLM as ViaPackages

    try:
        ViaPackages({"p": "r"}).complete("unrecorded")
    except ViaFlat as error:
        assert error.prompt == "unrecorded"
    else:  # pragma: no cover - the raise is asserted elsewhere too
        raise AssertionError("the double did not raise")
