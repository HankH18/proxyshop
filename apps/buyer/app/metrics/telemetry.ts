/**
 * What the metrics and tracing page is allowed to say, and where each sentence comes from.
 *
 * =======================================================================================
 * THE ONE FACT THAT SHAPED THIS WHOLE MODULE
 *
 * This page is served from the buyer origin, and from that origin almost none of this
 * stack's telemetry is readable. Two measurements, both from files in this repo:
 *
 *   1. `deploy/buyer-web/nginx.conf` has exactly four `location` blocks, and exactly ONE of
 *      them proxies an API: `location /buyer/` -> `http://buyer-svc:8081`. There is no
 *      `/trust/`, no `/exchange/`, no `/merchant/`, no `/ingest/` block. Every other path
 *      falls into `try_files $uri $uri/ /index.html`, which answers HTML.
 *   2. No service in this repo installs CORS. No `CORSMiddleware`, no `allow_origins` and no
 *      `Access-Control-*` header is set by any service under `apps/`, `services/` or
 *      `packages/`; the only middleware any of them adds is `RequestIdMiddleware`. Three
 *      separate files say so in prose as a design statement rather than an oversight — the
 *      nginx header, `apps/buyer/compose.yaml`, and `apps/merchant/Dockerfile.web`.
 *
 * So a `fetch()` from this page to the trust ledger on :8084, the exchange on :8083, the
 * merchant on :8082 or ingest on :8085 is blocked by the browser before it is sent. Not
 * slow, not flaky — impossible, by design, and the design is deliberate.
 *
 * A metrics page can respond to that in one of two ways. It can fire those requests anyway
 * and render whatever the failure looks like as an empty panel or a zero, which is the
 * defect this repo has spent a dozen commits closing: a number nothing produced. Or it can
 * KNOW what it cannot reach, say so by name, and say what would have to change. This module
 * does the second. {@link UNREACHABLE_SOURCES} is that list, and it carries the exact
 * `location` block each one needs, so the gap is a work item rather than a mystery.
 *
 * =======================================================================================
 * A BLANK IS NOT A ZERO
 *
 * Every reading is a {@link Reading}, a tagged union with six states, and NONE of them is a
 * number that stands in for an absent one. `ok` carries a value. `unauthorized`, `absent`,
 * `failed` and `unreachable` carry a sentence about why there is no value. `idle` means
 * nobody has asked yet. The page renders each state differently on purpose: "0 stores
 * answered" and "this page could not find out how many stores answered" are different
 * claims, and only the first is ever the market's.
 *
 * =======================================================================================
 * WHAT DOES NOT EXIST, so that nothing here invents it
 *
 * Searched across `apps/`, `services/`, `packages/`, `deploy/` and every compose file:
 *
 *   * There is NO `/metrics` route on any service. No Prometheus, no OpenTelemetry.
 *   * There is NO `/readyz` and NO `/livez` anywhere.
 *   * There is exactly one `/healthz` in the repo and it belongs to `services/shopify-stub`,
 *     a fake Shopify on :8787. It is not a ProxyShop service.
 *   * NO service records request latency, request counts or error rates. The only middleware
 *     any of them installs is `RequestIdMiddleware`, which mints a correlation id.
 *   * The compose healthchecks are not served routes: they shell out to
 *     `python -m proxyshop_support.service_launch ready`, which probes `/openapi.json` from
 *     INSIDE the container. A browser cannot reach any of it.
 *
 * This page therefore shows no latency chart, no request rate, no error rate and no uptime.
 * Those would each be a fabrication, and a fabricated number on a page called "metrics" is
 * worse than an absent one.
 */

/** Anything that behaves like `fetch`. Injected so tests need no network — house style. */
export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>

