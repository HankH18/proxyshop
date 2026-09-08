/**
 * The metrics and tracing page.
 *
 * What these tests are really pinning is the page's one promise: that nothing on it is a
 * number nobody produced. A panel with no reading shows the REASON it has none, an
 * unreachable source is named as unreachable rather than drawn as an empty chart, and a
 * measured zero is kept apart from a failure to measure.
 */
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MetricsPage } from './MetricsPage'
import {
  REMEMBERED_AUCTION_KEY,
  auctionPath,
  livecheckPath,
  type Fetcher,
} from './telemetry'

// `@testing-library/react` registers its own cleanup only when the runner exposes
// `afterEach` globally, and this workspace's vitest projects do not set `globals: true`.
afterEach(cleanup)
// `cleanup` unmounts the tree and touches no storage. `sessionStorage` is shared across every
// test in this file, and the page READS it in its initial state, so a key left behind by one
// test seeds the next one's auction field — a leak that would make these tests pass for the
// wrong reason and, worse, could make an unrelated one fail mysteriously.
afterEach(() => window.sessionStorage.clear())

const HERE = { protocol: 'http:', hostname: 'localhost' }

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

const TRACE = {
  auction_id: 'auc-1',
  shortlist: { slots: [{ slot: 'fit' }] },
  recorded_at: '2026-09-08T01:00:00Z',
  solicited: ['woolworks', 'alpine-supply', 'fastfleece'],
  entries: [
    { store_id: 'woolworks', fallback: false, fallback_reason: null, unit_price: 78 },
    { store_id: 'fastfleece', fallback: true, fallback_reason: 'store_declined:no_match' },
  ],
  excluded: [
    { bid_ref: 'auc-1:fastfleece', store_id: 'fastfleece', exclusion_reasons: ['blacklisted_store'] },
  ],
  denied: [],
  ranked: [{ bid_ref: 'auc-1:woolworks', store_id: 'woolworks', rank_score: 0.91, components: { fit: 0.8 } }],
}

/** Answers the two auction routes and refuses the session routes, as a real page would. */
function stack(overrides: Record<string, Response> = {}): Fetcher {
  return (path) => {
    if (overrides[path]) return Promise.resolve(overrides[path])
    if (path === auctionPath('auc-1')) return Promise.resolve(json(TRACE))
    if (path === livecheckPath('auc-1')) {
      return Promise.resolve(json({ auction_id: 'auc-1', records: [], refused: [] }))
    }
    return Promise.resolve(json({ detail: 'no live buyer session' }, 401))
  }
}

describe('the page says what it is', () => {
  it('marks itself a demo aid and warns against hosting it publicly', () => {
    render(<MetricsPage fetcher={stack()} location={HERE} initialAuctionId="" />)
    expect(screen.getByText(/A demo aid, not a product surface/)).toBeInTheDocument()
    const warning = screen.getByRole('note')
    expect(warning.textContent).toContain('Do not expose this page on a public deployment')
  })
})

describe('a panel with no reading shows the reason, never a zero', () => {
  it('is idle rather than empty before an auction id is given', async () => {
    render(<MetricsPage fetcher={stack()} location={HERE} initialAuctionId="" />)
    await waitFor(() => expect(screen.getAllByText('idle').length).toBeGreaterThan(0))
    // Nothing on the page claims a reading for an auction nobody named: the trace view is
    // absent entirely rather than rendered with empty lists, which would read as an auction
    // in which nobody was solicited and nobody bid.
    expect(document.querySelector('.metrics-trace')).toBeNull()
  })

  it('says the session panel is refused, and that the refusal is by design', async () => {
    render(<MetricsPage fetcher={stack()} location={HERE} initialAuctionId="" />)
    await waitFor(() => expect(screen.getAllByText('unauthorized').length).toBeGreaterThan(0))
    expect(screen.getByText(/bearer credential/)).toBeInTheDocument()
  })

  it('names the status when the service refuses the trace', async () => {
    const fetcher = stack({ [auctionPath('auc-1')]: json({ detail: 'gone' }, 502) })
    render(<MetricsPage fetcher={fetcher} location={HERE} initialAuctionId="auc-1" />)
    await screen.findByText('failed')
    expect(screen.getByText(/502/)).toBeInTheDocument()
  })

  it('says a 404 is the service having no record, not a failure', async () => {
    const fetcher = stack({ [auctionPath('auc-1')]: json({ detail: 'unknown' }, 404) })
    render(<MetricsPage fetcher={fetcher} location={HERE} initialAuctionId="auc-1" />)
    await screen.findByText('absent')
  })
})

