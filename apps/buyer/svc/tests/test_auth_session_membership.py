"""T-163 — a session subject must be one the vault actually issued, not one shaped like it.

``InMemorySessionStore.open`` used to refuse on :data:`~buyer_svc.vault.PSEUDONYM_PREFIX`
alone, which made the pseudonym a bearer credential whose *format* was its only proof:
``"psn-" + the buyer's own email`` opened a session and that session authenticated
``GET /buyer/profile``. These tests are the contract of the repair, driven end to end over
the ASGI app as well as at the seam, because "the store refuses it" and "the door refuses
it" are different claims and only the second one is the security property.

Every assertion here is made against a pure function, the module's public surface or an
in-process client. Nothing in this file touches a datastore.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

FORGED_EMAIL = "dana.reyes@example.com"


def test_a_forged_prefix_does_not_open_a_session_on_the_production_service() -> None:
    """The ticket's own reproduction, at the seam the production builder hands out."""
    from buyer_svc.auth import MagicLinkAuth, UnissuedPseudonym

    service = MagicLinkAuth()
    forged = f"psn-{FORGED_EMAIL}"
    assert service.vault.resolve(forged) is None, "fixture error: the vault must not know it"

    with pytest.raises(UnissuedPseudonym):
        service.sessions.open(forged)


def test_a_forged_subject_cannot_authenticate_the_profile_route() -> None:
    """The property that matters: a forged subject buys no access to a buyer's profile.

    Refusing inside the store would be worth little if the HTTP surface had another way to
    mint a session, so the door is tried rather than the seam: every route that answers an
    ``X-Buyer-Session`` is driven with a session id a forger could plausibly guess at, and a
    real login is driven alongside it so a blanket 401 cannot be mistaken for a repair.
    """
    from buyer_svc.auth import InMemoryAccountDirectory, MagicLinkAuth
    from buyer_svc.auth.routes import get_auth_service
    from buyer_svc.main import create_app
    from fastapi.testclient import TestClient

    tokens: list[str] = []
    service = MagicLinkAuth(
        accounts=InMemoryAccountDirectory({FORGED_EMAIL: {"email": FORGED_EMAIL, "orders": []}}),
        deliver=lambda email, token, expires_at: tokens.append(token),
    )
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: service
    client = TestClient(app, raise_server_exceptions=False)

    # Control: the real login works, so a 401 below is the forgery being refused and not the
    # route being broken.
    assert client.post("/buyer/auth/magic-link", json={"email": FORGED_EMAIL}).status_code == 202
    opened = client.post("/buyer/auth/session", json={"token": tokens[-1]})
    assert opened.status_code == 201, opened.text
    real = opened.json()["session_id"]
    assert client.get("/buyer/profile", headers={"X-Buyer-Session": real}).status_code == 200

    # The forgery: no session id was ever minted for it, and none can be.
    for header in (f"psn-{FORGED_EMAIL}", "psn-" + "0" * 32):
        refused = client.get("/buyer/profile", headers={"X-Buyer-Session": header})
        assert refused.status_code == 401, (
            f"a forged session header answered {refused.status_code}: {refused.text}"
        )


def test_refusing_a_forged_subject_does_not_echo_it() -> None:
    """The refusal must not become the disclosure (T-133).

    The value that reaches this guard is, in the case worth catching, the buyer's own email
    with four characters glued to the front. A message that quoted it would put an address
    into a traceback, a log line and a 500 body — the R5 guard as the shortest R5 leak.
    """
    from buyer_svc.auth import MagicLinkAuth, UnissuedPseudonym

    service = MagicLinkAuth()
    with pytest.raises(UnissuedPseudonym) as caught:
        service.sessions.open(f"psn-{FORGED_EMAIL}")

    message = str(caught.value).casefold()
    for fragment in ("dana", "reyes", "example.com"):
        assert fragment not in message, f"{fragment!r} was echoed by the refusal: {message}"
    assert "pseudonym" in message


