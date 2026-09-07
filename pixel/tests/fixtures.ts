/**
 * The recorded Shopify surfaces these tests grade the pixel against.
 *
 * Both files live under `services/shopify-stub/fixtures/recorded/` and were authored by the stub
 * lane, not by this one. That is the point: a transform graded against a shape its own author
 * wrote down grades nothing. `web_pixel_checkout_completed.json` carries the documented Web
 * Pixels `checkout_completed` object AND the collector body the extension is supposed to produce
 * from it, and `webhook_orders_paid.json` carries the authoritative side of the same purchase, so
 * the D24 join can be checked across two independently written recordings.
 *
 * Missing or unreadable fixtures throw here rather than skipping a test. A skipped test is a
 * green run that graded nothing, which is the failure mode this whole file exists to replace.
 */
import { readFileSync } from 'node:fs'

const RECORDINGS = new URL('../../services/shopify-stub/fixtures/recorded/', import.meta.url)

function readRecording(name: string): Record<string, unknown> {
  const url = new URL(name, RECORDINGS)
  let raw: string
  try {
    raw = readFileSync(url, 'utf8')
  } catch (cause) {
    throw new Error(
      `the recorded Shopify fixture ${name} is missing at ${url.pathname}; the pixel has ` +
        `nothing independent to be graded against`,
      { cause },
    )
  }
  return JSON.parse(raw) as Record<string, unknown>
}

function section(recording: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = recording[key]
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`the recorded fixture has no object at "${key}"`)
  }
  return value as Record<string, unknown>
}

/**
 * A recorded section with its documentation keys removed.
 *
 * The fixtures annotate themselves in-band with `$note`/`$may_be_empty` keys. Those are commentary
 * for a human reading the file and are not part of any wire shape, so they are stripped before a
 * comparison rather than reproduced by the code under test.
 */
function withoutAnnotations<T>(value: T): T {
  if (Array.isArray(value)) return value.map(withoutAnnotations) as unknown as T
  if (typeof value !== 'object' || value === null) return value
  const stripped: Record<string, unknown> = {}
  for (const [key, member] of Object.entries(value as Record<string, unknown>)) {
    if (key.startsWith('$')) continue
    stripped[key] = withoutAnnotations(member)
  }
  return stripped as unknown as T
}

const CHECKOUT_COMPLETED = readRecording('web_pixel_checkout_completed.json')
const ORDERS_PAID = readRecording('webhook_orders_paid.json')

/** The Web Pixels API `checkout_completed` standard event, as the sandbox delivers it. */
export const recordedCheckoutCompleted = withoutAnnotations(section(CHECKOUT_COMPLETED, 'payload'))

/** The collector body the stub lane says this extension must produce from that event. */
export const recordedCollectorPayload = withoutAnnotations(
  section(CHECKOUT_COMPLETED, 'collector_payload'),
)

/** The same, for the documented case where the beacon left before the order existed. */
export const recordedDegradedCollectorPayload = withoutAnnotations(
  section(CHECKOUT_COMPLETED, 'degraded_collector_payload'),
)

/** The authoritative `orders/paid` webhook body for the very same purchase (R4). */
export const recordedOrdersPaidWebhook = withoutAnnotations(section(ORDERS_PAID, 'payload'))

/**
 * The recorded event with `order` and `discountApplications` nulled — the degraded beacon.
 *
 * Built the way `shopify_stub.telemetry.checkout_completed_payload(degraded=True)` builds it, so
 * the two halves of the offline substrate describe the same failure rather than two of them.
 */
export function degradedCheckoutCompleted(): Record<string, unknown> {
  const event = structuredClone(recordedCheckoutCompleted) as Record<string, unknown>
  const data = event['data'] as Record<string, unknown>
  const checkout = data['checkout'] as Record<string, unknown>
  checkout['order'] = null
  checkout['discountApplications'] = null
  return event
}
