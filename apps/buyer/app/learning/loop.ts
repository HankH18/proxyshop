/**
 * The three beats of the learning demo, as requests rather than as a story.
 *
 * This module holds everything the page does that is not React: which routes it calls, in
 * what order, what it refuses to send, and how two shortlists are compared. It is separated
 * from `LearningPage.tsx` for the reason `telemetry.ts` is separated from `MetricsPage.tsx` —
 * the interesting claims here are about the WIRE, and a claim about the wire should be
 * testable without rendering anything.
 *
 * ---------------------------------------------------------------------------------------
 * THE ONE PROMISE. The second query is a real auction whose result differs because the
 * network's state differs. Nothing on this page stores a "before" and re-renders it as an
 * "after": both readings come from `GET /buyer/auctions/{id}` on two auctions the exchange
 * actually ran, minutes apart, with a real solicitation of real store agents in between.
 * `compare` is given two readings and cannot tell where they came from; it is arithmetic, and
 * arithmetic is all it is allowed to be.
 *
 * ---------------------------------------------------------------------------------------
 * WHAT REACHES WHAT, MEASURED ON THE RUNNING COMPOSE STACK RATHER THAN READ OFF A DIAGRAM.
 * Every route below is on the buyer origin, because that is the only origin this page has:
 * `deploy/buyer-web/nginx.conf` proxies exactly one prefix, `/buyer/`, and no service in this
 * stack installs CORS, so the trust ledger on :8084 and the exchange on :8083 are not merely
 * inconvenient from here — they are unreachable before the request is sent. The metrics page
 * measured that and wrote it down (`metrics/telemetry.ts`); this page does not re-litigate it.
 *
 * So the outcomes are fed through the two buyer-origin doors that really do reach the rest of
 * the network, and each one moves a different learner:
 *
 *   * `POST /buyer/shortlist/accept` — a PURCHASE. The buyer service asks the exchange to
 *     mint a checkout, and the exchange folds the result into its bandit on the way past:
 *     `apps/exchange/src/accept/routes.py` records a win for the accepted store and a loss
 *     for every other store the same shortlist showed. Measured: a 200 with a real
 *     `permalink_url` pinned to the store's registered domain and a discount code.
 *
 *   * `POST /buyer/feedback` — CUSTOMER FEEDBACK. The buyer service seals it as a `feedback`
 *     event in the trust ledger's hash chain, and the trust service then pushes the resulting
 *     trust delta to the affected store's agent over `POST /v1/trust-events`
 *     (`apps/trust/src/feedback/notify.py`, addressed per store, never broadcast). The agent
 *     credits the arm it played in that auction — pitch variant x commitment set x discount
 *     depth — and plays a different one next time. Measured on the running stack: trust's own
 *     `GET /events/verify` reports `store_agent_notifications.delivered` climbing and
 *     `lost: 0`, and the gaiaherbs agent logs `POST /v1/trust-events 200 OK`.
 *
 * ---------------------------------------------------------------------------------------
 * WHY THE OUTCOME'S SIGN FOLLOWS THE OFFER, which is the only modelling choice in this file.
 * A review has to say something, and inventing sentiment at random would teach the seller
 * agents noise. {@link outcomeIsPositive} says a shopper was satisfied when the shop gave them
 * a better deal than its list price, and dissatisfied when it did not. That is a coherent
 * synthetic buyer rather than a flattering one — it is the same shape as the checked-in seed
 * corpus, whose answers are derived from the measured verdict of the order rather than drawn
 * out of a hat (`services/sim/seed/population.py`) — and it teaches the sellers something
 * true about this market: in a shortlist where THREE of the five ranking terms are identical
 * across every candidate, price is what converts on the seller's side of the loop.
 *
 * Three, not four, and the difference is the whole reason this sentence is load-bearing rather
 * than decorative. `intent_match`, `verified_claim_ratio` and `delivery_fit` are the identical
 * three — every candidate scores the published neutral 0.5 on each. The other two BOTH move:
 * `price_value` is the seller's, and `trust` is the SHOPPER's, because
 * `deploy/demo/exchange-deployment.json` states no `trust_snapshot` key and the ranking gate
 * therefore reads the live trust service. Measured on the compose stack on 2026-09-09, cold,
 * auction `auction-958b7fc3-e433-4499-95f0-4d3a6cba1680`: every published `rank_score`
 * reconstructs exactly as `0.35*0.5 + 0.20*0.5 + 0.20*trust + 0.15*price_value + 0.10*0.5`, and
 * with all four bidders at list price (`price_value` 0.0 for every one) `trust` was the ONLY
 * thing separating them.
 *
 * So `outcomeIsPositive` is still the right rule — a discount is the only thing about the OFFER
 * that varies, so it is the only honest thing to condition a synthetic shopper's satisfaction
 * on — but it is not true that price is the only thing that moves the board. Trust moves it
 * too, and this page's feedback is what moves trust.
 *
 * It is also the honest thing to SAY, which is why the page says it: these outcomes are
 * manufactured, they are marked as manufactured, and this sentence is the rule that made
 * them.
 *
 * ---------------------------------------------------------------------------------------
 * WHAT MARKS THEM, AND WHAT THE MARK DOES NOT COVER. Every order this page invents carries an
 * `order_ref` beginning with the prefix `apps/buyer/app/feedback/seed-data/collection.json`
 * declares about itself — `sim-fb-`, read out of the artifact by
 * `journey/seeded-feedback.ts` rather than spelled here, so this producer and the corpus
 * cannot drift. `apps/buyer/svc/src/feedback/submission.py` copies `order_ref` verbatim onto
 * the sealed ledger event beside `event_hash` and `prev_hash`, so the mark is INSIDE the hash
 * chain: an event that landed marked cannot be un-marked, and an earned one cannot be marked,
 * without breaking `GET /events/verify`.
 *
 * What that buys is tamper-evidence, not membership. Nothing on the served route knows the
 * string `sim-fb-` exists — `apps/buyer/svc/src/feedback/routes.py` has no notion of a
 * simulator, deliberately — so anybody can mint a fresh `sim-fb-` reference today. The
 * enforcement is on the PRODUCER, here in {@link seededOrderRef} and {@link assertSeeded},
 * exactly as `seed.population._Services.submit` refuses to send an unmarked order before the
 * first byte leaves. This module states that limit rather than implying a guarantee it does
 * not have, and {@link MARKER_LIMIT} is the sentence the page prints.
 */
