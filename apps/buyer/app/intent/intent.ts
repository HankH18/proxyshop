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

/**
 * One question the service actually asked, and what came of the answer.
 *
 * `used` empty is the honest form of "you told us and we could not use it". Without this
 * record the page has only `unresolved`, which says the intent is still missing something
 * and says nothing about whether anybody answered — so the one sentence available gets
 * rendered over answers the shopper really gave.
 *
 * `gap` is what was ASKED; `addressed` is what the answer turned out to be about. They come
 * apart, and a page that reads only the first states the second untruth in place of the
 * first. Measured on the served route: a row `{gap: 'budget', answer: 'It must be cherry
 * wood, and at least 48 inches wide', used: ['material is cherry-wood']}` rendered as "You
 * did answer about budget" over a sentence that names no money.
 */
export interface GapAnswer {
  readonly gap: string
  readonly question: string
  readonly answer: string
  readonly used: readonly string[]
  /** The gaps this answer actually spoke to. Empty means it produced nothing at all. */
  readonly addressed: readonly string[]
  readonly understood: boolean
}

/**
 * The gaps a record's answer actually spoke to.
 *
 * Read defensively rather than trusting the type: this page's whole job is to be true about
 * what the shopper said, and a caller one version behind must lose the field, not the
 * screen.
 */
export function gapsAnswered(record: GapAnswer): readonly string[] {
  return Array.isArray(record.addressed) ? record.addressed : []
}

/** A record chosen to speak for one open gap, and whether it actually spoke to it. */
export interface GapAnswerMatch {
  readonly gap: string
  readonly record: GapAnswer
  /** True when the answer produced something ABOUT this gap, rather than merely following
   *  the question that asked about it. The two sentences the page can honestly write. */
  readonly addressed: boolean
}

/**
 * The one record to render for `gap`, or `undefined` when the shopper was neither asked nor
 * answered.
 *
 * Two rules, and each closes a way the page told a shopper they had said nothing.
 *
 * **Prefer the answer that spoke to this gap, and take the LAST one.** `.find()` took the
 * FIRST record whose `gap` matched, so a gap answered with a hedge and then asked again —
 * which is exactly what R1's three questions are for — quoted the hedge and rendered "we
 * could not turn any of it into something stores can be filtered on" over an answer that
 * WAS extracted.
 *
 * **Fall back to the question that was ASKED.** Keying only on what an answer produced
 * orphans every row whose answer spoke to a gap that is already closed: measured, the turns
 * "I want a cherry wood table" / "It must be cherry wood, and at least 48 inches wide" /
 * "nothing else" produce two rows asked under `budget` that both address `use_case`, which
 * is closed — so neither rendered anywhere and the page said "We never got an answer about:
 * budget" to a shopper who had answered that question three times. A row always belongs
 * somewhere: to the subjects it spoke to, and failing that to the question it followed.
 */
export function answerForGap(
  records: readonly GapAnswer[],
  gap: string,
): GapAnswerMatch | undefined {
  const spoke = records.filter(
    (record) => gapsAnswered(record).includes(gap) && record.used.length > 0,
  )
  const last = spoke.at(-1)
  if (last !== undefined) return { gap, record: last, addressed: true }
  const asked = records.filter((record) => record.gap === gap).at(-1)
  return asked === undefined ? undefined : { gap, record: asked, addressed: false }
}

/**
 * How a gap is named to a shopper. The raw keys are machine tokens — `use_case` was being
 * printed at people, in both "We never got an answer about: use_case" and "You did answer
 * about use_case".
 */
const GAP_LABELS: Readonly<Record<string, string>> = {
  use_case: 'what you are shopping for',
  budget: 'your budget',
  constraints: 'must-haves',
}

export function describeGap(gap: string): string {
  return GAP_LABELS[gap] ?? gap.replace(/_/g, ' ')
}

