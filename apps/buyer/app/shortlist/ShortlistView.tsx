/**
 * The screen R2 names: up to four differentiated slots, each carrying where its evidence
 * came from, and an Accept that hands off to the exchange's checkout (T-072).
 *
 * Four things here are load-bearing:
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
 * 4. **One accept per mount, and the shortlist collapses rather than pads.** A double click
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
  type ShortlistSlot,
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
            <p data-testid={`fit-${slot.bid_ref}`}>fit {slot.fit_score}</p>
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
