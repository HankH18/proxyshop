/**
 * Client half of R2's shortlist labels and R3's checkout handoff (T-072).
 *
 * Mirrors `buyer_svc/accept/routes.py`, and — since the record fold — reads one route it does
 * not own: `GET /buyer/auctions/{id}`, the auction as `buyer_svc/auctions/routes.py` recorded
 * it. That is the only place a shopper-facing surface can reach a promise's provenance and the
 * exchange's published ranking components, because both are projected away twice on the way to
 * a card; `loadRecordedAuction` and its readers are at the bottom of this file, and
 * `ShortlistView`'s own header argues why the fold re-reads rather than re-deriving.
 *
 * Four things here are load-bearing rather than decorative, and each is the client-side half of
 * a rule the service also enforces:
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
 * 4. **The record readers refuse rather than repair.** Everything `readRecordedAuction` walks
 *    is `unknown` at runtime, and its answer for a field it cannot read is the honest absence
 *    the rest of this file uses — never a default that reads as a claim. An `authority_rank`
 *    outside the contract's own `>= 1` bound is `null` rather than printed beside the sentence
 *    "1 is the strongest"; a provenance block with no source is no evidence rather than three
 *    blank rows under the word "Evidence"; and `null` commitments stay apart from `[]`, because
 *    "the exchange published none" and "this shop promised nothing" are different sentences.
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
 * WHAT THE PLATFORM CRAWLED: a product name a person can read, in the PLATFORM's voice (D55).
 *
 * The organic half of the market, published. A title the platform observed in its own crawl
 * is a platform-authored fact about a product exactly as `platform_case` is a
 * platform-authored case for a shop — so this is the same tenet as `SlotPitch`, applied to
 * the name instead of to the argument, and it is emphatically NOT the store's catalogue
 * speaking. **No field here is ever read off a bid.** A store that could write the
 * buyer-facing name of the thing it is selling would hold a persuasion lever with none of
 * the grading `message` goes through; the exchange reads this off the same crawl snapshot it
 * already grades the store's claims against, gated in Cypher to sources the platform
 * observed itself.
 *
 * `title` and `source` are BOTH REQUIRED, and the pairing is the point rather than schema
 * tidiness. `source` is the snapshot's own id (`neo4j-crawl:{store}:{product}` for the
 * crawl), so a rendered name can be traced back to the record that produced it — and a name
 * a reader cannot trace is indistinguishable from one the exchange made up. A reader that
 * accepted a title with no source would be rendering exactly that, which is why
 * `wire.ts::readIdentity` drops the whole object when either is missing.
 *
 * `brand` and `observed_at` are genuinely optional: the crawl states a brand for most rows
 * and not all, and `observed_at` is the snapshot's own stamp — WHEN the platform saw this,
 * which is the thing a shopper needs in order to distrust a stale name. Absent is absent and
 * neither is invented.
 */
export interface ShortlistProductIdentity {
  readonly title: string
  readonly brand?: string | null
  readonly source: string
  readonly observed_at?: string | null
}

/**
 * R2's PRODUCT: WHICH catalogue thing this slot offers, and — where the platform has crawled
 * it — what the platform's own record calls it.
 *
 * `product_ref` and `variant_ref` are REFERENCES, never a rendered name, and that has not
 * changed: they are what the roster and the offer agree on, and they are what the accept path
 * resolves against. What DID change is the argument that used to sit here, which said a title
 * "belongs to a catalogue, and nothing in this app has one" and concluded that a bare ref was
 * therefore the honest thing to show. The premise was right and the conclusion was wrong: this
 * app still owns no catalogue and still resolves nothing, but it is no longer handed only
 * references. `identity` is a name the PLATFORM crawled, carried under a key that says whose
 * it is and names the snapshot it came from — see `ShortlistProductIdentity`.
 *
 * `null`/absent where this exchange holds no crawled snapshot for the pair, which is the same
 * "we have not checked" that grades a claim `unsupported`. The producer publishes it only when
 * the ref it resolved the snapshot against is the ref published beside it, because a name read
 * for one product printed above another product's reference is D58's defect class wearing a
 * title, and a shopper cannot see the join to check it.
 */