def test_the_membership_refusal_is_still_a_session_error_and_a_not_a_pseudonym() -> None:
    """The new refusal must not escape a caller that already handled the old one.

    ``routes.read_profile`` catches ``SessionError`` and answers 401; callers elsewhere catch
    ``NotAPseudonym``. A forged subject raising something outside both hierarchies would
    surface as an unhandled 500 carrying the subject, which is the leak above by another
    route.
    """
    from buyer_svc.auth import MagicLinkAuth, NotAPseudonym, SessionError, UnissuedPseudonym

    assert issubclass(UnissuedPseudonym, NotAPseudonym)
    assert issubclass(UnissuedPseudonym, SessionError)

    service = MagicLinkAuth()
    with pytest.raises(SessionError):
        service.sessions.open(f"psn-{FORGED_EMAIL}")


def test_a_vault_issued_pseudonym_still_opens_a_session() -> None:
    """The guard must not have been bought by refusing everything.

    Both halves: the login gesture end to end, and a pseudonym drawn straight from the
    service's own vault handed back to its own store.
    """
    from buyer_svc.auth import MagicLinkAuth

    tokens: list[str] = []
    service = MagicLinkAuth(deliver=lambda email, token, expires_at: tokens.append(token))
    service.request_login(FORGED_EMAIL)
    session = service.redeem(tokens[-1])
    assert session.pseudonym.startswith("psn-")
    assert service.session(session.session_id).session_id == session.session_id

    issued = service.vault.issue("samir.okafor@example.com")
    assert service.sessions.open(issued).pseudonym == issued


def test_a_store_given_no_vault_keeps_the_format_guard_and_nothing_more() -> None:
    """An unbound store is a documented state, not an accidental bypass.

    ``InMemorySessionStore()`` on its own has no vault to ask; it must therefore still refuse
    the ``sessions.open(email)`` mistake, and it must not pretend to answer a membership
    question it cannot. Production never reaches this state — ``MagicLinkAuth`` binds — and
    the test below pins that difference so a future edit cannot quietly make the bound case
    behave like the unbound one.
    """
    from buyer_svc.auth import InMemorySessionStore, NotAPseudonym
    from buyer_svc.vault import PseudonymVault

    unbound = InMemorySessionStore()
    assert unbound.vault is None
    for refused in (FORGED_EMAIL, "acct-9f3c21", "Dana Reyes", ""):
        with pytest.raises(NotAPseudonym):
            unbound.open(refused)
    # Format-only, as documented: a foreign vault's pseudonym is admitted by an unbound store.
    assert unbound.open(PseudonymVault().issue(FORGED_EMAIL)).session_id


def test_magic_link_auth_binds_its_own_vault_into_whatever_store_it_was_handed() -> None:
    """The wiring is the fix; a custom store must get the check too, and keep its own clock."""
    from buyer_svc.auth import InMemorySessionStore, MagicLinkAuth, UnissuedPseudonym
    from buyer_svc.vault import PseudonymVault

    now = {"t": datetime(2026, 3, 1, 12, 0, tzinfo=UTC)}
    store = InMemorySessionStore(clock=lambda: now["t"], ttl=timedelta(hours=1))
    vault = PseudonymVault()
    MagicLinkAuth(vault=vault, sessions=store)

    assert store.vault is vault, "MagicLinkAuth did not bind its vault into the store it got"
    with pytest.raises(UnissuedPseudonym):
        store.open(f"psn-{FORGED_EMAIL}")
    # The injected clock survived the binding.
    assert store.open(vault.issue(FORGED_EMAIL)).expires_at == now["t"] + timedelta(hours=1)


