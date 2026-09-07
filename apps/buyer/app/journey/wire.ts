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
 * Four things here are load-bearing rather than decorative:
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
 * 4. **A slot's price comes off that slot, and from nowhere else.** There used to be a join
 *    here — `entryForSlot`, which took the store id out of a slot's `bid_ref` (the exchange
 *    mints it as `{auction_id}:{store_id}`) and looked that store up in `entries[]` on the
 *    same response, because the shortlist slot carried no price at all. It has been deleted.
 *    `ShortlistSlot` now carries `product`, `price` and `commitments`, so the join would be
 *    a SECOND source of truth for the price, and a worse one: `entries[]` is the RECORDED
 *    half of that response — what the exchange said when the auction opened — while the slot
 *    is the LIVE half, re-fetched for this request. Two clocks, one price line. The recorded
 *    entries are still read, and still shown, as what they are: the diagnostics in
 *    `WhyEmpty`, where every rostered store's answer is listed whether or not it made the
 *    shortlist. `readPrice` never adds, converts, rounds or currency-formats a number, and a
 *    slot the exchange priced nothing for gets no price rather than a zero.
 *
 * Nothing in this module builds a checkout URL, and `discountCodeFrom` reads a query
 * parameter off a URL the exchange minted rather than reconstructing one. R3.
 */
import { CONFIRM_PATH, assertConfirmable, type AuctionCreated, type Intent } from '../intent/intent'
import {
  LABEL_UNVERIFIED,
  type Shortlist,
  type ShortlistPrice,
  type ShortlistProduct,
  type ShortlistSlot,
  type SlotCommitment,
  type SlotDiscount,
  type TrustSummary,
} from '../shortlist/shortlist'

/** Anything that behaves like `fetch`. Injected so tests need no network. */
export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>

/** `GET {AUCTION_PATH_PREFIX}{auction_id}` — added by the buyer's composition root. */
export const AUCTION_PATH_PREFIX = '/buyer/auctions/'

/** `POST` — labels a shortlist for display. Creates nothing, accepts nothing. */
export const RENDER_PATH = '/buyer/shortlist/render'

/** The query parameter the exchange's permalink carries the minted code in. */
export const DISCOUNT_PARAM = 'discount'

/**
 * The exchange's `fallback_reason` vocabulary — the FAMILIES, not the whole strings.
 *
 * `exchange.auction.collect.refusal_reason` writes every reason either as one of these words
 * on its own or as `family:detail`, and `fallback_reason_family` reads one back by splitting
 * on the FIRST colon. This app copies the families rather than whole values because the detail
 * half is open-ended by construction: on a refusal it is the HTTP status the store's agent
 * answered with, and on a decline it is a reason header the store itself chose. A screen that
 * matched whole strings would recognise `store_refused:422` and then fail to recognise
 * `store_refused:503`, which is the same store behaviour.
 *
 * Copied from `apps/exchange/src/auction/collect.py::FALLBACK_REASONS`, in its order. A copy
 * and not an import — this app does not import the exchange's package — so it can go stale,
 * and `WhyEmpty` therefore has an explicit sentence for a family it does not recognise rather
 * than assuming this list is complete.
 */
export const FALLBACK_REASON_FAMILIES = [
  'tier_0_no_agent',
  'no_response',
  'response_after_deadline',
  'response_carried_no_bid',
  'response_not_stamped',
  'arrival_stamp_unparseable',
  'bid_price_unreconcilable',
  'store_declined',
  'store_refused',
] as const

/** One word out of {@link FALLBACK_REASON_FAMILIES}. */
export type FallbackReasonFamily = (typeof FALLBACK_REASON_FAMILIES)[number]

/**
 * Nothing came back from that store's agent at all.
 *
 * It used to cover three different facts, because the solicitor mapped every non-200 answer to
 * `None`: a store that declined, a store that refused the solicitation, and a store that was
 * switched off were one indistinguishable entry. It now means only the last of those, and the
 * other two have words of their own below.
 */
export const NO_RESPONSE_REASON = 'no_response'

/**
 * The store ANSWERED and its answer was no — the store-agent contract's published `204`. The
 * detail, when there is one, is the reason the store stated in `x-proxyshop-decline-reason`.
 */
export const STORE_DECLINED_REASON = 'store_declined'

/**
 * The store answered something the exchange could not read a bid out of. The detail is the
 * HTTP status, and a `422` there is the exchange's own fault rather than the store's.
 */
