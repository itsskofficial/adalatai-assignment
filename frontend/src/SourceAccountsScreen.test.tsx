import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import type { ConnectionResult, SourceAccount, SourceAccountList } from './api'
import { browser } from './browser'
import { openDashboard, Reply, serve, signedIn } from './test/dashboard'

const ENGINEERING = 'engineering@nyayalabs.example'
const DESIGN = 'design@nyayalabs.example'
const OPS = 'ops@nyayalabs.example'
const FINANCE = 'finance@nyayalabs.example'
const FOUND = 'found@nyayalabs.example'
const GOOGLE = 'https://accounts.google.example/auth?state=abc'

function account(address: string, changes: Partial<SourceAccount> = {}): SourceAccount {
  return {
    address,
    is_owner: false,
    connected_by: FINANCE,
    connected_at: '2026-09-25T10:30:00+00:00',
    sign_in: 'works',
    sign_in_problem: null,
    sign_in_ends_at: '2026-10-02T10:30:00+00:00',
    expiring_soon: false,
    needs_drive_access: false,
    last_read_month: '2026-08',
    latest_run: { month: '2026-08', read: true, reason: null, billing_documents: 3 },
    ...changes,
  }
}

const LIST: SourceAccountList = {
  source_accounts: [
    account(ENGINEERING, { is_owner: true }),
    account(DESIGN, {
      expiring_soon: true,
      sign_in_ends_at: '2026-09-30T08:00:00+00:00',
    }),
    account(OPS, {
      sign_in: 'expired',
      last_read_month: '2026-07',
      latest_run: {
        month: '2026-08',
        read: false,
        reason: `the sign-in for ${OPS} no longer works (it expired or was revoked)`,
        billing_documents: 0,
      },
    }),
    account(FINANCE, { sign_in: 'missing', last_read_month: null, latest_run: null }),
  ],
  found_on_this_machine: [FOUND],
  sign_in_lifetime_days: 7,
}

const NO_RESULT: ConnectionResult = {
  outcome: null,
  address: null,
  signed_in_address: null,
  reason: null,
}

function serveAccounts(answers: Record<string, unknown> = {}) {
  return serve(
    signedIn({
      'GET /api/months': { months: ['2026-08'] },
      'GET /api/source-accounts': LIST,
      'GET /api/source-accounts/history': [],
      'GET /api/source-accounts/connection-result': NO_RESULT,
      ...answers,
    }),
  )
}

async function openSourceAccounts(at = '/source-accounts') {
  openDashboard(at)
  await screen.findByRole('heading', { name: 'Source accounts', level: 1 })
  return screen.findByRole('region', { name: 'Connected source accounts' })
}

function cardOf(address: string): HTMLElement {
  return screen.getByRole('article', { name: address })
}

function pathOf(address: string): string {
  return `/api/source-accounts/${encodeURIComponent(address)}`
}

test('the screen explains that access is read-only', async () => {
  serveAccounts()
  await openSourceAccounts()

  expect(screen.getByText(/read-only access to Gmail/)).toBeInTheDocument()
  expect(screen.getByText(/cannot send, change or delete mail/)).toBeInTheDocument()
})

test('each state of a sign-in is shown', async () => {
  serveAccounts()
  await openSourceAccounts()

  expect(cardOf(ENGINEERING)).toHaveTextContent('Sign-in works')
  expect(cardOf(DESIGN)).toHaveTextContent('Sign-in expires soon, on 30 Sep 2026')
  expect(cardOf(OPS)).toHaveTextContent('Sign-in expired')
  expect(cardOf(FINANCE)).toHaveTextContent('Not signed in')
})

test('the owner account is marked', async () => {
  serveAccounts()
  await openSourceAccounts()

  expect(cardOf(ENGINEERING)).toHaveTextContent('Owner account')
  expect(cardOf(DESIGN)).not.toHaveTextContent('Owner account')
  expect(
    within(cardOf(ENGINEERING)).queryByRole('button', { name: 'Make owner' }),
  ).not.toBeInTheDocument()
  expect(within(cardOf(DESIGN)).getByRole('button', { name: 'Make owner' })).toBeEnabled()
})

