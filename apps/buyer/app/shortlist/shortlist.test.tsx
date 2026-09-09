/**
 * T-072 — the client half of R2 and R3.
 *
 * The Python suite proves the *service* labels provenance correctly and follows the
 * exchange's permalink. This one proves the browser does not undo either: every slot's
 * labels are on the screen as the exchange spelled them, there is no `href` for a decoy
 * `checkout_url` to end up in, and the redirect is refused for every spoof shape that
 * defeats a weaker host comparison — checked in the process that would do the navigating.
 *
 * Interaction is driven with `fireEvent` rather than `user-event`, which this workspace
 * does not carry — same as `intent/intent-confirm.test.tsx`.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ShortlistView } from './ShortlistView'
import {
  ACCEPT_PATH,
  FAN_OUT_CAPACITY_FALLBACK_FAMILY,
  RecordedAuctionUnreadable,
  LABEL_FROM_THEIR_WEBSITE,
  LABEL_STORE_CONFIRMED,
  LABEL_UNVERIFIED,
  MissingAuctionReferenceError,
  NEVER_ASKED_FALLBACK_FAMILIES,
  NO_AGENT_FALLBACK_FAMILY,
  NoPermalinkError,
  UnsafePermalinkError,
  acceptSlot,
  askedAndSilent,
  assertFollowable,
  fallbackReasonFamily,
  followPermalink,
  labelTone,
  loadRecordedAuction,
  neverAsked,
  permalinkRefusal,
  readRecordedAuction,
  slotLabels,
  type AcceptOutcome,
  type Fetcher,
  type Shortlist,
  type ShortlistSlot,
} from './shortlist'

// `@testing-library/react` registers its own cleanup only when the runner exposes
// `afterEach` globally, and this workspace's vitest projects do not set `globals: true`.
afterEach(cleanup)

const STORE = 'store-x.example.com'
const PERMALINK = `https://${STORE}/cart/44352913:1?discount=PS-ABC123`

const SLOT: ShortlistSlot = {
  slot: 'fit',
  bid_ref: 'bid-e7-7',
  auction_id: 'auc-e7-3',
  fit_score: 0.91,
  trust_summary: { score: 0.72, confidence: 0.4 },
  provenance_labels: [LABEL_STORE_CONFIRMED],
}

const SHORTLIST: Shortlist = {
  auction_id: 'auc-e7-3',
  slots: [
    SLOT,
    {
      slot: 'value',
      bid_ref: 'bid-e7-8',
      auction_id: 'auc-e7-3',
      fit_score: 0.77,
      trust_summary: { score: 0.55 },
      provenance_labels: [LABEL_FROM_THEIR_WEBSITE],
    },
  ],
}

const OUTCOME: AcceptOutcome = {
  permalink_url: PERMALINK,
  auction_id: 'auc-e7-3',
  bid_ref: 'bid-e7-7',
  slot: 'fit',
  accepted_at: '2026-01-01T00:00:00Z',
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

describe('provenance labels on the screen', () => {
  it('shows every slots labels as the exchange spelled them', () => {
    render(<ShortlistView shortlist={SHORTLIST} onAccept={vi.fn()} />)
    expect(screen.getByLabelText('Where this came from: bid-e7-7')).toHaveTextContent(
      'store-confirmed',
    )
    expect(screen.getByLabelText('Where this came from: bid-e7-8')).toHaveTextContent(
      'from their website',
    )
  })

  it('never re-labels a seller-asserted claim as store-confirmed', () => {
    render(
      <ShortlistView
        shortlist={{
          auction_id: 'a',
          slots: [{ ...SLOT, provenance_labels: [LABEL_UNVERIFIED] }],
        }}
        onAccept={vi.fn()}
      />,
    )
    const labels = screen.getByLabelText('Where this came from: bid-e7-7')
    expect(labels).toHaveTextContent('unverified')
    expect(labels).not.toHaveTextContent('store-confirmed')
    expect(labels).not.toHaveTextContent('from their website')
  })

  it('renders a label it has never heard of rather than hiding it', () => {
    render(
      <ShortlistView
        shortlist={{ auction_id: 'a', slots: [{ ...SLOT, provenance_labels: ['newly minted'] }] }}
        onAccept={vi.fn()}
      />,
    )
    expect(screen.getByTestId('label-bid-e7-7')).toHaveTextContent('newly minted')
    expect(labelTone('newly minted')).toBe('unknown')
  })

  it('reads as unverified rather than as nothing when the exchange sent no labels', () => {
    expect(slotLabels({ provenance_labels: [] })).toEqual([LABEL_UNVERIFIED])
    expect(slotLabels({ provenance_labels: ['  ', ''] })).toEqual([LABEL_UNVERIFIED])
  })

  it('de-duplicates without reordering what the exchange sent', () => {
    expect(
      slotLabels({
        provenance_labels: [LABEL_FROM_THEIR_WEBSITE, LABEL_STORE_CONFIRMED, 'from their website'],
      }),
    ).toEqual([LABEL_FROM_THEIR_WEBSITE, LABEL_STORE_CONFIRMED])
  })

  it('gives each known label a tone and every unknown one the same fallback', () => {
    expect(labelTone(LABEL_STORE_CONFIRMED)).toBe('confirmed')
    expect(labelTone(LABEL_FROM_THEIR_WEBSITE)).toBe('observed')
    expect(labelTone(LABEL_UNVERIFIED)).toBe('unverified')
  })
})

describe('the shortlist itself', () => {
  it('collapses rather than pads', () => {
    render(<ShortlistView shortlist={{ auction_id: 'a', slots: [SLOT] }} onAccept={vi.fn()} />)
    expect(screen.getByTestId('slot-count')).toHaveTextContent('1 option')
    expect(screen.getAllByRole('button', { name: /accept/i })).toHaveLength(1)
  })

  it('says so rather than showing an empty list when nothing was eligible', () => {
    render(<ShortlistView shortlist={{ auction_id: 'a', slots: [] }} onAccept={vi.fn()} />)
    expect(screen.getByLabelText('Shortlist')).toHaveTextContent('Nothing has been ordered')
    expect(screen.queryByRole('button', { name: /accept/i })).not.toBeInTheDocument()
  })

  it('offers no link a decoy checkout_url could hide in', () => {
    const { container } = render(
      <ShortlistView
        shortlist={{
          auction_id: 'a',
          slots: [{ ...SLOT, ...{ checkout_url: 'https://attacker.example/cart/1:1' } }],
        }}
        onAccept={vi.fn()}
      />,
    )
    expect(container.querySelectorAll('a')).toHaveLength(0)
    expect(container.innerHTML).not.toContain('attacker.example')
  })

  it('accepts exactly once however many times the button is clicked', async () => {
    const onAccept = vi.fn()
    render(<ShortlistView shortlist={SHORTLIST} onAccept={onAccept} />)
    const button = screen.getAllByRole('button', { name: /accept/i })[0]!
    fireEvent.click(button)
    fireEvent.click(button)
    fireEvent.click(button)
    await waitFor(() => expect(onAccept).toHaveBeenCalledTimes(1))
    expect(onAccept).toHaveBeenCalledWith(SLOT)
  })

  it('names the host the buyer is going to, and says who chose it', () => {
    render(<ShortlistView shortlist={SHORTLIST} onAccept={vi.fn()} accepted={OUTCOME} />)
    const line = screen.getByTestId('checkout-destination')
    expect(line).toHaveTextContent(STORE)
    expect(line).toHaveTextContent('We did not choose that address')
  })
})

describe('the wire', () => {
  it('posts the accept and returns the permalink unmodified', async () => {
    const seen: Array<{ path: string; body: unknown }> = []
    const fetcher: Fetcher = async (input, init) => {
      seen.push({ path: input, body: JSON.parse(String(init?.body)) as unknown })
      return jsonResponse(OUTCOME)
    }
    const outcome = await acceptSlot(SLOT, fetcher)
    expect(outcome.permalink_url).toBe(PERMALINK)
    expect(seen).toHaveLength(1)
    expect(seen[0]!.path).toBe(ACCEPT_PATH)
  })

  it('never sends the slots own checkout_url back out', async () => {
    const bodies: string[] = []
    const fetcher: Fetcher = async (_input, init) => {
      bodies.push(String(init?.body))
      return jsonResponse(OUTCOME)
    }
    await acceptSlot({ ...SLOT, ...{ checkout_url: 'https://attacker.example/c' } }, fetcher)
    expect(bodies[0]).not.toContain('attacker.example')
    expect(bodies[0]).not.toContain('checkout_url')
  })

  it('refuses an accept for a slot with no auction id, and sends nothing', async () => {
    const fetcher = vi.fn<Fetcher>()
    await expect(acceptSlot({ ...SLOT, auction_id: '' }, fetcher)).rejects.toBeInstanceOf(
      MissingAuctionReferenceError,
    )
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('refuses an accept for a slot with no bid ref, and sends nothing', async () => {
    const fetcher = vi.fn<Fetcher>()
    await expect(acceptSlot({ ...SLOT, bid_ref: '' }, fetcher)).rejects.toBeInstanceOf(
      MissingAuctionReferenceError,
    )
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('refuses a successful accept that carried no permalink', async () => {
    const fetcher: Fetcher = async () => jsonResponse({ auction_id: 'auc-e7-3' })
    await expect(acceptSlot(SLOT, fetcher)).rejects.toBeInstanceOf(NoPermalinkError)
  })

  it('refuses a permalink off the store domain the buyer was shown', async () => {
    const fetcher: Fetcher = async () =>
      jsonResponse({ ...OUTCOME, permalink_url: 'https://store-x.example.com.evil.tld/c' })
    await expect(
      acceptSlot({ ...SLOT, store_domain: STORE }, fetcher),
    ).rejects.toBeInstanceOf(UnsafePermalinkError)
  })
})

describe('the redirect, attacked as a redirect', () => {
  const spoofs: ReadonlyArray<readonly [string, string]> = [
    ['rival domain', 'https://evil.example/cart/1:1'],
    ['suffix spoof', 'https://store-x.example.com.evil.example.com/c'],
    ['glued prefix', 'https://evil-store-x.example.com/c'],
    ['userinfo spoof', 'https://store-x.example.com:8443@evil.example.com/c'],
    ['subdomain', 'https://checkout.store-x.example.com/c'],
    ['uppercase suffix spoof', 'https://STORE-X.EXAMPLE.COM.evil.tld/c'],
  ]

  for (const [name, spoofed] of spoofs) {
    it(`refuses a ${name}`, () => {
      expect(permalinkRefusal(spoofed, STORE)).toBeDefined()
      expect(() => assertFollowable(spoofed, STORE)).toThrow(UnsafePermalinkError)
    })
  }

  const unsafe: ReadonlyArray<readonly [string, string]> = [
    ['javascript scheme', 'javascript:alert(document.cookie)'],
    ['data scheme', 'data:text/html,<script>1</script>'],
    ['protocol-relative', '//evil.example/cart/1:1'],
    ['ftp scheme', 'ftp://store-x.example.com/c'],
    ['empty authority', 'https:///cart/1:1'],
    ['whitespace only', '   '],
    ['padded', `  ${PERMALINK}  `],
    ['newline smuggle', 'https://store-x.example.com\n@evil.example/c'],
    ['tab smuggle', 'https://store-x.example.com\t@evil.example/c'],
  ]

  for (const [name, hostile] of unsafe) {
    it(`refuses a ${name} on its own shape, with no domain to compare against`, () => {
      expect(permalinkRefusal(hostile)).toBeDefined()
      expect(() => assertFollowable(hostile)).toThrow(UnsafePermalinkError)
    })
  }

  it('lets the stores own permalink through — the control case', () => {
    expect(permalinkRefusal(PERMALINK, STORE)).toBeUndefined()
    expect(assertFollowable(PERMALINK, STORE)).toBe(PERMALINK)
    expect(assertFollowable(PERMALINK, 'STORE-X.Example.Com.')).toBe(PERMALINK)
  })

  it('shows why the newline smuggle needs its own rule', () => {
    const smuggled = 'https://store-x.example.com\n@evil.example/c'
    expect(new URL(smuggled).hostname).toBe('evil.example')
    expect(smuggled).toContain(STORE)
  })

  it('shows why an empty authority needs its own rule: the two parsers disagree', () => {
    // The browser resolves `https:///cart/1:1` to the host `cart`. Python's `urlsplit` —
    // what the buyer service and the exchange check with — reads no host at all. A URL the
    // two halves of this system read differently is refused rather than followed.
    expect(new URL('https:///cart/1:1').hostname).toBe('cart')
    expect(permalinkRefusal('https:///cart/1:1')).toContain('authority')
  })

  it('navigates to exactly the permalink and nowhere else', () => {
    const went: string[] = []
    expect(followPermalink(OUTCOME, STORE, (url) => went.push(url))).toBe(PERMALINK)
    expect(went).toEqual([PERMALINK])
  })

  it('navigates nowhere at all when the permalink is refused', () => {
    const went: string[] = []
    expect(() =>
      followPermalink({ ...OUTCOME, permalink_url: 'javascript:alert(1)' }, STORE, (url) =>
        went.push(url),
      ),
    ).toThrow(UnsafePermalinkError)
    expect(went).toEqual([])
  })
})

/**
 * R2's other three things on the slot — PRODUCT, PRICE, COMMITMENTS — on the screen.
 *
 * The exchange publishes all three and the buyer service forwards them; these tests are
 * about the last hop, where a person actually reads them. Every assertion is on rendered
 * text rather than on a prop, because "the component received a price" and "a shopper can
 * see a price" have been different things on this screen before.
 *
 * Half of them drive a MISSING field. A fallback bid carries no commitments and a roster row
 * with no readable list price carries no price; absence is the ordinary case, and the thing
 * it must never render as is `undefined`, `0`, or a blank.
 */