export const STORE_REFUSED_REASON = 'store_refused'

/**
 * What the exchange writes in place of a detail that was empty, longer than its 64-character
 * bound, or spelled outside its allowlist. It says "there was a detail and it could not be
 * rendered" — never "there was no detail".
 */
export const UNDISCLOSED_REFUSAL_DETAIL = 'undisclosed'

/**
 * The family half of a `fallback_reason`, or `''` when there is nothing to read.
 *
 * The mirror of `exchange.auction.collect.fallback_reason_family`: split on the FIRST colon
 * and keep the left. A reason carrying no colon is all family.
 */
export function fallbackReasonFamily(reason: string | null | undefined): string {
  if (typeof reason !== 'string') return ''
  return (reason.split(':', 1)[0] ?? '').trim()
}

/**
 * The detail half of a `fallback_reason`, or `''` when it carries none.
 *
 * `''` is returned both for "no colon at all" and for "a colon with nothing after it", which
 * are the same fact to a reader: the exchange named no detail. `refusal_reason` never writes
 * the second — it returns the bare family instead — so that branch is defensive, not observed.
 */
export function fallbackReasonDetail(reason: string | null | undefined): string {
  if (typeof reason !== 'string') return ''
  const at = reason.indexOf(':')
  return at < 0 ? '' : reason.slice(at + 1).trim()
}

/** R5's rotating handle. The only thing about the buyer a store is ever told. */
export const PSEUDONYM_PREFIX = 'psn-'

/**
 * The pseudonymous buyer profile the confirm route forwards to the exchange.
 *
 * **What this comment used to say, so a reader can see what moved underneath it.** It stated
 * as measured fact that `contracts.BidRequest` makes `profile` REQUIRED and that "confirming
 * without one has the exchange coerce it to `{}`, the store agent answer HTTP 422, and the
 * exchange report that as `fallback: true, fallback_reason: "no_response"` for every store".
 * Both halves of that are now false:
 *
 * 1. **The exchange no longer sends `{}`.** `exchange.composition.solicitation_profile` mints
 *    an opaque per-auction pseudonym — `anon-{auction_id}` — for a confirmation that named no
 *    profile, and leaves the buckets exactly as the caller stated them, empty when there were
 *    none. The solicitation that goes out is a valid `BidRequest`, so the stores answer it.
 * 2. **A refusal is reported as itself.** When a store really does answer 422 —
 *    `ProfileBuckets` is `extra="forbid"`, so a profile with an undeclared bucket key still
 *    earns one — `exchange.auction.collect` names it `store_refused:422`, and a store that
 *    declines with the contract's `204` reads `store_declined:<the reason it stated>`.
 *    `no_response` has gone back to meaning the one thing it says: nothing arrived.
 *
 * So this field is no longer what makes the request legal, and the page is no longer choosing
 * between sending it and getting an empty shortlist blamed on the market. It is here because
 * R5's handle is the buyer's to state: `anon-{auction_id}` is a name the exchange gives this
 * shopper, and `mintPseudonym` is one the shopper arrives with.
 */
export interface BuyerProfile {
  readonly pseudonym: string
  /** R5's k-anonymity buckets. Empty here: this page has learned nothing to bucket. */
  readonly buckets: Readonly<Record<string, unknown>>
}

/**
 * Thrown when a confirmation is about to go out with no usable pseudonym behind it.
 *
 * No longer a prediction about the market. `solicitation_profile` replaces a blank or absent
 * pseudonym with its own `anon-{auction_id}`, so sending one would not empty the shortlist —
 * it would quietly swap the handle this page believes it is rotating for one the exchange
 * chose. That is the thing worth refusing: a page that thinks it named the buyer and did not.
 */
export class MissingProfileError extends Error {
  constructor(readonly reason: string) {
    super(
      `this page will not open an auction without a pseudonymous handle of its own: ` +
        `${reason}. The exchange would mint an "anon-{auction_id}" one in its place, so the ` +
        `buyer would be named by the exchange rather than naming itself, and this page would ` +
        `be showing a rotating pseudonym it never actually sent.`,
    )
    this.name = 'MissingProfileError'
  }
}

