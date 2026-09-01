/**
 * Scaffold smoke test for the `pixel` vitest project.
 * Orchestrator-owned (T-000), frozen. `npx vitest run pixel` is a case-insensitive path
 * substring filter, so this is the one directory whose test paths may contain "pixel"
 * (D36 — check_verify_contracts.py enforces the converse for everywhere else).
 */
import { describe, expect, it } from 'vitest'

describe('pixel workspace scaffold', () => {
  it('is collected by the path filter its ticket verify uses', () => {
    expect(import.meta.url.toLowerCase()).toContain('pixel')
  })
})
