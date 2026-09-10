/**
 * What the confirmation screen CLAIMS, and whether it is true (Defects A and B).
 *
 * Both of the owner's findings end up on this page, and both are sentences rather than
 * data:
 *
 * **Defect A's tail.** `IntentConfirm` renders "We never got an answer about: {gaps}. We
 * have not guessed." from `unresolved` alone, unconditionally. `unresolved` means *the
 * intent is still missing this*, which is a fact about the intent — not about whether the
 * shopper answered. A shopper who typed "It must be cherry wood, and at least 48 inches
 * wide" and had it recorded as a preference reads that they never answered. The service now
 * returns `understood`, one row per question actually asked, so the page can tell the two
 * apart; these tests pin that it does.
 *
 * **Defect B.** `budget_band` is a coarse locator snapped onto a closed published
 * vocabulary (`profile.BUDGET_BANDS`), and no band ends at 200 — so for a stated ceiling of
 * $200 the honest label really is `100-250`, and measured on the served route every stated
 * ceiling behaves this way (20 -> 0-50, 100 -> 100-250, 200 -> 100-250, 250 -> 250-500).
 * Nothing downstream reads its value: `exchange.ranking.filters.BUDGET_FIELDS` excludes it
 * from R19's eligibility wall in as many words, and `store_agent.runtime.pitch
 * .PROFILE_BUCKET_KEYS` excludes it from what a pitch may condition on. The contradiction
 * the owner screenshotted is therefore not in the number — it is that the page gives the
 * DERIVED bucket the heading "Budget" (bare `100-250`, no currency) and demotes the
 * shopper's own stated ceiling to a raw field name under a different heading. Two numbers,
 * one subject, nothing tying them together.
 *
 * The band is not narrowed and the extractor is not touched: a bespoke label like `100-200`
 * would leave the closed vocabulary that `profile._BUCKET_VOCABULARY` exempts from the
 * identity-leak scan, and `clarifier._cluster_id` hashes the band — `devstack/demo-market
 * .json` hardcodes two cluster ids in every envelope's `pursue_clusters`, so a moved band
 * makes all three demo stores decline.
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { IntentConfirm } from './IntentConfirm'
import { clarifyTurns } from './intent'
import type { GapAnswer, Intent, SoftenedReading } from './intent'

afterEach(cleanup)

const noop = () => {}

/** The owner's dialogue, as the service answers it today. Measured, not invented. */
const CHERRY_WOOD: Intent = {
  intent_id: 'int-cherry',
  cluster_id: 'cl-cherry',
  query: 'I want a cherry wood table under $200',
  budget_band: '100-250',
  hard_constraints: [{ field: 'price_usd', op: 'lte', value: 200 }],
  preferences: [
    { field: 'material', direction: 'prefer', weight: 0.3 },
    { field: 'width_in', direction: 'maximize', weight: 0.3 },
  ],
  category: 'furniture',
  currency: 'USD',
}

const ANSWER = 'It must be cherry wood, and at least 48 inches wide'

const UNDERSTOOD: GapAnswer[] = [
  {
    gap: 'constraints',
    question: 'Are there any must-haves - a size, a colour, a material - that would rule an option out?',
    answer: ANSWER,
    used: ['material is cherry-wood'],
    addressed: ['constraints'],
    understood: true,
  },
]

const SOFTENED: SoftenedReading[] = [
  {
    field: 'material',
    op: 'eq',
    value: 'cherry-wood',
    reason: 'this network has almost no readings for material',
    source: 'buyer',
  },
  {
    field: 'width_in',
    op: 'gte',
    value: 48,
    reason: 'nothing established width_in from your own words',
    source: 'model',
  },
]

