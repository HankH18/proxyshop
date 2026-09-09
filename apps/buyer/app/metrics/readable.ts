/**
 * English for the machine words a trace carries — beside the code, never instead of it.
 *
 * =======================================================================================
 * WHY THIS EXISTS
 *
 * The trace surfaces in this app were readable by somebody who already knew the codebase and
 * by nobody else. `#/metrics` printed `store_declined · no_matching_product`, a row of
 * `intent_match=0.175 verified_claim_ratio=0.18785000000000002`, a bare `blacklisted` under a
 * heading that said "Denied entry", and the word `unauthorized` as the entire content of a
 * panel. Every one of those is a true, precise statement about what happened, and none of
 * them is a sentence a person can follow while somebody is talking over a demo.
 *
 * So this module publishes ONE English sentence per machine word, and the rendering rule that
 * goes with it: **the sentence is added, the code is kept.** Every helper here returns a
 * {@link Readable} carrying both, and every caller renders both — the sentence as prose, the
 * code in a monospace tail. Nothing is summarised away, no figure is recomputed, and a code
 * this file has no sentence for is printed plainly and SAID to be unrecognised rather than
 * being silently dropped or guessed at.
 *
 * That rule is `WhyEmpty.tsx`'s, stated there first and followed here: "The reason strings are
 * printed as the exchange spelled them. The only text this file adds is a plain-English gloss
 * … and every gloss sits *beside* the raw string rather than replacing it."
 *
 * =======================================================================================
 * WHAT IS DELIBERATELY NOT HERE
 *
 *   * **`fallback_reason`.** `WhyEmpty.tsx` already owns that vocabulary — all twelve families
 *     `collect.py::FALLBACK_REASONS` publishes, with a typed exhaustiveness check against
 *     `wire.ts`'s `FALLBACK_REASON_FAMILIES`, and its own suite. `MetricsPage` imports `explainFallbackReason` from it rather than this file
 *     restating it in a second place that could fall out of step. This module deliberately
 *     does not import that function either, so the dependency runs one way only:
 *     `wire.ts` → `readable.ts` → `WhyEmpty.tsx` → `MetricsPage.tsx`, with no cycle.
 *   * **The provenance labels.** `from their website` / `store-confirmed` / `unverified` are
 *     D55 claims about where a fact came from, not copy, and nothing here rewords them.
 *   * **Rounding of anything a caller then treats as the value.** {@link roundedScore} is a
 *     DISPLAY form. Every caller that uses it also prints the published figure unrounded.
 *
 * =======================================================================================
 * A NOTE ON ACCURACY
 *
 * Every sentence below was written against the code that MINTS the word, not against its
 * name. The sources, so a reader can check them rather than trust them:
 *
 *   * exclusion reasons — `apps/exchange/src/ranking/reasons.py`, whose module docstring is
 *     explicit that "the token that names the filter is part of the contract rather than
 *     incidental prose", and that readers match on the prefix and treat the rest as detail.
 *   * denial statuses — `apps/exchange/src/eligibility/__init__.py`, three statuses, and
 *     `orchestration/solicitation.py::_denial`, which is why the status word is usually
 *     repeated at the front of the reason text.
 *   * ranking components — `packages/contracts/src/ranking.py` (`RANK_FEATURES`, the weights
 *     and the penalty catalogue) and `apps/exchange/src/ranking/scoring.py`.
 *   * live-check outcomes — `apps/buyer/svc/src/livecheck/routes.py`, which counts exactly
 *     three: `agrees`, `contradicted`, `no_verdict`.
 */

/**
 * One machine word, and the English beside it.
 *
 * Both fields are always populated and neither is ever empty on a recognised code. A caller
 * that renders only `sentence` has thrown away the precision this page exists to show; a
 * caller that renders only `code` has changed nothing.
 */
