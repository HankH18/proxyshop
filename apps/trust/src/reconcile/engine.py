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
    "CODE_BRIDGE_KINDS",
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
    "discount_codes_of",
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

#: The kinds that are read for a join key and then thrown away: they are never graded, never
#: emitted, and contribute nothing to a verdict. They exist here for one reason — they are the
#: only events that carry BOTH the exchange's ``checkout_token`` and the single-use discount
#: code, and without that pairing the two halves of a real checkout never meet. See
#: :func:`discount_codes_of` for why the code is the handle and what it is worth as one.
#:
#: Both are already in the frozen 18-kind vocabulary, so admitting them is not a vocabulary
#: edit; ``reconcile`` simply stopped ignoring two records the ledger was already keeping.
CODE_BRIDGE_KINDS: tuple[str, ...] = ("code_created", "checkout_redirect")

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


#: Where a discount application can be spelled. ``discountApplications`` is the GraphQL/pixel
#: camelCase; ``discount_applications`` is what a REST ``orders/paid`` body actually carries,
#: and reading only the first one is why the discount verdict never graded on a real webhook.
_APPLICATION_ALIASES: tuple[str, ...] = ("discountApplications", "discount_applications")


def _is_percentage_application(application: Mapping[str, Any]) -> bool:
    """Whether one discount application states a PERCENTAGE rather than an amount.

    Two vocabularies meet here and they use ``type`` for different things. In the camelCase
    shape this repo's own producers emit, ``type`` is the value kind — ``"percentage"`` or
    ``"fixed_amount"``. In Shopify's REST body, ``type`` is the *application* kind —
    ``"discount_code"``, ``"manual"``, ``"script"``, ``"automatic"`` — and the value kind is
    ``value_type``. Reading ``type`` alone therefore threw away every real application:
    measured on the body the stub signs, a store that gave exactly the promised 20% and a
    store that gave nothing at all both came back ``observed_discount_percentage: None`` and
    ``discount_comparable: False``, so both were translated to ``unsupported`` — the honest
    store penalised, and the store that broke its promise spared the 2.0 ``contradicted``.

    So ``value_type`` wins where it is present, and ``type`` is the fallback.
    """
    for name in ("value_type", "valueType"):
        if application.get(name) not in (None, ""):
            return str(application[name]).lower() == "percentage"
    return str(application.get("type", "percentage")).lower() == "percentage"


def _discount_percentage(payload: Mapping[str, Any]) -> float | None:
    """The percentage discount an *observation* payload reports.

    Both the pixel and the webhook carry Shopify's discount-application shape, in one of its
    two spellings. Only percentage applications are summed: a fixed-amount discount is a
    different comparison (it lands in the totals, which ``price_honored`` already grades) and
    adding a currency figure to a percentage figure would produce a number that means nothing.
    """
    applications: Any = None
    for name in _APPLICATION_ALIASES:
        candidate = payload.get(name)
        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            applications = candidate
            break
    if applications is None:
        return _number(payload.get("discount_percentage"))
    # A PRESENT application list is a complete statement, so an empty one — or one holding
    # only fixed-amount applications — means "no percentage discount was applied", which is
    # 0.0 and not "unknown". Returning None here made a promised discount permanently
    # incomparable against the commonest webhook shape there is.
    total = 0.0
    for application in applications:
        if not isinstance(application, Mapping):
            continue
        if not _is_percentage_application(application):
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


#: Separator between a store scope and a join key. A control character, so it does not occur
#: inside a checkout token or an order reference by accident and collapse two keys into one.
#:
#: "By accident" is doing real work in that sentence, which is why :func:`_key_component`
#: exists. A composed key is now ``{store}\x1f[discount_code\x1f]{value}``, and every one of
#: those components is a string somebody else chose. Unescaped, a store calling itself
#: ``s\x1fdiscount_code`` and reporting an order whose checkout token is ``PSX-ABCD1234``
#: composes ``s\x1fdiscount_code\x1fPSX-ABCD1234`` — byte-for-byte the key store ``s``'s
#: single-use code composes. It would join another shop's authorized checkout and be graded
#: against that shop's promise. Escaping the separator out of every component closes it, and
#: closes the same forgery through ``store_id`` alone, which pre-dates the code namespace.
_SCOPE_SEPARATOR = "\x1f"

