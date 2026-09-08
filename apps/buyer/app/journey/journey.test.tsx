/**
 * The buyer journey shell — the wire it owns, and the four beats it walks a shopper through.
 *
 * Every request here is answered by an INJECTED fetcher. Nothing in this file touches the
 * network: the point of the suite is that the shell renders what a service said and refuses
 * to render anything else, and a test that reached a real service could not tell the two
 * apart when the service was down.
 *
 * MEASURED, and said precisely rather than sweepingly, because "these are the measured ones"
 * is itself a claim a fixture can get wrong:
 *
 *   * `INTENT` is what `buyer_svc.intent.clarifier.clarify` really returns for the opening
 *     utterance below — query, budget band, both hard constraints, no preferences — so
 *     `cluster_id` is genuinely the hash of THIS intent. The page prints that id and calls it
 *     "a hash of the use case, the budget band and the constraints above", and an earlier
 *     fixture asserted a cluster id belonging to a different intent, which made that sentence
 *     false on the page while the suite stayed green.
 *   * The three store ids are the only three in `apps/buyer/devstack/demo-market.json`, and
 *     the default auction body is that market as written: all three are `"eligible"`, so all
 *     three are solicited and all three really bid, and demo-fastfleece is then thrown out by
 *     the ranking's blacklist filter. The market file states exactly this.
 *   * Every exclusion prefix comes from `apps/exchange/src/ranking/reasons.py` and every
 *     detail after it was produced by running that filter, not composed here. The denial
 *     string is `exchange.eligibility`'s as re-prefixed by `solicitation._denial`.
 *   * Every `components` key is one of `contracts.ranking.RANK_FEATURES` and the five values
 *     sum to the stated `rank_score`.
 *
 * CONSTRUCTED, and labelled where it appears: `CLARIFY_ANSWER`'s question and `unresolved`
 * (the real clarifier asks nothing for that one utterance, and a test of the clarify loop
 * needs a question), the denial in `DENIED_VARIANT`, and the silent store in `SILENT_ENTRY`.
 * Those three are states this market does not currently produce. Each uses only real
 * vocabulary and each is reached through a fixture named for what it is, so no test claims
 * the demo market produces them.
 *
 * Interaction is driven with `fireEvent` rather than `user-event`, which this workspace does
 * not carry — same as `intent/intent-confirm.test.tsx` and `shortlist/shortlist.test.tsx`.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  MAGIC_LINK_PATH,
  PROFILE_PATH,
  SESSION_HEADER,
  SESSION_PATH,
  SIGN_IN_PATH,
} from '../chat/session'
import { ASK_PATH } from '../chat/ask'
import { CLARIFY_PATH, CONFIRM_PATH, type Intent } from '../intent/intent'
import { REMEMBERED_AUCTION_KEY } from '../metrics/telemetry'
import { ACCEPT_PATH } from '../shortlist/shortlist'
import { Journey } from './Journey'
import { SEEDED_PREFIX, SEEDED_PROMPT } from './seeded-feedback'
import { UNRECOGNISED_GLOSS, explainFallbackReason, glossedReasons } from './WhyEmpty'
// The whole module, so a test can assert what it does NOT export. `mintPseudonym` was
// deleted with this change and a re-added one must fail a test rather than a review.
import * as wireModule from './wire'
import {
  FALLBACK_REASON_FAMILIES,
  HttpFailure,
  MalformedResponseError,
  MissingProfileError,
  PSEUDONYM_PREFIX,
  RENDER_PATH,
  auctionPath,
  confirmWithProfile,
  describeComponents,
  describeTrust,
  detailFromBody,
  discountCodeFrom,
  explain,
  fallbackReasonDetail,
  fallbackReasonFamily,
  instrumentFetcher,
  loadAuction,
  permalinkHost,
  rankedForSlot,
  renderShortlist,
  storeIdFromBidRef,
  type AuctionEntry,
  type Fetcher,
} from './wire'

// `@testing-library/react` registers its own cleanup only when the runner exposes
// `afterEach` globally, and this workspace's vitest projects do not set `globals: true`.
afterEach(cleanup)

/**
 * The token out of the mailed link, and the two things the service answers with.
 *
 * `VAULT_PSEUDONYM` is thirty-two hex characters because that is what
 * `buyer_svc.vault.pseudonyms.default_pseudonym` mints — `psn-` + `secrets.token_hex(16)` —
 * and the shape is the assertion rather than decoration: the handle this page used to mint
 * in the browser was `psn-` + TEN hex characters, so a page that had quietly gone on minting
 * its own could not produce this value. `signed-in-pseudonym` is checked against the regex
 * below wherever it matters.
 */
const TOKEN = 'tok-out-of-the-mailbox'
const SESSION_ID = 'sess-demo-1'
const VAULT_PSEUDONYM = 'psn-3f9c1d47b20a8e56c4d1f0ab7e93d258'
const VAULT_PSEUDONYM_SHAPE = /^psn-[0-9a-f]{32}$/

/**
 * The buckets `GET /buyer/profile` really answers with: the five facets
 * `buyer_svc.profile.BUCKET_KEYS` pins and nothing else, which is exactly the field set
 * `contracts.ProfileBuckets` allows (it is `extra="forbid"`, so a sixth key would earn a 422
 * from every store agent). Values here are plausible rather than measured; the KEY SET is
 * the part a store's contract cares about.
 */
const BUCKETS = {
  budget_band: '100-250',
  category_affinity: ['apparel'],
  frequency_tier: 'new',
  region: null,
  first_time: true,
}

/** The address a buyer types, and everything about them that must never reach a store. */
const BUYER_EMAIL = 'dana.reyes@example.com'
const IDENTITY = ['dana', 'reyes', 'example.com', TOKEN, SESSION_ID]

/**
 * Every render starts the way a browser really arrives after a buyer clicks their emailed
 * link: at this origin with the single-use token in the query string. Tests that need the
 * signed-out page call `signedOut()` to take it away again.
 */
beforeEach(arrivingFromTheMailbox)

afterEach(signedOut)

/**
 * The address bar a browser really has after the buyer clicks their emailed link.
 *
 * Called again between renders inside one test, because the page takes the token OUT of the
 * address bar as soon as it has read it — a single-use bearer credential must not sit in the
 * history entry — so a second `render` in the same test is a second VISIT and needs a second
 * link, exactly as a buyer would.
 */
function arrivingFromTheMailbox(): void {
  window.history.replaceState({}, '', `/?token=${TOKEN}`)
}

/** No token in the address bar: a first-time visitor who has not signed in. */
function signedOut(): void {
  window.history.replaceState({}, '', '/')
}

const AUCTION_ID = 'auc-demo-1'
// `{auction_id}:{store_id}`, because that is literally what the exchange mints:
// `apps/exchange/src/ranking/candidates.py::mint_bid_id` is `f"{auction_id}:{store_id}"`.
// The price join in `wire.ts` reads the store id back out of it, so the two spellings below
// are the one fact the join depends on and are written out rather than concatenated.
const BID_REF = 'auc-demo-1:demo-woolworks'
const FASTFLEECE_BID_REF = 'auc-demo-1:demo-fastfleece'
const PERMALINK = 'https://demo-woolworks.example.com/cart/1:1?discount=PSX-MC4DM9A1'
const CREATED_AT = '2026-09-05T00:00:00Z'
const RECORDED_AT = '2026-09-05T00:00:01Z'

// MEASURED. `clarify(['I want a warm merino wool beanie for winter, under $100'])` on this
// tree returns exactly this — the whole utterance as `query`, `budget_band` '100-250', those
// two hard constraints with no `unit`, and NO preferences. It matters that the fields match
// rather than merely look plausible: `_cluster_id` hashes the query, the budget band, the
// constraints and the preferences, so any other combination makes `cl-4d3c3e4edadaa5e7` the
// hash of something else — and the page renders that id as "a hash of the use case, the
// budget band and the constraints above". `intent_id` is per-session by design and is the one
// field here that is not reproducible.
const UTTERANCE = 'I want a warm merino wool beanie for winter, under $100'

const INTENT: Intent = {
  intent_id: 'int-demo-1',
  cluster_id: 'cl-4d3c3e4edadaa5e7',
  query: UTTERANCE,
  budget_band: '100-250',
  hard_constraints: [
    { field: 'material', op: 'eq', value: 'merino-wool' },
    { field: 'price_usd', op: 'lte', value: 100 },
  ],
  preferences: [],
}

const RAW_SLOT = {
  slot: 'fit',
  bid_ref: BID_REF,
  fit_score: 0.564,
  provenance_labels: ['store-confirmed'],
  // MEASURED off the running exchange, and demo-woolworks' own `trust.score` in the market
  // file is the 0.82. There is no `confidence` field on a trust summary.
  trust_summary: { store_id: 'demo-woolworks', available: true, score: 0.82 },
  // The decoy the acceptance fixture ships: a slot may carry one and the buyer never
  // follows it. It is here so the "no href before accept" assertion has something to catch.
  checkout_url: 'https://evil.example.com/cart/hijack',
  claims: [{ field: 'material', value: 'merino-wool', provenance: { source: 'store_api' } }],
}

// The shape `POST /buyer/shortlist/render` answers with. MEASURED against the devstack
// (`apps/buyer/devstack/run.py`, three real store agents + the real exchange + the real buyer
// service): `product`, `price` and `commitments` come back on every slot, and the commitment
// carries a `label` the buyer service derived from the claim's own provenance. The AUCTION ID
// here is this file's (`auc-demo-1`); the run's was a uuid, and every other value below is
// the run's own.
//
// `store_domain: ''` NEEDS ITS OWN SENTENCE, because the one that used to be here is false.
// It read: "`store_domain` really is `''` — `contracts.protocol.ShortlistSlot` has no such
// field, so a slot served through the pinned model cannot carry one." Both halves are now
// wrong, and the same change is what retired `gap-domain` further down this file:
// `ShortlistSlot` declares `store_domain: str | None = None`, and `ranking/serving.py` joins
// the platform registry's answer onto the slot before the shortlist is served. Re-measured on
// the devstack this run, the two bidding slots came back
// `demo-woolworks.example.com` and `demo-alpine-supply.example.com`.
//
// The VALUE stays `''` on purpose rather than being updated to a domain, because `''` is
// still a served shape and it is the one nothing else here covers. `serving.py` publishes
// `None` — never `''` — when the deployment has no platform registry to answer from, and
// `readRenderedSlots` reads that absence back as `''`. So this fixture is the "the exchange
// vouches for no host" slot: `acceptSlot` sends `expected_domain: null` for it, which is what
// the accept-body assertion at the end of the four-beats test pins, and `ShortlistView` prints
// the absence sentence rather than a blank line. The populated case is asserted directly, on
// both branches, in `shortlist.test.tsx`'s "names whose shop it is".
const RENDERED_SLOT = {
  slot: 'fit',
  bid_ref: BID_REF,
  auction_id: AUCTION_ID,
  fit_score: 0.564,
  provenance_labels: ['store-confirmed'],
  labels_source: 'exchange',
  // MEASURED off the running exchange: two of these three are not numbers, and
  // `TrustSummary` is `Record<string, number>`. The page must still show all three.
  trust_summary: { store_id: 'demo-woolworks', available: true, score: 0.82 },
  store_domain: '',
  product: { product_ref: 'beanie-merino-01', variant_ref: '44352913' },
  price: {
    unit_price: 78.0,
    total_price: 78.0,
    currency: 'USD',
    discount: null,
    expires_at: '2026-09-05T00:15:00Z',
  },
  commitments: [{ key: 'free_returns', value: '30 days', unit: null, label: 'store-confirmed' }],
}

// MEASURED against `apps/buyer/devstack/demo-market.json`. All three stores carry
// `"eligibility": "eligible"`, so all three are solicited and all three really bid — the
// market file's own note on demo-fastfleece says so: "it is solicited, it really bids, and it
// is then thrown out by the ranking's blacklist filter". Prices are the catalogue prices in
// that file. An earlier fixture made demo-fastfleece a silent `no_response` fallback, which
// contradicted both the market file and its own exclusion reason below.
const ENTRIES = [
  {
    store_id: 'demo-woolworks',
    tier: 1,
    fallback: false,
    unit_price: 78.0,
    total_price: 78.0,
    fallback_reason: null,
  },
  {
    store_id: 'demo-alpine-supply',
    tier: 1,
    fallback: false,
    unit_price: 72.0,
    total_price: 72.0,
    fallback_reason: null,
  },
  {
    store_id: 'demo-fastfleece',
    tier: 1,
    fallback: false,
    unit_price: 45.0,
    total_price: 45.0,
    fallback_reason: null,
  },
]

const SOLICITED = ['demo-woolworks', 'demo-alpine-supply', 'demo-fastfleece']

// demo-fastfleece earns both, and both are real. `blacklisted_store` is `ranking/filters.py`'s
// f-string for a store the trust snapshot blacklists (R12) — the market file marks this one
// `"blacklisted": true`. The `hard_constraint_unsatisfied` detail is `HardCriterion.decide`'s
// verdict, measured by running that criterion against a verified `fleece` reading, which is
// the reading a store that BID with claims produces. (A fallback carries `claims: []` and
// yields the "carries no such attribute" verdict instead — see `SILENT_EXCLUSION` below. The
// two are not interchangeable, and pairing a fallback entry with this string described an
// auction that cannot have happened.) There is no `price_over_budget` prefix on this
// exchange; `EXCLUSION_REASON_PREFIXES` names the eight that exist and that is not one.
const EXCLUDED = [
  {
    bid_ref: FASTFLEECE_BID_REF,
    store_id: 'demo-fastfleece',
    exclusion_reasons: [
      "blacklisted_store: 'demo-fastfleece' is blacklisted and may not participate (R12)",
      "hard_constraint_unsatisfied: 'material' eq 'merino-wool' is not satisfied by " +
        "['fleece'] — only a verified supporting claim satisfies a hard constraint (R19)",
    ],
  },
]

// Nothing is denied in this market: a denial happens before solicitation, and all three
// stores are eligible. The default body therefore carries an EMPTY `denied[]`, which is the
// honest reading of the market file rather than a row added so a panel has something to show.
const DENIED: readonly { store_id: string; status: string; reason: string }[] = []

// CONSTRUCTED, from the market file's own note: "Change `eligibility` to 'blacklisted' and it
// lands in `denied[]` instead, which is a different (also real) story." Produced by running
// `StaticSellerEligibility({'demo-fastfleece': BLACKLISTED}).check(...)` through `_denial`.
// In this state demo-fastfleece is never solicited, so it has no entry and no exclusion.
const DENIED_VARIANT = [
  {
    store_id: 'demo-fastfleece',
    status: 'blacklisted',
    reason: 'blacklisted: static-eligibility: demo-fastfleece is blacklisted',
  },
]

// CONSTRUCTED: a store whose agent did not answer at all. `no_response` is the exchange's
// word for exactly that — one of the families `wire.ts` carries — and the exchange then
// represents the store at the list price its roster row carried. Paired with the exclusion
// reason a fallback really produces — `claims: []` makes every hard constraint undecidable,
// measured by running `hard_constraint_reasons([], [material eq merino-wool])`.
const SILENT_ENTRY = {
  store_id: 'demo-alpine-supply',
  tier: 1,
  fallback: true,
  unit_price: 72.0,
  total_price: 72.0,
  fallback_reason: 'no_response',
}

const SILENT_EXCLUSION = {
  bid_ref: `${AUCTION_ID}:demo-alpine-supply`,
  store_id: 'demo-alpine-supply',
  exclusion_reasons: [
    "hard_constraint_unsatisfied: 'material': the candidate carries no such attribute, so " +
      'the constraint is undecidable and does not count as satisfied (R19) — only a verified ' +
      'supporting claim satisfies a hard constraint (R19)',
  ],
}

// MEASURED. The five keys are `contracts.ranking.RANK_FEATURES` — there is no `fit` key and
// no bare `fit`/`trust` pair anywhere in the formula — and the five values sum to exactly the
// stated `rank_score` of 0.564, which is what makes them components rather than decoration.
// The `trust` term is 0.164 = 0.82 x 0.20, and 0.82 is demo-woolworks' own `trust.score` in
// the market file.
const RANKED = [
  {
    bid_ref: BID_REF,
    store_id: 'demo-woolworks',
    rank_score: 0.564,
    components: {
      intent_match: 0.175,
      verified_claim_ratio: 0.1,
      trust: 0.164,
      price_value: 0.075,
      delivery_fit: 0.05,
    },
  },
]

interface BodyOverrides {
  readonly recorded_at?: string
  readonly entries?: readonly unknown[]
  readonly excluded?: readonly unknown[]
  readonly denied?: readonly unknown[]
  readonly ranked?: readonly unknown[]
  readonly solicited?: readonly string[]
}

