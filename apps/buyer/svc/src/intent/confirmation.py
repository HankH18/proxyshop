"""The one door between a clarified intent and a live auction (T-071, SPEC R1).

R1 says *no auction exists before the buyer confirms*. That is a statement about a side
effect, so it is enforced where the side effect is, and nowhere else in this package can
reach it:

* :mod:`buyer_svc.intent.clarifier` and :mod:`buyer_svc.intent.extraction` never receive
  an auction client. Not "do not use one" — there is no parameter to pass one through, so
  no configuration, no route handler and no future caller can wire one in by accident.
* :func:`confirm` is the only function in the package that calls anything on an auction
  client, and its **first statement** is the confirmation check. The client is not looked
  up, not probed, not `hasattr`-ed and not touched until ``confirmed is True`` has held.

Why ``confirmed is True`` and not ``if confirmed:``
--------------------------------------------------
Truthiness is the hole. ``confirmed="false"``, ``confirmed="no"`` and ``confirmed=[]`` all
arrive from JSON bodies, query strings and half-typed clients, and two of those three are
**truthy** — a plain ``if confirmed:`` opens an auction for a buyer who said no. An
identity check against ``True`` admits exactly the boolean the buyer's confirmation button
produces and nothing else. A caller sending a string gets a refusal, which is loud, rather
than an auction, which is not.

One confirmation, one auction
-----------------------------
A confirmed intent is recorded in a :class:`ConfirmationLedger` *before* the client is
called, and a second confirmation of the same ``intent_id`` is refused. Without it a
double-clicked confirm button, a retried request or an impatient client opens two auctions
for one need: both solicit bids from real stores, both spend the R10 window, and the buyer
is shown one of them. The claim is released if the client call itself fails, so a genuine
retry after a network error still works.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .errors import (
    AuctionClientUnusable,
    ConfirmationWithheld,
    IntentAlreadyConfirmed,
    UnstructuredIntent,
)
from .models import AuctionCreated, Intent, check_intent_bounds, coerce_intent

_log = logging.getLogger(__name__)

__all__ = [
    "AUCTION_CLIENT_METHODS",
    "ConfirmationLedger",
    "confirm",
    "confirmations",
    "reset_confirmations",
]

#: Method names an auction client may expose, most specific first. The first callable one
#: wins; a bare callable client is the last resort.
AUCTION_CLIENT_METHODS: tuple[str, ...] = (
    "create_auction",
    "open_auction",
    "post_auction",
    "create",
    "open",
    "submit",
)


class ConfirmationLedger:
    """Which intents have already been turned into an auction.

    Process-local and lock-guarded. It is not a distributed lock and does not pretend to
    be one: across several service processes the exchange's own idempotency is what has to
    hold. What this closes is the common case — one process, one buyer, two clicks.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._auctions: dict[str, str] = {}

    def claim(self, intent_id: str) -> None:
        """Reserve ``intent_id``. Raises if it was already confirmed."""
        with self._lock:
            if intent_id in self._auctions:
                raise IntentAlreadyConfirmed(
                    f"intent {intent_id!r} already created auction "
                    f"{self._auctions[intent_id]!r}; one confirmation opens one auction. "
                    f"Clarify again to shop for something else."
                )
            self._auctions[intent_id] = ""

    def record(self, intent_id: str, auction_id: str) -> None:
        with self._lock:
            self._auctions[intent_id] = auction_id

    def release(self, intent_id: str) -> None:
        """Give an unspent claim back, so a failed call can honestly be retried."""
        with self._lock:
            if self._auctions.get(intent_id) == "":
                del self._auctions[intent_id]

    def auction_for(self, intent_id: str) -> str | None:
        with self._lock:
            return self._auctions.get(intent_id)

    def reset(self) -> None:
        with self._lock:
            self._auctions.clear()


_LEDGER = ConfirmationLedger()


def confirmations() -> ConfirmationLedger:
    """The process-wide ledger :func:`confirm` uses when none is injected."""
    return _LEDGER


def reset_confirmations() -> None:
    """Forget every confirmation. For tests and for a fresh process only."""
    confirmations().reset()


