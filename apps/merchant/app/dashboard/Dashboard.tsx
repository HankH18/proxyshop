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
import { DashboardRefused, killStore, readDashboard, saveEnvelope, solicitBid } from './api'
import { BidsCard, EnvelopeCard, KillSwitch, LossesCard, TrustCard } from './Cards'
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
        <Masthead storeId={storeId} generatedAt="" />
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
      <Masthead storeId={storeId} generatedAt={page?.generated_at ?? ''} />
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

      {page ? (
        <div className="columns">
          <KillSwitch
            storeId={storeId}
            activation={page.envelope.activation}
            mayBid={page.envelope.may_bid}
            reason={page.envelope.reason}
            busy={busy === 'kill'}
            error={busy === '' && actionError ? actionError : ''}
            onKill={() => void act('kill', () => killStore(storeId, token))}
          />
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
          <LossesCard panel={page.losses} />
          <TrustCard panel={page.trust} events={page.trust_events} />
          <EnvelopeCard
            panel={page.envelope}
            busy={busy === 'envelope'}
            error={busy === '' && actionError ? actionError : ''}
            onSave={(document) =>
              void act('envelope', () => saveEnvelope(storeId, token, document))
            }
          />
        </div>
      ) : null}
    </main>
  )
}

function Masthead({ storeId, generatedAt }: { storeId: string; generatedAt: string }): JSX.Element {
  return (
    <header className="masthead">
      <div>
        <p className="masthead__eyebrow">ProxyShop · merchant</p>
        <h1>{storeId || 'no store selected'}</h1>
      </div>
      <p className="masthead__stamp">
        {generatedAt ? `read at ${new Date(generatedAt).toLocaleString()}` : ''}
      </p>
    </header>
  )
}
