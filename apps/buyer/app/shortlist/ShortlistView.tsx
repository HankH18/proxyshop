/**
 * The screen R2 names: up to four differentiated slots, each showing WHAT THE THING IS, WHAT
 * IT COSTS, WHAT THE STORE COMMITS TO, a trust reading and where its evidence came from —
 * and an Accept that hands off to the exchange's checkout (T-072).
 *
 * The first three of those are the exchange's `product`, `price` and `commitments`, and this
 * component renders them where a person decides: on the card, above the diagnostics, in that
 * order. All three are OPTIONAL and ABSENCE IS THE ORDINARY CASE — a fallback bid promises
 * nothing, a roster row with no readable list price prices nothing — so each renders its own
 * plain sentence when the exchange sent none, and never an empty line, an `undefined`, or a
 * `0`. A card that quietly dropped its price line would leave a shopper comparing a store
 * that quoted nothing against one that quoted, with nothing on screen saying so.
 *
 * Seven things here are load-bearing:
 *
 * 1. **Every slot's provenance labels are rendered, always.** Not on hover, not behind a
 *    "details" toggle. R2's point is that a buyer can see which claims a store stands
 *    behind before choosing, and provenance a buyer has to go looking for is provenance
 *    they did not have when they chose. The record fold added in rule 7 does not weaken this
 *    and could not: nothing that was on the card moved into it.
 * 2. **The labels are the strings the exchange sent.** This component does not map, filter,
 *    translate or re-derive them — `labelTone` only picks a `data-tone` for styling, and a
 *    label it does not recognise is still printed (D30).
 * 3. **There is no `href` anywhere in this file.** The Accept control is a `<button>`. A
 *    slot may carry a `checkout_url` the buyer must never follow, and an `<a href>` built
 *    from slot data is exactly the R3 violation the acceptance fixture's decoy is designed
 *    to catch. The destination does not exist until the exchange has answered.
 * 4. **Each commitment carries its OWN provenance label, beside it.** The slot's
 *    `provenance_labels` are one aggregate line; a shopper deciding whether to believe "free
 *    returns" needs the label for that promise. It is a word and a `data-tone`, in the row,
 *    never behind a hover — provenance a buyer has to go looking for is provenance they did
 *    not have when they chose. A promise whose evidence cannot be named reads `unverified`
 *    rather than being hidden or, worse, defaulted to `store-confirmed`.
 * 5. **The two voices are rendered as two voices.** A slot's `pitch` carries the platform's
 *    own case (organic, written for every candidate) and, where a shop has an advocate, that
 *    shop's own words (sponsored, the thing a shop buys). `PitchPanel` below renders them in
 *    separate, separately-attributed blocks and never merges them, because a shopper who
 *    reads the seller's motive in the platform's voice has no reason to discount it. A
 *    seller's prose is untrusted text and is rendered as text: no markup, no link.
 * 6. **One accept per mount, and the shortlist collapses rather than pads.** A double click
 *    on Accept is the gesture that asks for two checkouts for one auction; the ref below is
 *    the client's half of the ledger the service keeps. And a shortlist with one eligible
 *    store renders one slot — there is no padding-out with a store that failed a filter.
 * 7. **A card opens into the record behind it, and the record keeps the same two voices.**
 *    `SlotRecordPanel` below. What a shopper decides on stays on the card; what opens is the
 *    material a card has no room for and which, until now, reached the browser and was
 *    rendered by nobody — the crawl's snapshot id and stamp, the whole trust snapshot rather
 *    than the two numeric fields the browser's narrowing leaves, the published rank score
 *    with what each term of the formula contributed, and every promise's own provenance,
 *    authority rank and evidence reference. It is split into a PLATFORM block and a SHOP
 *    block for the same reason `PitchPanel` is, and the split matters more here: this is the
 *    surface a shopper opens in order to decide whether to believe something.
 *
 * WHAT CHANGED ABOUT THIS COMPONENT'S I/O, because the sentence that used to close this
 * header — "This component performs no I/O" — is no longer true and its retirement is a
 * decision rather than a drift. Rule 7's platform block is drawn entirely from the slot this
 * component is handed; the claims, the ranking and the discount's authorising rule are not on
 * that slot and cannot be put there from here. They are dropped twice on the way in —
 * `buyer_svc.accept.labels.slot_commitments` projects a published `Claim` down to
 * `{key, value, unit, label}`, and `journey/wire.ts::readCommitments` keeps exactly those four
 * — and `Journey` holds the auction record they survive on without passing it down. So the
 * fold re-reads `GET /buyer/auctions/{id}`, on the click that opens it and once per mount.
 * The alternative was a component that computed a rank breakdown of its own, which would be
 * this page publishing a formula under the exchange's name. `shortlist.ts` still owns the
 * wire: the request, its reader and its refusal all live there.
 */
import { Fragment, useCallback, useRef, useState, type ReactNode } from 'react'

import {
  NO_AGENT_FALLBACK_FAMILY,
  VOICE_PLATFORM,
  VOICE_STORE,
  fallbackReasonFamily,
  labelTone,
  loadRecordedAuction,
  slotLabels,
  voiceOrder,
  type AcceptOutcome,
  type Fetcher,
  type RecordedAuction,
  type Shortlist,
  type ShortlistPrice,
  type ShortlistProduct,
  type ShortlistSlot,
  type SlotClaim,
  type SlotCommitment,
  type SlotDiscount,
  type SlotPitch,
} from './shortlist'

export interface ShortlistViewProps {
  /** The shortlist as the exchange assembled it. One to four slots. */
  readonly shortlist: Shortlist
  /** Accept one slot. The ONLY path to a checkout, and the only thing that mints a URL. */
  readonly onAccept: (slot: ShortlistSlot) => void | Promise<void>
  /** The accepted offer, once there is one. Shown so the buyer can see where they are going. */
  readonly accepted?: AcceptOutcome
  /** A refusal to show instead of a checkout. */
  readonly error?: string
  /** Shown while a request is in flight. */
  readonly busy?: boolean
  /**
   * How the record fold reads `GET /buyer/auctions/{id}`. Defaults to the browser's own
   * `fetch`, which is what makes the fold work on the served page without `Journey` passing
   * anything: a knob whose only setter is a test is a feature that is switched off in every
   * deployment, and this one defaults to the working path.
   *
   * Injected at all so a test can drive the fold against a body it controls and assert that
   * nothing was requested until a reader opened one.
   */
  readonly recordFetcher?: Fetcher
}

/**
 * The browser's own `fetch`, or `undefined` where there is no browser.
 *
 * Wrapped rather than passed by reference because a bare `fetch` detaches from its global and
 * throws `Illegal invocation` in a browser the moment it is called through a variable.
 */
function browserFetcher(): Fetcher | undefined {
  if (typeof fetch !== 'function') return undefined
  return (input, init) => fetch(input, init)
}

