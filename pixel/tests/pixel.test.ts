/**
 * The wiring: what the extension subscribes to, when it beacons, and how it fails.
 *
 * The Shopify sandbox itself is not available offline (C9), so the boundary is drawn at the
 * `ExtensionApi` object the sandbox hands `register()`'s callback. Everything on this side of that
 * object is exercised here with the real code; the sandbox's own behaviour — loading the bundle,
 * building the event, honouring `keepalive` after the tab closes — is not, and is stated as not
 * exercised in `pixel/README.md` rather than mimicked into a green.
 */
import type { ExtensionApi } from '@shopify/web-pixels-extension'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { installPixel } from '../src/pixel.ts'
import type { BeaconOutcome, Transport } from '../src/transport.ts'
import { recordedCheckoutCompleted, recordedCollectorPayload } from './fixtures.ts'

const COLLECTOR_URL = 'https://merchant.proxyshop.example/pixel/collect'

/**
 * Written out, never imported from `src/beacon.ts`.
 *
 * Asserting against the extension's own `OBSERVED_EVENT` would make these tests agree with
 * whatever it decided to watch: rename the constant to `page_viewed` and every one of them
 * still passes. `checkout_completed` is Shopify's name for this event and this file is the
 * sandbox's side of that contract, so it spells it independently.
 */
const CHECKOUT_COMPLETED = 'checkout_completed'

interface Subscription {
  readonly name: string
  readonly callback: (event: unknown) => void
}

interface SandboxDouble {
  readonly api: ExtensionApi
  readonly subscriptions: Subscription[]
  readonly unsubscribed: string[]
  fire(name: string, event: unknown): void
}

/**
 * A stand-in for the object the Web Pixel sandbox passes to the extension.
 *
 * `settings` and `analytics` are real implementations because they are the two members this
 * extension touches; the rest are present only so the double satisfies Shopify's published
 * `ExtensionApi` and `tsc` therefore checks the `subscribe` call against the real declaration.
 * Reaching for one of them would fail loudly rather than quietly return `undefined`.
 */
function sandbox(settings: Record<string, unknown> = { collectorUrl: COLLECTOR_URL }): SandboxDouble {
  const subscriptions: Subscription[] = []
  const unsubscribed: string[] = []
  const unavailable = (member: string) => {
    throw new Error(`the extension reached for ExtensionApi.${member}, which it must not need`)
  }
  const api = {
    settings,
    analytics: {
      subscribe(name: string, callback: (event: never) => void) {
        subscriptions.push({ name, callback: callback as (event: unknown) => void })
        return () => {
          unsubscribed.push(name)
          return true
        }
      },
    },
    get browser() {
      return unavailable('browser')
    },
    get init() {
      return unavailable('init')
    },
    get _pixelInfo() {
      return unavailable('_pixelInfo')
    },
    get customerPrivacy() {
      return unavailable('customerPrivacy')
    },
  } as unknown as ExtensionApi

  return {
    api,
    subscriptions,
    unsubscribed,
    fire(name, event) {
      for (const subscription of subscriptions) {
        if (subscription.name === name) subscription.callback(event)
      }
    },
  }
}

const DELIVERED: BeaconOutcome = { delivered: true, reason: null, status: 204 }

function recordingTransport(outcome: BeaconOutcome = DELIVERED): {
  transport: Transport
  calls: { url: string; body: unknown }[]
} {
  const calls: { url: string; body: unknown }[] = []
  const transport: Transport = (url, body) => {
    calls.push({ url, body })
    return Promise.resolve(outcome)
  }
  return { transport, calls }
}

/** Let every queued microtask and the macrotask after it run, so a beacon has really settled. */
async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0))
}

describe('what the extension observes', () => {
  it('subscribes to checkout_completed and to nothing else', () => {
    // C5: the web pixel is the ONLY checkout observation path, which bounds what it may watch as
    // much as it bounds how the app is built. Fourteen other standard events are available and
    // this app is entitled to none of them.
    const shopify = sandbox()
    installPixel(shopify.api, recordingTransport())

    expect(shopify.subscriptions.map((entry) => entry.name)).toEqual([CHECKOUT_COMPLETED])
  })

  it('ignores the other standard events even if the sandbox delivers them', async () => {
    const shopify = sandbox()
    const { transport, calls } = recordingTransport()
    installPixel(shopify.api, { transport })

    for (const name of ['page_viewed', 'product_viewed', 'checkout_started', 'cart_viewed']) {
      shopify.fire(name, recordedCheckoutCompleted)
    }
    await settle()

    expect(calls).toEqual([])
  })

  it('hands back the sandbox’s own unsubscribe', () => {
    const shopify = sandbox()
    const stop = installPixel(shopify.api, recordingTransport())

    expect(stop()).toBe(true)
    expect(shopify.unsubscribed).toEqual([CHECKOUT_COMPLETED])
  })
})