def test_binding_never_retargets_a_store_that_was_already_given_a_vault() -> None:
    """Two vaults and one store is a wiring mistake; it must not silently pick the newer one.

    Retargeting would let the membership check be answered by a vault that did not issue what
    the store is holding — which is the pre-fix behaviour wearing a bound store's clothes.
    """
    from buyer_svc.auth import InMemorySessionStore, MagicLinkAuth, UnissuedPseudonym
    from buyer_svc.vault import PseudonymVault

    issuing = PseudonymVault()
    other = PseudonymVault()
    store = InMemorySessionStore(vault=issuing)
    MagicLinkAuth(vault=other, sessions=store)

    assert store.vault is issuing
    with pytest.raises(UnissuedPseudonym):
        store.open(other.issue(FORGED_EMAIL))
    assert store.open(issuing.issue(FORGED_EMAIL)).session_id


def test_a_custom_session_store_without_bind_vault_is_not_a_crash() -> None:
    """The seam stays a seam: a third-party ``SessionStore`` predating this fix still works."""
    from buyer_svc.auth import MagicLinkAuth
    from buyer_svc.auth.sessions import Session

    class _Foreign:
        """Implements the three methods the service calls, and nothing else."""

        def __init__(self) -> None:
            self.opened: list[str] = []

        def open(self, pseudonym: str, *, ttl: timedelta | None = None) -> Session:
            self.opened.append(pseudonym)
            issued = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
            return Session(
                session_id="sid",
                pseudonym=pseudonym,
                issued_at=issued,
                expires_at=issued + (ttl or timedelta(hours=1)),
            )

        def get(self, session_id: str) -> Session:  # pragma: no cover - not exercised here
            raise AssertionError("not reached")

        def close(self, session_id: str) -> None:  # pragma: no cover - not exercised here
            raise AssertionError("not reached")

    tokens: list[str] = []
    store = _Foreign()
    service = MagicLinkAuth(sessions=store, deliver=lambda e, t, x: tokens.append(t))
    service.request_login(FORGED_EMAIL)
    assert service.redeem(tokens[-1]).session_id == "sid"
    assert store.opened and store.opened[0].startswith("psn-")


# =======================================================================================
# Membership is not currency. Appended: a defence-in-depth gate on T-163's own repair.
# =======================================================================================


def test_a_retired_pseudonym_does_not_open_a_session() -> None:
    """DEFENCE IN DEPTH, NOT A LIVE HOLE — read this before judging its severity.

    Nothing reachable over HTTP accepts a pseudonym as input. ``POST /buyer/auth/session``
    takes a token, the other routes take a session id, and the only production caller of
    ``SessionStore.open`` anywhere in the tree is
    ``MagicLinkAuth.redeem`` (``auth/magic_link.py``), which supplies a pseudonym it has just
    had issued. There is no request an attacker can send today that reaches this check with a
    retired value. This gate exists so the guarantee matches what T-163 claims, so that the
    next route or job that accepts a subject inherits the strong version rather than the weak
    one — not because a door is standing open.

    What was actually delivered was weaker than the ticket's words. ``admit`` asked
    ``vault.resolve()``, which answers for RETIRED rows as well as live ones, so the property
    enforced was "the vault ever issued this" rather than "this is the subject the buyer
    currently holds". MEASURED before the repair: issue ``p1`` for an address, rotate to
    ``p2``, and ``store.open(p1)`` returned a working session that authenticated
    ``GET /buyer/profile``. ``PseudonymVault.active()`` existed and was never consulted.

    That set matters more than it looks. Under T-142 — the same branch — every served
    pseudonym is written into ``app.buyer_accounts``, which this code describes as a table
    "every store-facing role can read". The values satisfying ``admit`` were therefore exactly
    the values published to stores.
    """
    # `NotAPseudonym` and not `RetiredPseudonym` here on purpose: the refusal is asserted
    # through a name that predates this repair, so against the unrepaired code this node
    # fails with "DID NOT RAISE" — the behaviour — rather than with an ImportError over a
    # symbol that simply does not exist yet. A gate whose only witness is a missing name
    # proves a rename happened, not that a defect was closed.
    from buyer_svc.auth import MagicLinkAuth, NotAPseudonym

    service = MagicLinkAuth()
    first = service.vault.issue(FORGED_EMAIL)
    second = service.vault.issue(FORGED_EMAIL)

    # ARMED: the vault really did rotate, and it really does still know the retired value —
    # otherwise this would be re-testing the forgery case under a new name.
    assert first != second
    assert service.vault.resolve(first) is not None, "fixture error: the vault forgot p1"
    assert service.vault.active(FORGED_EMAIL) == second

    with pytest.raises(NotAPseudonym) as caught:
        service.sessions.open(first)

    # ...and it is refused as retired, not misreported as a forgery the vault never issued.
    from buyer_svc.auth import RetiredPseudonym

    assert isinstance(caught.value, RetiredPseudonym), type(caught.value)

    # The subject the buyer actually holds still opens one, so the guard was not bought by
    # refusing everything.
    assert service.sessions.open(second).pseudonym == second


