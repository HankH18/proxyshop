"""Reconcile the accepted offer against what actually happened. The webhook is the truth.

R4. Three signals arrive about one checkout and they are not equally trustworthy:

* ``accepted`` — the offer the buyer accepted. This is the **promise** being graded, not
  evidence about the outcome.
* ``checkout_pixel`` — a client-side observation. It is lossy by construction: ad blockers
  eat it, the tab closes before it fires, the discount block renders after it reads. It is
  useful for *noticing* that something happened and for *diagnosing* a gap, and it is never
  authoritative.
* ``order_paid`` — the ``orders/paid`` webhook. Server-to-server, signed, and the record the
  merchant's own books agree with. **Every integrity comparison derives from this event.**

That last rule is the whole ticket, and both directions of getting it wrong are real:

* a *wrong pixel* must not fail an honest store — a pixel that reports 90.00 against a
  webhook of 100.00 is a broken pixel, not a broken promise;
* a *matching pixel* must not mask a webhook discrepancy — a store whose pixel echoes the
  promise while the webhook charged 120.00 is exactly the fraud this system exists to catch,
  and averaging the two sources, or preferring whichever agrees with the offer, hides it.

So the pixel never enters a comparison. It is recorded (``pixel_missing``, ``pixel_price``,
``pixel_agrees``) because a pixel that persistently disagrees with the webhook is itself a
finding — but a finding about the *integration*, and one that a later ticket grades, not one
that moves a trust dimension here.

A dropped pixel is not an error: with the webhook alone the order still reconciles cleanly
and the gap is recorded as ``pixel_missing: true``. Refusing to reconcile without the pixel
would let a store suppress its own grading by breaking its own analytics.

No scoring happens here (T-061 non-goal "no score math"): not one arithmetic operation in
this module touches a Beta, a weight or a score. What it does own is the *translation* --
:func:`reconciled_observations` and :func:`observation_events` say which dimension each
verdict lands on and which published observation type it is, and :mod:`trust.scoring` decides
what that is worth.

That translation lives here rather than in ``trust.ledger`` because for a while it lived
NOWHERE, and the loop dead-ended at exactly this seam: a ``reconciled`` payload carries
``price_honored`` / ``discount_honored`` and no ``dim`` and no ``type``, and
``observations_from_events`` silently skips any event lacking both. Measured, a store that
promised 100 at 20% off and charged 130 reconciled perfectly to ``price_honored: False`` and
then moved nothing at all. The consumer's shape is fixed and public, so the emitter meets it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

__all__ = [
    "ACCEPTED_KIND",
    "DISCOUNT_TOLERANCE",
    "DISHONORED_OBSERVATION_TYPE",
    "HONORED_OBSERVATION_TYPE",
    "INCOMPARABLE_OBSERVATION_TYPE",
    "OBSERVATION_KIND",
    "PIXEL_KIND",
    "PRICE_TOLERANCE",
    "RECONCILED_DIMENSIONS",
    "RECONCILED_KIND",
    "WEBHOOK_KIND",
    "ReconciliationInputError",
    "observation_events",
    "reconcile",
    "reconciled_event",
    "reconciled_observations",
]

#: The three input kinds, and the one output kind. All four are already in the frozen
#: 18-kind ledger vocabulary (``trust.events.LEDGER_EVENT_KINDS`` / the
#: ``commerce_events_kind_check`` constraint), so nothing here needs a migration or a
#: vocabulary edit — which would be a two-file change spanning T-060's and T-011's scopes.
ACCEPTED_KIND = "accepted"
PIXEL_KIND = "checkout_pixel"
WEBHOOK_KIND = "order_paid"
RECONCILED_KIND = "reconciled"

#: Money comparison tolerance, absolute, in the offer's currency unit. Half a cent: enough to
#: absorb the float round-trip a JSON payload takes, far too small to hide a real overcharge.
PRICE_TOLERANCE = 0.005

#: Discount comparison tolerance, in percentage points.
DISCOUNT_TOLERANCE = 0.01

#: The kind the translated trust observations are emitted under. ``offer_integrity`` is
#: already in the frozen 18-kind vocabulary and its published body is exactly what one of
#: these carries — ``(bid_ref, field, promised, observed)`` — so no vocabulary edit, no
#: migration, and nothing in ``contracts`` has to learn a new shape.
OBSERVATION_KIND = "offer_integrity"

#: Which promise each reconciled verdict grades, and the trust dimension it lands on. The
#: pairs are the approved ``claim_type -> dimension`` routing restated for the two fields a
#: reconciliation actually decides; nothing here invents a dimension.
RECONCILED_DIMENSIONS: Mapping[str, str] = MappingProxyType(
    {"price": "price_honored", "discount": "discount_honored"}
)

#: A promise the webhook shows was kept. ``fulfilled``, the published positive for exactly
#: that (weight 1.0) — a kept promise must be able to EARN a store its score, not merely
#: avoid costing it one, or an honest store never leaves the prior.
HONORED_OBSERVATION_TYPE = "fulfilled"

#: A promise the webhook shows was broken. The published ``contradicted`` (2.0).
DISHONORED_OBSERVATION_TYPE = "contradicted"

#: A promise the webhook did not say enough to grade. ``unsupported`` (0.5) — a small
#: published negative whose dominant effect is on coverage and confidence.
#:
#: This distinction is the whole reason ``price_comparable`` / ``discount_comparable`` are on
#: the reconciled payload. Both verdicts read ``False`` when the webhook was silent (the
#: fail-closed direction, "not demonstrated honored"), so a translator that read only the
#: verdict would manufacture a 2.0 contradiction out of a malformed webhook — a penalty the
#: store cannot see coming and cannot appeal.
INCOMPARABLE_OBSERVATION_TYPE = "unsupported"


class ReconciliationInputError(ValueError):
    """An event that cannot be reconciled — no join key, or an unusable payload."""


def _payload(event: Any) -> Mapping[str, Any]:
    value = _field(event, "payload")
    return value if isinstance(value, Mapping) else {}


def _field(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _number(value: Any) -> float | None:
    """A payload number as a float, or ``None`` when it is absent or not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def _discount_percentage(payload: Mapping[str, Any]) -> float | None:
    """The percentage discount an *observation* payload reports.

    Both the pixel and the webhook carry Shopify's ``discountApplications`` shape. Only
    percentage applications are summed: a fixed-amount discount is a different comparison
    (it lands in the totals, which ``price_honored`` already grades) and adding a currency
    figure to a percentage figure would produce a number that means nothing.
    """
    applications = payload.get("discountApplications")
    if not isinstance(applications, Sequence) or isinstance(applications, (str, bytes)):
        return _number(payload.get("discount_percentage"))
    # A PRESENT `discountApplications` list is a complete statement, so an empty one — or one
    # holding only fixed-amount applications — means "no percentage discount was applied",
    # which is 0.0 and not "unknown". Returning None here made a promised discount permanently
    # incomparable against the commonest webhook shape there is.
    total = 0.0
    for application in applications:
        if not isinstance(application, Mapping):
            continue
        if str(application.get("type", "percentage")).lower() != "percentage":
            continue
        value = _number(application.get("value"))
        if value is not None:
            total += value
    return total


