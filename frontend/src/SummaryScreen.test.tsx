import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import type { Gap, GapStatus } from './api'
import { AUGUST, emptySummary, openDashboard, Reply, serve, signedIn } from './test/dashboard'

async function openAugust(summary = AUGUST) {
  serve(
    signedIn({
      'GET /api/months': { months: ['2026-08'] },
      'GET /api/months/2026-08/summary': summary,
    }),
  )
  openDashboard('/summary?month=2026-08')
  await screen.findByRole('heading', { name: 'Summary for August 2026' })
  await screen.findByRole('region', { name: 'Billing documents' })
}

function section(name: string) {
  return within(screen.getByRole('region', { name }))
}

function cellsOfRow(row: HTMLElement): string[] {
  return within(row)
    .getAllByRole('cell')
    .map((cell) => cell.textContent ?? '')
}

test('summary table renders one row per billing document', async () => {
  await openAugust()

  const table = section('Billing documents')
  const headers = table.getAllByRole('columnheader').map((header) => header.textContent)
  expect(headers).toEqual([
    'Vendor',
    'Document type',
    'Invoice date',
    'Amount',
    'Currency',
    'Amount in rupees',
    'Source account',
    'File',
    'Notes',
    'History',
  ])
  const [, ...rows] = table.getAllByRole('row')
  expect(rows.map(cellsOfRow)).toEqual([
    [
      'Slack',
      'Invoice',
      '3 Aug 2026',
      '1,652.50',
      'USD',
      '₹1,57,549.35',
      'engineering@nyayalabs.example',
      '2026-08_Slack_1652.50-USD.pdf',
      'receipt also received: 2026-08_Slack_1652.50-USD_2.pdf',
      'History',
    ],
    [
      'Notion',
      'Receipt',
      '9 Aug 2026',
      '221.40',
      'EUR',
      'No rate',
      'design@nyayalabs.example',
      '2026-08_Notion_221.40-EUR.pdf',
      '',
      'History',
    ],
    [
      'Figma',
      'Credit note',
      '21 Aug 2026',
      '-40.00',
      'USD',
      '-₹3,828.00',
      'design@nyayalabs.example',
      '2026-08_Figma_-40.00-USD.pdf',
      '',
      'Not recorded',
    ],
  ])
})

test('each row links to its file', async () => {
  await openAugust()

  const table = section('Billing documents')
  expect(table.getByRole('link', { name: '2026-08_Slack_1652.50-USD.pdf' })).toHaveAttribute(
    'href',
    '/api/months/2026-08/billing-documents/2026-08_Slack_1652.50-USD.pdf',
  )
  expect(table.getByRole('link', { name: '2026-08_Notion_221.40-EUR.pdf' })).toHaveAttribute(
    'href',
    'https://drive.google.example/file/d/abc/view',
  )
})

test('a credit note shows a negative amount, set apart from the others', async () => {
  await openAugust()

  const table = section('Billing documents')
  const credit = table.getByText('-40.00')
  expect(credit).toHaveClass('is-negative')
  expect(credit.closest('tr')).toHaveClass('is-credit-note')
  expect(within(credit.closest('tr')!).getByText('Credit note')).toBeVisible()
  expect(table.getByText('1,652.50')).not.toHaveClass('is-negative')
})

test('the rupee total is shown beside the number of rows it leaves out', async () => {
  await openAugust()

  const total = section('Headline numbers').getByText('Total in rupees').nextSibling
  expect(total).toHaveTextContent('₹1,53,721.35')
  expect(total).toHaveTextContent('1 row has no rupee amount and is left out')
})

test('a rupee total that leaves nothing out says nothing more', async () => {
  const rows = AUGUST.rows.filter((row) => row.amount_inr !== null)
  await openAugust({ ...AUGUST, rows, total_inr: '153721.35', rows_without_rupees: 0 })

  const total = section('Headline numbers').getByText('Total in rupees').nextSibling
  expect(total).toHaveTextContent('₹1,53,721.35')
  expect(total).not.toHaveTextContent('left out')
})

test('a rupee amount says which rate it was converted at', async () => {
  await openAugust()

  const table = section('Billing documents')
  expect(table.getByText('₹1,57,549.35').closest('td')).toHaveAttribute(
    'title',
    '1 USD = 95.34 INR on the invoice date',
  )
})

test('amounts are right-aligned', async () => {
  await openAugust()

  const table = section('Billing documents')
  expect(table.getByRole('columnheader', { name: 'Amount' })).toHaveClass('amount')
  expect(table.getByText('221.40').closest('td')).toHaveClass('amount')
})

