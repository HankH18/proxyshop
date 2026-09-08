/**
 * The hash router the buyer SPA did not have.
 *
 * This app had no routing of any kind: `main.tsx` rendered exactly `<Journey />`, and the
 * five "steps" are sections of one scrolling page rather than routes. Adding a second page
 * therefore needs a way to choose between them, and the choice of mechanism is constrained
 * rather than stylistic.
 *
 * WHY THE HASH AND NOT THE PATH. The buyer page is served two ways and only one of them can
 * answer a deep link:
 *
 *   * Under compose, `deploy/buyer-web/nginx.conf` ends with
 *     `location / { try_files $uri $uri/ /index.html; }`, so `/metrics` would load the app.
 *   * Under `apps/buyer/devstack/run.py` — which is the launcher the demo runbook uses, on
 *     port 8100 — the page is served by `buyer_svc.composition.mount_ui`, i.e.
 *     `StaticFiles(..., html=True)`. `apps/buyer/compose.yaml` states the consequence in its
 *     own words: that mount "HAS NO SPA FALLBACK. It answers `/` with index.html and 404s
 *     every other non-file path."
 *
 * So a path route works on 8080 and 404s on 8100, and 8100 is where the demo is driven. A
 * hash never reaches the server at all, so `#/metrics` works on both, on any static host,
 * and on a `file://` open. It also leaves the magic link's `?token=` query untouched, which
 * a path-rewriting router would have had to be careful about.
 *
 * WHY THE NAV IS NOT MOUNTED HERE. It is mounted from `main.tsx` into `document.body`,
 * outside the React root. `journey.test.tsx` asserts four times over the rendered
 * `<Journey>` container that it holds a known number of `a[href]` — zero before an accept,
 * one after — and says at the last of them that the assertion exists so that a future link
 * fails a test. That is R3: this page must never build a navigable URL out of slot data.
 * A nav bar inside the React tree would break all four and the only way to "fix" it would be
 * to weaken a live security assertion. Outside the root, every one of them stays true.
 */
import { useEffect, useState } from 'react'

import { Journey } from '../journey/Journey'
import { MetricsPage } from '../metrics/MetricsPage'
import { routeForHash, type DemoRoute } from './nav'

/** The current hash route, kept in step with the address bar. */
export function useHashRoute(): DemoRoute {
  const [route, setRoute] = useState<DemoRoute>(() =>
    routeForHash(typeof window === 'undefined' ? '' : window.location.hash),
  )
  useEffect(() => {
    const onHashChange = (): void => {
      setRoute(routeForHash(window.location.hash))
    }
    window.addEventListener('hashchange', onHashChange)
    // Read once more on mount: the hash can have changed between the initial state and the
    // listener being attached, and a demo driver who opens `#/metrics` directly must land
    // there rather than on the journey.
    onHashChange()
    return () => {
      window.removeEventListener('hashchange', onHashChange)
    }
  }, [])
  return route
}

/** Chooses the page. Everything else about either page is that page's own business. */
export function DemoShell() {
  const route = useHashRoute()
  return route === 'metrics' ? <MetricsPage /> : <Journey />
}

export default DemoShell