test('each account shows when it was last read and what the latest run found', async () => {
  serveAccounts()
  await openSourceAccounts()

  expect(cardOf(ENGINEERING)).toHaveTextContent('Last read for August 2026')
  expect(cardOf(ENGINEERING)).toHaveTextContent('3 billing documents collected in August 2026')
  expect(cardOf(OPS)).toHaveTextContent('Last read for July 2026')
  expect(cardOf(OPS)).toHaveTextContent(
    `Could not be read in August 2026: the sign-in for ${OPS} no longer works`,
  )
  expect(cardOf(FINANCE)).toHaveTextContent('Not read yet')
})

test('connecting sends the person to the address the server returned', async () => {
  const goTo = vi.spyOn(browser, 'goTo').mockImplementation(() => {})
  const calls = serveAccounts({
    'POST /api/source-accounts/connect': { authorization_url: GOOGLE },
  })
  await openSourceAccounts()

  const form = screen.getByRole('form', { name: 'Connect a source account' })
  await userEvent.type(within(form).getByLabelText('Address'), 'new@nyayalabs.example')
  await userEvent.click(within(form).getByLabelText(/This is the owner account/))
  await userEvent.click(within(form).getByRole('button', { name: 'Connect' }))

  await waitFor(() => expect(goTo).toHaveBeenCalledWith(GOOGLE))
  expect(calls).toContainEqual({
    method: 'POST',
    path: '/api/source-accounts/connect',
    body: { address: 'new@nyayalabs.example', owner: true },
  })
})

test('a refused connection says why and stays on the screen', async () => {
  const goTo = vi.spyOn(browser, 'goTo').mockImplementation(() => {})
  serveAccounts({
    'POST /api/source-accounts/connect': new Reply(409, {
      detail: `${DESIGN} is already connected. Renew its sign-in instead.`,
    }),
  })
  await openSourceAccounts()

  const form = screen.getByRole('form', { name: 'Connect a source account' })
  await userEvent.type(within(form).getByLabelText('Address'), DESIGN)
  await userEvent.click(within(form).getByRole('button', { name: 'Connect' }))

  expect(await within(form).findByRole('alert')).toHaveTextContent('already connected')
  expect(goTo).not.toHaveBeenCalled()
})

test('renewing sends the person to Google for that account', async () => {
  const goTo = vi.spyOn(browser, 'goTo').mockImplementation(() => {})
  serveAccounts({ [`POST ${pathOf(OPS)}/renew`]: { authorization_url: GOOGLE } })
  await openSourceAccounts()

  await userEvent.click(within(cardOf(OPS)).getByRole('button', { name: 'Renew' }))

  await waitFor(() => expect(goTo).toHaveBeenCalledWith(GOOGLE))
})

test('a successful connection is shown after returning from Google', async () => {
  serveAccounts({
    'GET /api/source-accounts/connection-result': {
      outcome: 'connected',
      address: DESIGN,
      signed_in_address: DESIGN,
      reason: null,
    },
  })
  await openSourceAccounts('/source-accounts?connect=connected')

  expect(await screen.findByRole('status')).toHaveTextContent(
    `Connected ${DESIGN}. The next run reads it.`,
  )
})

test('the wrong address signing in is shown with the address expected', async () => {
  serveAccounts({
    'GET /api/source-accounts/connection-result': {
      outcome: 'wrong_address',
      address: DESIGN,
      signed_in_address: 'someone@gmail.example',
      reason: null,
    },
  })
  await openSourceAccounts('/source-accounts?connect=wrong-address')

  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent(`${DESIGN} was not connected`)
  expect(alert).toHaveTextContent(`Expected ${DESIGN}, but someone@gmail.example signed in`)
  expect(alert).toHaveTextContent('Nothing was stored')
})

test('a connection that failed at Google is shown with its reason', async () => {
  serveAccounts({
    'GET /api/source-accounts/connection-result': {
      outcome: 'failed',
      address: DESIGN,
      signed_in_address: null,
      reason: 'Access was not granted at Google',
    },
  })
  await openSourceAccounts('/source-accounts?connect=failed')

  expect(await screen.findByRole('alert')).toHaveTextContent(
    `${DESIGN} was not connected: Access was not granted at Google`,
  )
})

