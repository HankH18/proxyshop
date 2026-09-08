/**
 * The learning page.
 *
 * The promise under test is the one the page exists to keep: the second shortlist is a second
 * auction, and nothing on the page can produce a difference the exchange did not publish. So
 * the test that matters most is the one where the stack answers with the SAME auction twice —
 * the page must say the market did not move, and must not draw an arrow.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  LearningPage,
  MEASURED_SHIFT_AT_64_EVENTS,
  NO_TEACHING_SPREAD,
} from './LearningPage'
import {
  ACCEPT_PATH,
  BID_WINDOW_SECONDS,
  CLARIFY_PATH,
  CONFIRM_PATH,
  FEEDBACK_PATH,
  PROMPT_PATH,
  type Fetcher,
} from './loop'
import { SEEDED_PREFIX } from '../journey/seeded-feedback'

afterEach(cleanup)

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function auction(id: string, rows: { store: string; rank: number; price: number; pv: number }[]) {
  return {
    auction_id: id,
    recorded_at: '2026-09-08T08:00:00Z',
    shortlist: {
      slots: rows.map((row, index) => ({
        slot: index === 0 ? 'fit' : 'value',
        bid_ref: `${id}:${row.store}`,
        price: {
          unit_price: row.price,
          discount: row.pv > 0 ? { type: 'percentage', value: 10 } : null,
        },
      })),
    },
    entries: rows.map((row) => ({
      store_id: row.store,
      fallback: false,
      fallback_reason: null,
      unit_price: row.price,
    })),
    ranked: rows.map((row) => ({
      bid_ref: `${id}:${row.store}`,
      store_id: row.store,
      rank_score: row.rank,
      components: { trust: 0.172, price_value: row.pv },
    })),
  }
}

const COLD = auction('auc-1', [
  { store: 'gaia', rank: 0.497, price: 25.49, pv: 0 },
  { store: 'toniiq', rank: 0.473, price: 20.97, pv: 0 },
])

const WARM = auction('auc-2', [
  { store: 'toniiq', rank: 0.5296, price: 16.77, pv: 0.0566 },
  { store: 'gaia', rank: 0.497, price: 25.49, pv: 0 },
])

const PROMPT = {
  offered: true,
  reason: '',
  prompt: {
    question_id: 'matched_pitch',
    options: [
      { id: 'yes_as_described', label: 'Yes', matched_pitch: true },
      { id: 'not_as_described', label: 'No', matched_pitch: false },
    ],
  },
}

/**
 * A stack that answers the whole flow, serving `auctions[n]` for the n-th auction it opens.
 * The last one is repeated, so a run with more confirms than fixtures keeps working.
 */
function stack(auctions: Record<string, unknown>[]) {
  const calls: string[] = []
  const feedback: Record<string, unknown>[] = []
  let opened = 0
  const fetcher: Fetcher = (input, init) => {
    const path = String(input)
    calls.push(path)
    if (path === CLARIFY_PATH) return Promise.resolve(json({ intent: { intent_id: 'i1' } }))
    if (path === CONFIRM_PATH) {
      const body = auctions[Math.min(opened, auctions.length - 1)]
      opened += 1
      return Promise.resolve(json({ auction_id: (body as { auction_id: string }).auction_id }, 201))
    }
    if (path === PROMPT_PATH) return Promise.resolve(json(PROMPT))
    if (path === FEEDBACK_PATH) {
      feedback.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>)
      return Promise.resolve(json({ event_id: `fb-${feedback.length}` }, 201))
    }
    if (path === ACCEPT_PATH) {
      return Promise.resolve(json({ permalink_url: 'https://gaia.example/cart/1' }))
    }
    const found = auctions.find((row) => path.endsWith(String((row as { auction_id: string }).auction_id)))
    return Promise.resolve(json(found ?? auctions[auctions.length - 1]))
  }
  return { fetcher, calls, feedback }
}

const SMALL = { rounds: 1, reviewersPerRound: 1, bidWindowSeconds: 1 }

/**
 * Click, and let every promise the click started settle before asserting.
 *
 * `@testing-library/user-event` is not a dependency of this workspace — `metrics.test.tsx`
 * uses `fireEvent` for the same reason — and every button on this page starts an async chain
 * of real requests. `act` around the flush is what keeps React's state updates out of the
 * "not wrapped in act" warning, and it is why this is a helper rather than a bare call.
 */
async function click(element: HTMLElement): Promise<void> {
  await act(async () => {
    fireEvent.click(element)
    await Promise.resolve()
  })
}

