import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'
import type { PersonOnList, RefusedSignIn } from './api'
import { openDashboard, Reply, serve, signedIn } from './test/dashboard'

const FINANCE = 'finance@nyayalabs.example'
const OPS = 'ops@nyayalabs.example'
const AUDIT = 'audit@nyayalabs.example'
const STRANGER = 'stranger@elsewhere.example'

const PEOPLE: PersonOnList[] = [
  {
    address: FINANCE,
    role: 'administrator',
    set_by_installation: true,
    added_by: null,
    added_at: null,
    last_signed_in_at: '2026-09-29T10:30:00+00:00',
  },
  {
    address: OPS,
    role: 'member',
    set_by_installation: false,
    added_by: FINANCE,
    added_at: '2026-09-02T08:00:00+00:00',
    last_signed_in_at: null,
  },
]

const REFUSED: RefusedSignIn[] = [
  { address: STRANGER, attempted_at: '2026-09-28T17:05:00+00:00' },
]

function asAdministrator(answers: Record<string, unknown> = {}) {
  return serve(
    signedIn({
      'GET /api/me': { email: FINANCE, role: 'administrator' },
      'GET /api/months': { months: [] },
      'GET /api/people': PEOPLE,
      'GET /api/people/refused': REFUSED,
      ...answers,
    }),
  )
}

async function openPeople() {
  openDashboard('/people')
  await screen.findByRole('heading', { name: 'People' })
  return screen.findByRole('table', { name: 'People who may sign in' })
}

function rowOf(table: HTMLElement, address: string): HTMLElement {
  const row = within(table)
    .getAllByRole('row')
    .find((each) => within(each).queryByRole('rowheader', { name: address }))
  if (!row) throw new Error(`No row for ${address}`)
  return row
}

test('the People entry is shown to an administrator', async () => {
  asAdministrator()

  openDashboard()

  const nav = await screen.findByRole('navigation')
  expect(await within(nav).findByRole('link', { name: 'People' })).toBeVisible()
})

test('the People entry is hidden from a member', async () => {
  serve(
    signedIn({ 'GET /api/me': { email: OPS, role: 'member' }, 'GET /api/months': { months: [] } }),
  )

  openDashboard()

  const nav = await screen.findByRole('navigation')
  expect(within(nav).queryByRole('link', { name: 'People' })).not.toBeInTheDocument()
})

test('a member who opens the screen directly is told it is for administrators', async () => {
  const calls = serve(
    signedIn({ 'GET /api/me': { email: OPS, role: 'member' }, 'GET /api/months': { months: [] } }),
  )

  openDashboard('/people')

  expect(await screen.findByRole('heading', { name: 'People' })).toBeVisible()
  expect(
    screen.getByText('This screen is for administrators. Ask an administrator to make a change.'),
  ).toBeVisible()
  expect(calls.some((call) => call.path.startsWith('/api/people'))).toBe(false)
})

test('the list shows each person with their role, who added them and their last sign-in', async () => {
  asAdministrator()
  const table = await openPeople()

  const finance = rowOf(table, FINANCE)
  expect(finance).toHaveTextContent('Administrator')
  expect(finance).toHaveTextContent('Set by the installation')
  expect(finance).toHaveTextContent('29 Sep 2026')
  expect(within(finance).queryByRole('button', { name: /Remove/ })).not.toBeInTheDocument()
  expect(within(finance).queryByRole('combobox')).not.toBeInTheDocument()

  const ops = rowOf(table, OPS)
  expect(ops).toHaveTextContent(`${FINANCE} on 2 Sep 2026`)
  expect(ops).toHaveTextContent('Never')
  expect(ops).not.toHaveTextContent('Set by the installation')
  expect(within(ops).getByRole('combobox', { name: `Role of ${OPS}` })).toHaveValue('member')
})