test('no result is asked for when the screen is opened directly', async () => {
  const calls = serveAccounts()
  await openSourceAccounts()

  expect(calls.map((call) => call.path)).not.toContain('/api/source-accounts/connection-result')
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
})

test('removing asks for confirmation before calling the API', async () => {
  let removed = false
  const calls = serveAccounts({
    'GET /api/source-accounts': () =>
      removed
        ? {
            ...LIST,
            source_accounts: LIST.source_accounts.filter((each) => each.address !== DESIGN),
          }
        : LIST,
    [`DELETE ${pathOf(DESIGN)}`]: () => {
      removed = true
      return null
    },
  })
  await openSourceAccounts()

  await userEvent.click(within(cardOf(DESIGN)).getByRole('button', { name: 'Remove' }))

  expect(calls.filter((call) => call.method === 'DELETE')).toEqual([])
  expect(cardOf(DESIGN)).toHaveTextContent('Its stored sign-in is deleted')
  await userEvent.click(
    within(cardOf(DESIGN)).getByRole('button', { name: `Yes, remove ${DESIGN}` }),
  )

  await waitFor(() =>
    expect(screen.queryByRole('article', { name: DESIGN })).not.toBeInTheDocument(),
  )
  expect(calls).toContainEqual({ method: 'DELETE', path: pathOf(DESIGN) })
})

test('removal can be called off', async () => {
  const calls = serveAccounts()
  await openSourceAccounts()

  await userEvent.click(within(cardOf(DESIGN)).getByRole('button', { name: 'Remove' }))
  await userEvent.click(within(cardOf(DESIGN)).getByRole('button', { name: 'Keep' }))

  expect(within(cardOf(DESIGN)).getByRole('button', { name: 'Remove' })).toBeEnabled()
  expect(calls.filter((call) => call.method === 'DELETE')).toEqual([])
})

test('the owner account cannot be removed', async () => {
  serveAccounts()
  await openSourceAccounts()

  expect(within(cardOf(ENGINEERING)).getByRole('button', { name: 'Remove' })).toBeDisabled()
})

test('making an account the owner says it needs renewing', async () => {
  const calls = serveAccounts({
    [`POST ${pathOf(DESIGN)}/make-owner`]: {
      address: DESIGN,
      needs_renewal: true,
      message: `${DESIGN} is now the owner account. Renew its sign-in to give it access.`,
    },
  })
  await openSourceAccounts()

  await userEvent.click(within(cardOf(DESIGN)).getByRole('button', { name: 'Make owner' }))

  expect(await screen.findByRole('status')).toHaveTextContent('Renew its sign-in')
  expect(calls).toContainEqual({ method: 'POST', path: `${pathOf(DESIGN)}/make-owner` })
})

test('the owner account without Drive access is asked to renew', async () => {
  serveAccounts({
    'GET /api/source-accounts': {
      ...LIST,
      source_accounts: [account(ENGINEERING, { is_owner: true, needs_drive_access: true })],
    },
  })
  await openSourceAccounts()

  expect(cardOf(ENGINEERING)).toHaveTextContent('Renew to give it access to Drive')
})

test('accounts found on this machine can be added', async () => {
  const calls = serveAccounts({ [`POST ${pathOf(FOUND)}/add`]: { address: FOUND } })
  await openSourceAccounts()

  const found = screen.getByRole('region', { name: 'Found on this machine' })
  expect(found).toHaveTextContent(FOUND)
  await userEvent.click(within(found).getByRole('button', { name: `Add ${FOUND}` }))

  await waitFor(() =>
    expect(calls).toContainEqual({ method: 'POST', path: `${pathOf(FOUND)}/add` }),
  )
})

test('who changed the source accounts is shown', async () => {
  serveAccounts({
    'GET /api/source-accounts/history': [
      {
        address: DESIGN,
        action: 'connected',
        person: FINANCE,
        changed_at: '2026-09-25T10:30:00+00:00',
      },
    ],
  })
  await openSourceAccounts()

  expect(await screen.findByText(`Connected ${DESIGN}`)).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'Changes' })).toHaveTextContent(
    `by ${FINANCE} on 25 Sep 2026`,
  )
})
