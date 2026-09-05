/**
 * The wire the shopper journey needs and the already-tested modules do not own.
 *
 * `app/intent/intent.ts` owns `POST /buyer/intent/clarify` and `POST /buyer/intent/confirm`.
 * `app/shortlist/shortlist.ts` owns `POST /buyer/shortlist/accept`. Two calls sit between
 * them and are not spoken for anywhere else, so they live here:
 *
 *   * `GET  /buyer/auctions/{auction_id}`  — the live shortlist AND the diagnostics the
 *     exchange only ever publishes once, in the body of its `POST /auctions` answer. The
 *     buyer service records that answer so this screen can show a buyer *why* an empty
 *     shortlist is empty instead of showing them a blank page.
 *   * `POST /buyer/shortlist/render`       — the provenance labels for each slot.
 *
 * Three things here are load-bearing rather than decorative:
 *
 * 1. **Every response is validated at runtime.** A JSON body is `unknown` however the types
 *    are written, and a screen whose whole claim is "this came from the service" cannot
 *    also be the screen that casts an unread body into a shape and renders it.
 * 2. **The shortlist is forwarded to `/render` byte-for-byte as the service sent it.** It is
 *    typed `unknown` on purpose. `buyer_svc.accept.labels.render_shortlist` derives labels
 *    from a slot's `claims` when the exchange sent no `provenance_labels`, and a client that
 *    re-typed the slot into its own interface on the way past would silently drop that field
 *    and turn a labelled slot into an `unverified` one.
 * 3. **`instrumentFetcher` keeps the service's own error message.** The reused modules throw
 *    `"accept failed: HTTP 503"` — the status but not the reason. This wrapper reads a
 *    non-OK body off a *clone*, so the module still gets an unread stream, and the shell can
 *    show the buyer what the service actually said instead of a bare number.
 *
 * Nothing in this module builds a checkout URL, and `discountCodeFrom` reads a query
 * parameter off a URL the exchange minted rather than reconstructing one. R3.
 */
import { CONFIRM_PATH, assertConfirmable, type AuctionCreated, type Intent } from '../intent/intent'
import type { Shortlist, ShortlistSlot, TrustSummary } from '../shortlist/shortlist'

/** Anything that behaves like `fetch`. Injected so tests need no network. */
export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>

/** `GET {AUCTION_PATH_PREFIX}{auction_id}` — added by the buyer's composition root. */
export const AUCTION_PATH_PREFIX = '/buyer/auctions/'

/** `POST` — labels a shortlist for display. Creates nothing, accepts nothing. */
export const RENDER_PATH = '/buyer/shortlist/render'

/** The query parameter the exchange's permalink carries the minted code in. */
export const DISCOUNT_PARAM = 'discount'

/**
 * The exchange's `fallback_reason` for a store whose agent did not answer with a usable bid.
 * Recognised so the screen can explain it in a sentence — never re-derived, never invented.
 */
export const NO_RESPONSE_REASON = 'no_response'

/** R5's rotating handle. The only thing about the buyer a store is ever told. */
export const PSEUDONYM_PREFIX = 'psn-'

/**
 * The pseudonymous buyer profile the confirm route forwards to the exchange.
 *
 * `contracts.BidRequest` makes `profile` REQUIRED, so this is not decoration. Measured
 * against the running stack: confirming without one has the exchange coerce it to `{}`, the
 * store agent answer HTTP 422, and the exchange report that as
 * `fallback: true, fallback_reason: "no_response"` for every store — a silently empty
 * shortlist whose stated reason is true of the wire and false of the market.
 */
export interface BuyerProfile {
  readonly pseudonym: string
  /** R5's k-anonymity buckets. Empty here: this page has learned nothing to bucket. */
  readonly buckets: Readonly<Record<string, unknown>>
}

/** Thrown when a confirmation is about to go out with no usable pseudonym behind it. */
export class MissingProfileError extends Error {
  constructor(readonly reason: string) {
    super(
      `the exchange requires a pseudonymous buyer profile before any store is solicited: ` +
        `${reason}. Sending none makes every store answer 422 and the whole shortlist come ` +
        `back empty for a reason that is not the market's.`,
    )
    this.name = 'MissingProfileError'
  }
}

