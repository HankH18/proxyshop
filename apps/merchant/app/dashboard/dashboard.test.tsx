/**
 * The merchant dashboard, rendered, with a `fetch` that answers what the service answers.
 *
 * WHY THIS FILE MOUNTS BY HAND INSTEAD OF USING `@testing-library/react`
 * ---------------------------------------------------------------------
 * This workspace is pinned to **React 18** — `app/scaffold.test.ts` asserts it, because
 * Polaris 13 peers `react@^18` — while the repo root hoists React 19 for the buyer app. So
 * `react` and `react/jsx-runtime` resolve to `apps/merchant/node_modules` (18.3.1) and
 * `@testing-library/react`, which lives at the ROOT, resolves `react-dom/client` beside
 * itself (19.2.8). Rendering through it produces exactly one error and it names the cause:
 * *"A React Element from an older version of React was rendered."* Measured, not guessed.
 *
 * Mounting through `react-dom/client` imported from THIS file resolves both halves from the
 * same nested copy, so the component tree under test runs on one React. The helpers below are
 * the four gestures these tests need; a testing-library dependency for a five-file page is not
 * worth a second React in the process.
 *
 * The response bodies are the ones `apps/merchant/svc/tests/test_dashboard.py` drives out of
 * the real service, so both halves of the seam are graded against one shape.
 */
import type { ReactElement } from 'react'
import type { Root } from 'react-dom/client'
import { createRoot } from 'react-dom/client'
import { act } from 'react-dom/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DashboardPage, OnboardingPanel } from './api'
import { Dashboard } from './Dashboard'

const STORE = 'store-alpha'
const TOKEN = 'dashboard-test-token'

function page(overrides: Partial<DashboardPage> = {}): DashboardPage {
  return {
    store_id: STORE,
    generated_at: '2026-01-01T00:00:00+00:00',
    onboarding: onboarding(),
    envelope: {
      state: 'ok',
      detail: '',
      activation: 'active',
      may_bid: true,
      reason: "the store's current envelope is active",
      versions: [
        { version: 1, activation: 'shadow', approved_by: null, approved_at: null },
        { version: 2, activation: 'active', approved_by: 'owner', approved_at: null },
      ],
      current: {
        store_id: STORE,
        version: 2,
        floors: [],
        max_discount_pct: 20,
        budget_cap: 500,
        pursue_clusters: ['cluster-warm-layers'],
        standing_commitments: [],
        activation: 'active',
      },
    },
    losses: {
      state: 'ok',
      detail: '',
      window: { start: 0, end: 4 },
      by_cluster: [
        {
          cluster_id: 'cluster-warm-layers',
          lost: 3,
          reasons: { fit: 1, price: 1, commitments: 1, trust: 0 },
          unmet_criteria: ['material = merino wool'],
        },
      ],
    },
    trust: {
      state: 'ok',
      detail: '',
      snapshot: {
        store_id: STORE,
        score: 0.72,
        confidence: 0.31,
        // `dims`, alpha/beta, no mean: the shape a running trust service actually serves.
        dims: { price_honored: { alpha: 8, beta: 2, decayed_at: '2026-09-07T19:19:29.680Z' } },
        low_data: false,
        blacklisted: false,
      },
    },
    trust_events: { state: 'ok', detail: '', events: [], count: 0, truncated: false },
    bids: { state: 'ok', detail: '', entries: [] },
    ...overrides,
  }
}

/**
 * The `onboarding` panel, in the shape `merchant_svc.dashboard.routes._onboarding_panel`
 * serves it. The turns are the ones the real script emits — measured against the running
 * service, not invented — so the transcript these tests build is the transcript a browser
 * would build.
 */
function onboarding(overrides: Partial<OnboardingPanel> = {}): OnboardingPanel {
  return {
    state: 'ok',
    detail: 'the merchant answers these in plain English and the service reads the answers',
    step: 'interview',
    observation_surface: {
      registered: false,
      shop_domain: `${STORE}.myshopify.com`,
      start_url: `/install?shop=${STORE}.myshopify.com`,
      detail: 'this shop has no offline token on file',
    },
    interview: {
      turns: [
        { role: 'interviewer', text: 'This takes about five minutes.' },
        { role: 'interviewer', question: 'store', text: 'Which shop are we setting up?' },
        {
          role: 'interviewer',
          question: 'max_discount_pct',
          text: "What's the deepest discount you would ever authorise?",
        },
        {
          role: 'interviewer',
          question: 'pursue_clusters',
          options: [{ cluster_id: 'cluster-warm-layers', label: 'warm layers' }],
          text: 'Which kinds of shopper should your agent go after — warm layers?',
        },
        {
          role: 'interviewer',
          question: 'standing_commitment',
          commitment_key: 'free_returns',
          claim_type: 'return_policy',
          text: 'What do you always promise on returns?',
        },
      ],
      questions: [],
      required_questions: ['store', 'max_discount_pct', 'pursue_clusters', 'standing_commitment'],
      clusters_configured: true,
      options_offered: true,
      missing: [],
    },
    approval: null,
    ...overrides,
  }
}

