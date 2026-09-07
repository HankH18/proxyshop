/**
 * The extension body: subscribe to one standard event, flatten it, beacon it.
 *
 * `index.ts` hands this to `register()` and `register()` hands it to
 * `shopify.extend('WebPixel::Render', …)`. Everything Shopify-specific stops at that boundary —
 * this function takes the `ExtensionApi` object as a plain argument, so the whole wiring is
 * exercisable offline (C9) against a double of the sandbox rather than only inside a real
 * checkout page.
 *
 * ## One event, and only one
 *
 * SPEC C5: *"web pixel app extension is the only checkout observation path"*. That constrains
 * what is subscribed as much as it constrains how the app is built. The Web Pixels API publishes
 * fifteen standard events and this extension subscribes to exactly one of them,
 * `checkout_completed`, because it is the only one that answers R4's question — did the purchase
 * complete. Subscribing to `page_viewed` or `product_viewed` would build a second observation
 * path out of browsing behaviour that nothing in this system is entitled to and nothing asked
 * for; `apps/merchant/app/config/shopify.ts` refuses `checkouts/*` webhooks for the same reason
 * on the server side.
 *
 * ## Nothing may throw out of the subscriber
 *
 * The callback runs in a shopper's checkout page. An exception there is not a test failure, it
 * is a JavaScript error on a live storefront during payment, and it buys nothing: R4 already
 * makes the `orders/paid` webhook authoritative, so the correct behaviour when the pixel cannot
 * do its job is for the pixel to be silent and let the webhook be the record. Every failure path
 * below therefore ends in a return, and the transport's promise rejection is swallowed at the
 * call site rather than left to become an unhandled rejection.
 */
import type { ExtensionApi } from '@shopify/web-pixels-extension'

import { buildCollectorBody, type CollectorBody, OBSERVED_EVENT } from './beacon.ts'
import { readCollectorUrl } from './settings.ts'
import { postWithKeepalive, type Transport } from './transport.ts'

/** What `installPixel` was given in place of the real dependencies. Production passes none. */
export interface InstallOptions {
  /** Where beacons go. Defaults to {@link postWithKeepalive}, the `fetch` keepalive POST. */
  readonly transport?: Transport
}

/** Undoing an install. `false` means there was nothing subscribed to undo. */
export type Unsubscribe = () => boolean

const NOT_SUBSCRIBED: Unsubscribe = () => false

/**
 * Wire the extension up against `api`, and return the unsubscribe the sandbox handed back.
 *
 * When the app was installed without a usable `collectorUrl` this subscribes to **nothing** and
 * returns a no-op. That is deliberate rather than a subscriber that drops every event: the
 * callback would run on every completed checkout in the shop, on the shopper's device, to
 * discover each time that it has nowhere to send anything.
 */
export function installPixel(api: ExtensionApi, options: InstallOptions = {}): Unsubscribe {
  const collectorUrl = readCollectorUrl(api.settings)
  if (collectorUrl === null) return NOT_SUBSCRIBED
  const transport = options.transport ?? postWithKeepalive

  return api.analytics.subscribe(OBSERVED_EVENT, (event: unknown) => {
    try {
      const body: CollectorBody | null = buildCollectorBody(event)
      // No checkout token means nothing joinable: `accept_pixel_event` answers that body with a
      // 400 by contract, so sending it is a guaranteed-rejected request from a shopper's browser.
      if (body === null) return
      void transport(collectorUrl, body).catch(() => undefined)
    } catch {
      // The whole callback, not just the transform: a transport that throws *synchronously* —
      // a `fetch` a content blocker replaced, a sandbox that revoked it mid-session — would
      // otherwise surface as a JavaScript error on a live checkout page during payment. R4
      // already leaves the `orders/paid` webhook authoritative, so silence is the correct
      // failure and there is nothing this catch could usefully do with the error.
    }
  })
}