/**
 * Where the journey leaves the auction id it last opened, so the metrics page can offer it.
 *
 * An auction id and NOT a session id, and that distinction is the whole reason this constant
 * gets a paragraph. `Journey.tsx` holds its session in component state "and NOWHERE else.
 * Not `localStorage`, and not `sessionStorage`: the session id is a bearer credential for
 * this origin" — its own words, and a deliberate decision this demo aid does not get to
 * overturn for its own convenience.
 *
 * An auction id is not a credential. `GET /buyer/auctions/{auction_id}` takes no
 * authentication at all, and the id is already in the address bar of nothing and the secret
 * of nobody. Remembering one lets the trace panel survive a navigation between `#/` and
 * `#/metrics` without persisting anything that could sign a person in.
 *
 * The consequence, accepted rather than worked around: the metrics page's session panel is
 * normally `unauthorized`, and it says so in those words instead of showing a blank
 * pseudonym.
 */
export const REMEMBERED_AUCTION_KEY = 'proxyshop.demo.auction_id'

/** Read one remembered value, tolerating a storage that refuses (private mode, no storage). */
export function remembered(key: string): string {
  try {
    return window.sessionStorage.getItem(key) ?? ''
  } catch {
    return ''
  }
}

/** Remember one value, tolerating a storage that refuses. Never used for a credential. */
export function remember(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value)
  } catch {
    /* A browser that refuses storage costs this page a convenience, never a correctness. */
  }
}

/**
 * Forget one remembered value.
 *
 * Exists because signing out has to reach this storage too. An auction id is not a
 * credential, but the trace it opens is not nothing: `GET /buyer/auctions/{id}` takes no
 * session header, so anyone holding the tab can read every solicited store, every store's
 * answer and price, every exclusion reason and the exchange's ranking components for that
 * auction. `Journey`'s sign-out already clears the shortlist from the screen because "the
 * shortlist belonged to a pseudonym the vault has now retired", and its own gloss promises
 * signing out "clears the auction below with it". A remembered id that outlived sign-out
 * would make that sentence false and leave the retired pseudonym's auction one hash change
 * away.
 */
export function forget(key: string): void {
  try {
    window.sessionStorage.removeItem(key)
  } catch {
    /* Same as `remember`: a storage that refuses is not a correctness problem. */
  }
}

/**
 * Why a panel has no value.
 *
 * These are kept apart because they are different sentences to whoever is driving the demo:
 * `unreachable` is a fact about this deployment's topology and will not change by retrying;
 * `failed` might. `absent` is the service answering "I have no record", which is data.
 */
export type ReadingState =
  | 'idle'
  | 'loading'
  | 'ok'
  | 'absent'
  | 'unauthorized'
  | 'unreachable'
  | 'failed'

/** A value, or a stated reason there is none. Never a zero standing in for a blank. */
export type Reading<T> =
  | { readonly state: 'idle' }
  | { readonly state: 'loading' }
  | { readonly state: 'ok'; readonly value: T }
  | { readonly state: 'absent'; readonly detail: string }
  | { readonly state: 'unauthorized'; readonly detail: string }
  | { readonly state: 'unreachable'; readonly detail: string }
  | { readonly state: 'failed'; readonly detail: string }

/** True when a reading carries a value to render. */
export function hasValue<T>(reading: Reading<T>): reading is { state: 'ok'; value: T } {
  return reading.state === 'ok'
}

/** The sentence a reading carries when it has no value, or `''` when it has one. */
export function readingDetail(reading: Reading<unknown>): string {
  switch (reading.state) {
    case 'absent':
    case 'unauthorized':
    case 'unreachable':
    case 'failed':
      return reading.detail
    case 'idle':
      return 'not read yet'
    case 'loading':
      return 'reading…'
    default:
      return ''
  }
}

/* ------------------------------------------------------------------------------------- *
 * The routes this page CAN read, spelled once each so a test asserts on the spelling.
 * Every one is under `/buyer/`, which is the only prefix nginx proxies.
 * ------------------------------------------------------------------------------------- */

/*
 * Each route is named by its module and prefix rather than by a line number: the buyer
 * service is edited often enough that a pinned line drifts within a day, and a stale
 * pointer in a comment is worse than none. The spellings themselves are asserted in
 * `telemetry.test.ts` and were probed against the running app.
 */

/** `GET /buyer/auth/session` — `buyer_svc.auth.routes`, mounted under its `/buyer` prefix. */
export const SESSION_PATH = '/buyer/auth/session'

