import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'
import type { AssistedDownload, HeldDocument, ReviewItem, ReviewQueue } from './api'
import { fileNameFor } from './naming'
import { openDashboard, Reply, serve, signedIn } from './test/dashboard'

const ENGINEERING = 'engineering@nyayalabs.example'
const OPS = 'ops@nyayalabs.example'
const REVIEW = '/api/months/2026-08/review'

function held(vendor: string, changes: Partial<HeldDocument> = {}): HeldDocument {
  const total = changes.total ?? '652.50'
  return {
    content_hash: `hash-${vendor}`,
    vendor,
    invoice_date: '2026-08-03',
    total,
    currency: 'USD',
    document_type: 'invoice',
    doubts: [],
    read_again: false,
    read_by: 'claude-haiku-4-5',
    file_name: `2026-08_${vendor}_${total}-USD.pdf`,
    file_url: `${REVIEW}/billing-documents/2026-08_${vendor}_${total}-USD.pdf`,
    usual_amount: null,
    usual_currency: null,
    source_accounts: [ENGINEERING],
    ...changes,
  }
}

function item(messageId: string, documents: HeldDocument[], changes: Partial<ReviewItem> = {}) {
  return {
    source_account: ENGINEERING,
    message_id: messageId,
    sender: 'Billing <billing@vendor.example>',
    subject: `Your ${documents[0]?.vendor ?? 'vendor'} invoice`,
    received_at: '2026-08-03T09:00:00+00:00',
    invoice_format: 'attachment',
    portal_link: null,
    reason: documents[0]?.doubts[0]?.reason ?? null,
    needs_manual_download: false,
    documents,
    message_ids: [messageId],
    ...changes,
  } satisfies ReviewItem
}

const SLACK = item(
  'm-slack',
  [
    held('Slack', {
      doubts: [
        { field: null, reason: 'the reader was unsure: the total is smudged' },
        { field: 'total', reason: 'the email says 625.50 and the document says 652.50' },
      ],
      usual_amount: '640.00',
      usual_currency: 'USD',
      source_accounts: [ENGINEERING, OPS],
      read_again: true,
    }),
  ],
  { sender: 'Slack <feedback@slack.example>', subject: 'Your Slack invoice is available' },
)

const FIGMA = item('m-figma', [
  held('Figma', {
    total: '190.00',
    doubts: [{ field: 'currency', reason: 'the currency is EUR; Figma usually bills in USD' }],
  }),
])

const NOTION = item('m-notion', [
  held('Notion', {
    total: '221.40',
    doubts: [{ field: 'invoice_date', reason: 'second invoice from Notion this month' }],
  }),
])

const ZOOM = item('m-zoom', [], {
  subject: 'Your Zoom invoice is ready',
  reason: 'manual download needed',
  invoice_format: 'portal_link',
  portal_link: 'https://zoom.example/billing/invoices/889',
  needs_manual_download: true,
})

function queueOf(...items: ReviewItem[]): ReviewQueue {
  return { month: '2026-08', items }
}

function actionPath(entry: ReviewItem, action: string): string {
  return `${REVIEW}/${encodeURIComponent(entry.source_account)}/${entry.message_id}/${action}`
}

function serveReview(queue: ReviewQueue | (() => unknown), answers: Record<string, unknown> = {}) {
  return serve(
    signedIn({ 'GET /api/months': { months: ['2026-08'] }, [`GET ${REVIEW}`]: queue, ...answers }),
  )
}

async function openReview(at = '/review') {
  openDashboard(at)
  return screen.findByRole('complementary', { name: 'Extracted fields' })
}

