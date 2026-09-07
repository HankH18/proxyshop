/**
 * The one screen a person can look at: the whole shopper journey against the real backend.
 *
 * This shell owns the *flow* and nothing else. Every rule that matters was already written
 * and already tested, and is reused rather than re-stated:
 *
 *   * `intent.ts`        — `clarifyTurns` and `assertConfirmable`. Its `confirmIntent` is
 *                          NOT used: its body has no `profile` field, and this page states
 *                          R5's handle itself rather than letting the exchange name the
 *                          shopper. (This entry used to say that confirming without a profile
 *                          has the exchange coerce it to `{}`, every store agent answer 422,
 *                          and the exchange report `fallback_reason: "no_response"` for all of
 *                          them. Both halves are now false —
 *                          `exchange.composition.solicitation_profile` mints an
 *                          `anon-{auction_id}` pseudonym instead of `{}`, and a refusal that
 *                          does happen is named `store_refused:422`. `wire.ts`'s
 *                          `BuyerProfile` has the whole story.) `wire.ts`'s
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
 *    passed the identical check against the identical expected domain, and cannot fire.
 *
 *    "Identical" is load-bearing and is why `acceptedSlot` holds the slot OBJECT rather than
 *    re-finding it by the `bid_ref` the service echoed back. `acceptSlot` checks against the
 *    clicked slot's `store_domain`; a lookup keyed on the echo would return `undefined` for
 *    an echo naming no rendered slot, fall back to `''`, and make this guard strictly WEAKER
 *    than the check it claims to mirror — while a matching-but-different slot would make it
 *    stricter, and it would fire on a URL nothing had actually refused. Holding the same
 *    object removes both cases. The guard stays: one that never fires is still the guard,
 *    and `acceptSlot` is the wire layer a future refactor most easily loosens.
 * 3. **A failure shows the status and the service's own words.** `instrumentFetcher` keeps
 *    the refused body so a bare `HTTP 503` from a reused module can be printed with the
 *    reason the service gave for it. Nothing is swallowed.
 * 4. **The gaps are on the screen, permanently.** The exchange's slot carries neither a store
 *    domain nor a price, and the clarifying questions come from the buyer service's offline
 *    model double rather than a live model. All three are stated in the UI rather than faked,
 *    because a demo that supplies its own join is the defect this app exists to not be.
 *
 *    Two entries used to sit at the top of that list and are gone because the gap closed
 *    rather than because the sentence was softened. They said sign-in could not complete in a
 *    browser and that the pseudonym was therefore minted here. The first was never true of
 *    this tree: `buyer_svc/auth/delivery.py` mails a link built as the deployment's base URL
 *    with the token on it as a query parameter, so a browser arriving from the buyer's own
 *    mailbox holds the token in `location.search` and `POST /buyer/auth/session` is one call
 *    away. `magic-link.ts` reads it, `chat/session.ts` spends it, and both of those existed
 *    already — what was missing was this page calling them. The second went with it:
 *    `mintPseudonym` is deleted and the handle now comes from the service's vault. See
 *    point 6.
 * 5. **A shortlist has three outcomes here, not two.** A shortlist with slots; a shortlist
 *    with none, which is a fact about the MARKET (nothing was eligible); and no shortlist at
 *    all, which is a fact about the EXCHANGE — `buyer_svc/auctions/routes.py` answers
 *    `shortlist: null` once the exchange's 15-minute TTL has taken the auction away, while
 *    the recorded diagnostics survive. `wire.ts` keeps them apart as `liveness`, and this
 *    file renders the third with no `ShortlistView` and therefore no Accept: accepting an
 *    auction the exchange no longer holds cannot succeed, and a control that cannot work is
 *    worse than the sentence saying why it is not there. That sentence names no single
 *    cause — the exchange's own 404 lists four and picks none, and the buyer service passes
 *    on a bare `null` with no reason attached.
 * 6. **Browsing is open; confirming is not.** The deliberate half of R5's sign-in, stated
 *    here because the code below only shows it working.
 *
 *    Steps 1 and 2 run with no session at all. Everything in them is between the buyer and
 *    the buyer's OWN service — `POST /buyer/intent/clarify` reaches no exchange and no store
 *    — so there is nothing to protect a visitor from by making them hand over an address
 *    first, and a login wall in front of a page whose whole purpose is to show what this
 *    market does would be asking for an email to see a demo.
 *
 *    The gate sits at the confirm, because that is the first gesture that leaves this origin:
 *    `POST /buyer/intent/confirm` opens an auction, the exchange solicits real stores, and
 *    each of them is handed a pseudonym and a bucket set. R5 says that pseudonym rotates and
 *    is the service's, so a page that let the auction open without a session would be back to
 *    naming the buyer itself. The control is therefore ABSENT rather than disabled until
 *    there is a session — `IntentConfirm`'s button fires once per mount, so a click this page
 *    refused would have spent the buyer's one confirmation.
 *
 *    The cost is named on the page rather than hidden: opening the mailed link loads this
 *    page again, so a conversation started before signing in does not survive it. Nothing
 *    here persists the transcript to work around that — `sessionStorage` does not cross the
 *    new tab a mail client opens, and `localStorage` would write what a buyer is shopping for
 *    onto their disk to save them retyping one sentence.
 */
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from 'react'