/** `GET /buyer/profile` — also `buyer_svc.auth.routes`; there is no `profile` router. */
export const PROFILE_PATH = '/buyer/profile'

/** `GET /buyer/auctions/{auction_id}` — `buyer_svc.auctions.routes`, prefix `/buyer/auctions`. */
export const AUCTION_PATH_PREFIX = '/buyer/auctions/'

/** `GET /buyer/livecheck/{auction_id}` — `buyer_svc.livecheck.routes`, prefix `/buyer/livecheck`. */
export const LIVECHECK_PATH_PREFIX = '/buyer/livecheck/'

/** The header `buyer_svc` reads a session out of. Not a cookie — no request here sends one. */
export const SESSION_HEADER = 'X-Buyer-Session'

/** Path for one auction's recorded trace. */
export function auctionPath(auctionId: string): string {
  return `${AUCTION_PATH_PREFIX}${encodeURIComponent(auctionId.trim())}`
}

/** Path for one auction's live-check ledger. */
export function livecheckPath(auctionId: string): string {
  return `${LIVECHECK_PATH_PREFIX}${encodeURIComponent(auctionId.trim())}`
}

/* ------------------------------------------------------------------------------------- *
 * The routes this page CANNOT read, and exactly what each would need.
 * ------------------------------------------------------------------------------------- */

/** One telemetry source that exists and is real, but not from this origin. */
export interface UnreachableSource {
  /** What it would tell you. */
  readonly label: string
  /** The service and the route, spelled as the service serves it. */
  readonly route: string
  /** Which service, and the compose default port it is published on. */
  readonly service: string
  /** The compose default host port. */
  readonly port: string
  /** Why this page cannot fetch it. */
  readonly why: string
  /** What would have to change for it to become readable from here. */
  readonly remedy: string
  /** Whether opening it directly in a browser tab works — i.e. whether it needs no auth. */
  readonly openable: boolean
}

/**
 * Every telemetry source worth showing that this origin cannot fetch.
 *
 * They are listed rather than silently omitted because a metrics page that shows only what
 * is easy is a misleading map of the system. Each row names the route, the reason, and the
 * fix — and `openable` records whether the operator can simply click through to it in
 * another tab, which most of them can: a browser NAVIGATION to another origin is fine, and
 * only `fetch` is blocked. That is why this page links out instead of proxying.
 */
export const UNREACHABLE_SOURCES: readonly UnreachableSource[] = [
  {
    label: 'Hash-chain verification of the trust ledger',
    route: 'GET /events/verify',
    service: 'trust',
    port: '8084',
    why:
      'A different origin from this page, and no service in this stack installs CORS, so ' +
      'the browser refuses the request before it is sent. nginx proxies only /buyer/.',
    remedy:
      "add a `location /trust/` proxy block to deploy/buyer-web/nginx.conf pointing at " +
      'http://trust:8084, or a read-through route on buyer-svc under /buyer/.',
    openable: true,
  },
  {
    label: 'The ledger itself — every recorded event, in chain order',
    route: 'GET /events',
    service: 'trust',
    port: '8084',
    why: 'Same origin barrier as /events/verify.',
    remedy: 'the same /trust/ proxy block would serve both.',
    openable: true,
  },
  {
    label: 'The ledger head — its hash and length right now',
    route: 'GET /events/head',
    service: 'trust',
    port: '8084',
    why: 'Same origin barrier as /events/verify.',
    remedy: 'the same /trust/ proxy block would serve this too.',
    openable: true,
  },
  {
    label: "Every store's trust score, confidence and six dimensions",
    route: 'GET /snapshot',
    service: 'trust',
    port: '8084',
    why: 'Same origin barrier. This is also the only route that LISTS stores.',
    remedy: 'the same /trust/ proxy block.',
    openable: true,
  },
  {
    label: 'Reconciliation — what the ledger folded, and what it could not join',
    route: 'GET /reconcile',
    service: 'trust',
    port: '8084',
    why: 'Same origin barrier.',
    remedy: 'the same /trust/ proxy block.',
    openable: true,
  },
  {
    label: "A store's lost auctions, bucketed by reason",
    route: 'GET /reports/losses?start=&end=',
    service: 'exchange',
    port: '8083',
    why:
      'A different origin AND a per-store bearer token: the exchange derives the store from ' +
      'the Authorization header, so there is no store_id parameter. A shopper page holds ' +
      "no store's token and must not.",
    remedy:
      'this one belongs on the merchant console, which already reads it server-side, ' +
      'rather than on a page served to shoppers.',
    openable: false,
  },
  {
    label: 'The merchant dashboard payload — trust, losses, bids and envelope in one call',
    route: 'GET /stores/{store_id}/dashboard',
    service: 'merchant',
    port: '8082',
    why:
      'A different origin AND a global admin bearer (MERCHANT_ADMIN_TOKEN). It is already ' +
      'the aggregate view, but it is the operator’s, not the shopper’s.',
    remedy: 'open the merchant console itself — the demo nav links to it.',
    openable: false,
  },
  {
    label: 'Ingest freshness — which store facts are stale and when they are next due',
    route: 'GET /schedule',
    service: 'ingest',
    port: '8085',
    why: 'A different origin with no CORS and no proxy block.',
    remedy: 'an /ingest/ proxy block, if this is worth showing a shopper at all.',
    openable: true,
  },
]