const APPROVAL = {
  store_id: STORE,
  version: 1,
  envelope_hash: 'sha256:5a3d657b6ba4c56cdd1768b1c79f098c39bfecc8eb209e24bf96f5ea787b8f4a',
  artifact: {
    envelope_hash: 'sha256:5a3d657b6ba4c56cdd1768b1c79f098c39bfecc8eb209e24bf96f5ea787b8f4a',
  },
  needs: ['approver', 'approved_at'],
  header: 'X-Envelope-Approval',
  detail: 'the merchant signs these terms, not this document\'s lifecycle state',
}

/** A `fetch` double that records every call and answers from a table of route handlers. */
function stubFetch(handlers: Record<string, () => Response>) {
  const calls: { url: string; method: string; body?: string; headers?: HeadersInit }[] = []
  const impl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    calls.push({
      url,
      method,
      ...(typeof init?.body === 'string' ? { body: init.body } : {}),
      ...(init?.headers === undefined ? {} : { headers: init.headers }),
    })
    const key = `${method} ${url.split('?')[0]}`
    const handler = handlers[key]
    if (handler === undefined) return Promise.reject(new Error(`no stub for ${key}`))
    return Promise.resolve(handler())
  })
  return { impl, calls }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

let host: HTMLDivElement
let root: Root

async function mount(node: ReactElement): Promise<void> {
  await act(async () => {
    root.render(node)
    await Promise.resolve()
  })
  await settle()
}

/** Flush the promise chain a `useEffect` fetch leaves behind, inside `act`. */
async function settle(): Promise<void> {
  for (let pass = 0; pass < 6; pass += 1) {
    await act(async () => {
      await Promise.resolve()
    })
  }
}

function text(): string {
  return host.textContent ?? ''
}

function headings(): string[] {
  return [...host.querySelectorAll('th')].map((cell) => cell.textContent ?? '')
}

function button(match: RegExp): HTMLButtonElement {
  const found = [...host.querySelectorAll('button')].find((element) =>
    match.test(element.textContent ?? ''),
  )
  if (found === undefined) throw new Error(`no button matching ${String(match)}`)
  return found
}

/** Type into a CONTROLLED input the way a person does: React only sees the native setter. */
async function type(selector: string, value: string): Promise<void> {
  const field = host.querySelector<HTMLInputElement>(selector)
  if (field === null) throw new Error(`no input at ${selector}`)
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
  setter?.call(field, value)
  await act(async () => {
    field.dispatchEvent(new Event('input', { bubbles: true }))
    await Promise.resolve()
  })
}

async function click(element: HTMLElement): Promise<void> {
  await act(async () => {
    element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await Promise.resolve()
  })
  await settle()
}

function signedIn(): void {
  window.sessionStorage.setItem('proxyshop.merchant.admin-token', TOKEN)
  window.sessionStorage.setItem('proxyshop.merchant.store-id', STORE)
}

