"""The identity floor the leak backstop measures against (T-197, T-198, T-199).

Three tickets, one defect class: a fragment of the buyer's identity that reaches the
store-facing ``category_affinity`` bucket while ``identity_leaks`` reports clean. Each had a
different reason for being invisible, and each reason was a *threshold or a table* rather than
a bug in the matching:

* **T-197** — ``_MIN_LEAKABLE`` was 4, so ``Ann`` and ``Lee`` never entered the haystack at all
  and ``['ann-lee-gear']`` was published clean.
* **T-198** — ``_identity_sources`` recorded a value as written and in slug space but not with
  its digit grouping dropped (``555-0100`` vs ``gift-5550100-gear``), and it word-split the
  email local part while adding the domain whole (``reyes-family@example.com`` refused,
  ``x@reyes-family.example`` admitted).
* **T-199** — ``_BUCKET_VOCABULARY`` held every taxonomy label out of the ``category_affinity``
  haystack on the grounds that a coarsener chose them from a fixed table. True above the
  default floor; false at rung 0, where the bucket carries the account's own slugs.

The reproduction gates for all three live in ``test_repro_open_tickets.py``. This file exists
for the half those gates cannot express: **the trades that must not move.** Every fix here
widens what counts as identity, and widening it is exactly how a privacy backstop turns into a
denial of service — a buyer refused forever for living on Park Lane or being named Cook. So
each ticket's fix is pinned beside the collision it must still admit, and the boundary is
pinned from both sides.

Every product import happens inside a test body, matching the rest of this suite.
"""

from __future__ import annotations

from typing import Any

import pytest

PSEUDONYM = "psn-" + "3c" * 16


def _orders(*categories: str) -> list[dict[str, Any]]:
    """Two orders per category — enough support for the coarsener to publish each slug."""
    return [
        {"order_ref": f"ord-{index}-{n}", "total": 140.0 + n, "category": category}
        for index, category in enumerate(categories)
        for n in range(2)
    ]


# =======================================================================================
# T-197 — three characters is the floor, and two is not
# =======================================================================================


def test_a_three_character_name_is_tracked_and_a_two_character_one_is_not() -> None:
    """The threshold itself, asserted as a boundary rather than as a single example.

    Three is a deliberate stopping point, not a step on the way to zero. ``coarsen_region``
    emits two- and three-letter ISO codes, and ``region`` is the one bucket with no exemption
    of any kind, so a two-character fragment collides with it by construction: the buyer at
    "12 Park Lane, Boulder CO 80301" whose profile says ``region == "US-CO"`` would have their
    own address word reported as a disclosure. That is the denial of service the exemptions
    exist to prevent, arriving through the one place they do not reach.
    """
    from buyer_svc.profile import identity_leaks

    lee = {"first_name": "Ann", "last_name": "Lee", "region": "US-OR", "orders": []}
    assert identity_leaks(
        {"pseudonym": "psn-x", "buckets": {"category_affinity": ["ann-lee-gear"]}}, lee
    ) == ["ann", "lee"]

    boulder = {
        "address": "12 Park Lane, Boulder CO 80301",
        "postal_code": "80301",
        "region": "US-CO",
        "orders": [],
    }
    assert (
        identity_leaks({"pseudonym": "psn-x", "buckets": {"region": "US-CO"}}, boulder) == []
    ), "a two-character address word must not be able to make the region bucket a disclosure"


def test_the_short_name_fix_still_admits_the_documented_collisions() -> None:
    """Cook buying cookware, Park Lane buying park-gear, espresso.fan buying espresso.

    All three are one-collision coincidences the exemption was built for, and all three are
    reachable by ordinary buyers who cannot change the facts about themselves. If lowering the
    floor had cost any of them, the trade would have been re-made silently.
    """
    from buyer_svc.profile import build_profile

    for account in (
        {"last_name": "Cook", "email": "cook.buyer@example.com", "orders": _orders("cookware")},
        {
            "first_name": "Lena",
            "last_name": "Hunt",
            "address": "12 Park Lane, Boulder CO 80301",
            "postal_code": "80301",
            "region": "US-CO",
            "orders": _orders("park-gear", "trail-gear"),
        },
        {"email": "espresso.fan@example.com", "orders": _orders("espresso")},
    ):
        build_profile(account, PSEUDONYM)  # must not raise


