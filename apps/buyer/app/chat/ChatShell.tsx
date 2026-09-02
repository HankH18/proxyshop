/**
 * The buyer chat shell (T-070, SPEC R5).
 *
 * A shell, deliberately: it owns sign-in, the transcript and the composer, and it owns no
 * intent logic at all (T-070 non-goal — that is T-071's `apps/buyer/app/intent/**`). Sending
 * a line is delegated to `onSend`.
 *
 * Signed out it shows one field, an email address, and asks for a magic link. Signed in it
 * shows the transcript under a badge carrying the session's **pseudonym** — which is all the
 * identity the shell has, because `BuyerSession` has nowhere to put anything else. The
 * address the buyer typed stays in the input on their own device and is never rendered back
 * into the signed-in surface.
 */
import { useCallback, useId, useState, type FormEvent } from 'react'

import { shortPseudonym, type BuyerSession, type ChatMessage } from './session'

export interface ChatShellProps {
  /** The live session, or `null` when signed out. */
  readonly session: BuyerSession | null
  /** Everything said so far, oldest first. */
  readonly transcript: readonly ChatMessage[]
  /** Ask the service to email a magic link. */
  readonly onRequestLink: (email: string) => void | Promise<void>
  /** Hand a buyer utterance to whatever owns the conversation. */
  readonly onSend: (text: string) => void | Promise<void>
  /** Shown while a reply is in flight. */
  readonly busy?: boolean
}

export function ChatShell({
  session,
  transcript,
  onRequestLink,
  onSend,
  busy = false,
}: ChatShellProps) {
  const emailId = useId()
  const draftId = useId()
  const [email, setEmail] = useState('')
  const [draft, setDraft] = useState('')
  const [linkSent, setLinkSent] = useState(false)

  const requestLink = useCallback(
    async (event: FormEvent) => {
      event.preventDefault()
      if (!email.trim()) return
      await onRequestLink(email.trim())
      setLinkSent(true)
    },
    [email, onRequestLink],
  )

  const send = useCallback(
    async (event: FormEvent) => {
      event.preventDefault()
      const text = draft.trim()
      if (!text) return
      setDraft('')
      await onSend(text)
    },
    [draft, onSend],
  )

  if (session === null) {
    return (
      <section aria-label="Sign in to ProxyShop">
        <h1>Sign in</h1>
        <p>
          No password. We email you a single-use link, and stores only ever see a rotating
          pseudonym.
        </p>
        <form onSubmit={requestLink}>
          <label htmlFor={emailId}>Email address</label>
          <input
            id={emailId}
            name="email"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
          <button type="submit">Email me a link</button>
        </form>
        {linkSent ? <p role="status">Check your inbox for a sign-in link.</p> : null}
      </section>
    )
  }

  return (
    <section aria-label="Shopping chat">
      <header>
        <h1>Shopping chat</h1>
        <p data-testid="pseudonym-badge">
          Shopping as <strong>{shortPseudonym(session.pseudonym)}</strong>
          <span> — a fresh pseudonym for this session; stores never see who you are.</span>
        </p>
      </header>

      <ol aria-label="Transcript">
        {transcript.map((message) => (
          <li key={message.id} data-author={message.author}>
            {message.text}
          </li>
        ))}
      </ol>

      <form onSubmit={send}>
        <label htmlFor={draftId}>What are you shopping for?</label>
        <input
          id={draftId}
          name="message"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          disabled={busy}
        />
        <button type="submit" disabled={busy}>
          Send
        </button>
      </form>
    </section>
  )
}

export default ChatShell
