"""An append-only history of a store's envelope versions.

DESIGN puts the real thing in ``sealed.envelopes`` (versioned, and with **no grant for the
exchange role** — S7), and :mod:`merchant_svc.envelope.repository` is the seam that reaches
it. This module is where the *rules* live, over whatever backing store it was handed: the
default is in-memory, so a merchant service booted without a database behaves exactly as it
always has, and the forgetting is now a choice of repository rather than a property of the
class (T-239).

The invariant that earns the module: **a version number never goes backwards.** Every state
the envelope has ever been in is appended, nothing is overwritten, and a record whose version
is lower than the head's is refused. An envelope store that let a stale writer put v3 back
after v4 landed would silently reinstate limits the merchant had already replaced.

Two rules and one boundary, and the boundary is the reason both rules are stated here rather
than in the repository:

* **a version never goes backwards** — checked on every ``record`` AND on every load, so a
  table that somehow holds v4 before v3 is refused rather than trusted for having come from
  a database;
* **live means approved** — an ``active`` version carries the approval artifact that
  authorized it (R6, T-248), on the way in and on the way back.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from .digest import approval_covers, approval_digest
from .model import ACTIVE, FIRST_VERSION, SHADOW, ApprovalRejected, Envelope, EnvelopeError
from .repository import EnvelopeRepository, InMemoryEnvelopeRepository, restorable
from .versions import activate_envelope, edit_envelope, kill_envelope, revive_envelope


class UnknownStore(EnvelopeError, KeyError):
    """No envelope has ever been recorded for that store."""


class VersionWentBackwards(EnvelopeError, ValueError):
    """The submitted version is older than the one already on file."""


class StoreMismatch(EnvelopeError, ValueError):
    """The submitted envelope names a different store than the one being written."""


def _refuse_unapproved_activation(candidate: Envelope) -> None:
    """Refuse any version that calls itself ``active`` without an approval that covers it.

    T-248. ``record`` is the one door every other write goes through — ``put``, ``activate``
    and ``kill`` all end in it — and it used to validate only the contract shape and the
    monotonic-version rule. ``activation`` is a plain string field of the DESIGN document, so
    ``record({... "activation": "active"})`` filed a live envelope with ``approval=None`` and
    ``is_live`` then answered ``True``. The HTTP surface never does that (``put`` forces
    ``shadow`` and ``activate`` goes through :func:`~merchant_svc.envelope.versions.
    activate_envelope`), but ``record`` is public and exported, so the approve-then-edit
    guarantee held only for callers who happened to use the polite entry points.

    Both halves are checked, because the artifact alone is not the guarantee:

    * an artifact must **exist** — R6 asks for a *recorded* written approval, and "the caller
      said so" is not a record;
    * it must be **bound to these very terms**. Without this,
      ``record(edit_envelope(live_v1, {...}).with_activation(ACTIVE, live_v1.approval))``
      would carry v1's approval onto v2's terms — the exact edit-after-approve substitution
      :func:`activate_envelope` refuses, arriving through the back door instead.

    Raises:
        ApprovalRejected: the version is ``active`` with no artifact, or with one bound to a
            different document.
    """
    if candidate.activation != ACTIVE:
        return
    approval = candidate.approval
    if approval is None:
        raise ApprovalRejected(
            f"store {candidate.store_id!r} envelope v{candidate.version} was submitted as "
            f"{ACTIVE!r} with no written approval artifact; an envelope is never activated "
            "on the caller's say-so alone (R6) — record it in shadow and activate it against "
            "an approval"
        )
    if not approval_covers(candidate, approval.envelope_hash):
        raise ApprovalRejected(
            f"the approval by {approval.approver!r} is bound to {approval.envelope_hash!r}, "
            f"which is not store {candidate.store_id!r} envelope v{candidate.version} "
            f"({approval_digest(candidate)!r}); an approval only activates the exact terms it "
            "was given for"
        )


class EnvelopeVersions:
    """Every version of every store's envelope, newest last, nothing ever rewritten."""

    def __init__(self, repository: EnvelopeRepository | None = None) -> None:
        """Open the history, loading whatever the backing store already holds.

        Args:
            repository: where versions outlive this process. ``None`` — the default, and what
                the module-level :data:`ENVELOPES` gets — is an
                :class:`~merchant_svc.envelope.repository.InMemoryEnvelopeRepository`, so a
                merchant service booted without a database behaves exactly as it always has.

        The load goes through :meth:`_restore` rather than straight into ``_history``, so the
        three invariants are re-checked on the way in: a table that somehow holds v4 before v3
        for one store, or a version filed ``active`` whose approval artifact did not survive
        the boundary, is refused or downgraded here rather than trusted because it came from
        a database (T-239, T-248).
        """
        self._repository: EnvelopeRepository = repository or InMemoryEnvelopeRepository()
        self._history: dict[str, list[Envelope]] = {}
        # `record` is a read-modify-write over `_history`, and FastAPI runs synchronous route
        # handlers in the anyio threadpool, so two writers really are concurrent.
        self._lock = threading.Lock()
        self._restore(self._repository.load())

    def _restore(self, loaded: Mapping[str, Any]) -> None:
        """Seed the in-memory history from the backing store, re-checking every invariant."""
        for store_id, versions in loaded.items():
            for stored in versions:
                candidate = restorable(Envelope.from_obj(stored))
                _refuse_unapproved_activation(candidate)
                history = self._history.setdefault(candidate.store_id, [])
                if history and candidate.version < history[-1].version:
                    raise VersionWentBackwards(
                        f"the backing store returned store {store_id!r} envelope "
                        f"v{candidate.version} after v{history[-1].version}; a history that "
                        "goes backwards on disk is not a history"
                    )
                history.append(candidate)

    @property
    def repository(self) -> EnvelopeRepository:
        """The backing store this history persists to and was loaded from."""
        return self._repository

    def record(self, envelope: Any) -> Envelope:
        """Append one envelope state to its store's history and return what was stored.

        Args:
            envelope: the version to file.

        Returns:
            The validated envelope now at the head of that store's history.

        Raises:
            VersionWentBackwards: the store already has a *higher* version on file.
            EnvelopeInvalid: ``envelope`` is not a DESIGN Envelope.
            ApprovalRejected: the submitted version calls itself ``active`` without a written
                approval artifact bound to its own terms (T-248).
        """
        candidate = Envelope.from_obj(envelope)
        _refuse_unapproved_activation(candidate)
        with self._lock:
            # `get`, not `setdefault`: this store may still be refused below, and a
            # `setdefault` would have already created an empty history for it — enough to make
            # `stores()` report a store whose only version never landed.
            history = self._history.get(candidate.store_id)
            if history and candidate.version < history[-1].version:
                raise VersionWentBackwards(
                    f"store {candidate.store_id!r} is on envelope v{history[-1].version}; "
                    f"v{candidate.version} is older and would reinstate replaced limits"
                )
            # Durable first, in-memory second, both inside the lock. A failed write leaves
            # this process's history unchanged rather than one version ahead of the backing
            # store — believing you filed v4 while the table stops at v3 is exactly the stale
            # writer the version rule exists to refuse.
            self._repository.persist(candidate)
            self._history.setdefault(candidate.store_id, []).append(candidate)
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
            # A store's first envelope starts in shadow whatever the body said — nothing has
            # been approved yet — and at FIRST_VERSION whatever the body said.
            #
            # The version used to be carried through from the submitted document, which
            # contradicted this method's own contract ("The submitted ``version`` is ignored —
            # it is derived from what is already on file", onboarding/routes.py) for the one
            # case where nothing is on file. `contracts.Envelope` puts no lower bound on the
            # field, so a client could open a store at v0 — which `sealed.envelopes`
            # `envelopes_version_positive CHECK (version >= 1)` then rejects, turning a
            # request body into a 500 — or at v9999, permanently poisoning the monotonic rule
            # for that store because nothing may ever go backwards from it again.
            first = Envelope.from_obj(
                {**submitted.to_dict(), "version": FIRST_VERSION}, approval=None
            )
            return self.record(first.with_activation(SHADOW, None))
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

    def revive(self, store_id: str) -> Envelope:
        """Lift the kill on the store's current envelope and file it back in ``shadow``.

        The store is un-stopped and still not bidding: ``is_live`` answers ``False`` for a
        revived version exactly as it does for an edited one, and only
        :meth:`activate` against a written approval changes that. Reviving is therefore safe
        to reach for — the worst it can do is put a stopped store back in the queue for its
        owner's signature.

        Filed like every other transition, at the SAME version and through :meth:`record`, so
        the history reads ``shadow → active → killed → shadow`` and a merchant can see that the
        stop happened and that it was lifted. Nothing is rewritten and nothing disappears.

        Raises:
            UnknownStore: nothing has been recorded for ``store_id``.
            ReviveRefused: the store's current envelope is not killed.
        """
        return self.record(revive_envelope(self.current(store_id)))


#: The service-wide history. Backed by an
#: :class:`~merchant_svc.envelope.repository.InMemoryEnvelopeRepository` because the merchant
#: service is deployed with no ``PROXYSHOP_PG_DSN_*`` at all (``apps/merchant/compose.yaml``
#: says so, and deliberately), so this default is what a booted service really gets: a restart
#: still forgets it, and losing it can only *stop* a store bidding, never start one. What has
#: changed is that the forgetting is now a **choice of repository** rather than a property of
#: the class — hand ``EnvelopeVersions`` a
#: :class:`~merchant_svc.envelope.repository.PostgresEnvelopeRepository` over a connection
#: authorized for ``sealed.*`` and the same three invariants hold across the boundary.
ENVELOPES = EnvelopeVersions()
