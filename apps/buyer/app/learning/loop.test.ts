/**
 * The wire half of the learning demo.
 *
 * What these tests are really pinning is the two properties that make the page a
 * demonstration rather than a slide:
 *
 *  1. **`compare` is arithmetic.** It is handed two readings and has no idea where either
 *     came from, so it cannot invent a difference. The test that matters most here is the one
 *     where the two readings are the same: it must report nothing moved.
 *  2. **Nothing unmarked is ever sent.** `POST /buyer/feedback` writes to an append-only
 *     ledger with no delete, so the refusal has to happen before the request, and the test
 *     asserts on the FETCHER never being called rather than on a message.
 */
import { describe, expect, it, vi } from 'vitest'

import { SEEDED_PREFIX } from '../journey/seeded-feedback'
import {
  ACCEPT_PATH,
  CLARIFY_PATH,
  CONFIRM_PATH,
  FEEDBACK_PATH,
  PROMPT_PATH,
  UnmarkedOutcome,
  WireFailure,
  anythingMoved,
  assertSeeded,
  auctionPath,
  biddingStores,
  buyTopSlot,
  compare,
  discountOffered,
  figure,
  outcomeIsPositive,
  readAuction,
  refusalDetail,
  runQuery,
  seededOrder,
  seededOrderRef,
  signed,
  submitOutcome,
  type Fetcher,
  type Reading,
} from './loop'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

const COLD = {
  auction_id: 'auc-1',
  recorded_at: '2026-09-08T08:00:00Z',
  shortlist: {
    slots: [
      { slot: 'fit', bid_ref: 'auc-1:gaia', price: { unit_price: 25.49, discount: null } },
      { slot: 'value', bid_ref: 'auc-1:toniiq', price: { unit_price: 20.97, discount: null } },
    ],
  },
  entries: [
    { store_id: 'gaia', fallback: false, fallback_reason: null, unit_price: 25.49 },
    { store_id: 'toniiq', fallback: false, fallback_reason: null, unit_price: 20.97 },
    { store_id: 'nutricost', fallback: true, fallback_reason: 'no_response', unit_price: 15.97 },
  ],
  ranked: [
    { bid_ref: 'auc-1:gaia', store_id: 'gaia', rank_score: 0.497, components: { trust: 0.172, price_value: 0 } },
    { bid_ref: 'auc-1:toniiq', store_id: 'toniiq', rank_score: 0.473, components: { trust: 0.148, price_value: 0 } },
    { bid_ref: 'auc-1:nutricost', store_id: 'nutricost', rank_score: 0.469, components: { trust: 0.144, price_value: 0 } },
  ],
}

const WARM = {
  ...COLD,
  auction_id: 'auc-2',
  shortlist: {
    slots: [
      {
        slot: 'fit',
        bid_ref: 'auc-2:toniiq',
        price: { unit_price: 16.776, discount: { type: 'percentage', value: 20 } },
      },
    ],
  },
  entries: [
    { store_id: 'toniiq', fallback: false, fallback_reason: null, unit_price: 16.776 },
    { store_id: 'gaia', fallback: false, fallback_reason: null, unit_price: 25.49 },
    { store_id: 'nutricost', fallback: true, fallback_reason: 'no_response', unit_price: 15.97 },
  ],
  ranked: [
    { bid_ref: 'auc-2:toniiq', store_id: 'toniiq', rank_score: 0.5296, components: { trust: 0.148, price_value: 0.0566 } },
    { bid_ref: 'auc-2:gaia', store_id: 'gaia', rank_score: 0.497, components: { trust: 0.172, price_value: 0 } },
    { bid_ref: 'auc-2:nutricost', store_id: 'nutricost', rank_score: 0.469, components: { trust: 0.144, price_value: 0 } },
  ],
}

async function reading(body: unknown): Promise<Reading> {
  return readAuction('auc-1', () => Promise.resolve(json(body)))
}

