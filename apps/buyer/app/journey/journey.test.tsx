/**
 * The buyer journey shell — the wire it owns, and the four beats it walks a shopper through.
 *
 * Every request here is answered by an INJECTED fetcher. Nothing in this file touches the
 * network: the point of the suite is that the shell renders what a service said and refuses
 * to render anything else, and a test that reached a real service could not tell the two
 * apart when the service was down.
 *
 * The bodies below are not shaped like the measured ones, they ARE the measured ones. Every
 * store id is one of the three in `apps/buyer/devstack/demo-market.json`; every exclusion
 * reason begins with a prefix from `apps/exchange/src/ranking/reasons.py` and carries the
 * detail that filter actually formats after it; every `components` key is one of
 * `contracts.ranking.RANK_FEATURES`; the denial reason is what `exchange.eligibility` writes
 * and `orchestration.solicitation._denial` prefixes. A fixture that invents the shape of the
 * thing it tests tests nothing — the assertions here are that the shell surfaces these
 * fields verbatim, and a made-up field cannot be surfaced verbatim.
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
  describeComponents,
  describeTrust,
  detailFromBody,
  discountCodeFrom,
  entryForSlot,
  explain,
  instrumentFetcher,
  loadAuction,
  mintPseudonym,
  permalinkHost,
  rankedForSlot,
  renderShortlist,
  storeIdFromBidRef,
  type Fetcher,
} from './wire'

// `@testing-library/react` registers its own cleanup only when the runner exposes
// `afterEach` globally, and this workspace's vitest projects do not set `globals: true`.
afterEach(cleanup)

const AUCTION_ID = 'auc-demo-1'
// `{auction_id}:{store_id}`, because that is literally what the exchange mints:
// `apps/exchange/src/ranking/candidates.py::mint_bid_id` is `f"{auction_id}:{store_id}"`.
// The price join in `wire.ts` reads the store id back out of it, so the two spellings below
// are the one fact the join depends on and are written out rather than concatenated.
const BID_REF = 'auc-demo-1:demo-woolworks'
const FASTFLEECE_BID_REF = 'auc-demo-1:demo-fastfleece'
const PERMALINK = 'https://demo-woolworks.example.com/cart/1:1?discount=PSX-MC4DM9A1'
const CREATED_AT = '2026-09-05T00:00:00Z'
const RECORDED_AT = '2026-09-05T00:00:01Z'

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
  fit_score: 0.564,
  provenance_labels: ['store-confirmed'],
  // MEASURED off the running exchange, and demo-woolworks' own `trust.score` in the market
  // file is the 0.82. There is no `confidence` field on a trust summary.
  trust_summary: { store_id: 'demo-woolworks', available: true, score: 0.82 },
  // The decoy the acceptance fixture ships: a slot may carry one and the buyer never
  // follows it. It is here so the "no href before accept" assertion has something to catch.
  checkout_url: 'https://evil.example.com/cart/hijack',
  claims: [{ field: 'material', value: 'merino-wool', provenance: { source: 'store_api' } }],
}

const RENDERED_SLOT = {
  slot: 'fit',
  bid_ref: BID_REF,
  auction_id: AUCTION_ID,
  fit_score: 0.564,
  provenance_labels: ['store-confirmed'],
  labels_source: 'exchange',
  // MEASURED off the running exchange: two of these three are not numbers, and
  // `TrustSummary` is `Record<string, number>`. The page must still show all three.
  trust_summary: { store_id: 'demo-woolworks', available: true, score: 0.82 },
  store_domain: '',
}

// The two stores that were solicited, with the prices `apps/buyer/devstack/demo-market.json`
// gives them. demo-woolworks bids its catalogue price; demo-fastfleece's agent does not
// answer, so the exchange represents it at its own list price and says `no_response` — which
// is the one `fallback_reason` `wire.ts` recognises by name.
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
    store_id: 'demo-fastfleece',
    tier: 1,
    fallback: true,
    unit_price: 45.0,
    total_price: 45.0,
    fallback_reason: 'no_response',
  },
]

// demo-fastfleece earns both, and both are real. `blacklisted_store` is
// `ranking/filters.py`'s own f-string for a store the trust snapshot blacklists (R12) — the
// market file marks this one `"blacklisted": true`. `hard_constraint_unsatisfied` is
// `hard_constraint_reasons` wrapped around `HardCriterion.decide`'s verdict, measured by
// running that criterion against a fleece attribute. There is no `price_over_budget` prefix
// on this exchange; `EXCLUSION_REASON_PREFIXES` names the eight that exist and that is not
// one of them.
const EXCLUDED = [
  {
    bid_ref: FASTFLEECE_BID_REF,
    store_id: 'demo-fastfleece',
    exclusion_reasons: [
      "blacklisted_store: 'demo-fastfleece' is blacklisted and may not participate (R12)",
      "hard_constraint_unsatisfied: 'material' eq 'merino-wool' is not satisfied by " +
        "['fleece'] — only a verified supporting claim satisfies a hard constraint (R19)",
    ],
  },
]

// A denial is not an exclusion: a denied store is never solicited, so it has no bid to
// exclude. demo-alpine-supply is the market's third store, and this is the reason string
// `exchange.eligibility.StaticSellerEligibility.check` writes for `unavailable` once
// `orchestration.solicitation._denial` has put the status word in front of it.
const DENIED = [
  {
    store_id: 'demo-alpine-supply',
    status: 'unavailable',
    reason: 'unavailable: static-eligibility: demo-alpine-supply is unavailable',
  },
]

// MEASURED. The five keys are `contracts.ranking.RANK_FEATURES` — there is no `fit` or bare
// `trust`-plus-`fit` pair anywhere in the formula — and the five values sum to `rank_score`,
// which is what makes them components rather than decoration.
const RANKED = [
  {
    bid_ref: BID_REF,
    store_id: 'demo-woolworks',
    rank_score: 0.564,
    components: {
      intent_match: 0.175,
      verified_claim_ratio: 0.1,
      trust: 0.164,
      price_value: 0.075,
      delivery_fit: 0.05,
    },
  },
]

function auctionBody(
  slots: readonly unknown[],
  overrides: Partial<Record<'recorded_at', string>> = {},
) {
  return {
    auction_id: AUCTION_ID,
    shortlist: { auction_id: AUCTION_ID, slots },
    entries: ENTRIES,
    excluded: EXCLUDED,
    denied: DENIED,
    ranked: RANKED,
    solicited: ['demo-woolworks', 'demo-fastfleece'],
    recorded_at: overrides.recorded_at ?? RECORDED_AT,
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
function demoService(
  options: {
    readonly slots?: readonly unknown[]
    /** What `/render` answers with. Defaults to the one slot `slots` implies. */
    readonly rendered?: readonly unknown[]
    /** `shortlist: null` — the exchange's TTL took the auction away. Not an empty one. */
    readonly forgotten?: boolean
    /** `''` is a real answer: `confirmWithProfile` returns it when the service sent none. */
    readonly createdAt?: string
    /** `''` is a real answer too: the buyer service types `recorded_at` as nullable. */
    readonly recordedAt?: string
  } = {},
) {
  const rawSlots = options.slots ?? [RAW_SLOT]
  const rendered = options.rendered ?? (rawSlots.length === 0 ? [] : [RENDERED_SLOT])
  const createdAt = options.createdAt ?? CREATED_AT
  const recordedAt = options.recordedAt ?? RECORDED_AT
  return recorder((path) => {
    switch (path) {
      case CLARIFY_PATH:
        return json(CLARIFY_ANSWER)
      case CONFIRM_PATH:
        return json(
          { auction_id: AUCTION_ID, intent_id: INTENT.intent_id, created_at: createdAt },
          201,
        )
      case auctionPath(AUCTION_ID): {
        const body = auctionBody(rawSlots, { recorded_at: recordedAt })
        return json(options.forgotten === true ? { ...body, shortlist: null } : body)
      }
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
    expect(record.solicited).toEqual(['demo-woolworks', 'demo-fastfleece'])
    expect(record.entries.map((entry) => entry.fallback_reason)).toEqual([null, 'no_response'])
    expect(record.excluded[0]?.exclusion_reasons).toHaveLength(2)
    expect(record.excluded[0]?.exclusion_reasons[0]).toContain('blacklisted_store:')
    expect(record.denied[0]?.store_id).toBe('demo-alpine-supply')
    // The exchange's own five, not two this test made up.
    expect(record.ranked[0]?.components).toEqual({
      intent_match: 0.175,
      verified_claim_ratio: 0.1,
      trust: 0.164,
      price_value: 0.075,
      delivery_fit: 0.05,
    })
    expect(record.recorded_at).toBe(RECORDED_AT)
  })

  it('joins a slot to its price through the store id the exchange minted into the bid ref', async () => {
    const { fetcher } = recorder(() => json(auctionBody([RAW_SLOT])))

    const record = await loadAuction(AUCTION_ID, fetcher)

    // `mint_bid_id` is `f"{auction_id}:{store_id}"`, so the store id is what follows the
    // auction id and its colon — matched as a prefix, never split on the first `:`.
    expect(storeIdFromBidRef(BID_REF, AUCTION_ID)).toBe('demo-woolworks')
    expect(storeIdFromBidRef(FASTFLEECE_BID_REF, AUCTION_ID)).toBe('demo-fastfleece')
    // A ref some other auction minted names no store THIS page may attribute a price to.
    expect(storeIdFromBidRef('auc-other:demo-woolworks', AUCTION_ID)).toBeUndefined()
    expect(storeIdFromBidRef(`${AUCTION_ID}:`, AUCTION_ID)).toBeUndefined()
    expect(storeIdFromBidRef(undefined, AUCTION_ID)).toBeUndefined()

    expect(entryForSlot(record, BID_REF)?.unit_price).toBe(78)
    expect(entryForSlot(record, BID_REF)?.total_price).toBe(78)
    expect(entryForSlot(record, BID_REF)?.fallback).toBe(false)
    expect(entryForSlot(record, FASTFLEECE_BID_REF)?.fallback).toBe(true)
    // No entry for this store: `undefined`, so the page can say so instead of showing a zero.
    expect(entryForSlot(record, `${AUCTION_ID}:demo-alpine-supply`)).toBeUndefined()

    expect(rankedForSlot(record, BID_REF)?.rank_score).toBe(0.564)
    expect(rankedForSlot(record, FASTFLEECE_BID_REF)).toBeUndefined()
  })

  it('leaves an unreadable rank_score undefined rather than defaulting it to a zero', async () => {
    // A zero would print as "rank_score 0", which reads as the exchange having scored this
    // candidate at the bottom. It did not; this client just found no number.
    const body = {
      ...auctionBody([RAW_SLOT]),
      ranked: [{ bid_ref: BID_REF, store_id: 'demo-woolworks', components: RANKED[0]!.components }],
    }
    const { fetcher } = recorder(() => json(body))

    const record = await loadAuction(AUCTION_ID, fetcher)

    expect(record.ranked[0]?.rank_score).toBeUndefined()
    expect(record.ranked[0]?.rank_score).not.toBe(0)
    // The components the exchange DID publish are still all there.
    expect(record.ranked[0]?.components).toEqual(RANKED[0]!.components)
  })

  it('spells the exchange own ranking components and never a formula of its own', () => {
    expect(
      describeComponents({
        intent_match: 0.175,
        verified_claim_ratio: 0.1,
        trust: 0.164,
        price_value: 0.075,
        delivery_fit: 0.05,
      }),
    ).toBe(
      'intent_match=0.175 verified_claim_ratio=0.1 trust=0.164 price_value=0.075 ' +
        'delivery_fit=0.05',
    )
    // A term this build has never heard of still prints; nothing here spells the key set.
    expect(describeComponents({ some_future_term: 0.5 })).toBe('some_future_term=0.5')
    expect(describeComponents({})).toBe('the exchange published no components')
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

  it('keeps a forgotten shortlist, an empty one and a malformed body three different facts', async () => {
    // 1. `shortlist: null` — `buyer_svc/auctions/routes.py` writes exactly this when the
    //    exchange's 15-minute TTL has taken the auction away. The recorded rows survive.
    const gone = recorder(() => json({ ...auctionBody([]), shortlist: null }))
    const forgotten = await loadAuction(AUCTION_ID, gone.fetcher)
    expect(forgotten.liveness).toBe('forgotten')
    expect(forgotten.shortlist).toBeNull()
    // Not a shortlist with no slots — and the diagnostics are still all there.
    expect(forgotten.entries).toHaveLength(ENTRIES.length)
    expect(forgotten.solicited).toEqual(['demo-woolworks', 'demo-fastfleece'])

    // 2. A shortlist with no slots. A fact about the market, not about the exchange.
    const barren = recorder(() => json(auctionBody([])))
    const empty = await loadAuction(AUCTION_ID, barren.fetcher)
    expect(empty.liveness).toBe('live')
    expect(empty.shortlist).toEqual({ auction_id: AUCTION_ID, slots: [] })

    // 3. No `shortlist` key at all. An ABSENT field is not an explicit `null`, and reading
    //    it as "the exchange forgot it" would invent a reason the service never gave.
    const malformed = recorder(() => json({ auction_id: AUCTION_ID, entries: ENTRIES }))
    await expect(loadAuction(AUCTION_ID, malformed.fetcher)).rejects.toThrowError(
      MalformedResponseError,
    )
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
    expect(wire.latest()).toEqual({ status: 503, detail: 'no exchange client is wired' })
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
    expect(explain(failed, { status: 503, detail: 'no client' })).toBe(
      'accept failed: HTTP 503 — the service said: no client',
    )
    expect(explain(failed, { status: 503, detail: '' })).toBe('accept failed: HTTP 503')
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
    // The exchange's published ranking, its own five component keys, its own numbers.
    expect(provenance).toContain('rank_score 0.564')
    expect(provenance).toContain('intent_match=0.175')
    expect(provenance).toContain('delivery_fit=0.05')

    // The PRICE, which the slot itself does not carry: read out of `entries[]` by the store
    // id in the bid ref, printed exactly as it arrived, and labelled as a bid rather than as
    // a number this page worked out.
    expect(screen.getByTestId(`price-${BID_REF}`).textContent).toBe(
      'unit 78, total 78 — the price this store bid, as the exchange reported this auction.',
    )
    expect(screen.getByTestId('slot-prices').textContent).toContain('demo-woolworks')
    expect(screen.getByTestId('price-provenance').textContent).toContain('entries[]')
    // No currency symbol anywhere: the exchange named none, so this page names none.
    expect(screen.getByTestId('slot-prices').textContent).not.toContain('$')

    // The whole transcript is on the page, oldest first, as the buyer said it.
    expect(screen.getByTestId('transcript').textContent).toContain(
      'I want a warm merino wool beanie for winter, under $100',
    )
    expect(screen.getByTestId('transcript').textContent).toContain('about $100')

    const opened = screen.getByTestId('auction-id').textContent ?? ''
    expect(opened).toContain(AUCTION_ID)
    // The service's own clocks, and no invented stand-in for a clock it did not send.
    expect(opened).toContain(CREATED_AT)
    expect(opened).toContain(RECORDED_AT)
    expect(opened).not.toContain('just now')
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

    expect(screen.getByTestId('solicited').textContent).toContain('demo-fastfleece')

    const silent = screen.getByTestId('entry-demo-fastfleece')
    expect(silent.textContent).toContain('fallback: true')
    expect(silent.textContent).toContain('fallback_reason: no_response')
    expect(silent.textContent).toContain('unit 45, total 45')
    const answered = screen.getByTestId('entry-demo-woolworks')
    expect(answered.textContent).toContain('fallback: false')
    expect(answered.textContent).toContain('fallback_reason: null')

    expect(screen.getByTestId('no-response-gloss').textContent).toContain(
      'did not answer with a usable bid',
    )

    const excluded = screen.getByTestId('excluded-demo-fastfleece')
    for (const reason of EXCLUDED[0]!.exclusion_reasons) {
      expect(excluded.textContent).toContain(reason)
    }

    expect(screen.getByTestId('denied-demo-alpine-supply').textContent).toContain(
      DENIED[0]!.reason,
    )

    // The three lists exist as lists, each carrying exactly the rows the service sent and no
    // row this page padded them out with.
    expect(screen.getByTestId('entries').querySelectorAll(':scope > li')).toHaveLength(
      ENTRIES.length,
    )
    expect(screen.getByTestId('excluded').querySelectorAll(':scope > li')).toHaveLength(
      EXCLUDED.length,
    )
    expect(screen.getByTestId('denied').querySelectorAll(':scope > li')).toHaveLength(
      DENIED.length,
    )
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

  it('states the five gaps permanently, and asks for no email it could never redeem', async () => {
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

    // The slot carries no price either, so the page says where the price it shows came from.
    const price = screen.getByTestId('gap-price').textContent ?? ''
    expect(price).toContain('carries no price field')
    expect(price).toContain('entries[]')
    expect(price).toContain('bid_ref')

    // And the questions came from D20's offline double, which is measurable rather than
    // asserted: `build_llm("buyer")` with LLM_PROVIDER unset returns
    // `<DeterministicLLM role='buyer' calls=0>` with `model='double:buyer'`.
    const model = screen.getByTestId('gap-model').textContent ?? ''
    expect(model).toContain('build_llm("buyer")')
    expect(model).toContain('LLM_PROVIDER')
    expect(model).toContain('DeterministicLLM')
    expect(model).toContain('double:buyer')
    expect(model).toContain('no live model')

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
    expect(screen.getByTestId('gap-price')).toBeInTheDocument()
    expect(screen.getByTestId('gap-model')).toBeInTheDocument()
  })

  it('prints the clock the service sent, and nothing at all when it sent none', async () => {
    const { fetcher } = demoService({ createdAt: '', recordedAt: '' })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    // The service reported no time, so the page reports no time. It does not say 'just now',
    // which would be this browser's clock wearing the service's voice.
    const line = screen.getByTestId('auction-id').textContent ?? ''
    expect(line).toContain(AUCTION_ID)
    expect(line).not.toContain('just now')
    expect(line).not.toContain('opened')
    expect(line).not.toContain('recorded that answer')
  })

  it('says a slot has no reported price rather than showing a blank or a zero', async () => {
    // A rendered slot for demo-alpine-supply, which this auction DENIED and therefore never
    // solicited — so it is in no `entries[]` row and there is no price to join to it.
    const orphan = { ...RENDERED_SLOT, bid_ref: `${AUCTION_ID}:demo-alpine-supply` }
    const { fetcher } = demoService({ rendered: [orphan] })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    const cell = screen.getByTestId(`price-${orphan.bid_ref}`)
    expect(cell.textContent).toBe('price not reported for this slot')
    expect(cell.textContent).not.toBe('')
    expect(cell.textContent).not.toContain('0')
    // The ranking published no row for it either, and that is said rather than left blank.
    expect(screen.getByTestId(`labels-source-${orphan.bid_ref}`).textContent).toContain(
      'rank_score not published for this slot',
    )
  })

  it('prints a zero price and says what a zero there can also mean', async () => {
    // MEASURED: `apps/exchange/src/auction/routes.py::_entries_out` builds this field as
    // `float(offer.get("unit_price", 0.0))`, so an offer that named no price arrives as a
    // real 0.0. The page prints the number it was sent — it may not round it away or hide
    // it — and says what a zero there can also mean, because "unit 0" alone reads as free.
    const zeroed = [{ ...ENTRIES[0]!, unit_price: 0.0, total_price: 0.0 }]
    const { fetcher } = recorder((path) => {
      switch (path) {
        case CLARIFY_PATH:
          return json(CLARIFY_ANSWER)
        case CONFIRM_PATH:
          return json(
            { auction_id: AUCTION_ID, intent_id: INTENT.intent_id, created_at: CREATED_AT },
            201,
          )
        case auctionPath(AUCTION_ID):
          return json({ ...auctionBody([RAW_SLOT]), entries: zeroed })
        case RENDER_PATH:
          return json({ slots: [RENDERED_SLOT] })
        default:
          return json({ detail: `nothing serves ${path}` }, 404)
      }
    })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    const cell = screen.getByTestId(`price-${BID_REF}`).textContent ?? ''
    expect(cell).toContain('unit 0, total 0')
    expect(cell).toContain('an offer that named no price')
    // Not swallowed into "not reported": the service did send a number, and it is shown.
    expect(cell).not.toBe('price not reported for this slot')
  })

  it('says the exchange has forgotten the auction, and offers nothing to accept', async () => {
    const { fetcher, calls } = demoService({ forgotten: true })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByTestId('shortlist-forgotten')

    const notice = screen.getByTestId('shortlist-forgotten').textContent ?? ''
    expect(notice).toContain('no longer holds this auction')
    expect(notice).toContain('fifteen minutes')
    // The distinction the whole outcome exists for: this is the shortlist expiring, NOT the
    // market coming back empty, and the page says which.
    expect(notice).toContain('not an empty market')

    // No shortlist section at all, so no Accept button a buyer could press into a refusal.
    expect(screen.queryByLabelText('Shortlist')).toBeNull()
    expect(screen.queryByRole('button', { name: /accept this one/i })).toBeNull()

    // The recorded diagnostics ARE shown, labelled as a record rather than as live.
    await screen.findByLabelText('What the exchange reported when this auction ran')
    expect(screen.getByTestId('recorded-not-live').textContent).toContain('None of this is live')
    expect(screen.getByTestId('entry-demo-woolworks').textContent).toContain('fallback: false')
    expect(screen.getByTestId('denied-demo-alpine-supply')).toBeInTheDocument()
    // ...and NOT under the empty-market heading, which would be a different claim.
    expect(screen.queryByLabelText('Why the shortlist is empty')).toBeNull()

    // The count sentence does not report "0 options" for a shortlist that is simply gone.
    const line = screen.getByTestId('auction-id').textContent ?? ''
    expect(line).toContain('no longer holds the shortlist it answered with')
    expect(line).not.toContain('0 options')

    // `/render` labels a shortlist; there is none, so it was never called.
    expect(calls.map((call) => call.path)).not.toContain(RENDER_PATH)
  })

  it('tells a forgotten shortlist, an empty one and an unreadable one apart on the page', async () => {
    // Empty: the Shortlist section is there and says the MARKET had nothing.
    const barren = demoService({ slots: [] })
    render(<Journey fetcher={barren.fetcher} />)
    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Why the shortlist is empty')
    expect(screen.getByLabelText('Shortlist').textContent).toContain('No store was eligible')
    expect(screen.queryByTestId('shortlist-forgotten')).toBeNull()
    cleanup()

    // Forgotten: no Shortlist section, and the notice instead.
    const gone = demoService({ forgotten: true })
    render(<Journey fetcher={gone.fetcher} />)
    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByTestId('shortlist-forgotten')
    expect(screen.queryByLabelText('Shortlist')).toBeNull()
    expect(screen.queryByLabelText('Why the shortlist is empty')).toBeNull()
    cleanup()

    // Unreadable: neither claim is made, and the failure names the status and the reason.
    const broken = recorder((path) => {
      if (path === CLARIFY_PATH) return json(CLARIFY_ANSWER)
      if (path === CONFIRM_PATH) {
        return json({ auction_id: AUCTION_ID, intent_id: INTENT.intent_id, created_at: '' }, 201)
      }
      if (path === auctionPath(AUCTION_ID)) return json({ auction_id: AUCTION_ID })
      return json({ detail: 'unreachable' }, 500)
    })
    render(<Journey fetcher={broken.fetcher} />)
    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    const alert = await screen.findByTestId('journey-error')
    expect(alert.textContent).toContain('a body this screen cannot render')
    expect(screen.queryByTestId('shortlist-forgotten')).toBeNull()
    expect(screen.queryByLabelText('Shortlist')).toBeNull()
    expect(screen.queryByLabelText('Why the shortlist is empty')).toBeNull()
  })

  it('says each diagnostic list was empty rather than padding it with a row', async () => {
    const bare = {
      auction_id: AUCTION_ID,
      shortlist: { auction_id: AUCTION_ID, slots: [] },
      entries: [],
      excluded: [],
      denied: [],
      ranked: [],
      solicited: [],
      recorded_at: RECORDED_AT,
    }
    const { fetcher } = recorder((path) => {
      switch (path) {
        case CLARIFY_PATH:
          return json(CLARIFY_ANSWER)
        case CONFIRM_PATH:
          return json(
            { auction_id: AUCTION_ID, intent_id: INTENT.intent_id, created_at: CREATED_AT },
            201,
          )
        case auctionPath(AUCTION_ID):
          return json(bare)
        case RENDER_PATH:
          return json({ slots: [] })
        default:
          return json({ detail: `nothing serves ${path}` }, 404)
      }
    })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Why the shortlist is empty')

    expect(screen.getByTestId('solicited-empty').textContent).toContain('asked no store at all')
    expect(screen.getByTestId('entries-empty').textContent).toContain('No store answered')
    expect(screen.getByTestId('excluded-empty').textContent).toContain('refused nothing')
    expect(screen.getByTestId('denied-empty').textContent).toContain('Every rostered store')
    // Four empty lists and not one invented row anywhere.
    expect(screen.queryByTestId('solicited')).toBeNull()
    expect(screen.queryByTestId('entries')).toBeNull()
    expect(screen.queryByTestId('excluded')).toBeNull()
    expect(screen.queryByTestId('denied')).toBeNull()
    expect(screen.queryByTestId('no-response-gloss')).toBeNull()
  })
})