export interface Readable {
  /** One sentence a person can follow. No machine word in it, no figure recomputed. */
  readonly sentence: string
  /** The code as the service spelled it — the family/prefix only, without the detail. */
  readonly code: string
  /** The free text the service attached after the colon, or `''`. Printed verbatim. */
  readonly detail: string
  /** False when this file carries no sentence for `code`, so a caller can say so. */
  readonly recognised: boolean
}

/**
 * Split a `prefix: detail` code on the FIRST colon.
 *
 * The same rule the exchange's own readers follow, and the same one `wire.ts` and
 * `telemetry.ts` already copy for `fallback_reason`: a detail may itself contain colons —
 * `hard_constraint_unsatisfied: 'material': the candidate carries no such attribute` is a
 * real, measured string — so splitting on the last colon, or on every colon, mangles it.
 */
function split(code: string): { prefix: string; detail: string } {
  const at = code.indexOf(':')
  if (at < 0) return { prefix: code.trim(), detail: '' }
  return { prefix: code.slice(0, at).trim(), detail: code.slice(at + 1).trim() }
}

/**
 * The sentence for a code no version of this file has a sentence for.
 *
 * Explicit rather than a fall-through to the bare string, for the reason `WhyEmpty`'s
 * `UNRECOGNISED_GLOSS` gives: a machine word with nothing beside it is the failure this
 * module exists to prevent, and an unrecognised word is the one case where that failure is
 * most likely — the exchange grows a reason, this copy has not caught up, and the page
 * silently goes back to being unreadable for exactly the new thing a demo is showing.
 */
export const UNRECOGNISED_SENTENCE =
  'is a code this page has no plain-English sentence for. It is the service’s own word, ' +
  'printed exactly as it arrived — nothing has been hidden and nothing has been guessed.'

/**
 * The whole sentence for an unrecognised code, the code included.
 *
 * The recognised sentences below stand on their own, so a caller renders `sentence` and
 * nothing else. An unrecognised one has to name the word it is about or it says nothing at
 * all, and making the CALLER special-case that is how one of them ends up rendered as a bare
 * dangling clause. It is assembled here instead, once.
 */
function unrecognised(prefix: string): string {
  return `“${prefix}” ${UNRECOGNISED_SENTENCE}`
}

function readable(
  prefix: string,
  detail: string,
  sentence: string | undefined,
): Readable {
  return {
    sentence: sentence ?? unrecognised(prefix),
    code: prefix,
    detail,
    recognised: sentence !== undefined,
  }
}

/* ------------------------------------------------------------------------------------- *
 * Exclusion reasons — why a candidate never reached the formula.
 * ------------------------------------------------------------------------------------- */

/**
 * One sentence per prefix `apps/exchange/src/ranking/reasons.py` publishes.
 *
 * Each one **stands on its own** — a complete sentence, capitalised, naming the rule rather
 * than the candidate — so a caller renders it as prose with no lead-in to supply and no way
 * to leave a dangling clause on the page. The unrecognised case is assembled by
 * {@link unrecognised} to the same standard.
 *
 * They describe the RULE. What the exchange attached after the colon describes THIS
 * candidate, is that service's own words, and is carried through untouched in `detail`.
 *
 * `REASON_UNEVIDENCED_CONSTRAINT` is deliberately absent: `reasons.py` says in so many words
 * that it is "NOT an exclusion reason", names a constraint the auction SET ASIDE rather than
 * a candidate it refused, and is published on the ranking result instead of on a row. A
 * sentence for it here would invite a caller to render it as a refusal.
 */
