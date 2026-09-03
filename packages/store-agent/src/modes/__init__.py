"""Shadow mode, activation and trust intake for the hosted advocate (R7, R6, R13).

The public surface:

* :class:`~.runner.AgentRunner` — the loop. ``AgentRunner(context, sink=..., submitter=...)``
  answers a `BidRequest` with :func:`store_agent.runtime.bid`, logs the answer, and submits it
  only in `active` mode. ``runner.mode = "active"`` flips submission on **on the live object**;
  ``runner.mode = "killed"`` flips it off again.
* :class:`~.runner.BidLogEntry` — what the sink receives: the answer verbatim, the mode, the
  trust posture, and the `rationale`. The rationale lives HERE and not on the `Bid`, which is
  ``extra="forbid"`` and has no such field.
* :class:`~.runner.TrustPosture` / :class:`~.runner.TrustSignal` — what
  ``runner.ingest_trust_event(...)`` accumulates, and the only thing an injected trust event is
  allowed to move.

**This module is sealed state.** `.importlinter`'s `c3-exchange-cannot-read-envelopes` contract
names `store_agent.modes` in `forbidden_modules`: nothing under `apps/exchange/src` may import
it, directly or transitively. The contract was already enforceable before this package had
content — an empty ``__init__.py`` is still a node in grimp's graph — so what changed here is
only that there is now something worth forbidding. The exchange solicits bids over the protocol;
it never reaches into a store's own activation state.

Import path: both ``store_agent.modes`` (the flat-src namespace, via ``.pkgroot``) and
``packages.store_agent.src.modes`` (the repo-root dotted path the frozen suite binds to) resolve
here, and **they are the same module object** — see :data:`CANONICAL_MODULE` and the identical
arrangement in :mod:`store_agent.runtime`. Two spellings of one file would otherwise be two
module objects holding two `AgentRunner` classes, so ``isinstance(runner, AgentRunner)`` in a
caller that spelled the import the other way would answer False about a genuine runner.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from .runner import (
    COLLABORATOR_METHODS,
    GUARDED,
    NEUTRAL,
    REINFORCED,
    SUBMITTING_MODES,
    AgentRunner,
    BidLogEntry,
    TrustPosture,
    TrustSignal,
    rationale_for,
)

#: The one spelling that owns the module objects. `packages.store_agent.src.modes` is the same
#: files reached through the frozen suite's namespace alias; importing it yields *this* module.
CANONICAL_MODULE = "store_agent.modes"

#: The submodules aliased alongside the package, so `packages.store_agent.src.modes.runner` is
#: also one module object rather than a second copy of `AgentRunner`.
_ALIASED_SUBMODULES = ("runner",)


def _install_canonical_alias() -> bool:
    """Point this module's name at :data:`CANONICAL_MODULE`. True when the alias was installed.

    Runs at the end of import, under the non-canonical spelling only. The import machinery reads
    ``sys.modules[name]`` back after executing a module, so replacing the entry here is what
    makes ``from packages.store_agent.src.modes import AgentRunner`` hand back the canonical
    class rather than a second one.

    Defensive at every step, for the reason :mod:`store_agent.runtime` gives: the canonical
    spelling depends on ``.pkgroot`` being importable, which is a packaging fact this module does
    not control. If it is not importable, or resolves to a different file, this does nothing and
    both spellings keep working as before.
    """
    if __name__ == CANONICAL_MODULE:
        return False
    try:
        canonical = importlib.import_module(CANONICAL_MODULE)
    except ImportError:  # pragma: no cover - depends on how the caller set sys.path
        return False
    here = getattr(canonical, "__file__", None)
    if here is None or Path(here).resolve() != Path(__file__).resolve():
        # A different file answering to that name is not this package; leave it alone.
        return False  # pragma: no cover - would mean two store_agent trees on one path
    sys.modules[__name__] = canonical
    for name in _ALIASED_SUBMODULES:
        submodule = getattr(canonical, name, None)
        if submodule is not None:
            sys.modules[f"{__name__}.{name}"] = submodule
    return True


__all__ = [
    "CANONICAL_MODULE",
    "COLLABORATOR_METHODS",
    "GUARDED",
    "NEUTRAL",
    "REINFORCED",
    "SUBMITTING_MODES",
    "AgentRunner",
    "BidLogEntry",
    "TrustPosture",
    "TrustSignal",
    "rationale_for",
]

# Last, deliberately: the module is fully built either way, so a caller that arrived through the
# non-canonical spelling gets a complete module whether or not the alias could be installed.
_ALIAS_INSTALLED = _install_canonical_alias()
