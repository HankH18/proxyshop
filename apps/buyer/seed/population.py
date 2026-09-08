"""The seeded buyer population: cohorts of realistic shoppers, coarsened by the real coarsener.

What this generates, and what it deliberately does not
------------------------------------------------------
It generates **accounts** — region, order history, spend — and then hands each one to
:func:`buyer_svc.profile.build_buckets`, the same function ``GET /buyer/profile`` runs. The
corpus that gets written keeps only what the corpus is for: the five coarse buckets. The
accounts themselves, and the synthetic email addresses drawn beside them, exist for the length
of one function call and are never stored anywhere.

That is not tidiness, it is the point of the table. ``app.buyer_accounts`` is the store-visible
window; a corpus that also carried the accounts behind it would be a buyer database with a
window attached, and the first person to need "just the region" would read it from there.

Why the real coarsener and not hand-written buckets
----------------------------------------------------
Hand-written buckets are a fixture that *resembles* output. Running the product's own
coarsener means the corpus is, by construction, a set of values ``GET /buyer/profile`` can
actually emit — a hand-written ``"budget_band": "mid"`` would sail into the table and out to a
store, and no test that only read the corpus would notice. It also puts every row through R5's
identity backstop (:func:`~buyer_svc.profile.identity_leaks`, which ``build_buckets`` runs),
so a generated account whose category slug happened to spell a fragment of its own email is
refused here rather than published.

:func:`~buyer_svc.profile.build_buckets` and not
:func:`~buyer_svc.profile.build_profile`: ``build_profile`` pairs buckets with a pseudonym and
applies the configured release floor, and this producer can do neither. The pseudonym is
*derived from* the buckets (:mod:`apps.buyer.seed.chain`), so asking for it first is circular;
and the floor is a property of a release, which is the served route's job
(:mod:`buyer_svc.window.routes`), not the corpus's. What is stored is rung 0, exactly what a
single login publishes.

Cohorts, and why the population is not forty unrelated buyers
--------------------------------------------------------------
The window is released under a k-anonymity floor: a bucket combination held by fewer than
``k`` buyers is withheld from a store rather than generalised, because the served route has
only the coarse buckets and not the accounts behind them, and suppression is the only sound
move available to it (see :func:`buyer_svc.window.routes.release`).

A population of forty *distinct* profiles would therefore release nothing at all, and a corpus
that demonstrates the window by being unusually uniform would be a lie of a different kind. So
the population is built as :data:`COHORTS` — groups of shoppers who genuinely look alike to a
store — and every member of a cohort is a different account: different order totals inside the
band, a different long-tail purchase that the ``CATEGORY_LIMIT`` truncation drops. What they
share is what a store is allowed to see, which is what an equivalence class *is*.

Determinism, and exactly what the seed does
--------------------------------------------
:func:`draw` is a pure function of its arguments — ``random.Random`` seeded explicitly, never
``SystemRandom`` and never the module-level generator, because a corpus is checked in and has
to reproduce. That is the opposite of the requirement on
``apps/buyer/svc/tests/test_repro_open_tickets.py``'s draws, where a fixed address set would
let a repair key on it.

What the seed moves is narrower than it looks, and the difference is worth stating because it
is the property that makes the committed bytes trustworthy: **the seed moves the accounts and
not the released buckets.** It picks order totals inside each cohort's band and nothing else,
and a band is chosen so that every total inside it coarsens to the same
:data:`~buyer_svc.profile.BUDGET_BANDS` label — so a corpus drawn at any seed is the same
corpus. The released set is a function of :data:`COHORTS` and of the coarseners, full stop, and
``test_the_corpus_does_not_depend_on_the_draw`` asserts that across several seeds.

The seed still earns its place. :class:`CohortCollapsed` is evaluated against the *accounts*,
so a cohort whose band straddled a bucket boundary would collapse for some draws and not
others; a fixed, recorded seed is what makes that canary reproducible rather than
intermittent, and what lets a reader re-run the exact draw the committed corpus was checked
against.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .chain import CHAIN_GENESIS, mint

try:  # pragma: no cover - exercised by whichever spelling resolves in this process
    from buyer_svc.profile import BUCKET_KEYS, CATEGORY_LIMIT, build_buckets
except ImportError:  # pragma: no cover - the repo-root spelling
    from apps.buyer.svc.src.profile import (  # type: ignore[no-redef]
        BUCKET_KEYS,
        CATEGORY_LIMIT,
        build_buckets,
    )

__all__ = [
    "COHORTS",
    "DEFAULT_MEMBERS_PER_COHORT",
    "DEFAULT_SEED",
    "Cohort",
    "CohortCollapsed",
    "Row",
    "census",
    "draw",
]


class CohortCollapsed(Exception):
    """A cohort's members did not coarsen to one equivalence class.

    Loud, and never downgraded to a warning. A cohort whose members disagree is a cohort the
    release floor will suppress, so a corpus that shipped one would produce a window that
    served fewer rows than it holds — visible only as "the demo looks thin", which is exactly
    the failure that gets diagnosed as something else.
    """


@dataclass(frozen=True)
class Cohort:
    """One group of shoppers who look alike to a store.

    Attributes:
        name: what this cohort is, in words. Recorded in the corpus census so a reader does
            not have to infer the intent from five bucket values.
        region: the account's ``region`` field, an ISO-shaped code
            :func:`~buyer_svc.profile.coarsen_region` will accept.
        ranked: ``(slug, order_count)`` pairs, highest first. The first
            ``CATEGORY_LIMIT`` slugs become ``category_affinity``; counts must be
            distinct and ``>= 2`` so the ranking cannot be reordered by the tail purchase.
        band: ``(low, high)`` the member's order totals are drawn inside. Chosen so the
            median lands in one canonical :data:`~buyer_svc.profile.BUDGET_BANDS` label.
        tail: long-tail slugs, one per member, bought once. Present so members differ as
            accounts while agreeing as profiles — and therefore used **only** when the cohort
            already ranks ``CATEGORY_LIMIT`` slugs, because that is what guarantees the
            truncation drops it. On a cohort ranking fewer, the tail would land in the free
            affinity slot and every member would coarsen differently, which
            :class:`CohortCollapsed` catches and which this rule prevents.
    """

    name: str
    region: str
    ranked: tuple[tuple[str, int], ...]
    band: tuple[float, float]
    tail: tuple[str, ...] = ()


#: The population. Eight cohorts spanning every frequency tier and most of the budget bands,
#: with regions on three continents — a window a store can actually reason about, rather than
#: forty buyers who all shop the same way.
#:
#: The empty cohort is deliberate and is the one most likely to be "cleaned up": a first-time
#: buyer coarsens to ``budget_band=None, category_affinity=[], frequency_tier='none',
#: first_time=True``, which is the single most common real profile a live network sees and the
#: one carrying the least information. A corpus without it would show a store only the buyers
#: it already knows something about.
COHORTS: tuple[Cohort, ...] = (
    Cohort(
        name="Pacific-Northwest trail regulars",
        region="US-OR",
        ranked=(("trail-runners", 4), ("wool-socks", 3), ("hiking-boots", 2)),
        band=(52.0, 96.0),
        tail=("rain-shells", "trekking-poles", "camp-stoves", "wool-beanies", "dry-bags"),
    ),
    Cohort(
        name="Bay Area home-office outfitters",
        region="US-CA",
        ranked=(("espresso-gear", 5), ("desk-lamps", 3)),
        band=(110.0, 240.0),
        tail=("cable-trays", "monitor-arms", "task-chairs", "keycaps", "desk-mats"),
    ),
    Cohort(
        name="UK camera hobbyists",
        region="GB",
        ranked=(("camera-lenses", 2),),
        band=(520.0, 960.0),
        tail=("tripods", "lens-caps", "camera-bags", "filters", "flash-diffusers"),
    ),
    Cohort(
        name="Berlin board-game and vinyl collectors",
        region="DE-BE",
        ranked=(("board-games", 6), ("media-vinyl", 4)),
        band=(12.0, 46.0),
        tail=("card-sleeves", "record-brushes", "dice-trays", "poster-frames", "slipmats"),
    ),
    Cohort(
        name="British Columbia gardeners with pets",
        region="CA-BC",
        ranked=(("garden-tools", 5), ("home-textiles", 4), ("pet-treats", 3)),
        band=(260.0, 480.0),
        tail=("seed-trays", "hose-reels", "cat-towers", "throw-blankets", "bird-feeders"),
    ),
    Cohort(
        name="Paris skincare occasionals",
        region="FR",
        ranked=(("skincare-serums", 3), ("hair-care", 2)),
        band=(115.0, 235.0),
        tail=("cotton-pads", "hair-clips", "face-masks", "lip-balms", "brush-sets"),
    ),
    Cohort(
        name="Tokyo one-off big-ticket shoppers",
        region="JP",
        ranked=(("running-shoes", 2),),
        band=(1100.0, 2400.0),
        tail=("shoe-trees", "insoles", "sports-socks", "gym-bags", "water-bottles"),
    ),
    Cohort(
        name="Sydney first-timers",
        region="AU-NSW",
        ranked=(),
        band=(0.0, 0.0),
        tail=(),
    ),
)

#: Members per cohort. Five, so the released window still clears a floor of 2, 3, 4 or 5
#: without the corpus having to be regenerated to demonstrate one — the floor is a deployment
#: knob (``PROXYSHOP_BUYER_STORE_WINDOW_K``) and a corpus that only worked at the default
#: would make the knob untestable against real data.
DEFAULT_MEMBERS_PER_COHORT = 5

#: The seed the checked-in corpus was produced at, recorded in ``collection.json`` as well, so
#: ``python -m apps.buyer.seed run`` with no arguments reproduces the committed bytes.
#:
#: It draws the ACCOUNTS and not the released buckets — see this module's "Determinism"
#: heading. A reader who changes it gets different synthetic order totals and, if the cohort
#: table is sound, byte-identical output.
DEFAULT_SEED = 20260907

#: One corpus row as :func:`draw` builds it: exactly what reaches ``app.buyer_accounts``, plus
#: the cohort name for the census. Readers take the wider ``Mapping`` — a row loaded back off
#: disk is one, and a reader that insisted on ``dict`` would make the round trip a type error.
Row = dict[str, Any]


def _account(cohort: Cohort, member: int, rng: random.Random) -> dict[str, Any]:
    """One member's account. Synthetic, complete, and never stored.

    ``email`` is present so that :func:`~buyer_svc.profile.build_buckets` runs R5's identity
    backstop against a real identity value rather than against an account that has none. It is
    drawn from ``example.invalid`` — RFC 6761 reserves ``.invalid`` precisely so a generated
    address cannot be a real mailbox — and it is discarded when this function's caller returns.
    """
    orders: list[dict[str, Any]] = []
    for slug, count in cohort.ranked:
        for _ in range(count):
            orders.append({"category": slug, "total": round(rng.uniform(*cohort.band), 2)})
    if cohort.tail and len(cohort.ranked) >= CATEGORY_LIMIT:
        # Only where the affinity list is already full. See `Cohort.tail`: on a shorter cohort
        # this single purchase would BE the third affinity slot, so every member would publish
        # a different one and the cohort would stop being a cohort.
        orders.append(
            {
                "category": cohort.tail[member % len(cohort.tail)],
                "total": round(rng.uniform(*cohort.band), 2),
            }
        )
    return {
        "email": f"{cohort.region.lower().replace('-', '')}{member:02d}@example.invalid",
        "region": cohort.region,
        "orders": orders,
    }


def _class_of(buckets: Mapping[str, Any]) -> tuple[Any, ...]:
    """The five facets as a hashable tuple, in :data:`BUCKET_KEYS` order."""
    return tuple(
        tuple(buckets[key]) if isinstance(buckets.get(key), list) else buckets.get(key)
        for key in BUCKET_KEYS
    )


def draw(
    *,
    seed: int = DEFAULT_SEED,
    members: int = DEFAULT_MEMBERS_PER_COHORT,
    cohorts: Sequence[Cohort] = COHORTS,
) -> tuple[list[Row], str]:
    """``(rows, chain_head)`` for the whole population. Pure in its arguments.

    Rows come out in cohort order, member order, which is the order the chain is built in and
    the order the corpus is written in. ``ordinal`` is that position and is part of each link,
    so the sequence is not merely the file's layout — it is inside the digest.

    Raises:
        CohortCollapsed: two members of one cohort coarsened to different buckets. The whole
            point of a cohort is that they do not.
        ValueError: ``members`` is below 1.
    """
    if members < 1:
        raise ValueError(f"a cohort needs at least one member, got {members}")

    rng = random.Random(seed)
    rows: list[Row] = []
    previous = CHAIN_GENESIS

    for cohort in cohorts:
        released: list[tuple[Any, ...]] = []
        for member in range(members):
            buckets = build_buckets(_account(cohort, member, rng)).model_dump()
            released.append(_class_of(buckets))
            if released[0] != released[-1]:
                raise CohortCollapsed(
                    f"cohort {cohort.name!r} member {member} coarsens to {released[-1]!r}, "
                    f"and member 0 to {released[0]!r}. Every member of a cohort must share one "
                    "equivalence class or the release floor will suppress the cohort; adjust "
                    "the cohort's `ranked` counts or its `band` so the coarseners agree."
                )
            pseudonym, previous = mint(previous, len(rows), buckets)
            rows.append({"buckets": buckets, "cohort": cohort.name, "pseudonym": pseudonym})

    return rows, previous


def census(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """``{cohort name: rows}``, every cohort in :data:`COHORTS` present.

    Cohorts with no rows are listed at zero rather than omitted: an absent key and a key at
    zero read the same to a careless eye and mean different things — ``services/sim/seed``'s
    ``observation_census`` makes the same choice for the same reason.
    """
    counted = dict.fromkeys((cohort.name for cohort in COHORTS), 0)
    for row in rows:
        name = str(row.get("cohort", ""))
        counted[name] = counted.get(name, 0) + 1
    return counted
