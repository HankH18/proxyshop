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

**Nothing here is allowed to skip.** A skip is not a gate, and the compose datastore stack is
routinely down in this repo, so every assertion below is made against a pure function, the
module's public surface, or an in-process ASGI client — never against a live datastore.

Following the convention of the other suites in this directory, every product import happens
inside the test body rather than at module scope.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

#: Names that mean "the k-anonymity floor was consulted here". Any of them appearing as a
#: real AST reference — not a docstring, not a comment — counts.
FLOOR_SYMBOLS = frozenset(
    {"anonymise_cohort", "k_anonymity_floor", "buckets_at_level", "build_profiles"}
)


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


def _references(func: Any, symbols: frozenset[str]) -> bool:
    """True when ``func``'s body really mentions one of ``symbols``.

    Parsed rather than string-matched on purpose: ``"anonymise_cohort" in source`` is
    satisfied by adding the word to a docstring, which would close the ticket without
    changing a single execution.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in symbols:
            return True
        if isinstance(node, ast.Attribute) and node.attr in symbols:
            return True
    return False


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
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-164: build_buckets() and anonymise_cohort() are public and run NO identity-leak "
        "check — only build_profile()/build_profiles() do — so an account whose own order "
        "category carries its surname is refused through one public entry point and "
        "published verbatim through the other two; remove this marker with the fix"
    ),
)
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
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-197: _MIN_LEAKABLE = 4 (profile/__init__.py:436) drops every identity fragment "
        "shorter than four characters before the backstop ever sees it, so first_name='Ann' "
        "last_name='Lee' with an order category of 'ann lee gear' builds as "
        "['ann-lee-gear'] and the backstop reports clean; remove this marker with the fix"
    ),
)
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
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-198(a): the backstop matches an identity fragment verbatim and in slug space, but "
        "a number REGROUPED rather than repunctuated is neither — phone '555-0100' with an "
        "order category of 'gift 5550100 gear' builds as ['gift-5550100-gear'] with "
        "identity_leaks == []; remove this marker with the fix"
    ),
)
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
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-198(b): _identity_sources (profile/__init__.py:944-949) word-splits the email "
        "LOCAL PART but adds the domain whole, so 'x@reyes-family.example' with an order "
        "category of 'reyes family gear' builds as ['reyes-family-gear'] clean, while the "
        "identical words left of the @ are refused; remove this marker with the fix"
    ),
)
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
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-199: _BUCKET_VOCABULARY['category_affinity'] (profile/__init__.py:1038) holds "
        "every CATEGORY_TAXONOMY label out of the leak haystack on the grounds that they are "
        "values a coarsener chose from a fixed table, but at the k=1 default "
        "category_affinity carries the account's OWN slugs — last_name='Home' buying 'home' "
        "publishes the surname and the backstop skips it; remove this marker with the fix"
    ),
)
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
# T-221 — the k-anonymity floor is off by default and the production path never applies it
# ======================================================================================
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-221: DEFAULT_K_ANONYMITY = 1 (profile/__init__.py:175) is a no-op floor unless "
        "PROXYSHOP_BUYER_K_ANONYMITY is set, and build_profile — the ONLY path production "
        "takes (magic_link.py:330) — never calls anonymise_cohort or k_anonymity_floor at "
        "all, so the generalisation ladder T-138 delivered is unreachable and a rotated "
        "pseudonym still publishes a byte-identical tuple; remove this marker with the fix"
    ),
)
def test_t221_the_k_anonymity_floor_is_on_by_default_and_reaches_the_production_path() -> None:
    """The floor has to be a floor, and it has to be on the path the product takes.

    Both halves of the ticket, and nothing else. In particular this does NOT assert that a
    pseudonym rotation is re-linkable: that is the *defect*, and asserting it would turn the
    ticket's own fix into a permanent red.

    The second half is checked against BOTH ends of the production path — ``build_profile``
    and its only caller, ``MagicLinkAuth.profile_for`` — because the ticket admits two
    repairs: teach ``build_profile`` the floor, or route production through
    ``build_profiles``, which already applies it. Either one closes the ticket.
    """
    from buyer_svc import profile as profile_mod  # noqa: PLC0415
    from buyer_svc.auth import MagicLinkAuth  # noqa: PLC0415
    from buyer_svc.profile import build_profile, k_anonymity_floor  # noqa: PLC0415

    _assert_in_tree(profile_mod)

    # (1) A floor of 1 is not a floor. An explicit empty environment is passed so the reading
    # cannot be changed by whatever the surrounding shell happens to export.
    floor = k_anonymity_floor({})
    assert floor > 1, (
        f"the default k-anonymity floor is {floor}: a floor of 1 puts every buyer alone in "
        "their own equivalence class, which is exactly the re-linkability T-138 exists to "
        "prevent, and PROXYSHOP_BUYER_K_ANONYMITY is the only thing that raises it"
    )

    # (2) The production path must actually apply it. Parsed, not string-matched: adding
    # "anonymise_cohort" to a docstring must not close this ticket.
    applies_floor = _references(build_profile, FLOOR_SYMBOLS) or _references(
        MagicLinkAuth.profile_for, FLOOR_SYMBOLS
    )
    assert applies_floor, (
        "neither build_profile nor its only production caller "
        "(apps/buyer/svc/src/auth/magic_link.py:330 MagicLinkAuth.profile_for) references "
        f"any of {sorted(FLOOR_SYMBOLS)}, so the generalisation ladder commit 958fead added "
        "is never reached on the path the product takes — only build_profiles applies it, "
        "and nothing in production calls build_profiles"
    )


# ======================================================================================
# T-163 — SessionStore.open() checks the pseudonym's FORMAT and never its vault membership
# ======================================================================================
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-163: InMemorySessionStore.open (auth/sessions.py:170) refuses only on the "
        "PSEUDONYM_PREFIX and holds no vault reference at all, so any 'psn-'-prefixed string "
        "— including 'psn-' glued onto the buyer's own email — opens a session that "
        "authenticates GET /buyer/profile; the pseudonym is a bearer credential whose format "
        "is its only proof; remove this marker with the fix"
    ),
)
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
@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-165: POST /buyer/auth/magic-link is unauthenticated and unmetered — no "
        "rate-limit, throttle or backoff symbol exists in auth/routes.py, and the only "
        "ceiling (MagicLinkAuth.max_pending, which magic_link.py:248 itself calls 'not a "
        "rate limiter') never fires for a repeated address because each request supersedes "
        "the last, so 20 requests mail 20 live login tokens to one mailbox with "
        "pending_links pinned at 1; remove this marker with the fix"
    ),
)
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
