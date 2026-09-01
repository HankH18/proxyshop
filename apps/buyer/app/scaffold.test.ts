/**
 * Scaffold smoke test for the `buyer` vitest project.
 * Orchestrator-owned (T-000), frozen. The React version assertion is load-bearing: the
 * merchant/buyer React split (18 vs 19) is what lets `npm install` resolve at all, and a
 * hoist that collapsed it would break Polaris silently.
 */
import { describe, expect, it } from 'vitest'

describe('buyer workspace scaffold', () => {
  it('runs in a jsdom environment', () => {
    expect(typeof document).toBe('object')
    document.body.innerHTML = '<main id="root">buyer</main>'
    expect(document.getElementById('root')).toBeInTheDocument()
  })

  it('is pinned to React 19', async () => {
    const React = await import('react')
    expect(React.version.startsWith('19.')).toBe(true)
  })
})