export interface ShortlistProduct {
  readonly product_ref: string
  /** Absent means the bid named no variant — never "the default variant". */
  readonly variant_ref?: string | null
  /** The platform's own crawled name, or `null` — never the store's. Product-scoped. */
  readonly identity?: ShortlistProductIdentity | null
}

/**
 * The discount a slot's price states. A *stated* depth, not an entitlement: no single-use
 * code exists until the buyer accepts (D22), which is why the screen says "states".
 *
 * There is deliberately NO `provenance` here even though `contracts.protocol.Discount`
 * declares one and the buyer service forwards the discount whole. `journey/wire.ts::readDiscount`
 * rebuilds the object as `{type, value}`, so the rule that authorised the depth never reaches
 * this component on a slot — declaring the field would be a type promising a value the browser
 * does not populate. It is read off the auction record instead, as
 * {@link SlotRecord.discount_provenance}.
 */
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

/**
 * ONE CHECKED FACT the platform holds about a candidate, as `/render` serves it.
 *
 * `label` is the buyer-facing provenance label for THIS fact where it has one — a commitment
 * is a published `Claim` and carries its source's label — and `null` for the exchange's own
 * published fields (price, trust), which are not a store's claim and must not borrow a
 * store's badge. `null` is therefore a real answer here and is not rendered as a label.
 */
export interface PitchFact {
  readonly key: string
  readonly value: string
  /** `price` | `commitment` | `trust` — the platform's own vocabulary, rendered, not mapped. */
  readonly kind: string
  readonly label: string | null
}

/** The two voices `SlotPitch.voices` names. Recognised, never invented — see `voiceOrder`. */
export const VOICE_STORE = 'store'
export const VOICE_PLATFORM = 'platform'

/**
 * WHAT THIS SLOT SAYS TO THIS SHOPPER, AND IN WHOSE VOICE (SPEC core tenet, D55).
 *
 * The organic/sponsored split, arriving at the last hop. These two strings are NOT two
 * qualities of one thing and this app may not blur them:
 *
 * * `platform_case` is the ORGANIC result — the platform's own case for this candidate,
 *   written by the buyer-side agent from facts the platform already checked and constrained
 *   to them. Every candidate gets one, scraped shops included, because the buyer-side agent
 *   wants the shopper to buy *a* product.
 * * `store_pitch` is the SPONSORED result — the shop's own advocate's words, byte for byte.
 *   It is the thing a shop BUYS by joining the network, and it is `null` for a scraped shop,
 *   which has no advocate. Never paraphrased and never merged into the case beside it.
 *
 * The asymmetry is liability, not tidiness: the platform owns the false claim when the
 * platform writes the copy, so its own voice is bounded by its own snapshot, while the
 * seller's voice carries the seller's motive and is the one verified adversarially upstream.
 * A shopper who cannot tell which voice they are reading has been handed the seller's motive
 * wearing the platform's credibility.
 */
export interface SlotPitch {
  readonly platform_case: string
  /** `assembled` (the deterministic rendering) or `written` (prose that passed the screen). */
  readonly platform_case_source: string
  readonly store_pitch: string | null
  /** Which voices the service is serving, in its order. See `voiceOrder`. */
  readonly voices: readonly string[]
  /**
   * Every fact the platform holds about this candidate, ranked for this shopper — not only
   * the ones the case leads with. Served in full deliberately, so a reader can see the copy
   * is a SUBSET of what was checked rather than a summary of something else.
   */
  readonly facts: readonly PitchFact[]
}

/**
 * The voices to render, in the order to render them.
 *
 * **PRESENCE comes from the content and ORDER comes from `voices`**, and the split is
 * deliberate. A voice named in `voices` with nothing behind it would render as an attributed
 * empty box — the one shape rule 2 of this screen forbids — and a string present in the pitch
 * but missing from `voices` would be content the page silently swallowed. Reading each from
 * the field that can actually answer it makes both impossible.
 *
 * The default order is store-then-platform: the shop leads INSIDE ITS OWN SLOT, which is
 * presentation and is exactly what a shop buys. It applies only when `voices` names neither.
 */
