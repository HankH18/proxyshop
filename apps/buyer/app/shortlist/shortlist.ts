/**
 * Client half of R2's shortlist labels and R3's checkout handoff (T-072).
 *
 * Mirrors `buyer_svc/accept/routes.py`. Three things here are load-bearing rather than
 * decorative, and each is the client-side half of a rule the service also enforces:
 *
 * 1. **This module has no provenance table in it.** D30 puts the source → label map in
 *    `packages/contracts` so the exchange (which produces `provenance_labels`) and the
 *    buyer app (which renders them) cannot drift into two answers. A second copy here
 *    would be exactly that drift, so the strings below are only ever *recognised* — the
 *    three known labels get a tone for styling — and never *derived*. A label the exchange
 *    sent that this build has never heard of is still rendered, verbatim.
 * 2. **Nothing in this file builds a checkout URL.** No template, no `new URL(base, path)`,
 *    no string concatenation with a host in it. The one URL-shaped value it handles is the
 *    `permalink_url` off the service's response. The acceptance fixture ships a decoy
 *    `checkout_url` on the slot for precisely this reason, and `AcceptOutcome` has no field
 *    to carry it.
 * 3. **`followPermalink` re-checks the URL in the browser, where the redirect actually
 *    happens.** The exchange checks before minting and the buyer service checks on the way
 *    through; this is the last check before `location.assign`, and it is the only one
 *    running in the process that will do the navigating. A `javascript:` "permalink" that
 *    passed two server-side checks and failed here would still be a bug — but it would be a
 *    bug that did not execute.
 */

/** SPEC R2 fixes exactly two buyer-facing provenance label strings. */
export const LABEL_STORE_CONFIRMED = 'store-confirmed'
export const LABEL_FROM_THEIR_WEBSITE = 'from their website'

/** Not an R2 provenance label: a seller-asserted claim surfaces R18's verification badge. */
export const LABEL_UNVERIFIED = 'unverified'

/** D29's four slot names — labels for *why this one is here*, never rank-formula terms. */
export const SHORTLIST_SLOT_NAMES = ['fit', 'value', 'reliability', 'specialist'] as const

export type ShortlistSlotName = (typeof SHORTLIST_SLOT_NAMES)[number]

/** How a known label should read. An unknown label is rendered plainly, never hidden. */
export type LabelTone = 'confirmed' | 'observed' | 'unverified' | 'unknown'

/** The trust snapshot summary the exchange attaches to a slot. */
export type TrustSummary = Readonly<Record<string, number>>

/**
 * R2's PRODUCT: WHICH catalogue thing this slot offers. A reference, never a rendered name.
 *
 * The exchange holds the ref because that is what the roster and the offer agree on; a
 * title, an image or a description belongs to a catalogue, and nothing in this app has one.
 * So the ref is what a shopper is shown, and that is stated on the screen rather than
 * papered over with an invented product name.
 */
export interface ShortlistProduct {
  readonly product_ref: string
  /** Absent means the bid named no variant — never "the default variant". */
  readonly variant_ref?: string | null
}

/** The discount a slot's price states. A *stated* depth, not an entitlement: no single-use
 * code exists until the buyer accepts (D22), which is why the screen says "states". */
export interface SlotDiscount {
  readonly type: string
  readonly value: number
}

/**
 * R2's PRICE: what this store is asking, and until when.
 *
 * Both prices are present together or the whole object is `null` — a unit price with no
 * total invites comparing two different quantities as if they were one offer. `currency` is
 * nullable and the screen never invents a symbol for a missing one.
 */
export interface ShortlistPrice {
  readonly unit_price: number
  readonly total_price: number
  readonly currency?: string | null
  readonly discount?: SlotDiscount | null
  readonly expires_at?: string | null
}

/**
 * R2's COMMITMENT: one promise a store makes beside the price, with the provenance label
 * for THAT promise.
 *
 * `label` is the buyer service's addition to the published `Claim` — derived from the
 * claim's own provenance through the one `contracts.labels` table, so it is the same
 * source→label answer the slot's `provenance_labels` come from. It is the buyer's only
 * signal of what has been checked, so it travels beside each promise rather than being
 * flattened into the slot's aggregate row.
 */
export interface SlotCommitment {
  readonly key: string
  readonly value?: unknown
  readonly unit?: string | null
  readonly label: string
}

