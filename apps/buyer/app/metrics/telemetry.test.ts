/**
 * The metrics page's data layer.
 *
 * The tests that matter most here are the two honesty guards, because they are the ones that
 * catch this repo's dominant defect rather than merely describing behaviour:
 *
 *   * **every path this module fetches is under `/buyer/`** — the only prefix
 *     `deploy/buyer-web/nginx.conf` proxies. A path outside it does not fail loudly; nginx's
 *     `try_files` answers it with `index.html`, so the page would receive HTML with a 200 and
 *     a panel would show a parse error instead of a metric. That is "built, tested, and
 *     reachable by nobody" in one line, and this test is the tripwire.
 *   * **a missing number is never a zero.** Every absence carries a reason.
 */
import { describe, expect, it } from 'vitest'

import {
  AUCTION_PATH_PREFIX,
  LIVECHECK_PATH_PREFIX,
  PROFILE_PATH,
  SESSION_HEADER,
  SESSION_PATH,
  UNREACHABLE_SOURCES,
  auctionPath,
  directUrl,
  hasValue,
  livecheckPath,
  parseAuctionTrace,
  parseLiveCheck,
  readAuctionTrace,
  readLiveCheck,
  readProfile,
  readSession,
  readingDetail,
  reasonDetail,
  reasonFamily,
  type Fetcher,
} from './telemetry'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

/** Records what was asked for, so a test can assert on the spelling of a path. */
function recorder(answer: (path: string, init?: RequestInit) => Response) {
  const calls: { path: string; init?: RequestInit }[] = []
  const fetcher: Fetcher = (path, init) => {
    calls.push({ path, init })
    return Promise.resolve(answer(path, init))
  }
  return { calls, fetcher }
}

describe('every path is one nginx actually proxies', () => {
  it('keeps every fetched path under /buyer/', () => {
    for (const path of [SESSION_PATH, PROFILE_PATH, AUCTION_PATH_PREFIX, LIVECHECK_PATH_PREFIX]) {
      expect(path.startsWith('/buyer/')).toBe(true)
    }
    expect(auctionPath('a-1').startsWith('/buyer/')).toBe(true)
    expect(livecheckPath('a-1').startsWith('/buyer/')).toBe(true)
  })

  it('encodes an id rather than pasting it into the path', () => {
    expect(auctionPath('a/../b')).toBe('/buyer/auctions/a%2F..%2Fb')
    expect(livecheckPath(' a 1 ')).toBe('/buyer/livecheck/a%201')
  })
})

describe('a blank is never a zero', () => {
  it('reads an entry that quoted no price as having none, not as costing nothing', () => {
    const trace = parseAuctionTrace(
      {
        auction_id: 'a-1',
        shortlist: { slots: [] },
        entries: [{ store_id: 's1', fallback: false }],
      },
      'a-1',
    )
    // `undefined`, never `0`. A defaulted zero prints as a price the store quoted.
    expect(trace.entries[0]?.unit_price).toBeUndefined()
    expect(trace.entries[0]?.total_price).toBeUndefined()
  })

  it('drops a rank score that is not a finite number rather than calling it zero', () => {
    const trace = parseAuctionTrace(
      {
        auction_id: 'a-1',
        shortlist: { slots: [] },
        ranked: [
          { bid_ref: 'a-1:s1', store_id: 's1', rank_score: 'high', components: {} },
          { bid_ref: 'a-1:s2', store_id: 's2', rank_score: 0.5, components: { fit: 1 } },
        ],
      },
      'a-1',
    )
    expect(trace.ranked[0]?.rank_score).toBeUndefined()
    // A real zero survives: it is the exchange's measurement, not a missing one.
    expect(trace.ranked[1]?.rank_score).toBe(0.5)
  })

  it('keeps a genuine zero score, which is a verdict and not an absence', () => {
    const trace = parseAuctionTrace(
      {
        auction_id: 'a-1',
        shortlist: { slots: [] },
        ranked: [{ bid_ref: 'a-1:s1', store_id: 's1', rank_score: 0, components: { fit: 0 } }],
      },
      'a-1',
    )
    expect(trace.ranked[0]?.rank_score).toBe(0)
    expect(trace.ranked[0]?.components.fit).toBe(0)
  })

  it('drops a component that is not a number instead of coercing it', () => {
    const trace = parseAuctionTrace(
      {
        auction_id: 'a-1',
        shortlist: { slots: [] },
        ranked: [{ bid_ref: 'a-1:s1', store_id: 's1', components: { fit: 'good', price: 2 } }],
      },
      'a-1',
    )
    expect(trace.ranked[0]?.components).toEqual({ price: 2 })
  })
})

