"""The versioned seller-eligibility port (R12, D54).

One question, asked at three gates: *may this store participate?* The answer is an
:class:`EligibilityDecision` carrying one of three statuses, and the rule for reading it is
**fail closed** — three ways, not one:

* :data:`BLACKLISTED` denies;
* :data:`UNAVAILABLE` denies;
* a ``check`` that **raises** denies exactly like :data:`UNAVAILABLE`.

The third is the one that matters in production and the one a naive implementation gets
wrong: an eligibility backend that is down must not quietly admit every store. So the
reading of the port lives here, in :func:`read_eligibility`, rather than being re-derived at
each gate — two gates that disagree about what an exception means are two different
policies wearing one name.

Who owns what
-------------
* **T-030** (this module) owns the port, its statuses, its decision record and the
  fail-closed reader; and the solicitation gate in
  :mod:`apps.exchange.src.orchestration`.
* **T-033** adds the checkout gate to that same orchestration package.
* **T-032** owns the ranking gate inside ``rank()``.
* **T-062 / T-064** own the live blacklist lookup *behind* this port. Until then
  :class:`StaticSellerEligibility` is the deterministic double, and its default for a store
  it has never heard of is :data:`UNAVAILABLE` — fail closed, not fail open.

Versioning
----------
The port publishes :data:`SELLER_ELIGIBILITY_INTERFACE_VERSION`, and an implementation
declares the version it speaks in the ``interface_version`` class attribute. A source
speaking another version is *refused* at the gates rather than trusted: the meaning of a
status is part of the interface, so consulting a source whose vocabulary you do not know is
indistinguishable from not consulting one at all. :func:`speaks_supported_interface` is the
single place that comparison happens.

Import identity note
--------------------
Every module in this package reaches its siblings by **relative** import. This file is
reachable under two module names — ``apps.exchange.src.eligibility`` (repo-root path, which
is how the frozen acceptance suite imports it) and ``exchange.eligibility`` (through the
tracked ``.pkgroot/exchange`` symlink, which is how sibling members import it). A relative
import resolves inside whichever identity loaded the caller, so the gate and the port always
agree on which classes and constants they are talking about. For the same reason the three
statuses are plain ``str`` constants and not enum members: a string compares equal across
both identities, an enum member does not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "BLACKLISTED",
    "ELIGIBLE",
    "ELIGIBILITY_STATUSES",
    "SELLER_ELIGIBILITY_INTERFACE_VERSION",
    "UNAVAILABLE",
    "EligibilityDecision",
    "SellerEligibility",
    "StaticSellerEligibility",
    "read_eligibility",
    "speaks_supported_interface",
]

#: The interface version this exchange speaks. Published, not guessed: a source declaring
#: anything else is refused at the boundary gates.
SELLER_ELIGIBILITY_INTERFACE_VERSION = "seller-eligibility/1.0.0"

#: The store may participate.
ELIGIBLE = "eligible"
#: The store is blacklisted (R12) and may not participate.
BLACKLISTED = "blacklisted"
#: Eligibility could not be established. Fail closed — this denies.
UNAVAILABLE = "unavailable"

#: The three statuses, in the order a reader should think about them.
ELIGIBILITY_STATUSES: tuple[str, str, str] = (ELIGIBLE, BLACKLISTED, UNAVAILABLE)


@dataclass(frozen=True)
class EligibilityDecision:
    """One store's answer, plus the reason a gate records when it denies."""

    store_id: str
    status: str
    reason: str = ""

    @property
    def eligible(self) -> bool:
        return self.status == ELIGIBLE


class SellerEligibility:
    """The port. Implementations answer :meth:`check` for one store at a time.

    Subclasses declare the version they speak by overriding the ``interface_version`` class
    attribute; it is a plain class attribute (never a read-only property) precisely so an
    implementation can.
    """

    #: The interface version this implementation speaks. A plain class attribute on purpose
    #: — never `ClassVar` and never a property — so a subclass may set it on the class *or*
    #: shadow it per instance (a source that talks to two backends at two versions).
    interface_version: str = SELLER_ELIGIBILITY_INTERFACE_VERSION

    def check(self, store_id: str) -> EligibilityDecision:
        """Return this store's eligibility. May raise; callers must fail closed."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement check(store_id) -> EligibilityDecision"
        )


class StaticSellerEligibility(SellerEligibility):
    """The deterministic double (T-062/T-064 own the live lookup).

    ``default`` is :data:`UNAVAILABLE` on purpose: a store this source has never heard of is
    a store whose eligibility could not be established, which denies. Passing
    ``default=ELIGIBLE`` is possible but is an explicit, visible decision to fail open.
    """

    def __init__(
        self,
        statuses: Mapping[str, str] | None = None,
        *,
        default: str = UNAVAILABLE,
        version: str = SELLER_ELIGIBILITY_INTERFACE_VERSION,
    ) -> None:
        self._statuses = dict(statuses or {})
        self._default = default
        # Instance attribute deliberately shadows the class attribute so a test can build a
        # source that speaks a version this exchange does not.
        self.interface_version = version

    def check(self, store_id: str) -> EligibilityDecision:
        status = self._statuses.get(store_id, self._default)
        return EligibilityDecision(
            store_id=store_id,
            status=status,
            reason=f"static-eligibility: {store_id} is {status}",
        )


def speaks_supported_interface(source: object) -> bool:
    """True when ``source`` declares the interface version this exchange speaks.

    A source with no ``interface_version`` at all is *not* supported: an unversioned answer
    is an answer whose vocabulary is unknown.
    """
    declared = getattr(source, "interface_version", None)
    return isinstance(declared, str) and declared == SELLER_ELIGIBILITY_INTERFACE_VERSION


def read_eligibility(source: object, store_id: str) -> EligibilityDecision:
    """Read one store's eligibility, failing closed on every unhappy path.

    Returns a decision in every case. The three denials are collapsed here so no gate can
    disagree with another about what a raised exception, a missing decision or an
    unrecognised status means:

    * the read raises            -> :data:`UNAVAILABLE`, reason naming the failure;
    * the read returns nothing   -> :data:`UNAVAILABLE`;
    * the status is unrecognised -> :data:`UNAVAILABLE` (never "probably fine");
    * otherwise                  -> the source's own decision, with a reason guaranteed
      non-empty so a caller recording it always has something to record.
    """
    try:
        decision = source.check(store_id)  # type: ignore[attr-defined]
    except Exception as exc:  # a blanket catch IS the fail-closed rule, not a shortcut
        return EligibilityDecision(
            store_id=store_id,
            status=UNAVAILABLE,
            reason=(
                f"unavailable: eligibility read for {store_id} failed "
                f"({type(exc).__name__}: {exc}); failing closed"
            ),
        )

    if decision is None:
        return EligibilityDecision(
            store_id=store_id,
            status=UNAVAILABLE,
            reason=f"unavailable: eligibility source returned no decision for {store_id}",
        )

    status = getattr(decision, "status", None)
    reason = getattr(decision, "reason", "") or ""
    if status not in ELIGIBILITY_STATUSES:
        return EligibilityDecision(
            store_id=store_id,
            status=UNAVAILABLE,
            reason=(
                f"unavailable: eligibility source answered unrecognised status {status!r} "
                f"for {store_id}; failing closed"
            ),
        )
    if not reason:
        reason = f"{status}: eligibility source reports {store_id} is {status}"
    return EligibilityDecision(store_id=store_id, status=str(status), reason=str(reason))
