/**
 * The demo nav bar — a driving aid for a live walkthrough, and nothing else.
 *
 * WHAT IT IS FOR. Both portals are real and neither has any navigation: the shopper journey
 * is one long page mounted once (`journey/main.tsx` renders exactly `<Journey />`), and the
 * merchant console is a second SPA on a different port. Driving a demo therefore means
 * typing URLs. This bar is the shortcut, and it is marked as a demo aid on its face so that
 * nobody mistakes it for product chrome.
 *
 * WHERE IT IS ACTUALLY MOUNTED, said plainly so the rest of this comment is not read as a
 * claim about the running system. The shopper journey mounts it today, from
 * `journey/main.tsx`. The merchant console does NOT yet: its copy of this file is in place
 * at `apps/merchant/app/demo/`, alongside a `mount.ts` that needs one import line added to
 * `apps/merchant/app/dashboard/main.tsx`, and that file belongs to another lane. Until that
 * line lands, the `'merchant'` surface below is exercised by tests and by nothing else.
 *
 * ---------------------------------------------------------------------------------------
 * FOUR CONSTRAINTS SHAPED THIS FILE, and each one is a measurement rather than a taste.
 *
 * 1. **It imports nothing — not even React.** The bar has to appear on BOTH portals, and the
 *    two apps cannot share a module through the build system:
 *      * `apps/buyer/tsconfig.json` and `apps/merchant/tsconfig.json` both set
 *        `"rootDir": "."` with `"include": ["app/**"]`, and neither declares `paths`; a file
 *        outside an app's own tree is not in that app's program at all, and importing one is
 *        TS6059 under the `composite: true` project references the root tsconfig sets up.
 *      * Neither image would contain it. Both Dockerfiles take the REPO ROOT as their build
 *        context, so the constraint is not the context but what each `vite build` stage
 *        COPYs into itself: the buyer's node stage takes `packages/contracts/` and
 *        `apps/buyer/`, the merchant's takes `apps/merchant/`, and both take the four
 *        `package.json` files and the two tsconfigs. A shared directory such as
 *        `apps/shared/` is in NEITHER of those COPY sets, so the bundle step would fail on a
 *        file that is not there. (The wider COPY lists further down each Dockerfile belong
 *        to the runtime Python stage and never see the vite build.)
 *      * The buyer is on React 19 and the merchant on React 18, deliberately and with a test
 *        pinning the split (`apps/buyer/app/scaffold.test.ts`); the buyer compiles JSX with
 *        `"jsx": "preserve"` and the merchant with `"jsx": "react-jsx"`.
 *    A plain-DOM module with no imports is identical under both compilers, needs neither
 *    React, and is therefore the one form of this component that can exist twice without
 *    the two copies being able to drift apart in behaviour. The copies are kept honest by
 *    `nav-parity.test.ts`, which fails if the bytes stop matching.
 *
 * 2. **It routes on the HASH, never on the path.** The buyer SPA is served two ways and only
 *    one of them has an SPA fallback. Under compose, `deploy/buyer-web/nginx.conf` answers
 *    any unknown path with `try_files $uri $uri/ /index.html`. Under `apps/buyer/devstack`,
 *    the page is served by `buyer_svc.composition.mount_ui`, which is
 *    `StaticFiles(..., html=True)` and 404s every path that is not a real file — the compose
 *    file says so in as many words. `/metrics` would therefore work on port 8080 and 404 on
 *    port 8100, which is the demo launcher. `#/metrics` works on both, and on any other
 *    static host this stack is ever put behind.
 *
 * 3. **It mounts OUTSIDE the journey component.** `journey.test.tsx` asserts four times that
 *    the rendered `<Journey>` container holds a known number of `a[href]` — zero before an
 *    accept, one after — and the file says at its last such assertion that this exists so a
 *    future link fails a test. That is R3: the page must never build a navigable URL out of
 *    slot data. A nav bar rendered inside `<Journey>` would break all four, and the fix
 *    would be to weaken a live security assertion. Mounting into `document.body`, as a
 *    sibling of the React root, leaves every one of them true and untouched.
 *
 * 4. **It re-spells no colour.** Every value below resolves through the token block the two
 *    stylesheets already publish — `--ps-*` on the shopper journey, `--ds-*`/`--accent` on
 *    the merchant console — with the buyer's spelling tried first and the merchant's as the
 *    fallback. Where a chain could end with neither stylesheet loaded it terminates in a CSS
 *    *system* colour (`Canvas`, `CanvasText`, `GrayText`, `LinkText`), which is a role and
 *    not a palette entry. No hex literal appears in this component's stylesheet.
 * ---------------------------------------------------------------------------------------
 */

/** Which portal this bar was mounted on. Decides which entry is marked current. */
export type DemoSurface = 'buyer' | 'merchant'

/** The in-page routes the shopper SPA answers on its hash. */
export type DemoRoute = 'journey' | 'metrics'

