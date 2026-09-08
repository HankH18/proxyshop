/**
 * Client half of the shopper's follow-up questions — `POST /buyer/chat/ask`.
 *
 * Mirrors `buyer_svc/chat/routes.py`. The journey used to end at the shortlist: a shopper
 * who wanted to know *which of these is actually third-party tested?* had nowhere to put the
 * question. This is where it goes.
 *
 * **What this module refuses to do, and why it is here rather than in the component.**
 *
 * 1. **It sends no facts.** The request body is an auction id and a question. There is no
 *    field on it for the slots on screen, and that is the whole D55 property: an answer is
 *    the PLATFORM asserting something to a shopper who is trusting it, so a page that could
 *    post its own slots could make the platform assert anything. The service fetches the
 *    material from the exchange itself.
 * 2. **It keeps the two voices apart at the network boundary.** `answer` is the platform's
 *    sentences; `shopMessages` is what each shop wrote, verbatim, under that shop's domain.
 *    `readAnswer` refuses a body whose `answer` reproduces a shop's message — the service
 *    already screens for that, and this is the second layer, because the wire layer is the
 *    one a future refactor most easily loosens. A page that quietly re-voiced a shop's
 *    advertisement as the platform's answer would have sold the organic result.
 * 3. **It runs `assertPseudonymOnly` over the body** (R5), like every other call this app
 *    makes, so a server that regressed and started returning an identity field fails loudly
 *    at the boundary instead of rendering a shopper's name into the page.
 *
 * The types alone cannot do any of this: a JSON body is `unknown` at runtime and an extra
 * key sails straight through a structural type.
 */
import { assertPseudonymOnly, type Fetcher } from './session'

/** `POST` — answers about one auction's shortlist. Creates nothing, accepts nothing. */
export const ASK_PATH = '/buyer/chat/ask'

/**
 * The longest question this page will send, matching
 * `buyer_svc.chat.answering.MAX_QUESTION_CHARS`. Restated rather than fetched because the
 * service refuses a longer one at the wire with a 422, and a box that let a shopper type
 * 4 000 characters and then reported a validation error would be a worse page than one that
 * says the limit up front.
 */
export const MAX_QUESTION_CHARS = 400

/** Which voice a piece of evidence is in. `buyer_svc.chat.evidence`'s three, minus the one
 *  that is never evidence: a shop's own prose arrives on `shopMessages`, never here. */
export type EvidenceVoice = 'platform' | 'shop_claim'

/**
 * One thing the platform holds, and where it got it.
 *
 * `attribution` is not decoration. For a `shop_claim` it is the difference between "the shop
 * says it takes returns for 30 days" and "returns are free for 30 days", and the service
 * renders it inside the same sentence as the value for exactly that reason.
 */
export interface AnswerGround {
  readonly slot: string
  readonly bid_ref: string
  readonly store_domain: string
  readonly voice: string
  readonly topic: string
  readonly key: string
  readonly value: string
  readonly attribution: string
  readonly observed_at: string
}

/** Something the question named that the platform does not hold, and what it would have needed. */
export interface NotHeld {
  readonly subject: string
  readonly detail: string
}

/** One shop's own words, whole and unedited, under that shop's domain. */
export interface ShopMessage {
  readonly slot: string
  readonly bid_ref: string
  readonly store_domain: string
  readonly message: string
}

/** One answered follow-up. */
export interface ChatAnswer {
  readonly auctionId: string
  readonly question: string
  readonly answer: string
  /** `assembled` — built in code from the grounds — or `written`, a model's screened prose. */
  readonly answerSource: string
  readonly grounds: readonly AnswerGround[]
  readonly notHeld: readonly NotHeld[]
  readonly shopMessages: readonly ShopMessage[]
  /** Whether the service still holds the exchange's recorded ranking for this auction. */
  readonly rankingRecorded: boolean
}

/** Thrown when the service answers with something this page will not render. */
export class MalformedAnswerError extends Error {
  constructor(what: string) {
    super(`the answer service returned ${what}`)
    this.name = 'MalformedAnswerError'
  }
}

/**
 * Thrown when the platform's own answer reproduces a run of a shop's message.
 *
 * The service refuses this already — `buyer_svc.chat.answering.screen_reasons` compares the
 * reply against prose the writer is never shown — so reaching this class means a regression
 * upstream, and the page fails loudly rather than printing a shop's advertisement in the
 * platform's voice.
 */