/**
 * A fresh pseudonym for this page visit. `psn-` and ten hex characters of CSPRNG.
 *
 * Minted per mount and held in component state — deliberately NOT in `localStorage`. A
 * persisted handle would stop rotating, and a handle that does not rotate is a stable
 * identifier for the stores to join on, which is the exact thing R5's pseudonym exists to
 * deny them.
 */
export function mintPseudonym(): string {
  const bytes = new Uint8Array(5)
  crypto.getRandomValues(bytes)
  let hex = ''
  for (const byte of bytes) {
    hex += byte.toString(16).padStart(2, '0')
  }
  return `${PSEUDONYM_PREFIX}${hex}`
}

/** One rostered store's answer, as `POST /auctions` reported it. */
export interface AuctionEntry {
  readonly store_id: string
  readonly tier?: number
  /** True when the exchange represented this store itself rather than quoting its bid. */
  readonly fallback: boolean
  readonly unit_price?: number
  readonly total_price?: number
  /** Why the exchange fell back. `null` when it did not have to. */
  readonly fallback_reason: string | null
}

/** One candidate the eligibility filters refused, and every reason they refused it. */
export interface ExcludedBid {
  readonly bid_ref: string
  readonly store_id: string
  readonly exclusion_reasons: readonly string[]
}

/** One store that was not allowed to bid at all. */
export interface Denial {
  readonly store_id: string
  readonly status: string
  readonly reason: string
}

/** One candidate the published ranking scored. */
export interface RankedBid {
  readonly bid_ref: string
  readonly store_id: string
  readonly rank_score: number
  readonly components: Readonly<Record<string, number>>
}

/** What `GET /buyer/auctions/{auction_id}` answers. */
export interface AuctionRecord {
  readonly auction_id: string
  /**
   * The live shortlist EXACTLY as the service sent it, forwarded to `/render` untouched.
   * See this module's docstring for why it is not re-typed on the way through.
   */
  readonly shortlist: unknown
  /** How many slots that shortlist carries. Zero is the case `WhyEmpty` exists for. */
  readonly slot_count: number
  readonly entries: readonly AuctionEntry[]
  readonly excluded: readonly ExcludedBid[]
  readonly denied: readonly Denial[]
  readonly ranked: readonly RankedBid[]
  readonly solicited: readonly string[]
  readonly recorded_at: string
  /** The parsed body, kept so a reader can check the panel against the answer itself. */
  readonly raw: unknown
}

/** One slot as `POST /buyer/shortlist/render` returns it, ready for `ShortlistView`. */
export interface RenderedSlot extends ShortlistSlot {
  readonly slot: string
  readonly bid_ref: string
  /** Pushed down from the shortlist by the service, so `acceptSlot` can name the auction. */
  readonly auction_id: string
  readonly fit_score: number
  readonly provenance_labels: readonly string[]
  /** `exchange` | `derived` | `absent` — how the labels got there, not guessed at after. */
  readonly labels_source: string
  /**
   * The numeric fields only, because `ShortlistView` takes a `TrustSummary` and that type
   * is `Record<string, number>`. Use it ONLY for that component; it is a lossy view.
   */
  readonly trust_summary: TrustSummary
  /**
   * Every field of the trust summary, as the service sent it. It exists because the
   * narrowing above is lossy and this screen may not quietly drop what a service said:
   * measured, the exchange sends `{"store_id": "demo-woolworks", "available": true,
   * "score": 0.82}` and only `score` survives `TrustSummary`. `describeTrust` renders this
   * one, so the two dropped fields are on the page rather than in a type.
   */
  readonly trust_fields: Readonly<Record<string, unknown>>
  readonly store_domain: string
}

/** What the service answered with when it refused. Kept so the buyer sees its words. */
export interface FailureRecord {
  readonly path: string
  readonly status: number
  /** The service's own message, or `''` when it sent nothing readable. */
  readonly detail: string
}