const PRICED_SLOT: ShortlistSlot = {
  ...SLOT,
  product: { product_ref: 'prod-merino-crew', variant_ref: 'var-m-navy' },
  price: {
    unit_price: 78,
    total_price: 156,
    currency: 'USD',
    // `'percentage'`, which is what `store_agent.runtime.bidding.PERCENTAGE` actually emits.
    // The fixture said `'percent'` — a spelling no producer in this tree produces — so the
    // rendering branch it was meant to cover had never run under test either.
    discount: { type: 'percentage', value: 10 },
    expires_at: '2026-09-06T12:00:00Z',
  },
  commitments: [
    { key: 'free_returns', value: true, label: LABEL_STORE_CONFIRMED },
    { key: 'ships_in_days', value: 2, unit: 'days', label: LABEL_FROM_THEIR_WEBSITE },
  ],
}

function one(slot: ShortlistSlot): Shortlist {
  return { auction_id: 'auc-e7-3', slots: [slot] }
}

describe('what the shopper can read on a slot', () => {
  it('shows what the thing is', () => {
    render(<ShortlistView shortlist={one(PRICED_SLOT)} onAccept={vi.fn()} />)
    const product = screen.getByTestId('product-bid-e7-7').textContent ?? ''
    expect(product).toContain('prod-merino-crew')
    expect(product).toContain('var-m-navy')
  })

  it('names whose shop it is, with the host the accept will be pinned against', () => {
    render(
      <ShortlistView
        shortlist={one({ ...PRICED_SLOT, store_domain: STORE })}
        onAccept={vi.fn()}
      />,
    )
    // The exchange's own field, printed. It is the platform REGISTRY's answer rather than
    // anything the store put on its bid, and it is the string `permalinkRefusal` pins the
    // minted permalink's host against — so a shopper reading it is reading where Accept will
    // send them, before they press it rather than on the handoff screen afterwards.
    expect(screen.getByTestId('store-domain-bid-e7-7').textContent).toBe(STORE)
    // A host on a card is still not a destination. There is no `href` in this component and
    // this line is not the exception one could arrive through.
    expect(screen.queryAllByRole('link')).toHaveLength(0)
  })

  it('says the exchange named no domain rather than leaving the store nameless', () => {
    // `store_domain` is `str | None` on `contracts.protocol.ShortlistSlot` and the exchange
    // publishes `None` — never `''` — when it has no platform registry to answer from. That
    // is a deployment saying it vouches for no host, which is a different sentence from a
    // store that has none, and either way the card may not go quiet about who is offering.
    render(<ShortlistView shortlist={one(PRICED_SLOT)} onAccept={vi.fn()} />)
    const absent = screen.getByTestId('store-domain-bid-e7-7').textContent ?? ''
    expect(absent).toContain('named no domain')
    expect(absent).not.toContain('undefined')
  })

  it('shows what it costs, with the currency the exchange named and no invented symbol', () => {
    render(<ShortlistView shortlist={one(PRICED_SLOT)} onAccept={vi.fn()} />)
    const price = screen.getByTestId('price-bid-e7-7').textContent ?? ''
    expect(price).toContain('USD')
    expect(price).toContain('156')
    expect(price).toContain('78')
    expect(price).not.toContain('$')
    expect(price).not.toContain('undefined')
  })

  it('calls the discount a stated one, because no code exists until the buyer accepts', () => {
    render(<ShortlistView shortlist={one(PRICED_SLOT)} onAccept={vi.fn()} />)
    const discount = screen.getByTestId('discount-bid-e7-7').textContent ?? ''
    // The DEPTH as a shopper reads it, not merely the digits: asserting `toContain('10')`
    // alone is satisfied by `percentage 10`, which is how this line rendered for every
    // discount ever shown while this test stayed green.
    expect(discount).toContain('10% off')
    expect(discount.toLowerCase()).toContain('states')
  })

  it('says when the quote stops being live', () => {
    render(<ShortlistView shortlist={one(PRICED_SLOT)} onAccept={vi.fn()} />)
    expect(screen.getByTestId('price-expiry-bid-e7-7').textContent).toContain(
      '2026-09-06T12:00:00Z',
    )
  })

  it('shows every commitment with the provenance label for THAT promise', () => {
    render(<ShortlistView shortlist={one(PRICED_SLOT)} onAccept={vi.fn()} />)
    const list = screen.getByTestId('commitments-bid-e7-7').textContent ?? ''
    expect(list).toContain('free returns')
    expect(list).toContain('ships in days')
    expect(list).toContain('2 days')
    const labels = screen.getAllByTestId('commitment-label-bid-e7-7')
    expect(labels.map((node) => node.textContent)).toEqual([
      LABEL_STORE_CONFIRMED,
      LABEL_FROM_THEIR_WEBSITE,
    ])
    // The distinction is what a buyer has instead of having checked themselves, so it is
    // carried as a tone as well as a word — the same `data-tone` the slot labels use.
    expect(labels[0]).toHaveAttribute('data-tone', 'confirmed')
    expect(labels[1]).toHaveAttribute('data-tone', 'observed')
  })

  it('shows a boolean promise as a promise, not as the word true', () => {
    render(<ShortlistView shortlist={one(PRICED_SLOT)} onAccept={vi.fn()} />)
    const list = screen.getByTestId('commitments-bid-e7-7').textContent ?? ''
    expect(list).not.toContain('true')
  })
})

describe('a slot the exchange had nothing for', () => {
  it('says the store quoted no price rather than showing a zero', () => {
    render(<ShortlistView shortlist={one({ ...PRICED_SLOT, price: null })} onAccept={vi.fn()} />)
    const price = screen.getByTestId('price-bid-e7-7').textContent ?? ''
    expect(price).not.toContain('0')
    expect(price).not.toContain('undefined')
    expect(price.toLowerCase()).toContain('no price')
    expect(screen.queryByTestId('discount-bid-e7-7')).toBeNull()
    expect(screen.queryByTestId('price-expiry-bid-e7-7')).toBeNull()
  })

  it('says the exchange named no product rather than rendering an empty line', () => {
    render(<ShortlistView shortlist={one({ ...PRICED_SLOT, product: null })} onAccept={vi.fn()} />)
    const product = screen.getByTestId('product-bid-e7-7').textContent ?? ''
    expect(product.trim().length).toBeGreaterThan(0)
    expect(product).not.toContain('undefined')
    expect(product.toLowerCase()).toContain('did not name')
  })

  it('says a fallback bid promised nothing, and does not read as an error', () => {
    render(
      <ShortlistView shortlist={one({ ...PRICED_SLOT, commitments: null })} onAccept={vi.fn()} />,
    )
    const commitments = screen.getByTestId('commitments-bid-e7-7').textContent ?? ''
    expect(commitments).not.toContain('undefined')
    expect(commitments.toLowerCase()).toContain('no commitments')
    expect(screen.queryAllByTestId('commitment-label-bid-e7-7')).toHaveLength(0)
  })

  it('renders a slot carrying none of the three without dropping the rest of the card', () => {
    render(<ShortlistView shortlist={SHORTLIST} onAccept={vi.fn()} />)
    // The pre-existing fixture has no product, price or commitments on either slot: the
    // shape every producer older than this change still sends. It must still render.
    expect(screen.getByTestId('slot-bid-e7-7')).toBeInTheDocument()
    expect(screen.getByTestId('product-bid-e7-7')).toBeInTheDocument()
    expect(screen.getByTestId('price-bid-e7-7')).toBeInTheDocument()
    expect(screen.getByLabelText('Where this came from: bid-e7-7')).toHaveTextContent(
      'store-confirmed',
    )
  })

  it('shows the variant only when the bid named one', () => {
    render(
      <ShortlistView
        shortlist={one({ ...PRICED_SLOT, product: { product_ref: 'prod-plain' } })}
        onAccept={vi.fn()}
      />,
    )
    const product = screen.getByTestId('product-bid-e7-7').textContent ?? ''
    expect(product).toContain('prod-plain')
    expect(product.toLowerCase()).not.toContain('variant')
  })

  it('prints a price with no currency as the bare number and says the currency is missing', () => {
    render(
      <ShortlistView
        shortlist={one({ ...PRICED_SLOT, price: { unit_price: 40, total_price: 40 } })}
        onAccept={vi.fn()}
      />,
    )
    const price = screen.getByTestId('price-bid-e7-7').textContent ?? ''
    expect(price).toContain('40')
    expect(price).not.toContain('USD')
    expect(price.toLowerCase()).toContain('no currency')
  })

  /**
   * WHAT THE PLATFORM CRAWLED, on the card, attributed (D55) — and the reason these tests
   * replace `journey.test.tsx`'s retired `gap-product-name` bullet rather than merely
   * out-numbering it. That bullet said the card could show only a reference; these assert
   * what it shows instead, so the retirement is a moved claim and not a dropped one.
   *
   * The values below are the shape `POST /buyer/shortlist/render` actually answers with:
   * `buyer_svc.accept.labels._slot_identity` emits all four keys and publishes the object
   * only when `title` AND `source` both read, which is the rule `wire.ts::readIdentity`
   * keeps at this end.
   */
  const CRAWLED = {
    title: 'Milk Thistle Gummies',
    brand: 'Gaia Herbs',
    source: 'neo4j-crawl:gaiaherbs.com:prod-merino-crew',
    observed_at: '2026-09-05T11:02:44Z',
  }

  it('shows the platform’s crawled name instead of the reference, with the brand', () => {
    render(
      <ShortlistView
        shortlist={one({ ...PRICED_SLOT, product: { ...PRICED_SLOT.product!, identity: CRAWLED } })}
        onAccept={vi.fn()}
      />,
    )
    const product = screen.getByTestId('product-bid-e7-7').textContent ?? ''
    expect(product).toContain('Milk Thistle Gummies')
    expect(product).toContain('Gaia Herbs')
    // The reference is not the headline any more — but it is not LOST, see the next test.
    expect(product).not.toContain('prod-merino-crew')
  })

  it('attributes the crawled name to Proxyshop and keeps the reference beside it', () => {
    render(
      <ShortlistView
        shortlist={one({ ...PRICED_SLOT, product: { ...PRICED_SLOT.product!, identity: CRAWLED } })}
        onAccept={vi.fn()}
      />,
    )
    // THE POINT OF THE WHOLE FEATURE. A title with no attribution reads as the shop's own
    // word for its own product, which is the seller's voice wearing the platform's — the
    // exact laundering the two-voice split exists to prevent. So the snapshot the name came
    // out of is on the card, in words, not behind a hover.
    const gloss = screen.getByTestId('product-identity-bid-e7-7').textContent ?? ''
    expect(gloss).toContain('Proxyshop’s, not this shop’s')
    expect(gloss).toContain('neo4j-crawl:gaiaherbs.com:prod-merino-crew')
    expect(gloss).toContain('2026-09-05T11:02:44Z')
    // The reference the accept path resolves against survives the name arriving.
    expect(gloss).toContain('prod-merino-crew')
    expect(gloss).toContain('var-m-navy')
  })

  it('renders a crawled name that carries no brand and no observation time', () => {
    render(
      <ShortlistView
        shortlist={one({
          ...PRICED_SLOT,
          product: {
            product_ref: 'prod-plain',
            identity: { title: 'Milk Thistle Liver Support', source: 'op-doc:oregonswildharvest' },
          },
        })}
        onAccept={vi.fn()}
      />,
    )
    const product = screen.getByTestId('product-bid-e7-7').textContent ?? ''
    expect(product).toContain('Milk Thistle Liver Support')
    expect(product).not.toContain('undefined')
    expect(product).not.toContain('null')
    // No brand is invented out of the title, and the missing stamp is SAID rather than left
    // blank — a name with no date is the one a shopper most needs to be able to distrust.
    const gloss = screen.getByTestId('product-identity-bid-e7-7').textContent ?? ''
    expect(gloss).not.toContain('undefined')
    expect(gloss).toContain('named no time it was observed')
  })

  it('falls back to the reference, with no attribution line, when nothing was crawled', () => {
    render(<ShortlistView shortlist={one(PRICED_SLOT)} onAccept={vi.fn()} />)
    expect(screen.getByTestId('product-bid-e7-7').textContent).toContain('prod-merino-crew')
    // No name, so nothing to attribute: an attribution box over a bare reference would be the
    // empty attributed box this screen refuses everywhere else.
    expect(screen.queryByTestId('product-identity-bid-e7-7')).toBeNull()
  })

  /**
   * WHOSE PRICE IT IS — three states, and the third is not the first (R10, D55). These
   * replace `journey.test.tsx`'s retired `gap-fallback` bullet, which said the card could not
   * make this distinction at all.
   */
  it('says nothing about provenance when the store really quoted the price', () => {
    render(
      <ShortlistView shortlist={one({ ...PRICED_SLOT, fallback: false })} onAccept={vi.fn()} />,
    )
    // Silence is the deliberate answer: a line on every card saying "this shop quoted this"
    // teaches a reader to skim the one place the sentence matters.
    expect(screen.queryByTestId('price-provenance-bid-e7-7')).toBeNull()
  })

  it('says the price is Proxyshop’s, and why, when the exchange stood in', () => {
    render(
      <ShortlistView
        shortlist={one({
          ...PRICED_SLOT,
          fallback: true,
          fallback_reason: 'store_declined:cluster_not_pursued',
        })}
        onAccept={vi.fn()}
      />,
    )
    const provenance = screen.getByTestId('price-provenance-bid-e7-7').textContent ?? ''
    expect(provenance).toContain('Proxyshop’s, not this shop’s')
    expect(provenance).toContain('nobody at this shop quoted')
    // The exchange's own token, printed as the token it is rather than translated here.
    expect(provenance).toContain('store_declined:cluster_not_pursued')
    // The price itself is still shown — it is a real number the service sent.
    expect(screen.getByTestId('price-bid-e7-7').textContent).toContain('156')
  })

  it('says a stand-in with no stated reason is still a stand-in', () => {
    render(<ShortlistView shortlist={one({ ...PRICED_SLOT, fallback: true })} onAccept={vi.fn()} />)
    const provenance = screen.getByTestId('price-provenance-bid-e7-7').textContent ?? ''
    expect(provenance).toContain('Proxyshop’s, not this shop’s')
    expect(provenance).toContain('did not say why')
    expect(provenance).not.toContain('undefined')
    expect(provenance).not.toContain('null')
  })

  it('says the shop never answered even when the stand-in carried no price either', () => {
    // A stand-in minted from a roster row that named no readable list price. The flag is the
    // only thing on the card that can still say the shop was silent, so it says it — and it
    // does not claim a number nobody quoted, because there is no number.
    render(
      <ShortlistView
        shortlist={one({ ...PRICED_SLOT, price: null, fallback: true, fallback_reason: 'no_response' })}
        onAccept={vi.fn()}
      />,
    )
    const provenance = screen.getByTestId('price-provenance-bid-e7-7').textContent ?? ''
    expect(provenance).toContain('did not answer this auction')
    expect(provenance).toContain('no_response')
    expect(provenance).not.toContain('the number above')
  })

  it('stays quiet about whose price it is when there is no price to attribute', () => {
    // Nothing said the provenance AND nothing said a price: a sentence about whose price this
    // is, printed under a line that just said there is no price, is a sentence about nothing.
    render(<ShortlistView shortlist={one({ ...PRICED_SLOT, price: null })} onAccept={vi.fn()} />)
    expect(screen.queryByTestId('price-provenance-bid-e7-7')).toBeNull()
  })

  it('will not read an absent flag as a quote, and says the exchange did not say', () => {
    // THE FAILURE THIS FIELD EXISTS TO CLOSE. A producer older than `fallback` sends nothing,
    // and a screen that rendered that as `false` would present a price nobody quoted as a
    // quote. The pre-existing `PRICED_SLOT` carries no flag, which is exactly that producer.
    render(<ShortlistView shortlist={one(PRICED_SLOT)} onAccept={vi.fn()} />)
    const provenance = screen.getByTestId('price-provenance-bid-e7-7').textContent ?? ''
    expect(provenance).toContain('did not say whose price this is')
    expect(provenance).toContain('will not guess')
  })
})

