/**
 * The learning page — the same query, twice, with real outcomes fed in between.
 *
 * Three beats, in the order a person drives them: run a query and see what the market
 * answered; feed the network purchases and customer feedback through the routes that really
 * carry them; run the SAME query and read what moved, term by term.
 *
 * THE ONE THING THIS PAGE MUST NOT BE. A page that kept the first shortlist and re-rendered
 * it with different numbers would be a screenshot with a button on it. So the second reading
 * is a second `POST /buyer/intent/confirm` — a real auction, a real solicitation of the four
 * store agents, a real ranking — and `compare()` in `loop.ts` is given two readings with no
 * idea where either came from. If the market did not move, this page says the market did not
 * move; there is no branch in it that can produce a difference the exchange did not publish.
 *
 * WHAT IS ON SCREEN THAT IS NOT A NUMBER. Three things, all of them said because leaving them
 * unsaid would make the demo dishonest rather than merely thin:
 *
 *   1. Every manufactured order carries the seed marker, INSIDE the trust ledger's hash
 *      chain, and the page prints both the prefix and the limit of what that prefix proves.
 *   2. Which ranking terms can move in this deployment and which cannot, with the reason.
 *      Four of the five are identical across every candidate here, and a page that let a
 *      viewer believe the shortlist is being decided by five live signals would be lying by
 *      omission about the one that is doing the work.
 *   3. What resets what. The seller agents' learning is in process memory; the trust ledger
 *      is in Postgres. Those two facts have opposite consequences for a second run of the
 *      demo, and the person driving it should know which is which before they start.
 */
import { useCallback, useMemo, useRef, useState } from 'react'

import {
  ACCEPT_PATH,
  AUCTION_PATH_PREFIX,
  BID_WINDOW_SECONDS,
  CLARIFY_PATH,
  CONFIRM_PATH,
  FEEDBACK_PATH,
  MARKER_LIMIT,
  PROMPT_PATH,
  UnmarkedOutcome,
  WireFailure,
  anythingMoved,
  biddingStores,
  buyTopSlot,
  compare,
  discountOffered,
  figure,
  outcomeIsPositive,
  runQuery,
  seededOrder,
  signed,
  submitOutcome,
  type Candidate,
  type Fetcher,
  type LogLine,
  type Movement,
  type Reading,
} from './loop'
import { SEEDED_PREFIX, SEEDED_REFUSAL } from '../journey/seeded-feedback'

/** The browser's own fetch. Relative paths only — the API is served from this origin. */
const browserFetch: Fetcher = (input, init) => fetch(input, init)

/**
 * The query the page opens with, and the reason it is this one rather than a placeholder.
 *
 * The four store agents in the demo stack pursue exactly one cluster —
 * `pursue_clusters: ["cluster-liver-support"]` in every `deploy/demo/store-contexts/*.json` —
 * and the exchange maps an intent onto it by the terms in `intent_clusters`: "milk thistle",
 * "silymarin", "liver support", "liver", "detox", "dandelion root", "artichoke extract".
 * A query outside those terms is answered by four `204 cluster_not_pursued` declines and a
 * shortlist of nothing but ProxyShop's own fallback listings, which is a demo of the roster
 * rather than of the market. Measured on the running stack, both ways.
 */
export const DEFAULT_QUERY = 'milk thistle liver support supplement'

/**
 * How many rounds of real trade one press of the button runs.
 *
 * Each round is a whole auction — solicit, bid, rank, accept, review — so this is the number
 * that decides how long the button takes and how much the sellers get to learn. Measured on
 * the running compose stack: about 10 seconds per round, and the four agents' offers settle
 * into a consistent discounted posture by the fourth. Six is the smallest number that was
 * reliably past that point with margin.
 */
export const OUTCOME_ROUNDS = 6

/**
 * How many shoppers report on each shop in each round.
 *
 * The trust engine caps one observation's weight at 1.0 on purpose, so a single buyer cannot
 * count for five; volume is the only honest way to move a posterior. Four per shop per round
 * is 24 sealed ledger events per round and about 96 for a press of the button.
 */
export const REVIEWERS_PER_ROUND = 4