describe('the recorded auction trace', () => {
  it('shows every rostered store and what each one answered', async () => {
    render(<MetricsPage fetcher={stack()} location={HERE} initialAuctionId="auc-1" />)
    await screen.findByText(/woolworks, alpine-supply, fastfleece/)

    // The store that bid, and the store the exchange stood in for, told apart.
    expect(screen.getAllByText('bid').length).toBe(1)
    expect(screen.getAllByText('fallback').length).toBe(1)
    // The reason family and its detail, split the way the exchange splits them.
    expect(screen.getByText(/store_declined · no_match/)).toBeInTheDocument()
  })

  it('shows the exclusion the filters recorded, in the exchange own word', async () => {
    render(<MetricsPage fetcher={stack()} location={HERE} initialAuctionId="auc-1" />)
    expect(await screen.findByText('blacklisted_store')).toBeInTheDocument()
  })

  it('says nobody was denied rather than leaving the section blank', async () => {
    render(<MetricsPage fetcher={stack()} location={HERE} initialAuctionId="auc-1" />)
    expect(await screen.findByText('No store was denied entry.')).toBeInTheDocument()
  })

  it('prints the ranking with the components the exchange published', async () => {
    render(<MetricsPage fetcher={stack()} location={HERE} initialAuctionId="auc-1" />)
    await screen.findByText('score 0.91')
    expect(screen.getByText('fit=0.8')).toBeInTheDocument()
  })

  it('carries the answer itself, so a reader can check the panel against it', async () => {
    render(<MetricsPage fetcher={stack()} location={HERE} initialAuctionId="auc-1" />)
    expect(
      await screen.findByText('The answer itself, as the service sent it'),
    ).toBeInTheDocument()
  })

  it('tells a forgotten shortlist apart from an empty one', async () => {
    const forgotten = { ...TRACE, shortlist: null }
    const fetcher = stack({ [auctionPath('auc-1')]: json(forgotten) })
    render(<MetricsPage fetcher={fetcher} location={HERE} initialAuctionId="auc-1" />)
    expect(await screen.findByText(/forgotten/)).toBeInTheDocument()
  })

  it('traces the auction typed into the form', async () => {
    const asked: string[] = []
    const fetcher: Fetcher = (path) => {
      asked.push(path)
      return Promise.resolve(json({ auction_id: 'auc-2', shortlist: { slots: [] } }))
    }
    render(<MetricsPage fetcher={fetcher} location={HERE} initialAuctionId="" />)
    const input = screen.getByLabelText('Auction id')
    fireEvent.change(input, { target: { value: 'auc-2' } })
    fireEvent.submit(input.closest('form')!)
    await waitFor(() => expect(asked).toContain(auctionPath('auc-2')))
  })
})

