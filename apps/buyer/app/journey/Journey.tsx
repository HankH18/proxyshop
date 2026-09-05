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
 *    reach, and the single-use code read off its own `?discount=` parameter. Nothing here
 *    mints or reconstructs a URL.
 *
 *    **The refusal a buyer can actually see does not come from here.** `acceptSlot` calls
 *    `assertFollowable` on the permalink before it returns, so a URL a browser must not
 *    follow throws inside `accept()`, is caught by `run`, and reaches the buyer as the
 *    failure banner at the top of the page — `"R3: refusing to follow this checkout
 *    permalink — …"` — with step 4 never rendered at all. `journey.test.tsx` asserts exactly
 *    that. The `permalinkRefusal` call below therefore re-checks a URL that has already
 *    passed the identical check against the identical expected domain, and cannot fire. It
 *    stays anyway: a last-line guard that never fires is still the guard, and `acceptSlot`
 *    is the wire layer a future refactor most easily loosens.
 * 3. **A failure shows the status and the service's own words.** `instrumentFetcher` keeps
 *    the refused body so a bare `HTTP 503` from a reused module can be printed with the
 *    reason the service gave for it. Nothing is swallowed.
 * 4. **The gaps are on the screen, permanently.** Sign-in cannot complete in a browser; the
 *    exchange's slot carries neither a store domain nor a price; the clarifying questions
 *    come from the buyer service's offline model double rather than a live model; and the
 *    pseudonym is minted here rather than by the buyer service's vault. All five are stated
 *    in the UI rather than faked, because a demo that supplies its own join is the defect
 *    this app exists to not be.
 * 5. **A shortlist has three outcomes here, not two.** A shortlist with slots; a shortlist
 *    with none, which is a fact about the MARKET (nothing was eligible); and no shortlist at
 *    all, which is a fact about the EXCHANGE — `buyer_svc/auctions/routes.py` answers
 *    `shortlist: null` once the exchange's 15-minute TTL has taken the auction away, while
 *    the recorded diagnostics survive. `wire.ts` keeps them apart as `liveness`, and this
 *    file renders the third with no `ShortlistView` and therefore no Accept: accepting an
 *    auction the exchange has forgotten cannot succeed, and a control that cannot work is
 *    worse than the sentence saying why it is not there.
 */
import { Fragment, useCallback, useId, useMemo, useState, type FormEvent } from 'react'

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
  describeComponents,
  describeTrust,
  discountCodeFrom,
  entryForSlot,
  explain,
  instrumentFetcher,
  loadAuction,
  mintPseudonym,
  permalinkHost,
  rankedForSlot,
  renderShortlist,
  renderedShortlist,
  storeIdFromBidRef,
  type AuctionEntry,
  type AuctionRecord,
  type Fetcher,
  type RankedBid,
  type RenderedSlot,
} from './wire'

/** The browser's own fetch. Relative paths only — the API is served from this origin. */
const browserFetch: Fetcher = (input, init) => fetch(input, init)

/**
 * What the exchange reported this slot's store bid — the whole of what this page says about
 * price, and none of it arithmetic.
 *
 * The numbers are printed as they arrived. No `toFixed`, no `Intl.NumberFormat`, no currency
 * symbol: `entries[].unit_price` and `total_price` are bare numbers and the report names no
 * currency anywhere, so a page that added a `$` would be telling a buyer something the
 * service did not say. A slot whose store is in no entry, or an entry carrying neither
 * price, says so — never a blank cell and never a zero, because a zero is a price.
 *
 * `fallback` is why the sentence has two forms. The exchange sets it when it represented a
 * store at its own list price instead of quoting a bid that store made, and calling that
 * "the price this store bid" would be this page inventing a bid nobody placed.
 *
 * A zero gets a sentence of its own, and it is the reason this function is not a one-liner.
 * MEASURED in `apps/exchange/src/auction/routes.py::_entries_out`, which builds this very
 * field: `unit_price=float(offer.get("unit_price", 0.0))`. So an offer that named no price
 * is reported as `0.0`, indistinguishable on the wire from an offer that named zero. This
 * page cannot tell the two apart and does not pretend to — it prints the number the service
 * sent and says what a zero there can also mean, because a bare "unit 0" reads to a buyer
 * as free.
 */