/** One shortlist slot, as `GET /auctions/{id}/shortlist` sends it. */
export interface ShortlistSlot {
  readonly slot: ShortlistSlotName | string
  readonly bid_ref: string
  /**
   * OPTIONAL, and that is the point. A JSON body is `unknown` at runtime, so a client that
   * types this `number` has to invent one when the field is missing or unreadable — and the
   * value it invents is `0`, which renders as "fit 0": the exchange ranking this candidate
   * last. That is a different claim from "the exchange sent no score", and it is the same
   * manufactured zero that `rank_score` was corrected for. `undefined` here lets a renderer
   * say which of the two happened.
   */
  readonly fit_score?: number
  readonly trust_summary?: TrustSummary
  /** OPEN list of strings, produced by the exchange (D30). Rendered, never re-derived. */
  readonly provenance_labels: readonly string[]
  /** Present only on a slot the caller enriched; the protocol type carries no auction id. */
  readonly auction_id?: string
  readonly store_domain?: string
  /**
   * R2's other three, and all three are OPTIONAL and NULLABLE on purpose.
   *
   * `null` is the exchange saying it has nothing here — a fallback bid promises nothing, a
   * roster row with no readable list price prices nothing — and it is a different answer
   * from `0`, from `''` and from `[]`, each of which reads to a shopper as a claim the
   * store never made. `undefined` is a producer older than the fields. The screen renders
   * both as the same honest sentence, and neither as a number.
   */
  readonly product?: ShortlistProduct | null
  readonly price?: ShortlistPrice | null
  readonly commitments?: readonly SlotCommitment[] | null
}

/** A whole shortlist. It collapses rather than pads: one eligible store means one slot. */
export interface Shortlist {
  readonly auction_id: string
  readonly slots: readonly ShortlistSlot[]
}

/** What `POST /buyer/shortlist/accept` answers. */
export interface AcceptOutcome {
  readonly permalink_url: string
  readonly auction_id: string
  readonly bid_ref: string
  readonly slot: string
  readonly accepted_at: string
}

/** Anything that behaves like `fetch`. Injected so tests need no network. */
export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>

/** Anything that behaves like `location.assign`. Injected so tests navigate nowhere. */
export type Navigator = (url: string) => void

export const ACCEPT_PATH = '/buyer/shortlist/accept'

/** Schemes a checkout may be redirected to. Everything else is refused. */
export const ALLOWED_PERMALINK_SCHEMES = ['https:', 'http:'] as const

/** Thrown when a permalink is not somewhere a browser may be sent. */
export class UnsafePermalinkError extends Error {
  constructor(
    readonly url: string,
    readonly reason: string,
  ) {
    super(`R3: refusing to follow this checkout permalink — ${reason} (url: ${url})`)
    this.name = 'UnsafePermalinkError'
  }
}

/** Thrown when the slot names no auction or no bid, so there is nothing to accept. */
export class MissingAuctionReferenceError extends Error {
  constructor(readonly missing: string) {
    super(
      `R3: this shortlist slot names no ${missing}, so there is nothing for the exchange to ` +
        `accept. An empty auction id is a real value: the confirm route returns one when the ` +
        `exchange created the auction but answered without an id.`,
    )
    this.name = 'MissingAuctionReferenceError'
  }
}

/** Thrown when the service answered the accept without a permalink. */
export class NoPermalinkError extends Error {
  constructor() {
    super(
      'R3: the accept succeeded and carried no permalink. The buyer never mints a checkout ' +
        'URL of its own, so there is nowhere to send them.',
    )
    this.name = 'NoPermalinkError'
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

/**
 * How a label should read. Recognition only — a label this build does not know is
 * `'unknown'` and is still shown to the buyer, because the exchange is the authority on
 * what the labels are (D30) and hiding one would be the renderer overruling it.
 */
export function labelTone(label: string): LabelTone {
  switch (label) {
    case LABEL_STORE_CONFIRMED:
      return 'confirmed'
    case LABEL_FROM_THEIR_WEBSITE:
      return 'observed'
    case LABEL_UNVERIFIED:
      return 'unverified'
    default:
      return 'unknown'
  }
}

/**
 * The labels to render for a slot: exactly what the exchange sent, de-duplicated.
 *
 * A slot the exchange sent no labels for reads as `unverified` rather than as nothing —
 * an empty row would read to a buyer as a slot with nothing to hide, which is the opposite
 * of the truth. Nothing here inspects `Provenance.source`; the buyer app does not own that
 * mapping.
 */
export function slotLabels(slot: Pick<ShortlistSlot, 'provenance_labels'>): readonly string[] {
  const supplied = Array.isArray(slot.provenance_labels) ? slot.provenance_labels : []
  const cleaned = supplied.filter(isNonEmptyString).map((label) => label.trim())
  const unique = [...new Set(cleaned)]
  return unique.length > 0 ? unique : [LABEL_UNVERIFIED]
}

/**
 * Why `url` may not be followed, or `undefined` when it may be.
 *
 * The rule is exact host equality, lower-cased — `in`, `startsWith` and `endsWith` are each
 * defeated by a domain an attacker can register today (`store-x.example.com.evil.tld`,
 * `https://store-x.example.com@evil.tld/`, `checkout.store-x.example.com`). `URL.hostname`
 * is what makes the userinfo shape harmless: it returns the host *after* the `@`, which is
 * the host the browser will actually connect to.
 */
export function permalinkRefusal(url: unknown, expectedDomain = ''): string | undefined {
  if (!isNonEmptyString(url)) return 'there is no permalink here'
  if (url !== url.trim()) return 'it is padded with whitespace, so it cannot be followed unmodified'
  // The WHATWG URL parser STRIPS tab, CR and LF before parsing, so a URL containing them
  // reads as one destination and resolves to another. Refused rather than parsed.
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001F\u007F]/.test(url)) return 'it contains control characters'