const EXCLUSION_SENTENCES: Readonly<Record<string, string>> = {
  blacklisted_store:
    'This shop is on the platform’s blacklist, so it was not allowed to take part in this ' +
    'auction at all (R12).',
  blacklist_unreadable:
    'The platform could not read whether this shop is blacklisted — no row for it, or a value ' +
    'that was neither a yes nor a no. An answer it cannot read is treated exactly as a ' +
    'blacklisting would be, so the candidate was refused rather than admitted (R12).',
  blacklist_source_denied:
    'The service that decides which shops may bid refused this one, or could not answer for ' +
    'it. Either way the exchange did not admit it (R12).',
  // FOUR BRANCHES, NOT ONE. `filters.py::expiry_reason` fires this for an offer with no
  // `expires_at` at all, one whose stamp is unreadable, one whose stamp is not a finite
  // instant, and one that really has expired — and `composition.py` documents the first as a
  // shape this repo actually ships. A sentence saying only "it ran out" would be wrong on
  // three of the four, so this one names the rule and lets the exchange's own detail beneath
  // it say which branch fired.
  expired_offer:
    'The exchange could not establish that this offer was still standing when the auction was ' +
    'decided — it named no expiry, or one that could not be read, or one that had already ' +
    'passed. An offer that cannot be shown to be live is refused rather than shown to you.',
  // Three branches again: no registered domain, no checkout URL at all, or a URL whose host is
  // not the registered one. Only the third is a mismatch, and only the third carries D22.
  off_domain_checkout:
    'The exchange could not confirm that this offer’s checkout link goes to the shop’s own ' +
    'registered domain — the link points somewhere else, or there is no link, or the shop has ' +
    'no registered domain to compare it against. It is refused here rather than a shopper ' +
    'being sent to it (C10).',
  // NOT "nobody checked". The ordinary case is a claim that WAS checked and says something
  // else; `filters.py` only ever feeds attested-verified attributes into the decision, so an
  // unsatisfied constraint means the verified evidence does not meet it.
  hard_constraint_unsatisfied:
    'One of the things you said you must have is not met by evidence the platform has verified ' +
    'for this shop — the verified facts say otherwise, or there is no verified fact about it. ' +
    'Either way an unverified claim does not count as met, so the candidate was refused rather ' +
    'than shortlisted on the shop’s own say-so (R19).',
  // ONE unreadable entry refuses the whole intent — `filters.py` returns on the first
  // `MalformedIntent` rather than requiring every entry to be bad.
  undecidable_hard_constraint:
    'The exchange could not read one of your stated requirements, and an intent it cannot ' +
    'decide refuses every candidate rather than admitting them all. The rest of your ' +
    'requirements may have been perfectly readable.',
  malformed_candidate:
    'The candidate record itself was incomplete — no bid reference, no offer, or no shop — ' +
    'so there was nothing here to rank.',
  // `BUDGET_OPS` is `("lte", "gte")`, so this is a FLOOR as often as a ceiling, and currency
  // is deliberately not compared. "Too expensive" would be wrong in both directions.
  offer_price_outside_budget:
    'You stated a price bound, and the price this bid actually asked for is on the wrong side ' +
    'of it. It is decided against the bid rather than against the catalogue price a shop ' +
    'claims for itself, which is why a shop whose listed price fits can still be refused here.',
  // `filters.py::budget_reasons` fires this when THE BUYER'S OWN BOUND is not a finite amount
  // — the offer's price read fine. `reasons.py` names that branch explicitly.
  offer_price_unreadable:
    'A price bound was in play and the comparison could not be made: either the offer carried ' +
    'no price to compare, or the bound itself was not a finite amount. A bound that cannot be ' +
    'decided refuses rather than admitting everybody (R19).',
  // The exchange RETRACTED the flat "the platform picked the product" claim: `reasons.py` says
  // who picked it depends on where the roster came from, and the buyer service supplies its
  // own. What holds in every case is that no shop bid and no shop wrote the pitch.
  organic_result_off_topic:
    'No shop bid for this one, and no shop wrote a word of the case for it — it is an organic ' +
    'row, so the platform’s own crawl is the only thing vouching for it, and that crawl does ' +
    'not connect this product to what you asked for.',
}