function auctionBody(slots: readonly unknown[], overrides: BodyOverrides = {}) {
  return {
    auction_id: AUCTION_ID,
    shortlist: { auction_id: AUCTION_ID, slots },
    entries: overrides.entries ?? ENTRIES,
    excluded: overrides.excluded ?? EXCLUDED,
    denied: overrides.denied ?? DENIED,
    ranked: overrides.ranked ?? RANKED,
    solicited: overrides.solicited ?? SOLICITED,
    recorded_at: overrides.recorded_at ?? RECORDED_AT,
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

interface Call {
  readonly path: string
  readonly init?: RequestInit
}

/** A fetcher that records what it was asked and answers from a hand-written script. */
function recorder(
  handle: (path: string, init: RequestInit | undefined, nth: number) => Response,
): { readonly fetcher: Fetcher; readonly calls: readonly Call[] } {
  const calls: Call[] = []
  const fetcher: Fetcher = (path, init) => {
    const nth = calls.filter((call) => call.path === path).length
    calls.push({ path, init })
    return Promise.resolve(handle(path, init, nth))
  }
  return { fetcher, calls }
}

function bodyOf(init: RequestInit | undefined): unknown {
  const raw = init?.body
  return typeof raw === 'string' ? (JSON.parse(raw) as unknown) : undefined
}

const CLARIFY_ANSWER = {
  questions: ['What is your budget?'],
  intent: INTENT,
  unresolved: ['colour'],
  confirmed: false,
}

/**
 * The three login routes `buyer_svc/auth/routes.py` serves, answered in their real shapes:
 * `202` with an expiry and NO token, `201` with a session under a vault-minted pseudonym,
 * `200` with the coarsened profile. `DELETE` shares its path with the redemption, so the two
 * are told apart by method.
 *
 * Shared by every script in this file rather than by the happy path alone: the page redeems
 * the token in `beforeEach`'s URL on mount, so a fetcher that 404s these leaves it signed out
 * and no confirm control is ever rendered — which would make an unrelated test fail for a
 * reason that has nothing to do with what it is about.
 *
 * `undefined` means "not a login route", so a caller falls through to its own script.
 */
function authAnswer(path: string, init: RequestInit | undefined): Response | undefined {
  switch (path) {
    // A DEPLOYMENT THAT CAN MAIL, which is what this whole fixture has always described and
    // what every gate assertion in this file is about. `Journey` asks this before it renders
    // anything and shows the sign-in only when it answers `true`, so without this line the
    // fixture would describe a deployment with no mail transport and the gate tests below
    // would be asserting the gate of a page that correctly has none. The no-mail deployment
    // is a fixture of its own — see `withoutMailTransport`.
    case SIGN_IN_PATH:
      return json({ offered: true })
    case MAGIC_LINK_PATH:
      return json({ expires_at: '2026-09-05T00:15:00Z' }, 202)
    case SESSION_PATH:
      return init?.method === 'DELETE'
        ? new Response(null, { status: 204 })
        : json(
            {
              session_id: SESSION_ID,
              pseudonym: VAULT_PSEUDONYM,
              issued_at: '2026-09-05T00:00:00Z',
              expires_at: '2026-09-05T12:00:00Z',
            },
            201,
          )
    case PROFILE_PATH:
      return json({ pseudonym: VAULT_PSEUDONYM, buckets: BUCKETS })
    default:
      return undefined
  }
}

/** The happy path, with `slots` deciding whether the shortlist comes back empty. */
function demoService(
  options: {
    readonly slots?: readonly unknown[]
    /** What `/render` answers with. Defaults to the one slot `slots` implies. */
    readonly rendered?: readonly unknown[]
    /** `shortlist: null` — the exchange's TTL took the auction away. Not an empty one. */
    readonly forgotten?: boolean
    /** `''` is a real answer: `confirmWithProfile` returns it when the service sent none. */
    readonly createdAt?: string
    /** `''` is a real answer too: the buyer service types `recorded_at` as nullable. */
    readonly recordedAt?: string
    /** Swap any diagnostic array — used by the constructed-state tests. */
    readonly body?: BodyOverrides
    /**
     * Answer one of the three login routes differently. Returning `undefined` falls through
     * to the working answer below, so a test names only the refusal it is about.
     */
    readonly auth?: (path: string, init: RequestInit | undefined) => Response | undefined
  } = {},
) {
  const rawSlots = options.slots ?? [RAW_SLOT]
  const rendered = options.rendered ?? (rawSlots.length === 0 ? [] : [RENDERED_SLOT])
  const createdAt = options.createdAt ?? CREATED_AT
  const recordedAt = options.recordedAt ?? RECORDED_AT
  return recorder((path, init) => {
    const overridden = options.auth?.(path, init)
    if (overridden !== undefined) return overridden
    const signedIn = authAnswer(path, init)
    if (signedIn !== undefined) return signedIn
    switch (path) {
      case CLARIFY_PATH:
        return json(CLARIFY_ANSWER)
      case CONFIRM_PATH:
        return json(
          { auction_id: AUCTION_ID, intent_id: INTENT.intent_id, created_at: createdAt },
          201,
        )
      case auctionPath(AUCTION_ID): {
        const body = auctionBody(rawSlots, { ...options.body, recorded_at: recordedAt })
        return json(options.forgotten === true ? { ...body, shortlist: null } : body)
      }
      case RENDER_PATH:
        return json({ slots: rendered })
      case ACCEPT_PATH:
        return json({
          permalink_url: PERMALINK,
          auction_id: AUCTION_ID,
          bid_ref: BID_REF,
          slot: 'fit',
          accepted_at: '2026-09-05T00:00:02Z',
        })
      default:
        return json({ detail: `nothing serves ${path}` }, 404)
    }
  })
}

/**
 * Beat one and the clarifying question.
 *
 * THE FIRST LINE IS THE CHANGE, and it is a real one rather than a wait bolted on to keep a
 * test quiet. This helper's docstring used to say the walk was something "a visitor may do
 * WITHOUT signing in: nothing in this stretch leaves the buyer's own service". The first half
 * of that has stopped being true — the journey is now behind a blocking sign-in gate, so
 * there is no composer in the document until the redemption in `beforeEach`'s URL has landed
 * a session. The second half is untouched and still the reason the gate is where it is:
 * `POST /buyer/intent/clarify` still reaches no exchange and no store.
 *
 * So the walk now begins where a buyer's really begins, by waiting to be signed in. Without
 * it every caller raced the redemption's `await` and asked for a textarea that the page had
 * not rendered yet.
 */
async function walkToIntent(): Promise<void> {
  await screen.findByTestId('signed-in')
  fireEvent.change(screen.getByLabelText('What are you shopping for?'), {
    target: { value: 'I want a warm merino wool beanie for winter, under $100' },
  })
  fireEvent.submit(screen.getByLabelText('What are you shopping for?').closest('form')!)
  const question = await screen.findByLabelText('What is your budget?')
  fireEvent.change(question, { target: { value: 'about $100' } })
  fireEvent.submit(question.closest('form')!)
}

/**
 * The same walk, waiting for the confirm control — which exists only once the redemption in
 * `beforeEach`'s URL has landed a session. Signed out it never appears, by design.
 */
async function walkToConfirm(): Promise<void> {
  await walkToIntent()
  await screen.findByRole('button', { name: /confirm and ask stores/i })
}

describe('the wire the journey owns', () => {
  it('GETs the auction by id and validates the diagnostics it renders', async () => {
    const { fetcher, calls } = recorder(() => json(auctionBody([RAW_SLOT])))

    const record = await loadAuction(AUCTION_ID, fetcher)

    expect(calls.map((call) => call.path)).toEqual([`/buyer/auctions/${AUCTION_ID}`])
    expect(calls[0]?.init?.method).toBe('GET')
    expect(record.auction_id).toBe(AUCTION_ID)
    expect(record.solicited).toEqual(SOLICITED)
    expect(record.entries.map((entry) => entry.fallback_reason)).toEqual([null, null, null])
    expect(record.excluded[0]?.exclusion_reasons).toHaveLength(2)
    expect(record.excluded[0]?.exclusion_reasons[0]).toContain('blacklisted_store:')
    // Nothing is denied in this market — all three stores are eligible — and the fixture
    // says so rather than adding a row so the panel has something to show.
    expect(record.denied).toEqual([])
    // The exchange's own five, not two this test made up.
    expect(record.ranked[0]?.components).toEqual({
      intent_match: 0.175,
      verified_claim_ratio: 0.1,
      trust: 0.164,
      price_value: 0.075,
      delivery_fit: 0.05,
    })
    expect(record.recorded_at).toBe(RECORDED_AT)
  })

  it('names a slots store from the bid ref, and no longer joins a price out of it', async () => {
    // WHAT CHANGED, and why this test lost half its assertions rather than being loosened.
    // It used to assert `entryForSlot(record, BID_REF)?.unit_price === 78` — the client-side
    // price join, which took the store id out of a bid ref and looked that store up in the
    // RECORDED `entries[]` because the shortlist slot carried no price. The slot now carries
    // `price`, so that join is a second source of truth for the price, on a different clock
    // from the slot beside it, and it has been deleted along with `bidPrice` and the price
    // list it fed. The price is asserted on where it now comes from — off the slot — in
    // 'shows the price, product and commitments the exchange put on the slot' below.
    //
    // `storeIdFromBidRef` survives and is asserted unchanged: the page still uses it to NAME
    // a store, which was never the part that had two answers. `rankedForSlot` survives too —
    // `ranked[]` is published nowhere else, so it is not a second copy of anything.
    const { fetcher } = recorder(() => json(auctionBody([RAW_SLOT])))

    const record = await loadAuction(AUCTION_ID, fetcher)

    // `mint_bid_id` is `f"{auction_id}:{store_id}"`, so the store id is what follows the
    // auction id and its colon — matched as a prefix, never split on the first `:`.
    expect(storeIdFromBidRef(BID_REF, AUCTION_ID)).toBe('demo-woolworks')
    expect(storeIdFromBidRef(FASTFLEECE_BID_REF, AUCTION_ID)).toBe('demo-fastfleece')
    // A ref some other auction minted names no store THIS page may attribute anything to.
    expect(storeIdFromBidRef('auc-other:demo-woolworks', AUCTION_ID)).toBeUndefined()
    expect(storeIdFromBidRef(`${AUCTION_ID}:`, AUCTION_ID)).toBeUndefined()
    expect(storeIdFromBidRef(undefined, AUCTION_ID)).toBeUndefined()

    // The recorded entries are still READ — they are the diagnostics panel's subject — and
    // are still not joined to a slot.
    expect(record.entries.map((entry) => entry.unit_price)).toEqual([78, 72, 45])

    expect(rankedForSlot(record, BID_REF)?.rank_score).toBe(0.564)
    expect(rankedForSlot(record, FASTFLEECE_BID_REF)).toBeUndefined()
  })

  it('leaves an unreadable rank_score undefined rather than defaulting it to a zero', async () => {
    // A zero would print as "rank_score 0", which reads as the exchange having scored this
    // candidate at the bottom. It did not; this client just found no number.
    const body = {
      ...auctionBody([RAW_SLOT]),
      ranked: [{ bid_ref: BID_REF, store_id: 'demo-woolworks', components: RANKED[0]!.components }],
    }
    const { fetcher } = recorder(() => json(body))

    const record = await loadAuction(AUCTION_ID, fetcher)

    expect(record.ranked[0]?.rank_score).toBeUndefined()
    expect(record.ranked[0]?.rank_score).not.toBe(0)
    // The components the exchange DID publish are still all there.
    expect(record.ranked[0]?.components).toEqual(RANKED[0]!.components)
  })

  it('leaves an unreadable fit_score undefined rather than defaulting it to a zero', async () => {
    // The same rule as rank_score above, and it was NOT held here until it was fixed: "fit 0"
    // reads as the exchange ranking this candidate last, which is a different claim from
    // "no fit score arrived". `ShortlistSlot.fit_score` was a required `number`, so every
    // client of it had to invent one.
    const { fetcher } = recorder(() =>
      json({ slots: [{ ...RENDERED_SLOT, fit_score: 'not-a-number' }] }),
    )

    const slots = await renderShortlist({ slots: [] }, fetcher)

    expect(slots[0]?.fit_score).toBeUndefined()
    expect(slots[0]?.fit_score).not.toBe(0)
    // Everything the service DID send survives.
    expect(slots[0]?.bid_ref).toBe(RENDERED_SLOT.bid_ref)
    expect(slots[0]?.provenance_labels).toEqual(RENDERED_SLOT.provenance_labels)
  })

  it('spells the exchange own ranking components and never a formula of its own', () => {
    expect(
      describeComponents({
        intent_match: 0.175,
        verified_claim_ratio: 0.1,
        trust: 0.164,
        price_value: 0.075,
        delivery_fit: 0.05,
      }),
    ).toBe(
      'intent_match=0.175 verified_claim_ratio=0.1 trust=0.164 price_value=0.075 ' +
        'delivery_fit=0.05',
    )
    // A term this build has never heard of still prints; nothing here spells the key set.
    expect(describeComponents({ some_future_term: 0.5 })).toBe('some_future_term=0.5')
    expect(describeComponents({})).toBe('the exchange published no components')
  })

  it('names the status when the auction read is refused, and keeps the service message', async () => {
    const { fetcher } = recorder(() => json({ detail: 'no auction by that id' }, 404))

    await expect(loadAuction(AUCTION_ID, fetcher)).rejects.toThrowError(HttpFailure)
    await expect(loadAuction(AUCTION_ID, fetcher)).rejects.toThrowError(
      /load auction failed: HTTP 404 — no auction by that id/,
    )
  })

  it('refuses to render a 200 that carried no shortlist rather than showing an empty page', async () => {
    const { fetcher } = recorder(() => json({ auction_id: AUCTION_ID }))

    await expect(loadAuction(AUCTION_ID, fetcher)).rejects.toThrowError(MalformedResponseError)
  })

  it('keeps a forgotten shortlist, an empty one and a malformed body three different facts', async () => {
    // 1. `shortlist: null` — `buyer_svc/auctions/routes.py` writes exactly this when the
    //    exchange's 15-minute TTL has taken the auction away. The recorded rows survive.
    const gone = recorder(() => json({ ...auctionBody([]), shortlist: null }))
    const forgotten = await loadAuction(AUCTION_ID, gone.fetcher)
    expect(forgotten.liveness).toBe('forgotten')
    expect(forgotten.shortlist).toBeNull()
    // Not a shortlist with no slots — and the diagnostics are still all there.
    expect(forgotten.entries).toHaveLength(ENTRIES.length)
    expect(forgotten.solicited).toEqual(SOLICITED)

    // 2. A shortlist with no slots. A fact about the market, not about the exchange.
    const barren = recorder(() => json(auctionBody([])))
    const empty = await loadAuction(AUCTION_ID, barren.fetcher)
    expect(empty.liveness).toBe('live')
    expect(empty.shortlist).toEqual({ auction_id: AUCTION_ID, slots: [] })

    // 3. No `shortlist` key at all. An ABSENT field is not an explicit `null`, and reading
    //    it as "the exchange forgot it" would invent a reason the service never gave.
    const malformed = recorder(() => json({ auction_id: AUCTION_ID, entries: ENTRIES }))
    await expect(loadAuction(AUCTION_ID, malformed.fetcher)).rejects.toThrowError(
      MalformedResponseError,
    )
  })

  it('forwards the shortlist to /render byte-for-byte, fields this client cannot name included', async () => {
    const { fetcher, calls } = recorder(() => json({ slots: [RENDERED_SLOT] }))
    const shortlist = { auction_id: AUCTION_ID, slots: [RAW_SLOT] }

    const slots = await renderShortlist(shortlist, fetcher)

    expect(calls[0]?.path).toBe(RENDER_PATH)
    expect(bodyOf(calls[0]?.init)).toEqual({ shortlist })
    // `claims` is what `render_shortlist` derives labels from when the exchange sent none.
    expect(JSON.stringify(bodyOf(calls[0]?.init))).toContain('claims')
    expect(slots[0]?.auction_id).toBe(AUCTION_ID)
    expect(slots[0]?.labels_source).toBe('exchange')
    expect(slots[0]?.provenance_labels).toEqual(['store-confirmed'])
    // `trust_summary` is narrowed to the numbers `ShortlistView`'s type demands...
    expect(slots[0]?.trust_summary).toEqual({ score: 0.82 })
    // ...and `trust_fields` keeps every field, so nothing the service said is dropped.
    expect(slots[0]?.trust_fields).toEqual({
      store_id: 'demo-woolworks',
      available: true,
      score: 0.82,
    })
  })

  it('spells every trust field the service sent, booleans distinct from strings', () => {
    expect(describeTrust({ store_id: 'demo-woolworks', available: true, score: 0.82 })).toBe(
      'store_id="demo-woolworks" available=true score=0.82',
    )
    expect(describeTrust({ available: 'true' })).toBe('available="true"')
    expect(describeTrust({})).toBe('no trust snapshot')
  })

  it('names the status when the render is refused', async () => {
    const { fetcher } = recorder(() => json({ detail: 'derive_missing_labels must be a bool' }, 422))

    await expect(renderShortlist({ slots: [] }, fetcher)).rejects.toThrowError(
      /render shortlist failed: HTTP 422/,
    )
  })

  it('reads the minted code off the exchange URL and never reconstructs one', () => {
    expect(discountCodeFrom(PERMALINK)).toBe('PSX-MC4DM9A1')
    expect(permalinkHost(PERMALINK)).toBe('demo-woolworks.example.com')
    expect(discountCodeFrom('https://demo-woolworks.example.com/cart/1:1')).toBeUndefined()
    expect(discountCodeFrom('not a url at all')).toBeUndefined()
    expect(discountCodeFrom(undefined)).toBeUndefined()
  })

  it('keeps the service message off a refusal without consuming the body the caller reads', async () => {
    const inner: Fetcher = () => Promise.resolve(json({ detail: 'no exchange client is wired' }, 503))
    const wire = instrumentFetcher(inner)

    const response = await wire.fetcher('/buyer/shortlist/accept', { method: 'POST' })

    expect(response.status).toBe(503)
    expect(response.bodyUsed).toBe(false)
    expect(await response.json()).toEqual({ detail: 'no exchange client is wired' })
    expect(wire.latest()).toEqual({ status: 503, detail: 'no exchange client is wired' })
    wire.reset()
    expect(wire.latest()).toBeUndefined()
  })

  it('reads the message out of every detail shape the service uses', () => {
    expect(detailFromBody('{"detail":"plain"}')).toBe('plain')
    expect(detailFromBody('{"detail":{"message":"refused","denial_reason":"offer_expired"}}')).toBe(
      'refused (denial_reason="offer_expired")',
    )
    expect(detailFromBody('not json at all')).toBe('not json at all')
    expect(detailFromBody('   ')).toBe('')
  })

  it('sends the profile the exchange needs, with confirmed as a real boolean', async () => {
    const { fetcher, calls } = recorder(() =>
      json({ auction_id: AUCTION_ID, intent_id: INTENT.intent_id, created_at: '' }, 201),
    )

    const created = await confirmWithProfile(
      INTENT,
      { pseudonym: 'psn-0123456789', buckets: {} },
      fetcher,
    )

    expect(created.auction_id).toBe(AUCTION_ID)
    expect(calls[0]?.path).toBe(CONFIRM_PATH)
    const body = bodyOf(calls[0]?.init) as Record<string, unknown>
    // The page states R5's handle itself rather than letting the exchange name the shopper:
    // `solicitation_profile` would mint `anon-{auction_id}` in its place. It used to be here
    // because a missing profile made the exchange send `{}` and every store answer 422; that
    // is fixed on the exchange's side, and the field stays for the handle.
    expect(body.profile).toEqual({ pseudonym: 'psn-0123456789', buckets: {} })
    // `StrictBool` on the service: the string "true" is a 422, and a lax bool would coerce it.
    expect(typeof body.confirmed).toBe('boolean')
    expect(body.confirmed).toBe(true)
  })

  it('refuses to confirm with no pseudonym rather than emptying the shortlist silently', async () => {
    const { fetcher, calls } = recorder(() => json({ auction_id: AUCTION_ID }, 201))

    await expect(
      confirmWithProfile(INTENT, { pseudonym: '   ', buckets: {} }, fetcher),
    ).rejects.toThrowError(MissingProfileError)
    expect(calls).toHaveLength(0)
  })

  // The test that stood here asserted `mintPseudonym()` produced 32 distinct `psn-` handles
  // of ten hex characters, and it passed for as long as the function existed. It is gone with
  // the function: this app must not be able to mint a pseudonym at all, and a browser-minted
  // handle being *fresh* was never the property R5 asks for — the vault's is what rotates,
  // what a sign-out retires and what resolves back to an account. What replaces it is the
  // served-shape assertion in the R5 describe below (`psn-` + THIRTY-TWO hex characters, the
  // shape `default_pseudonym` mints) plus this, which is the only claim about the prefix this
  // app can still make on its own.
  it('spells the prefix a served pseudonym carries, and mints nothing', () => {
    expect(PSEUDONYM_PREFIX).toBe('psn-')
    expect(Object.keys(wireModule)).not.toContain('mintPseudonym')
  })

  it('joins a thrown status to the service message without inventing either', () => {
    const failed = new Error('accept failed: HTTP 503')
    expect(explain(failed, { status: 503, detail: 'no client' })).toBe(
      'accept failed: HTTP 503 — the service said: no client',
    )
    expect(explain(failed, { status: 503, detail: '' })).toBe('accept failed: HTTP 503')
    expect(explain(failed)).toBe('accept failed: HTTP 503')
  })
})

describe('the four beats', () => {
  it('walks say -> confirm -> shortlist -> checkout against the service answers', async () => {
    const { fetcher, calls } = demoService()
    const { container } = render(<Journey fetcher={fetcher} />)

    // Beat 0, which used not to exist: the journey is behind a blocking sign-in gate, so the
    // composer is not in the document until the redemption in `beforeEach`'s URL has landed a
    // session. Waiting for it is what a buyer arriving from their mailbox really does.
    await screen.findByTestId('signed-in')

    // Beat 1: the whole transcript goes across, every time.
    fireEvent.change(screen.getByLabelText('What are you shopping for?'), {
      target: { value: 'I want a warm merino wool beanie for winter, under $100' },
    })
    fireEvent.submit(screen.getByLabelText('What are you shopping for?').closest('form')!)

    // Beat 2: the outstanding question, and no confirm button while it is outstanding.
    const question = await screen.findByLabelText('What is your budget?')
    expect(screen.queryByRole('button', { name: /confirm and ask stores/i })).toBeNull()
    fireEvent.change(question, { target: { value: 'about $100' } })
    fireEvent.submit(question.closest('form')!)

    const confirmButton = await screen.findByRole('button', { name: /confirm and ask stores/i })
    expect(screen.getByTestId('intent-query').textContent).toBe(INTENT.query)
    expect(screen.getByTestId('unresolved').textContent).toContain('colour')
    expect(screen.getByTestId('cluster-id').textContent).toContain(INTENT.cluster_id)

    const clarifyBodies = calls
      .filter((call) => call.path === CLARIFY_PATH)
      .map((call) => bodyOf(call.init))
    expect(clarifyBodies).toEqual([
      { turns: ['I want a warm merino wool beanie for winter, under $100'] },
      { turns: ['I want a warm merino wool beanie for winter, under $100', 'about $100'] },
    ])

    // Beat 3: confirm, load the auction, render the labels.
    fireEvent.click(confirmButton)
    await screen.findByLabelText('Shortlist')

    expect(screen.getByTestId('slot-count').textContent).toContain('1 option')
    const label = screen.getByTestId(`label-${BID_REF}`)
    expect(label.textContent).toBe('store-confirmed')
    expect(label.getAttribute('data-tone')).toBe('confirmed')
    const provenance = screen.getByTestId(`labels-source-${BID_REF}`).textContent ?? ''
    expect(provenance).toContain('exchange')
    // The two fields `TrustSummary` cannot carry are on the page anyway.
    expect(provenance).toContain('store_id="demo-woolworks"')
    expect(provenance).toContain('available=true')
    expect(provenance).toContain('score=0.82')
    // The exchange's published ranking, its own five component keys, its own numbers.
    expect(provenance).toContain('rank_score 0.564')
    expect(provenance).toContain('intent_match=0.175')
    expect(provenance).toContain('delivery_fit=0.05')

    // WHAT THE THING IS, WHAT IT COSTS, WHAT THE STORE COMMITS TO — off the slot itself,
    // which is the LIVE half of the answer, re-fetched for this page. There is no longer a
    // price list joined out of the RECORDED `entries[]` beside it.
    // WHOSE SHOP IT IS, at the head of the card. This fixture is the slot an exchange with no
    // platform registry serves, so the card says the domain is absent rather than going quiet
    // about who is offering — the same rule the price and the product line follow.
    expect(screen.getByTestId(`store-domain-${BID_REF}`).textContent).toContain(
      'named no domain',
    )
    expect(screen.getByTestId(`product-${BID_REF}`).textContent).toContain('beanie-merino-01')
    expect(screen.getByTestId(`product-${BID_REF}`).textContent).toContain('44352913')
    const shownPrice = screen.getByTestId(`price-${BID_REF}`).textContent ?? ''
    expect(shownPrice).toContain('USD 78')
    // No currency SYMBOL: the exchange named a currency code and this page prints that code.
    expect(shownPrice).not.toContain('$')
    const commitments = screen.getByTestId(`commitments-${BID_REF}`).textContent ?? ''
    expect(commitments).toContain('free returns')
    expect(commitments).toContain('30 days')
    expect(screen.getByTestId(`commitment-label-${BID_REF}`).textContent).toBe('store-confirmed')
    expect(screen.getByTestId('price-provenance').textContent).toContain('live answer')
    expect(screen.queryByTestId('slot-prices')).toBeNull()

    // The service's whole answer is one click away on the slots-present page too, not only
    // on the empty-shortlist panel — which is what makes "these prices came from entries[]"
    // checkable rather than a claim the reader has to take.
    const verbatim = screen.getByTestId('verbatim-auction').textContent ?? ''
    expect(verbatim).toContain('The service\u2019s answer, verbatim')
    expect(verbatim).toContain('"recorded_at": "2026-09-05T00:00:01Z"')
    expect(verbatim).toContain('"rank_score": 0.564')
    expect(verbatim).toContain('"unit_price": 78')

    // THE RECORD FOLDS AWAY AND THE LABELS DO NOT \u2014 the design system's third rule, asserted
    // as a containment rather than as a look, so it survives any restyling of the fold.
    //
    // The two claims are separable and both matter. A build that folded the machine rows and
    // ALSO folded the provenance pills would satisfy the first half and break R2: a label a
    // buyer has to go looking for is a label they did not have when they chose. A build that
    // folded neither is the page as it was, with two blocks of mono between the shopper and
    // the next step.
    const record = screen.getByTestId('auction-record') as HTMLDetailsElement
    expect(record.tagName).toBe('DETAILS')
    // Shut until it is asked for. Opening it asserts nothing and changes nothing, which is
    // why it is a fold rather than a route.
    expect(record.open).toBe(false)
    // Both machine blocks are INSIDE it, so nothing the fold claims to hold was left out.
    expect(record.contains(screen.getByTestId(`labels-source-${BID_REF}`))).toBe(true)
    expect(record.contains(screen.getByTestId('verbatim-auction'))).toBe(true)
    // The provenance label on the card is NOT, and this is the half that must never fold.
    expect(record.contains(label)).toBe(false)
    // Neither is the sentence about where the price came from: that is a claim this page
    // makes about provenance, not a value the system wrote, so it stays on the page.
    expect(record.contains(screen.getByTestId('price-provenance'))).toBe(false)

    // The exchange sent one slot and one slot survived labelling, so the fault notice that
    // exists for the other case is correctly absent.
    expect(screen.queryByTestId('labels-dropped')).toBeNull()

    // The whole transcript is on the page, oldest first, as the buyer said it.
    expect(screen.getByTestId('transcript').textContent).toContain(
      'I want a warm merino wool beanie for winter, under $100',
    )
    expect(screen.getByTestId('transcript').textContent).toContain('about $100')

    const opened = screen.getByTestId('auction-id').textContent ?? ''
    expect(opened).toContain(AUCTION_ID)
    // The service's own clocks, and no invented stand-in for a clock it did not send.
    expect(opened).toContain(CREATED_AT)
    expect(opened).toContain(RECORDED_AT)
    expect(opened).not.toContain('just now')
    // Nothing is linkable before the exchange has minted a destination — and the slot's
    // decoy `checkout_url` is never turned into one.
    expect(container.querySelectorAll('a[href]')).toHaveLength(0)

    // Beat 4: accept, and the permalink the exchange minted.
    fireEvent.click(screen.getByRole('button', { name: /accept this one/i }))
    await screen.findByTestId('permalink-url')

    expect(screen.getByTestId('permalink-url').textContent).toBe(PERMALINK)
    expect(screen.getByTestId('permalink-host').textContent).toBe('demo-woolworks.example.com')
    expect(screen.getByTestId('discount-code').textContent).toBe('PSX-MC4DM9A1')

    const link = screen.getByTestId('permalink-link')
    expect(link.getAttribute('href')).toBe(PERMALINK)
    expect(container.querySelectorAll('a[href]')).toHaveLength(1)

    const accept = calls.find((call) => call.path === ACCEPT_PATH)
    expect(bodyOf(accept?.init)).toEqual({
      slot: { slot: 'fit', bid_ref: BID_REF, auction_id: AUCTION_ID },
      expected_domain: null,
    })
  })

  it('shows the exchange own report, verbatim, instead of an empty shortlist page', async () => {
    const { fetcher } = demoService({ slots: [] })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Why the shortlist is empty')

    expect(screen.getByTestId('solicited').textContent).toContain('demo-fastfleece')

    // All three bid, which is what this market really does. demo-fastfleece is not silent —
    // it is solicited, it bids at 45.00, and the ranking then throws it out.
    const blacklisted = screen.getByTestId('entry-demo-fastfleece')
    expect(blacklisted.textContent).toContain('fallback: false')
    expect(blacklisted.textContent).toContain('fallback_reason: null')
    expect(blacklisted.textContent).toContain('unit 45, total 45')
    const answered = screen.getByTestId('entry-demo-woolworks')
    expect(answered.textContent).toContain('fallback: false')
    expect(answered.textContent).toContain('fallback_reason: null')
    // Nothing fell back, so the gloss that explains `no_response` is correctly absent.
    expect(screen.queryByTestId('no-response-gloss')).toBeNull()

    const excluded = screen.getByTestId('excluded-demo-fastfleece')
    for (const reason of EXCLUDED[0]!.exclusion_reasons) {
      expect(excluded.textContent).toContain(reason)
    }

    // Nobody was denied, and the panel says that rather than showing an empty list.
    expect(screen.getByTestId('denied-empty').textContent).toContain('Every rostered store')

    // The two lists that DO have rows carry exactly the rows the service sent, with no row
    // this page padded them out with.
    expect(screen.getByTestId('entries').querySelectorAll(':scope > li')).toHaveLength(
      ENTRIES.length,
    )
    expect(screen.getByTestId('excluded').querySelectorAll(':scope > li')).toHaveLength(
      EXCLUDED.length,
    )
    expect(screen.getByTestId('verbatim-auction').textContent).toContain('recorded_at')
    // The exchange's own shortlist really had no slots, so this IS a fact about the market
    // and the labelling-failure notice must not appear beside it.
    expect(screen.queryByTestId('labels-dropped')).toBeNull()
    // No fabricated row stood in for the missing options.
    expect(screen.queryByTestId(`slot-${BID_REF}`)).toBeNull()
    expect(screen.getByLabelText('Shortlist').textContent).toContain('No store was eligible')
  })

  it('shows the status and the service own words when a request is refused', async () => {
    const { fetcher } = recorder((path, init) => {
      const signedIn = authAnswer(path, init)
      if (signedIn !== undefined) return signedIn

      if (path === CLARIFY_PATH) return json(CLARIFY_ANSWER)
      if (path === CONFIRM_PATH) {
        return json({ detail: 'no exchange client is wired; refusing to open an auction' }, 503)
      }
      return json({ detail: 'unreachable' }, 500)
    })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))

    const alert = await screen.findByTestId('journey-error')
    expect(alert.textContent).toContain('HTTP 503')
    expect(alert.textContent).toContain('no exchange client is wired; refusing to open an auction')
    expect(screen.queryByLabelText('Shortlist')).toBeNull()
  })

  it('refuses a permalink a browser must not follow instead of linking to it', async () => {
    const { fetcher } = recorder((path, init) => {
      const signedIn = authAnswer(path, init)
      if (signedIn !== undefined) return signedIn

      switch (path) {
        case CLARIFY_PATH:
          return json(CLARIFY_ANSWER)
        case CONFIRM_PATH:
          return json({ auction_id: AUCTION_ID, intent_id: INTENT.intent_id, created_at: '' }, 201)
        case auctionPath(AUCTION_ID):
          return json(auctionBody([RAW_SLOT]))
        case RENDER_PATH:
          return json({ slots: [RENDERED_SLOT] })
        default:
          return json({
            permalink_url: 'javascript:alert(1)',
            auction_id: AUCTION_ID,
            bid_ref: BID_REF,
            slot: 'fit',
            accepted_at: '',
          })
      }
    })
    const { container } = render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')
    fireEvent.click(screen.getByRole('button', { name: /accept this one/i }))

    const alert = await screen.findByTestId('journey-error')
    expect(alert.textContent).toContain('refusing to follow this checkout permalink')
    expect(container.querySelectorAll('a[href]')).toHaveLength(0)
    expect(screen.queryByTestId('permalink-url')).toBeNull()
  })

  // What stood here asserted the page confirmed under a handle it had generated itself
  // (`psn-` + ten hex characters) and that a second visit generated a different one. Both
  // halves were true and the behaviour was the defect: a browser-minted handle is not the
  // vault's, is not what a sign-out retires, and resolves back to no account. The property is
  // now asserted the other way round, against the service's own pseudonym, in
  // "opens the auction under the service pseudonym and the coarsened profile" below.

  it('states the gaps that remain, permanently, and no longer claims sign-in is one', async () => {
    const { fetcher } = demoService()
    render(<Journey fetcher={fetcher} />)

    // Sign-in and the browser-minted pseudonym were the first two entries on this list and
    // are gone from it, because the gap closed rather than because the sentence softened.
    expect(screen.queryByTestId('gap-signin')).toBeNull()
    expect(screen.queryByTestId('gap-pseudonym')).toBeNull()

    // `gap-domain` went the same way, and it is the newest of them. It read "the exchange's
    // shortlist slot carries no `store_domain`, so the checkout host could only be checked for
    // scheme and host presence" — true when it was written, and false now: `ShortlistSlot`
    // publishes `store_domain`, `ranking/serving.py` joins the platform registry's answer onto
    // the slot, and `permalinkRefusal` pins the permalink's host against it. Absent still means
    // absent — an exchange with no registry publishes `null` and the pin does not fire — but
    // that is a deployment saying it vouches for no host, not this page being unable to ask.
    expect(screen.queryByTestId('gap-domain')).toBeNull()

    // The price gap is CLOSED — the slot carries `price` now — so `gap-price` is gone from
    // this list, the same way sign-in and the browser-minted pseudonym went: because the gap
    // closed, not because the sentence softened.
    expect(screen.queryByTestId('gap-price')).toBeNull()

    // THREE MORE went with them, and this block used to assert the opposite of what it now
    // asserts, so the reason is recorded here rather than left to `git log`.
    //
    // It read: `const product = screen.getByTestId('gap-product-name').textContent ?? ''`,
    // then `expect(product).toContain('product_ref')` and `expect(product).toContain(
    // 'catalogue')` — pinning the page's claim that the product arrives as a REFERENCE and
    // never as a name, "because a title belongs to the store's own catalogue". Introduced in
    // `0dffb80`, true when written, and false now for a reason that is not a softening: the
    // name the card shows is NOT the store's catalogue's. `98529bd` published
    // `ShortlistProduct.identity`, `exchange.ranking.verification.catalog_identity` reads it
    // off the PLATFORM's own crawl (gated in Cypher to sources the platform observed itself,
    // reachable from no bid), and `buyer_svc.accept.labels._slot_identity` forwards it with
    // the snapshot id attached. The bullet's premise — that only the store could name the
    // product — is what stopped being true, so the bullet went rather than its wording.
    //
    // `gap-fallback` and `gap-store-voice` are the same commit's other two: `ShortlistSlot`
    // now declares `fallback`/`fallback_reason` and `message`, and `extra="forbid"` survived
    // the change, so nothing was loosened to make room for them. What each of the three is
    // replaced by is asserted positively on the card itself, in `shortlist.test.tsx` —
    // absence here would otherwise be indistinguishable from the claim having been dropped.
    expect(screen.queryByTestId('gap-product-name')).toBeNull()
    expect(screen.queryByTestId('gap-fallback')).toBeNull()
    expect(screen.queryByTestId('gap-store-voice')).toBeNull()

    // The retired premise must not creep back into the panel as prose under another id.
    const gaps = screen.getByLabelText('What is not wired yet').textContent ?? ''
    expect(gaps).not.toContain('a reference, not a name')
    expect(gaps).not.toContain('not on the wire to here')

    // The model bullet no longer claims the questions came from the offline double, and the
    // retirement of that claim is the assertion.
    //
    // It used to pin five strings — `build_llm("buyer")`, `LLM_PROVIDER`, `DeterministicLLM`,
    // `double:buyer` and `no live model` — because the bullet stated FLATLY, as the page's
    // own voice, that no live model had written anything.
    //
    // WHY THAT WAS RETIRED, stated carefully because the reason is not "the default
    // changed". It has not: `.env.example` still ships `LLM_PROVIDER=double`, and `docs/`
    // and `.gitlab-ci.yml` still say it is deliberately left unset. What was wrong is that
    // the sentence was UNCONDITIONAL about something the page cannot read. `LLM_PROVIDER` is
    // deployment configuration; `POST /buyer/intent/clarify` answers with `questions`,
    // `intent`, `unresolved` and `confirmed`, and no field on that route or any other route
    // this origin serves discloses which client answered. So on any deployment that does set
    // a provider — which the default does not forbid, and which this project has been told is
    // in use — the page asserted a falsehood it had no way to check, and on the default
    // deployment it was right by luck rather than by measurement.
    //
    // The claim was therefore retired rather than softened, and the assertions on its
    // wording went with the wording. What is pinned instead is the honest replacement: the
    // page says it cannot tell, and names the served field that would let it. The two
    // `not.toContain`s below are the guard that the unconditional claim does not creep back.
    const model = screen.getByTestId('gap-model').textContent ?? ''
    expect(model).toContain('cannot tell you')
    expect(model).toContain('build_llm("buyer")')
    expect(model).toContain('LLM_PROVIDER')
    // The retired claim must not creep back: an unconditional "no live model" is exactly the
    // sentence this deployment falsifies.
    expect(model).not.toContain('no live model')
    expect(model).not.toContain('double:buyer')

    // Signed in from the emailed link, so the sign-in form has been replaced rather than
    // hidden: the page asks for no address it has no use for.
    await screen.findByTestId('signed-in')
    expect(screen.queryByLabelText('Email address')).toBeNull()

    // Still there at the end of the journey, not only at the start.
    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')
    fireEvent.click(screen.getByRole('button', { name: /accept this one/i }))
    await screen.findByTestId('permalink-url')

    // The PANEL is what is being pinned here — that it survives to the end of the journey
    // rather than only appearing at the start — so the witness has to be a gap that is still
    // open. It used to be `gap-product-name`; that bullet was retired above, so the witness
    // moved to the two that remain rather than the assertion being dropped.
    await waitFor(() => expect(screen.getByTestId('gap-feedback-seeded')).toBeInTheDocument())
    expect(screen.getByTestId('gap-model')).toBeInTheDocument()
    expect(screen.queryByTestId('gap-domain')).toBeNull()
    expect(screen.queryByTestId('gap-price')).toBeNull()
    expect(screen.queryByTestId('gap-signin')).toBeNull()
    expect(screen.queryByTestId('gap-pseudonym')).toBeNull()
    expect(screen.queryByTestId('gap-product-name')).toBeNull()
    expect(screen.queryByTestId('gap-fallback')).toBeNull()
    expect(screen.queryByTestId('gap-store-voice')).toBeNull()
  })

  it('prints the clock the service sent, and nothing at all when it sent none', async () => {
    const { fetcher } = demoService({ createdAt: '', recordedAt: '' })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    // The service reported no time, so the page reports no time. It does not say 'just now',
    // which would be this browser's clock wearing the service's voice.
    const line = screen.getByTestId('auction-id').textContent ?? ''
    expect(line).toContain(AUCTION_ID)
    expect(line).not.toContain('just now')
    // The sentence says "when the auction opened" in every case — that is the page naming
    // the moment the exchange asked, not a clock. What must be absent is a TIMESTAMP: the
    // `, opened {created_at}` clause the page adds only when the service sent one.
    expect(line).not.toMatch(/opened 20\d\d-/)
    expect(line).not.toContain('recorded that answer')
  })

  it('says a store quoted no price rather than showing a blank or a zero', async () => {
    // The ordinary case, not an error: an R10 fallback minted from a roster row that named
    // no readable list price reaches the buyer as `price: null`, and so does a bid the
    // exchange could not read a pair of finite numbers out of. What it must never render as
    // is a blank, an `undefined`, or a `0` — a zero is a price, and the cheapest one there
    // is. (This assertion used to be about a slot whose store was in no RECORDED entry,
    // because the price came from `entries[]`; the price comes off the slot now, so the
    // absence that matters is the slot's own.)
    const unpriced = {
      ...RENDERED_SLOT,
      bid_ref: `${AUCTION_ID}:demo-alpine-supply`,
      price: null,
      product: null,
      commitments: null,
    }
    const { fetcher } = demoService({ rendered: [unpriced], body: { entries: [] } })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    const cell = screen.getByTestId(`price-${unpriced.bid_ref}`).textContent ?? ''
    expect(cell).toContain('No price')
    expect(cell).not.toContain('0')
    expect(cell).not.toContain('undefined')
    // The other two absences are sentences too, not empty elements.
    expect(screen.getByTestId(`product-${unpriced.bid_ref}`).textContent).toContain(
      'did not name a product',
    )
    expect(screen.getByTestId(`commitments-${unpriced.bid_ref}`).textContent).toContain(
      'No commitments',
    )
    // The ranking published no row for it either, and that is said rather than left blank.
    expect(screen.getByTestId(`labels-source-${unpriced.bid_ref}`).textContent).toContain(
      'rank_score not published for this slot',
    )
    // The store id still names the store, so `?? slot.bid_ref` is not silently standing in.
    expect(screen.getByTestId(`labels-source-${unpriced.bid_ref}`).textContent).toContain(
      'demo-alpine-supply',
    )
  })

  it('says a slot has no reported fit score rather than printing a manufactured zero', async () => {
    // `ShortlistView` prints `fit {slot.fit_score}`. While `fit_score` was a required
    // `number`, an unreadable one arrived here as `0` and the buyer read "fit 0" — the
    // exchange ranking this store last. It had not.
    const scoreless = { ...RENDERED_SLOT, fit_score: undefined }
    const { fetcher } = demoService({ rendered: [scoreless] })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    const cell = screen.getByTestId(`fit-${scoreless.bid_ref}`)
    expect(cell.textContent).toBe('fit not reported')
    expect(cell.textContent).not.toContain('fit 0')
    // The slot is still offerable: a missing score is not a missing option.
    expect(screen.getByRole('button', { name: /accept this one/i })).toBeInTheDocument()
  })

  it('blames this service, not the exchange, when no record was kept to read a ranking from', async () => {
    // `_recorded_rows` answers `[]` both for an empty list and for "no record at all", and
    // `outcome_for` reads a per-process ring of 64 — a restart or a busy run empties every
    // diagnostic while the live shortlist is fine. `recorded_at` is what tells them apart, and
    // saying "the exchange did not report a price" here would misfile this service's own
    // bookkeeping as a fact about the market.
    const { fetcher } = demoService({
      recordedAt: '',
      // `ranked` too, and for the same reason as the other four: with no record kept there
      // is no ranking row to read either, and leaving RANKED in place would have
      // `rankedForSlot` find a row and `rankLine` print the exchange's score off a record
      // this test says was never kept.
      body: { entries: [], excluded: [], denied: [], ranked: [], solicited: [] },
    })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    // The PRICE is unaffected, and that is the point of taking it off the slot: it is the
    // exchange's LIVE answer for this request, so an empty recorded half cannot blank it.
    // Before this change the price came out of `entries[]` and this same case printed
    // "no price here: this service kept no record of the auction to read one from".
    expect(screen.getByTestId(`price-${BID_REF}`).textContent).toContain('USD 78')

    const rank = screen.getByTestId(`labels-source-${BID_REF}`).textContent ?? ''
    expect(rank).toContain('this service kept no record of the auction')
    expect(rank).not.toContain('rank_score not published for this slot')
  })

  it('prints a zero price and says what a zero there can also mean', async () => {
    // MEASURED: `apps/exchange/src/auction/routes.py::_entries_out` builds this field as
    // `float(offer.get("unit_price", 0.0))`, so an offer that named no price arrives as a
    // real 0.0. The page prints the number it was sent — it may not round it away or hide
    // it — and says what a zero there can also mean, because "unit 0" alone reads as free.
    //
    // The number is the same one and the sentence is the same sentence; only the ELEMENT
    // moved. It used to be printed by `Journey`'s `bidPrice`, joining `entries[]` onto a
    // slot. That join is gone, so the recorded entries are printed in exactly one place now
    // — `WhyEmpty`'s "What each store answered" panel — and the gloss moved there with them,
    // rather than being deleted along with its old renderer. The shortlist is empty here
    // because that panel is the one this page shows when no store made a slot.
    const zeroed = [{ ...ENTRIES[0]!, unit_price: 0.0, total_price: 0.0 }]
    const { fetcher } = recorder((path, init) => {
      const signedIn = authAnswer(path, init)
      if (signedIn !== undefined) return signedIn

      switch (path) {
        case CLARIFY_PATH:
          return json(CLARIFY_ANSWER)
        case CONFIRM_PATH:
          return json(
            { auction_id: AUCTION_ID, intent_id: INTENT.intent_id, created_at: CREATED_AT },
            201,
          )
        case auctionPath(AUCTION_ID):
          return json({ ...auctionBody([]), entries: zeroed })
        case RENDER_PATH:
          return json({ slots: [] })
        default:
          return json({ detail: `nothing serves ${path}` }, 404)
      }
    })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    const cell = screen.getByTestId('entry-demo-woolworks').textContent ?? ''
    expect(cell).toContain('unit 0, total 0')
    expect(cell).toContain('an offer that named no price')
    // Not swallowed into "no price reported": the service did send a number, and it is shown.
    expect(cell).not.toContain('no price reported')
  })

  it('says the exchange has forgotten the auction, and offers nothing to accept', async () => {
    const { fetcher, calls } = demoService({ forgotten: true })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByTestId('shortlist-forgotten')

    const notice = screen.getByTestId('shortlist-forgotten').textContent ?? ''
    expect(notice).toContain('has no shortlist for this auction')
    // It must NOT pick a cause. The exchange's own 404 names four and chooses none, and its
    // docstring says filing eviction under the TTL "sends the reader to the wrong knob".
    expect(notice).toContain('without saying why')
    expect(notice).toContain('has not closed yet')
    expect(notice).toContain('never existed')
    expect(notice).toContain('fifteen-minute lifetime')
    expect(notice).toContain('pushed it out')
    // The distinction the whole outcome exists for: this is not the market coming back empty.
    expect(notice).toContain('not the market coming back empty')

    // No shortlist section at all, so no Accept button a buyer could press into a refusal.
    expect(screen.queryByLabelText('Shortlist')).toBeNull()
    expect(screen.queryByRole('button', { name: /accept this one/i })).toBeNull()

    // The recorded diagnostics ARE shown, labelled as a record rather than as live.
    await screen.findByLabelText('What the exchange reported when this auction ran')
    expect(screen.getByTestId('recorded-not-live').textContent).toContain('None of this is live')
    expect(screen.getByTestId('entry-demo-woolworks').textContent).toContain('fallback: false')
    // Nothing was denied in this market — `DENIED` is empty by design — so the recorded
    // rows that prove the panel is the RECORD rather than a live read are the entries and
    // the exclusions the exchange really did report.
    expect(screen.getByTestId('excluded-demo-fastfleece').textContent).toContain(
      'blacklisted_store:',
    )
    // ...and NOT under the empty-market heading, which would be a different claim.
    expect(screen.queryByLabelText('Why the shortlist is empty')).toBeNull()

    // The count sentence does not report "0 options" for a shortlist that is simply gone.
    const line = screen.getByTestId('auction-id').textContent ?? ''
    expect(line).toContain('no longer holds the shortlist it answered with')
    expect(line).not.toContain('0 options')
    expect(line).not.toContain('just now')

    // `/render` labels a shortlist; there is none, so it was never called.
    expect(calls.map((call) => call.path)).not.toContain(RENDER_PATH)
  })

  it('tells a forgotten shortlist, an empty one and an unreadable one apart on the page', async () => {
    // Empty: the Shortlist section is there and says the MARKET had nothing.
    const barren = demoService({ slots: [] })
    render(<Journey fetcher={barren.fetcher} />)
    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Why the shortlist is empty')
    expect(screen.getByLabelText('Shortlist').textContent).toContain('No store was eligible')
    expect(screen.queryByTestId('shortlist-forgotten')).toBeNull()
    cleanup()

    // Forgotten: no Shortlist section, and the notice instead.
    arrivingFromTheMailbox()
    const gone = demoService({ forgotten: true })
    render(<Journey fetcher={gone.fetcher} />)
    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByTestId('shortlist-forgotten')
    expect(screen.queryByLabelText('Shortlist')).toBeNull()
    expect(screen.queryByLabelText('Why the shortlist is empty')).toBeNull()
    cleanup()

    // Unreadable: neither claim is made, and the failure names the status and the reason.
    arrivingFromTheMailbox()
    const broken = recorder((path, init) => {
      const signedIn = authAnswer(path, init)
      if (signedIn !== undefined) return signedIn

      if (path === CLARIFY_PATH) return json(CLARIFY_ANSWER)
      if (path === CONFIRM_PATH) {
        return json({ auction_id: AUCTION_ID, intent_id: INTENT.intent_id, created_at: '' }, 201)
      }
      if (path === auctionPath(AUCTION_ID)) return json({ auction_id: AUCTION_ID })
      return json({ detail: 'unreachable' }, 500)
    })
    render(<Journey fetcher={broken.fetcher} />)
    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    const alert = await screen.findByTestId('journey-error')
    expect(alert.textContent).toContain('a body this screen cannot render')
    expect(screen.queryByTestId('shortlist-forgotten')).toBeNull()
    expect(screen.queryByLabelText('Shortlist')).toBeNull()
    expect(screen.queryByLabelText('Why the shortlist is empty')).toBeNull()
  })

  it('says the lists are empty because nothing was recorded, not because nobody was asked', async () => {
    // `_recorded_rows` answers `[]` both for a list the exchange really sent empty and for
    // "this service holds no record of the auction", and `outcome_for` reads a per-process
    // ring of 64 — a restart or a busy run empties all five while the live shortlist is
    // fine. `recorded_at` is the only field that tells the two apart. Without this test the
    // panel could go back to reading a gap in its own bookkeeping as a verdict on the market
    // and nothing would notice.
    const { fetcher } = demoService({
      slots: [],
      recordedAt: '',
      body: { entries: [], excluded: [], denied: [], solicited: [], ranked: [] },
    })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Why the shortlist is empty')

    expect(screen.getByTestId('no-record').textContent).toContain('holds no record of this auction')
    expect(screen.getByTestId('no-record').textContent).toContain('recorded_at')

    // Each of the four says "not recorded", and none of them makes the market claim.
    expect(screen.getByTestId('solicited-empty').textContent).toBe(
      'Not recorded here, so this page cannot say which stores were asked.',
    )
    expect(screen.getByTestId('entries-empty').textContent).toBe(
      'Not recorded here, so this page cannot say what any store answered.',
    )
    expect(screen.getByTestId('excluded-empty').textContent).toBe(
      'Not recorded here, so this page cannot say what the filters refused.',
    )
    expect(screen.getByTestId('denied-empty').textContent).toBe(
      'Not recorded here, so this page cannot say who was allowed to bid.',
    )

    // The four sentences that would be claims about the exchange are absent, every one.
    const page = document.body.textContent ?? ''
    expect(page).not.toContain('It asked no store at all')
    expect(page).not.toContain('No store answered')
    expect(page).not.toContain('The eligibility filters refused nothing')
    expect(page).not.toContain('Every rostered store was allowed to answer')
    expect(page).not.toContain('The auction ran.')

    // And the price and ranking absences beside the slots blame this service too, not the
    // exchange — the same fact, said the same way, wherever it shows up.
    expect(page).not.toContain('price not reported for this slot')
  })

  it('says each diagnostic list was empty rather than padding it with a row', async () => {
    const bare = {
      auction_id: AUCTION_ID,
      shortlist: { auction_id: AUCTION_ID, slots: [] },
      entries: [],
      excluded: [],
      denied: [],
      ranked: [],
      solicited: [],
      recorded_at: RECORDED_AT,
    }
    const { fetcher } = recorder((path, init) => {
      const signedIn = authAnswer(path, init)
      if (signedIn !== undefined) return signedIn

      switch (path) {
        case CLARIFY_PATH:
          return json(CLARIFY_ANSWER)
        case CONFIRM_PATH:
          return json(
            { auction_id: AUCTION_ID, intent_id: INTENT.intent_id, created_at: CREATED_AT },
            201,
          )
        case auctionPath(AUCTION_ID):
          return json(bare)
        case RENDER_PATH:
          return json({ slots: [] })
        default:
          return json({ detail: `nothing serves ${path}` }, 404)
      }
    })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Why the shortlist is empty')

    expect(screen.getByTestId('solicited-empty').textContent).toContain('asked no store at all')
    expect(screen.getByTestId('entries-empty').textContent).toContain('No store answered')
    expect(screen.getByTestId('excluded-empty').textContent).toContain('refused nothing')
    expect(screen.getByTestId('denied-empty').textContent).toContain('Every rostered store')
    // Four empty lists and not one invented row anywhere.
    expect(screen.queryByTestId('solicited')).toBeNull()
    expect(screen.queryByTestId('entries')).toBeNull()
    expect(screen.queryByTestId('excluded')).toBeNull()
    expect(screen.queryByTestId('denied')).toBeNull()
    expect(screen.queryByTestId('no-response-gloss')).toBeNull()
  })

  it('says the exchange stood in for a store rather than crediting it with a bid', async () => {
    // CONSTRUCTED, and `SILENT_ENTRY`'s own comment says so: in this market all three stores
    // really bid. A `fallback: true` row is the exchange standing in for a store, and the
    // number on it came off the caller-supplied roster row, so it may not be called a price
    // that store quoted.
    //
    // WHERE THIS MOVED, TWICE. It began as a sentence on the slot's price cell, written by
    // `bidPrice` off the joined `entries[]` row. When the price started coming off the slot
    // itself, the card lost the distinction — `contracts.protocol.ShortlistSlot` declared no
    // `fallback` and forbids extras — and `gap-fallback` in the gaps panel said so.
    //
    // `98529bd` closed that: the slot carries `fallback` and `fallback_reason`, `gap-fallback`
    // is retired, and `ShortlistView` prints a price-provenance sentence off the slot's own
    // flag (asserted in `shortlist.test.tsx`, on the card, in all three of its states).
    //
    // THIS TEST IS STILL ABOUT THE OTHER PANEL and is unchanged by that. It drives a shortlist
    // with NO slots, so there is no card to carry the flag; what it pins is the recorded
    // entries panel, which states `fallback` per store and glosses the reason. The two are
    // different surfaces answering for different shortlists, and the entries panel is the only
    // one a shopper has when the market returned nothing.
    const { fetcher } = demoService({
      slots: [],
      body: { entries: [ENTRIES[0]!, SILENT_ENTRY, ENTRIES[2]!] },
    })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Why the shortlist is empty')

    const row = screen.getByTestId('entry-demo-alpine-supply').textContent ?? ''
    // The number is still printed — the stand-in price is a real number the service sent —
    // beside the flag that says nobody bid it.
    expect(row).toContain('unit 72, total 72')
    expect(row).toContain('fallback: true')
    expect(row).toContain('fallback_reason: no_response')
    expect(screen.getByTestId('no-response-gloss').textContent).toContain('no_response')
  })

  it('says an entry that carried neither price reported none, and not that it has no row', async () => {
    // Defensive rather than observed: `AuctionEntryOut` declares `unit_price` and
    // `total_price` as REQUIRED floats, so an entry the exchange built always carries both.
    // `readEntries` reads an `unknown` body and answers `undefined` for anything that is not
    // a number, and this is the page's half of that. The row EXISTS here — the verbatim
    // block below shows it — so this is a statement about the prices, not about a store the
    // report never mentioned.
    //
    // Asserted on the recorded entries panel, which is the one place these numbers are
    // printed now that the slot carries its own price.
    const priceless = {
      store_id: 'demo-alpine-supply',
      tier: 1,
      fallback: false,
      fallback_reason: null,
    }
    const { fetcher } = demoService({
      slots: [],
      body: { entries: [ENTRIES[0]!, priceless, ENTRIES[2]!] },
    })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Why the shortlist is empty')

    expect(screen.getByTestId('entry-demo-alpine-supply').textContent).toContain(
      'no price reported',
    )
    // The record really does carry a row for this store — so the sentence above came from
    // the "entry present, neither price" path and not from "no entry at all".
    expect(screen.getByTestId('verbatim-auction').textContent).toContain(
      '"store_id": "demo-alpine-supply"',
    )
  })

  it('says a rank_score it could not read is unreadable, and never prints it as a zero', async () => {
    // The wire-level test above proves `readRanked` leaves it `undefined`; this one proves
    // the page then says so. A zero would read as the exchange having scored this candidate
    // at the bottom, which it did not — this client just found no number.
    const scoreless = {
      bid_ref: BID_REF,
      store_id: 'demo-woolworks',
      components: RANKED[0]!.components,
    }
    const { fetcher } = demoService({ body: { ranked: [scoreless] } })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    const rank = screen.getByTestId(`labels-source-${BID_REF}`).textContent ?? ''
    // Pinned with the join that follows it, so a mutation that merely prepends words to the
    // sentence still shows up here rather than sliding past a substring match.
    expect(rank).toContain(
      'rank_score not a readable number in the exchange row — intent_match=0.175',
    )
    expect(rank).not.toContain('rank_score 0')
    // The row IS published — this is not the "no row for this slot" absence.
    expect(rank).not.toContain('rank_score not published for this slot')
    // ...and the components the exchange did publish are still printed beside it.
    expect(rank).toContain('intent_match=0.175')
    expect(rank).toContain('delivery_fit=0.05')
  })

  it('says an entry row carried no price rather than leaving a blank beside the store', async () => {
    // Same defensive shape as above, one panel further in: `WhyEmpty` renders `entries[]`
    // itself, and `''` there would render an empty span, which beside a store's name reads
    // as a price of nothing rather than as no price reported.
    const priceless = {
      store_id: 'demo-alpine-supply',
      tier: 1,
      fallback: false,
      fallback_reason: null,
    }
    const { fetcher } = demoService({ slots: [], body: { entries: [priceless] } })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Why the shortlist is empty')

    // The WHOLE row, not a substring of it: `toContain('no price reported')` would also pass
    // against a row that said something else and happened to end with those words.
    expect(screen.getByTestId('entry-demo-alpine-supply').textContent).toBe(
      'demo-alpine-supply tier 1 fallback: false fallback_reason: null no price reported',
    )
  })

  it('glosses a fallback_reason beside the raw string and not over it', async () => {
    // CONSTRUCTED, exactly as `SILENT_ENTRY` and `SILENT_EXCLUSION` say: this market's three
    // stores all answer, so nothing here is a state it produces. Both rows use the exchange's
    // own vocabulary — `no_response` is its value for a store whose agent sent nothing at all
    // — and the exclusion is the verdict a claim-less fallback really earns. This title used
    // to say "the one fallback_reason it recognises"; the panel now has a sentence for every
    // family the exchange publishes, and the reason-shape suite below covers the rest.
    const { fetcher } = demoService({
      slots: [],
      body: {
        entries: [ENTRIES[0]!, SILENT_ENTRY, ENTRIES[2]!],
        excluded: [EXCLUDED[0]!, SILENT_EXCLUSION],
      },
    })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Why the shortlist is empty')

    // The raw value is printed as the exchange spelled it...
    const row = screen.getByTestId('entry-demo-alpine-supply').textContent ?? ''
    expect(row).toContain('fallback: true')
    expect(row).toContain('fallback_reason: no_response')
    expect(row).toContain('unit 72, total 72')

    // ...and the plain-English gloss sits beside it rather than replacing it.
    const gloss = screen.getByTestId('no-response-gloss').textContent ?? ''
    expect(gloss).toContain('fallback_reason: "no_response"')
    expect(gloss).toContain('did not answer with a usable bid')
    expect(gloss).toContain('represented it at its list price instead of dropping it')

    // The exclusion a claim-less fallback earns, printed as the filter spelled it.
    expect(screen.getByTestId('excluded-demo-alpine-supply').textContent).toContain(
      SILENT_EXCLUSION.exclusion_reasons[0]!,
    )
    // The two stores that did answer are untouched, so the gloss is attached to the one row
    // it is about rather than to the panel.
    expect(screen.getByTestId('entry-demo-woolworks').textContent).toContain('fallback: false')
    expect(screen.getByTestId('entry-demo-fastfleece').textContent).toContain('fallback: false')
  })

  it('prints a denial as the exchange spelled it, in place of the nobody-was-denied line', async () => {
    // CONSTRUCTED, from `DENIED_VARIANT`'s own comment and the market file's note: mark
    // demo-fastfleece `blacklisted` instead of `eligible` and it is refused BEFORE
    // solicitation. So in this state it is not in `solicited`, has no entry and has no
    // exclusion — the other three lists are the two stores that were allowed to answer.
    const { fetcher } = demoService({
      slots: [],
      body: {
        denied: DENIED_VARIANT,
        entries: [ENTRIES[0]!, ENTRIES[1]!],
        excluded: [],
        solicited: ['demo-woolworks', 'demo-alpine-supply'],
      },
    })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Why the shortlist is empty')

    const denied = screen.getByTestId('denied-demo-fastfleece').textContent ?? ''
    expect(denied).toContain('demo-fastfleece')
    expect(denied).toContain(DENIED_VARIANT[0]!.status)
    expect(denied).toContain(DENIED_VARIANT[0]!.reason)
    // The list has a row, so the sentence that stands in for an empty list is gone.
    expect(screen.queryByTestId('denied-empty')).toBeNull()
    // ...and a store that was never asked appears in none of the other three lists.
    expect(screen.queryByTestId('entry-demo-fastfleece')).toBeNull()
    expect(screen.queryByTestId('excluded-demo-fastfleece')).toBeNull()
    expect(screen.getByTestId('solicited').textContent).not.toContain('demo-fastfleece')
    expect(screen.getByTestId('excluded-empty').textContent).toContain('refused nothing')
  })

  it('blames the labelling step, not the market, when options were sent and none survived', async () => {
    // The exchange's shortlist carried a slot and `POST /buyer/shortlist/render` answered
    // with none. `stage.slots` is that step's output, not the exchange's, so running the
    // empty-market panel here would report a fault on this page's side of the wire to the
    // buyer as a verdict about the market.
    const { fetcher } = demoService({ slots: [RAW_SLOT], rendered: [] })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))

    const dropped = (await screen.findByTestId('labels-dropped')).textContent ?? ''
    // The count is the exchange's own, taken before the labelling step ran.
    expect(dropped).toContain('The exchange sent 1 option')
    expect(dropped).toContain('the step that labels them for display returned none')
    expect(dropped).toContain('not an answer about the market')
    // The panel that would say the market was empty must not run.
    expect(screen.queryByLabelText('Why the shortlist is empty')).toBeNull()
    // The exchange's own answer is still one click away, so the count above is checkable.
    expect(screen.getByTestId('verbatim-auction').textContent).toContain(`"${BID_REF}"`)
    // The sentence this whole branch exists to prevent. `ShortlistView` prints "No store was
    // eligible for what you asked for" whenever it is handed zero slots, so an earlier
    // version of this branch — which rendered the notice UNDERNEATH `ShortlistView` — put
    // both on the page at once, contradicting each other. `Journey.tsx` now hoists this case
    // into a branch of its own that renders no `ShortlistView` at all, and these two
    // assertions are what stop that regressing.
    expect(document.body.textContent).not.toContain('No store was eligible')
    expect(screen.queryByLabelText('Shortlist')).toBeNull()
    // And with no shortlist section there is no Accept button: a labelling failure is not an
    // offer, and a control that cannot produce one must not be on the page.
    expect(screen.queryByRole('button', { name: /accept this one/i })).toBeNull()
  })
})