describe('reading one auction', () => {
  it('joins the published ranking to the recorded offer of the same store', async () => {
    const cold = await reading(COLD)
    expect(cold.candidates.map((row) => row.store_id)).toEqual(['gaia', 'toniiq', 'nutricost'])
    expect(cold.candidates[0]?.unit_price).toBe(25.49)
    expect(cold.candidates[2]?.fallback).toBe(true)
    expect(cold.candidates[2]?.fallback_reason).toBe('no_response')
  })

  it('keeps every component key the exchange published, and invents none', async () => {
    const withPenalty = await reading({
      ...COLD,
      ranked: [
        {
          bid_ref: 'auc-1:gaia',
          store_id: 'gaia',
          rank_score: 0.347,
          components: { trust: 0.172, price_value: 0, policy_penalties: -0.15 },
        },
      ],
    })
    // A sixth term shows rather than being swallowed: `policy_penalties` is the one that is a
    // punishment, and a page that listed only the five published features would hide it.
    expect(Object.keys(withPenalty.candidates[0]?.components ?? {}).sort()).toEqual([
      'policy_penalties',
      'price_value',
      'trust',
    ])
  })

  it('reports a non-OK answer with the service’s own words', async () => {
    const fetcher: Fetcher = () => Promise.resolve(json({ detail: 'auction not found' }, 404))
    await expect(readAuction('auc-x', fetcher)).rejects.toThrow(/404.*auction not found/)
  })

  it('says the body was not JSON rather than letting a parse error escape', async () => {
    // This is what nginx's SPA fallback actually answers for a path outside `/buyer/`: a 200
    // whose body is index.html. `response.json()` on it throws "unexpected token <".
    const fetcher: Fetcher = () =>
      Promise.resolve(new Response('<!doctype html><title>x</title>', { status: 200 }))
    await expect(readAuction('auc-x', fetcher)).rejects.toThrow(/the body was not JSON/)
  })
})

describe('running a query', () => {
  it('clarifies, confirms with the literal boolean, then reads the auction back', async () => {
    const seen: { path: string; body: unknown }[] = []
    const fetcher: Fetcher = (path, init) => {
      const body = init?.body === undefined ? null : (JSON.parse(String(init.body)) as unknown)
      seen.push({ path: String(path), body })
      if (path === CLARIFY_PATH) return Promise.resolve(json({ intent: { intent_id: 'i1' } }))
      if (path === CONFIRM_PATH) return Promise.resolve(json({ auction_id: 'auc-1' }, 201))
      return Promise.resolve(json(COLD))
    }
    const result = await runQuery('milk thistle', fetcher, 60)
    expect(seen.map((row) => row.path)).toEqual([CLARIFY_PATH, CONFIRM_PATH, auctionPath('auc-1')])
    expect(seen[0]?.body).toEqual({ turns: ['milk thistle', 'nothing else, no must-haves'] })
    // `confirmed` must be the boolean. `buyer_svc` types it `StrictBool`, so the string
    // "true" is a 422 rather than something a lax field would coerce into an open auction.
    expect((seen[1]?.body as { confirmed: unknown }).confirmed).toBe(true)
    expect((seen[1]?.body as { bid_timeout_seconds: unknown }).bid_timeout_seconds).toBe(60)
    expect(result.auction_id).toBe('auc-1')
  })

  it('refuses a confirm that named no auction rather than reading an empty id', async () => {
    const fetcher: Fetcher = (path) => {
      if (path === CLARIFY_PATH) return Promise.resolve(json({ intent: {} }))
      return Promise.resolve(json({ intent_id: 'i1' }, 201))
    }
    await expect(runQuery('milk thistle', fetcher)).rejects.toThrow(/named no auction/)
  })
})

