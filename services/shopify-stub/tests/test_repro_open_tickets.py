"""Reproduction gates for the open `services/shopify-stub` findings.

Each test here asserts the behaviour that SHOULD hold. **While its defect is open** it carries
``xfail(strict=True)``, so an ordinary run reports ``xfailed`` and the repo-wide build gate
stays green, while the ticket's own gate (``pytest <file> -q --runxfail -k <name>``) reports a
real failure with the test SELECTED. When the defect is repaired the test XPASSes, which
``strict=True`` turns into a failure — so the marker cannot outlive the bug, and the lane that
repairs the defect is the lane that removes it.

**A test here whose marker is gone is no longer a reproduction.** It is a live regression guard
that must pass in its own name on every ordinary run, ``--runxfail`` or not, and must fail in
its own name if the defect returns. T-205's two guards are in that state; T-253 and T-255 are
still reproductions.

Every ``xfail`` gate here is paired with a ``..._is_armed`` control that is NOT xfail. That
separation is load-bearing: under ``xfail(strict=True)`` **any** exception in the graded body
is reported ``xfailed``, which is green, so a probe that had quietly stopped working would be
indistinguishable from the defect it is meant to detect — and under the ticket's own
``--runxfail`` gate every dead-probe state reads as "still broken". The preconditions that
make the red meaningful therefore live in the control, where they fail in their own name
during ``make verify``. T-205's guards need no separate control: not being xfail, every
precondition they assert already fails in its own name, and both carry theirs inline.

Covered here: T-205 (**FIXED** — its marker was removed with the repair, so it is now a live
regression guard rather than a reproduction), T-253, T-255.

**Read the T-205 docstring below before treating its recorded blast radius as fact.** The
swallow it names is real and is reproduced here; the consequence it states — that 219 stub
tests silently report green — is not, and the measurement is in the docstring.
"""

from __future__ import annotations

import builtins
import importlib.util
import sys
import types
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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