function bidPrice(entry: AuctionEntry | undefined): string {
  if (entry === undefined) return 'price not reported for this slot'
  const parts: string[] = []
  if (entry.unit_price !== undefined) parts.push(`unit ${entry.unit_price}`)
  if (entry.total_price !== undefined) parts.push(`total ${entry.total_price}`)
  if (parts.length === 0) return 'price not reported for this slot'
  const provenance = entry.fallback
    ? 'the exchange represented this store at its own list price'
    : 'the price this store bid'
  const zeroed =
    entry.unit_price === 0 || entry.total_price === 0
      ? ' A zero is also what the exchange reports for an offer that named no price.'
      : ''
  return `${parts.join(', ')} — ${provenance}, as the exchange reported this auction.${zeroed}`
}

/**
 * The exchange's published ranking for one slot, or the plain fact that it published none.
 *
 * Two absences, kept apart, because they are two different things a buyer might want to
 * know: the ranking published no row for this slot at all, and it published a row whose
 * score this client could not read as a number. Neither prints as a zero — a zero here would
 * read as the exchange having scored the candidate at the bottom.
 */
function rankLine(row: RankedBid | undefined): string {
  if (row === undefined) return 'rank_score not published for this slot'
  const score =
    row.rank_score === undefined
      ? 'rank_score not a readable number in the exchange row'
      : `rank_score ${row.rank_score}`
  return `${score} — ${describeComponents(row.components)}`
}

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
        // `/render` labels a shortlist. When the exchange has forgotten this auction there
        // is no shortlist to label — not an empty one, none — so the call is not made at
        // all rather than made with `null` and its refusal shown as a failure.
        const slots =
          record.liveness === 'forgotten'
            ? []
            : await renderShortlist(record.shortlist, wire.fetcher)
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
  // The slot the buyer accepted, found by the bid ref the service echoed back. It carries
  // the `store_domain` that `acceptSlot` checked the permalink against, so the re-check
  // below is that same check rather than a strictly weaker one done with `''`.
  const acceptedSlot = stage?.slots.find((slot) => slot.bid_ref === accepted?.bid_ref)
  const refusal =
    permalink === undefined
      ? undefined
      : permalinkRefusal(permalink, acceptedSlot?.store_domain ?? '')
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
            {/* Two clocks, and neither is this browser's. `created_at` is the confirm
                response's; `recorded_at` is when the buyer service recorded the exchange's
                answer, which its own route documents as ITS clock rather than the
                exchange's. When the service sent neither, this sentence says neither —
                there is no wording here for a time nobody reported. */}
            Auction {stage.record.auction_id}
            {stage.created.created_at ? `, opened ${stage.created.created_at}` : ''}. The
            exchange asked {stage.record.solicited.length}{' '}
            {stage.record.solicited.length === 1 ? 'store' : 'stores'}
            {stage.record.liveness === 'forgotten'
              ? ', and no longer holds the shortlist it answered with.'
              : ` and came back with ${stage.slots.length} ${
                  stage.slots.length === 1 ? 'option' : 'options'
                }.`}
            {stage.record.recorded_at
              ? ` The buyer service recorded that answer on its own clock at ${stage.record.recorded_at}.`
              : ''}
          </p>

          {stage.record.liveness === 'forgotten' ? (
            <>
              {/* No `ShortlistView`, and therefore no Accept button. Accepting an auction
                  the exchange has forgotten cannot succeed — the accept route would answer
                  `unknown_bid` — and a control that cannot work is worse than the sentence
                  explaining why it is absent. */}
              <p role="status" data-testid="shortlist-forgotten">
                <strong>The exchange no longer holds this auction.</strong> Its shortlist
                lives for fifteen minutes and that window has closed, so there is nothing live
                to choose from here and nothing to accept. This is not a store saying no and
                it is not an empty market &mdash; it is the shortlist having expired. What
                follows is the report the buyer service kept from when the auction ran.
              </p>
              <WhyEmpty record={stage.record} recorded />
            </>
          ) : (
            <>
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
                  <h3>What each of these stores asked for it</h3>
                  <dl className="facts" data-testid="slot-prices">
                    {stage.slots.map((slot) => (
                      <Fragment key={slot.bid_ref}>
                        <dt>
                          {slot.slot} —{' '}
                          {storeIdFromBidRef(slot.bid_ref, stage.record.auction_id) ?? slot.bid_ref}
                        </dt>
                        <dd data-testid={`price-${slot.bid_ref}`}>
                          {bidPrice(entryForSlot(stage.record, slot.bid_ref))}
                        </dd>
                      </Fragment>
                    ))}
                  </dl>
                  <p className="gloss" data-testid="price-provenance">
                    The exchange&rsquo;s shortlist slot carries no price, so these came out of{' '}
                    <code>entries[]</code> in the very same answer &mdash; printed below, verbatim
                    &mdash; joined to each slot by the store id inside its own{' '}
                    <code>bid_ref</code>. They are the numbers the service sent, unrounded and
                    unconverted; it names no currency, so this page names none either.
                  </p>

                  <ul className="mono provenance-source" aria-label="Where each label came from">
                    {stage.slots.map((slot) => (
                      <li key={slot.bid_ref} data-testid={`labels-source-${slot.bid_ref}`}>
                        {slot.bid_ref}: labels_source {slot.labels_source}
                        <br />
                        trust_summary {describeTrust(slot.trust_fields)}
                        <br />
                        {rankLine(rankedForSlot(stage.record, slot.bid_ref))}
                      </li>
                    ))}
                  </ul>
                  <details data-testid="verbatim-auction">
                    <summary>The service&rsquo;s answer, verbatim</summary>
                    <pre className="mono">{JSON.stringify(stage.record.raw, null, 2)}</pre>
                  </details>
              </>
            )}
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
            // Unreachable today, and deliberately kept — see this file's docstring, point 2.
            // `acceptSlot` ran `assertFollowable` on this exact URL with this exact expected
            // domain and threw, so a refusal reaches the buyer as the failure banner and
            // this whole section never renders. No `data-testid` here: this build has no
            // test that can reach this branch, and a testid nothing asserts on is a promise
            // to a reader that is not kept.
            <p role="alert">
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
          <li data-testid="gap-price">
            <strong>Price: not on the slot</strong> — the exchange&rsquo;s shortlist slot
            carries no price field at all, so this page does not read one off it. The prices
            in step 3 come from <code>entries[]</code> in the same{' '}
            <code>GET /buyer/auctions/{'{auction_id}'}</code> answer, joined to each slot by
            the store id inside its <code>bid_ref</code> &mdash; which the exchange mints as{' '}
            <code>{'{auction_id}:{store_id}'}</code>. A slot whose store is in no entry is
            shown as having no reported price rather than being quietly given one.
          </li>
          <li data-testid="gap-model">
            <strong>The questions came from no live model</strong> — the buyer service
            resolves its client with <code>build_llm(&quot;buyer&quot;)</code>, and with{' '}
            <code>LLM_PROVIDER</code> unset that returns D20&rsquo;s offline double
            (measured on this tree: <code>&lt;DeterministicLLM role=&apos;buyer&apos;&gt;</code>,{' '}
            <code>model=&quot;double:buyer&quot;</code>). That is the service&rsquo;s designed
            default, not a failure. It does mean the clarifying questions you were asked, and
            the intent extracted from your answers, are the buyer service&rsquo;s own wording
            and its own extraction — no live model wrote or read anything on this page.
          </li>
        </ul>
      </section>
    </main>
  )
}

export default Journey
