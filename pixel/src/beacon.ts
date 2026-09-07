/**
 * The pure half of the web pixel: one Web Pixels API `checkout_completed` standard event in,
 * one collector body out.
 *
 * This is the transformation `DESIGN.md:39` fixes — *"POST `fetch(..., {keepalive:true})` to
 * merchant app collector with clientId, checkout token, order id, discountApplications"* — and
 * it is deliberately separated from the transport and from the Shopify runtime so it can be
 * graded offline (C9) against the recorded event shape in
 * `services/shopify-stub/fixtures/recorded/web_pixel_checkout_completed.json`.
 *
 * ## The body is BUILT from an allowlist, never filtered from the event
 *
 * SPEC C5 grants this app no protected-customer-data scope and R5 says stores never receive
 * buyer identity. The Web Pixels `Checkout` type really does carry `email`, `phone`,
 * `billingAddress`, `shippingAddress` and `order.customer` — the sandbox hands them to this
 * callback whether or not the app wants them. So the safe construction is the one below: four
 * named keys assembled from four named reads. A copy-then-delete, or a "strip the PII fields"
 * denylist, accepts every field nobody thought of, and "nobody thought of it" is the normal way
 * a new personal field arrives. {@link COLLECTOR_BODY_KEYS} is the whole published surface, and
 * `merchant_svc.collector.ACCEPTED_FIELDS` is the same allowlist enforced again at the door.
 *
 * ## Two mappings that are not cosmetic
 *
 * **`order.id` is normalised to an Order GID.** D24 joins the pixel observation to the
 * `orders/paid` webhook on `order_ref`, and the webhook's carrier is
 * `admin_graphql_api_id` = `gid://shopify/Order/<id>` (`services/shopify-stub/src/orders.py:265`,
 * read by `merchant_svc.install.webhooks` at `webhooks.py:181`). The Web Pixels API's
 * `Checkout.order.id` is documented as a bare string with no example value, and the stub emits
 * the numeric form. Beaconing the numeric form would produce two spellings of one identifier
 * that never join — a reconciliation that silently finds nothing, which is the failure R4 is
 * built to make visible rather than one it should suffer.
 *
 * **A `code` is emitted only for a `DISCOUNT_CODE` application.** The Web Pixels
 * `DiscountApplication` type has NO `code` member; for a discount-code application the
 * documented carrier of the code is `title`. For an `AUTOMATIC`, `MANUAL` or `SCRIPT`
 * application `title` is a customer-facing *name* — "Summer sale" — and putting that in the
 * `code` slot would fabricate the fourth join key, because the collector lifts `code` out of the
 * first entry that has one and stores it as `discount_code`
 * (`merchant_svc.collector._discount_code`). A fabricated join key is worse than a missing one:
 * a missing one is published as a gap, a fabricated one joins to nothing or to somebody else's
 * order while still reading as a complete observation.
 *
 * ## What this module deliberately does NOT do
 *
 * It applies no length ceiling and no cap on how many discount applications it forwards. Those
 * bounds exist — `MAX_PIXEL_FIELD_CHARS` (512) and `MAX_DISCOUNT_APPLICATIONS` (32) — and they
 * live in the collector, which is the side that *keeps* the data and therefore the side whose
 * memory the bound protects. Duplicating them here would put the authority in two places and
 * let them drift; the emitter's job is to report what the browser saw, and the receiver's job is
 * to decide what it will hold.
 */

/** The four keys a collector body may carry, and no fifth. */
export const COLLECTOR_BODY_KEYS = [
  'clientId',
  'checkoutToken',
  'orderId',
  'discountApplications',
] as const

/** The Web Pixels standard event this extension observes. C5 makes it the only one. */
export const OBSERVED_EVENT = 'checkout_completed'

/** The prefix `admin_graphql_api_id` uses on the authoritative webhook side. */
export const ORDER_GID_PREFIX = 'gid://shopify/Order/'

/** Shopify's `DiscountApplication.type` for an application a shopper entered a code for. */
export const DISCOUNT_CODE_TYPE = 'DISCOUNT_CODE'

/**
 * One discount application, flattened.
 *
 * `code` is absent unless Shopify said the application was a `DISCOUNT_CODE`. `value` is the
 * bare number out of Shopify's `MoneyV2 | PricingPercentageValue` union, so its unit is decided
 * by which arm of that union the platform sent — percentage points for a percentage discount,
 * money for a fixed-amount one. That ambiguity is Shopify's, carried faithfully rather than
 * resolved by guessing; nothing downstream reads it today, because the collector lifts only
 * `code`.
 */