beforeEach(() => {
  ;(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT =
    true
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  window.sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('the merchant dashboard', () => {
  it('asks for the admin bearer before it asks the service for anything', async () => {
    const { impl } = stubFetch({})
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(host.querySelector('#token')).not.toBeNull()
    expect(impl).not.toHaveBeenCalled()
  })

  it('renders the loss report with reason categories and names no rival', async () => {
    signedIn()
    const { impl } = stubFetch({ [`GET /stores/${STORE}/dashboard`]: () => json(page()) })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(text()).toContain('cluster-warm-layers')
    expect(text()).toContain('material = merino wool')
    for (const reason of ['fit', 'price', 'commitments', 'trust']) {
      expect(headings()).toContain(reason)
    }
    // The page must never grow a column for who won or for how much. Asserting over the whole
    // rendered document is what survives a future refactor of these components.
    expect(text()).not.toMatch(/rival-/i)
    expect(text()).toContain('No rival is named')
  })

  it('reads only the merchant service, never the exchange or the trust service', async () => {
    signedIn()
    const { impl, calls } = stubFetch({
      [`GET /stores/${STORE}/dashboard`]: () => json(page()),
    })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(calls.length).toBeGreaterThan(0)
    // Same-origin and relative, every one. A dashboard that fetched the exchange directly
    // would need the store's report bearer in the browser, and at the exchange that bearer
    // IS the store's identity.
    for (const call of calls) {
      expect(call.url.startsWith('/stores/')).toBe(true)
    }
  })

  it('says what to configure instead of drawing an empty loss table', async () => {
    signedIn()
    const { impl } = stubFetch({
      [`GET /stores/${STORE}/dashboard`]: () =>
        json(
          page({
            losses: {
              state: 'not_configured',
              detail: 'no win/loss report can be fetched until the exchange is stated',
              missing: ['EXCHANGE_URL', 'MERCHANT_REPORT_TOKENS'],
            },
          }),
        ),
    })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(text()).toContain('Not configured')
    expect(text()).toContain('EXCHANGE_URL')
    expect(text()).toContain('MERCHANT_REPORT_TOKENS')
    expect(headings()).not.toContain('Intent cluster')
  })

  it('kills the store through the real route and then shows it stopped bidding', async () => {
    signedIn()
    let killed = false
    const { impl, calls } = stubFetch({
      [`GET /stores/${STORE}/dashboard`]: () =>
        json(
          killed
            ? page({
                envelope: {
                  ...page().envelope,
                  activation: 'killed',
                  may_bid: false,
                  reason: "the store's current envelope is 'killed'",
                },
              })
            : page(),
        ),
      [`POST /stores/${STORE}/kill`]: () => {
        killed = true
        return json({ store_id: STORE, activation: 'killed' })
      },
    })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(text()).toContain('is bidding')
    await type('#kill-confirm', STORE)
    await click(button(/Kill this store/))

    expect(text()).toContain('is not bidding')
    expect(calls.some((call) => call.method === 'POST' && call.url.endsWith('/kill'))).toBe(true)
  })

  it('shows a decline with the agent’s own reason, not as a store that chose not to bid', async () => {
    signedIn()
    const { impl } = stubFetch({
      [`GET /stores/${STORE}/dashboard`]: () =>
        json(
          page({
            bids: {
              state: 'ok',
              detail: '',
              entries: [
                {
                  recorded_at: '2026-01-01T00:00:00+00:00',
                  origin: 'dashboard',
                  store_id: STORE,
                  auction_id: 'dashboard-rehearsal-1',
                  outcome: 'declined',
                  activation: 'killed',
                  may_bid: false,
                  claims: 0,
                  contradiction: false,
                  decline_reason: 'store_killed',
                },
              ],
            },
          }),
        ),
    })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(text()).toContain('store_killed')
    expect(text()).toContain('The agent declined')
  })

  it('puts an alarm on a stopped store whose agent bid anyway', async () => {
    // The measured state of a two-process deployment: the merchant service holds the
    // authoritative envelope and nothing delivers it to a separately deployed agent, so a
    // killed store can still bid. The row must SHOUT — a merchant who reads `killed` in the
    // kill-switch card and scrolls past a normal-looking bid has been told the opposite of
    // what is happening.
    signedIn()
    const { impl } = stubFetch({
      [`GET /stores/${STORE}/dashboard`]: () =>
        json(
          page({
            envelope: { ...page().envelope, activation: 'killed', may_bid: false },
            bids: {
              state: 'ok',
              detail: '',
              entries: [
                {
                  recorded_at: '2026-01-01T00:00:00+00:00',
                  origin: 'dashboard',
                  store_id: STORE,
                  auction_id: 'dashboard-rehearsal-2',
                  outcome: 'bid',
                  activation: 'killed',
                  may_bid: false,
                  claims: 2,
                  contradiction: true,
                  detail: 'nothing delivers the envelope to a separately deployed agent',
                  offer: { product_ref: 'prod-cap', unit_price: 100 },
                },
              ],
            },
          }),
        ),
    })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(text()).toContain('This store is stopped and its agent bid anyway.')
    expect(host.querySelector('[role="alert"]')).not.toBeNull()
    expect(host.querySelector('.bid--contradiction')).not.toBeNull()
  })

  it('says "not activated" rather than "stopped" for a shadow store that bid', async () => {
    // Both are the same leak and the repair is different: a shadow store's owner never
    // touched the kill switch, and sending them to it is the wrong instruction.
    signedIn()
    const { impl } = stubFetch({
      [`GET /stores/${STORE}/dashboard`]: () =>
        json(
          page({
            bids: {
              state: 'ok',
              detail: '',
              entries: [
                {
                  recorded_at: '2026-01-01T00:00:00+00:00',
                  origin: 'dashboard',
                  store_id: STORE,
                  auction_id: 'dashboard-rehearsal-4',
                  outcome: 'bid',
                  activation: 'shadow',
                  may_bid: false,
                  claims: 2,
                  contradiction: true,
                  offer: { product_ref: 'prod-cap', unit_price: 100 },
                },
              ],
            },
          }),
        ),
    })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(text()).toContain('This store is not activated and its agent bid anyway.')
    expect(text()).not.toContain('This store is stopped')
  })

  it('renders every envelope version, including two states of one version', async () => {
    // A kill files a NEW state at the SAME version, so v1/v2/v2 is a real history. React
    // reported a duplicate-key collision on it against the live service before this row was
    // keyed on version AND activation.
    signedIn()
    const { impl } = stubFetch({
      [`GET /stores/${STORE}/dashboard`]: () =>
        json(
          page({
            envelope: {
              ...page().envelope,
              activation: 'killed',
              may_bid: false,
              versions: [
                { version: 1, activation: 'shadow', approved_by: null, approved_at: null },
                { version: 2, activation: 'shadow', approved_by: null, approved_at: null },
                { version: 2, activation: 'killed', approved_by: null, approved_at: null },
              ],
            },
          }),
        ),
    })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    const rows = [...host.querySelectorAll('tbody tr')].filter((row) =>
      /^v\d/.test(row.textContent ?? ''),
    )
    expect(rows).toHaveLength(3)
    expect(rows[2]?.className).toContain('is-head')
  })

  it('does not put that alarm on a bid the merchant authorised', async () => {
    signedIn()
    const { impl } = stubFetch({
      [`GET /stores/${STORE}/dashboard`]: () =>
        json(
          page({
            bids: {
              state: 'ok',
              detail: '',
              entries: [
                {
                  recorded_at: '2026-01-01T00:00:00+00:00',
                  origin: 'dashboard',
                  store_id: STORE,
                  auction_id: 'dashboard-rehearsal-3',
                  outcome: 'bid',
                  activation: 'active',
                  may_bid: true,
                  claims: 2,
                  contradiction: false,
                  offer: { product_ref: 'prod-cap', unit_price: 100 },
                },
              ],
            },
          }),
        ),
    })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(text()).not.toContain('its agent bid anyway')
    expect(host.querySelector('.bid--contradiction')).toBeNull()
    expect(text()).toContain('prod-cap')
  })

  it('reads the per-dimension breakdown off `dims` and shows the posterior mean', async () => {
    // The trust service serves `dims` with `alpha`/`beta` and no mean. An earlier draft read
    // `dimensions`/`mean` and rendered an empty table against the real service; this test is
    // what makes the wire shape a gate rather than an assumption.
    signedIn()
    const { impl } = stubFetch({ [`GET /stores/${STORE}/dashboard`]: () => json(page()) })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(text()).toContain('price_honored')
    expect(headings()).toContain('posterior mean')
    expect(text()).toContain('0.80') // 8 / (8 + 2), computed here, never expected on the wire
  })

  it('renders a refusal from the service as a sentence rather than a blank page', async () => {
    signedIn()
    const { impl } = stubFetch({
      [`GET /stores/${STORE}/dashboard`]: () =>
        json(
          {
            error: 'admin-api-not-configured',
            missing: ['MERCHANT_ADMIN_TOKEN'],
            detail: 'the dashboard refuses every caller until a token is configured',
          },
          503,
        ),
    })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(text()).toContain('This page could not be read')
    expect(text()).toContain('MERCHANT_ADMIN_TOKEN')
  })
  // ====================================================================================
  // R6 — the interview a merchant can reach, and the approval that activates it
  // ====================================================================================
  it('asks the service\u2019s questions and hands back the service\u2019s own turns', async () => {
    // The property this test exists for: the page authors NO machine-readable field. The
    // transcript it PUTs is the served turns, byte for byte, with prose spliced in.
    signedIn()
    const { impl, calls } = stubFetch({
      [`GET /stores/${STORE}/dashboard`]: () => json(page()),
      [`PUT /stores/${STORE}/envelope`]: () =>
        json({ store_id: STORE, version: 1, activation: 'shadow' }),
    })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(text()).toContain('Which shop are we setting up?')
    expect(text()).toContain('warm layers')
    expect(button(/still to answer/)).toBeDisabled()

    await type('#interview-1', 'We are store-alpha.myshopify.com.')
    await type('#interview-2', "Twenty percent, and that's the ceiling.")
    await type('#interview-3', 'Warm layers for sure.')
    await type('#interview-4', 'Free returns for 30 days.')
    await click(button(/Turn these answers into version 1/))

    const put = calls.find((call) => call.method === 'PUT')
    expect(put).toBeDefined()
    const body = JSON.parse(put?.body ?? '{}') as {
      completed_at: string
      turns: Record<string, unknown>[]
    }
    expect(typeof body.completed_at).toBe('string')
    // Every interviewer turn came back unchanged, carrying the vocabulary this page never
    // wrote: the cluster id, the commitment key and the claim type.
    const asked = body.turns.filter((turn) => turn.role === 'interviewer')
    expect(asked).toEqual(onboarding().interview.turns)
    const said = body.turns.filter((turn) => turn.role === 'merchant').map((turn) => turn.text)
    expect(said).toEqual([
      'We are store-alpha.myshopify.com.',
      "Twenty percent, and that's the ceiling.",
      'Warm layers for sure.',
      'Free returns for 30 days.',
    ])
  })

  it('sends the approval in the header the service named, over a body with no terms', async () => {
    // Two independent things are graded here and both are safety, not shape:
    //   * the artifact travels in `X-Envelope-Approval`, never in the body;
    //   * the body describes NO terms, so activation cannot mint a version the signed digest
    //     no longer covers.
    signedIn()
    const { impl, calls } = stubFetch({
      [`GET /stores/${STORE}/dashboard`]: () =>
        json(page({ onboarding: onboarding({ step: 'approval', approval: APPROVAL }) })),
      [`PUT /stores/${STORE}/envelope`]: () =>
        json({ store_id: STORE, version: 1, activation: 'active' }),
    })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(text()).toContain(APPROVAL.envelope_hash)
    expect(button(/I approve these terms/)).toBeDisabled()

    await type('#approver', 'Dana Okafor')
    await click(button(/I approve these terms/))

    const put = calls.find((call) => call.method === 'PUT')
    const headers = (put?.headers ?? {}) as Record<string, string>
    const artifact = JSON.parse(headers['X-Envelope-Approval'] ?? '{}') as Record<string, unknown>
    expect(artifact.envelope_hash).toBe(APPROVAL.envelope_hash)
    expect(artifact.approver).toBe('Dana Okafor')
    expect(typeof artifact.approved_at).toBe('string')

    const body = JSON.parse(put?.body ?? '{}') as Record<string, unknown>
    expect(body).toEqual({ activation: 'active' })
    for (const term of ['floors', 'max_discount_pct', 'budget_cap', 'pursue_clusters']) {
      expect(body[term]).toBeUndefined()
    }
    expect(headers['X-Envelope-Approval']).not.toContain('"approver":""')
  })

  it('will not send an approval nobody signed', async () => {
    // The button is disabled with no name typed. This is the WEAKER of the two refusals and
    // is asserted as such: the service refuses an approval with no approver too, and a form
    // validation that were the only guard would be a safety property held by CSS.
    signedIn()
    const { impl, calls } = stubFetch({
      [`GET /stores/${STORE}/dashboard`]: () =>
        json(page({ onboarding: onboarding({ step: 'approval', approval: APPROVAL }) })),
    })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    await click(button(/I approve these terms/))
    expect(calls.filter((call) => call.method === 'PUT')).toHaveLength(0)
  })

  it('names the shop whose outcome observation is not registered', async () => {
    // R6's other half. A store can hold an approved envelope and bid with nobody watching
    // what it ships, and then R12 grades its shipped-on-time record on no observations.
    signedIn()
    const { impl } = stubFetch({ [`GET /stores/${STORE}/dashboard`]: () => json(page()) })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(text()).toContain('not registered')
    expect(text()).toContain(`${STORE}.myshopify.com`)
    const link = [...host.querySelectorAll('a')].find((a) => /install/.test(a.getAttribute('href') ?? ''))
    expect(link?.getAttribute('href')).toBe(`/install?shop=${STORE}.myshopify.com`)
  })

  it('names the variable when the network states no intent clusters', async () => {
    signedIn()
    const { impl } = stubFetch({
      [`GET /stores/${STORE}/dashboard`]: () =>
        json(
          page({
            onboarding: onboarding({
              missing: ['NETWORK_INTENT_CLUSTERS'],
              detail: 'this deployment states no intent clusters',
            }),
          }),
        ),
    })
    vi.stubGlobal('fetch', impl)
    await mount(<Dashboard />)

    expect(text()).toContain('NETWORK_INTENT_CLUSTERS')
  })
})