def _promised(accepted: Any) -> dict[str, Any]:
    """The promise being graded, read off the ``accepted`` event's offer."""
    offer = _payload(accepted).get("offer")
    offer = offer if isinstance(offer, Mapping) else {}
    discount = offer.get("discount")
    discount = discount if isinstance(discount, Mapping) else {}
    percentage = (
        _number(discount.get("value"))
        if str(discount.get("type", "percentage")).lower() == "percentage"
        else None
    )
    return {
        "product_ref": offer.get("product_ref"),
        "unit_price": _number(offer.get("unit_price")),
        "total_price": _number(offer.get("total_price")),
        "discount_percentage": percentage,
    }


#: Separator between a store scope and a join key. A control character, so it cannot occur
#: inside a checkout token or an order reference and collapse two keys into one.
_SCOPE_SEPARATOR = "\x1f"

#: The scope events whose store could not be determined are filed under. They join each other
#: and nothing else, which is the pre-existing behaviour — not an improvement, just not a
#: regression.
_UNATTRIBUTED_SCOPE = ""


def _join_keys(event: Any) -> tuple[str, ...]:
    """Every identifier an event can be joined on.

    ``checkout_token`` and ``order_ref`` are network-issued and globally unique. ``order_id``
    is NOT: it is the platform's own order number, and Shopify numbers orders **per shop**, so
    two unrelated stores both legitimately have order ``1001``. That is why the caller scopes
    every one of these by store before joining — see :func:`_scoped_keys`.
    """
    payload = _payload(event)
    keys = []
    for candidate in (
        payload.get("checkout_token"),
        _field(event, "order_ref"),
        payload.get("order_ref"),
        payload.get("order_id"),
    ):
        if candidate is not None and str(candidate).strip():
            keys.append(str(candidate).strip())
    return tuple(dict.fromkeys(keys))