import {
  SEEDED_PREFIX,
  SEEDED_REFUSAL,
} from '../journey/seeded-feedback'
import { type Fetcher, readEntries, readRanked } from '../journey/wire'

/** `POST` — turns the shopper's words into a structured intent. Unauthenticated. */
export const CLARIFY_PATH = '/buyer/intent/clarify'

/**
 * `POST` — the only way a browser opens an auction. There is no `POST /buyer/auctions`; the
 * exchange's `POST /auctions` is on a different origin and cannot be reached from this page.
 */
export const CONFIRM_PATH = '/buyer/intent/confirm'

/** `GET {this}{auction_id}` — the live shortlist plus the recorded ranking diagnostics. */
export const AUCTION_PATH_PREFIX = '/buyer/auctions/'

/** `POST` — mints a real checkout, and feeds the exchange's bandit on the way past. */
export const ACCEPT_PATH = '/buyer/shortlist/accept'

/** `POST` — asks R14 whether this order may be asked for feedback at all. */
export const PROMPT_PATH = '/buyer/feedback/prompt'

/** `POST` — seals one answered prompt into the trust ledger. */
export const FEEDBACK_PATH = '/buyer/feedback'

/**
 * The bid window this page asks for, in seconds.
 *
 * THE EXCHANGE'S CEILING, and it used to be six times that. This asked for 60, which
 * `exchange.auction.routes.MAX_BID_TIMEOUT_SECONDS` silently clamps to 10 — measured through
 * the served route, which now publishes what it granted:
 *
 *     asked= 60s  granted=10.0  hosted_bids=4  excluded=0
 *     asked= 10s  granted=10.0  hosted_bids=4  excluded=0
 *     asked=  3s  granted= 3.0  hosted_bids=4  excluded=0
 *
 * Asking for a number the server will never grant is a request whose stated intent and actual
 * effect differ with nothing saying so, which is the shape this page exists to argue against.
 *
 * The reason recorded here was also out of date, and the third row above is what retired it.
 * It said the agents' model latency dropped bidders at a short window — "Measured at 3s: three
 * of four bidders excluded" — and that was true when the store agent composed its offer AFTER
 * its model answered. It no longer does: the offer is composed deterministically and only the
 * model-written pitch spends what is left of the exchange's deadline, so a 3-second window now
 * drops nobody. What the ceiling still buys is head-room between the shortlist being read and
 * the checkout being minted, which is the accept-window half of the paragraph this replaces.
 */
