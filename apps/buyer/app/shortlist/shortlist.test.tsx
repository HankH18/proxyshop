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
  LABEL_FROM_THEIR_WEBSITE,
  LABEL_STORE_CONFIRMED,
  LABEL_UNVERIFIED,
  MissingAuctionReferenceError,
  NoPermalinkError,
  UnsafePermalinkError,
  acceptSlot,
  assertFollowable,
  followPermalink,
  labelTone,
  permalinkRefusal,
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
    discount: { type: 'percent', value: 10 },
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
    expect(discount).toContain('10')
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
