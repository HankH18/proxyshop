/**
 * The buyer journey shell — the wire it owns, and the four beats it walks a shopper through.
 *
 * Every request here is answered by an INJECTED fetcher. Nothing in this file touches the
 * network: the point of the suite is that the shell renders what a service said and refuses
 * to render anything else, and a test that reached a real service could not tell the two
 * apart when the service was down.
 *
 * The bodies below are shaped like the measured ones — `entries` with `fallback_reason`,
 * `excluded` with several `exclusion_reasons`, the exchange's permalink with its minted
 * `?discount=` code — because the assertions are about the shell surfacing those fields
 * verbatim, and a body invented without them would assert nothing.
 *
 * Interaction is driven with `fireEvent` rather than `user-event`, which this workspace does
 * not carry — same as `intent/intent-confirm.test.tsx` and `shortlist/shortlist.test.tsx`.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { CLARIFY_PATH, CONFIRM_PATH, type Intent } from '../intent/intent'
import { ACCEPT_PATH } from '../shortlist/shortlist'
import { Journey } from './Journey'
import {
  HttpFailure,
  MalformedResponseError,
  MissingProfileError,
  PSEUDONYM_PREFIX,
  RENDER_PATH,
  auctionPath,
  confirmWithProfile,
  describeTrust,
  detailFromBody,
  discountCodeFrom,
  explain,
  instrumentFetcher,
  loadAuction,
  mintPseudonym,
  permalinkHost,
  renderShortlist,
  type Fetcher,
} from './wire'

// `@testing-library/react` registers its own cleanup only when the runner exposes
// `afterEach` globally, and this workspace's vitest projects do not set `globals: true`.
afterEach(cleanup)

const AUCTION_ID = 'auc-demo-1'
const BID_REF = 'auc-demo-1:demo-woolworks'
const PERMALINK = 'https://demo-woolworks.example.com/cart/1:1?discount=PSX-MC4DM9A1'

const INTENT: Intent = {
  intent_id: 'int-demo-1',
  cluster_id: 'cl-4d3c3e4edadaa5e7',
  query: 'warm merino wool beanie for winter',
  budget_band: '100-250',
  hard_constraints: [
    { field: 'material', op: 'eq', value: 'merino-wool' },
    { field: 'price_usd', op: 'lte', value: 100, unit: 'USD' },
  ],
  preferences: [{ field: 'warmth', direction: 'maximize', weight: 0.6 }],
}

const RAW_SLOT = {
  slot: 'fit',
  bid_ref: BID_REF,
  fit_score: 0.88,
  provenance_labels: ['store-confirmed'],
  trust_summary: { score: 0.7, confidence: 0.4 },
  // The decoy the acceptance fixture ships: a slot may carry one and the buyer never
  // follows it. It is here so the "no href before accept" assertion has something to catch.
  checkout_url: 'https://evil.example.com/cart/hijack',
  claims: [{ field: 'material', value: 'merino-wool', provenance: { source: 'store_api' } }],
}

const RENDERED_SLOT = {
  slot: 'fit',
  bid_ref: BID_REF,
  auction_id: AUCTION_ID,
  fit_score: 0.88,
  provenance_labels: ['store-confirmed'],
  labels_source: 'exchange',
  // MEASURED off the running exchange: two of these three are not numbers, and
  // `TrustSummary` is `Record<string, number>`. The page must still show all three.
  trust_summary: { store_id: 'demo-woolworks', available: true, score: 0.82 },
  store_domain: '',
}

const ENTRIES = [
  {
    store_id: 'demo-woolworks',
    tier: 1,
    fallback: false,
    unit_price: 78.0,
    total_price: 78.0,
    fallback_reason: null,
  },
  {
    store_id: 'demo-slowreply',
    tier: 2,
    fallback: true,
    unit_price: 95.0,
    total_price: 95.0,
    fallback_reason: 'no_response',
  },
]

const EXCLUDED = [
  {
    bid_ref: 'auc-demo-1:demo-slowreply',
    store_id: 'demo-slowreply',
    exclusion_reasons: [
      "hard_constraint_unsatisfied: material is not 'merino-wool'",
      'price_over_budget: 95.0 is above 80.0',
    ],
  },
]

const DENIED = [
  {
    store_id: 'demo-blocked',
    status: 'unavailable',
    reason: "'demo-blocked' is not registered on this exchange (R12)",
  },
]

function auctionBody(slots: readonly unknown[]) {
  return {
    auction_id: AUCTION_ID,
    shortlist: { auction_id: AUCTION_ID, slots },
    entries: ENTRIES,
    excluded: EXCLUDED,
    denied: DENIED,
    ranked: [
      {
        bid_ref: BID_REF,
        store_id: 'demo-woolworks',
        rank_score: 0.81,
        components: { fit: 0.5, trust: 0.31 },
      },
    ],
    solicited: ['demo-woolworks', 'demo-slowreply'],
    recorded_at: '2026-09-05T00:00:01Z',
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

interface Call {
  readonly path: string
  readonly init?: RequestInit
}

/** A fetcher that records what it was asked and answers from a hand-written script. */
function recorder(
  handle: (path: string, init: RequestInit | undefined, nth: number) => Response,
): { readonly fetcher: Fetcher; readonly calls: readonly Call[] } {
  const calls: Call[] = []
  const fetcher: Fetcher = (path, init) => {
    const nth = calls.filter((call) => call.path === path).length
    calls.push({ path, init })
    return Promise.resolve(handle(path, init, nth))
  }
  return { fetcher, calls }
}

