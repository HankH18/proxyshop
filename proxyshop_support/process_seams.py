"""Snapshot/restore for the *process-wide* wiring seams product code writes at configure time.

Why this exists — one measured failure, stated exactly
------------------------------------------------------
``apps/exchange/src/accept/offer.py`` keeps the platform's registered-domain lookup in a
module global (``_platform_domains``, written by ``use_registered_domains``). That is a
deliberate T-169 decision and not a defect: a call site that forgets to pass
``registered_domains=`` silently falls back to ``bid["store_domain"]`` — a field the *bidding
store* wrote — so the seam exists so that wiring an app binds every call site in the process.

``configure_accept`` (``apps/exchange/src/accept/routes.py:509``) therefore writes that global
whenever an app is configured, and ``composition.py:2796`` reaches the same call. In a
pytest session that is not one app, it is hundreds — and nothing put the global back. Measured
at ``5ded118``::

    pytest .swarm-loop/acceptance                                  -> 120 passed
    pytest apps/exchange packages/contracts packages/store-agent   -> 3157 passed, 2 xfailed
    pytest apps/exchange packages/contracts packages/store-agent \
           .swarm-loop/acceptance                                  -> 6 FAILED

The six were all in the frozen suite, all green when it ran alone. The last writer before it
(``test_repro_open_tickets.py``'s ``_t294_corpus``, in file order) left behind
``lambda store_id: domains.get(str(store_id))`` — a registry that knows only that corpus's own
store ids. The frozen suite calls ``accept()`` with no ``registered_domains=``, its stores are
named ``store-a``/``store-b``, the leaked registry answers ``None`` for those, and the
fail-closed branch refuses every accept: *"the platform holds no registered domain for
'store-a'; the bid's claim 'store-a.example.com' is not evidence of one"*.

``apps/exchange/src/accept/claims.py:97`` predicted this in prose ("call
``use_registered_domains(<real registry>)`` in the same process and five frozen goals go red")
and ``pyproject.toml``'s ``norecursedirs`` note describes the symptom ("the frozen suite
produces phantom failures when it shares one pytest session"). Neither named the writer.

What this module does, and what it deliberately does not
--------------------------------------------------------
:func:`snapshot` reads each seam listed in :data:`SEAMS`; :func:`restore` puts it back. The
root ``conftest.py`` calls the pair around **every** test, so a test that wires an app cannot
change the verdict of a later test in the same process. That is test isolation only — the
product keeps the seam and keeps writing it, because the seam is what stops an unbound call
site trusting the bidder.

Three properties that are load-bearing:

* **Nothing is imported.** A seam whose module is not in ``sys.modules`` is not touched, so a
  buyer or trust session never drags the exchange package in just to be isolated from it.
* **Both dotted spellings are handled.** ``apps/exchange/src`` is importable as ``exchange.*``
  and as ``apps.exchange.src.*``; ``accept/_spellings.py`` normally binds them to one module
  object, but if that binding ever fails there are two copies of the global and restoring only
  one would restore nothing.
* **A module first imported *during* a test is reset to** :data:`UNWIRED_VALUE`, the value the
  module has at import. ``apps/exchange/tests/test_process_seam_isolation.py`` asserts that
  against a freshly spawned interpreter, so this table cannot quietly drift from the source.

Stdlib only: this file ships into six container images with ``COPY proxyshop_support/`` and
none of them installs pytest (see :mod:`proxyshop_support.fixture_loader` on that constraint).
"""

from __future__ import annotations

import os
import sys
from typing import Any

__all__ = [
    "ENV_ALLOW_LEAK",
    "MAPPING",
    "SEAMS",
    "SPELLINGS",
    "UNWIRED_VALUE",
    "VALUE",
    "isolation_enabled",
    "restore",
    "snapshot",
]

#: The two dotted roots that name ``apps/exchange/src``; see ``accept/_spellings.py``.
SPELLINGS: tuple[str, ...] = ("exchange", "apps.exchange.src")

#: A seam that is one rebindable reference. Restoring it is ``setattr`` back to what it was,
#: and a module first imported mid-test is reset to :data:`UNWIRED_VALUE`.
VALUE = "value"

#: A seam that is one mutable mapping filled in place. Restoring it is "these exact keys,
#: nothing added" — never a rebind, because the module and its callers hold that same object.
#: A module first imported mid-test is left alone: its import-time contents are not
#: observable from here, and emptying a registry would be far worse than one leaked key.
MAPPING = "mapping"