export const BID_WINDOW_SECONDS = 10

/**
 * The second turn of the clarify dialogue.
 *
 * `POST /buyer/intent/clarify` answers the first turn with questions and leaves the intent
 * `unresolved`; a second turn that declines the questions is what settles it. Sent as the
 * shopper's own words rather than a flag because the route takes utterances, not a form.
 */
export const NO_FURTHER_CONSTRAINTS = 'nothing else, no must-haves'

/**
 * What the marker does NOT cover, printed on the page verbatim.
 *
 * Copied in shape from `db/migrations/0005_buyer_accounts_provenance.sql`, whose own
 * `what_it_does_not_cover` is the precedent for stating a provenance mechanism's limit beside
 * the mechanism instead of letting a reader assume the stronger claim.
 */
export const MARKER_LIMIT =
  'The mark is tamper-evident, not exclusive. Because the order reference is copied verbatim ' +
  'onto the sealed ledger event, an event that landed marked can never be un-marked and an ' +
  'earned one can never be marked, without breaking GET /events/verify. What it cannot tell ' +
  'you is that nobody ELSE minted a row under the same prefix: the served route has no notion ' +
  'that a simulator exists, and the refusal that keeps this page honest lives in this page.'

/** One candidate, as the published ranking and the recorded entries jointly describe it. */
export interface Candidate {
  readonly store_id: string
  /** The published `rank_score`, or `undefined` when the exchange published none. */
  readonly rank_score: number | undefined
  /** The weighted terms the exchange published. Never re-spelled — a sixth term shows. */
  readonly components: Readonly<Record<string, number>>
  /** The offer's unit price, from `entries[]`. `undefined` when the row carries none. */
  readonly unit_price: number | undefined
  /** True when this row is ProxyShop's own fallback listing rather than the shop's bid. */
  readonly fallback: boolean
  /** Why it is a fallback. `null` when it is not one. */
  readonly fallback_reason: string | null
}

/** One reading of the market: one auction, its ranking, and when it was recorded. */
export interface Reading {
  readonly auction_id: string
  readonly recorded_at: string | null
  /** In the exchange's published order, best first. */
  readonly candidates: readonly Candidate[]
}

/** What happened to one store between two readings. */
export interface Movement {
  readonly store_id: string
  /** 0-based position in the published order, or `null` when the reading had no such row. */
  readonly position_before: number | null
  readonly position_after: number | null
  readonly rank_before: number | undefined
  readonly rank_after: number | undefined
  /** `after - before`, or `undefined` when either side published no score. */
  readonly rank_delta: number | undefined
  readonly price_before: number | undefined
  readonly price_after: number | undefined
  /** Every component key either reading published, with both values and the difference. */
  readonly components: readonly ComponentMovement[]
}

/** One ranking term, before and after. */
export interface ComponentMovement {
  readonly key: string
  readonly before: number | undefined
  readonly after: number | undefined
  readonly delta: number | undefined
  /** True when both sides published a value and they are equal. Drives "held" on the page. */
  readonly held: boolean
}

/** One request the demo made, kept so the page can show its own work. */
export interface LogLine {
  readonly at: string
  readonly what: string
  readonly detail: string
  readonly ok: boolean
}

/** Anything that behaves like `fetch`. Re-exported so the page has one import for the wire. */
export type { Fetcher }

/**
 * A refusal this module raises rather than sending something it should not.
 *
 * Named for what it protects: `POST /buyer/feedback` writes to an append-only ledger with no
 * delete, so an unmarked synthetic row is not a mistake that can be tidied up afterwards.
 */
export class UnmarkedOutcome extends Error {}