test('the three panes show the chosen item', async () => {
  serveReview(queueOf(SLACK, FIGMA))

  const fields = await openReview()

  const queue = screen.getByRole('list', { name: 'Emails needing review' })
  expect(within(queue).getAllByRole('button')).toHaveLength(2)
  expect(screen.getByRole('heading', { name: 'Needs review 2' })).toBeVisible()
  const chosen = within(queue).getByRole('button', { current: true })
  expect(chosen).toHaveTextContent('Slack')
  expect(chosen).toHaveTextContent('652.50 USD')
  expect(chosen).toHaveTextContent('The reader was unsure: the total is smudged')
  const document = screen.getByRole('region', { name: 'Document' })
  expect(document.querySelector('object')).toHaveAttribute(
    'data',
    `${REVIEW}/billing-documents/2026-08_Slack_652.50-USD.pdf`,
  )
  expect(within(document).getByRole('link', { name: /Open 2026-08_Slack/ })).toBeVisible()
  expect(within(fields).getByLabelText('Vendor')).toHaveValue('Slack')
  expect(within(fields).getByLabelText('Total')).toHaveValue('652.50')
  expect(within(fields).getByText(`${ENGINEERING}, ${OPS}`)).toBeVisible()
  expect(within(fields).getByText(/Slack <feedback@slack.example>/)).toBeVisible()
  expect(within(fields).getByText(/Your Slack invoice is available/)).toBeVisible()
  expect(within(fields).getByText(/A stronger model read this document again/)).toBeVisible()
})

test('a document read by rules says that no model read it', async () => {
  const byRules = item('m-rules', [
    held('Slack', {
      read_by: 'rules',
      doubts: [{ field: null, reason: 'the reader was unsure: read by rules, not by a model' }],
    }),
  ])
  serveReview(queueOf(byRules))

  const fields = await openReview()

  expect(within(fields).getByRole('note')).toHaveTextContent(
    /No model read this document\. Rules read it/,
  )
})

test('a document read by a model says nothing of rules', async () => {
  serveReview(queueOf(SLACK))

  const fields = await openReview()

  expect(within(fields).queryByText(/No model read this document/)).toBeNull()
})

test('doubted fields are marked and the reasons are given in plain words', async () => {
  serveReview(queueOf(SLACK))

  const fields = await openReview()

  const reasons = within(fields).getByRole('list', { name: 'Reasons for review' })
  expect(within(reasons).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
    'The reader was unsure: the total is smudged',
    'The email says 625.50 and the document says 652.50',
  ])
  expect(within(fields).getByLabelText('Total')).toHaveAccessibleDescription(/Doubted/)
  expect(within(fields).getByLabelText('Vendor')).not.toHaveAccessibleDescription(/Doubted/)
})

test('the vendor usual amount is shown beside the total', async () => {
  serveReview(queueOf(SLACK))

  const fields = await openReview()

  expect(within(fields).getByLabelText('Total')).toHaveAccessibleDescription(
    /Usually 640.00 USD/,
  )
})

test('the file name follows the fields as they are typed', async () => {
  serveReview(queueOf(SLACK))
  const fields = await openReview()
  expect(within(fields).getByText('2026-08_Slack_652.50-USD.pdf')).toBeVisible()

  const total = within(fields).getByLabelText('Total')
  await userEvent.clear(total)
  await userEvent.type(total, '625.5')

  expect(within(fields).getByText('2026-08_Slack_625.50-USD.pdf')).toBeVisible()

  await userEvent.selectOptions(within(fields).getByLabelText('Document type'), 'credit_note')

  expect(within(fields).getByText('2026-08_Slack_-625.50-USD.pdf')).toBeVisible()
})

test('approving sends the confirmed fields and then shows the next item', async () => {
  let queue = queueOf(SLACK, FIGMA)
  const calls = serveReview(() => queue, {
    [`POST ${actionPath(SLACK, 'approve')}`]: () => {
      queue = queueOf(FIGMA)
      return { action: 'approved' }
    },
  })
  const fields = await openReview()

  const total = within(fields).getByLabelText('Total')
  await userEvent.clear(total)
  await userEvent.type(total, '625.50')
  await userEvent.click(within(fields).getByRole('button', { name: 'Approve' }))

  await waitFor(() => expect(screen.getByLabelText('Vendor')).toHaveValue('Figma'))
  const approval = calls.find((call) => call.method === 'POST')
  expect(approval?.body).toEqual({
    documents: [
      {
        content_hash: 'hash-Slack',
        vendor: 'Slack',
        invoice_date: '2026-08-03',
        total: '625.50',
        currency: 'USD',
        document_type: 'invoice',
      },
    ],
  })
  expect(calls.filter((call) => call.path === REVIEW)).toHaveLength(2)
  expect(screen.getByRole('heading', { name: 'Needs review 1' })).toBeVisible()
})