/** A must-have the service kept but will not enforce as an eligibility filter. */
export interface SoftenedReading {
  readonly field: string
  readonly op: string
  readonly value: unknown
  readonly reason: string
  readonly source: string
  /**
   * Does the preference minted beside this reading actually score anything?
   *
   * `false` for a field a published ranking term already answers for — the exchange refuses
   * a `price_usd`, `delivery_*` or `trust` preference outright, so such a reading is kept
   * as a record and nothing more. The page must not tell the shopper it ranks.
   */
  readonly scored?: boolean
}

/** What `POST /buyer/intent/clarify` answers. `confirmed` is always `false`. */
export interface ClarifyOutcome {
  readonly questions: readonly string[]
  readonly intent: Intent
  readonly unresolved: readonly string[]
  readonly confirmed: false
  readonly understood: readonly GapAnswer[]
  readonly softened: readonly SoftenedReading[]
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
    understood: Array.isArray(payload.understood)
      ? payload.understood.filter(isGapAnswer).map(toGapAnswer)
      : [],
    softened: Array.isArray(payload.softened)
      ? payload.softened.filter(isSoftenedReading).map(toSoftenedReading)
      : [],
  }
}

/** Is this a record of a question asked and answered? */
export function isGapAnswer(value: unknown): value is GapAnswer {
  if (!isRecord(value)) return false
  return (
    isNonEmptyString(value.gap) &&
    typeof value.answer === 'string' &&
    Array.isArray(value.used)
  )
}

/**
 * One wire record, with every list present.
 *
 * `addressed` is normalised rather than required by {@link isGapAnswer}, because a service
 * that predates the field is answering an older shape of a truthful record, not a malformed
 * one — and dropping the row entirely would take the shopper's own words off the page to
 * punish a version skew. An absent list reads as "we do not know what this answer was
 * about", which {@link gapsAnswered} resolves to the gap it was asked under.
 */
function toGapAnswer(record: GapAnswer): GapAnswer {
  return {
    ...record,
    used: Array.isArray(record.used) ? record.used.filter(isNonEmptyString) : [],
    addressed: Array.isArray(record.addressed)
      ? record.addressed.filter(isNonEmptyString)
      : [],
  }
}

/**
 * One softened reading, with `scored` present.
 *
 * Absent means a service that predates the field, and the honest default is the behaviour
 * that field was added to describe for everything EXCEPT the refused fields: a reading is
 * assumed to score unless it says otherwise. The page tests `scored === false`, so an absent
 * value must be resolved here rather than at every call site.
 */
function toSoftenedReading(reading: SoftenedReading): SoftenedReading {
  return { ...reading, scored: reading.scored !== false }
}

/** Is this a must-have kept but not enforced? */
export function isSoftenedReading(value: unknown): value is SoftenedReading {
  if (!isRecord(value)) return false
  return isNonEmptyString(value.field) && isNonEmptyString(value.op)
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
  return describeTriple(constraint.field, constraint.op, constraint.value, constraint.unit)
}

/** Money, spelled the way a shopper wrote it. `1200` -> `$1,200`, `19.99` -> `$19.99`. */
function money(value: unknown): string {
  const amount = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(amount)) return String(value)
  const whole = Number.isInteger(amount)
  return `$${amount.toLocaleString('en-US', {
    minimumFractionDigits: whole ? 0 : 2,
    maximumFractionDigits: 2,
  })}`
}

/**
 * The buyer's own budget statement, from the price bounds they actually stated.
 *
 * `null` when they stated none, which is the case where the coarse band is the only budget
 * statement there is and may keep the row to itself. This exists because the confirmation
 * screen gave the heading "Budget" to the DERIVED bucket (`100-250`, no currency) and put
 * the shopper's own ceiling under "Must have" as the raw field name `price_usd at most 200`
 * — two numbers for one subject, with nothing saying how they relate.
 */
