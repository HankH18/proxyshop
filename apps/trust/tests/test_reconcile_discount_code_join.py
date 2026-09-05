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
    ReconciliationInputError,
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
    """``code_created`` keeps its token but loses the code — the bridge stops bridging.

    ``permalink_url`` is left in place on purpose, and it still spells the code:
    ``…/cart/1:1?discount=PSX-XRN6JSN9``. The join must not be recoverable from it. Mining a
    key out of a URL would mean reading a *rendered* field rather than a declared one, and a
    permalink is a string a store can put anything into.
    """
    bridge = without(without(code_created(), "code"), "discount_code")
    assert CODE in bridge["payload"]["permalink_url"]

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
    order's overcharge is never graded and never raised. Measured on exactly this page, with
    the guard neutralised::

        1 reconciled event
        order_ref='gid://shopify/Order/5500000000001'  observed_price=389.0  honored=True

    — the honest first order, and order 5500000000002 at 999.00 simply gone. Emitting that
    one verdict is strictly worse than emitting none, because the missing order leaves no
    trace, while two paid orders naming one code and no verdict at all is legible.

    Note what the assertion is: **zero**, not one. A reconciler that "handles" the collision
    by grading whichever order it saw first passes a ``len(...) == 1`` test and loses money.

    Each order carries its OWN platform checkout token, because that is what two orders at
    one shop actually look like. Two webhooks that share an identifier are not two orders —
    they are one order delivered twice, which is the redelivery case below.
    """
    second = order_paid(
        token="2" * 32, order_ref="gid://shopify/Order/5500000000002", total_price=999.0
    )
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


def test_a_webhook_that_names_only_a_code_is_refused_not_attributed() -> None:
    """A code is a BRIDGE between two identified things, never an identity of its own.

    This assertion is the reverse of the one that first stood here, and the reversal is the
    finding. Letting a code stand in for a missing order reference looked generous and was
    three defects at once, each measured on this exact page:

    * the emitted ``order_ref`` — half of ``event_id``, the ledger's idempotency key — became
      the live redeemable discount code, published into a field the repo keeps a whole
      redaction module to keep codes out of;
    * two unrelated orders could then spell the SAME ``event_id``, so the second one's append
      is a silent no-op and its overcharge is gone;
    * and a second keyless webhook naming the same code was indistinguishable from the first,
      so the ambiguity guard could not see it — one order graded, the other dropped, where
      the pre-change code had raised loudly.

    The refusal is unchanged from before the discount-code join, and it is now checked BEFORE
    any code is read so it cannot depend on what else is on the page.
    """
    tokenless = order_paid()
    tokenless["payload"].pop("checkout_token")
    tokenless["payload"].pop("order_ref")
    tokenless.pop("order_ref")
    assert discount_codes_of(tokenless) == (CODE,), "the code is there; it is just not an id"

    with pytest.raises(ReconciliationInputError):
        reconcile([accepted(), code_created(), tokenless])


def test_two_keyless_webhooks_naming_one_code_cannot_be_told_apart_so_neither_is_guessed() -> None:
    """The reason the refusal above must come first. Measured, when it did not::

        1 reconciled event   order_ref='...T1'  observed_price=100.0  price_honored=True

    — one order graded, and a second order billed 9999.00 gone without an error, because two
    webhooks carrying nothing but a shared code have identical join keys and the guard that
    counts distinct orders counted one.
    """
    first = order_paid(total_price=389.0)
    second = order_paid(total_price=9999.0)
    for webhook in (first, second):
        webhook["payload"].pop("checkout_token")
        webhook["payload"].pop("order_ref")
        webhook.pop("order_ref")

    with pytest.raises(ReconciliationInputError):
        reconcile([accepted(), code_created(), first, second])


def test_the_emitted_order_reference_is_the_webhooks_own_never_the_groups_root() -> None:
    """A webhook with a token but no order reference names itself, not whatever key won.

    The root of a union-find group is whichever key happened to survive, so deriving the
    published ``order_ref`` from it made one order's ``event_id`` depend on the ORDER OF THE
    PAGE — measured, six permutations of one three-event page produced two different
    ``event_id`` values for one order — and let a discount code become the reference.
    """
    import itertools

    webhook = order_paid()
    webhook["payload"].pop("order_ref")
    webhook.pop("order_ref")
    page = [accepted(), code_created(), webhook]

    seen = set()
    for permutation in itertools.permutations(page):
        (emitted,) = reconcile(copy.deepcopy(list(permutation)))
        seen.add((emitted["event_id"], emitted["payload"]["order_ref"]))

    assert seen == {(f"reconciled:{STORE}:{PLATFORM_TOKEN}", PLATFORM_TOKEN)}, (
        f"one order produced more than one identity across permutations: {sorted(seen)}"
    )
    assert CODE not in str(seen), "a live discount code reached a published ledger field"


def test_a_webhook_with_no_key_at_all_still_raises() -> None:
    """The pre-existing escape hatch stays closed: silence is not an option."""
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
        ({"discount_codes": [["PSX-NESTED1"]]}, ()),  # nor is a nested list a code
        ({"discount_codes": [{"amount": "0.00"}]}, ()),  # an entry with no code
        ({"discount_code": {"code": CODE}}, ()),  # a mapping is not a scalar code
        ({"discount_codes": [None, CODE]}, (CODE,)),  # a hole does not stop the read
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


# =====================================================================================
# the separator, which three different components now share
# =====================================================================================
SEP = "\x1f"


def test_a_store_id_cannot_be_spelled_to_forge_another_shops_code_key() -> None:
    """The composed key is ``{store}\\x1f[discount_code\\x1f]{value}``, and all three parts
    are strings somebody else chose.

    The discount-code namespace made this reachable, which is why the escaping arrived with
    it: a store calling itself ``store-northroast\\x1fdiscount_code`` and reporting an order
    whose *checkout token* is the literal string ``PSX-XRN6JSN9`` composes exactly the key the
    honest shop's *discount code* composes. Measured with the escape removed, on this page::

        1 reconciled event
        store_id='store-northroast\\x1fdiscount_code'
        order_ref='gid://shopify/Order/9900000000001'   <- the forged shop's order
        product_ref='prod-northroast-hx'  promised_price=389.0   <- the honest shop's promise
        observed_price=999.0  price_honored=False

    Two frauds in one: the forged order is graded against a promise it never made, and
    ``store-northroast``'s own honest order disappears from the output entirely — the group
    already held a webhook, so ``setdefault`` kept the forged one. Note the count is 1 either
    way, so ``len(emitted) == 1`` is not the assertion that catches this.
    """
    forged = order_paid(
        token=CODE,  # a "checkout token" that is really another shop's code
        code=None,
        order_ref="gid://shopify/Order/9900000000001",
        total_price=999.0,
        store=f"{STORE}{SEP}discount_code",
    )
    # The forged webhook arrives FIRST, which is the ordering that steals rather than merely
    # self-destructs: whichever webhook reaches the group first is the one that gets graded.
    emitted = reconcile([forged, accepted(), code_created(), order_paid()])

    assert [event["store_id"] for event in emitted] == [STORE], (
        "a forged store_id reached another shop's checkout: "
        f"{[(e['store_id'], e['payload']['order_ref']) for e in emitted]}"
    )
    assert emitted[0]["payload"]["order_ref"] == ORDER_REF, (
        "the honest shop's own order was displaced by the forged one"
    )
    assert emitted[0]["payload"]["observed_price"] == 389.0
    assert emitted[0]["payload"]["price_honored"] is True


def test_the_separator_and_its_escape_survive_a_round_trip_into_a_published_field() -> None:
    """A value carrying the separator, or its escape sequence, still names its own order.

    ``%`` is escaped first so a code that legitimately contains ``%1F`` cannot spell the
    escape of a real separator — the same construction ``event_id`` already uses for ``:``.
    """
    for hostile in (f"PSX{SEP}HOSTILE", "PSX%1FHOSTILE", "PSX%25HOSTILE"):
        emitted = reconcile(
            [
                accepted(),
                code_created(code=hostile),
                order_paid(code=hostile),
            ]
        )
        assert len(emitted) == 1, f"{hostile!r} lost its order"
        assert emitted[0]["payload"]["order_ref"] == ORDER_REF

        # …and it does not collide with the honest code, which is a different string.
        both = reconcile(
            [
                accepted(),
                code_created(code=hostile),
                order_paid(code=hostile),
                accepted(token="7" * 32),
                code_created(token="7" * 32, code=CODE),
                order_paid(
                    token="8" * 32,
                    code=CODE,
                    order_ref="gid://shopify/Order/5500000000002",
                    total_price=999.0,
                ),
            ]
        )
        assert len(both) == 2, f"{hostile!r} merged with {CODE!r}"


def test_no_published_field_leaks_the_internal_separator() -> None:
    """Whatever the input spells, the emitted event stays free of control characters."""
    emitted = reconcile(
        [
            accepted(store=f"{STORE}{SEP}x"),
            code_created(store=f"{STORE}{SEP}x"),
            order_paid(store=f"{STORE}{SEP}x"),
        ]
    )
    assert len(emitted) == 1
    payload = emitted[0]["payload"]
    assert SEP not in str(payload["order_ref"])
    assert SEP not in str(payload["checkout_token"])


def orphaned_code_created(*, code: str, store: str = STORE) -> dict[str, Any]:
    """The ``code_created`` the exchange files when it REFUSES a checkout it already minted for.

    Transcribed from ``apps/exchange/src/accept/offer.py``'s ``_orphan_record``: it carries
    ``orphaned: True`` / ``revocation_required: True``, a ``bid_ref`` and a ``fingerprint``,
    and — the part that matters here — **no ``checkout_token``**, because no checkout was
    authorized. No ``accepted`` and no ``checkout_redirect`` accompany it.
    """
    return {
        "event_id": f"code_created:orphan:{code}",
        "ts": "2026-01-01T00:00:01+00:00",
        "kind": "code_created",
        "store_id": store,
        "payload": {
            "code": code,
            "permalink_url": f"https://{store}.example.com/cart/1:1?discount={code}",
            "expires_at": 1700172800.0,
            "orphaned": True,
            "revocation_required": True,
            "bid_ref": "bid-refused",
            "provider": "shopify",
            "fingerprint": "sha256:refused",
        },
    }


def test_a_redeemed_orphan_code_does_not_manufacture_a_verdict() -> None:
    """A refused checkout whose live code was spent anyway is graded against NOTHING.

    Admitting ``code_created`` as a bridge means the orphan record the refusal path files is
    now read too, so this is a consequence of the join and not a hypothetical. It is safe for
    a structural reason worth pinning rather than trusting: the orphan carries no
    ``checkout_token`` and no ``accepted`` accompanies it, so its group holds a webhook and no
    promise — and ``reconcile`` emits nothing for a group with no promise. Grading here would
    invent a promise out of a checkout the exchange itself refused.
    """
    orphan_code = "PSX-ORPHAN01"
    assert reconcile([orphaned_code_created(code=orphan_code), order_paid(code=orphan_code)]) == []


def test_an_orphan_code_cannot_reach_a_different_checkouts_promise() -> None:
    """The refused bid's code must not attach its order to the offer that was accepted."""
    orphan_code = "PSX-ORPHAN01"
    emitted = reconcile(
        [
            accepted(),
            code_created(),
            order_paid(),
            orphaned_code_created(code=orphan_code),
            order_paid(
                token="6" * 32,
                code=orphan_code,
                order_ref="gid://shopify/Order/6600000000001",
                total_price=999.0,
            ),
        ]
    )

    assert len(emitted) == 1, (
        "the refused checkout's order reached a promise: "
        f"{[(e['payload']['order_ref'], e['payload']['observed_price']) for e in emitted]}"
    )
    assert emitted[0]["payload"]["order_ref"] == ORDER_REF
    assert emitted[0]["payload"]["observed_price"] == 389.0


