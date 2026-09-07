/**
 * The `fetch` keepalive POST, and every way it is allowed to fail.
 *
 * `fetch` is stubbed rather than called: `checkout_completed` fires on a thank-you page whose tab
 * is about to close, and the property that matters — `keepalive: true`, so the browser finishes
 * the request after the document is gone — is a *request option*, observable in what the pixel
 * asks for. Whether a browser honours it is the browser's contract, not this code's, and no
 * offline test can witness it. What is witnessed here is the request this code actually builds
 * and the fact that no failure of it escapes into the page.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { CollectorBody } from '../src/beacon.ts'
import { postWithKeepalive } from '../src/transport.ts'

const COLLECTOR_URL = 'https://merchant.proxyshop.example/pixel/collect'

const BODY: CollectorBody = {
  clientId: '6f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8',
  checkoutToken: '0f3d1c9a5b2e4f8a9c7d6e5b4a3f2e1d',
  orderId: 'gid://shopify/Order/5500000000001',
  discountApplications: [{ code: 'PSX-7QK2ZB0M', value: 10, type: 'code' }],
}

function stubFetch(implementation: typeof fetch): ReturnType<typeof vi.fn> {
  const spy = vi.fn(implementation)
  vi.stubGlobal('fetch', spy)
  return spy
}

function answered(status: number): typeof fetch {
  return () => Promise.resolve(new Response(null, { status }))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the beacon request', () => {
  it('is a keepalive JSON POST of the collector body to the collector URL', async () => {
    const send = stubFetch(answered(204))

    const outcome = await postWithKeepalive(COLLECTOR_URL, BODY)

    expect(outcome).toEqual({ delivered: true, reason: null, status: 204 })
    expect(send).toHaveBeenCalledTimes(1)
    const [url, init] = send.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(COLLECTOR_URL)
    expect(init.method).toBe('POST')
    // The reason this is not a plain fetch: the thank-you page is frequently the last thing the
    // shopper looks at, and a request without `keepalive` is cancelled when the document goes.
    expect(init.keepalive).toBe(true)
    expect(init.headers).toEqual({ 'content-type': 'application/json' })
    expect(JSON.parse(String(init.body))).toEqual(BODY)
  })

  it('carries no cookies to the collector', async () => {
    // R5/C5: the body was built not to carry buyer identity; a credentialed request would carry
    // it through the transport instead.
    const send = stubFetch(answered(204))

    await postWithKeepalive(COLLECTOR_URL, BODY)

    const [, init] = send.mock.calls[0] as [string, RequestInit]
    expect(init.credentials).toBe('omit')
  })
})

describe('when the beacon does not land', () => {
  it('reports a refusal without retrying it', async () => {
    // The collector's 400 means this body is malformed; re-sending a malformed body produces a
    // second 400. R4 leaves the webhook authoritative, so there is nothing to recover here.
    const send = stubFetch(answered(400))

    const outcome = await postWithKeepalive(COLLECTOR_URL, BODY)

    expect(outcome).toEqual({ delivered: false, reason: 'refused', status: 400 })
    expect(send).toHaveBeenCalledTimes(1)
  })

  it('reports an unreachable collector instead of rejecting', async () => {
    const send = stubFetch(() => Promise.reject(new TypeError('Failed to fetch')))

    await expect(postWithKeepalive(COLLECTOR_URL, BODY)).resolves.toEqual({
      delivered: false,
      reason: 'unreachable',
      status: null,
    })
    expect(send).toHaveBeenCalledTimes(1)
  })

  it('reports a runtime with no fetch instead of throwing', async () => {
    vi.stubGlobal('fetch', undefined)

    await expect(postWithKeepalive(COLLECTOR_URL, BODY)).resolves.toEqual({
      delivered: false,
      reason: 'no-fetch',
      status: null,
    })
  })

  it('survives a fetch that throws synchronously', async () => {
    stubFetch((() => {
      throw new Error('blocked by a content blocker')
    }) as unknown as typeof fetch)

    await expect(postWithKeepalive(COLLECTOR_URL, BODY)).resolves.toEqual({
      delivered: false,
      reason: 'unreachable',
      status: null,
    })
  })

  it('survives a body that cannot be serialised', async () => {
    const send = stubFetch(answered(204))
    const circular = { clientId: null, checkoutToken: 'ck-1', orderId: null } as Record<
      string,
      unknown
    >
    circular['discountApplications'] = [circular]

    await expect(
      postWithKeepalive(COLLECTOR_URL, circular as unknown as CollectorBody),
    ).resolves.toEqual({ delivered: false, reason: 'unserializable', status: null })
    expect(send).not.toHaveBeenCalled()
  })
})
