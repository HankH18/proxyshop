/**
 * Client half of buyer login (T-070, SPEC R5).
 *
 * Mirrors `buyer_svc/auth/routes.py`. Two things here are load-bearing rather than
 * decorative:
 *
 * 1. The types describe a session by its **pseudonym** and nothing else. There is no
 *    `email` field on `BuyerSession` or `BuyerProfile` to put an address into.
 * 2. `assertPseudonymOnly` is run over every response body before it is handed to the UI,
 *    so a server that regressed and started returning an identity field would fail loudly
 *    at the network boundary instead of quietly rendering the buyer's name into a page that
 *    is meant to be pseudonymous. The types alone cannot do this: a JSON body is `unknown`
 *    at runtime and an extra key sails straight through a structural type.
 */

/** Keys that must never appear in a buyer-facing payload. SPEC R5. */
export const IDENTITY_KEY_RE =
  /e[-_ ]?mail|phone|first[_ -]?name|last[_ -]?name|full[_ -]?name|given[_ -]?name|surname|address|street|postal|zip[_ -]?code|account[_ -]?id|customer[_ -]?id|buyer[_ -]?id|user[_ -]?id|identity|ip[_ -]?address|device[_ -]?id/i

/** A live buyer session. Carries the rotating pseudonym; never the human behind it. */
export interface BuyerSession {
  readonly sessionId: string
  readonly pseudonym: string
  readonly issuedAt: string
  readonly expiresAt: string
}

/** The coarsened profile a store may be shown. */
export interface BuyerProfile {
  readonly pseudonym: string
  readonly buckets: Readonly<Record<string, unknown>>
}

/** Thrown when a payload carries an identity-shaped key. */
export class IdentityLeakError extends Error {
  constructor(
    readonly key: string,
    readonly where: string,
  ) {
    super(`R5: identity-shaped key "${key}" arrived in the ${where} payload`)
    this.name = 'IdentityLeakError'
  }
}

function walkKeys(value: unknown, visit: (key: string) => void, depth = 0): void {
  if (depth > 8 || value === null || typeof value !== 'object') return
  if (Array.isArray(value)) {
    for (const item of value) walkKeys(item, visit, depth + 1)
    return
  }
  for (const [key, sub] of Object.entries(value)) {
    visit(key)
    walkKeys(sub, visit, depth + 1)
  }
}

/**
 * Return `payload` unchanged, or throw if any key inside it looks like identity.
 *
 * Rejects `{ session_id, pseudonym, email }`; accepts `{ session_id, pseudonym }`.
 */
export function assertPseudonymOnly<T>(payload: T, where: string): T {
  walkKeys(payload, (key) => {
    if (IDENTITY_KEY_RE.test(key)) throw new IdentityLeakError(key, where)
  })
  return payload
}

/** The `fetch` shape this module needs. Injectable so tests do not touch the network. */
export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>

/**
 * The three paths `buyer_svc/auth/routes.py` serves — five routes, because
 * `/buyer/auth/session` answers POST (redeem), GET (read) and DELETE (sign out). Exported so
 * a test asserts on the spelling rather than on a guess, the same reason `wire.ts` exports
 * `auctionPath`.
 */
export const MAGIC_LINK_PATH = '/buyer/auth/magic-link'
export const SESSION_PATH = '/buyer/auth/session'
export const PROFILE_PATH = '/buyer/profile'

/** The header the service reads a session id out of. Spelled once, here. */
export const SESSION_HEADER = 'X-Buyer-Session'

const JSON_HEADERS = { 'Content-Type': 'application/json' }

async function readJson(response: Response, where: string): Promise<unknown> {
  if (!response.ok) throw new Error(`${where} failed with HTTP ${response.status}`)
  return assertPseudonymOnly((await response.json()) as unknown, where)
}

/** POST /buyer/auth/magic-link — asks for a link. Resolves with the link's expiry. */
export async function requestMagicLink(email: string, fetcher: Fetcher): Promise<string> {
  const body = (await readJson(
    await fetcher(MAGIC_LINK_PATH, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ email }),
    }),
    'magic-link',
  )) as { expires_at: string }
  return body.expires_at
}

/** POST /buyer/auth/session — redeems a link into a session under a fresh pseudonym. */
export async function redeemMagicLink(token: string, fetcher: Fetcher): Promise<BuyerSession> {
  const body = (await readJson(
    await fetcher(SESSION_PATH, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ token }),
    }),
    'session',
  )) as { session_id: string; pseudonym: string; issued_at: string; expires_at: string }
  return {
    sessionId: body.session_id,
    pseudonym: body.pseudonym,
    issuedAt: body.issued_at,
    expiresAt: body.expires_at,
  }
}

/** GET /buyer/profile — the identity-free profile behind a live session. */
export async function loadProfile(
  session: BuyerSession,
  fetcher: Fetcher,
): Promise<BuyerProfile> {
  const body = (await readJson(
    await fetcher(PROFILE_PATH, { headers: { [SESSION_HEADER]: session.sessionId } }),
    'profile',
  )) as BuyerProfile
  return { pseudonym: body.pseudonym, buckets: body.buckets }
}

/**
 * DELETE /buyer/auth/session — signs out, and the pseudonym stays retired for good.
 *
 * The route answers `204` with no body, so there is nothing to run `assertPseudonymOnly`
 * over and nothing to return. It is called for its effect on the SERVICE rather than on this
 * page: dropping the session object in the browser would leave the pseudonym live in the
 * vault, and R5's "rotating" is a property of the vault retiring one handle and issuing a
 * different one, not of a browser forgetting a string.
 */
export async function closeSession(session: BuyerSession, fetcher: Fetcher): Promise<void> {
  const response = await fetcher(SESSION_PATH, {
    method: 'DELETE',
    headers: { [SESSION_HEADER]: session.sessionId },
  })
  if (!response.ok) throw new Error(`sign out failed with HTTP ${response.status}`)
}
