/**
 * The browser entry point. `index.html` loads exactly this file.
 *
 * No `StrictMode`: `IntentConfirm` and `ShortlistView` each enforce "one per mount" with a
 * ref, and a development-only remount is not a distinction worth risking on the two gestures
 * that open an auction and mint a checkout.
 *
 * TWO THINGS THIS FILE DOES BEYOND MOUNTING, both for driving a live demo and both marked as
 * such on the page itself:
 *
 * 1. It renders `DemoShell` rather than `Journey` directly. The shell is a hash router —
 *    `#/` is the shopper journey, `#/metrics` is the metrics and tracing page — and its
 *    docstring records why the hash rather than the path (the devstack's static mount 404s
 *    every non-file path, so a path route would work on 8080 and break on 8100).
 *
 * 2. It mounts the demo nav bar into `document.body`, OUTSIDE the React root, and this
 *    placement is load-bearing rather than incidental. `journey.test.tsx` asserts four times
 *    that the rendered `<Journey>` container holds a known number of `a[href]` — zero before
 *    an accept, exactly one after — and the file says beside the last of them that the
 *    assertion is there so a future link fails a test. That is R3: the page must never build
 *    a navigable URL out of slot data. A nav bar rendered inside the tree would break all
 *    four assertions, and the only way to make them pass again would be to weaken a live
 *    security check. As a sibling of `#root` the bar is real navigation for a human and
 *    invisible to those assertions, which keep protecting exactly what they were written to
 *    protect.
 */
import { createRoot } from 'react-dom/client'

import { DemoShell } from '../demo/DemoShell'
import { mountDemoNav } from '../demo/nav'
import '../demo/demo-nav.css'
import '../metrics/metrics.css'
import './journey.css'

const host = document.getElementById('root')
if (host === null) {
  throw new Error('index.html has no #root to mount the buyer journey into')
}

mountDemoNav({ surface: 'buyer' })

createRoot(host).render(<DemoShell />)
