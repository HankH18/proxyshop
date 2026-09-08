/**
 * The metrics and tracing page — a demo aid that shows the machinery while it runs.
 *
 * Every number on this page comes off a served route, and the page says which one beside
 * each panel. Where a source exists but this origin cannot read it, the page says that too,
 * by name, with the reason and the fix — see `telemetry.ts`, whose docstring carries the
 * measurements behind that list. Nothing here is computed from a guess, and nothing renders
 * a zero in place of a value it failed to fetch.
 *
 * WHAT IS DELIBERATELY ABSENT. There is no latency chart, no request rate, no error rate and
 * no uptime, because no service in this stack records any of them: there is no `/metrics`
 * route anywhere, no `/readyz`, and the only `/healthz` in the repo belongs to a fake Shopify
 * on :8787. A chart of numbers nobody produces is the exact defect this page is meant to
 * help find, so it would be a poor thing for it to contain.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  UNREACHABLE_SOURCES,
  type AuctionTrace,
  type Fetcher,
  type LiveCheckReading,
  type ProfileReading,
  type Reading,
  type SessionReading,
  directUrl,
  forget,
  readAuctionTrace,
  readLiveCheck,
  readProfile,
  readSession,
  readingDetail,
  reasonDetail,
  reasonFamily,
  remembered,
  REMEMBERED_AUCTION_KEY,
  AUCTION_PATH_PREFIX,
  LIVECHECK_PATH_PREFIX,
  SESSION_PATH,
  PROFILE_PATH,
} from './telemetry'

/** The browser's own fetch. Relative paths only — the API is served from this origin. */
const browserFetch: Fetcher = (input, init) => fetch(input, init)

export interface MetricsPageProps {
  /** Injected in tests. Left alone, the page talks to the service on its own origin. */
  readonly fetcher?: Fetcher
  /**
   * Injected in tests, and in tests ONLY.
   *
   * Left alone this page holds no session and cannot get one: `Journey` keeps its session id
   * in component state "and NOWHERE else" — its words — because it is a bearer credential for
   * this origin, so navigating to `#/metrics` does not carry it along. The session panel is
   * therefore `unauthorized` on every real page load, and says so in those words. There is no
   * "remembered session" anywhere in this app to read.
   */
  readonly sessionId?: string
  /** Injected in tests. Left alone, read from the journey's remembered auction id. */
  readonly initialAuctionId?: string
  /** Injected in tests so the cross-origin links are deterministic. */
  readonly location?: { protocol: string; hostname: string }
}

/** A panel that names the route it read, so no figure on the page is anonymous. */
function Panel({
  title,
  route,
  children,
}: {
  title: string
  route: string
  children: React.ReactNode
}) {
  return (
    <section className="step" aria-label={title}>
      <h2>{title}</h2>
      <p className="metrics-source mono">{route}</p>
      {children}
    </section>
  )
}

/**
 * What a panel shows when it has no value.
 *
 * A blank and a zero are different sentences, and this is the component that keeps them
 * apart: it renders the REASON, toned by what kind of absence it is, and never a number.
 */
function Blank({ reading }: { reading: Reading<unknown> }) {
  const tone =
    reading.state === 'unreachable' || reading.state === 'failed'
      ? 'unverified'
      : reading.state === 'unauthorized'
        ? 'observed'
        : 'unknown'
  return (
    <p className="metrics-blank">
      <span data-tone={tone}>{reading.state}</span>{' '}
      <span className="metrics-blank__detail">{readingDetail(reading)}</span>
    </p>
  )
}