/** A fetcher that remembers the last non-OK answer it saw. */
export interface InstrumentedFetcher {
  readonly fetcher: Fetcher
  latest(): FailureRecord | undefined
  reset(): void
}

/** Thrown when a request this module owns came back non-OK. Names the status, always. */
export class HttpFailure extends Error {
  constructor(
    readonly where: string,
    readonly status: number,
    readonly detail: string,
  ) {
    super(
      detail
        ? `${where} failed: HTTP ${status} — ${detail}`
        : `${where} failed: HTTP ${status}`,
    )
    this.name = 'HttpFailure'
  }
}

/** Thrown when a 200 carried something this screen cannot honestly render. */
export class MalformedResponseError extends Error {
  constructor(
    readonly where: string,
    readonly reason: string,
  ) {
    super(`${where} answered 200 with a body this screen cannot render: ${reason}`)
    this.name = 'MalformedResponseError'
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asFiniteNumber(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function asArray(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? (value as readonly unknown[]) : []
}

function asUnknownMap(value: unknown): Readonly<Record<string, unknown>> {
  return isRecord(value) ? { ...value } : {}
}

function asNumberMap(value: unknown): Readonly<Record<string, number>> {
  if (!isRecord(value)) return {}
  const out: Record<string, number> = {}
  for (const [key, item] of Object.entries(value)) {
    if (typeof item === 'number' && Number.isFinite(item)) out[key] = item
  }
  return out
}

/**
 * The service's own message, dug out of a body that may be JSON or may be anything.
 *
 * FastAPI answers `{"detail": "..."}`, and the accept route answers
 * `{"detail": {"message": "...", "denial_reason": "..."}}` for a refusal the exchange made.
 * A 422 answers `{"detail": [ ... ]}`. All three are the service talking, so all three are
 * shown; only an unreadable body reduces to `''`, and the status is printed either way.
 */
export function detailFromBody(body: string): string {
  const trimmed = body.trim()
  if (!trimmed) return ''
  let parsed: unknown
  try {
    parsed = JSON.parse(trimmed) as unknown
  } catch {
    return trimmed.slice(0, 500)
  }
  if (!isRecord(parsed)) return trimmed.slice(0, 500)
  const detail = parsed.detail
  if (typeof detail === 'string') return detail
  if (isRecord(detail)) {
    const message = detail.message
    const extras = Object.entries(detail)
      .filter(([key]) => key !== 'message')
      .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    const head = typeof message === 'string' ? message : JSON.stringify(detail)
    return extras.length > 0 ? `${head} (${extras.join(', ')})` : head
  }
  if (detail !== undefined) return JSON.stringify(detail).slice(0, 500)
  return trimmed.slice(0, 500)
}

/**
 * Wrap a fetcher so the last refusal it saw keeps the service's own words.
 *
 * The body is read off `response.clone()`, so the wrapped module receives a response whose
 * stream nobody has touched and its own `await response.json()` still works.
 */
export function instrumentFetcher(inner: Fetcher): InstrumentedFetcher {
  let seen: FailureRecord | undefined
  const fetcher: Fetcher = async (input, init) => {
    const response = await inner(input, init)
    if (!response.ok) {
      let body = ''
      try {
        body = await response.clone().text()
      } catch {
        body = ''
      }
      seen = { path: input, status: response.status, detail: detailFromBody(body) }
    }
    return response
  }
  return {
    fetcher,
    latest: () => seen,
    reset: () => {
      seen = undefined
    },
  }
}

/**
 * One sentence for the buyer: what threw, and what the service said while it was throwing.
 *
 * The reused modules throw with the status and no body. The status is never dropped and the
 * detail is never invented — when there is no recorded refusal the thrown message stands
 * alone.
 */
export function explain(error: unknown, failure?: FailureRecord): string {
  const base = error instanceof Error ? error.message : String(error)
  if (failure === undefined || failure.detail === '') return base
  if (base.includes(failure.detail)) return base
  return `${base} — the service said: ${failure.detail}`
}

async function readJson(response: Response, where: string): Promise<unknown> {
  const body = await response.text()
  if (!response.ok) throw new HttpFailure(where, response.status, detailFromBody(body))
  try {
    return JSON.parse(body) as unknown
  } catch {
    return Promise.reject(new MalformedResponseError(where, 'the body was not JSON'))
  }
}

/** `entries[]` — every rostered store and what it answered with. */
export function readEntries(value: unknown): readonly AuctionEntry[] {
  return asArray(value)
    .filter(isRecord)
    .map((row) => ({
      store_id: asString(row.store_id),
      tier: typeof row.tier === 'number' ? row.tier : undefined,
      fallback: row.fallback === true,
      unit_price: typeof row.unit_price === 'number' ? row.unit_price : undefined,
      total_price: typeof row.total_price === 'number' ? row.total_price : undefined,
      fallback_reason: isNonEmptyString(row.fallback_reason) ? row.fallback_reason : null,
    }))
}

/** `excluded[]` — every reason each refused candidate was refused, not just the first. */
export function readExcluded(value: unknown): readonly ExcludedBid[] {
  return asArray(value)
    .filter(isRecord)
    .map((row) => ({
      bid_ref: asString(row.bid_ref),
      store_id: asString(row.store_id),
      exclusion_reasons: asArray(row.exclusion_reasons).filter(isNonEmptyString),
    }))
}

/** `denied[]` — stores that were not allowed to bid. */
export function readDenied(value: unknown): readonly Denial[] {
  return asArray(value)
    .filter(isRecord)
    .map((row) => ({
      store_id: asString(row.store_id),
      status: asString(row.status),
      reason: asString(row.reason),
    }))
}

/** `ranked[]` — the eligible candidates in published rank order. */
export function readRanked(value: unknown): readonly RankedBid[] {
  return asArray(value)
    .filter(isRecord)
    .map((row) => ({
      bid_ref: asString(row.bid_ref),
      store_id: asString(row.store_id),
      rank_score: asFiniteNumber(row.rank_score, 0),
      components: asNumberMap(row.components),
    }))
}

/**
 * Confirm the intent, carrying the pseudonymous profile the exchange needs to solicit.
 *
 * This exists because `intent.ts`'s `confirmIntent` has no parameter for a profile and the
 * body it sends therefore has none. That module is owned and tested elsewhere and is not
 * edited from here; this function is the same request with the field the wire requires,
 * and it reuses `assertConfirmable` rather than restating what confirmable means.
 *
 * `confirmed` is the literal boolean `true`. The service types it `StrictBool`, so the
 * string `"true"` is a 422 — and, worse, a lax `bool` field would have coerced it and
 * opened an auction.
 */
export async function confirmWithProfile(
  intent: unknown,
  profile: BuyerProfile,
  fetcher: Fetcher,
): Promise<AuctionCreated> {
  assertConfirmable(intent)
  if (!isNonEmptyString(profile.pseudonym)) {
    throw new MissingProfileError('the profile carries no pseudonym')
  }
  const body: { intent: Intent; confirmed: true; profile: BuyerProfile } = {
    intent,
    confirmed: true,
    profile,
  }
  const response = await fetcher(CONFIRM_PATH, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  const payload = await readJson(response, 'confirm')
  if (!isRecord(payload) || !isNonEmptyString(payload.auction_id)) {
    throw new MalformedResponseError('confirm', 'it named no auction id')
  }
  return {
    auction_id: payload.auction_id,
    intent_id: isNonEmptyString(payload.intent_id) ? payload.intent_id : '',
    created_at: isNonEmptyString(payload.created_at) ? payload.created_at : '',
  }
}

/** The path `loadAuction` reads. Exported so a test asserts on the spelling, not a guess. */
export function auctionPath(auctionId: string): string {
  return `${AUCTION_PATH_PREFIX}${encodeURIComponent(auctionId.trim())}`
}

/**
 * The live shortlist plus the diagnostics the exchange published exactly once.
 *
 * A non-OK status is thrown as an `HttpFailure` naming it. A 200 whose body has no shortlist
 * is thrown too: a screen that quietly rendered "no options" for a malformed answer would be
 * telling the buyer something about the market that it does not know.
 */
export async function loadAuction(auctionId: string, fetcher: Fetcher): Promise<AuctionRecord> {
  const wanted = auctionId.trim()
  if (!wanted) {
    throw new MalformedResponseError('load auction', 'there is no auction id to load')
  }
  const response = await fetcher(auctionPath(wanted), {
    method: 'GET',
    headers: { accept: 'application/json' },
  })
  const payload = await readJson(response, 'load auction')
  if (!isRecord(payload)) {
    throw new MalformedResponseError('load auction', 'the body was not an object')
  }
  const shortlist = payload.shortlist
  if (!isRecord(shortlist) || !Array.isArray(shortlist.slots)) {
    throw new MalformedResponseError(
      'load auction',
      'it carried no shortlist with a slots array',
    )
  }
  return {
    auction_id: isNonEmptyString(payload.auction_id) ? payload.auction_id : wanted,
    shortlist,
    slot_count: shortlist.slots.length,
    entries: readEntries(payload.entries),
    excluded: readExcluded(payload.excluded),
    denied: readDenied(payload.denied),
    ranked: readRanked(payload.ranked),
    solicited: asArray(payload.solicited).filter(isNonEmptyString),
    recorded_at: asString(payload.recorded_at),
    raw: payload,
  }
}

/**
 * Label a shortlist for display (R2). `shortlist` is forwarded verbatim — see the docstring.
 *
 * Returns the slots the service labelled, each carrying the auction id the service pushed
 * down onto it, so `acceptSlot` has an auction to name.
 */
export async function renderShortlist(
  shortlist: unknown,
  fetcher: Fetcher,
): Promise<readonly RenderedSlot[]> {
  const response = await fetcher(RENDER_PATH, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ shortlist }),
  })
  const payload = await readJson(response, 'render shortlist')
  if (!isRecord(payload) || !Array.isArray(payload.slots)) {
    throw new MalformedResponseError('render shortlist', 'it carried no slots array')
  }
  return payload.slots.filter(isRecord).map(
    (row): RenderedSlot => ({
      slot: asString(row.slot),
      bid_ref: asString(row.bid_ref),
      auction_id: asString(row.auction_id),
      fit_score: asFiniteNumber(row.fit_score, 0),
      provenance_labels: asArray(row.provenance_labels).filter(isNonEmptyString),
      labels_source: asString(row.labels_source),
      trust_summary: asNumberMap(row.trust_summary),
      trust_fields: asUnknownMap(row.trust_summary),
      store_domain: asString(row.store_domain),
    }),
  )
}

/**
 * Every field of a slot's trust summary, spelled as the service sent it.
 *
 * `JSON.stringify` per value rather than `String`, so a boolean `true` and the string
 * `"true"` do not read identically on the page.
 */
export function describeTrust(fields: Readonly<Record<string, unknown>>): string {
  const entries = Object.entries(fields)
  if (entries.length === 0) return 'no trust snapshot'
  return entries.map(([key, value]) => `${key}=${JSON.stringify(value)}`).join(' ')
}

/** The shortlist `ShortlistView` renders: the service's slots under the auction they name. */
export function renderedShortlist(
  auctionId: string,
  slots: readonly RenderedSlot[],
): Shortlist {
  return { auction_id: auctionId, slots }
}

/**
 * The single-use code the exchange put in its own permalink, or `undefined`.
 *
 * READ off the returned URL with `URL.searchParams` — never reconstructed, never pattern
 * matched out of a string, and never minted. A permalink with no `discount` parameter has no
 * code, and this says so rather than making one up.
 */
export function discountCodeFrom(url: unknown): string | undefined {
  if (!isNonEmptyString(url)) return undefined
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return undefined
  }
  const code = parsed.searchParams.get(DISCOUNT_PARAM)
  return isNonEmptyString(code) ? code : undefined
}

/** The host a permalink will actually send the browser to, or `undefined` if unparseable. */
export function permalinkHost(url: unknown): string | undefined {
  if (!isNonEmptyString(url)) return undefined
  try {
    return new URL(url).hostname || undefined
  } catch {
    return undefined
  }
}
