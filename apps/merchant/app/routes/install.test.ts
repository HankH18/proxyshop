/**
 * T-050, TypeScript half: the install route, and the C5 policy it shares with the service.
 *
 * The test that earns its place here is `the C5 policy is one policy, not two copies`. The
 * app and the Python service both have to know which scopes are asked for and which topics
 * are subscribed, and two hand-maintained copies of a security policy diverge silently —
 * the second copy keeps working, keeps passing its own tests, and is wrong. That test reads
 * `apps/merchant/svc/src/install/*.py` off disk and holds the two in both directions, so
 * editing one side alone is red.
 *
 * What the rest of this file proves: the route refuses a `shop` parameter that is not a
 * bare `.myshopify.com` host *before* it becomes a redirect target. What it does not prove:
 * anything about Shopify — the app performs no Shopify call at all, by design.
 */
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  ForbiddenWebhookTopic,
  InvalidShopDomain,
  PROTECTED_CUSTOMER_DATA_SCOPES,
  ProtectedScopeRequested,
  REQUIRED_SCOPES,
  WEBHOOK_TOPICS,
  assertScopesAllowed,
  assertTopicsAllowed,
  collectorUrl,
  isShopDomain,
  normalizeShopDomain,
  unauthorizedScopes,
  webPixelSettings,
  webhookCallbackUrl,
} from '../config/shopify'
import {
  DEFAULT_MERCHANT_SERVICE_URL,
  MERCHANT_SERVICE_URL_ENV,
  installHandoffUrl,
  installSummary,
  loader,
  merchantServiceUrl,
} from './install'

const SERVICE = 'https://merchant.example.test'
const SHOP = 'acceptance-store.myshopify.com'

const SERVICE_SOURCE_DIR = 'apps/merchant/svc/src/install'

/** Walk up from the working directory until the service module is found.
 *
 * Deliberately a search that throws rather than a path relative to this file: vitest's
 * jsdom environment does not give `import.meta.url` a filesystem URL, and a silently
 * wrong path would turn the drift check below into a test that cannot fail.
 */
function repoRoot(): string {
  let directory = process.cwd()
  for (let hop = 0; hop < 8; hop += 1) {
    if (existsSync(join(directory, SERVICE_SOURCE_DIR, 'scopes.py'))) return directory
    const parent = dirname(directory)
    if (parent === directory) break
    directory = parent
  }
  throw new Error(`could not locate ${SERVICE_SOURCE_DIR} from ${process.cwd()}`)
}

function readServiceSource(name: string): string {
  return readFileSync(join(repoRoot(), SERVICE_SOURCE_DIR, name), 'utf8')
}

/** Every double-quoted string inside the first `(...)` or `{...}` after `name =`. */
function pythonStringList(source: string, name: string): string[] {
  const match = new RegExp(`${name}[^=\\n]*=\\s*(?:frozenset\\()?\\s*[({]([\\s\\S]*?)[)}]`).exec(
    source,
  )
  if (!match?.[1]) throw new Error(`could not find ${name} in the service source`)
  return [...match[1].matchAll(/"([^"]+)"/g)].map((entry) => entry[1] as string)
}

describe('C5 — the scopes this app asks for', () => {
  it('requests read_orders and no protected-customer-data scope', () => {
    expect(REQUIRED_SCOPES).toContain('read_orders')
    expect(unauthorizedScopes(REQUIRED_SCOPES)).toEqual([])
  })

  it('refuses a protected scope wherever a caller can supply one', () => {
    const poisoned = [...REQUIRED_SCOPES, 'read_customers']
    expect(() => assertScopesAllowed(poisoned)).toThrow(ProtectedScopeRequested)
    expect(unauthorizedScopes(poisoned)).toEqual(['read_customers'])
    // Positive control: the C5-clean list still goes through, normalized.
    expect(assertScopesAllowed([' Read_Orders ', 'read_orders'])).toEqual(['read_orders'])
  })

  it('refuses every protected scope individually, not just the first one', () => {
    for (const scope of PROTECTED_CUSTOMER_DATA_SCOPES) {
      expect(() => assertScopesAllowed(['read_orders', scope])).toThrow(ProtectedScopeRequested)
    }
  })

  it('is not disarmed by the comma-joined shape Shopify itself returns', () => {
    // A string IS an Iterable<string> - of characters - so a bare scope name used to
    // shred into letters, none of which is a protected scope, and pass. The one-entry
    // comma-joined list is exactly what the token-exchange response puts in `scope`.
    // REFUSED, concretely:
    expect(() => assertScopesAllowed(['read_orders,read_customers'])).toThrow(
      ProtectedScopeRequested,
    )
    expect(unauthorizedScopes(['read_orders,read_customers'])).toEqual(['read_customers'])
    expect(() => assertScopesAllowed('read_customers' as unknown as string[])).toThrow(TypeError)
    // ADMITTED, concretely: the comma-joined form is split, not refused wholesale.
    expect(assertScopesAllowed(['read_orders, Write_Pixels '])).toEqual([
      'read_orders',
      'write_pixels',
    ])
  })
})