// MEASURED, off the exchange's own suite rather than composed here:
// `apps/exchange/tests/test_composition_catalog_and_refusals.py` runs four refusing store
// agents through `POST /auctions` and asserts exactly these strings —
//
//     decliner     -> 'store_declined:no_matching_product'
//     refuser      -> 'store_refused:422'
//     shouter      -> 'store_declined:undisclosed'
//     misreporter  -> 'store_refused:503'
//
// — and, on the same line, that `'no_response' not in reasons.values()`. The reasons below are
// those; the store ids are this market's own three, so nothing here invents a store OR a word.
// Before that repair every one of these arrived as `no_response`, which is why this panel only
// ever had a sentence for `no_response`.
/**
 * R5's first clause, on the screen that actually ships.
 *
 * The backend half of magic-link sign-in has been complete and mounted for weeks and no
 * served page called it: `index.html` loads `main.tsx`, which mounts THIS component, and it
 * minted its own pseudonym in the browser. A browser-minted handle is not the vault's, does
 * not rotate when the vault rotates, and is exactly the stable identifier R5 exists to deny
 * the stores — so these tests pin the join, not the client (`chat/session.test.ts` owns the
 * client and its identity backstop).
 *
 * The gate this page chose, stated once here because the tests below only show it working:
 * **sign in first, then the journey.** A visitor with no session gets the sign-in form and no
 * part of the journey — no composer, no transcript, no confirm control.
 *
 * WHAT THIS PARAGRAPH USED TO CLAIM, kept so the reversal is legible rather than silent. It
 * read "**browsing is open and confirming is not**", and argued that a first-time visitor
 * should get the page rather than a login wall because the clarifying loop reaches no store,
 * with the confirm control ABSENT until a session existed. The DATA half of that argument was
 * right and is unchanged — `POST /buyer/intent/clarify` still reaches no exchange and no
 * store — but the shape it produced was a visitor who held a whole conversation and then
 * discovered they could not use it, having to leave for their mailbox and lose it. The gate
 * moved to the front for that reason, and the tests below moved with it.
 *
 * What did NOT weaken: the confirm control is still never mounted for a session-less page,
 * and it is still absent rather than disabled. That property is now STRUCTURAL — the whole
 * journey is inside the gate, so `IntentConfirm` cannot be reached signed out at all —
 * which is why the old `confirm-gated` element is gone rather than merely unasserted. The
 * tests below pin the stronger claim: signed out, none of the journey is in the document.
 */