/** The element id the bar mounts under. Exported so a test asserts the spelling. */
export const DEMO_NAV_ID = 'proxyshop-demo-nav'

/** The hash the shopper journey lives at. Empty and `#/` both mean this one. */
export const JOURNEY_HASH = '#/'

/** The hash the metrics and tracing page lives at. */
export const METRICS_HASH = '#/metrics'

/**
 * The merchant console's port in `apps/merchant/compose.yaml` (`MERCHANT_SVC_PORT`), and the
 * mount `apps/merchant/svc/src/dashboard/routes.py` publishes as `DASHBOARD_MOUNT`.
 *
 * This is a DEFAULT and the bar says so rather than pretending to know: the port is
 * env-overridable in compose, and the shopper page cannot read the merchant's configuration
 * — the two are different origins and no service in this stack installs CORS, so there is
 * nothing to ask. A stack that moved the port gets a link to the old one; the bar therefore
 * prints the URL it derived in the link's `title`, so a wrong guess is visible rather than
 * mysterious. {@link mountDemoNav} takes an override for exactly that case.
 */
export const MERCHANT_DEFAULT_PORT = '8082'

/** The merchant dashboard's path prefix. `DASHBOARD_MOUNT` + a trailing slash. */
export const MERCHANT_DASHBOARD_PATH = '/dashboard/'

/**
 * The shopper page's port in `apps/buyer/compose.yaml` (`BUYER_WEB_PORT`), used only when
 * the bar is mounted on the MERCHANT console and has to name the buyer origin absolutely.
 *
 * The same kind of assumption as {@link MERCHANT_DEFAULT_PORT} and stated for the same
 * reason: it is env-overridable, the console cannot ask the buyer what port it is on, and a
 * stack that moved it gets a link to the old one. Note it is also wrong for the devstack,
 * which serves the shopper page on 8100 — but the devstack runs no merchant console at all,
 * so this constant is never read there.
 */
export const BUYER_DEFAULT_PORT = '8080'

/** One entry in the bar. */
export interface DemoDestination {
  /** Stable key, used for the current-entry test and as the DOM `data-nav` value. */
  readonly key: string
  /** What the entry reads as. */
  readonly label: string
  /** Where it goes. A hash for same-SPA routes, an absolute URL to cross the origin. */
  readonly href: string
  /** True when the entry leaves this origin — the bar marks these, because they can fail. */
  readonly external: boolean
  /** The `title`, which is where an entry says what it assumes. */
  readonly hint: string
}

/**
 * The merchant console's URL as seen from wherever this page is being served.
 *
 * Derived from the CURRENT hostname rather than hard-coded to localhost, so the bar keeps
 * working when the stack is reached by IP or by a hostname — which is the point at which a
 * hard-coded `localhost` link starts sending the operator to their own laptop.
 */
export function merchantConsoleUrl(
  location: { protocol: string; hostname: string },
  port: string = MERCHANT_DEFAULT_PORT,
): string {
  return `${location.protocol}//${location.hostname}:${port}${MERCHANT_DASHBOARD_PATH}`
}

/**
 * Which in-SPA route a hash names.
 *
 * Anything that is not the metrics hash is the journey, including the empty string, `#`, and
 * a hash this build does not know. A demo aid that showed a "not found" page for a typo
 * would be worse than one that shows the journey.
 */