/**
 * D55 ON THE SCREEN — the organic result and the sponsored one, and a reader who can tell
 * which is which.
 *
 * `POST /buyer/shortlist/render` serves a `pitch` per slot, and everything in it was
 * MEASURED against the devstack (`apps/buyer/devstack/run.py` — three real store agents, the
 * real exchange, the real buyer service) on the merino-beanie conversation. The two fixtures
 * below are that run's own bytes:
 *
 *   * `SPONSORED_PITCH` is demo-woolworks' slot. Its `platform_case` and all four `facts` are
 *     the served ones. Its `store_pitch` is a store message posted onto the slot on the way
 *     into `/render` and carried back byte for byte — see `STORE_PITCH_IS_NOT_ON_THE_WIRE`.
 *   * `ORGANIC_PITCH` is demo-alpine-supply's slot exactly as served: `store_pitch: null`,
 *     `voices: ["platform"]`. That is the ORGANIC result — a candidate the platform pitches
 *     out of its own crawl, with no advocate of its own.
 *
 * `store_pitch` carries markup and a URL in these tests for a measured reason.
 * `buyer_svc.pitch.writing.store_pitch_of` strips NOTHING: it drops a message that is not a
 * string, is blank, is over 1200 characters or carries control characters, and otherwise
 * returns the store's bytes unchanged. `FORBIDDEN_CHARACTERS` (`{}<>[]|\^~`) and the
 * invented-number arithmetic are the screen over the PLATFORM's written case, and they never
 * touch the seller's. So a `<b>` and an `https://` really do arrive here, and this last hop
 * is the only place that decides whether they become markup and a link. They must not.
 */
const SPONSORED_CASE =
  'You said price was a must-have, and here it is: 78.00 USD. Also offer held until: ' +
  '2026-09-07; reliability: 82%.'

const SHOP_OWN_WORDS =
  'We have been knitting merino in Yorkshire since 1974 and every beanie is finished by ' +
  'hand. Try it for 30 days -- if it is not the warmest hat you own, send it back on us. ' +
  'Read more at https://demo-woolworks.example.com/about <b>free wool wash</b> included.'

const SPONSORED_PITCH = {
  platform_case: SPONSORED_CASE,
  platform_case_source: 'assembled',
  store_pitch: SHOP_OWN_WORDS,
  voices: ['store', 'platform'],
  facts: [
    { key: 'price', value: '78.00 USD', kind: 'price', label: null },
    { key: 'offer held until', value: '2026-09-07', kind: 'price', label: null },
    { key: 'reliability', value: '82%', kind: 'trust', label: null },
    { key: 'free returns', value: '30 days', kind: 'commitment', label: LABEL_STORE_CONFIRMED },
  ],
} as const

const ORGANIC_PITCH = {
  platform_case:
    'You said price was a must-have, and here it is: 72.00 USD. Also offer held until: ' +
    '2026-09-07; reliability: 61%.',
  platform_case_source: 'assembled',
  store_pitch: null,
  voices: ['platform'],
  facts: [
    { key: 'price', value: '72.00 USD', kind: 'price', label: null },
    { key: 'offer held until', value: '2026-09-07', kind: 'price', label: null },
    { key: 'reliability', value: '61%', kind: 'trust', label: null },
    { key: 'ships within', value: '2 business days', kind: 'commitment', label: LABEL_STORE_CONFIRMED },
  ],
} as const

const SPONSORED_SLOT: ShortlistSlot = { ...PRICED_SLOT, pitch: SPONSORED_PITCH }
const ORGANIC_SLOT: ShortlistSlot = { ...PRICED_SLOT, pitch: ORGANIC_PITCH }

describe('the two voices, and a shopper who can tell them apart', () => {
  it("carries the shop's own words byte for byte", () => {
    render(<ShortlistView shortlist={one(SPONSORED_SLOT)} onAccept={vi.fn()} />)
    expect(screen.getByTestId('store-voice-bid-e7-7')).toHaveTextContent(SHOP_OWN_WORDS)
  })

  it("shows the platform's own case beside it, in the platform's words", () => {
    render(<ShortlistView shortlist={one(SPONSORED_SLOT)} onAccept={vi.fn()} />)
    expect(screen.getByTestId('platform-voice-bid-e7-7')).toHaveTextContent(SPONSORED_CASE)
  })

  it('never puts one voice inside the other', () => {
    render(<ShortlistView shortlist={one(SPONSORED_SLOT)} onAccept={vi.fn()} />)
    const store = screen.getByTestId('store-voice-bid-e7-7')
    const platform = screen.getByTestId('platform-voice-bid-e7-7')
    // Two separate elements, neither containing the other, neither carrying the other's text.
    expect(store.contains(platform)).toBe(false)
    expect(platform.contains(store)).toBe(false)
    expect(store.textContent ?? '').not.toContain(SPONSORED_CASE)
    expect(platform.textContent ?? '').not.toContain(SHOP_OWN_WORDS)
  })

  it('says whose voice each one is, in words, without a hover', () => {
    render(<ShortlistView shortlist={one(SPONSORED_SLOT)} onAccept={vi.fn()} />)
    const store = (screen.getByTestId('store-voice-bid-e7-7').textContent ?? '').toLowerCase()
    const platform = (
      screen.getByTestId('platform-voice-bid-e7-7').textContent ?? ''
    ).toLowerCase()
    // The seller's block names the SELLER as the author and says it is what the shop paid
    // for; the platform's names the PLATFORM and says it was written from checked facts.
    expect(store).toContain('shop')
    expect(store).toContain('own words')
    expect(platform).toContain('proxyshop')
    expect(platform).toContain('checked')
    // And neither attribution is the other's.
    expect(store).not.toContain('proxyshop wrote')
  })

  it('leads with the shop inside its own slot, which is what the shop bought', () => {
    const { container } = render(
      <ShortlistView shortlist={one(SPONSORED_SLOT)} onAccept={vi.fn()} />,
    )
    const text = container.textContent ?? ''
    expect(text.indexOf(SHOP_OWN_WORDS)).toBeGreaterThan(-1)
    expect(text.indexOf(SHOP_OWN_WORDS)).toBeLessThan(text.indexOf(SPONSORED_CASE))
  })

  it('renders a seller pitch as TEXT: no markup, no link, ever', () => {
    const { container } = render(
      <ShortlistView shortlist={one(SPONSORED_SLOT)} onAccept={vi.fn()} />,
    )
    const store = screen.getByTestId('store-voice-bid-e7-7')
    // The `<b>` arrives as four characters and stays four characters.
    expect(store.textContent ?? '').toContain('<b>free wool wash</b>')
    expect(store.querySelector('b')).toBeNull()
    expect(store.innerHTML).not.toContain('<b>')
    // The URL is on the screen as the seller wrote it and is not somewhere to click.
    expect(store.textContent ?? '').toContain('https://demo-woolworks.example.com/about')
    expect(container.querySelectorAll('a')).toHaveLength(0)
    expect(container.querySelector('script')).toBeNull()
  })

  it('renders a seller pitch that is nothing but an attack as text too', () => {
    const attack =
      '<img src=x onerror="alert(1)"> <a href="https://evil.example/cart">click here</a>'
    const { container } = render(
      <ShortlistView
        shortlist={one({ ...SPONSORED_SLOT, pitch: { ...SPONSORED_PITCH, store_pitch: attack } })}
        onAccept={vi.fn()}
      />,
    )
    expect(screen.getByTestId('store-voice-bid-e7-7')).toHaveTextContent(attack)
    expect(container.querySelectorAll('a')).toHaveLength(0)
    expect(container.querySelectorAll('img')).toHaveLength(0)
  })

  it('gives a scraped shop the platform voice and no empty box where an advocate would be', () => {
    render(<ShortlistView shortlist={one(ORGANIC_SLOT)} onAccept={vi.fn()} />)
    expect(screen.getByTestId('platform-voice-bid-e7-7')).toHaveTextContent(
      ORGANIC_PITCH.platform_case,
    )
    // No seller block at all — not a blank one, not an "undefined", not a null.
    expect(screen.queryByTestId('store-voice-bid-e7-7')).toBeNull()
    const card = screen.getByTestId('slot-bid-e7-7').textContent ?? ''
    expect(card).not.toContain('undefined')
    expect(card).not.toContain('null')
    // And it SAYS the seller's voice is missing rather than leaving a reader to guess that
    // this shop simply had nothing to say.
    //
    // It must NOT say "this shop has no advocate", and that assertion is here because this
    // test asserted exactly that until the devstack was driven. `store_pitch: null` does not
    // have ONE cause at this hop, and the gloss may not pick one.
    //
    // WHAT CHANGED, and why the third assertion below is not the one it used to be. This
    // block used to require the gloss to contain `'shortlist contract'` — introduced in
    // `b34d591`, when the second of the two causes it named was "an in-network shop whose
    // `Bid.message` was dropped by the exchange's `extra="forbid"` shortlist contract". That
    // cause is GONE, and not because the sentence was softened: `98529bd` declared
    // `ShortlistSlot.message`, `buyer_svc.pitch.writing.store_pitch_of` reads the seller's
    // bytes off it, and a shop that sends a pitch now has it rendered. Measured through the
    // repo's own buyer service on a real live-auction slot: `/buyer/shortlist/render`
    // answered `store_pitch: "Our milk thistle gummies…"` with `voices: ["store","platform"]`.
    // Keeping the old assertion would have pinned the page to blaming a contract that has
    // since been fixed for a silence the shop itself chose — so the assertion moved to the
    // three causes that are actually left, which is a stricter claim than the two it replaces.
    const why = (screen.getByTestId('no-store-voice-bid-e7-7').textContent ?? '').toLowerCase()
    // The typographic apostrophe is the one the page renders (`&rsquo;`), asserted as the
    // character it becomes rather than as the entity.
    expect(why).toContain('nothing here is in this shop’s own voice')
    // Cause 1: a scraped shop, which has no advocate and is paying for nothing.
    expect(why).toContain('crawling')
    // Cause 2: an in-network shop that had an advocate and chose to say nothing.
    expect(why).toContain('chose to say nothing')
    // Cause 3: a message longer than the published cap, refused WHOLE rather than truncated,
    // because a shortened pitch is words the shop did not write with the shop's name on them.
    expect(why).toContain('longer than the published limit')
    expect(why).not.toMatch(/this shop has no advocate/)
    // The retired cause must not creep back: the contract carries the message now.
    expect(why).not.toContain('shortlist contract')
  })

  it('shows the facts the case was drawn from, and says the case is a subset of them', () => {
    render(<ShortlistView shortlist={one(SPONSORED_SLOT)} onAccept={vi.fn()} />)
    const facts = screen.getByTestId('pitch-facts-bid-e7-7')
    // Every served fact, key and value, and each one's own provenance label where it has one.
    for (const fact of SPONSORED_PITCH.facts) {
      expect(facts).toHaveTextContent(fact.key)
      expect(facts).toHaveTextContent(fact.value)
    }
    expect(facts).toHaveTextContent(LABEL_STORE_CONFIRMED)
    // The subset sentence is on the screen WITHOUT opening anything: it is the claim, and the
    // enumeration behind it is the audit.
    const summary = (screen.getByTestId('pitch-facts-summary-bid-e7-7').textContent ?? '')
      .toLowerCase()
    expect(summary).toContain('4')
    expect(summary).toContain('checked')
  })

  it('renders no pitch block at all for a slot the platform had nothing to say about', () => {
    render(<ShortlistView shortlist={one({ ...PRICED_SLOT, pitch: null })} onAccept={vi.fn()} />)
    expect(screen.queryByTestId('pitch-bid-e7-7')).toBeNull()
    expect(screen.queryByTestId('store-voice-bid-e7-7')).toBeNull()
    expect(screen.queryByTestId('platform-voice-bid-e7-7')).toBeNull()
    // The rest of the card is untouched.
    expect(screen.getByTestId('price-bid-e7-7')).toBeInTheDocument()
  })

  it('renders a producer older than the pitch field without a blank block', () => {
    render(<ShortlistView shortlist={one(PRICED_SLOT)} onAccept={vi.fn()} />)
    expect(screen.queryByTestId('pitch-bid-e7-7')).toBeNull()
    expect(screen.getByTestId('slot-bid-e7-7').textContent ?? '').not.toContain('undefined')
  })

  it('shows the store voice alone when the platform has checked nothing', () => {
    // MEASURED shape: `buyer_svc.pitch.writing.pitch_for` returns `platform_case: ""`,
    // `voices: ["store"]` and `facts: []` for an in-network shop the crawl holds nothing
    // usable about. The platform says NOTHING rather than writing filler under its own name.
    render(
      <ShortlistView
        shortlist={one({
          ...PRICED_SLOT,
          pitch: {
            platform_case: '',
            platform_case_source: 'assembled',
            store_pitch: 'We have been roasting on this street since 1998.',
            voices: ['store'],
            facts: [],
          },
        })}
        onAccept={vi.fn()}
      />,
    )
    expect(screen.getByTestId('store-voice-bid-e7-7')).toHaveTextContent('since 1998')
    expect(screen.queryByTestId('platform-voice-bid-e7-7')).toBeNull()
    const why = (screen.getByTestId('no-platform-voice-bid-e7-7').textContent ?? '').toLowerCase()
    expect(why).toContain('checked')
    expect(screen.queryByTestId('pitch-facts-bid-e7-7')).toBeNull()
  })
})

