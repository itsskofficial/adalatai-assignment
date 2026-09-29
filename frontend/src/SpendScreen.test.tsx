import { screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import type { Spend } from './api'
import { openDashboard, Reply, serve, signedIn } from './test/dashboard'

const SPEND: Spend = {
  from_month: '2026-06',
  to_month: '2026-08',
  months: [
    { month: '2026-06', inr_total: '12450.00' },
    { month: '2026-07', inr_total: '14280.00' },
    { month: '2026-08', inr_total: '123456.00' },
  ],
  vendors: [
    { vendor: 'AWS', inr_total: '131130.00' },
    { vendor: 'Slack', inr_total: '12600.00' },
    { vendor: 'Linear', inr_total: '1000.00' },
    { vendor: 'Figma', inr_total: '-850.00' },
  ],
  source_accounts: [
    { source_account: 'engineering@nyayalabs.example', inr_total: '131130.00' },
    { source_account: 'design@nyayalabs.example', inr_total: '11750.00' },
    { source_account: 'ops@nyayalabs.example', inr_total: '1000.00' },
  ],
  shared_charges: 1,
  inr_total: '150186.00',
  without_rupees: { charges: 1, totals: [{ currency: 'EUR', amount: '20.00' }] },
  changes: {
    month: '2026-08',
    previous_month: '2026-07',
    vendors: [
      {
        vendor: 'AWS',
        previous_inr: '10080.00',
        current_inr: '12750.00',
        change_inr: '2670.00',
        change_percent: '26.5',
      },
      {
        vendor: 'Linear',
        previous_inr: '0.00',
        current_inr: '1000.00',
        change_inr: '1000.00',
        change_percent: null,
      },
      {
        vendor: 'Figma',
        previous_inr: '0.00',
        current_inr: '-850.00',
        change_inr: '-850.00',
        change_percent: null,
      },
    ],
  },
}

const NOTHING: Spend = {
  from_month: null,
  to_month: null,
  months: [],
  vendors: [],
  source_accounts: [],
  shared_charges: 0,
  inr_total: '0.00',
  without_rupees: { charges: 0, totals: [] },
  changes: null,
}

async function openSpend(spend: Spend = SPEND) {
  serve(signedIn({ 'GET /api/months': { months: ['2026-08'] }, 'GET /api/spend': spend }))
  openDashboard('/spend')
  await screen.findByRole('heading', { name: 'Spend' })
}

function section(name: string) {
  return within(screen.getByRole('region', { name }))
}

function cellsOfRow(row: HTMLElement): string[] {
  return within(row)
    .getAllByRole('cell')
    .map((cell) => cell.textContent ?? '')
}

test('the Spend navigation entry opens the Spend screen', async () => {
  await openSpend()

  expect(screen.getByRole('link', { name: 'Spend' })).toHaveClass('is-active')
  expect(screen.queryByText('Not built yet.')).not.toBeInTheDocument()
})

test('spend by month shows each month with its total in rupees', async () => {
  await openSpend()

  const months = await screen.findByRole('region', { name: 'Spend by month' })
  const chart = within(months).getByRole('figure', { name: /Spend by month/ })
  expect(chart.querySelectorAll('svg rect')).toHaveLength(3)
  expect(within(months).getByText('Jun 2026')).toBeInTheDocument()
  expect(within(months).getByText('Jul 2026')).toBeInTheDocument()
  expect(within(months).getByText('Aug 2026')).toBeInTheDocument()
  expect(within(months).getByText('₹14,280.00')).toBeInTheDocument()
})

test('amounts use Indian digit grouping', async () => {
  await openSpend()

  await screen.findByRole('region', { name: 'Spend by month' })
  expect(section('Spend by month').getByText('₹1,23,456.00')).toBeInTheDocument()
  expect(section('Spend by vendor').getByText('₹1,31,130.00')).toBeInTheDocument()
})

test('vendors are ranked by spend with a bar each', async () => {
  await openSpend()

  await screen.findByRole('region', { name: 'Spend by vendor' })
  const items = section('Spend by vendor').getAllByRole('listitem')
  expect(items.map((item) => item.textContent)).toEqual([
    'AWS₹1,31,130.00',
    'Slack₹12,600.00',
    'Linear₹1,000.00',
    'Figma-₹850.00',
  ])
})

test('spend by source account says how many charges were shared', async () => {
  await openSpend()

  await screen.findByRole('region', { name: 'Spend by source account' })
  const accounts = section('Spend by source account')
  expect(accounts.getByText('engineering@nyayalabs.example')).toBeInTheDocument()
  expect(accounts.getByText('₹11,750.00')).toBeInTheDocument()
  expect(
    accounts.getByText(
      '1 charge was found in several source accounts and is counted under the first.',
    ),
  ).toBeInTheDocument()
})

test('changes since last month mark increases and decreases', async () => {
  await openSpend()

  await screen.findByRole('region', { name: 'Changes since last month' })
  const changes = section('Changes since last month')
  expect(changes.getByText(/August 2026 compared with July 2026/)).toBeInTheDocument()
  const [, ...rows] = changes.getAllByRole('row')
  expect(rows.map(cellsOfRow)).toEqual([
    ['AWS', '₹10,080.00', '₹12,750.00', 'Up +₹2,670.00', '+26.5%'],
    ['Linear', '₹0.00', '₹1,000.00', 'Up +₹1,000.00', 'New'],
    ['Figma', '₹0.00', '-₹850.00', 'Down -₹850.00', '—'],
  ])
  expect(rows[0]).toHaveClass('is-increase')
  expect(rows[2]).toHaveClass('is-decrease')
})

test('charges with no rupee amount are called out', async () => {
  await openSpend()

  const note = await screen.findByRole('note')
  expect(note).toHaveTextContent(
    '1 charge has no rupee amount and is left out of these totals: EUR 20.00.',
  )
})

test('no note is shown when every charge has a rupee amount', async () => {
  await openSpend({ ...SPEND, without_rupees: { charges: 0, totals: [] } })

  await screen.findByRole('region', { name: 'Spend by month' })
  expect(screen.queryByRole('note')).not.toBeInTheDocument()
})

test('an empty ledger says there is no spend yet', async () => {
  await openSpend(NOTHING)

  expect(await screen.findByText('No charges have been collected yet.')).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'Spend by month' })).not.toBeInTheDocument()
})

test('a problem loading spend is shown', async () => {
  serve(
    signedIn({
      'GET /api/months': { months: [] },
      'GET /api/spend': new Reply(500, { detail: 'broken' }),
    }),
  )
  openDashboard('/spend')

  expect(await screen.findByRole('alert')).toHaveTextContent('/api/spend answered 500')
})
