"""Single use, enforced — and a second redemption recorded rather than raised.

A5: "duplicate use is an offer-integrity event, not an exception." That is not politeness
about error handling, it is a statement about what a redemption race *is*. Two carts can be
opened with the same ``usageLimit: 1`` code before either completes; the stub reproduces
exactly that (``services/shopify-stub/src/orders.py`` re-validates the code at payment and
lets the second order through at full price, deliberately). So a second redemption is a
**normal, expected condition of a distributed system**, and a webhook handler that raises on
one turns an observation the trust ledger wants into a 500 and a retry storm.

Everything here therefore returns a :class:`RedemptionOutcome`. Nothing raises.

**The check-and-set is one critical section, and that is the whole guarantee.** The repo
already contains the counterexample this must not copy:
``packages/store-agent/src/external/nonces.py`` has a measured non-atomic check-then-set, so
two concurrent callers can both pass the check before either writes. "This code has not been
used" followed by "mark it used" as two statements is the same bug wearing this ticket's
name: both racers would be told they were first, and a single-use discount would be honoured
twice with every assertion green. :meth:`RedemptionRegister.redeem` holds one lock across
the read, the decision, the state change **and** the integrity event, so a redemption can
never be recorded without its event or an event emitted without its state change.

**Codes are keyed canonically.** Shopify matches discount codes case-insensitively at the
till, so ``psx-7qk2zb0m`` off a webhook and ``PSX-7QK2ZB0M`` as minted are one redeemable
and a register keyed by the raw string would make "single use" hold per spelling. See
:func:`~merchant_svc.codes.mint.canonical_code` for the folding, which cannot merge two
minted codes.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from contracts.protocol import LedgerEvent

from .ledger import CODE_LEDGER, CodeLedger, build_event
from .mint import canonical_code

__all__ = [
    "REDEEMED",
    "REDEMPTIONS",
    "REFUSED",
    "UNRECOGNISED",
    "IssuedCode",
    "Redemption",
    "RedemptionOutcome",
    "RedemptionRegister",
    "on_redemption",
]

#: The three outcome statuses. Plain strings rather than an enum so the record serialises
#: to JSON without a ``mode=`` dance, and worded so that a *clean* redemption's record
#: carries no word ("duplicate", "reuse", "already") that reads as an integrity violation
#: to something scanning it.
REDEEMED = "redeemed"
REFUSED = "refused"
UNRECOGNISED = "not_ours"


@dataclass(frozen=True)
class IssuedCode:
    """One code this service minted, and the promise it carries.

    Attributes:
        code: the code as minted, uppercase, for display and for the permalink.
        key: the canonical form the register is keyed by.
        store_id: the shop it was minted on.
        usage_limit: how many redemptions the code was created with. D22 says 1; the field
            exists so the check is against what was *promised*, not against a constant.
        starts_at, expires_at: the validity window, as instants.
        offer_id, bid_ref: what it was minted for, so an integrity event can name it.
        permalink_url: where the buyer was sent.
    """

    code: str
    key: str
    store_id: str
    usage_limit: int = 1
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    offer_id: str | None = None
    bid_ref: str | None = None
    permalink_url: str = ""


@dataclass(frozen=True)
class Redemption:
    """One accepted redemption of one code."""

    key: str
    order_ref: str | None
    at: datetime


@dataclass(frozen=True)
class RedemptionOutcome:
    """What happened when a code was offered, and the event it produced, if any.

    ``event`` is ``None`` for a clean first redemption and for a code this service never
    minted. It carries a :class:`~contracts.protocol.LedgerEvent` — kind
    ``offer_integrity`` — exactly when something did not match what the offer promised.
    """

    code: str
    status: str
    order_ref: str | None = None
    store_id: str | None = None
    redemption_count: int = 0
    event: LedgerEvent | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """A JSON-able record, with the event rendered as its published body."""
        return {
            "code": self.code,
            "status": self.status,
            "order_ref": self.order_ref,
            "store_id": self.store_id,
            "redemption_count": self.redemption_count,
            "detail": self.detail,
            "event": None if self.event is None else self.event.model_dump(mode="json"),
        }


def _lookup(source: Any, *names: str) -> Any:
    """The first of ``names`` actually present on ``source``, else ``None``.

    Membership, never ``.get(name, default)``: a tolerant test double answers ``get`` for
    every key, so asking one politely always succeeds and would invent an order id out of
    nothing.
    """
    for name in names:
        if isinstance(source, Mapping):
            if name in source:
                return source[name]
        else:
            value = getattr(source, name, None)
            if value is not None:
                return value
    return None


def _order_ref(order: Any) -> str | None:
    """The order's identifier, in the spellings a Shopify order actually arrives under."""
    if order is None:
        return None
    value = _lookup(order, "admin_graphql_api_id", "id", "order_ref", "order_id", "orderId", "name")
    if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
        return str(value).strip()
    return None