describe('a forgotten shortlist and an empty one are different answers', () => {
  it('reads an explicit null as the exchange having forgotten the auction', () => {
    const trace = parseAuctionTrace({ auction_id: 'a-1', shortlist: null }, 'a-1')
    expect(trace.liveness).toBe('forgotten')
    expect(trace.shortlist_slot_count).toBe(0)
  })

  it('reads zero slots as a live answer about the market', () => {
    const trace = parseAuctionTrace({ auction_id: 'a-1', shortlist: { slots: [] } }, 'a-1')
    expect(trace.liveness).toBe('live')
    expect(trace.shortlist_slot_count).toBe(0)
  })

  it('refuses a body it cannot read instead of calling it a live zero', () => {
    // THE THIRD OUTCOME, and the one that is easy to lose. "A shortlist", "no shortlist" and
    // "a body this page cannot read" are three different answers. Collapsing the third into
    // the second prints `live, 0 slots` toned `confirmed` — a measurement of zero, in the
    // design system's strongest provenance tone, for an answer nobody could read.
    // `journey/wire.ts::loadAuction` throws on exactly these shapes for the same reason.
    for (const body of [
      { auction_id: 'a-1' }, // no `shortlist` key at all — absent is not an explicit null
      { auction_id: 'a-1', shortlist: {} }, // an object carrying no `slots`
      { auction_id: 'a-1', shortlist: { slots: 'two' } }, // `slots` that is not an array
      { auction_id: 'a-1', shortlist: 'gone' }, // not an object
      { auction_id: 'a-1', shortlist: 7 },
    ]) {
      expect(() => parseAuctionTrace(body, 'a-1')).toThrow()
    }
  })

  it('turns that refusal into a failed reading, never a rendered zero', async () => {
    const { fetcher } = recorder(() => json({ auction_id: 'a-1' }))
    const reading = await readAuctionTrace('a-1', fetcher)
    expect(reading.state).toBe('failed')
    expect(readingDetail(reading)).toContain('slots array')
  })
})

describe('the fallback reason splitter mirrors the exchange', () => {
  it('splits on the FIRST colon and keeps the family', () => {
    expect(reasonFamily('store_refused:503')).toBe('store_refused')
    expect(reasonDetail('store_refused:503')).toBe('503')
    expect(reasonFamily('no_response')).toBe('no_response')
    expect(reasonDetail('no_response')).toBe('')
    expect(reasonDetail('store_declined:no:matching:product')).toBe('no:matching:product')
  })

  it('answers empty for a reason that is not a string, rather than throwing', () => {
    expect(reasonFamily(null)).toBe('')
    expect(reasonDetail(undefined)).toBe('')
  })
})

describe('each HTTP outcome becomes the state it actually means', () => {
  it('calls a 401 a refusal, not a failure', async () => {
    const { fetcher } = recorder(() => json({ detail: 'no live buyer session' }, 401))
    const reading = await readSession('sid-1', fetcher)
    expect(reading.state).toBe('unauthorized')
    // The service's own words survive, so a panel can quote them.
    expect(readingDetail(reading)).toContain('no live buyer session')
  })

  it('calls a 404 an absence — the service has no record, which is data', async () => {
    const { fetcher } = recorder(() => json({ detail: 'unknown auction' }, 404))
    const reading = await readAuctionTrace('a-nope', fetcher)
    expect(reading.state).toBe('absent')
  })

  it('calls a 502 a failure and names the status', async () => {
    const { fetcher } = recorder(() => json({ detail: 'exchange unreachable' }, 502))
    const reading = await readAuctionTrace('a-1', fetcher)
    expect(reading.state).toBe('failed')
    expect(readingDetail(reading)).toContain('502')
  })

  it('names a thrown network error rather than rendering an empty panel', async () => {
    const fetcher: Fetcher = () => Promise.reject(new Error('NetworkError: failed to fetch'))
    const reading = await readAuctionTrace('a-1', fetcher)
    expect(reading.state).toBe('failed')
    expect(readingDetail(reading)).toContain('failed to fetch')
  })

  it('catches the SPA fallback answering HTML with a 200', async () => {
    // The exact shape a path outside `/buyer/` takes under nginx's `try_files`: a 200 whose
    // body is index.html. Left unhandled this is an "unexpected token <" in a console and an
    // empty panel on screen.
    const { fetcher } = recorder(
      () => new Response('<!doctype html><html></html>', { status: 200 }),
    )
    const reading = await readAuctionTrace('a-1', fetcher)
    expect(reading.state).toBe('failed')
  })

  it('is idle, not empty, before an auction id has been given', async () => {
    const { calls, fetcher } = recorder(() => json({}))
    expect((await readAuctionTrace('   ', fetcher)).state).toBe('idle')
    // And it asks nobody anything.
    expect(calls).toHaveLength(0)
  })
})

