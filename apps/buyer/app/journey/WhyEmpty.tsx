/**
 * The panel that runs instead of a blank page when the shortlist has no slots.
 *
 * This project's defining defect is a demo that supplies its own join, and an empty
 * shortlist is exactly where that temptation bites: a screen with nothing on it invites a
 * placeholder row. So this component renders the exchange's own report and nothing else —
 * every store it asked, what each one answered, every candidate its filters refused with
 * *all* of that candidate's reasons, and every store that was not allowed to bid.
 *
 * Two rules it keeps:
 *
 * 1. **Verbatim.** The reason strings are printed as the exchange spelled them. The only
 *    text this file adds is a plain-English gloss for one value it recognises —
 *    `fallback_reason: "no_response"` — and that gloss sits *beside* the raw string rather
 *    than replacing it.
 * 2. **No row this page invented.** A section with nothing in it says the list was empty.
 *    It never pads.
 *
 * The `<details>` at the bottom prints the parsed body itself, so the claim "this all came
 * from the service" is one click away from being checked rather than being taken on trust.
 */
import { NO_RESPONSE_REASON, type AuctionRecord } from './wire'

export interface WhyEmptyProps {
  readonly record: AuctionRecord
}

function priceLine(unit?: number, total?: number): string {
  const parts: string[] = []
  if (unit !== undefined) parts.push(`unit ${unit}`)
  if (total !== undefined) parts.push(`total ${total}`)
  return parts.join(', ')
}

export function WhyEmpty({ record }: WhyEmptyProps) {
  const silent = record.entries.filter((entry) => entry.fallback_reason === NO_RESPONSE_REASON)

  return (
    <section aria-label="Why the shortlist is empty" className="diagnostics">
      <h3>Why there is nothing here to choose from</h3>
      <p>
        The auction ran. Below is the exchange&rsquo;s own report of it, printed as it sent it.
        Nothing on this panel was filled in by this page.
      </p>

      <h4 id="why-solicited">Stores the exchange asked ({record.solicited.length})</h4>
      {record.solicited.length === 0 ? (
        <p data-testid="solicited-empty">
          It asked no store at all — the roster it was given was empty.
        </p>
      ) : (
        <ul aria-labelledby="why-solicited" className="mono" data-testid="solicited">
          {record.solicited.map((storeId, index) => (
            <li key={`${storeId}:${index}`}>{storeId}</li>
          ))}
        </ul>
      )}

      <h4 id="why-entries">What each store answered ({record.entries.length})</h4>
      {record.entries.length === 0 ? (
        <p data-testid="entries-empty">No store answered, and the exchange reported no entries.</p>
      ) : (
        <ul aria-labelledby="why-entries" className="mono" data-testid="entries">
          {record.entries.map((entry, index) => (
            <li key={`${entry.store_id}:${index}`} data-testid={`entry-${entry.store_id}`}>
              <span className="key">{entry.store_id}</span>{' '}
              {entry.tier === undefined ? null : <span>tier {entry.tier} </span>}
              <span data-tone={entry.fallback ? 'unverified' : 'confirmed'}>
                {entry.fallback ? 'fallback: true' : 'fallback: false'}
              </span>{' '}
              {entry.fallback_reason === null ? (
                <span>fallback_reason: null</span>
              ) : (
                <span>fallback_reason: {entry.fallback_reason}</span>
              )}{' '}
              <span>{priceLine(entry.unit_price, entry.total_price)}</span>
            </li>
          ))}
        </ul>
      )}
      {silent.length > 0 ? (
        <p className="gloss" data-testid="no-response-gloss">
          <code>fallback_reason: &quot;{NO_RESPONSE_REASON}&quot;</code> means that store&rsquo;s
          agent did not answer with a usable bid, so the exchange represented it at its list
          price instead of dropping it.
        </p>
      ) : null}

      <h4 id="why-excluded">Candidates the filters refused ({record.excluded.length})</h4>
      {record.excluded.length === 0 ? (
        <p data-testid="excluded-empty">The eligibility filters refused nothing.</p>
      ) : (
        <ul aria-labelledby="why-excluded" className="mono" data-testid="excluded">
          {record.excluded.map((row, index) => (
            <li key={`${row.bid_ref}:${index}`} data-testid={`excluded-${row.store_id}`}>
              <span className="key">{row.store_id}</span> <span>{row.bid_ref}</span>
              {row.exclusion_reasons.length === 0 ? (
                <p>the exchange named no reason</p>
              ) : (
                <ul aria-label={`Why ${row.bid_ref} was refused`}>
                  {row.exclusion_reasons.map((reason, at) => (
                    <li key={`${reason}:${at}`}>{reason}</li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}

      <h4 id="why-denied">Stores that were not allowed to bid ({record.denied.length})</h4>
      {record.denied.length === 0 ? (
        <p data-testid="denied-empty">None. Every rostered store was allowed to answer.</p>
      ) : (
        <ul aria-labelledby="why-denied" className="mono" data-testid="denied">
          {record.denied.map((row, index) => (
            <li key={`${row.store_id}:${index}`} data-testid={`denied-${row.store_id}`}>
              <span className="key">{row.store_id}</span> <span>{row.status}</span>{' '}
              <span>{row.reason}</span>
            </li>
          ))}
        </ul>
      )}

      <p>Nothing has been ordered, and no checkout exists.</p>

      <details data-testid="verbatim-auction">
        <summary>The service&rsquo;s answer, verbatim</summary>
        <pre className="mono">{JSON.stringify(record.raw, null, 2)}</pre>
      </details>
    </section>
  )
}

export default WhyEmpty