describe('Defect A — the page must not claim the shopper said nothing', () => {
  it('does not say "we never got an answer" about a gap the shopper answered', () => {
    render(
      <IntentConfirm
        questions={[UNDERSTOOD[0]!.question]}
        answers={[ANSWER]}
        intent={CHERRY_WOOD}
        unresolved={['constraints']}
        understood={UNDERSTOOD}
        softened={SOFTENED}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    const unresolved = screen.queryByTestId('unresolved')
    expect(unresolved?.textContent ?? '').not.toContain('never got an answer')
  })

  it('quotes the answer back rather than losing it', () => {
    render(
      <IntentConfirm
        questions={[UNDERSTOOD[0]!.question]}
        answers={[ANSWER]}
        intent={CHERRY_WOOD}
        unresolved={['constraints']}
        understood={UNDERSTOOD}
        softened={SOFTENED}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    expect(screen.getByTestId('answered-but-open').textContent).toContain(ANSWER)
  })

  it('still says "we never got an answer" when the shopper genuinely did not', () => {
    // The control, and the one that stops this fix being a whitewash. Making the sentence
    // truthful must not make it unreachable: a gap nobody was ever asked about — the loop
    // spent its three questions elsewhere — really was never answered, and R1 says the gap
    // is named rather than guessed at.
    //
    // `questions` and `answers` are equal-length on purpose: an outstanding question puts
    // this component on the question screen, where no confirmation copy renders at all.
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={{ ...CHERRY_WOOD, budget_band: 'unspecified' }}
        unresolved={['budget']}
        understood={[]}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    expect(screen.getByTestId('unresolved').textContent).toContain('never got an answer')
    expect(screen.getByTestId('unresolved').textContent).toContain('budget')
    expect(screen.queryByTestId('answered-but-open')).toBeNull()
  })

  it('shows a softened must-have as kept-but-not-enforced, never as a requirement', () => {
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={CHERRY_WOOD}
        unresolved={[]}
        understood={[]}
        softened={SOFTENED}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    const kept = screen.getByTestId('softened')
    expect(kept.textContent).toContain('cherry-wood')
    expect(kept.textContent).toContain('48')

    // And it is NOT presented as a requirement, because it is not one — a hard constraint
    // on a field this network cannot decide empties the shortlist (measured: 0 of 3 slots).
    // The "Must have" list is absent entirely here: the only hard constraint on this intent
    // is the price bound, and that now lives in the Budget row rather than being printed
    // twice.
    expect(screen.queryByLabelText('Requirements')).toBeNull()
    expect(screen.getByText(/every option in this category is eligible/)).toBeTruthy()
    expect(kept.textContent).not.toMatch(/must|require/i)
  })
})

describe('Defect B — the budget row must not state two different numbers', () => {
  it('states the shopper\'s own ceiling under the Budget heading', () => {
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={CHERRY_WOOD}
        unresolved={[]}
        understood={[]}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    const budget = screen.getByTestId('intent-budget-band')
    // The number the shopper typed, spelled as money — not a bare bucket label whose top is
    // above the ceiling they stated.
    expect(budget.textContent).toContain('$200')
  })

  it('labels the coarse band as the bracket it is, rather than as the budget', () => {
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={CHERRY_WOOD}
        unresolved={[]}
        understood={[]}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    const bracket = screen.getByTestId('intent-budget-bracket')
    expect(bracket.textContent).toContain('100-250')
    // Whatever words are used, the shopper must be able to tell that this is a coarse
    // bracket shared with stores and not a second, contradictory budget.
    expect(bracket.textContent?.toLowerCase()).toMatch(/bracket|range|band|stores/)
  })

  it('does not print a raw machine field name at the shopper', () => {
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={CHERRY_WOOD}
        unresolved={[]}
        understood={[]}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    expect(document.body.textContent).not.toContain('price_usd')
  })

  it('keeps the unspecified branch, which is the only budget statement there is', () => {
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={{ ...CHERRY_WOOD, budget_band: 'unspecified', hard_constraints: [] }}
        unresolved={[]}
        understood={[]}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    expect(screen.getByTestId('intent-budget-band').textContent).toContain('you did not say')
  })
})

describe('the regressions the first repair introduced', () => {
  /**
   * The service asked about BUDGET and the shopper answered about MATERIAL. Measured on
   * the served route (`POST /buyer/intent/clarify`, turns "I want a cherry wood table" /
   * "It must be cherry wood, and at least 48 inches wide" / "no, that is everything" /
   * "nothing else"), the row that came back was:
   *
   *     {gap: 'budget', answer: 'It must be cherry wood, and at least 48 inches wide',
   *      used: ['material is cherry-wood'], understood: true}
   *
   * A row keyed by the question it followed rather than by what the answer produced makes
   * the page assert "You did answer about budget" over a sentence that names no money at
   * all — the same class of untruth the row was added to end.
   */
  it('files an answer under the gap it addressed, not the gap it was asked under', () => {
    const misfiled: GapAnswer[] = [
      {
        gap: 'budget',
        question: 'What is the most you would want to spend?',
        answer: ANSWER,
        used: ['material is cherry-wood'],
        addressed: ['constraints'],
        understood: true,
      },
    ]
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={{ ...CHERRY_WOOD, budget_band: 'unspecified', hard_constraints: [] }}
        unresolved={['budget', 'constraints']}
        understood={misfiled}
        softened={SOFTENED}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    const answered = screen.getByTestId('answered-but-open')
    const text = answered.textContent ?? ''
    // The material sentence is credited to must-haves, WITH what it produced — and it says
    // "you did tell us", not "when we asked", because nobody asked about must-haves. The
    // row is selected by what the answer PRODUCED, so it lands under subjects that were
    // never put to the shopper, and claiming they were is its own untruth.
    expect(text).toContain('You did tell us about must-haves')
    expect(text).not.toContain('asked about must-haves')
    expect(text).toContain('We kept material is cherry-wood')
    // Nothing claims another question is coming: this is the confirmation screen, and on
    // the owner's own dialogue the loop has already spent all three of R1's questions.
    expect(text).not.toContain('still asking')
    // ...and the budget row, which the same sentence followed but never answered, says so
    // rather than either claiming it was answered or claiming it was never answered.
    expect(text).toContain('None of it told us about your budget')
    expect(screen.queryByTestId('unresolved')).toBeNull()
    // And no machine token reaches the shopper.
    expect(document.body.textContent).not.toContain('use_case')
    expect(document.body.textContent).not.toContain('constraints.')
  })

  /**
   * The first repair keyed a row ONLY on what the answer produced, so any answer that spoke
   * to a gap already closed rendered nowhere at all. Measured on the served route: the turns
   * "I want a cherry wood table" / "It must be cherry wood, and at least 48 inches wide" /
   * "nothing else" produced two rows asked under `budget`, both addressing `use_case` — and
   * `use_case` is closed, so the page said "We never got an answer about: budget" to a
   * shopper who had answered that question three times.
   */
  it('still reports an answer whose only subject is a gap that is already closed', () => {
    const orphan: GapAnswer[] = [
      {
        gap: 'budget',
        question: 'What is the most you would want to spend?',
        answer: 'it is for my apartment balcony',
        used: [],
        addressed: [],
        understood: false,
      },
    ]
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={{ ...CHERRY_WOOD, budget_band: 'unspecified', hard_constraints: [] }}
        unresolved={['budget']}
        understood={orphan}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    expect(screen.queryByTestId('unresolved')).toBeNull()
    expect(screen.getByTestId('answered-but-open').textContent).toContain(
      'it is for my apartment balcony',
    )
  })

  /**
   * "We kept price usd lte 300.0, material is oak" was rendered at a shopper — the raw
   * field, the raw op and a float, from `_terms_of` in `extraction.py`. The pre-existing
   * guard against this only asserted `not.toContain('price_usd')` and rendered
   * `understood={[]}`, so it never saw this path.
   */
  it('never prints a raw machine term inside what we kept', () => {
    const kept: GapAnswer[] = [
      {
        gap: 'budget',
        question: 'What is the most you would want to spend?',
        answer: 'oak, under $300',
        used: ['a budget of at most $300', 'material is oak'],
        addressed: ['budget', 'constraints'],
        understood: true,
      },
    ]
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={CHERRY_WOOD}
        unresolved={['constraints']}
        understood={kept}
        softened={SOFTENED}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    const text = document.body.textContent ?? ''
    expect(text).not.toContain('price_usd')
    expect(text).not.toContain('price usd')
    expect(text).not.toMatch(/\b(lte|gte|eq)\b/)
    // And the paragraph must not claim everything is still eligible while the shopper's
    // own $300 ceiling is an active hard constraint.
    expect(text).not.toContain('every option in this category is still eligible')
  })

  /**
   * `exchange.retrieval.criteria.build_query` refuses every `price_usd`, `delivery_*` and
   * `trust` preference (`contracts.ranking.preference_term_conflict`), so a softened
   * reading on one of those fields neither filters nor ranks. The page said "we use them to
   * rank rather than to exclude" over it.
   */
  it('does not claim a reading ranks when the network refuses to score it', () => {
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={{ ...CHERRY_WOOD, preferences: [] }}
        unresolved={[]}
        understood={[]}
        softened={[
          {
            field: 'price_usd',
            op: 'lte',
            value: 300,
            reason: 'nothing in this service established price_usd 300 from your own words',
            source: 'model',
            scored: false,
          },
        ]}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    const kept = screen.getByTestId('softened')
    expect(kept.textContent).toContain('changes nothing here')
    expect(document.body.textContent).not.toContain('to rank rather than to exclude')
  })

  /**
   * `openAndAnswered` used `.find()`, so the FIRST row for a gap won. A gap answered with
   * a hedge and then asked again is exactly the case R1's three questions exist for, and
   * the first row is the hedge — so the page renders "We could not turn any of it into
   * something stores can be filtered on" over an answer that WAS extracted.
   */
  it('quotes the answer that produced something, not the earlier hedge', () => {
    const twice: GapAnswer[] = [
      {
        gap: 'constraints',
        question: 'Are there any must-haves?',
        answer: 'not sure, something nice',
        used: [],
        addressed: [],
        understood: false,
      },
      {
        gap: 'constraints',
        question: 'Anything non-negotiable?',
        answer: ANSWER,
        used: ['material is cherry-wood'],
        addressed: ['constraints'],
        understood: true,
      },
    ]
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={CHERRY_WOOD}
        unresolved={['constraints']}
        understood={twice}
        softened={SOFTENED}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    const answered = screen.getByTestId('answered-but-open')
    expect(answered.textContent).toContain(ANSWER)
    expect(answered.textContent).toContain('material is cherry-wood')
    expect(answered.textContent).not.toContain('could not turn any of it')
  })

  /**
   * The demo's own S1 dialogue. "around $40" states a band and no ceiling, so
   * `describeStatedBudget` returns null and the row fell back to `around {band}` — while
   * the parenthetical still disclosed the same band as though it were a coarser second
   * fact. Measured through `clarify(['a warm merino wool beanie for winter','around $40'])`:
   * band `0-50`, zero `price_usd` constraints.
   */
  it('does not print the same bracket twice when the shopper only hedged', () => {
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={{ ...CHERRY_WOOD, budget_band: '0-50', hard_constraints: [] }}
        unresolved={[]}
        understood={[]}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    const row = screen.getByTestId('intent-budget-band')
    const occurrences = (row.textContent ?? '').split('0-50').length - 1
    expect(occurrences).toBe(1)
  })
})

describe('nothing on this page is a machine token', () => {
  /**
   * `_terms_of` on the service was fixed and `describeConstraint` / `describePreference`
   * here were not, so the same shopper read `width at least 48 inches` under one heading
   * and `width_in at least 48` under another. Measured from LEXICON output with no model in
   * the loop, through `POST /buyer/intent/clarify` and then this page's own renderers:
   *
   *   "a waterproof machine washable jacket under $200"   -> `machine_washable is true`
   *   "a lightweight sustainable daypack in navy under $150" -> `less weight_grams (weight 0.5)`
   *
   * The existing guard could not see either: it asserts over `understood[].used`, which is
   * the one path that was fixed.
   */
  it('renders no underscored field, JS literal or stranded unit anywhere on the screen', () => {
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={{
          ...CHERRY_WOOD,
          hard_constraints: [
            { field: 'price_usd', op: 'lte', value: 200 },
            { field: 'machine_washable', op: 'eq', value: true },
            { field: 'width_in', op: 'gte', value: 48 },
            { field: 'delivery_days', op: 'lte', value: 7 },
            { field: 'material', op: 'in', value: ['oak', 'walnut'] },
          ],
          preferences: [
            { field: 'weight_grams', direction: 'minimize', weight: 0.5 },
            { field: 'days_since_roast', direction: 'minimize', weight: 0.5 },
            { field: 'width_in', direction: 'maximize', weight: 0.8 },
          ],
        }}
        unresolved={[]}
        understood={[]}
        softened={[
          {
            field: 'weight_grams',
            op: 'lte',
            value: 500,
            reason: 'nothing established weight_grams from your own words',
            source: 'model',
            scored: true,
          },
          {
            field: 'waterproof',
            op: 'eq',
            value: true,
            reason: 'nothing established waterproof from your own words',
            source: 'model',
            scored: true,
          },
        ]}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    // Asserted per-<li>, NOT over `document.body.textContent`. React concatenates adjacent
    // siblings with no separator, so a rendered `machine washable is true` immediately
    // followed by the `Nice to have` heading reads as `...is trueNice to have...` — `\b`
    // needs a word/non-word transition and `e`->`N` is not one, so `/\b(true|false)\b/`
    // over the whole body cannot fail however wrong the page is. Measured: reducing
    // `valueInWords` to `String(value)` put "machine washable is true" and "waterproof is
    // true" on screen and left the whole suite green.
    const lines = [...document.querySelectorAll('li')].map((li) => li.textContent ?? '')
    expect(lines.length).toBeGreaterThan(4)
    for (const line of lines) {
      expect(line).not.toMatch(/[a-z]_[a-z]/)
      expect(line).not.toMatch(/\b(true|false)\b/)
      // Array values are joined for a reader, not by `Array.prototype.toString`.
      expect(line).not.toMatch(/\w,\w/)
    }
    const text = document.body.textContent ?? ''
    expect(text).not.toMatch(/[a-z]_[a-z]/)
    // A unit suffix stranded mid-phrase: `weight grams at most 500` instead of
    // `weight at most 500 grams`.
    for (const stranded of ['weight grams', 'width in at', 'delivery days at', 'price usd']) {
      expect(text).not.toContain(stranded)
    }
    // And the units are actually read back off rather than dropped.
    expect(text).toContain('inches')
    expect(text).toContain('grams')
  })

  /**
   * `toSoftenedReading` normalises an absent `scored` to `true`. A service one version
   * behind sends no `scored`, and `reading.scored === false` would then be false anyway —
   * so this pins the NORMALISER rather than the render, by going through `clarifyTurns`.
   */
  it('defaults a missing `scored` rather than leaving it undefined', async () => {
    const payload = {
      questions: [],
      intent: CHERRY_WOOD,
      unresolved: [],
      confirmed: false,
      understood: [],
      softened: [
        { field: 'material', op: 'eq', value: 'linen', reason: 'r', source: 'buyer' },
        { field: 'price_usd', op: 'lte', value: 300, reason: 'r', source: 'model', scored: false },
      ],
    }
    const outcome = await clarifyTurns(['a linen duvet cover'], async () => ({
      ok: true,
      status: 200,
      json: async () => payload,
    }) as unknown as Response)
    expect(outcome.softened[0]!.scored).toBe(true)
    expect(outcome.softened[1]!.scored).toBe(false)
  })
})

describe('the opening clause names the question that was really asked', () => {
  /**
   * The clause is a ternary on `record.gap === gap`, and only the DIFFERING-gap direction
   * was asserted. Forcing it to always render "You did tell us about X" — so the page never
   * acknowledges the question it really asked — left the whole suite green.
   */
  it('says "when we asked" for the gap the question was about', () => {
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={{ ...CHERRY_WOOD, budget_band: 'unspecified', hard_constraints: [] }}
        unresolved={['budget']}
        understood={[
          {
            gap: 'budget',
            question: 'What is the most you would want to spend?',
            answer: 'no idea honestly',
            used: [],
            addressed: [],
            understood: false,
          },
        ]}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    expect(screen.getByTestId('answered-but-open').textContent).toContain(
      'You did answer when we asked about your budget',
    )
  })

  /**
   * Price is filtered out of the "Must have" list so the shopper's own ceiling appears once,
   * under Budget. It came back through `used`: measured, "oak, under $300" printed
   * `Budget: at most $300` and then "We kept a budget of at most $300, material is oak".
   */
  it('does not repeat the budget the row above already states', () => {
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={{
          ...CHERRY_WOOD,
          budget_band: '250-500',
          hard_constraints: [{ field: 'price_usd', op: 'lte', value: 300 }],
        }}
        unresolved={['constraints']}
        understood={[
          {
            gap: 'budget',
            question: 'What is the most you would want to spend?',
            answer: 'oak, under $300',
            used: ['a budget of at most $300', 'material is oak'],
            addressed: ['budget', 'constraints'],
            understood: true,
          },
        ]}
        softened={SOFTENED}
        onAnswer={noop}
        onConfirm={noop}
      />,
    )
    const answered = screen.getByTestId('answered-but-open').textContent ?? ''
    expect(answered).toContain('material is oak')
    expect(answered).not.toContain('a budget of at most $300')
    // The Budget row still states it, once.
    expect(screen.getByTestId('intent-budget-band').textContent).toContain('$300')
  })
})