export class LaunderedVoiceError extends Error {
  constructor(readonly storeDomain: string) {
    super(`D55: the platform's answer reproduces ${storeDomain}'s own message`)
    this.name = 'LaunderedVoiceError'
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function rows(value: unknown): readonly Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : []
}

/**
 * The longest run of words the platform's answer may share with a shop's message.
 *
 * Six, the same number `buyer_svc.pitch.writing.MAX_ECHOED_WORDS` uses and the same number
 * the service screens on, so the two layers agree about what counts as quoting. Shorter runs
 * are unavoidable — both voices are describing the same product — and this is not a
 * plagiarism check; it is a check that one voice has not been substituted for the other.
 */
export const MAX_SHARED_RUN_WORDS = 6

function runsOf(text: string, size: number): Set<string> {
  const words = text
    .toLowerCase()
    .split(/\s+/)
    .map((word) => word.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, ''))
    .filter((word) => word !== '')
  const found = new Set<string>()
  for (let index = 0; index + size <= words.length; index += 1) {
    found.add(words.slice(index, index + size).join(' '))
  }
  return found
}

/** Whether `answer` reproduces a `MAX_SHARED_RUN_WORDS`-word run of `message`. */
export function sharesARun(answer: string, message: string): boolean {
  const theirs = runsOf(message, MAX_SHARED_RUN_WORDS)
  if (theirs.size === 0) return false
  for (const run of runsOf(answer, MAX_SHARED_RUN_WORDS)) {
    if (theirs.has(run)) return true
  }
  return false
}

/** Read one `POST /buyer/chat/ask` body, or throw. Exported so a test drives the parse. */
export function readAnswer(payload: unknown, fallbackAuctionId: string): ChatAnswer {
  if (!isRecord(payload)) throw new MalformedAnswerError('a body that is not an object')
  const answer = str(payload.answer).trim()
  if (answer === '') throw new MalformedAnswerError('an empty answer')

  const shopMessages: ShopMessage[] = rows(payload.shop_messages)
    .map((row) => ({
      slot: str(row.slot),
      bid_ref: str(row.bid_ref),
      store_domain: str(row.store_domain),
      message: str(row.message),
    }))
    .filter((row) => row.message !== '')

  for (const shop of shopMessages) {
    if (sharesARun(answer, shop.message)) {
      throw new LaunderedVoiceError(shop.store_domain || shop.bid_ref || 'a shop')
    }
  }

  return {
    auctionId: str(payload.auction_id) || fallbackAuctionId,
    question: str(payload.question),
    answer,
    answerSource: str(payload.answer_source),
    grounds: rows(payload.grounds).map((row) => ({
      slot: str(row.slot),
      bid_ref: str(row.bid_ref),
      store_domain: str(row.store_domain),
      voice: str(row.voice),
      topic: str(row.topic),
      key: str(row.key),
      value: str(row.value),
      attribution: str(row.attribution),
      observed_at: str(row.observed_at),
    })),
    notHeld: rows(payload.not_held)
      .map((row) => ({ subject: str(row.subject), detail: str(row.detail) }))
      .filter((row) => row.subject !== ''),
    shopMessages,
    rankingRecorded: payload.ranking_recorded === true,
  }
}

/**
 * Ask one follow-up about one auction's shortlist.
 *
 * Throws on any non-2xx, carrying the service's own `detail` where it sent one: "the
 * exchange holds no shortlist for this auction" and "this buyer service has no exchange
 * wired" are different facts and the page says which happened rather than showing one
 * shrug for both.
 */
export async function askAboutShortlist(
  auctionId: string,
  question: string,
  fetcher: Fetcher,
): Promise<ChatAnswer> {
  const asked = question.trim()
  if (asked === '') throw new MalformedAnswerError('an empty question')
  const response = await fetcher(ASK_PATH, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // Named fields, never a spread of page state: the service reads exactly these two and
    // there is nothing else this page is entitled to put in front of an answer.
    body: JSON.stringify({ auction_id: auctionId, question: asked.slice(0, MAX_QUESTION_CHARS) }),
  })
  if (!response.ok) {
    let detail = ''
    try {
      const body = (await response.json()) as unknown
      if (isRecord(body) && typeof body.detail === 'string') detail = body.detail
    } catch {
      // A non-JSON error body is not information; the status is.
    }
    throw new Error(detail || `the answer service refused with HTTP ${response.status}`)
  }
  const payload = assertPseudonymOnly((await response.json()) as unknown, 'chat answer')
  return readAnswer(payload, auctionId)
}
