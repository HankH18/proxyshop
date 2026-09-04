"""An append-only history of a store's envelope versions.

DESIGN puts the real thing in ``sealed.envelopes`` (versioned, and with **no grant for the
exchange role** — S7). This is the process-local stand-in the merchant service's own routes
read and write, in the same spirit as ``merchant_svc.collector.PIXEL_INBOX``: a real
structure with the real invariants, so the rules are written and tested here rather than
discovered later inside a migration.

The invariant that earns the module: **a version number never goes backwards.** Every state
the envelope has ever been in is appended, nothing is overwritten, and a record whose version
is lower than the head's is refused. An envelope store that let a stale writer put v3 back
after v4 landed would silently reinstate limits the merchant had already replaced.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from merchant_svc.envelope.model import ACTIVE, SHADOW, Envelope, EnvelopeError
from merchant_svc.envelope.versions import activate_envelope, edit_envelope, kill_envelope


class UnknownStore(EnvelopeError, KeyError):
    """No envelope has ever been recorded for that store."""


class VersionWentBackwards(EnvelopeError, ValueError):
    """The submitted version is older than the one already on file."""


class StoreMismatch(EnvelopeError, ValueError):
    """The submitted envelope names a different store than the one being written."""


class EnvelopeVersions:
    """Every version of every store's envelope, newest last, nothing ever rewritten."""

    def __init__(self) -> None:
        self._history: dict[str, list[Envelope]] = {}
        # `record` is a read-modify-write over `_history`, and FastAPI runs synchronous route
        # handlers in the anyio threadpool, so two writers really are concurrent.
        self._lock = threading.Lock()

    def record(self, envelope: Any) -> Envelope:
        """Append one envelope state to its store's history and return what was stored.

        Args:
            envelope: the version to file.

        Returns:
            The validated envelope now at the head of that store's history.

        Raises:
            VersionWentBackwards: the store already has a *higher* version on file.
            EnvelopeInvalid: ``envelope`` is not a DESIGN Envelope.
        """
        candidate = Envelope.from_obj(envelope)
        with self._lock:
            history = self._history.setdefault(candidate.store_id, [])
            if history and candidate.version < history[-1].version:
                raise VersionWentBackwards(
                    f"store {candidate.store_id!r} is on envelope v{history[-1].version}; "
                    f"v{candidate.version} is older and would reinstate replaced limits"
                )
            history.append(candidate)
        return candidate

    def current(self, store_id: str) -> Envelope:
        """The store's newest envelope state.

        Raises:
            UnknownStore: nothing has been recorded for ``store_id``.
        """
        history = self._history.get(store_id)
        if not history:
            raise UnknownStore(f"no envelope has been recorded for store {store_id!r}")
        return history[-1]

    def history(self, store_id: str) -> tuple[Envelope, ...]:
        """Every state that store's envelope has been in, oldest first."""
        return tuple(self._history.get(store_id, ()))

    def stores(self) -> tuple[str, ...]:
        """Every store with an envelope on file, in sorted order."""
        return tuple(sorted(self._history))

    def is_live(self, store_id: str) -> bool:
        """``True`` only when the store's newest envelope is *declared* active.

        An unknown store is not live. This is the question the bidding path asks, and it is
        answered by equality against the one live value — never by "not killed".
        """
        try:
            return self.current(store_id).activation == ACTIVE
        except UnknownStore:
            return False

    def put(self, store_id: str, terms: Any) -> Envelope:
        """Replace the store's envelope terms, creating the next version.

        The submitted document's ``version``, ``activation`` and ``approval`` are **not**
        honoured: version is derived from what is already on file, and a new set of terms has
        by definition not been approved. Activation is a separate, artifact-bearing step.

        Raises:
            StoreMismatch: the submitted document names a different store than ``store_id``.
            EnvelopeInvalid: the submitted terms are not a DESIGN Envelope.
        """
        submitted = Envelope.from_obj(terms, approval=None)
        if submitted.store_id != store_id:
            raise StoreMismatch(
                f"the submitted envelope belongs to store {submitted.store_id!r}, not "
                f"{store_id!r}; an envelope is not moved between stores by a PUT"
            )
        try:
            head = self.current(store_id)
        except UnknownStore:
            # A store's first envelope is filed exactly as submitted apart from its state:
            # nothing has been approved yet, so it starts in shadow whatever the body said.
            return self.record(submitted.with_activation(SHADOW, None))
        changes: Mapping[str, Any] = {
            "floors": submitted.to_dict()["floors"],
            "max_discount_pct": submitted.max_discount_pct,
            "budget_cap": submitted.budget_cap,
            "pursue_clusters": list(submitted.pursue_clusters),
            "standing_commitments": submitted.to_dict()["standing_commitments"],
        }
        return self.record(edit_envelope(head, changes))

    def activate(self, store_id: str, approval: Any) -> Envelope:
        """Activate the store's *current* version against a written approval.

        Activation is deliberately bound to the head of the history rather than to whatever
        envelope the caller happens to be holding: that is what closes the edit-versus-approve
        race. An approval collected against v1 does not activate v2, because v2's terms hash
        differently — the approval is refused rather than applied to the wrong document.
        """
        return self.record(activate_envelope(self.current(store_id), approval))

    def kill(self, store_id: str) -> Envelope:
        """Move the store's current envelope to ``killed`` and file that state."""
        return self.record(kill_envelope(self.current(store_id)))


#: The service-wide history. Process-local, exactly like the pixel inbox: a restart forgets
#: it, and losing it can only *stop* a store bidding, never start one.
ENVELOPES = EnvelopeVersions()
