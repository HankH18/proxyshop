"""The frozen suite's verdict must not depend on what ran before it in the same process.

The defect this file regresses, measured at ``5ded118`` on a clean tree::

    pytest .swarm-loop/acceptance                                  -> 120 passed
    pytest apps/exchange packages/contracts packages/store-agent   -> 3157 passed, 2 xfailed
    pytest apps/exchange packages/contracts packages/store-agent \
           .swarm-loop/acceptance                                  -> 6 FAILED

All six failures were in the frozen suite and all six passed when it ran alone. The cause is
one module global: ``exchange.accept.offer._platform_domains``, the platform's
registered-domain lookup. ``configure_accept`` writes it for the whole process
(``accept/routes.py:509``) — deliberately, T-169, because a call site that forgets
``registered_domains=`` silently falls back to ``bid["store_domain"]``, a field the *bidding
store* wrote — and nothing put it back. A pytest session configures hundreds of apps where a
deployment configures one, so the frozen suite inherited the last one's registry, which knew
nothing about ``store-a``, and every accept in it was refused fail-closed.

That is not only six red tests. A gate whose answer moves with collection order is not a gate,
and the leak cuts both ways: a registry left behind by a neighbour can make a frozen goal pass
that should have failed just as easily as the reverse.

The repair is ``proxyshop_support.process_seams`` plus the autouse ``_process_wide_wiring_seams``
fixture in the root ``conftest.py``. The product is unchanged: the seam is still global and
``configure_accept`` still writes it, because that is what stops an unbound call site trusting
the bidder.

The tests below, in the order a reader needs them:

1. the **writer** — configuring an app really does move the process-wide global (if this stops
   being true the rest of this file is measuring nothing);
2. the **mechanism** — setting that one global, and nothing else, is what refuses the frozen
   suite's accepts; clearing it restores them;
3. the **reset value** — what the isolation puts back for a module first imported mid-test is
   the value that module actually has at import, checked against a fresh interpreter;
4. the **regression** — a real two-suite ordering, in a subprocess, in both directions, green;
5. the **red-proof** — the same ordering with the isolation switched off, red, and red on
   exactly the six goals originally measured — so nobody can mistake (4) for a test that
   would pass with the fix reverted;
6. the **second seam** — ``checkout.registry._REGISTRY``, found by shuffling the combined
   suite once the first leak was closed. It never touched the frozen suite, and it broke
   ``test_fallback_handoff.py`` in exactly the same way: a verdict decided by what ran first.

``pytest apps/exchange/tests/test_process_seam_isolation.py`` is the whole of it, ~5 s.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from exchange.accept import accept, platform_registered_domains, use_registered_domains
from exchange.accept.routes import configure_accept
from exchange.checkout import (
    CHECKOUT_MODES,
    CheckoutProvider,
    StaticRegisteredDomains,
    register_provider,
    registered_modes,
)
from exchange.main import create_app

from proxyshop_support import process_seams

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The frozen nodes that went red, all six: five in the E3 file and the S8 release blocker
#: one directory over. Named rather than run wholesale so this stays a sub-second child run,
#: and never edited — the acceptance suite is hash-pinned.
FROZEN_CASUALTIES = (
    ".swarm-loop/acceptance/test_e3_exchange.py",
    ".swarm-loop/acceptance/test_spec_criteria.py::test_offdomain_checkout_url_is_refused",
)

#: The six frozen goals the leak was measured to break, by test name (see the module
#: docstring). ``test_that_ordering_check_still_fails_when_the_isolation_is_switched_off``
#: asserts the reproduction is exactly this set — not a subset, which would mean the check
#: had drifted onto some other failure, and not a superset, which would mean something new
#: is also broken and nobody noticed.
MEASURED_CASUALTIES = frozenset(
    {
        "test_accept_returns_a_permalink_and_emits_the_golden_event_sequence",
        "test_redirect_and_shopify_modes_emit_identical_event_kinds",
        "test_double_accept_on_one_auction_is_rejected",
        "test_accept_gate_refuses_a_bid_whose_store_became_ineligible_after_bidding",
        "test_both_eligibility_gates_require_a_versioned_seller_eligibility_interface",
        "test_offdomain_checkout_url_is_refused",
    }
)

#: The node in *this* file that plays the polluter in the subprocess runs. It is a real
#: ``configure_accept`` call rather than a bare ``use_registered_domains``, because the
#: production write is the thing under regression.
POLLUTER = f"apps/exchange/tests/{Path(__file__).name}::test_configuring_an_app_moves_the_process_wide_registry"

T_NOW = 1_700_000_000.0
T_FUTURE = 2_000_000_000.0


class _RecordingCodeCreator:
    """The frozen suite's stand-in for the merchant ``POST /codes`` client, restated."""

    def __init__(self) -> None:
        self.code = "PROXY-TEST-CODE"
        self.permalink = f"https://store-a.example.com/cart/1:1?discount={self.code}"
        self.calls: list[tuple[str, Any]] = []

    def create_code(self, store_id: str, offer: Any) -> dict[str, str]:
        self.calls.append((store_id, offer))
        return {"code": self.code, "permalink_url": self.permalink}

    __call__ = create_code