export interface CollectorDiscountApplication {
  readonly code?: string
  readonly value: number | null
  readonly type: string
}

/** The body POSTed to `{app_url}/pixel/collect`. */
export interface CollectorBody {
  readonly clientId: string | null
  readonly checkoutToken: string
  readonly orderId: string | null
  readonly discountApplications: readonly CollectorDiscountApplication[] | null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** A trimmed non-empty string, or `null`. Never coerces a number or an object into text. */
function text(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

/**
 * `order.id` as the webhook spells it.
 *
 * An id that is already a GID is passed through; a bare digit string is prefixed. Anything else
 * is forwarded verbatim rather than wrapped, because manufacturing `gid://shopify/Order/<junk>`
 * out of a shape this code does not recognise would assert a normalisation that did not happen.
 */
export function normalizeOrderRef(rawId: unknown): string | null {
  const id = text(rawId)
  if (id === null) return null
  if (id.startsWith('gid://')) return id
  return /^[0-9]+$/.test(id) ? `${ORDER_GID_PREFIX}${id}` : id
}

/** The number out of `MoneyV2 | PricingPercentageValue`, or `null` for anything else. */
function discountValue(raw: unknown): number | null {
  if (!isRecord(raw)) return typeof raw === 'number' && Number.isFinite(raw) ? raw : null
  for (const key of ['percentage', 'amount'] as const) {
    const candidate = raw[key]
    if (typeof candidate === 'number' && Number.isFinite(candidate)) return candidate
  }
  return null
}

function flattenDiscountApplication(raw: unknown): CollectorDiscountApplication | null {
  if (!isRecord(raw)) return null
  const shopifyType = text(raw['type'])
  const title = text(raw['title'])
  const value = discountValue(raw['value'])
  const type = shopifyType === null ? 'unknown' : shopifyType.toLowerCase().replace(/^discount_/, '')
  // `title` carries the code ONLY for a DISCOUNT_CODE application; see the module docstring.
  if (shopifyType === DISCOUNT_CODE_TYPE && title !== null) {
    return { code: title, value, type }
  }
  return { value, type }
}

/**
 * The discount applications a beacon carries.
 *
 * `null` in, `null` out: a beacon that raced order creation carries `discountApplications: null`
 * — the event happened and the field is unknown — and that is a different fact from the empty
 * list a checkout with no discount applied really does emit. Collapsing the two would turn "I do
 * not know" into "there was none".
 */
function flattenDiscountApplications(raw: unknown): CollectorDiscountApplication[] | null {
  if (!Array.isArray(raw)) return null
  const flattened: CollectorDiscountApplication[] = []
  for (const entry of raw) {
    const application = flattenDiscountApplication(entry)
    if (application !== null) flattened.push(application)
  }
  return flattened
}

/**
 * The collector body for one `checkout_completed` event, or `null` when there is nothing
 * joinable to report.
 *
 * `null` is returned when the event carries no `data.checkout.token`. That is not defensiveness
 * for its own sake: `merchant_svc.collector.accept_pixel_event` refuses a body with no
 * `checkout_token` with a 400 — "it can never be joined to an order" — so a beacon without one
 * is a request that is guaranteed to be rejected, sent from a shopper's browser during checkout.
 * Not sending it is the same outcome with none of the cost.
 *
 * `event` is typed `unknown` on purpose. It arrives from the Shopify sandbox, this code cannot
 * make it well-formed by declaring a type for it, and every read below narrows before it trusts.
 */
export function buildCollectorBody(event: unknown): CollectorBody | null {
  if (!isRecord(event)) return null
  const data = isRecord(event['data']) ? event['data'] : {}
  const checkout = isRecord(data['checkout']) ? data['checkout'] : {}
  const checkoutToken = text(checkout['token'])
  if (checkoutToken === null) return null
  const order = isRecord(checkout['order']) ? checkout['order'] : {}
  return {
    clientId: text(event['clientId']),
    checkoutToken,
    orderId: normalizeOrderRef(order['id']),
    discountApplications: flattenDiscountApplications(checkout['discountApplications']),
  }
}