describe('C5 — the webhook topics, and the ceiling on them', () => {
  it('accepts the three and refuses a second checkout-observation path', () => {
    expect(assertTopicsAllowed(WEBHOOK_TOPICS)).toEqual([...WEBHOOK_TOPICS])
    expect(assertTopicsAllowed(['ORDERS_PAID'])).toEqual(['orders/paid'])
    for (const forbidden of ['checkouts/create', 'carts/update', 'orders/create']) {
      expect(() => assertTopicsAllowed([...WEBHOOK_TOPICS, forbidden])).toThrow(
        ForbiddenWebhookTopic,
      )
    }
  })

  it('builds one callback URL per topic under the pinned prefix', () => {
    expect(webhookCallbackUrl(SERVICE, 'orders/paid')).toBe(
      `${SERVICE}/webhooks/shopify/orders/paid`,
    )
    expect(webhookCallbackUrl(`${SERVICE}/`, 'ORDERS_FULFILLED')).toBe(
      `${SERVICE}/webhooks/shopify/orders/fulfilled`,
    )
  })
})

describe('the C5 policy is one policy, not two copies', () => {
  it('matches the service module scope for scope', () => {
    const source = readServiceSource('scopes.py')
    expect(pythonStringList(source, 'REQUIRED_SCOPES')).toEqual([...REQUIRED_SCOPES])
    expect(pythonStringList(source, 'PROTECTED_CUSTOMER_DATA_SCOPES').sort()).toEqual(
      [...PROTECTED_CUSTOMER_DATA_SCOPES].sort(),
    )
  })

  it('matches the service module topic for topic', () => {
    const source = readServiceSource('webhooks.py')
    expect(pythonStringList(source, 'WEBHOOK_TOPICS')).toEqual([...WEBHOOK_TOPICS])
  })

  it('agrees with the service on the two public paths the pixel and Shopify call', () => {
    const source = readServiceSource('config.py')
    expect(source).toContain(`COLLECTOR_PATH = "${new URL(collectorUrl('https://x')).pathname}"`)
    expect(source).toContain('WEBHOOK_PATH_PREFIX = "/webhooks/shopify"')
  })
})

describe('the shop parameter is validated before it becomes a URL', () => {
  it('accepts a bare myshopify host and normalizes it', () => {
    expect(normalizeShopDomain('  Acceptance-Store.MyShopify.com ')).toBe(SHOP)
    expect(isShopDomain(SHOP)).toBe(true)
  })

  it.each([
    'good.myshopify.com@attacker.tld',
    'https://good.myshopify.com',
    'good.myshopify.com:8443',
    'good.myshopify.com/evil',
    'good.myshopify.com?x=1',
    'good.myshopify.com#f',
    'good.myshopify.com evil.tld',
    'good..myshopify.com',
    '.good.myshopify.com',
    'good-.myshopify.com',
    'good.myshopify.com\n',
    'attacker.tld',
    'myshopify.com',
    '',
  ])('refuses %j', (malformed) => {
    expect(() => normalizeShopDomain(malformed)).toThrow(InvalidShopDomain)
    expect(isShopDomain(malformed)).toBe(false)
  })
})

describe('GET /install', () => {
  it('redirects a well-formed shop to the service install endpoint', () => {
    process.env[MERCHANT_SERVICE_URL_ENV] = SERVICE
    try {
      const response = loader({ request: new Request(`https://app.test/install?shop=${SHOP}`) })
      expect(response.status).toBe(302)
      expect(response.headers.get('location')).toBe(`${SERVICE}/install?shop=${SHOP}`)
    } finally {
      delete process.env[MERCHANT_SERVICE_URL_ENV]
    }
  })

  it('refuses a hostile shop instead of redirecting to it', async () => {
    const hostile = 'good.myshopify.com@attacker.tld'
    const response = loader({
      request: new Request(`https://app.test/install?shop=${encodeURIComponent(hostile)}`),
    })
    expect(response.status).toBe(400)
    expect(response.headers.get('location')).toBeNull()
    expect(await response.json()).toMatchObject({ error: 'invalid-shop' })
  })

  it('refuses an install link with no shop at all', async () => {
    const response = loader({ request: new Request('https://app.test/install') })
    expect(response.status).toBe(400)
    expect(await response.json()).toMatchObject({ error: 'missing-shop' })
  })

  it('falls back to a non-resolvable service origin rather than a plausible one', () => {
    delete process.env[MERCHANT_SERVICE_URL_ENV]
    expect(merchantServiceUrl({})).toBe(DEFAULT_MERCHANT_SERVICE_URL)
    expect(merchantServiceUrl({ [MERCHANT_SERVICE_URL_ENV]: `${SERVICE}//` })).toBe(SERVICE)
    expect(() => installHandoffUrl(SERVICE, 'attacker.tld')).toThrow(InvalidShopDomain)
  })
})

describe('the settings the web pixel is created with', () => {
  it('carries the collector URL and the shop, and nothing about a shopper', () => {
    const settings = webPixelSettings(SHOP, { appUrl: SERVICE })
    expect(settings.collectorUrl).toBe(`${SERVICE}/pixel/collect`)
    expect(settings.shopDomain).toBe(SHOP)
    expect(Object.keys(settings).sort()).toEqual(['apiVersion', 'collectorUrl', 'shopDomain'])
  })

  it('tells the merchant exactly what the install will ask for', () => {
    expect(installSummary()).toEqual({ scopes: REQUIRED_SCOPES, topics: WEBHOOK_TOPICS })
  })
})