# =====================================================================================
# the rule, stated once: a code may BRIDGE an offer to an order, and may never MERGE two
# orders or two offers. Each test below is a way the second half was measured to fail.
# =====================================================================================
def test_an_order_that_names_two_codes_cannot_swallow_another_order() -> None:
    """The transitive merge, and the worst regression the join first introduced.

    A merchant decides how many codes its ``orders/paid`` body lists, and it knows every code
    the exchange issued to it. Listing a second checkout's code links three identity groups
    into one component; the component then holds two orders, ``setdefault`` keeps the first
    webhook, and the OTHER order's overcharge disappears. Measured before the rule::

        with the code join:    1 reconciled event   R1  100.0  honored
        without the code join: 2 reconciled events  R1  100.0  honored
                                                    R2  500.0  NOT honored   <- the 10x

    So the check is on the component, not on each code alone, and a component that would hold
    two orders loses every one of its code links rather than an arbitrary one — which also
    keeps the answer independent of the order the page arrived in.
    """
    other_token = "3" * 32
    other_code = "PSX-SECOND01"
    emitted = reconcile(
        [
            accepted(),
            accepted(token=other_token),
            code_created(),
            code_created(token=other_token, code=other_code),
            order_paid(token=AUTHORIZED_TOKEN, code=CODE),
            # the second order names ONLY its own checkout, and is billed 10x its promise
            order_paid(
                token=other_token,
                code=None,
                order_ref="gid://shopify/Order/5500000000002",
                total_price=3890.0,
            ),
        ]
    )
    by_ref = {event["payload"]["order_ref"]: event["payload"] for event in emitted}
    assert len(emitted) == 2, f"an order was swallowed: {sorted(by_ref)}"
    assert by_ref["gid://shopify/Order/5500000000002"]["price_honored"] is False

    # Now the first order lists the second checkout's code as well. Both orders must still be
    # graded — and each against its OWN promise.
    greedy = reconcile(
        [
            accepted(),
            accepted(token=other_token),
            code_created(),
            code_created(token=other_token, code=other_code),
            {
                **order_paid(token=AUTHORIZED_TOKEN),
                "payload": {
                    **order_paid(token=AUTHORIZED_TOKEN)["payload"],
                    "discount_codes": [{"code": CODE}, {"code": other_code}],
                },
            },
            order_paid(
                token=other_token,
                code=None,
                order_ref="gid://shopify/Order/5500000000002",
                total_price=3890.0,
            ),
        ]
    )
    greedy_by_ref = {event["payload"]["order_ref"]: event["payload"] for event in greedy}
    assert len(greedy) == 2, (
        f"listing a second checkout's code swallowed an order: {sorted(greedy_by_ref)}"
    )
    assert greedy_by_ref["gid://shopify/Order/5500000000002"]["price_honored"] is False, (
        "the 10x overcharge on the OTHER order stopped being graded"
    )


