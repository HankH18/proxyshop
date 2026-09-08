/**
 * The question box under the shortlist — the one place a shopper can ask a follow-up.
 *
 * The journey was one shot: clarify turned words into an intent, confirm opened an auction,
 * a shortlist came back and the conversation ended. A shopper who wanted to know *which of
 * these is actually third-party tested?* or *why is the second one cheaper?* had nowhere to
 * put it. This is the whole surface for that, and it is deliberately small: it asks about
 * the rows already on screen and it is not a general assistant.
 *
 * **It performs no I/O.** `Journey` owns the wire and `chat/ask.ts` owns the request; this
 * component owns the conversation — the turns, the composer, and how an answer is laid out.
 * That split is `SignIn`'s, for the same reason: a component that fetched would be a
 * component a test could only drive through a network double.
 *
 * The one property worth stating about a panel on a page whose whole claim is the
 * organic/sponsored split (D55): **the two voices are laid out as two voices, and this file
 * has no branch that merges them.**
 *
 * * The platform's answer renders in `.voice[data-voice="platform"]`, under an attribution
 *   line that says whether it was assembled from the record or written by the shopping agent
 *   and checked against it. A reader is never left to infer which they are reading.
 * * Each shop's own message renders in `.voice[data-voice="store"]`, verbatim, under that
 *   shop's own domain — the same two classes the shortlist cards already use, so the
 *   colouring that means "this is the shop talking" means it here too.
 * * There is no code path that puts a `shopMessages` string inside the platform block:
 *   `answer` is rendered from `turn.answer.answer` and the shop blocks are rendered from
 *   `turn.answer.shopMessages`, and `readAnswer` has already thrown if the service handed
 *   over an answer that reproduced one.
 *
 * What a refusal looks like, and why it is the loudest thing on the panel: `notHeld` renders
 * ABOVE the grounds, with what the platform would have needed. "I don't know whether it is
 * third-party tested; no shop here has published a claim for it and our crawl did not record
 * one" is the answer this feature exists to be able to give, and burying it under a fold
 * would make a fluent guess the more prominent option.
 */
import { useCallback, useId, useRef, useState, type FormEvent } from 'react'

import { MAX_QUESTION_CHARS, type ChatAnswer } from './ask'

export interface AskPanelProps {
  /**
   * Ask one question. Resolves with the service's answer, or rejects with an error whose
   * message this panel prints. `Journey` binds this to `askAboutShortlist`.
   */
  readonly onAsk: (question: string) => Promise<ChatAnswer>
  /** How many options are on screen, for the placeholder. Purely cosmetic. */
  readonly optionCount?: number
}

/** One exchange: what was asked, and what came back. `answer` is undefined while in flight. */
interface Turn {
  readonly asked: string
  readonly answer?: ChatAnswer
  readonly failure?: string
}

/** How the answer's own provenance reads. `answer_source` on the wire, in English here. */
function sourceLine(source: string): string {
  if (source === 'written') {
    return 'ProxyShop, in its shopping agent’s words — checked, line by line, against the record below'
  }
  if (source === 'assembled') {
    return 'ProxyShop, assembled from its own record — no model wrote this sentence'
  }
  // A source this build does not know is printed plainly rather than hidden. The same rule
  // the shortlist's provenance labels follow: recognition only.
  return `ProxyShop (${source || 'source not stated'})`
}

