/**
 * The demo nav bar.
 *
 * Two of these tests are guards rather than descriptions, and they are the reason this file
 * is worth its length: one pins that the bar mounts OUTSIDE the React root, because a bar
 * inside it would break four live `a[href]` assertions in `journey.test.tsx` that exist to
 * stop this page building a navigable URL out of slot data (R3); the other pins that the bar
 * spells no colour of its own, because the design pass this bar shipped into had just
 * finished unifying two surfaces onto one token block.
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it, afterEach } from 'vitest'

import {
  BUYER_DEFAULT_PORT,
  DEMO_NAV_ID,
  JOURNEY_HASH,
  METRICS_HASH,
  MERCHANT_DASHBOARD_PATH,
  MERCHANT_DEFAULT_PORT,
  currentKey,
  merchantConsoleUrl,
  mountDemoNav,
  navDestinations,
  renderDemoNav,
  routeForHash,
} from './nav'

const HERE = { protocol: 'http:', hostname: 'localhost' }

afterEach(() => {
  document.getElementById(DEMO_NAV_ID)?.remove()
  window.location.hash = ''
})

describe('which route a hash names', () => {
  it('reads the metrics hash, with or without slashes', () => {
    expect(routeForHash('#/metrics')).toBe('metrics')
    expect(routeForHash('#metrics')).toBe('metrics')
    expect(routeForHash('#//metrics')).toBe('metrics')
  })

  it('treats everything else as the journey, including nothing at all', () => {
    // A demo aid that answered a typo with a "not found" page would be worse than one that
    // answers it with the journey, so an unknown hash is not an error state.
    expect(routeForHash('')).toBe('journey')
    expect(routeForHash('#')).toBe('journey')
    expect(routeForHash('#/')).toBe('journey')
    expect(routeForHash('#/nonsense')).toBe('journey')
  })
})

describe('the merchant console link', () => {
  it('is built from the CURRENT hostname, never from a hard-coded localhost', () => {
    // The stack gets reached by IP and by hostname, and a hard-coded `localhost` link is the
    // point at which a demo driver on any other machine is sent to their own laptop.
    expect(merchantConsoleUrl({ protocol: 'https:', hostname: 'demo.example' })).toBe(
      `https://demo.example:${MERCHANT_DEFAULT_PORT}${MERCHANT_DASHBOARD_PATH}`,
    )
  })

  it('takes a port override, because the compose port is env-settable', () => {
    expect(merchantConsoleUrl(HERE, '9002')).toBe(
      `http://localhost:9002${MERCHANT_DASHBOARD_PATH}`,
    )
  })

  it('says in its hint that it is a different origin that may not be running', () => {
    const merchant = navDestinations('buyer', HERE).find((d) => d.key === 'merchant')
    expect(merchant?.external).toBe(true)
    // The page cannot probe it — different origin, no CORS anywhere in this stack — so the
    // honest thing is to say what it assumes rather than to imply it checked.
    expect(merchant?.hint).toContain('different service')
    expect(merchant?.hint).toContain('MERCHANT_SVC_PORT')
  })
})

describe('the entries on each surface', () => {
  it('links the shopper routes as bare hashes on the buyer origin', () => {
    const entries = navDestinations('buyer', HERE)
    expect(entries.map((d) => d.key)).toEqual(['journey', 'metrics', 'merchant'])
    expect(entries[0]?.href).toBe(JOURNEY_HASH)
    expect(entries[1]?.href).toBe(METRICS_HASH)
    expect(entries[0]?.external).toBe(false)
    expect(entries[1]?.external).toBe(false)
  })

  it('spells the shopper routes absolutely from the merchant console', () => {
    // A bare `#/metrics` on the merchant origin would append a hash to the merchant's own
    // URL and go nowhere, which is a link that looks like it works.
    const entries = navDestinations('merchant', HERE)
    expect(entries[1]?.href).toBe(`http://localhost:${BUYER_DEFAULT_PORT}/${METRICS_HASH}`)
    expect(entries[1]?.external).toBe(true)
    expect(entries.find((d) => d.key === 'merchant')?.external).toBe(false)
  })

  it('takes a buyer port override, and discloses the guess when it crosses origins', () => {
    // The buyer port is as much of an assumption as the merchant one — `BUYER_WEB_PORT` is
    // env-settable and the devstack uses 8100 — so it gets the same override and the same
    // disclosure rather than being hard-coded silently.
    const moved = navDestinations('merchant', HERE, undefined, '9100')
    expect(moved[0]?.href).toBe('http://localhost:9100/#/')
    expect(moved[0]?.hint).toContain('BUYER_WEB_PORT')
    expect(moved[0]?.hint).toContain('9100')
  })

  it('assumes no buyer port at all when it is already on the buyer origin', () => {
    // Nothing has been guessed, so nothing is disclosed: these are bare hashes.
    const here = navDestinations('buyer', HERE)
    expect(here[0]?.hint).not.toContain('BUYER_WEB_PORT')
    expect(here[1]?.href).toBe(METRICS_HASH)
  })

  it('marks the entry the reader is actually looking at', () => {
    expect(currentKey('buyer', '')).toBe('journey')
    expect(currentKey('buyer', '#/metrics')).toBe('metrics')
    // On the console neither shopper hash is current whatever the address bar says.
    expect(currentKey('merchant', '#/metrics')).toBe('merchant')
  })
})

describe('the bar it renders', () => {
  it('marks itself a demo aid on its face, not only in a comment', () => {
    const nav = renderDemoNav('buyer', { ...HERE, hash: '' })
    expect(nav.querySelector('.demo-nav__badge')?.textContent).toBe('demo aid')
    expect(nav.getAttribute('aria-label')).toBe('Demo navigation')
  })

  it('says where the reader is in a way that survives the stylesheet being off', () => {
    const nav = renderDemoNav('buyer', { ...HERE, hash: '#/metrics' })
    const current = nav.querySelector('[data-current="true"]')
    expect(current?.getAttribute('data-nav')).toBe('metrics')
    // A tint alone would say nothing to a screen reader.
    expect(current?.getAttribute('aria-current')).toBe('page')
  })

  it('mounts once however many times it is asked, so a hot reload cannot stack copies', () => {
    mountDemoNav({ surface: 'buyer' })
    mountDemoNav({ surface: 'buyer' })
    mountDemoNav({ surface: 'buyer' })
    expect(document.querySelectorAll(`#${DEMO_NAV_ID}`)).toHaveLength(1)
  })

  it('repaints the current entry when the hash changes, and stops when torn down', () => {
    const teardown = mountDemoNav({ surface: 'buyer' })
    expect(
      document.querySelector(`#${DEMO_NAV_ID} [data-current="true"]`)?.getAttribute('data-nav'),
    ).toBe('journey')

    window.location.hash = '#/metrics'
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    expect(
      document.querySelector(`#${DEMO_NAV_ID} [data-current="true"]`)?.getAttribute('data-nav'),
    ).toBe('metrics')

    teardown()
    expect(document.getElementById(DEMO_NAV_ID)).toBeNull()
  })

  it('mounts OUTSIDE the element it is given, never inside it', () => {
    // THE GUARD. `journey.test.tsx` counts `a[href]` over the rendered `<Journey>` container
    // at four places — zero before an accept, exactly one after — and says beside the last of
    // them that the assertion exists so a future link fails a test. That is R3. `main.tsx`
    // mounts this bar into `document.body`, a sibling of the React root, and this pins that
    // the bar goes where it is told rather than into whatever is rendering.
    const root = document.createElement('div')
    root.id = 'root'
    document.body.append(root)
    try {
      mountDemoNav({ surface: 'buyer' })
      const bar = document.getElementById(DEMO_NAV_ID)
      expect(bar).not.toBeNull()
      expect(root.contains(bar)).toBe(false)
      expect(root.querySelectorAll('a[href]')).toHaveLength(0)
    } finally {
      root.remove()
    }
  })
})

describe('the stylesheet', () => {
  // Read off disk rather than imported: the point is what the FILE says, and an `import` of
  // a stylesheet under vite yields a module, not its text. Resolved from this test's own
  // path — and deliberately NOT with `new URL('./x', import.meta.url)`, which vite rewrites
  // into an asset URL that is no longer a `file:` one.
  const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'demo-nav.css'), 'utf8')

  it('introduces no colour of its own', () => {
    // The design pass this bar shipped into unified two surfaces onto one token block, and a
    // third spelling of any colour would undo exactly that. Every value is a `var()` chain
    // ending in a CSS SYSTEM colour, which is a role the browser fills rather than a palette
    // entry — so there is no hex literal, and none may be added.
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    expect(css).not.toMatch(/\brgba?\(/)
    expect(css).not.toMatch(/\bhsla?\(/)
  })

  it('reaches for the buyer token first and the merchant token as the fallback', () => {
    // The two surfaces publish different vocabularies on purpose — `--ps-*` on the light
    // shopper journey, `--ds-*`/`--accent`/`--panel` on the dark merchant console — so each
    // chain has to name both or the bar is wrong on one of the two.
    expect(css).toContain('var(--ps-accent, var(--accent,')
    expect(css).toContain('var(--ps-ink, var(--ink,')
    expect(css).toContain('var(--ps-card, var(--panel,')
    expect(css).toContain('var(--ps-sans, var(--ds-sans,')
  })

  it('loads no font of its own', () => {
    // Both surfaces already self-host the same eight woff2 files, byte for byte. A bar that
    // declared a ninth `@font-face` would be a third way to load the same three faces.
    expect(css).not.toContain('@font-face')
    expect(css).not.toContain('fonts.googleapis.com')
  })
})
