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
})
