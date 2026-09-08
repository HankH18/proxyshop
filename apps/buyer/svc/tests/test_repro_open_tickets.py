"""Reproduction gates for the open ``apps/buyer`` tickets.

Every test here asserts the behaviour that SHOULD hold and therefore fails against the tree
as it stands. Each carries ``@pytest.mark.xfail(strict=True)`` while the defect is live, so:

* an ordinary run reports ``xfailed`` and the repo-wide build gate stays GREEN;
* the ticket's own gate runs
  ``export PROXYSHOP_WORKER=0 && uv run python -m pytest
  apps/buyer/svc/tests/test_repro_open_tickets.py -q --runxfail -k <name>``
  and gets a real, selected ``1 failed``;
* the marker cannot outlive the bug — once the defect is closed the test XPASSes, which
  ``strict=True`` turns into a failure, forcing whoever fixed it to delete the marker.

**As of T-142's close this file carries NO live marker: every ticket in it is fixed and every
test here passes.** That is the convention working rather than a reason to relax the first
paragraph, which still governs the next reproduction added. Each removal left a comment block
in place of its marker quoting what the marker said and what assertion, if any, changed with
it — those blocks are the record, and a reproduction whose gate was rewritten rather than
merely un-marked says so and says why (see T-142's, above its test).

**Nothing here is allowed to skip.** A skip is not a gate, and the compose datastore stack is
routinely down in this repo, so every assertion below is made against a pure function, the
module's public surface, or an in-process ASGI client — never against a live datastore.

Following the convention of the other suites in this directory, every product import happens
inside the test body rather than at module scope.
"""

from __future__ import annotations

import ast
import pathlib
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


def _assert_in_tree(module: ModuleType) -> None:
    """Refuse a reading taken from another checkout.

    This venv's ``site-packages/_proxyshop.pth`` puts a checkout on ``sys.path`` for every
    process that uses it, and it has already produced one false green in this repo: a
    reproduction passed while its defect was fully live, because the probe imported the
    primary checkout instead of the tree under test. Both trees contain these modules, so
    the only way to know which one answered is to look.
    """
    resolved = pathlib.Path(module.__file__ or "").resolve()
    assert resolved.is_relative_to(REPO_ROOT), (
        f"{module.__name__} resolved to {resolved}, which is outside the tree under test "
        f"({REPO_ROOT}) — a .pth leak, not a measurement"
    )


#: A well-formed session subject. The profile module only ever checks the ``psn-`` prefix and
#: a non-empty body, so the exact bytes are irrelevant — they are fixed here only so that two
#: calls in one test can differ deliberately rather than accidentally.
PSN_A = "psn-" + "a" * 32
PSN_B = "psn-" + "b" * 32

#: An account whose own order category carries the buyer's surname and email local part.
#: ``build_profile`` refuses this account today (``IdentityLeak``), which is what makes it the
#: right probe for an entry point that does *not* refuse it.
LEAKING_ACCOUNT: dict[str, Any] = {
    "email": "d.reyes@example.com",
    "first_name": "Dana",
    "last_name": "Reyes",
    "region": "US-OR",
    "orders": [
        {"order_ref": "ord-1", "total": 140.0, "category": "reyes gear"},
        {"order_ref": "ord-2", "total": 210.0, "category": "reyes gear"},
    ],
}


#: The same shape as :data:`LEAKING_ACCOUNT` with the identity fields removed, so it builds
#: cleanly and can be used to show that a pseudonym rotation is re-linkable rather than
#: refused.
CLEAN_ACCOUNT: dict[str, Any] = {
    "region": "US-OR",
    "orders": [
        {"order_ref": "ord-1", "total": 140.0, "category": "camera-lenses"},
        {"order_ref": "ord-2", "total": 210.0, "category": "camera-lenses"},
    ],
}


def _orders(category: str) -> list[dict[str, Any]]:
    """Two orders in one category — enough support for the coarsener to publish the slug."""
    return [
        {"order_ref": "ord-1", "total": 140.0, "category": category},
        {"order_ref": "ord-2", "total": 210.0, "category": category},
    ]


# ======================================================================================
# T-164 — the public bucket builders run no identity-leak check
# ======================================================================================
# MARKER REMOVED — T-164 is FIXED, by the ticket's FIRST accepted repair: `build_buckets` and
# `anonymise_cohort` both run the R5 backstop now and both refuse LEAKING_ACCOUNT. Neither was
# made private, so both are still reachable by `getattr` and this gate still grades them.
#
# CAUSATION PROVEN: reverting ONLY the `_refuse_if_leaking` call inside `build_buckets` and
# changing nothing else returns this node to `xfailed`. No assertion in the body was touched.
#
# READ THIS BEFORE TRUSTING THIS GATE — it grades LESS than it appears to. It calls
# `anonymise_cohort([LEAKING_ACCOUNT], k=1)`, and at k == 1 that function returns
# `[build_buckets(a) for a in accounts]`, so the refusal it observes is `build_buckets`'s.
# Measured: reverting ONLY `anonymise_cohort`'s own check and leaving `build_buckets` guarded,
# this node STILL PASSES. Its own emission path — the generalisation ladder at k > 1, which
# builds rungs through a private coarsener and never calls `build_buckets` — is selected by
# nothing here. That half is graded instead by
# apps/buyer/svc/tests/test_profile_identity_floor.py::
#   test_anonymise_cohort_refuses_on_its_own_and_not_only_through_build_buckets
# which was confirmed to fail when that check alone is reverted. Same family as T-328.
def test_t164_no_public_bucket_builder_publishes_identity_without_the_backstop() -> None:
    """Every public entry point that emits buckets must be behind the R5 backstop.

    The ticket names two acceptable repairs — run the check inside the unguarded entry
    points, or make them private — so this gate accepts either. A name that is gone from the
    module is not a publishing surface and is skipped; a name that is still reachable must
    refuse ``LEAKING_ACCOUNT`` the way ``build_profile`` already does.

    Reachability is ``getattr``, not ``__all__``. ``__all__`` governs ``import *`` and
    nothing else: deleting two strings from it would leave ``from buyer_svc.profile import
    build_buckets`` working, still unguarded, still publishing ``['reyes-gear']`` — a
    zero-behaviour edit that would close the ticket.
    """
    from buyer_svc import profile as profile_mod  # noqa: PLC0415
    from buyer_svc.profile import IdentityLeak, build_profile  # noqa: PLC0415

    _assert_in_tree(profile_mod)

    # Control: the guarded entry point does refuse this account, so the account is a real
    # leak and not a badly-built fixture.
    with pytest.raises(IdentityLeak):
        build_profile(LEAKING_ACCOUNT, PSN_A)

    # k=1 is passed explicitly. With k left to default, anonymise_cohort reads
    # PROXYSHOP_BUYER_K_ANONYMITY from the real environment, and a one-account release under
    # any floor above 1 raises ValueError before the leak check is reached — which would make
    # this gate red for the wrong reason in any shell that sets that documented knob.
    calls = {
        "build_buckets": lambda fn: fn(LEAKING_ACCOUNT),
        "anonymise_cohort": lambda fn: fn([LEAKING_ACCOUNT], k=1),
    }

    unguarded: list[tuple[str, Any]] = []
    for name, call in calls.items():
        entry = getattr(profile_mod, name, None)
        if entry is None:
            continue  # made private — the ticket's second accepted repair
        try:
            emitted = call(entry)
        except IdentityLeak:
            continue  # guarded — the ticket's first accepted repair
        unguarded.append((name, emitted))

    assert not unguarded, (
        "public profile entry points emitted buckets for an account build_profile refuses: "
        f"{unguarded}"
    )


# ======================================================================================
# T-197 — identity fragments shorter than four characters are never tracked at all
# ======================================================================================
# MARKER REMOVED — T-197 is FIXED. `_MIN_LEAKABLE` is 3, not 4
# (apps/buyer/svc/src/profile/__init__.py), so "Ann" and "Lee" enter the haystack and this
# account is refused. The marker is gone rather than kept because `strict=True` turns a passing
# xfail into a FAILURE, which would red `make verify`.
#
# CAUSATION PROVEN, not assumed. Reverting ONLY `_MIN_LEAKABLE` to 4 and changing nothing else
# returns this node to `xfailed` while t198_a, t198_b and t199 stay XPASS — measured on this
# tree. No assertion in the body was touched.
def test_t197_a_three_letter_name_in_a_category_slug_is_still_a_leak() -> None:
    """A short name is still the buyer's name."""
    from buyer_svc.profile import IdentityLeak, build_profile, identity_leaks  # noqa: PLC0415

    account: dict[str, Any] = {
        "first_name": "Ann",
        "last_name": "Lee",
        "region": "US-OR",
        "orders": _orders("ann lee gear"),
    }

    with pytest.raises(IdentityLeak):
        profile = build_profile(account, PSN_A)
        # Reached only while the defect is live; the message names what escaped.
        raise AssertionError(
            "the buyer's full name was published to the store-facing profile: "
            f"{profile.buckets.category_affinity!r}, "
            f"identity_leaks reported {identity_leaks(profile, account)!r}"
        )


# ======================================================================================
# T-198 (a) — a regrouped number escapes the backstop
# ======================================================================================
# MARKER REMOVED — T-198(a) is FIXED. `_identity_sources` now records a value's digits with
# every separator dropped, under the account key that contributed them, so phone "555-0100"
# is also tracked as "5550100" and the regrouped spelling is found.
#
# CAUSATION PROVEN: reverting ONLY the digit normalisation (the two `add_digits` calls) and
# changing nothing else returns this node to `xfailed` while t197, t198_b and t199 stay XPASS.
# No assertion in the body was touched.
def test_t198_a_a_regrouped_phone_number_in_a_category_slug_is_still_a_leak() -> None:
    """Dropping the separators from a phone number does not stop it being a phone number."""
    from buyer_svc.profile import IdentityLeak, build_profile, identity_leaks  # noqa: PLC0415

    account: dict[str, Any] = {
        "phone": "555-0100",
        "region": "US-OR",
        "orders": _orders("gift 5550100 gear"),
    }

    with pytest.raises(IdentityLeak):
        profile = build_profile(account, PSN_A)
        raise AssertionError(
            "the buyer's phone number was published to the store-facing profile: "
            f"{profile.buckets.category_affinity!r}, "
            f"identity_leaks reported {identity_leaks(profile, account)!r}"
        )


# ======================================================================================
# T-198 (b) — the email domain is added whole, so a vanity domain rides out
# ======================================================================================
# MARKER REMOVED — T-198(b) is FIXED. `_identity_sources` now word-splits the email DOMAIN the
# way it already split the local part, so the same two words are treated the same on both sides
# of the "@".
#
# CAUSATION PROVEN: reverting ONLY the domain word-split and changing nothing else returns this
# node to `xfailed` while t197, t198_a and t199 stay XPASS. No assertion in the body was
# touched.
#
# NOTE for whoever reads this next: this node is claimed by T-198 by name, by section comment
# and by decorator, and T-198's recorded `verify` selects ONLY t198_a. That under-selection is
# itself an open ticket (T-328) and is not fixed here — tickets.json is a frozen protected
# path. T-198's gate selects 1 node; both of its nodes pass.
def test_t198_b_a_vanity_email_domain_in_a_category_slug_is_still_a_leak() -> None:
    """The same two words are a leak on one side of the ``@`` and not on the other."""
    from buyer_svc.profile import IdentityLeak, build_profile, identity_leaks  # noqa: PLC0415

    orders = _orders("reyes family gear")

    # Control: the identical words in the LOCAL part are caught today. This pins the
    # asymmetry rather than the presence of any check at all.
    local_part_account: dict[str, Any] = {
        "email": "reyes-family@example.com",
        "region": "US-OR",
        "orders": list(orders),
    }
    with pytest.raises(IdentityLeak):
        build_profile(local_part_account, PSN_A)

    domain_account: dict[str, Any] = {
        "email": "x@reyes-family.example",
        "region": "US-OR",
        "orders": list(orders),
    }
    with pytest.raises(IdentityLeak):
        profile = build_profile(domain_account, PSN_A)
        raise AssertionError(
            "a vanity email domain was published to the store-facing profile: "
            f"{profile.buckets.category_affinity!r}, "
            f"identity_leaks reported {identity_leaks(profile, domain_account)!r}"
        )


