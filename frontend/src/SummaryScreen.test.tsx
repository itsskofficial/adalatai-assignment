import { screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import { AUGUST, emptySummary, openDashboard, serve, signedIn } from './test/dashboard'

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
  ])
  const [, ...rows] = table.getAllByRole('row')
  expect(rows.map(cellsOfRow)).toEqual([
    [
      'Slack',
      'Invoice',
      '3 Aug 2026',
      '1,652.50',
      'USD',
      '157,549.35',
      'engineering@nyayalabs.example',
      '2026-08_Slack_1652.50-USD.pdf',
      'receipt also received: 2026-08_Slack_1652.50-USD_2.pdf',
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
    ],
    [
      'Figma',
      'Credit note',
      '21 Aug 2026',
      '-40.00',
      'USD',
      '-3,828.00',
      'design@nyayalabs.example',
      '2026-08_Figma_-40.00-USD.pdf',
      '',
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
  expect(total).toHaveTextContent('INR 153,721.35')
  expect(total).toHaveTextContent('1 row has no rupee amount and is left out')
})

test('a rupee total that leaves nothing out says nothing more', async () => {
  const rows = AUGUST.rows.filter((row) => row.amount_inr !== null)
  await openAugust({ ...AUGUST, rows, total_inr: '153721.35', rows_without_rupees: 0 })

  const total = section('Headline numbers').getByText('Total in rupees').nextSibling
  expect(total).toHaveTextContent('INR 153,721.35')
  expect(total).not.toHaveTextContent('left out')
})

test('a rupee amount says which rate it was converted at', async () => {
  await openAugust()

  const table = section('Billing documents')
  expect(table.getByText('157,549.35').closest('td')).toHaveAttribute(
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
  expect(headline.getByText('Gaps').nextSibling).toHaveTextContent('Not available yet')
  const totals = within(headline.getByRole('list', { name: 'Total per currency' }))
  expect(totals.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
    'EUR 221.40',
    'USD 1,612.50',
  ])
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

test('a ledger with no collection months says no run has been recorded', async () => {
  serve(signedIn({ 'GET /api/months': { months: [] } }))

  openDashboard()

  expect(await screen.findByText('No run has been recorded yet.')).toBeVisible()
  expect(screen.getByRole('combobox', { name: 'Collection month' })).toBeDisabled()
})

test('a collection month that is not a month is refused', async () => {
  serve(signedIn({ 'GET /api/months': { months: ['2026-08'] } }))

  openDashboard('/summary?month=last-august')

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'last-august is not a collection month',
  )
})
