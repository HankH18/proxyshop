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
 * Five things here are load-bearing:
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
 * 5. **One accept per mount, and the shortlist collapses rather than pads.** A double click
 *    on Accept is the gesture that asks for two checkouts for one auction; the ref below is
 *    the client's half of the ledger the service keeps. And a shortlist with one eligible
 *    store renders one slot — there is no padding-out with a store that failed a filter.
 *
 * This component performs no I/O. It renders what it is given and calls back; `shortlist.ts`
 * owns the wire.
 */
import { useCallback, useRef, useState } from 'react'

import {
  labelTone,
  slotLabels,
  type AcceptOutcome,
  type Shortlist,
  type ShortlistPrice,
  type ShortlistProduct,
  type ShortlistSlot,
  type SlotCommitment,
  type SlotDiscount,
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
