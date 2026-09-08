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
 * 4. **The gaps are on the screen, permanently.** Whichever of a price and a stand-in the
 *    slot is carrying is not on the slot; the product arrives as a reference rather than a
 *    name; the shop's own voice is dropped at the exchange's shortlist contract; step 5's
 *    order is seeded; and the clarifying questions come from the buyer service's offline
 *    model double rather than a live model. All five are stated in the UI rather than faked,
 *    because a demo that supplies its own join is the defect this app exists to not be.
 *
 *    THAT SENTENCE NOW NAMES TWO GAPS, NOT FIVE, and everything else in it is history kept
 *    on purpose. What remains listed below is step 5's seeded order and the page's inability
 *    to say whether a live model wrote the clarifying questions. The retirements, newest
 *    last, each recorded here because a gap that closes quietly is indistinguishable from a
 *    gap that was talked away:
 *
 *    `gap-price` and `gap-domain` — the sentence used to open "the exchange's slot carries
 *    neither a store domain nor a price". Both closed. Re-measured on the devstack: a slot
 *    comes back with `price` on it, and with `store_domain` set to the platform registry's
 *    answer (`demo-woolworks.example.com`), which `ShortlistView` prints at the head of the
 *    card. `contracts.protocol.ShortlistSlot` declares `store_domain: str | None`, and
 *    `ranking/serving.py` joins it on. An exchange with no registry still publishes none,
 *    and the card says so.
 *
 *    `gap-fallback`, `gap-product-name` and `gap-store-voice` — three bullets, but ONE gap
 *    wearing three faces, which is why they retire together. Each said, in its own words,
 *    that a fact was computed on both sides of the wire and had nowhere to sit in the
 *    middle: `contracts.protocol.ShortlistSlot` was `extra="forbid"` and declared no field
 *    for whose price it was, for what the thing was called, or for what the shop had said.
 *    `98529bd` declared all three at once (SCHEMA_VERSION 2.0.0 → 3.0.0) and the
 *    `extra="forbid"` survived it, so nothing was loosened to make room.
 *
 *      * WHOSE PRICE. The slot carries `fallback` and `fallback_reason` in THREE states —
 *        `false` a bid the store really sent, `true` the exchange standing in at its roster
 *        row's list price, `null` a producer that never said. `ShortlistView` prints a
 *        sentence for the second and the third and, deliberately, nothing for the first.
 *      * WHAT IT IS CALLED. The retired bullet argued a title "belongs to the store's own
 *        catalogue, nothing in this app resolves one". The premise was true and the
 *        conclusion was wrong, because the name is not the store's: `exchange.ranking
 *        .verification.catalog_identity` reads it off the PLATFORM's own crawl — the same
 *        snapshot the exchange already grades the store's claims against, gated in Cypher to
 *        sources the platform observed itself, reachable from no bid — and publishes it as
 *        `product.identity` with the snapshot id that produced it. That is the ORGANIC half
 *        of D55 stated about a product instead of about a shop, so the card renders the name
 *        and attributes it to Proxyshop rather than letting it read as the shop's.
 *      * WHOSE WORDS. `ShortlistSlot.message` carries the seller's bytes verbatim,
 *        `buyer_svc.pitch.writing.store_pitch_of` reads them off it, and `PitchPanel`
 *        renders them in the labelled store blockquote it has had all along. The sponsored
 *        half of D55 — the one thing a shop buys by joining — reaches the screen.
 *
 *      MEASURED FOR THIS CHANGE, and the measurement is narrower than the claim, which is
 *      exactly why it is written down rather than summarised as "verified". THE DEVSTACK'S
 *      OWN CONTAINERS STILL SHIP `contracts` 2.0.0 — they predate `98529bd` — so a live
 *      `POST /buyer/shortlist/render` against them serves no `identity`, no `fallback` and
 *      `store_pitch: null` on every slot. What was driven instead is the repo's own buyer
 *      service, in-process, on the real four-slot shortlist a live `milk thistle liver
 *      support` auction produced against the running exchange: the pinned `Shortlist` model
 *      accepted the exchange's post-`98529bd` shape, and `/buyer/shortlist/render` answered
 *      with `identity: {"title": "Milk Thistle Gummies", "brand": "Gaia Herbs", "source":
 *      "neo4j-crawl:gaiaherbs.com:prod_5b31…", …}` and `fallback: false` on one slot,
 *      `fallback: true` with `fallback_reason: "store_declined:cluster_not_pursued"` on a
 *      second, a brand-less and stamp-less identity on a third, and `identity: null,
 *      fallback: null` on the fourth. `readProduct` and `readRenderedSlots` were written
 *      against that response rather than against the schema. This paragraph comes out when
 *      the containers are rebuilt and the served route answers the same way.
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
 * 6. **Sign in first, then the journey.** A blocking gate: a visitor with no session sees the
 *    sign-in form and no part of the journey.
 *
 *    WHAT THIS SECTION USED TO SAY, kept visible rather than deleted, because the reversal is
 *    the interesting part and a reader will find the old shape in the tests' history. It read
 *    "Browsing is open; confirming is not", and argued that steps 1 and 2 should run with no
 *    session because nothing in them leaves this origin, so "a login wall in front of a page
 *    whose whole purpose is to show what this market does would be asking for an email to see
 *    a demo". The gate therefore sat on the confirm control alone, which was withheld from
 *    the document until a session existed.
 *
 *    That argument was sound about the DATA and wrong about the EXPERIENCE, which is the half
 *    it never measured. A visitor typed what they wanted, answered the clarifying questions,
 *    and only at the end discovered there was nothing they could do with any of it — the
 *    conversation they had just had was unusable until they went to their mailbox, and
 *    opening the mailed link reloads this page and throws that conversation away. The wall
 *    was not removed by putting it late; it was moved to the most expensive possible moment.
 *
 *    So the gate is now where a person expects a gate: in front. Signing in is the first
 *    thing asked and the only thing offered, the reasoning for why it is required is stated
 *    on the form itself rather than five steps later, and the conversation a buyer has after
 *    signing in is one they can actually finish.
 *
 *    WHAT DID NOT CHANGE, and must not be read as having changed. The reason a session is
 *    required is exactly what it was: `POST /buyer/intent/confirm` is the gesture that leaves
 *    this origin, the exchange solicits real stores, and each is handed a pseudonym that R5
 *    says is the service's to mint and rotate. That sentence has moved onto the sign-in form
 *    (see `SignIn.tsx`) rather than being dropped — it is the honest answer to "why do I have
 *    to sign in to look around", and a gate that could not answer that question would be
 *    worse than the one it replaced.
 *
 *    THE COST, named rather than hidden, and it is larger than it was. This page persists no
 *    session — see the note on `session` below, which is unchanged: the id is a bearer
 *    credential and does not go to `localStorage` or `sessionStorage`. Redeeming a link does
 *    NOT reload the page (`history.replaceState` rewrites the entry in place), so arriving
 *    from the mailbox lands in the chat exactly as it should. But any LATER reload — F5, a
 *    restored tab, a crash — now ends the session with the whole journey behind it rather
 *    than just the confirm button, and the link is single-use and already stripped from the
 *    address bar. Recovering means requesting a new link. That is a real regression against
 *    the old shape and it is written here rather than discovered.
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
import { FeedbackPromptView } from '../feedback/FeedbackPromptView'
import { REMEMBERED_AUCTION_KEY, forget, remember } from '../metrics/telemetry'
import { submitFeedback, type FeedbackReceipt } from '../feedback/feedback'
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
import {
  SEEDED_PREFIX,
  SEEDED_PROMPT,
  SEEDED_REFUSAL,
  seededOrder,
} from './seeded-feedback'
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
  // The seeded post-purchase panel's own two pieces of state. Kept beside the journey's
  // rather than inside `FeedbackPromptView`, because that component is deliberately I/O-free
  // — it renders a prompt, a receipt or a refusal and owns none of them.
  const [feedbackReceipt, setFeedbackReceipt] = useState<FeedbackReceipt | undefined>(undefined)
  const [feedbackError, setFeedbackError] = useState<string | undefined>(undefined)
  // Its OWN busy flag, not the journey's. The journey's `busy` is true while an auction is
  // opening, and passing it here would grey out a panel that has nothing to do with that.
  const [feedbackBusy, setFeedbackBusy] = useState(false)

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
      // …and the id the demo's metrics page was told to remember, for the same reason and
      // not a weaker one. Clearing only the React state would take the auction off THIS
      // screen while leaving `#/metrics` able to reopen the retired pseudonym's full
      // recorded trace — every solicited store, every answer, every exclusion reason — from
      // a route that takes no session header. The gloss below promises signing out "clears
      // the auction below with it"; this is the half of that promise the screen cannot keep
      // on its own.
      forget(REMEMBERED_AUCTION_KEY)
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
        // `/render` labels a shortlist AND writes each candidate's case (D55). When the
        // exchange has forgotten this auction there is no shortlist to label — not an empty
        // one, none — so the call is not made at all rather than made with `null` and its
        // refusal shown as a failure.
        //
        // The intent and the profile go WITH it, and they are the same two objects the
        // confirm above sent: the intent this buyer confirmed and the coarsened profile the
        // buyer service's own vault and coarsener answered with. Neither is composed here.
        // Without them the buyer-side agent writes the UNCONDITIONED reading — measured on
        // the devstack, `"Offer held until: …. Also price: 78.00 USD; free returns: 30
        // days."` — instead of the per-shopper one, `"You said price was a must-have, and
        // here it is: 78.00 USD. …"`. Both are the platform's own voice; only the second is
        // the case D55 is about, and this page had the material for it all along.
        const slots =
          record.liveness === 'forgotten'
            ? []
            : await renderShortlist(record.shortlist, wire.fetcher, {
                intent,
                profile: { pseudonym: profile.pseudonym, buckets: profile.buckets },
              })
        setStage({ created, record, slots })
        // Leave the auction id where the demo's metrics page can offer it, so a driver who
        // switches to `#/metrics` does not have to copy an identifier by hand. An auction id
        // and NOTHING else: `GET /buyer/auctions/{id}` needs no authentication, so this is
        // not a credential, and the session id above stays in memory exactly as its own
        // comment requires. `remember` swallows a storage that refuses.
        remember(REMEMBERED_AUCTION_KEY, created.auction_id)
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

  /**
   * Answer the seeded prompt through the REAL wire.
   *
   * `submitFeedback` is the same function a mounted prompt would call, posting to the same
   * `POST /buyer/feedback`. Nothing here short-circuits it and nothing pre-cooks a receipt:
   * the panel shows whatever the service says, including a refusal, because a panel that
   * cannot fail is not evidence that anything works. The answer that lands carries
   * `order_ref` beginning `sim-fb-`, so the ledger event it becomes stays marked as
   * manufactured for the life of the chain.
   *
   * Deliberately NOT routed through `run`: `run` owns the journey's own failure banner and
   * busy flag, and a seeded side-panel must not be able to blank the page a shopper is
   * reading. Its errors stay inside its own section.
   */
  const answerSeededPrompt = useCallback(
    async (choice: string) => {
      if (SEEDED_PROMPT === null) return
      setFeedbackError(undefined)
      setFeedbackBusy(true)
      try {
        setFeedbackReceipt(
          await submitFeedback(seededOrder(SEEDED_PROMPT), SEEDED_PROMPT, choice, wire.fetcher),
        )
      } catch (error) {
        setFeedbackError(error instanceof Error ? error.message : String(error))
      } finally {
        setFeedbackBusy(false)
      }
    },
    [wire],
  )

  const answers = turns.slice(1)
  // One condition for "signed in", used by the sign-in panel, the pseudonym line and the
  // gate alike. The two pieces of state are set together and cleared together, so a
  // half-signed-in page is not reachable — and deriving the gate from the SAME thing
  // `confirm` needs is what makes the refusal inside `confirm` genuinely unreachable rather
  // than merely unlikely.
  //
  // WHAT THIS REPLACED. There used to be a second derived flag here, `gateOnSignIn`, and a
  // matching `confirmable` that restated `IntentConfirm`'s own mounting conditions so that
  // the confirm BUTTON could be withheld from a signed-out page while everything around it
  // still rendered. Both are gone, and not because the concern went away: the concern is now
  // structural. The journey — every step of it, including the one that holds that button —
  // is inside `signedIn` below, so `IntentConfirm` cannot be mounted by a signed-out page at
  // all. A flag computing "would the button be gated" would now be constant `false` in every
  // reachable state, and a test asserting on the gated branch would be asserting a branch the
  // page can no longer enter. The single structural gate is the guard; there is no second one
  // to fall out of step with it.
  const signedIn = session !== null && profile !== null
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
          the service on this origin during this session, except in the two places that say so
          where they appear: the grey text inside the box is a hint rather than an answer, and
          step 5&rsquo;s order is manufactured &mdash; badged SEEDED on the panel itself and
          listed under &ldquo;What is not wired yet&rdquo;.
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
          <>
            {/*
             * THE REASON, ON THE GATE. This paragraph is the one that used to sit inside step
             * 2 as `confirm-gated`, and it moved here with the gate rather than being deleted
             * with it. It is the honest answer to "why do I have to sign in to look around",
             * and a wall that cannot answer that question is a worse wall than the one this
             * replaced. The claim is unchanged and still true: confirming is the gesture that
             * leaves this origin, and the handle the stores are told has to be minted by the
             * service's vault rather than by this page.
             */}
            <p data-testid="why-sign-in">
              <strong>Signing in is what lets the exchange ask the stores.</strong> Everything
              you type here stays between you and this origin&rsquo;s own buyer service until
              you confirm. Confirming is the step that leaves it: the exchange solicits real
              stores, and each of them is told a pseudonym and a handful of coarse buckets.
              That pseudonym has to come from the buyer service&rsquo;s vault rather than from
              this page &mdash; a handle this browser minted would not rotate when the vault
              rotates, which is the stable identifier R5 exists to deny the stores &mdash; so
              the conversation starts once you are signed in.
            </p>
            {/*
             * The signed-out privacy statement, moved here from step 1's pseudonym line when
             * the gate went in. It used to render beside the composer for a visitor who had
             * not signed in; there is no such visitor any more — step 1 is behind this gate —
             * so without this it would render in no state at all. It is the claim a person
             * most reasonably wants answered BEFORE handing over an address, which makes the
             * gate a better home for it than the place it came from.
             */}
            <p className="gloss" data-testid="pseudonym-signed-out">
              No store has been told anything about you, and there is no handle to tell them
              with: you are not signed in. Nothing you type after signing in leaves this
              origin&rsquo;s own buyer service until you confirm.
            </p>
            <SignIn onRequestLink={askForLink} linkExpiresAt={linkExpiresAt} busy={busy} />
          </>
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

      {/*
       * THE GATE. Everything from step 1 to step 5 is the journey, and the journey needs a
       * session — so a visitor without one sees the sign-in panel above and nothing of this.
       *
       * WHAT IS DELIBERATELY OUTSIDE IT, in both directions:
       *
       *   * ABOVE — the masthead, the failure banner and the sign-in panel. A person who
       *     cannot sign in has to be able to SEE why: the refusal from a spent link, or a
       *     deployment with no mail transport answering 503, is rendered by that banner, and
       *     gating it would leave a blank wall as the only response to a broken sign-in.
       *   * BELOW — "What is not wired yet". Those bullets are this page's standing account
       *     of what it does not do, and none of them is about a signed-in buyer or reads any
       *     session state. They are as true, and as worth reading, to somebody deciding
       *     whether to sign in at all.
       *
       * Step 5 IS inside, though it is seeded and takes no session of its own: it is a
       * post-purchase prompt, and showing a purchase-feedback form to a visitor who has not
       * signed in would be the page telling a story about a journey they have not had.
       */}
      {!signedIn ? null : (
        <>
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
        {/*
         * This used to branch on `profile === null`, with a signed-out arm reading "No store
         * has been told anything about you, and there is no handle to tell them with: you are
         * not signed in. What you type here goes to this origin's own buyer service and no
         * further until you sign in and confirm."
         *
         * That arm is unreachable now — this section is inside the gate, so `profile` is
         * never null here — and an unreachable branch that carries the page's only signed-out
         * privacy statement is worse than dead code: the sentence would render in NO state at
         * all. Its claim is still true and still worth making, so it moved to where a
         * signed-out visitor can actually read it, on the sign-in gate above, rather than
         * being deleted along with the branch that had stopped being reachable.
         */}
        <p className="gloss" data-testid="pseudonym">
          The stores are told you are <strong>{profile.pseudonym}</strong>, and nothing else.
          That handle was minted by the buyer service&rsquo;s pseudonym vault when you signed
          in &mdash; this browser cannot mint one &mdash; and signing out retires it.
        </p>
      </section>

      {outcome !== undefined && stage === undefined ? (
        <section aria-label="Step 2 - check what we understood" className="step">
          <h2>
            <span className="ordinal">2</span> Check what we understood
          </h2>
          {/*
           * No sign-in branch here any more, and its absence is the point. This whole section
           * is inside the `signedIn` gate below, so there is no reachable state in which this
           * page renders step 2 to a visitor without a session — the confirm control cannot be
           * mounted signed out, which is the property the old `confirm-gated` branch existed
           * to provide and provided only for this one control.
           *
           * The sentence that branch carried — that the next step is the one leaving this
           * origin, that the exchange solicits real stores, and that the pseudonym has to come
           * from the service's vault rather than from this page — has NOT been deleted. It is
           * the reason a person is asked to sign in at all, so it now sits on the sign-in form
           * itself, where it is read before the decision rather than after the conversation.
           */}
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

                  {/* THE RECORD, folded away. Design system section 06, rule 3: "Provenance
                      is on the row, not behind a hover. The RECORD folds away; the LABELS
                      never do." Everything inside this fold is a value the system wrote —
                      which label came from where, what the exchange ranked each slot at, and
                      the whole answer verbatim — and none of it is something a shopper has to
                      open in order to choose. Nothing was hidden to get here: both blocks
                      below are the same blocks that used to sit open on the page, moved
                      wholesale into one gesture.

                      What is deliberately NOT in here is the provenance pill row on each card
                      above, and it must stay out. Those are the labels, and a label a buyer
                      has to go looking for is a label they did not have when they chose —
                      `ShortlistView`'s own docstring, rule 1. `.trace` styles this fold. */}
                  <details className="trace" data-trace="auction-record" data-testid="auction-record">
                    <summary>
                      Show the record &mdash; where each label came from, what the exchange
                      ranked each slot at, and the service&rsquo;s whole answer verbatim
                    </summary>
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
          {code !== undefined ? (
            // Said BEFORE the link, not after it. The store this permalink points at is a real
            // storefront that Proxyshop has no integration with, so the code above is one this
            // exchange minted and that storefront has never heard of: the cart will load and
            // the code will be refused at the discount field. That is the honest state of the
            // system, and a shopper who meets it as an unexplained error has hit what reads as
            // a bug. Naming it here — above the button, in the same box as the code — is what
            // makes it a thing the person running a demo can point at instead of apologise for.
            <p className="gloss" role="note" data-testid="discount-code-is-a-demonstration">
              Heads up: <strong>{code}</strong> is a demonstration code. Proxyshop minted it, and{' '}
              {host ?? 'the store'} is a real storefront we are not integrated with, so it has no
              record of this code and will reject it at checkout. It shows the shape of what an
              integrated store would honour. Everything else on the page — the cart, the product
              and the price — is real.
            </p>
          ) : null}
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

      {/*
        Step 5 is SEEDED, and unconditional — it does not wait on `accepted`, because the
        journey it would follow does not exist. R14's prompt needs an order reference that
        arrives days after delivery, and the served journey ends at the checkout handoff and
        never obtains one. So the panel below is a manufactured prompt shown in the place the
        real one will occupy, and every word of that is said on the page rather than left for
        a reader to discover.

        What is NOT faked: `FeedbackPromptView` is the real component, the options are the
        five `apps/buyer/svc/src/feedback/prompt.py` publishes, and answering posts to the
        real `POST /buyer/feedback`. Only the order is manufactured.
      */}
      <section aria-label="Step 5 - after your purchase (seeded)" className="step seeded">
        <h2>
          <span className="ordinal">5</span> After your purchase{' '}
          <span className="seeded-badge" data-testid="feedback-seeded-badge">
            SEEDED
          </span>
        </h2>
        {SEEDED_PROMPT === null ? (
          <p role="alert" data-testid="feedback-seed-refused">
            The seeded prompt was refused rather than shown: {SEEDED_REFUSAL}
          </p>
        ) : (
          <>
            <p data-testid="feedback-seeded-explanation">
              Nothing on this page reached this panel. Proxyshop asks this question days after
              delivery, and this session has not bought anything — so the order below was
              <strong> manufactured</strong> to show the prompt&rsquo;s shape. You can tell it
              apart from a real one without being told: its order reference is{' '}
              <code className="mono">{SEEDED_PROMPT.order_ref}</code>, and every manufactured
              reference begins <code className="mono">{SEEDED_PREFIX}</code>. The buyer service
              copies that reference verbatim onto the trust ledger&rsquo;s hash chain, so an
              answer given here stays marked as manufactured for as long as the chain exists,
              and a real answer could not be marked without breaking the chain.
            </p>
            <p className="gloss">
              The question, the five answers and the form are the real ones. Sending an answer
              posts to the real <code className="mono">POST /buyer/feedback</code> and shows
              whatever it replies — including a refusal.
            </p>
            <FeedbackPromptView
              prompt={SEEDED_PROMPT}
              onSubmit={answerSeededPrompt}
              receipt={feedbackReceipt}
              error={feedbackError}
              busy={feedbackBusy}
            />
          </>
        )}
      </section>
        </>
      )}

      <section aria-label="What is not wired yet" className="gaps">
        <h2>What is not wired yet</h2>
        <ul>
          <li data-testid="gap-feedback-seeded">
            <strong>Step 5 is seeded, and the real prompt is unreachable</strong> &mdash; the
            question in step 5 is real, the form is the real{' '}
            <code>FeedbackPromptView</code>, and answering it posts to the real{' '}
            <code>POST /buyer/feedback</code>. The <em>order</em> is not: Proxyshop asks that
            question days after delivery, and this journey ends at the checkout handoff
            without ever obtaining an order reference, so there is nothing here for a real
            prompt to be about. Mounting the component was never the missing piece &mdash;
            the order reference is &mdash; so the panel shows a manufactured one instead of an
            empty box, and marks it: every seeded reference begins{' '}
            <code>{SEEDED_PREFIX}</code>, and the buyer service copies that reference verbatim
            onto the trust ledger&rsquo;s hash chain, which is why a seeded answer cannot
            later be mistaken for an earned one.
          </li>
          <li data-testid="gap-model">
            <strong>Whether a live model wrote the questions: this page cannot tell you</strong>{' '}
            — the buyer service resolves its client with{' '}
            <code>build_llm(&quot;buyer&quot;)</code>, which returns a real provider when{' '}
            <code>LLM_PROVIDER</code> names one and D20&rsquo;s offline double when it does
            not. Both are designed behaviour and neither is a failure. What is missing is the
            disclosure: <code>POST /buyer/intent/clarify</code> answers with{' '}
            <code>questions</code>, <code>intent</code>, <code>unresolved</code> and{' '}
            <code>confirmed</code>, and no field on it &mdash; nor on any other route this
            origin serves &mdash; says which client answered. The service computes that fact
            internally and writes it to its own log, and it reaches no response body, so a
            page that told you either way would be guessing.
            <br />
            <span className="gloss">
              What this bullet used to say, and why it changed: it asserted flatly that the
              clarifying questions had been written by the offline double, because{' '}
              <code>LLM_PROVIDER</code> was unset. That was measured and true of the tree it
              was written on, and it is false on any deployment configured with a key &mdash;
              a claim the page had no way to check and therefore should never have made
              unconditionally. Making it conditional needs a served field: a{' '}
              <code>model</code> or <code>source</code> on the clarify response, or a small
              runtime route reporting what <code>resolve_provider()</code> answered.
            </span>
          </li>
        </ul>
      </section>
    </main>
  )
}

export default Journey
