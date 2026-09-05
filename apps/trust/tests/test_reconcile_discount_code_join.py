"""The offer and the order meet on the single-use discount code, not on a checkout token.

**The defect this closes, measured rather than reasoned about.** In a real Shopify checkout
there are two checkout tokens and they were never the same value. The exchange mints its own
with ``secrets.token_hex(16)`` *after* it has already called the merchant, and transmits it
nowhere — the cart permalink it builds carries ``?discount=<code>`` and nothing else. The
store mints a ``uuid4().hex`` of its own when the cart is visited, and that is the one that
rides onto ``orders/paid``. One S1 run, printed straight out of the ledger it produced::

    accepted.checkout_token             875bdce30a8cd964efd7d4fd3583c1dd
    order_paid.platform_checkout_token  b0ac31a698fa4a9896031f2f36b43c84
    equal?                              False

``reconcile`` joined an order to its offer on exactly those two values, so with the
merchant's own token in place it found no shared key and emitted nothing — with every other
stage of the flow green. The four-case measurement on that run, before this change::

    A. harness events as-is (token overridden)   -> 1 reconciled event
    B. merchant's own token restored (reality)   -> 0 reconciled events
    C. B minus every checkout_pixel event        -> 0 reconciled events
    D. A minus every checkout_pixel event        -> 1 reconciled event

and after it, B and C are 1. (A and D were already 1: case D is the reminder that a dropped
pixel is by design **not** a blocker — ``reconcile`` emits a complete verdict with
``pixel_missing: true`` — so the missing pixel was never the second half of this defect.)

**Why the code, and what it costs.** The single-use code is the one value that genuinely
crossed the wire: the exchange minted it, put it in the permalink, and the order came back
carrying it. It is *not* on the ``accepted`` event — the published body there is
``(bid_ref, checkout_token, offer)`` — so the join runs through ``code_created``, which
carries the exchange's token and the code together and is read for keys and nothing else.

But a discount code is a weaker handle than a token, and every test below that begins
``test_a_code_…`` is about that weakness rather than about the happy path. It is 12
characters of ``secrets`` draw with no mint-time registry, the merchant rather than the
exchange decides which order redeems it, and a false join on a money path is worse than no
join: two orders merged into one group means ``setdefault`` keeps the first webhook and the
second store's overcharge is never graded and never raised. So an ambiguous code joins
NOTHING, and the tests here are the demonstration that it does not merely join wrongly less
often.

Every join test carries a **control** that removes the one field the join now reads and shows
the join collapse, so none of them can pass on a reconciler that would have joined anyway.

Pure data: no clock, no socket, no database. The payload shapes are transcribed from a real
run — the ``orders/paid`` body the shopify-stub signs and sends really does spell the code
``discount_codes: [{"code": …, "amount": …, "type": …}]`` and carries no scalar
``discount_code`` at all, which is why ``contracts.join_key_view()`` reads ``None`` off it.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from apps.trust.src.reconcile import (
    CODE_BRIDGE_KINDS,
    discount_codes_of,
    reconcile,
)

#: The exchange's token, as ``secrets.token_hex(16)`` writes it. Verbatim from an S1 run.
AUTHORIZED_TOKEN = "875bdce30a8cd964efd7d4fd3583c1dd"

#: The store's own, minted independently when the cart was visited. Verbatim from the same
#: run — and the whole point is that it is a different string.
PLATFORM_TOKEN = "b0ac31a698fa4a9896031f2f36b43c84"

#: The single-use code the exchange minted for that checkout: ``PSX-`` + 8 Crockford base32.
CODE = "PSX-XRN6JSN9"

ORDER_REF = "gid://shopify/Order/5500000000001"
STORE = "store-northroast"

OFFER = {
    "product_ref": "prod-northroast-hx",
    "unit_price": 389.0,
    "total_price": 389.0,
    "discount": None,
}


def accepted(
    *,
    token: str = AUTHORIZED_TOKEN,
    store: str = STORE,
    offer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The exchange's ``accepted`` event. Published body: ``(bid_ref, checkout_token, offer)``."""
    return {
        "event_id": f"accepted:{token}",
        "ts": "2026-01-01T00:00:00+00:00",
        "kind": "accepted",
        "store_id": store,
        "payload": {
            "bid_ref": f"bid-{token[:6]}",
            "checkout_token": token,
            "offer": OFFER if offer is None else offer,
        },
    }


