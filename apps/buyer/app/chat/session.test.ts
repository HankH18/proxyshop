/**
 * The buyer's login client and its pseudonym boundary (T-070, SPEC R5).
 *
 * The Python suite proves the service never *emits* identity. This one proves the client
 * never *accepts* it: the fetch helpers run every response body through
 * `assertPseudonymOnly`, and the tests below hand them a body with an `email` on it to show
 * the refusal is real rather than decorative.
 *
 * **What used to be in this file, and where it went.** It was `chat-shell.test.tsx`, and half
 * of it exercised `ChatShell` — a whole second shopping UI, with its own transcript and
 * composer, that nothing but this test ever imported. `index.html` loads `journey/main.tsx`,
 * which mounts `Journey`, so `ChatShell` was rendered for nobody. It is deleted rather than
 * mounted beside the journey: the two would have been two competing shopping surfaces, and
 * the journey is the one that ships. Its signed-out half — the only part doing work the
 * journey did not already do — moved to `journey/SignIn.tsx`, and the behaviours asserted
 * here about it (a submitted address reaches the service; an empty field asks for nothing;
 * a signed-in surface renders no trace of the buyer) are asserted against the served page in
 * `journey/journey.test.tsx`.
 *
 * What stayed is everything whose subject is `session.ts`, which did not go anywhere — it is
 * the client `Journey` now calls, so this file's subject is finally on a served path.
 */
import { describe, expect, it, vi } from 'vitest'

import {
  MAGIC_LINK_PATH,
  PROFILE_PATH,
  SESSION_HEADER,
  SESSION_PATH,
  assertPseudonymOnly,
  closeSession,
  IdentityLeakError,
  loadProfile,
  redeemMagicLink,
  requestMagicLink,
  type BuyerSession,
  type Fetcher,
} from './session'

