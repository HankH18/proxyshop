"""The seed-data script: a population of simulated returning shoppers, run once and stored.

This package is **not a service**. Nothing here is hosted, nothing here opens a listening
socket a deployment would route to, and nothing here is in any container image. It is a
program a person runs deliberately, from a checkout, to manufacture the one signal the trust
engine has never received — and then the run is over and what survives is a directory of
JSON.

Three things it is, in the order they matter.

1. A script, and the container is honest about it
--------------------------------------------------
``services/sim/src/`` holds :mod:`sim` — the dishonest-actor simulation whose ``CMD`` is
``python -m sim --json`` — and ``services/sim/Dockerfile`` ships that directory whole. This
package lives in ``services/sim/seed/``, one directory across, and is therefore **outside the
image's COPY set by construction** rather than by a list somebody has to remember to update.

That placement is load-bearing and it is the repair for a real defect. While these modules
sat in ``services/sim/src/``, ``COPY services/sim/src/`` put them in an image that could not
run them: :mod:`seed.local_stack` imports ``buyer_svc``, :mod:`seed.population` imports
``exchange.ranking``, and the image ships neither — so
``test_t301_every_image_copy_set_covers_every_first_party_import_it_ships`` was red on
exactly that. Shipping the missing packages was tried and is worse: ``buyer_svc`` cascades to
``claim_verification`` and ``llm``, ``exchange.ranking`` cascades to ``exchange.retrieval``
and the ``ingest`` graph library, and the fix reverses T-310's deliberate narrowing to drag a
graph database client into an image that opens no socket. The container's ``CMD`` does not
need any of it. So the harness moved out instead, ``COPY services/sim/src/`` went back to
copying a directory whose every module the image can actually import, and the import
namespace comes from the tracked ``.pkgroot/seed`` symlink exactly as every other member's
does.

``seed`` imports ``sim`` (for :func:`sim.runner.build_roster` and the episode clock) and
``sim`` imports nothing from ``seed``. That direction is what keeps the image's closure a
straight line from its ``CMD``.

2. Run it once, store it, replay it
------------------------------------
::

    python -m seed run       # drive the real routes once; write services/sim/seed-data/
    python -m seed replay    # reproduce every posture from the stored bytes, offline
    python -m seed posture   # every store's posture with the seeded data, and without it

The stored artifact and its provenance record are :mod:`seed.store`; the format follows
``fixtures/real-catalogs/collection.json``, which is this repository's existing
collect-once-replay-forever convention, rather than inventing a second one. A replay
re-simulates nothing: it projects the stored ledger chain through the platform's own scorer
at the ``as_of`` the artifact recorded, so it is offline, needs no service, and returns the
same bytes every time.

3. The live switch — and what it actually is
---------------------------------------------
**The seam is ``POST /buyer/feedback``, and there is nothing else.**

This is measured, not asserted. ``apps/buyer/svc/src/feedback/routes.py`` contains **zero**
occurrences of "sim", "seed" or "simulat" in any case — the served route has no notion that a
simulator exists, no branch on the caller, and no field only a simulator fills in. It reads an
order body, checks R14's routed-buyer gate, and writes a ``feedback`` ledger event. The
population drives ``POST /buyer/feedback/prompt`` and ``POST /buyer/feedback`` over HTTP with
an ordinary JSON body, never :func:`buyer_svc.feedback.submit_feedback` behind them, so
**every hop after the route is already the production path**: the once-per-order book, the
composition root's ledger sink, ``POST /events``, the hash chain, the projection, the scorer,
``GET /snapshot``, and the exchange's eligibility read off it. None of that changes on the day
real customers arrive, because none of it can tell who called.

So going live is:

* **server side: nothing.** Not a flag, not a config value, not a branch. Stop running
  ``python -m seed run`` and the seeded answers stop; real answers arrive through the same
  door and are processed by the same code.
* **client side: mount the prompt.** ``apps/buyer/app/feedback/feedback.ts`` already speaks
  to exactly these two routes (``fetchFeedbackPrompt``, ``submitFeedback``, pointed at
  ``PROMPT_PATH`` and ``SUBMIT_PATH``) and ``FeedbackPromptView.tsx`` already renders the
  question, with tests. **They are not mounted on any page** — measured: the only importer of
  ``FeedbackPromptView`` in the whole app is its own test file. That is one gap in
  ``apps/buyer/app`` (outside this package's scope), and it is the single thing standing
  between this seam and a real shopper using it. It is stated here rather than glossed,
  because "the UI exists" and "the UI is reachable" are different claims and only the first
  one is true today.

The door that only opens one way
--------------------------------
Seeded feedback and real feedback must stay **distinguishable on the ledger, forever**. If
they are not, then on the first day real customers arrive there is no way to separate
manufactured reputation from earned reputation — retroactively, permanently, in an
append-only hash chain with no delete. Every observation this package writes carries
:data:`seed.shoppers.SIMULATED_ORDER_PREFIX` on its ``order_ref``, which the buyer service
copies verbatim onto the sealed event, and :mod:`seed.provenance` is the reader that computes
any store's posture with those observations and without them. That module holds the argument;
it is the difference between seed data and fake data and it is worth reading before changing
anything here.

Where it may point
------------------
Loopback, always, unless an operator passes ``--allow-remote`` on the command line. There is
no environment variable and no config file that can move the default, because this tool
manufactures trust signal and a default that can be moved from outside is a default that will
be. :mod:`seed.targets` holds the guard and the reasoning.
"""

from __future__ import annotations

__all__: list[str] = []
