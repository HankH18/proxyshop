/**
 * The one screen a person can look at: the whole shopper journey against the real backend.
 *
 * This shell owns the *flow* and nothing else. Every rule that matters was already written
 * and already tested, and is reused rather than re-stated:
 *
 *   * `intent.ts`        — `clarifyTurns` and `assertConfirmable`. Its `confirmIntent` is
 *                          NOT used: its body has no `profile` field, and
 *                          `contracts.BidRequest` makes one required. Measured against the
 *                          running stack, confirming without a profile has the exchange
 *                          coerce it to `{}`, every store agent answer 422, and the exchange
 *                          report `fallback_reason: "no_response"` for all of them — a
 *                          silently empty shortlist blamed on the market. `wire.ts`'s
 *                          `confirmWithProfile` is the same request with the field, and it
 *                          reuses that module's `assertConfirmable` rather than restating it.
 *   * `IntentConfirm`    — the clarifying question OR the confirm screen. While a question
 *                          is outstanding there is no confirm button in the document at all,
 *                          and this shell does not add one.
 *   * `shortlist.ts`     — `acceptSlot` and `permalinkRefusal`.
 *   * `ShortlistView`    — the slots, their provenance labels, and an Accept that is a
 *                          `<button>` because a slot may carry a `checkout_url` a buyer must
 *                          never follow.
 *   * `wire.ts`          — the two calls nothing else owned: the auction record and the
 *                          label render. Named `wire.ts` rather than `journey.ts`
 *                          because this machine's filesystem folds case, so `./Journey`
 *                          resolved to a lowercase `journey.ts` before this file and the
 *                          bundler failed with `"Journey" is not exported`.
 *
 * What this file adds, and why each piece is here:
 *
 * 1. **The turns array is sent whole, every time.** `POST /buyer/intent/clarify` is stateless
 *    over turns — the service's contract — so the transcript lives here and goes across in
 *    full on each round trip. `turns` is only advanced after the service accepted it, so a
 *    failed round trip cannot leave the answers out of step with the questions.
 * 2. **The permalink is shown before it is offered.** The full URL, the host it will actually
 *    reach, and the single-use code read off its own `?discount=` parameter. The link exists
 *    only when `permalinkRefusal` returns `undefined`; when it refuses, the refusal is what
 *    the buyer sees. Nothing here mints or reconstructs a URL.
 * 3. **A failure shows the status and the service's own words.** `instrumentFetcher` keeps
 *    the refused body so a bare `HTTP 503` from a reused module can be printed with the
 *    reason the service gave for it. Nothing is swallowed.
 * 4. **The gaps are on the screen, permanently.** Sign-in cannot complete in a browser, the
 *    exchange's slot carries no store domain, and the pseudonym is minted here rather than
 *    by the buyer service's vault. All three are stated in the UI rather than faked, because
 *    a demo that supplies its own join is the defect this app exists to not be.
 */
import { useCallback, useId, useMemo, useState, type FormEvent } from 'react'

import { IntentConfirm } from '../intent/IntentConfirm'
import {
  clarifyTurns,
  type AuctionCreated,
  type ClarifyOutcome,
  type Intent,
} from '../intent/intent'
import { ShortlistView } from '../shortlist/ShortlistView'
import {
  acceptSlot,
  permalinkRefusal,
  type AcceptOutcome,
  type ShortlistSlot,
} from '../shortlist/shortlist'
import { WhyEmpty } from './WhyEmpty'
import {
  confirmWithProfile,
  describeTrust,
  discountCodeFrom,
  explain,
  instrumentFetcher,
  loadAuction,
  mintPseudonym,
  permalinkHost,
  renderShortlist,
  renderedShortlist,
  type AuctionRecord,
  type Fetcher,
  type RenderedSlot,
} from './wire'

/** The browser's own fetch. Relative paths only — the API is served from this origin. */
const browserFetch: Fetcher = (input, init) => fetch(input, init)

interface AuctionStage {
  readonly created: AuctionCreated
  readonly record: AuctionRecord
  readonly slots: readonly RenderedSlot[]
}

export interface JourneyProps {
  /** Injected in tests. Left alone, the app talks to the service on its own origin. */
  readonly fetcher?: Fetcher
}

