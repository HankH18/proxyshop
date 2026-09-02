/**
 * T-070 — the buyer chat shell and its client-side pseudonym boundary (SPEC R5).
 *
 * The Python suite proves the service never *emits* identity. This one proves the client
 * never *renders* it and refuses to accept it if the service ever regressed: the fetch
 * helpers run every response body through `assertPseudonymOnly`, and the tests below hand
 * them a body with an `email` on it to show the refusal is real rather than decorative.
 *
 * Interaction is driven with `fireEvent` rather than `user-event`, which this workspace does
 * not carry. Every form here submits through its `<form onSubmit>`, so a submit event is the
 * honest gesture to fire.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ChatShell } from './ChatShell'
import {
  assertPseudonymOnly,
  IdentityLeakError,
  loadProfile,
  redeemMagicLink,
  requestMagicLink,
  shortPseudonym,
  type BuyerSession,
  type Fetcher,
} from './session'

const SESSION: BuyerSession = {
  sessionId: 'sess-e7-1',
  pseudonym: 'psn-7c1af204d9c3b1',
  issuedAt: '2026-01-01T00:00:00Z',
  expiresAt: '2026-01-01T12:00:00Z',
}

const IDENTITY = ['dana', 'reyes', 'example.com', '555-0100', 'alder way', '97205']

// `@testing-library/react` only registers its own cleanup when the test runner exposes
// `afterEach` globally, and this workspace's vitest projects do not set `globals: true`.
// Without this, one render's DOM survives into the next test and every `getByLabelText`
// after the first finds two matches.
afterEach(cleanup)

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function formFor(control: HTMLElement): HTMLFormElement {
  const form = control.closest('form')
  if (form === null) throw new Error('the control is not inside a form')
  return form
}

describe('the pseudonym boundary on the client', () => {
  it('accepts a pseudonym-only payload and rejects one carrying identity', () => {
    const clean = { session_id: 'sess-1', pseudonym: 'psn-abc' }
    expect(assertPseudonymOnly(clean, 'session')).toBe(clean)

    expect(() =>
      assertPseudonymOnly(
        { session_id: 'sess-1', pseudonym: 'psn-abc', email: 'd@e.com' },
        'session',
      ),
    ).toThrow(IdentityLeakError)

    // Nested, and under a differently-spelled key: still refused.
    expect(() =>
      assertPseudonymOnly({ pseudonym: 'psn-abc', buckets: { postal_code: '97205' } }, 'profile'),
    ).toThrow(/postal_code/)
  })

  it('redeems a link into a session that has nowhere to put an email', async () => {
    const fetcher: Fetcher = vi.fn(async () =>
      jsonResponse({
        session_id: 'sess-e7-1',
        pseudonym: 'psn-7c1af204d9c3b1',
        issued_at: '2026-01-01T00:00:00Z',
        expires_at: '2026-01-01T12:00:00Z',
      }),
    )
    const session = await redeemMagicLink('tok', fetcher)
    expect(session).toEqual(SESSION)
    expect(Object.keys(session)).toEqual(['sessionId', 'pseudonym', 'issuedAt', 'expiresAt'])
  })

  it('refuses a profile the service should never have sent', async () => {
    const leaky: Fetcher = async () =>
      jsonResponse({ pseudonym: 'psn-abc', buckets: {}, email: 'dana.reyes@example.com' })
    await expect(loadProfile(SESSION, leaky)).rejects.toThrow(IdentityLeakError)
  })

  it('reads a clean profile straight through', async () => {
    const clean: Fetcher = async () =>
      jsonResponse({ pseudonym: 'psn-abc', buckets: { region: 'US-OR', first_time: false } })
    await expect(loadProfile(SESSION, clean)).resolves.toEqual({
      pseudonym: 'psn-abc',
      buckets: { region: 'US-OR', first_time: false },
    })
  })

  it('asks for a magic link and never sees a token come back', async () => {
    const fetcher: Fetcher = vi.fn(async () =>
      jsonResponse({ expires_at: '2026-01-01T00:15:00Z' }),
    )
    await expect(requestMagicLink('dana.reyes@example.com', fetcher)).resolves.toBe(
      '2026-01-01T00:15:00Z',
    )
    expect(fetcher).toHaveBeenCalledWith('/buyer/auth/magic-link', expect.anything())
  })

  it('surfaces an HTTP failure rather than a half-built session', async () => {
    const fetcher: Fetcher = async () => jsonResponse({ detail: 'nope' }, 401)
    await expect(redeemMagicLink('bad', fetcher)).rejects.toThrow(/HTTP 401/)
  })

  it('shortens a pseudonym without inventing one', () => {
    expect(shortPseudonym('psn-7c1af204d9c3b1')).toBe('psn-7c1af2')
    expect(shortPseudonym('abcdef123456')).toBe('psn-abcdef')
  })
})

describe('the chat shell', () => {
  it('signed out, asks for an email and requests a link', async () => {
    const onRequestLink = vi.fn()
    render(
      <ChatShell session={null} transcript={[]} onRequestLink={onRequestLink} onSend={vi.fn()} />,
    )

    const field = screen.getByLabelText('Email address')
    fireEvent.change(field, { target: { value: 'dana.reyes@example.com' } })
    fireEvent.submit(formFor(field))

    await waitFor(() => expect(screen.getByRole('status')).toBeInTheDocument())
    expect(onRequestLink).toHaveBeenCalledWith('dana.reyes@example.com')
  })

  it('does not ask the service for a link when the field is empty', async () => {
    const onRequestLink = vi.fn()
    render(
      <ChatShell session={null} transcript={[]} onRequestLink={onRequestLink} onSend={vi.fn()} />,
    )
    fireEvent.submit(formFor(screen.getByLabelText('Email address')))
    await waitFor(() => expect(onRequestLink).not.toHaveBeenCalled())
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('signed in, renders the pseudonym and no trace of the buyer', () => {
    render(
      <ChatShell
        session={SESSION}
        transcript={[
          { id: 'm1', author: 'buyer', text: 'trail shoes under 150' },
          { id: 'm2', author: 'agent', text: 'What surface do you run on?' },
        ]}
        onRequestLink={vi.fn()}
        onSend={vi.fn()}
      />,
    )

    expect(screen.getByTestId('pseudonym-badge')).toHaveTextContent('psn-7c1af2')
    expect(screen.getByRole('list', { name: 'Transcript' })).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(2)

    const rendered = (document.body.textContent ?? '').toLowerCase()
    for (const secret of IDENTITY) {
      expect(rendered).not.toContain(secret)
    }
  })

  it('hands a typed line to onSend and clears the composer', async () => {
    const onSend = vi.fn()
    render(<ChatShell session={SESSION} transcript={[]} onRequestLink={vi.fn()} onSend={onSend} />)

    const composer = screen.getByLabelText('What are you shopping for?')
    fireEvent.change(composer, { target: { value: 'a quiet espresso grinder' } })
    fireEvent.submit(formFor(composer))

    await waitFor(() => expect(composer).toHaveValue(''))
    expect(onSend).toHaveBeenCalledWith('a quiet espresso grinder')
  })

  it('sends nothing when the composer holds only whitespace', async () => {
    const onSend = vi.fn()
    render(<ChatShell session={SESSION} transcript={[]} onRequestLink={vi.fn()} onSend={onSend} />)

    const composer = screen.getByLabelText('What are you shopping for?')
    fireEvent.change(composer, { target: { value: '   ' } })
    fireEvent.submit(formFor(composer))

    await waitFor(() => expect(onSend).not.toHaveBeenCalled())
    expect(composer).toHaveValue('   ')
  })

  it('disables the composer while a reply is in flight', () => {
    render(
      <ChatShell session={SESSION} transcript={[]} onRequestLink={vi.fn()} onSend={vi.fn()} busy />,
    )
    expect(screen.getByLabelText('What are you shopping for?')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  })
})