/**
 * A browser-openable URL for a source on another port of this same host.
 *
 * Navigation across origins is not what CORS restricts — only scripted reads are. So the
 * page hands the operator a real link and says plainly that it opens a different service,
 * rather than proxying data it cannot legitimately fetch.
 */
export function directUrl(
  source: UnreachableSource,
  location: { protocol: string; hostname: string },
): string {
  const path = source.route.replace(/^[A-Z]+\s+/, '').split('?')[0] ?? '/'
  return `${location.protocol}//${location.hostname}:${source.port}${path}`
}

/* ------------------------------------------------------------------------------------- *
 * Runtime validation. A JSON body is `unknown` however the types are written.
 * ------------------------------------------------------------------------------------- */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asArray(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : []
}

/**
 * A finite number, or `undefined` — NEVER `0`.
 *
 * The same rule `wire.ts` states for `rank_score`: a defaulted zero prints as a measurement
 * the service made, when in fact it is this client failing to find one.
 */
function asNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

/** Turn a thrown value into one sentence, without inventing detail it did not carry. */
export function describeError(error: unknown): string {
  if (error instanceof Error && error.message !== '') return error.message
  return String(error)
}

/**
 * Read a response into a {@link Reading}, mapping each status onto the state it actually
 * means rather than collapsing everything into "failed".
 */
export async function readingFrom<T>(
  response: Response,
  where: string,
  parse: (payload: unknown) => T,
): Promise<Reading<T>> {
  if (response.status === 401 || response.status === 403) {
    return {
      state: 'unauthorized',
      detail: `${where} refused this page: HTTP ${response.status}. ${await bodyHint(response)}`,
    }
  }
  if (response.status === 404) {
    return {
      state: 'absent',
      detail: `${where} has no record of that: HTTP 404. ${await bodyHint(response)}`,
    }
  }
  if (!response.ok) {
    return {
      state: 'failed',
      detail: `${where} answered HTTP ${response.status}. ${await bodyHint(response)}`,
    }
  }
  let payload: unknown
  try {
    payload = await response.json()
  } catch (error) {
    // THIS is where the SPA fallback lands. A path outside `/buyer/` is answered by nginx's
    // `try_files ... /index.html` with a 200 whose body is HTML, and `response.json()` throws
    // on it. Reporting "did not answer JSON" with the parser's own message beats letting an
    // "unexpected token <" reach a console while the panel renders blank.
    return { state: 'failed', detail: `${where} did not answer JSON: ${describeError(error)}` }
  }
  // A body that parsed as a bare JSON string — `"nope"` — is valid JSON and not an object,
  // so it would otherwise reach a parser expecting fields and produce a confusing message.
  if (typeof payload === 'string') {
    return { state: 'failed', detail: `${where} answered a JSON string, not an object.` }
  }
  try {
    return { state: 'ok', value: parse(payload) }
  } catch (error) {
    return { state: 'failed', detail: `${where}: ${describeError(error)}` }
  }
}

