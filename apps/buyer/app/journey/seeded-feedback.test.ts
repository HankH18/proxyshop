/**
 * The marker check that makes the journey's Step 5 seed data rather than fake data.
 *
 * `seeded-feedback.ts` decides whether the manufactured prompt in `app/feedback/seed-data/` may
 * be rendered at all. Its whole claim is that the check is ENFORCED rather than decorative: a
 * prompt is shown only when the body's `order_ref` carries the prefix that the artifact's own
 * `collection.json` declares about itself, and anything else disappears rather than rendering
 * unlabelled. This suite is that claim, taken adversarially.
 *
 * WHAT IT ESTABLISHES:
 *
 *  * A well-formed record and body are accepted, and the accepted value is the body itself —
 *    nothing is composed, relabelled or defaulted on the way through.
 *  * A body whose `order_ref` does NOT carry the declared prefix is refused, and the refusal
 *    names the offending reference. This is the test that separates an enforced marker from a
 *    comment claiming one: without it, `readSeededPrompt` could return every body it is handed
 *    and every other assertion here would still pass.
 *  * The prefix is read OUT OF the record rather than hard-coded, so a record declaring some
 *    other prefix is enforced against that one — including refusing `sim-fb-`, the prefix this
 *    repository's real artifact happens to use.
 *  * Every malformed shape (no marker, empty prefix, wrong field, non-object record, non-object
 *    body, non-string `order_ref`, a reference that merely CONTAINS the prefix) is refused with
 *    a reason and never by throwing. A broken artifact must cost the demo one panel, not the
 *    page.
 *  * The artifact actually checked into this repository passes its own check.
 *
 * WHAT IT DELIBERATELY DOES NOT ESTABLISH:
 *
 *  * That the seeded `order_ref` reaches the trust ledger's hash chain. That is the buyer
 *    service's doing — `apps/buyer/svc/src/feedback/submission.py` copies the field verbatim
 *    onto the sealed event — and it is proven by the Python suite. Nothing in this module can
 *    observe it, and a test here that claimed to would be asserting a comment.
 *  * That `services/sim/seed/shoppers.py` mints the prefix, or that `seed.population` refuses to
 *    submit without it. Same reason: those are the writer's guarantees, tested where they live.
 *  * Anything about how the panel LOOKS. `readSeededPrompt` is a pure function over two JSON
 *    values; the rendering of its result is `journey.test.tsx`'s business.
 */
import { describe, expect, it } from 'vitest'

import { SEEDED_PREFIX, SEEDED_PROMPT, SEEDED_REFUSAL, readSeededPrompt } from './seeded-feedback'

/** A `collection.json` that declares the marker the buyer service actually seals. */
const MARKED = { marker: { field: 'order_ref', prefix: 'sim-fb-' } }

/**
 * A `prompt.json` in the shape `feedback_prompt` publishes. Only `order_ref` is read by
 * `readSeededPrompt`; the rest is here so the accepted value is recognisably a whole prompt
 * rather than a stub that would pass a weaker check.
 */
const SEEDED_BODY = {
  order_ref: 'sim-fb-a01-01',
  store_id: 'store-northroast',
  auction_id: 'sim-fb-a01-01',
  question_id: 'matched_pitch',
  question: 'Did what arrived match what the store pitched?',
  input_type: 'single_select',
  options: [{ id: 'yes_as_described', label: 'Yes — it was what the store described', matched_pitch: true }],
}