  // MEASURED, and the reason this rule exists at all: `new URL('https:///cart/1:1')` does
  // not fail here — the WHATWG parser skips the extra slash and resolves the host to
  // `cart`, giving `https://cart/1:1`. Python's `urlsplit`, which is what the buyer service
  // and the exchange check with, reads the same string as having NO host and refuses it. So
  // this shape is one where the server-side check and the browser-side check disagree about
  // where the buyer is going, and "the two readings differ" is never a condition a redirect
  // should resolve by picking one. A real cart permalink never has an empty authority.
  if (/^[A-Za-z][A-Za-z0-9+.-]*:\/\/\//.test(url)) {
    return 'its authority section is empty, and URL parsers disagree about where that points'
  }

  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    // A protocol-relative `//evil.example/cart` lands here: it has no scheme of its own.
    return 'it is not an absolute URL'
  }
  if (!(ALLOWED_PERMALINK_SCHEMES as readonly string[]).includes(parsed.protocol)) {
    return `its scheme ${parsed.protocol} is not one a checkout can be redirected to`
  }
  if (!parsed.hostname) return 'it carries no host'
  if (expectedDomain.trim()) {
    const expected = expectedDomain.trim().toLowerCase().replace(/\.$/, '')
    const actual = parsed.hostname.toLowerCase().replace(/\.$/, '')
    if (actual !== expected) {
      return `its host ${actual} is not the store domain the buyer was shown (${expected})`
    }
  }
  return undefined
}

/** Return `url` unchanged, or throw. Normalises nothing — R3 says *unmodified*. */
export function assertFollowable(url: unknown, expectedDomain = ''): string {
  const refusal = permalinkRefusal(url, expectedDomain)
  if (refusal !== undefined) throw new UnsafePermalinkError(String(url), refusal)
  return url as string
}

/**
 * Accept one slot. Posts the slot to the buyer service and returns what came back.
 *
 * The slot is checked first, so an accept for an auction nobody can name never leaves the
 * browser. The service checks again; two layers, because the wire layer is the one a future
 * refactor most easily loosens.
 */
export async function acceptSlot(
  slot: ShortlistSlot,
  fetcher: Fetcher,
  options: { readonly auctionId?: string; readonly expectedDomain?: string } = {},
): Promise<AcceptOutcome> {
  const auctionId = (options.auctionId ?? slot.auction_id ?? '').trim()
  if (!auctionId) throw new MissingAuctionReferenceError('auction id')
  if (!isNonEmptyString(slot.bid_ref)) throw new MissingAuctionReferenceError('bid ref')

  const expectedDomain = options.expectedDomain ?? slot.store_domain ?? ''
  const response = await fetcher(ACCEPT_PATH, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      // Named fields, never the whole slot: a slot may carry a `checkout_url` the buyer
      // must not follow, and there is no reason to send it back to a service that would
      // rightly ignore it.
      slot: { slot: slot.slot, bid_ref: slot.bid_ref, auction_id: auctionId },
      expected_domain: expectedDomain === '' ? null : expectedDomain,
    }),
  })
  if (!response.ok) throw new Error(`accept failed: HTTP ${response.status}`)
  const payload = (await response.json()) as unknown
  if (!isRecord(payload) || !isNonEmptyString(payload.permalink_url)) throw new NoPermalinkError()

  assertFollowable(payload.permalink_url, expectedDomain)
  return {
    permalink_url: payload.permalink_url,
    auction_id: isNonEmptyString(payload.auction_id) ? payload.auction_id : auctionId,
    bid_ref: isNonEmptyString(payload.bid_ref) ? payload.bid_ref : slot.bid_ref,
    slot: isNonEmptyString(payload.slot) ? payload.slot : String(slot.slot),
    accepted_at: isNonEmptyString(payload.accepted_at) ? payload.accepted_at : '',
  }
}

/**
 * Send the buyer to the exchange's permalink. The last check before the navigation.
 *
 * `navigate` is injected so a test can assert on where the buyer *would* have gone; the
 * default is the browser's own `location.assign`, and outside a browser there is nothing to
 * navigate, so it refuses rather than pretending.
 */
export function followPermalink(
  outcome: AcceptOutcome,
  expectedDomain = '',
  navigate?: Navigator,
): string {
  const url = assertFollowable(outcome.permalink_url, expectedDomain)
  const go =
    navigate ??
    (typeof window !== 'undefined' ? (target: string) => window.location.assign(target) : undefined)
  if (!go) throw new Error('there is no browser here to follow the permalink with')
  go(url)
  return url
}