#: ``%`` first, so the escape sequence cannot be forged by a value that legitimately contains
#: ``%1F``. Same construction as :data:`_ID_ESCAPES`, applied to a different separator.
_KEY_ESCAPES = (("%", "%25"), (_SCOPE_SEPARATOR, "%1F"))


def _key_component(value: str) -> str:
    """One component of a composed join key, with the separator escaped out of it."""
    for raw, escaped in _KEY_ESCAPES:
        value = value.replace(raw, escaped)
    return value


def _key_component_decoded(value: str) -> str:
    """:func:`_key_component` undone — the original value, for publishing back out."""
    for raw, escaped in reversed(_KEY_ESCAPES):
        value = value.replace(escaped, raw)
    return value


#: The scope events whose store could not be determined are filed under. They join each other
#: and nothing else, which is the pre-existing behaviour — not an improvement, just not a
#: regression.
_UNATTRIBUTED_SCOPE = ""

#: The separator the emitted ``event_id``s are built from, and the escapes that keep it
#: unambiguous. ``%`` first, so the escape sequence itself cannot be forged by a reference
#: that legitimately contains ``%3A``.
_ID_SEPARATOR = ":"
_ID_ESCAPES = (("%", "%25"), (_ID_SEPARATOR, "%3A"))


def _id_component(value: Any) -> str:
    """One component of a composed ``event_id``, with the separator escaped out of it.

    ``event_id`` IS the ledger's idempotency key (D16), so two different orders that spell the
    same id are one order as far as the chain is concerned — the second append is a silent
    no-op and a whole shop's integrity finding disappears. Scoping ``reconciled:{store}:{order}``
    by store closes that for a per-shop ``order_id``, but only if the separator is actually a
    separator: store ``s:x`` order ``1`` and store ``s`` order ``x:1`` both spell
    ``reconciled:s:x:1``, which is the same escape hatch reached through a different field.

    Escaped rather than rejected: a colon in a merchant's own order reference is not
    misbehaviour, and refusing to reconcile such an order would let a store suppress its own
    grading by choosing its reference format.
    """
    text = "" if value is None else str(value)
    for raw, escaped in _ID_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def _join_keys(event: Any) -> tuple[str, ...]:
    """Every *identifier* an event can be joined on. The discount code is not one of these.

    ``checkout_token`` and ``order_ref`` are network-issued and globally unique. ``order_id``
    is NOT: it is the platform's own order number, and Shopify numbers orders **per shop**, so
    two unrelated stores both legitimately have order ``1001``. That is why the caller scopes
    every one of these by store before joining — see :func:`_scoped_keys`.

    The single-use discount code is a weaker handle than either and is read separately, by
    :func:`discount_codes_of`, so that the difference stays visible at the call site.
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
            keys.append(_key_component(str(candidate).strip()))
    return tuple(dict.fromkeys(keys))


#: The scalar spellings the discount code legitimately arrives under. Exactly the tuple
#: ``contracts.ledger`` publishes for this join key (``packages/contracts/src/ledger.py``:
#: ``"discount_code": ("discount_code", "discountCode", "code")``), restated rather than
#: imported so this module keeps its zero-import surface — a reconciler that cannot be loaded
#: without the contracts package is a reconciler that stops running when contracts moves.
_CODE_SCALAR_ALIASES: tuple[str, ...] = ("discount_code", "discountCode", "code")

#: Longer than any code a person types and longer than Shopify's own 255-character limit is
#: not a code; it is a payload field that happens to sit under one of these names.
_MAX_CODE_LENGTH = 255

#: The *list* spellings, which the published alias tuple cannot reach and which are the only
#: place a real ``orders/paid`` body puts the code. Measured on the recorded webhook the
#: shopify-stub signs and sends: the body carries ``discount_codes: [{"code": "PSX-…"}]`` and
#: ``discount_applications: [{…, "code": "PSX-…"}]`` and no scalar ``discount_code`` at all,
#: so ``contracts.join_key_view()`` reads ``discount_code: None`` off it. A reader that
#: honoured only the published aliases would therefore find the code on the exchange's half of
#: the checkout and never on the merchant's — which is indistinguishable from no join at all.
_CODE_LIST_ALIASES: tuple[str, ...] = (
    "discount_codes",
    "discountCodes",
    "discountApplications",
    "discount_applications",
)


def discount_codes_of(event: Any) -> tuple[str, ...]:
    """The single-use discount codes one event names, upper-cased, in first-seen order.

    **What this key is, and what it is not.** ``checkout_token`` and ``order_ref`` are
    network-issued and globally unique; a discount code is neither. It is 12 characters —
    ``PSX-`` plus 8 Crockford base32 draws from :mod:`secrets`
    (``apps/exchange/src/checkout/codes.py``), so 2^40 of entropy and no mint-time registry —
    and its uniqueness is probabilistic rather than enforced. Worse, the *exchange* issues it
    but the *merchant* decides which order redeems it, so unlike a token neither party alone
    can guarantee it names one checkout. Three consequences, all of them implemented:

    * it is scoped by store like every other key (:func:`_scoped_keys`), so two shops using
      the same code string never merge;
    * it is namespaced into its own key space by :func:`_code_key`, so a code can only ever
      join against another code and never against a token or an order reference that happens
      to spell the same characters;
    * it may **bridge** an offer to an order and may never **merge** two orders or two
      offers — see the component rule in :func:`reconcile`. A false join on a money path is
      worse than no join: two orders in one group means ``setdefault`` keeps the first
      webhook and the second one's overcharge disappears with no error and no trace.

    And one thing it is NOT, stated so nobody has to discover it: a code is not an identity.
    A webhook that names a code and no ``checkout_token`` or ``order_ref`` is still refused
    by :func:`reconcile`, because a code cannot say WHICH order it is — letting it stand in
    for an order reference put a live redeemable code into a published ledger field and let
    two unrelated orders spell one ``event_id``.

    Codes are compared case-insensitively because the platform redeems them that way (the
    stub matches ``order.discount_code.upper() == value.upper()``), so a merchant that echoes
    back ``psx-hd13pb0c`` is naming the same single-use code and must still join.

    The residual this cannot close, and no reconciler can: the merchant decides which order
    redeems a code and which codes its webhook lists, so a store that omits its order's real
    code and names a different one of its OWN checkouts is graded against that checkout's
    promise. Only the merchant's redemption register observes redemption. What IS closed is
    reaching another SHOP's promise, and making a second order disappear while doing it.
    """
    payload = _payload(event)
    found: list[str] = []

    def keep(value: Any) -> None:
        # Strings only, and short ones. A payload read back out of the ledger holds whatever
        # was stored: `str()` of a list is a perfectly good-looking string that would become
        # the join key `['PSX-A']`, `discount_codes: range(3)` yields the keys `0`, `1`, `2`
        # — which can collide with a numeric field somewhere else and force a whole component
        # to be refused — and a 100 000-character value is a 100 000-character key. A code is
        # a short human-typable string or it is not a code.
        if not isinstance(value, str):
            return
        text = value.strip().upper()
        if text and len(text) <= _MAX_CODE_LENGTH:
            found.append(text)

    for name in _CODE_SCALAR_ALIASES:
        keep(payload.get(name))
    for name in _CODE_LIST_ALIASES:
        entries = payload.get(name)
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            continue
        for entry in entries:
            if isinstance(entry, Mapping):
                for inner in _CODE_SCALAR_ALIASES:
                    keep(entry.get(inner))
            else:
                keep(entry)
    return tuple(dict.fromkeys(found))


#: The key space discount codes live in, kept apart from tokens and order references. The
#: separator is the same control character :data:`_SCOPE_SEPARATOR` uses, and what makes it a
#: separator rather than a convention is :func:`_key_component`, which escapes it out of every
#: value first — so no value in one space can be *spelled* to collide with a value in another.
_CODE_NAMESPACE = "discount_code"


def _code_key(code: str) -> str:
    """One discount code as a join key that only other discount codes can match."""
    return f"{_CODE_NAMESPACE}{_SCOPE_SEPARATOR}{_key_component(code)}"


#: The kinds whose discount codes are read at all. ``checkout_pixel`` is absent on purpose —
#: see the comment at its use — and so is every kind outside the checkout, because a code
#: read off an unrelated record could only ever merge things that are not one checkout.
_CODE_BEARING_KINDS: frozenset[str] = frozenset({ACCEPTED_KIND, WEBHOOK_KIND, *CODE_BRIDGE_KINDS})


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
    return tuple(f"{_key_component(scope)}{_SCOPE_SEPARATOR}{key}" for key in keys)


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
    promised_price_basis = "total_price" if promised_price is not None else None
    if promised_price is None:
        promised_price = promised.get("unit_price")
        promised_price_basis = "unit_price" if promised_price is not None else None
    promised_discount = promised.get("discount_percentage")

    # Whether the comparison could be made AT ALL, kept separate from its verdict. A webhook
    # that carries no total is not evidence that the store overcharged — it is a gap. The
    # boolean below still reads False in that case (the fail-closed direction: "not
    # demonstrated honored"), but a consumer turning this event into a trust observation must
    # read `price_comparable` to choose `unsupported` over `contradicted`. Collapsing the two
    # would let a malformed webhook manufacture a contradiction, which is a penalty the store
    # cannot see coming and cannot appeal.
    #
    # A promised UNIT price is one of those gaps, and a subtle one. `observed_price` is the
    # ORDER total, so an offer that named only a unit price is being compared against a
    # different quantity: measured, 30.00 a unit against a three-unit order billed at 90.00 —
    # an exactly honest order — read `price_honored: False`. That was harmless while nothing
    # scored the verdict and became a 2.0 `contradicted` the moment something did.
    price_comparable = (
        observed_price is not None
        and promised_price is not None
        and promised_price_basis != "unit_price"
    )

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
        "event_id": f"reconciled:{_id_component(store_id)}:{_id_component(order_ref)}",
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
            "promised_price_basis": promised_price_basis,
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
            joined on ``checkout_token`` and ``order_ref``, plus the ``code_created`` /
            ``checkout_redirect`` records that bridge those two by the single-use discount
            code. Other kinds are ignored — this function is handed whole ledger pages, not a
            curated triple.

    **Why the discount code is here at all.** The two checkout tokens in a real Shopify
    checkout are not the same value and never were. ``CheckoutProvider.checkout`` mints its
    ``checkout_token`` with ``secrets.token_hex(16)`` *after* the merchant has been called,
    and transmits it nowhere: the cart permalink it builds carries ``?discount=<code>`` and
    nothing else. The store mints its own ``uuid4().hex`` when the cart is visited, and THAT
    is the token that rides onto ``orders/paid``. Measured on the S1 run, the two are
    ``fac073c91fb5bcadf8e214c47a2166a4`` and ``91a1ed79fdb24f61b3e64c91e030b6e9`` — so a join
    on tokens alone finds no shared key, and reconciliation emits nothing while every other
    stage of the flow reports green.

    The single-use discount code is the one value that genuinely crossed the wire: the
    exchange minted it, put it in the permalink, and the order came back carrying it. It is
    not on the ``accepted`` event — the published body there is ``(bid_ref, checkout_token,
    offer)`` — but it IS on ``code_created`` and ``checkout_redirect`` alongside the
    exchange's token, which is why those two kinds are read for keys and for nothing else.
    :func:`discount_codes_of` documents what that key is worth and the three guards that keep
    a weaker key from producing a wrong pair.

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
    # SIX passes, and each split is load-bearing.
    #
    # 0. Read. Identifier keys and discount codes, kept apart, because a code is not an
    #    identifier and the difference decides everything below.
    # 1. Attribute. Which store claims which identifier key, and therefore which store an
    #    event that names none of its own belongs to. Keys are namespaced by store because
    #    `order_id` is a PER-SHOP number: two shops both have order 1001, and joining on it
    #    unscoped merges their orders into one group.
    # 2. Group on IDENTIFIERS ONLY. This is exactly the pre-code behaviour, and it is what
    #    the code links are then checked against. Two keys only become known to be the same
    #    order when some event carries both, which may be the LAST event in the stream — so
    #    nothing may be filed into a group while the groups are still merging.
    # 3. Decide which code links survive (see below).
    # 4. Apply the surviving links.
    # 5. File and emit.
    read: list[tuple[str, Any, tuple[str, ...], tuple[str, ...], str | None]] = []
    key_stores: dict[str, set[str]] = {}
    for event in events:
        kind = str(_field(event, "kind", ""))
        reads_codes = kind in _CODE_BEARING_KINDS
        if kind not in (ACCEPTED_KIND, PIXEL_KIND, WEBHOOK_KIND) and not reads_codes:
            continue
        identifiers = _join_keys(event)
        if not identifiers:
            if kind == WEBHOOK_KIND:
                # Unchanged, and deliberately checked BEFORE any code is consulted. A code is
                # a bridge between two identified things, never an identity of its own: a
                # webhook that names only a discount code cannot say which order it is, so
                # letting the code stand in for an order reference would put a live redeemable
                # code into the emitted `order_ref` — half of the ledger's idempotency key —
                # and let two unrelated orders spell the same one.
                raise ReconciliationInputError(
                    "an order_paid webhook carries no checkout_token and no order_ref, so it "
                    "cannot be joined to the offer it is meant to grade. Dropping it silently "
                    "would let a store escape reconciliation by omitting a field."
                )
            continue
        # The pixel is deliberately NOT a code side. It is a lossy client-side observation
        # (R4), and a key it contributes can MERGE two groups — so honouring a code off the
        # pixel would hand a client-side beacon the power to decide which order gets graded.
        # It costs nothing: the pixel and the webhook already share the platform's own
        # checkout token, and a pixel that joins nothing is `pixel_missing`, which by design
        # is not a blocker.
        codes = discount_codes_of(event) if reads_codes else ()
        store = _store_of(event)
        if store is not None:
            # Identifier keys only. A code key here would let a THIRD party's use of the same
            # code string decide which store an unattributed event belongs to: measured, an
            # unattributed webhook that reconciled cleanly stopped reconciling at all as soon
            # as an unrelated shop's order named `WELCOME10`, because the store vote went from
            # one candidate to two. Store attribution is an identity question.
            for key in identifiers:
                key_stores.setdefault(key, set()).add(store)
        read.append((kind, event, identifiers, codes, store))

    groups = _Groups()
    relevant: list[tuple[str, Any, tuple[str, ...], tuple[str, ...], str]] = []
    for kind, event, identifiers, codes, store in read:
        if store is None:
            # A client-side pixel often knows the checkout token and not the shop. Adopt the
            # store only when exactly one store claims one of its keys; two candidates is
            # precisely the collision this scoping exists to catch, so it stays unattributed
            # rather than being guessed into somebody's order.
            candidates: set[str] = set()
            for key in identifiers:
                candidates |= key_stores.get(key, set())
            store = next(iter(candidates)) if len(candidates) == 1 else None
        scope = store if store is not None else _UNATTRIBUTED_SCOPE
        scoped = _scoped_keys(identifiers, scope)
        groups.union(scoped)
        relevant.append((kind, event, scoped, codes, scope))

    # THE RULE FOR CODE LINKS, in one sentence: a discount code may bridge an offer to an
    # order, and may never merge two orders or two offers.
    #
    # Everything a code is weak at reduces to that. It is 2^40 of `secrets` with no mint-time
    # registry, and the MERCHANT decides which order redeems it and how many codes its
    # `orders/paid` body lists, so a code link is a claim by a party with an interest, not a
    # network-issued identity. Three measured failures, each of which this rule refuses:
    #
    # * one code on two orders — the group would hold two webhooks, `setdefault` would keep
    #   the first, and the second order's overcharge would vanish with no error and no trace;
    # * one ORDER naming two codes, one of them another checkout's — measured, that merged
    #   three identity groups transitively and a 10x overcharge on a DIFFERENT order
    #   disappeared: 2 reconciled events before the code join, 1 after;
    # * one code minted for two checkouts — grading one buyer's order against another's
    #   promise.
    #
    # So the check is on the RESULT, not on each code in isolation: build the graph whose
    # nodes are identifier groups and whose edges are codes, and refuse every code inside any
    # connected component that would end up holding two orders or two offers. Refusing the
    # whole component rather than an arbitrary edge is what keeps the answer independent of
    # the order the events arrived in — an ordering-dependent refusal would make one order's
    # `event_id` depend on the shuffle of the ledger page it was read in.
    identity_of: dict[str, str] = {}
    has_order: set[str] = set()
    has_offer: set[str] = set()
    for kind, _event, scoped, _codes, _scope in relevant:
        root = groups.find(scoped[0])
        identity_of[scoped[0]] = root
        if kind == WEBHOOK_KIND:
            has_order.add(root)
        elif kind == ACCEPTED_KIND:
            has_offer.add(root)

    #: Which identity groups each scoped code links together.
    linked: dict[str, set[str]] = {}
    for _kind, _event, scoped, codes, scope in relevant:
        root = groups.find(scoped[0])
        for code in codes:
            linked.setdefault(_scoped_keys((_code_key(code),), scope)[0], set()).add(root)

    components = _Groups()
    for code_key, roots in linked.items():
        components.union([code_key, *roots])

    all_roots = set(identity_of.values())
    refused: set[str] = set()
    for code_key in linked:
        component = components.find(code_key)
        reached = {root for root in all_roots if components.find(root) == component}
        if len(reached & has_order) > 1 or len(reached & has_offer) > 1:
            refused.add(code_key)

    for code_key, roots in linked.items():
        if code_key not in refused:
            groups.union([code_key, *roots])

    members: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for kind, event, keys, _codes, _scope in relevant:
        root = groups.find(keys[0])
        # A bridge event is filed under its own kind, which nothing below reads. It has done
        # its whole job already — its keys are in the union — and it must not be able to
        # become a promise or a piece of evidence: only `accepted`, `order_paid` and
        # `checkout_pixel` are ever taken back out of a bucket.
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
        # The fallback is the WEBHOOK's own first identifier, never the group's root key.
        # Three things went wrong when it was the root. The root is whichever key union-find
        # happened to keep, so one order's `event_id` changed with the ORDER OF THE PAGE —
        # measured, six permutations of one three-event page produced two different
        # `event_id`s for one order, and `event_id` IS the ledger's idempotency key. The root
        # can also be a discount code, which put a live redeemable code into a published
        # ledger field and let two unrelated orders spell the same `event_id`. And stripping
        # the namespaces back off a root re-introduced the separator this module escapes out
        # of every key. The webhook always has an identifier — a webhook with none is refused
        # above — so this is available, stable, and the order's own.
        fallback = _key_component_decoded(_join_keys(webhook)[0])
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
    * the same is true one epsilon over. The verdict honors on
      ``observed >= promised - DISCOUNT_TOLERANCE``, so ANY promise at or below the tolerance
      is auto-honored whatever the webhook reports — measured, a promise of 0.01% against a
      webhook reporting no discount at all minted a ``fulfilled``. A promise smaller than the
      smallest difference we can measure is not a promise this system can grade, so it is not
      graded. The threshold is the comparison tolerance itself, not a number chosen here.
    * a missing promised PRICE is likewise not gradeable. ``unsupported`` there would
      penalise a store for an offer that carried no price rather than for anything it did.

    Numbers are read through :func:`_number`, which yields ``None`` for anything that is not
    one. This function is documented to accept a whole ledger page, and a payload read back
    out of the ledger holds whatever was stored; a bare ``ValueError`` out of ``float()`` is
    not something a caller's ``except ReconciliationInputError`` would ever catch.
    """
    graded: list[tuple[str, str, str, Any, Any]] = []

    promised_price = _number(payload.get("promised_price"))
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

    promised_discount = _number(payload.get("promised_discount_percentage"))
    if promised_discount is not None and promised_discount > DISCOUNT_TOLERANCE:
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
        if store_id is None:
            # A trust observation is a statement ABOUT a store, and there is no honest store
            # to attribute this one to. `observations_from_events` drops such an event anyway
            # (replay.py: "there is no honest store to attribute it to"), so emitting one only
            # seals a permanent, immutable, never-scored finding into an append-only ledger.
            # The `reconciled` event still records exactly what happened.
            continue
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
    ``observations_from_events`` projects ``{store_id, dim, type, observed_at}`` -- plus an
    optional ``weight`` when the payload names one -- off any event whose payload names a
    ``dim`` and a ``type``. So the translation lives on the EMITTER side: these events are
    shaped to what that consumer already requires, rather than the consumer being asked to
    learn what a ``reconciled`` payload means. Reconciliation emits no ``weight`` (see
    :func:`observations` below on why a machine comparison is worth its full published type
    weight), so these events take the absent-means-1.0 branch.

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
        if store_id is None:
            continue  # see `reconciled_observations` — nothing to attribute the finding to
        order_ref = payload.get("order_ref") or _field(event, "order_ref")
        observed_at = payload.get("observed_at") or _field(event, "ts")
        for field, dimension, observation_type, promised_value, observed_value in _graded_fields(
            payload
        ):
            events.append(
                {
                    "event_id": (
                        f"{OBSERVATION_KIND}:{_id_component(store_id)}"
                        f":{_id_component(order_ref)}:{field}"
                    ),
                    # `observed_at`, not the raw `ts`: it already falls back to it, and a
                    # reconciled event with neither would otherwise emit `ts: None`, which
                    # `normalise_event` refuses — two unappendable events per order instead
                    # of the one that was already unappendable.
                    "ts": observed_at,
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
