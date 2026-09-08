/**
 * The five regions R9 names, one component each.
 *
 * Two things in here are the product rather than the presentation, and both are commented at
 * their site:
 *
 * * :func:`LossesCard` renders four reason categories and the buyer criteria this store did
 *   not meet, and NOTHING ELSE. There is no rival column to add, because there is no rival
 *   field in what arrives — the exchange projects amounts and identities away before the
 *   merchant service ever sees them (`exchange.reports.loss`). The card says so on the page,
 *   because a merchant who does not know the omission is deliberate reads it as missing data.
 * * :func:`KillSwitch` posts to the real `POST /stores/{id}/kill` and then invites the
 *   solicitation that shows the agent has stopped. An activation state nobody can observe is
 *   one nobody can trust, and this control was inert until an agent learned to answer
 *   `204 store_killed`. It also posts the real `POST /stores/{id}/revive`, which is the other
 *   half of the same switch: the kill is reversible, and the card that stops a store is the
 *   card that starts it. Reviving lands the envelope in `shadow` and never in `active`, so the
 *   restart finishes in :func:`OnboardingCard`, under the same written approval every other
 *   live envelope came through.
 */
import { useState } from 'react'

import type {
  BidsPanel,
  EnvelopePanel,
  InterviewTurn,
  LossesPanel,
  OnboardingPanel,
  SolicitationRow,
  TrustEventsPanel,
  TrustPanel,
} from './api'
import { Card, PanelNotice } from './Panel'

const REASONS = ['fit', 'price', 'commitments', 'trust'] as const

function money(value: number | null | undefined, currency?: string | null): string {
  if (typeof value !== 'number') return '—'
  return `${value.toFixed(2)}${currency ? ` ${currency}` : ''}`
}

function instant(raw: string | undefined): string {
  if (!raw) return '—'
  const parsed = new Date(raw)
  return Number.isNaN(parsed.getTime()) ? raw : parsed.toLocaleString()
}

// ======================================================================================
// Onboarding — R6's interview, and the written approval that activates what it produced
// ======================================================================================
/**
 * The interview a merchant can actually reach, and the approval they actually sign.
 *
 * WHAT WAS TRUE BEFORE THIS CARD
 * ------------------------------
 * Nothing on this page could create an envelope. The interview ran only under
 * `python -m merchant_svc.onboarding <transcript.json>` on an operator's laptop, and the
 * approval digest was published by no route at all — so the `X-Envelope-Approval` header
 * could only be filled by re-implementing the service's canonical SHA-256 in the browser.
 * A merchant could not join the network through the product.
 *
 * TWO RULES THIS COMPONENT MAY NEVER BREAK
 * ----------------------------------------
 * 1. **It authors no machine-readable field.** The served turn goes back byte for byte with a
 *    merchant turn behind it. Every `cluster_id`, `product_ref`, `commitment_key` and
 *    `claim_type` rides inside it. The only thing this file adds to the transcript is
 *    `completed_at`, and the only thing the merchant adds is prose.
 * 2. **It cannot activate anything.** The approve button sends a body describing no terms and
 *    an artifact the SERVICE minted the hash for. If the merchant has not typed their name,
 *    the button is disabled here and the service refuses it there — two independent refusals
 *    for one safety property, because a form validation is not a gate.
 */
