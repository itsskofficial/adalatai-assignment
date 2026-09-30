import { fireEvent, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'
import type { CollectionSettings, SettingsSaved } from './settingsApi'
import { openDashboard, Reply, serve, signedIn } from './test/dashboard'

const ADMIN = 'admin@nyayalabs.example'
const SETTINGS = '/api/settings'

function settings(changes: Partial<CollectionSettings> = {}): CollectionSettings {
  return {
    schedule: { enabled: false, day: 3, time: '06:00', time_zone: 'Asia/Kolkata' },
    drive_folder: 'Invoice Collection',
    next_run: null,
    can_change: true,
    runner: true,
    changes: [],
    ...changes,
  }
}

function serveSettings(answers: Record<string, unknown> = {}, role = 'administrator') {
  return serve(
    signedIn({
      'GET /api/me': { email: ADMIN, role },
      'GET /api/months': { months: ['2026-08'] },
      [`GET ${SETTINGS}`]: settings(),
      ...answers,
    }),
  )
}

async function openSettings() {
  openDashboard('/settings')
  return screen.findByRole('form', { name: 'Settings' })
}

test('the settings screen is in the navigation', async () => {
  serveSettings()
  await openSettings()

  const nav = screen.getByRole('navigation', { name: 'Screens' })
  expect(within(nav).getByRole('link', { name: 'Settings' })).toHaveAttribute(
    'href',
    '/settings',
  )
})

test('settings never set show their defaults, with the schedule off', async () => {
  serveSettings()
  const form = await openSettings()

  expect(within(form).getByLabelText('Collect on a schedule')).not.toBeChecked()
  expect(within(form).getByLabelText('Day of the month')).toHaveValue(3)
  expect(within(form).getByLabelText('Time of day')).toHaveValue('06:00')
  expect(within(form).getByLabelText('Time zone')).toHaveValue('Asia/Kolkata')
  expect(within(form).getByLabelText("Folder in the owner account's Drive")).toHaveValue(
    'Invoice Collection',
  )
  expect(screen.getByLabelText('Next scheduled run')).toHaveTextContent(
    'The schedule is off. Nothing runs by itself.',
  )
  expect(form).toHaveTextContent('nothing already filed is moved')
  expect(screen.getByText('No setting has been changed yet. Each is at its default.')).toBeVisible()
})

test('turning the schedule on saves it and shows the next run and the month it collects', async () => {
  const saved: SettingsSaved = {
    ...settings({
      schedule: { enabled: true, day: 5, time: '06:00', time_zone: 'Asia/Kolkata' },
      next_run: { due_at: '2026-09-05T06:00:00+05:30', collection_month: '2026-08' },
      changes: [
        {
          name: 'schedule.day',
          value_before: '3',
          value_after: '5',
          person: ADMIN,
          changed_at: '2026-09-01T10:00:00+00:00',
        },
        {
          name: 'schedule.enabled',
          value_before: 'off',
          value_after: 'on',
          person: ADMIN,
          changed_at: '2026-09-01T10:00:00+00:00',
        },
      ],
    }),
    runner_notice: 'The runner has the new schedule.',
  }
  const calls = serveSettings({ [`PUT ${SETTINGS}`]: saved })
  const form = await openSettings()

  await userEvent.click(within(form).getByLabelText('Collect on a schedule'))
  fireEvent.change(within(form).getByLabelText('Day of the month'), { target: { value: '5' } })
  await userEvent.click(within(form).getByRole('button', { name: 'Save settings' }))

  expect(calls).toContainEqual({
    method: 'PUT',
    path: SETTINGS,
    body: {
      schedule: { enabled: true, day: 5, time: '06:00', time_zone: 'Asia/Kolkata' },
      drive_folder: 'Invoice Collection',
    },
  })
  expect(await screen.findByText('The settings are saved.')).toBeVisible()
  expect(screen.getByText('The runner has the new schedule.')).toBeVisible()
  expect(screen.getByLabelText('Next scheduled run')).toHaveTextContent(
    'Next run: 5 Sep 2026 at 06:00 (Asia/Kolkata), collecting August 2026.',
  )
  const changes = screen.getByRole('region', { name: 'Changes to the settings' })
  expect(changes).toHaveTextContent(`${ADMIN} changed the day of the month from 3 to 5`)
  expect(changes).toHaveTextContent(`${ADMIN} changed the schedule from off to on`)
})

test('a change kept while the runner cannot be reached says it will pick it up', async () => {
  const notice =
    'The change is saved. The runner cannot be reached at http://runner:8001 (connection refused). Check that the runner service is running. The runner will pick up the new schedule when it starts.'
  serveSettings({
    [`PUT ${SETTINGS}`]: {
      ...settings({ schedule: { enabled: true, day: 3, time: '06:00', time_zone: 'Asia/Kolkata' } }),
      runner_notice: notice,
    },
  })
  const form = await openSettings()

  await userEvent.click(within(form).getByLabelText('Collect on a schedule'))
  await userEvent.click(within(form).getByRole('button', { name: 'Save settings' }))

  expect(await screen.findByText(notice)).toBeVisible()
})

test('a change that cannot be kept says why', async () => {
  serveSettings({
    [`PUT ${SETTINGS}`]: new Reply(422, {
      detail: "The Drive folder's name cannot hold a /, which would read as a folder inside another.",
    }),
  })
  const form = await openSettings()

  const folder = within(form).getByLabelText("Folder in the owner account's Drive")
  await userEvent.clear(folder)
  await userEvent.type(folder, 'Finance/2026')
  await userEvent.click(within(form).getByRole('button', { name: 'Save settings' }))

  expect(await screen.findByRole('alert')).toHaveTextContent("cannot hold a /")
})

test('a member sees the settings and cannot change them', async () => {
  serveSettings({ [`GET ${SETTINGS}`]: settings({ can_change: false }) }, 'member')
  const form = await openSettings()

  expect(screen.getByText(/Only an administrator can change these settings/)).toBeVisible()
  expect(within(form).getByLabelText('Day of the month')).toBeDisabled()
  expect(within(form).getByLabelText("Folder in the owner account's Drive")).toBeDisabled()
  expect(within(form).queryByRole('button', { name: 'Save settings' })).not.toBeInTheDocument()
})

test('without a runner the screen says nothing runs on the schedule', async () => {
  serveSettings({ [`GET ${SETTINGS}`]: settings({ runner: false }) })
  await openSettings()

  expect(screen.getByText(/No runner service is set up for this dashboard/)).toBeVisible()
})