/**
 * There is no `mintPseudonym` here any more, and its absence is the point.
 *
 * It returned `psn-` plus ten hex characters of CSPRNG and `Journey` held one per mount. Its
 * docstring argued it was safe because it was not persisted — true, and beside the point: a
 * handle this page mints is not the one the buyer service's vault issued, does not rotate
 * when the vault rotates it, cannot be retired by signing out, and resolves back to nobody.
 * R5 names the vault's pseudonym specifically, so the browser generating a lookalike was the
 * feature being faked rather than a lighter way of doing it.
 *
 * `Journey` now redeems the token out of the mailed link and forwards the pseudonym and
 * buckets `GET /buyer/profile` answers with. Restoring a client-side minter would put the
 * fake back; `PSEUDONYM_PREFIX` above stays because it documents the shape a *served*
 * pseudonym has, and nothing in this app builds one.
 */

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
  /**
   * `undefined` when the row carried no readable score — NOT `0`.
   *
   * A defaulted zero would print on the page as "rank_score 0", which reads as the exchange
   * having scored this candidate at the bottom rather than as this client not having found a
   * number. The two are different claims and only one of them is the service's.
   */
  readonly rank_score?: number
  /**
   * The published components. Numbers only, which loses nothing in practice and is stated
   * here rather than assumed: `apps/exchange/src/auction/routes.py::_ranked_out` builds this
   * map as `{str(k): float(v) for k, v in ...}`, so every value the exchange publishes is
   * already a float. A value that is not a finite number therefore did not come from the
   * exchange, and is not shown.
   */
  readonly components: Readonly<Record<string, number>>
}

/**
 * Whether the exchange still holds this auction's shortlist.
 *
 * `'live'` and `'forgotten'` are two different facts and this module refuses to collapse
 * them, because the page built on it would then tell a buyer the wrong one:
 *
 * * `'live'` — the service answered with a shortlist object. It may carry zero slots, and
 *   zero slots is a real answer about the MARKET: nothing was eligible.
 * * `'forgotten'` — the service answered `shortlist: null`. That is an answer about the
 *   EXCHANGE, not the market: `buyer_svc/auctions/routes.py` writes
 *   `shortlist=dict(shortlist) if shortlist is not None else None`, and the exchange returns
 *   nothing once its 15-minute TTL has taken the auction away. The recorded diagnostics are
 *   still there; the live shortlist is not.
 *
 * A body carrying no `shortlist` key at all is neither: that is a malformed answer and it
 * throws. An ABSENT field and an explicit `null` are not the same claim, and reading a
 * missing key as "the exchange forgot it" would invent a reason the service never gave.
 */
export type ShortlistLiveness = 'live' | 'forgotten'

/** What `GET /buyer/auctions/{auction_id}` answers. */
export interface AuctionRecord {
  readonly auction_id: string
  /**
   * The live shortlist EXACTLY as the service sent it, forwarded to `/render` untouched.
   * See this module's docstring for why it is not re-typed on the way through. `null` when
   * `liveness` is `'forgotten'` — there is no shortlist to forward, and an empty one is not
   * a stand-in for a missing one.
   */
  readonly shortlist: unknown
  /** Whether the shortlist above is a live answer or the absence of one. */
  readonly liveness: ShortlistLiveness
  /**
   * How many slots the LIVE shortlist carried, counted BEFORE `/render` labelled them, and
   * `0` when there is no shortlist at all.
   *
   * It exists to keep two failures from wearing each other's clothes. The page decides
   * whether to show "no store was eligible" from how many slots it ended up with, and that
   * number is `/render`'s output, not the exchange's. If the labelling step ever returned
   * fewer rows than it was given, a labelling failure would render as a verdict about the
   * market. Comparing this count with the rendered one is what lets the page tell a buyer
   * which of the two actually happened.
   */
  readonly shortlist_slot_count: number
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
  /** Absent when no finite score arrived — never a defaulted `0`. See `readRenderedSlots`. */
  readonly fit_score?: number
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

/**
 * What the service answered with when it refused. Kept so the buyer sees its words.
 *
 * There is no `path` field. The reused modules already throw messages that name the
 * operation — `"accept failed: HTTP 503"`, `"load auction failed: HTTP 404"` — and `explain`
 * joins the detail onto that, so a recorded path was a field nothing on the page ever read.
 */
export interface FailureRecord {
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

/**
 * The finite number `value` is, or `undefined`.
 *
 * There is deliberately NO `asFiniteNumber(value, fallback)` beside this. One used to sit
 * here, and every field that reached for it — `rank_score`, then `fit_score` — had to name
 * a fallback it had no authority to name, so `0` went onto the page as though the service
 * had sent it. A reader with no honest default must be able to say "nothing arrived".
 */
function finiteNumberOrUndefined(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
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
      seen = { status: response.status, detail: detailFromBody(body) }
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
      rank_score:
        typeof row.rank_score === 'number' && Number.isFinite(row.rank_score)
          ? row.rank_score
          : undefined,
      components: asNumberMap(row.components),
    }))
}

