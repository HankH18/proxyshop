"""The configurable k-anonymity floor SPEC promises and nothing implemented (T-070, R5).

SPEC §Non-goals reads: *"No k-anonymity enforcement beyond a configurable floor (default 1 in
fixtures; production knob documented)"*, and SPEC A6 restates it — ``k`` defaults to 1 in
fixtures, production gets a knob. The default half shipped; the knob did not. There was no
configurable anything: the floor was 1 because nothing could set it to anything else.

So this suite drives two claims and keeps them apart, because conflating them is how a
"privacy fix" gets built on top of a documented non-goal:

1. **The default is unchanged and unenforced.** ``k = 1`` publishes exactly what
   :func:`build_buckets` publishes, value for value, for every account in a 2,400-strong
   generated population — and at that default buyers *are* uniquely identified, which is the
   documented behaviour rather than a defect this suite is trying to hide.
2. **A configured floor is real.** Set the knob above 1 and the release genuinely satisfies
   it: over the same generated population, and over an adversarial one drawn to make every
   buyer unique, ``min(class size) >= k``.

The second claim is asserted as a joint quantity over a **generated population**, never
against a small fixture. A degenerate implementation can pass a k-floor test measured against
thirty accounts while fingerprinting everywhere else — it generalises where the fixture looks
and nowhere else. The population is what closes that door, and the class-count bounds on
either side are what stop the opposite cheat, an implementation that "anonymises" by
publishing one constant profile for everybody.

Every product import happens inside a test body, matching the rest of this suite.
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Any

import pytest

#: Big enough that a class of five is a real class rather than a small-sample artefact, and
#: that an implementation which generalises only where a fixture looks has nowhere to hide.
POPULATION = 2400

#: Merchandising vocabulary with a long tail: recognisable slugs and strings no closed
#: taxonomy could know. Deliberately messier than anything the implementation's tables hold.
_CATEGORIES = (
    "running-shoes",
    "trail-gear",
    "espresso",
    "cookware",
    "headphones",
    "tents",
    "skincare",
    "outerwear",
    "yoga-mats",
    "dog-food",
    "vinyl-records",
    "board-games",
    "hiking-boots",
    "cold-brew",
    "desk-lamps",
    "wool-socks",
    "camera-lenses",
    "climbing-rope",
    "bath-towels",
    "sunglasses",
    "artisanal-hot-sauce",
    "zzz-unknown-thing",
    "widgets",
    "reclaimed-teak-sideboards",
    "single-origin-tea",
    "kids-scooters",
    "cast-iron",
    "merino-baselayers",
    "bike-tools",
    "field-notebooks",
)

#: Regions shaped like a real directory's: ISO codes the coarsener accepts, and junk it must
#: refuse.
_REGIONS = (
    "US-OR",
    "US-CA",
    "US-NY",
    "US-TX",
    "GB-ENG",
    "GB-SCT",
    "DE",
    "FR",
    "NL",
    "SE",
    "JP",
    "CA-ON",
    "CA-BC",
    "AU-NSW",
    "BR",
    "IN-KA",
    "ES",
    "IT",
    "MX",
    "ZA",
    None,
    "97205",
    "44 Alder Way, Portland OR 97205",
    17,
)

_SURNAMES = ("Reyes", "Okafor", "Lindqvist", "Marchetti", "Nakamura", "Boateng", "Halvorsen")
_GIVENS = ("Dana", "Samir", "Priya", "Tomas", "Ingrid", "Kwame", "Noor")


def _population(count: int = POPULATION, seed: int = 1932) -> list[dict[str, Any]]:
    """A buyer directory with the variety a real one has.

    Order count, spend level and category basket are drawn *independently*, so the population
    covers cheap frequent buyers and expensive one-off buyers alike rather than the diagonal a
    single "customer value" knob would produce. Identity fields are populated too, so the same
    population exercises the leak backstop.
    """
    rng = random.Random(seed)
    accounts: list[dict[str, Any]] = []
    for index in range(count):
        given = _GIVENS[index % len(_GIVENS)]
        surname = _SURNAMES[(index // 3) % len(_SURNAMES)]
        order_count = rng.choice([0, 1, 2, 3, 5, 8, 13, 21, 34])
        scale = rng.choice([11.0, 38.0, 120.0, 340.0, 880.0, 2400.0])
        basket = rng.sample(_CATEGORIES, k=rng.choice([1, 2, 3, 5, 8]))
        account: dict[str, Any] = {
            "account_id": f"acct-{index:06d}-zz",
            "email": f"{given}.{surname}{index}@example.invalid".casefold(),
            "first_name": given,
            "last_name": surname,
            "phone": f"+1-555-{1000 + index:04d}",
            "address": f"{index + 10} Marlow Crescent, Bristol",
            "postal_code": f"{90000 + index}",
            "region": rng.choice(_REGIONS),
            "orders": [
                {
                    "order_ref": f"ord-{index}-{n}",
                    "total": round(rng.uniform(0.4, 1.9) * scale, 2),
                    "category": rng.choice(basket),
                }
                for n in range(order_count)
            ],
        }
        if rng.random() < 0.3:  # some directories carry a declared band, some do not
            account["budget_band"] = rng.choice(["100-250", "0-50", "1000+", "mid-range", 412.0])
        accounts.append(account)
    return accounts


def _adversarial_population(count: int = 2000, seed: int = 4242) -> list[dict[str, Any]]:
    """A directory built to make rung 0 as unique as it can be.

    Six hundred distinct region codes, spend drawn from a continuum, baskets from the long
    tail: almost every buyer is alone before generalisation. A floor that only holds on
    friendly populations is not a floor.
    """
    rng = random.Random(seed)
    subdivisions = [f"{a}{b}" for a in "ABCDEFGHIJ" for b in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[:6]]
    countries = ("US", "CA", "GB", "DE", "FR", "JP", "AU", "BR", "IN", "NL")
    accounts: list[dict[str, Any]] = []
    for index in range(count):
        accounts.append(
            {
                "email": f"buyer{index}@example.invalid",
                "last_name": _SURNAMES[index % len(_SURNAMES)],
                "region": f"{rng.choice(countries)}-{rng.choice(subdivisions)}",
                "orders": [
                    {
                        "order_ref": f"adv-{index}-{n}",
                        "total": round(rng.uniform(3.0, 4000.0), 2),
                        "category": rng.choice(_CATEGORIES),
                    }
                    for n in range(rng.choice([1, 2, 4, 7, 11, 19, 30]))
                ],
            }
        )
    return accounts


def _class_of(buckets: Any) -> tuple[Any, ...]:
    """The quasi-identifier tuple, computed here rather than imported.

    Deliberately not ``profile.equivalence_class``: the measurement this suite makes must not
    be one the code under test gets to define, or an implementation could pass by narrowing
    what counts as a class.
    """
    data = buckets.model_dump() if hasattr(buckets, "model_dump") else dict(buckets)
    return (
        data.get("budget_band"),
        tuple(data.get("category_affinity") or ()),
        data.get("frequency_tier"),
        data.get("region"),
        data.get("first_time"),
    )


def _report(classes: Counter) -> str:
    total = sum(classes.values())
    unique = sum(1 for size in classes.values() if size == 1)
    return (
        f"{total} buyers -> {len(classes)} distinct classes, "
        f"min class size {min(classes.values())}, "
        f"{unique} uniquely identified ({100.0 * unique / total:.1f}%)"
    )


# =======================================================================================
# The knob itself
# =======================================================================================


def test_the_floor_defaults_to_one_and_is_read_from_the_environment() -> None:
    """SPEC §Non-goals: "a configurable floor (default 1 in fixtures)". Both halves."""
    from buyer_svc.profile import DEFAULT_K_ANONYMITY, K_ANONYMITY_ENV, k_anonymity_floor

    assert DEFAULT_K_ANONYMITY == 1
    assert k_anonymity_floor({}) == 1
    assert k_anonymity_floor({K_ANONYMITY_ENV: "5"}) == 5
    assert k_anonymity_floor({K_ANONYMITY_ENV: " 12 "}) == 12

    # A typo in an optional knob must not fail a boot, and the fallback is the documented
    # default rather than an unsafe one.
    for junk in ("", "banana", "0", "-3", "5.5"):
        assert k_anonymity_floor({K_ANONYMITY_ENV: junk}) == 1, junk


def test_the_knob_is_honoured_from_the_real_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment sets a variable, not a keyword argument. That path must work."""
    from buyer_svc.profile import (
        K_ANONYMITY_ENV,
        anonymise_cohort,
        build_buckets,
        k_anonymity_floor,
    )

    monkeypatch.delenv(K_ANONYMITY_ENV, raising=False)
    assert k_anonymity_floor() == 1

    population = _population(600, seed=21)
    assert [_class_of(b) for b in anonymise_cohort(population)] == [
        _class_of(build_buckets(account)) for account in population
    ]

    monkeypatch.setenv(K_ANONYMITY_ENV, "5")
    assert k_anonymity_floor() == 5
    classes = Counter(_class_of(b) for b in anonymise_cohort(population))
    assert min(classes.values()) >= 5, _report(classes)


