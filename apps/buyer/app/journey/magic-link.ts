/**
 * The half of magic-link sign-in that lives in the address bar (SPEC R5).
 *
 * Named `magic-link.ts` and not `signin.ts`, for `wire.ts`'s reason one directory up: this
 * machine's filesystem folds case, so `./SignIn` resolved to a lowercase `signin.ts` sitting
 * beside `SignIn.tsx` and the component came back `undefined` at render time. MEASURED — it
 * is what the first run of these tests failed on.
 *
 * `buyer_svc/auth/delivery.py` mails a link built as `{PROXYSHOP_BUYER_MAGIC_LINK_BASE_URL}`
 * with the token appended as a `token` query parameter — `_magic_link_url` does exactly that
 * and nothing else. So the buyer's browser arrives at this origin carrying the token in its
 * own URL, and the only thing standing between a mounted page and a session is reading it.
 * That is the whole of what this module does. The requests are `chat/session.ts`'s.
 *
 * Two rules, and both are about the token being a **single-use bearer credential** rather
 * than a piece of navigation state:
 *
 * 1. **It is read once and taken out of the address bar immediately.** A credential left in
 *    `location.search` is in the session history, in anything the buyer bookmarks or pastes,
 *    and — for any absolute link this page later renders — in a `Referer` header to a store's
 *    domain. `strippedUrl` is what the page navigates to instead, and it keeps every other
 *    parameter: this page is not the only thing that may put one there.
 * 2. **A blank token is no token.** `?token=` is what a mail client's link rewriter leaves
 *    behind often enough to matter, and `POST /buyer/auth/session` types the field
 *    `min_length=1`, so redeeming one would spend a round trip to be told 401 and would show
 *    the buyer a refusal for a link they never clicked.
 */

/** The query parameter `_magic_link_url` appends the token as. Spelled once. */
export const TOKEN_PARAM = 'token'

/**
 * The token in `search`, or `undefined` when there is none worth redeeming.
 *
 * Takes the FIRST when a URL carries several. A second `token` is not a second login — the
 * link this service mails carries exactly one — so the alternatives are to guess which is
 * meant or to refuse the buyer's own link over something they did not do.
 */
export function tokenFromSearch(search: string): string | undefined {
  const raw = new URLSearchParams(search).get(TOKEN_PARAM)
  if (raw === null) return undefined
  const token = raw.trim()
  return token === '' ? undefined : token
}

/**
 * `url` with every `token` parameter removed, path and remaining query intact.
 *
 * Returns a path-relative string because that is what `history.replaceState` wants and
 * because rebuilding an absolute URL here would be this module choosing an origin.
 */
export function strippedUrl(url: URL): string {
  const params = new URLSearchParams(url.search)
  params.delete(TOKEN_PARAM)
  const query = params.toString()
  return `${url.pathname}${query === '' ? '' : `?${query}`}${url.hash}`
}