test('headline numbers give documents collected, needing review, gaps and totals', async () => {
  await openAugust()

  const headline = section('Headline numbers')
  expect(headline.getByText('Billing documents collected').nextSibling).toHaveTextContent('3')
  expect(headline.getByText('Needing review').nextSibling).toHaveTextContent('1')
  expect(headline.getByText('Gaps').nextSibling).toHaveTextContent('2')
  const totals = within(headline.getByRole('list', { name: 'Total per currency' }))
  expect(totals.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
    'EUR 221.40',
    'USD 1,612.50',
  ])
})

test('gaps are listed with what explains them', async () => {
  await openAugust()

  const [, ...rows] = section('Gaps').getAllByRole('row')
  expect(rows.map(cellsOfRow)).toEqual([
    ['Linear', 'Payment failed', 'engineering@nyayalabs.example', 'payment failed on 16 August'],
    ['Zoho', 'Mailbox not read', 'ops@nyayalabs.example', 'None found'],
  ])
})

test.each<[GapStatus, string, string]>([
  ['held_for_review', 'Held for review', 'text-warning'],
  ['manual_download', 'Awaiting manual download', 'text-warning'],
  ['email_failed', 'An email failed', 'text-destructive'],
  ['payment_failed', 'Payment failed', 'text-warning'],
  ['mailbox_unread', 'Mailbox not read', 'text-muted-foreground'],
  ['not_received', 'Not received', 'text-muted-foreground'],
])('a gap whose status is %s is labelled %s', async (status, label, tone) => {
  const gap: Gap = {
    vendor: 'Datadog',
    kind: status === 'mailbox_unread' ? 'unknown' : 'missing',
    status,
    source_account: 'engineering@nyayalabs.example',
    explanation: null,
  }
  await openAugust({ ...AUGUST, gaps: [gap] })

  const badge = section('Gaps').getByText(label)
  // The tone shows in the colour of its text.
  expect(badge).toHaveClass('tag', `tag-gap-${status}`, tone)
})

test('a source account that could not be read is named with the reason', async () => {
  await openAugust()

  expect(section('Source accounts that could not be read').getByRole('alert')).toHaveTextContent(
    'ops@nyayalabs.example: the sign-in no longer works. Whether its vendors billed is not known.',
  )
})

test('upcoming charges are listed', async () => {
  await openAugust()

  const [, row] = section('Upcoming charges').getAllByRole('row')
  expect(cellsOfRow(row!)).toEqual([
    '1Password',
    'engineering@nyayalabs.example',
    'Your 1Password subscription renews on September 24, 2026',
  ])
})

test('a month with no gaps says so, and shows no section with nothing to say', async () => {
  await openAugust({ ...AUGUST, gaps: [], upcoming: [], failed_source_accounts: [] })

  expect(section('Gaps').getByText('Every expected vendor sent a billing document.')).toBeVisible()
  expect(section('Headline numbers').getByText('Gaps').nextSibling).toHaveTextContent('0')
  expect(screen.queryByRole('region', { name: 'Upcoming charges' })).not.toBeInTheDocument()
  expect(
    screen.queryByRole('region', { name: 'Source accounts that could not be read' }),
  ).not.toBeInTheDocument()
})

test('emails needing review are listed with reasons and portal links', async () => {
  await openAugust()

  const review = section('Emails needing review')
  expect(review.getByText('Your Zoom invoice is ready')).toBeVisible()
  expect(review.getByText('manual download needed')).toBeVisible()
  expect(review.getByRole('link', { name: 'Open portal link' })).toHaveAttribute(
    'href',
    'https://zoom.example/billing/invoices/889',
  )
})

test('a portal link that is not a web address is shown as text, not as a link', async () => {
  const [email] = AUGUST.needs_review
  await openAugust({
    ...AUGUST,
    needs_review: [{ ...email!, portal_link: 'javascript:alert(1)' }],
  })

  const review = section('Emails needing review')
  expect(review.queryByRole('link')).not.toBeInTheDocument()
  expect(review.getByText('javascript:alert(1)')).toBeVisible()
})

test('skipped and failed emails are listed with reasons', async () => {
  await openAugust()

  const skipped = section('Skipped emails')
  expect(skipped.getByText('What is new in Slack')).toBeVisible()
  expect(skipped.getByText('not a billing email')).toBeVisible()
  expect(skipped.getByText('billing signal: payment failed')).toBeVisible()
  const failed = section('Failed emails')
  expect(failed.getByText('Your GitHub invoice')).toBeVisible()
  expect(failed.getByText('the PDF could not be read')).toBeVisible()
})