/** A route answered in a way this page cannot use. Carries the service's own words. */
export class WireFailure extends Error {
  constructor(
    readonly where: string,
    readonly status: number,
    detail: string,
  ) {
    super(`${where} answered ${status}: ${detail}`)
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function nonEmpty(value: unknown): value is string {
  return typeof value === 'string' && value.trim() !== ''
}

/**
 * The service's own words out of a failed response, or a short stand-in.
 *
 * FastAPI puts a refusal in `detail`, and `detail` is a string on some routes and an object
 * with `message`/`reason` on others (`buyer_svc.feedback.routes` answers both shapes). Both
 * are unwrapped, because the whole value of showing a refusal is showing the sentence the
 * service wrote rather than a status code the page invented a gloss for.
 */
export function refusalDetail(body: unknown): string {
  if (typeof body === 'string' && body.trim() !== '') return body.trim()
  if (!isRecord(body)) return 'no detail'
  const detail = body.detail
  if (nonEmpty(detail)) return detail.trim()
  if (isRecord(detail)) {
    if (nonEmpty(detail.message)) return detail.message.trim()
    if (nonEmpty(detail.reason)) return detail.reason.trim()
  }
  if (nonEmpty(body.reason)) return body.reason.trim()
  return 'no detail'
}

async function send(
  fetcher: Fetcher,
  path: string,
  body: unknown,
  where: string,
): Promise<unknown> {
  const response = await fetcher(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: JSON.stringify(body),
  })
  const text = await response.text()
  let parsed: unknown = null
  try {
    parsed = text === '' ? null : (JSON.parse(text) as unknown)
  } catch {
    // A body that is not JSON on this origin means nginx's SPA fallback answered — a path
    // outside `/buyer/` is served index.html with a 200. Saying so beats "unexpected token <".
    throw new WireFailure(where, response.status, 'the body was not JSON')
  }
  if (!response.ok) throw new WireFailure(where, response.status, refusalDetail(parsed))
  return parsed
}

async function read(fetcher: Fetcher, path: string, where: string): Promise<unknown> {
  const response = await fetcher(path, { method: 'GET', headers: { accept: 'application/json' } })
  const text = await response.text()
  let parsed: unknown = null
  try {
    parsed = text === '' ? null : (JSON.parse(text) as unknown)
  } catch {
    throw new WireFailure(where, response.status, 'the body was not JSON')
  }
  if (!response.ok) throw new WireFailure(where, response.status, refusalDetail(parsed))
  return parsed
}

/** The path one auction is read at. Exported so a test asserts the spelling, not a guess. */
export function auctionPath(auctionId: string): string {
  return `${AUCTION_PATH_PREFIX}${encodeURIComponent(auctionId.trim())}`
}

/**
 * Run one real auction for `query` and read back what the exchange published.
 *
 * Three requests, and none of them is optional: clarify turns words into an intent, confirm
 * is the only door a browser has onto `POST /auctions`, and the auction read is where the
 * ranking components live — `POST /buyer/intent/confirm` answers with an id and nothing else.
 */
export async function runQuery(
  query: string,
  fetcher: Fetcher,
  bidWindowSeconds: number = BID_WINDOW_SECONDS,
): Promise<Reading> {
  const clarified = await send(
    fetcher,
    CLARIFY_PATH,
    { turns: [query, NO_FURTHER_CONSTRAINTS] },
    'clarify',
  )
  if (!isRecord(clarified) || !isRecord(clarified.intent)) {
    throw new WireFailure('clarify', 200, 'the answer carried no intent')
  }
  const confirmed = await send(
    fetcher,
    CONFIRM_PATH,
    // `confirmed` is the literal boolean. The service types it `StrictBool`, so the string
    // "true" is a 422 rather than a coercion — and a lax field would have opened an auction.
    { intent: clarified.intent, confirmed: true, bid_timeout_seconds: bidWindowSeconds },
    'confirm',
  )
  if (!isRecord(confirmed) || !nonEmpty(confirmed.auction_id)) {
    throw new WireFailure('confirm', 201, 'the answer named no auction')
  }
  return readAuction(confirmed.auction_id, fetcher)
}

/** One auction, read back through the buyer origin and reduced to what this page shows. */
export async function readAuction(auctionId: string, fetcher: Fetcher): Promise<Reading> {
  const body = await read(fetcher, auctionPath(auctionId), 'read auction')
  if (!isRecord(body)) throw new WireFailure('read auction', 200, 'the body was not an object')
  // An explicit `shortlist: null` is the exchange saying it no longer holds this auction; the
  // recorded halves below survive it, and they are what this page compares. A MISSING key is
  // a different thing and `wire.ts` throws on it — here the recorded halves are read directly
  // and their absence shows as a reading with no candidates, which the page states as such.
  const ranked = readRanked(body.ranked)
  const entries = readEntries(body.entries)
  const priced = new Map(entries.map((entry) => [entry.store_id, entry]))
  return {
    auction_id: nonEmpty(body.auction_id) ? body.auction_id : auctionId,
    recorded_at: nonEmpty(body.recorded_at) ? body.recorded_at : null,
    candidates: ranked.map((row) => {
      const entry = priced.get(row.store_id)
      return {
        store_id: row.store_id,
        rank_score: row.rank_score,
        components: row.components,
        unit_price: entry?.unit_price,
        fallback: entry?.fallback ?? false,
        fallback_reason: entry?.fallback_reason ?? null,
      }
    }),
  }
}

/**
 * The discount percentage this shop offered in this auction, or `null` when it did not bid.
 *
 * Read off the LIVE shortlist rather than off `entries[]`, because `entries[]` carries the
 * price and not the discount, and the difference between "priced at 24.22" and "took 5% off
 * its own list price" is the thing the outcome's sign is about.
 */
export function discountOffered(shortlist: unknown, storeId: string): number | null {
  if (!isRecord(shortlist)) return null
  const slots = shortlist.slots
  if (!Array.isArray(slots)) return null
  for (const slot of slots) {
    if (!isRecord(slot)) continue
    if (!nonEmpty(slot.bid_ref) || !slot.bid_ref.endsWith(`:${storeId}`)) continue
    const price = slot.price
    if (!isRecord(price)) return 0
    const discount = price.discount
    if (!isRecord(discount)) return 0
    const value = discount.value
    return typeof value === 'number' && Number.isFinite(value) ? value : 0
  }
  return null
}

/**
 * Was this shopper satisfied? The single modelling choice in the demo, stated as a function.
 *
 * See this module's docstring: the sign follows the offer, so the sellers are taught something
 * true about a market whose three inert ranking terms are identical across every candidate and
 * whose only other moving term, `trust`, is not the seller's to set.
 * A shop that gave a deal converted a happy buyer; a shop that held its list price did not.
 */
export function outcomeIsPositive(discountPercent: number): boolean {
  return discountPercent > 0
}

/**
 * The `order_ref` for one manufactured order, marked as manufactured.
 *
 * The prefix is READ from the seed artifact (`SEEDED_PREFIX`) and never spelled here, so a
 * corpus that ever declares a different marker moves this producer with it. The rest of the
 * reference is shaped to survive `REFERENCE_PATTERN` on the served route
 * (`^[A-Za-z0-9_.:#/-]{1,200}$`): no spaces, no `@`, so it can be neither a sentence nor an
 * email address.
 */
export function seededOrderRef(round: number, index: number, storeId: string): string {
  return `${SEEDED_PREFIX}demo-${String(round).padStart(2, '0')}-${String(index).padStart(2, '0')}-${storeId}`
}

/**
 * Refuse an order reference that is not marked, before the first byte leaves.
 *
 * The same guard, for the same reason, as `seed.population._Services.submit`: synthetic
 * sentiment reaches an append-only ledger with no delete; it is labelled or it is not sent.
 * It also refuses when the artifact itself failed its own marker check — `SEEDED_REFUSAL`
 * being set means the corpus this prefix was read out of is not self-consistent, and a prefix
 * read out of a broken artifact is not a mark.
 */
export function assertSeeded(orderRef: string): void {
  if (SEEDED_REFUSAL !== undefined) {
    throw new UnmarkedOutcome(
      `refusing to submit manufactured outcomes: ${SEEDED_REFUSAL}`,
    )
  }
  if (!orderRef.startsWith(SEEDED_PREFIX)) {
    throw new UnmarkedOutcome(
      `refusing to submit ${orderRef}: it does not carry the '${SEEDED_PREFIX}' prefix the ` +
        'seed artifact declares. Synthetic sentiment reaches an append-only ledger with no ' +
        'delete; it is labelled or it is not sent.',
    )
  }
}

/**
 * The order record one manufactured purchase is reported under.
 *
 * `routed: true` is a claim, and it is one this page is entitled to make ONLY because the
 * round that produced it really did go through `POST /buyer/shortlist/accept` and get a
 * checkout back: the network routed this order. R14's gate exists so that a store cannot move
 * its own trust score with reviews of orders it was never given, and the honest way to pass a
 * gate is to satisfy it. The seeded corpus makes the same claim in the same place
 * (`services/sim/seed/population.py`) after the same real accept.
 */
export interface SeededOrder {
  readonly order_ref: string
  readonly store_id: string
  readonly auction_id: string
  readonly routed: true
  readonly status: 'delivered'
  readonly buyer_pseudonym: string
}

/** Build one, marked, or raise. */
export function seededOrder(
  round: number,
  index: number,
  storeId: string,
  auctionId: string,
): SeededOrder {
  const orderRef = seededOrderRef(round, index, storeId)
  assertSeeded(orderRef)
  return {
    order_ref: orderRef,
    store_id: storeId,
    auction_id: auctionId,
    routed: true,
    status: 'delivered',
    // Marked in the same breath as the order, with the simulator's own buyer prefix, so a
    // reader of the ledger sees a manufactured shopper as well as a manufactured order.
    buyer_pseudonym: `sim-buyer-${String(round).padStart(3, '0')}${String(index).padStart(3, '0')}`,
  }
}

/**
 * Ask R14 whether this order may be asked, then answer it. One sealed ledger event, or a
 * refusal in the service's own words.
 *
 * The prompt call is not ceremony: it is where the gate actually runs, and it is what makes
 * the answer's `question_id` and `choice` the service's vocabulary rather than this page's.
 * A `choice` invented here would be a 422 — the options come from the prompt.
 */
export async function submitOutcome(
  order: SeededOrder,
  positive: boolean,
  fetcher: Fetcher,
): Promise<string> {
  assertSeeded(order.order_ref)
  const offered = await send(fetcher, PROMPT_PATH, { order }, 'feedback prompt')
  if (!isRecord(offered) || offered.offered !== true || !isRecord(offered.prompt)) {
    const why = isRecord(offered) && nonEmpty(offered.reason) ? offered.reason : 'no reason given'
    throw new WireFailure('feedback prompt', 200, why)
  }
  const prompt = offered.prompt
  const options = Array.isArray(prompt.options) ? prompt.options : []
  // The chosen option is the first the SERVICE published whose `matched_pitch` has the sign
  // this outcome carries. Not a string typed here: the option ids belong to
  // `buyer_svc.feedback.prompt` and a page that spelled one would drift the moment it changed.
  const chosen = options.find(
    (option) => isRecord(option) && option.matched_pitch === positive && nonEmpty(option.id),
  )
  if (!isRecord(chosen) || !nonEmpty(chosen.id)) {
    throw new WireFailure(
      'feedback prompt',
      200,
      `the prompt published no option whose matched_pitch is ${String(positive)}`,
    )
  }
  const recorded = await send(
    fetcher,
    FEEDBACK_PATH,
    { order, response: { question_id: prompt.question_id, choice: chosen.id } },
    'feedback',
  )
  if (!isRecord(recorded) || !nonEmpty(recorded.event_id)) {
    throw new WireFailure('feedback', 201, 'the answer named no ledger event')
  }
  return recorded.event_id
}

/**
 * Buy the top-ranked bid of one auction, for real.
 *
 * Returns the checkout permalink the exchange minted. The slot is taken from the LIVE
 * shortlist and given the auction id the page already holds: `POST /buyer/shortlist/accept`
 * refuses a slot that names no auction, and the live shortlist's slots do not carry one —
 * `POST /buyer/shortlist/render` is what normally adds it, and this page does not render,
 * because it is not showing the shopper a product card.
 */
export async function buyTopSlot(
  shortlist: unknown,
  auctionId: string,
  fetcher: Fetcher,
): Promise<{ store_id: string; permalink_url: string }> {
  if (!isRecord(shortlist) || !Array.isArray(shortlist.slots) || shortlist.slots.length === 0) {
    throw new WireFailure('accept', 0, 'the auction published no shortlist slot to accept')
  }
  const first = shortlist.slots[0]
  if (!isRecord(first) || !nonEmpty(first.bid_ref)) {
    throw new WireFailure('accept', 0, 'the first shortlist slot carries no bid reference')
  }
  const accepted = await send(
    fetcher,
    ACCEPT_PATH,
    { slot: { ...first, auction_id: auctionId } },
    'accept',
  )
  if (!isRecord(accepted) || !nonEmpty(accepted.permalink_url)) {
    throw new WireFailure('accept', 200, 'the answer carried no checkout permalink')
  }
  return {
    store_id: first.bid_ref.slice(first.bid_ref.lastIndexOf(':') + 1),
    permalink_url: accepted.permalink_url,
  }
}

/**
 * Which shops actually bid in this auction, in published order.
 *
 * A fallback row is ProxyShop's own listing standing in for a shop that did not answer (R10),
 * and there is no seller agent behind it to teach — no address in trust's book, no arm to
 * credit. Feeding an outcome for one would be posting sentiment about a shop that was never
 * in the conversation, so this filter is a correctness rule and not a tidiness one.
 */
export function biddingStores(reading: Reading): readonly string[] {
  return reading.candidates.filter((row) => !row.fallback).map((row) => row.store_id)
}

/**
 * Two readings, term by term.
 *
 * Union of the store ids in both, ordered by the AFTER reading's published order so the page
 * reads top-down as the new shortlist; a store that was in the before and is not in the after
 * comes last, which is what "dropped out" should look like.
 */
export function compare(before: Reading, after: Reading): readonly Movement[] {
  const beforeAt = new Map(before.candidates.map((row, index) => [row.store_id, { row, index }]))
  const afterAt = new Map(after.candidates.map((row, index) => [row.store_id, { row, index }]))
  const ordered = [
    ...after.candidates.map((row) => row.store_id),
    ...before.candidates.map((row) => row.store_id).filter((id) => !afterAt.has(id)),
  ]
  const seen = new Set<string>()
  const movements: Movement[] = []
  for (const store_id of ordered) {
    if (seen.has(store_id)) continue
    seen.add(store_id)
    const was = beforeAt.get(store_id)
    const now = afterAt.get(store_id)
    const keys = new Set([
      ...Object.keys(was?.row.components ?? {}),
      ...Object.keys(now?.row.components ?? {}),
    ])
    movements.push({
      store_id,
      position_before: was?.index ?? null,
      position_after: now?.index ?? null,
      rank_before: was?.row.rank_score,
      rank_after: now?.row.rank_score,
      rank_delta: difference(was?.row.rank_score, now?.row.rank_score),
      price_before: was?.row.unit_price,
      price_after: now?.row.unit_price,
      components: [...keys].sort().map((key) => {
        const b = was?.row.components[key]
        const a = now?.row.components[key]
        return {
          key,
          before: b,
          after: a,
          delta: difference(b, a),
          held: b !== undefined && a !== undefined && b === a,
        }
      }),
    })
  }
  return movements
}

function difference(before: number | undefined, after: number | undefined): number | undefined {
  if (before === undefined || after === undefined) return undefined
  return after - before
}

/** True when anything at all moved. Drives the page's honest "nothing changed" branch. */
export function anythingMoved(movements: readonly Movement[]): boolean {
  return movements.some(
    (row) =>
      row.position_before !== row.position_after ||
      (row.rank_delta !== undefined && row.rank_delta !== 0) ||
      row.components.some((term) => term.delta !== undefined && term.delta !== 0),
  )
}

/** A number the page prints, or the em dash it prints when there is not one. */
export function figure(value: number | undefined, places = 4): string {
  return value === undefined ? '—' : value.toFixed(places)
}

/** A signed delta, with its sign always shown so a rise and a fall never look alike. */
export function signed(value: number | undefined, places = 4): string {
  if (value === undefined) return '—'
  if (value === 0) return '0'
  return `${value > 0 ? '+' : '−'}${Math.abs(value).toFixed(places)}`
}