def _auction() -> dict[str, Any]:
    """``test_e3_exchange._auction()``, restated — same ids, same domains, same shape.

    Restated rather than imported: the frozen suite is hash-pinned and importing across the
    freeze boundary would make this file's meaning depend on a file it may not edit. If the
    two ever drift, test 2 below stops demonstrating the frozen suite's failure and test 3
    still catches the regression, which is the right way round.
    """
    bids = []
    for bid_id in ("bid-a", "bid-b"):
        store_id = bid_id.replace("bid", "store")
        domain = f"{store_id}.example.com"
        bids.append(
            {
                "bid_id": bid_id,
                "store_id": store_id,
                "store_domain": domain,
                "offer": {
                    "product_ref": "product-1",
                    "unit_price": 100.0,
                    "total_price": 100.0,
                    "checkout_url": f"https://{domain}/cart/1:1?discount=NET",
                    "expires_at": T_FUTURE,
                },
            }
        )
    return {
        "auction_id": "auction-1",
        "intent_id": "intent-1",
        "cluster_id": "cluster-1",
        "bids": bids,
        "accepted_bid_ref": None,
        "now": T_NOW,
    }


def _run_pytest(*args: str, allow_leak: bool = False) -> subprocess.CompletedProcess[str]:
    """One pytest session in a child process, from the repo root, in the order given.

    ``-p no:randomly`` because this measures ORDER and the argument order has to survive.
    ``-p no:cacheprovider`` because a child writing ``.pytest_cache`` under the parent's run
    is a race nobody needs.
    """
    env = dict(os.environ)
    env.pop(process_seams.ENV_ALLOW_LEAK, None)
    if allow_leak:
        env[process_seams.ENV_ALLOW_LEAK] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "-q",
            "--no-header",
            *args,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


# =====================================================================================
# 1 — the writer
# =====================================================================================
def test_configuring_an_app_moves_the_process_wide_registry() -> None:
    """``configure_accept`` writes ``_platform_domains``, and this node is the subprocess polluter.

    Asserting it is not pedantry. This node is what the two subprocess tests below run *first*,
    so if ``configure_accept`` ever stopped writing the seam, those two would keep passing while
    measuring nothing at all — the exact shape of vacuous gate this repository keeps finding.

    The registry wired here deliberately knows **no** stores, which is what a real polluter
    leaves behind: ``_t294_corpus`` in ``test_repro_open_tickets.py`` leaves a lambda over its
    own 20-auction store table, and the frozen suite's ``store-a`` is not in it either.
    """
    before = platform_registered_domains()
    configure_accept(create_app(), registered_domains=StaticRegisteredDomains({}))
    after = platform_registered_domains()

    assert after is not before, (
        "configure_accept no longer moves exchange.accept.offer._platform_domains — the "
        "subprocess ordering tests in this file are now measuring nothing"
    )
    assert isinstance(after, StaticRegisteredDomains)


# =====================================================================================
# 2 — the mechanism, both directions, in this process
# =====================================================================================
def test_a_leaked_registry_is_exactly_what_refuses_the_frozen_suites_accepts() -> None:
    """Set that one global and the frozen suite's accept is refused; clear it and it is not.

    Nothing else differs between the two halves — same auction, same code creator, same mode,
    same call with no ``registered_domains=``. The refusal is the fail-closed branch of the
    T-169 guard, and its reason names the frozen suite's own store.
    """
    previous = use_registered_domains(
        lambda store_id: {"corpus-store": "corpus.example.com"}.get(str(store_id))
    )
    try:
        leaked = accept(_auction(), "bid-a", _RecordingCodeCreator(), "shopify")
    finally:
        use_registered_domains(previous)

    assert leaked.accepted is False
    assert "store-a" in str(leaked.denial_reason)

    previous = use_registered_domains(None)
    try:
        unwired = accept(_auction(), "bid-a", _RecordingCodeCreator(), "shopify")
    finally:
        use_registered_domains(previous)

    assert unwired.accepted is True, (
        "with nothing wired the frozen contract's fallback must still complete an accept — "
        "if this half fails the demonstration above proves nothing about ordering"
    )