describe('readSeededPrompt — the marker is enforced, not decorative', () => {
  it('accepts a body that carries the prefix its own artifact declares, and returns it unchanged', () => {
    const [prompt, refusal] = readSeededPrompt(MARKED, SEEDED_BODY)

    expect(refusal).toBeUndefined()
    // Identity, not equality: nothing was rebuilt, renamed or defaulted on the way through.
    expect(prompt).toBe(SEEDED_BODY)
  })

  it('REFUSES a body whose order_ref does not carry the declared prefix, and names it', () => {
    // The forgery this whole module exists to stop: a real-looking order reference presented
    // through the seeded panel. If this passed, the panel would render an unmarked prompt and
    // an answer given through it would land on the ledger indistinguishable from an earned one.
    const forged = { ...SEEDED_BODY, order_ref: 'ord-real-9001' }

    const [prompt, refusal] = readSeededPrompt(MARKED, forged)

    expect(prompt).toBeNull()
    expect(refusal).toBeTypeOf('string')
    // The reason must name the offending reference — a bare "refused" tells a reader looking at
    // the page nothing about which value was wrong.
    expect(refusal).toContain('ord-real-9001')
    expect(refusal).toContain('sim-fb-')
  })

  it('reads the prefix OUT OF the record, so a differently-marked artifact is enforced as written', () => {
    // Two assertions in one test on purpose: they are the same fact seen from both sides. A
    // module that hard-coded 'sim-fb-' would fail the first; one that accepted anything would
    // fail the second.
    const declaresDemo = { marker: { field: 'order_ref', prefix: 'demo-' } }

    const [accepted, noRefusal] = readSeededPrompt(declaresDemo, {
      ...SEEDED_BODY,
      order_ref: 'demo-77',
    })
    expect(noRefusal).toBeUndefined()
    expect(accepted?.order_ref).toBe('demo-77')

    const [refused, why] = readSeededPrompt(declaresDemo, SEEDED_BODY)
    expect(refused).toBeNull()
    expect(why).toContain('demo-')
  })

  it('refuses a record that marks any field other than order_ref', () => {
    // WHY THIS IS A REFUSAL AND NOT A GENERALISATION. `order_ref` is the ONLY field the buyer
    // service copies verbatim onto the sealed ledger event, beside `event_hash` and `prev_hash`
    // (`apps/buyer/svc/src/feedback/submission.py`). A marker on `store_id`, on `auction_id`, or
    // on some field a future artifact invents would be checkable here and absent from the hash
    // chain — so honouring it would move the check off the one field that is sealed, and the
    // seeded answer could then be un-marked without breaking `GET /events/verify`. Refusing is
    // the only reading that keeps "you can tell seeded from earned, forever" true.
    const marksStore = { marker: { field: 'store_id', prefix: 'sim-fb-' } }

    const [prompt, refusal] = readSeededPrompt(marksStore, {
      ...SEEDED_BODY,
      store_id: 'sim-fb-store',
    })

    expect(prompt).toBeNull()
    expect(refusal).toContain('store_id')
    expect(refusal).toContain('order_ref')
  })

  it('refuses an order_ref that merely CONTAINS the prefix instead of starting with it', () => {
    // `'ord-sim-fb-1'` is the shape a forgery takes once someone knows what the check looks
    // for: a real reference with the marker buried inside it. `startsWith`, not `includes`.
    const [prompt, refusal] = readSeededPrompt(MARKED, {
      ...SEEDED_BODY,
      order_ref: 'ord-sim-fb-1',
    })

    expect(prompt).toBeNull()
    expect(refusal).toContain('ord-sim-fb-1')
  })

  it('refuses a record with no marker at all, because nothing about it can be checked', () => {
    const [prompt, refusal] = readSeededPrompt({ kind: 'something-else' }, SEEDED_BODY)

    expect(prompt).toBeNull()
    expect(refusal).toContain('marker')
  })

  it('refuses a marker whose prefix is empty, which would match every reference there is', () => {
    const [prompt, refusal] = readSeededPrompt(
      { marker: { field: 'order_ref', prefix: '' } },
      SEEDED_BODY,
    )

    expect(prompt).toBeNull()
    // An empty prefix is `startsWith('')`, which is true of every string — so it is not a
    // weaker marker, it is no marker, and it is refused as one.
    expect(refusal).toContain('marker')
  })

  it('refuses a marker whose field is empty or not a string', () => {
    for (const marker of [{ field: '', prefix: 'sim-fb-' }, { field: 7, prefix: 'sim-fb-' }]) {
      const [prompt, refusal] = readSeededPrompt({ marker }, SEEDED_BODY)
      expect(prompt).toBeNull()
      expect(refusal).toContain('marker')
    }
  })

  it('refuses a record that is not an object', () => {
    // `undefined` is deliberately absent from this list: it is the default parameter, so
    // `readSeededPrompt(undefined, …)` reads the REAL artifact and is a different test.
    for (const record of [null, 'sim-fb-', 42, true, Symbol('marker')]) {
      const [prompt, refusal] = readSeededPrompt(record, SEEDED_BODY)
      expect(prompt).toBeNull()
      expect(refusal).toContain('marker')
    }
  })

  it('refuses an array as a record, even though typeof says object', () => {
    const [prompt, refusal] = readSeededPrompt([{ field: 'order_ref', prefix: 'sim-fb-' }], SEEDED_BODY)

    expect(prompt).toBeNull()
    expect(refusal).toContain('marker')
  })

  it('refuses a body that is not an object', () => {
    // Again no `undefined`: that is the default parameter and reads the real artifact.
    for (const body of [null, 'sim-fb-a01-01', 0, false]) {
      const [prompt, refusal] = readSeededPrompt(MARKED, body)
      expect(prompt).toBeNull()
      expect(refusal).toBeTypeOf('string')
    }
  })

  it('refuses an order_ref that is not a string, however prefix-shaped it looks', () => {
    for (const orderRef of [
      undefined,
      null,
      42,
      ['sim-fb-a01-01'],
      { toString: () => 'sim-fb-a01-01' },
      { startsWith: () => true },
    ]) {
      const [prompt, refusal] = readSeededPrompt(MARKED, { ...SEEDED_BODY, order_ref: orderRef })
      expect(prompt).toBeNull()
      expect(refusal).toBeTypeOf('string')
    }
  })

  it('never throws, for any malformed shape above or below', () => {
    const shapes: ReadonlyArray<readonly [unknown, unknown]> = [
      [null, null],
      [undefined, null],
      [{}, {}],
      [{ marker: null }, {}],
      [{ marker: 'order_ref' }, {}],
      [{ marker: [] }, {}],
      [{ marker: { field: 'order_ref' } }, {}],
      [{ marker: { prefix: 'sim-fb-' } }, {}],
      [MARKED, []],
      [MARKED, { order_ref: Symbol('sim-fb-') }],
      [MARKED, Object.create(null) as unknown],
      ['', ''],
      [0, 0],
      [NaN, NaN],
    ]

    for (const [record, body] of shapes) {
      expect(() => readSeededPrompt(record, body)).not.toThrow()
      // And every one of them refuses rather than quietly returning something. `[undefined, …]`
      // falls back to the real artifact for the RECORD, but the body beside it is malformed, so
      // that row must refuse too.
      const [prompt, refusal] = readSeededPrompt(record, body)
      expect(prompt).toBeNull()
      expect(refusal).toBeTypeOf('string')
      expect(refusal).not.toBe('')
    }
  })
})