export function AskPanel({ onAsk, optionCount }: AskPanelProps) {
  const questionId = useId()
  const [draft, setDraft] = useState('')
  const [turns, setTurns] = useState<readonly Turn[]>([])
  const [busy, setBusy] = useState(false)
  // Guards against a second submit while the first is in flight. `busy` disables the button,
  // but a keyboard submit can still land in the gap before React re-renders.
  const inFlight = useRef(false)

  const send = useCallback(
    async (event: FormEvent) => {
      event.preventDefault()
      const asked = draft.trim()
      if (asked === '' || inFlight.current) return
      inFlight.current = true
      setBusy(true)
      setDraft('')
      setTurns((previous) => [...previous, { asked }])
      try {
        const answer = await onAsk(asked)
        setTurns((previous) =>
          previous.map((turn, index) =>
            index === previous.length - 1 ? { ...turn, answer } : turn,
          ),
        )
      } catch (error) {
        setTurns((previous) =>
          previous.map((turn, index) =>
            index === previous.length - 1
              ? { ...turn, failure: error instanceof Error ? error.message : String(error) }
              : turn,
          ),
        )
      } finally {
        inFlight.current = false
        setBusy(false)
      }
    },
    [draft, onAsk],
  )

  const remaining = MAX_QUESTION_CHARS - draft.length

  return (
    <section className="ask" aria-label="Ask about these options" data-testid="ask-panel">
      <h3>Ask about these options</h3>
      <p className="gloss" data-testid="ask-scope">
        Questions about the {optionCount === undefined ? 'options' : `${optionCount} options`}{' '}
        above. ProxyShop answers from what it crawled and what the exchange published, tells
        you when a shop claimed something rather than the platform checking it, and says so
        plainly when it does not know. It will not answer for a shop &mdash; where a shop
        wrote its own message, that message is shown to you in the shop&rsquo;s own words,
        underneath.
      </p>

      {turns.length > 0 ? (
        <ol className="ask-turns" aria-label="Your questions and the answers" data-testid="ask-turns">
          {turns.map((turn, index) => (
            <li key={`${turn.asked}:${index}`} data-testid={`ask-turn-${index}`}>
              <p className="ask-question" data-testid={`ask-question-${index}`}>
                {turn.asked}
              </p>

              {turn.failure !== undefined ? (
                <p className="failure" role="alert" data-testid={`ask-failure-${index}`}>
                  {turn.failure}
                </p>
              ) : turn.answer === undefined ? (
                <p className="gloss" role="status" data-testid={`ask-pending-${index}`}>
                  Asking&hellip;
                </p>
              ) : (
                <AnswerBlock answer={turn.answer} index={index} />
              )}
            </li>
          ))}
        </ol>
      ) : null}

      <form onSubmit={send}>
        <label htmlFor={questionId}>What would you like to know?</label>
        <textarea
          id={questionId}
          name="question"
          rows={2}
          value={draft}
          maxLength={MAX_QUESTION_CHARS}
          placeholder="Which of these is actually third-party tested?"
          onChange={(event) => setDraft(event.target.value)}
          disabled={busy}
          data-testid="ask-input"
        />
        <button type="submit" disabled={busy || draft.trim() === ''} data-testid="ask-send">
          {busy ? 'Asking…' : 'Ask'}
        </button>
        {/* The limit is stated, not enforced silently. The service refuses a longer question
            with a 422, and a box that let someone type past it and then reported a
            validation error would be the worse page. */}
        <p className="gloss" data-testid="ask-remaining">
          {remaining} of {MAX_QUESTION_CHARS} characters left. Nothing is ordered by asking,
          and no store is asked anything &mdash; this reads the answer the exchange already
          gave.
        </p>
      </form>
    </section>
  )
}

function AnswerBlock({ answer, index }: { readonly answer: ChatAnswer; readonly index: number }) {
  return (
    <>
      {answer.notHeld.length > 0 ? (
        // ABOVE the answer and never behind a fold. A shopper who is about to spend money
        // on the strength of a sentence has to be able to see what that sentence does not
        // cover, and they have to see it without opening anything.
        <ul className="ask-not-held" aria-label="What we do not know" data-testid={`ask-not-held-${index}`}>
          {answer.notHeld.map((row) => (
            <li key={row.subject}>
              <strong>We don&rsquo;t know about &ldquo;{row.subject}&rdquo;.</strong> {row.detail}.
            </li>
          ))}
        </ul>
      ) : null}

      <div className="voice" data-voice="platform" data-testid={`ask-answer-${index}`}>
        <p className="voice-attribution" data-testid={`ask-source-${index}`}>
          <strong>{sourceLine(answer.answerSource)}</strong>
        </p>
        <p className="voice-body">{answer.answer}</p>
      </div>

      {answer.grounds.length > 0 ? (
        <details className="trace" data-testid={`ask-grounds-${index}`}>
          <summary>
            Show what this answer stands on &mdash; {answer.grounds.length}{' '}
            {answer.grounds.length === 1 ? 'record' : 'records'}, and where each came from
          </summary>
          <ul className="mono provenance-source" aria-label="What the answer stands on">
            {answer.grounds.map((ground) => (
              <li key={`${ground.bid_ref}:${ground.topic}:${ground.key}`}>
                {ground.store_domain || ground.bid_ref} &mdash; {ground.key}: {ground.value}
                <br />
                {/* The voice is printed for every row, not only for a shop's claim. A reader
                    who has to notice the ABSENCE of a badge to know something was checked has
                    been asked to do the platform's work. */}
                {ground.voice === 'shop_claim' ? 'the shop claims this' : 'the platform holds this'}
                : {ground.attribution}
                {ground.observed_at ? `, observed ${ground.observed_at}` : ''}
              </li>
            ))}
          </ul>
          {!answer.rankingRecorded ? (
            <p className="gloss" data-testid={`ask-no-ranking-${index}`}>
              This service no longer holds the exchange&rsquo;s recorded ranking for this
              auction, so nothing above explains the ORDER the options are in. That is not the
              same as the ranking having been flat.
            </p>
          ) : null}
        </details>
      ) : null}

      {answer.shopMessages.length > 0 ? (
        <div className="ask-shop-voices" data-testid={`ask-shop-messages-${index}`}>
          {answer.shopMessages.map((shop) => (
            <div
              className="voice"
              data-voice="store"
              key={shop.bid_ref}
              data-testid={`ask-shop-message-${index}-${shop.store_domain}`}
            >
              <p className="voice-attribution">
                <strong>{shop.store_domain || shop.bid_ref}</strong>, in its own words
              </p>
              {/* VERBATIM. Nothing here trims, summarises or re-wraps it: this is the thing
                  the shop bought, and re-voicing it would erase exactly the product. */}
              <p className="voice-body">{shop.message}</p>
            </div>
          ))}
        </div>
      ) : null}
    </>
  )
}
