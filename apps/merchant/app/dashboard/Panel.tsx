/**
 * How a panel that could not be filled says so.
 *
 * This component is the third of the ticket's three rules made concrete: UNCONFIGURED MUST BE
 * LEGIBLE. Every card on this page routes its non-`ok` states through here, and the one thing
 * this file may never do is render nothing — a blank region reads as "no losses", "no trust
 * events", "the agent never bid", and each of those is a claim the deployment cannot make.
 *
 * The five states are distinct on purpose, because the fix is different for each:
 *
 * * `not_configured` — an operator has to set the named variables. Names are shown verbatim.
 * * `unauthorized`   — the credential this service holds is wrong at the far end.
 * * `unreachable`    — the address is right and nothing is listening.
 * * `upstream_refused` — something answered, and said no.
 * * `absent`         — the far end is fine and there is genuinely no record yet.
 */
import type { ReactNode } from 'react'

import type { PanelBase } from './api'

const HEADINGS: Record<string, string> = {
  not_configured: 'Not configured',
  unauthorized: 'Credential refused',
  unreachable: 'Service not reachable',
  upstream_refused: 'Refused upstream',
  absent: 'Nothing recorded yet',
}

export function PanelNotice({ panel }: { panel: PanelBase }): JSX.Element | null {
  if (panel.state === 'ok') return null
  const heading = HEADINGS[panel.state] ?? 'Unavailable'
  return (
    <div className={`notice notice--${panel.state}`} role="status">
      <p className="notice__heading">{heading}</p>
      {panel.missing !== undefined && panel.missing.length > 0 ? (
        <p className="notice__missing">
          Set{' '}
          {panel.missing.map((name, index) => (
            <span key={name}>
              {index > 0 ? ', ' : ''}
              <code>{name}</code>
            </span>
          ))}
          .
        </p>
      ) : null}
      {panel.detail ? <p className="notice__detail">{panel.detail}</p> : null}
    </div>
  )
}

/**
 * The shared container every card renders into.
 *
 * `id` is optional and purely additive: the Screens document puts Onboarding LAST, which is
 * right for a store that has already joined and wrong for one that has not, so the page links
 * a merchant straight down to the card they can act on rather than reordering itself. A card
 * with no `id` renders exactly as it did before.
 */
export function Card({
  id,
  title,
  requirement,
  children,
}: {
  id?: string
  title: string
  requirement: string
  children: ReactNode
}): JSX.Element {
  return (
    <section className="card" {...(id === undefined ? {} : { id })}>
      <header className="card__head">
        <h2>{title}</h2>
        <span className="card__req" title="the requirement this card serves">
          {requirement}
        </span>
      </header>
      {children}
    </section>
  )
}