def _store_of(event: Any) -> str | None:
    store = _field(event, "store_id") or _payload(event).get("store_id")
    if store is None:
        return None
    text = str(store).strip()
    return text or None


def _scoped_keys(keys: tuple[str, ...], scope: str) -> tuple[str, ...]:
    """Namespace a set of join keys by the store they belong to.

    Without this, one platform ``order_id`` shared by two shops merges their orders into a
    single group and ``setdefault`` keeps whichever webhook and whichever offer arrived first.
    Measured before the fix: two stores each with order_id ``1001`` produced ONE reconciled
    event, and the second store's 10x overcharge was never graded and never raised — the order
    simply disappeared. That is the reconciliation escape hatch :class:`ReconciliationInputError`
    exists to close, reached through a field a store does not even have to omit.
    """
    return tuple(f"{scope}{_SCOPE_SEPARATOR}{key}" for key in keys)


class _Groups:
    """Union-find over join keys.

    A pixel that carries only a ``checkout_token`` and a webhook that carries only an
    ``order_ref`` are the same order, and joining on one key alone would split them into two
    groups — one of which has no webhook and would silently never reconcile. So every key an
    event carries is unioned together, and an event joins whichever group any of its keys is
    already in.
    """

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, key: str) -> str:
        self._parent.setdefault(key, key)
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:  # path compression, so the walk stays cheap
            self._parent[key], key = root, self._parent[key]
        return root

    def union(self, keys: Iterable[str]) -> str | None:
        keys = list(keys)
        if not keys:
            return None
        root = self.find(keys[0])
        for key in keys[1:]:
            other = self.find(key)
            if other != root:
                self._parent[other] = root
        return root


def reconciled_event(
    *,
    order_ref: str,
    store_id: Any,
    checkout_token: Any,
    promised: Mapping[str, Any],
    webhook: Any,
    pixel: Any,
    ts: Any,
    bid_ref: Any = None,
) -> dict[str, Any]:
    """Build the one ``reconciled`` event for one order.

    Every comparison in the payload derives from ``webhook``. ``pixel`` contributes only
    ``pixel_missing`` / ``pixel_price`` / ``pixel_agrees``, which describe the *integration*
    and never a promise.

    The payload carries ``price_comparable`` / ``discount_comparable`` alongside the two
    verdicts. Read them: a verdict of ``False`` on an *incomparable* field means "the webhook
    did not say", not "the store overcharged", and turning the second into a ``contradicted``
    observation would penalise a store for a malformed webhook.
    """
    observation = _payload(webhook)
    observed_price = _number(observation.get("total_price"))
    if observed_price is None:
        observed_price = _number(observation.get("current_total_price"))
    observed_discount = _discount_percentage(observation)

    promised_price = promised.get("total_price")
    if promised_price is None:
        promised_price = promised.get("unit_price")
    promised_discount = promised.get("discount_percentage")

    # Whether the comparison could be made AT ALL, kept separate from its verdict. A webhook
    # that carries no total is not evidence that the store overcharged — it is a gap. The
    # boolean below still reads False in that case (the fail-closed direction: "not
    # demonstrated honored"), but a consumer turning this event into a trust observation must
    # read `price_comparable` to choose `unsupported` over `contradicted`. Collapsing the two
    # would let a malformed webhook manufacture a contradiction, which is a penalty the store
    # cannot see coming and cannot appeal.
    price_comparable = observed_price is not None and promised_price is not None

    # A promise of 0% — or of no discount at all — has nothing to dishonour, so it is honored
    # trivially and comparably. Requiring an observed discount in that case made an explicitly
    # promised `discount: {"type": "percentage", "value": 0}` impossible to honor: the promise
    # was 0.0 rather than None, so the code demanded a discount observation that a correct
    # webhook has no reason to carry, and an honest store failed `discount_honored`.
    promised_discount_pct = 0.0 if promised_discount is None else float(promised_discount)
    discount_required = promised_discount_pct > 0.0
    discount_comparable = not discount_required or observed_discount is not None

    # `price_honored` is one-sided on purpose: charging LESS than promised is not a broken
    # promise, and grading it as one would penalise a store for a goodwill discount.
    price_honored = (
        observed_price is not None
        and promised_price is not None
        and observed_price <= promised_price + PRICE_TOLERANCE
    )
    discount_honored = not discount_required or (
        observed_discount is not None
        and observed_discount >= promised_discount_pct - DISCOUNT_TOLERANCE
    )

    pixel_price = _number(_payload(pixel).get("total_price")) if pixel is not None else None
    pixel_agrees = (
        pixel_price is not None
        and observed_price is not None
        and abs(pixel_price - observed_price) <= PRICE_TOLERANCE
    )

    return {
        # Scoped by store for the same reason the join keys are: a platform `order_id` is a
        # per-shop number, so `reconciled:1001` alone is not a unique event id across shops —
        # and `event_id` IS the ledger's idempotency key, so a collision would make one shop's
        # reconciliation a silent no-op against another's.
        "event_id": f"reconciled:{store_id}:{order_ref}",
        "ts": ts,
        "kind": RECONCILED_KIND,
        "store_id": store_id,
        "order_ref": order_ref,
        "payload": {
            "order_ref": order_ref,
            "checkout_token": checkout_token,
            "product_ref": promised.get("product_ref"),
            # Carried from the accepted offer so a translated observation can name the bid it
            # grades: `offer_integrity` publishes `bid_ref` in its body, and an integrity
            # finding that cannot be traced back to the bid that made the promise is one a
            # store can neither check nor contest.
            "bid_ref": bid_ref,
            # the verdicts — every one of them computed from the webhook
            "price_honored": bool(price_honored),
            "discount_honored": bool(discount_honored),
            "price_comparable": bool(price_comparable),
            "discount_comparable": bool(discount_comparable),
            "observed_price": observed_price,
            "observed_discount_percentage": observed_discount,
            "promised_price": promised_price,
            "promised_discount_percentage": promised_discount,
            "authority": WEBHOOK_KIND,
            # the pixel, recorded and never consulted
            "pixel_missing": pixel is None,
            "pixel_price": pixel_price,
            "pixel_agrees": bool(pixel_agrees),
        },
    }


