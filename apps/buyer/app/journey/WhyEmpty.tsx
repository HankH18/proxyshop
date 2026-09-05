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
  /**
   * True when the exchange no longer holds this auction (`shortlist: null`), so none of what
   * follows is live.
   *
   * The rows are identical either way — they are the same recorded diagnostics — but the
   * CLAIM around them is not. "Why there is nothing here to choose from" says something
   * about the market; "what the exchange reported when this auction ran" says something
   * about a window that has closed. Presenting the second as the first would tell a buyer
   * that no store was eligible when the truth is that nobody asked recently enough.
   */
  readonly recorded?: boolean
}

/**
 * The prices on one entry, printed as they arrived, or the fact that it carried none.
 *
 * `''` would render an empty span, and a blank beside a store's name reads as a price of
 * nothing rather than as no price reported. The numbers themselves are never touched: no
 * rounding, no currency symbol, because the exchange's report carries neither.
 *
 * The no-price case is defensive rather than observed: `AuctionEntryOut` declares
 * `unit_price` and `total_price` as REQUIRED floats, so an entry the exchange built always
 * carries both. This module reads an `unknown` body, so it handles the shape anyway — but it
 * is not a state this service can currently produce, and the comment says so rather than
 * implying the panel has seen one.
 */
function priceLine(unit?: number, total?: number): string {
  const parts: string[] = []
  if (unit !== undefined) parts.push(`unit ${unit}`)
  if (total !== undefined) parts.push(`total ${total}`)
  return parts.length === 0 ? 'no price reported' : parts.join(', ')
}

export function WhyEmpty({ record, recorded = false }: WhyEmptyProps) {
  const silent = record.entries.filter((entry) => entry.fallback_reason === NO_RESPONSE_REASON)
  // An empty diagnostic array has TWO meanings and this panel may not pick the flattering
  // one. `_recorded_rows` answers `[]` both for a list the exchange really sent empty and
  // for "this service holds no record of the auction at all" — and `outcome_for` reads a
  // per-process ring of 64, so a restart, a 65th auction, or a request landing on another
  // worker empties all five while the live shortlist is perfectly fine. `recorded_at` is the
  // field that tells them apart: the route sends it only when it HAS a record. Without one,
  // "the eligibility filters refused nothing" is a claim about the exchange that this page
  // has no evidence for, so it is not made.
  const noRecord = record.recorded_at === ''
  // The accessible name and the visible heading are two different strings on purpose, and
  // they were before this prop existed: the name says which panel this is, the heading says
  // what it answers. Both change with `recorded`, because both make a claim.
  const label = recorded
    ? 'What the exchange reported when this auction ran'
    : 'Why the shortlist is empty'
  const heading = recorded
    ? 'What the exchange reported when this auction ran'
    : 'Why there is nothing here to choose from'

  return (
    <section aria-label={label} className="diagnostics">
      <h3>{heading}</h3>
      {noRecord ? (
        <p data-testid="no-record">
          This buyer service holds no record of this auction &mdash; its answer carried no{' '}
          <code>recorded_at</code>, which is the field it sets only when it has one. The lists
          below are empty because there is nothing here to read, <em>not</em> because the
          exchange asked nobody, heard nothing, refused nobody and denied nobody. It keeps the
          last 64 auctions per process, so a restart or a busy run is enough to lose this one.
        </p>
      ) : null}
      {recorded ? (
        <p data-testid="recorded-not-live">
          None of this is live. The exchange has no shortlist for this auction any more, and
          does not say which of its several reasons applies, so what follows is the report the
          buyer service kept from when the auction ran &mdash; printed as the exchange sent it
          then. Nothing on this panel was filled in by this page, and nothing on it can be
          accepted now.
        </p>
      ) : (
        <p>
          {noRecord
            ? 'Nothing on this panel was filled in by this page, and with no record kept there is very little on it at all.'
            : 'The auction ran. Below is the exchange’s own report of it, printed as it sent it. Nothing on this panel was filled in by this page.'}
        </p>
      )}

      <h4 id="why-solicited">Stores the exchange asked ({record.solicited.length})</h4>
      {record.solicited.length === 0 ? (
        <p data-testid="solicited-empty">
          {noRecord
            ? 'Not recorded here, so this page cannot say which stores were asked.'
            : 'It asked no store at all — the roster it was given was empty.'}
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
        <p data-testid="entries-empty">
          {noRecord
            ? 'Not recorded here, so this page cannot say what any store answered.'
            : 'No store answered, and the exchange reported no entries.'}
        </p>
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
        <p data-testid="excluded-empty">
          {noRecord
            ? 'Not recorded here, so this page cannot say what the filters refused.'
            : 'The eligibility filters refused nothing.'}
        </p>
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
        <p data-testid="denied-empty">
          {noRecord
            ? 'Not recorded here, so this page cannot say who was allowed to bid.'
            : 'None. Every rostered store was allowed to answer.'}
        </p>
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