export function MetricsPage({
  fetcher = browserFetch,
  sessionId,
  initialAuctionId,
  location,
}: MetricsPageProps = {}) {
  const here = useMemo(
    () =>
      location ?? {
        protocol: window.location.protocol,
        hostname: window.location.hostname,
      },
    [location],
  )
  const liveSession = sessionId ?? ''

  const [auctionId, setAuctionId] = useState(
    () => initialAuctionId ?? remembered(REMEMBERED_AUCTION_KEY),
  )
  const [submitted, setSubmitted] = useState(
    () => initialAuctionId ?? remembered(REMEMBERED_AUCTION_KEY),
  )
  /**
   * Whether this browser still holds a remembered id — kept in state rather than re-read on
   * every render, because `remembered()` reads `sessionStorage` and nothing in React
   * re-renders when storage changes. The case that forces it into state is the one where
   * `onForget` changes nothing else: both fields already empty over a live key, where
   * `setAuctionId('')`/`setSubmitted('')` are no-ops, React skips the re-render, and a
   * button read straight off storage would keep offering a key that is already gone.
   */
  const [rememberedId, setRememberedId] = useState(() => remembered(REMEMBERED_AUCTION_KEY))

  const [trace, setTrace] = useState<Reading<AuctionTrace>>({ state: 'idle' })
  const [checks, setChecks] = useState<Reading<LiveCheckReading>>({ state: 'idle' })
  const [session, setSession] = useState<Reading<SessionReading>>({ state: 'idle' })
  const [profile, setProfile] = useState<Reading<ProfileReading>>({ state: 'idle' })

  useEffect(() => {
    let live = true
    void (async () => {
      setSession({ state: 'loading' })
      setProfile({ state: 'loading' })
      const [nextSession, nextProfile] = await Promise.all([
        readSession(liveSession, fetcher),
        readProfile(liveSession, fetcher),
      ])
      if (!live) return
      setSession(nextSession)
      setProfile(nextProfile)
    })()
    return () => {
      live = false
    }
  }, [fetcher, liveSession])

  useEffect(() => {
    const wanted = submitted.trim()
    if (wanted === '') {
      setTrace({ state: 'idle' })
      setChecks({ state: 'idle' })
      return
    }
    let live = true
    void (async () => {
      setTrace({ state: 'loading' })
      setChecks({ state: 'loading' })
      const [nextTrace, nextChecks] = await Promise.all([
        readAuctionTrace(wanted, fetcher),
        readLiveCheck(wanted, fetcher),
      ])
      if (!live) return
      setTrace(nextTrace)
      setChecks(nextChecks)
    })()
    return () => {
      live = false
    }
  }, [fetcher, submitted])

  const onSubmit = useCallback((event: React.FormEvent) => {
    event.preventDefault()
    setSubmitted(auctionId)
  }, [auctionId])

  /**
   * Clear the remembered auction id — from this screen AND from `sessionStorage`.
   *
   * THE DEAD END THIS CLOSES. `remember()` is written by `Journey` on every confirm, and for
   * a while the only `forget()` was behind `Journey`'s sign-out button. That button renders
   * only where `GET /buyer/auth/sign-in` answers `{"offered": true}`, and neither the compose
   * stack nor the devstack launcher offers it — both answer `{"offered": false}` because
   * neither names a mail transport. So on every deployment a demo is actually driven on, a
   * remembered id could be written and never cleared: it survived a reload, survived a "start
   * over", and reopened the previous run's full trace on the next visit to `#/metrics`.
   *
   * Clearing the field alone would not do it. `remembered()` is read in the initial state of
   * both `auctionId` and `submitted`, so the next mount would put the id straight back.
   *
   * WHICH IS ALSO WHY THE BUTTON CANNOT BE OFFERED ON THE TWO FIELDS ALONE. A demo driver who
   * empties the seeded field by hand and presses "Trace it" — the most natural reset gesture
   * on the page — leaves `auctionId` and `submitted` both empty while the key is still in
   * `sessionStorage`, and the next visit to `#/metrics` seeds the field from it all over
   * again. So the control asks `rememberedId` too, and is dark only when there is genuinely
   * nothing on screen and nothing in storage to clear.
   */
  const onForget = useCallback(() => {
    forget(REMEMBERED_AUCTION_KEY)
    setRememberedId('')
    setAuctionId('')
    setSubmitted('')
  }, [])

  return (
    <main className="journey" data-testid="metrics-page">
      <header className="masthead">
        <h1>Metrics &amp; tracing</h1>
        <p className="lede">
          A demo aid, not a product surface. Every figure below names the served route it was
          read from, and where a source exists that this origin cannot read, it is listed as
          such rather than shown as an empty chart.
        </p>
        <p className="metrics-warning" data-tone="unverified" role="note">
          Do not expose this page on a public deployment. It prints an auction&rsquo;s full
          recorded trace including every store&rsquo;s answer and price, every exclusion
          reason and the exchange&rsquo;s ranking components &mdash; from a route that takes
          no session header &mdash; and it links to the trust ledger and the ingest
          scheduler, which are unauthenticated on their own ports. What it does <em>not</em>{' '}
          do is read a buyer session out of this browser: there is none here to read, and the
          session panel below is refused rather than filled.
        </p>
      </header>

      <Panel
        title="One auction, as this service recorded it"
        route={`GET ${AUCTION_PATH_PREFIX}{auction_id}`}
      >
        <p>
          The richest real trace this origin can reach: who was solicited, what each store
          answered, which candidates were refused and for what stated reason, and the
          published ranking with its components. No route on any service lists auctions, so
          an id has to come from a journey you have run <em>against this stack</em>. The
          trust ledger linked below does name auction ids inside its events, but they are not
          a shortcut: this route answers 404 unless one of two sources still knows the
          auction &mdash; this buyer service&rsquo;s own in-process record of opening it, or
          a shortlist the exchange has not yet dropped. An id lifted from the ledger clears
          the first only if this very process opened that auction, and the second only while
          the exchange is still holding its shortlist &mdash; so one from an earlier run, or
          from a journey driven anywhere but here, 404s.
        </p>
        <form onSubmit={onSubmit} className="metrics-form">
          <label htmlFor="metrics-auction">Auction id</label>
          <input
            id="metrics-auction"
            name="auction"
            className="mono"
            value={auctionId}
            placeholder="auction id from a journey you have run"
            onChange={(event) => setAuctionId(event.target.value)}
          />
          <button type="submit">Trace it</button>
          <button
            type="button"
            onClick={onForget}
            disabled={auctionId === '' && submitted === '' && rememberedId === ''}
            title={
              'Clears the id this browser remembered from your last confirm, so a fresh ' +
              'demo does not open on the previous run’s trace.'
            }
          >
            Forget it
          </button>
        </form>

        {trace.state === 'ok' ? (
          <TraceView trace={trace.value} />
        ) : (
          <Blank reading={trace} />
        )}
      </Panel>

      <Panel
        title="What the platform checked against the stores' own pages"
        route={`GET ${LIVECHECK_PATH_PREFIX}{auction_id}`}
      >
        <p>
          What was checked, and what was declined. The service keeps those apart and so does
          this panel: &ldquo;no page was checked for this slot&rdquo; and &ldquo;the page was
          checked and agreed&rdquo; are different answers.
        </p>
        {checks.state === 'ok' ? (
          <LiveCheckView reading={checks.value} />
        ) : (
          <Blank reading={checks} />
        )}
      </Panel>

      <Panel title="The session, and the handle the stores are told" route={`GET ${SESSION_PATH} · GET ${PROFILE_PATH}`}>
        <p>
          R5&rsquo;s rotating pseudonym. The buyer service mints it in its own vault; this
          page never composes one, and both routes are refused without a live session.
        </p>
        {session.state === 'ok' ? (
          <dl className="metrics-rows">
            <dt>pseudonym</dt>
            <dd className="mono">{session.value.pseudonym}</dd>
            <dt>issued at</dt>
            <dd className="mono">{session.value.issued_at}</dd>
            <dt>expires at</dt>
            <dd className="mono">{session.value.expires_at}</dd>
          </dl>
        ) : (
          <Blank reading={session} />
        )}
        <h3>Coarsened profile</h3>
        {profile.state === 'ok' ? (
          Object.keys(profile.value.buckets).length === 0 ? (
            <p className="metrics-empty">
              The service returned a profile with no buckets. That is a real answer, not a
              missing one: this page has learned nothing about the shopper to coarsen.
            </p>
          ) : (
            <ul className="mono">
              {Object.entries(profile.value.buckets).map(([key, value]) => (
                <li key={key}>
                  {key}: {JSON.stringify(value)}
                </li>
              ))}
            </ul>
          )
        ) : (
          <Blank reading={profile} />
        )}
      </Panel>

      <section className="gaps" aria-label="What this page cannot source from this origin">
        <h2>What this page cannot source from this origin</h2>
        <p>
          These sources are real and they hold the most interesting telemetry in the stack —
          the hash-chained trust ledger above all. This page cannot <em>fetch</em> any of
          them, and the reason is deliberate rather than a bug:{' '}
          <code className="mono">deploy/buyer-web/nginx.conf</code> proxies exactly one
          prefix, <code className="mono">/buyer/</code>, and no service in this stack installs
          CORS. A cross-origin <code className="mono">fetch</code> is refused by the browser
          before it is sent.
        </p>
        <p>
          Browser <em>navigation</em> across origins is not restricted, so the ones that need
          no credential are linked below and open in a new tab. The rest name what they would
          need.
        </p>
        <ul className="metrics-unreachable">
          {UNREACHABLE_SOURCES.map((source) => (
            <li key={`${source.service}:${source.route}`}>
              <p className="metrics-unreachable__head">
                <span data-tone="unknown">{source.service}:{source.port}</span>{' '}
                <code className="mono">{source.route}</code>
              </p>
              <p>{source.label}</p>
              <p className="metrics-unreachable__why">
                <strong>Why not here:</strong> {source.why}
              </p>
              <p className="metrics-unreachable__why">
                <strong>What would change it:</strong> {source.remedy}
              </p>
              {source.openable ? (
                <p>
                  <a
                    href={directUrl(source, here)}
                    target="_blank"
                    rel="noreferrer"
                    className="mono"
                  >
                    open {directUrl(source, here)}
                  </a>
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      </section>

      <section className="gaps" aria-label="Metrics this stack does not record">
        <h2>Metrics this stack does not record at all</h2>
        <p>
          Named here so their absence from the panels above reads as a measurement rather than
          an oversight. Searched across every service, every compose file and{' '}
          <code className="mono">deploy/</code>:
        </p>
        <ul>
          <li>
            <strong>No <code className="mono">/metrics</code> route on any service.</strong> No
            Prometheus, no OpenTelemetry exporter, no counters endpoint.
          </li>
          <li>
            <strong>
              No <code className="mono">/readyz</code>, no <code className="mono">/livez</code>
              , and no <code className="mono">/healthz</code> on any ProxyShop service.
            </strong>{' '}
            The single <code className="mono">/healthz</code> in the repo belongs to{' '}
            <code className="mono">services/shopify-stub</code>, a fake Shopify on :8787.
          </li>
          <li>
            <strong>No request latency, request count or error rate is recorded anywhere.</strong>{' '}
            The only middleware any service installs is{' '}
            <code className="mono">RequestIdMiddleware</code>, which mints a correlation id and
            counts nothing.
          </li>
          <li>
            The compose healthchecks are not served routes — they shell out to{' '}
            <code className="mono">proxyshop_support.service_launch ready</code> inside the
            container, which a browser cannot reach.
          </li>
        </ul>
      </section>
    </main>
  )
}

/** One auction's recorded trace, rendered without computing anything the service did not. */
function TraceView({ trace }: { trace: AuctionTrace }) {
  return (
    <div className="metrics-trace">
      <dl className="metrics-rows">
        <dt>auction</dt>
        <dd className="mono">{trace.auction_id}</dd>
        <dt>recorded at</dt>
        <dd className="mono">
          {trace.recorded_at === '' ? (
            <span data-tone="unknown">the service recorded no timestamp</span>
          ) : (
            trace.recorded_at
          )}
        </dd>
        <dt>shortlist</dt>
        <dd>
          {trace.liveness === 'forgotten' ? (
            <span data-tone="unknown">
              forgotten — the exchange&rsquo;s 15-minute TTL has taken this auction away. The
              recorded diagnostics below survive; the live shortlist does not.
            </span>
          ) : (
            <span data-tone="confirmed">
              live, {trace.shortlist_slot_count} slot
              {trace.shortlist_slot_count === 1 ? '' : 's'}
            </span>
          )}
        </dd>
        <dt>solicited</dt>
        <dd className="mono">
          {trace.solicited.length === 0 ? (
            <span data-tone="unknown">the record names no solicited store</span>
          ) : (
            trace.solicited.join(', ')
          )}
        </dd>
      </dl>

      <h3>What each rostered store answered</h3>
      {trace.entries.length === 0 ? (
        <p className="metrics-empty">
          The record carries no entries. That is the exchange having reported none, not a
          failure to read them.
        </p>
      ) : (
        <ul className="metrics-entries">
          {trace.entries.map((entry) => {
            const family = reasonFamily(entry.fallback_reason)
            const detail = reasonDetail(entry.fallback_reason)
            return (
              <li key={entry.store_id}>
                <span className="mono">{entry.store_id}</span>{' '}
                {entry.fallback ? (
                  <>
                    <span data-tone="unverified">fallback</span>{' '}
                    <span className="mono">
                      {family === '' ? 'the exchange named no reason' : family}
                      {detail === '' ? '' : ` · ${detail}`}
                    </span>
                  </>
                ) : (
                  <span data-tone="confirmed">bid</span>
                )}{' '}
                {entry.unit_price === undefined ? (
                  <span data-tone="unknown">no unit price on this row</span>
                ) : (
                  <span className="mono">unit {entry.unit_price}</span>
                )}
              </li>
            )
          })}
        </ul>
      )}

      <h3>Refused by the eligibility filters</h3>
      {trace.excluded.length === 0 ? (
        <p className="metrics-empty">No candidate was excluded.</p>
      ) : (
        <ul className="metrics-entries">
          {trace.excluded.map((row) => (
            <li key={row.bid_ref}>
              <span className="mono">{row.store_id || row.bid_ref}</span>{' '}
              {row.exclusion_reasons.length === 0 ? (
                <span data-tone="unknown">excluded, and the record states no reason</span>
              ) : (
                row.exclusion_reasons.map((reason) => (
                  <span key={reason} data-tone="unverified">
                    {reason}
                  </span>
                ))
              )}
            </li>
          ))}
        </ul>
      )}

      <h3>Denied entry</h3>
      {trace.denied.length === 0 ? (
        <p className="metrics-empty">No store was denied entry.</p>
      ) : (
        <ul className="metrics-entries">
          {trace.denied.map((row) => (
            <li key={row.store_id}>
              <span className="mono">{row.store_id}</span>{' '}
              <span data-tone="unverified">{row.status}</span> {row.reason}
            </li>
          ))}
        </ul>
      )}

      <h3>The published ranking</h3>
      {trace.ranked.length === 0 ? (
        <p className="metrics-empty">The exchange published no ranking for this auction.</p>
      ) : (
        <ul className="metrics-entries">
          {trace.ranked.map((row) => (
            <li key={row.bid_ref}>
              <span className="mono">{row.store_id || row.bid_ref}</span>{' '}
              {row.rank_score === undefined ? (
                <span data-tone="unknown">no readable score on this row</span>
              ) : (
                <span className="mono">score {row.rank_score}</span>
              )}{' '}
              <span className="mono">
                {Object.keys(row.components).length === 0
                  ? 'the exchange published no components'
                  : Object.entries(row.components)
                      .map(([key, value]) => `${key}=${value}`)
                      .join(' ')}
              </span>
            </li>
          ))}
        </ul>
      )}

      <details className="trace">
        <summary>The answer itself, as the service sent it</summary>
        <pre className="mono">{JSON.stringify(trace.raw, null, 2)}</pre>
      </details>
    </div>
  )
}

/** The live-check ledger for one auction. */
function LiveCheckView({ reading }: { reading: LiveCheckReading }) {
  return (
    <div>
      {reading.records.length === 0 ? (
        <p className="metrics-empty">
          The platform completed no live check for this auction. That is the ledger being
          empty, not a reading this page failed to take.
        </p>
      ) : (
        <ul className="metrics-entries">
          {reading.records.map((record, index) => (
            <li key={`${record.checked_at}-${index}`}>
              <span
                data-tone={
                  record.outcome === 'agrees'
                    ? 'confirmed'
                    : record.outcome === 'contradicted'
                      ? 'unverified'
                      : 'unknown'
                }
              >
                {record.outcome}
              </span>{' '}
              <span className="mono">{record.surface}</span>{' '}
              <span className="mono">{record.checked_at}</span>
            </li>
          ))}
        </ul>
      )}
      <h3>Declined to check</h3>
      {reading.refused.length === 0 ? (
        <p className="metrics-empty">Nothing was declined.</p>
      ) : (
        <details className="trace">
          <summary>
            {reading.refused.length} refusal{reading.refused.length === 1 ? '' : 's'}
          </summary>
          <pre className="mono">{JSON.stringify(reading.refused, null, 2)}</pre>
        </details>
      )}
    </div>
  )
}

export default MetricsPage
