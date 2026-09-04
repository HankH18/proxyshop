/**
 * T-073 — the client half of R14.
 *
 * The Python suite proves the *service* offers one structured prompt only for routed orders and
 * lands the answer as a ledger event. This one proves the browser does not undo either: an
 * un-routed order renders no form at all, the rendered form has no free-text field of any kind,
 * an answer outside the service's published options never leaves the browser, and one mount
 * submits once.
 *
 * Interaction is driven with `fireEvent` rather than `user-event`, which this workspace does not
 * carry — same as `shortlist/shortlist.test.tsx` and `intent/intent-confirm.test.tsx`.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { FeedbackPromptView } from './FeedbackPromptView'
import {
  FeedbackNotYoursError,
  LedgerUnavailableError,
  OrderNotRoutedError,
  PROMPT_PATH,
  SUBMIT_PATH,
  UnknownChoiceError,
  fetchFeedbackPrompt,
  orderBody,
  readPrompt,
  submitFeedback,
  type FeedbackOrder,
  type FeedbackPromptData,
  type FeedbackReceipt,
  type Fetcher,
} from './feedback'

// `@testing-library/react` registers its own cleanup only when the runner exposes `afterEach`
// globally, and this workspace's vitest projects do not set `globals: true`.
afterEach(cleanup)

const ORDER: FeedbackOrder = {
  order_ref: 'ord-e7-101',
  store_id: 'st-1',
  auction_id: 'auc-e7-3',
  routed: true,
}

const PROMPT: FeedbackPromptData = {
  order_ref: 'ord-e7-101',
  store_id: 'st-1',
  auction_id: 'auc-e7-3',
  question_id: 'matched_pitch',
  question: 'Did what arrived match what the store pitched?',
  input_type: 'single_select',
  options: [
    { id: 'yes_as_described', label: 'Yes — it was what the store described', matched_pitch: true },
    { id: 'not_as_described', label: 'No — it was not what the store described', matched_pitch: false },
    { id: 'never_arrived', label: 'It never arrived', matched_pitch: false },
  ],
}

const RECEIPT: FeedbackReceipt = {
  event_id: 'fb-abc123',
  kind: 'feedback',
  ts: '2026-01-01T00:00:00Z',
  order_ref: 'ord-e7-101',
  store_id: 'st-1',
  auction_id: 'auc-e7-3',
  matched_pitch: true,
  reason: 'yes_as_described',
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

describe('the wire', () => {
  it('reads the one prompt the service published', async () => {
    const fetcher = vi.fn<Fetcher>(async () =>
      jsonResponse({ offered: true, reason: '', prompt: PROMPT }),
    )

    const outcome = await fetchFeedbackPrompt(ORDER, fetcher)

    expect(outcome.offered).toBe(true)
    expect(outcome.prompt?.question_id).toBe('matched_pitch')
    expect(outcome.prompt?.options).toHaveLength(3)
    expect(fetcher.mock.calls[0]?.[0]).toBe(PROMPT_PATH)
  })

  it('reports an un-routed order as no prompt, with the reason', async () => {
    const fetcher = vi.fn<Fetcher>(async () =>
      jsonResponse({ offered: false, reason: 'the network did not route this order', prompt: null }),
    )

    const outcome = await fetchFeedbackPrompt({ order_ref: 'ord-1', routed: false }, fetcher)

    expect(outcome.offered).toBe(false)
    expect(outcome.prompt).toBeNull()
    expect(outcome.reason).toContain('did not route')
  })

  it('posts only the order fields the service reads, never the whole record', () => {
    const fat = {
      ...ORDER,
      email: 'dana.reyes@example.com',
      first_name: 'Dana',
      note: 'left by the bins',
    } as FeedbackOrder

    const body = orderBody(fat)

    expect(Object.keys(body).sort()).toEqual(['auction_id', 'order_ref', 'routed', 'store_id'])
    expect(JSON.stringify(body)).not.toContain('Dana')
    expect(JSON.stringify(body)).not.toContain('example.com')
  })

  it('sends `routed` only when it really is a boolean', () => {
    // The service's `routed` is a StrictBool: a string here would be a 422, and inventing a
    // boolean from one would be exactly the coercion R14 cannot afford.
    expect(orderBody({ order_ref: 'o', routed: undefined })).not.toHaveProperty('routed')
    expect(orderBody({ order_ref: 'o', routed: 'yes' as unknown as boolean })).not.toHaveProperty(
      'routed',
    )
    expect(orderBody({ order_ref: 'o', routed: false })).toHaveProperty('routed', false)
  })

  it('submits the option id and nothing that could hold free text', async () => {
    const fetcher = vi.fn<Fetcher>(async () => jsonResponse(RECEIPT, 201))

    const receipt = await submitFeedback(ORDER, PROMPT, 'yes_as_described', fetcher)

    expect(receipt.event_id).toBe('fb-abc123')
    expect(fetcher.mock.calls[0]?.[0]).toBe(SUBMIT_PATH)
    const sent = JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body)) as Record<string, unknown>
    expect(sent.response).toEqual({ question_id: 'matched_pitch', choice: 'yes_as_described' })
    for (const banned of ['comment', 'note', 'free_text', 'message', 'textarea']) {
      expect(JSON.stringify(sent)).not.toContain(banned)
    }
  })

  it('refuses an answer outside the published options before anything leaves the browser', async () => {
    const fetcher = vi.fn<Fetcher>(async () => jsonResponse(RECEIPT, 201))

    await expect(
      submitFeedback(ORDER, PROMPT, 'it was fine, I am Dana Reyes', fetcher),
    ).rejects.toBeInstanceOf(UnknownChoiceError)
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('tells a buyer nothing false when the service offers a prompt this build cannot read', async () => {
    // MEASURED: `offered: true` with an unreadable prompt reported `offered: false, reason: ''`,
    // and the view then told the buyer "the network did not route this order" — false about a
    // routed order, and it sends them looking for a problem with their order.
    const fetcher = vi.fn<Fetcher>(async () =>
      jsonResponse({ offered: true, reason: '', prompt: { ...PROMPT, options: [] } }),
    )

    const outcome = await fetchFeedbackPrompt(ORDER, fetcher)

    expect(outcome.offered).toBe(false)
    expect(outcome.prompt).toBeNull()
    expect(outcome.reason).not.toContain('route')
    expect(outcome.reason).toContain('could not read')
  })

  it('separates the two 403s: not routed, and not yours', async () => {
    // MEASURED: both collapsed into OrderNotRoutedError, so a buyer looking at someone else's
    // order was told "the network did not route this order" — false about that order.
    const notYours = vi.fn<Fetcher>(async () =>
      jsonResponse({ detail: 'order was routed for a different buyer' }, 403),
    )
    await expect(
      submitFeedback(ORDER, PROMPT, 'yes_as_described', notYours),
    ).rejects.toBeInstanceOf(FeedbackNotYoursError)
  })

  it('reports an unconfirmed ledger write as such, carrying the id a retry must reuse', async () => {
    const fetcher = vi.fn<Fetcher>(async () =>
      jsonResponse(
        { detail: { message: 'unknown', event_id: 'fb-abc123', retry_with_event_id: true } },
        503,
      ),
    )

    const failure = await submitFeedback(ORDER, PROMPT, 'yes_as_described', fetcher).catch(
      (error: unknown) => error,
    )

    expect(failure).toBeInstanceOf(LedgerUnavailableError)
    expect((failure as LedgerUnavailableError).eventId).toBe('fb-abc123')
    // Not "try again": a blind retry is what the service refuses.
    expect((failure as LedgerUnavailableError).message).toContain('fb-abc123')
  })

  it('turns the service 403 into a routing refusal rather than a generic failure', async () => {
    const fetcher = vi.fn<Fetcher>(async () =>
      jsonResponse({ detail: { message: 'no', reason: 'the network did not route this order' } }, 403),
    )

    await expect(
      submitFeedback(ORDER, PROMPT, 'yes_as_described', fetcher),
    ).rejects.toBeInstanceOf(OrderNotRoutedError)
  })

  it('refuses a submission the service answered without a ledger event id', async () => {
    const fetcher = vi.fn<Fetcher>(async () => jsonResponse({ ok: true }, 201))

    await expect(submitFeedback(ORDER, PROMPT, 'never_arrived', fetcher)).rejects.toThrow(
      /no ledger event id/,
    )
  })

  it('reads no prompt out of a body that offers fewer than two options', () => {
    expect(readPrompt({ ...PROMPT, options: [PROMPT.options[0]] })).toBeNull()
    expect(readPrompt({ ...PROMPT, question: '   ' })).toBeNull()
    expect(readPrompt(null)).toBeNull()
    expect(readPrompt('a prompt')).toBeNull()
  })
})

describe('the screen', () => {
  it('renders the question and every option the service sent', () => {
    render(<FeedbackPromptView prompt={PROMPT} onSubmit={() => {}} />)

    expect(screen.getByTestId('feedback-question')).toHaveTextContent(PROMPT.question)
    for (const option of PROMPT.options) {
      expect(screen.getByLabelText(option.label)).toBeInTheDocument()
    }
  })

  it('has no free-text field of any kind', () => {
    const { container } = render(<FeedbackPromptView prompt={PROMPT} onSubmit={() => {}} />)

    expect(container.querySelector('textarea')).toBeNull()
    expect(container.querySelector('[contenteditable]')).toBeNull()
    for (const input of Array.from(container.querySelectorAll('input'))) {
      expect(input.type).toBe('radio')
    }
  })

  it('renders no form at all for an order that was not routed', () => {
    const { container } = render(
      <FeedbackPromptView
        prompt={null}
        reason="the network did not route this order"
        onSubmit={() => {}}
      />,
    )

    expect(container.querySelector('form')).toBeNull()
    expect(container.querySelector('input')).toBeNull()
    expect(screen.getByTestId('no-prompt')).toHaveTextContent('did not route')
  })

  it('cannot be submitted until an option is picked', () => {
    const onSubmit = vi.fn()
    render(<FeedbackPromptView prompt={PROMPT} onSubmit={onSubmit} />)

    const send = screen.getByRole('button', { name: /send this/i })
    expect(send).toBeDisabled()

    fireEvent.click(screen.getByLabelText(PROMPT.options[1]!.label))
    expect(send).not.toBeDisabled()
  })

  it('submits the picked option id, once, however many times Send is clicked', async () => {
    const onSubmit = vi.fn()
    render(<FeedbackPromptView prompt={PROMPT} onSubmit={onSubmit} />)

    fireEvent.click(screen.getByLabelText(PROMPT.options[2]!.label))
    const send = screen.getByRole('button', { name: /send this/i })
    fireEvent.click(send)
    fireEvent.click(send)
    fireEvent.click(send)

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(onSubmit).toHaveBeenCalledWith('never_arrived')
  })

  it('shows the recorded answer instead of the form once there is a receipt', () => {
    const { container } = render(
      <FeedbackPromptView prompt={PROMPT} onSubmit={() => {}} receipt={RECEIPT} />,
    )

    expect(screen.getByTestId('feedback-recorded')).toHaveTextContent('yes_as_described')
    expect(container.querySelector('form')).toBeNull()
  })

  it('shows a refusal as an alert', () => {
    render(
      <FeedbackPromptView prompt={PROMPT} onSubmit={() => {}} error="that did not go through" />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent('that did not go through')
  })
})
