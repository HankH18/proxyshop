"""T-070 — buyers authenticate lightly and stores never see who they are (SPEC R5).

The ticket's three acceptance criteria, and where each is driven:

1. *Login issues a session plus a fresh pseudonym; prior pseudonyms are never reissued* —
   :func:`test_login_issues_a_session_and_a_fresh_pseudonym` and the rotation tests below,
   plus :func:`test_postgres_refuses_to_reissue_a_retired_pseudonym` which proves the
   database enforces it too, not just the application.
2. *Grant test: non-vault roles cannot read* ``vault.*`` — the ``@pytest.mark.docker`` block
   at the bottom, against the live cluster and the real migration files.
3. *Profile objects contain no identity fields (schema property test)* —
   :func:`test_profile_objects_contain_no_identity_fields`, which generates 240 accounts and
   checks every profile against the published ``BuyerProfile`` JSON Schema *and* against the
   R5 identity regex, in both directions (no identity-shaped key, no identity value).

Two notes on what the green here is evidence of.

*The HTTP flow is driven through the real app.* :func:`test_the_http_login_flow_hands_out_a_
pseudonym_and_never_an_identity` builds the service with the frozen
:func:`buyer_svc.main.create_app`, which discovers ``auth/routes.py`` by glob exactly as a
deployment does, and drives ``POST /buyer/auth/magic-link`` → ``POST /buyer/auth/session`` →
``GET /buyer/profile`` over HTTP. Nothing in that path is stubbed except the mail transport,
which is the one thing that cannot run in a test.

*The grant tests read a real cluster.* They apply T-011's migration files to this worker's
database and then ask a connection authenticated as ``exchange``, ``trust_rw`` and ``app`` to
read the mapping. A denial that came from a mock would be evidence of nothing.
"""

from __future__ import annotations

import json
import pathlib
import random
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

#: Roles that must NOT reach ``vault.*``. Restated rather than imported from
#: ``_fixtures_vault``: that file is loaded into the directory conftest's namespace by
#: ``proxyshop_support.fixture_loader``, and importing it a second time under its own dotted
#: name would execute it twice for no gain.
NON_VAULT_ROLES = ("exchange", "trust_rw", "app")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
PROTOCOL_SCHEMA = REPO_ROOT / "packages" / "contracts" / "schemas" / "protocol.schema.json"

#: R5, as the frozen acceptance suite spells it: keys that must never appear inside a
#: store-facing object. Restated here rather than imported so this suite stands on its own.
IDENTITY_KEY_RE = re.compile(
    r"e[-_ ]?mail|phone|first[_ -]?name|last[_ -]?name|full[_ -]?name|"
    r"given[_ -]?name|surname|address|street|postal|zip[_ -]?code|"
    r"account[_ -]?id|customer[_ -]?id|buyer[_ -]?id|user[_ -]?id|"
    r"identity|ip[_ -]?address|device[_ -]?id",
    re.IGNORECASE,
)

PROFILE_BUCKET_KEYS = {
    "budget_band",
    "category_affinity",
    "frequency_tier",
    "region",
    "first_time",
}

DANA = {
    "account_id": "acct-9f3c21",
    "email": "dana.reyes@example.com",
    "first_name": "Dana",
    "last_name": "Reyes",
    "phone": "+1-555-0100",
    "address": "44 Alder Way, Portland OR 97205",
    "postal_code": "97205",
    "region": "US-OR",
    "budget_band": "100-250",
    "orders": [
        {"order_ref": "ord-1", "total": 118.0, "category": "running-shoes"},
        {"order_ref": "ord-2", "total": 232.5, "category": "trail-gear"},
        {"order_ref": "ord-3", "total": 96.0, "category": "running-shoes"},
    ],
}


# =======================================================================================
# helpers
# =======================================================================================


def _walk(value: Any, path: str = ""):
    """Yield every ``(key, value)`` pair inside a plain nested structure."""
    if isinstance(value, dict):
        for key, sub in value.items():
            here = f"{path}.{key}" if path else str(key)
            yield str(key), sub
            yield from _walk(sub, here)
    elif isinstance(value, (list, tuple)):
        for index, sub in enumerate(value):
            yield from _walk(sub, f"{path}[{index}]")