function bodyOf(init: RequestInit | undefined): unknown {
  const raw = init?.body
  return typeof raw === 'string' ? (JSON.parse(raw) as unknown) : undefined
}

const CLARIFY_ANSWER = {
  questions: ['What is your budget?'],
  intent: INTENT,
  unresolved: ['colour'],
  confirmed: false,
}

/** The happy path, with `slots` deciding whether the shortlist comes back empty. */
function demoService(options: { readonly slots?: readonly unknown[] } = {}) {
  const rawSlots = options.slots ?? [RAW_SLOT]
  const rendered = rawSlots.length === 0 ? [] : [RENDERED_SLOT]
  return recorder((path) => {
    switch (path) {
      case CLARIFY_PATH:
        return json(CLARIFY_ANSWER)
      case CONFIRM_PATH:
        return json(
          { auction_id: AUCTION_ID, intent_id: INTENT.intent_id, created_at: '2026-09-05T00:00:00Z' },
          201,
        )
      case auctionPath(AUCTION_ID):
        return json(auctionBody(rawSlots))
      case RENDER_PATH:
        return json({ slots: rendered })
      case ACCEPT_PATH:
        return json({
          permalink_url: PERMALINK,
          auction_id: AUCTION_ID,
          bid_ref: BID_REF,
          slot: 'fit',
          accepted_at: '2026-09-05T00:00:02Z',
        })
      default:
        return json({ detail: `nothing serves ${path}` }, 404)
    }
  })
}

/** Walk beats one and two: one utterance, one answer, and the confirm screen is up. */
async function walkToConfirm(): Promise<void> {
  fireEvent.change(screen.getByLabelText('What are you shopping for?'), {
    target: { value: 'I want a warm merino wool beanie for winter, under $100' },
  })
  fireEvent.submit(screen.getByLabelText('What are you shopping for?').closest('form')!)
  const question = await screen.findByLabelText('What is your budget?')
  fireEvent.change(question, { target: { value: 'about $100' } })
  fireEvent.submit(question.closest('form')!)
  await screen.findByRole('button', { name: /confirm and ask stores/i })
}

