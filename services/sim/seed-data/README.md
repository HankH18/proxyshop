# Seed data — simulated buyer feedback

**Everything in this directory was manufactured. Not one answer in it came from a person.**

It exists because R14 — post-purchase buyer feedback — is the only signal the trust engine
grades that is *not the store talking about itself*, and the routes that collect it had never
been called: a real prompt needs a shopper who comes back days after delivery, and this system
has no shoppers yet. So a seeded population of simulated returning shoppers was run once
through the real routes, and this is what it produced.

The demo replays these bytes. It does not re-run the simulation.

## What is here

    feedback-population/
      collection.json   provenance: what was generated, when, by what, from which seed,
                        with a sha256 for every other file and the marker a reader needs
      transcript.json   the canonical run. Byte-stable: the same seed produces these exact
                        bytes in any process, on any machine
      ledger.jsonl      the hash-chained trust events the served routes actually wrote,
                        one per line, in insertion order
      roster.json       the stores, so a replay needs this directory and nothing else

The shape follows `fixtures/real-catalogs/collection.json`, which is this repository's
existing collect-once-replay-forever convention, rather than inventing a second one.

## Using it

    python -m seed replay     # reproduce every posture from these bytes, offline
    python -m seed posture    # every store's posture WITH the seeded data, and WITHOUT it
    python -m seed run        # produce a new corpus (overwrites this one)

`replay` opens no socket, stands up no service, and re-simulates nothing: it projects
`ledger.jsonl` through the platform's own scorer at the `as_of` recorded in
`collection.json`. Its exit status is the finding — **0** means the postures it recomputed
from the chain match the ones the served `GET /snapshot` returned during the run, and **1**
means the chain and the transcript disagree and the corpus should not be used.

`load()` verifies every digest in `collection.json` before returning anything. A corpus whose
bytes and whose provenance record disagree is worse than no corpus: it looks accounted for.

## Seeded, not fake

Every observation here carries a durable marker, and that is the difference.

`order_ref` begins with `sim-fb-`. The buyer service copies `order_ref` **verbatim** onto the
sealed ledger event, so the marker is inside the hash chain: a manufactured observation cannot
be un-marked and an earned one cannot be marked without breaking `GET /events/verify`.

This matters because the ledger is append-only and has no delete. On the day real customers
arrive, either the manufactured observations can still be separated from the earned ones or
they can never be — retroactively, permanently. There is no later repair for the second case.
`python -m seed posture` is the reader, and on this corpus it says:

    store                     with seed     earned   seeded  organic
    store-brightbean           0.468750   0.500000        9        0
    store-harborline           0.519231   0.500000        8        0
    store-northroast           0.490741   0.500000        4        0
    store-secondchance         0.532609   0.500000        7        0
    store-slowreply            0.500000   0.500000        0        0

Read that as: *every point of every posture here is manufactured, and with the seed removed
each store falls back to the low-data prior, because none of them has earned anything yet.*
Once real feedback exists the "earned" column starts moving on its own and the two stay
separable for good.

## Going live

**Server side: nothing changes.** `apps/buyer/svc/src/feedback/routes.py` contains zero
occurrences of "sim", "seed" or "simulat" — the served route has no notion that a simulator
exists, no branch on the caller, and no field only a simulator fills in. The seeder drives
`POST /buyer/feedback/prompt` and `POST /buyer/feedback` over HTTP with an ordinary JSON
body, so every hop after the route — the once-per-order book, the ledger sink, `POST /events`,
the hash chain, the projection, the scorer, `GET /snapshot`, the exchange's eligibility read —
is already the production path. Stop running `python -m seed run` and the seeded answers stop;
real answers arrive through the same door and are processed by the same code.

**Client side: mount the prompt.** `apps/buyer/app/feedback/feedback.ts` already speaks to
exactly those two routes and `FeedbackPromptView.tsx` already renders the question, with
tests — but **nothing in the app mounts them**: the only importer of `FeedbackPromptView` is
its own test file. That is the one real gap between this seam and a shopper using it, it lives
in `apps/buyer/app`, and it is stated here rather than glossed because "the UI exists" and "a
shopper can reach it" are different claims and only the first is true today.

The code that produced this directory is `services/sim/seed/`. It is a script, not a service:
nothing in it is hosted and nothing in it ships in a container image. `services/sim/seed/__init__.py`
carries the full argument, including why the simulation image deliberately does not carry it.
