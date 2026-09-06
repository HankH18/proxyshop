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
    """The threshold itself, asserted as a boundary rather than as a single example."""
    from buyer_svc.profile import identity_leaks

    lee = {"first_name": "Ann", "last_name": "Lee", "region": "US-OR", "orders": []}
    assert identity_leaks(
        {"pseudonym": "psn-x", "buckets": {"category_affinity": ["ann-lee-gear"]}}, lee
    ) == ["ann", "lee"]

    # Two characters is still invisible, everywhere. This is a real residue, not a win: Ng,
    # Wu, Yu, Li, Oh and Xu are among the most common surnames on earth and none of them can
    # be tracked. Lowering the global floor to 2 was measured and refused — it reds eleven
    # tests, including the HTTP route tests, because a two-character fragment matches inside
    # every ISO region code there is.
    ng = {"last_name": "Ng", "orders": []}
    assert identity_leaks({"pseudonym": "psn-x", "buckets": {"region": "ng"}}, ng) == []


@pytest.mark.parametrize(
    ("surname", "region"),
    [("Eng", "GB-ENG"), ("Ben", "BEN"), ("Pan", "PAN"), ("Nam", "NAM"), ("Che", "CHE")],
)
def test_a_three_letter_name_that_spells_an_iso_code_does_not_lock_the_buyer_out(
    surname: str, region: str
) -> None:
    """``region`` keeps a floor of four, and this is the buyer that argument is about.

    ``coarsen_region`` accepts alphabetic parts of two OR THREE characters, so the bucket
    emits ``GB-ENG`` and ``BEN`` as readily as ``US-OR``. Dropping the global floor to three
    without exempting this bucket refused every one of these buyers permanently — measured,
    all five, before ``_MIN_LEAKABLE_BY_BUCKET`` existed. They are ordinary surnames and the
    collision is with the code's alphabet, not a disclosure: nothing about ``GB-ENG`` in a
    profile tells a store the buyer is called Eng, and no rotation of the pseudonym helps.

    ``region`` has no per-value exemption at all, which is exactly why the floor has to carry
    the argument here.
    """
    from buyer_svc.profile import build_profile, coarsen_region

    assert coarsen_region(region) == region, "this test is about a code the coarsener emits"

    account = {
        "last_name": surname,
        "email": "b@mail.invalid",
        "region": region,
        "orders": _orders("trail-gear"),
    }
    built = build_profile(account, PSEUDONYM).model_dump()  # must not raise
    assert built["buckets"]["region"] == region


def test_a_genuine_disclosure_through_region_is_still_reported() -> None:
    """The other side of that floor: four characters is above anything a real code can be."""
    from buyer_svc.profile import identity_leaks

    park = {
        "address": "12 Park Lane, Boulder CO 80301",
        "postal_code": "80301",
        "region": "US-CO",
        "orders": [],
    }
    assert identity_leaks({"pseudonym": "psn-x", "buckets": {"region": "US-CO"}}, park) == []
    for rewired in ("80301", "US-CO 80301", "boulder", "park-lane-boulder"):
        assert identity_leaks({"pseudonym": "psn-x", "buckets": {"region": rewired}}, park), (
            f"a coarsener rewired to return {rewired!r} must still be caught"
        )


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