export function OnboardingCard({
  panel,
  onSubmitInterview,
  onApprove,
  busy,
  error,
}: {
  panel: OnboardingPanel
  onSubmitInterview: (turns: InterviewTurn[], completedAt: string) => void
  onApprove: (artifact: Record<string, unknown>, header: string) => void
  busy: boolean
  error: string
}): JSX.Element {
  const [said, setSaid] = useState<Record<number, string>>({})
  const [approver, setApprover] = useState('')
  const surface = panel.observation_surface
  const asked = panel.interview.turns
    .map((turn, index) => ({ turn, index }))
    .filter(({ turn }) => typeof turn.question === 'string' && turn.question.length > 0)
  const unanswered = asked.filter(({ index }) => (said[index] ?? '').trim() === '')

  return (
    <Card id="onboarding" title="Onboarding" requirement="R6 · R7">
      <PanelNotice panel={panel} />
      {/*
        `PanelNotice` renders nothing in the `ok` state, and this panel IS ok while one of its
        questions has nothing to offer: the interview is served and answerable without a
        cluster taxonomy. So the missing variable is named here instead of being swallowed —
        an empty multi-select that said nothing would read as "this store pursues nothing",
        which is a term the merchant never set.
      */}
      {panel.state === 'ok' && panel.missing !== undefined && panel.missing.length > 0 ? (
        <div className="notice notice--not_configured" role="status">
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
          <p className="notice__detail">{panel.detail}</p>
        </div>
      ) : null}
      <p className="status-line">
        This store is at step <span className={`pill pill--${panel.step}`}>{panel.step}</span>.
      </p>

      <p className={surface.registered ? 'muted' : 'error'}>
        <strong>Outcome observation:</strong>{' '}
        {surface.registered ? 'registered' : 'not registered'} for{' '}
        <code>{surface.shop_domain}</code>.{' '}
        {surface.registered ? null : <a href={surface.start_url}>Install the app</a>}
      </p>
      <p className="muted">{surface.detail}</p>

      {panel.step === 'interview' || panel.step === 'approval' ? (
        <form
          className="stack"
          onSubmit={(event) => {
            event.preventDefault()
            const turns: InterviewTurn[] = []
            panel.interview.turns.forEach((turn, index) => {
              // The served turn, untouched. Spreading it rather than rebuilding it is what
              // guarantees this page never drops a field it does not itself understand.
              turns.push(turn)
              if (typeof turn.question === 'string' && turn.question.length > 0) {
                turns.push({ role: 'merchant', text: said[index] ?? '' })
              }
            })
            onSubmitInterview(turns, new Date().toISOString())
          }}
        >
          <p className="muted">
            Answer in your own words. A sentence like <em>“twenty percent, and that’s the
            ceiling”</em> sets the cap; <em>“no, nothing special there”</em> sets no floor at
            all. An answer this service cannot read is refused rather than filed as blank — a
            wall that quietly failed to parse is a wall you think you have and do not.
          </p>
          {asked.map(({ turn, index }) => (
            <div className="stack" key={`${String(turn.question)}-${index}`}>
              <label htmlFor={`interview-${index}`}>{turn.text}</label>
              {turn.options !== undefined && turn.options.length > 0 ? (
                <p className="muted">
                  On offer:{' '}
                  {turn.options.map((option, position) => (
                    <span key={option.cluster_id}>
                      {position > 0 ? ', ' : ''}
                      <em>{option.label}</em>
                    </span>
                  ))}
                </p>
              ) : null}
              <input
                id={`interview-${index}`}
                name={`interview-${index}`}
                value={said[index] ?? ''}
                onChange={(event) => {
                  const next = event.target.value
                  setSaid((current) => ({ ...current, [index]: next }))
                }}
              />
            </div>
          ))}
          {error ? <p className="error">{error}</p> : null}
          <button type="submit" disabled={busy || unanswered.length > 0}>
            {busy
              ? 'Reading your answers…'
              : unanswered.length > 0
                ? `${String(unanswered.length)} question(s) still to answer`
                : 'Turn these answers into version 1 (shadow)'}
          </button>
        </form>
      ) : null}

      {panel.approval !== null ? (
        <form
          className="stack"
          onSubmit={(event) => {
            event.preventDefault()
            const approval = panel.approval
            if (approval === null) return
            onApprove(
              {
                ...approval.artifact,
                approver: approver.trim(),
                approved_at: new Date().toISOString(),
              },
              approval.header,
            )
          }}
        >
          <h3>Approve v{panel.approval.version} in writing</h3>
          <p className="muted">{panel.approval.detail}</p>
          <p className="muted">
            Bound to <code className="hash">{panel.approval.envelope_hash}</code>. This hash is
            the service’s, not this page’s: it is a digest of the terms you just read, and an
            approval only ever activates the exact document it was given for.
          </p>
          <label htmlFor="approver">Your name, as the person approving these terms</label>
          <input
            id="approver"
            name="approver"
            value={approver}
            onChange={(event) => setApprover(event.target.value)}
          />
          {error ? <p className="error">{error}</p> : null}
          <button type="submit" disabled={busy || approver.trim() === ''}>
            {busy ? 'Recording…' : 'I approve these terms — go live'}
          </button>
        </form>
      ) : null}

      {panel.step === 'active' ? (
        <p className="muted">
          This store’s envelope is live under a recorded approval. Editing any term mints the
          next version in <strong>shadow</strong>: the approval on file is bound to the version
          it covers, so new terms need a new signature.
        </p>
      ) : null}

      {/*
        The killed step used to render nothing at all: no interview, no approval form, and no
        sentence — a merchant who had stopped their store opened this card and found a blank. The
        approval is still deliberately not offered here, because the service refuses to activate a
        killed envelope however well the artifact is bound; what was missing was the step that
        comes first. It is in the kill-switch card, so this says so rather than duplicating it.
      */}
      {panel.step === 'killed' ? (
        <p className="muted">
          This store is stopped, so there is nothing to approve here yet: a killed envelope refuses
          activation whoever signs it. Restart it from the kill-switch card — it comes back in{' '}
          <strong>shadow</strong> at the same version — and this card will then ask you to approve
          those terms, which is what puts the store back on the network.
        </p>
      ) : null}
    </Card>
  )
}

