/**
 * T-071 — the client half of R1: at most three questions, and nothing before confirmation.
 *
 * The Python suite proves the *service* never opens an auction early. This one proves the
 * client never asks it to: while a question is outstanding there is no confirm button in
 * the document, `clarifyTurns` has no confirmation field to send, and `confirmIntent`
 * sends the literal boolean `true` rather than a string a lax parser would coerce.
 *
 * Interaction is driven with `fireEvent` rather than `user-event`, which this workspace
 * does not carry — same as `chat/chat-shell.test.tsx`.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { IntentConfirm } from './IntentConfirm'
import {
  CONFIRM_PATH,
  MAX_CLARIFYING_QUESTIONS,
  QuestionCapExceededError,
  UnstructuredIntentError,
  assertWithinQuestionCap,
  clarifyTurns,
  confirmIntent,
  describeConstraint,
  describePreference,
  isStructuredIntent,
  type Fetcher,
  type Intent,
} from './intent'

// `@testing-library/react` registers its own cleanup only when the runner exposes
// `afterEach` globally, and this workspace's vitest projects do not set `globals: true`.
afterEach(cleanup)

const INTENT: Intent = {
  intent_id: 'int-e7-1',
  cluster_id: 'cl-e7-1',
  query: 'a light roast under $20',
  budget_band: '0-50',
  hard_constraints: [
    { field: 'roast_level', op: 'eq', value: 'light' },
    { field: 'price_usd', op: 'lte', value: 20 },
  ],
  preferences: [{ field: 'days_since_roast', direction: 'minimize', weight: 0.7 }],
  category: 'coffee',
  currency: 'USD',
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

describe('IntentConfirm', () => {
  it('shows no confirm button at all while a question is outstanding', () => {
    render(
      <IntentConfirm
        questions={['What is the most you would want to spend?']}
        answers={[]}
        intent={INTENT}
        onAnswer={vi.fn()}
        onConfirm={vi.fn()}
      />,
    )
    expect(screen.getByLabelText('What is the most you would want to spend?')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /confirm/i })).not.toBeInTheDocument()
  })

  it('counts the question against R1s cap in front of the buyer', () => {
    render(
      <IntentConfirm
        questions={['a?', 'b?']}
        answers={['one']}
        intent={INTENT}
        onAnswer={vi.fn()}
        onConfirm={vi.fn()}
      />,
    )
    expect(screen.getByTestId('question-counter')).toHaveTextContent(
      `Question 2 of at most ${MAX_CLARIFYING_QUESTIONS}`,
    )
  })

  it('hands an answer back and never confirms on the way', async () => {
    const onAnswer = vi.fn()
    const onConfirm = vi.fn()
    render(
      <IntentConfirm
        questions={['Which colour?']}
        answers={[]}
        intent={INTENT}
        onAnswer={onAnswer}
        onConfirm={onConfirm}
      />,
    )
    fireEvent.change(screen.getByLabelText('Which colour?'), { target: { value: 'navy' } })
    fireEvent.submit(screen.getByLabelText('Which colour?').closest('form')!)
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith('navy'))
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('shows the structured intent once every question is answered', () => {
    render(
      <IntentConfirm
        questions={['a?']}
        answers={['yes']}
        intent={INTENT}
        onAnswer={vi.fn()}
        onConfirm={vi.fn()}
      />,
    )
    expect(screen.getByTestId('intent-query')).toHaveTextContent('a light roast under $20')
    expect(screen.getByTestId('intent-budget-band')).toHaveTextContent('0-50')
    // `roast_level`, not `roast level`, was what this asserted. The underscore is a machine
    // token and this is the screen a shopper reads — the owner's screenshot complaint was
    // exactly this shape one field over (`price_usd at most 200` under "Must have"), and
    // `describeSoftened` in the same module has always read a field's unit suffix off. The
    // renderers now agree; the assertion is updated to the corrected wording rather than
    // deleted, so the change of contract is visible.
    expect(screen.getByLabelText('Requirements').textContent).toContain('roast level is light')
    expect(screen.getByLabelText('Preferences').textContent).toContain('days since roast')
  })

  it('says so rather than inventing a band when the buyer never named one', () => {
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={{ ...INTENT, budget_band: 'unspecified' }}
        unresolved={['budget']}
        onAnswer={vi.fn()}
        onConfirm={vi.fn()}
      />,
    )
    expect(screen.getByTestId('intent-budget-band')).toHaveTextContent('you did not say')
    expect(screen.getByTestId('unresolved')).toHaveTextContent('budget')
  })

  it('confirms exactly once however many times the button is clicked', async () => {
    const onConfirm = vi.fn()
    render(
      <IntentConfirm
        questions={[]}
        answers={[]}
        intent={INTENT}
        onAnswer={vi.fn()}
        onConfirm={onConfirm}
      />,
    )
    const button = screen.getByRole('button', { name: /confirm and ask stores/i })
    fireEvent.click(button)
    fireEvent.click(button)
    fireEvent.click(button)
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1))
    expect(onConfirm).toHaveBeenCalledWith(INTENT)
  })

  it('refuses to render a fourth question rather than showing it', () => {
    render(
      <IntentConfirm
        questions={['a?', 'b?', 'c?', 'd?']}
        answers={['1', '2', '3']}
        intent={INTENT}
        onAnswer={vi.fn()}
        onConfirm={vi.fn()}
      />,
    )
    expect(screen.getByRole('alert').textContent).toContain('Nothing has been ordered')
    expect(screen.queryByRole('button', { name: /confirm/i })).not.toBeInTheDocument()
  })
})

describe('the wire', () => {
  it('sends the confirmation as a real boolean, never a coercible string', async () => {
    const seen: RequestInit[] = []
    const fetcher: Fetcher = async (_input, init) => {
      seen.push(init ?? {})
      return jsonResponse({ auction_id: 'auc-1', intent_id: INTENT.intent_id, created_at: 'now' })
    }
    await confirmIntent(INTENT, fetcher)
    const body = JSON.parse(String(seen[0]?.body)) as Record<string, unknown>
    expect(body.confirmed).toBe(true)
    expect(typeof body.confirmed).toBe('boolean')
  })

  it('never sends a confirmation while clarifying', async () => {
    const seen: string[] = []
    const fetcher: Fetcher = async (input, init) => {
      seen.push(String(init?.body))
      return jsonResponse({ questions: ['What is your budget?'], intent: INTENT, unresolved: [] })
    }
    const outcome = await clarifyTurns(['something for espresso'], fetcher)
    expect(outcome.confirmed).toBe(false)
    expect(seen[0]).not.toContain('confirmed')
  })

  it('refuses a clarify response that broke R1s cap instead of rendering it', async () => {
    const fetcher: Fetcher = async () =>
      jsonResponse({ questions: ['a?', 'b?', 'c?', 'd?'], intent: INTENT, unresolved: [] })
    await expect(clarifyTurns(['hmm'], fetcher)).rejects.toBeInstanceOf(QuestionCapExceededError)
  })

  it('refuses to confirm an intent with no budget band', async () => {
    const fetcher = vi.fn<Fetcher>()
    await expect(
      confirmIntent({ ...INTENT, budget_band: '' }, fetcher),
    ).rejects.toBeInstanceOf(UnstructuredIntentError)
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('refuses to confirm something that is not an intent, and sends nothing', async () => {
    const fetcher = vi.fn<Fetcher>()
    for (const junk of [null, undefined, 'coffee', 42, {}, { query: 'coffee' }]) {
      await expect(confirmIntent(junk, fetcher)).rejects.toBeInstanceOf(UnstructuredIntentError)
    }
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('posts a confirmation to the confirm route and nowhere else', async () => {
    const paths: string[] = []
    const fetcher: Fetcher = async (input) => {
      paths.push(input)
      return jsonResponse({ auction_id: 'auc-1', intent_id: 'int-e7-1', created_at: 'now' })
    }
    await confirmIntent(INTENT, fetcher)
    expect(paths).toEqual([CONFIRM_PATH])
  })
})

describe('the R19 split, client side', () => {
  it('rejects a hard constraint that carries a weight', () => {
    const weighted = {
      ...INTENT,
      hard_constraints: [{ field: 'price_usd', op: 'lte', value: 20, weight: 0.5 }],
    }
    expect(isStructuredIntent(weighted)).toBe(false)
  })

  it('rejects an op R19 cannot express', () => {
    const bad = { ...INTENT, hard_constraints: [{ field: 'price_usd', op: 'lt', value: 20 }] }
    expect(isStructuredIntent(bad)).toBe(false)
  })

  it('rejects a preference with no numeric weight', () => {
    const bad = {
      ...INTENT,
      preferences: [{ field: 'rating', direction: 'maximize', weight: 'high' }],
    }
    expect(isStructuredIntent(bad)).toBe(false)
  })

  it('accepts the intent the service actually emits', () => {
    expect(isStructuredIntent(INTENT)).toBe(true)
  })

  it('never calls a preference a requirement', () => {
    expect(describePreference(INTENT.preferences[0]!)).not.toMatch(/must|require/i)
    // Was `'price_usd at most 20'`. That asserted the raw field name as the shopper-facing
    // contract — the same defect the owner screenshotted — while `describeSoftened` next
    // door already spelled `width_in` as "width … inches". Both renderers now go through
    // one `describeTriple`, so `usd` is read off as the currency it is.
    expect(describeConstraint(INTENT.hard_constraints[1]!)).toBe('price at most 20 dollars')
  })

  it('lets three questions through and refuses the fourth', () => {
    expect(() => assertWithinQuestionCap(['a', 'b', 'c'])).not.toThrow()
    expect(() => assertWithinQuestionCap(['a', 'b', 'c', 'd'])).toThrow(QuestionCapExceededError)
  })
})