def test_a_phone_stored_under_any_grouping_is_found_in_the_joined_slug() -> None:
    """However the ACCOUNT spells the number, the joined form in a slug is caught.

    The name of this test is deliberately narrow, and an earlier one was not: it claimed
    "every grouping" while varying only the stored side and holding the published slug fixed.
    What shipped rewrites the needle, never the haystack — the account's digits are joined,
    and a published value is matched only if it carries them fully joined. The published-side
    residue that leaves is pinned separately below.

    The ticket left open *which* digits of a phone number are the phone number. The answer
    taken is the conservative one — all of them, in order, separators dropped — because that is
    the only grouping-independent reading and it is exactly the transformation that hid it.
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


def test_a_regrouped_published_slug_is_the_other_half_of_the_same_residue() -> None:
    """The normalisation rewrites the needle, not the haystack. Recorded, not covered.

    T-198(a)'s own word is "REGROUPED", and the ticket's example regroups the *stored* number.
    Vary the grouping on the *published* side instead and nothing matches: the account's digits
    are joined into one fragment, and a slug that keeps its own separators does not contain it.

    Closing this means normalising the bucket text as well — searching in digit space on both
    sides. That is a bigger change than T-198 describes and it needs a minimum run length
    nobody has chosen yet, or every two-digit run in a slug starts matching postal codes. It is
    written down here so the boundary is a decision with a diff rather than a drift, and it is
    reported as an open defect rather than being quietly folded into a closed ticket.
    """
    from buyer_svc.profile import build_profile, identity_leaks

    for regrouped in ("gift 555 01 00 gear", "gift 55 50 100 gear"):
        account = {"phone": "555-0100", "region": "US-OR", "orders": _orders(regrouped)}
        built = build_profile(account, PSEUDONYM).model_dump()
        assert identity_leaks(built, account) == [], (
            "if this now reports the phone, the published-side residue was closed — delete "
            f"this case and say so ({regrouped!r})"
        )


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


def test_the_domain_split_stops_at_the_last_label_so_a_tld_is_never_a_fragment() -> None:
    """The TLD is the one part of an address nobody chooses, and it is inside real merchandise.

    ``com`` is a substring of the taxonomy tokens ``comics`` and ``computer``; ``org`` is
    inside ``organic`` and ``organizers``. Adding it as a fragment gives EVERY ``.com`` account
    a universal collision, and because ``_MAX_INCIDENTAL_FRAGMENTS`` is 1 and rule 5 charges
    that budget over the whole published list, one universal fragment plus one real collision
    withdraws the exemption for every slug on the account.

    Measured before the last label was dropped: both of the module's canonical coincidences
    broke as soon as the buyer placed one more perfectly ordinary order.
    """
    from buyer_svc.profile import _identity_sources, build_profile

    assert "com" not in _identity_sources({"email": "b@example.com"})
    assert "org" not in _identity_sources({"email": "b@shop.org"})
    # Everything left of the last label is still split — that is what T-198(b) needs.
    assert {"reyes", "family"} <= set(_identity_sources({"email": "x@reyes-family.example"}))

    for account in (
        {"last_name": "Cook", "email": "cook.buyer@example.com", "orders": _orders("cookware")},
        {
            "last_name": "Cook",
            "email": "cook.buyer@example.com",
            "orders": _orders("cookware", "computers"),
        },
        {"email": "espresso.fan@example.com", "orders": _orders("espresso")},
        {"email": "espresso.fan@example.com", "orders": _orders("espresso", "comics")},
    ):
        build_profile(account, PSEUDONYM)  # must not raise


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

    # The orders matter and are not decoration: the exemption is conditional on the label not
    # being one of the account's OWN slugs, and it is the buyer buying `home` that makes it
    # one. Without them this is the generalised-release case, which stays exempt on purpose.
    account = {"last_name": "Home", "region": "US-OR", "orders": _orders("home")}
    assert identity_leaks(
        {"pseudonym": "psn-x", "buckets": {"category_affinity": ["home"]}}, account
    ) == ["home"]


def test_a_taxonomy_label_the_account_never_bought_is_still_incidental() -> None:
    """The narrowing is conditional, and this is the half a blanket removal got wrong.

    "The coarsener chose this from a fixed table" is TRUE for a generalised release — that is
    exactly what ``taxonomy_affinity`` does — and false at rung 0 for a buyer whose own slug
    spells the label. Removing the exemption outright rather than conditioning it failed the
    ENTIRE release below, because one buyer's email local part happened to be ``home@`` while
    the label ``home`` was chosen off orders for bedding, cookware and lighting.
    """
    from buyer_svc.profile import build_profiles, identity_leaks

    def account(index: int, email: str, category: str) -> dict[str, Any]:
        return {
            "account_id": f"acct-{index:04d}",
            "email": email,
            "region": "US-CO",
            "orders": _orders(category),
        }

    cohort = [
        account(0, "a@mail.example", "bedding"),
        account(1, "b@mail.example", "cookware"),
        account(2, "home@mail.example", "lighting"),
    ]
    profiles = build_profiles(cohort, [f"psn-{index:032x}" for index in range(3)], k=3)
    assert len(profiles) == 3
    assert all(p.buckets.category_affinity == ["home"] for p in profiles)

    # And the condition really is "not the account's own slug", not "never reported".
    owns_it = {"last_name": "Home", "region": "US-OR", "orders": _orders("home")}
    assert identity_leaks(
        {"pseudonym": "psn-x", "buckets": {"category_affinity": ["home"]}}, owns_it
    ) == ["home"]


def test_a_place_word_that_is_also_a_taxonomy_label_still_builds() -> None:
    """Home Farm Road buying ``home`` is the Park Lane collision, one bucket over.

    ``_NAMING_IDENTITY_KEYS`` deliberately excludes ``address`` and ``street`` because place
    words genuinely collide with merchandise. Rule 3's one-token leniency used to be spelled
    "anything but the email", which admitted ``espresso.fan@`` buying ``espresso`` and refused
    this buyer — a refusal that was invisible only because the blanket taxonomy exemption was
    covering it for the eleven labels, and for nothing else.
    """
    from buyer_svc.profile import build_profile

    account = {
        "email": "j.k@mail.example",
        "last_name": "Kim",
        "address": "9 Home Farm Road, Boulder CO",
        "region": "US-CO",
        "orders": _orders("home", "trail-gear"),
    }
    built = build_profile(account, PSEUDONYM).model_dump()  # must not raise
    assert built["buckets"]["category_affinity"] == ["home", "trail-gear"]


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
# T-164 — the half of the fix its own gate cannot see
# =======================================================================================


def test_anonymise_cohort_refuses_on_its_own_and_not_only_through_build_buckets() -> None:
    """T-164's gate calls ``anonymise_cohort([account], k=1)``, and that proves nothing about it.

    At ``k == 1`` ``anonymise_cohort`` returns ``[build_buckets(a) for a in accounts]``, so the
    refusal the gate observes is ``build_buckets``'s. Measured: reverting ONLY the
    ``anonymise_cohort`` check and leaving ``build_buckets`` guarded, the gate still passes.
    Its own emission path — the generalisation ladder at ``k > 1``, which builds rungs through
    a private coarsener and never calls ``build_buckets`` at all — is not selected by anything.

    So this test drives that path deliberately. Two identical leaking accounts at ``k = 2``
    already share an equivalence class at rung 0, so nothing is generalised and the ladder
    releases the rung-0 buckets verbatim — the exact case where the only thing between a
    surname and a store is ``anonymise_cohort``'s own check.
    """
    from buyer_svc.profile import IdentityLeak, anonymise_cohort, build_profile

    # No email on purpose: "reyes" would then reach the account through two keys and the
    # attribution assertion below would stop saying anything about which one was found.
    leaking: dict[str, Any] = {
        "last_name": "Reyes",
        "region": "US-OR",
        "orders": _orders("reyes gear"),
    }

    # Control: the guarded single-account builder does refuse this account, so it is a real
    # leak and not a badly-built fixture.
    with pytest.raises(IdentityLeak):
        build_profile(leaking, PSEUDONYM)

    with pytest.raises(IdentityLeak, match="R5") as caught:
        anonymise_cohort([dict(leaking), dict(leaking)], k=2)
    assert caught.value.account_keys == ("last_name",)


def test_the_ladder_may_still_release_a_record_whose_rung_zero_would_leak() -> None:
    """The check is against what is RELEASED, not against rung 0. That distinction is the point.

    A generalisation ladder that refused a record because a rung nobody published carries a
    fragment would punish the buyer it had just protected. Here the leaking account sits in a
    cohort that generalises it past its own free-text slug, and the release is admitted —
    carrying a closed-taxonomy label instead of the surname.

    THE COHORT IS BUILT SO THE RELEASE IS NOT SUPPRESSED, and that is not incidental. An
    earlier version of this test used a crowd whose members all landed in the bottom class:
    every released set was ``category_affinity == []``, the assertion loop over the labels ran
    zero times in total, and the only surviving assertion was ``10 == 10``. It stayed green
    against an implementation that suppressed everything. So the released affinity is asserted
    to be non-empty FIRST, and the six accounts here share a rung-1 class rather than falling
    to the floor.
    """
    from buyer_svc.profile import CATEGORY_TAXONOMY, IdentityLeak, anonymise_cohort, build_profile

    leaking: dict[str, Any] = {
        "last_name": "Reyes",
        "region": "US-OR",
        "orders": _orders("reyes gear"),
    }
    # Control: rung 0 for this account really does leak, so the release below is the ladder
    # doing work rather than the account having been harmless all along.
    with pytest.raises(IdentityLeak):
        build_profile(dict(leaking), PSEUDONYM)

    crowd = [dict(leaking)] + [
        {"region": "US-OR", "orders": _orders(f"widget-{suffix}")} for suffix in "abcde"
    ]
    released = anonymise_cohort(crowd, k=5)

    assert len(released) == len(crowd)
    assert all(buckets.category_affinity for buckets in released), (
        "the cohort was suppressed rather than generalised, so this test would grade nothing: "
        f"{[b.model_dump() for b in released]}"
    )
    assert released[0].category_affinity == ["other"]
    for buckets in released:
        assert buckets.region == "US"  # generalised, not blanked
        for label in buckets.category_affinity:
            assert label in CATEGORY_TAXONOMY, f"free text survived generalisation: {label}"


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
    assert identity_leaks({"pseudonym": "psn-x", "buckets": {"region": "ab cd"}}, account) == [
        "ab  cd"
    ]


# =======================================================================================
# T-221 / T-363 — what the floor buys on the production path, and what it costs
# =======================================================================================

#: A floor small enough that a modest directory can satisfy it without collapsing to the
#: bottom rung, and large enough that a class of ``k`` is visibly not a class of one.
_ROTATION_K = 4

#: Buyers in the rotation directory. Three full classes at :data:`_ROTATION_K`.
_ROTATION_BUYERS = 12


def _rotation_directory(count: int = _ROTATION_BUYERS) -> dict[str, dict[str, Any]]:
    """A directory whose buyers are all distinct at rung 0, built without a generator.

    Spend, region and basket move independently and none of them repeats inside ``count``, so
    every buyer is alone in their equivalence class before any floor is applied. That is the
    arm the rotation test needs: a population that was already crowded could not show whether
    a floor did anything.
    """
    regions = ("US-OR", "US-CA", "GB-ENG", "CA-ON")
    baskets = ("running-shoes", "cookware", "board-games", "skincare", "headphones", "cold-brew")
    directory: dict[str, dict[str, Any]] = {}
    for index in range(count):
        email = f"rot{index:03d}@example.com"
        directory[email] = {
            "email": email,
            "region": regions[index % len(regions)],
            "orders": [
                {
                    "order_ref": f"rot-{index}-{n}",
                    "total": 12.0 + index * 97.0 + n,
                    "category": baskets[(index + n) % len(baskets)],
                }
                for n in range(1 + index % 5)
            ],
        }
    return directory


def _served(directory: dict[str, dict[str, Any]]) -> tuple[Any, list[tuple[str, Any]]]:
    """Log every buyer in ``directory`` in through the real service and keep what was served.

    Returns the service (so a caller can rotate a pseudonym on it) and ``(pseudonym, buckets)``
    per buyer, in directory order.
    """
    from buyer_svc.auth import InMemoryAccountDirectory, MagicLinkAuth

    tokens: dict[str, str] = {}
    service = MagicLinkAuth(
        accounts=InMemoryAccountDirectory(directory),
        deliver=lambda email, token, expires_at: tokens.__setitem__(email, token),
    )
    served: list[tuple[str, Any]] = []
    for email in directory:
        service.request_login(email)
        session = service.redeem(tokens[email])
        served.append((session.pseudonym, service.profile_for(session.session_id).buckets))
    assert len(served) == len(directory), f"only {len(served)} of {len(directory)} logged in"
    return service, served


def test_a_rotated_pseudonym_is_re_linkable_only_into_a_class_of_at_least_k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-363's reproduction, turned the right way round.

    The ticket's observation is correct and is **not** by itself the defect: rotating a
    pseudonym publishes a byte-identical bucket tuple, because the tuple is a deterministic
    function of the account and rotation does not change the account. Suppressing that would
    mean publishing a different profile to the same buyer at every login, which is noise, not
    privacy.

    What makes byte-identity a re-linkage is that at ``k = 1`` the tuple belongs to ONE buyer,
    so whoever holds it can join the old pseudonym to the new one exactly. What a floor buys is
    that the same join lands on at least ``k`` candidates. Both halves are measured here, over
    the production login path, on the same population:

    * ARM — with no floor configured the target buyer's tuple is unique in the release, so the
      rotation join is exact. Without this the assertion below could pass on a population that
      was already crowded.
    * With the floor configured the tuple is still byte-identical across the rotation, and it
      is now shared by at least ``k`` buyers.
    """
    from buyer_svc.profile import K_ANONYMITY_ENV, equivalence_class

    directory = _rotation_directory()
    target = next(iter(directory))

    monkeypatch.delenv(K_ANONYMITY_ENV, raising=False)
    _, unenforced = _served(directory)
    unenforced_classes = [equivalence_class(buckets) for _, buckets in unenforced]
    assert unenforced_classes.count(unenforced_classes[0]) == 1, (
        "the unenforced population already shares the target buyer's tuple, so it cannot show "
        f"whether the floor did anything: {unenforced_classes[0]!r}"
    )

    monkeypatch.setenv(K_ANONYMITY_ENV, str(_ROTATION_K))
    service, enforced = _served(directory)
    first_pseudonym, first_buckets = enforced[0]

    # The rotation itself, on the same service and the same account record.
    tokens: dict[str, str] = {}
    service.deliver = lambda email, token, expires_at: tokens.__setitem__(email, token)
    service.request_login(target)
    rotated = service.redeem(tokens[target])
    second_buckets = service.profile_for(rotated.session_id).buckets

    assert rotated.pseudonym != first_pseudonym, "the pseudonym did not rotate"
    assert equivalence_class(second_buckets) == equivalence_class(first_buckets), (
        "the same buyer was served two different tuples across a rotation; the floor is not "
        "supposed to randomise the profile, it is supposed to crowd it"
    )

    classes = [equivalence_class(buckets) for _, buckets in enforced]
    candidates = classes.count(equivalence_class(second_buckets))
    assert candidates >= _ROTATION_K, (
        f"PROXYSHOP_BUYER_K_ANONYMITY={_ROTATION_K} was set and the rotated pseudonym's tuple "
        f"is shared by only {candidates} buyer(s) in the release, so the join from the old "
        "pseudonym to the new one still names a single buyer"
    )
    assert len(set(classes)) >= 2, (
        "the release collapsed to one constant tuple, which satisfies any floor and tells a "
        "store nothing"
    )