# ======================================================================================
# T-199 — the bucket vocabulary exempts taxonomy labels, including at the k=1 default
# ======================================================================================
# MARKER REMOVED — T-199 is FIXED. `_BUCKET_VOCABULARY` no longer carries a
# `"category_affinity"` entry, so a taxonomy label in that bucket is searched like any other
# value. `budget_band` and `frequency_tier` keep theirs, because their coarseners really do
# choose from a fixed table at every rung.
#
# CAUSATION PROVEN: putting ONLY `"category_affinity": frozenset(CATEGORY_TAXONOMY)` back into
# `_BUCKET_VOCABULARY` and changing nothing else returns this node to `xfailed` while t197,
# t198_a and t198_b stay XPASS. No assertion in the body was touched.
def test_t199_a_surname_that_is_also_a_taxonomy_label_is_still_a_leak() -> None:
    """A vocabulary exemption that is sound at k>1 is not sound at the default floor."""
    from buyer_svc.profile import IdentityLeak, build_profile, identity_leaks  # noqa: PLC0415

    account: dict[str, Any] = {
        "last_name": "Home",
        "region": "US-OR",
        "orders": _orders("home"),
    }

    # Control: the same surname one letter longer is not in the taxonomy and IS refused, so
    # the fragment is trackable and only the vocabulary skip suppresses the real case.
    with pytest.raises(IdentityLeak):
        build_profile(
            {"last_name": "Homeward", "region": "US-OR", "orders": _orders("homeward")},
            PSN_A,
        )

    with pytest.raises(IdentityLeak):
        profile = build_profile(account, PSN_A)
        raise AssertionError(
            "the buyer's surname was published to the store-facing profile: "
            f"{profile.buckets.category_affinity!r}, "
            f"identity_leaks reported {identity_leaks(profile, account)!r}"
        )


# ======================================================================================
# T-221 — the k-anonymity floor has to be REACHABLE on the production path, and raising the
#         knob has to change what a store is shown
# ======================================================================================
# MARKER REMOVED, AND THE ASSERTION IT GUARDED REPLACED. justify-test-edit, recorded here
# because a test diff with no justification is indistinguishable from reward hacking six
# months later.
#
# QUOTED, the marker that was here:
#     @pytest.mark.xfail(strict=True, reason=(
#         "T-221: DEFAULT_K_ANONYMITY = 1 (profile/__init__.py:175) is a no-op floor unless "
#         "PROXYSHOP_BUYER_K_ANONYMITY is set, and build_profile — the ONLY path production "
#         "takes (magic_link.py:330) — never calls anonymise_cohort or k_anonymity_floor at "
#         "all, so the generalisation ladder T-138 delivered is unreachable and a rotated "
#         "pseudonym still publishes a byte-identical tuple; remove this marker with the fix"))
#
# QUOTED, the assertion that was here and is now gone:
#     floor = k_anonymity_floor({})
#     assert floor > 1, (
#         f"the default k-anonymity floor is {floor}: a floor of 1 puts every buyer alone in "
#         "their own equivalence class, which is exactly the re-linkability T-138 exists to "
#         "prevent, and PROXYSHOP_BUYER_K_ANONYMITY is the only thing that raises it")
#
# WOULD THIS TEST STILL BE WRONG IF I REVERTED MY CHANGE? YES — which is what makes this a
# TEST-IS-WRONG edit and not green-pressure. `k_anonymity_floor({}) > 1` fails against every
# tree this repo has ever had, today's fix included, because SPEC pins the opposite value in
# two places and the product obeys SPEC:
#     SPEC.md:56 (Non-goals) — "No k-anonymity enforcement beyond a configurable floor
#         (default 1 in fixtures; production knob documented)."
#     SPEC.md:84 (A6) — "k defaults to 1 in fixtures (non-goal notes the production knob)."
# THE USER RULED ON THE CONTRADICTION, verbatim: "If users only have anonymity at scale,
# that is totally fine and expected. Obviously, they wouldn't have anonymity at k = 1."
# Anonymity-at-scale is the intended posture. DEFAULT_K_ANONYMITY therefore STAYS 1, no
# product code was touched by this edit, and the argument is not to be re-opened. Measured
# elsewhere in this cycle and reported alongside the ruling, not by this node: raising the
# default broke 47 tests across six files, 41 of which encode intended behaviour rather than
# this defect. The ticket over-reached; the spec was right.
#
# WHAT REMAINS TRUE IS THE HALF WORTH GRADING, and it was a real defect. Before today
# `build_profile` — the only builder production reaches (`MagicLinkAuth.profile_for`,
# auth/magic_link.py:560) — never read the floor and never called the ladder, so a
# deployment that set PROXYSHOP_BUYER_K_ANONYMITY got rung 0 whatever it set: the
# generalisation ladder T-138 delivered was unreachable in a deployment. REACHABILITY, not
# the default, was the bug. So this node now grades reachability and effect AT THE
# CONFIGURED k, driven end to end through the login path rather than asserted about a
# constant:
#   (1) at k=1 — both spellings, knob unset and knob set to 1 — the login path still
#       publishes rung 0, value for value: the new wiring costs honest traffic nothing;
#   (2) `build_profile` reads the knob itself, with no caller passing anything;
#   (3) with the knob raised, what `profile_for` actually publishes changes, and the
#       profiles published across the whole directory form a release whose smallest
#       equivalence class holds at least k buyers;
#   (4) a floor too large for the population withholds every facet, and never falls through
#       to the fine-grained tuple.
# A gate that pins a number the spec sets elsewhere is a gate that fights the spec forever.
# These four are properties of the product, and SPEC agrees with all four.
#
# THE AST PROBE WENT WITH IT. `_references(build_profile, FLOOR_SYMBOLS)` asked whether the
# floor's NAME appears in a function body — a proxy for reachability that a never-executed
# call satisfies, and that pins one call-site shape. Driving `MagicLinkAuth.profile_for` and
# reading what it publishes is the property itself and strictly subsumes the proxy: a
# `profile_for` that read the floor and then hardcoded k=1 still parses as "references
# k_anonymity_floor" and fails (3) at once. `FLOOR_SYMBOLS`, `_references` and the
# `inspect`/`textwrap` imports were used by this node alone, so they were deleted rather
# than left dead.
#
# NO MARKER SURVIVES, and that is a decision rather than tidying. The ticket's default half
# is not a defect (SPEC + the ruling above) and its reachability half is fixed and asserted
# below, so there is nothing left for an xfail to encode; per this file's convention (module
# docstring, line 10: "the marker cannot outlive the bug") the node is now a live regression
# test. One thing this node deliberately does NOT grade, because it belongs to T-140 and not
# here: `build_account_directory` (auth/routes.py:646) returns an in-memory directory that
# forgets every buyer on restart. That costs the cohort its contents after a restart, not
# its correctness — an empty directory is a release of one, which no floor above 1 admits,
# so the documented answer is to withhold everything (4) and never to publish rung 0.
#
# NOT VACUOUS, MEASURED, not argued. Four separate sabotages of the product were applied one
# at a time and reverted; each turns this node RED, and each trips a DIFFERENT one of the
# four claims, so no claim is riding on another's coverage:
#   (a) auth/magic_link.py:558-559, the production path's floor read, neutered to
#       `floor = 1` / `cohort = ()` — RED at (3): "PROXYSHOP_BUYER_K_ANONYMITY=3 changed
#       nothing the login path published for [all six buyers]". This is the T-221 defect
#       itself, put back; the node sees it.
#   (b) profile/__init__.py build_profile, `k_anonymity_floor() if k is None else k` weakened
#       to `1 if k is None else k` — RED at (2): the builder no longer reads the knob, so a
#       deployment could only get a floor by having every caller pass one.
#   (c) profile/__init__.py build_profile, the unsatisfiable-floor branch changed from
#       `UNSATISFIABLE_FLOOR_LEVEL` to `0` — RED at (4): a floor of 7 over 6 buyers published
#       the fine-grained tuple instead of withholding, which is the floor pretending to hold.
#   (d) DEFAULT_K_ANONYMITY raised from 1 to 3 — RED at (1), quoting SPEC.md:56 and :84: an
#       unconfigured deployment stopped getting rung 0. Recorded because it is the direction
#       the ORIGINAL gate demanded; the node now refuses it, on the spec's authority and the
#       user's, instead of demanding it.
# Every other node in this file is untouched by all four, and with the tree restored
# byte-for-byte to HEAD this node passes.

#: A buyer directory shaped like a real one: identity fields the coarseners must never read,
#: order histories with distinct budgets, categories and frequencies. Distinct on purpose —
#: the test asserts that every one of these lands in its own class of ONE at the default,
#: which is the precondition that makes "a floor of 3 changed the release" mean something.
#: A fixture whose rung-0 tuples happened to collide would satisfy k=3 without generalising
#: anything and the node would prove nothing.
_DIRECTORY: dict[str, dict[str, Any]] = {
    "dana.reyes@example.com": {
        "first_name": "Dana",
        "last_name": "Reyes",
        "region": "US-OR",
        "orders": [
            {"order_ref": "ord-1", "total": 140.0, "category": "camera-lenses"},
            {"order_ref": "ord-2", "total": 210.0, "category": "camera-lenses"},
        ],
    },
    "samir.okafor@example.com": {
        "first_name": "Samir",
        "last_name": "Okafor",
        "region": "US-CA",
        "orders": [{"order_ref": "ord-3", "total": 38.0, "category": "espresso"}],
    },
    "wei.chen@example.com": {
        "first_name": "Wei",
        "last_name": "Chen",
        "region": "CA-BC",
        "orders": [
            {"order_ref": "ord-4", "total": 610.0, "category": "headphones"},
            {"order_ref": "ord-5", "total": 720.0, "category": "headphones"},
            {"order_ref": "ord-6", "total": 95.0, "category": "cookware"},
        ],
    },
    "ola.nilsen@example.com": {
        "first_name": "Ola",
        "last_name": "Nilsen",
        "region": "US-NY",
        "orders": [
            {"order_ref": "ord-7", "total": 42.0, "category": "dog-food"},
            {"order_ref": "ord-8", "total": 51.0, "category": "dog-food"},
        ],
    },
    "priya.nair@example.com": {
        "first_name": "Priya",
        "last_name": "Nair",
        "region": "US-WA",
        "orders": [{"order_ref": "ord-9", "total": 330.0, "category": "tents"}],
    },
    "juan.ortiz@example.com": {
        "first_name": "Juan",
        "last_name": "Ortiz",
        "region": "MX-JAL",
        "orders": [
            {"order_ref": "ord-10", "total": 175.0, "category": "trail-gear"},
            {"order_ref": "ord-11", "total": 160.0, "category": "trail-gear"},
            {"order_ref": "ord-12", "total": 180.0, "category": "trail-gear"},
        ],
    },
}


def _class_sizes(profiles: dict[str, Any]) -> list[int]:
    """How many of ``profiles`` share each published quasi-identifier tuple, ascending.

    Measured with the product's own :func:`~buyer_svc.profile.equivalence_class` rather than
    a tuple rebuilt here: "how many buyers share this profile" is the quantity a floor turns
    on, and an auditor who re-derives it can drift from what the floor is actually counting.
    """
    from buyer_svc.profile import equivalence_class  # noqa: PLC0415

    counts: dict[tuple[Any, ...], int] = {}
    for profile in profiles.values():
        key = equivalence_class(profile.buckets)
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.values())


def _set_floor(monkeypatch: pytest.MonkeyPatch, floor: int | None) -> None:
    """Configure the deployment knob, or explicitly unset it.

    The environment is set rather than a ``k=`` argument threaded in, because "a deployment
    sets ``PROXYSHOP_BUYER_K_ANONYMITY`` and the login path spends it" is the whole claim; a
    test that passed ``k`` by hand would grade a parameter, not a deployment. Unset is made
    explicit for the same reason the old node passed ``{}``: whatever the surrounding shell
    exports must not be able to change the reading.
    """
    from buyer_svc.profile import K_ANONYMITY_ENV  # noqa: PLC0415

    if floor is None:
        monkeypatch.delenv(K_ANONYMITY_ENV, raising=False)
    else:
        monkeypatch.setenv(K_ANONYMITY_ENV, str(floor))


def _buyer_service(monkeypatch: pytest.MonkeyPatch, floor: int | None) -> tuple[Any, list[str]]:
    """A ``MagicLinkAuth`` over :data:`_DIRECTORY` at ``floor``, plus the tokens it delivers."""
    from buyer_svc.auth import InMemoryAccountDirectory, MagicLinkAuth  # noqa: PLC0415

    _set_floor(monkeypatch, floor)
    tokens: list[str] = []

    def deliver(email: str, token: str, expires_at: Any) -> None:
        tokens.append(token)

    service = MagicLinkAuth(accounts=InMemoryAccountDirectory(_DIRECTORY), deliver=deliver)
    return service, tokens