def code_created(
    *, token: str = AUTHORIZED_TOKEN, code: str = CODE, store: str = STORE
) -> dict[str, Any]:
    """The bridge. Carries the exchange's token and the code it minted for that checkout."""
    return {
        "event_id": f"code_created:{code}",
        "ts": "2026-01-01T00:00:01+00:00",
        "kind": "code_created",
        "store_id": store,
        "payload": {
            "checkout_token": token,
            "code": code,
            "discount_code": code,
            "expires_at": 1700172800.0,
            "permalink_url": f"https://{store}.example.com/cart/1:1?discount={code}",
        },
    }


def order_paid(
    *,
    token: str = PLATFORM_TOKEN,
    code: str | None = CODE,
    order_ref: str = ORDER_REF,
    total_price: float = 389.0,
    store: str = STORE,
) -> dict[str, Any]:
    """The webhook, with the MERCHANT's own token — which is what reality hands us.

    ``discount_codes`` is Shopify's own spelling and is transcribed from the body the stub
    signs. A scalar ``discount_code`` never appears on it.
    """
    return {
        "event_id": f"order_paid:{order_ref}",
        "ts": "2026-01-01T00:00:02+00:00",
        "kind": "order_paid",
        "store_id": store,
        "order_ref": order_ref,
        "payload": {
            "checkout_token": token,
            "order_ref": order_ref,
            "total_price": total_price,
            "discount_codes": (
                [{"code": code, "amount": "0.00", "type": "percentage"}] if code else []
            ),
        },
    }


def checkout_pixel(
    *, token: str = PLATFORM_TOKEN, code: str | None = None, store: str | None = STORE
) -> dict[str, Any]:
    return {
        "event_id": f"checkout_pixel:{token}",
        "ts": "2026-01-01T00:00:01+00:00",
        "kind": "checkout_pixel",
        "store_id": store,
        "payload": {
            "checkout_token": token,
            "client_id": "49e0d938-8aa7-a81e-cbbf-4f540e904e5d",
            "total_price": 389.0,
            **({"discount_codes": [{"code": code}]} if code else {}),
        },
    }


def without(event: dict[str, Any], key: str) -> dict[str, Any]:
    """The same event with one payload key taken back out — the control."""
    stripped = copy.deepcopy(event)
    stripped["payload"].pop(key, None)
    assert key not in stripped["payload"]
    return stripped


# =====================================================================================
# the premise: the two tokens really are different, and the code really is shared
# =====================================================================================
def test_the_two_checkout_tokens_share_no_value_and_the_code_is_on_both_halves() -> None:
    """If this ever goes red the seam has been closed elsewhere and this file is obsolete."""
    assert AUTHORIZED_TOKEN != PLATFORM_TOKEN
    assert accepted()["payload"]["checkout_token"] == AUTHORIZED_TOKEN
    assert order_paid()["payload"]["checkout_token"] == PLATFORM_TOKEN

    assert discount_codes_of(code_created()) == (CODE,)
    assert discount_codes_of(order_paid()) == (CODE,)
    # …and the accepted event does NOT carry it, which is why a bridge kind is needed at all.
    assert discount_codes_of(accepted()) == ()


def test_contracts_published_aliases_cannot_read_the_code_off_a_real_webhook() -> None:
    """Why the reader had to grow past ``("discount_code", "discountCode", "code")``.

    ``contracts.ledger`` publishes those three spellings and ``join_key_view()`` honours
    exactly them. A real ``orders/paid`` body carries none of the three: the code is inside
    ``discount_codes``, a LIST. So the published reader finds the code on the exchange's half
    of the checkout and never on the merchant's — indistinguishable from no join at all.
    """
    from contracts.ledger import join_key_view

    assert join_key_view(order_paid())["discount_code"] is None
    assert discount_codes_of(order_paid()) == (CODE,)


# =====================================================================================
# the join, with its control
# =====================================================================================
def test_reconcile_joins_the_offer_to_the_order_through_the_single_use_code() -> None:
    """Case B: the merchant's own token in place. 0 reconciled events before, 1 after."""
    emitted = reconcile([accepted(), code_created(), order_paid()])

    assert len(emitted) == 1, "the accepted offer and its webhook did not reach one group"
    payload = emitted[0]["payload"]
    assert payload["order_ref"] == ORDER_REF
    assert payload["checkout_token"] == PLATFORM_TOKEN, "the webhook's own token is authority"
    assert payload["price_honored"] is True and payload["price_comparable"] is True
    assert payload["promised_price"] == 389.0 and payload["observed_price"] == 389.0
    assert payload["pixel_missing"] is True