/**
 * How far one shop's `rank_score` moves between two identical queries with NO teaching at all.
 *
 * The sellers Thompson-sample a discount rung per auction
 * (`store_agent.learning.state.sample_depth`), so the same query asked twice can be answered
 * at a different depth, and `price_value` — a published term — moves with it. That is the
 * loop exploring, and it is the reason this page cannot let one before/after pair stand as
 * evidence on its own.
 *
 * MEASURED on the running compose stack, `gaiaherbs.com` on the default query:
 *
 *     8 identical queries, no teaching   0.4970 – 0.5395   spread 0.0425
 *     after 16 sealed feedback events    mean shift +0.0024
 *     after 64 sealed feedback events    mean shift +0.0071
 *
 * The shift is real, positive and grows with volume — and it is smaller than the spread. The
 * three shops that never discounted moved by exactly 0.0000 in every run, which is what says
 * the spread is the sampling and not this page.
 */
export const NO_TEACHING_SPREAD = 0.0425

/** The mean shift measured after four times what one press of the button feeds one shop. */
export const MEASURED_SHIFT_AT_64_EVENTS = 0.0071

export interface LearningPageProps {
  /** Injected in tests. Left alone, the page talks to the service on its own origin. */
  readonly fetcher?: Fetcher
  /** Injected in tests so a run is not held up by a real bid window. */
  readonly bidWindowSeconds?: number
  /** Injected in tests so a run is a handful of requests rather than a hundred. */
  readonly rounds?: number
  /** Injected in tests. */
  readonly reviewersPerRound?: number
}

/** A panel that names the route it read, so no figure on the page is anonymous. */
function Panel({
  title,
  route,
  children,
}: {
  readonly title: string
  readonly route: string
  readonly children: React.ReactNode
}) {
  return (
    <section className="step" aria-label={title}>
      <h2>{title}</h2>
      <p className="metrics-source mono">{route}</p>
      {children}
    </section>
  )
}