function trustLine(slot: ShortlistSlot): string {
  const summary = slot.trust_summary ?? {}
  const entries = Object.entries(summary)
  if (entries.length === 0) return 'no trust snapshot yet'
  return entries.map(([key, value]) => `${key} ${value}`).join(', ')
}

/**
 * WHOSE SHOP THIS IS.
 *
 * Both design documents put the store's identity at the head of the card, and until now the
 * card named no store at all: a shopper comparing two slots saw `fit` against `value`, two
 * product refs and two prices, with nothing on either card saying who was offering them.
 *
 * The domain is the right field for it rather than a display name, and not only because it is
 * the one the exchange sends. It is the host the accepted permalink is PINNED against —
 * `permalinkRefusal` checks the minted URL's host against this exact string, and a permalink
 * that names anything else is refused before a browser is sent to it. Putting it on the card
 * is therefore the shopper seeing where Accept will take them BEFORE they press it, rather
 * than on the handoff screen afterwards.
 *
 * Absent is an ordinary answer and gets a sentence, never a blank line: an exchange with no
 * platform registry behind it publishes no domain, which is a deployment saying it vouches
 * for no host — not a store without one.
 */
function storeDomainLine(domain: string | undefined): string {
  const named = typeof domain === 'string' ? domain.trim() : ''
  return named === '' ? 'The exchange named no domain for this store.' : named
}

/**
 * WHAT THE THING IS — the platform's own crawled name where it has one, the reference where
 * it does not.
 *
 * WHAT THIS USED TO ARGUE, and why the argument is gone rather than softened. It read: "the
 * exchange sends a catalogue *reference*, deliberately — a title belongs to the store's
 * catalogue and copying one through the exchange would publish a second, staler spelling of
 * a fact it does not own." Every clause of that is about the STORE's catalogue, and it was
 * sound about the store's catalogue. It was the wrong question. The name on the card is not
 * the store's and never passes through the store: `exchange.ranking.verification
 * .catalog_identity` reads it off the PLATFORM's own snapshot — the same one the exchange
 * already grades the store's claims against, gated in Cypher to sources the platform
 * observed itself, reachable from no bid at all — and publishes it as `product.identity`
 * with the snapshot id that produced it. So this is not the exchange re-spelling a fact it
 * does not own; it is the platform stating a fact it does own, in its own voice.
 *
 * That snapshot is a crawl on a crawled shop (`neo4j-crawl:{store}:{product}`) and an
 * operator's deployment document on a configured one, which is why the card's attribution
 * says "Proxyshop's own record" and prints the id rather than claiming Proxyshop went and
 * looked. Preserving that distinction is the stated reason `source` is a REQUIRED field.
 *
 * That makes the name the ORGANIC half of D55, exactly as `platform_case` is: a
 * platform-authored fact about a product beside a platform-authored case for a shop. It is
 * therefore attributed on the card rather than printed bare — see the identity gloss in the
 * slot below, which names the snapshot and when the platform saw it — because a title with
 * no attribution reads as the shop's word for its own product, and a shopper who cannot tell
 * whose name they are reading has been handed the seller's voice wearing the platform's.
 *
 * `brand` joins the title when the crawl recorded one, and is absent otherwise; no brand is
 * inferred from a title. With NO identity the reference is what a shopper sees, unchanged
 * and for the original reason — this app resolves nothing and dresses nothing up. Absence of
 * a product altogether gets a sentence, never a blank line.
 */
function productLine(product: ShortlistProduct | null | undefined): string {
  if (!product || !product.product_ref) {
    return 'The exchange did not name a product for this slot.'
  }
  const identity = product.identity
  if (identity) {
    return identity.brand ? `${identity.title} — ${identity.brand}` : identity.title
  }
  const variant = product.variant_ref
  return variant ? `${product.product_ref} — variant ${variant}` : product.product_ref
}

/**
 * WHOSE PRICE THIS IS — three answers, and the third is not the first (R10, D55).
 *
 * `false` renders NOTHING, and that is the deliberate half. A price a store actually quoted
 * is the unremarkable case, and a line on every card saying "this shop quoted this" would
 * teach a reader to skim the one place the sentence matters. The other two both get words:
 *
 * * `true` — the exchange stood in for the shop at the list price on its own roster row. The
 *   number is real and nobody at that shop quoted it. The reason is the exchange's own token,
 *   printed as the token it is rather than translated, because this screen is not the layer
 *   that owns that vocabulary.
 * * `null`/absent — the exchange did not state whose price this is. That is a producer older
 *   than the field, and the page says so instead of guessing. Rendering it as `false` would
 *   turn silence into a quote, which is the entire reason the flag exists.
 *
 * `priced` is here because this line attributes A NUMBER, and a slot can carry the flag with
 * no number under it: an R10 stand-in minted from a roster row that named no readable list
 * price is exactly that, and it is not rare. With no price, "the exchange did not say whose
 * price this is" is a sentence about nothing, printed directly beneath a line that already
 * said there is no price — so the unknown state stays quiet and the stand-in state says the
 * thing that is still true and still useful.
 *
 * WHY `true` IS TWO SENTENCES AND NOT ONE, which is the correction this function most needed.
 * It used to branch on the flag and the price alone, so every stand-in read "**The shop did
 * not answer this auction**" — byte-identical for a shop whose agent was solicited and stayed
 * silent and for a shop that has no agent at all. The second of those is an ORGANIC result: a
 * shop Proxyshop found by crawling, on the roster from its catalogue alone, which was never
 * spoken to and declined nothing. Telling a shopper it did not answer is the platform putting
 * a refusal in a shop's mouth, and D55's whole asymmetry is that the platform may state only
 * what it can check. The exchange has told the two apart all along —
 * {@link NO_AGENT_FALLBACK_FAMILY} is the family it writes for the second, measured live on
 * this stack as `tier_0_no_agent:no_bid_endpoint` — and only this line was collapsing them.
 *
 * ONE distinction and not nine. Past "was there anybody to ask?", the exchange's token is
 * printed unchanged; the nine-family gloss lives in `journey/WhyEmpty.tsx` and is not forked
 * here (see {@link NO_AGENT_FALLBACK_FAMILY}).
 */