def _with_stub_app_missing_its_app_attribute() -> Iterator[None]:
    """``shopify_stub.app`` imports cleanly but no longer exposes ``app``.

    The OTHER half of what the fixture used to swallow. ``except (ImportError, AttributeError)``
    named two failures and ``_with_broken_stub_import`` drives only the first; a module that
    imports fine and has lost the one attribute the whole repo pins is the second, and nothing
    anywhere exercised it.

    A stand-in module is handed back rather than the real one with ``app`` deleted, because
    deleting the attribute would mutate a module every later test in the session shares — the
    breakage has to end when this generator does.
    """
    real_import = builtins.__import__

    def without_app(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == STUB_ENTRY_POINT:
            return types.ModuleType(STUB_ENTRY_POINT)
        return real_import(name, *args, **kwargs)

    builtins.__import__ = without_app
    try:
        yield
    finally:
        builtins.__import__ = real_import


# =============================================================================================
# T-205 — the root `shopify_stub_url` fixture turned an ImportError into a skip. FIXED.
# =============================================================================================
#
# The `xfail(strict=True)` marker that stood here was removed WITH the repair, not around it:
# `conftest.py`'s `shopify_stub_url` no longer wraps `__import__("shopify_stub.app")` in
# `except (ImportError, AttributeError): pytest.skip(...)`, so a broken entry point reaches
# the reporter as an error in its own name. This test is now a LIVE regression guard and must
# fail in its own name if the swallow ever returns. Not one assertion in its body was touched.
#
# TWO GUARDS, ONE PER ARM OF THE SWALLOW. The removed `except` named `(ImportError,
# AttributeError)`, and the guard directly below breaks the IMPORT, so on its own it covered
# half the repair: a `shopify_stub.app` that imports fine and simply stopped defining `app`
# went untested. `test_the_stub_url_fixture_does_not_turn_a_missing_app_attribute_into_a_skip`
# covers the other half. Both arms are proven independently load-bearing — reinstating a
# swallow that catches ONLY ImportError reds the first and leaves the second green, and
# catching ONLY AttributeError does the reverse (measured; the numbers are in the commit body).
#
# WHAT ESTABLISHED THE DEFECT IS GONE, measured rather than argued, all on this branch:
#   * before the change the gate was RED — "Failed: the fixture converted an ImportError into
#     a skip: services/shopify-stub does not expose `shopify_stub.app:app` yet (synthetic
#     transitive breakage in shopify_stub.app)";
#   * after it, the gate passes, and reinstating ONLY the `try`/`except pytest.skip` on a
#     scratch copy outside the repo returns it to `1 failed` in 0.14s — no hang;
#   * the `finally: pytest.fail("the synthetic import breakage never fired")` arm and the
#     `assert fixture is not None` positive control are both still reachable, so a probe that
#     stopped working fails here instead of reading as a pass.
#
# BLAST RADIUS, RE-MEASURED (the record's "219 stub tests silently green" is wrong, and so is
# the 513 an earlier lane recorded — the tree has grown). With a real `ModuleNotFoundError`
# planted inside `shopify_stub.app` on a scratch copy of this tree:
#   * `pytest services/shopify-stub` collects 521 tests and dies rc=4 at
#     `tests/_fixtures_stub.py:26`, which imports the stub at module scope. Loudly red, and it
#     never reaches the fixture at all;
#   * `pytest proxyshop_support/tests/test_shared_runtime.py -k shopify_stub_url` reported
#     `1 passed`, rc=0 — that test catches `pytest.skip.Exception` and returns, so the swallow
#     did not even show as an `s`. It showed as a PASS.
# So the true cost was ONE silently-green test, not a silent suite — and "silently green" is
# literal here, which is worse than the record claimed even as the count is far smaller.


def test_the_stub_url_fixture_does_not_turn_a_broken_import_into_a_skip() -> None:
    """A skip is not a pass, but it is scored like one.

    ``conftest.py``'s ``shopify_stub_url`` fixture USED TO READ (T-205, now repaired)::

        try:
            module = __import__("shopify_stub.app", fromlist=["app"])
            app = module.app
        except (ImportError, AttributeError) as exc:
            pytest.skip(f"services/shopify-stub does not expose `shopify_stub.app:app` yet ({exc})")

    That was right while the stub was a scaffold and wrong once it was a service: the
    condition it was written for — T-013 has not landed yet — had been false for a long time,
    and what was left was a fixture that answered "the stub is broken" with the same word it
    answers "the stub does not exist yet". ``test_stub_contract.py``'s
    ``test_the_root_fixture_serves_a_working_stub`` says in its own docstring that it "does the
    work the fixture declines to" — but it requests that same fixture, so when the fixture
    skipped, it skipped too, and the only test guarding the pinned entry point guarded nothing.
    The ``try`` is gone; both exceptions now reach the reporter in their own name.

    **Two things in T-205's text are wrong and the record should say so.** The line numbers
    are stale (the fixture is at ``conftest.py:439``, not 417-421), and the blast radius is
    not 219 tests: ``services/shopify-stub`` collects **521** tests on this tree (an earlier
    lane measured 513; it has grown since), and making ``shopify_stub.app`` unimportable does
    NOT turn them green — ``_fixtures_stub.py:26`` and ``test_stub_contract.py:32`` both import
    the stub at module scope, so the directory dies as a collection error, ``rc=4``, loudly
    red. Exactly **two** tests in the repository request this fixture, and the second
    (``proxyshop_support/tests/test_shared_runtime.py:788``,
    ``test_shopify_stub_url_fixture_is_wired_to_the_stub``) catches the skip deliberately —
    measured with the entry point genuinely broken, it reported ``1 passed``, rc=0, so the
    swallow did not even surface as an ``s``. The true cost was one silently-*passing* test,
    not a silent suite.

    It was worth closing because the shape is the one T-159 names and because the fixture's
    skip was the last thing standing between a broken entry point and a green report for the
    test that exists to check it. Raising instead of skipping does **not** break
    ``test_shared_runtime.py:788`` — that test's skip branch was already dead, since the import
    succeeds today and it simply asserts the yielded URL.

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


def test_the_stub_url_fixture_does_not_turn_a_missing_app_attribute_into_a_skip() -> None:
    """The second arm of the same repair, which was unguarded when the repair landed.

    The swallow this ticket removed read ``except (ImportError, AttributeError)`` and named
    TWO failures. The guard above breaks the *import*, so it exercised only the first, and a
    ``shopify_stub.app`` that imports perfectly and simply does not define ``app`` — a rename,
    a factory-only refactor, a lost module-level assignment — had nothing testing it at all.
    The repair covered both; the gate covered one. This closes that.

    ``AttributeError`` is the exception the fixture's ``module.app`` raises today.
    ``ImportError`` is accepted beside it because a legitimate respelling to
    ``from shopify_stub.app import app`` raises that instead for a missing attribute, and both
    are loud errors in their own name. What is refused is the skip, in either spelling.
    """
    fixture = getattr(_root_conftest().shopify_stub_url, "__wrapped__", None)
    assert fixture is not None, (
        "positive control: shopify_stub_url must still be a pytest fixture wrapping a "
        "generator function, or this probe is measuring the wrong thing"
    )

    breaker = _with_stub_app_missing_its_app_attribute()
    next(breaker)
    try:
        # Positive control on the stand-in itself. If it still carried `app`, nothing in the
        # fixture could raise, the final `pytest.fail` would be the only outcome, and a
        # passing run would mean the opposite of what it looks like.
        stand_in = builtins.__import__(STUB_ENTRY_POINT, fromlist=["app"])
        assert not hasattr(stand_in, "app"), (
            "the stand-in shopify_stub.app still exposes `app`, so the missing-attribute path "
            "is never taken; this probe is measuring nothing"
        )

        generator = fixture()
        try:
            next(generator)
        except pytest.skip.Exception as skipped:
            pytest.fail(f"the fixture converted a missing `app` attribute into a skip: {skipped}")
        except (AttributeError, ImportError):
            return  # the behaviour asked for: the breakage reaches the reporter as an error
        finally:
            generator.close()
        pytest.fail("the synthetic missing-`app` breakage never fired; this probe is wrong")
    finally:
        next(breaker, None)


# =============================================================================================
# T-253 — `utc_now()` is documented as the stub's single wall-clock read and is called by
#          nothing, while `StubState.now()` reads `datetime.now(UTC)` for itself
# =============================================================================================
#
# ``services/shopify-stub/src/codes.py:245`` publishes this contract in its own docstring:
#
#     The stub's single wall-clock read.
#     One function so a test can freeze time (``frozen_clock``) or the control plane can
#     override it, without every module reaching for ``datetime.now`` independently.
#
# Measured at HEAD, on this branch, from this worktree:
#   * ``git grep -n utc_now -- services apps packages`` finds the definition and NO call site,
#     so the "single wall-clock read" is read by nobody;
#   * ``git grep -n 'datetime.now' -- services/shopify-stub/src`` finds exactly two hits, and
#     the second is ``state.py:513`` — ``StubState.now()``, which every live clock read in the
#     stub actually goes through (``app.py:210,285,348,610``; ``orders.py:103,167,210``;
#     ``webhooks.py:155``). So the module that matters reaches for ``datetime.now``
#     independently, which is the exact thing the docstring says does not happen.
#
# The gate is behavioural rather than a grep, deliberately: a source scan over
# ``services/shopify-stub/src`` would be grading files this lane can edit, which measures
# nothing. What is asserted instead is the *consequence* the docstring promises — that
# overriding the one function moves the stub's clock.
#
# ONE PREMISE OF THE TICKET IS FALSE AND THE RECORD SHOULD SAY SO. T-253 states that
# "frozen_clock does not exist anywhere in the repo". It does: ``conftest.py:471`` defines it
# (a ``time_machine`` fixture), ``conftest.py:52`` documents it, ``proxyshop_support/clock.py:9``
# names it, and ``proxyshop_support/tests/test_shared_runtime.py:141`` exercises it. The
# docstring's reference is therefore live, not dangling. What is false is the *first* sentence
# — "the stub's single wall-clock read" — and that half reproduces exactly.


def test_t253_the_utc_now_probe_is_armed() -> None:
    """Not xfail. Everything the gate below needs in order for its red to mean something.

    If ``utc_now`` were deleted, or ``StubState.now`` stopped being the stub's clock, the gate
    would raise and ``xfail(strict=True)`` would report that as green. These assertions fail
    in their own name instead.
    """
    from shopify_stub import codes as codes_module
    from shopify_stub.state import StubState

    assert callable(getattr(codes_module, "utc_now", None)), (
        "shopify_stub.codes.utc_now is gone, so the gate below has no override point"
    )
    live = codes_module.utc_now()
    assert isinstance(live, datetime) and live.tzinfo is not None, (
        f"utc_now() returned {live!r}; the gate compares it against an aware sentinel"
    )

    # The override mechanism itself works — so a red below is the stub not honouring the
    # override, never the monkeypatch failing to take.
    sentinel = datetime(2031, 3, 4, 5, 6, 7, tzinfo=UTC)
    original = codes_module.utc_now
    try:
        codes_module.utc_now = lambda: sentinel  # type: ignore[assignment]
        assert codes_module.utc_now() == sentinel, "patching the module attribute did not take"
    finally:
        codes_module.utc_now = original  # type: ignore[assignment]

    clock = StubState().now()
    assert isinstance(clock, datetime) and clock.tzinfo is not None, (
        f"StubState.now() returned {clock!r}; it is supposed to be the stub's aware clock"
    )


# FIXED — the ``xfail(strict=True)`` marker that stood here was removed with the repair, not
# around it. ``StubState.now`` (services/shopify-stub/src/state.py) now delegates to
# ``shopify_stub.codes.utc_now`` through the module attribute, so ``utc_now`` really is the
# stub's single wall-clock read and overriding it really does move the clock. CAUSATION
# PROVEN: reverting that ONE expression to ``datetime.now(UTC)`` and changing nothing else
# returns this test to ``xfailed`` (measured: ``1 passed, 3 deselected, 1 xfailed``);
# restoring it returns it to a pass. No assertion in this test's body was touched.
def test_t253_overriding_utc_now_moves_the_stubs_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The docstring's promise, driven at the clock every live caller in the stub uses."""
    from shopify_stub import codes as codes_module
    from shopify_stub.state import StubState

    sentinel = datetime(2031, 3, 4, 5, 6, 7, tzinfo=UTC)
    monkeypatch.setattr(codes_module, "utc_now", lambda: sentinel)

    observed = StubState().now()
    assert observed == sentinel, (
        "StubState.now() ignored the override of shopify_stub.codes.utc_now and returned "
        f"{observed!r} instead of {sentinel!r}. utc_now is not the stub's single wall-clock "
        "read: state.py reaches for datetime.now(UTC) on its own, which is what its docstring "
        "says no module does."
    )


# =============================================================================================
# T-255 — `DiscountCode.is_redeemable_at()` cannot express the cart state both live callers
#          pass, so it silently answers a narrower question than the redemption path asks
# =============================================================================================
#
# ``codes.py:240`` reads::
#
#     def is_redeemable_at(self, now: datetime) -> bool:
#         """Convenience for the common "no other discount on the cart" case."""
#         return self.rejection(now=now) is None
#
# ``rejection``'s ``cart_has_order_discount`` is therefore pinned to ``False`` with no way for
# a caller to say otherwise. Measured at HEAD: BOTH live redemption sites pass it explicitly
# and neither passes ``False`` — ``app.py:347-350`` and ``orders.py:124-127`` both forward
# ``state.config.has_active_automatic_discount``. So the "common case" the docstring names is
# not the case any live caller is in, and a future caller reaching for the shorter spelling
# gets ``True`` for a cart the redemption path rejects with
# ``CONFLICTS_WITH_EXISTING_DISCOUNT``.
#
# The two functions are asserted against EACH OTHER rather than against a hand-written
# expectation, so the gate keeps grading if either one's rules change.


def _t255_code(**overrides: Any) -> Any:
    """A minted code in its ordinary live state: active, unused, non-combining."""
    from shopify_stub.codes import DiscountCode

    base: dict[str, Any] = {
        "code": "PSX-T255ABCD",
        "starts_at": _T255_NOW,
        "ends_at": _T255_NOW + timedelta(hours=48),
        "usage_limit": 1,
        "percentage": 0.1,
    }
    base.update(overrides)
    return DiscountCode(**base)


_T255_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_t255_the_is_redeemable_at_probe_is_armed() -> None:
    """Not xfail. The disagreement the gate below measures is real, and is not a fixture bug.

    Three facts, none of which needs the repair: the live path really does narrow on the
    cart; the same code with no cart discount really is redeemable; and the convenience
    wrapper really does answer ``True`` for the code the live path would reject. If any of
    these stops holding, the gate's red would mean something other than T-255.
    """
    from shopify_stub.codes import RejectionReason

    code = _t255_code()
    assert code.rejection(now=_T255_NOW, cart_has_order_discount=True) is (
        RejectionReason.CONFLICTS_WITH_EXISTING_DISCOUNT
    ), "the live path no longer narrows on an order-level discount already on the cart"
    assert code.rejection(now=_T255_NOW, cart_has_order_discount=False) is None, (
        "the probe's code is not redeemable even on an empty cart, so it grades nothing"
    )
    assert code.is_redeemable_at(_T255_NOW) is True, (
        "the unqualified question — no order-level discount on the cart — must still answer "
        "True, so the default stayed False and existing single-argument callers kept their "
        "meaning. Before the repair this same True was the DEFECT, because False was pinned "
        "and there was no way to ask anything else; it is kept here as the control on the "
        "default, and the gate below is what grades the parameter"
    )


# FIXED — the ``xfail(strict=True)`` marker that stood here was removed with the repair.
# ``DiscountCode.is_redeemable_at`` now takes ``cart_has_order_discount`` (keyword-only,
# defaulting to False) and forwards it to ``rejection``, so the two can no longer disagree
# for the same inputs. CAUSATION PROVEN: reverting the signature and the forwarded argument,
# and changing nothing else, returns this test to ``xfailed`` (measured: ``1 passed, 3
# deselected, 1 xfailed``); restoring returns it to a pass. No assertion in this test's body
# was touched. A fix that ACCEPTED the keyword and ignored it does not pass: the sweep
# compares against ``rejection``'s own verdict, which does not ignore it.
def test_t255_is_redeemable_at_agrees_with_the_live_rejection_path() -> None:
    """For every cart state the redemption path can be in, the two must give one answer."""
    code = _t255_code()
    for cart_has_order_discount in (False, True):
        expected = (
            code.rejection(now=_T255_NOW, cart_has_order_discount=cart_has_order_discount) is None
        )
        observed = code.is_redeemable_at(_T255_NOW, cart_has_order_discount=cart_has_order_discount)
        assert observed is expected, (
            f"with cart_has_order_discount={cart_has_order_discount!r} the redemption path "
            f"says redeemable={expected} and is_redeemable_at says {observed}. The helper "
            "answers a narrower question than the live path asks."
        )
