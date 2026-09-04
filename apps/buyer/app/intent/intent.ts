/**
 * Client half of R1's clarification loop (T-071).
 *
 * Mirrors `buyer_svc/intent/routes.py`. Three things here are load-bearing rather than
 * decorative, and each one is the client-side half of a rule the service also enforces:
 *
 * 1. **`confirmIntent` is the only function in this module that sends `confirmed`, and it
 *    sends the literal boolean `true`.** Never the string `"true"`, never `1`. The service
 *    types that field `StrictBool` precisely because pydantic's lax mode coerces `"yes"`,
 *    `"true"`, `"on"` and `"1"` into `true` — measured, and it opened a real auction. A
 *    client that stringifies its flag would sail through a `bool` field and be refused by
 *    the strict one, so this side spells it as a boolean and `intent.test.tsx` asserts the
 *    serialized body's `typeof`.
 * 2. **`clarifyTurns` cannot send a confirmation at all.** Its body type has no
 *    `confirmed` field, so the clarifying round-trip is structurally incapable of
 *    creating an auction — the same argument the service makes by giving `clarify()` no
 *    auction client.
 * 3. **`assertWithinQuestionCap` runs over every clarify response before the UI sees it.**
 *    The types alone cannot do this: a JSON body is `unknown` at runtime and a fourth
 *    question sails straight through a structural type. A service that regressed past R1's
 *    cap fails loudly at the network boundary instead of quietly rendering a fourth
 *    question to a buyer who was promised at most three.
 */

/** R1's cap. Three, and the client checks it rather than trusting the service. */
export const MAX_CLARIFYING_QUESTIONS = 3

/** R19's closed set of eligibility-filter comparisons. */
export const CONSTRAINT_OPS = ['eq', 'lte', 'gte', 'in', 'contains'] as const

/** R19's closed set of scoring directions. */
export const PREFERENCE_DIRECTIONS = ['maximize', 'minimize', 'prefer'] as const

export type ConstraintOp = (typeof CONSTRAINT_OPS)[number]
export type PreferenceDirection = (typeof PREFERENCE_DIRECTIONS)[number]

/** R19: an eligibility FILTER. It has no `weight`, and that is the point. */
export interface HardConstraint {
  readonly field: string
  readonly op: ConstraintOp
  readonly value: unknown
  readonly unit?: string
}

/** R19: a SCORE term. It has no `op`, and that is the point. */
export interface Preference {
  readonly field: string
  readonly direction: PreferenceDirection
  readonly weight: number
}

/** The structured intent R1 shows the buyer for confirmation. */
export interface Intent {
  readonly intent_id: string
  readonly cluster_id: string
  readonly query: string
  readonly budget_band: string
  readonly hard_constraints: readonly HardConstraint[]
  readonly preferences: readonly Preference[]
  readonly category?: string
  readonly currency?: string
  readonly created_at?: string
}

/** What `POST /buyer/intent/clarify` answers. `confirmed` is always `false`. */
export interface ClarifyOutcome {
  readonly questions: readonly string[]
  readonly intent: Intent
  readonly unresolved: readonly string[]
  readonly confirmed: false
}

/** What `POST /buyer/intent/confirm` answers. */
export interface AuctionCreated {
  readonly auction_id: string
  readonly intent_id: string
  readonly created_at: string
}

/** Anything that behaves like `fetch`. Injected so tests need no network. */
export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>

export const CLARIFY_PATH = '/buyer/intent/clarify'
export const CONFIRM_PATH = '/buyer/intent/confirm'

/** Thrown when a clarify response carries more questions than R1 permits. */
export class QuestionCapExceededError extends Error {
  constructor(readonly asked: number) {
    super(
      `R1 caps the clarification loop at ${MAX_CLARIFYING_QUESTIONS} questions; the service ` +
        `returned ${asked}. Refusing to show a question the buyer was promised would not be asked.`,
    )
    this.name = 'QuestionCapExceededError'
  }
}