function priceProvenanceLine(
  fallback: boolean | null | undefined,
  reason: string | null | undefined,
  priced: boolean,
): string | null {
  if (fallback === false) return null
  if (fallback === true) {
    const named = typeof reason === 'string' ? reason.trim() : ''
    const because =
      named === ''
        ? 'The exchange did not say why it stood in.'
        : `The exchange gives its reason as ${named}.`
    // "Nobody asked" is not a weaker version of "nobody answered" — it is about a different
    // party. Only the second sentence is about the shop at all.
    const neverAsked = fallbackReasonFamily(named) === NO_AGENT_FALLBACK_FAMILY
    const what = neverAsked
      ? priced
        ? 'This price is Proxyshop’s, not this shop’s. This shop has no bidding agent on the exchange, so nobody asked it for a price and it turned nothing down — Proxyshop found it by crawling and is showing it to you at the list price on its own roster row for the shop.'
        : 'This shop has no bidding agent on the exchange, so nobody asked it for a price and it turned nothing down. Proxyshop found it by crawling and is showing it to you rather than leaving it out, and its roster row carried no list price either, so there is no number here that anyone quoted.'
      : priced
        ? 'This price is Proxyshop’s, not this shop’s. The shop did not answer this auction, so the exchange stood in for it at the list price on its own roster row — nobody at this shop quoted the number above.'
        : 'This shop did not answer this auction. The exchange stood in for it rather than dropping it, and its roster row carried no list price to show you either, so there is no number here that anyone quoted.'
    return `${what} ${because}`
  }
  if (!priced) return null
  return 'The exchange did not say whose price this is: whether this shop quoted it, or Proxyshop stood in for a shop that did not answer and used a list price instead. This page will not guess.'
}

/**
 * An amount as the exchange sent it, with the currency it named and no symbol it did not.
 *
 * `78` and `78.0` are one number and JavaScript spells it `78`; nothing here rounds, pads to
 * two places, or prepends a `$`. A currency this app invented would be a claim about what a
 * shopper will be charged, made by the one party in this system that is not charging them.
 */
function moneyText(amount: number, currency: string | null | undefined): string {
  return currency ? `${currency} ${amount}` : `${amount}`
}

/**
 * WHAT IT COSTS. `null` is the ordinary case, not an error, and it is never a zero.
 *
 * The total leads because it is what the shopper pays. The unit price is shown beside it
 * only when the two differ — when they are equal, printing both invites reading a quantity
 * into a slot that never stated one.
 */
function priceLine(price: ShortlistPrice | null | undefined): string {
  if (!price) return 'No price — this store did not quote one for this slot.'
  const total = moneyText(price.total_price, price.currency)
  const parts = [total]
  if (price.unit_price !== price.total_price) {
    parts.push(`${moneyText(price.unit_price, price.currency)} each`)
  }
  const line = parts.join(' — ')
  return price.currency ? line : `${line} (the exchange named no currency for this)`
}

/**
 * `Discount.type` spellings that mean "`value` is a percentage depth".
 *
 * MIRRORS `contracts.boundary.PERCENTAGE_DISCOUNT_TYPES` (and its TypeScript twin in
 * `packages/contracts/src/ts/boundary.ts`) rather than importing it, because this app imports
 * nothing from `packages/contracts` and starting here would be a new dependency for one word.
 *
 * Restated because a single spelling was wrong in the one direction that mattered. The only
 * producer in the tree is `store_agent.runtime.bidding.PERCENTAGE`, whose value is
 * `"percentage"` and whose comment calls it "the only form a tool hook can authorize" — while
 * this function tested `=== 'percent'`. So the `% off` branch had never once run: every
 * discount a shopper has ever been shown rendered as `percentage 15, as this store states it`.
 * The test that covers this line passed throughout, because its fixture typed `'percent'` — a
 * value no producer emits — and asserted only that the text contained the number.
 */
const PERCENTAGE_DISCOUNT_TYPES: ReadonlySet<string> = new Set(['percentage', 'percent', 'pct'])

/** The discount the store STATES. Not a code, not an entitlement — see `SlotDiscount`. */
function discountLine(discount: SlotDiscount): string {
  const depth = PERCENTAGE_DISCOUNT_TYPES.has(discount.type)
    ? `${discount.value}% off`
    : `${discount.type} ${discount.value}`
  return `${depth}, as this store states it — no code exists until you accept.`
}

/**
 * ONE PROMISE, in words rather than in the store's field name.
 *
 * `free_returns` becomes `free returns` and a boolean becomes `yes`/`no`: a shopper reading
 * `free_returns: true` is reading a database row, not a promise. The key's own characters
 * are otherwise untouched — this is the store's vocabulary and this app does not translate
 * it into one of its own — and a value that is neither boolean nor number is printed as the
 * store wrote it.
 */
function commitmentText(commitment: SlotCommitment): string {
  const promise = commitment.key.replace(/_/g, ' ')
  const { value, unit } = commitment
  if (value === true) return `${promise} — yes`
  if (value === false) return `${promise} — no`
  if (value === null || value === undefined) return promise
  const spelled = typeof value === 'string' || typeof value === 'number' ? String(value) : ''
  if (spelled === '') return promise
  return unit ? `${promise} — ${spelled} ${unit}` : `${promise} — ${spelled}`
}

/**
 * WHY THIS ONE — the two voices, kept apart on the screen (SPEC core tenet, D55).
 *
 * This is the organic/sponsored split arriving in front of a person, and the only rule that
 * matters here is that a reader can tell which voice they are reading. Four things do that
 * work, and each of them is a decision rather than a style:
 *
 * 1. **Each voice is its own element with its own attribution sentence, in words, above the
 *    prose it belongs to.** Not a colour, not an icon, not a tooltip: a shopper who has to
 *    hover to learn that the sentence praising this shop was written BY the shop has already
 *    read it as the platform's. The store block says Proxyshop did not write it and does not
 *    vouch for it; the platform block says Proxyshop wrote it and may only use facts it has
 *    already checked. Neither sentence is reachable without the other being on screen too.
 * 2. **The shop leads inside its own slot** — `voiceOrder`'s default and the order the
 *    service serves. That is presentation, and it is precisely what a shop buys by joining:
 *    not visibility, not a better score, the right to make its case in its own voice.
 * 3. **A seller's pitch is UNTRUSTED TEXT and is rendered as text.** It is a JSX text child,
 *    so React escapes it; there is no `dangerouslySetInnerHTML` in this file and no `<a>`
 *    anywhere in this component. This matters more than it looks: measured, the service
 *    strips NOTHING out of a store's message — `store_pitch_of` drops a blank, over-long or
 *    control-bearing message and otherwise carries the seller's bytes unchanged, while the
 *    forbidden-character screen applies to the PLATFORM's case and never the seller's. So a
 *    `<b>` and an `https://` really do arrive here, and this hop is the only one that decides
 *    whether they become markup and a link. They do not.
 * 4. **An absent voice is stated, never blank.** A scraped shop has no advocate and that is
 *    the ordinary case, not an error — so the slot says the shop has none rather than leaving
 *    a reader to conclude it had nothing to say. Likewise a shop the crawl holds nothing
 *    usable about: the platform says it has checked nothing rather than filling the space.
 *
 * WHERE IT SITS, and why: below WHAT IT IS / WHAT IT COSTS / WHAT IS PROMISED, above the
 * scores. Persuasion does not get to lead over the price and the commitments a shopper can
 * hold somebody to, and the scores below it are diagnostics rather than an argument.
 */