/** The first line of an error body, bounded, so a panel can quote the service's own words. */
async function bodyHint(response: Response): Promise<string> {
  try {
    const text = await response.clone().text()
    const trimmed = text.trim().slice(0, 300)
    return trimmed === '' ? '' : `The service said: ${trimmed}`
  } catch {
    return ''
  }
}

/* ------------------------------------------------------------------------------------- *
 * The readings themselves.
 * ------------------------------------------------------------------------------------- */

/** `GET /buyer/auth/session`, as the page shows it. */
export interface SessionReading {
  readonly session_id: string
  readonly pseudonym: string
  readonly issued_at: string
  readonly expires_at: string
}

/** `GET /buyer/profile`, as the page shows it. */
export interface ProfileReading {
  readonly pseudonym: string
  readonly buckets: Readonly<Record<string, unknown>>
}

/** One rostered store's answer, off the recorded auction. */
export interface TraceEntry {
  readonly store_id: string
  readonly fallback: boolean
  readonly fallback_reason: string | null
  readonly unit_price?: number
  readonly total_price?: number
}

/** One candidate the eligibility filters refused. */
export interface TraceExclusion {
  readonly bid_ref: string
  readonly store_id: string
  readonly exclusion_reasons: readonly string[]
}

/** One store that was not allowed to bid at all. */
export interface TraceDenial {
  readonly store_id: string
  readonly status: string
  readonly reason: string
}

/** One scored candidate, with the components the exchange published. */
export interface TraceRanked {
  readonly bid_ref: string
  readonly store_id: string
  readonly rank_score?: number
  readonly components: Readonly<Record<string, number>>
}

/**
 * One auction's recorded trace, exactly as `GET /buyer/auctions/{id}` served it.
 *
 * This is the page's single richest real source, and it is genuinely a TRACE rather than a
 * summary: it names every store that was solicited, what each one answered, which candidates
 * the filters refused and for what stated reason, which stores were denied entry, and the
 * published ranking with its score components. None of it is computed here.
 */
export interface AuctionTrace {
  readonly auction_id: string
  /** `'live'` when the exchange still holds the shortlist; `'forgotten'` after its TTL. */
  readonly liveness: 'live' | 'forgotten'
  readonly shortlist_slot_count: number
  readonly solicited: readonly string[]
  readonly entries: readonly TraceEntry[]
  readonly excluded: readonly TraceExclusion[]
  readonly denied: readonly TraceDenial[]
  readonly ranked: readonly TraceRanked[]
  readonly recorded_at: string
  /** The parsed body, so a reader can check the panel against the answer itself. */
  readonly raw: unknown
}

/** One completed live check, off `GET /buyer/livecheck/{id}`. */
export interface LiveCheckRecord {
  readonly checked_at: string
  readonly outcome: string
  readonly surface: string
  readonly fetch_reason: string
}

/**
 * What the platform checked for one auction, and what it declined to check.
 *
 * `refused` is kept apart from `records` because the service keeps them apart, and its own
 * docstring says why: "no page was checked for this slot" and "the page was checked and
 * agreed" are different answers, and rendering them the same claims evidence nobody has.
 */
export interface LiveCheckReading {
  readonly auction_id: string
  readonly records: readonly LiveCheckRecord[]
  readonly refused: readonly unknown[]
}