# =======================================================================================
# Claim 1 — the default changes nothing
# =======================================================================================


def test_the_default_release_is_byte_identical_to_todays_profile() -> None:
    """``k = 1`` must publish exactly what the single-account builder publishes.

    Not "equivalent", not "as coarse": the same values. This is the assertion that stops the
    knob from quietly becoming an unrequested behaviour change to every existing caller.
    """
    from buyer_svc.profile import anonymise_cohort, build_buckets, build_profile, build_profiles

    population = _population()
    names = [f"psn-{index:05d}" for index in range(len(population))]

    assert [b.model_dump() for b in anonymise_cohort(population, k=1)] == [
        build_buckets(account).model_dump() for account in population
    ]
    assert [p.model_dump() for p in build_profiles(population[:300], names[:300], k=1)] == [
        build_profile(account, name).model_dump()
        for account, name in zip(population[:300], names[:300])
    ]


def test_the_documented_default_really_does_leave_buyers_unique() -> None:
    """The default is *no* enforcement, and this suite says so out loud.

    SPEC A6 chose ``k = 1`` for fixtures deliberately: a floor above 1 is unsatisfiable at
    fixture scale. Recording the consequence here means nobody later reads this file as a
    claim that the default is anonymous, and it is the measurement the knob exists to change.
    """
    from buyer_svc.profile import build_buckets

    population = _population()
    classes = Counter(_class_of(build_buckets(account)) for account in population)

    assert min(classes.values()) == 1, _report(classes)
    unique = sum(1 for size in classes.values() if size == 1)
    assert unique > len(population) // 2, (
        f"the unenforced default should leave most buyers in a class of one — {_report(classes)}"
    )