function PitchPanel({ slot, pitch }: { slot: ShortlistSlot; pitch: SlotPitch }) {
  const order = voiceOrder(pitch)
  const facts = pitch.facts
  // Nothing to attribute and nothing to audit. Rendering the heading alone would be the
  // empty attributed box this panel exists to never draw.
  if (order.length === 0 && facts.length === 0) return null
  const factCount = facts.length

  return (
    <section
      className="pitch"
      aria-label={`Why this one: ${slot.bid_ref}`}
      data-testid={`pitch-${slot.bid_ref}`}
    >
      <h4>Why this one</h4>

      {order.map((voice) =>
        voice === VOICE_STORE ? (
          <blockquote
            key={voice}
            className="voice"
            data-voice="store"
            data-testid={`store-voice-${slot.bid_ref}`}
          >
            <p className="voice-attribution">
              <strong>The shop&rsquo;s own words.</strong> This shop is in the network, and its
              own advocate wrote this for you. Proxyshop did not write it, has not edited it
              and does not vouch for it: it is the seller&rsquo;s case in the seller&rsquo;s
              voice, which is the thing a shop buys by joining.
            </p>
            {/* A JSX text child. Never markup, never a link, never trimmed — these are the
                store's bytes and this is the last hop before a person reads them. */}
            <p className="voice-body">{pitch.store_pitch}</p>
          </blockquote>
        ) : (
          <blockquote
            key={voice}
            className="voice"
            data-voice="platform"
            data-testid={`platform-voice-${slot.bid_ref}`}
          >
            <p className="voice-attribution">
              <strong>Proxyshop&rsquo;s case.</strong> Your own agent wrote this, and it may
              only use facts Proxyshop has already checked about this shop &mdash; it is not
              allowed to introduce one it has not. Every candidate gets this voice, including
              shops that never joined the network.
            </p>
            <p className="voice-body">{pitch.platform_case}</p>
          </blockquote>
        ),
      )}

      {order.includes(VOICE_STORE) ? null : (
        // WHAT THIS USED TO SAY, and why it could not stay. It named the missing message as
        // one of two causes — a scraped shop with no advocate, OR a real pitch "dropped at
        // the exchange boundary" because `contracts.protocol.ShortlistSlot` was
        // `extra="forbid"` with no message field — and said the page could not tell them
        // apart. The second cause is gone: `ShortlistSlot.message` exists now and carries the
        // seller's bytes verbatim, `buyer_svc.pitch.writing.store_pitch_of` reads them off it,
        // and a shop that sends a pitch has it rendered in the block above. Leaving the old
        // sentence up would have the page blaming a contract that has since been fixed for a
        // silence the shop itself chose.
        //
        // THREE causes remain and this page still does not guess between them, because from
        // here they are one value. `ShortlistSlot.message` is `null` for a shop with no
        // advocate at all, for a shop that has one and said nothing, and for a message longer
        // than the published 1200-character cap — which is refused WHOLE rather than cut,
        // because a truncated pitch is words the store did not write attributed to the store.
        // `fallback` separates the first from the other two often enough to be worth reading,
        // and it is on this card: the price-provenance line above says when the shop did not
        // answer this auction at all.
        <p className="gloss" data-testid={`no-store-voice-${slot.bid_ref}`}>
          <strong>Nothing here is in this shop&rsquo;s own voice.</strong> The exchange carries
          a shop&rsquo;s own words when it sends them, and this slot arrived with none. That
          happens three ways and this page will not guess which: Proxyshop found this shop by
          crawling, so it has no advocate, has asked for nothing and is not paying for this
          placement; or it has an advocate and chose to say nothing this time; or it wrote
          something longer than the published limit, which is refused whole rather than
          trimmed, because a shortened pitch is words the shop did not write with the
          shop&rsquo;s name on them. Either way, what you have read above is Proxyshop&rsquo;s
          own case and not the seller&rsquo;s.
        </p>
      )}

      {order.includes(VOICE_PLATFORM) ? null : (
        <p className="gloss" data-testid={`no-platform-voice-${slot.bid_ref}`}>
          Proxyshop has checked nothing about this shop that it could say out loud, so it is
          saying nothing rather than writing something plausible under its own name. What you
          have read above is the seller&rsquo;s own claim and nobody else&rsquo;s.
        </p>
      )}

      {factCount > 0 ? (
        // The facts are served IN FULL so a reader can see the copy above is a subset of what
        // was checked rather than a summary of something else. That sentence is the claim and
        // it is on the screen unopened; the enumeration behind it is the audit, and it is one
        // click rather than a hover because every fact in it is already a line on this card.
        <details data-testid={`pitch-facts-${slot.bid_ref}`}>
          <summary data-testid={`pitch-facts-summary-${slot.bid_ref}`}>
            Proxyshop checked {factCount} {factCount === 1 ? 'thing' : 'things'} about this
            shop. The case above leads with some of them; these are all of them.
          </summary>
          <dl className="facts">
            {facts.map((fact) => (
              <Fragment key={`${fact.kind}:${fact.key}`}>
                <dt data-testid={`pitch-fact-key-${slot.bid_ref}`}>{fact.key}</dt>
                <dd data-testid={`pitch-fact-value-${slot.bid_ref}`}>
                  {fact.value}{' '}
                  {/* A fact's own provenance label where it has one. `null` is a real answer
                      — the exchange's own published price and trust are not a store's claim
                      and must not borrow a store's badge — so it renders as no badge at all
                      rather than as `unverified`, which would be this page inventing a
                      verdict nobody reached. */}
                  {fact.label === null ? (
                    <span className="gloss">({fact.kind}, published by the exchange)</span>
                  ) : (
                    <span data-tone={labelTone(fact.label)}>{fact.label}</span>
                  )}
                </dd>
              </Fragment>
            ))}
          </dl>
        </details>
      ) : null}
    </section>
  )
}

/**
 * One machine row. `dt`/`dd` because it is a record, and `.facts` because that is the grid
 * this design system already dresses key/value rows with.
 */
function RecordRow({ term, children }: { term: string; children: ReactNode }) {
  return (
    <>
      <dt>{term}</dt>
      <dd>{children}</dd>
    </>
  )
}

/**
 * A recorded value, spelled the way it arrived.
 *
 * `JSON.stringify` per value rather than `String`, and for the reason `journey/wire.ts`'s
 * `describeTrust` gives for the same choice: a boolean `true` and the string `"true"` must not
 * read identically on a page whose subject is what was actually recorded. A list comes out as a
 * list rather than as its members glued together by `Array.prototype.toString`.
 */