export function describeStatedBudget(
  constraints: readonly HardConstraint[],
): string | null {
  const ceiling = constraints.find((c) => c.field === 'price_usd' && c.op === 'lte')
  const floor = constraints.find((c) => c.field === 'price_usd' && c.op === 'gte')
  if (ceiling && floor) return `${money(floor.value)} to ${money(ceiling.value)}`
  if (ceiling) return `at most ${money(ceiling.value)}`
  if (floor) return `at least ${money(floor.value)}`
  return null
}

/**
 * Units the contract asks the model to put in the FIELD name rather than the value, so
 * `width_in` means inches. Read back off here, because "width in at least 48" is not a
 * sentence and "width at least 48 inches" is.
 */
const FIELD_UNITS: Readonly<Record<string, string>> = {
  in: 'inches',
  inches: 'inches',
  cm: 'cm',
  mm: 'mm',
  ft: 'feet',
  m: 'metres',
  g: 'grams',
  grams: 'grams',
  kg: 'kg',
  lb: 'lb',
  lbs: 'lb',
  oz: 'oz',
  ml: 'ml',
  l: 'litres',
  usd: 'dollars',
  days: 'days',
  hours: 'hours',
}

/**
 * A field name split into the thing being measured and the unit it is measured in.
 *
 * `width_in` -> `['width', 'inches']`; `material` -> `['material', '']`. Lives here, used by
 * every renderer below, because it did NOT: `describeSoftened` had this logic inline and
 * `describeConstraint` and `describePreference` interpolated the field raw, so the same
 * shopper saw `width at least 48 inches` under one heading and `width_in at least 48` under
 * another. Measured from lexicon output with no model in the loop: "a waterproof machine
 * washable jacket under $200" printed `machine_washable is true` under Must have, and "a
 * lightweight sustainable daypack" printed `less weight_grams (weight 0.5)` under Nice to
 * have.
 */
function fieldInWords(field: string): readonly [string, string] {
  const parts = field.split('_')
  const tail = parts.length > 1 ? FIELD_UNITS[parts[parts.length - 1]!.toLowerCase()] : undefined
  return [(tail ? parts.slice(0, -1) : parts).join(' '), tail ?? '']
}

/**
 * A value as a shopper would write it — never a JS literal.
 *
 * `true` printed as the word "true" is this page showing its insides; a shopper reads "yes".
 */
function valueInWords(value: unknown): string {
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (Array.isArray(value)) return value.map(valueInWords).join(', ')
  return String(value)
}

/** One `field op value` triple as a sentence, whatever heading it is rendered under. */
function describeTriple(field: string, op: string, value: unknown, explicitUnit?: string): string {
  const [name, tail] = fieldInWords(field)
  const unit = explicitUnit ? ` ${explicitUnit}` : tail ? ` ${tail}` : ''
  const spelled = valueInWords(value)
  switch (op) {
    case 'gte':
      return `${name} at least ${spelled}${unit}`
    case 'lte':
      return `${name} at most ${spelled}${unit}`
    case 'in':
      return `${name} one of ${spelled}${unit}`
    case 'contains':
      return `${name} includes ${spelled}${unit}`
    default:
      return `${name} is ${spelled}${unit}`
  }
}

/** A softened must-have in a sentence the buyer can check. Never called a requirement. */
export function describeSoftened(reading: SoftenedReading): string {
  return describeTriple(reading.field, reading.op, reading.value)
}

/** A preference in a sentence the buyer can check. Never called a requirement. */
export function describePreference(preference: Preference): string {
  const verb =
    preference.direction === 'maximize'
      ? 'more'
      : preference.direction === 'minimize'
        ? 'less'
        : 'preferably'
  // The field goes through `fieldInWords` here too. `less weight_grams (weight 0.5)` was
  // what a shopper who typed "something lightweight" actually read.
  const [name, unit] = fieldInWords(preference.field)
  return `${verb} ${name}${unit ? ` in ${unit}` : ''} (weight ${preference.weight})`
}
