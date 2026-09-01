"""Transient-versus-permanent classification for the ledger's datastore calls.

Owned by T-011. It also carries carry-forward **CF-2**, deliberately and permanently.

CF-2, restated so the next person to read this file knows why an import is load-bearing:
``.importlinter``'s D39 contract forbids every member package from importing ``redis``, with
one carve-out --

.. code-block:: ini

    ignore_imports =
        ** -> redis.exceptions

-- so that ``except redis.exceptions.ConnectionError`` stays available. In import-linter's
wildcard syntax ``*`` matches exactly **one** module segment and ``**`` matches zero or more.
The single star was a real bug that broke four downstream tickets, and the double star is the
fix. But a carve-out for an import that nothing performs is a carve-out nothing tests: with no
tracked file importing ``redis.exceptions``, reverting ``**`` to ``*`` still passes
``make verify``, and the first ticket to write ``except redis.exceptions.ConnectionError``
inherits a break it did not cause.

This module is that file. ``trust.ledger.errors`` is **three** segments deep, so it is matched
by ``**`` and not by ``*``; the import below is a legal, useful one that the D39 contract has
to forgive, and ``apps/trust/tests/test_schema_grants.py`` asserts both directions -- that
``lint-imports`` passes with the shipped configuration and **fails**, naming this module, when
the carve-out is narrowed back to a single star.

Note what is *not* imported: no client, no connection pool, no ``from_url``. The D39 contract
and ``scripts/check_verify_contracts.py`` both allow exactly the exceptions submodule, because
catching an error is not constructing an unprefixed client that another worker's ``FLUSHDB``
would erase.
"""

from __future__ import annotations

import psycopg
import redis.exceptions

__all__ = ["TRANSIENT_DATASTORE_ERRORS", "LedgerError", "is_transient_datastore_error"]


class LedgerError(RuntimeError):
    """A ledger operation failed for a reason the caller is expected to handle."""


#: Failures worth retrying: the datastore was unreachable or dropped the connection. A
#: constraint violation is emphatically *not* here -- a rejected ``prev_hash`` or a duplicate
#: ``idempotency_key`` means the write was wrong, and retrying a wrong write just makes it
#: wrong again more slowly.
TRANSIENT_DATASTORE_ERRORS: tuple[type[BaseException], ...] = (
    psycopg.OperationalError,
    redis.exceptions.ConnectionError,
    redis.exceptions.TimeoutError,
)


def is_transient_datastore_error(exc: BaseException) -> bool:
    """Is ``exc`` worth retrying?

    Args:
        exc: the exception raised by a datastore call.

    Returns:
        ``True`` for connection-level failures against Postgres or Redis, ``False`` for
        everything else -- integrity violations, privilege denials and programming errors
        all included, because none of them becomes true on a second attempt.
    """
    if isinstance(exc, psycopg.errors.IntegrityError | psycopg.errors.InsufficientPrivilege):
        return False
    return isinstance(exc, TRANSIENT_DATASTORE_ERRORS)