def test_the_retired_refusal_is_a_session_error_and_never_echoes_the_subject() -> None:
    """The new refusal must not escape a caller that already handled the old ones (T-133).

    ``routes.read_profile`` catches ``SessionError``; callers elsewhere catch
    ``NotAPseudonym``. A refusal outside both hierarchies surfaces as an unhandled 500
    carrying the subject — and a subject built from a rotated pseudonym is still a value the
    vault can map straight back to an email.
    """
    from buyer_svc.auth import MagicLinkAuth, NotAPseudonym, SessionError, UnissuedPseudonym

    service = MagicLinkAuth()
    retired = service.vault.issue(FORGED_EMAIL)
    service.vault.issue(FORGED_EMAIL)

    # The behavioural half first, through names that predate the repair, so the witness here
    # is "the retired subject opened a session" and not "a new class is missing".
    with pytest.raises(SessionError) as caught:
        service.sessions.open(retired)
    message = str(caught.value).casefold()
    for fragment in ("dana", "reyes", "example.com", retired.casefold()):
        assert fragment not in message, f"{fragment!r} was echoed by the refusal: {message}"

    from buyer_svc.auth import RetiredPseudonym

    assert issubclass(RetiredPseudonym, NotAPseudonym)
    assert issubclass(RetiredPseudonym, SessionError)
    # NOT a forgery: a reader catching "the vault never issued this" must not silently be
    # catching "the vault issued this and moved on".
    assert not issubclass(RetiredPseudonym, UnissuedPseudonym)


def test_the_ordinary_login_path_is_untouched_by_the_rotation_check() -> None:
    """The check must not break the one production caller that reaches it.

    ``redeem`` issues a pseudonym and immediately opens a session on it, and issuing is what
    retires the previous one — so every login rotates a subject out from under a check that
    now looks at rotation. Logging in repeatedly must therefore keep working, and a session
    opened before a rotation must keep working too: a live session is a session, and ending
    it is ``DELETE /buyer/auth/session``'s job, not this guard's.
    """
    from buyer_svc.auth import InMemoryAccountDirectory, MagicLinkAuth

    tokens: list[str] = []
    service = MagicLinkAuth(
        accounts=InMemoryAccountDirectory({FORGED_EMAIL: {"email": FORGED_EMAIL, "orders": []}}),
        deliver=lambda email, token, expires_at: tokens.append(token),
    )

    sessions = []
    for _ in range(5):
        service.request_login(FORGED_EMAIL)
        sessions.append(service.redeem(tokens[-1]))
    assert len({session.pseudonym for session in sessions}) == 5, "the vault stopped rotating"

    # The session opened four rotations ago still resolves and still serves a profile.
    oldest = sessions[0]
    assert service.session(oldest.session_id).session_id == oldest.session_id
    assert service.profile_for(oldest.session_id).model_dump()["pseudonym"] == oldest.pseudonym
