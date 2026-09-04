"""ProxyShop's seeded market simulator — T-081.

One seed drives the whole network: the approved fixture intent goes to the approved store
roster, the exchange runs a real auction over real bids, a winner is accepted through the
real checkout port, and the manifest's scripted dishonest store attacks the real trust
engine for as many episodes as a human approved a budget for.

Two modules, and the split between them is the point:

* :mod:`sim.dishonest` — the adversary. It holds **no behaviour of its own**: every kind,
  dimension, observation type and claim type it emits is read out of
  ``fixtures/manifest.json``. SPEC A3 is why — "the trust engine catches the dishonest
  store" is circular if the attacker and the grader are written by the same hand, so the
  attack lives in a third document a human signs.
* :mod:`sim.runner` — the market. It drives ``exchange``, ``trust``, ``contracts`` and
  ``fixtures.generator`` through their public entry points, never a copy of them, because
  the only thing a simulator is worth is the code it actually exercises.

``python -m sim`` runs it headless and bounded (see :mod:`sim.__main__`); its exit status
says whether the dishonest store was caught inside the approved budget.

The import namespace is provided by the tracked ``.pkgroot/sim`` symlink to this directory,
so ``services/sim/src/x.py`` is ``sim.x``. The frozen acceptance suite reaches the same
modules by their dotted path (``services.sim.src.dishonest``); both spellings resolve here.
"""

from __future__ import annotations

__all__: list[str] = []
