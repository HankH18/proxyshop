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

Those two modules, this one and ``__main__`` are the whole of what
``services/sim/Dockerfile`` ships, and the ``COPY services/sim/src/`` that ships them is a
plain directory copy because everything in this directory is something the image's ``CMD``
can actually run.

**The R14 seeding harness is a different program and it is not here.** It lives one
directory across in ``services/sim/seed/`` (import name :mod:`seed`, via ``.pkgroot/seed``)
because it stands up the real buyer service and the real trust service in process and drives
``POST /buyer/feedback`` through them — so it needs ``buyer_svc``, ``claim_verification``,
``llm`` and ``exchange.ranking``, four subtrees this image deliberately does not carry. It is
a script somebody runs from a checkout (``python -m seed run``), not a service; :mod:`seed`
carries the argument, including why shipping it would reverse T-310's narrowing and drag the
ingest graph library into an image that opens no socket. ``seed`` imports ``sim``; ``sim``
imports nothing from ``seed``, which is what keeps this image's closure a straight line from
its ``CMD``.

The import namespace is provided by the tracked ``.pkgroot/sim`` symlink to this directory,
so ``services/sim/src/x.py`` is ``sim.x``. The frozen acceptance suite reaches the same
modules by their dotted path (``services.sim.src.dishonest``); both spellings resolve here.
"""

from __future__ import annotations

__all__: list[str] = []