def _identity_fragments(account: dict[str, Any]) -> set[str]:
    """Every fragment of ``account`` long enough to be a meaningful disclosure."""
    fragments: set[str] = set()
    for key in (
        "account_id",
        "address",
        "email",
        "first_name",
        "last_name",
        "phone",
        "postal_code",
    ):
        value = account.get(key)
        if not isinstance(value, str):
            continue
        for piece in [value, *re.split(r"[\s,@]+", value)]:
            piece = piece.strip().casefold()
            if len(piece) >= 4:
                fragments.add(piece)
    return fragments


def _profile_validator():
    """A jsonschema validator for the published ``BuyerProfile`` definition."""
    import jsonschema

    bundle = json.loads(PROTOCOL_SCHEMA.read_text(encoding="utf-8"))
    schema = {
        "$schema": bundle.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
        "$ref": "#/$defs/BuyerProfile",
        "$defs": bundle["$defs"],
    }
    return jsonschema.Draft202012Validator(schema)


def _generated_accounts(count: int) -> list[dict[str, Any]]:
    """Deterministic account records covering the shapes a real directory holds.

    The pools are chosen disjoint on purpose: no surname is a substring of a category and no
    city is a region code, so a leak assertion that fires is a real leak rather than two
    unrelated words colliding.
    """
    rng = random.Random(70)
    surnames = ["Reyes", "Okafor", "Lindqvist", "Marchetti", "Nakamura", "Boateng"]
    givens = ["Dana", "Samir", "Priya", "Tomas", "Ingrid", "Kwame"]
    cities = ["Portland", "Lisbon", "Osaka", "Bristol", "Turin"]
    streets = ["Alder Way", "Hollis Street", "Beacon Lane", "Marlow Crescent"]
    regions = ["US-OR", "US-CA", "GB-ENG", "DE", "JP-13", "97205", "44 Alder Way", None, 17]
    categories = ["running-shoes", "trail-gear", "espresso", "cookware", "headphones", "tents"]
    bands = ["100-250", "0-50", "1000+", "mid-range", 412.0, "412", None]

    accounts: list[dict[str, Any]] = []
    for index in range(count):
        given = givens[index % len(givens)]
        surname = surnames[(index // 2) % len(surnames)]
        orders = [
            {
                "order_ref": f"ord-{index}-{n}",
                "total": round(rng.uniform(9.0, 1800.0), 2),
                "category": rng.choice(categories),
            }
            for n in range(rng.choice([0, 1, 3, 7, 14]))
        ]
        accounts.append(
            {
                "account_id": f"acct-{index:06d}-zz",
                "email": f"{given}.{surname}{index}@example.invalid".casefold(),
                "first_name": given,
                "last_name": surname,
                "phone": f"+1-555-{1000 + index:04d}",
                "address": f"{index + 10} {rng.choice(streets)}, {rng.choice(cities)}",
                "postal_code": f"{90000 + index}",
                "region": rng.choice(regions),
                "budget_band": rng.choice(bands),
                "orders": orders,
            }
        )
    return accounts


# =======================================================================================
# Acceptance 1 — login issues a session and a fresh pseudonym
# =======================================================================================


def test_login_issues_a_session_and_a_fresh_pseudonym() -> None:
    """Redeeming a magic link opens a session whose subject is a brand-new pseudonym."""
    from buyer_svc.auth import MagicLinkAuth
    from buyer_svc.vault import PSEUDONYM_PREFIX

    tokens: list[str] = []
    auth = MagicLinkAuth(deliver=lambda email, token, expires_at: tokens.append(token))

    issued = auth.request_login("dana.reyes@example.com")
    assert issued.expires_at > datetime.now(UTC)
    assert not hasattr(issued, "token"), "the issue receipt must not carry the bearer token"

    session = auth.redeem(tokens[-1])
    assert session.session_id
    assert session.pseudonym.startswith(PSEUDONYM_PREFIX)
    assert not session.expired()
    assert auth.session(session.session_id) == session


def test_every_login_rotates_the_pseudonym_and_never_reissues_a_retired_one() -> None:
    """R5: each session gets its own pseudonym, and a retired one never comes back."""
    from buyer_svc.auth import MagicLinkAuth

    tokens: list[str] = []
    auth = MagicLinkAuth(deliver=lambda email, token, expires_at: tokens.append(token))

    seen: list[str] = []
    for _ in range(32):
        auth.request_login("dana.reyes@example.com")
        seen.append(auth.redeem(tokens[-1]).pseudonym)

    assert len(set(seen)) == len(seen), f"a pseudonym was reissued across sessions: {seen}"
    assert auth.vault.active("dana.reyes@example.com") == seen[-1]
    history = [row.pseudonym for row in auth.vault.history("dana.reyes@example.com")]
    assert history == seen, "the vault must remember every pseudonym the buyer has held"
    assert [row.active for row in auth.vault.history("dana.reyes@example.com")].count(True) == 1


def test_a_pseudonym_is_never_issued_to_two_buyers() -> None:
    """R5: freshness is global, not per buyer."""
    from buyer_svc.vault import PseudonymVault

    vault = PseudonymVault()
    mine = {vault.issue("dana.reyes@example.com") for _ in range(64)}
    theirs = {vault.issue("sam.okafor@example.net") for _ in range(64)}
    assert len(mine) == 64 and len(theirs) == 64
    assert mine.isdisjoint(theirs)


def test_the_vault_refuses_to_reissue_rather_than_repeat_itself() -> None:
    """A generator that cannot produce a fresh value gets an error, never a repeat.

    The default generator makes this unreachable; an injected deterministic one — a seeded
    PRNG, a "readable pseudonym" scheme someone adds later — makes it ordinary. The property
    R5 promises is *never reissued*, not *unlikely to be reissued*.
    """
    from buyer_svc.vault import PseudonymExhausted, PseudonymVault

    vault = PseudonymVault(generator=lambda: "psn-always-the-same")
    assert vault.issue("dana.reyes@example.com") == "psn-always-the-same"
    with pytest.raises(PseudonymExhausted):
        vault.issue("dana.reyes@example.com")
    with pytest.raises(PseudonymExhausted):
        vault.issue("someone.else@example.net")


def test_a_case_variant_email_is_the_same_buyer() -> None:
    """One human, one history. Otherwise the rotation guarantee is per spelling."""
    from buyer_svc.vault import PseudonymVault

    vault = PseudonymVault()
    first = vault.issue("Dana.Reyes@Example.com")
    second = vault.issue("dana.reyes@example.com")
    assert first != second
    assert vault.active("DANA.REYES@EXAMPLE.COM") == second
    assert len(vault.history("dana.reyes@example.com")) == 2


# =======================================================================================
# The magic link itself
# =======================================================================================


def test_a_magic_link_is_single_use() -> None:
    from buyer_svc.auth import MagicLinkAlreadyUsed, MagicLinkAuth

    tokens: list[str] = []
    auth = MagicLinkAuth(deliver=lambda email, token, expires_at: tokens.append(token))
    auth.request_login("dana.reyes@example.com")
    auth.redeem(tokens[-1])
    with pytest.raises(MagicLinkAlreadyUsed):
        auth.redeem(tokens[-1])


def test_an_expired_magic_link_is_refused() -> None:
    from buyer_svc.auth import MagicLinkAuth, MagicLinkExpired

    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    tokens: list[str] = []
    auth = MagicLinkAuth(
        deliver=lambda email, token, expires_at: tokens.append(token),
        clock=lambda: now,
        link_ttl=timedelta(minutes=15),
    )
    auth.request_login("dana.reyes@example.com")
    auth.clock = lambda: now + timedelta(minutes=16)
    with pytest.raises(MagicLinkExpired):
        auth.redeem(tokens[-1])


def test_an_unknown_or_empty_token_is_refused() -> None:
    from buyer_svc.auth import MagicLinkAuth, MagicLinkUnknown

    auth = MagicLinkAuth()
    for token in ("", "not-a-token", "x" * 64):
        with pytest.raises(MagicLinkUnknown):
            auth.redeem(token)


def test_only_a_fingerprint_of_the_token_is_kept() -> None:
    """A dump of the service's state must not yield a usable link."""
    from buyer_svc.auth import MagicLinkAuth, token_fingerprint

    tokens: list[str] = []
    auth = MagicLinkAuth(deliver=lambda email, token, expires_at: tokens.append(token))
    auth.request_login("dana.reyes@example.com")

    token = tokens[-1]
    stored = json.dumps(
        {key: str(value) for key, value in vars(auth).items()}, sort_keys=True, default=str
    )
    assert token not in stored, "the raw magic-link token is being kept in memory"
    assert token_fingerprint(token) in stored


# =======================================================================================
# The boundary itself — a session may only ever be about a pseudonym
# =======================================================================================


def test_a_session_cannot_be_opened_for_an_email() -> None:
    """The shortest wrong implementation, refused at the seam.

    ``sessions.open(email)`` would produce a working session whose "pseudonym" is the
    buyer's address, and that address would then ride on every bid request the intent fans
    out. Only a vault-issued pseudonym is an acceptable subject.
    """
    from buyer_svc.auth import InMemorySessionStore, NotAPseudonym
    from buyer_svc.vault import PseudonymVault

    sessions = InMemorySessionStore()
    for refused in ("dana.reyes@example.com", "acct-9f3c21", "Dana Reyes", ""):
        with pytest.raises(NotAPseudonym):
            sessions.open(refused)

    accepted = sessions.open(PseudonymVault().issue("dana.reyes@example.com"))
    assert accepted.session_id


def test_a_session_carries_no_identity_field() -> None:
    from buyer_svc.auth import MagicLinkAuth

    tokens: list[str] = []
    auth = MagicLinkAuth(deliver=lambda email, token, expires_at: tokens.append(token))
    auth.request_login("dana.reyes@example.com")
    session = auth.redeem(tokens[-1])

    fields = vars(session)
    for key in fields:
        assert not IDENTITY_KEY_RE.search(key), f"identity-shaped key {key!r} on a Session"
    blob = json.dumps(fields, default=str).casefold()
    for secret in ("dana", "reyes", "example.com"):
        assert secret not in blob


def test_an_expired_session_is_refused_and_forgotten() -> None:
    from buyer_svc.auth import InMemorySessionStore, SessionExpired, UnknownSession
    from buyer_svc.vault import PseudonymVault

    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    clock = {"now": now}
    sessions = InMemorySessionStore(clock=lambda: clock["now"], ttl=timedelta(hours=1))
    session = sessions.open(PseudonymVault().issue("dana.reyes@example.com"))

    clock["now"] = now + timedelta(hours=2)
    with pytest.raises(SessionExpired):
        sessions.get(session.session_id)
    with pytest.raises(UnknownSession):
        sessions.get(session.session_id)


# =======================================================================================
# Acceptance 3 — profile objects contain no identity fields
# =======================================================================================


def test_profile_objects_contain_no_identity_fields() -> None:
    """The schema property test: 240 generated accounts, every profile clean and valid.

    Three independent checks per case, because each catches a different mistake:

    * the published ``BuyerProfile`` JSON Schema (``additionalProperties: false``) — catches
      a *new* field being added to the store-facing object;
    * the R5 identity **key** regex — catches an identity-shaped key;
    * the account's own identity **values** — catches a correctly-named key carrying the
      wrong content, which the first two would both wave through.
    """
    from buyer_svc.profile import build_profile

    validator = _profile_validator()
    accounts = _generated_accounts(240)
    assert len(accounts) == 240

    for index, account in enumerate(accounts):
        pseudonym = f"psn-case-{index:04d}"
        profile = build_profile(account, pseudonym).model_dump()

        assert set(profile) == {"pseudonym", "buckets"}, profile
        assert profile["pseudonym"] == pseudonym
        assert set(profile["buckets"]) == PROFILE_BUCKET_KEYS, profile
        assert isinstance(profile["buckets"]["first_time"], bool)
        assert isinstance(profile["buckets"]["category_affinity"], list)
        validator.validate(profile)

        for key, _value in _walk(profile):
            assert not IDENTITY_KEY_RE.search(key), (
                f"case {index}: identity-shaped key {key!r} reached the profile"
            )

        blob = json.dumps(profile, sort_keys=True).casefold()
        for fragment in _identity_fragments(account):
            assert fragment not in blob, (
                f"case {index}: identity value {fragment!r} leaked into {blob}"
            )


def test_the_affinity_list_is_truncated_rather_than_a_purchase_log() -> None:
    """Coarsening is the point: a store learns a habit, not an order history."""
    from buyer_svc.profile import CATEGORY_LIMIT, build_profile

    account = {
        "email": "dana.reyes@example.com",
        "orders": [
            {"order_ref": f"ord-{n}", "total": 20.0 + n, "category": f"cat-{n}"} for n in range(20)
        ],
    }
    affinity = build_profile(account, "psn-affinity").model_dump()["buckets"]["category_affinity"]
    assert len(affinity) == CATEGORY_LIMIT


def test_coarsen_region_refuses_a_postal_code_and_a_street_address() -> None:
    """The guard, driven with the inputs only it rejects."""
    from buyer_svc.profile import coarsen_region

    assert coarsen_region("US-OR") == "US-OR"
    assert coarsen_region("de") == "DE"
    assert coarsen_region("GB-ENG") == "GB-ENG"

    for refused in (
        "97205",
        "44 Alder Way, Portland OR 97205",
        "Portland",
        "SW1A 1AA",
        17,
        None,
    ):
        assert coarsen_region(refused) is None, f"{refused!r} passed the region coarsener"


def test_a_free_text_budget_band_is_dropped_rather_than_echoed() -> None:
    """Only canonical bands, or an amount snapped into one. Nothing else passes through."""
    from buyer_svc.profile import coarsen_budget_band

    assert coarsen_budget_band({"budget_band": "100-250"}) == "100-250"
    assert coarsen_budget_band({"budget_band": 412.0}) == "250-500"
    assert coarsen_budget_band({"budget_band": "412"}) == "250-500"
    assert coarsen_budget_band({"budget_band": 4120}) == "1000+"

    # Neither a band nor an amount, and no orders to fall back on: dropped.
    assert coarsen_budget_band({"budget_band": "dana.reyes@example.com"}) is None
    assert coarsen_budget_band({"budget_band": "44 Alder Way"}) is None


def test_the_identity_backstop_rejects_a_leaking_profile() -> None:
    """:func:`identity_leaks` driven directly, with input only it rejects."""
    from buyer_svc.profile import build_profile, identity_leaks

    clean = build_profile(DANA, "psn-clean")
    assert identity_leaks(clean, DANA) == []

    leaked = {"pseudonym": "psn-leaky", "buckets": {"region": "97205"}}
    assert identity_leaks(leaked, DANA) == ["97205"]

    email_as_pseudonym = {"pseudonym": DANA["email"], "buckets": {}}
    assert "dana.reyes@example.com" in identity_leaks(email_as_pseudonym, DANA)


def test_build_profile_raises_when_a_coarsener_is_rewired_to_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backstop is wired into the builder, not merely defined beside it.

    Rewiring the region coarsener to return the postal code is exactly the change that would
    slip past the allowlist — the key is still ``region``, the shape is still a string. The
    build must refuse rather than return a disclosive profile.
    """
    from buyer_svc import profile as profile_module

    monkeypatch.setattr(profile_module, "coarsen_region", lambda value: "97205")
    with pytest.raises(profile_module.IdentityLeak) as caught:
        profile_module.build_profile(DANA, "psn-rewired")
    assert "97205" in str(caught.value)


def test_build_profile_refuses_a_missing_pseudonym() -> None:
    from buyer_svc.profile import build_profile

    for refused in ("", "   ", None):
        with pytest.raises(ValueError):
            build_profile(DANA, refused)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_profile("not-a-mapping", "psn-x")  # type: ignore[arg-type]


def test_both_import_spellings_share_one_contracts_class() -> None:
    """``buyer_svc.profile`` and ``apps.buyer.svc.src.profile`` must agree on ``BuyerProfile``.

    The tree is reachable under two dotted names and Python happily executes the file twice,
    once per name. That is harmless for the data — but only as long as both executions bound
    the *same* ``BuyerProfile`` class. If they did not, a profile built through one spelling
    would fail an ``isinstance`` check written against the other.
    """
    from buyer_svc.profile import build_profile as build_by_namespace

    from apps.buyer.svc.src.profile import build_profile as build_by_path

    by_path = build_by_path(DANA, "psn-dual")
    by_namespace = build_by_namespace(DANA, "psn-dual")
    assert type(by_path) is type(by_namespace)
    assert by_path.model_dump() == by_namespace.model_dump()


# =======================================================================================
# The real entry point — the app the deployment builds, over HTTP
# =======================================================================================


def _client_and_service():
    """The frozen ``create_app`` with a login service whose mail transport records tokens."""
    from buyer_svc.auth import InMemoryAccountDirectory, MagicLinkAuth
    from buyer_svc.auth.routes import get_auth_service
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    tokens: list[str] = []
    service = MagicLinkAuth(
        accounts=InMemoryAccountDirectory({DANA["email"]: DANA}),
        deliver=lambda email, token, expires_at: tokens.append(token),
    )
    app = create_app()
    assert "buyer_svc.auth.routes" in app.state.mounted_routers, (
        f"the frozen entrypoint did not discover the auth router: {app.state.mounted_routers}"
    )
    app.dependency_overrides[get_auth_service] = lambda: service
    return TestClient(app), service, tokens


def test_the_http_login_flow_hands_out_a_pseudonym_and_never_an_identity() -> None:
    """The whole R5 promise, over HTTP, through the app a deployment actually boots."""
    client, _service, tokens = _client_and_service()

    requested = client.post("/buyer/auth/magic-link", json={"email": "Dana.Reyes@Example.com"})
    assert requested.status_code == 202, requested.text
    assert set(requested.json()) == {"expires_at"}
    assert tokens, "no magic link was delivered"
    assert tokens[-1] not in requested.text, "the bearer token came back on the response"

    created = client.post("/buyer/auth/session", json={"token": tokens[-1]})
    assert created.status_code == 201, created.text
    session = created.json()
    assert session["pseudonym"].startswith("psn-")

    fetched = client.get("/buyer/profile", headers={"X-Buyer-Session": session["session_id"]})
    assert fetched.status_code == 200, fetched.text
    profile = fetched.json()
    assert set(profile) == {"pseudonym", "buckets"}
    assert profile["pseudonym"] == session["pseudonym"]
    assert set(profile["buckets"]) == PROFILE_BUCKET_KEYS

    wire = (requested.text + created.text + fetched.text).casefold()
    for secret in ("dana", "reyes", "example.com", "555-0100", "alder", "acct-9f3c21", "97205"):
        assert secret not in wire, f"R5: {secret!r} reached the wire: {wire}"

    # A second login is a second session, and a different pseudonym.
    client.post("/buyer/auth/magic-link", json={"email": DANA["email"]})
    again = client.post("/buyer/auth/session", json={"token": tokens[-1]}).json()
    assert again["pseudonym"] != session["pseudonym"]


def test_the_http_layer_does_not_say_why_a_link_failed() -> None:
    """Unknown, expired and already-used are one answer on the wire.

    Distinguishing them turns the endpoint into an oracle over other people's links.
    """
    client, _service, tokens = _client_and_service()
    client.post("/buyer/auth/magic-link", json={"email": DANA["email"]})
    good = tokens[-1]

    used = client.post("/buyer/auth/session", json={"token": good})
    assert used.status_code == 201
    replayed = client.post("/buyer/auth/session", json={"token": good})
    unknown = client.post("/buyer/auth/session", json={"token": "nope"})
    assert replayed.status_code == unknown.status_code == 401
    assert replayed.json() == unknown.json()


def test_the_profile_route_refuses_an_absent_or_dead_session() -> None:
    client, _service, _tokens = _client_and_service()
    assert client.get("/buyer/profile").status_code == 401
    assert client.get("/buyer/profile", headers={"X-Buyer-Session": "nope"}).status_code == 401
    assert client.get("/buyer/auth/session", headers={"X-Buyer-Session": "n"}).status_code == 401


def test_signing_out_ends_the_session_and_keeps_the_pseudonym_retired() -> None:
    client, service, tokens = _client_and_service()
    client.post("/buyer/auth/magic-link", json={"email": DANA["email"]})
    session = client.post("/buyer/auth/session", json={"token": tokens[-1]}).json()

    assert (
        client.delete(
            "/buyer/auth/session", headers={"X-Buyer-Session": session["session_id"]}
        ).status_code
        == 204
    )
    assert (
        client.get("/buyer/profile", headers={"X-Buyer-Session": session["session_id"]}).status_code
        == 401
    )

    client.post("/buyer/auth/magic-link", json={"email": DANA["email"]})
    reborn = client.post("/buyer/auth/session", json={"token": tokens[-1]}).json()
    assert reborn["pseudonym"] != session["pseudonym"]
    assert service.vault.resolve(session["pseudonym"]) == DANA["email"]


# =======================================================================================
# Acceptance 2 — the grant test, against the live cluster
# =======================================================================================


@pytest.mark.docker
def test_only_the_vault_role_can_read_the_email_pseudonym_mapping(
    vault_scratch: Any, vault_roles: Any
) -> None:
    """D5/R5: ``buyer_vault`` reads ``vault.*``; ``exchange``, ``trust_rw`` and ``app`` do not.

    Both directions on purpose. A denial test on a role that can read nothing at all is not
    evidence of isolation, so the vault role's successful read is asserted first.
    """
    import psycopg

    rows = vault_roles.fetch("buyer_vault", "select count(*) from vault.pseudonym_history")
    assert rows and isinstance(rows[0][0], int)
    vault_roles.fetch("buyer_vault", "select count(*) from vault.payment_methods")

    for role in NON_VAULT_ROLES:
        for table in ("vault.pseudonym_history", "vault.payment_methods"):
            error = vault_roles.denied(role, f"select * from {table}")  # noqa: S608
            assert isinstance(error, psycopg.errors.InsufficientPrivilege)
        # The write direction too: a role that cannot read must not be able to append either.
        vault_roles.denied(
            role,
            "insert into vault.pseudonym_history (history_id, email, pseudonym, issued_at) "
            "values ('x', 'x@example.com', 'psn-x', now())",
        )


@pytest.mark.docker
def test_the_vault_round_trips_through_postgres_as_the_vault_role(
    vault_scratch: Any, vault_roles: Any
) -> None:
    """The real store, on a real connection: issue, rotate, read back, de-anonymise."""
    from buyer_svc.vault import PostgresPseudonymStore, PseudonymVault

    connection = vault_roles.connection("buyer_vault")
    counter = {"n": 0}

    def generate() -> str:
        counter["n"] += 1
        return vault_scratch.pseudonym(f"{counter['n']:04d}")

    email = vault_scratch.email("dana")
    try:
        vault = PseudonymVault(
            PostgresPseudonymStore(connection, autocommit=True), generator=generate
        )
        issued = [vault.issue(email) for _ in range(3)]
        assert len(set(issued)) == 3

        history = vault.history(email)
        assert [row.pseudonym for row in history] == issued
        assert [row.active for row in history] == [False, False, True]
        assert vault.active(email) == issued[-1]

        # The de-anonymising direction exists, and only here.
        assert vault.resolve(issued[0]) == email
        assert vault.resolve("psn-never-issued") is None
    finally:
        connection.rollback()


@pytest.mark.docker
def test_postgres_refuses_to_reissue_a_retired_pseudonym(
    vault_scratch: Any, vault_roles: Any
) -> None:
    """ "Never reissued" is enforced by the table, not only by the application read.

    ``pseudonym_history_pseudonym_key UNIQUE (pseudonym)`` is what stops a second process
    racing the first past :meth:`PseudonymStore.is_known`.
    """
    from buyer_svc.vault import PostgresPseudonymStore, PseudonymReissued

    connection = vault_roles.connection("buyer_vault")
    email = vault_scratch.email("sam")
    pseudonym = vault_scratch.pseudonym("collide")
    try:
        store = PostgresPseudonymStore(connection, autocommit=True)
        store.record(email, pseudonym, datetime.now(UTC))
        assert store.is_known(pseudonym)
        with pytest.raises(PseudonymReissued):
            store.record(email, pseudonym, datetime.now(UTC))
    finally:
        connection.rollback()


@pytest.mark.docker
def test_a_non_vault_connection_fails_loudly_instead_of_degrading(
    vault_scratch: Any, vault_roles: Any
) -> None:
    """Handed an ``exchange`` connection, the vault raises — it does not fall back to memory.

    A silent fallback would be the worst outcome available: logins would keep working, the
    history would live in one process's heap, and "never reissued" would quietly become
    "never reissued since the last restart".
    """
    import psycopg
    from buyer_svc.vault import PostgresPseudonymStore, PseudonymVault

    connection = vault_roles.connection("exchange")
    try:
        vault = PseudonymVault(PostgresPseudonymStore(connection))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            vault.issue(vault_scratch.email("dana"))
    finally:
        connection.rollback()


@pytest.mark.docker
def test_the_store_facing_row_is_readable_while_the_mapping_is_not(
    vault_scratch: Any, vault_roles: Any
) -> None:
    """The boundary, end to end, in one database: buckets in ``app``, identity in ``vault``.

    A store's side of the network reads ``app.buyer_accounts`` through the ``exchange`` role
    and gets a pseudonym and five coarse buckets. The same role asking who that pseudonym is
    gets ``InsufficientPrivilege``. That pair — one allowed read and one denied read of the
    *same buyer* — is what R5 actually claims.
    """
    from buyer_svc.profile import build_profile, publish_profile
    from buyer_svc.vault import PostgresPseudonymStore, PseudonymVault

    pseudonym = vault_scratch.pseudonym("published")
    email = vault_scratch.email("dana")
    account = {**DANA, "email": email}

    vault_connection = vault_roles.connection("buyer_vault")
    app_connection = vault_roles.connection("app")
    try:
        vault = PseudonymVault(
            PostgresPseudonymStore(vault_connection, autocommit=True),
            generator=lambda: pseudonym,
        )
        assert vault.issue(email) == pseudonym

        profile = build_profile(account, pseudonym)
        publish_profile(app_connection, profile)
        app_connection.commit()
    finally:
        vault_connection.rollback()
        app_connection.rollback()

    published = vault_roles.fetch(
        "exchange",
        "select pseudonym, buckets from app.buyer_accounts where pseudonym = %s",
        (pseudonym,),
    )
    assert len(published) == 1, "the store-facing row is not readable by the auction role"
    row_pseudonym, buckets = published[0]
    assert row_pseudonym == pseudonym
    assert set(buckets) == PROFILE_BUCKET_KEYS
    blob = json.dumps({"pseudonym": row_pseudonym, "buckets": buckets}).casefold()
    for secret in ("dana", "reyes", "555-0100", "alder", "acct-9f3c21", "97205"):
        assert secret not in blob, f"R5: {secret!r} is visible to the auction role: {blob}"

    vault_roles.denied(
        "exchange",
        "select email from vault.pseudonym_history where pseudonym = %s",
        (pseudonym,),
    )
    resolved = vault_roles.fetch(
        "buyer_vault",
        "select email from vault.pseudonym_history where pseudonym = %s",
        (pseudonym,),
    )
    assert resolved == [(email,)], "the vault role must still be able to resolve the pseudonym"