#: ``(module suffix, attribute, kind)`` for every process-wide seam in the exchange tree.
#:
#: ``_platform_domains`` is the one measured breaking the frozen suite (module docstring).
#:
#: ``_platform_claims`` is the same shape one file over — written by ``use_acceptance_claims``,
#: read by ``platform_acceptance_claims`` as the fallback under the request-scoped ContextVar.
#: No test writes it today, which is precisely why it belongs here: the next one to reach for
#: it would re-open the same hole, and ``claims.py``'s own docstring says what that costs.
#:
#: ``checkout.registry._REGISTRY`` is the ``CHECKOUT_MODE -> provider`` table, and it is a
#: *measured* second instance of the same defect rather than a precaution.
#: ``test_orphaned_code.py::_accept_against`` registers a ``hostile-<label>`` provider per
#: parametrised case and never removes it, and ``test_checkout_provider.py`` leaves
#: ``bank_transfer`` behind. ``test_fallback_handoff.py`` then sweeps
#: ``registered_modes()`` — every mode there is — and asserts that at least half of them
#: mint. In file order the sweep runs before the registrations and sees 3 modes; shuffled, it
#: sees up to 12, nine of which are providers written to fail, and
#: ``test_the_same_sweep_mints_for_a_real_bid`` goes red on a product that did not move.
SEAMS: tuple[tuple[str, str, str], ...] = (
    ("accept.offer", "_platform_domains", VALUE),
    ("accept.claims", "_platform_claims", VALUE),
    ("checkout.registry", "_REGISTRY", MAPPING),
)

#: The value every :data:`VALUE` seam holds at module import — "nothing is wired".
UNWIRED_VALUE: Any = None

#: Set to ``1`` to make :func:`restore` a no-op. This is the *red-proof* switch for
#: ``apps/exchange/tests/test_process_seam_isolation.py``: a guard that cannot be turned off
#: cannot be shown to be doing anything, and this repo has been bitten by gates that were
#: vacuously green. It is read at restore time and never written here.
ENV_ALLOW_LEAK = "PROXYSHOP_ALLOW_SEAM_LEAK"


def isolation_enabled() -> bool:
    """False when :data:`ENV_ALLOW_LEAK` asks for the un-isolated (pre-fix) behaviour."""
    return os.environ.get(ENV_ALLOW_LEAK) != "1"


def snapshot() -> tuple[tuple[str, str, str, bool, Any], ...]:
    """Read every loaded seam: ``(module, attribute, kind, was present, value)`` per entry.

    Modules that are not loaded are still reported — with ``was present = False`` — so that a
    :data:`VALUE` seam whose module is first imported *during* the test is reset rather than
    left holding whatever that test wired. Loading nothing is the point; see the module
    docstring. A :data:`MAPPING` seam is copied, never referenced: the whole hazard is that
    the live object is mutated in place.
    """
    taken: list[tuple[str, str, str, bool, Any]] = []
    for root in SPELLINGS:
        for suffix, attribute, kind in SEAMS:
            name = f"{root}.{suffix}"
            module = sys.modules.get(name)
            if module is None:
                taken.append((name, attribute, kind, False, UNWIRED_VALUE))
                continue
            current = getattr(module, attribute, UNWIRED_VALUE)
            if kind == MAPPING:
                current = dict(current) if current is not None else None
            taken.append((name, attribute, kind, True, current))
    return tuple(taken)


def restore(taken: tuple[tuple[str, str, str, bool, Any], ...]) -> tuple[str, ...]:
    """Put every seam in ``taken`` back; return the names that had actually moved.

    The return value is what a caller can assert on — an empty tuple means the block under
    the snapshot wired nothing — and it is what makes this function testable without a
    second process.
    """
    if not isolation_enabled():
        return ()
    changed: list[str] = []
    for name, attribute, kind, was_present, value in taken:
        module = sys.modules.get(name)
        if module is None:
            continue
        current = getattr(module, attribute, UNWIRED_VALUE)
        if kind == MAPPING:
            if not was_present or value is None or current is None:
                continue
            if dict(current) == value:
                continue
            changed.append(f"{name}.{attribute}")
            current.clear()
            current.update(value)
            continue
        if current is value:  # includes "imported mid-test and left unwired"
            continue
        changed.append(f"{name}.{attribute}")
        setattr(module, attribute, value)
    return tuple(changed)