test('a pending copy that could not be removed is reported after the decision', async () => {
  let queue = queueOf(SLACK, FIGMA)
  const warning =
    'The copy of 2026-08_Slack_652.50-USD.pdf in the pending folder could not be removed ' +
    '(Drive could not be reached). The approval stands; remove the copy by hand.'
  serveReview(() => queue, {
    [`POST ${actionPath(SLACK, 'approve')}`]: () => {
      queue = queueOf(FIGMA)
      return { action: 'approved', warnings: [warning] }
    },
  })
  const fields = await openReview()

  await userEvent.click(within(fields).getByRole('button', { name: 'Approve' }))

  await waitFor(() => expect(screen.getByLabelText('Vendor')).toHaveValue('Figma'))
  const queuePane = screen.getByRole('complementary', { name: 'Review queue' })
  expect(within(queuePane).getByRole('status')).toHaveTextContent(warning)
})

test('buttons are disabled while a decision is in flight', async () => {
  let answer: (value: unknown) => void = () => {}
  serveReview(queueOf(SLACK), {
    [`POST ${actionPath(SLACK, 'approve')}`]: () =>
      new Promise((resolve) => {
        answer = resolve
      }),
  })
  const fields = await openReview()

  await userEvent.click(within(fields).getByRole('button', { name: 'Approve' }))

  expect(within(fields).getByRole('button', { name: 'Approve' })).toBeDisabled()
  expect(within(fields).getByRole('button', { name: 'Not a billing document' })).toBeDisabled()
  answer(new Reply(422, { detail: { message: 'Nothing was changed.', problems: [] } }))
  await waitFor(() =>
    expect(within(fields).getByRole('button', { name: 'Approve' })).toBeEnabled(),
  )
})

test('a validation message from the server appears beside its field', async () => {
  serveReview(queueOf(SLACK), {
    [`POST ${actionPath(SLACK, 'approve')}`]: new Reply(422, {
      detail: {
        message: 'Nothing was changed. Correct the fields named and approve again.',
        problems: [
          {
            content_hash: 'hash-Slack',
            field: 'invoice_date',
            reason: 'The invoice date 2026-09-02 belongs to collection month 2026-09, not 2026-08',
          },
        ],
      },
    }),
  })
  const fields = await openReview()

  const date = within(fields).getByLabelText('Invoice date')
  await userEvent.clear(date)
  await userEvent.type(date, '2026-09-02')
  await userEvent.click(within(fields).getByRole('button', { name: 'Approve' }))

  await waitFor(() =>
    expect(date).toHaveAccessibleDescription(/belongs to collection month 2026-09/),
  )
  expect(date).toHaveAttribute('aria-invalid', 'true')
  expect(within(fields).getByLabelText('Total')).not.toHaveAttribute('aria-invalid')
  expect(within(fields).getByRole('alert')).toHaveTextContent('Nothing was changed.')
  expect(date).toHaveValue('2026-09-02')
})

test('an archive that cannot be reached is reported and the item stays to approve again', async () => {
  serveReview(queueOf(SLACK), {
    [`POST ${actionPath(SLACK, 'approve')}`]: new Reply(502, {
      detail:
        'The billing document could not be filed to the archive. Nothing was changed; try approving again.',
    }),
  })
  const fields = await openReview()

  await userEvent.click(within(fields).getByRole('button', { name: 'Approve' }))

  expect(await within(fields).findByRole('alert')).toHaveTextContent(
    'could not be filed to the archive. Nothing was changed',
  )
  expect(within(fields).getByRole('button', { name: 'Approve' })).toBeEnabled()
})