def confirm(
    intent: Any,
    auction_client: Any,
    *,
    confirmed: bool = False,
    profile: Any = None,
    roster: Sequence[Any] | None = None,
    bid_timeout_seconds: float | None = None,
    ledger: ConfirmationLedger | None = None,
    now: datetime | None = None,
) -> AuctionCreated:
    """Create the auction for an intent the buyer has confirmed. R1's only side effect.

    Args:
        intent: the structured intent that was shown to the buyer — an
            :class:`~buyer_svc.intent.models.Intent`, its ``dict``, or any object that
            serializes to one.
        auction_client: whatever talks to the exchange's ``POST /auctions``. Any object
            exposing one of :data:`AUCTION_CLIENT_METHODS`, or a bare callable.
        confirmed: must be exactly ``True``. Anything else — including a truthy string —
            is a refusal, and the client is never touched.
        profile: the pseudonymous :class:`BuyerProfile` (T-070) to solicit under. Omitted
            rather than faked when absent; this function never invents buyer data.
        roster: the store roster to solicit, passed straight through.
        bid_timeout_seconds: R10's bid window for this auction.
        ledger: the confirmation ledger; defaults to the process-wide one.
        now: creation timestamp for the receipt.

    Returns:
        :class:`~buyer_svc.intent.models.AuctionCreated` — the receipt for exactly one
        auction, and the only evidence this package produces that one exists.

    Raises:
        ConfirmationWithheld: ``confirmed`` was not exactly ``True``. **Nothing was
            called.**
        UnstructuredIntent: there is no structured intent here to confirm.
        IntentTooLarge: the intent is bigger than this service stores for one shopping
            need — see :func:`~buyer_svc.intent.models.check_intent_bounds`. Raised before
            the ledger claim, so an oversized intent leaves nothing behind.
        IntentAlreadyConfirmed: this intent already opened an auction.
        AuctionClientUnusable: the client exposes no way to create an auction.
    """
    # FIRST. Before the intent is read, before the client is looked at. Everything below
    # this line is unreachable for an unconfirmed buyer.
    if confirmed is not True:
        raise ConfirmationWithheld(
            f"the buyer has not confirmed this intent (confirmed={confirmed!r}), so no "
            f"auction may be created (R1). `confirmed` must be exactly True: a truthy "
            f"string such as 'false' is a refusal here, not a confirmation."
        )

    resolved = coerce_intent(intent)
    _require_structure(resolved)
    # BEFORE the claim, and that ordering is the whole of T-368. `intent_id` becomes a key
    # in a ledger with no capacity, no TTL, no LRU and no sweep, whose `release()` frees only
    # an UNSPENT claim — so a key written here is written for the life of the process. This
    # door is unauthenticated, so the size of what it stores cannot be the caller's choice.
    check_intent_bounds(resolved)

    # Through the accessor, not the module global: one place decides what "the default
    # ledger" is, so a deployment that swaps it in has one thing to swap rather than
    # every reader of `_LEDGER` to find.
    book = ledger if ledger is not None else confirmations()
    book.claim(resolved.intent_id)

    try:
        name, method = _auction_entrypoint(auction_client)
        payload = _request_payload(
            resolved,
            profile=profile,
            roster=roster,
            bid_timeout_seconds=bid_timeout_seconds,
        )
        response = method(payload)
    except Exception:
        # The auction was not created, so the claim was not spent. Give it back, or a
        # transient exchange outage would permanently refuse this buyer's need.
        book.release(resolved.intent_id)
        raise

    auction_id = _auction_id(response)
    if not auction_id:
        _log.warning(
            "the exchange accepted the auction for intent %s but returned no auction_id "
            "(%r); the auction exists and this receipt cannot name it",
            resolved.intent_id,
            response,
        )
    book.record(resolved.intent_id, auction_id)
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return AuctionCreated(
        auction_id=auction_id,
        intent_id=resolved.intent_id,
        intent=resolved,
        response=response,
        created_at=moment.isoformat().replace("+00:00", "Z"),
        called=name,
    )


def _require_structure(intent: Intent) -> None:
    """R1: the thing the buyer confirmed has to have been a *structured* intent."""
    if not intent.query.strip():
        raise UnstructuredIntent("the confirmed intent carries no use case / query")
    if not intent.budget_band.strip():
        raise UnstructuredIntent("the confirmed intent carries no budget band")


def _auction_entrypoint(auction_client: Any) -> tuple[str, Any]:
    """The one callable that creates an auction on this client."""
    if auction_client is None:
        raise AuctionClientUnusable(
            "confirm() was given no auction client, so the confirmed intent has nowhere "
            "to go. Pass the exchange client that owns POST /auctions."
        )
    for name in AUCTION_CLIENT_METHODS:
        candidate = getattr(auction_client, name, None)
        if callable(candidate):
            return name, candidate
    if callable(auction_client):
        return "__call__", auction_client
    raise AuctionClientUnusable(
        f"the auction client {type(auction_client).__name__} exposes none of "
        f"{list(AUCTION_CLIENT_METHODS)} and is not callable, so there is no way to "
        f"create the auction this buyer confirmed."
    )


def _request_payload(
    intent: Intent,
    *,
    profile: Any,
    roster: Sequence[Any] | None,
    bid_timeout_seconds: float | None,
) -> dict[str, Any]:
    """The exchange's ``POST /auctions`` body: ``{intent, profile?, roster?, timeout?}``.

    Optional halves are **omitted** when the caller did not supply them rather than sent
    as empty defaults. An empty roster and a missing roster mean different things to the
    exchange, and a fabricated profile would be a buyer record nobody produced.
    """
    payload: dict[str, Any] = {"intent": intent.to_dict()}
    if profile is not None:
        payload["profile"] = _plain(profile)
    if roster is not None:
        payload["roster"] = [_plain(entry) for entry in roster]
    if bid_timeout_seconds is not None:
        payload["bid_timeout_seconds"] = float(bid_timeout_seconds)
    return payload


def _plain(value: Any) -> Any:
    for attr in ("to_dict", "model_dump", "dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001 - fall through to the value itself
                continue
    return value


def _auction_id(response: Any) -> str:
    """Read the auction id off whatever the exchange handed back."""
    if isinstance(response, str):
        return response.strip()
    if isinstance(response, Mapping):
        for key in ("auction_id", "auctionId", "id"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    for key in ("auction_id", "auctionId", "id"):
        value = getattr(response, key, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
