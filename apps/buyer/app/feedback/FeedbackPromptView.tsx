/**
 * The screen R14 names: one structured question about one routed order (T-073).
 *
 * Four things here are load-bearing:
 *
 * 1. **There is no `textarea` and no text `input` in this file.** Not hidden, not optional, not
 *    behind an "add a comment" toggle. R14 says *one structured prompt*, and the service refuses
 *    a `choice` outside its published option ids — so a text box here would be a field whose
 *    contents could never be recorded, collecting the buyer's own name and email address (R5) on
 *    the way to being discarded. The only inputs are radios whose values come from the service.
 * 2. **The question and every option are rendered as the service sent them.** This component
 *    maps nothing and translates nothing. The option `id` is what lands in the ledger as
 *    `reason`, so a label invented here would describe a value it does not control (D30).
 * 3. **No prompt means no form, and a reason instead.** An order the network did not route gets
 *    a sentence saying so, not a disabled form and not silence. A buyer who sees nothing cannot
 *    tell "we are not asking" from "this page is broken".
 * 4. **One submission per mount.** The double-clicked Submit is the gesture that asks for two
 *    trust observations for one purchase; the ref below is the client's half of the ledger the
 *    service keeps.
 *
 * This component performs no I/O. It renders what it is given and calls back; `feedback.ts` owns
 * the wire.
 */
import { useCallback, useRef, useState } from 'react'

import type { FeedbackPromptData, FeedbackReceipt } from './feedback'

export interface FeedbackPromptViewProps {
  /** The prompt the service published, or `null` when this order is offered none. */
  readonly prompt: FeedbackPromptData | null
  /** Why there is no prompt. Shown only when `prompt` is `null`. */
  readonly reason?: string
  /** Record one answer. Given the option id, which is the only thing that may be submitted. */
  readonly onSubmit: (choice: string) => void | Promise<void>
  /** The ledger event the answer became, once there is one. */
  readonly receipt?: FeedbackReceipt
  /** A refusal to show instead of a receipt. */
  readonly error?: string
  /** Shown while a request is in flight. */
  readonly busy?: boolean
}

export function FeedbackPromptView({
  prompt,
  reason,
  onSubmit,
  receipt,
  error,
  busy = false,
}: FeedbackPromptViewProps) {
  const answeredOnce = useRef(false)
  const [choice, setChoice] = useState('')

  const submit = useCallback(async () => {
    // One answer per mount. React batches nothing across an await, so a second click while the
    // first is in flight would otherwise be a second feedback event for one purchase.
    if (answeredOnce.current || choice === '') return
    answeredOnce.current = true
    await onSubmit(choice)
  }, [choice, onSubmit])

  if (prompt === null) {
    return (
      <section aria-label="Feedback">
        <p data-testid="no-prompt">
          {reason !== undefined && reason !== ''
            ? reason
            : 'We are not asking about this order — the network did not route it.'}
        </p>
      </section>
    )
  }

  if (receipt !== undefined) {
    return (
      <section aria-label="Feedback">
        <p data-testid="feedback-recorded">
          Thank you — that is recorded against {prompt.store_id || 'this store'} as{' '}
          {receipt.reason}. It is one observation among many, and no store sees who gave it.
        </p>
      </section>
    )
  }

  return (
    <section aria-label="Feedback">
      <form
        onSubmit={(event) => {
          event.preventDefault()
          void submit()
        }}
      >
        <fieldset>
          <legend data-testid="feedback-question">{prompt.question}</legend>
          {prompt.options.map((option) => (
            <label key={option.id} htmlFor={`fb-${option.id}`}>
              <input
                type="radio"
                id={`fb-${option.id}`}
                name={prompt.question_id}
                value={option.id}
                checked={choice === option.id}
                onChange={() => setChoice(option.id)}
                disabled={busy}
              />
              {option.label}
            </label>
          ))}
        </fieldset>

        <button type="submit" disabled={busy || choice === ''}>
          Send this
        </button>
      </form>

      {error !== undefined ? (
        <p role="alert" data-testid="feedback-error">
          {error}
        </p>
      ) : null}

      <p>
        This is the only thing we will ask you about this order, and it is the whole of it. We do
        not pass your name, your address or your email to the store.
      </p>
    </section>
  )
}

export default FeedbackPromptView
