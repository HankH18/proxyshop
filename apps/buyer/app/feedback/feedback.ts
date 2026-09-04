/**
 * Client half of R14's post-purchase feedback (T-073).
 *
 * Mirrors `buyer_svc/feedback/routes.py`. Three things here are load-bearing rather than
 * decorative, and each is the client-side half of a rule the service also enforces:
 *
 * 1. **This module has no copy of the question or the options in it.** The prompt — its one
 *    question, its five option ids and the words on them — arrives from the service and is
 *    rendered as it came. A second copy here would be exactly the drift D30 exists to prevent,
 *    and it would be worse than the provenance-label case T-072 handles: an option id is what
 *    the ledger records as `reason`, so a client that invented one would put a value in the
 *    trust ledger that the service's own vocabulary does not contain.
 * 2. **This module has no copy of the routing rule either.** "Was this order network-routed?"
 *    is answered by asking `/buyer/feedback/prompt`, which returns `offered: false` and a reason
 *    for an order the network did not route. A client-side re-derivation would be a second
 *    place that decides who may leave feedback, and the two would disagree the first time the
 *    rule changed.
 * 3. **There is no free-text field anywhere in this file or its component.** Not a `textarea`,
 *    not an optional "anything else?" box, not a `comment` property on the submitted body. The
 *    service refuses a `choice` that is not one of the published option ids, and
 *    {@link submitFeedback} refuses it here too — before it leaves the browser — because the
 *    box a buyer types their own name into is the one that must not exist at all (R5).
 */

/** One option on the prompt, exactly as the service published it. */
export interface FeedbackOption {
  readonly id: string
  readonly label: string
  readonly matched_pitch: boolean
}

/** The one prompt an order is offered. A single object — R14 asks one question, not a survey. */
export interface FeedbackPromptData {
  readonly order_ref: string
  readonly store_id: string
  readonly auction_id: string
  readonly question_id: string
  readonly question: string
  readonly input_type: string
  readonly options: readonly FeedbackOption[]
}

/** What `POST /buyer/feedback/prompt` answers. */
export interface PromptOutcome {
  readonly offered: boolean
  readonly reason: string
  readonly prompt: FeedbackPromptData | null
}

/** The order, as much of it as the service reads. Named fields only — see {@link orderBody}. */
export interface FeedbackOrder {
  readonly order_ref: string
  readonly store_id?: string
  readonly auction_id?: string
  readonly routed?: boolean
  readonly status?: string
}

/** What `POST /buyer/feedback` answers: the ledger event the submission became. */
export interface FeedbackReceipt {
  readonly event_id: string
  readonly kind: string
  readonly ts: string
  readonly order_ref: string
  readonly store_id: string
  readonly auction_id: string
  readonly matched_pitch: boolean
  readonly reason: string
}

/** Anything that behaves like `fetch`. Injected so tests need no network. */
export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>

export const PROMPT_PATH = '/buyer/feedback/prompt'
export const SUBMIT_PATH = '/buyer/feedback'

/** Thrown when the answer is not one of the options the service published. */
export class UnknownChoiceError extends Error {
  constructor(
    readonly choice: string,
    readonly options: readonly string[],
  ) {
    super(
      `R14: this prompt is structured — the answer must be one of ${options.join(', ')}. ` +
        `There is no free-text option, so an answer outside that list cannot be recorded.`,
    )
    this.name = 'UnknownChoiceError'
  }
}

/** Thrown when feedback is submitted for an order the network did not route. */
export class OrderNotRoutedError extends Error {
  constructor(readonly reason: string) {
    super(
      `R14: feedback is taken only from buyers the network actually routed. ${reason} ` +
        `Nothing was recorded.`,
    )
    this.name = 'OrderNotRoutedError'
  }
}

/**
 * Thrown when the order was routed for a different buyer.
 *
 * Separate from {@link OrderNotRoutedError} because the service answers both with 403 and the
 * two are not the same news. Collapsing them told a buyer looking at somebody else's order
 * "the network did not route this order", which is false about that order and unhelpful about
 * their own.
 */
export class FeedbackNotYoursError extends Error {
  constructor(readonly detail: string) {
    super(
      `R14: this order was routed for a different buyer, so this session may not leave ` +
        `feedback about it. Nothing was recorded.`,
    )
    this.name = 'FeedbackNotYoursError'
  }
}

/**
 * Thrown when the service could not reach the ledger, or reached it and did not hear back.
 *
 * `eventId` is present when the write's outcome is UNKNOWN, and it is not decoration: the
 * service refuses a blind retry, because a sink that raised may still have written the row. A
 * retry must carry this id so the ledger writes the same row rather than a second trust
 * observation. A caller that does not have one should show the buyer that their answer may
 * already be recorded, not a Try Again button.
 */