describe('R5 — signing in, and the pseudonym that comes with it', () => {
  it('signed out, shows the sign-in form and no part of the journey', async () => {
    signedOut()
    const { fetcher, calls } = demoService()
    render(<Journey fetcher={fetcher} />)

    // THE GATE. Not one beat of the journey is in the document: no composer to type into, no
    // transcript, no confirm control. This replaces the old assertion that the clarify loop
    // ran signed out and only the confirm button was withheld — a claim the page has stopped
    // making — and it is the stronger of the two, because it pins the absence of the whole
    // journey rather than of one control inside it.
    await screen.findByLabelText('Email address')
    expect(screen.queryByLabelText('What are you shopping for?')).toBeNull()
    expect(screen.queryByTestId('transcript')).toBeNull()
    expect(screen.queryByRole('button', { name: /confirm and ask stores/i })).toBeNull()
    expect(screen.queryByLabelText('Shortlist')).toBeNull()

    // The reason for the gate is ON the gate. A wall that cannot say why it is there is a
    // worse wall than the one this replaced, so the sentence that used to live inside step 2
    // is asserted here rather than allowed to go missing with the element that carried it.
    const why = screen.getByTestId('why-sign-in').textContent ?? ''
    expect(why).toContain('leaves it')
    expect(why).toContain('solicits real stores')
    expect(why).toMatch(/vault rather than from\s+this page/)

    // And the privacy statement a signed-out visitor most reasonably wants BEFORE handing
    // over an address. It used to sit beside the composer in step 1, which a signed-out
    // visitor can no longer see; moving it here is what kept it renderable in some state
    // rather than none.
    const privacy = screen.getByTestId('pseudonym-signed-out').textContent ?? ''
    expect(privacy).toContain('No store has been told anything about you')
    expect(privacy).toContain('you are not signed in')

    // Nothing has been asked of anybody.
    expect(calls.some((call) => call.path === CONFIRM_PATH)).toBe(false)

    const email = screen.getByLabelText('Email address')
    fireEvent.change(email, { target: { value: BUYER_EMAIL } })
    fireEvent.submit(email.closest('form')!)

    await screen.findByTestId('link-sent')
    expect(bodyOf(calls.find((call) => call.path === MAGIC_LINK_PATH)?.init)).toEqual({
      email: BUYER_EMAIL,
    })
    // 202 carries an expiry and never a token, so there is no session to be had from it —
    // and no vault pseudonym anywhere in the document, which is what the retired
    // `pseudonym` assertion was really guarding. That line lived inside step 1 and step 1 is
    // now behind the gate, so the check is made over the whole page instead of over one
    // element, which is a wider net rather than a looser one.
    expect(screen.queryByTestId('signed-in')).toBeNull()
    expect(document.body.textContent ?? '').not.toMatch(/psn-/)
  })

  it('lands a buyer arriving from the mailbox in the chat, not back on the form', async () => {
    // THE GATE'S OTHER HALF, and the failure mode a blocking gate most easily introduces: a
    // wall the emailed link cannot get you past. It cannot happen here, and this pins why —
    // redemption sets the session on the SAME mount that read the token. `Journey` reads
    // `?token=` on mount, strips it with `history.replaceState` (which rewrites the entry in
    // place and does NOT reload), redeems over `fetch`, and sets state. So the arrival that
    // `beforeEach` sets up ends inside the journey rather than in front of it.
    const { fetcher } = demoService()
    render(<Journey fetcher={fetcher} />)

    // The composer is the thing the gate withholds, so the composer is the proof.
    expect(await screen.findByLabelText('What are you shopping for?')).toBeInTheDocument()
    expect(screen.getByTestId('signed-in')).toBeInTheDocument()
    // And the form the visitor came through is gone rather than merely scrolled past.
    expect(screen.queryByLabelText('Email address')).toBeNull()
    expect(screen.queryByTestId('why-sign-in')).toBeNull()
    // The single-use credential is out of the address bar before anything else happens.
    expect(window.location.search).toBe('')
  })

  it('redeems the token out of the emailed link and wears the pseudonym the vault minted', async () => {
    const { fetcher, calls } = demoService()
    render(<Journey fetcher={fetcher} />)

    await screen.findByTestId('signed-in')

    expect(bodyOf(calls.find((call) => call.path === SESSION_PATH)?.init)).toEqual({
      token: TOKEN,
    })
    const profileCall = calls.find((call) => call.path === PROFILE_PATH)
    expect(profileCall?.init?.headers).toEqual({ [SESSION_HEADER]: SESSION_ID })

    // The handle on the page is the service's, and its shape says so: thirty-two hex
    // characters out of the vault, not the ten this page used to generate for itself.
    expect(VAULT_PSEUDONYM).toMatch(VAULT_PSEUDONYM_SHAPE)
    expect(screen.getByTestId('pseudonym').textContent).toContain(VAULT_PSEUDONYM)

    // The single-use bearer credential is out of the address bar, so it is not in the
    // history entry, a bookmark or a `Referer` header on the next request.
    expect(window.location.search).toBe('')
    expect(document.body.textContent ?? '').not.toContain(TOKEN)
  })

  it('opens the auction under the service pseudonym and the coarsened profile', async () => {
    const { fetcher, calls } = demoService()
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    const body = bodyOf(calls.find((call) => call.path === CONFIRM_PATH)?.init) as Record<
      string,
      unknown
    >
    // The pseudonym AND the buckets are the service's — R5's "rotating pseudonym and a
    // coarsened profile", neither of them made up here.
    expect(body.profile).toEqual({ pseudonym: VAULT_PSEUDONYM, buckets: BUCKETS })
    expect(body.confirmed).toBe(true)

    // And nothing that could name the buyer went with it. The session id is on this list
    // deliberately: it is a bearer credential for this origin and the exchange has no use
    // for it, so a page that helpfully forwarded it would be handing it to the stores.
    const sent = JSON.stringify(body).toLowerCase()
    for (const secret of IDENTITY) {
      expect(sent).not.toContain(secret.toLowerCase())
    }
  })

  it('refuses a profile that came back carrying identity, and stays signed out', async () => {
    const { fetcher, calls } = demoService({
      auth: (path) =>
        path === PROFILE_PATH
          ? json({ pseudonym: VAULT_PSEUDONYM, buckets: {}, email: BUYER_EMAIL })
          : undefined,
    })
    render(<Journey fetcher={fetcher} />)

    const error = await screen.findByTestId('journey-error')
    expect(error.textContent).toContain('identity-shaped key')
    expect(screen.queryByTestId('signed-in')).toBeNull()
    expect(screen.getByLabelText('Email address')).toBeInTheDocument()
    // The address the service should never have sent is not rendered anywhere either.
    expect((document.body.textContent ?? '').toLowerCase()).not.toContain('dana.reyes')
    expect(calls.some((call) => call.path === CONFIRM_PATH)).toBe(false)
  })

  it('says a spent link is not valid, in the service own words, and stays signed out', async () => {
    const { fetcher } = demoService({
      auth: (path, init) =>
        path === SESSION_PATH && init?.method !== 'DELETE'
          ? json({ detail: 'this login link is not valid' }, 401)
          : undefined,
    })
    render(<Journey fetcher={fetcher} />)

    const error = await screen.findByTestId('journey-error')
    expect(error.textContent).toContain('401')
    expect(error.textContent).toContain('this login link is not valid')
    expect(screen.getByLabelText('Email address')).toBeInTheDocument()
    expect(screen.queryByTestId('signed-in')).toBeNull()
  })

  it('signs out through the service, and takes the signed-in journey off the page with it', async () => {
    const { fetcher, calls } = demoService()
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    // The confirm really did leave an auction id behind for the demo's metrics page. Checked
    // BEFORE signing out so that the assertion after it cannot pass by the id never having
    // been written — an empty-before, empty-after check would prove nothing.
    expect(window.sessionStorage.getItem(REMEMBERED_AUCTION_KEY)).toBe(AUCTION_ID)

    fireEvent.click(screen.getByRole('button', { name: /sign out/i }))
    await screen.findByLabelText('Email address')

    const out = calls.find(
      (call) => call.path === SESSION_PATH && call.init?.method === 'DELETE',
    )
    expect(out?.init?.headers).toEqual({ [SESSION_HEADER]: SESSION_ID })

    // The shortlist belonged to a pseudonym the vault has now retired, so it does not stay
    // on the screen with an Accept button under it.
    expect(screen.queryByLabelText('Shortlist')).toBeNull()
    // The retired handle is gone from the WHOLE page, not merely from the one line that used
    // to carry it. `pseudonym` lives inside step 1 and step 1 is behind the gate now, so
    // asserting on that element would be asserting on something signing out has removed —
    // this checks the document instead, which is what the original line was protecting
    // against and catches strictly more.
    expect(document.body.textContent ?? '').not.toContain(VAULT_PSEUDONYM)

    // …and the auction id the demo's metrics page was told to remember goes with it. The
    // gloss beside the sign-out button promises signing out "clears the auction below with
    // it", and clearing only React state would have left `#/metrics` able to reopen the
    // retired pseudonym's full recorded trace from a route that takes no session header.
    expect(window.sessionStorage.getItem(REMEMBERED_AUCTION_KEY)).toBeNull()
  })

  it('never asks the exchange for anything while signed out, however hard the page is pushed', async () => {
    signedOut()
    const { fetcher, calls } = demoService()
    render(<Journey fetcher={fetcher} />)

    const email = await screen.findByLabelText('Email address')

    // The signed-out page is DRIVEN, not merely inspected, and that distinction is the whole
    // value of this test. The old shape walked the clarify loop and then clicked everything;
    // the walk is gone because the gate means there is no loop to walk signed out. What
    // replaced it has to do real work, or the test passes over an empty `calls` array and can
    // no longer fail for the reason it exists: the page's one button is `disabled` until an
    // address is typed, so clicking the page as-found presses nothing at all.
    //
    // So: type an address and submit it. That is a genuine round trip — it puts
    // MAGIC_LINK_PATH into `calls`, proving the fetcher is live and reachable — and only then
    // is "no exchange path followed" a claim about the page rather than about an empty list.
    fireEvent.change(email, { target: { value: BUYER_EMAIL } })
    fireEvent.submit(email.closest('form')!)
    await screen.findByTestId('link-sent')

    // Now every control the signed-out page offers, pressed, including the ones the request
    // above just enabled or revealed.
    for (const control of screen.getAllByRole('button')) {
      fireEvent.click(control)
    }
    await waitFor(() => expect(screen.getByLabelText('Email address')).toBeInTheDocument())
    expect(screen.queryByLabelText('What are you shopping for?')).toBeNull()
    expect(screen.queryByRole('button', { name: /confirm and ask stores/i })).toBeNull()

    const reached = calls.map((call) => call.path)
    // The fetcher really was exercised, so the three refusals below mean something.
    expect(reached).toContain(MAGIC_LINK_PATH)
    expect(reached).not.toContain(CONFIRM_PATH)
    expect(reached).not.toContain(ACCEPT_PATH)
    expect(reached).not.toContain(RENDER_PATH)
  })
})

