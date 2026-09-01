# shopify-stub

A local implementation of **exactly the Shopify surface this system uses**, and nothing
more. It exists because SPEC C9 requires ticket verification to run offline: no Shopify
credential exists in this environment, and none is needed.

Import namespace is `shopify_stub` (the directory keeps its hyphen on disk). The ASGI app is
`shopify_stub.app:app` — that path and attribute are pinned by the root `conftest.py`'s
`shopify_stub_url` fixture and must not move.

```python
from shopify_stub.app import create_app  # a fresh, isolated stub
from shopify_stub.testing import StubClient  # a typed client for driving it
```

## What it implements

### Shopify's own surface

| Route | Method | Notes |
|---|---|---|
| `/admin/api/{version}/graphql.json` | POST | Four root fields; anything else is `undefinedField` |
| `/cart/{variant_id}:{quantity}?discount={code}` | GET | Cart-permalink redemption |

The four root fields are `discountCodeBasicCreate`, `orders`, `webPixelCreate` and
`webhookSubscriptionCreate`. A fifth would be scope creep; asking for one gets Shopify's real
`undefinedField` error rather than a plausible-looking success.

Webhook delivery covers `orders/paid`, `orders/fulfilled` and `refunds/create`, signed with
HMAC-SHA256 over the raw body and carrying the documented `X-Shopify-*` headers.

### The control plane

Everything under `/_stub` is **not Shopify**. The prefix exists so no consumer can mistake a
test affordance for a real endpoint, and so that grepping for `_stub` finds every place
something has coupled itself to the stub rather than to the API.

| Route | Method | Notes |
|---|---|---|
| `/_stub/config` | GET/PUT | The knobs. An unknown key is a 400, never a silent no-op |
| `/_stub/reset` | POST | Wipes data, keeps configuration |
| `/_stub/seed` | POST | Idempotent catalog seeding → `{created, unchanged, updated}` |
| `/_stub/codes` | GET | The code table plus D22's `offer_id` index |
| `/_stub/checkouts/{token}` | GET | Introspection, including *why* a code was ignored |
| `/_stub/checkouts/{token}/complete` | POST | Order + pixel event + `orders/paid` |
| `/_stub/orders/{id}/fulfill` | POST | → `orders/fulfilled` |
| `/_stub/orders/{id}/refund` | POST | → `refunds/create` |
| `/_stub/events` | GET | Emitted pixel events |
| `/_stub/events/suppressed` | GET | Events that did **not** fire, and why |
| `/_stub/webhooks/deliveries` | GET | Every delivery attempt, successful or not |

Plus `GET /healthz`.

## The asymmetry: a lossy pixel and a truthful webhook

This is the point of the whole service, and it is deliberate on both sides.

**The pixel is a sample.** A real web pixel is a browser beacon, lost to content blockers,
consent banners, tab closes and flaky networks — and when it is lost, nothing anywhere
records that it was lost. The stub therefore has a drop-rate knob, and it keeps a
suppression log that real Shopify has no counterpart for, so the loss is *testable* rather
than merely real.

**The webhook is the truth.** Shopify retries a failed delivery for up to 48 hours. The stub
retries until the receiver accepts, records every attempt, and has **no loss knob at all**.
Adding one would let a consumer's test pass while its production reconciliation was wrong,
by making "the webhook never arrived" a normal state. It is not one.

### The documented non-firing modes

`pixel_mode` has three values, and the last two are the non-firing cases the ticket asks to
be documented:

| `pixel_mode` | `pixel_drop_rate` | Result |
|---|---|---|
| `on` | `0.0` | Every checkout emits a full event |
| `on` | `0 < r ≤ 1` | Each event is independently dropped with probability `r` |
| `partial` | any | Events emit with `orderId: null` and `discountApplications: null` |
| `off` | any | **No** event is ever emitted, whatever the drop rate says |

`off` and `drop_rate: 1.0` both emit nothing, and they are **not the same condition**. One
is a pixel that is installed and losing everything; the other is a pixel that is not there —
not installed, blocked, or consent denied. A reconciler that treats "absent" as "lossy"
waits for an event that is never coming; one that treats "lossy" as "absent" stops
reconciling a signal it still has. The stub keeps them apart in the suppression reason
(`dropped` vs `not_firing`), which is the only place the difference is visible at all.

`partial` is the third case: the beacon left before the order id and the discount allocation
were known. The event exists; the join keys do not. A collector must record a visible gap
for it rather than count a conversion.

Set `pixel_seed` and the drop pattern is exactly reproducible, so a test can assert an exact
emitted count instead of a statistical band.