/**
 * THE RECORD BEHIND A CARD — what a shopper gets by clicking into a row.
 *
 * Every fixture below is LIVE BYTES. They were taken off the running stack on 2026-09-08 by
 * driving the served routes in order — `POST /buyer/intent/clarify`, `POST /buyer/intent/confirm`,
 * `GET /buyer/auctions/{id}` — against the demo corpus, so the shapes these tests assert on are
 * the shapes the exchange actually publishes rather than shapes invented to make a renderer
 * pass. Two auctions are represented:
 *
 *   * `RECORD_BODY` is `auction-027aa418-…`, a four-slot shortlist against the full demo
 *     roster. The `gaiaherbs.com` slot below is that response's own bytes, including the
 *     `commitments[0].provenance` block that `buyer_svc.accept.labels.slot_commitments` and
 *     `journey/wire.ts::readCommitments` each drop on the way to a card.
 *   * `NO_AGENT_SLOT` is `auction-75953d79-…`, opened with a roster of the two shops that have
 *     no bidding agent. Both slots came back `fallback: true` with
 *     `fallback_reason: "tier_0_no_agent:no_bid_endpoint"` — the case the card used to describe
 *     as a shop that "did not answer this auction".
 *
 * The `seller_asserted` claim is the one addition, and it is a shape the corpus does not
 * currently produce: the demo store agents commit through envelope hooks, so every live claim
 * carries `owner_statement`. It is here because "a verified claim and an unverified one must
 * not look alike" is the property this panel exists for, and a suite with only the evidenced
 * shape would never once have rendered the other side of it.
 */
const LIVE_BID = 'auction-027aa418-3ab4-4b43-b88d-d27e5d68cdd7:gaiaherbs.com'

const LIVE_TRUST = {
  store_id: 'gaiaherbs.com',
  available: true,
  score: 0.7728621443663455,
  confidence: 0.9421102773436725,
  low_data: false,
  dimensions: [
    'catalog_claim_accuracy',
    'discount_honored',
    'feedback_match',
    'not_returned',
    'price_honored',
    'shipped_on_time',
  ],
}

/** The slot as it reaches this component: the browser's narrowing, applied. */
const LIVE_SLOT: ShortlistSlot = {
  slot: 'fit',
  bid_ref: LIVE_BID,
  auction_id: 'auction-027aa418-3ab4-4b43-b88d-d27e5d68cdd7',
  fit_score: 0.5390724288732691,
  // What `wire.ts::asNumberMap` leaves of the snapshot above — two of its six fields.
  trust_summary: { score: 0.7728621443663455, confidence: 0.9421102773436725 },
  // What `wire.ts::asUnknownMap` keeps beside it, and what nothing rendered until now.
  trust_fields: LIVE_TRUST,
  provenance_labels: ['from their website', 'store-confirmed', 'unverified'],
  labels_source: 'exchange',
  store_domain: 'gaiaherbs.com',
  product: {
    product_ref: 'prod_5b3100b381998843c2f732f147e632d0',
    variant_ref: '42280407990408',
    identity: {
      title: 'Milk Thistle Gummies',
      brand: 'Gaia Herbs',
      source: 'snap-gaiaherbs.com',
      observed_at: '2026-01-01T00:00:00Z',
    },
  },
  price: {
    unit_price: 25.49,
    total_price: 25.49,
    currency: 'USD',
    discount: null,
    expires_at: '2026-09-08T19:33:20.508000Z',
  },
  commitments: [
    { key: 'free_returns', value: '30 return window', unit: null, label: LABEL_STORE_CONFIRMED },
    { key: 'ships_in_days', value: 2, unit: 'days', label: LABEL_UNVERIFIED },
  ],
  fallback: false,
  fallback_reason: null,
}

/** `GET /buyer/auctions/{id}`, reduced to the two arrays this fold reads. Live bytes. */
const RECORD_BODY = {
  auction_id: 'auction-027aa418-3ab4-4b43-b88d-d27e5d68cdd7',
  recorded_at: '2026-09-08T19:23:20.601Z',
  shortlist: {
    auction_id: 'auction-027aa418-3ab4-4b43-b88d-d27e5d68cdd7',
    slots: [
      {
        slot: 'fit',
        bid_ref: LIVE_BID,
        fit_score: 0.5390724288732691,
        trust_summary: LIVE_TRUST,
        provenance_labels: ['from their website', 'store-confirmed', 'unverified'],
        product: LIVE_SLOT.product,
        price: LIVE_SLOT.price,
        commitments: [
          {
            claim_id: null,
            key: 'free_returns',
            claim_type: 'return_policy',
            value: '30 return window',
            unit: null,
            source_span: null,
            provenance: {
              source: 'owner_statement',
              ref: 'envelope:gaiaherbs.com:demo-1#free_returns',
              observed_at: '2026-01-01T00:00:00Z',
              authority_rank: 1,
            },
          },
          {
            claim_id: null,
            key: 'ships_in_days',
            claim_type: 'shipping_speed',
            value: 2,
            unit: 'days',
            source_span: null,
            provenance: {
              source: 'seller_asserted',
              ref: 'pitch:gaiaherbs.com:1#ships_in_days',
              observed_at: '2026-09-08T19:23:18Z',
              authority_rank: 5,
            },
          },
        ],
        message: 'Milk thistle for liver support comes with a 30-day return window.',
        fallback: false,
        fallback_reason: null,
        store_domain: 'gaiaherbs.com',
      },
    ],
  },
  ranked: [
    {
      bid_ref: LIVE_BID,
      store_id: 'gaiaherbs.com',
      rank_score: 0.5390724288732691,
      components: {
        intent_match: 0.175,
        verified_claim_ratio: 0.15950000000000003,
        trust: 0.1545724288732691,
        price_value: 0.0,
        delivery_fit: 0.05,
      },
    },
  ],
}

/**
 * `RECORD_BODY` with the one slot's fields replaced.
 *
 * The result is typed `unknown` because it is wire JSON: the point of every reader in
 * `shortlist.ts` is that a body is `unknown` at runtime however a test spells it, and a fixture
 * that arrived already typed would exercise none of them.
 */
function recordBodyWith(slotPatch: Record<string, unknown>): unknown {
  const body = JSON.parse(JSON.stringify(RECORD_BODY)) as {
    shortlist: { slots: Record<string, unknown>[] }
  }
  body.shortlist.slots[0] = { ...body.shortlist.slots[0], ...slotPatch }
  return body
}

/** A fetcher that answers the auction route and records every path it was asked for. */
function recordingFetcher(body: unknown, status = 200): { fetcher: Fetcher; asked: string[] } {
  const asked: string[] = []
  const fetcher: Fetcher = async (input) => {
    asked.push(input)
    return jsonResponse(body, status)
  }
  return { fetcher, asked }
}

async function openTheRecord(bidRef: string): Promise<void> {
  fireEvent.click(screen.getByTestId(`record-toggle-${bidRef}`))
  await waitFor(() => expect(screen.getByTestId(`record-platform-${bidRef}`)).toBeInTheDocument())
}

