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

/**
 * `trust` defaults to the one value every fixture in this file used to hard-code, so every
 * assertion written against the old helper reads the same number it always did. It is a
 * PARAMETER now because holding it constant is what made the suite blind: with one trust value
 * in every fixture, no test in this file had ever rendered a trust term that MOVED, and the
 * page's copy and the page's tests agreed with each other while disagreeing with the stack.
 * See `renders a trust term that moved` below.
 */
function auction(
  id: string,
  rows: { store: string; rank: number; price: number; pv: number; trust?: number }[],
) {
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
      components: { trust: row.trust ?? 0.172, price_value: row.pv },
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

  it('renders a trust term that moved, rather than only one that held', async () => {
    // The sibling of the case above, and the gap it closes is the suite's own blindness: every
    // fixture in this file used to publish trust: 0.172 for both readings, so `held` was the
    // ONLY trust rendering any test had ever seen — which is how the page came to say trust
    // could not move here while the stack moved it. On the compose stack the ranker reads
    // `GET /snapshot` live (see the panel test above for the measurement), so a shopper's
    // feedback moves this term between two auctions and the page has to render that.
    const TAUGHT = auction('auc-3', [
      { store: 'gaia', rank: 0.497, price: 25.49, pv: 0, trust: 0.172 },
      { store: 'toniiq', rank: 0.4813, price: 20.97, pv: 0, trust: 0.2135 },
    ])
    const { fetcher } = stack([COLD, TAUGHT])
    render(<LearningPage fetcher={fetcher} {...SMALL} />)
    await click(screen.getByRole('button', { name: /run this query/i }))
    await screen.findByText(/auction auc-1/)
    await click(screen.getByRole('button', { name: /run the same query again/i }))
    const table = await screen.findByTestId('movement-table')
    const toniiq = table.querySelector('tr[data-store="toniiq"]')
    const trust = [...(toniiq?.querySelectorAll('li') ?? [])].find((li) =>
      li.textContent?.startsWith('trust'),
    )
    // Not `held`, and the direction is the sign of the delta the exchange published.
    expect(trust?.getAttribute('data-delta')).toBe('up')
    expect(trust?.textContent).toContain('+0.0415')
    expect(trust?.querySelector('em')).toBeNull()
    // Price is what held in this pair, so the page cannot be reading the two terms off one
    // switch: the same row must show `price_value` flat while `trust` moves.
    const price = [...(toniiq?.querySelectorAll('li') ?? [])].find((li) =>
      li.textContent?.startsWith('price_value'),
    )
    expect(price?.getAttribute('data-delta')).toBe('flat')
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

  it('states which ranking terms can move here, and does not promise a reorder', () => {
    render(<LearningPage fetcher={stack([COLD]).fetcher} {...SMALL} />)
    const panel = screen.getByLabelText('Which terms can move here')
    // THIS ASSERTION REPLACED `toMatch(/trust.*cannot move here/s)`, WHICH WAS MEASURED FALSE.
    //
    // The old assertion pinned the page's claim that the exchange ranks against a frozen
    // `trust_snapshot` literal in `deploy/demo/exchange-deployment.json`. That claim was true
    // when 85b7ff7 wrote both the sentence and this test — its own commit body states the
    // CONDITION, "`trust` cannot move while the deployment states a literal snapshot" — and
    // b2981da ("trust stops being a constant, and the loop that moves it closes end to end"),
    // a descendant of it, deleted the 372-line `trust_snapshot` block from that document. The
    // condition went away; the sentence and this assertion did not.
    //
    // Re-measured on the running compose stack on 2026-09-09 rather than argued from the
    // diff. `deploy/demo/exchange-deployment.json` parses (3,101,472 bytes) with top-level
    // keys exactly sellers / intent_clusters / catalog / checkout_mode / trust_url, and
    // `docker inspect proxyshop-exchange-1` shows that very directory bound read-only at
    // /srv/deploy with EXCHANGE_DEPLOYMENT pointing into it — so the absence is the running
    // exchange's, not a stale copy's. With that key absent,
    // `exchange.composition._bind_live_ranking_snapshot` binds `LiveTrustSnapshot` and the
    // ranking gate reads the trust service. Served-route proof, on auction
    // auction-958b7fc3-e433-4499-95f0-4d3a6cba1680: the `trust` figures the exchange
    // published at 17:31:13Z fall strictly BETWEEN two `GET /snapshot` reads that bracket it
    // (17:26:25Z and 17:31:40Z), decaying across both intervals — gaiaherbs 0.729750460423 >
    // 0.729746021961 > 0.729745612292, and the same for all four. A literal typed into a
    // document cannot drift between two reads of a clock.
    expect(panel.textContent).toMatch(/trust moves here too/)
    expect(panel.textContent).toMatch(/states no trust_snapshot key/)
    expect(panel.textContent).not.toMatch(/cannot move here/)
    // And the page must not overcorrect into promising a reorder. Measured on the served route
    // — POST /buyer/intent/clarify -> POST /buyer/intent/confirm -> GET /buyer/auctions/{id},
    // the exact three calls `runQuery` makes — on 2026-09-09 at 18:26:09.940Z, auction
    // auction-7fe8e755-7a53-4108-9b0e-5368359eda2c: adjacent `rank_score` gaps of 0.0036303 /
    // 0.0040249 / 0.0045898, against 0.0075385 for ONE five-point rung of the sellers'
    // discount ladder (0.15 x 0.05 / 0.9948870, the band the fifteen rostered listings spread
    // — 11.99 to 2345.00). One rung therefore outweighs every SINGLE gap and no PAIR of them
    // (the smallest two sum to 0.0076552), which is "at most one place" and nothing more.
    expect(panel.textContent).toMatch(/does not promise is a new ORDER/)
    expect(panel.textContent).toMatch(/0\.0075/)
    expect(panel.textContent).toMatch(/at most ONE place/)
    // THE CONTRADICTION THIS CLOSES, and it is the reason these four lines are assertions
    // rather than a comment. The FIRST bullet of this panel says a cold agent asks for nothing
    // — measured, all four bidders quoted list price with `discount: null` — while the draft
    // of the LAST bullet said one rung is "sampled fresh every auction, taught or not" and that
    // "a seller stepping one rung with nothing taught can reorder the board". The same panel
    // asserted both. The code settles it: `sample_arm`
    // (packages/store-agent/src/learning/state.py) computes
    // `depth=sample_depth(state, cluster, seed) if has_record else 0.0`, so a store with no
    // record in this cluster is pinned at rung zero however the other two axes are drawn; and
    // `AgentRunner._select_arm` (packages/store-agent/src/modes/runner.py) independently
    // declines to overlay a learned policy at all until that record exists, so a cold bid is
    // byte-identical to what the agent served before the loop existed. A cold seller cannot
    // step a rung, so it cannot reorder anything.
    expect(panel.textContent).toMatch(/UNTAUGHT seller cannot buy one/)
    expect(panel.textContent).toMatch(/steps no rung and reorders nothing/)
    expect(panel.textContent).not.toMatch(/taught or not/)
    expect(panel.textContent).not.toMatch(/nothing taught can reorder/)
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
