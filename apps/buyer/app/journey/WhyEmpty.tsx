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
 * 1. **Verbatim.** The reason strings are printed as the exchange spelled them. The only text
 *    this file adds is a plain-English gloss for each distinct `fallback_reason` on the page,
 *    and every gloss sits *beside* the raw string rather than replacing it. The glosses are
 *    keyed by the reason's FAMILY — see `wire.ts`'s `FALLBACK_REASON_FAMILIES` — and a family
 *    this file has no sentence for gets a sentence saying exactly that, so an unrecognised
 *    reason is never left on the page as a bare machine word with nothing beside it.
 * 2. **No row this page invented.** A section with nothing in it says the list was empty.
 *    It never pads.
 *
 * The `<details>` at the bottom prints the parsed body itself, so the claim "this all came
 * from the service" is one click away from being checked rather than being taken on trust.
 */
import {
  NO_RESPONSE_REASON,
  UNDISCLOSED_REFUSAL_DETAIL,
  fallbackReasonDetail,
  fallbackReasonFamily,
  type AuctionEntry,
  type AuctionRecord,
  type FallbackReasonFamily,
} from './wire'

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
 *
 * A ZERO gets a sentence of its own, and this is now the only place in the app that prints
 * one of these numbers, so the sentence lives here. MEASURED in
 * `apps/exchange/src/auction/routes.py::_entries_out`, which builds this very field as
 * `unit_price=float(offer.get("unit_price", 0.0))`: an offer that named no price is reported
 * as `0.0`, indistinguishable on the wire from an offer that named zero. This panel cannot
 * tell the two apart and does not pretend to — it prints the number the service sent and says
 * what a zero there can also mean, because a bare "unit 0" reads to a buyer as free. The
 * fallback case names the other origin of a zero: a roster row whose `list_price` the
 * exchange could not read.
 */
function priceLine(unit?: number, total?: number, fallback = false): string {
  const parts: string[] = []
  if (unit !== undefined) parts.push(`unit ${unit}`)
  if (total !== undefined) parts.push(`total ${total}`)
  if (parts.length === 0) return 'no price reported'
  const line = parts.join(', ')
  if (unit !== 0 && total !== 0) return line
  return fallback
    ? `${line} — a zero is also what gets reported when that roster row carried no readable list price.`
    : `${line} — a zero is also what the exchange reports for an offer that named no price.`
}

/**
 * A shopper's sentence for one reason family, given whatever detail the reason carried.
 *
 * A function rather than a string because the detail is half the answer: "a store said no" is
 * a different fact from "a store said no because it has no matching product", and the second
 * is the one a shopper can act on. The detail is printed as the exchange bounded it and is
 * never re-derived here.
 */
type ReasonGloss = (detail: string) => string

/**
 * `store_declined` — the store answered, and the answer was no.
 *
 * Three cases, because they are three different things and only one of them is "it did not
 * say why". `UNDISCLOSED_REFUSAL_DETAIL` is the exchange saying it HAD a reason and could not
 * print it, which is not the same as the store having stayed quiet.
 */
function describeDecline(detail: string): string {
  const opening =
    'means that store was asked, it answered, and its answer was no — the store-agent ' +
    'contract’s 204, which is its own way of saying “I choose not to bid”. '
  if (detail === '') {
    return `${opening}It stated no reason, and this page will not invent one for it.`
  }
  if (detail === UNDISCLOSED_REFUSAL_DETAIL) {
    return (
      `${opening}It did state a reason and the exchange could not print it — too long, or ` +
      'spelled outside what it will echo back — so the reason reads “undisclosed” rather ' +
      'than being dropped.'
    )
  }
  return `${opening}The reason it gave for itself is “${detail}”.`
}

/**
 * `store_refused` — the store answered with something that was not a bid.
 *
 * The detail is usually the HTTP status, but not always: the exchange re-labels a refusal in a
 * family it does not publish by making the WHOLE unrecognised string the detail, so a
 * non-numeric detail is a real shape here rather than a defensive branch.
 */
function describeRefusal(detail: string): string {
  const opening =
    'means that store was asked and answered with something the exchange could not read a ' +
    'bid out of. It is an error, not a decision about what you asked for'
  if (detail === '' || detail === UNDISCLOSED_REFUSAL_DETAIL) {
    return `${opening}, and the exchange could not print which error it was.`
  }
  if (/^[0-9]+$/.test(detail)) {
    const aside =
      detail === '422'
        ? ' A 422 means the store rejected the request the exchange sent it, so that one is a ' +
          'fault on the exchange’s side of the wire rather than the store’s.'
        : ''
    return `${opening} — its agent answered HTTP ${detail}.${aside}`
  }
  return `${opening}. What the exchange recorded of it is “${detail}”.`
}

/**
 * One sentence per family the exchange publishes.
 *
 * Typed `Record<FallbackReasonFamily, …>` on purpose: adding a word to
 * `FALLBACK_REASON_FAMILIES` without adding a sentence here is a type error rather than a
 * shopper reading a machine word. The reverse — the exchange growing a word this copy has not
 * caught up with — is what {@link UNRECOGNISED_GLOSS} is for.
 */