describe('the wire the journey owns', () => {
  it('GETs the auction by id and validates the diagnostics it renders', async () => {
    const { fetcher, calls } = recorder(() => json(auctionBody([RAW_SLOT])))

    const record = await loadAuction(AUCTION_ID, fetcher)

    expect(calls.map((call) => call.path)).toEqual([`/buyer/auctions/${AUCTION_ID}`])
    expect(calls[0]?.init?.method).toBe('GET')
    expect(record.auction_id).toBe(AUCTION_ID)
    expect(record.slot_count).toBe(1)
    expect(record.solicited).toEqual(['demo-woolworks', 'demo-slowreply'])
    expect(record.entries.map((entry) => entry.fallback_reason)).toEqual([null, 'no_response'])
    expect(record.excluded[0]?.exclusion_reasons).toHaveLength(2)
    expect(record.denied[0]?.store_id).toBe('demo-blocked')
    expect(record.ranked[0]?.components).toEqual({ fit: 0.5, trust: 0.31 })
  })

  it('names the status when the auction read is refused, and keeps the service message', async () => {
    const { fetcher } = recorder(() => json({ detail: 'no auction by that id' }, 404))

    await expect(loadAuction(AUCTION_ID, fetcher)).rejects.toThrowError(HttpFailure)
    await expect(loadAuction(AUCTION_ID, fetcher)).rejects.toThrowError(
      /load auction failed: HTTP 404 — no auction by that id/,
    )
  })

  it('refuses to render a 200 that carried no shortlist rather than showing an empty page', async () => {
    const { fetcher } = recorder(() => json({ auction_id: AUCTION_ID }))

    await expect(loadAuction(AUCTION_ID, fetcher)).rejects.toThrowError(MalformedResponseError)
  })

  it('forwards the shortlist to /render byte-for-byte, fields this client cannot name included', async () => {
    const { fetcher, calls } = recorder(() => json({ slots: [RENDERED_SLOT] }))
    const shortlist = { auction_id: AUCTION_ID, slots: [RAW_SLOT] }

    const slots = await renderShortlist(shortlist, fetcher)

    expect(calls[0]?.path).toBe(RENDER_PATH)
    expect(bodyOf(calls[0]?.init)).toEqual({ shortlist })
    // `claims` is what `render_shortlist` derives labels from when the exchange sent none.
    expect(JSON.stringify(bodyOf(calls[0]?.init))).toContain('claims')
    expect(slots[0]?.auction_id).toBe(AUCTION_ID)
    expect(slots[0]?.labels_source).toBe('exchange')
    expect(slots[0]?.provenance_labels).toEqual(['store-confirmed'])
    // `trust_summary` is narrowed to the numbers `ShortlistView`'s type demands...
    expect(slots[0]?.trust_summary).toEqual({ score: 0.82 })
    // ...and `trust_fields` keeps every field, so nothing the service said is dropped.
    expect(slots[0]?.trust_fields).toEqual({
      store_id: 'demo-woolworks',
      available: true,
      score: 0.82,
    })
  })

  it('spells every trust field the service sent, booleans distinct from strings', () => {
    expect(describeTrust({ store_id: 'demo-woolworks', available: true, score: 0.82 })).toBe(
      'store_id="demo-woolworks" available=true score=0.82',
    )
    expect(describeTrust({ available: 'true' })).toBe('available="true"')
    expect(describeTrust({})).toBe('no trust snapshot')
  })

  it('names the status when the render is refused', async () => {
    const { fetcher } = recorder(() => json({ detail: 'derive_missing_labels must be a bool' }, 422))

    await expect(renderShortlist({ slots: [] }, fetcher)).rejects.toThrowError(
      /render shortlist failed: HTTP 422/,
    )
  })

  it('reads the minted code off the exchange URL and never reconstructs one', () => {
    expect(discountCodeFrom(PERMALINK)).toBe('PSX-MC4DM9A1')
    expect(permalinkHost(PERMALINK)).toBe('demo-woolworks.example.com')
    expect(discountCodeFrom('https://demo-woolworks.example.com/cart/1:1')).toBeUndefined()
    expect(discountCodeFrom('not a url at all')).toBeUndefined()
    expect(discountCodeFrom(undefined)).toBeUndefined()
  })

  it('keeps the service message off a refusal without consuming the body the caller reads', async () => {
    const inner: Fetcher = () => Promise.resolve(json({ detail: 'no exchange client is wired' }, 503))
    const wire = instrumentFetcher(inner)

    const response = await wire.fetcher('/buyer/shortlist/accept', { method: 'POST' })

    expect(response.status).toBe(503)
    expect(response.bodyUsed).toBe(false)
    expect(await response.json()).toEqual({ detail: 'no exchange client is wired' })
    expect(wire.latest()).toEqual({
      path: '/buyer/shortlist/accept',
      status: 503,
      detail: 'no exchange client is wired',
    })
    wire.reset()
    expect(wire.latest()).toBeUndefined()
  })

  it('reads the message out of every detail shape the service uses', () => {
    expect(detailFromBody('{"detail":"plain"}')).toBe('plain')
    expect(detailFromBody('{"detail":{"message":"refused","denial_reason":"offer_expired"}}')).toBe(
      'refused (denial_reason="offer_expired")',
    )
    expect(detailFromBody('not json at all')).toBe('not json at all')
    expect(detailFromBody('   ')).toBe('')
  })

  it('sends the profile the exchange needs, with confirmed as a real boolean', async () => {
    const { fetcher, calls } = recorder(() =>
      json({ auction_id: AUCTION_ID, intent_id: INTENT.intent_id, created_at: '' }, 201),
    )

    const created = await confirmWithProfile(
      INTENT,
      { pseudonym: 'psn-0123456789', buckets: {} },
      fetcher,
    )

    expect(created.auction_id).toBe(AUCTION_ID)
    expect(calls[0]?.path).toBe(CONFIRM_PATH)
    const body = bodyOf(calls[0]?.init) as Record<string, unknown>
    // `contracts.BidRequest` makes `profile` required; without it every store agent 422s and
    // the exchange reports `no_response` for all of them.
    expect(body.profile).toEqual({ pseudonym: 'psn-0123456789', buckets: {} })
    // `StrictBool` on the service: the string "true" is a 422, and a lax bool would coerce it.
    expect(typeof body.confirmed).toBe('boolean')
    expect(body.confirmed).toBe(true)
  })

  it('refuses to confirm with no pseudonym rather than emptying the shortlist silently', async () => {
    const { fetcher, calls } = recorder(() => json({ auction_id: AUCTION_ID }, 201))

    await expect(
      confirmWithProfile(INTENT, { pseudonym: '   ', buckets: {} }, fetcher),
    ).rejects.toThrowError(MissingProfileError)
    expect(calls).toHaveLength(0)
  })

  it('mints a fresh rotating pseudonym rather than a stable identifier', () => {
    const minted = new Set(Array.from({ length: 32 }, () => mintPseudonym()))

    expect(minted.size).toBeGreaterThan(30)
    for (const handle of minted) {
      expect(handle.startsWith(PSEUDONYM_PREFIX)).toBe(true)
      expect(handle.slice(PSEUDONYM_PREFIX.length)).toMatch(/^[0-9a-f]{10}$/)
    }
  })

  it('joins a thrown status to the service message without inventing either', () => {
    const failed = new Error('accept failed: HTTP 503')
    expect(explain(failed, { path: ACCEPT_PATH, status: 503, detail: 'no client' })).toBe(
      'accept failed: HTTP 503 — the service said: no client',
    )
    expect(explain(failed, { path: ACCEPT_PATH, status: 503, detail: '' })).toBe(
      'accept failed: HTTP 503',
    )
    expect(explain(failed)).toBe('accept failed: HTTP 503')
  })
})