const REFUSAL_ENTRIES = [
  {
    store_id: 'demo-woolworks',
    tier: 1,
    fallback: true,
    unit_price: 78.0,
    total_price: 78.0,
    fallback_reason: 'store_declined:no_matching_product',
  },
  {
    store_id: 'demo-fastfleece',
    tier: 1,
    fallback: true,
    unit_price: 45.0,
    total_price: 45.0,
    fallback_reason: 'store_refused:422',
  },
  {
    store_id: 'demo-alpine-supply',
    tier: 1,
    fallback: true,
    unit_price: 72.0,
    total_price: 72.0,
    fallback_reason: 'store_declined:undisclosed',
  },
]

describe('the reasons a shortlist is empty, in a shopper’s words', () => {
  it('reads a reason the way the exchange writes one', () => {
    // The mirror of `exchange.auction.collect.fallback_reason_family`, which is
    // `str(reason).split(":", 1)[0]` — the FIRST colon, and nothing clever about the rest.
    expect(fallbackReasonFamily('store_refused:422')).toBe('store_refused')
    expect(fallbackReasonDetail('store_refused:422')).toBe('422')
    expect(fallbackReasonFamily('no_response')).toBe('no_response')
    expect(fallbackReasonDetail('no_response')).toBe('')
    expect(fallbackReasonFamily('store_declined:a:b')).toBe('store_declined')
    expect(fallbackReasonDetail('store_declined:a:b')).toBe('a:b')
    // `entry.fallback_reason` is `string | null`, and null is "it did not have to fall back".
    expect(fallbackReasonFamily(null)).toBe('')
    expect(fallbackReasonDetail(null)).toBe('')
  })

  it('has a plain-English sentence for every family the exchange publishes', () => {
    // Not "for `no_response`". `FALLBACK_REASON_FAMILIES` is copied from
    // `collect.py::FALLBACK_REASONS`, and this is what stops the copy from being carried
    // without a sentence attached to each word in it.
    for (const family of FALLBACK_REASON_FAMILIES) {
      const sentence = explainFallbackReason(family)
      expect(sentence, family).not.toBe(UNRECOGNISED_GLOSS)
      expect(sentence, family).toMatch(/^means /)
    }
  })

  it('says it does not recognise a family rather than leaving the raw string bare', () => {
    // The set is open-ended: the exchange can add a word, and this is a COPY of its
    // vocabulary, so the unknown case is answered on purpose instead of falling through.
    expect(explainFallbackReason('quantum_flux:7')).toBe(UNRECOGNISED_GLOSS)
    expect(explainFallbackReason('')).toBe(UNRECOGNISED_GLOSS)
  })

  it('tells a decline, a refusal and a silence apart', () => {
    const declined = explainFallbackReason('store_declined:no_matching_product')
    expect(declined).toContain('its answer was no')
    expect(declined).toContain('The reason it gave for itself is “no_matching_product”.')

    // A decline with a reason the exchange could not print is not a decline with no reason.
    const undisclosed = explainFallbackReason('store_declined:undisclosed')
    expect(undisclosed).toContain('It did state a reason and the exchange could not print it')
    // ...and a bare `store_declined` is the one that really said nothing.
    expect(explainFallbackReason('store_declined')).toContain('It stated no reason')

    const refused = explainFallbackReason('store_refused:422')
    expect(refused).toContain('It is an error, not a decision about what you asked for')
    expect(refused).toContain('its agent answered HTTP 422')
    // The 422 is the exchange's own fault and the page says so rather than blaming the store.
    expect(refused).toContain('a fault on the exchange’s side of the wire rather than the store’s')
    expect(explainFallbackReason('store_refused:503')).toContain('its agent answered HTTP 503')
    // `_unusable_because` makes the WHOLE unrecognised string the detail, so a non-numeric
    // detail is a real shape here — `reason_for('no_matching_product')` is measured to be
    // `store_refused:no_matching_product` in the exchange's suite.
    expect(explainFallbackReason('store_refused:no_matching_product')).toContain(
      'What the exchange recorded of it is “no_matching_product”.',
    )

    const silent = explainFallbackReason('no_response')
    expect(silent).toContain('nothing came back from that store’s agent at all')
    // The three sentences are three different sentences. That is the whole point.
    expect(new Set([declined, refused, silent]).size).toBe(3)
  })

  it('glosses each distinct reason once, in the order the entries carried them', () => {
    const entries: readonly AuctionEntry[] = [
      { store_id: 'a', fallback: true, fallback_reason: 'store_refused:422' },
      { store_id: 'b', fallback: false, fallback_reason: null },
      { store_id: 'c', fallback: true, fallback_reason: 'store_refused:422' },
      { store_id: 'd', fallback: true, fallback_reason: 'store_declined' },
    ]

    expect(glossedReasons(entries).map((gloss) => gloss.reason)).toEqual([
      'store_refused:422',
      'store_declined',
    ])
    // A store that did not fall back has nothing to explain, and gets no paragraph.
    expect(glossedReasons([entries[1]!])).toEqual([])
  })

  it('explains a market where the stores declined, instead of printing the raw string alone', async () => {
    const { fetcher } = demoService({ slots: [], body: { entries: REFUSAL_ENTRIES } })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Why the shortlist is empty')

    // The raw string is still printed as the exchange spelled it — that rule has not moved.
    expect(screen.getByTestId('entry-demo-woolworks').textContent).toContain(
      'fallback_reason: store_declined:no_matching_product',
    )

    // ...and now it has a sentence beside it. THIS is what was missing: before this change the
    // panel filtered `fallback_reason === 'no_response'`, found none of these three, and
    // rendered no explanation at all for a page made entirely of refusals.
    const declined = screen.getByTestId('gloss-store-declined-no-matching-product').textContent ?? ''
    expect(declined).toContain('fallback_reason: "store_declined:no_matching_product"')
    expect(declined).toContain('it answered, and its answer was no')
    expect(declined).toContain('The reason it gave for itself is “no_matching_product”')

    const refused = screen.getByTestId('gloss-store-refused-422').textContent ?? ''
    expect(refused).toContain('fallback_reason: "store_refused:422"')
    expect(refused).toContain('its agent answered HTTP 422')

    const undisclosed = screen.getByTestId('gloss-store-declined-undisclosed').textContent ?? ''
    expect(undisclosed).toContain('the exchange could not print it')

    // Not one store here was silent, so the one gloss this panel used to have is absent — and
    // with only that gloss, the page a shopper saw carried no explanation whatsoever.
    expect(screen.queryByTestId('no-response-gloss')).toBeNull()

    // Nothing the exchange reported is left on the page as a bare machine word: every reason
    // in the entries list appears inside a gloss paragraph as well as in its own row.
    const glosses = screen.getByTestId('reason-glosses').textContent ?? ''
    for (const entry of REFUSAL_ENTRIES) {
      expect(glosses, entry.fallback_reason).toContain(entry.fallback_reason)
    }
  })

  it('names a reason it does not recognise as unrecognised, on the page', async () => {
    // `wire.ts`'s family list is a COPY of the exchange's, so it can go stale. A shopper meets
    // that as a sentence saying this page does not know the word — never as the word alone.
    const { fetcher } = demoService({
      slots: [],
      body: {
        entries: [
          { ...REFUSAL_ENTRIES[0]!, fallback_reason: 'a_word_this_page_has_never_seen' },
          REFUSAL_ENTRIES[1]!,
        ],
      },
    })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Why the shortlist is empty')

    const unknown = screen.getByTestId('gloss-a-word-this-page-has-never-seen').textContent ?? ''
    expect(unknown).toContain('fallback_reason: "a_word_this_page_has_never_seen"')
    expect(unknown).toContain('is a reason this page has no plain-English sentence for')
    expect(unknown).toContain('nothing is being hidden')
    // The recognised one beside it is still recognised.
    expect(screen.getByTestId('gloss-store-refused-422').textContent).toContain('HTTP 422')
  })
})