def test_control_without_the_bridge_the_same_three_events_reconcile_to_nothing() -> None:
    """Remove ``code_created`` and the join collapses — this is the defect itself.

    Not "the verdict changes": the reconciler emits *no event at all*, because the accepted
    offer and the webhook share no key and a group with no accepted offer has no promise.
    """
    assert reconcile([accepted(), order_paid()]) == []


def test_control_without_the_orders_code_the_same_three_events_reconcile_to_nothing() -> None:
    """The other end of the same join: the order stops naming the code it redeemed."""
    assert reconcile([accepted(), code_created(), order_paid(code=None)]) == []


def test_control_without_the_bridges_code_the_join_collapses() -> None:
    """``code_created`` keeps its token but loses the code — the bridge stops bridging."""
    bridge = without(without(code_created(), "code"), "discount_code")
    bridge["payload"].pop("permalink_url")  # the code also appears inside the URL
    assert reconcile([accepted(), bridge, order_paid()]) == []


def test_checkout_redirect_bridges_it_too() -> None:
    """The second kind that carries both values. Either one alone is enough."""
    redirect = {
        "event_id": "checkout_redirect:1",
        "ts": "2026-01-01T00:00:01+00:00",
        "kind": "checkout_redirect",
        "store_id": STORE,
        "payload": {
            "checkout_token": AUTHORIZED_TOKEN,
            "discount_code": CODE,
            "domain_verified": True,
            "permalink_url": f"https://{STORE}.example.com/cart/1:1?discount={CODE}",
        },
    }
    assert len(reconcile([accepted(), redirect, order_paid()])) == 1
    assert set(CODE_BRIDGE_KINDS) == {"code_created", "checkout_redirect"}


def test_an_overcharge_is_caught_only_because_the_code_carried_the_offer_across() -> None:
    """What the join is FOR: the webhook says 519 against a promise of 389."""
    payload = reconcile([accepted(), code_created(), order_paid(total_price=519.0)])[0]["payload"]

    assert payload["price_comparable"] is True
    assert payload["price_honored"] is False
    assert payload["observed_price"] == 519.0 and payload["promised_price"] == 389.0

    # The control: with no bridge the same overcharge is not merely ungraded, it is invisible.
    assert reconcile([accepted(), order_paid(total_price=519.0)]) == []


def test_the_code_is_matched_case_insensitively() -> None:
    """The platform redeems codes case-insensitively, so a lower-cased echo still joins."""
    assert len(reconcile([accepted(), code_created(), order_paid(code=CODE.lower())])) == 1


def test_the_pixel_still_lands_on_the_order_the_code_joined() -> None:
    """A pixel carrying the platform's token joins the webhook, and the group keeps it."""
    payload = reconcile([accepted(), code_created(), checkout_pixel(), order_paid()])[0]["payload"]
    assert payload["pixel_missing"] is False
    assert payload["pixel_price"] == 389.0 and payload["pixel_agrees"] is True


def test_the_order_of_the_stream_does_not_matter() -> None:
    """The bridge may arrive last; a one-pass filer would silently lose the order."""
    import itertools

    stream = [accepted(), code_created(), checkout_pixel(), order_paid()]
    for permutation in itertools.permutations(stream):
        emitted = reconcile(copy.deepcopy(list(permutation)))
        assert len(emitted) == 1, f"lost the order for {[e['kind'] for e in permutation]}"
        assert emitted[0]["payload"]["price_honored"] is True