test('not a billing document asks for confirmation before it is sent', async () => {
  let queue = queueOf(SLACK, FIGMA)
  const calls = serveReview(() => queue, {
    [`POST ${actionPath(SLACK, 'reject')}`]: () => {
      queue = queueOf(FIGMA)
      return { action: 'rejected' }
    },
  })
  const fields = await openReview()

  await userEvent.click(within(fields).getByRole('button', { name: 'Not a billing document' }))

  expect(calls.some((call) => call.method === 'POST')).toBe(false)
  // The question is asked in a dialog, which stands in front of the fields until answered.
  const confirm = screen.getByRole('alertdialog', { name: 'Skip this email and delete its PDF?' })
  await userEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }))
  expect(calls.some((call) => call.method === 'POST')).toBe(false)

  await userEvent.click(within(fields).getByRole('button', { name: 'Not a billing document' }))
  await userEvent.click(screen.getByRole('button', { name: 'Yes, not a billing document' }))

  await waitFor(() => expect(screen.getByLabelText('Vendor')).toHaveValue('Figma'))
  expect(calls.filter((call) => call.method === 'POST').map((call) => call.path)).toEqual([
    actionPath(SLACK, 'reject'),
  ])
})

test('an empty queue says that nothing needs review', async () => {
  serveReview(queueOf())

  openDashboard('/review')

  expect(await screen.findByText('Nothing needs review for August 2026.')).toBeVisible()
  expect(screen.getByRole('heading', { name: 'Needs review 0' })).toBeVisible()
  expect(screen.queryByRole('complementary', { name: 'Extracted fields' })).toBeNull()
})

test('items needing a manual download are shown apart with their portal link', async () => {
  serveReview(queueOf(SLACK, ZOOM))

  await openReview()

  const manual = screen.getByRole('region', { name: 'Manual download needed' })
  expect(within(manual).getByText('Your Zoom invoice is ready')).toBeVisible()
  expect(within(manual).getByRole('link', { name: 'Open the portal link' })).toHaveAttribute(
    'href',
    'https://zoom.example/billing/invoices/889',
  )
  expect(within(manual).getByText(/download the PDF, and upload it here/)).toBeVisible()
  expect(within(manual).queryByRole('button', { name: 'Approve' })).toBeNull()
  expect(screen.getByRole('heading', { name: 'Needs review 1' })).toBeVisible()
})

test('the screen opens at the item named in the address', async () => {
  serveReview(queueOf(SLACK, FIGMA, NOTION))

  const fields = await openReview('/review?month=2026-08&email=m-figma')

  expect(within(fields).getByLabelText('Vendor')).toHaveValue('Figma')
  const queue = screen.getByRole('list', { name: 'Emails needing review' })
  expect(within(queue).getByRole('button', { current: true })).toHaveTextContent('Figma')
})

test('the queue is worked from the keyboard', async () => {
  serveReview(queueOf(SLACK, FIGMA, NOTION))
  await openReview()
  const queue = screen.getByRole('list', { name: 'Emails needing review' })
  within(queue).getByRole('button', { current: true }).focus()

  await userEvent.keyboard('{ArrowDown}')
  await userEvent.keyboard('{ArrowDown}')

  expect(within(queue).getByRole('button', { current: true })).toHaveTextContent('Notion')
  expect(within(queue).getByRole('button', { current: true })).toHaveFocus()
  expect(screen.getByLabelText('Vendor')).toHaveValue('Notion')

  await userEvent.keyboard('{ArrowUp}')

  expect(within(queue).getByRole('button', { current: true })).toHaveTextContent('Figma')
})

test('an email with several documents shows each and approves them together', async () => {
  const usage = held('Slack', { content_hash: 'hash-usage', total: '12.00' })
  const both = item('m-both', [SLACK.documents[0]!, usage])
  const calls = serveReview(queueOf(both), {
    [`POST ${actionPath(both, 'approve')}`]: { action: 'approved' },
  })
  const fields = await openReview()

  const tabs = within(fields).getAllByRole('tab')
  expect(tabs).toHaveLength(2)
  await userEvent.click(tabs[1]!)
  expect(within(fields).getByLabelText('Total')).toHaveValue('12.00')
  await userEvent.click(within(fields).getByRole('button', { name: 'Approve' }))

  await waitFor(() => expect(calls.some((call) => call.method === 'POST')).toBe(true))
  const approval = calls.find((call) => call.method === 'POST')?.body as {
    documents: { content_hash: string }[]
  }
  expect(approval.documents.map((d) => d.content_hash)).toEqual(['hash-Slack', 'hash-usage'])
})