import {
  closeSession,
  loadProfile,
  redeemMagicLink,
  requestMagicLink,
  type BuyerProfile,
  type BuyerSession,
} from '../chat/session'
import { IntentConfirm } from '../intent/IntentConfirm'
import {
  MAX_CLARIFYING_QUESTIONS,
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
import { SignIn } from './SignIn'
import { WhyEmpty } from './WhyEmpty'
import { strippedUrl, tokenFromSearch } from './magic-link'
import {
  MissingProfileError,
  confirmWithProfile,
  describeComponents,
  describeTrust,
  discountCodeFrom,
  explain,
  instrumentFetcher,
  loadAuction,
  permalinkHost,
  rankedForSlot,
  renderShortlist,
  renderedShortlist,
  storeIdFromBidRef,
  type AuctionRecord,
  type Fetcher,
  type RankedBid,
  type RenderedSlot,
} from './wire'

/** The browser's own fetch. Relative paths only — the API is served from this origin. */
const browserFetch: Fetcher = (input, init) => fetch(input, init)

/**
 * The exchange's published ranking for one slot, or the plain fact that it published none.
 *
 * Three absences, kept apart, because they are three different things a buyer might want to
 * know: this service kept no record to read a ranking out of; the ranking published no row
 * for this slot; and it published a row whose score this client could not read as a number.
 * None prints as a zero — a zero here would read as the exchange having scored the candidate
 * at the bottom.
 */
function rankLine(row: RankedBid | undefined, noRecord: boolean): string {
  if (row === undefined) {
    return noRecord
      ? 'no ranking here: this service kept no record of the auction to read one from'
      : 'rank_score not published for this slot'
  }
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

  // The session, and the store-facing profile behind it. BOTH come from the buyer service:
  // `POST /buyer/auth/session` mints the pseudonym in the service's vault and
  // `GET /buyer/profile` coarsens the account into R5's buckets. This page mints neither and
  // no longer can — `mintPseudonym` was deleted with this change, because a browser-minted
  // handle does not rotate when the vault rotates and is exactly the stable identifier R5
  // exists to deny the stores.
  //
  // Held in component state and NOWHERE else. Not `localStorage`, and not `sessionStorage`:
  // the session id is a bearer credential for this origin, and the rest of this page's state
  // (the transcript, the auction, the shortlist) is in memory too, so persisting only the
  // credential would buy a reload nothing except a live session with no journey around it.
  const [session, setSession] = useState<BuyerSession | null>(null)
  const [profile, setProfile] = useState<BuyerProfile | null>(null)
  // What the service said the last accepted link expires at. Never a clock of this page's.
  const [linkExpiresAt, setLinkExpiresAt] = useState<string | undefined>(undefined)
  const [turns, setTurns] = useState<readonly string[]>([])
  const [draft, setDraft] = useState('')
  const [outcome, setOutcome] = useState<ClarifyOutcome | undefined>(undefined)
  const [stage, setStage] = useState<AuctionStage | undefined>(undefined)
  const [accepted, setAccepted] = useState<AcceptOutcome | undefined>(undefined)
  // The slot object `acceptSlot` was actually handed, kept so the re-check below can use the
  // SAME `store_domain` it checked against. Looking the slot up again by the `bid_ref` the
  // service echoed back would not be the same thing: an echo naming no rendered slot would
  // silently fall back to `''` and make the last-line guard weaker than the check it mirrors.
  const [acceptedSlot, setAcceptedSlot] = useState<ShortlistSlot | undefined>(undefined)
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

  // The redemption happens once per page load, and only when the buyer arrived from their
  // own mailbox. `redeemed` is a ref rather than state because the guard has to hold BEFORE
  // the first render commits — a second redemption of a single-use token is a 401, and it
  // would be this page showing a buyer a refusal it caused itself.
  const redeemed = useRef(false)

  useEffect(() => {
    if (redeemed.current) return
    redeemed.current = true
    const token = tokenFromSearch(window.location.search)
    if (token === undefined) return
    // Out of the address bar first, before any request is made and whatever the redemption
    // then does: a single-use bearer credential must not survive in the history entry, in a
    // bookmark, or in a `Referer` header on a later navigation to a store's domain.
    window.history.replaceState({}, '', strippedUrl(new URL(window.location.href)))
    void run(async () => {
      const live = await redeemMagicLink(token, wire.fetcher)
      // The profile is loaded before the session is shown, on purpose: `loadProfile` runs
      // `assertPseudonymOnly` over the body, so a service that regressed and returned an
      // identity field leaves this page signed OUT with the refusal on screen, rather than
      // signed in with a profile it is about to forward to the exchange.
      const coarsened = await loadProfile(live, wire.fetcher)
      setSession(live)
      setProfile(coarsened)
    })
  }, [run, wire])

  const askForLink = useCallback(
    async (email: string) => {
      await run(async () => {
        // The address goes up and nothing about it comes back: the answer is `202` with an
        // expiry, and the token is mailed rather than returned. This page therefore cannot
        // sign anybody in from here, and does not pretend to.
        setLinkExpiresAt(await requestMagicLink(email, wire.fetcher))
      })
    },
    [run, wire],
  )

  const signOut = useCallback(async () => {
    const live = session
    if (live === null) return
    await run(async () => {
      await closeSession(live, wire.fetcher)
      setSession(null)
      setProfile(null)
      setLinkExpiresAt(undefined)
      // Everything downstream of the confirm belonged to a pseudonym the vault has now
      // retired. Leaving a shortlist on the screen with an Accept under it would leave a
      // store-reaching control wired to a session that no longer exists.
      setTurns([])
      setOutcome(undefined)
      setStage(undefined)
      setAccepted(undefined)
      setAcceptedSlot(undefined)
    })
  }, [run, session, wire])

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
        if (profile === null) {
          // Defence in depth, and it should be unreachable: the control that calls this is
          // not in the document while `session` is null. Reusing `MissingProfileError`
          // rather than inventing a second one — its message already says the right thing
          // about what sending nothing would cost.
          throw new MissingProfileError(
            'this browser has no signed-in session, so there is no vault-minted pseudonym ' +
              'to open an auction under',
          )
        }
        // R5's two halves, both the service's: the rotating pseudonym its vault minted and
        // the coarsened buckets it built. Neither is composed here, and the session id — a
        // bearer credential for this origin — is not among them.
        const created = await confirmWithProfile(
          intent,
          { pseudonym: profile.pseudonym, buckets: profile.buckets },
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
    [profile, run, wire],
  )

  const accept = useCallback(
    async (slot: ShortlistSlot) => {
      if (stage === undefined) return
      await run(async () => {
        const outcomeOfAccept = await acceptSlot(slot, wire.fetcher, {
          auctionId: stage.record.auction_id,
        })
        setAcceptedSlot(slot)
        setAccepted(outcomeOfAccept)
      })
    },
    [run, stage, wire],
  )

  const answers = turns.slice(1)
  // Whether `IntentConfirm` would put its confirm button in the document. It is restated
  // here rather than asked of the component because this page must not MOUNT that button
  // while signed out: it fires `onConfirm` at most once per mount, so a click this page
  // refused would spend the buyer's single confirmation and leave a dead button behind.
  // Both halves mirror `IntentConfirm`'s own conditions, in its order.
  const confirmable =
    outcome !== undefined &&
    outcome.questions.length <= MAX_CLARIFYING_QUESTIONS &&
    outcome.questions.length <= answers.length
  // One condition for "signed in", used by the sign-in panel, the pseudonym line and the
  // gate alike. The two pieces of state are set together and cleared together, so a
  // half-signed-in page is not reachable — and deriving the gate from the SAME thing
  // `confirm` needs is what makes the refusal inside `confirm` genuinely unreachable rather
  // than merely unlikely.
  const signedIn = session !== null && profile !== null
  const gateOnSignIn = confirmable && !signedIn
  const permalink = accepted?.permalink_url
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
          the stores answer for themselves. Every value below arrived in an HTTP response from
          the service on this origin during this session, with exactly one exception, named
          where it appears and listed under &ldquo;What is not wired yet&rdquo;: the grey text
          inside the box is a hint rather than an answer.
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

      <section aria-label="Sign in" className="step" data-testid="signin">
        <h2>Sign in</h2>
        {session === null || profile === null ? (
          <SignIn onRequestLink={askForLink} linkExpiresAt={linkExpiresAt} busy={busy} />
        ) : (
          <>
            <p data-testid="signed-in">
              Signed in. This session runs under{' '}
              <strong>{profile.pseudonym}</strong>, which the buyer
              service&rsquo;s pseudonym vault minted when you opened your link. The address you
              typed stayed in your mailbox and on your own device: nothing the service answers
              with has a field it could ride back in.
            </p>
            <button type="button" onClick={() => void signOut()} disabled={busy}>
              Sign out
            </button>
            <p className="gloss">
              Signing out retires that handle in the vault for good &mdash; it is never
              reissued, to you or to anybody &mdash; and clears the auction below with it.
              Signing in again mints a different one, which is what R5 means by rotating.
            </p>
          </>
        )}
      </section>

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
          {profile === null ? (
            <>
              No store has been told anything about you, and there is no handle to tell them
              with: you are not signed in. What you type here goes to this origin&rsquo;s own
              buyer service and no further until you sign in and confirm.
            </>
          ) : (
            <>
              The stores are told you are <strong>{profile.pseudonym}</strong>, and nothing
              else. That handle was minted by the buyer service&rsquo;s pseudonym vault when
              you signed in &mdash; this browser cannot mint one &mdash; and signing out
              retires it.
            </>
          )}
        </p>
      </section>

      {outcome !== undefined && stage === undefined ? (
        <section aria-label="Step 2 - check what we understood" className="step">
          <h2>
            <span className="ordinal">2</span> Check what we understood
          </h2>
          {gateOnSignIn ? (
            // The whole of the gate. Not a disabled confirm button — absent, for the reason
            // `IntentConfirm` gives for never rendering one while a question is outstanding,
            // plus the one in `confirmable` above. Nothing here restates the intent: that is
            // `IntentConfirm`'s job and it does it as soon as there is a session to do it
            // under.
            <p role="status" data-testid="confirm-gated">
              <strong>Sign in to ask the stores.</strong> Everything so far has stayed between
              you and this origin&rsquo;s buyer service. The next step is the one that leaves
              it: the exchange solicits real stores, and each of them is told a pseudonym and
              a handful of coarse buckets. That pseudonym has to come from the service&rsquo;s
              vault rather than from this page, so there is no button here until you have
              signed in above.
            </p>
          ) : (
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
          )}
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
            {stage.record.solicited.length === 1 ? 'store' : 'stores'} when the auction opened.
            {stage.record.liveness === 'forgotten'
              ? ' It no longer holds the shortlist it answered with.'
              : ` Its shortlist, re-fetched for this page, came back with ${stage.slots.length} ${
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
              {/* This page may not name WHICH cause. `GET /auctions/{id}/shortlist` 404s
                  for four of them and says so itself, and its docstring is explicit that
                  filing eviction under "the TTL took it away" is "a diagnosis that sends the
                  reader to the wrong knob". The buyer service turns that 404 into a plain
                  `shortlist: null` with no reason attached, so the reason is not on the wire
                  and this page does not invent one. */}
              <p role="status" data-testid="shortlist-forgotten">
                <strong>The exchange has no shortlist for this auction.</strong> It says so
                without saying why. Its own answer names four possibilities and does not
                choose between them: the auction has not closed yet, it never existed, its
                fifteen-minute lifetime has taken it away, or a burst of newer auctions
                pushed it out of the exchange&rsquo;s store. There is nothing live to choose
                from here and nothing to accept. This is not the market coming back empty
                &mdash; that would be a shortlist with no slots in it, which is a different
                answer. What follows is the report the buyer service kept from when the
                auction ran.
              </p>
              <WhyEmpty record={stage.record} recorded />
            </>
          ) : stage.record.shortlist_slot_count > 0 && stage.slots.length === 0 ? (
            // The exchange DID send slots and the labelling step returned none of them. That
            // is a failure in `POST /buyer/shortlist/render`, not a verdict about the market.
            //
            // `ShortlistView` is deliberately NOT rendered here, and that is the whole point
            // of hoisting this out into its own branch: handed zero slots it prints "No store
            // was eligible for what you asked for", which is exactly the claim this branch
            // exists to stop the page making. Rendering the explanation underneath that
            // sentence would have left both on the page, contradicting each other.
            <>
              <p role="alert" data-testid="labels-dropped">
                The exchange sent {stage.record.shortlist_slot_count}{' '}
                {stage.record.shortlist_slot_count === 1 ? 'option' : 'options'} for this
                auction, and the step that labels them for display returned none. That is a
                fault on this page&rsquo;s side of the wire, not an answer about the market
                &mdash; so the options are not shown rather than being reported to you as
                though no store was eligible. Nothing has been ordered. The exchange&rsquo;s
                own answer is printed below, verbatim.
              </p>
              <details data-testid="verbatim-auction">
                <summary>The service&rsquo;s answer, verbatim</summary>
                <pre className="mono">{JSON.stringify(stage.record.raw, null, 2)}</pre>
              </details>
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
                  {/* The prices, the products and the commitments are on the CARDS above,
                      off each slot's own `price` / `product` / `commitments`, live from the
                      exchange. There used to be a second price list here, joined out of the
                      RECORDED `entries[]` by the store id in each bid ref, because the slot
                      carried no price; that join is gone with the field it stood in for.
                      `entries[]` is still on this page as what it is — every rostered
                      store's recorded answer, in the panel below and in the verbatim body. */}
                  <p className="gloss" data-testid="price-provenance">
                    <strong>
                      The price on each card above is the exchange&rsquo;s live answer for
                      that slot.
                    </strong>{' '}
                    It comes off the slot itself, in the shortlist this page re-fetched from
                    the exchange for this request &mdash; not out of the <code>entries[]</code>
                    report the buyer service recorded when the auction opened, which is a
                    different clock and is printed separately below. Where a store quoted no
                    price, the card says so rather than showing a zero. The numbers are the
                    ones the service sent, unrounded and unconverted, and the card names a
                    currency only where the exchange named one.
                  </p>

                  <ul className="mono provenance-source" aria-label="Where each label came from">
                    {stage.slots.map((slot) => (
                      <li key={slot.bid_ref} data-testid={`labels-source-${slot.bid_ref}`}>
                        {storeIdFromBidRef(slot.bid_ref, stage.record.auction_id) ?? slot.bid_ref}
                        {' — '}
                        {slot.bid_ref}: labels_source {slot.labels_source}
                        <br />
                        trust_summary {describeTrust(slot.trust_fields)}
                        <br />
                        {rankLine(
                          rankedForSlot(stage.record, slot.bid_ref),
                          stage.record.recorded_at === '',
                        )}
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
          <li data-testid="gap-domain">
            <strong>Store domain: not pinned</strong> — the exchange&rsquo;s shortlist slot
            carries no <code>store_domain</code>, so the checkout host could only be checked
            for scheme and host presence, not pinned to a named store. The host above is shown
            to you for exactly that reason.
          </li>
          <li data-testid="gap-fallback">
            <strong>Whose price it is: not on the slot</strong> &mdash; when a store does not
            answer, the exchange stands in for it at the list price its roster row carried,
            and that number reaches the card as the slot&rsquo;s <code>price</code> like any
            other. The slot carries no <code>fallback</code> flag &mdash;{' '}
            <code>ShortlistSlot</code> has no such field &mdash; so a card cannot tell you
            which of the two happened, and this page does not guess. Which stores answered and
            which were stood in for is in <code>entries[]</code>, stated per store, in the
            panel that opens when the shortlist is empty and in the verbatim answer.
          </li>
          <li data-testid="gap-product-name">
            <strong>Product: a reference, not a name</strong> &mdash; the slot carries{' '}
            <code>product_ref</code> (and a <code>variant_ref</code> where the bid named one),
            which is what the roster and the offer agree on. It is not a title: a title
            belongs to the store&rsquo;s own catalogue, nothing in this app resolves one, and
            the exchange does not copy one through. So the card shows the reference the
            exchange sent rather than a product name this page would have had to invent.
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