## Join keys

The ledger pins the pixel↔webhook join keys as
`{checkout_token, order_ref, client_id, discount_code}`. Those are the *ledger event's*
snake_case names; the two wire shapes differ, because the stub reproduces what Shopify
actually sends rather than normalising them for the consumer's convenience:

| Join key | Pixel collector payload | Order webhook |
|---|---|---|
| `checkout_token` | `checkoutToken` | `checkout_token` |
| `order_ref` | `orderId` | `admin_graphql_api_id` / `id` |
| `client_id` | `clientId` | `note_attributes[proxyshop_client_id]` |
| `discount_code` | `discountApplications[].code` | `discount_codes[].code` |

The `client_id` row is the one place the stub goes beyond Shopify: Shopify never puts a
web-pixel client id on an order webhook. Carrying it through `note_attributes` is how a real
app propagates its own correlation id through checkout, and without it the four pinned join
keys are not all reachable from the two payloads. It is an **app-level convention, not a
Shopify guarantee**.

## Two divergences from real Shopify, both narrowing

1. **No customer PII, anywhere.** Real `orders/paid` carries a full `customer` object with
   `email`, `phone`, `first_name`, `last_name` and `default_address`; the Web Pixels
   `Checkout` type carries `email`, `phone` and both addresses. The stub emits none of them
   and sends `"customer": null`, the shape a guest checkout produces. SPEC C5 grants this app
   no protected-customer-data scopes, so emitting PII it is not entitled to would let a
   consumer build on a field that is absent in production.
2. **A subset of the keys, never a superset.** Real `orders/paid` has 93 top-level keys; the
   stub emits the ones this system reads. Every key it *does* emit is a documented real key,
   and `tests/test_stub_recordings.py` checks that in both directions.

## Recorded fixtures (D21)

`fixtures/recorded/` holds hand-authored JSON derived from published Shopify documentation.
No live capture was performed and no credential exists here. Every file carries a
`$provenance` header naming the doc URLs, the API version (`2026-07`), and — this is the part
that matters — a `caveats` list stating exactly what the cited pages did **not** confirm.

The honest gaps, collected in one place:

* **The permalink behaviour this stub is graded on is undocumented.** Neither shopify.dev nor
  help.shopify.com says what a cart permalink does with a discount code that is invalid,
  expired, exhausted or non-combinable. The silent no-op is mandated by this ticket's
  acceptance criterion 2 and is consistent with the Storefront API modelling a non-applying
  code as `CartDiscountCode.applicable: false` on an otherwise successful mutation — but that
  is inference, not a quote. Verify it empirically if a dev store ever becomes available.
* `discount_applications[].value_type == "percentage"` is unconfirmed; only `"fixed_amount"`
  appears in the published examples.
* `note_attributes` and `fulfillments` are `[]` in every published webhook sample, so their
  element shapes come from the REST Order resource page instead.
* `webhookSubscriptionCreate` is named nowhere in this repo's planning documents — only the
  three topics are. The mutation is the stub's choice.
* Which fields the "orders query subset" contains is specified nowhere. The subset is the
  stub's design, chosen to mirror the webhook payload field for field.

## Declared limitations

* **The selection set is not honoured.** The stub returns the whole documented node shape
  whatever fields were requested, so its response is a *superset* of a narrowed real
  response, never a differently-shaped one. Implementing selection filtering needs a schema
  and a GraphQL engine, and this repo's dependency manifest carries neither.
* **Single-variant permalinks only.** Shopify's format allows comma-separated variants and
  codes; D22 pins the single form and the stub refuses the multi form explicitly rather than
  half-handling it.
* **No product→variant lookup** (D25). Permalinks are variant-scoped; variants arrive by
  seeding.
* **Webhook delivery is synchronous**, awaited inside the request that triggers it. Real
  Shopify delivers asynchronously, but an async dispatcher would make every consumer test
  race it and need a sleep, and a test that sleeps is flaky on a loaded machine. The
  observable contract — the receiver got the signed payload before anything downstream
  looked — is preserved.
* **The retry budget is 3 attempts**, not Shopify's 19 over 48 hours. The shape is kept; the
  duration is not.

## Running it

```bash
# The tests (149 of them). PROXYSHOP_WORKER is mandatory: the root conftest fails without it.
PROXYSHOP_WORKER=2 ./.venv/bin/python -m pytest services/shopify-stub -q

# As a container — e2e profile only, and NOT verified offline (see compose.yaml).
docker compose --profile e2e up shopify-stub
```
