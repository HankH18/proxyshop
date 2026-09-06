"""Reproduction gates for the open `services/shopify-stub` findings.

Each test here asserts the behaviour that SHOULD hold and therefore fails against the tree as
it stands. It carries ``xfail(strict=True)`` so an ordinary run reports ``xfailed`` and the
repo-wide build gate stays green, while the ticket's own gate
(``pytest <file> -q --runxfail -k <name>``) reports a real failure with the test SELECTED. When
the defect is repaired the test XPASSes, which ``strict=True`` turns into a failure — so the
marker cannot outlive the bug.

Every ``xfail`` gate here is paired with a ``..._is_armed`` control that is NOT xfail. That
separation is load-bearing: under ``xfail(strict=True)`` **any** exception in the graded body
is reported ``xfailed``, which is green, so a probe that had quietly stopped working would be
indistinguishable from the defect it is meant to detect — and under the ticket's own
``--runxfail`` gate every dead-probe state reads as "still broken". The preconditions that
make the red meaningful therefore live in the control, where they fail in their own name
during ``make verify``.

Covered here: T-205, T-253, T-255.

**Read the T-205 docstring below before treating its recorded blast radius as fact.** The
swallow it names is real and is reproduced here; the consequence it states — that 219 stub
tests silently report green — is not, and the measurement is in the docstring.
"""

from __future__ import annotations

import builtins
import importlib.util
import sys
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
        "is_redeemable_at answers True for a cart the redemption path rejects — this is the "
        "narrowing T-255 names, and it is asserted here so the gate's red is about the "
        "MISSING PARAMETER rather than about this disagreement having evaporated"
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
            code.rejection(
                now=_T255_NOW, cart_has_order_discount=cart_has_order_discount
            )
            is None
        )
        observed = code.is_redeemable_at(
            _T255_NOW, cart_has_order_discount=cart_has_order_discount
        )
        assert observed is expected, (
            f"with cart_has_order_discount={cart_has_order_discount!r} the redemption path "
            f"says redeemable={expected} and is_redeemable_at says {observed}. The helper "
            "answers a narrower question than the live path asks."
        )
