/**
 * What the pixel puts on the wire, graded against recordings this lane did not write.
 *
 * `services/shopify-stub/fixtures/recorded/web_pixel_checkout_completed.json` carries both halves
 * of the transform — the documented Web Pixels `checkout_completed` object and the collector body
 * that must come out of it — and `webhook_orders_paid.json` carries the authoritative record of
 * the same purchase. Holding the emitter to those two is the difference between a test and a
 * restatement: neither file is editable from this ticket's scope, so the expected shapes cannot
 * be tuned to whatever the code happens to produce.
 */
import { describe, expect, it } from 'vitest'

import {
  buildCollectorBody,
  COLLECTOR_BODY_KEYS,
  normalizeOrderRef,
  ORDER_GID_PREFIX,
} from '../src/beacon.ts'
import {
  degradedCheckoutCompleted,
  recordedCheckoutCompleted,
  recordedCollectorPayload,
  recordedDegradedCollectorPayload,
  recordedOrdersPaidWebhook,
} from './fixtures.ts'

/** `structuredClone` of the recorded event, so a mutation in one test cannot reach another. */
function checkoutCompleted(): Record<string, unknown> {
  return structuredClone(recordedCheckoutCompleted) as Record<string, unknown>
}

function checkoutOf(event: Record<string, unknown>): Record<string, unknown> {
  return (event['data'] as Record<string, unknown>)['checkout'] as Record<string, unknown>
}

describe('the collector body built from a checkout_completed event', () => {
  it('is exactly the body the recorded Shopify surface says the collector receives', () => {
    expect(buildCollectorBody(recordedCheckoutCompleted)).toEqual(recordedCollectorPayload)
  })

  it('carries the documented gap when the beacon raced order creation', () => {
    // The documented non-firing-ish case: the event fires, the join keys are not there yet.
    // `orderId: null` and `discountApplications: null` are what a reconciler must see, because
    // `accept_pixel_event` publishes the absent join keys as `gaps` and R4 leaves the webhook
    // authoritative. Inventing an order id here would turn a visible gap into a false conversion.
    expect(buildCollectorBody(degradedCheckoutCompleted())).toEqual(
      recordedDegradedCollectorPayload,
    )
  })

  it('carries only the four published keys, however much the event carries', () => {
    expect(Object.keys(buildCollectorBody(recordedCheckoutCompleted) ?? {}).sort()).toEqual(
      [...COLLECTOR_BODY_KEYS].sort(),
    )
  })

  it('reports nothing at all when there is no checkout token to join on', () => {
    // `accept_pixel_event` answers a tokenless body with a 400 — "it can never be joined to an
    // order". Emitting one would be a guaranteed-rejected POST out of a shopper's browser.
    const event = checkoutCompleted()
    delete checkoutOf(event)['token']
    expect(buildCollectorBody(event)).toBeNull()

    checkoutOf(event)['token'] = '   '
    expect(buildCollectorBody(event)).toBeNull()
  })

  it('does not throw on any shape the sandbox could hand it', () => {
    for (const junk of [null, undefined, 42, 'checkout_completed', [], {}, { data: 7 }]) {
      expect(buildCollectorBody(junk)).toBeNull()
    }
    const event = checkoutCompleted()
    ;(event['data'] as Record<string, unknown>)['checkout'] = { token: 'ck-1', order: 'nope' }
    expect(buildCollectorBody(event)).toEqual({
      clientId: recordedCheckoutCompleted['clientId'],
      checkoutToken: 'ck-1',
      orderId: null,
      discountApplications: null,
    })
  })
})

describe('the join keys R4 reconciles against the authoritative webhook', () => {
  // The two recordings describe the same purchase. If the pixel spells a join key differently
  // from the webhook, the reconciliation in T-061 finds nothing and reports it as a missing
  // pixel — a silent failure that looks exactly like a shopper who blocked the beacon.
  const body = buildCollectorBody(recordedCheckoutCompleted)

  it('spells the order reference the way the webhook does', () => {
    expect(body?.orderId).toBe(recordedOrdersPaidWebhook['admin_graphql_api_id'])
    // And that is not the spelling the event itself carried, which is the whole point.
    const rawOrderId = (
      checkoutOf(recordedCheckoutCompleted as Record<string, unknown>)['order'] as Record<
        string,
        unknown
      >
    )['id']
    expect(body?.orderId).not.toBe(rawOrderId)
    expect(body?.orderId).toBe(`${ORDER_GID_PREFIX}${String(rawOrderId)}`)
  })

  it('carries the same checkout token the webhook carries', () => {
    expect(body?.checkoutToken).toBe(recordedOrdersPaidWebhook['checkout_token'])
  })

  it('carries the same discount code the webhook carries', () => {
    const webhookCodes = recordedOrdersPaidWebhook['discount_codes'] as { code: string }[]
    expect(body?.discountApplications?.[0]?.code).toBe(webhookCodes[0]?.code)
  })

  it('carries the client id the webhook only has because the app propagated it', () => {
    const attributes = recordedOrdersPaidWebhook['note_attributes'] as {
      name: string
      value: string
    }[]
    const propagated = attributes.find((entry) => entry.name === 'proxyshop_client_id')
    expect(body?.clientId).toBe(propagated?.value)
  })
})