/** Parse the recorded auction. Throws on a body this page cannot read, never guesses. */
export function parseAuctionTrace(payload: unknown, wanted: string): AuctionTrace {
  if (!isRecord(payload)) throw new Error('the body was not an object')
  const shortlist = payload.shortlist
  // `null` ONLY, never `undefined`. A body with no `shortlist` key reads `undefined`, and
  // that is a malformed answer rather than "the exchange forgot it" — reading a missing key
  // as a forgotten auction would invent a reason the service never gave.
  const forgotten = shortlist === null
  // AND A MALFORMED SHORTLIST IS FATAL, which is the whole point of this branch.
  //
  // Without it, a body whose `shortlist` is absent, or is not an object, or carries no
  // `slots` array, fell through to an empty list and this function answered
  // `liveness: 'live', shortlist_slot_count: 0` — which the page then printed as
  // "live, 0 slots" toned `confirmed`, the strongest provenance tone in the design system.
  // That is a page reporting a measurement of zero, in green, for an answer it could not
  // read: precisely the defect this module's docstring promises it does not commit, and
  // precisely the class this repo has spent a dozen commits closing.
  //
  // `journey/wire.ts::loadAuction` treats the same shape as fatal on the same route, and for
  // the same stated reason — "a screen that quietly rendered 'no options' for a malformed
  // answer would be telling the buyer something about the market that it does not know".
  // Two readers of one route must not disagree about what a readable answer is.
  if (!forgotten && (!isRecord(shortlist) || !Array.isArray(shortlist.slots))) {
    throw new Error('it carried no shortlist with a slots array')
  }
  const slots = forgotten || !isRecord(shortlist) ? [] : (shortlist.slots as readonly unknown[])
  return {
    auction_id: asString(payload.auction_id) || wanted,
    liveness: forgotten ? 'forgotten' : 'live',
    shortlist_slot_count: slots.length,
    solicited: asArray(payload.solicited).map(asString).filter((id) => id !== ''),
    entries: asArray(payload.entries)
      .filter(isRecord)
      .map((row) => ({
        store_id: asString(row.store_id),
        fallback: row.fallback === true,
        fallback_reason: typeof row.fallback_reason === 'string' ? row.fallback_reason : null,
        unit_price: asNumber(row.unit_price),
        total_price: asNumber(row.total_price),
      })),
    excluded: asArray(payload.excluded)
      .filter(isRecord)
      .map((row) => ({
        bid_ref: asString(row.bid_ref),
        store_id: asString(row.store_id),
        exclusion_reasons: asArray(row.exclusion_reasons).map(asString).filter((r) => r !== ''),
      })),
    denied: asArray(payload.denied)
      .filter(isRecord)
      .map((row) => ({
        store_id: asString(row.store_id),
        status: asString(row.status),
        reason: asString(row.reason),
      })),
    ranked: asArray(payload.ranked)
      .filter(isRecord)
      .map((row) => {
        const components: Record<string, number> = {}
        if (isRecord(row.components)) {
          for (const [key, value] of Object.entries(row.components)) {
            const num = asNumber(value)
            if (num !== undefined) components[key] = num
          }
        }
        return {
          bid_ref: asString(row.bid_ref),
          store_id: asString(row.store_id),
          rank_score: asNumber(row.rank_score),
          components,
        }
      }),
    recorded_at: asString(payload.recorded_at),
    raw: payload,
  }
}

/** Parse the live-check ledger for one auction. */
export function parseLiveCheck(payload: unknown, wanted: string): LiveCheckReading {
  if (!isRecord(payload)) throw new Error('the body was not an object')
  return {
    auction_id: asString(payload.auction_id) || wanted,
    records: asArray(payload.records)
      .filter(isRecord)
      .map((row) => ({
        checked_at: asString(row.checked_at),
        outcome: asString(row.outcome),
        surface: asString(row.surface),
        fetch_reason: asString(row.fetch_reason),
      })),
    refused: asArray(payload.refused),
  }
}

/** Read one auction's recorded trace. */
export async function readAuctionTrace(
  auctionId: string,
  fetcher: Fetcher,
): Promise<Reading<AuctionTrace>> {
  const wanted = auctionId.trim()
  if (wanted === '') {
    return { state: 'idle' }
  }
  try {
    const response = await fetcher(auctionPath(wanted), {
      method: 'GET',
      headers: { accept: 'application/json' },
    })
    return await readingFrom(response, `GET ${auctionPath(wanted)}`, (payload) =>
      parseAuctionTrace(payload, wanted),
    )
  } catch (error) {
    return { state: 'failed', detail: `GET ${auctionPath(wanted)}: ${describeError(error)}` }
  }
}