/**
 * D55 ON THE WIRE — the pitch this client used to drop, and the shopper context it used to
 * withhold.
 *
 * Two separate defects, both MEASURED against the devstack (three real store agents, the
 * real exchange, the real buyer service) on the merino-beanie conversation:
 *
 * 1. `POST /buyer/shortlist/render` answers with a `pitch` object on every slot it can say
 *    anything about, and `readRenderedSlots` mapped a fixed field list that did not name it.
 *    `RenderedSlot` ignores unknown keys, so nothing broke — the whole of what the persuasion
 *    market decided was simply computed, served and never read.
 * 2. The client sent `{shortlist}` alone. `intent` and `profile` are optional on `RenderBody`,
 *    and WITHOUT them the served case for demo-woolworks is
 *      "Offer held until: 2026-09-07. Also price: 78.00 USD; free returns: 30 days."
 *    while WITH them it is
 *      "You said price was a must-have, and here it is: 78.00 USD. Also offer held until:
 *       2026-09-07; free returns: 30 days."
 *    Both strings are that run's own bytes. The unconditioned reading is a real answer — a
 *    screen that has not confirmed an intent is a real caller — but it is not the one this
 *    page has, and it is not the per-shopper case D55 is about.
 */
const SERVED_PITCH = {
  platform_case:
    'You said price was a must-have, and here it is: 78.00 USD. Also offer held until: ' +
    '2026-09-07; free returns: 30 days.',
  platform_case_source: 'assembled',
  store_pitch: null,
  voices: ['platform'],
  facts: [
    { key: 'price', value: '78.00 USD', kind: 'price', label: null },
    { key: 'offer held until', value: '2026-09-07', kind: 'price', label: null },
    { key: 'free returns', value: '30 days', kind: 'commitment', label: 'store-confirmed' },
    { key: 'reliability', value: '82%', kind: 'trust', label: null },
  ],
}

