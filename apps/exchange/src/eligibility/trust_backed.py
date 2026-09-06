"""The trust-backed :class:`~.SellerEligibility` source (T-303 b).

Until this module existed there was no trust-backed implementation of the R12 port anywhere
in the tree — not an unwired one, *none*. The package shipped the port, the fail-closed
reader and :class:`~.StaticSellerEligibility`, whose rows are typed into a deployment
document by a human. So the served exchange could not tell an honest store from a delisted
one, because it was not asking anybody: it answered ``unavailable`` for every store alive and
the trust engine — the product's whole differentiator — had no reader on the auction path.

WHAT IT READS
-------------
The mapping trust publishes at ``GET /snapshot``: ``{store_id: TrustSnapshot}``. That is the
same document ``exchange.ranking.filters`` already reads as ``trust_snapshot``, deliberately
— one snapshot answering both R12 questions means the eligibility gate and the ranking's
blacklist filter cannot end up with two opinions about which stores a delisting covers. Both
published shapes are accepted (:func:`snapshot_rows`): the flat mapping the route serves, and
``build_snapshot``'s ``{"version": …, "stores": {…}}`` envelope.

The source is a **seam**, never a transport: hand it a mapping already in hand (a deployment
document's ``trust_snapshot``) or a zero-argument callable that fetches one (the composition
root's :class:`~exchange.composition.HttpTrustSnapshot`). Nothing in this package opens a
socket; ``apps/exchange/src/eligibility`` is the port's home and a port that imported
``httpx`` would make constructing an eligibility source a networking decision.

THREE STATES, AND WHY THEY ARE NOT ONE
--------------------------------------
The whole defect this closes lives in the difference between them, so they are spelled out
rather than collapsed:

=================================  ===============  ==================================
the snapshot **could not be read**  ``UNAVAILABLE``  the exchange knows nothing about
(unreachable, refused, unparseable                   *any* store, and knowing nothing
or not a snapshot at all)                            must still mean refusing
the snapshot **holds no row** for   ``UNAVAILABLE``  trust answered and has no opinion
this store                                           on this store. Published as the
                                                     contract's own rule: "A store with
                                                     no row here is treated as an
                                                     UNAVAILABLE eligibility read and
                                                     denied (R12, fail-closed) — never
                                                     admitted on the grounds that no
                                                     blacklist entry was found."
the row's ``blacklisted`` is not    ``UNAVAILABLE``  ``0`` / ``"false"`` / a missing key
a real ``bool``                                      are unreadable, not "probably fine"
the row says ``blacklisted: true``  ``BLACKLISTED``  trust has delisted this store
otherwise                           ``ELIGIBLE``     trust answered, and holds nothing
                                                     against this store
=================================  ===============  ==================================

Only the LAST line admits, and it is reached only when a snapshot was actually read and that
snapshot actually carries this store. "Trust-backed" therefore never means "admits when trust
is unreachable": an unreachable trust service denies exactly as loudly as a delisting does,
which is what lets this be the DEFAULT source for an exchange nobody has configured without
weakening the invariant :mod:`exchange.composition` is built on.

The first four rows agree, deliberately, with
:func:`exchange.ranking.filters.blacklist_reason` — the exchange's own published reading of
the same document at the ranking gate. They are not shared code because the two gates answer
in different vocabularies (a ranking *exclusion reason* against an
:class:`~.EligibilityDecision`), and because ``ranking`` imports this package: a shared
helper would have to live in one of them and the dependency runs this way.

REASON VOCABULARY
-----------------
Every reason this module writes begins ``"<status>: trust-eligibility: …"``. The status word
is first because callers grep it (``test_an_unwired_exchange_denies_every_store_…`` asserts
``"unavailable" in reason.lower()``), and :data:`TRUST_ELIGIBILITY_SOURCE` follows it so a
denial says *which source refused* — the thing that was impossible to tell when every denial
in the tree read ``static-eligibility``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .. import describe, describe_exception
from . import BLACKLISTED, ELIGIBLE, UNAVAILABLE, EligibilityDecision, SellerEligibility

__all__ = [
    "TRUST_ELIGIBILITY_SOURCE",
    "TrustBackedSellerEligibility",
    "TrustSnapshotUnavailable",
    "snapshot_rows",
]

#: The word every reason this module writes names itself by. Deliberately NOT
#: ``static-eligibility``: a denial has to say which source refused, and the two sources
#: refuse for entirely different reasons — one has no rows because nobody typed any, the
#: other has no row because trust has never heard of the store.
TRUST_ELIGIBILITY_SOURCE = "trust-eligibility"


class TrustSnapshotUnavailable(RuntimeError):
    """A snapshot read that did not produce a snapshot.

    Raised by a reader — never caught and turned into "no store is blacklisted". The only
    thing it can mean at a gate is :data:`~.UNAVAILABLE`.
    """


def snapshot_rows(document: Any) -> Mapping[str, Any] | None:
    """The flat ``{store_id: row}`` mapping out of either published shape, or ``None``.

    ``GET /snapshot`` serves the flat mapping; ``trust.snapshot.build_snapshot`` returns the
    envelope ``{version, score_version, dimensions, as_of, stores, delistings}``. Handing a
    reader the envelope where it expects the mapping denies **every** store, because the only
    keys it finds are ``version`` and friends — so both are unwrapped here, once.

    ``None`` for anything that is not a mapping at all, which is how a reader that answered
    ``null``, a list or an error page becomes an UNAVAILABLE read rather than an exception
    escaping a gate.

    :func:`exchange.composition._trust_snapshot` performs the same unwrap for the *deployment
    document*, and deliberately keeps its own copy: it must name the offending key in a
    :class:`~exchange.composition.DeploymentConfigurationError` an operator reads at boot,
    while this one is on a request path and may only ever answer "not a snapshot".
    """
    if not isinstance(document, Mapping):
        return None
    stores = document.get("stores")
    if isinstance(stores, Mapping):
        return stores
    return document


class TrustBackedSellerEligibility(SellerEligibility):
    """R12, answered out of the trust service's own snapshot.

    ``snapshot`` is either the mapping itself or a zero-argument callable returning one. A
    callable is re-read on every :meth:`check`, so a source that caches decides its own
    freshness policy and this class never serves a snapshot the reader considers stale — see
    :class:`~exchange.composition.HttpTrustSnapshot`, which caches on the version trust
    publishes and fails closed rather than serving a snapshot it could not revalidate.

    ``source`` is the phrase every reason names, e.g. ``"the trust snapshot at
    http://trust:8084/snapshot"``. It is a caller-supplied *description of configuration*,
    never anything from a request, so nothing store-controlled reaches a persisted reason.
    """

    def __init__(
        self,
        snapshot: Mapping[str, Any] | Callable[[], Any],
        *,
        source: str = "the trust snapshot",
    ) -> None:
        self._snapshot = snapshot
        self._source = str(source)

    @property
    def source(self) -> str:
        """The phrase this source names itself by in a denial reason."""
        return self._source

    def _rows(self) -> Mapping[str, Any]:
        reader = self._snapshot
        document = reader() if callable(reader) else reader
        rows = snapshot_rows(document)
        if rows is None:
            raise TrustSnapshotUnavailable(
                # `describe`, not `{document!r}`: whatever the reader returned is republished
                # as a denial reason on an unauthenticated 409 and persisted in a
                # `policy_event`, and an object's default `repr` carries its address (T-264).
                f"answered {describe(document)}, which is not a trust snapshot"
            )
        return rows

    def check(self, store_id: str) -> EligibilityDecision:
        """This store's eligibility. Denies four ways and admits one; see the module header."""
        try:
            rows = self._rows()
        except Exception as exc:
            # A blanket catch IS the fail-closed rule, exactly as it is in `read_eligibility`.
            # It is caught HERE as well so the denial names the trust snapshot and its failure
            # rather than arriving as the port reader's generic "the read raised" — an
            # operator reading a denial should be able to tell an unreachable trust service
            # from a store trust has delisted without correlating two logs.
            return EligibilityDecision(
                store_id=store_id,
                status=UNAVAILABLE,
                reason=(
                    f"{UNAVAILABLE}: {TRUST_ELIGIBILITY_SOURCE}: {self._source} could not be "
                    f"read ({describe_exception(exc)}); failing closed (R12)"
                ),
            )

        row = rows.get(store_id)
        if row is None:
            return EligibilityDecision(
                store_id=store_id,
                status=UNAVAILABLE,
                reason=(
                    f"{UNAVAILABLE}: {TRUST_ELIGIBILITY_SOURCE}: {self._source} holds no row "
                    f"for {store_id!r}, so its eligibility could not be established; failing "
                    f"closed (R12)"
                ),
            )

        flag = (
            row.get("blacklisted")
            if isinstance(row, Mapping)
            else getattr(row, "blacklisted", None)
        )
        if not isinstance(flag, bool):
            return EligibilityDecision(
                store_id=store_id,
                status=UNAVAILABLE,
                reason=(
                    f"{UNAVAILABLE}: {TRUST_ELIGIBILITY_SOURCE}: {self._source} states "
                    f"blacklisted={describe(flag)} for {store_id!r}, which is not a readable "
                    f"yes/no; failing closed (R12)"
                ),
            )
        if flag:
            return EligibilityDecision(
                store_id=store_id,
                status=BLACKLISTED,
                reason=(
                    f"{BLACKLISTED}: {TRUST_ELIGIBILITY_SOURCE}: {store_id!r} is blacklisted "
                    f"in {self._source} and may not participate (R12)"
                ),
            )
        return EligibilityDecision(
            store_id=store_id,
            status=ELIGIBLE,
            reason=(
                f"{ELIGIBLE}: {TRUST_ELIGIBILITY_SOURCE}: {store_id!r} is not blacklisted in "
                f"{self._source}"
            ),
        )