test('billing signals are listed with their kind and vendor', async () => {
  await openAugust()

  const signals = section('Billing signals')
  const [, row] = signals.getAllByRole('row')
  expect(cellsOfRow(row!)).toEqual([
    'Payment failed',
    'Linear',
    'Payment failed for Linear',
    'engineering@nyayalabs.example',
    '16 Aug 2026',
  ])
})

test('a collection month with nothing shows an empty state in every section', async () => {
  await openAugust(emptySummary('2026-08'))

  expect(section('Billing documents').getByText('No billing documents were collected.')).toBeVisible()
  expect(section('Emails needing review').getByText('No emails need review.')).toBeVisible()
  expect(section('Skipped emails').getByText('No emails were skipped.')).toBeVisible()
  expect(section('Failed emails').getByText('No emails failed.')).toBeVisible()
  expect(section('Billing signals').getByText('No billing signals were found.')).toBeVisible()
  expect(screen.queryByRole('table')).not.toBeInTheDocument()
  const headline = section('Headline numbers')
  expect(headline.getByText('Billing documents collected').nextSibling).toHaveTextContent('0')
  expect(headline.getByText('Total per currency').nextSibling).toHaveTextContent('Nothing collected')
})

test('with nothing run yet, the month that has just ended is chosen and can be run', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date(2026, 8, 30) })
  const calls = serve(
    signedIn({
      'GET /api/months': { months: [] },
      'GET /api/months/2026-08/summary': emptySummary('2026-08'),
      'POST /api/months/2026-08/runs': { month: '2026-08', source_account: null },
      'GET /api/months/2026-08/runs': {
        month: '2026-08',
        running: null,
        runs: [],
        not_started: [],
        connected_source_accounts: 3,
        cannot_start: null,
      },
    }),
  )

  openDashboard()

  expect(await screen.findByRole('heading', { name: 'Summary for August 2026' })).toBeVisible()
  const picker = screen.getByRole('combobox', { name: 'Collection month' })
  expect(picker).toHaveValue('2026-08')
  expect(within(picker).getByRole('option', { name: 'August 2026 (not run yet)' })).toBeVisible()
  expect(within(picker).getByRole('option', { name: 'September 2026 (not run yet)' })).toBeVisible()

  await userEvent.click(await screen.findByRole('button', { name: 'Run August 2026' }))

  expect(calls).toContainEqual({
    method: 'POST',
    path: '/api/months/2026-08/runs',
    body: { source_account: null },
  })
  // The Runs screen follows the run.
  expect(await screen.findByRole('heading', { name: 'Runs', level: 1 })).toBeVisible()
  vi.useRealTimers()
})

test('the month picker is only on the screens that show one month', async () => {
  serve(
    signedIn({
      'GET /api/months': { months: ['2026-08'] },
      'GET /api/spend': {
        from_month: '2026-08',
        to_month: '2026-08',
        months: [],
        vendors: [],
        source_accounts: [],
        without_rupees: { charges: 0, totals: [] },
      },
    }),
  )

  openDashboard('/spend')

  await screen.findByRole('heading', { name: 'Spend', level: 1 })
  expect(screen.queryByRole('combobox', { name: 'Collection month' })).not.toBeInTheDocument()
  // The month is still kept for the screens that show one.
  expect(screen.getByRole('link', { name: 'Summary' })).toHaveAttribute('href', '/summary')
})

test('a collection month that is not a month is refused', async () => {
  serve(signedIn({ 'GET /api/months': { months: ['2026-08'] } }))

  openDashboard('/summary?month=last-august')

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'last-august is not a collection month',
  )
})

test('each row with a recorded history links to it', async () => {
  await openAugust()

  const table = section('Billing documents')
  expect(
    table.getByRole('link', { name: 'History of 2026-08_Slack_1652.50-USD.pdf' }),
  ).toHaveAttribute('href', '/documents/hash-slack?month=2026-08')
})

test('when the list of months cannot be read, a month can still be chosen and run', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date(2026, 8, 30) })
  serve(
    signedIn({
      'GET /api/months': new Reply(500, { detail: 'broken' }),
      'GET /api/months/2026-08/summary': emptySummary('2026-08'),
    }),
  )

  openDashboard()

  expect(await screen.findByRole('alert')).toHaveTextContent('/api/months answered 500')
  expect(await screen.findByRole('combobox', { name: 'Collection month' })).toHaveValue('2026-08')
  expect(await screen.findByRole('button', { name: 'Run August 2026' })).toBeVisible()
  vi.useRealTimers()
})