const REASON_GLOSSES: Readonly<Record<FallbackReasonFamily, ReasonGloss>> = {
  tier_0_no_agent: () =>
    'means that store has no bidding agent for the exchange to ask. It is on the roster from ' +
    'its catalogue alone, so the exchange represented it at its list price without anybody ' +
    'having declined anything.',
  no_response: () =>
    'means nothing came back from that store’s agent at all — it did not answer with a usable ' +
    'bid, or with anything else — so the exchange represented it at its list price instead of ' +
    'dropping it. It is the word for a store that is switched off or too slow to reach, not ' +
    'for one that said no.',
  response_after_deadline: () =>
    'means that store did answer, but after the bidding window had already closed, so its bid ' +
    'could not be counted. The exchange represented it at its list price instead.',
  response_carried_no_bid: () =>
    'means that store answered and its answer carried no bid — there was nothing in it to ' +
    'rank or to put on a shortlist.',
  response_not_stamped: () =>
    'means the exchange never recorded when that store’s answer arrived, so it could not ' +
    'certify the answer came in before the window closed. That is a fault inside the ' +
    'exchange, not something the store did.',
  arrival_stamp_unparseable: () =>
    'means the arrival time the exchange recorded for that store’s answer was not a number it ' +
    'could read, so again it could not certify the answer came in on time. Also the ' +
    'exchange’s own record rather than the store’s answer.',
  bid_price_unreconcilable: () =>
    'means that store bid at a price its roster row does not authorise, so the exchange ' +
    'refused to rank the bid and represented the store at its list price instead. The ' +
    'candidates the filters refused, above, name the price rule it broke.',
  store_declined: describeDecline,
  store_refused: describeRefusal,
}

/**
 * The sentence for a family this copy of the vocabulary does not carry.
 *
 * Explicit, and not a fall-through to the raw string: the reason a shopper is on this panel at
 * all is that the shortlist is empty, and "here is a word you have never seen, with nothing
 * beside it" is the failure this panel exists to prevent.
 */
export const UNRECOGNISED_GLOSS =
  'is a reason this page has no plain-English sentence for. It is the exchange’s own word, ' +
  'printed here and in the list above exactly as it sent it — nothing is being hidden, and ' +
  'nothing has been guessed at on top of it.'

/** The sentence for one whole `fallback_reason`, family and detail together. */
export function explainFallbackReason(reason: string): string {
  const family = fallbackReasonFamily(reason)
  // Through a `Record<string, …>` view so the lookup is `| undefined`: indexing the literal
  // keys directly would type the miss away and make the unrecognised branch unreachable.
  const gloss: ReasonGloss | undefined = (REASON_GLOSSES as Readonly<Record<string, ReasonGloss>>)[
    family
  ]
  return gloss === undefined ? UNRECOGNISED_GLOSS : gloss(fallbackReasonDetail(reason))
}

/** One `data-testid` per distinct reason, derived from the reason so it is predictable. */
function glossTestId(reason: string): string {
  // The id an existing test pins for the one gloss this panel has always had. Kept as it was:
  // this is the same paragraph about the same reason, and renaming it would be a change to
  // the tests rather than to the panel.
  if (reason === NO_RESPONSE_REASON) return 'no-response-gloss'
  const slug = reason
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
  return `gloss-${slug === '' ? 'unrecognised' : slug}`
}

/** One glossed reason: the exchange's string, and what this page says about it. */
export interface GlossedReason {
  readonly reason: string
  readonly testId: string
  readonly sentence: string
}

/**
 * Every distinct `fallback_reason` in `entries`, in the order the rows carried them.
 *
 * Distinct rather than one per row: five stores refusing with the same status is one fact, and
 * a shopper needs it said once. Order is the entries' own — roster order, as the exchange
 * built it — so the glosses read down the page in the same order as the rows they explain.
 */
export function glossedReasons(entries: readonly AuctionEntry[]): readonly GlossedReason[] {
  const seen = new Set<string>()
  const out: GlossedReason[] = []
  for (const entry of entries) {
    const reason = entry.fallback_reason
    if (reason === null || reason.trim() === '' || seen.has(reason)) continue
    seen.add(reason)
    out.push({ reason, testId: glossTestId(reason), sentence: explainFallbackReason(reason) })
  }
  return out
}

export function WhyEmpty({ record, recorded = false }: WhyEmptyProps) {
  // Every reason the exchange actually reported, each with a sentence — rather than the one
  // string this panel used to recognise. It filtered for `fallback_reason === 'no_response'`
  // and glossed only that, which was the whole vocabulary back when the solicitor mapped every
  // non-200 answer to nothing. It no longer is: a store that declines now reads
  // `store_declined:no_matching_product` and a store that refuses reads `store_refused:422`,
  // and both used to land on this page as raw machine words with no explanation beside them.
  const glosses = glossedReasons(record.entries)
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
              <span>{priceLine(entry.unit_price, entry.total_price, entry.fallback)}</span>
            </li>
          ))}
        </ul>
      )}
      {glosses.length > 0 ? (
        <div data-testid="reason-glosses">
          {glosses.map((gloss) => (
            <p className="gloss" data-testid={gloss.testId} key={gloss.reason}>
              <code>fallback_reason: &quot;{gloss.reason}&quot;</code> {gloss.sentence}
            </p>
          ))}
        </div>
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