/**
 * The store id inside a bid ref, or `undefined` when this ref is not one this auction minted.
 *
 * The exchange mints the ref itself, as `f"{auction_id}:{store_id}"` — measured in
 * `apps/exchange/src/ranking/candidates.py::mint_bid_id`, which is the only place a bid ref
 * is ever built. So the store id is exactly what follows this auction's id and its colon.
 *
 * Matched as a PREFIX against the auction id the page is showing rather than split on the
 * first `:`: nothing forbids a colon inside an auction id, so a split is a guess where a
 * prefix match is at least a check. Not a proof, and this says so rather than overstating
 * it — `AuctionView.auction_id` is set from the request's own path parameter, so the prefix
 * is an echo of what this page asked for, not something read back off the exchange. What it
 * does buy is real: a ref carrying some OTHER auction's id fails the match and answers
 * `undefined`, and the page then prints the bid ref rather than naming the wrong store.
 *
 * It is used to NAME a store, and no longer to look one up: the price join that used to
 * consume it is gone (see this module's docstring, point 4). Nothing downstream of this
 * attributes a number to the store it returns.
 */
export function storeIdFromBidRef(bidRef: unknown, auctionId: string): string | undefined {
  if (!isNonEmptyString(bidRef) || !isNonEmptyString(auctionId)) return undefined
  const prefix = `${auctionId}:`
  if (!bidRef.startsWith(prefix)) return undefined
  const storeId = bidRef.slice(prefix.length)
  return storeId === '' ? undefined : storeId
}

/** The published ranking row for one bid ref, or `undefined` when the ranking has none. */
export function rankedForSlot(
  record: Pick<AuctionRecord, 'ranked'>,
  bidRef: unknown,
): RankedBid | undefined {
  if (!isNonEmptyString(bidRef)) return undefined
  return record.ranked.find((row) => row.bid_ref === bidRef)
}

/**
 * One ranked row's components, spelled with the keys and numbers the exchange published.
 *
 * The five keys are the exchange's own (`contracts.ranking.RANK_FEATURES`) and are never
 * spelled here, so a build that publishes a sixth term shows a sixth term rather than
 * swallowing it. What this cannot show is a component whose value is not a finite number —
 * `RankedBid.components` is a number map — and that is safe rather than lossy for the reason
 * given on that field: the exchange floats every value before it publishes it.
 */
