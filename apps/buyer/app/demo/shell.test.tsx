/**
 * The entry point's composition, tested as `main.tsx` actually wires it.
 *
 * The unit tests beside this one cover the bar and the pages separately. What they cannot
 * catch is the thing most likely to be wrong: that the two are put together correctly — the
 * bar OUTSIDE the React root, the hash choosing the page, and no link of the bar's landing
 * inside the tree that `journey.test.tsx` counts `a[href]` over.
 *
 * `main.tsx` itself is not imported here: it calls `createRoot` against a `#root` it expects
 * `index.html` to have provided, and importing it for its side effects would mount a second
 * React root into this test file's document. What is reproduced instead is exactly what it
 * does — `mountDemoNav({ surface: 'buyer' })`, then render `<DemoShell />` — so a change to
 * either half that broke the pairing fails here.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { DemoShell } from './DemoShell'
import { DEMO_NAV_ID, mountDemoNav } from './nav'

// `@testing-library/react` registers its own cleanup only when the runner exposes
// `afterEach` globally, and this workspace's vitest projects do not set `globals: true`.
afterEach(cleanup)
afterEach(() => {
  document.getElementById(DEMO_NAV_ID)?.remove()
  window.location.hash = ''
})

// WHAT THESE WAIT ON, AND WHY IT CHANGED FROM 'Email address'.
//
// Nothing in this file passes a fetcher, so `Journey` runs against jsdom's own `fetch` and
// reaches no service at all. These waits used to name the sign-in form, which rendered in
// that state because the form was unconditional. It is not any more: `Journey` asks
// `GET /buyer/auth/sign-in` whether this deployment can deliver a login link, and a
// deployment it cannot ask is one it cannot prove can — so it opens the journey with no gate,
// exactly as a deployment with no mail transport does.
//
// The composer is the same signal these assertions always wanted — "the shopper journey is
// the page that rendered" — and it is the element `journey.test.tsx` uses for it too. The
// subjects of these tests are the hash routing and where the nav bar's links live; none of
// them is about the gate, and none of them has been loosened.
const SHOPPER_PAGE = 'What are you shopping for?'

describe('the demo shell chooses a page from the hash', () => {
  it('renders the shopper journey by default', async () => {
    window.location.hash = ''
    render(<DemoShell />)
    expect(await screen.findByLabelText(SHOPPER_PAGE)).toBeInTheDocument()
    expect(screen.queryByTestId('metrics-page')).toBeNull()
  })

  it('renders the metrics page when the hash asks for it', async () => {
    window.location.hash = '#/metrics'
    render(<DemoShell />)
    expect(await screen.findByTestId('metrics-page')).toBeInTheDocument()
    expect(screen.queryByLabelText(SHOPPER_PAGE)).toBeNull()
  })

  it('switches pages when the hash changes, with no reload', async () => {
    window.location.hash = ''
    render(<DemoShell />)
    await screen.findByLabelText(SHOPPER_PAGE)

    window.location.hash = '#/metrics'
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await waitFor(() => expect(screen.getByTestId('metrics-page')).toBeInTheDocument())

    window.location.hash = '#/'
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await waitFor(() => expect(screen.getByLabelText(SHOPPER_PAGE)).toBeInTheDocument())
  })
})

describe('the bar and the root, put together the way main.tsx does', () => {
  it('keeps every nav link OUT of the tree the journey assertions count', async () => {
    // THE INTEGRATION GUARD, and the reason this file exists. `journey.test.tsx` asserts at
    // four places that the rendered container holds a known number of `a[href]` — zero before
    // an accept, exactly one after — and says beside the last of them that the assertion is
    // there so a future link fails a test. That is R3: the page must never build a navigable
    // URL out of slot data. The bar has four links; all four must be outside that container.
    //
    // The count was three until the learning demo was added as a third shopper route, at the
    // owner's own request. It is still asserted exactly rather than as "more than zero",
    // because the property being protected is that EVERY link the bar carries is outside the
    // journey's container — a count that could drift would let one slip inside unnoticed.
    const root = document.createElement('div')
    root.id = 'root'
    document.body.append(root)
    try {
      mountDemoNav({ surface: 'buyer' })
      const { container } = render(<DemoShell />, { container: root })
      await screen.findByLabelText(SHOPPER_PAGE)

      const bar = document.getElementById(DEMO_NAV_ID)
      expect(bar).not.toBeNull()
      // The bar really does carry links — this is not passing because it is empty.
      expect(bar!.querySelectorAll('a[href]').length).toBe(4)
      // And not one of them is inside the tree the journey's assertions see.
      expect(container.querySelectorAll('a[href]')).toHaveLength(0)
      expect(root.contains(bar)).toBe(false)
    } finally {
      root.remove()
    }
  })

  it('puts the bar above the root in the document, so nothing overlaps', () => {
    const root = document.createElement('div')
    root.id = 'root'
    document.body.append(root)
    try {
      mountDemoNav({ surface: 'buyer' })
      const bar = document.getElementById(DEMO_NAV_ID)!
      // `prepend`, i.e. in normal flow ahead of the app, rather than `position: fixed` with a
      // compensating top padding that this component would have to reach into two apps'
      // layouts to apply.
      expect(bar.compareDocumentPosition(root) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    } finally {
      root.remove()
    }
  })
})