def test_a_third_partys_use_of_the_same_code_string_cannot_blind_a_webhook() -> None:
    """Store attribution is an identity question, so a code gets no vote in it.

    A real ``orders/paid`` names the shop in a header, not the body, so an ingested webhook
    can legitimately arrive with no ``store_id`` and adopt one from the checkout token it
    shares with the offer. When code keys counted toward that vote, an unrelated shop running
    its own ``WELCOME10`` was enough to make the vote ambiguous — measured, an order that
    reconciled to a 900-against-100 overcharge stopped reconciling at all, with no error.
    """
    unattributed = order_paid(token=AUTHORIZED_TOKEN, total_price=3890.0, store=None)
    unattributed["store_id"] = None
    unattributed["payload"]["discount_codes"] = [{"code": "WELCOME10"}]
    elsewhere = order_paid(
        token="4" * 32,
        code="WELCOME10",
        order_ref="gid://shopify/Order/7700000000001",
        store="store-brightbean",
    )

    (event,) = reconcile([accepted(), unattributed, elsewhere])
    assert event["payload"]["order_ref"] == ORDER_REF
    assert event["payload"]["price_honored"] is False


def test_one_order_delivered_twice_is_one_order() -> None:
    """Shopify redelivers ``orders/paid``, and an append-only ledger keeps both copies.

    The two copies can expose different key spellings — one with the event-level ``order_ref``,
    the redelivery with only ``order_id`` in the body. Both name the same order, so they are
    one identity and the shared code is not "two orders". When identity was the raw key tuple
    instead, a redelivered webhook made the code ambiguous and the overcharge vanished.
    """
    redelivery = {
        "event_id": "order_paid:redelivered",
        "ts": "2026-01-01T00:00:03+00:00",
        "kind": "order_paid",
        "store_id": STORE,
        "payload": {
            "order_id": ORDER_REF,
            "total_price": 3890.0,
            "discount_codes": [{"code": CODE}],
        },
    }
    (event,) = reconcile([accepted(), code_created(), order_paid(total_price=3890.0), redelivery])
    assert event["payload"]["order_ref"] == ORDER_REF
    assert event["payload"]["price_honored"] is False