function recordValue(value: unknown): string {
  if (value === undefined) return 'not stated'
  try {
    // A LIST IS SPELLED OUT ELEMENT BY ELEMENT, and that is a layout fix rather than a
    // preference. `JSON.stringify` writes an array with no spaces in it, so the six trust
    // dimensions arrive as one 120-character token with no break opportunity anywhere — and a
    // token that cannot break widens the grid column that holds it. MEASURED on the served
    // page at 1512px: the dimensions row ran off the right edge of the card. Each element
    // keeps its own JSON spelling, so a string still reads as a string beside a number.
    if (Array.isArray(value)) {
      return `[${value.map((item) => JSON.stringify(item) ?? 'null').join(', ')}]`
    }
    return JSON.stringify(value) ?? 'not stated'
  } catch {
    // A circular object cannot arrive over JSON, so this is unreachable through the wire and
    // is here because a caller may build a slot by hand.
    return 'not stated'
  }
}

/** How strong the network rates this kind of evidence, in the published semantics (D30). */
function authorityRankText(rank: number | null): string {
  return rank === null
    ? 'the claim named no authority rank, so how strongly this network rates the evidence is not stated'
    : `${rank} — 1 is the strongest evidence this network records, and larger numbers are weaker`
}

/**
 * WHAT PROXYSHOP OBSERVED — the platform's own half of the record (D55).
 *
 * Every row here is reachable from no bid at all. The crawl's identity and its snapshot id,
 * the trust snapshot, the published rank score and its components, and whose price this is,
 * are the PLATFORM's facts about a candidate: `contracts.protocol.ShortlistSlot` says so field
 * by field, and `exchange.ranking.candidates` writes them from the registry and the crawl
 * rather than from anything a store sent. So they are attributed to Proxyshop and they are
 * kept in their own block, away from the shop's own words below.
 *
 * The ranking is the RECORDED half of this auction and is labelled as such: the exchange
 * publishes `rank_score` and `components` once, in its `POST /auctions` answer, and this is
 * that answer read back — a different clock from the live shortlist the card was drawn from.
 */
function PlatformRecord({
  slot,
  auction,
}: {
  slot: ShortlistSlot
  auction: RecordedAuction | undefined
}) {
  const identity = slot.product?.identity
  const record = auction?.slots[slot.bid_ref]
  // The unnarrowed snapshot where the browser kept one, and the numeric-only view otherwise.
  // See `ShortlistSlot.trust_fields`: the narrowing drops most of what the exchange sent.
  const trust: Readonly<Record<string, unknown>> =
    slot.trust_fields !== undefined && Object.keys(slot.trust_fields).length > 0
      ? slot.trust_fields
      : (slot.trust_summary ?? {})
  const ranking = record?.ranking ?? null
  return (
    <div className="voice" data-voice="platform" data-testid={`record-platform-${slot.bid_ref}`}>
      <p className="voice-attribution">
        <strong>What Proxyshop observed.</strong> These are the platform&rsquo;s own records
        of this shop and this product &mdash; its crawl, its trust engine and its own auction
        &mdash; and no shop wrote a word of any of them. The one line a shop had a hand in is
        the catalogue reference: that is the exchange&rsquo;s reading of which product this bid
        was for, and it is published beside the roster row the shop was solicited on. Every
        other line here is reachable from no bid at all, so a shop cannot move one of them by
        what it says.
      </p>

      <dl className="facts">
        {identity ? (
          <>
            <RecordRow term="Name Proxyshop crawled">{identity.title}</RecordRow>
            <RecordRow term="Brand">
              {identity.brand ?? 'the crawl recorded none, and none is inferred from the title'}
            </RecordRow>
            <RecordRow term="Snapshot it was read from">
              <code className="mono">{identity.source}</code>
            </RecordRow>
            <RecordRow term="When Proxyshop saw it">
              {identity.observed_at ?? 'the snapshot named no time it was observed'}
            </RecordRow>
          </>
        ) : (
          <RecordRow term="Name Proxyshop crawled">
            Proxyshop holds no crawled snapshot for this product, so it has no name of its own
            to show and does not borrow the shop&rsquo;s.
          </RecordRow>
        )}
        <RecordRow term="Catalogue reference">
          {slot.product ? (
            <>
              <code className="mono">{slot.product.product_ref}</code>
              {slot.product.variant_ref ? (
                <>
                  {' '}
                  (variant <code className="mono">{slot.product.variant_ref}</code>)
                </>
              ) : (
                ' — the bid named no variant'
              )}
            </>
          ) : (
            'the exchange named no product for this slot'
          )}
        </RecordRow>
        <RecordRow term="Registered domain">
          {storeDomainLine(slot.store_domain)}
        </RecordRow>
        {/* THREE absences, and they are three different statements. "The record was not
            read" is about this page's own request; "the record names no such candidate" is
            about the auction; "the record named no store" is about the row. A single
            fall-through sentence would have this page assert one of them whenever any was
            true — and after a failed read it would have asserted the one thing it could not
            know. */}
        <RecordRow term="Store the exchange attributed this bid to">
          {auction === undefined
            ? 'Proxyshop’s record of this auction was not read, so this page cannot say'
            : record === undefined
              ? 'the record names no candidate with this bid reference'
              : record.store_id === ''
                ? 'the record named no store for this candidate'
                : <code className="mono">{record.store_id}</code>}
        </RecordRow>
        <RecordRow term="Where the labels above came from">
          {slot.labels_source === 'exchange'
            ? 'the exchange sent them and this page printed them'
            : slot.labels_source === 'derived'
              ? 'the exchange sent none, so the buyer service worked them out from this slot’s own claims'
              : slot.labels_source === 'absent'
                ? 'neither the exchange nor this slot’s claims named any, so it reads unverified'
                : 'the service did not say'}
        </RecordRow>
        {Object.entries(trust).map(([key, value]) => (
          <RecordRow key={`trust-${key}`} term={`Trust · ${key}`}>
            <span className="mono">{recordValue(value)}</span>
          </RecordRow>
        ))}
      </dl>

      <p className="gloss" data-testid={`record-ranking-${slot.bid_ref}`}>
        <strong>Why it ranked where it did.</strong>{' '}
        {auction === undefined
          ? 'Proxyshop’s record of this auction was not read, so the exchange’s published ranking components are not on this page. Nothing has been computed in their place.'
          : ranking === null
            ? 'The record carries no ranking for this candidate, so this page has no components to show and will not compute any of its own.'
            : `The exchange scored this candidate ${
                ranking.rank_score === null
                  ? 'with no readable score'
                  : `at ${ranking.rank_score}`
              } when the auction was opened, and published what each term of its formula contributed. Those numbers are the exchange’s, recorded at that moment; they are not re-computed for this page.`}
      </p>
      {ranking !== null && ranking.components.length > 0 ? (
        <dl className="facts">
          {ranking.components.map(([term, contribution]) => (
            <RecordRow key={term} term={term}>
              <span className="mono">{contribution}</span>
            </RecordRow>
          ))}
        </dl>
      ) : null}
    </div>
  )
}