# =======================================================================================
# Claim 2 — a configured floor is real
# =======================================================================================


@pytest.mark.parametrize("k", [2, 5, 25])
def test_a_configured_floor_is_enforced_over_a_generated_population(k: int) -> None:
    """The joint quantity, over 2,400 generated buyers rather than a fixture.

    Both bounds are asserted. ``min(class) >= k`` is the floor. The upper bound on the class
    count pins the arithmetic (it follows from the floor, since class sizes sum to ``N``), and
    the *lower* bound is the one that actually kills the degenerate implementation which
    satisfies a floor by publishing one constant profile for every buyer.
    """
    from buyer_svc.profile import anonymise_cohort

    population = _population()
    released = anonymise_cohort(population, k=k)
    assert len(released) == len(population)

    classes = Counter(_class_of(buckets) for buckets in released)
    assert min(classes.values()) >= k, f"k={k}: floor breached — {_report(classes)}"
    assert len(classes) <= len(population) // k, f"k={k}: {_report(classes)}"
    assert len(classes) >= 8, f"k={k}: the release carries no information — {_report(classes)}"


@pytest.mark.parametrize("seed", [1932, 7, 88, 404, 2718])
def test_a_configured_floor_holds_across_differently_shaped_populations(seed: int) -> None:
    """One population is a fixture with extra steps; five shapes is a claim about the code."""
    from buyer_svc.profile import anonymise_cohort

    population = _population(2000, seed=seed)
    classes = Counter(_class_of(b) for b in anonymise_cohort(population, k=5))
    assert min(classes.values()) >= 5, f"seed {seed}: {_report(classes)}"
    assert len(classes) >= 8, f"seed {seed}: {_report(classes)}"