describe('the remembered auction id can be cleared from this page', () => {
  // The dead end these pin: `Journey` writes this key on every confirm, and for a while the
  // ONLY `forget` was behind its sign-out button — which renders only where
  // `GET /buyer/auth/sign-in` answers `{"offered": true}`. Neither the compose stack nor the
  // devstack offers it, so on every deployment a demo is driven on, a remembered id could be
  // written and never cleared.
  it('removes the key from sessionStorage, not just the text from the field', () => {
    window.sessionStorage.setItem(REMEMBERED_AUCTION_KEY, 'auc-stale')
    render(<MetricsPage fetcher={stack()} location={HERE} />)
    // Read out of storage, because no `initialAuctionId` was injected — this is the real
    // path a demo audience arrives on.
    expect(screen.getByLabelText('Auction id')).toHaveValue('auc-stale')

    fireEvent.click(screen.getByRole('button', { name: 'Forget it' }))

    expect(screen.getByLabelText('Auction id')).toHaveValue('')
    // The half that clearing the field alone would not do: `remembered()` seeds the initial
    // state of both `auctionId` and `submitted`, so a surviving key comes straight back on
    // the next mount.
    expect(window.sessionStorage.getItem(REMEMBERED_AUCTION_KEY)).toBeNull()
  })

  it('stops tracing the forgotten auction rather than leaving its trace on screen', async () => {
    window.sessionStorage.setItem(REMEMBERED_AUCTION_KEY, 'auc-1')
    render(<MetricsPage fetcher={stack()} location={HERE} />)
    expect(await screen.findByText('The answer itself, as the service sent it')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Forget it' }))

    await waitFor(() => expect(document.querySelector('.metrics-trace')).toBeNull())
  })

  it('offers nothing to forget when nothing is remembered', () => {
    render(<MetricsPage fetcher={stack()} location={HERE} initialAuctionId="" />)
    expect(screen.getByRole('button', { name: 'Forget it' })).toBeDisabled()
  })

  it('stays offered while the key is still there, however empty the field looks', () => {
    // The state the previous case does NOT cover, and the one a demo driver reaches by the
    // most natural "reset" gesture there is: select the seeded id, delete it, press "Trace
    // it". Both component states are now empty while `sessionStorage` still holds the key,
    // and this page is the only control that can remove it. Asking the two states alone
    // switches the button off over a live key, and the next visit to `#/metrics` re-seeds
    // the field from that key — the exact dead end "Forget it" was added to close.
    window.sessionStorage.setItem(REMEMBERED_AUCTION_KEY, 'auc-stale')
    render(<MetricsPage fetcher={stack()} location={HERE} />)
    const input = screen.getByLabelText('Auction id')
    expect(input).toHaveValue('auc-stale')

    fireEvent.change(input, { target: { value: '' } })
    fireEvent.submit(input.closest('form')!)

    expect(input).toHaveValue('')
    expect(window.sessionStorage.getItem(REMEMBERED_AUCTION_KEY)).toBe('auc-stale')
    const forgetIt = screen.getByRole('button', { name: 'Forget it' })
    expect(forgetIt).toBeEnabled()

    // And it still works from there: pressing it empties the key rather than only the field.
    fireEvent.click(forgetIt)
    expect(window.sessionStorage.getItem(REMEMBERED_AUCTION_KEY)).toBeNull()
    expect(forgetIt).toBeDisabled()
  })
})

describe('the route beside a panel is the route, spelled the way a reader can use it', () => {
  // `auctionPath`/`livecheckPath` build a FETCH path and therefore `encodeURIComponent` their
  // argument — correct there, and wrong for a heading, where it printed
  // `GET /buyer/auctions/%7Bauction_id%7D` on the served page. A page whose whole thesis is
  // "every figure names the served route it was read from" cannot name it in percent-encoding.
  it('prints the auction and live-check templates unescaped', () => {
    render(<MetricsPage fetcher={stack()} location={HERE} initialAuctionId="" />)
    expect(screen.getByText('GET /buyer/auctions/{auction_id}')).toBeInTheDocument()
    expect(screen.getByText('GET /buyer/livecheck/{auction_id}')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('%7B')
  })
})

describe('what it cannot source, it names', () => {
  it('lists the trust ledger with the reason and the remedy', async () => {
    render(<MetricsPage fetcher={stack()} location={HERE} initialAuctionId="" />)
    expect(screen.getByText('GET /events/verify')).toBeInTheDocument()
    expect(screen.getAllByText(/Why not here:/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/What would change it:/).length).toBeGreaterThan(0)
  })

  it('links out to the sources a browser can simply open', () => {
    render(<MetricsPage fetcher={stack()} location={HERE} initialAuctionId="" />)
    // Navigation across origins is not what CORS restricts, so a link is honest where a
    // fetch would be a lie.
    const link = screen.getByRole('link', { name: /http:\/\/localhost:8084\/events\/verify/ })
    expect(link).toHaveAttribute('href', 'http://localhost:8084/events/verify')
  })

  it('offers no link for a source that needs a credential this page must not hold', () => {
    render(<MetricsPage fetcher={stack()} location={HERE} initialAuctionId="" />)
    expect(screen.queryByRole('link', { name: /reports\/losses/ })).toBeNull()
  })

  it('states plainly that no latency, request-count or health route exists to read', () => {
    render(<MetricsPage fetcher={stack()} location={HERE} initialAuctionId="" />)
    const section = screen.getByLabelText('Metrics this stack does not record')
    const text = section.textContent ?? ''
    expect(text).toContain('No request latency, request count or error rate is recorded')
    expect(text).toContain('shopify-stub')
  })

  it('draws no chart of a metric nothing produces', () => {
    const { container } = render(
      <MetricsPage fetcher={stack()} location={HERE} initialAuctionId="" />,
    )
    // The cheapest possible guard against the defect this page exists to help find: no
    // canvas, no svg chart, nothing that could be showing a fabricated series.
    expect(container.querySelectorAll('canvas')).toHaveLength(0)
    expect(container.querySelectorAll('svg')).toHaveLength(0)
  })
})
