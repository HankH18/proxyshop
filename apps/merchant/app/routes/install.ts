/**
 * `GET /install` — the door a merchant actually clicks, as a React Router v7 route module.
 *
 * The route does **not** re-implement OAuth. Signature verification, the offline-token
 * exchange and the Admin mutations all live once, in the Python service
 * (`apps/merchant/svc/src/install`); a second implementation in TypeScript would be a
 * second place for the callback HMAC to be subtly wrong. What this module owns is the part
 * that has to live in the app: reading the `shop` parameter, refusing anything that is not
 * a bare `.myshopify.com` host before it is ever used as a URL, and handing off.
 *
 * `shop` is attacker-controllable — the install link is a link anyone can construct — so
 * the redirect target is built from the *validated* host and never from the raw parameter.
 */
import {
  InvalidShopDomain,
  REQUIRED_SCOPES,
  WEBHOOK_TOPICS,
  normalizeShopDomain,
} from '../config/shopify'

/** Environment variable naming the merchant service's origin. */
export const MERCHANT_SERVICE_URL_ENV = 'MERCHANT_SERVICE_URL'

/** Used when the environment says nothing. Not resolvable on purpose: a misconfigured
 * deployment fails loudly rather than quietly talking to somebody else. */
export const DEFAULT_MERCHANT_SERVICE_URL = 'https://merchant.proxyshop.example'

type Env = Record<string, string | undefined>

/** The merchant service origin, without a trailing slash. */
export function merchantServiceUrl(env: Env = process.env): string {
  const configured = env[MERCHANT_SERVICE_URL_ENV]
  const base = configured && configured.length > 0 ? configured : DEFAULT_MERCHANT_SERVICE_URL
  return base.replace(/\/+$/, '')
}

/** Where `GET /install?shop=…` hands the merchant off to. */
export function installHandoffUrl(serviceUrl: string, shop: string): string {
  const validated = normalizeShopDomain(shop)
  return `${serviceUrl.replace(/\/+$/, '')}/install?shop=${encodeURIComponent(validated)}`
}

function problem(status: number, error: string, detail?: string): Response {
  return new Response(JSON.stringify(detail === undefined ? { error } : { error, detail }), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

/**
 * Start an install.
 *
 * Answers `302` to the service's install endpoint for a well-formed shop, `400` for
 * anything else. The response body of the refusal names what was wrong, because an
 * operator staring at a blank 400 cannot tell a typo from an attack.
 */
export function loader({ request }: { request: Request }): Response {
  const shop = new URL(request.url).searchParams.get('shop')
  if (!shop) return problem(400, 'missing-shop', 'the install link must carry ?shop=')
  try {
    return new Response(null, {
      status: 302,
      headers: { location: installHandoffUrl(merchantServiceUrl(), shop) },
    })
  } catch (error) {
    if (error instanceof InvalidShopDomain) return problem(400, 'invalid-shop', error.message)
    throw error
  }
}

/** What the install screen tells the merchant it is about to ask Shopify for. */
export function installSummary(): { scopes: readonly string[]; topics: readonly string[] } {
  return { scopes: REQUIRED_SCOPES, topics: WEBHOOK_TOPICS }
}