# =====================================================================================
# the discount verdict on the body a real merchant actually sends
# =====================================================================================
def real_order_paid(*, percentage: float, total: float = 80.0) -> dict[str, Any]:
    """The ``orders/paid`` body ``services/shopify-stub/src/orders.py`` signs and sends.

    Note ``type: "discount_code"`` and ``value_type: "percentage"``: in Shopify's REST body
    ``type`` is the APPLICATION kind, and the value kind is ``value_type``. In the camelCase
    shape this repo's own producers emit, ``type`` is the value kind. Both are real.
    """
    return {
        "event_id": "order_paid:real",
        "ts": "2026-01-01T00:00:02+00:00",
        "kind": "order_paid",
        "store_id": STORE,
        "order_ref": ORDER_REF,
        "payload": {
            "checkout_token": PLATFORM_TOKEN,
            "order_ref": ORDER_REF,
            "total_price": f"{total:.2f}",
            "discount_codes": [{"code": CODE, "amount": "0.00", "type": "percentage"}],
            "discount_applications": [
                {
                    "target_type": "line_item",
                    "type": "discount_code",
                    "value": f"{percentage}",
                    "value_type": "percentage",
                    "allocation_method": "across",
                    "target_selection": "all",
                    "code": CODE,
                }
            ],
        },
    }