@pytest.mark.parametrize("k", [2, 5, 25])
def test_no_floor_above_one_generalises_the_single_record_path_it_suppresses_it(k: int) -> None:
    """Why raising :data:`DEFAULT_K_ANONYMITY` is not the same change as wiring the ladder.

    A floor is a property of a release, and a release of one record cannot satisfy any floor
    above 1 by any generalisation — so on the single-record call ``build_profile(account,
    pseudonym)``, which is every caller in the tree except
    :meth:`MagicLinkAuth.profile_for`, the answer at ``k = 2`` and at ``k = 25`` is the same
    answer: withhold everything. The cost of raising the default is therefore a step at
    ``k > 1`` and not a curve in ``k``. MEASURED: simulating ``DEFAULT_K_ANONYMITY = 5`` fails
    47 nodes in this suite and ``= 2`` fails 46, and the two sets differ by one node.

    The second half is the part that is easy to miss and is the reason this is a test rather
    than a comment. R5's identity backstop is measured by publishing a hostile account and
    requiring :class:`IdentityLeak`. A default that publishes nothing does not make that
    backstop safer; it makes it **unobservable** — the same account that is refused at the
    documented default reports clean here, because there is no longer a value to leak. Twenty
    of those 47 failures are backstop probes going quiet exactly this way.
    """
    from buyer_svc.profile import IdentityLeak, build_profile, identity_leaks

    hostile: dict[str, Any] = {
        "last_name": "Reyes",
        "region": "US-OR",
        "orders": _orders("reyes gear"),
    }

    # Control: at the documented default this account is refused, so it is a real leak.
    with pytest.raises(IdentityLeak):
        build_profile(dict(hostile), PSEUDONYM, k=1)

    withheld = build_profile(dict(hostile), PSEUDONYM, k=k).model_dump()["buckets"]
    assert withheld == {
        "budget_band": None,
        "category_affinity": [],
        "frequency_tier": None,
        "region": None,
        "first_time": None,
    }, f"a release of one cannot meet a floor of {k}; every facet has to be withheld: {withheld}"

    assert identity_leaks({"pseudonym": PSEUDONYM, "buckets": withheld}, hostile) == [], (
        "the backstop still reports a leak on a fully-withheld profile, which would mean this "
        "test is measuring something other than the suppression it claims to measure"
    )
