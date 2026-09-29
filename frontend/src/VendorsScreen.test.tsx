import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'
import type { Vendor, VendorList } from './api'
import { openDashboard, Reply, serve, signedIn } from './test/dashboard'

const ENGINEERING = 'engineering@nyayalabs.example'
const DESIGN = 'design@nyayalabs.example'

function vendor(name: string, changes: Partial<Vendor> = {}): Vendor {
  return {
    vendor: name,
    status: 'expected',
    source_account: ENGINEERING,
    billing_cycle: 'monthly',
    renewal_month: null,
    usual_amount: null,
    currency: 'USD',
    months_billed: [],
    latest_amount: null,
    latest_currency: null,
    gap: null,
    ...changes,
  }
}

const VERCEL = vendor('Vercel', {
  status: 'suggested',
  usual_amount: '22.00',
  months_billed: ['2026-07', '2026-08'],
  latest_amount: '24.00',
  latest_currency: 'USD',
})

const LIST: VendorList = {
  latest_month: '2026-08',
  expected: [
    vendor('AWS', {
      usual_amount: '1400.00',
      months_billed: ['2026-06', '2026-07'],
      latest_amount: '1400.00',
      latest_currency: 'USD',
      gap: 'missing',
    }),
    vendor('Figma', {
      source_account: DESIGN,
      billing_cycle: 'annual',
      renewal_month: 3,
      usual_amount: '540.00',
    }),
    vendor('Slack', {
      usual_amount: '650.00',
      months_billed: ['2026-08'],
      latest_amount: '652.50',
      latest_currency: 'USD',
    }),
  ],
  suggested: [VERCEL],
  ignored: [vendor('Canva', { status: 'ignored', source_account: DESIGN })],
}

function serveVendors(answers: Record<string, unknown> = {}) {
  return serve(
    signedIn({ 'GET /api/months': { months: ['2026-08'] }, 'GET /api/vendors': LIST, ...answers }),
  )
}

async function openVendors() {
  openDashboard('/vendors')
  await screen.findByRole('heading', { name: 'Vendors' })
  return screen.findByRole('table', { name: 'Expected vendors' })
}

function rowOf(table: HTMLElement, name: string): HTMLElement {
  const row = within(table)
    .getAllByRole('row')
    .find((each) => within(each).queryByRole('rowheader', { name }))
  if (!row) throw new Error(`No row for ${name}`)
  return row
}

test('suggested vendors are shown with what is known and their actions', async () => {
  serveVendors()
  await openVendors()

  const suggested = screen.getByRole('region', { name: 'Suggested vendors' })
  const vercel = within(suggested).getByRole('article', { name: 'Vercel' })
  expect(vercel).toHaveTextContent('Billed in July 2026 and August 2026')
  expect(vercel).toHaveTextContent('Latest amount 24.00 USD')
  expect(within(vercel).getByRole('button', { name: 'Accept' })).toBeEnabled()
  expect(within(vercel).getByRole('button', { name: 'Accept with changes' })).toBeEnabled()
  expect(within(vercel).getByRole('button', { name: 'Ignore' })).toBeEnabled()
})

test('no suggested vendors section is shown when there are none', async () => {
  serveVendors({ 'GET /api/vendors': { ...LIST, suggested: [] } })
  await openVendors()

  expect(screen.queryByRole('region', { name: 'Suggested vendors' })).not.toBeInTheDocument()
})

test('accepting a suggested vendor calls the API and refreshes the list', async () => {
  let accepted = false
  const calls = serveVendors({
    'GET /api/vendors': () =>
      accepted
        ? {
            ...LIST,
            expected: [...LIST.expected, { ...VERCEL, status: 'expected' }],
            suggested: [],
          }
        : LIST,
    'POST /api/vendors/Vercel/accept': () => {
      accepted = true
      return {}
    },
  })
  await openVendors()

  const suggested = screen.getByRole('region', { name: 'Suggested vendors' })
  await userEvent.click(within(suggested).getByRole('button', { name: 'Accept' }))

  const table = await screen.findByRole('table', { name: 'Expected vendors' })
  expect(await within(table).findByRole('rowheader', { name: 'Vercel' })).toBeVisible()
  expect(screen.queryByRole('region', { name: 'Suggested vendors' })).not.toBeInTheDocument()
  expect(calls).toContainEqual({ method: 'POST', path: '/api/vendors/Vercel/accept' })
  expect(calls.filter((call) => call.path === '/api/vendors')).toHaveLength(2)
})

