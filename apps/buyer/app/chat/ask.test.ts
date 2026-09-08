/**
 * `POST /buyer/chat/ask`, from the browser's side.
 *
 * What is pinned here is the boundary, not the layout: what this page is willing to SEND
 * (an auction id and a question, and nothing that could become a fact) and what it is
 * willing to ACCEPT (an answer in the platform's voice, with each shop's own words carried
 * separately and never folded into it).
 */
import { describe, expect, it, vi } from 'vitest'

import {
  ASK_PATH,
  LaunderedVoiceError,
  MAX_QUESTION_CHARS,
  MalformedAnswerError,
  askAboutShortlist,
  readAnswer,
  sharesARun,
} from './ask'
import { IdentityLeakError } from './session'

const AUCTION = 'auction-1'

const SHOP_PITCH =
  'We have knitted these in Yorkshire since 1974 and we will take it back for any reason ' +
  'inside a month, no questions asked.'

function body(overrides: Record<string, unknown> = {}) {
  return {
    auction_id: AUCTION,
    question: 'what is the return policy?',
    answer:
      'woolworks.example published a free returns window of 30 days as an owner statement.',
    answer_source: 'assembled',
    grounds: [
      {
        slot: 'fit',
        bid_ref: `${AUCTION}:woolworks.example`,
        store_domain: 'woolworks.example',
        voice: 'shop_claim',
        topic: 'commitment',
        key: 'free returns',
        value: '30 days',
        attribution: 'woolworks.example published this as owner statement, authority rank 1',
        observed_at: '2026-01-01T00:00:00Z',
      },
    ],
    not_held: [],
    shop_messages: [
      {
        slot: 'fit',
        bid_ref: `${AUCTION}:woolworks.example`,
        store_domain: 'woolworks.example',
        message: SHOP_PITCH,
      },
    ],
    ranking_recorded: true,
    ...overrides,
  }
}

function ok(payload: unknown) {
  return vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 }))
}

describe('what this page sends', () => {
  it('sends an auction id and a question, and no field an answer could be built from', async () => {
    const fetcher = ok(body())

    await askAboutShortlist(AUCTION, '  what is the return policy?  ', fetcher)

    expect(fetcher).toHaveBeenCalledTimes(1)
    const [path, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe(ASK_PATH)
    expect(init.method).toBe('POST')
    const sent = JSON.parse(String(init.body)) as Record<string, unknown>
    // Exactly two keys. Not "the slots are omitted" — there is no key they could be in, and
    // that is the D55 property this whole route is shaped around.
    expect(Object.keys(sent).sort()).toEqual(['auction_id', 'question'])
    expect(sent).toEqual({ auction_id: AUCTION, question: 'what is the return policy?' })
  })

  it('truncates at the same ceiling the service refuses at, rather than sending a 422', async () => {
    const fetcher = ok(body())

    await askAboutShortlist(AUCTION, 'x'.repeat(MAX_QUESTION_CHARS + 500), fetcher)

    const [, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit]
    const sent = JSON.parse(String(init.body)) as Record<string, unknown>
    expect((sent.question as string).length).toBe(MAX_QUESTION_CHARS)
  })

  it('refuses to send an empty question rather than asking the service about nothing', async () => {
    const fetcher = ok(body())

    await expect(askAboutShortlist(AUCTION, '   ', fetcher)).rejects.toBeInstanceOf(
      MalformedAnswerError,
    )
    expect(fetcher).not.toHaveBeenCalled()
  })
})

describe('what this page accepts', () => {
  it('reads the answer, its source, its grounds and each shop’s own words apart', async () => {
    const answer = await askAboutShortlist(AUCTION, 'what is the return policy?', ok(body()))

    expect(answer.answerSource).toBe('assembled')
    expect(answer.grounds).toHaveLength(1)
    expect(answer.grounds[0]?.voice).toBe('shop_claim')
    expect(answer.grounds[0]?.attribution).toContain('woolworks.example published this')
    expect(answer.shopMessages).toEqual([
      {
        slot: 'fit',
        bid_ref: `${AUCTION}:woolworks.example`,
        store_domain: 'woolworks.example',
        message: SHOP_PITCH,
      },
    ])
    // The shop's words are on their own field and nowhere inside the platform's sentence.
    expect(answer.answer).not.toContain('Yorkshire')
    expect(answer.rankingRecorded).toBe(true)
  })

  it('carries a refusal with what the platform would have needed', async () => {
    const answer = await askAboutShortlist(
      AUCTION,
      'is any of this third-party tested?',
      ok(
        body({
          answer:
            'I don’t know about “third-party tested”: no shop on this shortlist has ' +
            'published a claim for it and the platform’s crawl did not record one.',
          grounds: [],
          not_held: [
            {
              subject: 'third-party tested',
              detail:
                'no shop on this shortlist has published a claim for it and the ' +
                'platform’s crawl did not record one',
            },
          ],
        }),
      ),
    )

    expect(answer.notHeld).toHaveLength(1)
    expect(answer.notHeld[0]?.subject).toBe('third-party tested')
    expect(answer.notHeld[0]?.detail).toContain('did not record one')
  })

  it('refuses an answer that reproduces a shop’s own message', () => {
    // The service screens for this already; this is the second layer, because the wire layer
    // is the one a future refactor most easily loosens. A page that printed a shop's
    // advertisement in the platform's voice would have sold the organic result.
    expect(() =>
      readAnswer(
        body({
          answer:
            'They will take it back for any reason inside a month, no questions asked, ' +
            'which is a strong policy.',
        }),
        AUCTION,
      ),
    ).toThrow(LaunderedVoiceError)
  })

  it('does not mistake an ordinary shared phrase for a laundered message', () => {
    // The silent-on-honest direction. Both voices describe the same product, so short
    // overlaps are unavoidable and must not trip the guard.
    expect(
      sharesARun('The free returns window is 30 days, published by the shop.', SHOP_PITCH),
    ).toBe(false)
    expect(() =>
      readAnswer(
        body({ answer: 'woolworks.example will take it back — that is the shop’s own claim.' }),
        AUCTION,
      ),
    ).not.toThrow()
  })

  it('throws on an empty answer rather than rendering a blank reply', () => {
    expect(() => readAnswer(body({ answer: '   ' }), AUCTION)).toThrow(MalformedAnswerError)
  })

  it('fails loudly when the service returns an identity-shaped key (R5)', async () => {
    const leaking = ok({ ...body(), email: 'shopper@example.com' })

    await expect(
      askAboutShortlist(AUCTION, 'what is the return policy?', leaking),
    ).rejects.toBeInstanceOf(IdentityLeakError)
  })

  it('carries the service’s own reason for a refusal rather than one shrug for all of them', async () => {
    const forgotten = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            detail:
              'the exchange holds no shortlist for this auction, so there is nothing here ' +
              'to answer about. It does not say why.',
          }),
          { status: 404 },
        ),
    )

    await expect(askAboutShortlist(AUCTION, 'how much?', forgotten)).rejects.toThrow(
      /no shortlist for this auction/,
    )
  })

  it('says the status when the service sent no readable reason', async () => {
    const broken = vi.fn(async () => new Response('<html>502</html>', { status: 502 }))

    await expect(askAboutShortlist(AUCTION, 'how much?', broken)).rejects.toThrow(/HTTP 502/)
  })
})
