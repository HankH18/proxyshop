/**
 * The screen R1's last clause names: the structured intent, *visible to the buyer for
 * confirmation* (T-071).
 *
 * The R1 ordering invariant has a client-side half, and this component is it: **while a
 * clarifying question is outstanding there is no confirm button in the document at all.**
 * Not disabled — absent. A disabled button is one `disabled={false}` away from opening an
 * auction for a buyer who has not finished answering, and a `disabled` attribute is not a
 * guarantee anyone can read off the screen.
 *
 * The button, once it exists, fires `onConfirm` at most once per mount. A double-clicked
 * confirm button is the exact gesture that opens two auctions for one need — the service
 * refuses the second through its confirmation ledger, and this side does not send it.
 *
 * This component performs no I/O. It renders what it is given and calls back; `intent.ts`
 * owns the wire.
 */
import { useCallback, useId, useRef, useState, type FormEvent } from 'react'

import {
  MAX_CLARIFYING_QUESTIONS,
  describeConstraint,
  describePreference,
  type Intent,
} from './intent'

export interface IntentConfirmProps {
  /** The questions asked so far, oldest first. Never more than three. */
  readonly questions: readonly string[]
  /** The buyer's answers, oldest first. One shorter than `questions` means one is open. */
  readonly answers: readonly string[]
  /** The structured intent as it currently stands. */
  readonly intent: Intent
  /** Gaps the loop could not close. Shown, never silently filled in. */
  readonly unresolved?: readonly string[]
  /** Hand the buyer's answer to whatever owns the conversation. */
  readonly onAnswer: (text: string) => void | Promise<void>
  /** The buyer confirmed. This is the only path to an auction. */
  readonly onConfirm: (intent: Intent) => void | Promise<void>
  /** Shown while a request is in flight. */
  readonly busy?: boolean
}

export function IntentConfirm({
  questions,
  answers,
  intent,
  unresolved = [],
  onAnswer,
  onConfirm,
  busy = false,
}: IntentConfirmProps) {
  const answerId = useId()
  const [draft, setDraft] = useState('')
  const confirmed = useRef(false)
  const [sent, setSent] = useState(false)

  const pending = questions.length > answers.length ? questions[questions.length - 1] : undefined

  const submitAnswer = useCallback(
    async (event: FormEvent) => {
      event.preventDefault()
      const text = draft.trim()
      if (!text) return
      setDraft('')
      await onAnswer(text)
    },
    [draft, onAnswer],
  )

  const submitConfirmation = useCallback(async () => {
    // One confirmation per mount. React batches nothing across an await, so a second
    // click while the first is in flight would otherwise be a second auction.
    if (confirmed.current) return
    confirmed.current = true
    setSent(true)
    await onConfirm(intent)
  }, [intent, onConfirm])

  if (questions.length > MAX_CLARIFYING_QUESTIONS) {
    return (
      <section aria-label="Clarification error" role="alert">
        <p>
          Something went wrong: we were asked to show {questions.length} clarifying questions and
          you were promised at most {MAX_CLARIFYING_QUESTIONS}. Nothing has been ordered.
        </p>
      </section>
    )
  }

  if (pending !== undefined) {
    return (
      <section aria-label="Clarifying question">
        <p data-testid="question-counter">
          Question {questions.length} of at most {MAX_CLARIFYING_QUESTIONS}
        </p>
        <form onSubmit={submitAnswer}>
          <label htmlFor={answerId}>{pending}</label>
          <input
            id={answerId}
            name="answer"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            disabled={busy}
          />
          <button type="submit" disabled={busy}>
            Answer
          </button>
        </form>
        <p>Nothing is ordered and no stores have been asked anything yet.</p>
      </section>
    )
  }

  return (
    <section aria-label="Confirm what we will shop for">
      <h2>Here is what we understood</h2>

      <dl>
        <dt>What you asked for</dt>
        <dd data-testid="intent-query">{intent.query}</dd>

        <dt>Budget</dt>
        <dd data-testid="intent-budget-band">
          {intent.budget_band === 'unspecified'
            ? 'you did not say - we will not filter on price'
            : intent.budget_band}
        </dd>
      </dl>

      <h3>Must have</h3>
      {intent.hard_constraints.length === 0 ? (
        <p>Nothing yet - every option in this category is eligible.</p>
      ) : (
        <ul aria-label="Requirements">
          {intent.hard_constraints.map((constraint) => (
            <li key={`${constraint.field}:${constraint.op}`}>{describeConstraint(constraint)}</li>
          ))}
        </ul>
      )}

      <h3>Nice to have</h3>
      {intent.preferences.length === 0 ? (
        <p>Nothing yet.</p>
      ) : (
        <ul aria-label="Preferences">
          {intent.preferences.map((preference) => (
            <li key={preference.field}>{describePreference(preference)}</li>
          ))}
        </ul>
      )}

      {unresolved.length > 0 ? (
        <p data-testid="unresolved">
          We never got an answer about: {unresolved.join(', ')}. We have not guessed.
        </p>
      ) : null}

      <button type="button" onClick={submitConfirmation} disabled={busy || sent}>
        Confirm and ask stores
      </button>
      <p>Nothing is ordered until you press that.</p>
    </section>
  )
}

export default IntentConfirm