def _login_and_read_profiles(service: Any, tokens: list[str]) -> dict[str, Any]:
    """Every buyer in the directory logged in for real, mapped to the profile served back.

    The whole login gesture per buyer — ``request_login`` -> the delivered token ->
    ``redeem`` -> ``profile_for`` — because the claim under test is about the path
    production takes, and a profile obtained by calling the builder directly is not it.
    """
    profiles: dict[str, Any] = {}
    for email in _DIRECTORY:
        service.request_login(email)
        session = service.redeem(tokens[-1])
        profiles[email] = service.profile_for(session.session_id)
    return profiles


def test_t221_the_k_anonymity_floor_is_on_by_default_and_reaches_the_production_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor is read on the production path by default, and spending it changes the release.

    Read the node's name as what it grades: the floor *read* is on by default — every
    profile production builds consults it with no caller passing anything — NOT that the
    default value is above 1, which SPEC.md:56 and :84 forbid and the user ruled against
    (see the block above). The name is kept verbatim because T-221's own gate command in
    ``tickets.json`` selects this node with a ``-k`` expression spelling it out in full, and
    that file is frozen and protected (T-328): renaming the node would leave the ticket's
    gate selecting nothing and exiting 5 rather than running.

    Four claims, in the order a deployment meets them: honest traffic at the default, the
    builder's own read of the knob, the effect of raising it on the path production takes,
    and the direction it fails in when the population is too small to satisfy it.
    """
    from buyer_svc import profile as profile_mod  # noqa: PLC0415
    from buyer_svc.profile import (  # noqa: PLC0415
        build_buckets,
        build_profile,
        equivalence_class,
    )

    _assert_in_tree(profile_mod)

    # (1) HONEST TRAFFIC AT k = 1, BOTH SPELLINGS OF IT. Six real buyers log in — once with
    # the knob unset, once with it set to 1 explicitly — and every one must come back rung 0,
    # value for value. Deliberately NOT `assert k_anonymity_floor() == 1`: pinning the number
    # here would put SPEC's default in a second place that has to be edited to change it, and
    # a gate that pins a number the spec sets elsewhere is a gate that fights the spec
    # forever. The behaviour is what a deployment actually experiences, and the explicit-1
    # pass makes the honest-traffic claim ("the ladder wiring costs k=1 nothing") independent
    # of whatever the default happens to be.
    service, tokens = _buyer_service(monkeypatch, None)
    at_default = _login_and_read_profiles(service, tokens)
    explicit, explicit_tokens = _buyer_service(monkeypatch, 1)
    at_explicit_one = _login_and_read_profiles(explicit, explicit_tokens)
    for email, profile in at_default.items():
        record = service.accounts.get(email)
        assert profile.buckets == build_buckets(record), (
            f"with no floor configured, the login path published {profile.buckets!r} for "
            f"{email} where build_buckets publishes {build_buckets(record)!r}. SPEC.md:56 "
            "('No k-anonymity enforcement beyond a configurable floor (default 1 in "
            "fixtures; production knob documented)') and SPEC.md:84 put the unconfigured "
            "deployment at k=1, and the user ruled that anonymity-at-scale is the intended "
            "posture — so an unconfigured deployment gets rung 0, value for value, and the "
            "k-anonymity wiring must not cost it anything"
        )
        assert at_explicit_one[email].buckets == profile.buckets, (
            f"PROXYSHOP_BUYER_K_ANONYMITY=1 published {at_explicit_one[email].buckets!r} for "
            f"{email} where the unset knob published {profile.buckets!r}: k=1 is documented "
            "as no enforcement at all, so the two spellings of it cannot differ"
        )

    # The precondition that makes (3) mean something: at the default every one of these
    # buyers is alone in their own equivalence class, so a release satisfying k=3 CANNOT be
    # reached by leaving the tuples as they are.
    singletons = _class_sizes(at_default)
    assert singletons == [1] * len(_DIRECTORY), (
        f"this fixture is supposed to publish {len(_DIRECTORY)} distinct rung-0 tuples so "
        f"that a floor of 3 has to generalise something; class sizes were {singletons}. A "
        "fixture whose tuples collide would satisfy the floor without the ladder running"
    )

    # (2) THE BUILDER READS THE KNOB ITSELF. No `k=` is passed here: if the default for `k`
    # is not `k_anonymity_floor()`, a deployment's environment reaches nothing, which is the
    # shape the ticket's second half described.
    _set_floor(monkeypatch, 3)
    cohort = list(_DIRECTORY.values())
    account = cohort[0]
    unconfigured = build_profile(account, PSN_A, k=1, cohort=cohort)
    from_env = build_profile(account, PSN_A, cohort=cohort)
    assert from_env.buckets != unconfigured.buckets, (
        "build_profile published the same buckets with PROXYSHOP_BUYER_K_ANONYMITY=3 as it "
        f"did at k=1 ({unconfigured.buckets!r}), so its `k=None` default is not reading "
        "k_anonymity_floor() and no deployment can configure a floor without every caller "
        "passing one"
    )

    # (3) EFFECT AT THE CONFIGURED k, ON THE PATH PRODUCTION TAKES. The same six buyers log
    # in with the floor raised; what the login path publishes must change, and the release
    # those published profiles form must actually satisfy the floor.
    floor = 3
    service, tokens = _buyer_service(monkeypatch, floor)
    published = _login_and_read_profiles(service, tokens)
    changed = [
        email
        for email, profile in published.items()
        if profile.buckets != at_default[email].buckets
    ]
    assert len(changed) == len(_DIRECTORY), (
        f"PROXYSHOP_BUYER_K_ANONYMITY={floor} changed nothing the login path published for "
        f"{sorted(set(_DIRECTORY) - set(changed))}: MagicLinkAuth.profile_for still serves "
        "the rung-0 tuple, so the generalisation ladder is unreachable from the only path "
        "production takes and the knob is decoration"
    )
    sizes = _class_sizes(published)
    assert min(sizes) >= floor, (
        f"the profiles GET /buyer/profile actually published form classes of {sizes} at "
        f"k={floor}: a buyer in a class of {min(sizes)} is re-linkable across a pseudonym "
        "rotation by whoever holds the tuple, which is the whole point of the floor"
    )

    # (4) A FLOOR THE POPULATION CANNOT SATISFY WITHHOLDS, and does not fall through. The
    # direction of failure is the safety property: publishing the fine-grained tuple when
    # the floor cannot be met would be the defect wearing the fix's name.
    unsatisfiable = len(_DIRECTORY) + 1
    service, tokens = _buyer_service(monkeypatch, unsatisfiable)
    for email, profile in _login_and_read_profiles(service, tokens).items():
        assert equivalence_class(profile.buckets) == (None, (), None, None, None), (
            f"a floor of {unsatisfiable} over {len(_DIRECTORY)} buyers cannot be satisfied "
            f"by any generalisation, and the login path published {profile.buckets!r} for "
            f"{email} anyway. Withholding every facet is the documented answer; falling "
            "through to the fine-grained tuple is the floor pretending to hold"
        )


# ======================================================================================
# T-163 — SessionStore.open() checks the pseudonym's FORMAT and never its vault membership
# ======================================================================================
# MARKER REMOVED — T-163 is fixed. justify-test-edit, recorded here because a test diff with
# no justification is indistinguishable from reward hacking six months later.
#
# QUOTED, the marker that was here:
#     @pytest.mark.xfail(strict=True, reason=(
#         "T-163: InMemorySessionStore.open (auth/sessions.py:170) refuses only on the "
#         "PSEUDONYM_PREFIX and holds no vault reference at all, so any 'psn-'-prefixed "
#         "string — including 'psn-' glued onto the buyer's own email — opens a session that "
#         "authenticates GET /buyer/profile; the pseudonym is a bearer credential whose "
#         "format is its only proof; remove this marker with the fix"))
#
# WHAT IT ENCODED: not a requirement — the *presence of the defect*. It is the file's own
# convention (module docstring, line 10: "the marker cannot outlive the bug"): while the bug
# is live the reproduction runs as `xfailed` and the repo-wide build gate stays green; the
# instant the bug is fixed the test XPASSes and `strict=True` turns that into a red, which is
# what forces this deletion. The assertions below are untouched — the marker was the only
# edit, and it was the one the marker itself asked for.
#
# WOULD THIS TEST STILL BE WRONG IF I REVERTED MY CHANGE? No, and that is the whole proof.
# MEASURED, not argued: with auth/sessions.py, auth/magic_link.py and auth/__init__.py put
# back to their HEAD (ad9583d) contents, this file reports `7 passed, 5 xfailed` — this node
# among the xfailed. With the fix restored it is the only marker in the file that flips, so
# the XPASS is caused by this repair and by nothing else. The marker is not being removed
# because the test is inconvenient; it is being removed because its subject no longer exists.
#
# THE FIX IT NAMES: SessionStore.admit() (auth/sessions.py) now asks a bound
# PseudonymRegistry whether the subject was ever issued, and MagicLinkAuth.__post_init__
# binds its own vault into the session store, so the production path refuses a forged
# 'psn-<email>' with UnissuedPseudonym instead of opening a session for it.
def test_t163_a_session_subject_that_no_vault_ever_issued_cannot_open_a_session() -> None:
    """Format is not membership. A session subject has to have been issued by the vault.

    Deliberately *not* coupled to how the repair is shaped. The store comes from
    ``MagicLinkAuth``'s own default factory rather than being constructed here, so threading
    a vault reference into ``SessionStore`` — the repair the ticket names, which changes that
    constructor's signature — does not break the probe. And the refusal is caught as
    ``SessionError``, the base, so a fix that draws a new distinction ("prefix fine, never
    issued") with its own subclass still satisfies this gate.
    """
    from buyer_svc.auth import InMemoryAccountDirectory, MagicLinkAuth  # noqa: PLC0415
    from buyer_svc.auth import sessions as sessions_mod  # noqa: PLC0415
    from buyer_svc.auth.sessions import SessionError  # noqa: PLC0415

    _assert_in_tree(sessions_mod)

    service = MagicLinkAuth(accounts=InMemoryAccountDirectory())

    # Control: a subject with no prefix is refused, so the guard is live and this test is not
    # measuring an absent code path.
    with pytest.raises(SessionError):
        service.sessions.open("dana.reyes@example.com")

    forged = "psn-dana.reyes@example.com"
    assert service.vault.resolve(forged) is None, (
        "fixture error: the forged subject must be one no vault ever issued"
    )

    with pytest.raises(SessionError):
        session = service.sessions.open(forged)
        raise AssertionError(
            "a subject the vault never issued opened a session: "
            f"session_id={session.session_id!r} pseudonym={session.pseudonym!r}"
        )


# ======================================================================================
# T-165 — the unauthenticated magic-link endpoint has no rate limit of any kind
# ======================================================================================
# MARKER REMOVED — T-165 is fixed. justify-test-edit, recorded at the removal site.
#
# QUOTED, the marker that was here:
#     @pytest.mark.xfail(strict=True, reason=(
#         "T-165: POST /buyer/auth/magic-link is unauthenticated and unmetered — no "
#         "rate-limit, throttle or backoff symbol exists in auth/routes.py, and the only "
#         "ceiling (MagicLinkAuth.max_pending, which magic_link.py:248 itself calls 'not a "
#         "rate limiter') never fires for a repeated address because each request supersedes "
#         "the last, so 20 requests mail 20 live login tokens to one mailbox with "
#         "pending_links pinned at 1; remove this marker with the fix"))
#
# WHAT IT ENCODED: the presence of the defect, per this file's convention (module docstring,
# line 10: "the marker cannot outlive the bug"). It is not a requirement — the requirement is
# the assertion body, which is untouched.
#
# WOULD THIS TEST STILL BE WRONG IF I REVERTED MY CHANGE? No. MEASURED: with
# auth/routes.py alone put back to its contents at 8242046 (the T-163 commit, i.e. this
# file's only other change already in place), this file reports `8 passed, 4 xfailed` with
# THIS node among the xfailed and T-140, T-142 and T-221 still xfailed alongside it. Restore
# routes.py and this is the only node that flips. The XPASS is caused by the limiter and by
# nothing else.
#
# THE FIX IT NAMES: MagicLinkRateLimiter (auth/routes.py) now charges every
# POST /buyer/auth/magic-link against a per-address budget — five links per fifteen minutes
# by default, overridable per deployment — and the route answers 429 with a Retry-After when
# it is spent. It sits in FRONT of MagicLinkAuth.request_login rather than inside it,
# because the memory ceiling and the mailbox budget are two different properties: the
# thousand-call assertion in test_auth_vault.py's
# test_pending_links_do_not_accumulate_for_an_unauthenticated_caller still holds, and still
# passes.
def test_t165_repeated_unauthenticated_magic_link_requests_are_eventually_refused() -> None:
    """One caller cannot mail an unbounded number of login tokens into one mailbox."""
    from buyer_svc.auth import InMemoryAccountDirectory, MagicLinkAuth  # noqa: PLC0415
    from buyer_svc.auth.routes import get_auth_service  # noqa: PLC0415
    from buyer_svc.main import create_app  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    delivered: list[str] = []
    service = MagicLinkAuth(
        accounts=InMemoryAccountDirectory(),
        deliver=lambda email, token, expires_at: delivered.append(token),
    )
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: service
    client = TestClient(app, raise_server_exceptions=False)

    attempts = 20
    body = {"email": "victim@example.com"}
    codes = [client.post("/buyer/auth/magic-link", json=body).status_code for _ in range(attempts)]

    # Control: the endpoint really is reachable unauthenticated, so a green here would mean
    # a limiter and not a broken probe.
    assert codes[0] == 202, f"the magic-link door did not accept a first request: {codes[0]}"

    refused = [code for code in codes if code == 429]
    assert refused, (
        f"{attempts} unauthenticated magic-link requests for one address were all accepted "
        f"({sorted(set(codes))}); {len(delivered)} live login tokens were handed to the "
        f"mail transport while service.pending_links stayed at {service.pending_links}, so "
        "the memory ceiling structurally cannot see this abuse"
    )


# ======================================================================================
# shared machinery for T-140 and T-142
# ======================================================================================
#: The whole family, not the one member a gate happens to know about. ``PROXYSHOP_PG_DSN_*``
#: has five documented members (``_ADMIN``, ``_EXCHANGE``, ``_TRUST_RW``, ``_VAULT``,
#: ``_APP``; proxyshop_support/postgres.py:91-95) and ``apps/buyer/compose.yaml:31-32`` hands
#: the buyer service two of them. A gate that cleared only the one it expected would take a
#: reading that depends on the runner's shell.
_PG_DSN_PREFIX = "PROXYSHOP_PG_DSN"


def _clear_datastore_environment(monkeypatch: Any, extra: tuple[str, ...] = ()) -> None:
    """Take the runner's shell out of the reading, then hand back nothing.

    Every DSN in the family goes, so a repair that gates buyer records on a *new* member of
    it cannot be red on a dev box and green — against a live database, in a file whose
    standing rule is that nothing here touches one — in whatever shell exports it. The
    worker-count variables go with them: ``build_auth_service`` raises
    ``ProcessLocalStateUnsafe`` when one of them asks for more than one worker, which would
    be a red for a reason that is not the ticket.
    """
    import os  # noqa: PLC0415

    for name in list(os.environ):
        if name.startswith(_PG_DSN_PREFIX) or name in extra:
            monkeypatch.delenv(name, raising=False)


def _production_modules() -> list[pathlib.Path]:
    """Every non-test ``*.py`` the product ships. Used only by armed sweeps."""
    roots = ("apps", "services", "packages", "proxyshop_support", "pixel", "e2e")
    found: list[pathlib.Path] = []
    for root in roots:
        directory = REPO_ROOT / root
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            if set(path.parts) & {"tests", ".venv", "node_modules", "__pycache__"}:
                continue
            if path.name.startswith("test_") or path.name.endswith("_test.py"):
                continue
            found.append(path)
    return found


# ======================================================================================
# T-140 — build_auth_service() wires no account directory, so nothing ever populates one
# ======================================================================================
#: Regions and merchandising slugs the generated buyers are drawn from. Fixed English words,
#: deliberately disjoint from the random alphabetic local parts and domains below, so that no
#: generated buyer can trip the identity backstop and turn this gate red for a reason that is
#: not the ticket. The spread canaries assert the draw actually used them.
_T140_REGIONS = ("US-OR", "US-CA", "GB", "DE-BE", "CA-BC", "FR", "JP", "AU-NSW")
_T140_CATEGORIES = (
    "camera-lenses",
    "hiking-boots",
    "espresso-gear",
    "trail-runners",
    "desk-lamps",
    "wool-socks",
    "board-games",
    "garden-tools",
)
#: Totals chosen to straddle several :data:`~buyer_svc.profile.BUDGET_BANDS` boundaries.
_T140_TOTALS = (18.0, 42.0, 74.0, 128.0, 240.0, 310.0, 620.0, 1450.0)

#: The SHAPE draws are reproducible; the ADDRESSES deliberately are not. A pinned seed on its
#: own is a parametrized probe in a costume — a repair could key on the sixty addresses it is
#: always shown. ``SystemRandom`` supplies the identity bytes so no two runs agree on a single
#: address, and the canaries below assert the generator's own spread instead of trusting it.
_T140_SHAPE_SEED = 20260904
_T140_BUYERS = 60


def _draw_buyers(count: int, seed: int) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Draw ``count`` synthetic buyers. Shared by T-140, T-142 and both arming tests.

    Lifted out of the graded bodies deliberately. While the draw and its canaries lived
    *inside* a ``xfail(strict=True)`` test, a generator that collapsed — one address repeated,
    one order shape, every buyer coarsening to the same profile — raised inside the marker and
    was reported as ``xfailed``: the same word the live defect produces. The gate could not
    tell "the defect is present" from "my fixture is broken". The canaries now live in
    :func:`test_t140_the_generated_buyer_sweep_is_armed` and
    :func:`test_t142_the_published_row_sweep_is_armed`, neither of which carries a marker.

    The SHAPE draw is seeded and reproducible; the ADDRESSES are drawn from
    :class:`random.SystemRandom` and deliberately are not, so no repair can key on a fixed
    sixty addresses. The arming tests assert both halves of that rather than trusting it.
    """
    import random  # noqa: PLC0415
    import string  # noqa: PLC0415

    shapes = random.Random(seed)
    identities = random.SystemRandom()

    emails: list[str] = []
    accounts: dict[str, dict[str, Any]] = {}
    for _ in range(count):
        local = "".join(identities.choice(string.ascii_lowercase) for _ in range(10))
        domain = "d" + "".join(identities.choice(string.ascii_lowercase) for _ in range(10))
        email = f"{local}@{domain}.example"
        categories = shapes.sample(_T140_CATEGORIES, shapes.randint(1, 3))
        orders = [
            {
                "order_ref": f"ord-{index}",
                "total": shapes.choice(_T140_TOTALS),
                "category": categories[index % len(categories)],
            }
            for index in range(shapes.randint(1, 7))
        ]
        emails.append(email)
        accounts[email] = {
            "email": email,
            "region": shapes.choice(_T140_REGIONS),
            "orders": orders,
        }
    return emails, accounts


def _coarsened(accounts: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Each drawn account's published buckets, canonicalised, keyed by address.

    ``build_profile`` is called directly rather than over HTTP: this is the *target* a served
    profile has to match, so it must be read from the record and not from the path under test.
    """
    import json  # noqa: PLC0415

    from buyer_svc.profile import build_profile  # noqa: PLC0415

    return {
        email: json.dumps(build_profile(account, PSN_A).model_dump()["buckets"], sort_keys=True)
        for email, account in accounts.items()
    }


def _informationless_profile() -> str:
    """What a buyer the coarsener knows nothing about publishes."""
    import json  # noqa: PLC0415

    from buyer_svc.profile import build_profile  # noqa: PLC0415

    return json.dumps(
        build_profile({"email": "probe@nowhere.example", "orders": []}, PSN_A).model_dump()[
            "buckets"
        ],
        sort_keys=True,
    )


def _assert_draw_is_wide(
    emails: list[str],
    accounts: dict[str, dict[str, Any]],
    *,
    count: int,
    min_order_shapes: int,
    min_regions: int,
    min_affinities: int,
) -> None:
    """The generator's own spread. Every clause here is a way a sweep can report nothing.

    A generator that yields one address ``count`` times still reports ``count`` completed
    iterations, so DISTINCT inputs are counted, not loop trips.
    """
    assert len(emails) == count, (
        f"the generator returned {len(emails)} draws, not {count}; the sweep has shrunk"
    )
    assert len(set(emails)) == count, (
        f"the generator produced {len(set(emails))} distinct addresses across {count} draws; "
        "a sweep over a repeated address measures one buyer while reporting many"
    )
    assert len(accounts) == count, f"{count} addresses collapsed to {len(accounts)} account records"
    order_shapes = {len(account["orders"]) for account in accounts.values()}
    region_shapes = {account["region"] for account in accounts.values()}
    affinity_shapes = {
        tuple(sorted({order["category"] for order in account["orders"]}))
        for account in accounts.values()
    }
    assert len(order_shapes) >= min_order_shapes, (
        f"order-count shapes drawn: {sorted(order_shapes)}; fewer than {min_order_shapes} "
        "distinct histories means the buyers differ in name only"
    )
    assert len(region_shapes) >= min_regions, (
        f"regions drawn: {sorted(region_shapes)}; fewer than {min_regions}"
    )
    assert len(affinity_shapes) >= min_affinities, (
        f"category shapes drawn: {len(affinity_shapes)}; fewer than {min_affinities}"
    )


# --------------------------------------------------------------------------------------
# ARMING — NOT xfail. The T-140 gate below is worthless without this, and this must stay
# green. Every assertion here is one the gate used to make inside its own strict-xfail body,
# where a failure was indistinguishable from the defect it was built to catch.
# --------------------------------------------------------------------------------------
def test_t140_the_generated_buyer_sweep_is_armed(monkeypatch: Any) -> None:
    """Six ways T-140's sweep could measure nothing and still report ``xfailed``.

    1. **The draw collapses.** Sixty iterations over one address, or sixty buyers with one
       order shape, satisfy every count the gate takes. Distinctness is asserted, not counted.
    2. **The buyers are indistinguishable after coarsening.** The gate's teeth compare a
       served profile against ``build_profile`` of the record written for that buyer. If the
       sixty records coarsen to one or two profiles, a service that invented a constant would
       match most of them. Twelve distinct profiles is the floor the ticket recorded.
    3. **A generated buyer coarsens to the informationless profile.** For that buyer "the
       served profile changed after a history was written" is unobservable, so the gate's
       anti-hash clause (1b) would be vacuous for it.
    4. **The addresses are pinned.** The gate's whole reason for using ``SystemRandom`` for
       identity bytes is that a repair must not be able to key on a fixed corpus. Two draws
       are taken and required to be disjoint — the one way a "random" generator can be a
       parametrized probe in a costume.
    5. **The identity backstop refuses a generated buyer.** ``_T140_CATEGORIES`` and
       ``_T140_REGIONS`` are fixed English words chosen to be disjoint from the random local
       parts and domains precisely so no draw trips ``IdentityLeak``. If one did,
       ``build_profile`` would raise inside the gate and read as ``xfailed``. Calling it here,
       outside the marker, turns that into a red.
    6. **The reading depends on the runner's shell.** ``PROXYSHOP_BUYER_K_ANONYMITY`` changes
       what ``build_profile`` publishes, so the spread measured here is only the spread the
       gate will see if the environment is cleared the same way.
    """
    import os  # noqa: PLC0415

    from buyer_svc import profile as profile_mod  # noqa: PLC0415
    from buyer_svc.auth.routes import WORKER_COUNT_ENVS  # noqa: PLC0415

    _assert_in_tree(profile_mod)
    _clear_datastore_environment(
        monkeypatch, extra=(*WORKER_COUNT_ENVS, "PROXYSHOP_BUYER_K_ANONYMITY")
    )
    leftover = sorted(name for name in os.environ if name.startswith(_PG_DSN_PREFIX))
    assert not leftover, (
        f"{leftover} survived the environment clear, so the spread measured here is not the "
        "spread the gate will see"
    )

    # 1. the draw itself
    emails, accounts = _draw_buyers(_T140_BUYERS, _T140_SHAPE_SEED)
    _assert_draw_is_wide(
        emails,
        accounts,
        count=_T140_BUYERS,
        min_order_shapes=5,
        min_regions=4,
        min_affinities=6,
    )

    # 2 + 5. what those buyers publish, through the coarsener the gate compares against
    targets = _coarsened(accounts)
    assert len(set(targets.values())) >= 12, (
        f"the generated buyers coarsen to only {len(set(targets.values()))} distinct "
        "profiles; the draw is too narrow to distinguish a real populator from a constant"
    )

    # 3. no buyer is invisible to the anti-hash clause
    informationless = _informationless_profile()
    assert informationless not in set(targets.values()), (
        "fixture error: a generated buyer coarsens to the empty profile, so 'the served "
        "profile changed' would be unobservable for that buyer"
    )

    # 4. the identity bytes are genuinely redrawn
    second_emails, _second_accounts = _draw_buyers(_T140_BUYERS, _T140_SHAPE_SEED)
    shared = set(emails) & set(second_emails)
    assert not shared, (
        f"two draws of {_T140_BUYERS} buyers share {len(shared)} address(es) ({sorted(shared)[:3]}); "
        "the addresses are effectively pinned, and a repair could key on the corpus it is "
        "always shown while the gate reported a randomized sweep"
    )
    # ...while the SHAPES must be reproducible, or the thresholds above measure a lottery.
    _, third_accounts = _draw_buyers(_T140_BUYERS, _T140_SHAPE_SEED)
    assert [len(a["orders"]) for a in accounts.values()] == [
        len(a["orders"]) for a in third_accounts.values()
    ], "the seeded shape draw is not reproducible; the spread floors above are a coin flip"


# MARKER REMOVED — T-140 is fixed. justify-test-edit, recorded at the removal site.
#
# QUOTED, the marker that was here:
#     @pytest.mark.xfail(strict=True, reason=(
#         "T-140: build_auth_service() (auth/routes.py:133-134) decides the vault and "
#         "nothing else — the word 'accounts' does not appear in that module at all — so "
#         "production always gets the empty default InMemoryAccountDirectory "
#         "(magic_link.py:193), and the ONLY non-test writer of a buyer record in the tree "
#         "is redeem's self.accounts.upsert(email, {'email': email}) (magic_link.py:304). "
#         "There is no production populator, so a record can never carry anything the login "
#         "gesture did not already know, and a service rebuilt as a restart would rebuild it "
#         "has forgotten every buyer; remove this marker with the fix"))
#
# WHAT IT ENCODED: the presence of the defect, per this file's convention ("the marker
# cannot outlive the bug"). Not a requirement — every requirement is in the assertion body
# below, which is untouched, including clause (5)'s AST read of build_auth_service.
#
# WOULD THIS TEST STILL BE WRONG IF I REVERTED MY CHANGE? No. MEASURED: with
# auth/routes.py and auth/magic_link.py alone restored to their contents at f688b32 (the
# T-165 commit), this file reports `9 passed, 3 xfailed` with THIS node among the xfailed,
# beside T-142 and T-221. Restore the two files and this is the only node that flips; T-142
# stays xfailed either way, which is why only this marker is being removed.
#
# THE FIX IT NAMES: build_auth_service() now hands every service it builds
# `accounts=account_directory()` — one process-wide AccountDirectory behind a module-level
# factory (routes.py), installable by a deployment through set_account_directory(). A buyer
# record written through it therefore outlives the service that was running when it was
# written, so a rebuilt service reads the history rather than re-inventing a buyer out of
# the address. The in-memory default is still not durable across a real process restart;
# that half needs a table for the unredacted account record, which does not exist and needs
# a migration outside this module. It is reported rather than silently claimed.


def test_t140_the_production_login_path_serves_a_profile_that_reflects_the_buyer(
    monkeypatch: Any,
) -> None:
    """A served profile must be a coarsening of a buyer record, not a function of an address.

    The trap this gate is built to avoid: asserting only that served profiles *differ across
    buyers*. Distinctness is a necessary consequence of the repair and not a witness of it —
    the cheapest way to satisfy it is to widen ``magic_link.py:304`` so the stored record is
    derived from the email (a region hashed out of the domain), after which sixty randomized
    addresses hash to more than one profile, the served buckets are still a faithful
    coarsening of what is stored, and the finding — *no production populator; the profile
    reflects no real buyer* — is untouched.

    So the teeth are elsewhere. Each buyer's history is established through a channel the
    login gesture cannot see: it is written into the account directory **the production
    builder itself handed out**, never into a directory this test constructed and never as an
    argument to ``build_profile``, which is exactly what makes the frozen acceptance test
    (``.swarm-loop/acceptance/test_e7_buyer.py``) worthless as evidence here. Then the
    service is rebuilt the way a restart rebuilds it and the same address logs in again. The
    profile that comes back must carry the region, the spend band, the affinity list and the
    frequency tier of the account that was written — five values this test chose and no
    function of the address could invent.

    Note what is deliberately NOT asserted: nothing here says how the repair must be shaped.
    A process-wide directory behind a module-level factory, a DSN-backed one with an
    in-memory dev default, an importer — any of them satisfies this. What cannot satisfy it
    is a login that keeps inventing its own buyers.
    """
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415

    from buyer_svc import profile as profile_mod  # noqa: PLC0415
    from buyer_svc.auth import magic_link as magic_link_mod  # noqa: PLC0415
    from buyer_svc.auth import routes as routes_mod  # noqa: PLC0415
    from buyer_svc.auth.routes import WORKER_COUNT_ENVS  # noqa: PLC0415
    from buyer_svc.main import create_app  # noqa: PLC0415
    from buyer_svc.profile import build_profile  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    _assert_in_tree(routes_mod)
    _assert_in_tree(magic_link_mod)
    _assert_in_tree(profile_mod)

    _clear_datastore_environment(
        monkeypatch, extra=(*WORKER_COUNT_ENVS, "PROXYSHOP_BUYER_K_ANONYMITY")
    )
    # Asserted, not merely attempted: the reading below must not depend on the shell, and a
    # leftover DSN would also mean this test had reached a live datastore, which the module
    # docstring forbids outright.
    leftover = sorted(name for name in os.environ if name.startswith(_PG_DSN_PREFIX))
    assert not leftover, (
        f"{leftover} survived the environment clear, so this reading would depend on the "
        "runner's shell and could reach a live database"
    )

    # -- generate the buyers -----------------------------------------------------------
    # The draw's SPREAD is not judged here, on purpose. Under xfail(strict=True) a collapsed
    # generator raises inside this body and is reported as `xfailed` — the same word the live
    # defect produces — so a canary asserted here could never distinguish a broken fixture
    # from the finding. Every one of them now lives, unmarked and therefore observable, in
    # test_t140_the_generated_buyer_sweep_is_armed. `targets` is still computed because the
    # failure message below reports how many distinct histories were actually swept.
    emails, accounts = _draw_buyers(_T140_BUYERS, _T140_SHAPE_SEED)
    targets = _coarsened(accounts)

    # -- drive the production path ------------------------------------------------------
    tokens: dict[str, str] = {}

    def _capture(email: str, token: str, expires_at: Any) -> None:
        """Only the delivery transport is wrapped. No account data passes through here."""
        tokens[email] = token

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    def _login(email: str) -> tuple[str, dict[str, Any]]:
        posted = client.post("/buyer/auth/magic-link", json={"email": email})
        assert posted.status_code == 202, f"magic-link refused: {posted.status_code} {posted.text}"
        token = tokens.get(email)
        assert token, f"no login token was delivered for {email}"
        redeemed = client.post("/buyer/auth/session", json={"token": token})
        assert redeemed.status_code == 201, (
            f"redeem refused: {redeemed.status_code} {redeemed.text}"
        )
        opened = redeemed.json()
        read = client.get("/buyer/profile", headers={"X-Buyer-Session": opened["session_id"]})
        assert read.status_code == 200, f"profile refused: {read.status_code} {read.text}"
        return opened["pseudonym"], read.json()["buckets"]

    try:
        # (i) first login for every address, against a service built the way production
        # builds one. No accounts= is passed and no AccountDirectory is constructed here:
        # that abstention is the finding.
        routes_mod.set_auth_service(None)
        service = routes_mod.build_auth_service()
        routes_mod.set_auth_service(service)
        service.deliver = _capture

        baseline: dict[str, dict[str, Any]] = {}
        for email in emails:
            baseline[email] = _login(email)[1]
        # ARM: a loop that short-circuited, or one whose HTTP calls all failed into an
        # `except`, must be red here rather than quiet.
        assert len(baseline) == _T140_BUYERS, (
            f"only {len(baseline)} of {_T140_BUYERS} buyers completed a first login"
        )

        directory_type = type(service.accounts).__name__
        after_first_logins = [service.accounts.get(email) for email in emails]

        # (ii) establish each buyer's history through the directory the PRODUCTION builder
        # handed out — a path that is not `redeem`, and not an injected fixture.
        for email in emails:
            service.accounts.upsert(email, accounts[email])

        # (iii) a restart: a second service, built exactly the same way.
        routes_mod.set_auth_service(None)
        rebuilt = routes_mod.build_auth_service()
        routes_mod.set_auth_service(rebuilt)
        rebuilt.deliver = _capture

        served: dict[str, tuple[str, dict[str, Any]]] = {}
        for email in emails:
            served[email] = _login(email)
        assert len(served) == _T140_BUYERS, (
            f"only {len(served)} of {_T140_BUYERS} buyers completed a second login"
        )

        held = sum(1 for email in emails if rebuilt.accounts.get(email) is not None)

        # -- (1) THE TEETH -------------------------------------------------------------
        # Five values this test chose, none of them derivable from the address.
        mismatched: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for email in emails:
            pseudonym, buckets = served[email]
            expected = build_profile(accounts[email], pseudonym).model_dump()["buckets"]
            if buckets != expected:
                mismatched.append((email, buckets, expected))
        assert not mismatched, (
            f"{len(mismatched)} of {_T140_BUYERS} buyers were served a profile that does not "
            f"reflect the history written for them through the production service's own "
            f"account directory ({directory_type}, holding {held} of {_T140_BUYERS} of these "
            f"buyers after a rebuild). First: {mismatched[0][0]} was served "
            f"{json.dumps(mismatched[0][1], sort_keys=True)} where the record says "
            f"{json.dumps(mismatched[0][2], sort_keys=True)}. build_auth_service() passes no "
            "accounts=, so the rebuilt service got a fresh empty directory and the login "
            "invented a buyer out of the address again"
        )

        # -- (1b) it must have CHANGED, for that same address ---------------------------
        # The anti-hash clause, and the reason this gate takes a baseline at all. A repair
        # that widens magic_link.py:304 to derive the record from the email gives a buyer the
        # same synthesised profile on both logins — the address did not change — so this stays
        # red while every distinctness clause it could satisfy goes green.
        unchanged = [email for email in emails if served[email][1] == baseline[email]]
        assert not unchanged, (
            f"{len(unchanged)} of {_T140_BUYERS} buyers were served exactly the profile they "
            f"got on their FIRST login, after a full order history was written for them "
            f"through the production service's own account directory. A profile that does not "
            f"move when the buyer's history does is a function of the address, not of the "
            f"buyer; first: {unchanged[0]} served "
            f"{json.dumps(baseline[unchanged[0]], sort_keys=True)} both times"
        )

        # -- (2) the record the rebuilt service reads must carry more than the address ---
        # Asserted rather than reported in a message. Deliberately read AFTER the history was
        # written: a genuinely new buyer's record legitimately holds only an address, so it is
        # only the record a *restarted* service finds for a buyer with history that proves
        # whether anything but `redeem` ever wrote one.
        addressed_only = [
            email
            for email in emails
            if set(dict(rebuilt.accounts.get(email) or {})) <= {"email", "orders"}
        ]
        assert not addressed_only, (
            f"{len(addressed_only)} of {_T140_BUYERS} buyer records visible to a "
            f"freshly-built production service hold nothing but the address the login "
            f"already had, because redeem's upsert(email, {{'email': email}}) "
            f"(magic_link.py:304) is the only writer of a record in the tree and "
            f"build_auth_service() gives every new service its own empty "
            f"{directory_type}; {len(after_first_logins)} first logins were taken as the "
            "baseline"
        )

        # -- (3) the ticket's own reproduction, kept last ------------------------------
        # Necessary, not sufficient: a record hashed out of the email satisfies this clause
        # while leaving the finding entirely in place. It is here so the gate still names the
        # symptom the ticket was filed on. Read on the served profiles rather than the
        # baseline, because sixty brand-new buyers with no history SHOULD coarsen alike.
        distinct = {json.dumps(buckets, sort_keys=True) for _psn, buckets in served.values()}
        assert len(distinct) > 1, (
            f"{_T140_BUYERS} buyers with {len(set(targets.values()))} distinct histories were "
            f"served {len(distinct)} distinct profile(s) through GET /buyer/profile: "
            f"{sorted(distinct)}"
        )

        # -- (4) fabrication guard -----------------------------------------------------
        # The route must coarsen a stored record, never synthesise one. True today; here so a
        # future repair cannot satisfy (1) by building the profile at the route instead.
        fabricated = [
            email
            for email in emails
            if served[email][1]
            != build_profile(rebuilt.accounts.get(email) or {}, served[email][0]).model_dump()[
                "buckets"
            ]
        ]
        assert not fabricated, (
            f"{len(fabricated)} served profiles are not a coarsening of any record in the "
            "account directory; the route is manufacturing buckets downstream of the seam"
        )

        # -- (5) the DEPLOYMENT BUILDER must decide something about the directory -------
        # ANDed with the clauses above, never in place of them, and last because the
        # behavioural teeth are the better diagnostic. This exists to close one loophole the
        # behavioural clauses alone leave open: giving InMemoryAccountDirectory a CLASS-level
        # store would make every instance share state and satisfy (1) through (4) while
        # build_auth_service still decided nothing, which is the finding verbatim.
        #
        # SCOPED to build_auth_service's own body. The previous form accepted any of
        # {accounts, AccountDirectory, InMemoryAccountDirectory, account_directory} appearing
        # anywhere in the module, as a Name, Attribute, alias, keyword or def. It was argued
        # that a symbol SET keeps the gate from dictating whether the repair is a process-wide
        # default, a DSN-backed directory or an importer — and that goal is right; the gate
        # has no business choosing the repair's shape. But the implementation bought that
        # freedom with a clause a reference that decides NOTHING could satisfy: the audit's
        # sabotage (a class-level backing dict on InMemoryAccountDirectory, routes.py
        # otherwise untouched) plus one unused
        # `from .magic_link import InMemoryAccountDirectory` import is enough, because an
        # ast.alias anywhere in the module counted. The ticket's own finding is narrower than
        # "the module mentions accounts": it is that THE PRODUCTION CONSTRUCTOR decides the
        # vault and nothing else. So that is what is read — a real `accounts=` handed to
        # something inside build_auth_service, or a real assignment to an `.accounts`
        # attribute there. Both require editing the builder; neither says where the directory
        # comes from, so the design freedom the wider form was reaching for is untouched.
        # Parsed, never grepped: a docstring, a comment or an __all__ string is not a wiring.
        routes_path = pathlib.Path(routes_mod.__file__ or "")
        routes_tree = ast.parse(routes_path.read_text(encoding="utf-8"))
        builder = next(
            (
                node
                for node in ast.walk(routes_tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "build_auth_service"
            ),
            None,
        )
        # An absence has two readings — "the builder wires nothing" and "there is no builder
        # to read" — and only the first is this ticket. Rule the second one out loudly rather
        # than letting a renamed function read as a wiring that is missing.
        assert builder is not None, (
            f"no build_auth_service() is defined in {routes_path.name}, so this clause has "
            "nothing to read; that is an unreadable gate, not a failing wiring"
        )
        wired: set[str] = set()
        for node in ast.walk(builder):
            if isinstance(node, ast.keyword) and node.arg == "accounts":
                wired.add(f"accounts= at {routes_path.name}:{node.value.lineno}")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr == "accounts"
                and isinstance(node.ctx, ast.Store)
            ):
                wired.add(f".accounts assigned at {routes_path.name}:{node.lineno}")
        assert wired, (
            f"build_auth_service() ({routes_path.name}:{builder.lineno}) hands no `accounts=` "
            "to anything and assigns no `.accounts`: it decides the vault (_vault_from_env) "
            "and the worker count and nothing else, so every service this deployment builds "
            "gets the empty default InMemoryAccountDirectory and no deployment can configure "
            "where buyer records come from — whatever a shared in-memory directory might make "
            "the served profiles above look like"
        )
    finally:
        routes_mod.set_auth_service(None)


# ======================================================================================
# T-142 — publish_profile writes app.buyer_accounts and nothing production-side calls it
# ======================================================================================
_T142_SHAPE_SEED = 20260421
_T142_BUYERS = 40
#: A DSN that cannot connect to anything, carrying a sentinel this gate can recognise. It is
#: only ever handed to the doubled ``psycopg.connect`` below; finding it there is how the
#: gate observes that the deployment builder read ``PROXYSHOP_PG_DSN_APP`` at all.
_T142_APP_DSN = "postgresql://app:pw@127.0.0.1:1/proxyshop_t142_sentinel"


class _RecordingCursor:
    """A cursor that records statements instead of sending them."""

    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self._log = log

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, statement: Any, params: Any = None, **_kwargs: Any) -> _RecordingCursor:
        self._log.append((str(statement), params))
        return self

    def executemany(self, statement: Any, seq: Any, **_kwargs: Any) -> _RecordingCursor:
        for params in seq:
            self._log.append((str(statement), params))
        return self

    def fetchone(self) -> Any:
        return None

    def fetchall(self) -> list[Any]:
        return []

    def close(self) -> None:
        return None


class _RecordingConnection:
    """A stand-in for a real ``app``-role connection. Permissive on purpose.

    It answers every shape a writer might plausibly use — ``with conn.cursor() as cur``, a
    bare ``conn.cursor()``, ``conn.transaction()``, ``commit``/``rollback``/``close``, and use
    as a context manager — so that a repair which genuinely tries to write a row is recorded
    rather than crashing into an ``AttributeError`` this gate would then misread as "no row".
    """

    def __init__(self, log: list[tuple[str, Any]], dsns: list[Any], dsn: Any) -> None:
        self._log = log
        dsns.append(dsn)

    def cursor(self, *_a: Any, **_k: Any) -> _RecordingCursor:
        return _RecordingCursor(self._log)

    def transaction(self, *_a: Any, **_k: Any) -> _RecordingCursor:
        return _RecordingCursor(self._log)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _published_buckets(params: Any) -> list[dict[str, Any]]:
    """Every bucket object hiding in one statement's parameters, however it was passed."""
    import json  # noqa: PLC0415

    found: list[dict[str, Any]] = []
    candidates = list(params) if isinstance(params, (list, tuple)) else [params]
    if isinstance(params, dict):
        candidates = list(params.values())
    for candidate in candidates:
        if isinstance(candidate, dict):
            found.append(candidate)
            continue
        if isinstance(candidate, (str, bytes)):
            try:
                decoded = json.loads(candidate)
            except (ValueError, TypeError):
                continue
            if isinstance(decoded, dict):
                found.append(decoded)
    return found


def _t142_publish_scan() -> tuple[list[pathlib.Path], str, list[str], list[str]]:
    """The production-module sweep both the gate and its arming test read.

    Returns every non-test module the product ships, ``publish_profile``'s own definition
    site, its call sites, and the modules that mention ``buyer_accounts`` at all. Parsed,
    never grepped: ``"publish_profile"`` is already a string in ``__all__``
    (profile/__init__.py:125) and appears in docstrings, and neither is a call.
    """
    import ast as ast_mod  # noqa: PLC0415

    modules = _production_modules()
    definition = ""
    callers: list[str] = []
    referencing_buyer_accounts: list[str] = []
    for path in modules:
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(REPO_ROOT).as_posix()
        if "buyer_accounts" in source:
            referencing_buyer_accounts.append(relative)
        try:
            tree = ast_mod.parse(source)
        except SyntaxError:  # pragma: no cover - a module that will not parse is not a caller
            continue
        for node in ast_mod.walk(tree):
            if isinstance(node, ast_mod.FunctionDef) and node.name == "publish_profile":
                definition = f"{relative}:{node.lineno}"
            if not isinstance(node, ast_mod.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast_mod.Name)
                else func.attr
                if isinstance(func, ast_mod.Attribute)
                else None
            )
            if name == "publish_profile":
                callers.append(f"{relative}:{node.lineno}")
    return modules, definition, callers, referencing_buyer_accounts


# --------------------------------------------------------------------------------------
# ARMING — NOT xfail. Four independent apparatus this gate depends on, each of which can
# fail silently inside a strict-xfail body and be reported as the defect.
# --------------------------------------------------------------------------------------
def test_t142_the_published_row_sweep_is_armed(monkeypatch: Any) -> None:
    """The gate below concludes from ABSENCES — no connection, no statement, no caller.

    An absence has two readings every time, and only one of them is the ticket. These are
    the four ways to get the wrong one, and each is closed by an assertion rather than by
    inspection:

    1. **The draw collapses.** Forty logins over one address, or forty buyers coarsening to
       one profile, would let a publisher that wrote a single constant row look correct. The
       distinctness and the spread are asserted here, where a failure is a red rather than an
       ``xfailed``.
    2. **The production path does not run.** If the forty logins never completed — a refused
       magic link, a 500 on the profile route — the gate would find no statement naming
       ``app.buyer_accounts`` for a reason that has nothing to do with ``publish_profile``.
       The same forty logins are driven here, in the SAME configuration (``psycopg.connect``
       doubled and ``PROXYSHOP_PG_DSN_APP`` set to the sentinel), and required to succeed.
       Matching the configuration is load-bearing rather than tidy: an earlier version of
       this test cleared that DSN while the gate set it, so a login-path failure conditional
       on the one variable the gate introduces was invisible here and read as ``xfailed``
       there. See the comment at the loop.
    3. **The observation apparatus is dead.** This is the load-bearing one. The gate does not
       double ``publish_profile``; it doubles the *connection* and then asserts row-shaped
       facts. If ``_RecordingCursor`` stopped recording, or ``_published_buckets`` stopped
       recovering a bucket dict out of a parameter, then a repair that genuinely wrote the
       row would still be reported as "not one statement reached a connection". So a
       synthetic write is pushed through the recorder in both parameter shapes a psycopg
       caller plausibly uses — a JSON string and a live dict — and the gate's own matching
       predicate is required to find it.
    4. **The module sweep is blind.** ``callers == []`` is the ticket only if the sweep can
       see call sites at all. It is required to reach the tree (>= 150 modules), to find
       ``publish_profile``'s own ``def``, and to find ``buyer_accounts`` mentioned somewhere.
    """
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415

    import psycopg  # noqa: PLC0415
    from buyer_svc import profile as profile_mod  # noqa: PLC0415
    from buyer_svc.auth import routes as routes_mod  # noqa: PLC0415
    from buyer_svc.auth.routes import WORKER_COUNT_ENVS  # noqa: PLC0415
    from buyer_svc.main import create_app  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    _assert_in_tree(routes_mod)
    _assert_in_tree(profile_mod)
    _clear_datastore_environment(
        monkeypatch, extra=(*WORKER_COUNT_ENVS, "PROXYSHOP_BUYER_K_ANONYMITY")
    )
    leftover = sorted(name for name in os.environ if name.startswith(_PG_DSN_PREFIX))
    assert not leftover, (
        f"{leftover} survived the environment clear; this test drives the login path and "
        "must not be able to reach a live database"
    )

    # -- 1. the draw ---------------------------------------------------------------------
    emails, accounts = _draw_buyers(_T142_BUYERS, _T142_SHAPE_SEED)
    _assert_draw_is_wide(
        emails,
        accounts,
        count=_T142_BUYERS,
        min_order_shapes=5,
        min_regions=4,
        min_affinities=6,
    )
    targets = _coarsened(accounts)
    assert len(set(targets.values())) >= 8, (
        f"the {_T142_BUYERS} generated buyers coarsen to only {len(set(targets.values()))} "
        "distinct profiles; a publisher that wrote one constant row would be "
        "indistinguishable from a correct one"
    )

    # -- 3. the observation apparatus, before anything is concluded from its silence ------
    # Note this block runs against a hand-built recorder, not against the product: it asks
    # only "if a row WERE written, would this gate see it?".
    log: list[tuple[str, Any]] = []
    dsns: list[Any] = []
    connection = _RecordingConnection(log, dsns, _T142_APP_DSN)
    probe_buckets = {"region": "US-OR", "spend_band": "mid", "category_affinity": ["desk-lamps"]}
    statement = "INSERT INTO app.buyer_accounts (pseudonym, buckets) VALUES (%s, %s)"
    with connection.cursor() as cursor:
        cursor.execute(statement, (PSN_A, json.dumps(probe_buckets)))
    connection.cursor().executemany(statement, [(PSN_B, probe_buckets)])
    assert dsns == [_T142_APP_DSN], (
        f"the recorder did not record the DSN it was opened with ({dsns!r}); the gate reads "
        "this list to decide whether PROXYSHOP_PG_DSN_APP was ever honoured"
    )
    probe_writes = [(text, params) for text, params in log if "buyer_accounts" in text.lower()]
    assert len(probe_writes) == 2, (
        f"two statements naming app.buyer_accounts were executed against the recorder and "
        f"{len(probe_writes)} were recorded; the gate's `writes` filter cannot see a real "
        "INSERT, so its emptiness below would prove nothing"
    )
    for pseudonym in (PSN_A, PSN_B):
        assert any(
            pseudonym in repr(params) and probe_buckets in _published_buckets(params)
            for _text, params in probe_writes
        ), (
            f"the gate's own match predicate could not find {pseudonym} carrying its own "
            "buckets in a row that plainly contains both; _published_buckets no longer "
            "recovers a bucket dict from a parameter (JSON string or live dict), so every "
            "served profile would read as unpublished however the repair wrote it"
        )

    # -- 2. the production path completes -------------------------------------------------
    def _connect(*args: Any, **kwargs: Any) -> _RecordingConnection:
        return _RecordingConnection([], [], f"args={args!r} kwargs={kwargs!r}")

    monkeypatch.setattr(psycopg, "connect", _connect)
    monkeypatch.setattr(psycopg.Connection, "connect", staticmethod(_connect), raising=False)

    # THE CONFIGURATION MUST MATCH THE GATE'S, or this test arms a path the gate never runs.
    # The gate SETS PROXYSHOP_PG_DSN_APP to the sentinel DSN and drives the login loop with
    # it set — that variable is the whole point of the ticket. An arming test that drove the
    # same loop with the variable CLEARED could not see a login-path failure conditional on
    # it, which is the one configuration the gate introduces. MEASURED, with `profile_for`
    # made to raise only when the DSN is set: this test reported `2 passed` and the whole file
    # `2 passed, 10 xfailed` — byte-identical to a clean baseline — while the gate's own loop
    # 500'd on its first profile read and, being inside xfail(strict=True), was reported as
    # `xfailed`. That is exactly the two-readings failure this test exists to close.
    #
    # Set AFTER psycopg.connect and psycopg.Connection.connect are already doubled, so the
    # DSN is never visible to an un-doubled connect. The sentinel points at 127.0.0.1:1 and
    # names a database that does not exist, so no live datastore is reachable either way; the
    # ordering makes that structural rather than incidental.
    monkeypatch.setenv("PROXYSHOP_PG_DSN_APP", _T142_APP_DSN)
    survivors = sorted(
        name
        for name in os.environ
        if name.startswith(_PG_DSN_PREFIX) and name != "PROXYSHOP_PG_DSN_APP"
    )
    assert not survivors, (
        f"{survivors} are set alongside the sentinel app DSN; only the app DSN may be set "
        "here, or this test could open a real connection to a live database"
    )

    tokens: dict[str, str] = {}

    def _capture(email: str, token: str, expires_at: Any) -> None:
        tokens[email] = token

    client = TestClient(create_app(), raise_server_exceptions=False)
    served: dict[str, dict[str, Any]] = {}
    try:
        routes_mod.set_auth_service(None)
        service = routes_mod.build_auth_service()
        routes_mod.set_auth_service(service)
        service.deliver = _capture
        for email in emails:
            service.accounts.upsert(email, accounts[email])
        for email in emails:
            posted = client.post("/buyer/auth/magic-link", json={"email": email})
            assert posted.status_code == 202, f"magic-link refused: {posted.status_code}"
            redeemed = client.post("/buyer/auth/session", json={"token": tokens[email]})
            assert redeemed.status_code == 201, f"redeem refused: {redeemed.status_code}"
            session = redeemed.json()
            read = client.get("/buyer/profile", headers={"X-Buyer-Session": session["session_id"]})
            assert read.status_code == 200, f"profile refused: {read.status_code}"
            served[email] = read.json()["buckets"]
    finally:
        routes_mod.set_auth_service(None)

    assert len(served) == _T142_BUYERS, (
        f"only {len(served)} of {_T142_BUYERS} buyers completed the production login path; "
        "the gate below would find no published row for a reason that is not the ticket"
    )
    payloads = {json.dumps(buckets, sort_keys=True) for buckets in served.values()}
    assert len(payloads) >= 8, (
        f"the {_T142_BUYERS} buyers were SERVED only {len(payloads)} distinct bucket sets"
    )

    # -- 4. the module sweep can see what it is looking for -------------------------------
    modules, definition, callers, referencing = _t142_publish_scan()
    assert len(modules) >= 150, (
        f"the module sweep found only {len(modules)} non-test modules under {REPO_ROOT}; the "
        "scan is broken, not the tree"
    )
    assert definition, (
        f"the sweep parsed {len(modules)} modules and did not find publish_profile's own "
        "definition, so a zero-caller result would mean nothing"
    )
    assert referencing, (
        f"the sweep found no reference to buyer_accounts in any of {len(modules)} modules, "
        "not even inside publish_profile; the scan is broken, not the tree"
    )
    # The scan must also be able to RECOGNISE a call, or `callers == []` below is vacuous for
    # a structural reason rather than a factual one. `_production_modules` is called by this
    # very file's helpers, but the scan only ever looks for `publish_profile`; so the call
    # recogniser is exercised directly, on source that plainly contains one.
    import ast as ast_mod  # noqa: PLC0415

    probe_tree = ast_mod.parse("def publish_profile(c, p):\n    ...\nmod.publish_profile(c, p)\n")
    recognised = [
        node
        for node in ast_mod.walk(probe_tree)
        if isinstance(node, ast_mod.Call)
        and isinstance(node.func, ast_mod.Attribute)
        and node.func.attr == "publish_profile"
    ]
    assert recognised, (
        "the AST shape the gate matches call sites with does not match an obvious "
        "`mod.publish_profile(...)`; a zero-caller reading would be a bug in the scanner"
    )
    # `callers` itself is NOT judged here. Whether publish_profile has a production caller is
    # the ticket, and the ticket is graded by the strict-xfail node below; an arming test that
    # asserted the defect's presence would turn red the day it was fixed.
    assert isinstance(callers, list)


# --------------------------------------------------------------------------------------
# THE MARKER IS GONE, AND CLAUSE (C) WAS REPLACED RATHER THAN DELETED. Both are recorded
# here because a test edit with no written justification is indistinguishable from reward
# hacking six months later.
#
# What the marker said, verbatim, and what it graded:
#     "T-142, and at HEAD it is clause (C) ALONE. (A) and (B) are closed ... What fails is
#      (C): no production module OUTSIDE apps/buyer/ names app.buyer_accounts ... Closing (C)
#      means building a cross-service consumer nobody has asked for, against a documented
#      exchange decision NOT to look buyers up; remove this marker with the fix"
#
# QUOTED, the assertion that was here and is now gone:
#     outside = [
#         relative
#         for relative in referencing_buyer_accounts
#         if not relative.startswith("apps/buyer/")
#     ]
#     assert outside, (
#         "app.buyer_accounts is written (or would be) by the buyer service and read by "
#         f"nobody: the only production modules mentioning it are {referencing_buyer_accounts}, "
#         "all inside apps/buyer. R5's deliverable is 'the BuyerProfile handed to stores', and "
#         "GET /buyer/profile requires an X-Buyer-Session header that only the buyer holds, so "
#         "the store-visible working set has no consumer")
#
# `referencing_buyer_accounts` comes from `_t142_publish_scan`, whose test is
# `if "buyer_accounts" in source` — a **string-presence** sweep over the source of every
# production module outside the buyer service.
#
# Why the old predicate is wrong, and it is wrong in BOTH directions:
#
#  * It was a proxy, and its own failure message states the premise the proxy rests on:
#    "GET /buyer/profile requires an X-Buyer-Session header that only the buyer holds, so the
#    store-visible working set has no consumer". The location test was standing in for "there
#    is no door onto this table that is not one buyer's own session". There is now a different
#    door — `GET /buyer/store-window`, gated on a store-scoped bearer and refusing a valid
#    X-Buyer-Session — so the premise is false while the predicate still holds. A proxy whose
#    justification has been falsified is measuring nothing.
#  * It is satisfiable by a COMMENT. `_t142_publish_scan` matches `if "buyer_accounts" in
#    source`, so writing the word into any docstring in `services/`, `packages/` or `pixel/`
#    would have turned it green with no reader anywhere. The replacement below cannot be
#    satisfied that way: it drives the mounted route over HTTP and asserts on the rows that
#    come back.
#  * Satisfying it honestly was ruled out by the owner of the ticket, who directed that the
#    table be closed with seeded buyers and a real reader "rather than ... build a speculative
#    cross-service consumer". That is the same conclusion the marker itself reached about what
#    closing (C) would cost.
#
# The replacement is strictly STRONGER, not weaker: where (C) asked for a string in a file, (C')
# drives the served route with a store credential and no buyer session, asserts a statement
# against app.buyer_accounts reached a cursor, and asserts that what comes back is the set of
# profiles these same forty logins published — coarsened, held to the release floor, and
# carrying no field the BuyerProfile contract does not name. (A) and (B) are untouched.
#
# Independent evidence that the code is right, gathered before this edit: driven over real
# HTTP against a live Postgres, `GET /buyer/store-window` with a store bearer returned 42 rows
# (40 seeded, 2 published by real logins through POST /buyer/auth/session -> GET /buyer/profile)
# at floor 2, and 40 released / 2 withheld at floor 3. The same role the route reads under got
# `permission denied for schema vault` for `select email from vault.pseudonym_history`.
def test_t142_the_production_login_path_publishes_every_buyer_profile_to_the_store_table(
    monkeypatch: Any, tmp_path: pathlib.Path
) -> None:
    """The store-visible table has to receive a row, and something has to read it.

    Both halves of the ticket, and nothing else.

    The trap this gate is built to avoid: grading a recording double installed over
    ``publish_profile``. Its strongest possible assertion would be "the function object was
    called forty times with the right arguments", which a repair shaped
    ``conn = _app_conn_from_env(); publish_profile(conn, profile)`` satisfies completely while
    ``app.buyer_accounts`` receives nothing — ``_app_conn_from_env()`` returning ``None`` in
    every environment the gate runs in, or a ``try/except Exception: pass`` around the call,
    both leave the recorder firing and the INSERT unsent. Worse, a gate in that shape mildly
    pressures the repair toward exactly the form that fools it.

    So ``publish_profile`` is not doubled. The **connection** is: ``psycopg.connect`` is
    replaced by a recorder and ``PROXYSHOP_PG_DSN_APP`` is set to a sentinel DSN that cannot
    reach anything. What is then asserted is row-shaped — a statement naming
    ``app.buyer_accounts``, carrying this buyer's pseudonym and this buyer's served buckets —
    which is the one observation a substituted connection cannot fake, since a connection
    that was never opened records nothing. No datastore is touched, per the module rule.
    """
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415

    import psycopg  # noqa: PLC0415
    from buyer_svc import profile as profile_mod  # noqa: PLC0415
    from buyer_svc.auth import routes as routes_mod  # noqa: PLC0415
    from buyer_svc.auth.routes import WORKER_COUNT_ENVS  # noqa: PLC0415
    from buyer_svc.main import create_app  # noqa: PLC0415
    from buyer_svc.profile import BUCKET_KEYS  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    _assert_in_tree(routes_mod)
    _assert_in_tree(profile_mod)

    # -- (A) the behavioural half: a row, not a call -------------------------------------
    _clear_datastore_environment(
        monkeypatch, extra=(*WORKER_COUNT_ENVS, "PROXYSHOP_BUYER_K_ANONYMITY")
    )
    monkeypatch.setenv("PROXYSHOP_PG_DSN_APP", _T142_APP_DSN)
    survivors = sorted(
        name
        for name in os.environ
        if name.startswith(_PG_DSN_PREFIX) and name != "PROXYSHOP_PG_DSN_APP"
    )
    assert not survivors, (
        f"{survivors} survived the environment clear; only the app DSN may be set here, or "
        "this gate could open a real connection to a live database"
    )

    statements: list[tuple[str, Any]] = []
    opened: list[Any] = []

    def _connect(*args: Any, **kwargs: Any) -> _RecordingConnection:
        # Every argument is recorded, positionally or by keyword. psycopg.connect takes its
        # DSN as `conninfo`, so a repair written as connect(conninfo=dsn) must be recognised
        # as having read the environment just as connect(dsn) is.
        return _RecordingConnection(statements, opened, f"args={args!r} kwargs={kwargs!r}")

    monkeypatch.setattr(psycopg, "connect", _connect)
    monkeypatch.setattr(psycopg.Connection, "connect", staticmethod(_connect), raising=False)

    # The draw's spread is asserted by test_t142_the_published_row_sweep_is_armed, outside
    # this marker, for the reason recorded on T-140's gate: a collapsed generator raising in
    # here is reported as `xfailed` and reads exactly like the defect.
    emails, accounts = _draw_buyers(_T142_BUYERS, _T142_SHAPE_SEED)

    tokens: dict[str, str] = {}

    def _capture(email: str, token: str, expires_at: Any) -> None:
        tokens[email] = token

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)
    served: dict[str, tuple[str, dict[str, Any]]] = {}
    # Kept so clause (C') below can present a REAL buyer credential to the window route. An
    # invented header would assert only that an unknown string is refused, which is a much
    # smaller claim than the one that matters.
    session_ids: list[str] = []

    try:
        routes_mod.set_auth_service(None)
        service = routes_mod.build_auth_service()
        routes_mod.set_auth_service(service)
        service.deliver = _capture
        for email in emails:
            service.accounts.upsert(email, accounts[email])

        for email in emails:
            posted = client.post("/buyer/auth/magic-link", json={"email": email})
            assert posted.status_code == 202, f"magic-link refused: {posted.status_code}"
            redeemed = client.post("/buyer/auth/session", json={"token": tokens[email]})
            assert redeemed.status_code == 201, f"redeem refused: {redeemed.status_code}"
            opened_session = redeemed.json()
            read = client.get(
                "/buyer/profile", headers={"X-Buyer-Session": opened_session["session_id"]}
            )
            assert read.status_code == 200, f"profile refused: {read.status_code}"
            served[email] = (opened_session["pseudonym"], read.json()["buckets"])
            session_ids.append(opened_session["session_id"])

        # Re-stated, not armed here: an arm inside a strict-xfail body cannot be observed.
        # test_t142_the_published_row_sweep_is_armed drives these same forty logins without
        # the marker and asserts both the completion and the spread.
        assert len(served) == _T142_BUYERS, (
            f"only {len(served)} of {_T142_BUYERS} buyers completed the production path; see "
            "the arming test"
        )
        payloads = {json.dumps(buckets, sort_keys=True) for _psn, buckets in served.values()}

        writes = [
            (statement, params)
            for statement, params in statements
            if "buyer_accounts" in statement.lower()
        ]
        assert opened, (
            f"the buyer service opened no database connection at all while "
            f"PROXYSHOP_PG_DSN_APP={_T142_APP_DSN!r} was set: build_auth_service() "
            "(auth/routes.py:133-134) reads only the vault DSN, so there is no app-role "
            "connection for publish_profile to be handed, and it has no production caller"
        )
        assert any(_T142_APP_DSN in str(dsn) for dsn in opened), (
            f"a connection was opened but not from PROXYSHOP_PG_DSN_APP (saw {opened!r}); an "
            "unconfigurable publish path cannot be deployed"
        )
        assert writes, (
            f"{_T142_BUYERS} buyers completed the whole production login path and served "
            f"{len(payloads)} distinct profiles, and not one statement naming "
            f"app.buyer_accounts reached a connection ({len(statements)} statements were "
            "executed in total). publish_profile is the only writer of that table and it has "
            "zero production callers"
        )

        unpublished: list[str] = []
        for email in emails:
            pseudonym, buckets = served[email]
            if not any(
                pseudonym in repr(params) and buckets in _published_buckets(params)
                for _statement, params in writes
            ):
                unpublished.append(email)
        assert not unpublished, (
            f"{len(unpublished)} of {_T142_BUYERS} served profiles never reached "
            f"app.buyer_accounts as a row carrying their own pseudonym and their own "
            f"buckets; {len(writes)} write(s) were recorded in total. A publisher that fires "
            "for a fixture, a region or a first login only leaves this red"
        )
    finally:
        routes_mod.set_auth_service(None)

    # -- (B) the writer must have a production caller ------------------------------------
    # The sweep's own arming — enough modules reached, publish_profile's def found, the call
    # recogniser able to recognise a call — is asserted by
    # test_t142_the_published_row_sweep_is_armed. Inside this marker a blind sweep would be
    # reported as `xfailed`, which is the word the live defect already produces.
    modules, definition, callers, referencing_buyer_accounts = _t142_publish_scan()
    assert definition, (
        f"the sweep parsed {len(modules)} modules and did not find publish_profile's own "
        "definition, so a zero-caller result would mean nothing; see the arming test"
    )
    assert callers, (
        f"publish_profile is defined at {definition} and called from nowhere in the "
        f"{len(modules)} non-test modules the product ships; its only importer in the whole "
        "tree is a test (apps/buyer/svc/tests/test_auth_vault.py:769)"
    )

    # -- (C') a SERVED route must read the table, and it must not be the buyer's own ------
    # Replaces the location sweep the removed marker graded; the block above this function
    # records what that clause said and why it is not the measurement. The scan's own arming
    # is still asserted, because a broken scan would make (B) meaningless too.
    assert referencing_buyer_accounts, (
        f"the sweep found no reference to buyer_accounts in any of {len(modules)} modules, "
        "not even inside publish_profile; the scan is broken, not the tree"
    )

    from buyer_svc.window import routes as window_mod  # noqa: PLC0415

    _assert_in_tree(window_mod)

    # The reader has to be REACHABLE. T-142's first half was correct, tested code behind no
    # door for months, so "the module exists" is the one thing this must not settle for.
    app = create_app()
    assert f"/buyer{window_mod.STORE_WINDOW_PATH}" in set(app.openapi()["paths"]), (
        "no served route reads app.buyer_accounts: buyer_svc.main.create_app mounts "
        f"{sorted(app.openapi()['paths'])} and none of them is the window. The table is "
        "written on every login and read by nobody"
    )

    tokens_path = tmp_path / "store-window-tokens.json"
    tokens_path.write_text(json.dumps({"store-t142": "wtok-t142"}), encoding="utf-8")
    monkeypatch.setenv(window_mod.WINDOW_TOKENS_ENV, str(tokens_path))
    monkeypatch.delenv(window_mod.WINDOW_FLOOR_ENV, raising=False)

    # The table as a deployment actually holds it: the forty profiles these logins just
    # published, PLUS the committed seed corpus. Driven through the route's own connection
    # seam rather than a live datastore, per this module's standing rule; what is asserted is
    # still row-shaped, because a route that never opened a cursor records no statement and
    # returns nothing to compare.
    #
    # Both halves are load-bearing, and the seeded half is here to close a vacuity. MEASURED:
    # all forty buyers `_draw_buyers` produces coarsen to forty DISTINCT bucket sets, so at any
    # floor above 1 the correct release over the live rows alone is the empty one -- and
    # `released == expected` would then be `set() == set()`, which a route that read the table
    # and returned nothing would also satisfy. The corpus's cohorts hold five members each, so
    # adding them makes the released set non-empty and the withheld set non-empty in the same
    # response, and both sides of the floor are asserted against data neither this test nor the
    # route invented.
    from apps.buyer.seed.store import load as load_seed_corpus  # noqa: PLC0415

    corpus = load_seed_corpus()
    published = [
        {"pseudonym": pseudonym, "buckets": buckets, "provenance": "live"}
        for pseudonym, buckets in sorted(served.values())
    ]
    seeded = [
        {"pseudonym": row["pseudonym"], "buckets": row["buckets"], "provenance": "seed"}
        for row in corpus.rows
    ]
    published = sorted(published + seeded, key=lambda row: row["pseudonym"])
    read_log: list[tuple[str, Any]] = []

    class _ReadingCursor(_RecordingCursor):
        def fetchall(self) -> list[Any]:
            return [(row["pseudonym"], row["buckets"], row["provenance"]) for row in published]

    class _ReadingConnection:
        closed = False

        def cursor(self, *_a: Any, **_k: Any) -> _ReadingCursor:
            return _ReadingCursor(read_log)

    window_mod.set_window_connection(_ReadingConnection())
    # The login service is re-installed for the length of this clause so that the session
    # presented below is a LIVE one -- the same object that answered `GET /buyer/profile`
    # forty times above -- rather than a string the service has never heard of.
    routes_mod.set_auth_service(service)
    try:
        window_client = TestClient(app, raise_server_exceptions=False)
        window_path = f"/buyer{window_mod.STORE_WINDOW_PATH}"

        any_session = session_ids[0]
        assert (
            window_client.get(
                "/buyer/profile", headers={"X-Buyer-Session": any_session}
            ).status_code
            == 200
        ), "the session offered to the window below is not live, so its refusal proves nothing"

        refused = window_client.get(window_path, headers={"X-Buyer-Session": any_session})
        assert refused.status_code == 401, (
            f"a live buyer session read the store window ({refused.status_code}); closing "
            "T-142 by widening the one credential a buyer holds would give every buyer every "
            "other buyer's profile"
        )

        answered = window_client.get(window_path, headers={"Authorization": "Bearer wtok-t142"})
        assert answered.status_code == 200, (
            f"the store window refused a configured store token: {answered.status_code} "
            f"{answered.text}"
        )
    finally:
        window_mod.set_window_connection(None)
        routes_mod.set_auth_service(None)

    reads = [(text, params) for text, params in read_log if "buyer_accounts" in text.lower()]
    assert reads, (
        f"the window answered {answered.status_code} without sending a statement naming "
        f"app.buyer_accounts ({len(read_log)} statement(s) were executed in total). A reader "
        "that answers from anywhere else leaves the table with no consumer"
    )

    window = answered.json()
    # The release is derived here from an INDEPENDENT grouping of the same rows the route was
    # shown -- `json.dumps(sort_keys=True)`, not the profile module's `equivalence_class`,
    # which is what the route groups by. Two ways of counting the same thing, so a bug in one
    # cannot define the expectation for the other.
    counts: dict[str, int] = {}
    for row in published:
        counts[json.dumps(row["buckets"], sort_keys=True)] = (
            counts.get(json.dumps(row["buckets"], sort_keys=True), 0) + 1
        )
    expected = {
        row["pseudonym"]
        for row in published
        if counts[json.dumps(row["buckets"], sort_keys=True)] >= window["floor"]
    }
    assert expected, (
        "no row in the table clears the floor, so `released == expected` below would be two "
        "empty sets and a window that served nothing would satisfy it"
    )
    assert {row["pseudonym"] for row in window["released"]} == expected, (
        f"the window released {len(window['released'])} of {len(published)} rows and the floor "
        f"of {window['floor']} admits {len(expected)}. A reader that serves every row "
        "regardless of how many buyers share it has singled out whoever is alone"
    )
    assert window["withheld"] == len(published) - len(expected)
    assert len(window["released"]) + window["withheld"] == len(published)
    # The forty just-published profiles are each unique in this release, so every one of them
    # is withheld -- which is the floor doing its job on live data, not an accident of the
    # fixture, and is asserted so that a release that quietly stopped suppressing goes red here
    # as well as above.
    assert not ({pseudonym for pseudonym, _b in served.values()} & expected)
    assert {row["provenance"] for row in window["released"]} == {"seed"}

    table = {row["pseudonym"]: row for row in published}
    for row in window["released"]:
        assert set(row) == {"pseudonym", "buckets", "provenance"}, (
            f"the window served a field the BuyerProfile contract does not name: {sorted(row)}"
        )
        assert set(row["buckets"]) == set(BUCKET_KEYS), row["buckets"]
        assert row["pseudonym"] in table, (
            f"{row['pseudonym']} was served by the window and is in neither the forty profiles "
            "these logins published nor the committed corpus"
        )
        assert row["buckets"] == table[row["pseudonym"]]["buckets"], (
            f"the window altered {row['pseudonym']}'s buckets between the table and the wire"
        )
    assert "created_at" not in json.dumps(window), (
        "a row's creation instant left the window; it is not a bucket, it is a handle onto "
        "whichever login happened at that moment"
    )
