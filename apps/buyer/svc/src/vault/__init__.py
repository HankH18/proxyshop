"""``buyer_svc.vault`` — the buyer identity vault (T-070, SPEC R5).

The only place in the repository that knows which human is behind a pseudonym. Everything
it persists lives in the Postgres ``vault`` schema, which D5's grant model leaves reachable
by the single role ``buyer_vault``; ``exchange``, ``trust_rw`` and ``app`` are refused, and
``apps/buyer/svc/tests/test_auth_vault.py`` proves it against the live cluster.

Public surface::

    from apps.buyer.svc.src.vault import PseudonymVault
    vault = PseudonymVault()
    pseudonym = vault.issue("dana.reyes@example.com")   # a NEW one, every session

Imports are relative on purpose. This tree is reachable under two dotted spellings —
``buyer_svc.vault`` (through the tracked ``.pkgroot/buyer_svc`` symlink) and
``apps.buyer.svc.src.vault`` (the repo-root path the frozen acceptance suite uses, which
bootstraps *only* the repo root onto ``sys.path``). A relative import resolves correctly
under both; an absolute one silently depends on which of the two happens to be importable.
"""

from __future__ import annotations

from .pseudonyms import (
    PSEUDONYM_PREFIX,
    PseudonymExhausted,
    PseudonymVault,
    default_pseudonym,
    normalise_buyer_key,
)
from .store import (
    InMemoryPseudonymStore,
    PostgresPseudonymStore,
    PseudonymRecord,
    PseudonymReissued,
    PseudonymStore,
)

__all__ = [
    "PSEUDONYM_PREFIX",
    "InMemoryPseudonymStore",
    "PostgresPseudonymStore",
    "PseudonymExhausted",
    "PseudonymRecord",
    "PseudonymReissued",
    "PseudonymStore",
    "PseudonymVault",
    "default_pseudonym",
    "normalise_buyer_key",
]