describe('the artifact this repository actually ships', () => {
  it('passes its own marker check, so the journey renders a prompt rather than a refusal', () => {
    // Not a constructed record: the checked-in `app/feedback/seed-data/` pair, read through the
    // module's own defaults. If the artifact is ever regenerated with a reference that does not
    // carry its declared prefix, this fails here rather than showing an unmarked prompt on the
    // page.
    expect(SEEDED_REFUSAL).toBeUndefined()
    expect(SEEDED_PROMPT).not.toBeNull()
    expect(SEEDED_PROMPT?.order_ref).toBeTypeOf('string')
    expect(SEEDED_PROMPT?.order_ref.startsWith(SEEDED_PREFIX)).toBe(true)
  })

  it('declares a real prefix rather than the placeholder that stands in for a missing one', () => {
    expect(SEEDED_PREFIX).not.toBe('(none declared)')
    expect(SEEDED_PREFIX).not.toBe('')
  })

  it('carries the whole prompt the buyer service publishes, not just the marked reference', () => {
    // A body that satisfied the marker and carried nothing else would pass every assertion
    // above while rendering an empty form.
    expect(SEEDED_PROMPT?.question).toBeTypeOf('string')
    expect(SEEDED_PROMPT?.question).not.toBe('')
    expect(SEEDED_PROMPT?.options).toHaveLength(5)
    expect(SEEDED_PROMPT?.options.map((option) => option.id)).toEqual([
      'yes_as_described',
      'as_described_but_late',
      'not_as_described',
      'wrong_item',
      'never_arrived',
    ])
  })

  it('is accepted by the same function, called explicitly on the same artifact', () => {
    // `SEEDED_PROMPT` is computed at import time. This re-runs the check on the module's own
    // defaults so a future edit that froze the constant while breaking the function is caught.
    const [prompt, refusal] = readSeededPrompt()

    expect(refusal).toBeUndefined()
    expect(prompt).toEqual(SEEDED_PROMPT)
  })
})