describe('the case each candidate makes, and in whose voice', () => {
  it('reads the served pitch off a slot instead of dropping it', async () => {
    const { fetcher } = recorder(() =>
      json({ slots: [{ ...RENDERED_SLOT, pitch: SERVED_PITCH }] }),
    )

    const slots = await renderShortlist({ slots: [] }, fetcher)

    expect(slots[0]?.pitch?.platform_case).toBe(SERVED_PITCH.platform_case)
    expect(slots[0]?.pitch?.platform_case_source).toBe('assembled')
    expect(slots[0]?.pitch?.store_pitch).toBeNull()
    expect(slots[0]?.pitch?.voices).toEqual(['platform'])
    // Every fact, in the served order, with each one's own label — `null` stays `null`.
    expect(slots[0]?.pitch?.facts).toEqual(SERVED_PITCH.facts)
  })

  it("carries a shop's own words byte for byte, markup and URLs included", async () => {
    // MEASURED: `buyer_svc.pitch.writing.store_pitch_of` strips NOTHING. It drops a message
    // that is blank, over 1200 characters or control-bearing, and otherwise returns the
    // seller's bytes unchanged — `FORBIDDEN_CHARACTERS` screens the PLATFORM's case, never
    // the seller's. So this client may not "tidy" one either.
    const words = 'Since 1974. <b>Free wool wash</b> — https://demo-woolworks.example.com/about'
    const { fetcher } = recorder(() =>
      json({ slots: [{ ...RENDERED_SLOT, pitch: { ...SERVED_PITCH, store_pitch: words, voices: ['store', 'platform'] } }] }),
    )

    const slots = await renderShortlist({ slots: [] }, fetcher)

    expect(slots[0]?.pitch?.store_pitch).toBe(words)
    expect(slots[0]?.pitch?.voices).toEqual(['store', 'platform'])
  })

  it('drops a pitch it cannot read rather than putting an object on the screen', async () => {
    // A `store_pitch` that is not a string would reach React as a child it refuses to
    // render, and a `facts` row that is not an object would print as `[object Object]`.
    const { fetcher } = recorder(() =>
      json({
        slots: [
          {
            ...RENDERED_SLOT,
            pitch: {
              platform_case: 42,
              platform_case_source: null,
              store_pitch: { text: 'nice hat' },
              voices: ['store', 7, ''],
              facts: [{ key: 'price', value: '78.00 USD', kind: 'price', label: 3 }, 'nope'],
            },
          },
        ],
      }),
    )

    const slots = await renderShortlist({ slots: [] }, fetcher)

    const pitch = slots[0]?.pitch
    expect(pitch).not.toBeUndefined()
    expect(pitch?.platform_case).toBe('')
    expect(pitch?.store_pitch).toBeNull()
    expect(pitch?.voices).toEqual(['store'])
    expect(pitch?.facts).toEqual([{ key: 'price', value: '78.00 USD', kind: 'price', label: null }])
  })

  it('leaves a slot with no pitch null, and never invents an empty one', async () => {
    const { fetcher } = recorder(() => json({ slots: [{ ...RENDERED_SLOT, pitch: null }] }))
    const withNull = await renderShortlist({ slots: [] }, fetcher)
    expect(withNull[0]?.pitch).toBeNull()

    // A producer older than the field sends no key at all. Same answer, not an empty object.
    const older = recorder(() => json({ slots: [RENDERED_SLOT] }))
    const withNone = await renderShortlist({ slots: [] }, older.fetcher)
    expect(withNone[0]?.pitch).toBeNull()
  })

  it('sends the confirmed intent and the coarsened profile, so the case is this shoppers', async () => {
    const { fetcher, calls } = recorder(() => json({ slots: [RENDERED_SLOT] }))
    const shortlist = { auction_id: AUCTION_ID, slots: [RAW_SLOT] }
    const profile = { pseudonym: VAULT_PSEUDONYM, buckets: BUCKETS }

    await renderShortlist(shortlist, fetcher, { intent: INTENT, profile })

    expect(bodyOf(calls[0]?.init)).toEqual({ shortlist, intent: INTENT, profile })
  })

  it('sends neither key when it has neither, so the unconditioned reading stays reachable', async () => {
    const { fetcher, calls } = recorder(() => json({ slots: [RENDERED_SLOT] }))
    const shortlist = { auction_id: AUCTION_ID, slots: [RAW_SLOT] }

    await renderShortlist(shortlist, fetcher, {})

    // Not `intent: null` and not `intent: undefined` — the key is absent, which is the body
    // every caller older than this parameter sends.
    expect(bodyOf(calls[0]?.init)).toEqual({ shortlist })
  })

  it('puts both voices on the page, attributed, when the service serves both', async () => {
    const words = 'We have been knitting merino in Yorkshire since 1974.'
    const { fetcher } = demoService({
      rendered: [
        {
          ...RENDERED_SLOT,
          pitch: { ...SERVED_PITCH, store_pitch: words, voices: ['store', 'platform'] },
        },
      ],
    })
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    expect(await screen.findByTestId(`store-voice-${BID_REF}`)).toHaveTextContent(words)
    expect(screen.getByTestId(`platform-voice-${BID_REF}`)).toHaveTextContent(
      SERVED_PITCH.platform_case,
    )
  })

  it('asks the render route for THIS shopper by name, from the page', async () => {
    const { fetcher, calls } = demoService()
    render(<Journey fetcher={fetcher} />)

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    const rendered = calls.find((call) => call.path === RENDER_PATH)
    const body = bodyOf(rendered?.init) as { intent?: unknown; profile?: unknown }
    // The intent the buyer actually confirmed, and the profile the service's own vault and
    // coarsener answered with. Neither is composed here.
    expect(body.intent).toEqual(INTENT)
    expect(body.profile).toEqual({ pseudonym: VAULT_PSEUDONYM, buckets: BUCKETS })
  })
})

/**
 * What the page says about the code it hands over, and the seeded beat that follows it.
 *
 * Two additions to the four beats above, and each one is about a sentence being in the right
 * place rather than merely existing:
 *
 *  * Step 4's permalink carries a discount code this exchange minted for a storefront it is not
 *    integrated with, so the code WILL be refused at that store's checkout. The page says so.
 *    Presence is not enough — the warning has to be above the button, because a shopper who
 *    reads it after clicking has already met the refusal as what looks like a bug. The order
 *    assertions below are the whole point of these tests; a presence-only check would stay green
 *    if the copy moved underneath the link.
 *  * Step 5 is SEEDED and unconditional. The journey it would follow does not exist — R14's
 *    prompt needs an order reference that arrives days after delivery, and the served journey
 *    ends at the checkout handoff — so the panel shows a manufactured order in the place a real
 *    one will occupy. What is NOT manufactured is asserted here: the component is the real
 *    `FeedbackPromptView`, and the question and the five options are the ones
 *    `apps/buyer/svc/src/feedback/prompt.py` publishes, spelled out below rather than read back
 *    off the artifact, so a seed file that quietly shipped four options or reworded one fails.
 */
