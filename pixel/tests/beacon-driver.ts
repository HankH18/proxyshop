/**
 * Harness, not a test: the shim that lets a Python test drive the real pixel over a real socket.
 *
 * `apps/merchant/svc/tests/test_collector.py` serves the real merchant app on loopback
 * (`proxyshop_support.asgi_server.serve`, D40) and runs this file with `node`, which strips the
 * types and executes `src/pixel.ts`, `src/settings.ts`, `src/beacon.ts` and `src/transport.ts`
 * unchanged — no build step, no bundler, no second copy of the logic. The POST that arrives at
 * `POST /pixel/collect` is therefore the one the extension really builds: the same `keepalive`
 * request, the same JSON body. That is the only version of this claim worth making, because a
 * Python re-implementation of the transform would prove that the re-implementation matches the
 * collector and say nothing at all about the pixel.
 *
 * It is deliberately NOT a `*.test.ts`: vitest collects those, and a file that reads stdin and
 * opens a socket has no business in the unit suite.
 *
 * Protocol: one JSON object on stdin, `{collectorUrl, event}`; one JSON object on stdout,
 * `{subscribed, body, outcome}`. `subscribed` is every standard-event name the extension asked
 * for, `body` is what it chose to send and `outcome` is what `postWithKeepalive` reported — so the
 * Python side can assert on what was watched, on the bytes, AND on the answer the served route
 * gave them.
 *
 * The one stand-in is the `ExtensionApi` object below. Shopify's sandbox is not available offline
 * (C9) and nothing here pretends otherwise: what this exercises is everything from the subscriber
 * callback onwards. `pixel/README.md` states that boundary.
 */
import type { ExtensionApi } from '@shopify/web-pixels-extension'

import { installPixel } from '../src/pixel.ts'
import { postWithKeepalive, type BeaconOutcome } from '../src/transport.ts'

/**
 * The event name is written out here rather than imported from `src/beacon.ts`.
 *
 * Importing the extension's own constant would make this harness fire whatever the extension
 * happened to subscribe to, so a pixel that watched `page_viewed` would still be handed a
 * checkout and still look correct. Shopify delivers `checkout_completed` under that literal
 * name; this file is the sandbox's side of the contract and has to spell it independently.
 */
const CHECKOUT_COMPLETED = 'checkout_completed'

interface DriveRequest {
  readonly collectorUrl: string
  readonly event: unknown
}

async function readRequest(): Promise<DriveRequest> {
  const chunks: Buffer[] = []
  for await (const chunk of process.stdin) chunks.push(chunk as Buffer)
  return JSON.parse(Buffer.concat(chunks).toString('utf8')) as DriveRequest
}

async function main(): Promise<void> {
  const request = await readRequest()
  const subscriptions: { name: string; callback: (event: unknown) => void }[] = []
  const api = {
    settings: { collectorUrl: request.collectorUrl },
    analytics: {
      subscribe(name: string, callback: (event: never) => void) {
        subscriptions.push({ name, callback: callback as (event: unknown) => void })
        return () => true
      },
    },
  } as unknown as ExtensionApi

  let sent: unknown = null
  let pending: Promise<BeaconOutcome> | null = null
  installPixel(api, {
    transport: (collectorUrl, body) => {
      // The REAL transport, wrapped only to keep hold of the promise the extension discards.
      sent = body
      pending = postWithKeepalive(collectorUrl, body)
      return pending
    },
  })

  // Only a real `checkout_completed` is delivered. A subscriber on any other name is reported
  // in `subscribed` and never fed, which is what the sandbox would do.
  for (const subscription of subscriptions) {
    if (subscription.name === CHECKOUT_COMPLETED) subscription.callback(request.event)
  }
  const settled: Promise<BeaconOutcome> | null = pending
  const outcome: BeaconOutcome | null = settled === null ? null : await settled

  const subscribed = subscriptions.map((subscription) => subscription.name)
  process.stdout.write(JSON.stringify({ subscribed, body: sent, outcome }))
}

await main()
