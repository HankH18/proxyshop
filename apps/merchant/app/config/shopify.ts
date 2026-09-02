/**
 * The Shopify app's own statement of what it asks a merchant for — the TypeScript half.
 *
 * SPEC C5 is one policy with two enforcement points, because the install has two halves:
 * the React Router app the merchant clicks through, and the Python service that issues the
 * Admin mutations (`apps/merchant/svc/src/install`). Two copies of a policy is how a policy
 * silently drifts, so `app/routes/install.test.ts` reads the Python module and holds these
 * constants to it in both directions. Changing one side alone is a red test, not a
 * discovery six weeks later.
 *
 * Nothing here talks to Shopify. The app hands the OAuth flow to the service, which owns
 * the single implementation of the signature checks and the token store.
 */

/** The scopes this app requests. `read_orders` (C5) plus the two write scopes the two
 * mutations need: `write_discounts` for `discountCodeBasicCreate`, `write_pixels` for
 * `webPixelCreate`. */
export const REQUIRED_SCOPES = ['read_orders', 'write_discounts', 'write_pixels'] as const

/**
 * Shopify's protected-customer-data scopes. C5 forbids every one, so asking for any is a
 * refusal here rather than a request Shopify gets to decline.
 *
 * `read_customer_events` is on this list even though Shopify's own web-pixel documentation
 * pairs it with `write_pixels`. That divergence is deliberate and is stated in the service
 * module's docstring: the collector accepts join keys only, so customer-event authority is
 * authority this system has decided not to hold.
 */
export const PROTECTED_CUSTOMER_DATA_SCOPES = [
  'read_all_orders',
  'read_customer_events',
  'read_customer_merge',
  'read_customer_payment_methods',
  'read_customers',
  'read_marketing_events',
  'write_customer_merge',
  'write_customers',
] as const

/** The three order webhooks, and no fourth: C5 makes the web pixel the only
 * checkout-observation path, so a `checkouts/*` subscription would build a second one. */
export const WEBHOOK_TOPICS = ['orders/paid', 'orders/fulfilled', 'refunds/create'] as const

/** `WebhookSubscriptionTopic` enum spellings for {@link WEBHOOK_TOPICS}. */
export const TOPIC_ENUM: Readonly<Record<string, string>> = {
  'orders/paid': 'ORDERS_PAID',
  'orders/fulfilled': 'ORDERS_FULFILLED',
  'refunds/create': 'REFUNDS_CREATE',
}

/** The Admin API version this app pins; `services/shopify-stub` serves the same one. */
export const ADMIN_API_VERSION = '2026-07'

/** Where the web pixel beacons to, and where order webhooks are delivered. */
export const COLLECTOR_PATH = '/pixel/collect'
export const WEBHOOK_PATH_PREFIX = '/webhooks/shopify'

/** Every shop this app can be installed on lives under this suffix. */
export const MYSHOPIFY_SUFFIX = '.myshopify.com'

export class ProtectedScopeRequested extends Error {
  readonly offending: readonly string[]
  constructor(offending: readonly string[]) {
    super(`SPEC C5 forbids protected-customer-data scopes; refused: ${offending.join(', ')}`)
    this.name = 'ProtectedScopeRequested'
    this.offending = offending
  }
}

export class ForbiddenWebhookTopic extends Error {
  readonly offending: readonly string[]
  constructor(offending: readonly string[]) {
    super(
      `C5 allows exactly ${WEBHOOK_TOPICS.join(', ')}; a second checkout-observation ` +
        `path is refused. Rejected: ${offending.join(', ')}`,
    )
    this.name = 'ForbiddenWebhookTopic'
    this.offending = offending
  }
}

export class InvalidShopDomain extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'InvalidShopDomain'
  }
}

/** Lower-case, trim and de-duplicate a scope list, preserving first-seen order. */
export function normalizeScopes(scopes: Iterable<string>): string[] {
  const seen = new Set<string>()
  for (const scope of scopes) {
    const cleaned = String(scope).trim().toLowerCase()
    if (cleaned) seen.add(cleaned)
  }
  return [...seen]
}

/** The protected-customer-data scopes present in `scopes`, sorted. */
export function unauthorizedScopes(scopes: Iterable<string>): string[] {
  const protectedSet = new Set<string>(PROTECTED_CUSTOMER_DATA_SCOPES)
  return normalizeScopes(scopes)
    .filter((scope) => protectedSet.has(scope))
    .sort()
}