export function Journey({ fetcher = browserFetch }: JourneyProps = {}) {
  const wire = useMemo(() => instrumentFetcher(fetcher), [fetcher])
  const utteranceId = useId()

  // One rotating handle per page visit, minted here and held nowhere else. Not in
  // `localStorage`: a persisted pseudonym stops rotating, and a handle that does not rotate
  // is exactly the stable identifier R5 exists to deny the stores.
  const [pseudonym] = useState(mintPseudonym)
  const [turns, setTurns] = useState<readonly string[]>([])
  const [draft, setDraft] = useState('')
  const [outcome, setOutcome] = useState<ClarifyOutcome | undefined>(undefined)
  const [stage, setStage] = useState<AuctionStage | undefined>(undefined)
  const [accepted, setAccepted] = useState<AcceptOutcome | undefined>(undefined)
  const [failure, setFailure] = useState<string | undefined>(undefined)
  const [busy, setBusy] = useState(false)
  // Bumped only by an explicit "try that again". `IntentConfirm` and `ShortlistView` each
  // fire at most once per MOUNT, which is what stops a double click from opening two
  // auctions; a remount is therefore a deliberate second attempt by the buyer, and the
  // service's own ledger — not this counter — is what refuses a duplicate.
  const [attempt, setAttempt] = useState(0)

  const run = useCallback(
    async (work: () => Promise<void>) => {
      wire.reset()
      setFailure(undefined)
      setBusy(true)
      try {
        await work()
      } catch (error) {
        setFailure(explain(error, wire.latest()))
      } finally {
        setBusy(false)
      }
    },
    [wire],
  )

  const say = useCallback(
    async (text: string) => {
      const spoken = text.trim()
      if (!spoken) return
      const next = [...turns, spoken]
      await run(async () => {
        // The WHOLE transcript, every time: the clarify route is stateless over turns.
        const answered = await clarifyTurns(next, wire.fetcher)
        setTurns(next)
        setOutcome(answered)
      })
    },
    [run, turns, wire],
  )

  const sendDraft = useCallback(
    (event: FormEvent) => {
      event.preventDefault()
      const text = draft.trim()
      if (!text) return
      setDraft('')
      void say(text)
    },
    [draft, say],
  )

  const confirm = useCallback(
    async (intent: Intent) => {
      await run(async () => {
        const created = await confirmWithProfile(
          intent,
          { pseudonym, buckets: {} },
          wire.fetcher,
        )
        const record = await loadAuction(created.auction_id, wire.fetcher)
        const slots = await renderShortlist(record.shortlist, wire.fetcher)
        setStage({ created, record, slots })
      })
    },
    [pseudonym, run, wire],
  )

  const accept = useCallback(
    async (slot: ShortlistSlot) => {
      if (stage === undefined) return
      await run(async () => {
        const outcomeOfAccept = await acceptSlot(slot, wire.fetcher, {
          auctionId: stage.record.auction_id,
        })
        setAccepted(outcomeOfAccept)
      })
    },
    [run, stage, wire],
  )

  const answers = turns.slice(1)
  const permalink = accepted?.permalink_url
  const refusal = permalink === undefined ? undefined : permalinkRefusal(permalink)
  const host = refusal === undefined ? permalinkHost(permalink) : undefined
  const code = refusal === undefined ? discountCodeFrom(permalink) : undefined

  return (
    <main className="journey">
      <header className="masthead">
        <h1>Proxyshop</h1>
        <p className="lede">
          Say what you need. Your agent asks the exchange, the exchange asks the stores, and
          the stores answer for themselves. Every value below arrived in an HTTP response
          from the service on this origin during this session — there are no examples on this
          page.
        </p>
      </header>

      {failure !== undefined ? (
        <p role="alert" className="failure" data-testid="journey-error">
          {failure}{' '}
          <button type="button" className="linkish" onClick={() => setAttempt(attempt + 1)}>
            Try that again
          </button>{' '}
          <span className="gloss">
            If the first attempt did reach the exchange, it refuses the second and you will see
            that refusal here rather than a second auction.
          </span>
        </p>
      ) : null}

      <section aria-label="Step 1 - say what you need" className="step">
        <h2>
          <span className="ordinal">1</span> Say what you need
        </h2>
        {outcome === undefined ? (
          <form onSubmit={sendDraft}>
            <label htmlFor={utteranceId}>What are you shopping for?</label>
            <textarea
              id={utteranceId}
              name="utterance"
              rows={3}
              value={draft}
              placeholder="I want a warm merino wool beanie for winter, under $100"
              onChange={(event) => setDraft(event.target.value)}
              disabled={busy}
            />
            <button type="submit" disabled={busy || draft.trim() === ''}>
              Send
            </button>
            <p className="gloss">
              Nothing is ordered by this, and no store has been asked anything yet. The
              greyed-out line above is a hint, not an answer: the stores in this market stock
              a handful of things and each one only bids for the request clusters its own
              merchant envelope pursues, so a request nothing here sells comes back with no
              options. That is the real answer and step 3 will show you the stores&rsquo; own
              reasons for it.
            </p>
          </form>
        ) : (
          <ol className="transcript" aria-label="What you have said" data-testid="transcript">
            {turns.map((turn, index) => (
              <li key={`${turn}:${index}`}>{turn}</li>
            ))}
          </ol>
        )}
        <p className="gloss" data-testid="pseudonym">
          The stores are told you are <strong>{pseudonym}</strong>, and nothing else. That
          handle was generated in this browser when you opened the page and is thrown away
          when you reload it.
        </p>
      </section>

      {outcome !== undefined && stage === undefined ? (
        <section aria-label="Step 2 - check what we understood" className="step">
          <h2>
            <span className="ordinal">2</span> Check what we understood
          </h2>
          <IntentConfirm
            key={`intent-${attempt}`}
            questions={outcome.questions}
            answers={answers}
            intent={outcome.intent}
            unresolved={outcome.unresolved}
            onAnswer={say}
            onConfirm={confirm}
            busy={busy}
          />
          <p className="gloss" data-testid="cluster-id">
            The exchange will match this to cluster {outcome.intent.cluster_id}, which is a hash
            of the use case, the budget band and the constraints above.
          </p>
        </section>
      ) : null}

      {stage !== undefined ? (
        <section aria-label="Step 3 - what the stores answered" className="step">
          <h2>
            <span className="ordinal">3</span> What the stores answered
          </h2>
          <p className="gloss" data-testid="auction-id">
            Auction {stage.record.auction_id}, opened {stage.created.created_at || 'just now'}.
            The exchange asked {stage.record.solicited.length}{' '}
            {stage.record.solicited.length === 1 ? 'store' : 'stores'} and came back with{' '}
            {stage.slots.length} {stage.slots.length === 1 ? 'option' : 'options'}.
          </p>

          <ShortlistView
            key={`shortlist-${attempt}`}
            shortlist={renderedShortlist(stage.record.auction_id, stage.slots)}
            onAccept={accept}
            accepted={accepted}
            busy={busy}
          />

          {stage.slots.length === 0 ? (
            <WhyEmpty record={stage.record} />
          ) : (
            <>
              <ul className="mono provenance-source" aria-label="Where each label came from">
                {stage.slots.map((slot) => (
                  <li key={slot.bid_ref} data-testid={`labels-source-${slot.bid_ref}`}>
                    {slot.bid_ref}: labels_source {slot.labels_source}
                    <br />
                    trust_summary {describeTrust(slot.trust_fields)}
                  </li>
                ))}
              </ul>
              <details data-testid="verbatim-auction">
                <summary>The service&rsquo;s answer, verbatim</summary>
                <pre className="mono">{JSON.stringify(stage.record.raw, null, 2)}</pre>
              </details>
            </>
          )}
        </section>
      ) : null}

      {accepted !== undefined ? (
        <section aria-label="Step 4 - your checkout" className="step">
          <h2>
            <span className="ordinal">4</span> Your checkout
          </h2>
          <p>
            The exchange accepted {accepted.slot} ({accepted.bid_ref}) on auction{' '}
            {accepted.auction_id}
            {accepted.accepted_at ? ` at ${accepted.accepted_at}` : ''} and minted this
            permalink. This page did not build it:
          </p>
          <p className="mono permalink" data-testid="permalink-url">
            {accepted.permalink_url}
          </p>
          <dl className="facts">
            <dt>The host your browser will actually reach</dt>
            <dd data-testid="permalink-host">{host ?? 'this URL names no host we can read'}</dd>
            <dt>Your single-use code</dt>
            <dd data-testid="discount-code">
              {code ?? 'this permalink carries no discount parameter'}
            </dd>
          </dl>
          {refusal === undefined ? (
            <p>
              <a
                // The one URL-shaped value on this page that a browser may be sent to. It came
                // off the accept response and was checked by `permalinkRefusal` first; no
                // `href` on this page is ever built from slot data.
                href={accepted.permalink_url}
                rel="noreferrer"
                data-testid="permalink-link"
                className="cta"
              >
                Continue to {host}
              </a>
            </p>
          ) : (
            <p role="alert" data-testid="permalink-refusal">
              We will not send you to that address: {refusal}. Nothing has been ordered.
            </p>
          )}
          <p className="gloss">
            Nothing is ordered until you finish checkout on the store&rsquo;s own site.
          </p>
        </section>
      ) : null}

      <section aria-label="What is not wired yet" className="gaps">
        <h2>What is not wired yet</h2>
        <ul>
          <li data-testid="gap-signin">
            <strong>Sign-in: not available yet</strong> —{' '}
            <code>POST /buyer/auth/magic-link</code> answers 202 and by design never returns
            the token, so a browser cannot redeem one without an email transport. There is no
            sign-in form on this page because there is nothing a sign-in form could complete.
          </li>
          <li data-testid="gap-pseudonym">
            <strong>Pseudonym: minted in this browser</strong> — your pseudonym was generated
            in this browser for this visit. In a real deployment it comes from{' '}
            <code>POST /buyer/auth/session</code>, which mints it in the buyer service&rsquo;s
            pseudonym vault and rotates it; sign-in cannot complete here (above), so there is
            no session to mint one from. Stores never see anything else about you either way.
          </li>
          <li data-testid="gap-domain">
            <strong>Store domain: not pinned</strong> — the exchange&rsquo;s shortlist slot
            carries no <code>store_domain</code>, so the checkout host could only be checked
            for scheme and host presence, not pinned to a named store. The host above is shown
            to you for exactly that reason.
          </li>
        </ul>
      </section>
    </main>
  )
}

export default Journey