/** Read one auction's live-check ledger. */
export async function readLiveCheck(
  auctionId: string,
  fetcher: Fetcher,
): Promise<Reading<LiveCheckReading>> {
  const wanted = auctionId.trim()
  if (wanted === '') {
    return { state: 'idle' }
  }
  try {
    const response = await fetcher(livecheckPath(wanted), {
      method: 'GET',
      headers: { accept: 'application/json' },
    })
    return await readingFrom(response, `GET ${livecheckPath(wanted)}`, (payload) =>
      parseLiveCheck(payload, wanted),
    )
  } catch (error) {
    return { state: 'failed', detail: `GET ${livecheckPath(wanted)}: ${describeError(error)}` }
  }
}

/**
 * Read the live session, if there is one.
 *
 * A signed-out visitor gets `unauthorized`, which is the truth and not an error: the route
 * requires an `X-Buyer-Session` header and this page has none to send. The panel says
 * "no session" rather than showing an empty pseudonym.
 */
export async function readSession(
  sessionId: string,
  fetcher: Fetcher,
): Promise<Reading<SessionReading>> {
  if (sessionId.trim() === '') {
    return {
      state: 'unauthorized',
      detail:
        'This page holds no session, by design. GET /buyer/auth/session needs an ' +
        'X-Buyer-Session header; the journey keeps that id in memory only — never in ' +
        'localStorage or sessionStorage, because it is a bearer credential — so navigating ' +
        'here does not carry it along. This is a refusal, not a reading of zero.',
    }
  }
  try {
    const response = await fetcher(SESSION_PATH, {
      method: 'GET',
      headers: { accept: 'application/json', [SESSION_HEADER]: sessionId },
    })
    return await readingFrom(response, `GET ${SESSION_PATH}`, (payload) => {
      if (!isRecord(payload)) throw new Error('the body was not an object')
      return {
        session_id: asString(payload.session_id),
        pseudonym: asString(payload.pseudonym),
        issued_at: asString(payload.issued_at),
        expires_at: asString(payload.expires_at),
      }
    })
  } catch (error) {
    return { state: 'failed', detail: `GET ${SESSION_PATH}: ${describeError(error)}` }
  }
}

/** Read the coarsened profile behind a live session. */
export async function readProfile(
  sessionId: string,
  fetcher: Fetcher,
): Promise<Reading<ProfileReading>> {
  if (sessionId.trim() === '') {
    return {
      state: 'unauthorized',
      detail:
        'This page holds no session, by design, so there is no account for the service to ' +
        'coarsen into buckets. A refusal, not an empty profile.',
    }
  }
  try {
    const response = await fetcher(PROFILE_PATH, {
      method: 'GET',
      headers: { accept: 'application/json', [SESSION_HEADER]: sessionId },
    })
    return await readingFrom(response, `GET ${PROFILE_PATH}`, (payload) => {
      if (!isRecord(payload)) throw new Error('the body was not an object')
      return {
        pseudonym: asString(payload.pseudonym),
        buckets: isRecord(payload.buckets) ? payload.buckets : {},
      }
    })
  } catch (error) {
    return { state: 'failed', detail: `GET ${PROFILE_PATH}: ${describeError(error)}` }
  }
}

/**
 * The family half of a `fallback_reason` — the mirror of the exchange's own splitter.
 *
 * `exchange.auction.collect.refusal_reason` writes every reason either as a bare word or as
 * `family:detail`, and reads one back by splitting on the FIRST colon. Copied rather than
 * imported, exactly as `journey/wire.ts` copies it, because this app does not import the
 * exchange's package.
 */
export function reasonFamily(reason: string | null | undefined): string {
  if (typeof reason !== 'string') return ''
  return (reason.split(':', 1)[0] ?? '').trim()
}

/** The detail half of a `fallback_reason`, or `''` when it carries none. */
export function reasonDetail(reason: string | null | undefined): string {
  if (typeof reason !== 'string') return ''
  const at = reason.indexOf(':')
  return at < 0 ? '' : reason.slice(at + 1).trim()
}