def promised_twenty_percent() -> dict[str, Any]:
    return accepted(
        offer={
            "product_ref": "prod-northroast-hx",
            "unit_price": 80.0,
            "total_price": 80.0,
            "discount": {"type": "percentage", "value": 20.0},
        }
    )


@pytest.mark.parametrize(
    ("label", "given", "expected_type", "expected_honored"),
    [
        ("the store gave the promised 20%", 20.0, "fulfilled", True),
        ("the store gave nothing at all", 0.0, "contradicted", False),
    ],
)
def test_the_discount_verdict_grades_on_the_body_a_real_merchant_sends(
    label: str, given: float, expected_type: str, expected_honored: bool
) -> None:
    """Reading only ``discountApplications`` made an honest store and a cheat identical.

    This is the defect the join unmasked rather than caused: before it, a real body never
    reached a comparison at all. Measured with only the camelCase spelling honoured, BOTH
    rows below came back ``observed_discount_percentage: None``, ``discount_comparable:
    False`` and translated to ``unsupported`` (0.5) — so the store that kept its promise was
    penalised and the store that broke it escaped the 2.0 ``contradicted``.
    """
    from apps.trust.src.reconcile import reconciled_observations

    (event,) = reconcile(
        [promised_twenty_percent(), code_created(), real_order_paid(percentage=given)]
    )
    payload = event["payload"]
    assert payload["observed_discount_percentage"] == given, label
    assert payload["discount_comparable"] is True, label
    assert payload["discount_honored"] is expected_honored, label

    types = [
        observation["type"]
        for observation in reconciled_observations(event)
        if observation["dim"] == "discount_honored"
    ]
    assert types == [expected_type], f"{label}: {types}"


def test_the_merchant_still_chooses_which_of_its_own_promises_it_is_graded_against() -> None:
    """The limit of the code as a handle, pinned so it is a known property and not a surprise.

    The exchange issues the code; the MERCHANT decides which order redeems it and which codes
    its webhook lists. A store that omits its order's real code and names a different one of
    its OWN checkouts is indistinguishable, from the ledger, from a store whose buyer really
    did use that code — so it is graded against that checkout's promise. Below, an order
    billed 389.00 escapes its own 100.00 promise by naming the code of a 389.00 one.

    Nothing in reconciliation can close this: the exchange never observes redemption, and the
    only party that does is the one with the incentive. Closing it needs the merchant's own
    redemption register (``apps/merchant/svc/src/codes/redemption.py``) to be the thing that
    reports which order redeemed which code. What IS closed is the store's ability to reach
    ANOTHER shop's promise (the store scope) or to make a second order disappear while doing
    it (the component rule) — both tested above.
    """
    cheap_token = "5" * 32
    cheap_code = "PSX-CHEAP001"
    dishonest = order_paid(token="7" * 32, code=CODE, total_price=389.0)

    (event,) = reconcile(
        [
            accepted(),  # promises 389.00, code CODE
            code_created(),
            accepted(
                token=cheap_token,
                offer={
                    "product_ref": "p",
                    "unit_price": 100.0,
                    "total_price": 100.0,
                    "discount": None,
                },
            ),
            code_created(token=cheap_token, code=cheap_code),
            dishonest,
        ]
    )
    assert event["payload"]["promised_price"] == 389.0
    assert event["payload"]["price_honored"] is True
    assert event["payload"]["bid_ref"] == accepted()["payload"]["bid_ref"], (
        "the order was graded against the promise whose code it named — if this changes, the "
        "redemption oracle this docstring asks for has landed and this test should say so"
    )
