/**
 * The sign-in panel the shipped journey shows while there is no session (SPEC R5).
 *
 * It is `ChatShell`'s signed-out half, moved to the page that actually ships. `ChatShell`
 * owned a whole second shopping UI — a transcript and a composer duplicating steps 1 and 2 of
 * `Journey` — and the only thing that ever imported it was its own test, so it was deleted
 * rather than mounted beside the journey. This is the part of it that was doing real work.
 *
 * It performs no I/O: `Journey` owns the wire, `chat/session.ts` owns the requests. What this
 * component owns is the one property worth stating about a login form on a page whose whole
 * claim is pseudonymity — **the address goes up and never comes back down.** It is held in
 * this component's own state, it is sent to `onRequestLink`, and no branch below renders it
 * into the document. There is nowhere for it to come back from either: the service answers
 * `202 {expires_at}` and `BuyerSession` has no field an address could ride in.
 */
import { useCallback, useId, useState, type FormEvent } from 'react'

export interface SignInProps {
  /** Ask the service to mail a single-use link. */
  readonly onRequestLink: (email: string) => void | Promise<void>
  /**
   * When the last accepted link stops working, exactly as `POST /buyer/auth/magic-link`
   * spelled it. `undefined` until the service has accepted one — this page never predicts
   * an expiry, and a request that failed leaves this undefined so the banner above is the
   * only thing on screen about it.
   */
  readonly linkExpiresAt?: string
  /** A request is in flight. */
  readonly busy?: boolean
}

export function SignIn({ onRequestLink, linkExpiresAt, busy = false }: SignInProps) {
  const emailId = useId()
  const [email, setEmail] = useState('')

  const requestLink = useCallback(
    async (event: FormEvent) => {
      event.preventDefault()
      const address = email.trim()
      if (!address) return
      await onRequestLink(address)
    },
    [email, onRequestLink],
  )

  return (
    <>
      <p>
        No password. We email you a single-use link; opening it starts a session under a
        fresh handle minted by the buyer service&rsquo;s pseudonym vault, and that handle is
        the only thing a store is ever told about you.
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
          disabled={busy}
        />
        {/* `primary`, because on the signed-out page this is THE act — the design system's
            rule is that the accent marks exactly one act per screen, and there is no confirm
            button in the document until there is a session. It is a class rather than a
            colour decided in the stylesheet by position, so the button keeps its meaning if
            this panel is ever mounted somewhere else. */}
        <button type="submit" className="primary" disabled={busy || email.trim() === ''}>
          Email me a link
        </button>
      </form>
      {linkExpiresAt === undefined ? null : (
        <p role="status" data-testid="link-sent">
          A link is on its way to the address you typed. It works once and stops working at{' '}
          {linkExpiresAt} &mdash; the expiry the service stated, not a countdown this page
          invented. The token is never in that answer, so opening the mail is the only way to
          finish signing in.
        </p>
      )}
      {/*
       * WHAT THIS PARAGRAPH USED TO SAY, and why it could not stay. It opened "You do not
       * need to sign in to look around: say what you need and answer the clarifying questions
       * first if you like", and went on to explain that signing in was asked for at the
       * confirm and not before. Every clause of that was true of the page as it was built and
       * every clause of it is false now: the journey is behind this form, so there is nothing
       * to look around at first. Leaving it would have been a page instructing a visitor to
       * do something it no longer lets them do.
       *
       * The two facts in it that DID survive the change are kept, because both are still
       * load-bearing: the 503 a deployment with no mail transport answers with, which is the
       * difference between a broken demo and a configured refusal; and the warning about a
       * conversation not surviving, restated for where the risk actually is now.
       */}
      <p className="gloss">
        Signing in comes first here: the conversation it opens is one the exchange can act on,
        and it is minted against the handle the vault issues when you open your link. If this
        deployment has no mail transport configured, the service answers <code>503</code> and
        says so in the banner above rather than promising a mail nothing will send.
      </p>
      <p className="gloss">
        Open the link in this same browser, and keep the tab once you are in. The session
        lives in this page and is deliberately not written to your disk &mdash; the session id
        is a bearer credential for this origin &mdash; and the link works exactly once, so
        reloading after you have signed in ends the session and needs a fresh link rather than
        restoring the old one.
      </p>
    </>
  )
}

export default SignIn
