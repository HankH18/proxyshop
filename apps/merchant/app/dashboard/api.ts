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

/**
 * One interviewer turn, exactly as the service authored it and exactly as it goes back.
 *
 * THE FIELDS BESIDE `text` ARE THE POINT. `question`, `product_ref`, `options`,
 * `commitment_key` and `claim_type` are the service's vocabulary — D53's closed claim-type
 * set, the exchange's cluster ids, Shopify variant gids — and this page never authors one.
 * It renders `text`, collects a sentence, and hands the turn back untouched with a
 * `{role: 'merchant', text}` behind it. A browser that invented a `claim_type` would be
 * inventing a trust dimension.
 */
export interface InterviewTurn {
  role: string
  text: string
  question?: string
  product_ref?: string | null
  commitment_key?: string
  claim_type?: string
  options?: { cluster_id: string; label: string }[]
}

export interface InterviewQuestion {
  question: string
  prompt: string
  answer: string
  may_decline: boolean
  product_ref?: string | null
  commitment_key?: string
  claim_type?: string
  options?: { cluster_id: string; label: string }[]
}

export interface InterviewScript {
  turns: InterviewTurn[]
  questions: InterviewQuestion[]
  required_questions: string[]
  /** The DEPLOYMENT stated a readable intent-cluster taxonomy. */
  clusters_configured: boolean
  /** The pursue question has at least one option, from any of its three sources. */
  options_offered: boolean
  missing: string[]
}

/**
 * The written approval, minted by the service.
 *
 * `artifact` arrives carrying ONLY `envelope_hash`, and `needs` names the two fields a person
 * has to add. That asymmetry is deliberate on the service's side and must be preserved here:
 * a template pre-filled with a placeholder name would activate a merchant's envelope under
 * that placeholder if this page ever posted it back unchanged.
 */
export interface ApprovalToSign {
  store_id: string
  version: number
  envelope_hash: string
  artifact: { envelope_hash: string }
  needs: string[]
  header: string
  detail: string
}

export interface OnboardingPanel extends PanelBase {
  step: 'interview' | 'approval' | 'active' | 'killed'
  observation_surface: {
    registered: boolean
    shop_domain: string
    start_url: string
    detail: string
  }
  interview: InterviewScript
  approval: ApprovalToSign | null
}

export interface DashboardPage {
  store_id: string
  generated_at: string
  onboarding: OnboardingPanel
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

/**
 * Hand the answered interview to the service. R6's missing door, from the browser's side.
 *
 * `turns` is the SERVED script with the merchant's prose spliced in and nothing else added;
 * `completed_at` is the one field this page contributes, and it is the instant the merchant
 * finished. It is stamped here rather than by the service on purpose: every standing
 * commitment the envelope records is provenanced from it, so reading a server clock would
 * make the same transcript produce a different envelope on every run.
 *
 * Comes back as the stored envelope, always in `shadow`. Nothing a merchant can say in an
 * interview activates anything — that is what the approval is for.
 */
export async function submitInterview(
  storeId: string,
  token: string,
  turns: InterviewTurn[],
  completedAt: string,
  fetchImpl: typeof fetch = fetch,
): Promise<EnvelopeDocument> {
  const response = await fetchImpl(`/stores/${encodeURIComponent(storeId)}/envelope`, {
    method: 'PUT',
    headers: { ...authHeaders(token), 'content-type': 'application/json' },
    body: JSON.stringify({ completed_at: completedAt, turns }),
  })
  if (!response.ok) throw await refusalFrom(response)
  return (await response.json()) as EnvelopeDocument
}

/**
 * Record the merchant's written approval and activate the version it covers.
 *
 * The body describes NO terms. That is what keeps the approval valid: a body carrying terms
 * would mint the next version, and the digest the merchant signed covers the version number
 * as well as the walls — so the freshly minted one would be a document nobody approved. The
 * artifact travels in the header the service named, never in the body, because a body field
 * saying "approved" is exactly the say-so R6 refuses.
 */
export async function approveEnvelope(
  storeId: string,
  token: string,
  artifact: Record<string, unknown>,
  header: string,
  fetchImpl: typeof fetch = fetch,
): Promise<EnvelopeDocument> {
  const response = await fetchImpl(`/stores/${encodeURIComponent(storeId)}/envelope`, {
    method: 'PUT',
    headers: {
      ...authHeaders(token),
      'content-type': 'application/json',
      [header]: JSON.stringify(artifact),
    },
    body: JSON.stringify({ activation: 'active' }),
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