describe('the demonstration code, and the seeded beat after the handoff', () => {
  /** The four-beats walk, driven to the point where the exchange has minted a permalink. */
  async function walkToCheckout(): Promise<void> {
    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')
    fireEvent.click(screen.getByRole('button', { name: /accept this one/i }))
    await screen.findByTestId('permalink-url')
  }

  /** The five, in order, from `prompt.py::FEEDBACK_CHOICES`. Written out, not derived. */
  const CHOICE_LABELS = [
    'Yes — it was what the store described',
    'Yes, but it arrived later than promised',
    'No — it was not what the store described',
    'No — a different item arrived',
    'It never arrived',
  ]
  const CHOICE_IDS = [
    'yes_as_described',
    'as_described_but_late',
    'not_as_described',
    'wrong_item',
    'never_arrived',
  ]

  it('names the discount code and says the store will reject it, ABOVE the button', async () => {
    const { fetcher } = demoService()
    const { container } = render(<Journey fetcher={fetcher} />)

    await walkToCheckout()

    const warning = screen.getByTestId('discount-code-is-a-demonstration')
    const said = warning.textContent ?? ''
    // The actual code off the actual permalink, not the word "code".
    expect(said).toContain('PSX-MC4DM9A1')
    expect(said).toContain('demonstration')
    // What will happen, in the future tense, about the store the link really points at.
    expect(said).toMatch(/reject/i)
    expect(said).toContain('demo-woolworks.example.com')

    // ORDER, twice, two ways. This is the assertion the test exists for: a warning below the
    // button is a warning the shopper meets after the refusal it was written to prevent.
    const link = screen.getByTestId('permalink-link')
    expect(
      warning.compareDocumentPosition(link) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    const inDocumentOrder = Array.from(
      container.querySelectorAll(
        '[data-testid="discount-code-is-a-demonstration"], [data-testid="permalink-link"]',
      ),
    ).map((element) => element.getAttribute('data-testid'))
    expect(inDocumentOrder).toEqual(['discount-code-is-a-demonstration', 'permalink-link'])
  })

  it('says nothing about a demonstration code while there is no code to warn about', async () => {
    const { fetcher } = demoService()
    render(<Journey fetcher={fetcher} />)

    expect(screen.queryByTestId('discount-code-is-a-demonstration')).toBeNull()

    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    // The shortlist is on the page and nothing has been accepted, so no permalink and no code
    // exist yet. A warning about a code that has not been minted would be the page inventing a
    // fact about a checkout that has not happened.
    expect(screen.queryByTestId('discount-code')).toBeNull()
    expect(screen.queryByTestId('discount-code-is-a-demonstration')).toBeNull()
  })

  it('renders the seeded Step 5 on arrival, with no walk and nothing bought', async () => {
    const { fetcher } = demoService()
    render(<Journey fetcher={fetcher} />)
    await screen.findByTestId('signed-in')

    // Nothing was said, confirmed, shortlisted or accepted — and the panel is there anyway,
    // because it is not waiting on a journey that cannot reach it.
    expect(screen.queryByTestId('permalink-url')).toBeNull()
    expect(screen.getByTestId('feedback-seeded-badge').textContent).toContain('SEEDED')
    // The artifact passed its own marker check, so this is a prompt and not a refusal notice.
    expect(screen.queryByTestId('feedback-seed-refused')).toBeNull()

    expect(SEEDED_PROMPT).not.toBeNull()
    const explanation = screen.getByTestId('feedback-seeded-explanation').textContent ?? ''
    // The reference itself is on the page, beside the prefix that makes it recognisable — so a
    // reader can tell this row apart from an earned one from the page alone, without being told.
    expect(explanation).toContain(SEEDED_PROMPT!.order_ref)
    expect(explanation).toContain(SEEDED_PREFIX)
    expect(SEEDED_PREFIX).toBe('sim-fb-')
    expect(SEEDED_PROMPT!.order_ref.startsWith('sim-fb-')).toBe(true)
    // And it says what is manufactured about it, rather than leaving SEEDED to carry the load.
    expect(explanation).toMatch(/manufactured/i)
  })

  it('shows the real question and the five real options the buyer service publishes', async () => {
    const { fetcher } = demoService()
    render(<Journey fetcher={fetcher} />)
    await screen.findByTestId('signed-in')

    const step5 = screen.getByLabelText('Step 5 - after your purchase (seeded)')

    // `PROMPT_QUESTION` in `apps/buyer/svc/src/feedback/prompt.py`, verbatim.
    expect(screen.getByTestId('feedback-question').textContent).toBe(
      'Did what arrived match what the store pitched?',
    )

    const radios = Array.from(step5.querySelectorAll('input[type="radio"]'))
    expect(radios).toHaveLength(5)
    // The option ids are what the ledger records as `reason`, so they are asserted too — a
    // relabelled option with an invented id would put a value in the trust ledger that the
    // service's own closed vocabulary does not contain.
    expect(radios.map((radio) => (radio as HTMLInputElement).value)).toEqual(CHOICE_IDS)
    // One radio group, named for the question, which is also the ledger payload key.
    expect(new Set(radios.map((radio) => (radio as HTMLInputElement).name))).toEqual(
      new Set(['matched_pitch']),
    )

    const labels = Array.from(step5.querySelectorAll('label')).map(
      (label) => label.textContent ?? '',
    )
    expect(labels).toEqual(CHOICE_LABELS)
    // Named individually as well, so a failure says which of the five moved.
    expect(labels).toContain('Yes, but it arrived later than promised')
    expect(labels).toContain('No — a different item arrived')

    // R14 read literally: no free-text field reached the page by way of the seeded panel.
    expect(step5.querySelectorAll('textarea')).toHaveLength(0)
    expect(step5.querySelectorAll('input:not([type="radio"])')).toHaveLength(0)
  })

  it('adds no link of its own: after accept the permalink is still the only href on the page', async () => {
    const { fetcher } = demoService()
    const { container } = render(<Journey fetcher={fetcher} />)

    await walkToCheckout()

    // Step 4 and Step 5 are both on the page at once...
    expect(screen.getByTestId('permalink-link').getAttribute('href')).toBe(PERMALINK)
    expect(screen.getByTestId('feedback-seeded-badge')).toBeTruthy()
    // ...and the seeded panel contributed nothing a browser can be sent to. The one-href
    // assertion in `the four beats` is the guard; this restates it beside the section that
    // would break it, so a future link added to Step 5 fails a test that names Step 5.
    expect(container.querySelectorAll('a[href]')).toHaveLength(1)
    expect(
      screen.getByLabelText('Step 5 - after your purchase (seeded)').querySelectorAll('a[href]'),
    ).toHaveLength(0)
  })
})

/**
 * The deployment that CANNOT deliver a login link, which is most of them (SPEC R5).
 *
 * WHAT THIS BLOCK IS ABOUT, because it is the newer half of a pair and the pair is the whole
 * point. Everything above renders the journey behind a sign-in gate, and that is still exactly
 * what happens — on a deployment whose buyer service holds a real mail transport. The shared
 * `authAnswer` fixture describes such a deployment; it answers `GET /buyer/auth/sign-in` with
 * `{offered: true}` and every gate assertion above is unchanged.
 *
 * This block describes the other one, and it is not a corner case: the hosted demo runs
 * `PROXYSHOP_BUYER_MAGIC_LINK_TRANSPORT=console` with every SMTP variable empty, and no MTA
 * exists anywhere in this project to point at. On that deployment the sign-in panel's own
 * words — "No password. We email you a single-use link" — were false, the link never came,
 * and the entire product sat behind a door with no key. `Journey` now asks the service
 * whether a login can be completed and offers the form only where it can.
 *
 * So the two things asserted here are: nothing on screen mentions email where no email can be
 * sent, and the journey a shopper reaches instead is the WHOLE journey rather than a degraded
 * one — same five beats, same real requests, with the exchange naming the shopper per auction
 * because there is no vault-minted handle to send.
 */
describe('R5 — a deployment with no mail transport asks nobody to sign in', () => {
  /** `demoService`, with the one answer that says this deployment cannot mail. */
  function withoutMailTransport(options: Parameters<typeof demoService>[0] = {}) {
    return demoService({
      ...options,
      auth: (path, init) =>
        path === SIGN_IN_PATH ? json({ offered: false }) : options.auth?.(path, init),
    })
  }

  it('opens on the journey, with no form and no word about email', async () => {
    signedOut()
    const { fetcher, calls } = withoutMailTransport()
    render(<Journey fetcher={fetcher} />)

    // The composer, which is the gate's own proof in every test above: reaching it means the
    // wall is not there.
    expect(await screen.findByLabelText('What are you shopping for?')).toBeInTheDocument()

    // Not merely "the form is hidden" — the whole section is gone, heading included. A
    // deployment that cannot log anybody in should not have a "Sign in" heading with an
    // apology under it; it should look like a product that does not ask.
    expect(screen.queryByLabelText('Email address')).toBeNull()
    expect(screen.queryByTestId('signin')).toBeNull()
    expect(screen.queryByTestId('why-sign-in')).toBeNull()
    expect(screen.queryByTestId('signin-unknown')).toBeNull()
    expect(screen.queryByRole('button', { name: /sign out/i })).toBeNull()

    // THE DEFECT THIS EXISTS TO CLOSE, asserted over the whole document rather than over the
    // component that used to carry it: a deployment that cannot send mail must not talk
    // about sending you mail. Checked as a substring sweep and not against the sign-in
    // component, because the failure being prevented is a sentence surviving somewhere
    // nobody thought to look.
    //
    // ONE MENTION OF THE WORD SURVIVES, and it is deliberately allowed. Step 5's seeded
    // panel ends "We do not pass your name, your address or your email to the store" —
    // `feedback/FeedbackPromptView.tsx`. That is a claim about what reaches a STORE, it is
    // true on every deployment, and it promises the shopper nothing. The defect is a page
    // saying a mail is coming; a page saying one is not being forwarded is the opposite of
    // it. So the sweep runs over everything OUTSIDE that panel, which keeps the check strict
    // where it matters without pretending a true sentence is a false one.
    const step5 = screen.getByLabelText('Step 5 - after your purchase (seeded)')
    const elsewhere = (document.body.textContent ?? '')
      .replace(step5.textContent ?? '', '')
      .toLowerCase()
    expect(elsewhere).not.toContain('email')
    expect(elsewhere).not.toContain('mailbox')
    expect(elsewhere).not.toContain('sign in')
    expect(elsewhere).not.toContain('single-use link')

    // And it asked for no link and redeemed no token to get here. The only auth call made is
    // the capability question itself.
    const authCalls = calls
      .map((call) => call.path)
      .filter((path) => path.startsWith('/buyer/auth') || path === PROFILE_PATH)
    expect(authCalls).toEqual([SIGN_IN_PATH])
  })

  it('runs the whole journey with no session, naming no handle it did not send', async () => {
    signedOut()
    const { fetcher, calls } = withoutMailTransport()
    render(<Journey fetcher={fetcher} />)

    // The same walk the signed-in tests do, minus the wait for a session there is none of.
    fireEvent.change(await screen.findByLabelText('What are you shopping for?'), {
      target: { value: 'I want a warm merino wool beanie for winter, under $100' },
    })
    fireEvent.submit(screen.getByLabelText('What are you shopping for?').closest('form')!)
    const question = await screen.findByLabelText('What is your budget?')
    fireEvent.change(question, { target: { value: 'about $100' } })
    fireEvent.submit(question.closest('form')!)

    fireEvent.click(await screen.findByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByLabelText('Shortlist')

    // BEAT BY BEAT, and every one of them a real request. This is the claim that matters:
    // the sessionless page is not a preview or a subset, it is the journey.
    const reached = calls.map((call) => call.path)
    expect(reached).toContain(CLARIFY_PATH)
    expect(reached).toContain(CONFIRM_PATH)
    expect(reached).toContain(auctionPath(AUCTION_ID))
    expect(reached).toContain(RENDER_PATH)

    // THE ONE ROUTE THAT REALLY NEEDS A SESSION IS NOT CALLED. `GET /buyer/profile` answers
    // 401 without an `X-Buyer-Session` header, so calling it here would put a refusal on
    // screen that the shopper caused by existing. Not called at all is the coherent answer.
    expect(reached).not.toContain(PROFILE_PATH)
    // Nothing anywhere sent a session header, because there is no session to send.
    expect(calls.every((call) => bodyHasNoSessionHeader(call.init))).toBe(true)

    // THE CONFIRMATION CARRIES NO PROFILE, and that is deliberate rather than an omission:
    // `exchange.composition.solicitation_profile` mints `anon-{auction_id}` with empty
    // buckets for a confirmation that names none, and the stores bid against it normally. A
    // `profile` field here would be this page naming a buyer it cannot name.
    const confirmed = bodyOf(calls.find((call) => call.path === CONFIRM_PATH)?.init) as Record<
      string,
      unknown
    >
    expect(Object.keys(confirmed).sort()).toEqual(['confirmed', 'intent'])
    expect(confirmed.confirmed).toBe(true)

    // The label render is asked the same way, so the buyer-side case is written in its
    // unconditioned voice rather than addressed to a shopper this page invented.
    const rendered = bodyOf(calls.find((call) => call.path === RENDER_PATH)?.init) as Record<
      string,
      unknown
    >
    expect('profile' in rendered).toBe(false)

    // AND THE PAGE SAYS SO. It does not print a handle, and it does not print the signed-in
    // line claiming a vault minted one.
    expect(screen.queryByTestId('pseudonym')).toBeNull()
    expect(screen.queryByTestId('signed-in')).toBeNull()
    const anonymous = screen.getByTestId('pseudonym-anonymous').textContent ?? ''
    expect(anonymous).toContain('anon-')
    expect(anonymous).toContain('empty set of buckets')
    // No vault pseudonym anywhere on the page, because none was ever minted for this visitor.
    expect(document.body.textContent ?? '').not.toMatch(/psn-/)
  })

  it('shows the journey rather than a wall when it cannot ask the service at all', async () => {
    // THE FALLBACK, DRIVEN. An old service with no such route, or a proxy that eats it, is a
    // deployment this page cannot PROVE can deliver a login — and "cannot prove it can" has
    // to land on the same page as "cannot", because failing the other way puts a shopper in
    // front of a gate nothing has said anyone can pass. It must also be silent: this is a
    // question the page asked itself, not a gesture anybody made, so it earns no banner.
    signedOut()
    const { fetcher } = demoService({
      auth: (path) => (path === SIGN_IN_PATH ? json({ detail: 'no such route' }, 404) : undefined),
    })
    render(<Journey fetcher={fetcher} />)

    expect(await screen.findByLabelText('What are you shopping for?')).toBeInTheDocument()
    expect(screen.queryByLabelText('Email address')).toBeNull()
    expect(screen.queryByTestId('journey-error')).toBeNull()
  })

  it('still gates, unchanged, the moment the service says it can mail', async () => {
    // THE OTHER DIRECTION, side by side with its opposite so the pair cannot drift apart.
    // This is the assertion that will rot silently if it is not here: nothing on the local
    // stack runs an MTA, so every hand-driven check of this change exercises the no-mail
    // branch, and a predicate that accidentally answered `false` everywhere would delete the
    // login from a working deployment with every other test still green.
    signedOut()
    const { fetcher } = demoService()
    render(<Journey fetcher={fetcher} />)

    expect(await screen.findByLabelText('Email address')).toBeInTheDocument()
    expect(screen.getByTestId('why-sign-in')).toBeInTheDocument()
    // And the wall is a real wall: not one beat of the journey is in the document.
    expect(screen.queryByLabelText('What are you shopping for?')).toBeNull()
    expect(screen.queryByTestId('transcript')).toBeNull()
    expect(screen.queryByTestId('pseudonym-anonymous')).toBeNull()
  })

  it('lets somebody holding an old link sign in, and sign back out, with no form on offer', async () => {
    // A STATE THAT IS REACHABLE AND WOULD OTHERWISE TRAP SOMEBODY. `Journey` redeems a
    // `?token=` out of the address bar whatever this deployment can or cannot mail, so a link
    // issued before the MTA was switched off — or read out of a `console` deployment's own
    // log — still opens a real session. The sign-in section is where the Sign out button
    // lives, so hiding it purely on `offered === false` would leave that person signed in
    // with no way to end it, and step 1 printing a vault pseudonym on a page that otherwise
    // claims to have no login at all.
    arrivingFromTheMailbox()
    const { fetcher } = withoutMailTransport()
    render(<Journey fetcher={fetcher} />)

    // Signed in, and the panel that says so is on the page.
    await screen.findByTestId('signed-in')
    const signOut = screen.getByRole('button', { name: /sign out/i })
    // But no FORM: nothing offers a link this deployment could not send.
    expect(screen.queryByLabelText('Email address')).toBeNull()
    expect(screen.queryByTestId('why-sign-in')).toBeNull()
    // And the journey is open, because a session is not what gates it here.
    expect(screen.getByLabelText('What are you shopping for?')).toBeInTheDocument()
    // With a real handle, so the anonymous line correctly stands down.
    expect(screen.getByTestId('pseudonym').textContent).toContain(VAULT_PSEUDONYM)
    expect(screen.queryByTestId('pseudonym-anonymous')).toBeNull()

    // Signing out ends it and leaves the shopper in the journey rather than at a wall.
    fireEvent.click(signOut)
    await waitFor(() => expect(screen.queryByTestId('signed-in')).toBeNull())
    expect(screen.getByLabelText('What are you shopping for?')).toBeInTheDocument()
    expect(screen.queryByLabelText('Email address')).toBeNull()
    expect(screen.getByTestId('pseudonym-anonymous')).toBeInTheDocument()
  })

  it('shows neither the form nor the journey until the service has answered', async () => {
    // The third state, and it is not decoration. Opening on the form and removing it says
    // "sign in" to a deployment that cannot log anyone in; opening on the journey and then
    // walling it throws away whatever a visitor had begun typing. So the page commits to
    // neither until it knows, and this drives that window rather than assuming it is brief.
    signedOut()
    let answer!: (offered: boolean) => void
    const pending = new Promise<boolean>((resolve) => {
      answer = resolve
    })
    const inner = demoService()
    const fetcher: Fetcher = async (path, init) =>
      path === SIGN_IN_PATH ? json({ offered: await pending }) : inner.fetcher(path, init)

    render(<Journey fetcher={fetcher} />)

    await screen.findByTestId('signin-unknown')
    expect(screen.queryByLabelText('Email address')).toBeNull()
    expect(screen.queryByLabelText('What are you shopping for?')).toBeNull()

    answer(false)
    expect(await screen.findByLabelText('What are you shopping for?')).toBeInTheDocument()
    expect(screen.queryByTestId('signin-unknown')).toBeNull()
  })
})

/** True when `init` carries no `X-Buyer-Session` header, however the headers were spelled. */
function bodyHasNoSessionHeader(init: RequestInit | undefined): boolean {
  const headers = init?.headers
  if (headers === undefined) return true
  const asRecord = headers as Record<string, unknown>
  return asRecord[SESSION_HEADER] === undefined
}

/**
 * The follow-up box under the shortlist — mounted, wired, and only where it can work.
 *
 * The journey used to end at the shortlist. These tests are about the three things that make
 * the box safe on THIS page rather than about the panel itself (`chat/AskPanel.test.tsx`
 * covers the layout, and `chat/ask.test.ts` covers the boundary):
 *
 * 1. it is mounted where there are live options and NOWHERE ELSE, because a question box on
 *    a page whose auction the exchange has forgotten could only ever fail;
 * 2. what it sends is the auction id this page was given and the shopper's words — never the
 *    slots on screen, which is the property that stops a browser making the platform assert
 *    something (D55); and
 * 3. a refused question leaves the shortlist standing.
 */
describe('asking a follow-up about the shortlist', () => {
  const ANSWER_BODY = {
    auction_id: AUCTION_ID,
    question: 'which of these is actually third-party tested?',
    answer:
      'I don’t know about “third-party tested”: no shop on this shortlist has published a ' +
      'claim for it and the platform’s crawl did not record one.',
    answer_source: 'assembled',
    grounds: [],
    not_held: [
      {
        subject: 'third-party tested',
        detail:
          'no shop on this shortlist has published a claim for it and the platform’s crawl ' +
          'did not record one',
      },
    ],
    shop_messages: [],
    ranking_recorded: true,
  }

  /** `demoService`, plus an answer for `POST /buyer/chat/ask`. */
  function withAnswers(
    answer: (init: RequestInit | undefined) => Response,
    options: Parameters<typeof demoService>[0] = {},
  ) {
    const inner = demoService(options)
    const asked: RequestInit[] = []
    const fetcher: Fetcher = async (path, init) => {
      if (path !== ASK_PATH) return inner.fetcher(path, init)
      if (init !== undefined) asked.push(init)
      return answer(init)
    }
    return { fetcher, asked }
  }

  async function walkToShortlist(): Promise<void> {
    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))
    await screen.findByTestId('ask-panel')
  }

  it('sends the auction id and the question, and never the slots on screen', async () => {
    const { fetcher, asked } = withAnswers(() => json(ANSWER_BODY))
    render(<Journey fetcher={fetcher} />)
    await walkToShortlist()

    fireEvent.change(screen.getByTestId('ask-input'), {
      target: { value: 'which of these is actually third-party tested?' },
    })
    fireEvent.click(screen.getByTestId('ask-send'))
    await screen.findByTestId('ask-answer-0')

    expect(asked).toHaveLength(1)
    const sent = JSON.parse(String(asked[0]?.body)) as Record<string, unknown>
    // Two keys. The page holds the whole rendered shortlist and sends none of it: the
    // service fetches its own material from the exchange, so nothing this browser is
    // holding can become something the platform asserts to the person reading it.
    expect(Object.keys(sent).sort()).toEqual(['auction_id', 'question'])
    expect(sent.auction_id).toBe(AUCTION_ID)
    expect(screen.getByTestId('ask-not-held-0').textContent).toContain('third-party tested')
  })

  it('is not mounted on an auction the exchange has forgotten', async () => {
    // There is no shortlist to answer from, `POST /buyer/chat/ask` would 404 on it, and a
    // question box that could only fail is worse than no question box.
    const { fetcher } = withAnswers(() => json(ANSWER_BODY), { forgotten: true })
    render(<Journey fetcher={fetcher} />)
    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))

    await screen.findByTestId('shortlist-forgotten')
    expect(screen.queryByTestId('ask-panel')).toBeNull()
  })

  it('is not mounted when the market came back empty', async () => {
    const { fetcher } = withAnswers(() => json(ANSWER_BODY), { slots: [] })
    render(<Journey fetcher={fetcher} />)
    await walkToConfirm()
    fireEvent.click(screen.getByRole('button', { name: /confirm and ask stores/i }))

    await screen.findByLabelText('Why the shortlist is empty')
    expect(screen.queryByTestId('ask-panel')).toBeNull()
  })

  it('leaves the shortlist standing when a question is refused', async () => {
    const { fetcher } = withAnswers(() =>
      json({ detail: 'the exchange holds no shortlist for this auction.' }, 404),
    )
    render(<Journey fetcher={fetcher} />)
    await walkToShortlist()

    fireEvent.change(screen.getByTestId('ask-input'), { target: { value: 'how much?' } })
    fireEvent.click(screen.getByTestId('ask-send'))

    expect((await screen.findByTestId('ask-failure-0')).textContent).toContain('no shortlist')
    // The journey's own failure banner is untouched, and the options are still on screen: a
    // refused question must not blank the page a shopper is in the middle of reading.
    expect(screen.queryByTestId('journey-error')).toBeNull()
    expect(screen.getByTestId('auction-id')).toBeInTheDocument()
  })
})