const SESSION: BuyerSession = {
  sessionId: 'sess-e7-1',
  pseudonym: 'psn-7c1af204d9c3b1',
  issuedAt: '2026-01-01T00:00:00Z',
  expiresAt: '2026-01-01T12:00:00Z',
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('the pseudonym boundary on the client', () => {
  it('accepts a pseudonym-only payload and rejects one carrying identity', () => {
    const clean = { session_id: 'sess-1', pseudonym: 'psn-abc' }
    expect(assertPseudonymOnly(clean, 'session')).toBe(clean)

    expect(() =>
      assertPseudonymOnly(
        { session_id: 'sess-1', pseudonym: 'psn-abc', email: 'd@e.com' },
        'session',
      ),
    ).toThrow(IdentityLeakError)

    // Nested, and under a differently-spelled key: still refused.
    expect(() =>
      assertPseudonymOnly({ pseudonym: 'psn-abc', buckets: { postal_code: '97205' } }, 'profile'),
    ).toThrow(/postal_code/)
  })

  it('lets the five buckets the service really publishes straight through', () => {
    // The other half of the backstop, and the half a refusal that fires on honest traffic
    // would break. These are exactly `buyer_svc.profile.BUCKET_KEYS`, which is exactly the
    // field set `contracts.ProfileBuckets` allows — so if any of them tripped
    // `IDENTITY_KEY_RE`, signing in would refuse the profile every real buyer has, and R5's
    // coarsened profile could never reach a store at all.
    const served = {
      pseudonym: 'psn-abc',
      buckets: {
        budget_band: '100-250',
        category_affinity: ['apparel'],
        frequency_tier: 'new',
        region: 'US-OR',
        first_time: true,
      },
    }
    expect(assertPseudonymOnly(served, 'profile')).toBe(served)
  })

  it('redeems a link into a session that has nowhere to put an email', async () => {
    const fetcher: Fetcher = vi.fn(async () =>
      jsonResponse({
        session_id: 'sess-e7-1',
        pseudonym: 'psn-7c1af204d9c3b1',
        issued_at: '2026-01-01T00:00:00Z',
        expires_at: '2026-01-01T12:00:00Z',
      }),
    )
    const session = await redeemMagicLink('tok', fetcher)
    expect(session).toEqual(SESSION)
    expect(Object.keys(session)).toEqual(['sessionId', 'pseudonym', 'issuedAt', 'expiresAt'])
    expect(fetcher).toHaveBeenCalledWith(SESSION_PATH, expect.anything())
  })

  it('refuses a profile the service should never have sent', async () => {
    const leaky: Fetcher = async () =>
      jsonResponse({ pseudonym: 'psn-abc', buckets: {}, email: 'dana.reyes@example.com' })
    await expect(loadProfile(SESSION, leaky)).rejects.toThrow(IdentityLeakError)
  })

  it('reads a clean profile straight through', async () => {
    const calls: { path: string; init?: RequestInit }[] = []
    const clean: Fetcher = async (path, init) => {
      calls.push({ path, init })
      return jsonResponse({ pseudonym: 'psn-abc', buckets: { region: 'US-OR', first_time: false } })
    }
    await expect(loadProfile(SESSION, clean)).resolves.toEqual({
      pseudonym: 'psn-abc',
      buckets: { region: 'US-OR', first_time: false },
    })
    // The session id travels in the header the service reads, and in nothing else.
    expect(calls[0]?.path).toBe(PROFILE_PATH)
    expect(calls[0]?.init?.headers).toEqual({ [SESSION_HEADER]: SESSION.sessionId })
  })

  it('asks for a magic link and never sees a token come back', async () => {
    const fetcher: Fetcher = vi.fn(async () =>
      jsonResponse({ expires_at: '2026-01-01T00:15:00Z' }),
    )
    await expect(requestMagicLink('dana.reyes@example.com', fetcher)).resolves.toBe(
      '2026-01-01T00:15:00Z',
    )
    expect(fetcher).toHaveBeenCalledWith(MAGIC_LINK_PATH, expect.anything())
  })

  it('surfaces an HTTP failure rather than a half-built session', async () => {
    const fetcher: Fetcher = async () => jsonResponse({ detail: 'nope' }, 401)
    await expect(redeemMagicLink('bad', fetcher)).rejects.toThrow(/HTTP 401/)
  })

  it('signs out through the service rather than by forgetting a string', async () => {
    const calls: { path: string; init?: RequestInit }[] = []
    const fetcher: Fetcher = async (path, init) => {
      calls.push({ path, init })
      return new Response(null, { status: 204 })
    }

    await expect(closeSession(SESSION, fetcher)).resolves.toBeUndefined()

    // The route the vault retires a pseudonym on, by the method that means it. A client that
    // dropped its own copy instead would leave the handle live and R5's "rotating" untrue.
    expect(calls).toHaveLength(1)
    expect(calls[0]?.path).toBe(SESSION_PATH)
    expect(calls[0]?.init?.method).toBe('DELETE')
    expect(calls[0]?.init?.headers).toEqual({ [SESSION_HEADER]: SESSION.sessionId })
  })

  it('reports a refused sign-out rather than reporting success', async () => {
    const fetcher: Fetcher = async () => jsonResponse({ detail: 'no live buyer session' }, 401)
    await expect(closeSession(SESSION, fetcher)).rejects.toThrow(/HTTP 401/)
  })

  it('spells the four routes the service serves', () => {
    // Asserted rather than assumed: these strings are the contract with
    // `buyer_svc/auth/routes.py`, whose router carries the `/buyer` prefix.
    expect(MAGIC_LINK_PATH).toBe('/buyer/auth/magic-link')
    expect(SESSION_PATH).toBe('/buyer/auth/session')
    expect(PROFILE_PATH).toBe('/buyer/profile')
    expect(SESSION_HEADER).toBe('X-Buyer-Session')
  })

  // `shortPseudonym` was tested here and is deleted with `ChatShell`. It existed for "the
  // badge in the chat header" — its own docstring — and that header is gone; the journey
  // prints the whole handle, because a truncated one is not the value the stores are given.
})