/**
 * The overflow line `GET /buyer/auctions/{id}` appends when a candidate failed more checks
 * than the response reports.
 *
 * NOT A REASON, and it reached the page as one. `auction/routes.py::_exclusion_reasons_out`
 * caps a candidate's list at eight and appends `"... and N further exclusion reason(s) not
 * reported: …"` — a memory bound on an unauthenticated response, and deliberately a summary
 * rather than a silent truncation. Split on its first colon that becomes an unrecognised
 * "code", and the unrecognised sentence then told a reader **"nothing has been hidden"** about
 * the one string in the whole vocabulary that exists to say something was.
 */
const OVERFLOW_MARK = '...'

const OVERFLOW_SENTENCE =
  'This is not a reason. It is the service saying it capped the list: this candidate failed ' +
  'more checks than one answer reports, and the ones above are the first of them. The cap is ' +
  'a size bound on a route that takes no credential, not a judgement about the candidate.'

/**
 * The English for one `exclusion_reasons[]` entry.
 *
 * The exchange writes these as `prefix: free text`, and the free text is often already a
 * sentence — `"'demo-fastfleece' is blacklisted and may not participate (R12)"`. That text is
 * returned in `detail` untouched: it is the exchange's own account of THIS candidate, where
 * the sentence above is this page's account of the RULE, and a caller shows both.
 */
export function explainExclusion(reason: string): Readable {
  const { prefix, detail } = split(reason)
  if (reason.trimStart().startsWith(OVERFLOW_MARK)) {
    return { sentence: OVERFLOW_SENTENCE, code: prefix, detail, recognised: true }
  }
  return readable(prefix, detail, EXCLUSION_SENTENCES[prefix])
}

/* ------------------------------------------------------------------------------------- *
 * Denial statuses — why a store was never asked to bid.
 * ------------------------------------------------------------------------------------- */

/**
 * The three statuses `exchange.eligibility` publishes, in English.
 *
 * `eligible` is here even though a store carrying it is not denied and should never reach a
 * "denied" list. It is included because a status this file did not recognise would read as an
 * unrecognised code, and "the record says this shop was allowed to bid, under a heading that
 * says it was not" is a real fact about a defect that is worth surfacing as itself.
 */
const DENIAL_SENTENCES: Readonly<Record<string, string>> = {
  // NOT "the trust record says so". `composition.py` complains about exactly this wording:
  // under `StaticSellerEligibility` the verdict comes from a hand-typed map, and calling that
  // a trust verdict is "a sentence that reads like a trust verdict and is not one". The
  // gate's own words are printed beside this, and they name the source.
  blacklisted:
    'The eligibility gate has this shop blacklisted, so it was never asked to bid (R12). ' +
    'Which source said so is in the gate’s own words below.',
  unavailable:
    'The platform could not establish whether this shop was allowed to bid. It fails closed: ' +
    'an answer it cannot get is treated as a no, so the shop was not asked.',
  eligible:
    'The record says this shop was eligible, which is not a reason for it to be on this ' +
    'list. That is the record disagreeing with itself rather than something the shop did.',
}

/** The English for one `denied[]` row's `status`. The `reason` is printed beside it. */
export function explainDenial(status: string): Readable {
  const word = status.trim()
  return readable(word, '', DENIAL_SENTENCES[word])
}

/* ------------------------------------------------------------------------------------- *
 * Ranking components — what each term of the published formula measures.
 * ------------------------------------------------------------------------------------- */

/**
 * The five features of D13's formula plus its penalty term, named the way a shopper would.
 *
 * From `packages/contracts/src/ranking.py`: `RANK_FEATURES` is
 * `(intent_match, verified_claim_ratio, trust, price_value, delivery_fit)` and the score is
 * `w_m*intent_match + w_e*verified_claim_ratio + w_t*trust + w_v*price_value + w_d*delivery_fit
 * − policy_penalties`. `policy_penalties` is `scoring.py`'s `PENALTY_COMPONENT`, published so
 * the components still sum to `rank_score`.
 *
 * These are the ROLES, not the numbers. Every caller prints the published figure beside the
 * name, because the name is what this file adds and the figure is what the exchange said.
 */
