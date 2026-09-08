/**
 * The merchant dashboard: sign in, read the page, act on it.
 *
 * WHY THE TOKEN IS ASKED FOR RATHER THAN CONFIGURED
 * -------------------------------------------------
 * The administrative routes take a bearer (`MERCHANT_ADMIN_TOKEN`) and there is no session
 * layer in front of them, so the operator supplies it. It is held in memory and in
 * `sessionStorage` — which dies with the tab — and never in `localStorage`: a merchant admin
 * token that survives a closed browser on a shared machine is a worse trade than retyping it.
 *
 * WHAT THIS PAGE DOES NOT HAVE
 * ----------------------------
 * No exchange address, no trust address and no report bearer. Those live in the merchant
 * service's environment and the browser never sees them. The exchange resolves a merchant
 * FROM its report token, so that token IS this store's identity there; putting it in a page
 * would put it in a devtools tab.
 */
import { useCallback, useEffect, useState } from 'react'

import type { DashboardPage } from './api'
import {
  approveEnvelope,
  DashboardRefused,
  killStore,
  readDashboard,
  reviveStore,
  saveEnvelope,
  solicitBid,
  submitInterview,
} from './api'
import {
  BidsCard,
  EnvelopeCard,
  KillSwitch,
  LossesCard,
  OnboardingCard,
  TrustCard,
} from './Cards'
import './dashboard.css'

const TOKEN_KEY = 'proxyshop.merchant.admin-token'
const STORE_KEY = 'proxyshop.merchant.store-id'

function remembered(key: string, fallback: string): string {
  try {
    return window.sessionStorage.getItem(key) ?? fallback
  } catch {
    // A browser with storage disabled is a browser that retypes the token, not one that
    // cannot open the dashboard.
    return fallback
  }
}

function remember(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value)
  } catch {
    /* see `remembered` */
  }
}

function initialStore(): string {
  const fromQuery = new URLSearchParams(window.location.search).get('store')
  return (fromQuery ?? '').trim() || remembered(STORE_KEY, '')
}

function describe(problem: unknown): string {
  if (problem instanceof DashboardRefused) {
    const missing = problem.missing.length > 0 ? ` Set ${problem.missing.join(', ')}.` : ''
    return `${problem.error} (${problem.status}). ${problem.message}${missing}`.trim()
  }
  return problem instanceof Error ? problem.message : String(problem)
}

