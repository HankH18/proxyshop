/**
 * The English beside the machine words.
 *
 * What these pin is the one rule the module exists for: **the sentence is added and the code
 * is kept.** A helper that returned prose and dropped the code would make the trace pages
 * prettier and useless — they are demo aids whose whole claim is that every figure on them is
 * the service's — and a helper that quietly echoed a code it did not recognise would put the
 * page back where it started while every test stayed green.
 *
 * The vocabularies here are COPIES of the exchange's, exactly as `wire.ts`'s
 * `FALLBACK_REASON_FAMILIES` is, and this file cannot import the Python that publishes them.
 * So the lists below are spelled out with the file that mints each one named beside them, and
 * the unrecognised path is asserted directly — because a copy that goes stale is not a
 * hypothetical here. It has already happened once: `FALLBACK_REASON_FAMILIES` sat three words
 * short of `collect.py::FALLBACK_REASONS` for long enough to reach a shopper, and the gate
 * that was supposed to catch it iterated the short copy.
 */
import { describe, expect, it } from 'vitest'

import {
  COMPONENTS_ARE,
  UNRECOGNISED_SENTENCE,
  componentIsNamed,
  componentName,
  explainDenial,
  explainExclusion,
  explainOutcome,
  explainState,
  roundedScore,
  roundingHidesDigits,
} from './readable'

/** `apps/exchange/src/ranking/reasons.py::EXCLUSION_REASON_PREFIXES`, in its order. */
const EXCLUSION_PREFIXES = [
  'blacklisted_store',
  'blacklist_unreadable',
  'blacklist_source_denied',
  'expired_offer',
  'off_domain_checkout',
  'hard_constraint_unsatisfied',
  'undecidable_hard_constraint',
  'malformed_candidate',
  'offer_price_outside_budget',
  'offer_price_unreadable',
  'organic_result_off_topic',
] as const

/** `apps/exchange/src/eligibility/__init__.py::ELIGIBILITY_STATUSES`. */
const ELIGIBILITY_STATUSES = ['eligible', 'blacklisted', 'unavailable'] as const

/** `packages/contracts/src/ranking.py::RANK_FEATURES`, plus `scoring.py::PENALTY_COMPONENT`. */
const RANK_COMPONENTS = [
  'intent_match',
  'verified_claim_ratio',
  'trust',
  'price_value',
  'delivery_fit',
  'policy_penalties',
] as const

