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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-140: build_auth_service() (auth/routes.py:133-134) decides the vault and nothing "
        "else — the word 'accounts' does not appear in that module at all — so production "
        "always gets the empty default InMemoryAccountDirectory (magic_link.py:193), and the "
        "ONLY non-test writer of a buyer record in the tree is redeem's "
        "self.accounts.upsert(email, {'email': email}) (magic_link.py:304). There is no "
        "production populator, so a record can never carry anything the login gesture did "
        "not already know, and a service rebuilt as a restart would rebuild it has forgotten "
        "every buyer; remove this marker with the fix"
    ),
)
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
    import random  # noqa: PLC0415
    import string  # noqa: PLC0415

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
    shapes = random.Random(_T140_SHAPE_SEED)
    identities = random.SystemRandom()

    emails: list[str] = []
    accounts: dict[str, dict[str, Any]] = {}
    for _ in range(_T140_BUYERS):
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

    # -- canaries on the generator itself ----------------------------------------------
    # A generator that yields one address sixty times still reports sixty completed
    # iterations, so DISTINCT inputs are counted, not loop trips.
    assert len(set(emails)) == _T140_BUYERS, (
        f"the generator produced {len(set(emails))} distinct addresses across "
        f"{_T140_BUYERS} draws; a sweep over a repeated address measures one buyer while "
        "reporting sixty"
    )
    order_shapes = {len(account["orders"]) for account in accounts.values()}
    region_shapes = {account["region"] for account in accounts.values()}
    affinity_shapes = {
        tuple(sorted({order["category"] for order in account["orders"]}))
        for account in accounts.values()
    }
    assert len(order_shapes) >= 5, f"order-count shapes drawn: {sorted(order_shapes)}"
    assert len(region_shapes) >= 4, f"regions drawn: {sorted(region_shapes)}"
    assert len(affinity_shapes) >= 6, f"category shapes drawn: {len(affinity_shapes)}"

    targets = {
        email: json.dumps(build_profile(account, PSN_A).model_dump()["buckets"], sort_keys=True)
        for email, account in accounts.items()
    }
    assert len(set(targets.values())) >= 12, (
        f"the generated buyers coarsen to only {len(set(targets.values()))} distinct "
        "profiles; the draw is too narrow to distinguish a real populator from a constant"
    )
    informationless = json.dumps(
        build_profile({"email": "probe@nowhere.example", "orders": []}, PSN_A).model_dump()[
            "buckets"
        ],
        sort_keys=True,
    )
    assert informationless not in set(targets.values()), (
        "fixture error: a generated buyer coarsens to the empty profile, so 'the served "
        "profile changed' would be unobservable for that buyer"
    )

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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T-142: publish_profile (profile/__init__.py:1378) is the ONLY writer of "
        "app.buyer_accounts and has zero production callers — an AST scan of all 269 "
        "non-test modules finds its own def and nothing else — while "
        "apps/buyer/compose.yaml:32 already hands the buyer service a PROXYSHOP_PG_DSN_APP "
        "that no line of apps/buyer reads. Nothing outside the buyer service references "
        "app.buyer_accounts either, so R5's 'the BuyerProfile handed to stores' is handed to "
        "nobody: GET /buyer/profile needs an X-Buyer-Session header only the buyer holds; "
        "remove this marker with the fix"
    ),
)
def test_t142_the_production_login_path_publishes_every_buyer_profile_to_the_store_table(
    monkeypatch: Any,
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
    import ast as ast_mod  # noqa: PLC0415
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415
    import random  # noqa: PLC0415
    import string  # noqa: PLC0415

    import psycopg  # noqa: PLC0415
    from buyer_svc import profile as profile_mod  # noqa: PLC0415
    from buyer_svc.auth import routes as routes_mod  # noqa: PLC0415
    from buyer_svc.auth.routes import WORKER_COUNT_ENVS  # noqa: PLC0415
    from buyer_svc.main import create_app  # noqa: PLC0415
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

    shapes = random.Random(_T142_SHAPE_SEED)
    identities = random.SystemRandom()
    emails: list[str] = []
    accounts: dict[str, dict[str, Any]] = {}
    for _ in range(_T142_BUYERS):
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

    assert len(set(emails)) == _T142_BUYERS, (
        f"the generator produced {len(set(emails))} distinct addresses across "
        f"{_T142_BUYERS} draws; a publisher wired for one address would pass unnoticed"
    )

    tokens: dict[str, str] = {}

    def _capture(email: str, token: str, expires_at: Any) -> None:
        tokens[email] = token

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)
    served: dict[str, tuple[str, dict[str, Any]]] = {}

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

        # ARM: distinct completed logins, and a spread of published payloads, before any
        # conclusion is drawn about what was or was not published.
        assert len(served) == _T142_BUYERS, (
            f"only {len(served)} of {_T142_BUYERS} buyers completed the production path"
        )
        payloads = {json.dumps(buckets, sort_keys=True) for _psn, buckets in served.values()}
        assert len(payloads) >= 8, (
            f"the {_T142_BUYERS} buyers served only {len(payloads)} distinct bucket sets; a "
            "publisher that wrote one constant row would be indistinguishable from a correct "
            "one"
        )

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
    # Parsed, never grepped: `"publish_profile"` is already a string in `__all__`
    # (profile/__init__.py:125) and appears in docstrings, and neither is a call.
    modules = _production_modules()
    assert len(modules) >= 150, (
        f"the module sweep found only {len(modules)} non-test modules under {REPO_ROOT}; the "
        "scan is broken, not the tree"
    )
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

    # ARM the sweep: it must be able to see the symbol it is looking for.
    assert definition, (
        f"the sweep parsed {len(modules)} modules and did not find publish_profile's own "
        "definition, so a zero-caller result would mean nothing"
    )
    assert callers, (
        f"publish_profile is defined at {definition} and called from nowhere in the "
        f"{len(modules)} non-test modules the product ships; its only importer in the whole "
        "tree is a test (apps/buyer/svc/tests/test_auth_vault.py:769)"
    )

    # -- (C) something outside the buyer service must read the table ----------------------
    assert referencing_buyer_accounts, (
        f"the sweep found no reference to buyer_accounts in any of {len(modules)} modules, "
        "not even inside publish_profile; the scan is broken, not the tree"
    )
    outside = [
        relative
        for relative in referencing_buyer_accounts
        if not relative.startswith("apps/buyer/")
    ]
    assert outside, (
        "app.buyer_accounts is written (or would be) by the buyer service and read by "
        f"nobody: the only production modules mentioning it are {referencing_buyer_accounts}, "
        "all inside apps/buyer. R5's deliverable is 'the BuyerProfile handed to stores', and "
        "GET /buyer/profile requires an X-Buyer-Session header that only the buyer holds, so "
        "the store-visible working set has no consumer"
    )