test('a suggested vendor can be accepted with changes', async () => {
  const calls = serveVendors({ 'POST /api/vendors/Vercel/accept': {} })
  await openVendors()

  const vercel = screen.getByRole('article', { name: 'Vercel' })
  await userEvent.click(within(vercel).getByRole('button', { name: 'Accept with changes' }))
  const form = within(vercel).getByRole('form', { name: 'Accept Vercel' })
  await userEvent.selectOptions(within(form).getByLabelText('Billing cycle'), 'annual')
  await userEvent.selectOptions(within(form).getByLabelText('Renewal month'), 'June')
  await userEvent.clear(within(form).getByLabelText('Usual amount'))
  await userEvent.type(within(form).getByLabelText('Usual amount'), '240')
  await userEvent.click(within(form).getByRole('button', { name: 'Accept' }))

  await screen.findByRole('table', { name: 'Expected vendors' })
  expect(calls).toContainEqual({
    method: 'POST',
    path: '/api/vendors/Vercel/accept',
    body: {
      vendor: 'Vercel',
      source_account: ENGINEERING,
      billing_cycle: 'annual',
      renewal_month: 6,
      usual_amount: '240',
      currency: 'USD',
    },
  })
})

test('ignoring a suggested vendor calls the API', async () => {
  const calls = serveVendors({ 'POST /api/vendors/Vercel/ignore': {} })
  await openVendors()

  await userEvent.click(
    within(screen.getByRole('article', { name: 'Vercel' })).getByRole('button', { name: 'Ignore' }),
  )

  expect(calls).toContainEqual({ method: 'POST', path: '/api/vendors/Vercel/ignore' })
})

test('expected vendors are shown with their billing cycle, amounts and gaps', async () => {
  serveVendors()
  const table = await openVendors()

  expect(rowOf(table, 'Figma')).toHaveTextContent('Annual, renews in March')
  expect(rowOf(table, 'Figma')).toHaveTextContent(DESIGN)
  expect(rowOf(table, 'Slack')).toHaveTextContent('Monthly')
  expect(rowOf(table, 'Slack')).toHaveTextContent('650.00 USD')
  expect(rowOf(table, 'Slack')).toHaveTextContent('652.50 USD')
  expect(within(rowOf(table, 'AWS')).getByText('Gap in August 2026')).toBeVisible()
  expect(within(rowOf(table, 'Slack')).queryByText(/Gap/)).not.toBeInTheDocument()
})

test('a vendor can be added', async () => {
  const calls = serveVendors({ 'POST /api/vendors': {} })
  await openVendors()

  await userEvent.click(screen.getByRole('button', { name: 'Add vendor' }))
  const form = screen.getByRole('form', { name: 'Add vendor' })
  await userEvent.type(within(form).getByLabelText('Vendor'), 'Linear')
  await userEvent.type(within(form).getByLabelText('Source account'), ENGINEERING)
  await userEvent.type(within(form).getByLabelText('Usual amount'), '96.00')
  await userEvent.type(within(form).getByLabelText('Currency'), 'USD')
  await userEvent.click(within(form).getByRole('button', { name: 'Add' }))

  expect(calls).toContainEqual({
    method: 'POST',
    path: '/api/vendors',
    body: {
      vendor: 'Linear',
      source_account: ENGINEERING,
      billing_cycle: 'monthly',
      renewal_month: null,
      usual_amount: '96.00',
      currency: 'USD',
    },
  })
  expect(screen.queryByRole('form', { name: 'Add vendor' })).not.toBeInTheDocument()
})

test('a validation message from the server is shown beside the form', async () => {
  serveVendors({
    'POST /api/vendors': new Reply(422, {
      detail: 'The currency must be a three-letter code, such as USD',
    }),
  })
  await openVendors()

  await userEvent.click(screen.getByRole('button', { name: 'Add vendor' }))
  const form = screen.getByRole('form', { name: 'Add vendor' })
  await userEvent.type(within(form).getByLabelText('Vendor'), 'Linear')
  await userEvent.type(within(form).getByLabelText('Currency'), 'dollars')
  await userEvent.click(within(form).getByRole('button', { name: 'Add' }))

  expect(await within(form).findByRole('alert')).toHaveTextContent(
    'The currency must be a three-letter code, such as USD',
  )
  expect(within(form).getByLabelText('Vendor')).toHaveValue('Linear')
})