test('the file name is formed the way the tool forms it', () => {
  expect(
    fileNameFor({
      vendor: 'Atlassian Pty. Ltd.',
      invoice_date: '2026-08-11',
      total: '1,200',
      currency: 'aud',
      document_type: 'invoice',
    }),
  ).toBe('2026-08_AtlassianPtyLtd_1,200-AUD.pdf')
  expect(
    fileNameFor({
      vendor: 'Figma',
      invoice_date: '2026-08-21',
      total: '40',
      currency: 'USD',
      document_type: 'credit_note',
    }),
  ).toBe('2026-08_Figma_-40.00-USD.pdf')
})

test('the chosen document links to its history', async () => {
  serveReview(queueOf(SLACK))

  const fields = await openReview('/review?month=2026-08')

  expect(
    within(fields).getByRole('link', { name: 'How this document was found, read and checked' }),
  ).toHaveAttribute('href', '/documents/hash-Slack?month=2026-08')
})

// Assisted download: a PDF uploaded for a portal link that needs a sign-in

const ZOOM_PDF = new File(['%PDF-1.7 zoom'], 'invoice-889.pdf', { type: 'application/pdf' })

function uploaded(outcome: AssistedDownload['outcome'], changes: Partial<AssistedDownload> = {}) {
  return {
    outcome,
    collection_month: '2026-08',
    source_account: ENGINEERING,
    message_id: 'm-zoom',
    subject: 'Your Zoom invoice is ready',
    portal_link: 'https://zoom.example/billing/invoices/889',
    file_name: '2026-08_Zoom_149.90-USD.pdf',
    size: 13,
    document: {
      vendor: 'Zoom',
      invoice_date: '2026-08-12',
      total: '149.90',
      currency: 'USD',
      document_type: 'invoice',
      doubts: [],
    },
    person: 'finance@nyayalabs.example',
    uploaded_at: '2026-09-29T10:30:00+00:00',
    warnings: [],
    ...changes,
  } satisfies AssistedDownload
}

async function openManual() {
  openDashboard('/review')
  return screen.findByRole('region', { name: 'Manual download needed' })
}

async function chooseAndUpload(manual: HTMLElement) {
  await userEvent.upload(
    within(manual).getByLabelText('PDF for Your Zoom invoice is ready'),
    ZOOM_PDF,
  )
  await userEvent.click(within(manual).getByRole('button', { name: 'Upload' }))
}

test('each email needing a manual download shows who sent it, when, where, and its link', async () => {
  serveReview(queueOf(ZOOM))

  const manual = await openManual()

  expect(within(manual).getByText('Billing <billing@vendor.example>')).toBeVisible()
  expect(within(manual).getByText('Your Zoom invoice is ready')).toBeVisible()
  expect(within(manual).getByText(`Arrived 3 Aug 2026 in ${ENGINEERING}`)).toBeVisible()
  expect(within(manual).getByRole('link', { name: 'Open the portal link' })).toHaveAttribute(
    'href',
    'https://zoom.example/billing/invoices/889',
  )
  expect(within(manual).getByLabelText('PDF for Your Zoom invoice is ready')).toHaveAttribute(
    'type',
    'file',
  )
  expect(within(manual).getByRole('button', { name: 'Upload' })).toBeDisabled()
})

test('uploading sends the PDF as it is, then reads the queue again and says it was filed', async () => {
  let queue = queueOf(SLACK, ZOOM)
  const calls = serveReview(() => queue, {
    [`POST ${actionPath(ZOOM, 'upload')}`]: () => {
      queue = queueOf(SLACK)
      return uploaded('collected')
    },
  })
  const manual = await openManual()

  await chooseAndUpload(manual)

  expect(await screen.findByRole('status')).toHaveTextContent(
    'Filed 2026-08_Zoom_149.90-USD.pdf and added to the summary.',
  )
  await waitFor(() =>
    expect(screen.queryByRole('region', { name: 'Manual download needed' })).toBeNull(),
  )
  const sent = calls.find((call) => call.method === 'POST')
  expect(sent?.path).toBe(actionPath(ZOOM, 'upload'))
  expect(sent?.body).toBe(ZOOM_PDF)
  expect(sent?.headers).toEqual({ 'Content-Type': 'application/pdf' })
  expect(calls.filter((call) => call.path === REVIEW)).toHaveLength(2)
})