# =======================================================================================
# T-198 (a) — a number is its digits, however they are grouped
# =======================================================================================


def test_a_phone_number_is_found_under_every_grouping_of_its_own_digits() -> None:
    """Regrouped, repunctuated and verbatim are spellings of one number, not three numbers.

    The ticket left open *which* digits of a phone number are the phone number. The answer
    taken is the conservative one — all of them, in order, separators dropped — because that is
    the only grouping-independent reading and it is exactly the transformation that hid it.
    Every spelling below carries the same seven digits and every one is refused.
    """
    from buyer_svc.profile import IdentityLeak, build_profile

    for spelling in ("555-0100", "(555) 0100", "555.0100", "555 0100", "5550100"):
        account = {"phone": spelling, "region": "US-OR", "orders": _orders("gift 5550100 gear")}
        with pytest.raises(IdentityLeak, match="R5") as caught:
            build_profile(account, PSEUDONYM)
        assert caught.value.account_keys == ("phone",), spelling


def test_a_stored_country_code_the_published_slug_omits_is_still_a_residue() -> None:
    """What T-198(a)'s fix does NOT close, recorded so it is not mistaken for covered.

    The normalisation is *grouping*-independent, not *length*-independent: it records the
    digits a value actually holds. So an account whose phone is stored as ``"+1-555-0100"``
    (eight digits) and whose order category carries ``5550100`` (seven) is not matched, because
    ``"15550100"`` is not a substring of ``"5550100"``.

    Closing it means deciding that the leading ``1`` is a country code rather than part of the
    subscriber number — which is the "which digits of a phone number are the phone number"
    question the ticket explicitly declined to guess at, and guessing it wrong in the other
    direction turns every seven-digit run in a slug into a refusal. This test asserts the
    boundary as it stands so that moving it is a deliberate act with a diff, not a drift.

    Note what is *not* at risk while it stands: a slug carrying a digit run can never earn the
    ``_exempt_category_slugs`` exemption (``_is_merchandising_slug`` refuses any two-digit
    run), so the value is in the haystack and fully searched — it is the matching that stops
    short, not the exemption that waves it through.
    """
    from buyer_svc.profile import build_profile, identity_leaks

    account = {"phone": "+1-555-0100", "region": "US-OR", "orders": _orders("gift 5550100 gear")}
    built = build_profile(account, PSEUDONYM).model_dump()
    assert built["buckets"]["category_affinity"] == ["gift-5550100-gear"]
    assert identity_leaks(built, account) == [], (
        "if this now reports the phone, the residue was closed — delete this test and say so"
    )

    # The same account with the same digits stored the way the slug spells them IS refused, so
    # the gap is the length difference and nothing else.
    same_digits = dict(account, phone="555-0100")
    assert identity_leaks(built, same_digits) == ["5550100"]


def test_the_digit_normalisation_is_attributed_to_the_key_that_carried_it() -> None:
    """The refusal names ``phone``, not a synthetic key invented by the normalisation.

    ``IdentityLeak.account_keys`` is the structural half of the report a log sink reads. A
    fragment recorded under ``phone_digits`` would be true and useless — no such key exists on
    the account, so nobody could act on it.
    """
    from buyer_svc.profile import _identity_sources

    sources = _identity_sources({"phone": "555-0100"})
    assert sources["5550100"] == {"phone"}


# =======================================================================================
# T-198 (b) — the same words on both sides of the "@"
# =======================================================================================


