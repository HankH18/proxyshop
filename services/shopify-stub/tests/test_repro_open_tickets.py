"""Reproduction gate for the open `services/shopify-stub` finding.

The test here asserts the behaviour that SHOULD hold and therefore fails against the tree as it
stands. It carries ``xfail(strict=True)`` so an ordinary run reports ``xfailed`` and the
repo-wide build gate stays green, while the ticket's own gate
(``pytest <file> -q --runxfail -k <name>``) reports a real failure with the test SELECTED. When
the defect is repaired the test XPASSes, which ``strict=True`` turns into a failure — so the
marker cannot outlive the bug.

**Read the docstring below before treating T-205's recorded blast radius as fact.** The
swallow it names is real and is reproduced here; the consequence it states — that 219 stub
tests silently report green — is not, and the measurement is in the docstring.
"""

from __future__ import annotations

import builtins
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ROOT_CONFTEST = REPO_ROOT / "conftest.py"

#: The import the fixture makes, and the one this test breaks.
STUB_ENTRY_POINT = "shopify_stub.app"


def _root_conftest() -> Any:
    """The already-loaded root conftest, or a fresh load of it.

    Preferring the loaded module matters: re-executing a conftest is a side effect nobody
    asked for, and the object pytest is actually using is the one whose behaviour is under
    test.
    """
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename and Path(filename).resolve() == ROOT_CONFTEST:
            return module

    spec = importlib.util.spec_from_file_location("_root_conftest_probe", ROOT_CONFTEST)
    assert spec is not None and spec.loader is not None, ROOT_CONFTEST
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _with_broken_stub_import() -> Iterator[None]:
    real_import = builtins.__import__

    def broken(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == STUB_ENTRY_POINT:
            raise ImportError("synthetic transitive breakage in shopify_stub.app")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = broken
    try:
        yield
    finally:
        builtins.__import__ = real_import


# =============================================================================================
# T-205 — the root `shopify_stub_url` fixture turns an ImportError into a skip
# =============================================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-205: the root conftest's shopify_stub_url fixture catches (ImportError, "
        "AttributeError) around `__import__('shopify_stub.app')` and calls pytest.skip, so a "
        "broken stub import reaches the reporter as a skip rather than an error — and the one "
        "test written to 'convert that silent skip into a failure' requests the same fixture "
        "and skips with it; remove this marker with the fix"
    ),
)
def test_the_stub_url_fixture_does_not_turn_a_broken_import_into_a_skip() -> None:
    """A skip is not a pass, but it is scored like one.

    ``conftest.py``'s ``shopify_stub_url`` fixture reads::

        try:
            module = __import__("shopify_stub.app", fromlist=["app"])
            app = module.app
        except (ImportError, AttributeError) as exc:
            pytest.skip(f"services/shopify-stub does not expose `shopify_stub.app:app` yet ({exc})")

    That was right while the stub was a scaffold and wrong now that it is a service: the
    condition it was written for — T-013 has not landed yet — has been false for a long time,
    and what remains is a fixture that answers "the stub is broken" with the same word it
    answers "the stub does not exist yet". ``test_stub_contract.py``'s
    ``test_the_root_fixture_serves_a_working_stub`` says in its own docstring that it "does the
    work the fixture declines to" — but it requests that same fixture, so when the fixture
    skips, it skips too, and the only test guarding the pinned entry point guards nothing.

    **Two things in T-205's text are wrong and the record should say so.** The line numbers
    are stale (the fixture is at ``conftest.py:439-462``, not 417-421), and the blast radius is
    not 219 tests: ``services/shopify-stub`` collects **513** tests, and making
    ``shopify_stub.app`` unimportable does NOT turn them green — ``_fixtures_stub.py:26`` and
    ``test_stub_contract.py:32`` both import the stub at module scope, so the directory dies as
    a collection error, ``rc=4``, loudly red. Exactly **two** tests in the repository request
    this fixture, and the second (``proxyshop_support/tests/test_shared_runtime.py:470``)
    catches the skip deliberately. So the true cost of the swallow is one silent test, not a
    silent suite.

    It is still worth closing, because the shape is the one T-159 names and because the
    fixture's skip is the last thing standing between a broken entry point and a green report
    for the test that exists to check it. Note for whoever fixes it: raising instead of
    skipping does **not** break ``test_shared_runtime.py:470`` — that test's skip branch is
    already dead, since the import succeeds today and it simply asserts the yielded URL.

    ``pytest.raises(ImportError)`` cannot express this: ``Skipped`` is not caught by
    ``pytest.raises``, so it propagates and the test that was written to catch the swallow is
    itself swallowed — reported ``s``, exit 0, green. The explicit ``except`` below is what
    makes the failure visible.
    """
    fixture = getattr(_root_conftest().shopify_stub_url, "__wrapped__", None)
    assert fixture is not None, (
        "positive control: shopify_stub_url must still be a pytest fixture wrapping a "
        "generator function, or this probe is measuring the wrong thing"
    )

    breaker = _with_broken_stub_import()
    next(breaker)
    try:
        generator = fixture()
        try:
            next(generator)
        except ImportError:
            return  # the behaviour asked for: the breakage reaches the reporter as an error
        except pytest.skip.Exception as skipped:
            pytest.fail(f"the fixture converted an ImportError into a skip: {skipped}")
        finally:
            generator.close()
        pytest.fail("the synthetic import breakage never fired; this probe is wrong")
    finally:
        next(breaker, None)