/**
 * Return the normalized scope list, or throw.
 *
 * Enforced on the *argument*, never on {@link REQUIRED_SCOPES}: a check against a module
 * constant can never refuse anything.
 */
export function assertScopesAllowed(scopes: Iterable<string>): string[] {
  const normalized = normalizeScopes(scopes)
  if (normalized.length === 0) throw new Error('at least one scope must be requested')
  const offending = unauthorizedScopes(normalized)
  if (offending.length > 0) throw new ProtectedScopeRequested(offending)
  return normalized
}

/** `ORDERS_PAID`, `orders/paid` and `Orders/Paid` all name one topic. */
export function normalizeTopic(raw: string): string {
  return String(raw).trim().toLowerCase().replaceAll('_', '/')
}

/** Return the normalized topic list, or throw {@link ForbiddenWebhookTopic}. */
export function assertTopicsAllowed(topics: Iterable<string>): string[] {
  const normalized = [...new Set([...topics].map(normalizeTopic))]
  if (normalized.length === 0) throw new Error('at least one webhook topic must be subscribed')
  const offending = normalized.filter((topic) => !(topic in TOPIC_ENUM))
  if (offending.length > 0) throw new ForbiddenWebhookTopic(offending)
  return normalized
}

const LABEL = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/

/**
 * Return the lower-cased bare shop host, or throw {@link InvalidShopDomain}.
 *
 * The shop name arrives in a query parameter and is then used as an API host, so every
 * form that is not a bare DNS name is refused rather than repaired — `good.myshopify.com@
 * attacker.tld` reads as the merchant's own store in a log line and resolves to somebody
 * else's server in a browser. Control bytes are refused, never trimmed: a trailing LF on a
 * host is a response-splitting payload.
 */
export function normalizeShopDomain(raw: string): string {
  if (typeof raw !== 'string') throw new InvalidShopDomain('shop domain must be a string')
  for (const char of raw) {
    const code = char.codePointAt(0) ?? 0
    if ((code < 0x21 && char !== ' ') || code === 0x7f) {
      throw new InvalidShopDomain(`shop domain carries a control character: ${JSON.stringify(raw)}`)
    }
  }
  const candidate = raw.trim().toLowerCase()
  if (!candidate) throw new InvalidShopDomain('shop domain is empty')
  for (const forbidden of ['://', '@', '/', '?', '#', ':', '\\', ' ']) {
    if (candidate.includes(forbidden)) {
      throw new InvalidShopDomain(
        `shop domain must be a bare host, not ${JSON.stringify(raw)} ` +
          `(contains ${JSON.stringify(forbidden)})`,
      )
    }
  }
  if (!candidate.endsWith(MYSHOPIFY_SUFFIX)) {
    throw new InvalidShopDomain(
      `shop domain must end in ${MYSHOPIFY_SUFFIX}, got ${JSON.stringify(raw)}`,
    )
  }
  const labels = candidate.split('.')
  if (labels.length < 3 || !labels.every((label) => LABEL.test(label))) {
    throw new InvalidShopDomain(`shop domain is not a well-formed host: ${JSON.stringify(raw)}`)
  }
  return candidate
}

/** `true` when {@link normalizeShopDomain} would accept `raw`. */
export function isShopDomain(raw: string): boolean {
  try {
    normalizeShopDomain(raw)
    return true
  } catch {
    return false
  }
}

/** The URL the web pixel POSTs checkout events to. */
export function collectorUrl(appUrl: string): string {
  return `${appUrl.replace(/\/+$/, '')}${COLLECTOR_PATH}`
}

/** The URL Shopify delivers `topic` to, e.g. `…/webhooks/shopify/orders/paid`. */
export function webhookCallbackUrl(appUrl: string, topic: string): string {
  return `${appUrl.replace(/\/+$/, '')}${WEBHOOK_PATH_PREFIX}/${normalizeTopic(topic)}`
}

/**
 * The `WebPixelInput.settings` payload `webPixelCreate` is called with.
 *
 * Deliberately carries nothing identifying a shopper: C5 grants this app no
 * protected-customer-data scope and the collector rejects every PII field.
 */
export function webPixelSettings(
  shopDomain: string,
  options: { collectorUrl?: string; apiVersion?: string; appUrl?: string } = {},
): Record<string, string> {
  const shop = normalizeShopDomain(shopDomain)
  const collector =
    options.collectorUrl ?? collectorUrl(options.appUrl ?? 'https://merchant.proxyshop.example')
  return {
    collectorUrl: collector,
    shopDomain: shop,
    apiVersion: options.apiVersion ?? ADMIN_API_VERSION,
  }
}