describe('the learning page', () => {
  it('shows nothing until a query has actually been run', () => {
    render(<LearningPage fetcher={stack([COLD]).fetcher} {...SMALL} />)
    expect(screen.queryByTestId('movement-table')).toBeNull()
    expect(screen.getByRole('button', { name: /run this query/i })).toBeEnabled()
  })

  it('runs a real auction and shows what the exchange published', async () => {
    const { fetcher, calls } = stack([COLD])
    render(<LearningPage fetcher={fetcher} {...SMALL} />)
    await click(screen.getByRole('button', { name: /run this query/i }))
    await screen.findByText(/auction auc-1/)
    // The confirm is the only door a browser has onto an auction; if it is not called, the
    // page is showing something it did not open.
    expect(calls).toContain(CONFIRM_PATH)
    // Both candidates are cold, so both carry the same term — `getAllByText`, because a
    // `getByText` that happened to be unique would be asserting an accident of the fixture.
    expect(screen.getAllByText(/price_value=0\.0000/)).toHaveLength(2)
  })

  it('locks the query box after the first auction, because beat three is the SAME query', async () => {
    render(<LearningPage fetcher={stack([COLD]).fetcher} {...SMALL} />)
    await click(screen.getByRole('button', { name: /run this query/i }))
    await screen.findByText(/auction auc-1/)
    expect(screen.getByLabelText(/what are you shopping for/i)).toBeDisabled()
  })

  it('feeds outcomes through the real doors, marked, and says so', async () => {
    const { fetcher, calls, feedback } = stack([COLD])
    render(<LearningPage fetcher={fetcher} {...SMALL} />)
    await click(screen.getByRole('button', { name: /run this query/i }))
    await screen.findByText(/auction auc-1/)
    await click(screen.getByRole('button', { name: /feed 1 rounds/i }))
    await waitFor(() => {
      expect(screen.getByTestId('fed-count')).toBeInTheDocument()
    })
    // The purchase reaches the exchange's bandit; the feedback reaches the trust ledger.
    expect(calls).toContain(ACCEPT_PATH)
    expect(calls).toContain(FEEDBACK_PATH)
    // And every order that reached the ledger carries the marker the artifact declares.
    expect(feedback.length).toBeGreaterThan(0)
    for (const body of feedback) {
      const order = body.order as { order_ref: string; routed: boolean }
      expect(order.order_ref.startsWith(SEEDED_PREFIX)).toBe(true)
      expect(order.routed).toBe(true)
    }
  })

  it('names the term that moved and the term that held', async () => {
    const { fetcher } = stack([COLD, WARM])
    render(<LearningPage fetcher={fetcher} {...SMALL} />)
    await click(screen.getByRole('button', { name: /run this query/i }))
    await screen.findByText(/auction auc-1/)
    await click(screen.getByRole('button', { name: /run the same query again/i }))
    const table = await screen.findByTestId('movement-table')
    const toniiq = table.querySelector('tr[data-store="toniiq"]')
    expect(toniiq?.textContent).toContain('#2 → #1')
    expect(toniiq?.textContent).toContain('+0.0566')
    // The trust term is published for both readings and is identical; the page must say it
    // held rather than leaving a reader to infer that trust did the work.
    expect(toniiq?.querySelector('li[data-delta="flat"]')?.textContent).toMatch(/trust/)
  })

  it('does not present its two reference figures as a reading of the viewer\u2019s own stack', () => {
    // `NO_TEACHING_SPREAD` and `MEASURED_SHIFT_AT_64_EVENTS` are LITERALS. Nothing recomputes
    // them when the page runs, and they were rendered under the words "Measured on this
    // stack:" — which is exactly the dressing-a-constant-as-a-reading that this page exists to
    // argue against. The figures stay (a reader sizing "is this bigger than chance" needs a
    // bar) and the claim about where they came from is now true.
    const { fetcher } = stack([COLD, WARM])
    render(<LearningPage fetcher={fetcher} {...SMALL} />)
    return click(screen.getByRole('button', { name: /run this query/i }))
      .then(() => screen.findByText(/auction auc-1/))
      .then(() => click(screen.getByRole('button', { name: /run the same query again/i })))
      .then(() => screen.findByTestId('movement-table'))
      .then(() => {
        expect(screen.getByText(/Read one pair with care\./)).toBeInTheDocument()
        expect(screen.queryByText(/Measured on this stack/i)).toBeNull()
        expect(screen.getByText(/not on yours, and not just now/i)).toBeInTheDocument()
      })
  })

  it('asks for a bid window the exchange will actually grant', () => {
    // The page used to ask for 60 seconds. `exchange.auction.routes.MAX_BID_TIMEOUT_SECONDS`
    // is 10.0 and clamps silently, so the request's stated intent and its effect differed with
    // nothing saying so -- measured through the served route, which publishes what it granted:
    // asked 60 -> granted 10.0. A ceiling is not a suggestion, and a page whose whole argument
    // is "check what the service actually did" must not ship a number the service ignores.
    expect(BID_WINDOW_SECONDS).toBeLessThanOrEqual(10)
    expect(BID_WINDOW_SECONDS).toBeGreaterThan(0)
  })

  it('warns that one pair is not evidence, whenever something did move', async () => {
    // The other direction of the "Nothing moved" warning below, and the one this page was
    // missing. The sellers Thompson-sample a discount rung per auction, so `price_value` moves
    // between two identical queries with nothing taught in between -- MEASURED at 0.0425 of
    // spread over eight no-teaching runs, against a +0.0071 mean shift after sixty-four sealed
    // feedback events. A page that showed the movement and not the scale of the noise would be
    // presenting exploration as learning, which is the one claim this page exists to make.
    const { fetcher } = stack([COLD, WARM])
    render(<LearningPage fetcher={fetcher} {...SMALL} />)
    await click(screen.getByRole('button', { name: /run this query/i }))
    await screen.findByText(/auction auc-1/)
    await click(screen.getByRole('button', { name: /run the same query again/i }))
    await screen.findByTestId('movement-table')

    expect(screen.getByText(/Read one pair with care\./)).toBeInTheDocument()
    // The measured numbers, not a hand-wave: a caution that names no scale tells a reader
    // nothing about whether what they are looking at is bigger than the noise.
    expect(screen.getByText(NO_TEACHING_SPREAD.toFixed(4))).toBeInTheDocument()
    expect(screen.getByText(`+${MEASURED_SHIFT_AT_64_EVENTS.toFixed(4)}`)).toBeInTheDocument()
    // ...and it must NOT appear alongside the opposite message.
    expect(screen.queryByText(/Nothing moved\./)).toBeNull()
  })

  it('says the market did not move when the stack answers the same auction twice', async () => {
    // The anti-screenshot test. A page that stored a before and re-rendered it as an after
    // would have a difference to show here; this one must not.
    const { fetcher } = stack([COLD])
    render(<LearningPage fetcher={fetcher} {...SMALL} />)
    await click(screen.getByRole('button', { name: /run this query/i }))
    await screen.findByText(/auction auc-1/)
    await click(screen.getByRole('button', { name: /run the same query again/i }))
    await screen.findByTestId('movement-table')
    expect(screen.getByText(/Nothing moved\./)).toBeInTheDocument()
  })

  it('shows a refusal in the service’s own words rather than a blank panel', async () => {
    const fetcher: Fetcher = (path) => {
      if (path === CLARIFY_PATH) return Promise.resolve(json({ intent: {} }))
      return Promise.resolve(
        json({ detail: 'the exchange is not configured; this auction was not created' }, 503),
      )
    }
    render(<LearningPage fetcher={fetcher} {...SMALL} />)
    await click(screen.getByRole('button', { name: /run this query/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/the exchange is not configured/)
  })

  it('states which ranking terms cannot move here, and why', () => {
    render(<LearningPage fetcher={stack([COLD]).fetcher} {...SMALL} />)
    const panel = screen.getByLabelText('Which terms can move here')
    expect(panel.textContent).toMatch(/trust.*cannot move here/s)
    expect(panel.textContent).toMatch(/exchange-deployment\.json/)
    expect(panel.textContent).toMatch(/neutral of 0\.5/)
  })

  it('prints the marker prefix and the limit of what it proves', () => {
    render(<LearningPage fetcher={stack([COLD]).fetcher} {...SMALL} />)
    const panel = screen.getByLabelText('What marks the manufactured data')
    expect(panel.textContent).toContain(SEEDED_PREFIX)
    expect(panel.textContent).toMatch(/tamper-evident, not exclusive/)
  })

  it('says what resets the sellers’ learning and what survives a compose down', () => {
    render(<LearningPage fetcher={stack([COLD]).fetcher} {...SMALL} />)
    const panel = screen.getByLabelText('What resets this demo')
    expect(panel.textContent).toMatch(/docker compose restart store-agent-/)
    expect(panel.textContent).toMatch(/pgdata/)
  })

  it('logs every request it made, so the page shows its own work', async () => {
    const { fetcher } = stack([COLD])
    render(<LearningPage fetcher={fetcher} {...SMALL} />)
    await click(screen.getByRole('button', { name: /run this query/i }))
    await screen.findByText(/auction auc-1/)
    expect(screen.getByText(/Every request this page made/)).toBeInTheDocument()
  })

  it('starts over without keeping a reading from the previous run', async () => {
    const { fetcher } = stack([COLD, WARM])
    render(<LearningPage fetcher={fetcher} {...SMALL} />)
    await click(screen.getByRole('button', { name: /run this query/i }))
    await screen.findByText(/auction auc-1/)
    await click(screen.getByRole('button', { name: /start over/i }))
    expect(screen.queryByText(/auction auc-1/)).toBeNull()
    expect(screen.getByLabelText(/what are you shopping for/i)).toBeEnabled()
  })
})

describe('the demo’s honesty under a hostile stack', () => {
  it('does not record a purchase it could not make', async () => {
    const { fetcher: base } = stack([COLD])
    const fetcher = vi.fn<Fetcher>((input, init) => {
      if (String(input) === ACCEPT_PATH) {
        return Promise.resolve(json({ detail: { message: 'checkout_refused: expired offer' } }, 409))
      }
      return base(input, init)
    })
    render(<LearningPage fetcher={fetcher} {...SMALL} />)
    await click(screen.getByRole('button', { name: /run this query/i }))
    await screen.findByText(/auction auc-1/)
    await click(screen.getByRole('button', { name: /feed 1 rounds/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/checkout_refused/)
    // No feedback may follow a purchase that did not happen: R14's gate is about orders the
    // network actually routed, and the page must not claim one it was refused.
    expect(fetcher.mock.calls.some(([path]) => String(path) === FEEDBACK_PATH)).toBe(false)
  })
})
