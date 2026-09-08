/**
 * The one seeded prompt the journey is allowed to render, and the check that it is seeded.
 *
 * WHY THIS FILE EXISTS. `apps/buyer/app/feedback/` is built, tested and mounted by nothing:
 * the served journey ends at the checkout handoff and never obtains an order reference, so
 * there is no real prompt to show. Rather than leave the panel invisible, the demo shows a
 * manufactured one — and the whole difference between *seed data* and *fake data* is that a
 * reader can tell which they are looking at, from the record itself, without being told.
 *
 * THE MARKER. `order_ref` begins with the prefix the artifact's own `collection.json`
 * declares (`marker.prefix`, `sim-fb-`). That is not decoration and not a flag this module
 * invents:
 *
 *  * `services/sim/seed/shoppers.py` mints it and `seed.population._Services.submit` refuses
 *    to send anything without it, before the first byte leaves;
 *  * `apps/buyer/svc/src/feedback/submission.py` copies `order_ref` VERBATIM onto the sealed
 *    ledger event, beside `event_hash` and `prev_hash`;
 *  * so it is inside the trust ledger's hash chain. An answer submitted from this panel stays
 *    marked as manufactured for as long as the chain exists, and cannot be un-marked without
 *    breaking `GET /events/verify`.
 *
 * The prefix is read OUT OF the artifact rather than spelled here, so this reader and the
 * Python writer cannot drift apart: if the artifact ever declares a different marker, this
 * module enforces that one. What is hard-coded is only the refusal — a prompt that does not
 * carry its own artifact's declared prefix is not rendered at all.
 *
 * WHY THE CHECK IS A REFUSAL AND NOT A LABEL. A seeded row that renders unlabelled is the
 * failure this whole arrangement exists to prevent, and a label is a promise a future edit
 * can quietly drop. Returning `null` means the panel disappears rather than showing an
 * unmarked prompt: the safe direction is "no demo beat", never "an unmarked one".
 */

import collection from '../feedback/seed-data/collection.json'
import prompt from '../feedback/seed-data/prompt.json'
import type { FeedbackOrder, FeedbackPromptData } from '../feedback/feedback'

/** Why a seeded prompt was refused, or `undefined` when it was accepted. */
export type SeedRefusal = string | undefined

/**
 * The marker the artifact declares about itself: which field carries it, and what it starts
 * with. Read as `unknown` and narrowed, because a JSON import is typed off the bytes that
 * happened to be on disk at build time and a `collection.json` written by an older layout
 * must be refused rather than trusted.
 */
interface DeclaredMarker {
  readonly field: string
  readonly prefix: string
}

function declaredMarker(record: unknown): DeclaredMarker | undefined {
  if (typeof record !== 'object' || record === null) return undefined
  const marker = (record as { marker?: unknown }).marker
  if (typeof marker !== 'object' || marker === null) return undefined
  const { field, prefix } = marker as { field?: unknown; prefix?: unknown }
  if (typeof field !== 'string' || field === '') return undefined
  if (typeof prefix !== 'string' || prefix === '') return undefined
  return { field, prefix }
}

/**
 * `[prompt, undefined]` when the artifact is seeded and self-consistent, `[null, why]` when
 * it is not. Never throws: a malformed artifact must cost the demo one panel, not the page.
 */
export function readSeededPrompt(
  record: unknown = collection,
  body: unknown = prompt,
): readonly [FeedbackPromptData | null, SeedRefusal] {
  const marker = declaredMarker(record)
  if (marker === undefined) {
    return [null, 'this seed artifact declares no marker, so nothing about it can be checked']
  }
  // The marker must live on `order_ref` specifically. A `collection.json` naming some other
  // field would be describing a different artifact than the one the buyer service seals into
  // the hash chain, and honouring it would move the check off the only field that is sealed.
  if (marker.field !== 'order_ref') {
    return [null, `this seed artifact marks '${marker.field}', but only 'order_ref' is sealed`]
  }
  if (typeof body !== 'object' || body === null) {
    return [null, 'this seed artifact carries no prompt']
  }
  const orderRef = (body as { order_ref?: unknown }).order_ref
  if (typeof orderRef !== 'string' || !orderRef.startsWith(marker.prefix)) {
    return [
      null,
      `refusing to render ${String(orderRef)} as seeded: it does not carry the ` +
        `'${marker.prefix}' prefix its own artifact declares`,
    ]
  }
  return [body as FeedbackPromptData, undefined]
}

const [seeded, refusal] = readSeededPrompt()

/** The seeded prompt, or `null` when the artifact failed its own marker check. */
export const SEEDED_PROMPT: FeedbackPromptData | null = seeded

/** Why there is no seeded prompt, when there is none. */
export const SEEDED_REFUSAL: SeedRefusal = refusal

/** The prefix the artifact declares, for the panel to state on the page. */
export const SEEDED_PREFIX: string = declaredMarker(collection)?.prefix ?? '(none declared)'

/**
 * The order the seeded answer would be recorded against.
 *
 * `routed` is deliberately omitted rather than asserted `true`: the buyer service decides
 * whether this exchange routed the order, and a client that claimed it did would be making
 * the one claim the route exists to check. Submitting this prompt therefore reaches the real
 * `POST /buyer/feedback` and is answered by it honestly — including a refusal.
 */
export function seededOrder(from: FeedbackPromptData): FeedbackOrder {
  return { order_ref: from.order_ref, store_id: from.store_id, auction_id: from.auction_id }
}