def reconcile(events: Iterable[Any]) -> list[dict[str, Any]]:
    """Join one stream of checkout events into ``reconciled`` events, one per paid order.

    Args:
        events: ``accepted`` / ``checkout_pixel`` / ``order_paid`` records in any order,
            joined on ``checkout_token`` and ``order_ref``. Other kinds are ignored — this
            function is handed whole ledger pages, not a curated triple.

    Returns:
        The events to emit, in the order their webhooks arrived: exactly one ``reconciled``
        event per order that has an ``order_paid``. An order with a pixel but **no** webhook
        yields nothing: there is no authoritative record to grade it against, and inventing a
        verdict from the pixel is the one thing R4 forbids. An order with no ``accepted``
        offer likewise yields nothing — there is no promise to compare against, so there is
        no integrity question to answer.

    Raises:
        ReconciliationInputError: a webhook carrying no join key at all, which cannot be
            attributed to an order and must not be silently dropped.
    """
    # THREE passes, and each split is load-bearing.
    #
    # 1. Collect, and record which store claims which raw join key. Join keys are namespaced
    #    by store because `order_id` is a PER-SHOP number: two shops both have order 1001, and
    #    joining on it unscoped merges their orders into one group.
    # 2. Union. Two keys only become known to be the same order when some event carries both,
    #    which may be the LAST event in the stream — so nothing may be filed into a group
    #    while the groups are still merging, or a webhook and its offer end up in different
    #    buckets and the order silently never reconciles.
    # 3. File and emit.
    collected: list[tuple[str, Any, tuple[str, ...], str | None]] = []
    key_stores: dict[str, set[str]] = {}
    for event in events:
        kind = str(_field(event, "kind", ""))
        if kind not in (ACCEPTED_KIND, PIXEL_KIND, WEBHOOK_KIND):
            continue
        keys = _join_keys(event)
        if not keys:
            if kind == WEBHOOK_KIND:
                raise ReconciliationInputError(
                    "an order_paid webhook carries no checkout_token and no order_ref, so it "
                    "cannot be joined to the offer it is meant to grade. Dropping it silently "
                    "would let a store escape reconciliation by omitting a field."
                )
            continue
        store = _store_of(event)
        if store is not None:
            for key in keys:
                key_stores.setdefault(key, set()).add(store)
        collected.append((kind, event, keys, store))

    groups = _Groups()
    relevant: list[tuple[str, Any, tuple[str, ...]]] = []
    for kind, event, keys, store in collected:
        if store is None:
            # A client-side pixel often knows the checkout token and not the shop. Adopt the
            # store only when exactly one store claims one of its keys; two candidates is
            # precisely the collision this scoping exists to catch, so it stays unattributed
            # rather than being guessed into somebody's order.
            candidates: set[str] = set()
            for key in keys:
                candidates |= key_stores.get(key, set())
            store = next(iter(candidates)) if len(candidates) == 1 else None
        scoped = _scoped_keys(keys, store if store is not None else _UNATTRIBUTED_SCOPE)
        groups.union(scoped)
        relevant.append((kind, event, scoped))

    members: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for kind, event, keys in relevant:
        root = groups.find(keys[0])
        members.setdefault(root, {}).setdefault(kind, event)
        if kind == WEBHOOK_KIND and root not in order:
            order.append(root)

    emitted: list[dict[str, Any]] = []
    for root in order:
        bucket = members[root]
        webhook = bucket.get(WEBHOOK_KIND)
        accepted = bucket.get(ACCEPTED_KIND)
        if webhook is None or accepted is None:
            continue
        order_ref = _field(webhook, "order_ref") or _payload(webhook).get("order_id")
        # The fallback strips the store scope back off: `root` is a namespaced key, and a
        # control character has no business appearing in an emitted order reference.
        fallback = root.split(_SCOPE_SEPARATOR, 1)[-1]
        emitted.append(
            reconciled_event(
                order_ref=str(order_ref) if order_ref is not None else fallback,
                store_id=_field(webhook, "store_id") or _field(accepted, "store_id"),
                checkout_token=_payload(webhook).get("checkout_token")
                or _payload(accepted).get("checkout_token"),
                promised=_promised(accepted),
                webhook=webhook,
                pixel=bucket.get(PIXEL_KIND),
                ts=_field(webhook, "ts"),
                bid_ref=_payload(accepted).get("bid_ref"),
            )
        )
    return emitted