/** One reading of the market, as the exchange published it. */
function Shortlist({ reading }: { readonly reading: Reading }) {
  if (reading.candidates.length === 0) {
    return (
      <p className="metrics-empty">
        The exchange ranked nobody in this auction. That is a real answer and not a failure to
        read one — every candidate was excluded or denied, and{' '}
        <code>GET {AUCTION_PATH_PREFIX}
          {reading.auction_id}</code>{' '}
        carries the reasons.
      </p>
    )
  }
  return (
    <table className="learning-table">
      <caption className="mono">
        auction {reading.auction_id}
        {reading.recorded_at === null ? '' : ` · recorded ${reading.recorded_at}`}
      </caption>
      <thead>
        <tr>
          <th scope="col">#</th>
          <th scope="col">shop</th>
          <th scope="col">rank_score</th>
          <th scope="col">offer</th>
          <th scope="col">the five published terms</th>
        </tr>
      </thead>
      <tbody>
        {reading.candidates.map((row, index) => (
          <tr key={row.store_id} data-store={row.store_id}>
            <td className="mono">{index + 1}</td>
            <td>
              {row.store_id}
              {row.fallback ? (
                <span data-tone="unknown" title={row.fallback_reason ?? ''}>
                  ProxyShop listing
                </span>
              ) : null}
            </td>
            <td className="mono">{figure(row.rank_score)}</td>
            <td className="mono">{row.unit_price === undefined ? '—' : row.unit_price.toFixed(2)}</td>
            <td className="mono learning-terms">{terms(row)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/**
 * The components, spelled with the exchange's own keys.
 *
 * Never re-spelled here: the five names are `contracts.ranking.RANK_FEATURES`, the exchange
 * adds a sixth (`policy_penalties`) when a penalty applied, and a page that listed five would
 * hide the one term that is a punishment.
 */
function terms(row: Candidate): string {
  const entries = Object.entries(row.components)
  if (entries.length === 0) return 'the exchange published no components'
  return entries.map(([key, value]) => `${key}=${value.toFixed(4)}`).join('  ')
}

/** What moved, term by term, for every shop in either reading. */
function Movements({ movements }: { readonly movements: readonly Movement[] }) {
  return (
    <table className="learning-table" data-testid="movement-table">
      <thead>
        <tr>
          <th scope="col">shop</th>
          <th scope="col">place</th>
          <th scope="col">rank_score</th>
          <th scope="col">offer</th>
          <th scope="col">which term moved</th>
        </tr>
      </thead>
      <tbody>
        {movements.map((row) => (
          <tr key={row.store_id} data-store={row.store_id}>
            <td>{row.store_id}</td>
            <td className="mono">{place(row)}</td>
            <td className="mono">
              {figure(row.rank_before)} → {figure(row.rank_after)}{' '}
              <strong data-delta={direction(row.rank_delta)}>{signed(row.rank_delta)}</strong>
            </td>
            <td className="mono">
              {row.price_before === undefined ? '—' : row.price_before.toFixed(2)} →{' '}
              {row.price_after === undefined ? '—' : row.price_after.toFixed(2)}
            </td>
            <td className="learning-terms">
              <ul>
                {row.components.map((term) => (
                  <li key={term.key} className="mono" data-delta={direction(term.delta)}>
                    {term.key} {figure(term.before)} → {figure(term.after)}{' '}
                    {term.held ? <em>held</em> : <strong>{signed(term.delta)}</strong>}
                  </li>
                ))}
              </ul>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function direction(delta: number | undefined): string {
  if (delta === undefined) return 'absent'
  if (delta > 0) return 'up'
  if (delta < 0) return 'down'
  return 'flat'
}

function place(row: Movement): string {
  const before = row.position_before === null ? 'not ranked' : `#${row.position_before + 1}`
  const after = row.position_after === null ? 'not ranked' : `#${row.position_after + 1}`
  return `${before} → ${after}`
}

/** Why a request failed, in the service's own words wherever the service supplied them. */
function explain(error: unknown): string {
  if (error instanceof WireFailure) return error.message
  if (error instanceof UnmarkedOutcome) return error.message
  if (error instanceof Error && error.message !== '') return error.message
  return 'the request failed and said nothing about why'
}

export function LearningPage({
  fetcher = browserFetch,
  bidWindowSeconds = BID_WINDOW_SECONDS,
  rounds = OUTCOME_ROUNDS,
  reviewersPerRound = REVIEWERS_PER_ROUND,
}: LearningPageProps = {}) {
  const [query, setQuery] = useState(DEFAULT_QUERY)
  const [before, setBefore] = useState<Reading | null>(null)
  const [after, setAfter] = useState<Reading | null>(null)
  const [fed, setFed] = useState(0)
  const [log, setLog] = useState<readonly LogLine[]>([])
  const [busy, setBusy] = useState<'query' | 'outcomes' | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  // The query the FIRST reading was run for. Beat three is "the same query", and a page that
  // let the box be edited between the two readings would be comparing two different questions
  // while calling the difference learning.
  const asked = useRef<string | null>(null)

  const note = useCallback((what: string, detail: string, ok: boolean) => {
    setLog((rows) => [
      ...rows,
      { at: new Date().toISOString(), what, detail, ok },
    ])
  }, [])

  const movements = useMemo(
    () => (before === null || after === null ? null : compare(before, after)),
    [before, after],
  )

  const run = useCallback(async () => {
    setBusy('query')
    setFailure(null)
    const wanted = before === null ? query.trim() : (asked.current ?? query.trim())
    try {
      note('run query', `POST ${CONFIRM_PATH} — "${wanted}"`, true)
      const reading = await runQuery(wanted, fetcher, bidWindowSeconds)
      note(
        'read auction',
        `GET ${AUCTION_PATH_PREFIX}${reading.auction_id} — ${reading.candidates.length} ranked`,
        true,
      )
      if (before === null) {
        asked.current = wanted
        setBefore(reading)
        setAfter(null)
      } else {
        setAfter(reading)
      }
    } catch (error) {
      const why = explain(error)
      note('run query', why, false)
      setFailure(why)
    } finally {
      setBusy(null)
    }
  }, [before, query, fetcher, bidWindowSeconds, note])

  const feed = useCallback(async () => {
    setBusy('outcomes')
    setFailure(null)
    const wanted = asked.current ?? query.trim()
    let events = 0
    try {
      for (let round = 0; round < rounds; round += 1) {
        const reading = await runQuery(wanted, fetcher, bidWindowSeconds)
        const shortlist = await liveShortlist(reading.auction_id, fetcher)
        const bought = await buyTopSlot(shortlist, reading.auction_id, fetcher)
        note(
          `round ${round + 1}: purchase`,
          `POST ${ACCEPT_PATH} — ${bought.store_id} — ${bought.permalink_url}`,
          true,
        )
        let index = 0
        for (const store of biddingStores(reading)) {
          const discount = discountOffered(shortlist, store) ?? 0
          const positive = outcomeIsPositive(discount)
          for (let n = 0; n < reviewersPerRound; n += 1) {
            const order = seededOrder(round, index, store, reading.auction_id)
            const eventId = await submitOutcome(order, positive, fetcher)
            events += 1
            index += 1
            note(
              `round ${round + 1}: feedback`,
              `POST ${FEEDBACK_PATH} — ${order.order_ref} — ` +
                `${positive ? 'as described' : 'not as described'} (shop offered ${discount}% off) ` +
                `— ledger event ${eventId}`,
              true,
            )
          }
        }
      }
      setFed((count) => count + events)
    } catch (error) {
      const why = explain(error)
      note('feed outcomes', why, false)
      setFailure(why)
      setFed((count) => count + events)
    } finally {
      setBusy(null)
    }
  }, [query, fetcher, bidWindowSeconds, rounds, reviewersPerRound, note])

  const reset = useCallback(() => {
    setBefore(null)
    setAfter(null)
    setFed(0)
    setLog([])
    setFailure(null)
    asked.current = null
  }, [])

  return (
    <main className="journey" data-testid="learning-page">
      <header className="masthead">
        <h1>Watch the network learn</h1>
        <p className="lede">
          Run a query. Feed the network real purchases and real customer feedback. Run the
          same query again and read what moved, and why. Every request on this page goes to a
          served route on this origin — the second shortlist is a second auction, not a
          re-render of the first.
        </p>
      </header>

      {SEEDED_REFUSAL === undefined ? null : (
        <p className="metrics-warning" role="alert">
          The seed artifact this page reads its marker out of failed its own check:{' '}
          {SEEDED_REFUSAL}. Feeding outcomes is refused, because an unmarked synthetic review
          reaches an append-only ledger with no delete.
        </p>
      )}

      <section className="step" aria-label="The query">
        <h2>1 &middot; Ask the market something</h2>
        <div className="metrics-form">
          <label htmlFor="learning-query">What are you shopping for?</label>
          <input
            id="learning-query"
            value={query}
            disabled={before !== null || busy !== null}
            onChange={(event) => {
              setQuery(event.target.value)
            }}
          />
          <button
            type="button"
            className="primary"
            disabled={busy !== null || query.trim() === ''}
            onClick={() => {
              void run()
            }}
          >
            {busy === 'query'
              ? 'Running the auction…'
              : before === null
                ? 'Run this query'
                : 'Run the same query again'}
          </button>
          {before === null ? null : (
            <button
              type="button"
              className="linkish"
              disabled={busy !== null}
              onClick={reset}
            >
              Start over
            </button>
          )}
        </div>
        <p className="learning-hint">
          The four store agents in this stack pursue one cluster —{' '}
          <code className="mono">cluster-liver-support</code> — so a query has to be about
          milk thistle, silymarin, liver support, detox, dandelion root or artichoke extract
          for anyone to bid. Anything else is answered by four{' '}
          <code className="mono">204 cluster_not_pursued</code> declines and a shortlist of
          ProxyShop&apos;s own fallback listings. The box locks once the first auction has run,
          because beat three is <em>the same query</em>.
        </p>
        {failure === null ? null : (
          <p className="metrics-warning" role="alert">
            {failure}
          </p>
        )}
      </section>

      {before === null ? null : (
        <Panel title="2 &middot; What the market answered" route={`GET ${AUCTION_PATH_PREFIX}{auction_id}`}>
          <Shortlist reading={before} />
        </Panel>
      )}

      {before === null ? null : (
        <section className="step" aria-label="Feed the network real outcomes">
          <h2>3 &middot; Feed the network what actually happened</h2>
          <p>
            Each round below is a whole auction, bought and reviewed:{' '}
            <code className="mono">POST {CLARIFY_PATH}</code> and{' '}
            <code className="mono">POST {CONFIRM_PATH}</code> open it,{' '}
            <code className="mono">POST {ACCEPT_PATH}</code> buys the top slot and returns a
            real checkout permalink, and <code className="mono">POST {PROMPT_PATH}</code> plus{' '}
            <code className="mono">POST {FEEDBACK_PATH}</code> record what each shopper
            reported. The purchase reaches the exchange&apos;s bandit; the feedback is sealed
            into the trust ledger, and the trust service pushes the resulting delta to the
            affected shop&apos;s own agent.
          </p>
          <div className="metrics-form">
            <button
              type="button"
              className="primary"
              disabled={busy !== null || SEEDED_REFUSAL !== undefined}
              onClick={() => {
                void feed()
              }}
            >
              {busy === 'outcomes'
                ? 'Trading…'
                : `Feed ${rounds} rounds of purchases and feedback`}
            </button>
            {fed === 0 ? null : (
              <span className="mono" data-testid="fed-count">
                {fed} sealed ledger events so far
              </span>
            )}
          </div>
          <p className="learning-hint">
            A shopper reports satisfaction when the shop gave them a better deal than its list
            price and dissatisfaction when it did not. That is the one thing on this page that
            is modelled rather than measured, and it is stated here rather than buried: it is a
            coherent synthetic buyer, and in a shortlist where four of the five ranking terms
            are identical for every candidate, it teaches the sellers something true about what
            converts here.
          </p>
        </section>
      )}

      {after === null ? null : (
        <Panel
          title="4 &middot; The same query, asked again"
          route={`GET ${AUCTION_PATH_PREFIX}{auction_id}`}
        >
          <Shortlist reading={after} />
        </Panel>
      )}

      {movements === null ? null : (
        <section className="step" aria-label="What changed and why">
          <h2>5 &middot; What changed, and which term did it</h2>
          {anythingMoved(movements) ? (
            <p className="learning-hint">
              <strong>Read one pair with care.</strong> The sellers Thompson-sample a discount
              rung per auction, so <code className="mono">price_value</code> moves between two
              identical queries even with nothing taught in between. Measured on this stack:{' '}
              <code className="mono">{NO_TEACHING_SPREAD.toFixed(4)}</code> of spread across
              eight identical queries and no teaching, against a mean shift of{' '}
              <code className="mono">+{MEASURED_SHIFT_AT_64_EVENTS.toFixed(4)}</code> after
              sixty-four sealed feedback events. The learning is real and it grows with volume;
              a single pair is not how you see it. Run beats one and four a few times over —
              what you are looking for is the middle of the range moving, not any one run.
            </p>
          ) : (
            <p className="metrics-warning" role="alert">
              Nothing moved. Both auctions published the same order and the same terms for
              every candidate. That is what this page shows when the market did not move, and
              it is the honest answer: there is no code path here that can manufacture a
              difference the exchange did not publish. Feed more rounds and ask again.
            </p>
          )}
          <Movements movements={movements} />
        </section>
      )}

      {log.length === 0 ? null : (
        <details className="trace">
          <summary>Every request this page made ({log.length})</summary>
          <ol className="learning-log">
            {log.map((line) => (
              <li key={`${line.at}-${line.what}-${line.detail}`} data-ok={String(line.ok)}>
                <span className="mono">{line.at}</span> <strong>{line.what}</strong>{' '}
                <span className="mono">{line.detail}</span>
              </li>
            ))}
          </ol>
        </details>
      )}

      <section className="gaps" aria-label="What marks the manufactured data">
        <h2>What marks the manufactured data</h2>
        <ul>
          <li>
            <strong>Every order this page invents is marked, in the hash chain.</strong> Its{' '}
            <code className="mono">order_ref</code> begins{' '}
            <code className="mono">{SEEDED_PREFIX}</code>, which is read out of{' '}
            <code className="mono">apps/buyer/app/feedback/seed-data/collection.json</code>{' '}
            rather than spelled in this page&apos;s source, so the producer and the corpus
            cannot drift apart. <code className="mono">buyer_svc.feedback.submission</code>{' '}
            copies that field verbatim onto the sealed ledger event beside{' '}
            <code className="mono">event_hash</code> and <code className="mono">prev_hash</code>.
          </li>
          <li>
            <strong>What the mark does not cover.</strong> {MARKER_LIMIT}
          </li>
          <li>
            <strong>The purchases are real, and that is why the reviews are allowed.</strong>{' '}
            R14 will not take feedback on an order the network did not route — otherwise a shop
            could move its own trust score with reviews of orders it was never given. Each
            round really does call <code className="mono">POST {ACCEPT_PATH}</code> and get a
            checkout back before it reports anything, so the claim that the order was routed is
            one the network can stand behind.
          </li>
        </ul>
      </section>

      <section className="gaps" aria-label="Which terms can move here">
        <h2>Which of the five terms can move in this deployment</h2>
        <ul>
          <li>
            <strong>
              <code className="mono">price_value</code> moves, and it is the term doing the
              work.
            </strong>{' '}
            A shop&apos;s agent samples an arm — pitch variant &times; commitment set &times;
            discount depth — for every auction, and a cold agent&apos;s depth is pinned at
            zero until it has a record in this cluster. Feeding outcomes gives it one, and the
            offer moves off list price. That is the seller half of the loop, and it is what you
            are watching.
          </li>
          <li>
            <strong>
              <code className="mono">trust</code> cannot move here, and the reason is
              configuration rather than code.
            </strong>{' '}
            The exchange ranks against the <code className="mono">trust_snapshot</code> written
            into <code className="mono">deploy/demo/exchange-deployment.json</code>. A
            deployment that states that key keeps it, deliberately — it is a person&apos;s
            statement and the composition root does not overrule one — so the live trust
            service is consulted for eligibility and not for the ranking&apos;s trust term.
            The feedback below <em>is</em> reaching the ledger and <em>is</em> reaching the
            shops&apos; agents; it is the ranker that is reading a frozen copy.
          </li>
          <li>
            <strong>
              <code className="mono">intent_match</code>,{' '}
              <code className="mono">verified_claim_ratio</code> and{' '}
              <code className="mono">delivery_fit</code> read as neutral for everyone.
            </strong>{' '}
            Each is absent from the candidate records this stack produces, and an absent
            feature is scored at the published neutral of 0.5 rather than at zero — so they
            contribute an identical constant to every candidate and cannot separate two shops.
            You will see them hold at exactly the same value in both readings; that is
            correct, not a bug in this page.
          </li>
        </ul>
      </section>

      <section className="gaps" aria-label="What resets this demo">
        <h2>Running it again: what resets, and what does not</h2>
        <ul>
          <li>
            <strong>The sellers&apos; learning is in process memory.</strong> Every store
            agent&apos;s arms live in its own process and nothing writes them down, so{' '}
            <code className="mono">docker compose restart store-agent-gaiaherbs
            store-agent-toniiq store-agent-paradiseherbs store-agent-oregonswildharvest</code>{' '}
            puts all four back to cold — list price, depth pinned at zero — in a few seconds.
            That is the reset to run before a second live demo, and it is the only one you
            need.
          </li>
          <li>
            <strong>The trust ledger is in Postgres and survives.</strong>{' '}
            <code className="mono">docker compose down</code> keeps it (it is in the{' '}
            <code className="mono">pgdata</code> volume) and so every{' '}
            <code className="mono">{SEEDED_PREFIX}</code> event you fed is still there,
            verifiable, tomorrow. <code className="mono">docker compose down -v</code> destroys
            it along with the catalogue graph, and that is the only thing that does.
          </li>
          <li>
            <strong>The exchange&apos;s bandit is in process memory too</strong>, and it is
            re-seeded from the trust snapshot on every call. Restarting the exchange clears it.
            Nothing about it is visible on this page: in this deployment its exploration slice
            never fires, because it promotes only a shop the trust snapshot positively marks{' '}
            <code className="mono">low_data</code> and no row in the demo document carries that
            flag.
          </li>
          <li>
            <strong>Feeding again without a restart keeps teaching.</strong> The agents do not
            forget between presses, so a second press moves them further rather than starting
            over. If you want the clean cold-to-warm story, restart the four agents first.
          </li>
        </ul>
      </section>
    </main>
  )
}

/**
 * The live shortlist of one auction, which is where the offers' discounts live.
 *
 * Read separately from {@link Reading} because a `Reading` is deliberately the RECORDED half —
 * the ranking and the entries the exchange published once — while the accept path needs the
 * LIVE half, re-fetched, and the two must not be quietly joined into one object that hides
 * which clock each number came off. `GET /buyer/auctions/{id}` serves both, labelled.
 */
async function liveShortlist(auctionId: string, fetcher: Fetcher): Promise<unknown> {
  const response = await fetcher(`${AUCTION_PATH_PREFIX}${encodeURIComponent(auctionId)}`, {
    method: 'GET',
    headers: { accept: 'application/json' },
  })
  const text = await response.text()
  let body: unknown = null
  try {
    body = text === '' ? null : (JSON.parse(text) as unknown)
  } catch {
    throw new WireFailure('read auction', response.status, 'the body was not JSON')
  }
  if (!response.ok) throw new WireFailure('read auction', response.status, 'could not be read')
  if (typeof body !== 'object' || body === null) {
    throw new WireFailure('read auction', 200, 'the body was not an object')
  }
  return (body as { shortlist?: unknown }).shortlist ?? null
}

export default LearningPage