describe('every code the exchange can publish has a sentence', () => {
  it('covers every exclusion prefix the ranker publishes', () => {
    for (const prefix of EXCLUSION_PREFIXES) {
      const said = explainExclusion(prefix)
      expect(said.recognised, prefix).toBe(true)
      expect(said.code, prefix).toBe(prefix)
      // A real sentence, not a restatement of the code: it must end like one and it must not
      // simply be the machine word with a full stop after it.
      expect(said.sentence, prefix).toMatch(/\.$/)
      expect(said.sentence, prefix).not.toContain(prefix)
    }
  })

  it('covers all three eligibility statuses, including the one that should never be denied', () => {
    for (const status of ELIGIBILITY_STATUSES) {
      expect(explainDenial(status).recognised, status).toBe(true)
    }
    // `eligible` under a "Denied entry" heading is the record contradicting itself, and the
    // sentence says that rather than reading as a denial.
    expect(explainDenial('eligible').sentence).toContain('disagreeing with itself')
    // AND `blacklisted` MAY NOT CALL ITSELF A TRUST VERDICT. Under `StaticSellerEligibility`
    // it comes from a hand-typed map, and `composition.py` complains about precisely this
    // wording: "a sentence that reads like a trust verdict and is not one".
    expect(explainDenial('blacklisted').sentence).not.toContain('trust record')
    expect(explainDenial('blacklisted').sentence).toContain('eligibility gate')
  })

  /*
   * PINNED PAIRWISE, not "each key has some name" — which is what this asserted first, and
   * which is worthless. Swapping `intent_match`'s text with `trust`'s makes the page tell a
   * shopper each figure is the other one, and the weaker assertion stayed green through it.
   * That is not hypothetical: the first version of this vocabulary described `price_value` as
   * "its price against the rest of this auction" when `features.py` says in so many words that
   * the term is "rival-independent", and described `verified_claim_ratio` with the v1.0.0
   * definition the contract package has since versioned away from. Both shipped green.
   *
   * Each expectation below names the producer it was checked against. A rename that is a real
   * improvement costs one line here; a swap costs five and is impossible to make by accident.
   */
  it('names every term of the published rank formula, and names the RIGHT one', () => {
    for (const key of RANK_COMPONENTS) {
      expect(componentIsNamed(key), key).toBe(true)
      expect(componentName(key), key).not.toBe(key)
    }

    // `features.py::intent_match` reads the candidate record against the intent. It is NOT
    // the `fit_score` a shortlist card prints, so the wording deliberately differs.
    expect(componentName('intent_match')).toBe(
      'how the platform’s record of it lines up with what you asked for',
    )
    // `features.py::verified_claim_ratio` — a noisy-OR over verified claims whose subject the
    // buyer asked about. NOT the raw share of decided claims, which was v1.0.0.
    expect(componentName('verified_claim_ratio')).toBe(
      'verified evidence about the things you actually asked about',
    )
    expect(componentName('trust')).toBe('the shop’s standing trust score')
    // `features.py`: "reads only the store's own two numbers, so it is rival-independent".
    // The auction only supplies a saturation scale, and it drops out when absent.
    expect(componentName('price_value')).toBe('how deeply the shop discounted its own list price')
    // ...and THIS is the min-max-normalised-across-the-auction one.
    expect(componentName('delivery_fit')).toBe(
      'how its delivery promise compares with the others in this auction',
    )
    // `DEFAULT_PENALTIES_PER_KIND` holds TWO kinds and the heavier is the schema default, so
    // the name may not commit to one of them.
    expect(componentName('policy_penalties')).toContain('penalties')
    expect(componentName('policy_penalties')).not.toContain('contradicting')

    // The two terms most easily confused must not read as each other.
    expect(componentName('price_value')).not.toContain('rest of this auction')
    expect(componentName('delivery_fit')).toContain('others in this auction')
  })

  it('says what a component IS, because a contribution is not a mark out of one', () => {
    // `scoring.py` publishes `weight * feature`, so `intent_match` cannot exceed 0.35 and
    // `delivery_fit` cannot exceed 0.15. A page calling any of them "out of 1" is asserting a
    // scale nothing produced.
    expect(COMPONENTS_ARE).toContain('not a mark out of 1')
    expect(COMPONENTS_ARE).toContain('weight')
  })

  it('names the three live-check outcomes, and keeps no-verdict apart from contradicted', () => {
    for (const outcome of ['agrees', 'contradicted', 'no_verdict']) {
      expect(explainOutcome(outcome).recognised, outcome).toBe(true)
    }
    // The three are three answers, and `no_verdict` is the one most easily collapsed into
    // either of the others: it is neither "the page agreed" nor "the page disagreed", and a
    // panel that read it as either would be claiming a verdict the ledger does not hold.
    expect(explainOutcome('no_verdict').sentence).toContain('reached no conclusion')
    // AND IT MAY NOT CLAIM A FETCH. `composition.py` defaults `live_page_fetcher` to "off",
    // `livecheck/routes.py` binds `NoPageFetcher`, and `fetching.py` returns `status=0` with
    // no socket opened — and nothing in this tree turns it on. A sentence saying "the page was
    // fetched" is false on every configuration this repo ships, and it shipped that way.
    expect(explainOutcome('no_verdict').sentence).not.toContain('fetched')
    expect(explainOutcome('no_verdict').sentence).not.toContain('page was read')
    expect(explainOutcome('no_verdict').sentence).not.toBe(explainOutcome('agrees').sentence)
    expect(explainOutcome('no_verdict').sentence).not.toBe(
      explainOutcome('contradicted').sentence,
    )
  })

  it('gives every reading state a claim rather than a status word', () => {
    for (const state of ['idle', 'loading', 'ok', 'unauthorized', 'absent', 'failed', 'unreachable']) {
      expect(explainState(state).recognised, state).toBe(true)
    }
    // The distinction the whole `Reading` union exists to keep. These two look identical to a
    // reader who has not read `telemetry.ts`, and they are not the same answer.
    expect(explainState('absent').sentence).toContain('no record')
    expect(explainState('failed').sentence).toContain('could not read')
  })
})