/**
 * ONE PROMISE AND THE EVIDENCE BEHIND IT.
 *
 * The promise and its value are the SHOP's, in the shop's own field names. The evidence rows
 * beneath are PROXYSHOP's record of how that promise was obtained, and the two are labelled as
 * two things inside one row rather than run together, because a shopper who reads "envelope
 * rule, authority rank 1" as something the shop said has been handed the platform's weight
 * under the seller's name.
 *
 * `label` is NOT derived here. D30 puts the source&rarr;label map in `packages/contracts` so the
 * exchange and the buyer app cannot answer differently, and this component prints the label the
 * buyer service already attached to this promise on the card. `provenance.source` beside it is
 * the exchange's own token, rendered and never mapped &mdash; which is exactly what lets a
 * reader see the derivation instead of taking the badge on faith.
 */
function ClaimRecord({
  claim,
  label,
  bidRef,
}: {
  claim: SlotClaim
  label: string | undefined
  bidRef: string
}) {
  const provenance = claim.provenance
  return (
    <div data-testid={`record-claim-${bidRef}`}>
      <p>
        <strong>{claim.key.replace(/_/g, ' ')}</strong> &mdash;{' '}
        <span className="mono">{recordValue(claim.value)}</span>
        {claim.unit ? ` ${claim.unit}` : ''}{' '}
        {label === undefined ? null : (
          <span data-tone={labelTone(label)} data-testid={`record-claim-label-${bidRef}`}>
            {label}
          </span>
        )}
      </p>
      <dl className="facts">
        <RecordRow term="Kind of claim">
          {claim.claim_type ?? 'the exchange did not type this one'}
        </RecordRow>
        {provenance === null ? (
          <RecordRow term="Evidence">
            This promise arrived with no provenance at all, so there is nothing Proxyshop can
            show for it. That is why it reads unverified rather than being hidden.
          </RecordRow>
        ) : (
          <>
            <RecordRow term="Evidence">
              <span className="mono">{provenance.source}</span>
            </RecordRow>
            <RecordRow term="How strongly it is rated">
              {authorityRankText(provenance.authority_rank)}
            </RecordRow>
            <RecordRow term="When it was observed">
              {provenance.observed_at ?? 'the claim named no observation time'}
            </RecordRow>
            <RecordRow term="Evidence reference">
              {provenance.ref === null ? (
                'the claim pointed at nothing'
              ) : (
                <code className="mono">{provenance.ref}</code>
              )}
            </RecordRow>
          </>
        )}
      </dl>
    </div>
  )
}

/**
 * WHAT THIS SHOP SAYS ABOUT ITSELF, and what Proxyshop can show for each of it (D55).
 *
 * The sponsored half of the record. The shop's prose is not repeated here &mdash; `PitchPanel`
 * above owns it and owns its attribution &mdash; because this block is about the shop's
 * CHECKABLE assertions: the promises it made beside its price, each with the evidence the
 * platform holds for it.
 *
 * What is deliberately NOT here, said out loud so a reader does not assume the badge is more
 * than it is: the exchange decides a `verified` / `contradicted` / `unsupported` / `ambiguous`
 * verdict per claim against its own catalogue snapshot, under a MAC no bidder can compute, and
 * publishes only the AGGREGATE of those verdicts &mdash; the `verified_claim_ratio` term in the
 * ranking above. `contracts.protocol.Claim` is `additionalProperties: false` and declares no
 * verdict field, so the per-claim decisions reach no buyer-facing surface at all. The gloss
 * below says so rather than letting the provenance label be read as one.
 */
function StoreRecord({
  slot,
  auction,
}: {
  slot: ShortlistSlot
  auction: RecordedAuction | undefined
}) {
  const record = auction?.slots[slot.bid_ref]
  const claims = record?.claims ?? null
  const labelled = slot.commitments ?? []
  const discount = slot.price?.discount ?? null
  const discountProvenance = record?.discount_provenance ?? null
  return (
    <div className="voice" data-voice="store" data-testid={`record-store-${slot.bid_ref}`}>
      <p className="voice-attribution">
        <strong>What this shop says about itself.</strong> Each promise below is the
        shop&rsquo;s, in the shop&rsquo;s own words for it. Proxyshop did not write any of them
        and does not vouch for them &mdash; what it adds is the evidence line under each one,
        which says where the promise came from and how strongly this network rates that kind of
        evidence.
      </p>

      {auction === undefined ? (
        // NOT "the exchange published none". The request that would have told us either way
        // did not complete, and the difference matters most here: a shopper reading "this shop
        // promised nothing" when the truth is "this page did not manage to look" has been told
        // something about the shop that nobody established.
        <p className="gloss" data-testid={`record-no-claims-${slot.bid_ref}`}>
          Proxyshop&rsquo;s record of this auction was not read, so the evidence behind each
          promise is not on this page. The promises themselves, and the label for each, are on
          the card above and came with it.
        </p>
      ) : auction.liveness === 'forgotten' ? (
        <p className="gloss" data-testid={`record-no-claims-${slot.bid_ref}`}>
          The exchange no longer holds this auction&rsquo;s shortlist, so the promises cannot be
          re-read with their evidence attached. That is a fact about the exchange and its
          fifteen-minute window rather than about this shop, and the recorded ranking above
          survives it because the exchange published that once when the auction opened.
        </p>
      ) : record === undefined ? (
        <p className="gloss" data-testid={`record-no-claims-${slot.bid_ref}`}>
          The record names no candidate with this bid reference, so there is nothing in it to
          show the evidence for. What is on the card above still came from the exchange.
        </p>
      ) : claims === null ? (
        <p className="gloss" data-testid={`record-no-claims-${slot.bid_ref}`}>
          The exchange published no commitments for this slot. That is the ordinary answer for a
          shop the exchange stood in for: a stand-in offer is rebuilt from the roster row with an
          empty claims list, so there is nothing here the shop promised and nothing to evidence.
        </p>
      ) : claims.length === 0 ? (
        <p className="gloss" data-testid={`record-no-claims-${slot.bid_ref}`}>
          The exchange published commitments for this slot and this page could not read any of
          them. That is not the same as the shop promising nothing, and it is not being shown as
          if it were.
        </p>
      ) : (
        claims.map((claim) => (
          <ClaimRecord
            key={claim.key}
            claim={claim}
            bidRef={slot.bid_ref}
            label={labelled.find((commitment) => commitment.key === claim.key)?.label}
          />
        ))
      )}

      {discount === null ? null : (
        <div data-testid={`record-discount-${slot.bid_ref}`}>
          <p>
            <strong>{discountLine(discount)}</strong>
          </p>
          <dl className="facts">
            {discountProvenance === null ? (
              <RecordRow term="What authorised it">
                The exchange published no provenance for this discount, so the rule behind the
                depth is not on this page. It is a depth the shop states, not one Proxyshop can
                show a rule for.
              </RecordRow>
            ) : (
              <>
                <RecordRow term="What authorised it">
                  <span className="mono">{discountProvenance.source}</span>
                </RecordRow>
                <RecordRow term="How strongly it is rated">
                  {authorityRankText(discountProvenance.authority_rank)}
                </RecordRow>
                <RecordRow term="The rule">
                  {discountProvenance.ref === null ? (
                    'the discount pointed at no rule'
                  ) : (
                    <code className="mono">{discountProvenance.ref}</code>
                  )}
                </RecordRow>
              </>
            )}
          </dl>
        </div>
      )}

      <p className="gloss" data-testid={`record-verdict-gloss-${slot.bid_ref}`}>
        A badge above says what KIND of evidence a promise has, not that Proxyshop went and
        confirmed it. The exchange does decide a verdict on each of a shop&rsquo;s claims
        against its own crawl, and it publishes only the total of those verdicts &mdash; the
        <span className="mono"> verified_claim_ratio </span> term in the ranking above. The
        claim-by-claim decisions are not published to this page, and this page will not invent
        them.
      </p>
    </div>
  )
}

