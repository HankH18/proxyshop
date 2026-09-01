/**
 * Scaffold smoke test for the `contracts` vitest project.
 * Orchestrator-owned (T-000), frozen. It exists so this project is executed rather than
 * silently empty: only the ROOT vitest invocation gets --passWithNoTests (D7), so an
 * empty project here would look identical to a passing one in the root run.
 */
import { describe, expect, it } from 'vitest'

describe('contracts workspace scaffold', () => {
  it('runs in the node environment configured for this project', () => {
    expect(typeof process.versions.node).toBe('string')
    expect(Number(process.versions.node.split('.')[0])).toBeGreaterThanOrEqual(22)
  })

  it('resolves the schema-validation dependencies this workspace declares', async () => {
    const { default: Ajv } = await import('ajv')
    expect(new Ajv().compile({ type: 'string' })('ok')).toBe(true)
  })
})