def test_the_unwired_value_this_isolation_resets_to_is_the_modules_own_default() -> None:
    """``process_seams.UNWIRED_VALUE`` is a claim about the source; check it against the source.

    A module first imported *during* a test is reset to this value, so a table that drifted
    away from the module's import-time state would quietly wire something instead of unwiring
    it. Measured in a fresh interpreter, which is the only place "at import" is observable.
    """
    probe = ";".join(
        f"import {root}.{suffix} as m; assert m.{attribute} is None, m.{attribute}"
        for root in process_seams.SPELLINGS
        for suffix, attribute, kind in process_seams.SEAMS
        if kind == process_seams.VALUE
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{REPO_ROOT / '.pkgroot'}"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert process_seams.UNWIRED_VALUE is None


# =====================================================================================
# 3 and 4 — the ordering regression, and the proof it can go red
# =====================================================================================
@pytest.mark.parametrize("order", ["polluter-first", "polluter-last"])
def test_the_frozen_exchange_goals_are_green_in_either_order(order: str) -> None:
    """The whole point: the frozen suite's verdict does not move with what shares its process.

    Both orders, because isolation has two directions — a neighbour must not decide the frozen
    suite's answer, and the frozen suite must not decide a neighbour's.
    """
    args = (
        (POLLUTER, *FROZEN_CASUALTIES)
        if order == "polluter-first"
        else (*FROZEN_CASUALTIES, POLLUTER)
    )
    result = _run_pytest(*args)

    assert result.returncode == 0, (
        f"order={order}: the frozen exchange goals moved with collection order.\n"
        f"{result.stdout[-4000:]}"
    )


def test_that_ordering_check_still_fails_when_the_isolation_is_switched_off() -> None:
    """The red-proof. A green gate that would be green without the fix is not evidence.

    ``PROXYSHOP_ALLOW_SEAM_LEAK=1`` makes ``process_seams.restore`` a no-op and nothing else,
    so this run is the pre-fix behaviour reproduced on demand. It must fail, and it must fail
    inside the frozen file rather than anywhere else.
    """
    result = _run_pytest(POLLUTER, *FROZEN_CASUALTIES, allow_leak=True)

    assert result.returncode != 0, (
        "with the process-seam isolation disabled the frozen suite still passed behind a "
        "configured app — either the leak is closed somewhere else now (good: delete this "
        "test and say where) or this check has stopped exercising it.\n"
        f"{result.stdout[-4000:]}"
    )
    # All six of the originally measured casualties, by name, so a partial reproduction
    # cannot be mistaken for the whole one.
    red = {
        # `-ra` appends " - <reason>" to some summary lines; the node name is what precedes it.
        line.split("::", 1)[-1].split(" - ", 1)[0].strip()
        for line in result.stdout.splitlines()
        if line.startswith("FAILED .swarm-loop/acceptance/")
    }
    assert red == MEASURED_CASUALTIES, (
        f"the six frozen goals this leak was measured to break are {sorted(MEASURED_CASUALTIES)}; "
        f"switching the isolation off reproduced {sorted(red)}"
    )


# =====================================================================================
# 6 — the second seam, found while proving the first fix holds under shuffled ordering
# =====================================================================================
def test_registering_a_checkout_provider_does_not_outlive_the_test_that_registered_it() -> None:
    """``CHECKOUT_MODE -> provider`` is a process-global table, and tests write to it.

    ``test_orphaned_code.py::_accept_against`` registers one ``hostile-<label>`` provider per
    parametrised case and never removes it; ``test_checkout_provider.py`` leaves
    ``bank_transfer`` behind. ``test_fallback_handoff.py`` then sweeps ``registered_modes()``
    — every mode there is — and requires at least half of them to mint. In file order the
    sweep runs first and sees 3 modes; shuffled it sees up to 12, nine of them providers
    written to fail, and ``test_the_same_sweep_mints_for_a_real_bid`` goes red on a product
    that did not move. Measured under ``SHUFFLE_SEED`` 20260907 and 424242.

    This node is one half of the check. The other half is that it does not *see* a leaked
    mode: the autouse isolation restores the table after every test, so ``registered_modes()``
    at the top of any test is the three shipped modes plus whatever this process's product
    code registered — never another test's fixture.
    """
    shipped = registered_modes()
    assert set(CHECKOUT_MODES) <= set(shipped), shipped
    assert not [mode for mode in shipped if mode.startswith("hostile-")], (
        f"a hostile test provider outlived the test that registered it: {shipped}"
    )

    class _Ephemeral(CheckoutProvider):
        name = "ephemeral"

        def mint(self, request: Any) -> Any:  # pragma: no cover - never resolved here
            raise AssertionError("not reachable")

    register_provider("ephemeral-mode-for-this-test-only", _Ephemeral())
    assert "ephemeral-mode-for-this-test-only" in registered_modes()

    # And the isolation itself, driven directly rather than inferred: a snapshot taken before
    # a registration puts the table back exactly, in place, without rebinding the object every
    # importer of `checkout.registry` is holding.
    from exchange.checkout import registry as registry_module  # noqa: PLC0415

    live = registry_module._REGISTRY
    taken = process_seams.snapshot()
    register_provider("another-ephemeral-mode", _Ephemeral())
    moved = process_seams.restore(taken)

    assert registry_module._REGISTRY is live, "the registry object was rebound, not restored"
    assert "another-ephemeral-mode" not in registered_modes()
    assert "ephemeral-mode-for-this-test-only" in registered_modes(), (
        "restore() reset the table further back than the snapshot it was given"
    )
    assert any(name.endswith("_REGISTRY") for name in moved), moved