describe('the order reference', () => {
  it('prefixes a bare numeric id and leaves a GID alone', () => {
    expect(normalizeOrderRef('5500000000001')).toBe(`${ORDER_GID_PREFIX}5500000000001`)
    expect(normalizeOrderRef('gid://shopify/Order/5500000000001')).toBe(
      'gid://shopify/Order/5500000000001',
    )
  })

  it('never manufactures a GID out of a shape it does not recognise', () => {
    // Wrapping an unknown id would assert a normalisation that did not happen, and the wrong
    // `order_ref` joins to the wrong order rather than to none.
    expect(normalizeOrderRef('#1001')).toBe('#1001')
    expect(normalizeOrderRef('')).toBeNull()
    expect(normalizeOrderRef(null)).toBeNull()
    expect(normalizeOrderRef(5500000000001)).toBeNull()
  })
})

describe('discount applications', () => {
  it('lifts the code out of `title`, which is where the Web Pixels API puts it', () => {
    // The Web Pixels `DiscountApplication` type has no `code` member at all.
    const application = (
      checkoutOf(recordedCheckoutCompleted as Record<string, unknown>)[
        'discountApplications'
      ] as Record<string, unknown>[]
    )[0]
    expect(application?.['code']).toBeUndefined()
    expect(buildCollectorBody(recordedCheckoutCompleted)?.discountApplications?.[0]?.code).toBe(
      application?.['title'],
    )
  })

  it('refuses to call an automatic discount title a code', () => {
    // `title` is the code only for a DISCOUNT_CODE application. For an AUTOMATIC one it is a
    // customer-facing name, and the collector lifts whatever `code` it finds straight into the
    // stored `discount_code` join key — so shipping "Summer sale" as a code fabricates the
    // fourth join key. A fabricated join key is worse than an absent one: an absent one is
    // published as a gap.
    const event = checkoutCompleted()
    checkoutOf(event)['discountApplications'] = [
      { type: 'AUTOMATIC', title: 'Summer sale', value: { percentage: 15 } },
    ]
    const applications = buildCollectorBody(event)?.discountApplications
    expect(applications).toEqual([{ value: 15, type: 'automatic' }])
    expect(JSON.stringify(applications)).not.toContain('Summer sale')
  })

  it('carries the number out of either arm of Shopify’s value union', () => {
    const event = checkoutCompleted()
    checkoutOf(event)['discountApplications'] = [
      { type: 'DISCOUNT_CODE', title: 'PSX-FIXED', value: { amount: 7.5, currencyCode: 'USD' } },
    ]
    expect(buildCollectorBody(event)?.discountApplications).toEqual([
      { code: 'PSX-FIXED', value: 7.5, type: 'code' },
    ])
  })

  it('keeps an empty list distinct from an unknown one', () => {
    // `[]` is "this checkout had no discount". `null` is "the beacon does not know". Collapsing
    // them would turn a gap into a fact.
    const event = checkoutCompleted()
    checkoutOf(event)['discountApplications'] = []
    expect(buildCollectorBody(event)?.discountApplications).toEqual([])

    checkoutOf(event)['discountApplications'] = null
    expect(buildCollectorBody(event)?.discountApplications).toBeNull()
  })
})

describe('buyer identity (R5, C5)', () => {
  // The Web Pixels `Checkout` type really does carry email, phone, billingAddress,
  // shippingAddress and order.customer; the sandbox hands them to the callback whether the app
  // wants them or not. The body is BUILT from an allowlist for exactly this reason, so adding a
  // field to the event can never add one to the beacon.
  const PII = {
    email: 'shopper@example.com',
    phone: '+15555550123',
    firstName: 'Ada',
    lastName: 'Lovelace',
    address1: '12 Marylebone Road',
    city: 'London',
    zip: 'NW1 5LA',
  }

  function eventCarryingPii(): Record<string, unknown> {
    const event = checkoutCompleted()
    const checkout = checkoutOf(event)
    checkout['email'] = PII.email
    checkout['phone'] = PII.phone
    checkout['billingAddress'] = {
      firstName: PII.firstName,
      lastName: PII.lastName,
      address1: PII.address1,
      city: PII.city,
      zip: PII.zip,
      phone: PII.phone,
    }
    checkout['shippingAddress'] = { ...(checkout['billingAddress'] as object) }
    checkout['smsMarketingPhone'] = PII.phone
    ;(checkout['order'] as Record<string, unknown>)['customer'] = {
      id: 'gid://shopify/Customer/99',
      email: PII.email,
      firstName: PII.firstName,
      lastName: PII.lastName,
    }
    return event
  }

  it('carries no buyer identity anywhere in the emitted body', () => {
    const wire = JSON.stringify(buildCollectorBody(eventCarryingPii()))
    for (const [field, value] of Object.entries(PII)) {
      expect(wire, `the beacon leaked ${field}`).not.toContain(value)
    }
    for (const key of ['email', 'phone', 'billingAddress', 'shippingAddress', 'customer']) {
      expect(wire, `the beacon carries a "${key}" member`).not.toContain(key)
    }
  })

  it('emits the same body it would have emitted if the PII had never been there', () => {
    // The strongest statement available: the presence of customer data changes nothing about
    // what leaves the browser.
    expect(buildCollectorBody(eventCarryingPii())).toEqual(
      buildCollectorBody(recordedCheckoutCompleted),
    )
  })
})