test('adding a person calls the API and refreshes the list', async () => {
  let added = false
  const calls = asAdministrator({
    'GET /api/people': () =>
      added
        ? [
            ...PEOPLE,
            {
              address: AUDIT,
              role: 'administrator',
              set_by_installation: false,
              added_by: FINANCE,
              added_at: '2026-09-29T11:00:00+00:00',
              last_signed_in_at: null,
            },
          ]
        : PEOPLE,
    'POST /api/people': () => {
      added = true
      return {}
    },
  })
  await openPeople()

  const form = screen.getByRole('form', { name: 'Add a person' })
  await userEvent.type(within(form).getByLabelText('Email address'), AUDIT)
  await userEvent.selectOptions(within(form).getByLabelText('Role'), 'administrator')
  await userEvent.click(within(form).getByRole('button', { name: 'Add' }))

  const table = await screen.findByRole('table', { name: 'People who may sign in' })
  expect(await within(table).findByRole('rowheader', { name: AUDIT })).toBeVisible()
  expect(calls).toContainEqual({
    method: 'POST',
    path: '/api/people',
    body: { address: AUDIT, role: 'administrator' },
  })
  expect(within(form).getByLabelText('Email address')).toHaveValue('')
})

test('a reason the server gives for refusing an addition is shown', async () => {
  asAdministrator({
    'POST /api/people': new Reply(422, {
      detail: 'The address must be an email address, such as name@example.com',
    }),
  })
  await openPeople()

  const form = screen.getByRole('form', { name: 'Add a person' })
  await userEvent.type(within(form).getByLabelText('Email address'), 'ops')
  await userEvent.click(within(form).getByRole('button', { name: 'Add' }))

  expect(await within(form).findByRole('alert')).toHaveTextContent(
    'The address must be an email address, such as name@example.com',
  )
  expect(within(form).getByLabelText('Email address')).toHaveValue('ops')
})

test('changing a role calls the API', async () => {
  const calls = asAdministrator({ [`PUT /api/people/${encodeURIComponent(OPS)}`]: {} })
  const table = await openPeople()

  await userEvent.selectOptions(
    within(rowOf(table, OPS)).getByRole('combobox', { name: `Role of ${OPS}` }),
    'administrator',
  )

  await waitFor(() =>
    expect(calls).toContainEqual({
      method: 'PUT',
      path: `/api/people/${encodeURIComponent(OPS)}`,
      body: { role: 'administrator' },
    }),
  )
})

test('removal asks for confirmation before it is made', async () => {
  const path = `/api/people/${encodeURIComponent(OPS)}`
  const calls = asAdministrator({ [`DELETE ${path}`]: null })
  const table = await openPeople()

  await userEvent.click(within(rowOf(table, OPS)).getByRole('button', { name: 'Remove' }))

  expect(screen.getByText(`Remove ${OPS}? They will be signed out at once.`)).toBeVisible()
  expect(calls.some((call) => call.method === 'DELETE')).toBe(false)

  await userEvent.click(screen.getByRole('button', { name: 'Keep' }))
  expect(calls.some((call) => call.method === 'DELETE')).toBe(false)

  await userEvent.click(within(rowOf(table, OPS)).getByRole('button', { name: 'Remove' }))
  await userEvent.click(screen.getByRole('button', { name: `Yes, remove ${OPS}` }))

  await waitFor(() => expect(calls).toContainEqual({ method: 'DELETE', path }))
})

test('a refused sign-in can be added as a member', async () => {
  const calls = asAdministrator({ 'POST /api/people': {} })
  await openPeople()

  const refused = screen.getByRole('region', { name: 'Refused sign-ins' })
  expect(refused).toHaveTextContent(STRANGER)
  expect(refused).toHaveTextContent('28 Sep 2026')
  await userEvent.click(within(refused).getByRole('button', { name: `Add ${STRANGER} as a member` }))

  await waitFor(() =>
    expect(calls).toContainEqual({
      method: 'POST',
      path: '/api/people',
      body: { address: STRANGER, role: 'member' },
    }),
  )
})

test('a refused sign-in already on the list offers no button', async () => {
  asAdministrator({
    'GET /api/people/refused': [{ address: OPS, attempted_at: '2026-09-01T09:00:00+00:00' }],
  })
  await openPeople()

  const refused = screen.getByRole('region', { name: 'Refused sign-ins' })
  expect(within(refused).queryByRole('button')).not.toBeInTheDocument()
  expect(refused).toHaveTextContent('Now on the list')
})