test('a vendor already on the list under another spelling is refused beside the form', async () => {
  serveVendors({
    'POST /api/vendors': new Reply(409, { detail: 'Slack is already on the vendor list' }),
  })
  await openVendors()

  await userEvent.click(screen.getByRole('button', { name: 'Add vendor' }))
  const form = screen.getByRole('form', { name: 'Add vendor' })
  await userEvent.type(within(form).getByLabelText('Vendor'), 'Slack Technologies, Inc.')
  await userEvent.click(within(form).getByRole('button', { name: 'Add' }))

  expect(await within(form).findByRole('alert')).toHaveTextContent(
    'Slack is already on the vendor list',
  )
})

test('a vendor is edited in place', async () => {
  const calls = serveVendors({ 'PUT /api/vendors/Slack': {} })
  const table = await openVendors()

  await userEvent.click(within(rowOf(table, 'Slack')).getByRole('button', { name: 'Edit' }))
  const form = screen.getByRole('form', { name: 'Edit Slack' })
  await userEvent.clear(within(form).getByLabelText('Usual amount'))
  await userEvent.type(within(form).getByLabelText('Usual amount'), '700')
  await userEvent.click(within(form).getByRole('button', { name: 'Save' }))

  expect(calls).toContainEqual({
    method: 'PUT',
    path: '/api/vendors/Slack',
    body: {
      vendor: 'Slack',
      source_account: ENGINEERING,
      billing_cycle: 'monthly',
      renewal_month: null,
      usual_amount: '700',
      currency: 'USD',
    },
  })
})

test('removal asks for confirmation before it is made', async () => {
  const name = 'Acme Tools / EU'
  const path = '/api/vendors/Acme%20Tools%20%2F%20EU'
  const calls = serveVendors({
    'GET /api/vendors': { ...LIST, expected: [...LIST.expected, vendor(name)] },
    [`DELETE ${path}`]: null,
  })
  const table = await openVendors()

  await userEvent.click(within(rowOf(table, name)).getByRole('button', { name: 'Remove' }))

  expect(calls.filter((call) => call.method === 'DELETE')).toEqual([])
  expect(screen.getByText(`Remove ${name} from the vendor list?`)).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Keep' }))
  expect(screen.queryByText(`Remove ${name} from the vendor list?`)).not.toBeInTheDocument()

  await userEvent.click(within(rowOf(table, name)).getByRole('button', { name: 'Remove' }))
  await userEvent.click(screen.getByRole('button', { name: `Yes, remove ${name}` }))

  expect(calls).toContainEqual({ method: 'DELETE', path })
})

test('ignored vendors are collapsed and can be restored', async () => {
  const calls = serveVendors({ 'POST /api/vendors/Canva/restore': {} })
  await openVendors()

  const ignored = screen.getByText('Ignored vendors (1)')
  expect(screen.getByRole('button', { name: 'Restore' })).not.toBeVisible()
  await userEvent.click(ignored)
  await userEvent.click(screen.getByRole('button', { name: 'Restore' }))

  expect(calls).toContainEqual({ method: 'POST', path: '/api/vendors/Canva/restore' })
})

test('buttons are disabled while a change is in flight', async () => {
  let finish: (value: Response) => void = () => {}
  serveVendors()
  const fetchAnswers = globalThis.fetch
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'POST') return new Promise<Response>((resolve) => (finish = resolve))
    return fetchAnswers(input, init)
  }) as typeof fetch
  await openVendors()

  const vercel = screen.getByRole('article', { name: 'Vercel' })
  await userEvent.click(within(vercel).getByRole('button', { name: 'Ignore' }))

  expect(within(vercel).getByRole('button', { name: 'Accept' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Add vendor' })).toBeDisabled()
  finish(Response.json({}))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Add vendor' })).toBeEnabled())
})

test('a refused decision is shown on the screen', async () => {
  serveVendors({
    'POST /api/vendors/Vercel/ignore': new Reply(409, {
      detail: 'Vercel is not a suggested vendor',
    }),
  })
  await openVendors()

  await userEvent.click(
    within(screen.getByRole('article', { name: 'Vercel' })).getByRole('button', { name: 'Ignore' }),
  )

  expect(await screen.findByRole('alert')).toHaveTextContent('Vercel is not a suggested vendor')
})