describe('what the extension sends', () => {
  it('POSTs the collector body to the configured collector on a completed checkout', async () => {
    const shopify = sandbox()
    const { transport, calls } = recordingTransport()
    installPixel(shopify.api, { transport })

    shopify.fire(CHECKOUT_COMPLETED, recordedCheckoutCompleted)
    await settle()

    expect(calls).toEqual([{ url: COLLECTOR_URL, body: recordedCollectorPayload }])
  })

  it('sends exactly one beacon per completed checkout', async () => {
    // R4 makes the webhook authoritative, so a second copy of an observation buys nothing and a
    // retry loop on an unauthenticated endpoint costs the collector something during an outage.
    const shopify = sandbox()
    const { transport, calls } = recordingTransport({
      delivered: false,
      reason: 'refused',
      status: 400,
    })
    installPixel(shopify.api, { transport })

    shopify.fire(CHECKOUT_COMPLETED, recordedCheckoutCompleted)
    await settle()
    await settle()

    expect(calls).toHaveLength(1)
  })

  it('sends nothing when the event carries no checkout token', async () => {
    const shopify = sandbox()
    const { transport, calls } = recordingTransport()
    installPixel(shopify.api, { transport })

    shopify.fire(CHECKOUT_COMPLETED, { clientId: 'cid-1', data: { checkout: { order: { id: '1' } } } })
    await settle()

    expect(calls).toEqual([])
  })
})

describe('how the extension degrades', () => {
  const unhandled: unknown[] = []
  const captureUnhandled = (reason: unknown) => unhandled.push(reason)
  process.on('unhandledRejection', captureUnhandled)

  afterEach(() => {
    expect(unhandled, 'the extension leaked an unhandled rejection into the page').toEqual([])
  })

  it('does not subscribe at all when no collector URL was registered', () => {
    // Nothing to send to means nothing to run on every checkout in the shop.
    for (const settings of [{}, { collectorUrl: '' }, { collectorUrl: 42 }]) {
      const shopify = sandbox(settings)
      const stop = installPixel(shopify.api, recordingTransport())

      expect(shopify.subscriptions).toEqual([])
      expect(stop()).toBe(false)
    }
  })

  it('refuses a relative collector URL rather than beaconing at the shop', () => {
    // A relative path resolves against the checkout page's origin, so this would POST a
    // shopper's join keys to the merchant's own storefront instead of to this app.
    const shopify = sandbox({ collectorUrl: '/pixel/collect' })

    expect(installPixel(shopify.api, recordingTransport())()).toBe(false)
    expect(shopify.subscriptions).toEqual([])
  })

  it('survives a collector that is unreachable', async () => {
    const shopify = sandbox()
    const transport: Transport = () => Promise.reject(new Error('net::ERR_CONNECTION_REFUSED'))
    installPixel(shopify.api, { transport })

    expect(() => shopify.fire(CHECKOUT_COMPLETED, recordedCheckoutCompleted)).not.toThrow()
    await settle()
  })

  it('survives a transport that throws synchronously', async () => {
    // A content blocker that replaced `fetch`, or a sandbox that revoked it mid-session, throws
    // on the call rather than rejecting. Nothing the transport does may reach the checkout page
    // as a JavaScript error during payment.
    const shopify = sandbox()
    const transport: Transport = () => {
      throw new Error('the sandbox blocked the request')
    }
    installPixel(shopify.api, { transport })

    expect(() => shopify.fire(CHECKOUT_COMPLETED, recordedCheckoutCompleted)).not.toThrow()
    await settle()
  })

  it('survives an event whose shape it cannot read', async () => {
    const shopify = sandbox()
    const { transport, calls } = recordingTransport()
    installPixel(shopify.api, { transport })

    for (const junk of [undefined, null, 'checkout_completed', 7, []]) {
      expect(() => shopify.fire(CHECKOUT_COMPLETED, junk)).not.toThrow()
    }
    await settle()

    expect(calls).toEqual([])
  })
})

describe('the extension entry point', () => {
  afterEach(() => {
    vi.resetModules()
    delete (globalThis as unknown as Record<string, unknown>)['shopify']
  })

  it('claims the WebPixel::Render extension point with a working installer', async () => {
    // `src/index.ts` is the module Shopify's bundler ships. This is the one assertion that the
    // wiring above is actually reached in production rather than only from tests.
    const extend = vi.fn()
    ;(globalThis as unknown as Record<string, unknown>)['shopify'] = { extend }

    await import('../src/index')

    expect(extend).toHaveBeenCalledTimes(1)
    const [extensionPoint, installer] = extend.mock.calls[0] as [string, (api: ExtensionApi) => void]
    expect(extensionPoint).toBe('WebPixel::Render')

    const shopify = sandbox()
    installer(shopify.api)
    expect(shopify.subscriptions.map((entry) => entry.name)).toEqual([CHECKOUT_COMPLETED])
  })
})