test('an upload filed without reaching Drive says so', async () => {
  const warning =
    'Google Drive was not reached: The owner account ops@nyayalabs.example is not signed in ' +
    'to Google Drive. The billing document was filed on this machine only.'
  serveReview(queueOf(ZOOM), {
    [`POST ${actionPath(ZOOM, 'upload')}`]: uploaded('collected', { warnings: [warning] }),
  })
  const manual = await openManual()

  await chooseAndUpload(manual)

  expect(await screen.findByText(warning)).toBeVisible()
  expect(screen.getByText('Filed 2026-08_Zoom_149.90-USD.pdf and added to the summary.')).toBeVisible()
})

test('an upload held for review is opened in the queue with its reason', async () => {
  const reason = 'the reader was unsure: the date is smudged'
  const heldZoom = item('m-zoom', [
    held('Zoom', { total: '149.90', doubts: [{ field: null, reason }] }),
  ])
  let queue = queueOf(SLACK, ZOOM)
  serveReview(() => queue, {
    [`POST ${actionPath(ZOOM, 'upload')}`]: () => {
      queue = queueOf(SLACK, heldZoom)
      const answer = uploaded('held')
      return { ...answer, document: { ...answer.document, doubts: [{ field: null, reason }] } }
    },
  })
  const manual = await openManual()

  await chooseAndUpload(manual)

  expect(await screen.findByRole('status')).toHaveTextContent(
    `Held 2026-08_Zoom_149.90-USD.pdf for review: ${reason}.`,
  )
  await waitFor(() => expect(screen.getByLabelText('Vendor')).toHaveValue('Zoom'))
  expect(screen.getByRole('heading', { name: 'Needs review 2' })).toBeVisible()
})

test('an upload already collected elsewhere, and dated in another month, is said so', async () => {
  let queue = queueOf(ZOOM)
  serveReview(() => queue, {
    [`POST ${actionPath(ZOOM, 'upload')}`]: () => {
      queue = queueOf()
      return uploaded('already_collected', {
        collection_month: '2026-07',
        file_name: '2026-07_Zoom_149.90-USD.pdf',
      })
    },
  })
  const manual = await openManual()

  await chooseAndUpload(manual)

  expect(await screen.findByRole('status')).toHaveTextContent(
    'Already collected as 2026-07_Zoom_149.90-USD.pdf under July 2026. This email is now linked to it.',
  )
})

test('a refused upload says why beside the email and changes nothing', async () => {
  const calls = serveReview(queueOf(ZOOM), {
    [`POST ${actionPath(ZOOM, 'upload')}`]: new Reply(422, {
      detail: 'The file is not a PDF. Upload the PDF downloaded from the portal.',
    }),
  })
  const manual = await openManual()

  await chooseAndUpload(manual)

  expect(await within(manual).findByRole('alert')).toHaveTextContent('The file is not a PDF.')
  expect(within(manual).getByRole('button', { name: 'Upload' })).toBeEnabled()
  expect(calls.filter((call) => call.path === REVIEW)).toHaveLength(1)
  expect(screen.queryByRole('status')).toBeNull()
})

test('the upload button is disabled while the PDF is being sent', async () => {
  let answer: (value: unknown) => void = () => {}
  serveReview(queueOf(ZOOM), {
    [`POST ${actionPath(ZOOM, 'upload')}`]: () =>
      new Promise((resolve) => {
        answer = resolve
      }),
  })
  const manual = await openManual()

  await chooseAndUpload(manual)

  expect(within(manual).getByRole('button', { name: 'Uploading…' })).toBeDisabled()
  answer(new Reply(502, { detail: 'The PDF could not be read. Nothing was changed.' }))
  expect(await within(manual).findByRole('alert')).toHaveTextContent('could not be read')
  expect(within(manual).getByRole('button', { name: 'Upload' })).toBeEnabled()
})

test('cancelling the confirmation gives focus back to the button that asked', async () => {
  serveReview(() => queueOf(SLACK, FIGMA))
  const fields = await openReview()
  const button = within(fields).getByRole('button', { name: 'Not a billing document' })

  await userEvent.click(button)
  const confirm = screen.getByRole('alertdialog', { name: 'Skip this email and delete its PDF?' })
  await userEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }))

  await waitFor(() => expect(button).toHaveFocus())
})
