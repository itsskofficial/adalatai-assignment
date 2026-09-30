import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'
import { AUGUST, emptySummary, openDashboard, serve, signedIn } from './test/dashboard'

test('sign-in page appears when not signed in', async () => {
  serve({})

  openDashboard()

  const button = await screen.findByRole('button', { name: 'Sign in with Google' })
  expect(button.closest('form')).toHaveAttribute('action', '/auth/login')
  expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
})

test('sign-in page says when the address is not on the allowlist', async () => {
  serve({})

  openDashboard('/?sign_in=refused')

  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent('That address is not allowed to use the dashboard')
  expect(alert).toHaveTextContent('An administrator can add it on the People screen')
})

test('sign-in page says when sign-in did not complete', async () => {
  serve({})

  openDashboard('/?sign_in=failed')

  expect(await screen.findByRole('alert')).toHaveTextContent('Sign-in did not complete')
})

test('signed-in person sees their email and every screen in the top bar', async () => {
  serve(signedIn({ 'GET /api/months': { months: [] } }))

  openDashboard()

  expect(await screen.findByText('finance@nyayalabs.example')).toBeInTheDocument()
  const links = within(screen.getByRole('navigation')).getAllByRole('link')
  expect(links.map((link) => link.textContent)).toEqual([
    'Summary',
    'Review',
    'Vendors',
    'Spend',
    'Questions',
    'Source accounts',
    'Runs',
    'Settings',
  ])
})

test('an administrator also sees People, after every other screen', async () => {
  serve(
    signedIn({
      'GET /api/me': { email: 'finance@nyayalabs.example', role: 'administrator' },
      'GET /api/months': { months: [] },
    }),
  )

  openDashboard()

  const nav = await screen.findByRole('navigation')
  await within(nav).findByRole('link', { name: 'People' })
  expect(within(nav).getAllByRole('link').map((link) => link.textContent)).toEqual([
    'Summary',
    'Review',
    'Vendors',
    'Spend',
    'Questions',
    'Source accounts',
    'Runs',
    'Settings',
    'People',
  ])
})

test('dashboard opens on the summary of the newest collection month', async () => {
  serve(
    signedIn({
      'GET /api/months': { months: ['2026-08', '2026-07'] },
      'GET /api/months/2026-08/summary': AUGUST,
    }),
  )

  openDashboard()

  expect(await screen.findByRole('heading', { name: 'Summary for August 2026' })).toBeVisible()
  expect(screen.getByRole('combobox', { name: 'Collection month' })).toHaveValue('2026-08')
  expect(await screen.findByRole('cell', { name: 'Slack' })).toBeInTheDocument()
})

test('choosing another collection month shows its summary', async () => {
  serve(
    signedIn({
      'GET /api/months': { months: ['2026-08', '2026-07'] },
      'GET /api/months/2026-08/summary': AUGUST,
      'GET /api/months/2026-07/summary': emptySummary('2026-07'),
    }),
  )
  openDashboard()
  await screen.findByRole('cell', { name: 'Slack' })

  await userEvent.selectOptions(
    screen.getByRole('combobox', { name: 'Collection month' }),
    'July 2026',
  )

  expect(await screen.findByRole('heading', { name: 'Summary for July 2026' })).toBeVisible()
  expect(screen.queryByRole('cell', { name: 'Slack' })).not.toBeInTheDocument()
})

test('chosen collection month is kept when moving between screens', async () => {
  serve(
    signedIn({
      'GET /api/months': { months: ['2026-08', '2026-07'] },
      'GET /api/months/2026-07/summary': emptySummary('2026-07'),
      'GET /api/months/2026-07/review': { month: '2026-07', items: [] },
    }),
  )
  openDashboard('/summary?month=2026-07')
  await screen.findByRole('heading', { name: 'Summary for July 2026' })

  await userEvent.click(screen.getByRole('link', { name: 'Review' }))
  await screen.findByText('Nothing needs review for July 2026.')
  await userEvent.click(screen.getByRole('link', { name: 'Summary' }))

  expect(await screen.findByRole('heading', { name: 'Summary for July 2026' })).toBeVisible()
})

test('signing out returns to the sign-in page', async () => {
  const calls = serve(
    signedIn({ 'GET /api/months': { months: [] }, 'POST /auth/logout': null }),
  )
  openDashboard()

  await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }))

  expect(await screen.findByRole('button', { name: 'Sign in with Google' })).toBeVisible()
  expect(calls).toContainEqual({ method: 'POST', path: '/auth/logout' })
  expect(screen.queryByText('finance@nyayalabs.example')).not.toBeInTheDocument()
})

test('sign-in page appears when the session ends while the dashboard is open', async () => {
  // The month summary is not in the table, so it answers 401.
  serve(signedIn({ 'GET /api/months': { months: ['2026-08'] } }))

  openDashboard()

  expect(await screen.findByRole('button', { name: 'Sign in with Google' })).toBeVisible()
})

test('a problem reaching the API is shown, not hidden', async () => {
  serve({})
  globalThis.fetch = () => Promise.reject(new TypeError('Failed to fetch'))

  openDashboard()

  expect(await screen.findByRole('alert')).toHaveTextContent('The dashboard could not reach the API')
})