/**
 * THE RECORD BEHIND ONE CARD — the fold this screen did not have.
 *
 * Additive, and that word is load-bearing. Nothing that was on the card has moved in here:
 * rule 1 of this file is that a buyer's provenance is never behind a toggle, and provenance a
 * shopper has to go looking for is provenance they did not have when they chose. What is here
 * is what was reaching the browser and being rendered by nobody &mdash; the crawl's snapshot id
 * and stamp, the whole trust snapshot rather than its two numeric fields, the published rank
 * score with what each term of the formula contributed, every promise's provenance and
 * authority rank, and the rule that authorised a discount.
 *
 * `details.trace` because the design system already names this exact shape: *the RECORD folds
 * away; the LABELS never do*. A `<summary>` is keyboard-operable by construction, and the fold
 * is controlled from React state rather than left to the element so that opening it is the
 * thing that starts the read.
 *
 * TWO SOURCES, kept apart on the page. The platform block is drawn from the slot the card was
 * already given; the claims, the ranking and the discount rule come from
 * `GET /buyer/auctions/{id}`, which is why a panel can render in full while the record is still
 * arriving, or fail to fetch and still be worth reading.
 */
function SlotRecordPanel({
  slot,
  auction,
  reading,
  failure,
}: {
  slot: ShortlistSlot
  auction: RecordedAuction | undefined
  reading: boolean
  failure: string | undefined
}) {
  return (
    <>
      {reading ? (
        <p className="gloss" role="status" data-testid={`record-reading-${slot.bid_ref}`}>
          Reading Proxyshop&rsquo;s record of this auction&hellip;
        </p>
      ) : null}
      {failure === undefined ? null : (
        <p className="gloss" role="alert" data-testid={`record-failed-${slot.bid_ref}`}>
          Proxyshop&rsquo;s record of this auction could not be read, so the promises&rsquo; own
          evidence and the ranking components are missing from this panel. Everything else below
          came with the card. The service said: {failure}
        </p>
      )}
      <PlatformRecord slot={slot} auction={auction} />
      <StoreRecord slot={slot} auction={auction} />
    </>
  )
}

