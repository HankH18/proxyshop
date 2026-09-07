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
 * Six things here are load-bearing:
 *
 * 1. **Every slot's provenance labels are rendered, always.** Not on hover, not behind a
 *    "details" toggle. R2's point is that a buyer can see which claims a store stands
 *    behind before choosing, and provenance a buyer has to go looking for is provenance
 *    they did not have when they chose.
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
 *
 * This component performs no I/O. It renders what it is given and calls back; `shortlist.ts`
 * owns the wire.
 */
import { Fragment, useCallback, useRef, useState } from 'react'

import {
  VOICE_PLATFORM,
  VOICE_STORE,
  labelTone,
  slotLabels,
  voiceOrder,
  type AcceptOutcome,
  type Shortlist,
  type ShortlistPrice,
  type ShortlistProduct,
  type ShortlistSlot,
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
}

function trustLine(slot: ShortlistSlot): string {
  const summary = slot.trust_summary ?? {}
  const entries = Object.entries(summary)
  if (entries.length === 0) return 'no trust snapshot yet'
  return entries.map(([key, value]) => `${key} ${value}`).join(', ')
}

/**
 * WHAT THE THING IS.
 *
 * The exchange sends a catalogue *reference*, deliberately — a title belongs to the store's
 * catalogue and copying one through the exchange would publish a second, staler spelling of
 * a fact it does not own. So the ref is what a shopper sees, and this app does not dress it
 * up as a product name it has not resolved. Absence gets a sentence, never a blank line.
 */
function productLine(product: ShortlistProduct | null | undefined): string {
  if (!product || !product.product_ref) {
    return 'The exchange did not name a product for this slot.'
  }
  const variant = product.variant_ref
  return variant ? `${product.product_ref} — variant ${variant}` : product.product_ref
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

/** The discount the store STATES. Not a code, not an entitlement — see `SlotDiscount`. */
function discountLine(discount: SlotDiscount): string {
  const depth =
    discount.type === 'percent'
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
        // MEASURED, and the reason this sentence names TWO causes instead of the obvious
        // one. `store_pitch: null` is not "a scraped shop with no advocate" at this hop: the
        // buyer service reads it off the slot's `message`, and `contracts.protocol
        // .ShortlistSlot` is `extra="forbid"` with no message field, so a store agent's real
        // pitch is dropped at the exchange boundary before it can reach this origin. On the
        // devstack BOTH shops that bid are in-network, both have an advocate, and both come
        // back `null`. A gloss that said "this shop has no advocate" would therefore be false
        // on every card this stack can currently draw — the page does not guess which cause
        // it is looking at, because from here the two are the same value.
        <p className="gloss" data-testid={`no-store-voice-${slot.bid_ref}`}>
          <strong>Nothing here is in this shop&rsquo;s own voice.</strong> Either Proxyshop
          found this shop by crawling &mdash; in which case it has no advocate of its own, has
          not asked for anything and is not paying for this placement &mdash; or it has one
          and its message did not survive the exchange&rsquo;s published shortlist contract,
          which carries no field to put it in. This page cannot tell those two apart and will
          not guess. Either way, what you have read above is Proxyshop&rsquo;s own case and
          not the seller&rsquo;s.
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

export function ShortlistView({
  shortlist,
  onAccept,
  accepted,
  error,
  busy = false,
}: ShortlistViewProps) {
  const acceptedOnce = useRef(false)
  const [sent, setSent] = useState('')

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
        {shortlist.slots.map((slot) => (
          <li key={slot.bid_ref} data-testid={`slot-${slot.bid_ref}`}>
            <h3>{slot.slot}</h3>

            {/* WHAT THE THING IS, WHAT IT COSTS, WHAT THE STORE COMMITS TO — in that order,
                above the diagnostics, because that is the order a person decides in. Each
                one renders its own absence rather than disappearing: a card that drops its
                price line when there is no price leaves a shopper comparing a store that
                quoted nothing against one that quoted, with nothing on screen saying so. */}
            <p className="item" data-testid={`product-${slot.bid_ref}`}>
              {productLine(slot.product)}
            </p>
            <p className="price" data-testid={`price-${slot.bid_ref}`}>
              {priceLine(slot.price)}
            </p>
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

            <button
              type="button"
              onClick={() => void submitAccept(slot)}
              disabled={busy || sent !== ''}
            >
              Accept this one
            </button>
          </li>
        ))}
      </ul>

      {error !== undefined ? (
        <p role="alert" data-testid="accept-error">
          {error}
        </p>
      ) : null}

      {accepted !== undefined ? (
        <p data-testid="checkout-destination">
          Your checkout is at {new URL(accepted.permalink_url).hostname}. We did not choose that
          address — the store&apos;s exchange did.
        </p>
      ) : null}

      <p>Nothing is ordered until you finish checkout on the store&apos;s own site.</p>
    </section>
  )
}

export default ShortlistView