const COMPONENT_NAMES: Readonly<Record<string, string>> = {
  // `intent_match` is the platform's reading of the CANDIDATE RECORD against the intent —
  // and it is not the `fit_score` a shortlist card prints, which is why this wording is
  // deliberately not the card's. Its absent reading is the published neutral 0.5, so a
  // component of `0.35 * 0.5 = 0.175` means "nobody measured this", not "a poor fit"; the
  // published `components` map cannot tell the two apart, and neither does this page.
  intent_match: 'how the platform’s record of it lines up with what you asked for',
  // NOT "the share of its claims that were checked" — that was the v1.0.0 definition and
  // `RANKING_FEATURES_VERSION` is 3.0.0. It is now an aggregation over verified claims whose
  // subject is something THIS buyer asked about; a verified claim nobody asked about is worth
  // exactly what silence is worth, and a contradicted one contributes nothing.
  verified_claim_ratio: 'verified evidence about the things you actually asked about',
  trust: 'the shop’s standing trust score',
  // NOT "its price against the rest of this auction". `features.py` states outright that this
  // term "reads only the store’s own two numbers, so it is rival-independent": it is the
  // depth of the shop's discount off its OWN list price, saturating once it clears the
  // auction's band. The cheapest offer in an auction scores zero if it bid at list.
  price_value: 'how deeply the shop discounted its own list price',
  // ...and THIS is the one that is measured against the rest of the auction: the declared
  // estimate weighed against the shop's dispatch record, then compared with the other offers.
  // There is no absolute days-to-score curve anywhere in this system, by design.
  delivery_fit: 'how its delivery promise compares with the others in this auction',
  // TWO KINDS, not one. `DEFAULT_PENALTIES_PER_KIND` is
  // `{severe_policy_violation: 0.30, contradicted_claim: 0.15}`, and the heavier of the two is
  // the schema default. Naming only the lighter one would understate what a penalty can be.
  policy_penalties: 'penalties the platform applied against this shop, subtracted from the rest',
}

/**
 * What one entry of the published `components` map IS — stated once, here, because getting it
 * wrong is the easiest way for this page to assert a number nobody produced.
 *
 * `ranking/scoring.py` returns TWO maps: `features`, the raw inputs, each clamped to [0, 1];
 * and `components`, `weight * feature` for each term, which is what `POST /auctions` publishes
 * and what every surface in this app renders. So a component is a **weighted contribution**,
 * bounded by that term's weight rather than by 1 — `intent_match` cannot exceed 0.35,
 * `delivery_fit` cannot exceed 0.15 — and the components sum to `rank_score`. A caller may
 * therefore say "these add up to the score"; it may NOT say each one is "out of 1", and it may
 * not divide the weight back out, which `scoring.py` notes is impossible for a zero weight.
 */
export const COMPONENTS_ARE =
  'Each figure is that term’s contribution to the score, not a mark out of 1: the exchange has ' +
  'already multiplied it by the term’s published weight, and the contributions add up to the ' +
  'total.'

/**
 * A component key in English, or the key itself when this file does not know it.
 *
 * The key is returned unchanged rather than prettified — no underscore-stripping, no title
 * case. A key this page has never seen is a real thing to notice on a demo, and dressing it
 * up as English would hide exactly that.
 */
export function componentName(key: string): string {
  return COMPONENT_NAMES[key] ?? key
}

/** True when {@link componentName} returned a sentence rather than echoing the key. */
export function componentIsNamed(key: string): boolean {
  return key in COMPONENT_NAMES
}

/* ------------------------------------------------------------------------------------- *
 * Live-check outcomes.
 * ------------------------------------------------------------------------------------- */