/** Thrown when something that is not a structured intent is about to be confirmed. */
export class UnstructuredIntentError extends Error {
  constructor(readonly reason: string) {
    super(`R1 requires a structured intent before confirmation: ${reason}`)
    this.name = 'UnstructuredIntentError'
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

/** Is this a hard constraint R19 can express? A `weight` on one disqualifies it. */
export function isHardConstraint(value: unknown): value is HardConstraint {
  if (!isRecord(value)) return false
  if ('weight' in value && value.weight !== null && value.weight !== undefined) return false
  return (
    isNonEmptyString(value.field) &&
    CONSTRAINT_OPS.includes(value.op as ConstraintOp)
  )
}

/** Is this a preference R19 can score? */
export function isPreference(value: unknown): value is Preference {
  if (!isRecord(value)) return false
  return (
    isNonEmptyString(value.field) &&
    PREFERENCE_DIRECTIONS.includes(value.direction as PreferenceDirection) &&
    typeof value.weight === 'number' &&
    Number.isFinite(value.weight)
  )
}

/**
 * Is this the structured intent R1 asks the buyer to confirm?
 *
 * Checked at runtime, on a body that is `unknown` however the types are written. An intent
 * missing its use case or its budget band is exactly the thing R1 says must not be
 * presented for confirmation, and a structural type would let it through.
 */
export function isStructuredIntent(value: unknown): value is Intent {
  if (!isRecord(value)) return false
  if (!isNonEmptyString(value.query)) return false
  if (!isNonEmptyString(value.budget_band)) return false
  if (!isNonEmptyString(value.intent_id)) return false
  if (!Array.isArray(value.hard_constraints) || !value.hard_constraints.every(isHardConstraint)) {
    return false
  }
  return Array.isArray(value.preferences) && value.preferences.every(isPreference)
}

/** Throw unless `intent` is confirmable. The message names what is missing. */
export function assertConfirmable(intent: unknown): asserts intent is Intent {
  if (!isRecord(intent)) throw new UnstructuredIntentError('it is not an object')
  if (!isNonEmptyString(intent.query)) throw new UnstructuredIntentError('no use case')
  if (!isNonEmptyString(intent.budget_band)) throw new UnstructuredIntentError('no budget band')
  if (!isStructuredIntent(intent)) {
    throw new UnstructuredIntentError('its constraints or preferences are not R19-shaped')
  }
}

/** Throw when the service asked more than R1 permits. */
export function assertWithinQuestionCap(questions: readonly unknown[]): void {
  if (questions.length > MAX_CLARIFYING_QUESTIONS) {
    throw new QuestionCapExceededError(questions.length)
  }
}

async function readJson(response: Response, where: string): Promise<unknown> {
  if (!response.ok) {
    throw new Error(`${where} failed: HTTP ${response.status}`)
  }
  return (await response.json()) as unknown
}

/**
 * Ask the service to clarify. **Creates nothing.**
 *
 * The request body has no `confirmed` field to set, so no caller and no future edit to
 * this function can turn a clarifying round-trip into an auction.
 */
export async function clarifyTurns(
  turns: readonly string[],
  fetcher: Fetcher,
): Promise<ClarifyOutcome> {
  const body: { turns: readonly string[] } = { turns }
  const payload = await readJson(
    await fetcher(CLARIFY_PATH, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }),
    'clarify',
  )
  if (!isRecord(payload) || !Array.isArray(payload.questions)) {
    throw new Error('clarify returned no questions array')
  }
  assertWithinQuestionCap(payload.questions)
  assertConfirmable(payload.intent)
  return {
    questions: payload.questions.filter(isNonEmptyString),
    intent: payload.intent,
    unresolved: Array.isArray(payload.unresolved) ? payload.unresolved.filter(isNonEmptyString) : [],
    confirmed: false,
  }
}

/**
 * Confirm the intent and create the one auction it is entitled to.
 *
 * The only place in the client that sends `confirmed`, and it sends the boolean `true`.
 * The intent is checked first: a confirmation of something unstructured never leaves the
 * browser, so the service is not asked to refuse what the client could see was wrong.
 */
export async function confirmIntent(intent: unknown, fetcher: Fetcher): Promise<AuctionCreated> {
  assertConfirmable(intent)
  const body: { intent: Intent; confirmed: true } = { intent, confirmed: true }
  const payload = await readJson(
    await fetcher(CONFIRM_PATH, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }),
    'confirm',
  )
  if (!isRecord(payload) || !isNonEmptyString(payload.auction_id)) {
    throw new Error('confirm returned no auction id')
  }
  return {
    auction_id: payload.auction_id,
    intent_id: isNonEmptyString(payload.intent_id) ? payload.intent_id : '',
    created_at: isNonEmptyString(payload.created_at) ? payload.created_at : '',
  }
}

/** A hard constraint in a sentence the buyer can check. */
export function describeConstraint(constraint: HardConstraint): string {
  const value = Array.isArray(constraint.value)
    ? constraint.value.join(', ')
    : String(constraint.value)
  const unit = constraint.unit ? ` ${constraint.unit}` : ''
  switch (constraint.op) {
    case 'lte':
      return `${constraint.field} at most ${value}${unit}`
    case 'gte':
      return `${constraint.field} at least ${value}${unit}`
    case 'in':
      return `${constraint.field} one of ${value}${unit}`
    case 'contains':
      return `${constraint.field} includes ${value}${unit}`
    default:
      return `${constraint.field} is ${value}${unit}`
  }
}

/** A preference in a sentence the buyer can check. Never called a requirement. */
export function describePreference(preference: Preference): string {
  const verb =
    preference.direction === 'maximize'
      ? 'more'
      : preference.direction === 'minimize'
        ? 'less'
        : 'preferably'
  return `${verb} ${preference.field} (weight ${preference.weight})`
}