# =====================================================================================
# the weakness of the key — every one of these is a way a WRONG pair could match
# =====================================================================================
def test_a_code_redeemed_by_two_different_orders_joins_neither_of_them() -> None:
    """The expensive failure, refused rather than guessed.

    A merchant that applies one code to two orders would, without the ambiguity guard, merge
    both into one group; ``members.setdefault`` keeps the FIRST webhook and the second
    order's overcharge is never graded and never raised. Emitting one verdict here would be
    strictly worse than emitting none, because the missing order leaves no trace.

    Note what the assertion is: **zero**, not one. A reconciler that "handles" the collision
    by grading whichever order it saw first passes a ``len(...) == 1`` test and loses money.
    """
    second = order_paid(order_ref="gid://shopify/Order/5500000000002", total_price=999.0)
    emitted = reconcile([accepted(), code_created(), order_paid(), second])

    assert emitted == [], (
        "a code claimed by two orders was used as a join key; one of the two orders is now "
        f"silently ungraded: {[event['payload']['order_ref'] for event in emitted]}"
    )

    # The control: give each order its own code and its own authorized checkout, and BOTH
    # reconcile. So the refusal above is about the collision, not about the second order.
    other_token = "c" * 32
    other_code = "PSX-SECOND01"
    both = reconcile(
        [
            accepted(),
            code_created(),
            order_paid(),
            accepted(token=other_token),
            code_created(token=other_token, code=other_code),
            order_paid(
                token="d" * 32,
                code=other_code,
                order_ref="gid://shopify/Order/5500000000002",
                total_price=999.0,
            ),
        ]
    )
    assert len(both) == 2


def test_a_code_minted_for_two_different_checkouts_joins_neither_of_them() -> None:
    """The same rule on the offer side: one code, two authorized checkouts, no join.

    This is the mint-collision case. The code is 2^40 of ``secrets`` with no registry check,
    so "it cannot happen" is a probability rather than a guarantee, and the failure it would
    cause is grading one buyer's order against another buyer's promise.
    """
    emitted = reconcile(
        [
            accepted(),
            code_created(),
            accepted(token="e" * 32),
            code_created(token="e" * 32),  # same CODE, different checkout
            order_paid(),
        ]
    )
    assert emitted == []

    # The control: one checkout claiming the code, and the same order reconciles.
    assert len(reconcile([accepted(), code_created(), order_paid()])) == 1


def test_a_discount_code_cannot_join_against_a_token_that_spells_the_same_characters() -> None:
    """Key spaces are separate, so a code can only ever match another code.

    Without the namespace, an ``accepted`` whose checkout token happened to BE the string
    ``PSX-XRN6JSN9`` would join an unrelated order that redeemed that code — two different
    fields colliding in one flat key space, which is the same escape hatch the store scope
    exists to close, reached through a different field.
    """
    token_shaped_like_a_code = accepted(token=CODE)
    emitted = reconcile([token_shaped_like_a_code, order_paid()])
    assert emitted == []

    # The control: add the bridge that legitimately ties that token to that code, and the
    # same pair reconciles. So the refusal above is the namespace, not a broken reader.
    assert len(reconcile([token_shaped_like_a_code, code_created(token=CODE), order_paid()])) == 1


def test_two_stores_using_the_same_code_string_do_not_merge() -> None:
    """The code is scoped by store like every other join key."""
    emitted = reconcile(
        [
            accepted(),
            code_created(),
            order_paid(),
            accepted(token="f" * 32, store="store-brightbean"),
            code_created(token="f" * 32, store="store-brightbean"),  # same CODE, other shop
            order_paid(
                token="9" * 32,
                order_ref="gid://shopify/Order/7700000000001",
                store="store-brightbean",
                total_price=999.0,
            ),
        ]
    )

    assert len(emitted) == 2, "one shop's order absorbed the other's"
    by_store = {event["store_id"]: event["payload"] for event in emitted}
    assert by_store[STORE]["observed_price"] == 389.0
    assert by_store["store-brightbean"]["observed_price"] == 999.0
    assert by_store["store-brightbean"]["price_honored"] is False


def test_a_pixel_cannot_pull_an_order_into_a_group_with_a_code() -> None:
    """R4: the pixel is lossy and untrusted, so it never gets to decide what joins what.

    A beacon that names a code belonging to another checkout must not attach itself — or the
    order it names — to that checkout's group. It costs nothing to refuse: the pixel and the
    webhook already share the platform's own token, and an unattached pixel is
    ``pixel_missing``, which by design is not a blocker.
    """
    stray = checkout_pixel(token="a" * 32, code=CODE, store=None)
    emitted = reconcile([accepted(), code_created(), order_paid(), stray])

    assert len(emitted) == 1
    assert emitted[0]["payload"]["pixel_missing"] is True, (
        "a pixel joined an order on a discount code; a client-side beacon now decides which "
        "checkout an order is graded against"
    )


def test_a_bridge_event_is_never_a_promise_and_never_an_outcome() -> None:
    """``code_created`` contributes keys and nothing else.

    Without an ``accepted`` there is no promise, so the order yields nothing — the bridge
    must not stand in for one. And on its own it must not produce an event either.
    """
    assert reconcile([code_created(), order_paid()]) == []
    assert reconcile([code_created()]) == []
    assert reconcile([accepted(), code_created()]) == []


