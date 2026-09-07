/**
 * The merchant dashboard's build. `npm --workspace @proxyshop/merchant run build:ui`.
 *
 * This file lives inside `app/` rather than beside `package.json` because `app/**` is what
 * this ticket owns; nothing about the build depends on where the config sits, since `root`
 * below is resolved against the config file itself.
 *
 * `base: '/dashboard/'` is the one setting that is not boilerplate and the one that breaks
 * silently if it is wrong. The merchant service serves this bundle from a mount at
 * `/dashboard` (`merchant_svc.dashboard.routes.DASHBOARD_MOUNT`), so the `<script src>` vite
 * writes into `index.html` has to be `/dashboard/assets/...`. With the default `/` the page
 * loads, requests `/assets/index-*.js`, and gets the merchant API's 404 — a blank dashboard
 * whose HTML is perfectly fine.
 */
import { fileURLToPath } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// `__dirname` does not exist in an ESM config, and a relative `root` would resolve against
// whatever directory the build was started from. This resolves against the config file.
const root = fileURLToPath(new URL('.', import.meta.url))

export default defineConfig({
  root,
  base: '/dashboard/',
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true },
})
