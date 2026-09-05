"""Pixel/webhook reconciliation: the webhook is authoritative, the pixel is a witness.

Owned by T-061 (scope ``apps/trust/src/reconcile/**``). Importable as
``apps.trust.src.reconcile`` (repo-root path, how the frozen acceptance suite reaches it) and
as ``trust.reconcile`` (via ``.pkgroot/trust``, how member packages reach it); the block at
the bottom makes both spellings resolve to the same objects — see :mod:`trust._shared._binding`.

===========================  ===========================================================
:func:`reconcile`            a checkout event stream -> one ``reconciled`` event/order.
:func:`reconciled_event`     build that event for one already-joined order.
:data:`RECONCILED_KIND`      ``"reconciled"``, already in the frozen 18-kind vocabulary.
:func:`reconciled_observations`  those verdicts -> the trust observations the scorer reads.
:func:`observation_events`   the same, as ``offer_integrity`` events the ledger replays.
:func:`discount_codes_of`    the single-use codes an event names, in every real spelling.
:data:`CODE_BRIDGE_KINDS`    ``code_created`` / ``checkout_redirect`` — read for a join key
                             and nothing else.
===========================  ===========================================================

The join is not only on tokens. The exchange's ``checkout_token`` and the merchant's are two
unrelated values for one checkout — the exchange mints its own after calling the merchant and
transmits it nowhere — so the offer and the order meet on the single-use discount code, which
:data:`CODE_BRIDGE_KINDS` carries alongside the exchange's token. :func:`discount_codes_of`
documents what that key is worth and the three guards that keep a weaker key honest.

The one rule (R4): **every integrity comparison derives from the ``order_paid`` webhook.**
The pixel is recorded and never consulted. :mod:`.engine` carries the full rationale and
both directions of getting it wrong.

No score math (T-061 non-goal). This package decides what *happened*; :mod:`trust.scoring`
decides what that is worth. The translation above is the seam between the two, and it is
here rather than in ``trust.ledger`` because ``observations_from_events`` already defines the
shape a trust observation arrives in — so the emitter meets the consumer, not the reverse.
"""

from __future__ import annotations

import importlib as _importlib
from pathlib import Path as _Path

# --- The two spellings are SEQUENCED before anything else runs (the T-126 pattern) -------
# `.pkgroot/trust` makes this file reachable under two dotted names and Python executes it
# once per name. The binding at the bottom runs LAST while the eager imports run FIRST, so
# without this wait a race builds two copies of every submodule and the binding's
# `setdefault` then silently declines to fix it. Measured for `trust.ledger`; not rediscovered
# here. See `apps/trust/src/ledger/__init__.py` for the full rationale and the boundary it
# does not fix.
_SPELLINGS: tuple[str, ...] = ("trust.reconcile", "apps.trust.src.reconcile")
_PRIMARY_SPELLING = _SPELLINGS[0]

if __name__ in _SPELLINGS and __name__ != _PRIMARY_SPELLING:
    try:
        _importlib.import_module(_PRIMARY_SPELLING)
    except ImportError:
        pass

# E402 below is the point of the block above: the sequencing has to run BEFORE the first
# relative import, because it is the eager imports that build the second copy.
from .._shared._binding import bind_submodules as _bind_submodules  # noqa: E402
from .engine import (  # noqa: E402
    ACCEPTED_KIND,
    CODE_BRIDGE_KINDS,
    DISCOUNT_TOLERANCE,
    DISHONORED_OBSERVATION_TYPE,
    HONORED_OBSERVATION_TYPE,
    INCOMPARABLE_OBSERVATION_TYPE,
    OBSERVATION_KIND,
    PIXEL_KIND,
    PRICE_TOLERANCE,
    RECONCILED_DIMENSIONS,
    RECONCILED_KIND,
    WEBHOOK_KIND,
    ReconciliationInputError,
    discount_codes_of,
    observation_events,
    reconcile,
    reconciled_event,
    reconciled_observations,
)

__all__ = [
    "ACCEPTED_KIND",
    "CODE_BRIDGE_KINDS",
    "DISCOUNT_TOLERANCE",
    "DISHONORED_OBSERVATION_TYPE",
    "HONORED_OBSERVATION_TYPE",
    "INCOMPARABLE_OBSERVATION_TYPE",
    "OBSERVATION_KIND",
    "PIXEL_KIND",
    "PRICE_TOLERANCE",
    "RECONCILED_DIMENSIONS",
    "RECONCILED_KIND",
    "WEBHOOK_KIND",
    "ReconciliationInputError",
    "discount_codes_of",
    "observation_events",
    "reconcile",
    "reconciled_event",
    "reconciled_observations",
]


def __dir__() -> list[str]:
    return sorted(__all__)


_REPO_ROOT = _Path(__file__).resolve().parents[4]
_SPELLING_ROOTS: dict[str, _Path] = {
    "trust.reconcile": _REPO_ROOT / ".pkgroot",
    "apps.trust.src.reconcile": _REPO_ROOT,
}

_bind_submodules(__name__, __path__, _SPELLINGS, _SPELLING_ROOTS)