def _as_reconciled_events(reconciled: Any) -> list[Any]:
    """One reconciled event, or an iterable of them, as a list of the ``reconciled`` ones."""
    events = [reconciled] if isinstance(reconciled, Mapping) else list(reconciled)
    return [event for event in events if str(_field(event, "kind", "")) == RECONCILED_KIND]


def _verdict_observation_type(*, honored: Any, comparable: Any) -> str:
    """One reconciled verdict as a published observation type."""
    if not bool(comparable):
        return INCOMPARABLE_OBSERVATION_TYPE
    return HONORED_OBSERVATION_TYPE if bool(honored) else DISHONORED_OBSERVATION_TYPE


def _graded_fields(payload: Mapping[str, Any]) -> list[tuple[str, str, str, Any, Any]]:
    """The promises this reconciliation graded: ``(field, dim, type, promised, observed)``.

    ONE place decides which promises are gradeable and what each verdict is worth, so the
    observation records and the ledger events below cannot drift apart about either.

    A verdict is only evidence about a store when there was a promise behind it, so a field
    the offer never promised produces NOTHING here — not a positive, and not a negative.

    * ``discount_honored`` reads ``True`` when no discount was promised, because there is
      nothing to dishonour. Translating that into a ``fulfilled`` observation would pay a
      store for a promise it never made, on every order it ever takes: promise no discount,
      collect free positive evidence on ``discount_honored`` forever.
    * a missing promised PRICE is likewise not gradeable. ``unsupported`` there would
      penalise a store for an offer that carried no price rather than for anything it did.
    """
    graded: list[tuple[str, str, str, Any, Any]] = []

    promised_price = payload.get("promised_price")
    if promised_price is not None:
        graded.append(
            (
                "price",
                RECONCILED_DIMENSIONS["price"],
                _verdict_observation_type(
                    honored=payload.get("price_honored"),
                    comparable=payload.get("price_comparable"),
                ),
                promised_price,
                payload.get("observed_price"),
            )
        )

    promised_discount = payload.get("promised_discount_percentage")
    if promised_discount is not None and float(promised_discount) > 0.0:
        graded.append(
            (
                "discount",
                RECONCILED_DIMENSIONS["discount"],
                _verdict_observation_type(
                    honored=payload.get("discount_honored"),
                    comparable=payload.get("discount_comparable"),
                ),
                promised_discount,
                payload.get("observed_discount_percentage"),
            )
        )
    return graded