export class LedgerUnavailableError extends Error {
  constructor(
    readonly eventId: string,
    readonly detail: string,
  ) {
    super(
      eventId === ''
        ? `The feedback ledger is unavailable, so nothing was recorded. ${detail}`
        : `The feedback ledger did not confirm the write, so whether it landed is unknown. ` +
          `Any retry must reuse event ${eventId}; a fresh submission is refused.`,
    )
    this.name = 'LedgerUnavailableError'
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/**
 * The order fields the service reads, and nothing else.
 *
 * Named fields rather than the whole record, for the reason T-072's `acceptSlot` gives about a
 * shortlist slot: an order record carries plenty the buyer's own browser was handed — an email,
 * a delivery address, a note the buyer wrote — and there is no reason to post any of it to a
 * service that would rightly ignore it. `routed` is sent only when it really is a boolean; the
 * service's `StrictBool` would answer 422 to a string, and inventing one here would be a guess.
 */
export function orderBody(order: FeedbackOrder): Record<string, unknown> {
  const body: Record<string, unknown> = { order_ref: order.order_ref }
  if (isNonEmptyString(order.store_id)) body.store_id = order.store_id
  if (isNonEmptyString(order.auction_id)) body.auction_id = order.auction_id
  if (typeof order.routed === 'boolean') body.routed = order.routed
  if (isNonEmptyString(order.status)) body.status = order.status
  return body
}

function readOption(value: unknown): FeedbackOption | null {
  if (!isRecord(value)) return null
  if (!isNonEmptyString(value.id) || !isNonEmptyString(value.label)) return null
  return {
    id: value.id,
    label: value.label,
    matched_pitch: value.matched_pitch === true,
  }
}

/**
 * Read a prompt off the service's answer, or `null`.
 *
 * A prompt with fewer than two options is `null` rather than a one-button form: R14's prompt is a
 * choice, and a "choice" with one answer is a nag with a button on it.
 */
export function readPrompt(value: unknown): FeedbackPromptData | null {
  if (!isRecord(value)) return null
  if (!isNonEmptyString(value.question) || !isNonEmptyString(value.question_id)) return null
  const options = Array.isArray(value.options)
    ? value.options.map(readOption).filter((option): option is FeedbackOption => option !== null)
    : []
  if (options.length < 2) return null
  return {
    order_ref: text(value.order_ref),
    store_id: text(value.store_id),
    auction_id: text(value.auction_id),
    question_id: value.question_id,
    question: value.question,
    input_type: text(value.input_type),
    options,
  }
}

/**
 * Ask whether this order has a feedback prompt, and get it if it does.
 *
 * Returns the whole {@link PromptOutcome} rather than just the prompt, because "no" carries a
 * reason the buyer is entitled to — for an un-routed order it says, in effect, *we did not send
 * you there*, which is the honest answer and the one that explains the absence of a form.
 */
export async function fetchFeedbackPrompt(
  order: FeedbackOrder,
  fetcher: Fetcher,
): Promise<PromptOutcome> {
  const response = await fetcher(PROMPT_PATH, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ order: orderBody(order) }),
  })
  if (!response.ok) throw new Error(`feedback prompt failed: HTTP ${response.status}`)
  const payload: unknown = await response.json()
  if (!isRecord(payload)) throw new Error('the feedback prompt response was not an object')
  const prompt = readPrompt(payload.prompt)
  if (payload.offered === true && prompt === null) {
    // The service offered a prompt this build could not read — a shape change, or a prompt with
    // fewer than two options. Reporting it as `offered: false` with the service's own empty
    // `reason` made the view fall back to "the network did not route this order", which is a
    // false statement about a routed order. Say what actually happened instead.
    return {
      offered: false,
      reason:
        'There is a feedback question for this order, but this app could not read the form it ' +
        'came in. Nothing is wrong with your order.',
      prompt: null,
    }
  }
  return {
    offered: payload.offered === true && prompt !== null,
    reason: text(payload.reason),
    prompt,
  }
}

/**
 * Submit one answer. Refuses anything that is not one of the prompt's own published options.
 *
 * The check runs here as well as in the service, and the duplication is deliberate: the service's
 * refusal is the one that holds, but this one keeps a malformed answer from ever leaving the
 * browser, and — more to the point — it is what makes it structurally impossible for a future
 * edit to this app to add a text box and have it "just work" against the API.
 */
export async function submitFeedback(
  order: FeedbackOrder,
  prompt: FeedbackPromptData,
  choice: string,
  fetcher: Fetcher,
): Promise<FeedbackReceipt> {
  const ids = prompt.options.map((option) => option.id)
  if (!ids.includes(choice)) throw new UnknownChoiceError(choice, ids)

  const response = await fetcher(SUBMIT_PATH, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      order: orderBody(order),
      // Exactly the two keys the service reads. No `comment`, no `note`, no `free_text`.
      response: { question_id: prompt.question_id, choice },
    }),
  })
  if (response.status === 403 || response.status === 503) {
    const body: unknown = await response.json().catch(() => null)
    const detail: unknown = isRecord(body) ? body.detail : null
    if (response.status === 503) {
      throw new LedgerUnavailableError(
        isRecord(detail) ? text(detail.event_id) : '',
        isRecord(detail) ? text(detail.message) : text(detail),
      )
    }
    // Two different 403s. The routing refusal answers with an object carrying `reason`; the
    // ownership refusal answers with a plain string, and reading that as "not routed" told the
    // buyer something false about the order.
    if (isRecord(detail) && isNonEmptyString(detail.reason)) {
      throw new OrderNotRoutedError(detail.reason)
    }
    throw new FeedbackNotYoursError(typeof detail === 'string' ? detail : '')
  }
  if (!response.ok) throw new Error(`feedback submission failed: HTTP ${response.status}`)

  const payload: unknown = await response.json()
  if (!isRecord(payload) || !isNonEmptyString(payload.event_id)) {
    throw new Error(
      'the feedback submission returned no ledger event id, so there is no evidence it landed',
    )
  }
  return {
    event_id: payload.event_id,
    kind: text(payload.kind),
    ts: text(payload.ts),
    order_ref: text(payload.order_ref),
    store_id: text(payload.store_id),
    auction_id: text(payload.auction_id),
    matched_pitch: payload.matched_pitch === true,
    reason: text(payload.reason),
  }
}