describe('an unrecognised code is said to be unrecognised, never dressed up', () => {
  it('names the word it could not explain, so the sentence is not a dangling clause', () => {
    const said = explainExclusion('quantum_flux_denied')
    expect(said.recognised).toBe(false)
    expect(said.code).toBe('quantum_flux_denied')
    expect(said.sentence).toContain('quantum_flux_denied')
    expect(said.sentence).toContain(UNRECOGNISED_SENTENCE)
  })

  it('echoes a component key it does not know rather than prettifying it', () => {
    // Deliberate: a key nobody has a name for is a real thing to notice during a demo, and
    // turning `some_new_term` into "Some New Term" would hide exactly that.
    expect(componentName('some_new_term')).toBe('some_new_term')
    expect(componentIsNamed('some_new_term')).toBe(false)
  })

  it('does not recognise `fit`, which is not a term of the formula', () => {
    // `packages/contracts/tests/ranking.test.ts` asserts `mapped["fit"]` is undefined and says
    // in so many words that "`fit` and `offer_value` are not terms". It appears in this app's
    // metrics fixtures anyway, so this states what it is: a key nothing publishes, and
    // therefore one this module must not silently claim to know.
    expect(componentIsNamed('fit')).toBe(false)
    expect(componentIsNamed('offer_value')).toBe(false)
  })
})

describe('the overflow notice is not treated as a refusal', () => {
  // `auction/routes.py::_exclusion_reasons_out` caps a candidate's list at 8 and appends this
  // line. Split on its first colon it looked like an unrecognised code, and the unrecognised
  // sentence then told a reader "nothing has been hidden" about the one string in the
  // vocabulary whose entire job is to say something was.
  const OVERFLOW =
    '... and 3 further exclusion reason(s) not reported: this candidate failed 11 checks and ' +
    'the response reports the first 8'

  it('says it is a cap on the report rather than a judgement about the candidate', () => {
    const said = explainExclusion(OVERFLOW)
    expect(said.recognised).toBe(true)
    expect(said.sentence).toContain('not a reason')
    expect(said.sentence).toContain('capped the list')
  })

  it('never tells a reader nothing was hidden by the line that says something was', () => {
    expect(explainExclusion(OVERFLOW).sentence).not.toContain('nothing has been hidden')
    expect(explainExclusion(OVERFLOW).sentence).not.toBe(UNRECOGNISED_SENTENCE)
  })
})

describe('the code survives the sentence', () => {
  it('splits on the FIRST colon, so a detail carrying colons is not mangled', () => {
    // MEASURED on the devstack: this is a real string off `GET /buyer/auctions/{id}`.
    const said = explainExclusion(
      "hard_constraint_unsatisfied: 'material': the candidate carries no such attribute (R19)",
    )
    expect(said.code).toBe('hard_constraint_unsatisfied')
    expect(said.detail).toBe(
      "'material': the candidate carries no such attribute (R19)",
    )
  })

  it('reports no detail as empty rather than as the code repeated', () => {
    const said = explainExclusion('blacklisted_store')
    expect(said.detail).toBe('')
    expect(said.code).toBe('blacklisted_store')
  })
})

describe('a rounded figure is a reading form, never the value', () => {
  it('rounds to three places for reading aloud', () => {
    expect(roundedScore(0.18785000000000002)).toBe('0.188')
    expect(roundedScore(0.05)).toBe('0.050')
  })

  it('turns an absent score into an absent string, never a zero', () => {
    // The defect this whole app has spent a dozen commits closing, and the one line where it
    // would be easiest to reintroduce: a defaulted `0.000` reads as the exchange having
    // scored the candidate at the bottom.
    expect(roundedScore(undefined)).toBeUndefined()
    expect(roundedScore(Number.NaN)).toBeUndefined()
    expect(roundedScore(Number.POSITIVE_INFINITY)).toBeUndefined()
    // A real, measured zero still prints. It is a value, not an absence.
    expect(roundedScore(0)).toBe('0.000')
  })

  it('says when rounding has actually thrown digits away', () => {
    expect(roundingHidesDigits(0.18785000000000002)).toBe(true)
    expect(roundingHidesDigits(0.05)).toBe(false)
    expect(roundingHidesDigits(undefined)).toBe(false)
  })
})