def test_the_email_domain_is_split_exactly_as_the_local_part_is() -> None:
    """The asymmetry was the defect. A vanity domain is the buyer's name; a shared one is not."""
    from buyer_svc.profile import IdentityLeak, build_profile

    for email in ("reyes-family@example.com", "x@reyes-family.example"):
        account = {"email": email, "region": "US-OR", "orders": _orders("reyes family gear")}
        with pytest.raises(IdentityLeak, match="R5") as caught:
            build_profile(account, PSEUDONYM)
        assert caught.value.account_keys == ("email",), email


def test_splitting_the_domain_does_not_refuse_an_ordinary_shared_domain() -> None:
    """"example", "com" and "invalid" are now fragments of every account. They match nothing.

    A shared domain carries no identity, which is why splitting it is free: its words are not
    the buyer's, so no coarsener can emit one. This is the assertion that catches the day one
    of them starts colliding with a bucket value.
    """
    from buyer_svc.profile import build_profile

    account = {
        "email": "b@example.com",
        "first_name": "Dana",
        "region": "US-OR",
        "orders": _orders("camera-lenses", "headphones"),
    }
    built = build_profile(account, PSEUDONYM).model_dump()
    assert built["buckets"]["category_affinity"] == ["camera-lenses", "headphones"]


# =======================================================================================
# T-199 — a taxonomy label is not a free pass at the default floor
# =======================================================================================


def test_a_taxonomy_label_carrying_the_buyers_surname_is_reported() -> None:
    """Roughly eleven common English words are taxonomy labels. Surnames are English words.

    At rung 0 — the default release, and the only release production makes —
    ``category_affinity`` carries the account's own order slugs, so "the coarsener chose this
    from a fixed table" is simply not true of the value being skipped.
    """
    from buyer_svc.profile import CATEGORY_TAXONOMY, identity_leaks

    assert "home" in CATEGORY_TAXONOMY, "this test is about a label that IS in the taxonomy"

    account = {"last_name": "Home", "region": "US-OR", "orders": []}
    assert identity_leaks(
        {"pseudonym": "psn-x", "buckets": {"category_affinity": ["home"]}}, account
    ) == ["home"]


def test_the_other_two_closed_vocabularies_keep_their_exemption() -> None:
    """``budget_band`` and ``frequency_tier`` are chosen from a fixed table at every rung.

    ``none@example.com`` whose brand-new profile says ``frequency_tier == "none"`` is the
    documented signup this exemption exists for, and narrowing T-199 must not have cost it.
    """
    from buyer_svc.profile import build_profile, identity_leaks

    built = build_profile({"email": "none@example.com", "orders": []}, PSEUDONYM).model_dump()
    assert built["buckets"]["frequency_tier"] == "none"

    banded = {"last_name": "Band", "budget_band": "100-250", "orders": _orders("camera-lenses")}
    assert (
        identity_leaks({"pseudonym": "psn-x", "buckets": {"budget_band": "100-250"}}, banded) == []
    )


# =======================================================================================
# The seam, pinned independently of the threshold
# =======================================================================================


def test_bucket_values_are_searched_one_at_a_time_and_never_concatenated() -> None:
    """A fragment must not be able to match across the seam between two unrelated values.

    ``test_auth_vault.py`` already pins this, with a fixture whose two words were invisible
    because they were under the old four-character floor. That made the assertion a hostage to
    the threshold: T-197 moved the floor and the expected value had to move with it. This
    fixture is threshold-free — both words are two characters, so neither can ever be a
    fragment on its own however low the floor goes — and it therefore keeps measuring the seam
    and nothing else.
    """
    from buyer_svc.profile import identity_leaks

    account = {"full_name": "Ab  Cd", "email": "q@example.com", "orders": []}

    assert (
        identity_leaks(
            {"pseudonym": "psn-x", "buckets": {"region": "ab", "category_affinity": ["cd"]}},
            account,
        )
        == []
    ), "no single bucket ever held 'ab  cd', so no bucket may be reported for it"

    # The control that proves the assertion above can fail: the joined form, in one bucket, IS
    # found. An implementation that concatenated before searching would produce exactly this.
    assert identity_leaks(
        {"pseudonym": "psn-x", "buckets": {"region": "ab cd"}}, account
    ) == ["ab  cd"]