def _declared_codes(order: Any) -> tuple[str, ...] | None:
    """The codes the order says it applied, canonicalised — or ``None`` when it says none.

    ``None`` and ``()`` are different answers and the difference matters: an order that
    lists no ``discountCodes`` field has not told us anything, while an order that lists an
    empty one has told us the discount was **not** honoured.
    """
    if order is None:
        return None
    raw = _lookup(order, "discountCodes", "discount_codes")
    if raw is None or isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Sequence):
        return None
    found: list[str] = []
    for entry in raw:
        value = entry.get("code") if isinstance(entry, Mapping) else entry
        if isinstance(value, str):
            found.append(canonical_code(value))
    return tuple(found)


class RedemptionRegister:
    """Every code this service minted, and every redemption offered against one."""

    def __init__(self, ledger: CodeLedger | None = None) -> None:
        self._lock = threading.Lock()
        self._issued: dict[str, IssuedCode] = {}
        self._accepted: dict[str, list[Redemption]] = {}
        self._refused: dict[str, list[Redemption]] = {}
        self._ledger = ledger if ledger is not None else CODE_LEDGER

    def issue(self, issued: IssuedCode) -> IssuedCode:
        """File a freshly minted code, so a redemption of it can be judged.

        A code minted twice under one key is refused rather than overwritten: the second
        mint would silently reset the first one's usage count, which is the same
        single-use hole this class exists to close.
        """
        with self._lock:
            existing = self._issued.get(issued.key)
            if existing is not None and existing.code != issued.code:
                raise ValueError(
                    f"code {issued.code!r} canonicalises onto the already-issued "
                    f"{existing.code!r}; refusing to overwrite a live redeemable"
                )
            self._issued.setdefault(issued.key, issued)
            self._accepted.setdefault(issued.key, [])
            self._refused.setdefault(issued.key, [])
        return issued

    def issued(self, code: str) -> IssuedCode | None:
        """The record for a code, or ``None`` when this service did not mint it."""
        with self._lock:
            return self._issued.get(canonical_code(code))

    def redemptions(self, code: str) -> tuple[Redemption, ...]:
        """The accepted redemptions of one code, oldest first."""
        with self._lock:
            return tuple(self._accepted.get(canonical_code(code), ()))

    def refusals(self, code: str) -> tuple[Redemption, ...]:
        """The refused redemption attempts against one code, oldest first."""
        with self._lock:
            return tuple(self._refused.get(canonical_code(code), ()))

    def clear(self) -> None:
        """Forget every issued code. For tests only."""
        with self._lock:
            self._issued.clear()
            self._accepted.clear()
            self._refused.clear()

    def redeem(
        self, code: str, order: Any = None, *, now: datetime | None = None
    ) -> RedemptionOutcome:
        """Judge one redemption. **Never raises.**

        The whole decision — read the record, compare it against what was promised, write
        the state change, build and emit the integrity event — happens inside one lock, so
        two concurrent redemptions of one code cannot both be told they were first, and no
        outcome can exist without the event that explains it.

        Returns:
            A :class:`RedemptionOutcome`. ``status`` is ``redeemed`` when the redemption
            was accepted (including a re-delivery of a webhook for an order already
            recorded), ``refused`` when the code could not honour it — and that outcome
            carries the ``offer_integrity`` event — and ``not_ours`` for a code this
            service never minted.
        """
        moment = now or datetime.now(UTC)
        key = canonical_code(code)
        order_ref = _order_ref(order)

        with self._lock:
            record = self._issued.get(key)
            if record is None:
                # Not every code on a shop is ours: a merchant's own campaign codes come
                # through the same webhook. Silence is the honest answer — there is no
                # promise of ours to compare this against, so there is nothing to flag.
                return RedemptionOutcome(
                    code=str(code),
                    status=UNRECOGNISED,
                    order_ref=order_ref,
                    detail="this service did not mint that code",
                )

            accepted = self._accepted.setdefault(key, [])
            declared = _declared_codes(order)

            if declared is not None and key not in declared:
                return self._refuse(
                    record,
                    order_ref,
                    moment,
                    field_name="discount_code",
                    promised=record.code,
                    observed=list(declared),
                    detail="the order does not carry the code it was reported against",
                )

            for prior in accepted:
                if order_ref is not None and prior.order_ref == order_ref:
                    # A re-delivered webhook for an order already on file. Shopify retries;
                    # counting a retry as a second use would manufacture the very integrity
                    # event this method exists to make meaningful.
                    return RedemptionOutcome(
                        code=record.code,
                        status=REDEEMED,
                        order_ref=order_ref,
                        store_id=record.store_id,
                        redemption_count=len(accepted),
                        detail="a repeat delivery for an order on file",
                    )

            if len(accepted) >= record.usage_limit:
                return self._refuse(
                    record,
                    order_ref,
                    moment,
                    field_name="usage_limit",
                    promised=record.usage_limit,
                    observed=len(accepted) + 1,
                    detail="a second order presented a code created for one",
                )

            if record.expires_at is not None and moment >= record.expires_at:
                # The window is half-open: valid while `now < expires_at`. A redemption AT
                # the expiry instant is outside it, which is the reading that makes
                # "expires at T" mean the same thing as every other deadline in the system.
                return self._refuse(
                    record,
                    order_ref,
                    moment,
                    field_name="expires_at",
                    promised=record.expires_at.isoformat(),
                    observed=moment.isoformat(),
                    detail="the code was presented after its validity window closed",
                )

            accepted.append(Redemption(key=key, order_ref=order_ref, at=moment))
            return RedemptionOutcome(
                code=record.code,
                status=REDEEMED,
                order_ref=order_ref,
                store_id=record.store_id,
                redemption_count=len(accepted),
                detail="the first and only redemption of this code",
            )

    def _refuse(
        self,
        record: IssuedCode,
        order_ref: str | None,
        moment: datetime,
        *,
        field_name: str,
        promised: Any,
        observed: Any,
        detail: str,
    ) -> RedemptionOutcome:
        """Record a refused attempt and its event, inside the caller's critical section.

        Private, and it takes the lock as a *precondition* rather than acquiring one: the
        state change and the event must land in the same critical section, and a method
        that could be called from outside it would be a way to get one without the other.
        """
        event = build_event(
            "offer_integrity",
            store_id=record.store_id,
            order_ref=order_ref,
            ts=moment,
            payload={
                # The published `offer_integrity` body (D24).
                "bid_ref": record.bid_ref or record.offer_id or record.code,
                "field": field_name,
                "promised": promised,
                "observed": observed,
                # ...and what makes it actionable rather than merely recorded.
                "code": record.code,
                "offer_id": record.offer_id,
                "order_ref": order_ref,
                "reason": detail,
            },
        )
        self._refused.setdefault(record.key, []).append(
            Redemption(key=record.key, order_ref=order_ref, at=moment)
        )
        self._ledger.emit(event)
        return RedemptionOutcome(
            code=record.code,
            status=REFUSED,
            order_ref=order_ref,
            store_id=record.store_id,
            redemption_count=len(self._accepted.get(record.key, [])),
            event=event,
            detail=detail,
        )


#: The service-wide register. Process-local, and the single-use guarantee lives in it — see
#: this package's ``_spellings`` for why there must be exactly one of it per process.
REDEMPTIONS = RedemptionRegister()


def on_redemption(
    code: str, order: Any = None, *, now: datetime | None = None
) -> RedemptionOutcome:
    """Judge one redemption of one code against the service-wide register.

    This is the entry point an ``orders/paid`` webhook handler calls. It **never raises**:
    a duplicate redemption comes back as a :class:`RedemptionOutcome` carrying an
    ``offer_integrity`` :class:`~contracts.protocol.LedgerEvent`, because a redemption race
    is an expected condition of a distributed system and not a crash (A5).

    Args:
        code: the discount code the order presented, in any case.
        order: the order body, as the webhook delivered it. ``id``/``admin_graphql_api_id``
            names the order; ``discountCodes`` — when present — says which codes it applied.
        now: the instant to judge against. Injected by tests; production reads the clock.
    """
    return REDEMPTIONS.redeem(code, order, now=now)
