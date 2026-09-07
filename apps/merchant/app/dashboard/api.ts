/**
 * The dashboard's reads and writes, and the shape of what comes back.
 *
 * Every request is SAME-ORIGIN and relative. The bundle is served by the merchant service at
 * `/dashboard`, so `fetch('/stores/x/dashboard')` reaches the process that served the page —
 * no configured API base, and therefore no way to point the dashboard at a service other than
 * the one it came from. That matters more than it looks: the merchant admin bearer travels on
 * every one of these, and an origin the page could be talked into using is an origin that
 * bearer can be sent to.
 *
 * The exchange and the trust service are NOT reached from here. The merchant service reads
 * them, because the exchange's report bearer is that store's identity at the exchange
 * (`exchange.reports.routes` resolves the store FROM the token) and a browser is the wrong
 * place for it.
 */

/** The four refusal states a server-side panel can be in, plus `ok` and `absent`. */
export type PanelState =
  | 'ok'
  | 'absent'
  | 'not_configured'
  | 'unauthorized'
  | 'unreachable'
  | 'upstream_refused'

export interface PanelBase {
  state: PanelState
  detail: string
  missing?: string[]
}

export interface EnvelopeVersionRow {
  version: number
  activation: string
  approved_by: string | null
  approved_at: string | null
}

export interface EnvelopeDocument {
  store_id: string
  version: number
  floors: unknown[]
  max_discount_pct: number
  budget_cap: number
  pursue_clusters: string[]
  standing_commitments: unknown[]
  activation: string
}

export interface EnvelopePanel extends PanelBase {
  current?: EnvelopeDocument
  versions: EnvelopeVersionRow[]
  activation: string
  may_bid: boolean
  reason: string
}

export interface ClusterLossRow {
  cluster_id: string
  lost: number
  reasons: { fit: number; price: number; commitments: number; trust: number }
  unmet_criteria: string[]
}

export interface LossesPanel extends PanelBase {
  window?: { start: number; end: number }
  by_cluster?: ClusterLossRow[]
}

/**
 * One Beta posterior, exactly as `GET /snapshot` serves it.
 *
 * `alpha` and `beta` and NOTHING derived — measured against the running trust service, which
 * serves `{"alpha": 2.0, "beta": 2.0, "decayed_at": "..."}` per dimension and no mean at all.
 * An earlier draft of this file expected a `mean`, and against the real service the whole
 * per-dimension table rendered empty. The mean is arithmetic on these two numbers and is
 * computed where it is displayed, so there is one source for it and it is this one.
 */
export interface TrustDimension {
  alpha?: number
  beta?: number
  decayed_at?: string
}

export interface TrustSnapshot {
  store_id: string
  score?: number
  confidence?: number
  /** The trust service's own key. It is `dims`, not `dimensions`; measured, not assumed. */
  dims?: Record<string, TrustDimension>
  low_data?: boolean
  blacklisted?: boolean
  score_version?: string
  snapshot_version?: string
}

export interface TrustPanel extends PanelBase {
  snapshot?: TrustSnapshot | null
}

export interface TrustEventRow {
  seq?: number
  store_id?: string
  kind?: string
  payload?: unknown
  [key: string]: unknown
}

export interface TrustEventsPanel extends PanelBase {
  events?: TrustEventRow[]
  count?: number
  truncated?: boolean
}

export interface SolicitationRow {
  recorded_at: string
  origin: string
  store_id: string
  auction_id: string
  outcome: 'bid' | 'declined'
  activation: string
  may_bid: boolean
  claims: number
  /**
   * The merchant stopped this store and its agent bid anyway. Not a display flag — a state
   * the service measured, and the one row on this page a merchant must not scroll past.
   */
  contradiction: boolean
  decline_reason?: string
  offer?: {
    product_ref?: string | null
    variant_ref?: string | null
    unit_price?: number | null
    total_price?: number | null
    currency?: string | null
    discount?: Record<string, unknown> | null
    commitments?: string[]
    expires_at?: string | null
  }
  message?: string
  detail?: string
}

export interface BidsPanel extends PanelBase {
  entries?: SolicitationRow[]
}

export interface DashboardPage {
  store_id: string
  generated_at: string
  envelope: EnvelopePanel
  losses: LossesPanel
  trust: TrustPanel
  trust_events: TrustEventsPanel
  bids: BidsPanel
}

/** A refusal the page renders as a sentence — never as an empty result. */
export class DashboardRefused extends Error {
  readonly status: number
  readonly error: string
  readonly missing: string[]

  constructor(status: number, body: Record<string, unknown>) {
    const error = typeof body.error === 'string' ? body.error : `http_${status}`
    const detail = typeof body.detail === 'string' ? body.detail : ''
    super(detail || error)
    this.name = 'DashboardRefused'
    this.status = status
    this.error = error
    this.missing = Array.isArray(body.missing) ? body.missing.map(String) : []
  }
}

async function refusalFrom(response: Response): Promise<DashboardRefused> {
  let body: Record<string, unknown> = {}
  try {
    const parsed: unknown = await response.json()
    if (parsed !== null && typeof parsed === 'object') {
      body = parsed as Record<string, unknown>
    }
  } catch {
    // A refusal with an unreadable body is still a refusal, and losing the status code to a
    // parse error would turn "your token is wrong" into "something went wrong".
    body = {}
  }
  return new DashboardRefused(response.status, body)
}

function authHeaders(token: string): Record<string, string> {
  return { authorization: `Bearer ${token}` }
}

export async function readDashboard(
  storeId: string,
  token: string,
  fetchImpl: typeof fetch = fetch,
): Promise<DashboardPage> {
  const response = await fetchImpl(`/stores/${encodeURIComponent(storeId)}/dashboard`, {
    headers: authHeaders(token),
  })
  if (!response.ok) throw await refusalFrom(response)
  return (await response.json()) as DashboardPage
}

export async function killStore(
  storeId: string,
  token: string,
  fetchImpl: typeof fetch = fetch,
): Promise<{ store_id: string; activation: string }> {
  const response = await fetchImpl(`/stores/${encodeURIComponent(storeId)}/kill`, {
    method: 'POST',
    headers: authHeaders(token),
  })
  if (!response.ok) throw await refusalFrom(response)
  return (await response.json()) as { store_id: string; activation: string }
}

export async function saveEnvelope(
  storeId: string,
  token: string,
  document: unknown,
  fetchImpl: typeof fetch = fetch,
): Promise<EnvelopeDocument> {
  const response = await fetchImpl(`/stores/${encodeURIComponent(storeId)}/envelope`, {
    method: 'PUT',
    headers: { ...authHeaders(token), 'content-type': 'application/json' },
    body: JSON.stringify(document),
  })
  if (!response.ok) throw await refusalFrom(response)
  return (await response.json()) as EnvelopeDocument
}

export interface SolicitBody {
  cluster_id?: string
  query?: string
  hard_constraints?: unknown[]
}

export async function solicitBid(
  storeId: string,
  token: string,
  body: SolicitBody,
  fetchImpl: typeof fetch = fetch,
): Promise<SolicitationRow> {
  const response = await fetchImpl(`/stores/${encodeURIComponent(storeId)}/bids/solicit`, {
    method: 'POST',
    headers: { ...authHeaders(token), 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw await refusalFrom(response)
  return (await response.json()) as SolicitationRow
}
