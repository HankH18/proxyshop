/**
 * The browser entry point. `apps/merchant/index.html` loads exactly this file, and
 * `merchant_svc.dashboard.routes` serves the build it produces at `/dashboard`.
 *
 * No `StrictMode`, for the reason the buyer app gives: a development-only remount is not a
 * distinction worth risking on a page whose controls kill a store's agent.
 */
import { createRoot } from 'react-dom/client'

import { Dashboard } from './Dashboard'

const host = document.getElementById('root')
if (host === null) {
  throw new Error('index.html has no #root to mount the merchant dashboard into')
}

createRoot(host).render(<Dashboard />)