export function voiceOrder(pitch: SlotPitch): readonly string[] {
  const present: string[] = []
  if (typeof pitch.store_pitch === 'string' && pitch.store_pitch.trim() !== '') {
    present.push(VOICE_STORE)
  }
  if (pitch.platform_case.trim() !== '') present.push(VOICE_PLATFORM)
  const stated = Array.isArray(pitch.voices) ? pitch.voices : []
  const ordered = stated.filter((voice) => present.includes(voice))
  // Anything the service did not order goes after, in the default order, so content is never
  // dropped by a `voices` list that disagreed with the pitch it arrived with.
  return [...ordered, ...present.filter((voice) => !ordered.includes(voice))]
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
  /**
   * HOW the labels above got there: `exchange` (the exchange sent them), `derived` (it sent
   * none and the buyer service worked them out from the slot's claims) or `absent` (neither,
   * so the slot reads `unverified`).
   *
   * `POST /buyer/shortlist/render` has published this on every slot since T-072
   * (`buyer_svc.accept.labels.LabelledSlot.labels_source`) and the browser has carried it
   * since — `journey/wire.ts::renderShortlist` writes `labels_source: asString(row.labels_source)`
   * onto every slot it hands this component. It was simply never DECLARED here, so nothing
   * could read it without a cast, and nothing did. Optional because a caller building a slot
   * by hand has no obligation to state it, and because a producer older than the field is a
   * real caller.
   */
  readonly labels_source?: string
  /** Present only on a slot the caller enriched; the protocol type carries no auction id. */
  readonly auction_id?: string
  readonly store_domain?: string
  /**
   * The WHOLE trust snapshot the exchange attached to this slot, values and all.
   *
   * `trust_summary` above is typed `Record<string, number>` and the browser narrows to it by
   * dropping every non-numeric field, which on a live shortlist is most of them: measured on
   * this stack, the exchange sends `{store_id, available, score, confidence, low_data,
   * dimensions[]}` and only `score` and `confidence` survive that narrowing. `wire.ts` keeps
   * the unnarrowed object beside it precisely so a screen can show what the narrowing lost —
   * and until this declaration existed, no screen could reach it.
   *
   * Values are `unknown` because they genuinely are: a boolean, a number, a string and a list
   * of dimension names all arrive here, and a reader that typed them would be inventing a
   * shape the exchange never promised.
   */
  readonly trust_fields?: Readonly<Record<string, unknown>>
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
  /**
   * WHOSE PRICE the `price` above is: `false` a bid this store actually sent, `true` a
   * stand-in the exchange wrote for it at its roster row's list price (R10), `null`/absent a
   * producer that did not say.
   *
   * **THREE states, and this screen may not collapse them into two.** The exchange represents
   * a rostered store that does not answer usably rather than dropping it, so a shopper sees
   * the shop at its catalogue price instead of not seeing it at all — and that number reaches
   * `price` like any other. Reading an absent flag as `false` would present a price nobody
   * quoted as a quote, which is the exact defect the field exists to close; so absence is its
   * own answer here and `ShortlistView` gives it its own sentence.
   *
   * `true` is NOT a verdict about the store. It is reachable by silence, by having no agent
   * at all, by a late reply and by an explicit decline alike, which is why the reason travels
   * separately instead of being folded into this boolean.
   */
  readonly fallback?: boolean | null
  /**
   * WHY the exchange stood in, in the exchange's own vocabulary
   * (`exchange.auction.collect.FALLBACK_REASONS`), or `null`.
   *
   * The buyer service already guarantees `null` whenever `fallback` is not `true` — a reason
   * carried beside a real quote would be read as one — and the token is forwarded rather than
   * turned into a sentence, because how much of it to say is the screen's decision and not
   * the wire's.
   */
  readonly fallback_reason?: string | null
  /**
   * The case for this slot and whose voice makes it, or `null`.
   *
   * `null` is the honest and ORDINARY answer, in two different situations the screen renders
   * identically because a shopper has no use for the distinction: a producer older than this
   * field, and a candidate the platform holds nothing sayable about whose shop also sent no
   * message. The second is `buyer_svc.pitch.writing.pitch_for` deciding to say less rather
   * than invent a reason to buy. Neither renders as an empty attributed box.
   */
  readonly pitch?: SlotPitch | null
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
 * The FAMILY half of an exchange `fallback_reason` — everything before the first colon.
 *
 * The exchange writes every reason either as a bare family word or as `family:detail`
 * (`exchange.auction.collect.refusal_reason`), and the detail half is open-ended by
 * construction: on a refusal it is the HTTP status a store's agent answered with, on a
 * decline it is a reason header the store chose. So a screen that matched whole strings would
 * recognise `store_refused:422` and then fail to recognise `store_refused:503`, which is the
 * same store behaviour.
 *
 * A SECOND COPY of `journey/wire.ts::fallbackReasonFamily`, and deliberately so rather than an
 * import: `wire.ts` imports this module, so the dependency between the two runs one way and
 * importing it back would close a cycle. What is copied is one published splitting rule, not a
 * vocabulary — {@link NEVER_ASKED_FALLBACK_FAMILIES} below names two of the exchange's twelve
 * families, which is the whole of what this screen has to tell apart.
 */
export function fallbackReasonFamily(reason: string | null | undefined): string {
  if (typeof reason !== 'string') return ''
  return (reason.split(':', 1)[0] ?? '').trim()
}

/**
 * The `fallback_reason` family for a shop that HAS NO BIDDING AGENT for anyone to solicit.
 *
 * `exchange.auction.collect.NO_AGENT_REASON` is `tier_0_no_agent:<detail>`, and it is the
 * exchange's word for a shop that is on the roster from its crawled catalogue alone. It is one
 * MEMBER of {@link NEVER_ASKED_FALLBACK_FAMILIES}, and it is named on its own because it is the
 * only member whose cause this screen can state in the shop's own terms: the shop has no agent,
 * which is a durable fact about the shop rather than about one auction.
 */
export const NO_AGENT_FALLBACK_FAMILY = 'tier_0_no_agent'

/**
 * The `fallback_reason` family for a store the exchange HAD NO WORKER FREE TO ASK.
 *
 * `exchange.auction.collect.FAN_OUT_CAPACITY_REASON`, a bare family word with no detail half:
 * "**The exchange never asked, because it had no worker free to ask with** … Both are R10's
 * bounded degradation and both are the EXCHANGE's condition, not the store's". Reachable on any
 * busy auction — `collect.py::_unusable_because` returns it whenever the fan-out stamped
 * `NOT_ASKED_FIELD`, which `BoundedFanOutPool` does when every worker is held and which the
 * per-call `max_workers` cap does when it ends the roster.
 */
export const FAN_OUT_CAPACITY_FALLBACK_FAMILY = 'fan_out_capacity_exhausted'

/**
 * EVERY `fallback_reason` family that means NOBODY WAS ASKED — the class, not two instances.
 *
 * The exchange's vocabulary splits in two at one joint, and this is the joint a shopper-facing
 * sentence has to respect: these families describe something that happened BEFORE any
 * solicitation went out, and every other family in `collect.py::FALLBACK_REASONS` describes
 * something that happened after one did — silence, a late reply, an unreadable answer, an
 * explicit decline. Telling a shopper a never-dialled shop "did not answer this auction" puts a
 * refusal in the mouth of a shop nobody spoke to, and D55's whole asymmetry is that the platform
 * may state only what it can check. That is a rule about a CLASS, so it is written as one: a
 * screen that split on `tier_0_no_agent` alone said the same false thing about the other member.
 *
 * Both members were checked against `collect.py` one by one, and the other ten were checked the
 * same way and are NOT here: `response_timed_out` is documented "**the store was solicited** and
 * was still answering when the window shut", and `no_response`, `response_after_deadline`, the
 * three `MALFORMED_RESPONSE_REASONS`, `bid_price_unreconcilable`, `bid_claim_unprovenanced`,
 * `store_declined` and `store_refused` are each a verdict on something a solicited store did.
 *
 * A COPY, so it can go stale: the exchange grew this vocabulary twice already
 * (`response_timed_out` and `fan_out_capacity_exhausted` were both added after the first split),
 * and a family this list has not caught up with falls to the ASKED sentence — which is the safe
 * direction only because that sentence names the exchange's own token beside it. Adding a family
 * to `FALLBACK_REASONS` that means "never dialled" means adding it here.
 *
 * ONE distinction and not nine. The nine-family gloss lives in
 * `journey/WhyEmpty.tsx::explainFallbackReason` and this file does not fork it — it cannot
 * import it (see {@link fallbackReasonFamily}), and a second copy of a vocabulary that has
 * already grown twice would go stale where a splitting rule cannot. What this screen needs is
 * not nine sentences; it is the answer to one question — was there anybody to ask? — and past
 * that it prints the exchange's own token unchanged.
 */
export const NEVER_ASKED_FALLBACK_FAMILIES: readonly string[] = [
  NO_AGENT_FALLBACK_FAMILY,
  FAN_OUT_CAPACITY_FALLBACK_FAMILY,
]

/**
 * Was this stand-in minted WITHOUT the shop ever being solicited?
 *
 * `false` for an unreadable reason and for a family this copy of the vocabulary does not carry —
 * which lands on the sentence that asserts nothing about the shop, see {@link askedAndSilent}.
 */
export function neverAsked(reason: string | null | undefined): boolean {
  return NEVER_ASKED_FALLBACK_FAMILIES.includes(fallbackReasonFamily(reason))
}

/**
 * The ONE family that means the shop was asked and NOTHING CAME BACK.
 *
 * `journey/wire.ts::NO_RESPONSE_REASON`, whose own doc comment is "nothing came back from that
 * store's agent at all … It used to cover three different facts … It now means only the last of
 * those, and the other two have words of their own". It is the only family in the exchange's
 * twelve that licenses the sentence "this shop did not answer this auction".
 *
 * WHY IT IS ONE FAMILY AND NOT EIGHT, which is the second half of the never-asked repair. The
 * card had exactly two sentences, so every family that was not `tier_0_no_agent` read "the shop
 * did not answer this auction" — and eight of those families mean the shop ANSWERED.
 * `store_declined` is the store-agent contract's 204 and this app's own gloss for it says so in
 * as many words ("means that store was asked, it answered, and its answer was no"), so the
 * shortlist card and the empty-shortlist panel one screen away made opposite statements about
 * the same shop. `store_refused`, `response_carried_no_bid`, `response_not_stamped`,
 * `arrival_stamp_unparseable`, `bid_price_unreconcilable`, `bid_claim_unprovenanced` and
 * `response_after_deadline` are all verdicts on something that ARRIVED. Three of the twelve are
 * neither silence nor an answer — `response_timed_out` is a store "still answering when the
 * window shut", which is the distinction the exchange minted that word to stop collapsing, and
 * `tier_0_no_agent` and `fan_out_capacity_exhausted` are the two nobody dialled, handled above
 * by {@link NEVER_ASKED_FALLBACK_FAMILIES}. Eight, one and three is the whole vocabulary.
 *
 * So this screen makes three statements and no more: nobody asked, nothing came back, or — for
 * every other family AND for a family this copy has not caught up with — the exchange could not
 * use what it got, which asserts nothing about the shop's own conduct. Under-claiming is the
 * safe direction and the exchange's own token is printed beside all three.
 */
export const NO_RESPONSE_FALLBACK_FAMILY = 'no_response'

/** Was this shop asked, with nothing coming back at all? See {@link NO_RESPONSE_FALLBACK_FAMILY}. */
export function askedAndSilent(reason: string | null | undefined): boolean {
  return fallbackReasonFamily(reason) === NO_RESPONSE_FALLBACK_FAMILY
}

/**
 * WHERE THIS SLOT'S EVIDENCE CAME FROM, as the exchange published it (D30).
 *
 * The `Provenance` on a published `Claim`, read back off the auction record. `source` is the
 * exchange's own closed vocabulary and is rendered as the token it is — this app does not own
 * the source→label map (D30 puts it in `packages/contracts` so the exchange and the buyer
 * cannot drift into two answers) and nothing here derives a label from it. The buyer-facing
 * label a shopper reads still comes from the buyer service, on `SlotCommitment.label`.
 *
 * `authority_rank` has published semantics: **1 is the most authoritative and larger is
 * weaker.** `null` when the claim stated none, or stated something this reader cannot read as
 * one — the contract validates it `>= 1`, so a `0` is not a stronger rank than the strongest
 * the network publishes, it is a value from a producer this page cannot identify.
 */
export interface ClaimProvenance {
  readonly source: string
  /** A pointer to the evidence — a snapshot URI, an envelope commitment path, a pitch span. */
  readonly ref: string | null
  readonly observed_at: string | null
  /** 1 = strongest. `null` when the claim named no readable rank. */
  readonly authority_rank: number | null
}

/**
 * ONE PROMISE AS THE EXCHANGE PUBLISHED IT — the claim before the buyer service projected it.
 *
 * `SlotCommitment` is what a card renders: the promise, its value, and the one buyer-facing
 * label. This is the same promise with the evidence still attached, and the difference is the
 * whole reason the record panel exists — `buyer_svc.accept.labels.slot_commitments` projects a
 * published `Claim` down to `{key, value, unit, label}`, and `journey/wire.ts::readCommitments`
 * keeps exactly those four. So `claim_type` and the entire `provenance` block are dropped twice
 * on the way to a shopper, and the only place they survive is the auction record itself.
 */
export interface SlotClaim {
  readonly key: string
  readonly value: unknown
  readonly unit: string | null
  /** D53's typed claim vocabulary (`return_policy`, `shipping_speed`, …), or `null`. */
  readonly claim_type: string | null
  readonly provenance: ClaimProvenance | null
}

/**
 * WHAT THE PUBLISHED RANKING SCORED THIS CANDIDATE AT, and what each term contributed.
 *
 * The exchange publishes `rank_score` and its `components` once, in the body of its
 * `POST /auctions` answer, and the buyer service records that body — so this is the RECORDED
 * half of the auction, from the moment it was scored, and it is not re-computed for this
 * request. A screen must say so: it is a different clock from the live shortlist beside it.
 *
 * `components` is kept as ordered pairs rather than an object because the ORDER is the
 * exchange's and carries meaning a reader uses (the terms are published in formula order);
 * round-tripping through an object literal would be one refactor away from being sorted.
 */
export interface SlotRanking {
  /** `null` when the row carried no finite score — never a defaulted `0`. */
  readonly rank_score: number | null
  readonly components: readonly (readonly [string, number])[]
}

/** Everything the auction record holds about ONE candidate that its shortlist card does not. */
export interface SlotRecord {
  readonly bid_ref: string
  /** The exchange's own attribution of this bid to a store. `''` when the row named none. */
  readonly store_id: string
  /**
   * `null` means the exchange published no commitments for this slot — an R10 stand-in bid is
   * rebuilt from the roster with an empty claims list and is exactly that case. `[]` would say
   * the shop committed to nothing, which is a different sentence.
   */
  readonly claims: readonly SlotClaim[] | null
  /** The rule that authorised this slot's discount, or `null`. See {@link ClaimProvenance}. */
  readonly discount_provenance: ClaimProvenance | null
  readonly ranking: SlotRanking | null
}

/**
 * The buyer service's record of one auction, reduced to what a shortlist card cannot show.
 *
 * `liveness` is the same two-state answer `journey/wire.ts::ShortlistLiveness` publishes and it
 * is not collapsed here either: `'forgotten'` is `shortlist: null`, which is a fact about the
 * EXCHANGE (its 15-minute TTL took the auction away) and not about the market. The recorded
 * diagnostics survive that — `ranked` is what the exchange said when the auction opened — so a
 * record fetched after the TTL still carries every candidate's ranking and no claims at all.
 */
export interface RecordedAuction {
  readonly auction_id: string
  readonly liveness: 'live' | 'forgotten'
  readonly recorded_at: string
  /** Keyed by `bid_ref`, which is what a shortlist slot names itself with. */
  readonly slots: Readonly<Record<string, SlotRecord>>
}

/**
 * `GET {AUCTION_RECORD_PATH}{auction_id}` — the auction as the buyer service recorded it.
 *
 * A second spelling of `journey/wire.ts::AUCTION_PATH_PREFIX`, for the reason
 * {@link fallbackReasonFamily} is a second copy: that module imports this one.
 */
export const AUCTION_RECORD_PATH = '/buyer/auctions/'

/** Thrown when the auction record could not be read. Names the status, always. */
export class RecordedAuctionUnreadable extends Error {
  constructor(
    readonly auctionId: string,
    readonly reason: string,
  ) {
    super(`the record for auction ${auctionId} could not be read: ${reason}`)
    this.name = 'RecordedAuctionUnreadable'
  }
}

function readClaimProvenance(value: unknown): ClaimProvenance | null {
  if (!isRecord(value)) return null
  const source = isNonEmptyString(value.source) ? value.source.trim() : ''
  // With no source there is no evidence to name, and an object of three empty fields under the
  // word "evidence" reads as evidence a reader cannot check rather than as none at all.
  if (source === '') return null
  const rank = value.authority_rank
  return {
    source,
    ref: isNonEmptyString(value.ref) ? value.ref.trim() : null,
    observed_at: isNonEmptyString(value.observed_at) ? value.observed_at.trim() : null,
    // `>= 1` is the contract's own bound and it is checked rather than trusted: a `0` rendered
    // beside "1 is the strongest" would read as stronger than the strongest rank the network
    // publishes, which is a claim about evidence that nobody made.
    authority_rank:
      typeof rank === 'number' && Number.isInteger(rank) && rank >= 1 ? rank : null,
  }
}

function readSlotClaim(value: unknown): SlotClaim | undefined {
  if (!isRecord(value)) return undefined
  const key = isNonEmptyString(value.key) ? value.key.trim() : ''
  // A row with no key has nothing in it a shopper could read. Dropped alone, never with its
  // neighbours — the same rule `buyer_svc.accept.labels.slot_commitments` keeps, because
  // dropping a whole list over one bad row hides promises that were fine.
  if (key === '') return undefined
  return {
    key,
    value: value.value,
    unit: isNonEmptyString(value.unit) ? value.unit.trim() : null,
    claim_type: isNonEmptyString(value.claim_type) ? value.claim_type.trim() : null,
    provenance: readClaimProvenance(value.provenance),
  }
}

function readSlotClaims(value: unknown): readonly SlotClaim[] | null {
  // `null` and `[]` are kept apart the whole way: `null` is "the exchange published none",
  // `[]` is "it published some and none of them were readable". Only the first is ordinary.
  if (!Array.isArray(value)) return null
  const claims: SlotClaim[] = []
  for (const row of value) {
    const claim = readSlotClaim(row)
    if (claim !== undefined) claims.push(claim)
  }
  return claims
}

function readRanking(value: unknown): SlotRanking | null {
  if (!isRecord(value)) return null
  const score = value.rank_score
  const components: (readonly [string, number])[] = []
  if (isRecord(value.components)) {
    for (const [key, term] of Object.entries(value.components)) {
      // Numbers only, and that loses nothing: `exchange/auction/routes.py::_ranked_out` builds
      // this map as `{str(k): float(v)}`, so a value that is not a finite number did not come
      // from the exchange and is not shown under the exchange's name.
      if (typeof term === 'number' && Number.isFinite(term)) components.push([key, term])
    }
  }
  return {
    rank_score: typeof score === 'number' && Number.isFinite(score) ? score : null,
    components,
  }
}

/**
 * The auction record, reduced to `SlotRecord`s keyed by bid ref.
 *
 * The two halves are merged by `bid_ref` and neither is required to carry the other's rows: a
 * candidate the ranking scored but the shortlist did not seat has a ranking and no claims, and
 * a shortlist read after the exchange's TTL has claims for nothing. Merging on the key the
 * shortlist card already names itself with is what keeps this join from being a guess.
 */
export function readRecordedAuction(auctionId: string, payload: unknown): RecordedAuction {
  if (!isRecord(payload)) {
    throw new RecordedAuctionUnreadable(auctionId, 'the service answered with something that is not an object')
  }
  const shortlist = payload.shortlist
  // `Object.create(null)` and not `{}`, and this is a correctness rule rather than a style.
  // A `bid_ref` is a string off the wire, so the reader must behave for every string — and on
  // a plain object literal the strings that name `Object.prototype`'s own members do not
  // behave: `slots['toString']` reads back a FUNCTION nobody put there, which `seated` would
  // then spread into a `SlotRecord`, and an assignment to `slots['__proto__']` retargets the
  // prototype instead of storing a row. A prototype-less map has no inherited keys for either
  // to find. The exchange mints `{auction_id}:{store_id}`, so this is not reachable today; the
  // point is that it does not depend on that staying true.
  const slots: Record<string, SlotRecord> = Object.create(null) as Record<string, SlotRecord>

  const seated = (bidRef: string): SlotRecord =>
    slots[bidRef] ?? {
      bid_ref: bidRef,
      store_id: '',
      claims: null,
      discount_provenance: null,
      ranking: null,
    }

  if (isRecord(shortlist) && Array.isArray(shortlist.slots)) {
    for (const row of shortlist.slots) {
      if (!isRecord(row) || !isNonEmptyString(row.bid_ref)) continue
      const bidRef = row.bid_ref.trim()
      const price = isRecord(row.price) ? row.price : undefined
      const discount = price !== undefined && isRecord(price.discount) ? price.discount : undefined
      slots[bidRef] = {
        ...seated(bidRef),
        claims: readSlotClaims(row.commitments),
        discount_provenance:
          discount === undefined ? null : readClaimProvenance(discount.provenance),
      }
    }
  }

  if (Array.isArray(payload.ranked)) {
    for (const row of payload.ranked) {
      if (!isRecord(row) || !isNonEmptyString(row.bid_ref)) continue
      const bidRef = row.bid_ref.trim()
      slots[bidRef] = {
        ...seated(bidRef),
        store_id: isNonEmptyString(row.store_id) ? row.store_id.trim() : '',
        ranking: readRanking(row),
      }
    }
  }

  return {
    auction_id: isNonEmptyString(payload.auction_id) ? payload.auction_id : auctionId,
    // An explicit `null` is the exchange having forgotten this auction; anything else that is
    // not a readable shortlist is a body this reader could not use, and both leave the claims
    // half empty. They are told apart because only the first is a statement the service made.
    liveness: shortlist === null ? 'forgotten' : 'live',
    recorded_at: isNonEmptyString(payload.recorded_at) ? payload.recorded_at : '',
    slots,
  }
}

/**
 * Fetch the auction record. The ONLY request this module makes that is not an accept.
 *
 * `GET`, and it creates nothing: it re-reads a record the buyer service already holds, on a
 * route the shopper page has already called once through `journey/wire.ts::loadAuction`. It is
 * fetched again here rather than passed down because `Journey` holds that record and hands
 * `ShortlistView` only the labelled slots — and a component that invented the ranking numbers
 * rather than reading them would be publishing a formula under the exchange's name.
 */
export async function loadRecordedAuction(
  auctionId: string,
  fetcher: Fetcher,
): Promise<RecordedAuction> {
  const named = auctionId.trim()
  if (named === '') throw new RecordedAuctionUnreadable('', 'this shortlist names no auction')
  const response = await fetcher(`${AUCTION_RECORD_PATH}${encodeURIComponent(named)}`)
  if (!response.ok) throw new RecordedAuctionUnreadable(named, `the service answered HTTP ${response.status}`)
  let payload: unknown
  try {
    payload = (await response.json()) as unknown
  } catch {
    throw new RecordedAuctionUnreadable(named, 'the service answered 200 with a body that is not JSON')
  }
  return readRecordedAuction(named, payload)
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