// ======================================================================================
// Envelope — R9's "envelope editing (versioned)"
// ======================================================================================
export function EnvelopeCard({
  panel,
  onSave,
  busy,
  error,
}: {
  panel: EnvelopePanel
  onSave: (document: unknown) => void
  busy: boolean
  error: string
}): JSX.Element {
  const current = panel.current
  const [draft, setDraft] = useState<string>('')
  const [dirty, setDirty] = useState(false)
  const text = dirty ? draft : current ? JSON.stringify(current, null, 2) : ''

  return (
    <Card title="Economic envelope" requirement="R6 · R9 (versioned)">
      <PanelNotice panel={panel} />
      <p className="muted">
        Every version is a value and is never rewritten, so this history is the record rather
        than a reconstruction of it. Saving writes the <em>next</em> version in{' '}
        <strong>shadow</strong>: the body is your request, and activation is granted only by a
        written approval artifact (<code>X-Envelope-Approval</code>), never by asking for it.
      </p>
      {panel.versions.length > 0 ? (
        <div className="scroller">
        <table className="grid">
          <thead>
            <tr>
              <th>Version</th>
              <th>Activation</th>
              {/*
                Three columns, as the Screens document draws it. `approved_at` used to be a
                fourth and, beside an approver address that is a single 43-character token, it
                sat off the right edge of the card where nobody could read it. It is now the
                second line of the approval cell — same two facts, both on the page, and they
                belong together anyway: who signed and when they signed is one event.
              */}
              <th>Approved by</th>
            </tr>
          </thead>
          <tbody>
            {/*
              Keyed on version AND activation AND position, not on version alone. A kill files
              a NEW state at the SAME version — v1 shadow, v2 shadow, v2 killed is a real
              history — so `key={row.version}` collides. React reported it against the live
              service: "Encountered two children with the same key, `2`".
            */}
            {panel.versions.map((row, index) => (
              <tr
                key={`${row.version}-${row.activation}-${index}`}
                className={index === panel.versions.length - 1 ? 'is-head' : ''}
              >
                <td>v{row.version}</td>
                <td>
                  <span className={`pill pill--${row.activation}`}>{row.activation}</span>
                </td>
                <td className="approver">
                  {row.approved_by ?? '—'}
                  {row.approved_at ? (
                    <>
                      <br />
                      <span className="muted">{instant(row.approved_at)}</span>
                    </>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      ) : null}
      {current ? (
        <form
          className="stack"
          onSubmit={(event) => {
            event.preventDefault()
            try {
              onSave(JSON.parse(text) as unknown)
            } catch (problem) {
              onSave({ __unparseable: String(problem) })
            }
          }}
        >
          <label htmlFor="envelope-terms">Terms for the next version</label>
          <textarea
            id="envelope-terms"
            className="code"
            rows={14}
            spellCheck={false}
            value={text}
            onChange={(event) => {
              setDirty(true)
              setDraft(event.target.value)
            }}
          />
          {error ? <p className="error">{error}</p> : null}
          <button type="submit" disabled={busy}>
            {busy ? 'Saving…' : 'Save as a new version'}
          </button>
        </form>
      ) : null}
    </Card>
  )
}

// ======================================================================================
// Kill switch — R9
// ======================================================================================
export function KillSwitch({
  storeId,
  activation,
  mayBid,
  reason,
  onKill,
  onRevive,
  busy,
  error,
}: {
  storeId: string
  activation: string
  mayBid: boolean
  reason: string
  onKill: () => void
  onRevive: () => void
  busy: boolean
  error: string
}): JSX.Element {
  const [confirmation, setConfirmation] = useState('')
  const armed = confirmation.trim() === storeId
  const stopped = activation === 'killed'
  /*
    Disarm on the way out, whichever button was pressed. Both controls read the same field, and
    the card flips to the other one as soon as the page reloads — so a merchant who typed the
    store id to arm the RESTART would have found the kill button already armed underneath it,
    one misclick from stopping the store they had just started. Clearing here rather than in an
    effect keeps it a property of the press rather than of a render.
  */
  const press = (act: () => void) => () => {
    setConfirmation('')
    act()
  }

  return (
    <Card title="Kill switch" requirement="R9">
      <p className="status-line">
        This store is{' '}
        <span className={`pill pill--${activation}`}>{activation}</span> and{' '}
        <strong>{mayBid ? 'is bidding' : 'is not bidding'}</strong>.
      </p>
      <p className="muted">{reason}</p>
      {/*
        WHAT THE BUTTON DOES, SAID BEFORE THE PRESS RATHER THAN DISCOVERED AFTER IT.

        This notice used to read "Stopping this store is permanent", and it was true of the
        service it described: `activate_envelope` refused a killed envelope, `edit_envelope`
        carried `killed` forward onto every version an edit minted, and the refusal's own advice
        ("publish a new version and approve that") named the one thing that could not work. The
        owner ruled that a merchant must be able to restart their agent, so
        `POST /stores/{id}/revive` now lifts the kill — and this copy is the first thing that had
        to stop saying otherwise. A console that overstates a consequence is not being careful;
        it is being wrong in the direction that keeps a merchant from using their own store.

        What is still true, and is what this card owes the person about to press the button:
        stopping is instant and takes no approval, and coming back is neither. Restarting is two
        deliberate acts — this card's restart returns the envelope to `shadow`, and only a
        written approval in the onboarding card puts the store back on the network. Measured in
        `merchant_svc.envelope.versions`: `revive_envelope` can only produce `shadow`, and
        `edit_envelope` still carries `killed` forward, so saving new terms while stopped does
        NOT restart anything.
      */}
      <div className="notice notice--final">
        <p className="notice__heading">Stopping is instant. Starting again is deliberate.</p>
        <p className="notice__detail">
          Stopping needs no approval and no confirmation from anyone else — that is the whole
          point of the switch. The agent submits nothing from the next solicitation onward.
        </p>
        <p className="notice__detail">
          The stop is reversible, but starting again is not one click: this card returns the
          envelope to{' '}
          <strong>shadow</strong>, which is stopped-but-restartable, and the store bids again only
          once you approve its terms in writing in the <a href="#onboarding">onboarding card</a>.
          Saving new terms while stopped does <em>not</em> restart the store — the version an edit
          publishes is killed too.
        </p>
      </div>
      {error ? <p className="error">{error}</p> : null}
      {stopped ? (
        /*
          The restart, in the card that did the stopping. It is armed the same way the kill is —
          the store id typed out — because this card has exactly one convention for a button that
          changes whether the store participates, and a looser one in the other direction would
          be a second convention in the same card.
        */
        <>
          <p className="muted">
            Stopped. Solicit a bid above and the agent answers <code>204 store_killed</code>, which
            is what makes the switch observable rather than merely recorded.
          </p>
          <div className="stack">
            <label htmlFor="revive-confirm">Type the store id to arm the restart</label>
            <input
              id="revive-confirm"
              value={confirmation}
              placeholder={storeId}
              onChange={(event) => setConfirmation(event.target.value)}
            />
            <button type="button" disabled={!armed || busy} onClick={press(onRevive)}>
              {busy ? 'Restarting…' : 'Restart this store’s agent — back to shadow'}
            </button>
            <p className="muted">
              This lifts the kill and nothing more. The envelope returns to <strong>shadow</strong>{' '}
              at the same version, carrying no approval, and the agent still submits nothing until
              you sign for those terms again.
            </p>
          </div>
        </>
      ) : (
        <>
          <p className="muted">
            After killing, solicit a bid above: the agent answers <code>204 store_killed</code>,
            which is what makes the switch observable rather than merely recorded.
          </p>
          <div className="stack">
            <label htmlFor="kill-confirm">Type the store id to arm</label>
            <input
              id="kill-confirm"
              value={confirmation}
              placeholder={storeId}
              onChange={(event) => setConfirmation(event.target.value)}
            />
            <button
              type="button"
              className="danger"
              disabled={!armed || busy}
              onClick={press(onKill)}
            >
              {busy ? 'Killing…' : 'Kill this store’s agent'}
            </button>
          </div>
        </>
      )}
    </Card>
  )
}

// ======================================================================================
// Win / loss — R9, reason categories only
// ======================================================================================
export function LossesCard({ panel }: { panel: LossesPanel }): JSX.Element {
  const rows = panel.by_cluster ?? []
  return (
    <Card title="Where you lost" requirement="R9 · by cluster">
      <PanelNotice panel={panel} />
      <p className="muted">
        Reason categories and the buyer criteria you did not meet. <strong>No rival is named
        and no amount is shown</strong> — not because they were left out of this page, but
        because the exchange never sends them: a report that carried the winning price would
        turn a seller feedback loop into a price-intelligence feed.
      </p>
      {panel.state === 'ok' && rows.length === 0 ? (
        <p className="muted">
          No losses recorded in this window. The window is the one this page asked for, so this
          is an answer and not an empty panel.
        </p>
      ) : null}
      {rows.length > 0 ? (
        <>
          <div className="scroller">
            <table className="grid">
              <thead>
                <tr>
                  <th>Intent cluster</th>
                  <th>Lost</th>
                  {REASONS.map((reason) => (
                    <th key={reason}>{reason}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.cluster_id}>
                    <td>{row.cluster_id}</td>
                    <td>{row.lost}</td>
                    {REASONS.map((reason) => (
                      <td key={reason} className={row.reasons[reason] > 0 ? 'hot' : ''}>
                        {row.reasons[reason]}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/*
            The unmet criteria sit BELOW the table rather than in a seventh column, which is
            where the Screens document puts them and, measured in the browser, the only place
            they are readable: in a 23rem card a seventh column holding "free shipping over
            $35" fell off the right edge and a merchant scrolling past read the loss report as
            though no criterion had been missed.

            Each list still names its own cluster. The design's mock shows one flat list under
            a two-cluster table, and that ambiguity is the one thing not carried over: which
            criteria belong to which cluster is the actionable half of this panel.
          */}
          <h3>Buyer criteria unmet</h3>
          {rows.map((row) => (
            <div key={row.cluster_id}>
              <p className="muted">
                <code>{row.cluster_id}</code>
              </p>
              {row.unmet_criteria.length > 0 ? (
                <ul className="tight">
                  {row.unmet_criteria.map((criterion) => (
                    <li key={criterion}>{criterion}</li>
                  ))}
                </ul>
              ) : (
                <p className="muted">
                  The report names no unmet criterion for this cluster — the losses above were
                  categorised without one.
                </p>
              )}
            </div>
          ))}
        </>
      ) : null}
    </Card>
  )
}

// ======================================================================================
// Trust — R9's per-dimension breakdown and the event payloads
// ======================================================================================
export function TrustCard({
  panel,
  events,
}: {
  panel: TrustPanel
  events: TrustEventsPanel
}): JSX.Element {
  const snapshot = panel.snapshot
  // `dims`, which is what the trust service actually serves. Measured against a running
  // instance: an earlier draft read `dimensions` and rendered an empty table forever.
  const dimensions = Object.entries(snapshot?.dims ?? {})
  return (
    <Card title="Trust" requirement="R12 · R13 · R9">
      <PanelNotice panel={panel} />
      {snapshot ? (
        <>
          <p className="score">
            <span className="score__value">
              {typeof snapshot.score === 'number' ? snapshot.score.toFixed(2) : '—'}
            </span>
            <span className="muted">
              {typeof snapshot.confidence === 'number'
                ? `confidence ${snapshot.confidence.toFixed(2)}`
                : ''}
              {snapshot.low_data ? ' · low data — a neutral prior, not a verdict' : ''}
              {snapshot.blacklisted ? ' · blacklisted' : ''}
            </span>
          </p>
          <div className="scroller">
          <table className="grid">
            <thead>
              <tr>
                <th>Dimension</th>
                <th>α</th>
                <th>β</th>
                <th>posterior mean</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {dimensions.map(([name, value]) => {
                // The Beta posterior mean. Arithmetic on the two numbers the service serves,
                // done here rather than expected from the wire, so there is exactly one
                // spelling of it and it cannot disagree with the alphas beside it.
                const alpha = value.alpha ?? 0
                const beta = value.beta ?? 0
                const total = alpha + beta
                const mean = total > 0 ? alpha / total : null
                return (
                  <tr key={name}>
                    <td>{name}</td>
                    <td>{alpha.toFixed(1)}</td>
                    <td>{beta.toFixed(1)}</td>
                    <td>{mean === null ? '—' : mean.toFixed(2)}</td>
                    <td className="bar-cell">
                      <span
                        className="bar"
                        style={{ width: `${Math.round((mean ?? 0) * 100)}%` }}
                      />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          </div>
        </>
      ) : panel.state === 'ok' ? (
        <p className="muted">{panel.detail}</p>
      ) : null}

      <h3>Event payloads</h3>
      <PanelNotice panel={events} />
      {events.state === 'ok' ? (
        (events.events ?? []).length === 0 ? (
          <p className="muted">No trust events for this store yet.</p>
        ) : (
          <ul className="events">
            {(events.events ?? []).map((event, index) => (
              <li key={`${String(event.seq ?? index)}-${String(event.kind ?? '')}`}>
                <details>
                  <summary>
                    <code>#{String(event.seq ?? '?')}</code> {String(event.kind ?? 'event')}
                  </summary>
                  <pre className="code">{JSON.stringify(event, null, 2)}</pre>
                </details>
              </li>
            ))}
          </ul>
        )
      ) : null}
    </Card>
  )
}

// ======================================================================================
// Bids — R9's bid log, and an honest account of the half that has no door
// ======================================================================================
export function BidsCard({
  panel,
  onSolicit,
  busy,
  error,
}: {
  panel: BidsPanel
  onSolicit: (cluster: string, query: string) => void
  busy: boolean
  error: string
}): JSX.Element {
  const [cluster, setCluster] = useState('')
  const [query, setQuery] = useState('')
  const entries = panel.entries ?? []

  return (
    <Card title="Bid activity" requirement="R7 · R9">
      <PanelNotice panel={panel} />
      <p className="muted">
        These are solicitations <strong>you</strong> made from this page, against your own
        agent’s real <code>POST /v1/bid-requests</code> — the same door the exchange uses. They
        are rehearsals, and each row is labelled <code>dashboard</code> so they cannot be read
        as auctions you lost.
      </p>
      <p className="muted">
        The per-auction rationale a <em>live</em> bid was written with is held in the agent’s
        own in-process shadow log and no service publishes it, so it cannot be shown here yet.
        That is a missing route, not an empty log — see this card’s note in the service.
      </p>
      {panel.state === 'ok' ? (
        <form
          className="row"
          onSubmit={(event) => {
            event.preventDefault()
            onSolicit(cluster.trim(), query.trim())
          }}
        >
          <input
            aria-label="intent cluster"
            placeholder="cluster (defaults to your envelope’s first)"
            value={cluster}
            onChange={(event) => setCluster(event.target.value)}
          />
          <input
            aria-label="shopper query"
            placeholder="what the shopper asked for"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <button type="submit" disabled={busy}>
            {busy ? 'Soliciting…' : 'Solicit a bid'}
          </button>
        </form>
      ) : null}
      {error ? <p className="error">{error}</p> : null}
      {entries.length === 0 && panel.state === 'ok' ? (
        <p className="muted">No solicitation has been made from this dashboard yet.</p>
      ) : null}
      <ul className="bids">
        {entries.map((entry) => (
          <BidRow key={`${entry.auction_id}-${entry.recorded_at}`} entry={entry} />
        ))}
      </ul>
    </Card>
  )
}

function BidRow({ entry }: { entry: SolicitationRow }): JSX.Element {
  const offer = entry.offer
  return (
    <li className={`bid bid--${entry.outcome}${entry.contradiction ? ' bid--contradiction' : ''}`}>
      <p className="bid__head">
        <span className={`pill pill--${entry.outcome}`}>{entry.outcome}</span>
        <span className="muted"> {instant(entry.recorded_at)} </span>
        <span className={`pill pill--${entry.activation}`}>{entry.activation}</span>
        <span className="muted"> {entry.origin}</span>
      </p>
      {/*
        The one row on this page a merchant must not scroll past: they stopped the store and
        the agent bid anyway. It is rendered as an alert rather than a badge because the whole
        reason the kill switch is on this page is to be believed.
      */}
      {/*
        "Stopped" and "not activated yet" are opposite facts about the merchant and the repair
        is different for each, so the alarm says which. Both are the same leak — the agent bid
        without authorisation — but a shadow store reading "stopped" would send its owner to
        the kill switch they never touched.
      */}
      {entry.contradiction ? (
        <p className="bid__alarm" role="alert">
          <strong>
            {entry.activation === 'killed'
              ? 'This store is stopped and its agent bid anyway.'
              : 'This store is not activated and its agent bid anyway.'}
          </strong>{' '}
          {entry.detail ??
            'The merchant service holds the authoritative envelope; nothing delivers it to a separately deployed agent.'}
        </p>
      ) : null}
      {entry.outcome === 'declined' ? (
        <p className="bid__reason">
          The agent declined: <code>{entry.decline_reason ?? 'no reason given'}</code>
        </p>
      ) : (
        <dl className="bid__offer">
          <dt>Product</dt>
          <dd>{offer?.product_ref ?? '—'}</dd>
          <dt>Unit price</dt>
          <dd>{money(offer?.unit_price, offer?.currency)}</dd>
          <dt>Discount</dt>
          <dd>
            {offer?.discount
              ? `${String(offer.discount.type ?? '')} ${String(offer.discount.value ?? '')}`
              : 'none authorised — list price stands'}
          </dd>
          <dt>Commitments</dt>
          <dd>{offer?.commitments?.length ? offer.commitments.join(', ') : 'none'}</dd>
          <dt>Claims</dt>
          <dd>{entry.claims}</dd>
        </dl>
      )}
      {entry.message ? <p className="bid__message">“{entry.message}”</p> : null}
    </li>
  )
}