describe('clicking into a row for the record behind it', () => {
  it('asks for nothing until a reader opens one', () => {
    const { asked, fetcher } = recordingFetcher(RECORD_BODY)
    render(
      <ShortlistView
        shortlist={one(LIVE_SLOT)}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    // The fold is on the card and shut. Nothing has been requested, and nothing inside it has
    // been rendered — a panel that mounted its own contents closed would be a fetch a shopper
    // never asked for, on every card of every shortlist.
    expect(screen.getByTestId(`record-toggle-${LIVE_BID}`)).toBeInTheDocument()
    expect(screen.queryByTestId(`record-platform-${LIVE_BID}`)).toBeNull()
    expect(asked).toEqual([])
  })

  it('reads the auction record once, however many cards are opened', async () => {
    const { asked, fetcher } = recordingFetcher(RECORD_BODY)
    const second: ShortlistSlot = { ...LIVE_SLOT, bid_ref: 'bid-second', slot: 'value' }
    render(
      <ShortlistView
        shortlist={{ auction_id: RECORD_BODY.auction_id, slots: [LIVE_SLOT, second] }}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(LIVE_BID)
    fireEvent.click(screen.getByTestId('record-toggle-bid-second'))
    await waitFor(() => expect(screen.getByTestId('record-platform-bid-second')).toBeInTheDocument())
    expect(asked).toEqual([`/buyer/auctions/${encodeURIComponent(RECORD_BODY.auction_id)}`])
    // Both stay open: two records side by side is the comparison a shopper is making.
    expect(screen.getByTestId(`record-platform-${LIVE_BID}`)).toBeInTheDocument()
  })

  it('closes again without asking for anything else', async () => {
    const { asked, fetcher } = recordingFetcher(RECORD_BODY)
    render(
      <ShortlistView shortlist={one(LIVE_SLOT)} onAccept={vi.fn()} recordFetcher={fetcher} />,
    )
    await openTheRecord(LIVE_BID)
    fireEvent.click(screen.getByTestId(`record-toggle-${LIVE_BID}`))
    await waitFor(() => expect(screen.queryByTestId(`record-platform-${LIVE_BID}`)).toBeNull())
    expect(asked).toHaveLength(1)
  })

  it('never offers a link, however much of the record is on the screen', async () => {
    const { fetcher } = recordingFetcher(RECORD_BODY)
    const { container } = render(
      <ShortlistView
        shortlist={one({ ...LIVE_SLOT, ...{ checkout_url: 'https://attacker.example/cart/1:1' } })}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(LIVE_BID)
    // R3, restated over the new surface: the record carries refs and snapshot ids, and an
    // `<a href>` built out of any of them is the violation the acceptance decoy hunts for.
    expect(container.querySelectorAll('a')).toHaveLength(0)
    expect(container.innerHTML).not.toContain('attacker.example')
  })
})

describe('the platform half of the record', () => {
  it('names the snapshot the product name was read from, and when it was seen', async () => {
    const { fetcher } = recordingFetcher(RECORD_BODY)
    render(<ShortlistView shortlist={one(LIVE_SLOT)} onAccept={vi.fn()} recordFetcher={fetcher} />)
    await openTheRecord(LIVE_BID)
    const platform = screen.getByTestId(`record-platform-${LIVE_BID}`).textContent ?? ''
    expect(platform).toContain('Milk Thistle Gummies')
    expect(platform).toContain('Gaia Herbs')
    expect(platform).toContain('snap-gaiaherbs.com')
    expect(platform).toContain('2026-01-01T00:00:00Z')
    expect(platform).toContain('prod_5b3100b381998843c2f732f147e632d0')
    expect(platform).toContain('42280407990408')
  })

  it('shows the trust fields the browser’s narrowing throws away', async () => {
    const { fetcher } = recordingFetcher(RECORD_BODY)
    render(<ShortlistView shortlist={one(LIVE_SLOT)} onAccept={vi.fn()} recordFetcher={fetcher} />)
    await openTheRecord(LIVE_BID)
    const platform = screen.getByTestId(`record-platform-${LIVE_BID}`).textContent ?? ''
    // `TrustSummary` is `Record<string, number>`, so `wire.ts` keeps two of these six fields
    // and drops the rest. The card's trust line therefore reads `score …, confidence …` and
    // says nothing about whether there IS a snapshot or how thin it is.
    expect(platform).toContain('low_data')
    expect(platform).toContain('available')
    expect(platform).toContain('catalog_claim_accuracy')
    expect(platform).toContain('gaiaherbs.com')
    // A boolean and the string "true" must not read identically in a record of what was
    // actually recorded, so values keep their JSON spelling: `false`, and `"gaiaherbs.com"`
    // with its quotes.
    expect(platform).toContain('"gaiaherbs.com"')
  })

  it('spells a list out so it can wrap instead of running off the card', async () => {
    const { fetcher } = recordingFetcher(RECORD_BODY)
    render(<ShortlistView shortlist={one(LIVE_SLOT)} onAccept={vi.fn()} recordFetcher={fetcher} />)
    await openTheRecord(LIVE_BID)
    const platform = screen.getByTestId(`record-platform-${LIVE_BID}`).textContent ?? ''
    // MEASURED on the served page before this: `JSON.stringify` writes an array with no
    // spaces, so the six trust dimensions arrived as one 120-character token with no break
    // opportunity in it and the row ran off the right edge of the card. The separator is what
    // gives the line somewhere to fold.
    expect(platform).toContain('"catalog_claim_accuracy", "discount_honored"')
    expect(platform).not.toContain('"catalog_claim_accuracy","discount_honored"')
  })

  it('publishes the exchange’s five ranking components and what each contributed', async () => {
    const { fetcher } = recordingFetcher(RECORD_BODY)
    render(<ShortlistView shortlist={one(LIVE_SLOT)} onAccept={vi.fn()} recordFetcher={fetcher} />)
    await openTheRecord(LIVE_BID)
    const platform = screen.getByTestId(`record-platform-${LIVE_BID}`).textContent ?? ''
    for (const term of [
      'intent_match',
      'verified_claim_ratio',
      'trust',
      'price_value',
      'delivery_fit',
    ]) {
      expect(platform).toContain(term)
    }
    expect(platform).toContain('0.175')
    expect(platform).toContain('0.5390724288732691')
    // Recorded, not recomputed — and the panel says which, because it is a different clock
    // from the live shortlist the card above it was drawn from.
    expect(screen.getByTestId(`record-ranking-${LIVE_BID}`).textContent ?? '').toContain(
      'not re-computed',
    )
  })

  it('says whether the exchange supplied the labels or the service worked them out', async () => {
    const { fetcher } = recordingFetcher(RECORD_BODY)
    render(<ShortlistView shortlist={one(LIVE_SLOT)} onAccept={vi.fn()} recordFetcher={fetcher} />)
    await openTheRecord(LIVE_BID)
    expect(screen.getByTestId(`record-platform-${LIVE_BID}`).textContent ?? '').toContain(
      'the exchange sent them',
    )
  })

  it('keeps the shop’s own words out of the platform’s block', async () => {
    const { fetcher } = recordingFetcher(RECORD_BODY)
    render(<ShortlistView shortlist={one(LIVE_SLOT)} onAccept={vi.fn()} recordFetcher={fetcher} />)
    await openTheRecord(LIVE_BID)
    // D55. The record body carries `message` — the seller's bytes — and this block is the
    // platform speaking. `PitchPanel` owns the shop's prose and owns its attribution; a
    // sentence of it inside the platform's block is the seller's motive wearing the
    // platform's credibility, which is the one thing this screen exists to prevent.
    expect(screen.getByTestId(`record-platform-${LIVE_BID}`).textContent ?? '').not.toContain(
      'Milk thistle for liver support comes with',
    )
  })
})

describe('the shop half of the record', () => {
  it('shows each promise’s provenance, authority rank, stamp and evidence reference', async () => {
    const { fetcher } = recordingFetcher(RECORD_BODY)
    render(<ShortlistView shortlist={one(LIVE_SLOT)} onAccept={vi.fn()} recordFetcher={fetcher} />)
    await openTheRecord(LIVE_BID)
    const store = screen.getByTestId(`record-store-${LIVE_BID}`).textContent ?? ''
    // None of these four reach a card: the service projects a `Claim` down to
    // `{key, value, unit, label}` and the browser's reader keeps exactly those four.
    expect(store).toContain('owner_statement')
    expect(store).toContain('return_policy')
    expect(store).toContain('envelope:gaiaherbs.com:demo-1#free_returns')
    expect(store).toContain('2026-01-01T00:00:00Z')
    expect(store).toContain('1 is the strongest evidence this network records')
  })

  it('does not let an evidenced promise and an unevidenced one read alike', async () => {
    const { fetcher } = recordingFetcher(RECORD_BODY)
    render(<ShortlistView shortlist={one(LIVE_SLOT)} onAccept={vi.fn()} recordFetcher={fetcher} />)
    await openTheRecord(LIVE_BID)
    const claims = screen.getAllByTestId(`record-claim-${LIVE_BID}`)
    expect(claims).toHaveLength(2)
    const evidenced = claims[0]!.textContent ?? ''
    const asserted = claims[1]!.textContent ?? ''
    // The store standing behind it through a hook, versus the store simply saying so. Same
    // shape of row, four different values, and the badges carry different tones.
    expect(evidenced).toContain('owner_statement')
    expect(evidenced).toContain(LABEL_STORE_CONFIRMED)
    expect(asserted).toContain('seller_asserted')
    expect(asserted).toContain(LABEL_UNVERIFIED)
    const tones = screen
      .getAllByTestId(`record-claim-label-${LIVE_BID}`)
      .map((badge) => badge.getAttribute('data-tone'))
    expect(tones).toEqual(['confirmed', 'unverified'])
  })

  it('says what the badge is not — the per-claim verdicts are not published here', async () => {
    const { fetcher } = recordingFetcher(RECORD_BODY)
    render(<ShortlistView shortlist={one(LIVE_SLOT)} onAccept={vi.fn()} recordFetcher={fetcher} />)
    await openTheRecord(LIVE_BID)
    // `contracts.protocol.Claim` is `additionalProperties: false` and declares no verdict
    // field, so the exchange's per-claim `verified`/`contradicted`/`unsupported`/`ambiguous`
    // decisions reach no buyer-facing surface — only their total does, as the
    // `verified_claim_ratio` term. A panel that let the provenance badge be read as the
    // verdict would be overclaiming exactly where a shopper is deciding whom to believe.
    const gloss = screen.getByTestId(`record-verdict-gloss-${LIVE_BID}`).textContent ?? ''
    expect(gloss).toContain('verified_claim_ratio')
    expect(gloss).toContain('will not invent')
  })

  it('shows the rule that authorised a discount', async () => {
    const { fetcher } = recordingFetcher(
      recordBodyWith({
        price: {
          unit_price: 25.49,
          total_price: 25.49,
          currency: 'USD',
          expires_at: '2026-09-08T19:33:20.508000Z',
          discount: {
            type: 'percentage',
            value: 15,
            provenance: {
              source: 'envelope_rule',
              ref: 'envelope:gaiaherbs.com:demo-1#max_discount_pct',
              observed_at: '2026-01-01T00:00:00Z',
              authority_rank: 1,
            },
          },
        },
      }),
    )
    render(
      <ShortlistView
        shortlist={one({
          ...LIVE_SLOT,
          // What `wire.ts::readDiscount` leaves of it: the depth, and nothing that authorised it.
          price: { ...LIVE_SLOT.price!, discount: { type: 'percentage', value: 15 } },
        })}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(LIVE_BID)
    const discount = screen.getByTestId(`record-discount-${LIVE_BID}`).textContent ?? ''
    expect(discount).toContain('15% off')
    expect(discount).toContain('envelope_rule')
    expect(discount).toContain('envelope:gaiaherbs.com:demo-1#max_discount_pct')
  })

  it('says the exchange published no commitments rather than showing a store that promised nothing', async () => {
    const { fetcher } = recordingFetcher(recordBodyWith({ commitments: null }))
    render(
      <ShortlistView
        shortlist={one({ ...LIVE_SLOT, commitments: null })}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(LIVE_BID)
    const said = screen.getByTestId(`record-no-claims-${LIVE_BID}`).textContent ?? ''
    expect(said).toContain('published no commitments')
    expect(screen.queryAllByTestId(`record-claim-${LIVE_BID}`)).toHaveLength(0)
  })
})

describe('the record when the service cannot answer', () => {
  it('still renders everything that came with the card, and says what failed', async () => {
    const { fetcher } = recordingFetcher({ detail: 'no such auction' }, 404)
    render(<ShortlistView shortlist={one(LIVE_SLOT)} onAccept={vi.fn()} recordFetcher={fetcher} />)
    await openTheRecord(LIVE_BID)
    expect(screen.getByTestId(`record-failed-${LIVE_BID}`).textContent ?? '').toContain('HTTP 404')
    // The platform half is drawn from the slot, so a failed read costs the claims and the
    // ranking and nothing else. A panel that blanked itself would lose the crawl's snapshot id
    // over a request that has nothing to do with it.
    const platform = screen.getByTestId(`record-platform-${LIVE_BID}`).textContent ?? ''
    expect(platform).toContain('snap-gaiaherbs.com')
    // "WAS NOT READ", never "the record carries no ranking for this candidate". The second is
    // a statement about the auction and this page has no standing to make it — the request
    // that would have settled it 404'd. An earlier version of this panel said exactly that,
    // and this test asserted it, which is how a false sentence gets a green tick beside it.
    const ranking = screen.getByTestId(`record-ranking-${LIVE_BID}`).textContent ?? ''
    expect(ranking).toContain('was not read')
    expect(ranking).toContain('Nothing has been computed in their place')
    expect(ranking).not.toContain('no ranking for this candidate')
    // Same rule on the shop's side: not "this shop promised nothing", which is a claim about
    // the shop, but "this page did not manage to look".
    const claims = screen.getByTestId(`record-no-claims-${LIVE_BID}`).textContent ?? ''
    expect(claims).toContain('was not read')
    expect(claims).not.toContain('published no commitments')
    expect(platform).toContain('cannot say')
  })

  it('says the record names no such candidate rather than blaming the shop', async () => {
    // A record that WAS read and holds nothing under this bid reference. Three absences and
    // three sentences: the request failed, the record has no such row, the row promised
    // nothing. Collapsing them is how a page ends up telling a shopper that a shop committed
    // to nothing when the truth is that a join did not land.
    const { fetcher } = recordingFetcher({ ...RECORD_BODY, shortlist: { slots: [] }, ranked: [] })
    render(<ShortlistView shortlist={one(LIVE_SLOT)} onAccept={vi.fn()} recordFetcher={fetcher} />)
    await openTheRecord(LIVE_BID)
    expect(screen.getByTestId(`record-no-claims-${LIVE_BID}`).textContent ?? '').toContain(
      'names no candidate with this bid reference',
    )
    expect(screen.getByTestId(`record-platform-${LIVE_BID}`).textContent ?? '').toContain(
      'names no candidate with this bid reference',
    )
    // And the card's own material is untouched by the miss.
    expect(screen.getByTestId(`record-platform-${LIVE_BID}`).textContent ?? '').toContain(
      'snap-gaiaherbs.com',
    )
  })

  it('keeps the recorded ranking when the exchange has forgotten the shortlist', async () => {
    // `shortlist: null` is the exchange's 15-minute TTL, and it is a fact about the exchange
    // rather than about the market. The RECORDED diagnostics survive it, so the components are
    // still there and only the live claims are gone.
    const { fetcher } = recordingFetcher({ ...RECORD_BODY, shortlist: null })
    render(<ShortlistView shortlist={one(LIVE_SLOT)} onAccept={vi.fn()} recordFetcher={fetcher} />)
    await openTheRecord(LIVE_BID)
    expect(screen.getByTestId(`record-platform-${LIVE_BID}`).textContent ?? '').toContain(
      'intent_match',
    )
    // And the promises say why they are missing, in a sentence about the EXCHANGE rather than
    // about the shop: the fifteen-minute window closed, so they cannot be re-read. "The
    // exchange published no commitments for this slot" would be a verdict on the shop drawn
    // from a shortlist nobody can look at any more.
    const claims = screen.getByTestId(`record-no-claims-${LIVE_BID}`).textContent ?? ''
    expect(claims).toContain('no longer holds this auction’s shortlist')
    expect(claims).not.toContain('published no commitments')
  })
})

/**
 * READING THE RECORD — the parsing rules, driven directly.
 *
 * A JSON body is `unknown` at runtime however the types are written, and every one of these is
 * a shape that would put a false statement on the screen if it were let through.
 */
describe('reading the auction record', () => {
  it('keeps the exchange’s own order for the ranking components', () => {
    const read = readRecordedAuction(RECORD_BODY.auction_id, RECORD_BODY)
    expect(read.slots[LIVE_BID]!.ranking!.components.map(([term]) => term)).toEqual([
      'intent_match',
      'verified_claim_ratio',
      'trust',
      'price_value',
      'delivery_fit',
    ])
  })

  it('refuses an authority rank the contract does not admit rather than printing it', () => {
    // `Provenance.authority_rank` is validated `>= 1`, and "1 is the strongest" is on the
    // screen beside it. A `0` printed there would read as stronger than the strongest rank
    // this network publishes — a claim about evidence that nobody made.
    const read = readRecordedAuction('a', {
      shortlist: {
        slots: [
          {
            bid_ref: 'b',
            commitments: [
              { key: 'k', value: 1, provenance: { source: 'scraped', ref: 'r', observed_at: 't', authority_rank: 0 } },
            ],
          },
        ],
      },
    })
    expect(read.slots.b!.claims![0]!.provenance!.authority_rank).toBeNull()
    expect(read.slots.b!.claims![0]!.provenance!.source).toBe('scraped')
  })

  it('drops a claim with no key and keeps its neighbours', () => {
    const read = readRecordedAuction('a', {
      shortlist: {
        slots: [{ bid_ref: 'b', commitments: [{ value: 1 }, { key: 'free_returns', value: true }] }],
      },
    })
    expect(read.slots.b!.claims!.map((claim) => claim.key)).toEqual(['free_returns'])
  })

  it('keeps "the exchange sent none" apart from "none of them were readable"', () => {
    const none = readRecordedAuction('a', { shortlist: { slots: [{ bid_ref: 'b', commitments: null }] } })
    const unreadable = readRecordedAuction('a', {
      shortlist: { slots: [{ bid_ref: 'b', commitments: [{ value: 1 }] }] },
    })
    expect(none.slots.b!.claims).toBeNull()
    expect(unreadable.slots.b!.claims).toEqual([])
  })

  it('names an evidence block with no source as no evidence at all', () => {
    const read = readRecordedAuction('a', {
      shortlist: {
        slots: [{ bid_ref: 'b', commitments: [{ key: 'k', provenance: { ref: 'r' } }] }],
      },
    })
    expect(read.slots.b!.claims![0]!.provenance).toBeNull()
  })

  it('indexes a bid ref that names an Object.prototype member without inventing a row', () => {
    // A `bid_ref` is a string off the wire and the index must behave for every string. On a
    // plain `{}` these two do not: `slots['toString']` reads back a function nobody put there
    // and would be spread into a `SlotRecord`, and writing `slots['__proto__']` retargets the
    // prototype instead of storing a row. The exchange mints `{auction_id}:{store_id}`, so
    // neither is reachable today — the point is that the reader does not depend on that.
    const read = readRecordedAuction('a', {
      shortlist: { slots: [{ bid_ref: '__proto__', commitments: [{ key: 'k', value: 1 }] }] },
      ranked: [{ bid_ref: 'toString', store_id: 's', rank_score: 0.5, components: { trust: 0.5 } }],
    })
    // Read through `string`-typed keys, which is how a bid ref actually arrives — and which
    // also stops TypeScript resolving `slots.toString` to `Object.prototype`'s member instead
    // of the index signature, the static half of the same confusion.
    const inherited: readonly string[] = ['__proto__', 'toString', 'valueOf', 'hasOwnProperty']
    const [protoKey, toStringKey, valueOfKey, hasOwnKey] = inherited as [
      string,
      string,
      string,
      string,
    ]
    expect(read.slots[protoKey]!.claims!.map((claim) => claim.key)).toEqual(['k'])
    expect(read.slots[toStringKey]!.ranking!.rank_score).toBe(0.5)
    // And a ref nobody sent is still absent rather than an inherited member of the map.
    expect(read.slots[valueOfKey]).toBeUndefined()
    expect(read.slots[hasOwnKey]).toBeUndefined()
  })

  it('tells a forgotten shortlist apart from an unreadable one', () => {
    expect(readRecordedAuction('a', { shortlist: null }).liveness).toBe('forgotten')
    expect(readRecordedAuction('a', { shortlist: { slots: [] } }).liveness).toBe('live')
  })

  it('refuses a shortlist that names no auction rather than fetching one', async () => {
    const { asked, fetcher } = recordingFetcher(RECORD_BODY)
    await expect(loadRecordedAuction('   ', fetcher)).rejects.toBeInstanceOf(RecordedAuctionUnreadable)
    expect(asked).toEqual([])
  })
})

/**
 * WHOSE SILENCE IT WAS — the fix for a card that put a refusal in an organic shop's mouth.
 *
 * `NO_AGENT_SLOT` is live: a roster of the two demo shops with no bidding agent produced two
 * slots, both `fallback: true` with `fallback_reason: "tier_0_no_agent:no_bid_endpoint"`. The
 * card rendered that byte-identically to a shop whose agent was asked and stayed silent.
 */
const NO_AGENT_SLOT: ShortlistSlot = {
  slot: 'fit',
  bid_ref: 'auction-75953d79-60d6-4aee-877a-a104776a43d4:nutricost.com',
  auction_id: 'auction-75953d79-60d6-4aee-877a-a104776a43d4',
  fit_score: 0.45247839305364085,
  trust_summary: { score: 0.6373919652682042, confidence: 0.913116144629241 },
  provenance_labels: [LABEL_UNVERIFIED],
  store_domain: 'nutricost.com',
  product: {
    product_ref: 'prod_261dd5a5c47ecef42fe3755e93ddb20e',
    variant_ref: null,
    identity: null,
  },
  price: { unit_price: 15.97, total_price: 15.97, currency: 'USD', expires_at: null },
  commitments: null,
  fallback: true,
  fallback_reason: 'tier_0_no_agent:no_bid_endpoint',
}

describe('a shop that was never asked', () => {
  it('does not say an organic shop declined to answer', () => {
    render(<ShortlistView shortlist={one(NO_AGENT_SLOT)} onAccept={vi.fn()} />)
    const line = screen.getByTestId(`price-provenance-${NO_AGENT_SLOT.bid_ref}`).textContent ?? ''
    // THE DEFECT. This shop has no agent for anyone to solicit; nobody spoke to it, so it
    // declined nothing. Saying it "did not answer" is the platform describing a refusal that
    // never happened — a claim about a shop that the platform cannot check, which is the one
    // thing D55 does not allow it to make.
    expect(line).not.toContain('did not answer')
    expect(line).toContain('no bidding agent')
    expect(line).toContain('nobody asked it for a price')
    expect(line).toContain('turned nothing down')
    // Still Proxyshop's price, and still the exchange's own token, unchanged.
    expect(line).toContain('Proxyshop’s, not this shop’s')
    expect(line).toContain('tier_0_no_agent:no_bid_endpoint')
  })

  it('still says a solicited shop stayed silent — the honest direction', () => {
    // The other half of the same branch. A refusal that fires on every fallback is no more
    // useful than one that fires on none: `no_response` really does mean a store's agent was
    // asked and nothing came back, and that sentence must survive the fix.
    render(
      <ShortlistView
        shortlist={one({ ...NO_AGENT_SLOT, fallback_reason: 'no_response' })}
        onAccept={vi.fn()}
      />,
    )
    const line = screen.getByTestId(`price-provenance-${NO_AGENT_SLOT.bid_ref}`).textContent ?? ''
    expect(line).toContain('did not answer this auction')
    expect(line).not.toContain('no bidding agent')
  })

  it('reads the family off the whole reason, however the exchange spelled the detail', () => {
    // The detail half is open-ended by construction — a status code on a refusal, a
    // store-chosen header on a decline — so the family is what a screen may match on.
    expect(fallbackReasonFamily('tier_0_no_agent:no_bid_endpoint')).toBe(NO_AGENT_FALLBACK_FAMILY)
    expect(fallbackReasonFamily('tier_0_no_agent')).toBe(NO_AGENT_FALLBACK_FAMILY)
    expect(fallbackReasonFamily('store_refused:503')).toBe('store_refused')
    expect(fallbackReasonFamily(null)).toBe('')
  })

  it('says a shop with no agent and no list price quoted nothing, without blaming it', () => {
    render(
      <ShortlistView
        shortlist={one({ ...NO_AGENT_SLOT, price: null })}
        onAccept={vi.fn()}
      />,
    )
    const line = screen.getByTestId(`price-provenance-${NO_AGENT_SLOT.bid_ref}`).textContent ?? ''
    expect(line).toContain('no bidding agent')
    expect(line).not.toContain('did not answer')
    expect(line).not.toContain('the number above')
  })
})

/**
 * THE OTHER SHOP NOBODY DIALLED — the half of the never-asked class the first repair missed.
 *
 * `tier_0_no_agent` was fixed and `fan_out_capacity_exhausted` was not, so the same false
 * sentence went on being printed in the same slot: `collect.py::FAN_OUT_CAPACITY_REASON` is
 * documented "**The exchange never asked, because it had no worker free to ask with** … Both are
 * the EXCHANGE's condition, not the store's", and the card said the shop did not answer. It is
 * reachable on any busy auction — `_unusable_because` mints it whenever the fan-out stamped
 * `exchange_not_asked`, which a bounded pool with every worker held and a per-call `max_workers`
 * cap both do — and `collect_bids` then makes a list-price stand-in out of it (`_list_price_bid`,
 * `fallback=True`, `fallback_reason=reason`) for EVERY non-`None` reason, exactly as it does for
 * a shop with no agent.
 */
describe('a shop the exchange had no worker free to ask', () => {
  const CAPACITY_SLOT: ShortlistSlot = {
    ...NO_AGENT_SLOT,
    fallback_reason: FAN_OUT_CAPACITY_FALLBACK_FAMILY,
  }

  it('does not say a shop nobody dialled declined to answer', () => {
    render(<ShortlistView shortlist={one(CAPACITY_SLOT)} onAccept={vi.fn()} />)
    const line = screen.getByTestId(`price-provenance-${CAPACITY_SLOT.bid_ref}`).textContent ?? ''
    expect(line).not.toContain('did not answer')
    expect(line).toContain('Nobody asked this shop for a price')
    expect(line).toContain('turned nothing down')
    // Still Proxyshop's price, and still the exchange's own token, unchanged.
    expect(line).toContain('Proxyshop’s, not this shop’s')
    expect(line).toContain('fan_out_capacity_exhausted')
  })

  it('does not call an in-network shop a crawl result either', () => {
    // The other direction of the same rule. A shop with no bidding agent is on the roster from
    // its catalogue alone, and saying so is true of it. A shop the exchange simply had no worker
    // free to dial may hold a bid endpoint and answer every other auction, so "Proxyshop found
    // it by crawling" would be the same overclaim pointed the other way.
    render(<ShortlistView shortlist={one(CAPACITY_SLOT)} onAccept={vi.fn()} />)
    const line = screen.getByTestId(`price-provenance-${CAPACITY_SLOT.bid_ref}`).textContent ?? ''
    expect(line).not.toContain('found it by crawling')
    expect(line).not.toContain('no bidding agent')
    expect(line).toContain('at the list price on its own roster row')
  })

  it('says the same thing when the stand-in carried no price to attribute', () => {
    render(
      <ShortlistView shortlist={one({ ...CAPACITY_SLOT, price: null })} onAccept={vi.fn()} />,
    )
    const line = screen.getByTestId(`price-provenance-${CAPACITY_SLOT.bid_ref}`).textContent ?? ''
    expect(line).not.toContain('did not answer')
    expect(line).toContain('Nobody asked this shop for a price')
    expect(line).not.toContain('the number above')
  })

  it('holds the class and nothing but the class', () => {
    // The membership rule itself, driven directly, because the sentence above is only as good
    // as this list. Both members of the never-asked class, and the ten families that mean a
    // solicitation DID go out — `collect.py::FALLBACK_REASONS`, checked one by one.
    expect(NEVER_ASKED_FALLBACK_FAMILIES).toEqual([
      NO_AGENT_FALLBACK_FAMILY,
      FAN_OUT_CAPACITY_FALLBACK_FAMILY,
    ])
    expect(neverAsked('tier_0_no_agent:no_bid_endpoint')).toBe(true)
    expect(neverAsked('fan_out_capacity_exhausted')).toBe(true)
    for (const asked of [
      'no_response',
      'response_timed_out',
      'response_after_deadline',
      'response_carried_no_bid',
      'response_not_stamped',
      'arrival_stamp_unparseable',
      'bid_price_unreconcilable',
      'bid_claim_unprovenanced',
      'store_declined:no_matching_product',
      'store_refused:422',
    ]) {
      expect(neverAsked(asked)).toBe(false)
    }
    // A family this copy has not caught up with falls to the asked sentence, which is the one
    // that names the exchange's own token beside it rather than inventing a cause.
    expect(neverAsked('a_word_this_app_has_never_seen')).toBe(false)
    expect(neverAsked(null)).toBe(false)
  })
})

/**
 * WHICH BADGE BELONGS TO WHICH PROMISE.
 *
 * Nothing dedupes a store's own claim keys: `exchange.ranking.serving.shortlist_commitments`
 * appends every schema-valid `Claim` ("`offer["commitments"]` is whatever the store wrote") and
 * `buyer_svc.accept.labels.slot_commitments` keeps them all. So two promises under one name is a
 * shape a shop can send today, and the fold joined its rows to the card's badges with
 * `find(key === key)` — which gave every promise under that name the FIRST one's badge. A shop
 * chooses the order of its own claims array, so a shop chose which badge its weakest promise
 * inherited, in the one place a shopper goes to decide whom to believe.
 */
describe('two promises under one name', () => {
  const TWO_CLAIMS = [
    {
      claim_id: null,
      key: 'free_returns',
      claim_type: 'return_policy',
      value: '30 day window',
      unit: null,
      source_span: null,
      provenance: {
        source: 'owner_statement',
        ref: 'envelope:gaiaherbs.com:demo-1#free_returns',
        observed_at: '2026-01-01T00:00:00Z',
        authority_rank: 1,
      },
    },
    {
      claim_id: null,
      key: 'free_returns',
      claim_type: 'return_policy',
      value: 'lifetime, no questions',
      unit: null,
      source_span: null,
      provenance: {
        source: 'seller_asserted',
        ref: 'pitch:gaiaherbs.com:1#free_returns',
        observed_at: '2026-09-08T19:23:18Z',
        authority_rank: 5,
      },
    },
  ]

  /** The card's half of the same two rows, labelled as the service labels them. */
  const TWO_COMMITMENTS = [
    { key: 'free_returns', value: '30 day window', unit: null, label: LABEL_STORE_CONFIRMED },
    { key: 'free_returns', value: 'lifetime, no questions', unit: null, label: LABEL_UNVERIFIED },
  ]

  it('does not hand the unverified one the verified one’s badge', async () => {
    const { fetcher } = recordingFetcher(recordBodyWith({ commitments: TWO_CLAIMS }))
    render(
      <ShortlistView
        shortlist={one({ ...LIVE_SLOT, commitments: TWO_COMMITMENTS })}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(LIVE_BID)
    const badges = screen
      .getAllByTestId(`record-claim-label-${LIVE_BID}`)
      .map((badge) => badge.textContent)
    expect(badges).toEqual([LABEL_STORE_CONFIRMED, LABEL_UNVERIFIED])
    // And the weak promise's own row carries the weak badge, beside the evidence that earned it.
    const rows = screen.getAllByTestId(`record-claim-${LIVE_BID}`)
    expect(rows).toHaveLength(2)
    const asserted = rows[1]!.textContent ?? ''
    expect(asserted).toContain('lifetime, no questions')
    expect(asserted).toContain('seller_asserted')
    expect(asserted).toContain(LABEL_UNVERIFIED)
    expect(asserted).not.toContain(LABEL_STORE_CONFIRMED)
  })

  it('shows no badge at all, and says why, when the two lists do not line up', async () => {
    // The join is by POSITION and the position is CHECKED. Here the card carries a third
    // promise the record's reader dropped, so position 0 on one side is not position 0 on the
    // other and the key is not unique — the one shape where no sound join exists. A badge is a
    // verification signal, so the panel shows none and says so rather than guessing.
    const { fetcher } = recordingFetcher(recordBodyWith({ commitments: TWO_CLAIMS }))
    render(
      <ShortlistView
        shortlist={one({
          ...LIVE_SLOT,
          commitments: [
            { key: 'ships_in_days', value: 2, unit: 'days', label: LABEL_UNVERIFIED },
            ...TWO_COMMITMENTS,
          ],
        })}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(LIVE_BID)
    expect(screen.queryAllByTestId(`record-claim-label-${LIVE_BID}`)).toHaveLength(0)
    const said = screen.getAllByTestId(`record-claim-unjoined-${LIVE_BID}`)
    expect(said).toHaveLength(2)
    expect(said[0]!.textContent ?? '').toContain('2 promises under the name free_returns')
    expect(said[0]!.textContent ?? '').toContain('shows none of them here')
  })

  it('still joins by a key that is unique, when the positions disagree — the honest direction', async () => {
    // A join that refused whenever the positions disagreed would be no more useful than one
    // that never checked: the ordinary drift is one row dropped on one side, and a key that
    // names exactly one promise on the card is still a sound join. This must not go silent.
    const { fetcher } = recordingFetcher(
      recordBodyWith({ commitments: [RECORD_BODY.shortlist.slots[0]!.commitments[1]] }),
    )
    render(
      <ShortlistView
        shortlist={one({ ...LIVE_SLOT })}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(LIVE_BID)
    const badges = screen
      .getAllByTestId(`record-claim-label-${LIVE_BID}`)
      .map((badge) => badge.textContent)
    expect(badges).toEqual([LABEL_UNVERIFIED])
    expect(screen.queryAllByTestId(`record-claim-unjoined-${LIVE_BID}`)).toHaveLength(0)
  })
})

/**
 * WHOSE NUMBER THE AUTHORITY RANK IS.
 *
 * `contracts.protocol.Provenance` validates `authority_rank` as `ge=1` and says why —
 * "validated as `>= 1` rather than pinned to that table, because a hook may legitimately
 * down-rank a stale observation" — and `PROVENANCE_AUTHORITY_RANK`, the table it would be
 * checked against, is read nowhere outside `packages/contracts`. So the number is the BID's,
 * and the fold printed it under "how strongly this network rates that kind of evidence": the
 * platform's authority, over a seller's number, on the surface where a shopper decides whom
 * to believe.
 */
describe('the rank a claim gives its own evidence', () => {
  const SELLER_RANKED_ONE = [
    {
      claim_id: null,
      key: 'free_returns',
      claim_type: 'return_policy',
      value: 'lifetime',
      unit: null,
      source_span: null,
      provenance: {
        source: 'seller_asserted',
        ref: 'pitch:gaiaherbs.com:1#free_returns',
        observed_at: '2026-09-08T19:23:18Z',
        // The shop's own hook, writing the rank the contract publishes for `owner_statement`
        // onto a claim it asserts itself. Nothing between that hook and this page compares the
        // two, which is precisely why the page may not call it the network's rating.
        authority_rank: 1,
      },
    },
  ]

  it('does not print a bid-written rank as the network’s own rating', async () => {
    const { fetcher } = recordingFetcher(recordBodyWith({ commitments: SELLER_RANKED_ONE }))
    render(
      <ShortlistView
        shortlist={one({
          ...LIVE_SLOT,
          commitments: [{ key: 'free_returns', value: 'lifetime', unit: null, label: LABEL_UNVERIFIED }],
        })}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(LIVE_BID)
    const store = screen.getByTestId(`record-store-${LIVE_BID}`).textContent ?? ''
    expect(store).not.toContain('how strongly this network rates')
    expect(store).toContain('How strongly the shop rates that evidence')
    expect(store).toContain('the claim’s own number for its own evidence')
    // The SCALE is still the network's and is still stated: D30 publishes it, and a number with
    // no direction on it is unreadable.
    expect(store).toContain('1 is the strongest evidence this network records')
    expect(store).toContain('does not check it against the rank published')
  })

  it('says the shop stated none rather than that the network did not rate it', async () => {
    const { fetcher } = recordingFetcher(
      recordBodyWith({
        commitments: [
          {
            ...SELLER_RANKED_ONE[0],
            provenance: { ...SELLER_RANKED_ONE[0]!.provenance, authority_rank: 0 },
          },
        ],
      }),
    )
    render(
      <ShortlistView
        shortlist={one({
          ...LIVE_SLOT,
          commitments: [{ key: 'free_returns', value: 'lifetime', unit: null, label: LABEL_UNVERIFIED }],
        })}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(LIVE_BID)
    const store = screen.getByTestId(`record-store-${LIVE_BID}`).textContent ?? ''
    expect(store).not.toContain('how strongly this network rates')
    expect(store).toContain('the claim stated no rank for its own evidence')
  })
})

/**
 * A SLOT WITH NO PROMISES, and whose non-promise it is.
 *
 * The fold asserted "that is the ordinary answer for a shop the exchange stood in for" for every
 * slot with no claims, without ever consulting `slot.fallback` — so a shop that really bid, and
 * whose card correctly prints no price-provenance line at all, was described in its own record
 * as a shop the exchange stood in for. It bid. The two surfaces contradicted each other two
 * inches apart, and the card's own no-commitments line had the mirror-image defect.
 */
describe('a slot that promised nothing', () => {
  const bare = (patch: Partial<ShortlistSlot>): ShortlistSlot => ({
    ...LIVE_SLOT,
    commitments: null,
    ...patch,
  })

  it('does not call a shop that bid a shop the exchange stood in for', async () => {
    const { fetcher } = recordingFetcher(recordBodyWith({ commitments: null }))
    render(
      <ShortlistView
        shortlist={one(bare({ fallback: false }))}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(LIVE_BID)
    const said = screen.getByTestId(`record-no-claims-${LIVE_BID}`).textContent ?? ''
    expect(said).toContain('published no commitments')
    expect(said).not.toContain('stood in for')
    expect(said).toContain('This shop did bid')
    // And the card agrees with the fold: `fallback: false` prints no provenance line at all.
    expect(screen.queryByTestId(`price-provenance-${LIVE_BID}`)).toBeNull()
  })

  it('still says a stand-in is a stand-in — the honest direction', async () => {
    const { fetcher } = recordingFetcher(recordBodyWith({ commitments: null }))
    render(
      <ShortlistView
        shortlist={one(bare({ fallback: true, fallback_reason: 'no_response' }))}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(LIVE_BID)
    const said = screen.getByTestId(`record-no-claims-${LIVE_BID}`).textContent ?? ''
    expect(said).toContain('a shop the exchange stood in for')
    expect(said).not.toContain('This shop did bid')
  })

  it('will not guess whose it was when the exchange did not say', async () => {
    const { fetcher } = recordingFetcher(recordBodyWith({ commitments: null }))
    const unstated = bare({})
    delete (unstated as { fallback?: boolean | null }).fallback
    render(<ShortlistView shortlist={one(unstated)} onAccept={vi.fn()} recordFetcher={fetcher} />)
    await openTheRecord(LIVE_BID)
    const said = screen.getByTestId(`record-no-claims-${LIVE_BID}`).textContent ?? ''
    expect(said).toContain('did not say')
    // The hypothetical is allowed and the assertion is not: what may not appear is the sentence
    // that settles it, which is the one the fold printed for every slot.
    expect(said).not.toContain('a shop the exchange stood in for')
    expect(said).not.toContain('This shop did bid')
  })

  it('does not tell a shopper a stand-in’s shop promised nothing', () => {
    // The card, one paragraph above the fold's sentence and with the same defect: a stand-in is
    // rebuilt from the roster row with an empty claims list, so "this store promised nothing"
    // fired on every fallback — including the shop nobody had asked anything of.
    render(
      <ShortlistView
        shortlist={one(bare({ fallback: true, fallback_reason: NO_AGENT_FALLBACK_FAMILY }))}
        onAccept={vi.fn()}
      />,
    )
    const line = screen.getByTestId(`commitments-${LIVE_BID}`).textContent ?? ''
    expect(line).toContain('No commitments')
    expect(line).not.toContain('promised nothing')
    expect(line).toContain('stand-in')
  })

  it('still says a bidder that promised nothing promised nothing — the honest direction', () => {
    render(<ShortlistView shortlist={one(bare({ fallback: false }))} onAccept={vi.fn()} />)
    const line = screen.getByTestId(`commitments-${LIVE_BID}`).textContent ?? ''
    expect(line).toContain('this store promised nothing alongside the price')
  })
})

/**
 * A FAILED READ IS NOT AN ANSWER.
 *
 * The guard that keeps the fold to one request per mount was set before the await and never
 * cleared, so a single 503 — a restarted service, a dropped connection — ended the feature for
 * the session: every card's fold then carried the "could not be read" banner with no way to try
 * again.
 */
describe('reading the record again after a failure', () => {
  function flakyFetcher(body: unknown): { asked: string[]; fetcher: Fetcher } {
    const asked: string[] = []
    const fetcher: Fetcher = async (input) => {
      asked.push(input)
      return asked.length === 1
        ? jsonResponse({ detail: 'the service is restarting' }, 503)
        : jsonResponse(body, 200)
    }
    return { asked, fetcher }
  }

  it('asks again when a reader opens another fold', async () => {
    const { asked, fetcher } = flakyFetcher(RECORD_BODY)
    render(
      <ShortlistView
        shortlist={{ auction_id: RECORD_BODY.auction_id, slots: [LIVE_SLOT] }}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(LIVE_BID)
    await waitFor(() =>
      expect(screen.getByTestId(`record-failed-${LIVE_BID}`).textContent ?? '').toContain(
        'HTTP 503',
      ),
    )
    expect(asked).toHaveLength(1)

    // Shut it and open it again: the same gesture that started the first read.
    fireEvent.click(screen.getByTestId(`record-toggle-${LIVE_BID}`))
    await waitFor(() => expect(screen.queryByTestId(`record-platform-${LIVE_BID}`)).toBeNull())
    await openTheRecord(LIVE_BID)
    await waitFor(() => expect(asked).toHaveLength(2))
    // The record arrived, the banner is gone, and the panel is showing what it could not before.
    await waitFor(() => expect(screen.queryByTestId(`record-failed-${LIVE_BID}`)).toBeNull())
    expect(screen.getByTestId(`record-store-${LIVE_BID}`).textContent ?? '').toContain(
      'owner_statement',
    )
  })

  it('still reads once when the read succeeded, however many folds are opened', async () => {
    // The honest direction: the retry must not turn one read per mount into one per click.
    const { asked, fetcher } = recordingFetcher(RECORD_BODY)
    render(
      <ShortlistView
        shortlist={{ auction_id: RECORD_BODY.auction_id, slots: [LIVE_SLOT] }}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(LIVE_BID)
    fireEvent.click(screen.getByTestId(`record-toggle-${LIVE_BID}`))
    await waitFor(() => expect(screen.queryByTestId(`record-platform-${LIVE_BID}`)).toBeNull())
    await openTheRecord(LIVE_BID)
    expect(asked).toHaveLength(1)
  })
})

/**
 * A SECOND AUCTION UNDER A LIVE MOUNT — the guard and its own dependency, made to agree.
 *
 * `readRecord` lists `shortlist.auction_id` in its dependency array, so the callback follows the
 * auction; the guard beside it used to be a boolean, so the READ did not. A mount that outlived
 * one auction therefore answered questions about a record it never requested: the panel asserts
 * "the record names no candidate with this bid reference" and "The record carries no ranking for
 * this candidate", which are statements about the PREVIOUS auction's body.
 *
 * Not reachable through `Journey` today — `Journey.tsx` keys `ShortlistView` on the attempt, so a
 * second auction remounts — and that is exactly why it is pinned here rather than left to the
 * keying: nothing in `ShortlistView` can see that key, and the correctness of a guard should not
 * rest on a caller's prop nobody in this file can check.
 */
describe('a second auction under the same mount', () => {
  const SECOND_AUCTION = 'auction-11111111-2222-3333-4444-555555555555'
  const SECOND_BID = `${SECOND_AUCTION}:otherherbs.com`

  /** `RECORD_BODY` re-keyed to a second auction, as the service would answer for one. */
  function secondBody(): unknown {
    const body = JSON.parse(JSON.stringify(RECORD_BODY)) as {
      auction_id: string
      shortlist: { auction_id: string; slots: Record<string, unknown>[] }
      ranked: Record<string, unknown>[]
    }
    body.auction_id = SECOND_AUCTION
    body.shortlist.auction_id = SECOND_AUCTION
    body.shortlist.slots[0] = { ...body.shortlist.slots[0], bid_ref: SECOND_BID }
    body.ranked[0] = { ...body.ranked[0], bid_ref: SECOND_BID }
    return body
  }

  /** Answers each auction path with that auction's own body, and records what it was asked. */
  function perAuctionFetcher(): { fetcher: Fetcher; asked: string[] } {
    const asked: string[] = []
    const fetcher: Fetcher = async (input) => {
      asked.push(input)
      return jsonResponse(input.includes(SECOND_AUCTION) ? secondBody() : RECORD_BODY, 200)
    }
    return { fetcher, asked }
  }

  it('reads the new auction’s record rather than answering from the old one', async () => {
    const { asked, fetcher } = perAuctionFetcher()
    const { rerender } = render(
      <ShortlistView
        shortlist={{ auction_id: RECORD_BODY.auction_id, slots: [LIVE_SLOT] }}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(LIVE_BID)
    expect(asked).toEqual([`/buyer/auctions/${RECORD_BODY.auction_id}`])

    rerender(
      <ShortlistView
        shortlist={{
          auction_id: SECOND_AUCTION,
          slots: [{ ...LIVE_SLOT, auction_id: SECOND_AUCTION, bid_ref: SECOND_BID }],
        }}
        onAccept={vi.fn()}
        recordFetcher={fetcher}
      />,
    )
    await openTheRecord(SECOND_BID)
    await waitFor(() => expect(asked).toHaveLength(2))
    expect(asked[1]).toBe(`/buyer/auctions/${SECOND_AUCTION}`)

    // And it says what the second record holds, not that it holds nothing about this bid.
    const platform = screen.getByTestId(`record-platform-${SECOND_BID}`).textContent ?? ''
    expect(platform).not.toContain('names no candidate with this bid reference')
    expect(platform).not.toContain('carries no ranking for this candidate')
    expect(screen.getByTestId(`record-store-${SECOND_BID}`).textContent ?? '').toContain(
      'owner_statement',
    )
  })

  it('still reads once across a rerender of the same auction — the honest direction', async () => {
    // The guard holds an auction id so that it can tell two auctions apart, not so that it can
    // forget: a mount re-rendered for any other reason (a `busy` flip, an accept landing) must
    // not turn one read per auction into one per render.
    const { asked, fetcher } = perAuctionFetcher()
    const view = (busy: boolean) => (
      <ShortlistView
        shortlist={{ auction_id: RECORD_BODY.auction_id, slots: [LIVE_SLOT] }}
        onAccept={vi.fn()}
        busy={busy}
        recordFetcher={fetcher}
      />
    )
    const { rerender } = render(view(false))
    await openTheRecord(LIVE_BID)
    expect(asked).toHaveLength(1)

    rerender(view(true))
    fireEvent.click(screen.getByTestId(`record-toggle-${LIVE_BID}`))
    await waitFor(() => expect(screen.queryByTestId(`record-platform-${LIVE_BID}`)).toBeNull())
    await openTheRecord(LIVE_BID)
    expect(asked).toHaveLength(1)
  })
})

/**
 * A SHOP THAT ANSWERED IS NOT A SHOP THAT WAS SILENT — the other half of the same sweep.
 *
 * The card had two sentences for twelve families, so everything that was not `tier_0_no_agent`
 * read "the shop did not answer this auction". Eight of those families mean the shop ANSWERED:
 * `store_declined` is the store-agent contract's published 204, and this app's own gloss for it
 * — `journey/WhyEmpty.tsx::describeDecline`, one screen away from this card — says "that store
 * was asked, it answered, and its answer was no". The two surfaces made opposite statements
 * about the same shop, and the false one was on the card a shopper decides from.
 */
describe('a shop whose answer the exchange could not use', () => {
  const declined: ShortlistSlot = {
    ...NO_AGENT_SLOT,
    fallback_reason: 'store_declined:no_matching_product',
  }

  it('does not say a shop that answered stayed silent', () => {
    render(<ShortlistView shortlist={one(declined)} onAccept={vi.fn()} />)
    const line = screen.getByTestId(`price-provenance-${declined.bid_ref}`).textContent ?? ''
    expect(line).not.toContain('did not answer')
    expect(line).not.toContain('Nobody asked this shop')
    expect(line).toContain('could not use a price from this shop')
    // Still Proxyshop's number, still the exchange's own token, and still no invented cause.
    expect(line).toContain('Proxyshop’s, not this shop’s')
    expect(line).toContain('nobody at this shop quoted the number above')
    expect(line).toContain('store_declined:no_matching_product')
  })

  it('says the same for a family this copy of the vocabulary has never seen', () => {
    // The exchange grew this vocabulary twice already. An unrecognised family must land on the
    // sentence that asserts nothing about the shop — never on "it did not answer", which is the
    // direction the old two-way split failed in.
    render(
      <ShortlistView
        shortlist={one({ ...declined, fallback_reason: 'some_word_added_after_this_page' })}
        onAccept={vi.fn()}
      />,
    )
    const line = screen.getByTestId(`price-provenance-${declined.bid_ref}`).textContent ?? ''
    expect(line).not.toContain('did not answer')
    expect(line).not.toContain('Nobody asked this shop')
    expect(line).toContain('could not use a price from this shop')
    expect(line).toContain('some_word_added_after_this_page')
  })

  it('does not call a shop that was still answering when the window shut a silent one', () => {
    // `response_timed_out` exists because a slow store was indistinguishable from a dead one —
    // "24 samples across 4 live agents answered in 1.97 s – 4.73 s against a 3.0 s window …
    // every entry read `no_response`". Collapsing it back into silence on the card would undo
    // that fix one layer further out.
    render(
      <ShortlistView
        shortlist={one({ ...declined, fallback_reason: 'response_timed_out' })}
        onAccept={vi.fn()}
      />,
    )
    const line = screen.getByTestId(`price-provenance-${declined.bid_ref}`).textContent ?? ''
    expect(line).not.toContain('did not answer')
    expect(line).toContain('could not use a price from this shop')
  })

  it('still says the one family that really means nothing came back — the honest direction', () => {
    // `no_response` is the only family in the twelve that licenses that sentence, and it must
    // keep it: a page that refused to say anything about any shop would be no more honest than
    // one that said the wrong thing about all of them.
    expect(askedAndSilent('no_response')).toBe(true)
    expect(askedAndSilent('store_declined:no_matching_product')).toBe(false)
    expect(askedAndSilent('response_timed_out')).toBe(false)
    expect(askedAndSilent(null)).toBe(false)
    render(
      <ShortlistView shortlist={one({ ...declined, fallback_reason: 'no_response' })} onAccept={vi.fn()} />,
    )
    const line = screen.getByTestId(`price-provenance-${declined.bid_ref}`).textContent ?? ''
    expect(line).toContain('did not answer this auction')
    expect(line).not.toContain('could not use a price from this shop')
  })
})

/**
 * WHAT THE PLATFORM BLOCK MAY CLAIM ABOUT ITSELF.
 *
 * The block asserted "every other line here is reachable from no bid at all, so a shop cannot
 * move one of them by what it says", and the crawled-name rows do not meet that bar:
 * `exchange.ranking.serving.shortlist_product` publishes `identity` only under
 * `if identity and _text(identity_ref) == product_ref`, and `product_ref` on that same line is
 * `read(offer, "product_ref", None)` — the STORE's own offer. So a bid naming a different
 * product cannot change the crawled name, but it can decide whether there is one, which is a
 * weaker claim than the one the block was making about itself.
 */
describe('the platform block’s claim about its own rows', () => {
  it('does not say a bid can move nothing here, and says what a bid can move', async () => {
    const { fetcher } = recordingFetcher(RECORD_BODY)
    render(<ShortlistView shortlist={one(LIVE_SLOT)} onAccept={vi.fn()} recordFetcher={fetcher} />)
    await openTheRecord(LIVE_BID)
    const platform = screen.getByTestId(`record-platform-${LIVE_BID}`).textContent ?? ''
    expect(platform).not.toContain('a shop cannot move one of them')
    expect(platform).toContain('joined on that same reference')
    expect(platform).toContain('can take the name away')
    // The attribution that IS true of every row survives: none of this is a shop's prose.
    expect(platform).toContain('no shop wrote a word of any of them')
  })
})
