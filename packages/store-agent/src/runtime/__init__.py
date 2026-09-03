"""The hosted store advocate's runtime — ``BidRequest -> Bid | Decline`` (R8, R10, S4, S5).

The public surface, in the order it is used:

* :func:`~.bidding.bid` — the entry point. ``bid(request, context)`` builds its own tool-hook
  facade; ``bid(request, context, hooks=hooks)`` uses the one you hand it, so the call log and
  the emitted claims are yours to inspect. Every fact in the answer came out of a hook, and the
  whole `Bid` — not its `claims` list — goes through `enforce_bid_provenance` before it is
  returned.
* :class:`~.decline.Decline` / :class:`~.decline.DeclineReason` — the other half of the answer.
  Not bidding is a decision the exchange must be able to read, count and explain.
* :func:`~.context.assemble_context` / :class:`~.context.AuctionContext` — the store context
  joined to one request, ordered static-first for prompt caching (DESIGN §Decisions). The bid
  path itself calls no LLM; the layout is for the pitch that travels beside the bid.

Import path: both ``store_agent.runtime`` (the flat-src namespace, via ``.pkgroot``) and
``packages.store_agent.src.runtime`` (the repo-root dotted path the frozen suite binds to)
resolve here, and **they are the same module object** — see :data:`CANONICAL_MODULE` and the
identical arrangement in :mod:`store_agent.hooks`. Two spellings of one file would otherwise be
two module objects holding two `Decline` classes, so ``isinstance(answer, Decline)`` in a caller
that spelled the import the other way would answer False about a genuine decline and the caller
would treat a refusal as a bid.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from .bidding import (
    AGENT_VERSION,
    IN_STOCK_KEY,
    LIST_PRICE_KEY,
    PERCENTAGE,
    bid,
)
from .context import (
    INTRO_DISCOUNT_KEY,
    OFFER_EXPIRES_AT_KEY,
    AuctionContext,
    HardConstraint,
    assemble_context,
    satisfies,
)
from .decline import Decline, DeclineReason, is_decline

#: The one spelling that owns the module objects. `packages.store_agent.src.runtime` is the same
#: files reached through the frozen suite's namespace alias; importing it yields *this* module.
CANONICAL_MODULE = "store_agent.runtime"

#: The submodules aliased alongside the package, so `packages.store_agent.src.runtime.decline`
#: is also one module object rather than a second copy of `Decline`.
_ALIASED_SUBMODULES = ("bidding", "context", "decline")


def _install_canonical_alias() -> bool:
    """Point this module's name at :data:`CANONICAL_MODULE`. True when the alias was installed.

    Runs at the end of import, under the non-canonical spelling only. The import machinery reads
    ``sys.modules[name]`` back after executing a module, so replacing the entry here is what
    makes ``from packages.store_agent.src.runtime import Decline`` hand back the canonical class
    rather than a second one.

    Defensive at every step, for the reason :mod:`store_agent.hooks` gives: the canonical
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
    "AGENT_VERSION",
    "CANONICAL_MODULE",
    "INTRO_DISCOUNT_KEY",
    "OFFER_EXPIRES_AT_KEY",
    "IN_STOCK_KEY",
    "LIST_PRICE_KEY",
    "PERCENTAGE",
    "AuctionContext",
    "Decline",
    "DeclineReason",
    "HardConstraint",
    "assemble_context",
    "bid",
    "is_decline",
    "satisfies",
]

# Last, deliberately: the module is fully built either way, so a caller that arrived through the
# non-canonical spelling gets a complete module whether or not the alias could be installed.
_ALIAS_INSTALLED = _install_canonical_alias()