def test_a_webhook_with_only_a_code_is_still_attributable_and_not_an_input_error() -> None:
    """A code IS a join key, so a webhook carrying one is not the keyless case that raises."""
    tokenless = order_paid()
    tokenless["payload"].pop("checkout_token")
    tokenless["payload"].pop("order_ref")
    tokenless.pop("order_ref")

    emitted = reconcile([accepted(), code_created(), tokenless])
    assert len(emitted) == 1
    assert emitted[0]["payload"]["price_honored"] is True


def test_an_order_reference_fallen_back_to_a_code_key_does_not_leak_the_namespace() -> None:
    """When a code is the ONLY key a group has, the emitted ``order_ref`` falls back to it.

    A join key is namespaced twice over — ``{store}\\x1fdiscount_code\\x1f{code}`` — and the
    fallback strips the scope off a root key to name the order. Splitting on only the first
    separator would publish ``discount_code\\x1fPSX-…``: a control character, in a field that
    becomes half of the ledger's idempotency key.
    """
    bridge = code_created()
    bridge["payload"].pop("checkout_token")
    bridge["payload"].pop("permalink_url")
    offer = accepted()
    offer["payload"]["checkout_token"] = None
    offer["payload"]["discount_code"] = CODE  # the offer names only the code
    tokenless = order_paid()
    tokenless["payload"].pop("checkout_token")
    tokenless["payload"].pop("order_ref")
    tokenless.pop("order_ref")

    emitted = reconcile([offer, bridge, tokenless])

    assert len(emitted) == 1
    order_ref = emitted[0]["payload"]["order_ref"]
    assert "\x1f" not in order_ref and "discount_code" not in order_ref
    assert order_ref == CODE


def test_a_webhook_with_no_key_at_all_still_raises() -> None:
    """The pre-existing escape hatch stays closed: silence is not an option."""
    from apps.trust.src.reconcile import ReconciliationInputError

    keyless = order_paid(code=None)
    keyless["payload"].pop("checkout_token")
    keyless["payload"].pop("order_ref")
    keyless.pop("order_ref")

    with pytest.raises(ReconciliationInputError):
        reconcile([accepted(), code_created(), keyless])


# =====================================================================================
# the reader
# =====================================================================================
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"discount_code": CODE}, (CODE,)),
        ({"discountCode": CODE}, (CODE,)),
        ({"code": CODE}, (CODE,)),
        ({"discount_codes": [{"code": CODE, "amount": "0.00"}]}, (CODE,)),
        ({"discountCodes": [CODE]}, (CODE,)),
        ({"discount_applications": [{"type": "discount_code", "code": CODE}]}, (CODE,)),
        ({"discountApplications": [{"code": CODE}]}, (CODE,)),
        ({"discount_codes": []}, ()),
        ({"discount_codes": None}, ()),
        ({"discount_code": ""}, ()),
        ({"discount_code": "  "}, ()),
        ({"discount_code": True}, ()),  # a bool is not a code
        ({"discount_codes": "PSX-NOTALIST"}, ()),  # a string is not a list of codes
        ({}, ()),
    ],
)
def test_the_code_reader_reads_every_spelling_and_refuses_the_non_codes(
    payload: dict[str, Any], expected: tuple[str, ...]
) -> None:
    assert discount_codes_of({"kind": "order_paid", "payload": payload}) == expected


def test_the_reader_de_duplicates_and_keeps_first_seen_order() -> None:
    """A real body names the code twice — once in ``discount_codes``, once in the
    applications — and one order that applied two codes must yield both, in order."""
    event = {
        "kind": "order_paid",
        "payload": {
            "discount_codes": [{"code": CODE}, {"code": "PSX-SECOND01"}],
            "discount_applications": [{"code": CODE}],
        },
    }
    assert discount_codes_of(event) == (CODE, "PSX-SECOND01")


def test_reconcile_is_deterministic_across_two_runs_over_one_stream() -> None:
    """The emitted event is the ledger's, so a second pass must produce the same bytes."""
    stream = [accepted(), code_created(), checkout_pixel(), order_paid()]
    assert reconcile(copy.deepcopy(stream)) == reconcile(copy.deepcopy(stream))
