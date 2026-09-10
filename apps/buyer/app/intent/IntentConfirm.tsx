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
  answerForGap,
  describeConstraint,
  describeGap,
  describePreference,
  describeSoftened,
  describeStatedBudget,
  type GapAnswer,
  type GapAnswerMatch,
  type Intent,
  type SoftenedReading,
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
  /**
   * One record per question actually put to the buyer, and what came of their answer.
   *
   * This is what makes "We never got an answer about: X" a checkable claim. `unresolved`
   * says the intent is still missing something, which is true of a shopper who answered
   * "no idea" and equally true of one who never came back — and only this list can tell
   * them apart. Rendering the same sentence for both is what told a shopper who typed "It
   * must be cherry wood, and at least 48 inches wide" that they had never answered.
   */
  readonly understood?: readonly GapAnswer[]
  /**
   * Must-haves the service kept but will not enforce as filters, with the reason.
   *
   * Shown under their own heading rather than folded into "Must have", because they are
   * not requirements: a hard constraint on an attribute this network holds no readings for
   * excludes every store rather than narrowing the list (measured: 0 of 3 shortlist slots
   * for `material eq cherry-wood`, 3 of 3 for the same thing as a preference).
   */
  readonly softened?: readonly SoftenedReading[]
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
  understood = [],
  softened = [],
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

  // What the shopper actually said about money, and the coarse bracket derived from it.
  // These are two statements about one subject and the page used to show them under two
  // headings with nothing connecting them: `100-250` under "Budget" (a bucket whose top is
  // ABOVE the stated ceiling, with no currency on it) and `price_usd at most 200` under
  // "Must have". A shopper read two different numbers and no way to reconcile them.
  //
  // The band is not wrong and is deliberately not narrowed: `BUDGET_BANDS` is a closed
  // published vocabulary, no band ends at 200, and `clarifier._cluster_id` hashes the band
  // that `devstack/demo-market.json` pins in every envelope's `pursue_clusters`. What was
  // wrong is which of the two wore the heading a shopper trusts.
  const statedBudget = describeStatedBudget(intent.hard_constraints)
  const requirements = intent.hard_constraints.filter(
    // Price has its own row above, in the shopper's own terms. Listing it here as well was
    // the second half of the same number appearing twice.
    (constraint) => constraint.field !== 'price_usd',
  )
  const preferences = intent.preferences.filter(
    (preference) => !softened.some((reading) => reading.field === preference.field),
  )
  // One open gap, the record that answered it, and nothing else. `answerForGap` prefers the
  // answer that PRODUCED something about this gap — the service's own `addressed` — and
  // falls back to the question that was asked, because the two come apart and the page is
  // what states the difference out loud. Measured: a record `{gap: 'budget', answer: 'It
  // must be cherry wood, and at least 48 inches wide'}` rendered "You did answer about
  // budget" over a sentence with no money in it, and the gap it really answered was
  // reported as unanswered beside it.
  // What an answer produced, minus anything the Budget row above already states in the
  // shopper's own terms. Price was deliberately filtered out of `requirements` to stop the
  // same number appearing under two headings, and it came straight back in through `used`:
  // measured, "oak, under $300" printed `Budget: at most $300` and then "We kept a budget of
  // at most $300, material is oak". The prefix is the one `extraction._describe_constraint`
  // emits for `price_usd`, and the two are pinned together by
  // `test_nothing_the_page_prints_back_is_a_machine_term` on one side and by this page's
  // own test on the other.
  const keptTerms = (record: GapAnswer): readonly string[] => {
    const kept = statedBudget === null ? record.used : record.used.filter(
      (term) => !term.startsWith('a budget of'),
    )
    return kept.length > 0 ? kept : record.used
  }
  const openAndAnswered = unresolved
    .map((gap) => answerForGap(understood, gap))
    .filter((match): match is GapAnswerMatch => match !== undefined)
  const openAndUnanswered = unresolved.filter(
    (gap) => answerForGap(understood, gap) === undefined,
  )

  return (
    <section aria-label="Confirm what we will shop for">
      <h2>Here is what we understood</h2>

      <dl>
        <dt>What you asked for</dt>
        <dd data-testid="intent-query">{intent.query}</dd>

        <dt>Budget</dt>
        <dd data-testid="intent-budget-band">
          {intent.budget_band === 'unspecified' ? (
            'you did not say - we will not filter on price'
          ) : statedBudget === null ? (
            // The shopper hedged ("around $40"), so the band is the ONLY budget statement
            // there is and it wears the heading on its own. The parenthetical used to
            // render here too, disclosing the same band a second time as though it were a
            // coarser fact about a finer one — `around 0-50 (stores are also told your
            // rough bracket: 0-50)` on the demo's own S1 dialogue, measured through
            // `clarify(['a warm merino wool beanie for winter', 'around $40'])`.
            `around ${intent.budget_band}`
          ) : (
            <>
              {statedBudget}{' '}
              <span data-testid="intent-budget-bracket">
                (stores are also told your rough bracket: {intent.budget_band}, which is
                coarser than the figure above)
              </span>
            </>
          )}
        </dd>
      </dl>

      <h3>Must have</h3>
      {requirements.length === 0 ? (
        <p>Nothing yet - every option in this category is eligible.</p>
      ) : (
        <ul aria-label="Requirements">
          {requirements.map((constraint) => (
            <li key={`${constraint.field}:${constraint.op}`}>{describeConstraint(constraint)}</li>
          ))}
        </ul>
      )}

      {softened.length > 0 ? (
        <>
          <h3>Noted, but not used to rule anything out</h3>
          <ul aria-label="Kept but not enforced" data-testid="softened">
            {/* `scored === false` is the service saying this reading changes NOTHING — the
                exchange refuses a preference on any field a published ranking term already
                answers for (price, delivery, trust), so it is neither filtering nor
                ranking. Saying "we use them to rank" over one of those was false, and the
                shopper had no way to tell which of these lines did anything. */}
            {softened.map((reading) => (
              <li key={`${reading.field}:${reading.op}`}>
                {describeSoftened(reading)}
                {reading.scored === false
                  ? ' - recorded only; this network judges that from the offers themselves, so it changes nothing here'
                  : ''}
              </li>
            ))}
          </ul>
          <p>
            We could not turn these into a filter stores can be measured against, so we have not
            used any of them to rule an option out. Requiring them would have hidden every store
            instead of narrowing the list.
          </p>
        </>
      ) : null}

      <h3>Nice to have</h3>
      {/* A softened reading is already listed above WITH the value the shopper gave, and
          the paragraph there says it ranks rather than filters — which is what a preference
          is. Printing its valueless twin here as well ("preferably material") reads as a
          second, vaguer requirement rather than as the same one. */}
      {preferences.length === 0 ? (
        <p>Nothing yet.</p>
      ) : (
        <ul aria-label="Preferences">
          {preferences.map((preference) => (
            <li key={preference.field}>{describePreference(preference)}</li>
          ))}
        </ul>
      )}

      {/* Two sentences, because there are two different facts and the page used to have
          only one sentence for both. A gap nobody answered is the shopper's silence; a gap
          they answered that we could not turn into a filter is our limitation, and saying
          "we never got an answer" about it is simply false. `understood` is what lets these
          be told apart — see `IntentConfirmProps.understood`. */}
      {openAndUnanswered.length > 0 ? (
        <p data-testid="unresolved">
          We never got an answer about: {openAndUnanswered.map(describeGap).join(', ')}. We have
          not guessed.
        </p>
      ) : null}

      {openAndAnswered.length > 0 ? (
        <div data-testid="answered-but-open">
          {/* Four sentences out of two independent facts, and every one of them was a
              false claim at some point in this repair.

              The OPENING clause turns on whether this gap is the one the question asked
              about. A row is chosen for a gap by what the answer PRODUCED, so it is
              routinely rendered under a subject nobody asked about: measured, the turns "I
              want a cherry wood table" / "oak, under $300" ask ONE question — the budget
              one — and the answer's material sends the row to the must-haves gap as well,
              where "You did answer when we asked about must-haves" is simply untrue.

              The CLOSING clause is about the SUBJECT and never about eligibility or about
              what happens next. "every option in this category is still eligible" was
              printed beside an active `price_usd lte 300` filter the same answer had just
              created, and "so we are still asking" was printed on the confirmation screen
              after the loop had spent all three of R1's questions and could ask nothing. */}
          {openAndAnswered.map(({ gap, record, addressed }) => (
            <p key={gap}>
              {record.gap === gap
                ? `You did answer when we asked about ${describeGap(gap)}`
                : `You did tell us about ${describeGap(gap)}`}{' '}
              - you said &ldquo;{record.answer}&rdquo;.{' '}
              {addressed
                ? `We kept ${keptTerms(record).join(', ')}, but none of it settles ${describeGap(gap)} on its own, so we have not guessed.`
                : `None of it told us about ${describeGap(gap)}, so we have not used it for that and we have not guessed.`}
            </p>
          ))}
        </div>
      ) : null}

      {/* `primary`: the one act on this screen, and the only gesture on the whole journey
          that leaves this origin. The stylesheet also reaches it positionally, through the
          section `Journey` wraps it in; the class says the same thing about the button
          itself, so it does not stop being the primary act when it is mounted elsewhere.
          Nothing about the class gates the click — `submitConfirmation`'s once-per-mount ref
          is what does that. */}
      <button type="button" className="primary" onClick={submitConfirmation} disabled={busy || sent}>
        Confirm and ask stores
      </button>
      <p>Nothing is ordered until you press that.</p>
    </section>
  )
}

export default IntentConfirm