describe('the four beats', () => {
  it('walks say -> confirm -> shortlist -> checkout against the service answers', async () => {
    const { fetcher, calls } = demoService()
    const { container } = render(<Journey fetcher={fetcher} />)

    // Beat 1: the whole transcript goes across, every time.
    fireEvent.change(screen.getByLabelText('What are you shopping for?'), {
      target: { value: 'I want a warm merino wool beanie for winter, under $100' },
    })
    fireEvent.submit(screen.getByLabelText('What are you shopping for?').closest('form')!)

    // Beat 2: the outstanding question, and no confirm button while it is outstanding.
    const question = await screen.findByLabelText('What is your budget?')
    expect(screen.queryByRole('button', { name: /confirm and ask stores/i })).toBeNull()
    fireEvent.change(question, { target: { value: 'about $100' } })
    fireEvent.submit(question.closest('form')!)

    const confirmButton = await screen.findByRole('button', { name: /confirm and ask stores/i })
    expect(screen.getByTestId('intent-query').textContent).toBe(INTENT.query)
    expect(screen.getByTestId('unresolved').textContent).toContain('colour')
    expect(screen.getByTestId('cluster-id').textContent).toContain(INTENT.cluster_id)

    const clarifyBodies = calls
      .filter((call) => call.path === CLARIFY_PATH)
      .map((call) => bodyOf(call.init))
    expect(clarifyBodies).toEqual([
      { turns: ['I want a warm merino wool beanie for winter, under $100'] },
      { turns: ['I want a warm merino wool beanie for winter, under $100', 'about $100'] },
    ])

    // Beat 3: confirm, load the auction, render the labels.
    fireEvent.click(confirmButton)
    await screen.findByLabelText('Shortlist')

    expect(screen.getByTestId('slot-count').textContent).toContain('1 option')
    const label = screen.getByTestId(`label-${BID_REF}`)
    expect(label.textContent).toBe('store-confirmed')
    expect(label.getAttribute('data-tone')).toBe('confirmed')
    const provenance = screen.getByTestId(`labels-source-${BID_REF}`).textContent ?? ''
    expect(provenance).toContain('exchange')
    // The two fields `TrustSummary` cannot carry are on the page anyway.
    expect(provenance).toContain('store_id="demo-woolworks"')
    expect(provenance).toContain('available=true')
    expect(provenance).toContain('score=0.82')
    expect(screen.getByTestId('auction-id').textContent).toContain(AUCTION_ID)
    // Nothing is linkable before the exchange has minted a destination — and the slot's
    // decoy `checkout_url` is never turned into one.
    expect(container.querySelectorAll('a[href]')).toHaveLength(0)

    // Beat 4: accept, and the permalink the exchange minted.
    fireEvent.click(screen.getByRole('button', { name: /accept this one/i }))
    await screen.findByTestId('permalink-url')

    expect(screen.getByTestId('permalink-url').textContent).toBe(PERMALINK)
    expect(screen.getByTestId('permalink-host').textContent).toBe('demo-woolworks.example.com')
    expect(screen.getByTestId('discount-code').textContent).toBe('PSX-MC4DM9A1')

    const link = screen.getByTestId('permalink-link')
    expect(link.getAttribute('href')).toBe(PERMALINK)
    expect(container.querySelectorAll('a[href]')).toHaveLength(1)

    const accept = calls.find((call) => call.path === ACCEPT_PATH)
    expect(bodyOf(accept?.init)).toEqual({
      slot: { slot: 'fit', bid_ref: BID_REF, auction_id: AUCTION_ID },
      expected_domain: null,
    })
  })

  it('shows the exchange own report, verbatim, instead of an empty shortlist page', async () => {
    const { fetcher } = demoService({ slots: [] })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Why the shortlist is empty')

    expect(screen.getByTestId('solicited').textContent).toContain('demo-slowreply')

    const silent = screen.getByTestId('entry-demo-slowreply')
    expect(silent.textContent).toContain('fallback: true')
    expect(silent.textContent).toContain('fallback_reason: no_response')
    const answered = screen.getByTestId('entry-demo-woolworks')
    expect(answered.textContent).toContain('fallback: false')
    expect(answered.textContent).toContain('fallback_reason: null')

    expect(screen.getByTestId('no-response-gloss').textContent).toContain(
      'did not answer with a usable bid',
    )

    const excluded = screen.getByTestId('excluded-demo-slowreply')
    for (const reason of EXCLUDED[0]!.exclusion_reasons) {
      expect(excluded.textContent).toContain(reason)
    }

    expect(screen.getByTestId('denied-demo-blocked').textContent).toContain(DENIED[0]!.reason)
    expect(screen.getByTestId('verbatim-auction').textContent).toContain('recorded_at')
    // No fabricated row stood in for the missing options.
    expect(screen.queryByTestId(`slot-${BID_REF}`)).toBeNull()
    expect(screen.getByLabelText('Shortlist').textContent).toContain('No store was eligible')
  })

  it('shows the status and the service own words when a request is refused', async () => {
    const { fetcher } = recorder((path) => {
      if (path === CLARIFY_PATH) return json(CLARIFY_ANSWER)
      if (path === CONFIRM_PATH) {
        return json({ detail: 'no exchange client is wired; refusing to open an auction' }, 503)
      }
      return json({ detail: 'unreachable' }, 500)
    })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))

    const alert = await screen.findByTestId('journey-error')
    expect(alert.textContent).toContain('HTTP 503')
    expect(alert.textContent).toContain('no exchange client is wired; refusing to open an auction')
    expect(screen.queryByLabelText('Shortlist')).toBeNull()
  })

  it('refuses a permalink a browser must not follow instead of linking to it', async () => {
    const { fetcher } = recorder((path) => {
      switch (path) {
        case CLARIFY_PATH:
          return json(CLARIFY_ANSWER)
        case CONFIRM_PATH:
          return json({ auction_id: AUCTION_ID, intent_id: INTENT.intent_id, created_at: '' }, 201)
        case auctionPath(AUCTION_ID):
          return json(auctionBody([RAW_SLOT]))
        case RENDER_PATH:
          return json({ slots: [RENDERED_SLOT] })
        default:
          return json({
            permalink_url: 'javascript:alert(1)',
            auction_id: AUCTION_ID,
            bid_ref: BID_REF,
            slot: 'fit',
            accepted_at: '',
          })
      }
    })
    const { container } = render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')
    fireEvent.click(screen.getByRole('button', { name: /accept this one/i }))

    const alert = await screen.findByTestId('journey-error')
    expect(alert.textContent).toContain('refusing to follow this checkout permalink')
    expect(container.querySelectorAll('a[href]')).toHaveLength(0)
    expect(screen.queryByTestId('permalink-url')).toBeNull()
  })

  it('confirms with a per-visit pseudonym, and a second visit gets a different one', async () => {
    const first = demoService()
    render(<Journey fetcher={first.fetcher} />)
    const shown = screen.getByTestId('pseudonym').textContent ?? ''
    const handle = shown.match(/psn-[0-9a-f]{10}/)?.[0]
    expect(handle).toBeDefined()

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    const body = bodyOf(first.calls.find((call) => call.path === CONFIRM_PATH)?.init) as Record<
      string,
      unknown
    >
    expect(body.profile).toEqual({ pseudonym: handle, buckets: {} })
    expect(typeof body.confirmed).toBe('boolean')
    expect(body.confirmed).toBe(true)

    // A second visit is a second handle: nothing is persisted for a store to join on.
    cleanup()
    const second = demoService()
    render(<Journey fetcher={second.fetcher} />)
    const again = (screen.getByTestId('pseudonym').textContent ?? '').match(/psn-[0-9a-f]{10}/)?.[0]
    expect(again).toBeDefined()
    expect(again).not.toBe(handle)
  })

  it('states the three gaps permanently, and asks for no email it could never redeem', async () => {
    const { fetcher } = demoService()
    render(<Journey fetcher={fetcher} />)

    const signin = screen.getByTestId('gap-signin')
    expect(signin.textContent).toContain('POST /buyer/auth/magic-link')
    expect(signin.textContent).toContain('202')
    expect(signin.textContent).toContain('never returns')
    expect(screen.getByTestId('gap-domain').textContent).toContain('store_domain')
    const minted = screen.getByTestId('gap-pseudonym').textContent ?? ''
    expect(minted).toContain('POST /buyer/auth/session')
    expect(minted).toContain('generated in this browser')
    expect(screen.queryByLabelText(/email/i)).toBeNull()

    // Still there at the end of the journey, not only at the start.
    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')
    fireEvent.click(screen.getByRole('button', { name: /accept this one/i }))
    await screen.findByTestId('permalink-url')

    await waitFor(() => expect(screen.getByTestId('gap-signin')).toBeInTheDocument())
    expect(screen.getByTestId('gap-domain')).toBeInTheDocument()
    expect(screen.getByTestId('gap-pseudonym')).toBeInTheDocument()
  })
})