/**
 * The three outcomes `apps/buyer/svc/src/livecheck/routes.py` counts.
 *
 * `no_verdict` is the one that most needs a sentence: it is the ledger holding a check that
 * reached no conclusion, which is a different answer from the page having failed to read one
 * and a different answer again from the claim having been contradicted.
 */
const OUTCOME_SENTENCES: Readonly<Record<string, string>> = {
  agrees: 'The shop’s own page said the same thing as the claim the platform checked.',
  contradicted: 'The shop’s own page contradicted the claim the platform checked.',
  // NO CLAIM ABOUT A FETCH. This sentence used to say "the page was fetched and nothing on it
  // either confirmed or contradicted the claim", and on every configuration this repo ships
  // that is false: `composition.py` defaults `live_page_fetcher` to `"off"`, `livecheck/
  // routes.py` then binds `NoPageFetcher`, and `fetching.py` returns a page with `status=0`
  // having opened no socket. Nothing in the tree turns it on. So the ONE thing the record
  // reliably knows is that no verdict was reached — and the record's own `fetch_reason`, which
  // the panel now prints beside this, is what says whether anything was read at all.
  no_verdict:
    'No verdict. The platform reached no conclusion about this claim — the reason it gives is ' +
    'printed beside this, and it is often that nothing was read in the first place.',
}

/** The English for one live-check `outcome`. */
export function explainOutcome(outcome: string): Readable {
  const word = outcome.trim()
  return readable(word, '', OUTCOME_SENTENCES[word])
}

/* ------------------------------------------------------------------------------------- *
 * Reading states.
 * ------------------------------------------------------------------------------------- */

/**
 * What each {@link import('./telemetry').Reading} state means, said as a claim rather than as
 * a status word.
 *
 * The distinction the whole `Reading` union exists to keep — "a blank is not a zero" — is
 * carried by these six sentences and not by the six words alone. `absent` and `failed` look
 * equally like "no data" to a reader who has not read `telemetry.ts`; they are the difference
 * between the service answering "I have no record of that" and this page being unable to read
 * the answer at all.
 */
const STATE_SENTENCES: Readonly<Record<string, string>> = {
  idle: 'Nothing has been asked for yet.',
  loading: 'Asked, and the answer has not come back.',
  ok: 'Read, and shown.',
  unauthorized: 'The service refused this page — it is not allowed to read this.',
  absent: 'The service answered, and it has no record of this.',
  failed: 'This page asked and could not read the answer.',
  unreachable: 'This page cannot reach that source at all from this origin.',
}

/** The English for one reading state. The state word itself stays on the page beside it. */
export function explainState(state: string): Readable {
  return readable(state, '', STATE_SENTENCES[state])
}

/* ------------------------------------------------------------------------------------- *
 * Figures.
 * ------------------------------------------------------------------------------------- */

/**
 * A published figure at three decimal places, for reading aloud.
 *
 * **A display form, and only a display form.** `verified_claim_ratio=0.18785000000000002` is
 * the exact double the exchange published and it is not wrong — it is unreadable, which is a
 * different problem with a different fix. Every caller of this function also prints the
 * unrounded figure, so rounding here never becomes the only record of the value on the page.
 *
 * `undefined` in, `undefined` out. This function will not turn an absent score into `0.000`;
 * that is the defect `telemetry.ts::asNumber` and `wire.ts::rankLine` both exist to prevent,
 * and it would be undone in one line here.
 */
export function roundedScore(value: number | undefined): string | undefined {
  if (value === undefined || !Number.isFinite(value)) return undefined
  return value.toFixed(3)
}

/**
 * True when {@link roundedScore} has actually changed the figure, so a caller can decide
 * whether printing both forms says anything. `0.05` rounds to `0.050`, which is the same
 * number spelled differently and not worth a second column.
 */
export function roundingHidesDigits(value: number | undefined): boolean {
  const rounded = roundedScore(value)
  if (rounded === undefined || value === undefined) return false
  return Number(rounded) !== value
}