def test_a_configured_floor_holds_on_an_adversarial_population() -> None:
    """600 distinct regions, continuous spend, long-tail baskets — and still no class under k.

    At rung 0 this population is almost entirely singletons, so the floor here is doing real
    work rather than agreeing with a population that was already crowded.
    """
    from buyer_svc.profile import anonymise_cohort, build_buckets

    population = _adversarial_population()
    before = Counter(_class_of(build_buckets(account)) for account in population)
    assert min(before.values()) == 1, f"the population should start unique — {_report(before)}"

    classes = Counter(_class_of(b) for b in anonymise_cohort(population, k=5))
    assert min(classes.values()) >= 5, f"floor breached — {_report(classes)}"
    assert len(classes) <= len(population) // 5, _report(classes)
    assert len(classes) >= 8, f"the release collapsed to nothing — {_report(classes)}"


def test_an_enforced_release_coarsens_progressively_rather_than_suppressing() -> None:
    """A floor a store cannot use is a floor nobody will turn on.

    The thresholds are budget arithmetic, not a snapshot. 2,400 buyers at ``k = 5`` buy at
    most 480 equivalence classes, and region times affinity times band times tier is an order
    of magnitude more combinations than that — so not every buyer can keep every facet. What
    is asserted is that the release *spends* the budget rather than blanking the profile:
    nearly everyone keeps a spend band and a tier, most keep an affinity, and hardly anybody
    is suppressed outright.
    """
    from buyer_svc.profile import CATEGORY_TAXONOMY, anonymise_cohort

    released = [b.model_dump() for b in anonymise_cohort(_population(), k=5)]
    total = len(released)

    def share(predicate) -> float:
        return sum(1 for row in released if predicate(row)) / total

    assert share(lambda row: row["budget_band"]) >= 0.85, "the spend band should survive widely"
    assert share(lambda row: row["frequency_tier"]) >= 0.95, "the tier is cheap and should stay"
    assert share(lambda row: row["category_affinity"]) >= 0.55, (
        f"only {share(lambda row: row['category_affinity']):.1%} kept an affinity — the release "
        f"is suppressing where it should be generalising"
    )
    assert share(lambda row: row["region"]) >= 0.40, (
        f"only {share(lambda row: row['region']):.1%} kept a region"
    )
    assert share(lambda row: row["frequency_tier"] is None) <= 0.05, (
        "blanket suppression is a way to pass a floor without anonymising anything useful"
    )
    for row in released:
        for label in row["category_affinity"]:
            assert label in CATEGORY_TAXONOMY, f"affinity escaped the closed taxonomy: {label}"


def test_generalising_a_record_can_only_ever_merge_classes() -> None:
    """The ladder is monotone, which is what makes 'generalise one rung' sound.

    If a coarser rung could split a class, the enforcement loop would not converge and its
    guarantee would be an accident of the input.
    """
    from buyer_svc.profile import BOTTOM_LEVEL, buckets_at_level

    population = _population(600, seed=77)
    previous = [_class_of(buckets_at_level(account, 0)) for account in population]
    for level in range(1, BOTTOM_LEVEL + 1):
        current = [_class_of(buckets_at_level(account, level)) for account in population]
        for i in range(len(population)):
            for j in range(i + 1, min(i + 12, len(population))):
                if previous[i] == previous[j]:
                    assert current[i] == current[j], (
                        f"rung {level} split a class that rung {level - 1} held together"
                    )
        previous = current


