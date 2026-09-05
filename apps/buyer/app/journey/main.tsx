/**
 * The browser entry point. `index.html` loads exactly this file.
 *
 * No `StrictMode`: `IntentConfirm` and `ShortlistView` each enforce "one per mount" with a
 * ref, and a development-only remount is not a distinction worth risking on the two gestures
 * that open an auction and mint a checkout.
 */
import { createRoot } from 'react-dom/client'

import { Journey } from './Journey'
import './journey.css'

const host = document.getElementById('root')
if (host === null) {
  throw new Error('index.html has no #root to mount the buyer journey into')
}

createRoot(host).render(<Journey />)
