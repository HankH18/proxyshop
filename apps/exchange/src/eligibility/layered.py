"""Two R12 sources, one answer: the platform's stated rows, then the trust service (T-303 b).

WHY THIS EXISTS, and it is a deployment shape rather than a preference
---------------------------------------------------------------------
A deployment document states its sellers for three independent reasons: where each store's
agent answers (``bid_endpoint``), which host it checks out on (``registered_domain``), and
whether the platform itself will have it (``eligibility``). Only the third is R12. But
:func:`exchange.composition.bind_eligibility` took the presence of ``sellers`` as the answer
to all three, so the ONLY deployment shape that consulted the trust service was one that
named no sellers at all — which is also a deployment that solicits nobody, ranks nothing and
shortlists nobody. Measured on the served exchange before this landed: a document with
``sellers`` produced ``static-eligibility: store-brightbean is eligible`` for a store the
trust engine scored at 0.10 against a published 0.35 threshold.

So a seller row may now say ``"eligibility": "trust"``, which is the platform declining to
answer for that store and handing the question to the trust service. This class is what makes
one source out of the two halves. It is transport-free like everything else in this package —
the trust half arrives already built, as a :class:`~.SellerEligibility`.

THE THREE STATES ARE PRESERVED, NOT COLLAPSED
---------------------------------------------
For a store the document states a word for, the answer is that word, through
:class:`~.StaticSellerEligibility` so the vocabulary and the reason string are the port's own.

For a store the document leaves to trust — and for any store the document does not mention at
all — the answer is the trust source's, read through :func:`~.read_eligibility`, so:

* trust **unreachable**                 -> ``UNAVAILABLE`` (denies), reason names the service;
* trust reachable, **no row**           -> ``UNAVAILABLE`` (denies), reason says so;
* ``blacklisted`` **is not a bool**     -> ``UNAVAILABLE`` (denies), unreadable is not "fine";
* trust says **delisted**               -> ``BLACKLISTED`` (denies);
* trust says otherwise                  -> ``ELIGIBLE``.

An unmentioned store falling through to trust is a strengthening, not a loosening: it
previously answered ``static-eligibility: <store> is unavailable`` — a denial from a source
that had never been asked anything — and now it answers with whatever trust actually says,
which denies in exactly the same four of five cases.

INTERFACE VERSION
-----------------
The gates check :func:`~.speaks_supported_interface` on the source they are handed, which is
THIS object. A composite that passed that check while holding a trust source speaking another
version would launder an unreadable vocabulary past the one place that looks, so the deferred
source's version is checked here, per read, and a mismatch is ``UNAVAILABLE`` naming the
version rather than a decision.
"""

from __future__ import annotations

from collections.abc import Mapping

from . import (
    UNAVAILABLE,
    EligibilityDecision,
    SellerEligibility,
    StaticSellerEligibility,
    read_eligibility,
    speaks_supported_interface,
)

__all__ = ["LayeredSellerEligibility"]


class LayeredSellerEligibility(SellerEligibility):
    """The platform's stated rows, deferring to ``deferred`` for every store they do not cover.

    ``stated`` is ``{store_id: status}`` — exactly
    :attr:`exchange.composition.Deployment.eligibility_rows`, which now omits the rows that
    say ``"trust"``. ``deferred`` is the source those rows, and every store the document never
    mentions, are asked of.
    """

    def __init__(
        self,
        stated: Mapping[str, str],
        deferred: SellerEligibility,
        *,
        source: str = "the deployment's seller registry",
    ) -> None:
        self._stated = dict(stated)
        self._static = StaticSellerEligibility(self._stated)
        self._deferred = deferred
        self._source = str(source)

    @property
    def source(self) -> str:
        """The phrase the stated half names itself by."""
        return self._source

    def defers(self, store_id: str) -> bool:
        """Whether this store's answer comes from the deferred source rather than a stated row."""
        return store_id not in self._stated

    def check(self, store_id: str) -> EligibilityDecision:
        if not self.defers(store_id):
            return self._static.check(store_id)
        if not speaks_supported_interface(self._deferred):
            declared = getattr(self._deferred, "interface_version", None)
            return EligibilityDecision(
                store_id=store_id,
                status=UNAVAILABLE,
                reason=(
                    f"{UNAVAILABLE}: {self._source} leaves {store_id!r} to an eligibility "
                    f"source declaring interface version {declared!r}, which this exchange "
                    f"does not speak; failing closed (R12)"
                ),
            )
        # `read_eligibility`, never `self._deferred.check`: a deferred source that raises,
        # answers nothing or answers a status this exchange does not recognise must deny here
        # exactly as it would at a gate, and that reading lives in one place.
        return read_eligibility(self._deferred, store_id)