def test_rung_zero_is_the_default_profile_and_the_rungs_are_bounded() -> None:
    from buyer_svc.profile import BOTTOM_LEVEL, buckets_at_level, build_buckets

    for account in _population(200, seed=13):
        assert buckets_at_level(account, 0).model_dump() == build_buckets(account).model_dump()

    suppressed = buckets_at_level({"orders": []}, BOTTOM_LEVEL).model_dump()
    assert suppressed == {
        "budget_band": None,
        "category_affinity": [],
        "frequency_tier": None,
        "region": None,
        "first_time": None,
    }

    for bad in (-1, BOTTOM_LEVEL + 1, "2", None, True):
        with pytest.raises(ValueError):
            buckets_at_level({"orders": []}, bad)  # type: ignore[arg-type]


def test_a_release_too_small_for_the_configured_floor_is_refused() -> None:
    """Four buyers cannot be 5-anonymous by any means, and saying so is the honest answer.

    At the default floor the same release is fine, because the default enforces nothing —
    which is exactly the distinction SPEC A6 draws between fixtures and production.
    """
    from buyer_svc.profile import anonymise_cohort, build_profiles

    population = _population(4, seed=9)
    names = [f"psn-{n}" for n in range(4)]

    assert len(anonymise_cohort(population, k=1)) == 4
    assert len(build_profiles(population, names)) == 4

    with pytest.raises(ValueError) as caught:
        anonymise_cohort(population, k=5)
    assert "anonymous" in str(caught.value)
    with pytest.raises(ValueError):
        build_profiles(population, names, k=5)
    with pytest.raises(ValueError):
        anonymise_cohort(population, k=0)

    assert anonymise_cohort([], k=5) == []


def test_build_profiles_pairs_every_account_with_its_own_pseudonym() -> None:
    from buyer_svc.profile import build_profiles

    population = _population(50, seed=3)
    names = [f"psn-{n:04x}" for n in range(50)]

    assert [profile.pseudonym for profile in build_profiles(population, names)] == names
    with pytest.raises(ValueError):
        build_profiles(population, names[:10])
    with pytest.raises(ValueError):
        build_profiles(population, ["  "] * 50)
    with pytest.raises(ValueError):
        build_profiles(["not-a-mapping"], ["psn-x"])  # type: ignore[list-item]


def test_an_enforced_release_still_carries_no_identity() -> None:
    """The floor generalises R5's buckets; it must not weaken R5's backstop on the way."""
    from buyer_svc.profile import build_profiles, identity_leaks

    population = _population(500, seed=11)
    names = [f"psn-{index:05d}" for index in range(len(population))]
    for k in (1, 5):
        for account, profile in zip(population, build_profiles(population, names, k=k)):
            assert identity_leaks(profile, account) == [], k


def test_a_generalised_affinity_never_carries_the_accounts_own_free_text() -> None:
    """The taxonomy is closed, so a purchase log cannot ride out on a generalised release."""
    import json

    from buyer_svc.profile import CATEGORY_TAXONOMY, buckets_at_level, taxonomy_affinity

    log = {
        "email": "dana.reyes@example.com",
        "orders": [
            {"order_ref": f"ord-{n}", "total": 20.0 + n, "category": f"artisanal-thing-{n}"}
            for n in range(20)
        ],
    }
    blob = json.dumps(buckets_at_level(log, 1).model_dump(), sort_keys=True)
    for n in range(20):
        assert f"artisanal-thing-{n}" not in blob, blob

    # A recognised category with a single order behind it is dropped: one purchase is not a
    # habit, and one-off purchases are the most identifying thing an account holds.
    one_of_each = {
        "orders": [
            {"order_ref": "o-1", "total": 90.0, "category": "running-shoes"},
            {"order_ref": "o-2", "total": 90.0, "category": "espresso"},
            {"order_ref": "o-3", "total": 90.0, "category": "vinyl-records"},
        ]
    }
    assert taxonomy_affinity(one_of_each) == []

    habit = {
        "orders": [
            {"order_ref": f"o{n}", "total": 90.0, "category": "running-shoes"} for n in range(6)
        ]
        + [{"order_ref": "o-x", "total": 90.0, "category": "climbing-rope"}]
    }
    assert taxonomy_affinity(habit) == ["footwear"], "the repeat habit is what survives"

    for account in _population(300, seed=5):
        assert set(taxonomy_affinity(account)) <= set(CATEGORY_TAXONOMY)