def reconciled_observations(reconciled: Any) -> list[dict[str, Any]]:
    """Translate reconciled verdicts into the trust observations the scorer consumes.

    This is the R4 -> R12 seam. Before it existed, ``reconciled`` events carried
    ``price_honored`` / ``discount_honored`` and no ``dim`` and no ``type``, and
    ``trust.ledger.replay.observations_from_events`` silently skips any event whose payload
    lacks both — so a dishonest fulfilment reconciled perfectly and then moved nothing.
    Measured: ``accepted{total 100, discount 20%}`` + ``order_paid{total 130}`` produced
    ``price_honored=False``, ``discount_honored=False``, and then ``[]`` observations and
    ``{}`` snapshots.

    Args:
        reconciled: one ``reconciled`` event, or an iterable of events. Events of any other
            kind are ignored, so a whole ledger page can be handed straight in.

    Returns:
        ``[{store_id, dim, type, observed_at}, ...]`` in field order (price, then discount)
        per order, in the order the reconciled events arrived. Deterministic: no clock, no
        randomness, and the same input gives the same list — the ledger is replayable only if
        everything written into it is.

        Observations carry NO per-observation ``weight``: a reconciliation is a machine
        comparison against the authoritative webhook, worth exactly its published type weight.
        The weight channel exists for reports whose *source* is discountable (R14 buyer
        feedback), and defaulting these to 1.0 is what keeps a replayed snapshot bit-identical
        to a served one.
    """
    observations: list[dict[str, Any]] = []
    for event in _as_reconciled_events(reconciled):
        payload = _payload(event)
        store_id = _field(event, "store_id") or payload.get("store_id")
        observed_at = payload.get("observed_at") or _field(event, "ts")
        for _field_name, dimension, observation_type, _promised, _observed in _graded_fields(
            payload
        ):
            observations.append(
                {
                    "store_id": store_id,
                    "dim": dimension,
                    "type": observation_type,
                    "observed_at": observed_at,
                }
            )
    return observations


def observation_events(reconciled: Any) -> list[dict[str, Any]]:
    """The same translation, as ledger events the existing replay consumer already reads.

    ``apps/trust/src/ledger/**`` belongs to another ticket and
    ``observations_from_events`` projects exactly ``{store_id, dim, type, observed_at}`` off
    any event whose payload names a ``dim`` and a ``type``. So the translation lives on the
    EMITTER side: these events are shaped to what that consumer already requires, rather than
    the consumer being asked to learn what a ``reconciled`` payload means.

    One event per graded promise, not one per order, because an observation is about exactly
    one dimension and ``observations_from_events`` reads exactly one ``dim`` per event.

    Args:
        reconciled: one ``reconciled`` event, or an iterable of events.

    Returns:
        ``offer_integrity`` events carrying the published body for that kind
        (``bid_ref`` / ``field`` / ``promised`` / ``observed``) plus the ``dim``, ``type`` and
        ``observed_at`` the trust projection reads. ``event_id`` is
        ``offer_integrity:{store}:{order}:{field}`` — deterministic, and scoped by store for
        the same reason the reconciled event's is: a platform ``order_id`` is a PER-SHOP
        number, and ``event_id`` IS the ledger's idempotency key, so an unscoped one would
        make one shop's integrity finding a silent no-op against another's.
    """
    events: list[dict[str, Any]] = []
    for event in _as_reconciled_events(reconciled):
        payload = _payload(event)
        store_id = _field(event, "store_id") or payload.get("store_id")
        order_ref = payload.get("order_ref") or _field(event, "order_ref")
        observed_at = payload.get("observed_at") or _field(event, "ts")
        for field, dimension, observation_type, promised_value, observed_value in _graded_fields(
            payload
        ):
            events.append(
                {
                    "event_id": f"{OBSERVATION_KIND}:{store_id}:{order_ref}:{field}",
                    "ts": _field(event, "ts"),
                    "kind": OBSERVATION_KIND,
                    "store_id": store_id,
                    "order_ref": order_ref,
                    "payload": {
                        "bid_ref": payload.get("bid_ref"),
                        "field": field,
                        "promised": promised_value,
                        "observed": observed_value,
                        "dim": dimension,
                        "type": observation_type,
                        "observed_at": observed_at,
                        "reconciled_event_id": _field(event, "event_id"),
                        "authority": WEBHOOK_KIND,
                    },
                }
            )
    return events