export function ShortlistView({
  shortlist,
  onAccept,
  accepted,
  error,
  busy = false,
  recordFetcher,
}: ShortlistViewProps) {
  const acceptedOnce = useRef(false)
  const [sent, setSent] = useState('')
  // WHICH RECORDS ARE OPEN. A list rather than one id: two cards' records side by side is the
  // comparison a shopper is actually making, and closing one to read another would take that
  // away for no reason a reader would recognise.
  const [openRecords, setOpenRecords] = useState<readonly string[]>([])
  const [record, setRecord] = useState<RecordedAuction | undefined>(undefined)
  const [recordFailure, setRecordFailure] = useState<string | undefined>(undefined)
  const [readingRecord, setReadingRecord] = useState(false)
  // ONE read per mount, whatever a reader opens. `useRef` and not state, for the reason
  // `acceptedOnce` is one: React batches nothing across an await, so two folds opened before
  // the first response lands would otherwise be two requests for the same record.
  const recordAsked = useRef(false)

  const readRecord = useCallback(async () => {
    if (recordAsked.current) return
    recordAsked.current = true
    const fetcher = recordFetcher ?? browserFetcher()
    if (fetcher === undefined) {
      setRecordFailure('there is no browser here to read the record with')
      return
    }
    setReadingRecord(true)
    try {
      setRecord(await loadRecordedAuction(shortlist.auction_id, fetcher))
    } catch (failure) {
      // The service's own words, kept. A fold that said only "could not be read" would leave a
      // reader unable to tell an expired auction from a service that is down.
      setRecordFailure(failure instanceof Error ? failure.message : String(failure))
    } finally {
      setReadingRecord(false)
    }
  }, [recordFetcher, shortlist.auction_id])

  const toggleRecord = useCallback(
    (bidRef: string) => {
      setOpenRecords((open) =>
        open.includes(bidRef) ? open.filter((each) => each !== bidRef) : [...open, bidRef],
      )
      void readRecord()
    },
    [readRecord],
  )

  const submitAccept = useCallback(
    async (slot: ShortlistSlot) => {
      // One accept per mount. React batches nothing across an await, so a second click
      // while the first is in flight would otherwise be a second checkout request.
      if (acceptedOnce.current) return
      acceptedOnce.current = true
      setSent(slot.bid_ref)
      await onAccept(slot)
    },
    [onAccept],
  )

  if (shortlist.slots.length === 0) {
    return (
      <section aria-label="Shortlist">
        <p>No store was eligible for what you asked for. Nothing has been ordered.</p>
      </section>
    )
  }

  return (
    <section aria-label="Shortlist">
      <h2>Here is what we found</h2>
      <p data-testid="slot-count">
        {shortlist.slots.length} {shortlist.slots.length === 1 ? 'option' : 'options'}, one per
        store
      </p>

      <ul aria-label="Options">
        {shortlist.slots.map((slot) => {
          const priceProvenance = priceProvenanceLine(
            slot.fallback,
            slot.fallback_reason,
            Boolean(slot.price),
          )
          const identity = slot.product?.identity
          const recordIsOpen = openRecords.includes(slot.bid_ref)
          return (
          <li key={slot.bid_ref} data-testid={`slot-${slot.bid_ref}`}>
            <h3>{slot.slot}</h3>

            {/* WHOSE SHOP IT IS — see `storeDomainLine`. Above the product and the price
                because that is the order both design documents put them in, and because it
                is the host Accept will hand the browser to. Text, never a link: there is no
                `href` anywhere in this file and this string is not the exception. */}
            <p className="store-domain" data-testid={`store-domain-${slot.bid_ref}`}>
              {storeDomainLine(slot.store_domain)}
            </p>

            {/* WHAT THE THING IS, WHAT IT COSTS, WHAT THE STORE COMMITS TO — in that order,
                above the diagnostics, because that is the order a person decides in. Each
                one renders its own absence rather than disappearing: a card that drops its
                price line when there is no price leaves a shopper comparing a store that
                quoted nothing against one that quoted, with nothing on screen saying so. */}
            <p className="item" data-testid={`product-${slot.bid_ref}`}>
              {productLine(slot.product)}
            </p>
            {/* WHOSE NAME THAT WAS. Rendered only when a name replaced the reference above,
                because it is the attribution for that name and there is nothing to attribute
                otherwise. The reference itself moves down here rather than disappearing: it
                is what Accept resolves against and it is the join a reader would need to
                check the name against the exchange's own record. */}
            {identity && slot.product ? (
              <p className="gloss" data-testid={`product-identity-${slot.bid_ref}`}>
                {/* "Proxyshop's own record", NOT "Proxyshop's own crawl". Both are true of
                    the usual case and only the first is true of all of them: `catalog_identity`
                    reads whatever snapshot the exchange holds, and that is a `neo4j-crawl:…`
                    document on a crawled shop and an operator's deployment document on a
                    configured one. Naming the crawl here would state, on a shopper's card,
                    that Proxyshop went and looked — when on some deployments an operator
                    simply wrote it down. The snapshot id below is the discriminator, which is
                    exactly what the contract says `source` is REQUIRED for. */}
                <strong>That name is Proxyshop&rsquo;s, not this shop&rsquo;s.</strong> It was
                read from Proxyshop&rsquo;s own record of this shop &mdash; the same record the
                shop&rsquo;s claims are graded against, which no shop can write to &mdash; so
                the shop did not choose the name on its own card. That record is{' '}
                <code className="mono">{identity.source}</code>
                {identity.observed_at
                  ? `, observed ${identity.observed_at}`
                  : ', which named no time it was observed'}
                . The exchange&rsquo;s reference for it is{' '}
                <code className="mono">{slot.product.product_ref}</code>
                {slot.product.variant_ref ? (
                  <>
                    {' '}
                    (variant <code className="mono">{slot.product.variant_ref}</code>)
                  </>
                ) : null}
                .
              </p>
            ) : null}
            <p className="price" data-testid={`price-${slot.bid_ref}`}>
              {priceLine(slot.price)}
            </p>
            {/* WHOSE PRICE THAT WAS — see `priceProvenanceLine`. Directly under the number,
                above the discount and the expiry, because both of those are qualities OF a
                quote and this is the line that says whether there was a quote at all. A slot
                the store really bid renders nothing here. */}
            {priceProvenance === null ? null : (
              <p className="gloss" data-testid={`price-provenance-${slot.bid_ref}`}>
                {priceProvenance}
              </p>
            )}
            {slot.price?.discount ? (
              <p className="gloss" data-testid={`discount-${slot.bid_ref}`}>
                {discountLine(slot.price.discount)}
              </p>
            ) : null}
            {slot.price?.expires_at ? (
              <p className="gloss" data-testid={`price-expiry-${slot.bid_ref}`}>
                This quote is good until {slot.price.expires_at}.
              </p>
            ) : null}

            {slot.commitments && slot.commitments.length > 0 ? (
              <ul
                aria-label={`What this store commits to: ${slot.bid_ref}`}
                data-testid={`commitments-${slot.bid_ref}`}
              >
                {slot.commitments.map((commitment) => (
                  <li key={commitment.key} data-testid={`commitment-${slot.bid_ref}`}>
                    {commitmentText(commitment)}{' '}
                    {/* The label rides beside the promise, not in a tooltip. It is the only
                        thing telling a buyer whether anyone checked this one, and a promise
                        whose evidence is a hover away is a promise they chose without. */}
                    <span
                      data-tone={labelTone(commitment.label)}
                      data-testid={`commitment-label-${slot.bid_ref}`}
                    >
                      {commitment.label}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p data-testid={`commitments-${slot.bid_ref}`}>
                No commitments — this store promised nothing alongside the price.
              </p>
            )}

            {/* WHY THIS ONE — see `PitchPanel`. Below the price and the commitments so that
                persuasion never leads over what a shopper can hold somebody to, and above
                the scores, which are diagnostics rather than an argument. `null` is the
                ordinary case and draws nothing at all. */}
            {slot.pitch ? <PitchPanel slot={slot} pitch={slot.pitch} /> : null}

            <p data-testid={`fit-${slot.bid_ref}`}>
              {slot.fit_score === undefined ? 'fit not reported' : `fit ${slot.fit_score}`}
            </p>
            <p data-testid={`trust-${slot.bid_ref}`}>{trustLine(slot)}</p>

            <ul aria-label={`Where this came from: ${slot.bid_ref}`}>
              {slotLabels(slot).map((label) => (
                <li key={label} data-tone={labelTone(label)} data-testid={`label-${slot.bid_ref}`}>
                  {label}
                </li>
              ))}
            </ul>

            {/* THE RECORD BEHIND THIS CARD — see `SlotRecordPanel`. Last, and folded, because
                everything a shopper decides on is above it and unfolded: the design system's
                own rule for this treatment is that the record folds away and the labels never
                do. `preventDefault` keeps React's state the single source of truth for whether
                it is open, so the same click that opens the fold is the one that starts the
                read; a `<summary>` answers the keyboard for free. */}
            <details className="trace" open={recordIsOpen} data-testid={`record-${slot.bid_ref}`}>
              <summary
                data-testid={`record-toggle-${slot.bid_ref}`}
                onClick={(event) => {
                  event.preventDefault()
                  toggleRecord(slot.bid_ref)
                }}
              >
                {recordIsOpen
                  ? 'Hide the record behind this one'
                  : 'See the record behind this one'}
              </summary>
              {recordIsOpen ? (
                <SlotRecordPanel
                  slot={slot}
                  auction={record}
                  reading={readingRecord}
                  failure={recordFailure}
                />
              ) : null}
            </details>

            <button
              type="button"
              onClick={() => void submitAccept(slot)}
              disabled={busy || sent !== ''}
            >
              Accept this one
            </button>
          </li>
          )
        })}
      </ul>

      {error !== undefined ? (
        <p role="alert" data-testid="accept-error">
          {error}
        </p>
      ) : null}

      {accepted !== undefined ? (
        <p data-testid="checkout-destination">
          Your checkout is at {new URL(accepted.permalink_url).hostname}. We did not choose that
          address — it came back with the store&apos;s accepted offer, and the exchange checked it
          against the store&apos;s own registered domain before showing it to you.
        </p>
      ) : null}

      <p>Nothing is ordered until you finish checkout on the store&apos;s own site.</p>
    </section>
  )
}

export default ShortlistView
