/**
 * Scaffold smoke test for the `merchant` vitest project.
 * Orchestrator-owned (T-000), frozen. Proves the jsdom environment and the shared
 * `app/test-setup.ts` (jest-dom matchers) are both wired up before any feature test needs them.
 */
import { describe, expect, it } from 'vitest'

describe('merchant workspace scaffold', () => {
  it('runs in a jsdom environment', () => {
    expect(typeof document).toBe('object')
    document.body.innerHTML = '<main id="root">merchant</main>'
    expect(document.getElementById('root')).toBeInTheDocument()
  })

  it('is pinned to React 18 (Polaris 13 peers react ^18)', async () => {
    const React = await import('react')
    expect(React.version.startsWith('18.')).toBe(true)
  })
})
