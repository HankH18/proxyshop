/**
 * Getting one collector body out of the browser, and surviving every way that fails.
 *
 * `DESIGN.md:39` names the mechanism: `fetch(..., {keepalive: true})`. `keepalive` is the whole
 * reason this is not a plain `fetch` — `checkout_completed` fires on the thank-you page, which
 * is frequently the last thing a shopper looks at before closing the tab, and a request without
 * it is cancelled when the document goes away. With it the browser is obliged to finish the POST
 * after the page is gone.
 *
 * ## Fire and forget, and it means forget
 *
 * There is no retry, no backoff and no queue. That is not laziness, it is R4: *the webhook is
 * authoritative*. The pixel is a lossy sample of an event whose truth arrives separately over a
 * server-to-server delivery Shopify retries for up to 48 hours, and
 * `merchant_svc.collector` already records what a lossy beacon did not carry as a visible `gap`.
 * A retry loop inside a shopper's checkout page would buy a duplicate observation of something
 * already known, at the cost of holding a page open and multiplying load on an unauthenticated
 * endpoint the moment it has an outage. The one thing this must never do is throw into the
 * sandbox, so every failure below is *reported* as a value and never raised.
 *
 * ## Credentials are omitted deliberately
 *
 * `credentials: 'omit'` keeps cookies out of the request. R5 says stores never receive buyer
 * identity and C5 grants no protected-customer-data scope; a beacon that carried the shopper's
 * cookies to this app's origin would be collecting identity through the transport that the body
 * was carefully built not to carry.
 */
import type { CollectorBody } from './beacon.ts'

/** Why a beacon did not land. `null` on the delivered path. */
export type BeaconFailure =
  /** The runtime has no `fetch`. Nothing was attempted. */
  | 'no-fetch'
  /** The request never got an answer: DNS, TLS, offline, blocked by an extension. */
  | 'unreachable'
  /** The collector answered, and refused. A 4xx here means the body was wrong. */
  | 'refused'
  /**
   * The body would not serialise. A {@link CollectorBody} out of `buildCollectorBody` is flat
   * scalars and cannot reach this, but a transport that promises never to throw has to be total
   * over its own parameter type rather than over the one caller it happens to have.
   */
  | 'unserializable'

/** What one beacon attempt did. Returned, never thrown. */
export interface BeaconOutcome {
  readonly delivered: boolean
  readonly reason: BeaconFailure | null
  readonly status: number | null
}

/**
 * The seam the extension sends through.
 *
 * Injectable so the wiring can be exercised offline (C9) against a double that records what was
 * sent, rather than only against a real `fetch` that would need a network to say anything.
 */
export type Transport = (collectorUrl: string, body: CollectorBody) => Promise<BeaconOutcome>

function failed(reason: BeaconFailure, status: number | null = null): BeaconOutcome {
  return { delivered: false, reason, status }
}

/**
 * POST `body` to `collectorUrl` with `keepalive`, and never throw.
 *
 * A non-2xx answer is a `refused` outcome rather than a retry: the collector's 400 means this
 * body is malformed, and re-sending a malformed body produces a second 400.
 */
export const postWithKeepalive: Transport = async (collectorUrl, body) => {
  const send: typeof fetch | undefined = globalThis.fetch
  if (typeof send !== 'function') return failed('no-fetch')
  let payload: string
  try {
    payload = JSON.stringify(body)
  } catch {
    return failed('unserializable')
  }
  try {
    const response = await send(collectorUrl, {
      method: 'POST',
      keepalive: true,
      mode: 'cors',
      credentials: 'omit',
      headers: { 'content-type': 'application/json' },
      body: payload,
    })
    const status = typeof response.status === 'number' ? response.status : null
    return response.ok ? { delivered: true, reason: null, status } : failed('refused', status)
  } catch {
    // A rejected request, a `fetch` a content blocker replaced with a thrower, a hostile
    // double — all the same outcome, and none of them may escape into the checkout page.
    return failed('unreachable')
  }
}
