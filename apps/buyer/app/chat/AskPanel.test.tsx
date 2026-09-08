/**
 * The follow-up panel, rendered.
 *
 * What is pinned here is what a shopper can SEE. Three things, and each one is a claim the
 * page would be unsafe without:
 *
 * 1. **The two voices are two blocks.** The platform's answer and each shop's own message
 *    render in separate elements with separate attributions, and the shop's words never
 *    appear inside the platform's block.
 * 2. **A refusal is above the answer and outside every fold.** Somebody about to spend money
 *    has to see what the platform does not know without opening anything.
 * 3. **The panel says which voice wrote the answer** — assembled from the record, or written
 *    by the shopping agent and checked against it — rather than leaving a reader to infer it.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AskPanel } from './AskPanel'
import type { ChatAnswer } from './ask'

// `@testing-library/react` registers its own cleanup only when the runner exposes globals,
// and this workspace does not. Without it every render stacks in one document and
// `getByTestId` finds two of everything. Same line, same reason, as `journey.test.tsx`.
afterEach(cleanup)

const SHOP_PITCH =
  'We have knitted these in Yorkshire since 1974 and we will take it back for any reason.'

function answer(overrides: Partial<ChatAnswer> = {}): ChatAnswer {
  return {
    auctionId: 'auction-1',
    question: 'what is the return policy?',
    answer: 'woolworks.example published a free returns window of 30 days as an owner statement.',
    answerSource: 'assembled',
    grounds: [
      {
        slot: 'fit',
        bid_ref: 'auction-1:woolworks.example',
        store_domain: 'woolworks.example',
        voice: 'shop_claim',
        topic: 'commitment',
        key: 'free returns',
        value: '30 days',
        attribution: 'woolworks.example published this as owner statement, authority rank 1',
        observed_at: '2026-01-01T00:00:00Z',
      },
    ],
    notHeld: [],
    shopMessages: [
      {
        slot: 'fit',
        bid_ref: 'auction-1:woolworks.example',
        store_domain: 'woolworks.example',
        message: SHOP_PITCH,
      },
    ],
    rankingRecorded: true,
    ...overrides,
  }
}

async function ask(panelAnswer: ChatAnswer, question = 'what is the return policy?') {
  const onAsk = vi.fn(async () => panelAnswer)
  render(<AskPanel onAsk={onAsk} optionCount={4} />)
  fireEvent.change(screen.getByTestId('ask-input'), { target: { value: question } })
  fireEvent.click(screen.getByTestId('ask-send'))
  await waitFor(() => expect(screen.getByTestId('ask-answer-0')).toBeTruthy())
  return onAsk
}

describe('asking', () => {
  it('sends the question and shows it back as the shopper’s own turn', async () => {
    const onAsk = await ask(answer())

    expect(onAsk).toHaveBeenCalledWith('what is the return policy?')
    expect(screen.getByTestId('ask-question-0').textContent).toBe('what is the return policy?')
    // The composer is cleared, so a second question is not a re-send of the first.
    expect((screen.getByTestId('ask-input') as HTMLTextAreaElement).value).toBe('')
  })

  it('will not send an empty question', () => {
    const onAsk = vi.fn(async () => answer())
    render(<AskPanel onAsk={onAsk} />)

    fireEvent.change(screen.getByTestId('ask-input'), { target: { value: '   ' } })
    fireEvent.click(screen.getByTestId('ask-send'))

    expect(onAsk).not.toHaveBeenCalled()
  })

  it('prints the service’s own refusal on the turn that caused it, and keeps the panel', async () => {
    const onAsk = vi.fn(async () => {
      throw new Error('the exchange holds no shortlist for this auction. It does not say why.')
    })
    render(<AskPanel onAsk={onAsk} />)
    fireEvent.change(screen.getByTestId('ask-input'), { target: { value: 'how much?' } })
    fireEvent.click(screen.getByTestId('ask-send'))

    await waitFor(() => expect(screen.getByTestId('ask-failure-0')).toBeTruthy())
    expect(screen.getByTestId('ask-failure-0').textContent).toContain('no shortlist')
    // The composer survives a failure: one refused question must not end the conversation.
    expect(screen.getByTestId('ask-input')).toBeTruthy()
  })
})

describe('the two voices', () => {
  it('renders the shop’s own message in its own block, under the shop’s name', async () => {
    await ask(answer())

    const shop = screen.getByTestId('ask-shop-message-0-woolworks.example')
    expect(shop.getAttribute('data-voice')).toBe('store')
    expect(shop.textContent).toContain(SHOP_PITCH)
    expect(shop.textContent).toContain('in its own words')
  })

  it('keeps the shop’s words out of the platform’s block', async () => {
    await ask(answer())

    const platform = screen.getByTestId('ask-answer-0')
    expect(platform.getAttribute('data-voice')).toBe('platform')
    expect(platform.textContent).toContain('free returns window of 30 days')
    expect(platform.textContent).not.toContain('Yorkshire')
  })

  it('says the platform assembled the answer, when no model wrote it', async () => {
    await ask(answer())

    expect(screen.getByTestId('ask-source-0').textContent).toContain(
      'assembled from its own record',
    )
    expect(screen.getByTestId('ask-source-0').textContent).toContain('no model wrote this')
  })

  it('says a model wrote it, and that it was checked, when one did', async () => {
    await ask(answer({ answerSource: 'written' }))

    const source = screen.getByTestId('ask-source-0').textContent ?? ''
    expect(source).toContain('shopping agent’s words')
    expect(source).toContain('checked, line by line, against the record')
  })

  it('prints an answer_source this build does not know rather than hiding it', async () => {
    // Recognition only, the same rule the shortlist's provenance labels follow: a source
    // this page cannot name is still a source a reader is entitled to see.
    await ask(answer({ answerSource: 'transcribed-by-hand' }))

    expect(screen.getByTestId('ask-source-0').textContent).toContain('transcribed-by-hand')
  })

  it('labels every ground with the voice it is in, not only the shop’s claims', async () => {
    await ask(
      answer({
        grounds: [
          ...answer().grounds,
          {
            slot: 'fit',
            bid_ref: 'auction-1:woolworks.example',
            store_domain: 'woolworks.example',
            voice: 'platform',
            topic: 'price',
            key: 'price',
            value: '72.00 USD',
            attribution: 'the exchange’s published offer for this auction',
            observed_at: '',
          },
        ],
      }),
    )

    const grounds = screen.getByTestId('ask-grounds-0')
    expect(grounds.textContent).toContain('the shop claims this')
    expect(grounds.textContent).toContain('the platform holds this')
  })
})

describe('a refusal', () => {
  it('is shown above the answer and behind no fold at all', async () => {
    await ask(
      answer({
        answer:
          'I don’t know about “third-party tested”: no shop on this shortlist has published ' +
          'a claim for it and the platform’s crawl did not record one.',
        grounds: [],
        notHeld: [
          {
            subject: 'third-party tested',
            detail:
              'no shop on this shortlist has published a claim for it and the platform’s ' +
              'crawl did not record one',
          },
        ],
      }),
      'which of these is actually third-party tested?',
    )

    const refusal = screen.getByTestId('ask-not-held-0')
    expect(refusal.textContent).toContain('third-party tested')
    expect(refusal.textContent).toContain('did not record one')
    // Not inside a <details>: a shopper must not have to open anything to learn this.
    expect(refusal.closest('details')).toBeNull()
    // And it precedes the answer in the document, so it is read first.
    expect(
      refusal.compareDocumentPosition(screen.getByTestId('ask-answer-0')) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('says when the ranking is no longer held, rather than implying it was flat', async () => {
    await ask(answer({ rankingRecorded: false }))

    expect(screen.getByTestId('ask-no-ranking-0').textContent).toContain(
      'no longer holds the exchange’s recorded ranking',
    )
  })
})