export function Dashboard(): JSX.Element {
  const [token, setToken] = useState(() => remembered(TOKEN_KEY, ''))
  const [storeId, setStoreId] = useState(initialStore)
  const [page, setPage] = useState<DashboardPage | null>(null)
  const [loadError, setLoadError] = useState('')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const [actionError, setActionError] = useState('')

  const load = useCallback(async () => {
    if (!token || !storeId) return
    setLoading(true)
    setLoadError('')
    try {
      setPage(await readDashboard(storeId, token))
    } catch (problem) {
      setPage(null)
      setLoadError(describe(problem))
    } finally {
      setLoading(false)
    }
  }, [storeId, token])

  useEffect(() => {
    void load()
  }, [load])

  const act = useCallback(
    async (name: string, run: () => Promise<unknown>) => {
      setBusy(name)
      setActionError('')
      try {
        await run()
        await load()
      } catch (problem) {
        setActionError(describe(problem))
      } finally {
        setBusy('')
      }
    },
    [load],
  )

  if (!token || !storeId) {
    return (
      <main className="shell">
        <Masthead storeId={storeId} page={null} />
        <section className="card">
          <h2>Sign in</h2>
          <p className="muted">
            The administrative routes take the merchant admin bearer
            (<code>MERCHANT_ADMIN_TOKEN</code>). It is kept for this tab only and is never
            written to durable storage.
          </p>
          <form
            className="stack"
            onSubmit={(event) => {
              event.preventDefault()
              const form = new FormData(event.currentTarget)
              const nextStore = String(form.get('store') ?? '').trim()
              const nextToken = String(form.get('token') ?? '').trim()
              remember(STORE_KEY, nextStore)
              remember(TOKEN_KEY, nextToken)
              setStoreId(nextStore)
              setToken(nextToken)
            }}
          >
            <label htmlFor="store">Store id</label>
            <input id="store" name="store" defaultValue={storeId} required />
            <label htmlFor="token">Admin token</label>
            <input id="token" name="token" type="password" defaultValue={token} required />
            <button type="submit">Open the dashboard</button>
          </form>
        </section>
      </main>
    )
  }

  return (
    <main className="shell">
      <Masthead storeId={storeId} page={page} />
      <div className="toolbar">
        <button type="button" onClick={() => void load()} disabled={loading}>
          {loading ? 'Reading…' : 'Reload'}
        </button>
        <button
          type="button"
          className="link"
          onClick={() => {
            remember(TOKEN_KEY, '')
            setToken('')
            setPage(null)
          }}
        >
          Sign out
        </button>
      </div>

      {loadError ? (
        <div className="notice notice--upstream_refused" role="alert">
          <p className="notice__heading">This page could not be read</p>
          <p className="notice__detail">{loadError}</p>
        </div>
      ) : null}

      {/*
        Onboarding is LAST in the Screens document, which is right for the store that document
        draws — one that has already joined — and wrong for one that has not: five panels that
        can only say "nothing recorded yet" would stand between a new merchant and the single
        card they can act on. So the order below is the designed one and this line is the
        repair, shown only while the store still has an interview or an approval outstanding.
      */}
      {page && (page.onboarding.step === 'interview' || page.onboarding.step === 'approval') ? (
        <p className="status-line muted">
          This store is not live yet — <a href="#onboarding">finish onboarding</a> at the foot of
          this page.
        </p>
      ) : null}

      {page ? (
        <div className="columns">
          {/*
            The order the Screens document specifies: trust, then where the store lost, then
            what its agent actually answered, then the terms it answers under, then the switch
            that stops it, then the door it came in by. It reads outward from the fact a
            merchant opens this page to check.
          */}
          <TrustCard panel={page.trust} events={page.trust_events} />
          <LossesCard panel={page.losses} />
          <BidsCard
            panel={page.bids}
            busy={busy === 'solicit'}
            error={busy === '' && actionError ? actionError : ''}
            onSolicit={(cluster, query) =>
              void act('solicit', () =>
                solicitBid(storeId, token, {
                  ...(cluster ? { cluster_id: cluster } : {}),
                  ...(query ? { query } : {}),
                }),
              )
            }
          />
          <EnvelopeCard
            panel={page.envelope}
            busy={busy === 'envelope'}
            error={busy === '' && actionError ? actionError : ''}
            onSave={(document) =>
              void act('envelope', () => saveEnvelope(storeId, token, document))
            }
          />
          <KillSwitch
            storeId={storeId}
            activation={page.envelope.activation}
            mayBid={page.envelope.may_bid}
            reason={page.envelope.reason}
            busy={busy === 'kill' || busy === 'revive'}
            error={busy === '' && actionError ? actionError : ''}
            onKill={() => void act('kill', () => killStore(storeId, token))}
            onRevive={() => void act('revive', () => reviveStore(storeId, token))}
          />
          <OnboardingCard
            panel={page.onboarding}
            busy={busy === 'interview' || busy === 'approve'}
            error={busy === '' && actionError ? actionError : ''}
            onSubmitInterview={(turns, completedAt) =>
              void act('interview', () => submitInterview(storeId, token, turns, completedAt))
            }
            onApprove={(artifact, header) =>
              void act('approve', () => approveEnvelope(storeId, token, artifact, header))
            }
          />
        </div>
      ) : null}
    </main>
  )
}

/**
 * The store header the Screens document puts above everything else.
 *
 * The stamp carries three facts rather than one — when the page was read, WHICH envelope
 * version it was read against, and whether that version is live. The last two were on the page
 * already but only inside two different cards, so a merchant scrolling the console could not
 * see at a glance that the terms they were reading were the killed ones. Nothing here is
 * derived: `version` and `activation` are the served payload's own fields.
 */
function Masthead({ storeId, page }: { storeId: string; page: DashboardPage | null }): JSX.Element {
  const generatedAt = page?.generated_at ?? ''
  // Gated on `current`, not on `activation`. The served payload reports `activation: "shadow"`
  // for a store that has NO envelope at all, so stamping a shadow pill on the masthead there
  // would announce a lifecycle state for a document that does not exist. No envelope, no
  // envelope stamp; the kill-switch card is where a store with nothing on file is explained.
  const envelope = page?.envelope.current
  return (
    <header className="masthead">
      <div>
        <p className="masthead__eyebrow">Proxyshop for merchants</p>
        <h1>{storeId || 'no store selected'}</h1>
      </div>
      <p className="masthead__stamp">
        {generatedAt ? <span>read at {new Date(generatedAt).toLocaleString()}</span> : null}
        {envelope === undefined ? null : (
          <>
            <span>· envelope v{envelope.version}</span>
            <span>·</span>
            <span className={`pill pill--${page?.envelope.activation ?? ''}`}>
              {page?.envelope.activation}
            </span>
          </>
        )}
      </p>
    </header>
  )
}