export function describeComponents(components: Readonly<Record<string, number>>): string {
  const entries = Object.entries(components)
  if (entries.length === 0) return 'the exchange published no components'
  return entries.map(([key, value]) => `${key}=${value}`).join(' ')
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
 * A non-OK status is thrown as an `HttpFailure` naming it. A 200 whose body has no
 * `shortlist` KEY is thrown too: a screen that quietly rendered "no options" for a malformed
 * answer would be telling the buyer something about the market that it does not know.
 *
 * An explicit `shortlist: null` is NOT malformed and does not throw. It is the service
 * saying the exchange no longer holds this auction while the recorded diagnostics survive —
 * see `ShortlistLiveness`. Three outcomes, kept apart: a shortlist, no shortlist, and a body
 * this screen cannot read.
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
  // `null` only — never `undefined`. That single comparison is the whole discriminator: a
  // body with no `shortlist` key reads `undefined`, and `JSON.parse` output inherits from
  // `Object.prototype`, which has no `shortlist` of its own for the read to find. An earlier
  // version also tested `'shortlist' in payload`, which can never change the answer.
  const forgotten = shortlist === null
  if (!forgotten && (!isRecord(shortlist) || !Array.isArray(shortlist.slots))) {
    throw new MalformedResponseError(
      'load auction',
      'it carried no shortlist with a slots array',
    )
  }
  return {
    auction_id: isNonEmptyString(payload.auction_id) ? payload.auction_id : wanted,
    shortlist,
    liveness: forgotten ? 'forgotten' : 'live',
    shortlist_slot_count:
      !forgotten && isRecord(shortlist) && Array.isArray(shortlist.slots)
        ? shortlist.slots.length
        : 0,
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
 * R2's PRODUCT off one served slot, or `null`.
 *
 * `null` for both of the service's absences and for a body this client cannot read as a
 * product — they are one thing on a screen ("the exchange named no product"), and inventing
 * a third state for "malformed" would put a distinction on the page that a shopper has no
 * use for. What is NOT collapsed is a missing `product_ref`: an object with no ref names no
 * product, so it is an absence rather than a product with a blank name.
 */
function readProduct(value: unknown): ShortlistProduct | null {
  if (!isRecord(value)) return null
  const productRef = asString(value.product_ref).trim()
  if (!productRef) return null
  const variantRef = asString(value.variant_ref).trim()
  return { product_ref: productRef, variant_ref: variantRef === '' ? null : variantRef }
}

/** The discount off a served price, or `null`. A stated depth (D22), never an entitlement. */
function readDiscount(value: unknown): SlotDiscount | null {
  if (!isRecord(value)) return null
  const type = asString(value.type).trim()
  const amount = finiteNumberOrUndefined(value.value)
  if (!type || amount === undefined) return null
  return { type, value: amount }
}

/**
 * R2's PRICE off one served slot, or `null`.
 *
 * BOTH prices or neither, and each read as a finite number — the same rule the service
 * applies, kept here too because this is the last reader before a number reaches a shopper's
 * eyes and `undefined` rendered into a price line is the failure this whole file exists to
 * avoid. A half-price is `null`: it is not a cheaper offer, it is an unreadable one.
 */
function readPrice(value: unknown): ShortlistPrice | null {
  if (!isRecord(value)) return null
  const unitPrice = finiteNumberOrUndefined(value.unit_price)
  const totalPrice = finiteNumberOrUndefined(value.total_price)
  if (unitPrice === undefined || totalPrice === undefined) return null
  const currency = asString(value.currency).trim()
  const expiresAt = asString(value.expires_at).trim()
  return {
    unit_price: unitPrice,
    total_price: totalPrice,
    currency: currency === '' ? null : currency,
    discount: readDiscount(value.discount),
    expires_at: expiresAt === '' ? null : expiresAt,
  }
}

/**
 * R2's COMMITMENTS off one served slot, or `null`.
 *
 * `null` and `[]` are kept apart all the way from the exchange to here: `null` is "the
 * exchange sent none", `[]` would be "this store committed to nothing", and a fallback bid
 * is the first of those. So a body that carried no array reads as `null`, while an array
 * that carried only unreadable rows reads as `[]` — this client saw commitments and could
 * not read them, which is not the same as the store having made none.
 *
 * A row with no `key` is dropped: there is nothing in it a shopper could read. A row with no
 * `label` is NOT dropped — it is shown as `unverified`, because a promise whose evidence
 * this client cannot name is still a promise the store made, and the honest thing is to show
 * it as unchecked rather than to hide it.
 */
function readCommitments(value: unknown): readonly SlotCommitment[] | null {
  if (!Array.isArray(value)) return null
  const rows: SlotCommitment[] = []
  for (const row of value) {
    if (!isRecord(row)) continue
    const key = asString(row.key).trim()
    if (!key) continue
    const unit = asString(row.unit).trim()
    const label = asString(row.label).trim()
    rows.push({
      key,
      value: row.value,
      unit: unit === '' ? null : unit,
      label: label === '' ? LABEL_UNVERIFIED : label,
    })
  }
  return rows
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
      // `undefined`, never a defaulted `0` — the same rule `readRanked` applies to
      // `rank_score` twelve lines up. "fit 0" is the exchange ranking this candidate last;
      // "no fit score arrived" is this client failing to read one. `ShortlistSlot.fit_score`
      // is optional so the renderer can tell a reader which of the two happened.
      // NOTE the service manufactures its own: `buyer_svc/accept/labels.py` falls back to
      // `0.0`, so a slot that reaches here already carrying a real zero is indistinguishable
      // from one whose score the service could not read. That half is not this file's to fix.
      fit_score: finiteNumberOrUndefined(row.fit_score),
      provenance_labels: asArray(row.provenance_labels).filter(isNonEmptyString),
      labels_source: asString(row.labels_source),
      trust_summary: asNumberMap(row.trust_summary),
      trust_fields: asUnknownMap(row.trust_summary),
      store_domain: asString(row.store_domain),
      // R2's other three. They come off THIS slot, from the live shortlist the service
      // fetched for this request — not joined in from anywhere else on the page.
      product: readProduct(row.product),
      price: readPrice(row.price),
      commitments: readCommitments(row.commitments),
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