export function routeForHash(hash: string): DemoRoute {
  return hash.replace(/^#/, '').replace(/^\/+/, '') === 'metrics' ? 'metrics' : 'journey'
}

/** The bar's entries, in order, for a given surface. */
export function navDestinations(
  surface: DemoSurface,
  location: { protocol: string; hostname: string },
  merchantPort?: string,
  buyerPort: string = BUYER_DEFAULT_PORT,
): readonly DemoDestination[] {
  const merchant = merchantConsoleUrl(location, merchantPort)
  // The shopper's two routes are hash links ON the buyer origin. From the merchant console
  // they are a different origin, so they have to be spelled absolutely or they would append
  // a hash to the merchant's own URL and go nowhere. See BUYER_DEFAULT_PORT for why that
  // port is an assumption rather than a lookup.
  const crossing = surface !== 'buyer'
  const buyerBase = crossing ? `${location.protocol}//${location.hostname}:${buyerPort}/` : ''
  // Only a link that CROSSES to the buyer origin has had a port guessed for it; from the
  // buyer's own page these are bare hashes and there is nothing assumed to disclose. Said
  // the same way the merchant hint says it, because it is the same kind of guess.
  const buyerPortNote = crossing
    ? ` Port ${buyerPort} is the compose default (BUYER_WEB_PORT); a stack that moved it, ` +
      `or the devstack on 8100, will need this link corrected.`
    : ''
  return [
    {
      key: 'journey',
      label: 'Shopper journey',
      href: `${buyerBase}${JOURNEY_HASH}`,
      external: crossing,
      hint:
        'The buyer portal: sign in, say what you need, see what the stores answered.' +
        buyerPortNote,
    },
    {
      key: 'metrics',
      label: 'Metrics & tracing',
      href: `${buyerBase}${METRICS_HASH}`,
      external: crossing,
      hint:
        'What this origin can actually measure, and the recorded trace of one auction.' +
        buyerPortNote,
    },
    {
      key: 'merchant',
      label: 'Merchant console',
      href: merchant,
      external: surface !== 'merchant',
      hint:
        `A different service on a different origin (${merchant}). It is not part of the ` +
        `buyer devstack, and this port is the compose default — a stack that moved ` +
        `MERCHANT_SVC_PORT will need this link corrected.`,
    },
  ]
}

/** Which entry is the one being looked at right now. */
export function currentKey(surface: DemoSurface, hash: string): string {
  if (surface === 'merchant') return 'merchant'
  return routeForHash(hash) === 'metrics' ? 'metrics' : 'journey'
}

/** Options for {@link mountDemoNav}. */
export interface DemoNavOptions {
  /** Which portal is mounting the bar. */
  readonly surface: DemoSurface
  /** Where to put it. Defaults to `document.body`. */
  readonly host?: HTMLElement
  /** Overrides the merchant port when a stack has moved it. */
  readonly merchantPort?: string
  /**
   * Overrides the buyer port. Read only when the bar is on the merchant console, which is
   * the only surface that has to name the buyer origin absolutely.
   */
  readonly buyerPort?: string
}

/**
 * Build the bar's element. Pure: it touches no document and registers no listener, so a test
 * can assert on the markup without a mount and without a teardown.
 */
export function renderDemoNav(
  surface: DemoSurface,
  location: { protocol: string; hostname: string; hash: string },
  merchantPort?: string,
  buyerPort?: string,
): HTMLElement {
  const nav = document.createElement('nav')
  nav.id = DEMO_NAV_ID
  nav.className = 'demo-nav'
  nav.setAttribute('aria-label', 'Demo navigation')
  nav.dataset.surface = surface

  const badge = document.createElement('span')
  badge.className = 'demo-nav__badge'
  // Said on the bar's face, not only in a comment: this is scaffolding for driving a
  // walkthrough and it is not part of either product surface.
  badge.textContent = 'demo aid'
  badge.title =
    'A navigation aid for driving a live demo. Not a product surface: neither portal ' +
    'ships this bar, and nothing below it is reachable from the products themselves.'
  nav.append(badge)

  const list = document.createElement('ul')
  list.className = 'demo-nav__list'
  const current = currentKey(surface, location.hash)

  for (const destination of navDestinations(surface, location, merchantPort, buyerPort)) {
    const item = document.createElement('li')
    const link = document.createElement('a')
    link.className = 'demo-nav__link'
    link.href = destination.href
    link.textContent = destination.label
    link.dataset.nav = destination.key
    link.title = destination.hint
    if (destination.key === current) {
      link.dataset.current = 'true'
      // `aria-current="page"` rather than a class alone: the marked entry is a claim about
      // where the reader is, and that is a thing a screen reader has to be able to say.
      link.setAttribute('aria-current', 'page')
    }
    if (destination.external) {
      link.dataset.external = 'true'
      const mark = document.createElement('span')
      mark.className = 'demo-nav__external'
      mark.setAttribute('aria-hidden', 'true')
      mark.textContent = '↗'
      link.append(' ', mark)
    }
    item.append(link)
    list.append(item)
  }

  nav.append(list)
  return nav
}

/**
 * Put the bar on the page and keep its current-entry marker in step with the hash.
 *
 * Returns a teardown. Idempotent: mounting twice replaces the first bar rather than stacking
 * two, because both apps hot-reload in development and a bar that accumulated copies of
 * itself on every save would be its own bug report.
 */
export function mountDemoNav(options: DemoNavOptions): () => void {
  const host = options.host ?? document.body
  const existing = host.ownerDocument.getElementById(DEMO_NAV_ID)
  if (existing !== null) existing.remove()

  const win = host.ownerDocument.defaultView
  const paint = (): HTMLElement => {
    const previous = host.ownerDocument.getElementById(DEMO_NAV_ID)
    const next = renderDemoNav(
      options.surface,
      {
        protocol: win?.location.protocol ?? 'http:',
        hostname: win?.location.hostname ?? 'localhost',
        hash: win?.location.hash ?? '',
      },
      options.merchantPort,
      options.buyerPort,
    )
    if (previous === null) host.prepend(next)
    else previous.replaceWith(next)
    return next
  }

  paint()
  const onHashChange = (): void => {
    paint()
  }
  win?.addEventListener('hashchange', onHashChange)

  return () => {
    win?.removeEventListener('hashchange', onHashChange)
    host.ownerDocument.getElementById(DEMO_NAV_ID)?.remove()
  }
}