describe('the session routes', () => {
  it('refuses without a session and says so is by design', async () => {
    const { calls, fetcher } = recorder(() => json({}))
    const reading = await readSession('', fetcher)
    expect(reading.state).toBe('unauthorized')
    expect(readingDetail(reading)).toContain('by design')
    expect(calls).toHaveLength(0)
  })

  it('sends the session in the header the service reads, never as a cookie', async () => {
    const { calls, fetcher } = recorder(() =>
      json({ session_id: 's', pseudonym: 'psn-1', issued_at: 'a', expires_at: 'b' }),
    )
    const reading = await readSession('sid-1', fetcher)
    expect(hasValue(reading)).toBe(true)
    const headers = calls[0]?.init?.headers as Record<string, string>
    expect(headers[SESSION_HEADER]).toBe('sid-1')
    // No `credentials` anywhere — this stack authenticates on a header, and a cookie would
    // be a second mechanism nothing serves.
    expect(calls[0]?.init).not.toHaveProperty('credentials')
  })

  it('reads a profile with no buckets as a real answer', async () => {
    const { fetcher } = recorder(() => json({ pseudonym: 'psn-1', buckets: {} }))
    const reading = await readProfile('sid-1', fetcher)
    expect(hasValue(reading)).toBe(true)
    if (reading.state === 'ok') expect(reading.value.buckets).toEqual({})
  })
})

describe('the live-check ledger keeps checked and declined apart', () => {
  it('reads records and refusals as two separate lists', async () => {
    const { fetcher } = recorder(() =>
      json({
        auction_id: 'a-1',
        records: [{ checked_at: 't', outcome: 'agrees', surface: 'pdp', fetch_reason: '' }],
        refused: [{ store_id: 's2', reason: 'no_url' }],
      }),
    )
    const reading = await readLiveCheck('a-1', fetcher)
    if (reading.state !== 'ok') throw new Error(`expected ok, got ${reading.state}`)
    expect(reading.value.records).toHaveLength(1)
    expect(reading.value.refused).toHaveLength(1)
  })

  it('does not invent a record for a refusal', () => {
    const reading = parseLiveCheck({ auction_id: 'a-1', refused: [{}, {}] }, 'a-1')
    expect(reading.records).toHaveLength(0)
    expect(reading.refused).toHaveLength(2)
  })
})

describe('the sources this origin cannot reach', () => {
  it('names the trust ledger, which is the most interesting of them', () => {
    const routes = UNREACHABLE_SOURCES.map((s) => s.route)
    expect(routes).toContain('GET /events/verify')
    expect(routes).toContain('GET /snapshot')
  })

  it('gives every source a reason AND a remedy, so the gap is a work item', () => {
    for (const source of UNREACHABLE_SOURCES) {
      expect(source.why.length).toBeGreaterThan(15)
      expect(source.remedy.length).toBeGreaterThan(15)
      expect(source.port).toMatch(/^\d{4}$/)
    }
  })

  it('never claims a port this page could actually fetch from', () => {
    // Everything on this list is on another origin by definition; a `/buyer/` route would
    // belong in a panel, not in the list of things that cannot be read.
    for (const source of UNREACHABLE_SOURCES) {
      expect(source.route).not.toContain('/buyer/')
    }
  })

  it('builds an openable URL on the right port for the ones needing no credential', () => {
    const verify = UNREACHABLE_SOURCES.find((s) => s.route === 'GET /events/verify')!
    expect(directUrl(verify, { protocol: 'http:', hostname: 'localhost' })).toBe(
      'http://localhost:8084/events/verify',
    )
    // The loss report needs a per-store bearer, so it is not offered as a link.
    const losses = UNREACHABLE_SOURCES.find((s) => s.service === 'exchange')!
    expect(losses.openable).toBe(false)
  })
})
