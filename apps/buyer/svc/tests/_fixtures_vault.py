"""Fixtures for T-070's identity-vault, grant and login tests.

Owned by T-070. Every name here is prefixed ``vault_`` because ``apps/buyer/svc/tests/`` is
shared with T-071, T-072 and T-073 and ``proxyshop_support.fixture_loader`` binds one name to
one fixture across the whole directory.

CARRY-FORWARD CF-4 — the idle-in-transaction deadlock
-----------------------------------------------------
``pg_admin`` (root ``conftest.py``) is autocommit; ``pg_role`` hands back a connection in
default transactional mode, cached per role for the whole test. So the canonical privilege
assertion —

.. code-block:: python

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg_role("exchange").cursor().execute("select * from vault.pseudonym_history")

— leaves that connection **idle in transaction holding a lock**, and any later ``DELETE`` or
DDL through ``pg_admin`` blocks rather than raising. :class:`VaultRoleRunner` closes that off
structurally: every method rolls its connection back before returning, so a test that goes
through it cannot leave one open. T-011 hit this first and wrote it down; this file is the
buyer-side application of the same rule.

Scratch data, and why nothing here truncates
--------------------------------------------
``vault.pseudonym_history`` and ``app.buyer_accounts`` are shared with every other suite that
runs against this worker's database. Truncating them would be a cross-suite wipe dressed up
as test hygiene. Instead every row this suite writes carries a marker, and
:func:`vault_scratch` deletes exactly the marked rows — before the test as well as after
it, so a crashed run does not poison the next one.

The email marker and the pseudonym marker are deliberately DIFFERENT strings. Sharing one
made these fixtures fail their own product code, correctly: an email of
``dana.t070-vault-scratch@example.invalid`` under a pseudonym of
``psn-t070-vault-scratch-published`` really does embed the address in the pseudonym, and
:func:`buyer_svc.profile.build_profile` raised :class:`~buyer_svc.profile.IdentityLeak`
rather than publish it. The test data was wrong and the guard was right; keeping the
markers disjoint is what makes a leak reported here a leak in the product.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest

#: Markers on every row this suite writes. Long and specific enough that no other suite's
#: data can collide with them, and no row of ours can escape the cleanup below. Disjoint on
#: purpose — see the module docstring.
EMAIL_MARKER = "t070scratchmail"
PSEUDONYM_MARKER = "t070scratchpsn"


class VaultScratch:
    """Namer for this suite's scratch rows, and the thing :func:`vault_scratch` yields."""

    email_marker = EMAIL_MARKER
    pseudonym_marker = PSEUDONYM_MARKER

    def email(self, name: str) -> str:
        """A scratch email address the cleanup will find."""
        return f"{name}.{EMAIL_MARKER}@example.invalid"

    def pseudonym(self, name: str) -> str:
        """A scratch pseudonym the cleanup will find, carrying the real vault prefix."""
        return f"psn-{PSEUDONYM_MARKER}-{name}"


class VaultRoleRunner:
    """Run statements as a least-privilege role, never leaving a transaction open (CF-4)."""

    def __init__(self, connect: Callable[[str], Any]) -> None:
        self._connect = connect
        self._roles: set[str] = set()

    def connection(self, role: str) -> Any:
        """The cached connection for ``role``. Prefer the methods below to using it raw."""
        self._roles.add(role)
        return self._connect(role)

    def reset(self) -> None:
        """Roll back every role connection this runner has opened."""
        for role in self._roles:
            self._connect(role).rollback()

    def fetch(self, role: str, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
        """Execute ``sql`` as ``role`` and return every row. Rolls back afterwards."""
        connection = self.connection(role)
        try:
            with connection.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())
        finally:
            connection.rollback()

    def execute(self, role: str, sql: str, params: Sequence[Any] | None = None) -> None:
        """Execute a non-returning statement as ``role``. Rolls back afterwards."""
        connection = self.connection(role)
        try:
            with connection.cursor() as cur:
                cur.execute(sql, params)
        finally:
            connection.rollback()

    def denied(self, role: str, sql: str, params: Sequence[Any] | None = None) -> Exception:
        """Assert ``role`` cannot run ``sql``, and return the error it raised.

        Fails the test if the statement succeeds — a denial test that silently passes when
        the grant model is wide open is worse than no test.
        """
        import psycopg

        connection = self.connection(role)
        try:
            with connection.cursor() as cur:
                cur.execute(sql, params)
        except psycopg.errors.InsufficientPrivilege as exc:
            return exc
        else:
            raise AssertionError(
                f"role {role!r} was ALLOWED to run {sql!r} — the D5 grant model is not holding"
            )
        finally:
            connection.rollback()


@pytest.fixture(scope="session")
def vault_migrated(pg_admin: Any) -> str:
    """Bring this worker's database up to the full T-011 schema, once per session.

    Reuses T-011's own migration runner rather than a second copy of the DDL: the thing under
    test is the grant model those files ship, so applying anything else would be grading a
    schema nobody deploys.
    """
    from apps.trust.src.ledger import apply_migrations

    return " ".join(apply_migrations(pg_admin))


@pytest.fixture
def vault_scratch(vault_migrated: str, pg_admin: Any) -> Iterator[VaultScratch]:
    """Yield a :class:`VaultScratch` namer, with this suite's rows removed before and after.

    Deletion order follows the foreign keys: ``vault.payment_methods`` references
    ``vault.pseudonym_history (pseudonym)`` with ``ON DELETE RESTRICT``, so the child goes
    first. ``app.buyer_accounts`` has no FK into ``vault`` at all — that absence is the
    boundary, not an oversight — so it is independent.
    """

    emails = f"%{EMAIL_MARKER}%"
    pseudonyms = f"%{PSEUDONYM_MARKER}%"

    def purge() -> None:
        with pg_admin.cursor() as cur:
            cur.execute(
                "delete from vault.payment_methods where pseudonym in "
                "(select pseudonym from vault.pseudonym_history "
                " where email like %s or pseudonym like %s)",
                (emails, pseudonyms),
            )
            cur.execute(
                "delete from vault.pseudonym_history where email like %s or pseudonym like %s",
                (emails, pseudonyms),
            )
            cur.execute("delete from app.buyer_accounts where pseudonym like %s", (pseudonyms,))

    purge()
    try:
        yield VaultScratch()
    finally:
        purge()


@pytest.fixture
def vault_roles(pg_role: Callable[[str], Any]) -> Iterator[VaultRoleRunner]:
    """A :class:`VaultRoleRunner` over the ``pg_role`` factory (CF-4)."""
    runner = VaultRoleRunner(pg_role)
    try:
        yield runner
    finally:
        runner.reset()