describe('the seed marker', () => {
  it('builds every order reference out of the prefix the artifact declares', () => {
    const ref = seededOrderRef(2, 7, 'gaiaherbs.com')
    expect(ref.startsWith(SEEDED_PREFIX)).toBe(true)
    // It must also survive the served route's own pattern, `^[A-Za-z0-9_.:#/-]{1,200}$` —
    // no spaces and no `@`, so it can be neither a sentence nor an email address.
    expect(ref).toMatch(/^[A-Za-z0-9_.:#/-]{1,200}$/)
  })

  it('refuses an unmarked reference', () => {
    expect(() => {
      assertSeeded('ord-91231')
    }).toThrow(UnmarkedOutcome)
  })

  it('refuses a reference that merely CONTAINS the prefix', () => {
    // The forgery shape `seeded-feedback.test.ts` already pins on the reading side: a marker
    // that is not the first thing in the field is not a marker.
    expect(() => {
      assertSeeded(`ord-${SEEDED_PREFIX}1`)
    }).toThrow(UnmarkedOutcome)
  })

  it('marks the order it builds, and claims routed only as a whole record', () => {
    const order = seededOrder(0, 0, 'gaiaherbs.com', 'auc-1')
    expect(order.order_ref.startsWith(SEEDED_PREFIX)).toBe(true)
    expect(order.routed).toBe(true)
    expect(order.buyer_pseudonym.startsWith('sim-buyer-')).toBe(true)
  })

  it('never reaches the network with an unmarked order', async () => {
    const fetcher = vi.fn<Fetcher>(() => Promise.resolve(json({})))
    const forged = { ...seededOrder(0, 0, 'gaia', 'auc-1'), order_ref: 'ord-real-1' }
    await expect(submitOutcome(forged, true, fetcher)).rejects.toThrow(UnmarkedOutcome)
    // The assertion that matters: the refusal is BEFORE the request, because the ledger this
    // would have reached is append-only and has no delete.
    expect(fetcher).not.toHaveBeenCalled()
  })
})

describe('submitting one outcome', () => {
  const PROMPT = {
    offered: true,
    reason: '',
    prompt: {
      order_ref: 'x',
      question_id: 'matched_pitch',
      question: 'Did what arrived match what the store pitched?',
      options: [
        { id: 'yes_as_described', label: 'Yes', matched_pitch: true },
        { id: 'not_as_described', label: 'No', matched_pitch: false },
      ],
    },
  }

  function stack(prompt: unknown = PROMPT) {
    const sent: { path: string; body: Record<string, unknown> }[] = []
    const fetcher: Fetcher = (path, init) => {
      sent.push({
        path: String(path),
        body: JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>,
      })
      if (path === PROMPT_PATH) return Promise.resolve(json(prompt))
      return Promise.resolve(json({ event_id: 'fb-1' }, 201))
    }
    return { sent, fetcher }
  }

  it('asks R14 first, then answers with an option the SERVICE published', async () => {
    const { sent, fetcher } = stack()
    const order = seededOrder(1, 2, 'gaia', 'auc-1')
    expect(await submitOutcome(order, false, fetcher)).toBe('fb-1')
    expect(sent.map((row) => row.path)).toEqual([PROMPT_PATH, FEEDBACK_PATH])
    expect(sent[1]?.body.response).toEqual({
      question_id: 'matched_pitch',
      choice: 'not_as_described',
    })
  })

  it('picks the option whose matched_pitch carries the sign it was asked for', async () => {
    const { sent, fetcher } = stack()
    await submitOutcome(seededOrder(1, 2, 'gaia', 'auc-1'), true, fetcher)
    expect((sent[1]?.body.response as { choice: string }).choice).toBe('yes_as_described')
  })

  it('reports R14 refusing the order in R14’s own words', async () => {
    const { fetcher } = stack({
      offered: false,
      reason: 'this order is not marked as network-routed',
      prompt: null,
    })
    await expect(submitOutcome(seededOrder(0, 0, 'gaia', 'a'), true, fetcher)).rejects.toThrow(
      /not marked as network-routed/,
    )
  })

  it('refuses rather than guessing when the prompt has no option of that sign', async () => {
    const { fetcher } = stack({
      offered: true,
      reason: '',
      prompt: {
        question_id: 'q',
        options: [{ id: 'yes_as_described', label: 'Yes', matched_pitch: true }],
      },
    })
    await expect(submitOutcome(seededOrder(0, 0, 'gaia', 'a'), false, fetcher)).rejects.toThrow(
      /published no option whose matched_pitch is false/,
    )
  })
})

describe('buying the top slot', () => {
  it('adds the auction id the live shortlist slot does not carry', async () => {
    let body: Record<string, unknown> = {}
    const fetcher: Fetcher = (path, init) => {
      body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>
      expect(path).toBe(ACCEPT_PATH)
      return Promise.resolve(json({ permalink_url: 'https://gaia.example/cart/1' }))
    }
    const bought = await buyTopSlot(COLD.shortlist, 'auc-1', fetcher)
    expect(bought.store_id).toBe('gaia')
    expect((body.slot as { auction_id: string }).auction_id).toBe('auc-1')
  })

  it('refuses when the auction shortlisted nobody', async () => {
    const fetcher = vi.fn<Fetcher>(() => Promise.resolve(json({})))
    await expect(buyTopSlot({ slots: [] }, 'auc-1', fetcher)).rejects.toThrow(WireFailure)
    expect(fetcher).not.toHaveBeenCalled()
  })
})

describe('choosing what a shopper reports', () => {
  it('reads the discount the shop actually offered, not the price it landed at', () => {
    expect(discountOffered(WARM.shortlist, 'toniiq')).toBe(20)
    expect(discountOffered(COLD.shortlist, 'gaia')).toBe(0)
  })

  it('answers null for a shop that is not in this shortlist at all', () => {
    expect(discountOffered(COLD.shortlist, 'nutricost')).toBeNull()
  })

  it('is satisfied exactly when the shop gave a better deal than its list price', () => {
    expect(outcomeIsPositive(0)).toBe(false)
    expect(outcomeIsPositive(5)).toBe(true)
  })

  it('never teaches a shop ProxyShop bid on behalf of', async () => {
    // A fallback row has no seller agent behind it — no address in trust's book and no arm to
    // credit — so an outcome for one would be sentiment about a shop that was never in the
    // conversation.
    expect(biddingStores(await reading(COLD))).toEqual(['gaia', 'toniiq'])
  })
})

describe('comparing two readings', () => {
  it('reports nothing moved when the two readings are the same auction twice', async () => {
    const twice = compare(await reading(COLD), await reading(COLD))
    expect(anythingMoved(twice)).toBe(false)
    for (const row of twice) {
      expect(row.rank_delta).toBe(0)
      expect(row.components.every((term) => term.held)).toBe(true)
    }
  })

  it('names the term that moved and the term that held', async () => {
    const moved = compare(await reading(COLD), await reading(WARM))
    const toniiq = moved.find((row) => row.store_id === 'toniiq')
    expect(toniiq?.position_before).toBe(1)
    expect(toniiq?.position_after).toBe(0)
    expect(toniiq?.rank_delta).toBeCloseTo(0.0566, 6)
    const price = toniiq?.components.find((term) => term.key === 'price_value')
    const trust = toniiq?.components.find((term) => term.key === 'trust')
    expect(price?.delta).toBeCloseTo(0.0566, 6)
    expect(price?.held).toBe(false)
    expect(trust?.held).toBe(true)
  })

  it('orders by the AFTER reading, so the table reads as the new shortlist', async () => {
    const moved = compare(await reading(COLD), await reading(WARM))
    expect(moved.map((row) => row.store_id)).toEqual(['toniiq', 'gaia', 'nutricost'])
  })

  it('keeps a store that dropped out, with a null place rather than a zero', async () => {
    const gone = await reading({ ...WARM, ranked: WARM.ranked.slice(0, 1) })
    const moved = compare(await reading(COLD), gone)
    const dropped = moved.find((row) => row.store_id === 'gaia')
    expect(dropped?.position_after).toBeNull()
    expect(dropped?.rank_after).toBeUndefined()
    // Not 0: a store that is not in the second ranking has no score there, and printing a
    // zero would be a measurement nobody made.
    expect(dropped?.rank_delta).toBeUndefined()
  })
})

describe('printing a figure', () => {
  it('prints an em dash for a value nobody published, never a zero', () => {
    expect(figure(undefined)).toBe('—')
    expect(signed(undefined)).toBe('—')
    expect(figure(0)).toBe('0.0000')
  })

  it('always shows the sign, so a rise and a fall never look alike', () => {
    expect(signed(0.0566)).toBe('+0.0566')
    expect(signed(-0.0566)).toBe('−0.0566')
    expect(signed(0)).toBe('0')
  })
})

describe('reading a refusal', () => {
  it('unwraps both shapes buyer_svc answers with', () => {
    expect(refusalDetail({ detail: 'plain string' })).toBe('plain string')
    expect(refusalDetail({ detail: { message: 'no feedback was recorded', reason: 'r' } })).toBe(
      'no feedback was recorded',
    )
    expect(refusalDetail({ detail: { reason: 'not routed' } })).toBe('not routed')
    expect(refusalDetail(null)).toBe('no detail')
  })
})
