/**
 * The drift guard on the demo nav bar's two copies.
 *
 * The bar has to appear on both portals, and the two apps cannot share a module through the
 * build: both tsconfigs set `"rootDir": "."` with `"include": ["app/**"]` and declare no
 * `paths`, so a file outside an app's own tree is not in its program; and neither
 * `Dockerfile.web`'s vite-build stage COPYs a shared directory into itself — the buyer's
 * takes `packages/contracts/` and `apps/buyer/`, the merchant's takes `apps/merchant/` —
 * so `apps/shared/` would not exist when either bundle is built. The repo already made this
 * trade once, for the same reasons, with the eight woff2 files that are duplicated between
 * the two apps.
 *
 * The cost of a copy is drift, and this is the thing that stops it being silent. `nav.ts`
 * imports nothing at all — not React, not a stylesheet, not a helper — precisely so that the
 * two copies CAN be byte-identical rather than merely similar, which turns "did anyone update
 * the other one" into a failing test instead of a code review someone has to remember to do.
 *
 * If this test fails: copy the buyer's file over the merchant's. The buyer's is the canonical
 * one — it is the copy with the unit tests beside it.
 */
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

// Resolved from this file's own location, and deliberately NOT with
// `new URL('./x', import.meta.url)` — vite rewrites that pattern into an asset URL.
const BUYER_DEMO = dirname(fileURLToPath(import.meta.url))
const MERCHANT_DEMO = resolve(BUYER_DEMO, '../../../merchant/app/demo')

/** The files that must be identical, and the reason each one is on the list. */
const SHARED = [
  // The component itself. Behaviour drifting between the two surfaces is the whole risk.
  'nav.ts',
  // Its stylesheet. A token chain fixed on one surface and not the other is the same bug
  // wearing a different coat: the bar would be legible on the journey and not on the console.
  'demo-nav.css',
]

describe('the two copies of the demo nav', () => {
  for (const name of SHARED) {
    it(`keeps ${name} byte-identical between the buyer and the merchant`, () => {
      const buyer = readFileSync(join(BUYER_DEMO, name), 'utf8')
      const merchant = readFileSync(join(MERCHANT_DEMO, name), 'utf8')
      expect(merchant).toBe(buyer)
    })
  }

  it('keeps the component importing nothing, which is what lets it be copied at all', () => {
    const source = readFileSync(join(BUYER_DEMO, 'nav.ts'), 'utf8')
    // No `import` statement of any kind. An import of React would pin the file to one major
    // (the two apps are deliberately on 18 and 19); an import of a sibling would have to be
    // copied too; an import across app trees would not compile under either tsconfig.
    expect(source).not.toMatch(/^\s*import\s/m)
    expect(source).not.toMatch(/\brequire\(/)
  })

  it('is a .ts and not a .tsx, so both jsx settings compile it the same', () => {
    // The buyer compiles with `"jsx": "preserve"` and the merchant with `"jsx": "react-jsx"`.
    // The extension is the actual guarantee rather than a scan of the text: TypeScript will
    // not parse JSX in a `.ts` file at all, so a `.ts` is unaffected by that difference and
    // cannot quietly grow a component. Both copies are named the same way.
    expect(() => readFileSync(join(BUYER_DEMO, 'nav.ts'))).not.toThrow()
    expect(() => readFileSync(join(MERCHANT_DEMO, 'nav.ts'))).not.toThrow()
    expect(() => readFileSync(join(BUYER_DEMO, 'nav.tsx'))).toThrow()
    expect(() => readFileSync(join(MERCHANT_DEMO, 'nav.tsx'))).toThrow()
  })
})
