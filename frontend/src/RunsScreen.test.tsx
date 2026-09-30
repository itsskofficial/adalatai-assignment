import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { polling, type ModelCost, type RunsOfMonth, type RunView } from './runsApi'
import { openDashboard, Reply, serve, signedIn } from './test/dashboard'

const ENGINEERING = 'engineering@nyayalabs.example'
const OPS = 'ops@nyayalabs.example'
const FINANCE = 'finance@nyayalabs.example'
const RUNS = '/api/months/2026-08/runs'

function run(id: number, changes: Partial<RunView> = {}): RunView {
  return {
    id,
    started_by: 'schedule',
    person: null,
    only_source_account: null,
    state: 'finished',
    started_at: '2026-09-03T00:30:00+00:00',
    finished_at: '2026-09-03T00:33:20+00:00',
    duration_seconds: 200,
    emails_found: 9,
    collected: 5,
    needs_review: 1,
    skipped: 2,
    failed: 1,
    model_cost_usd: null,
    models: [],
    source_accounts: [
      { source_account: ENGINEERING, read: true, reason: null, can_run_again: false },
      {
        source_account: OPS,
        read: false,
        reason: 'the sign-in has expired',
        can_run_again: true,
      },
    ],
    problem: null,
    ...changes,
  }
}

function month(changes: Partial<RunsOfMonth> = {}): RunsOfMonth {
  return {
    month: '2026-08',
    running: null,
    runs: [run(1)],
    not_started: [],
    connected_source_accounts: 2,
    cannot_start: null,
    ...changes,
  }
}

function serveRuns(answers: Record<string, unknown> = {}) {
  return serve(
    signedIn({
      'GET /api/months': { months: ['2026-08'] },
      [`GET ${RUNS}`]: month(),
      ...answers,
    }),
  )
}

async function openRuns() {
  openDashboard('/runs')
  await screen.findByRole('heading', { name: 'Runs', level: 1 })
  return screen.findByRole('region', { name: 'Runs of the month' })
}

function runCard(started = 'Run started 3 Sep 2026 at 00:30 UTC'): HTMLElement {
  return screen.getByRole('article', { name: started })
}

function figure(card: HTMLElement, label: string): string | null {
  const term = within(card).getByText(label, { selector: 'dt' })
  return term.nextElementSibling?.textContent ?? null
}

beforeEach(() => {
  polling.everyMs = 10
})

afterEach(() => {
  polling.everyMs = 2000
})

test('each run shows its counts and its metrics', async () => {
  serveRuns()
  await openRuns()

  const card = runCard()
  expect(figure(card, 'Emails found')).toBe('9')
  expect(figure(card, 'Collected')).toBe('5')
  expect(figure(card, 'Needs review')).toBe('1')
  expect(figure(card, 'Skipped')).toBe('2')
  expect(figure(card, 'Failed')).toBe('1')
  expect(figure(card, 'Duration')).toBe('3 min 20 s')
  expect(card).toHaveTextContent('Finished')
})

test('a model cost that was not recorded says so and is never shown as zero', async () => {
  serveRuns({
    [`GET ${RUNS}`]: month({
      runs: [
        run(2, { model_cost_usd: '0.42', started_at: '2026-09-04T10:00:00+00:00' }),
        run(1),
      ],
    }),
  })
  await openRuns()

  expect(figure(runCard('Run started 4 Sep 2026 at 10:00 UTC'), 'Model cost')).toBe('$0.42')
  expect(figure(runCard(), 'Model cost')).toBe('Not recorded')
})

const HAIKU: ModelCost = {
  model: 'claude-haiku-4-5',
  calls: 5,
  input_tokens: 11825,
  output_tokens: 285,
  cost_usd: '0.013250',
}
const JEV: ModelCost = {
  model: 'jev-latest',
  calls: 8,
  input_tokens: 1696,
  output_tokens: 64,
  cost_usd: '0.000071232',
}

test('a run shows what its model calls cost in all and for each model', async () => {
  serveRuns({
    [`GET ${RUNS}`]: month({
      runs: [run(1, { model_cost_usd: '0.013321232', models: [HAIKU, JEV] })],
    }),
  })
  await openRuns()

  const card = runCard()
  expect(figure(card, 'Model cost')).toBe('$0.0133')
  const table = within(card).getByRole('table', { name: 'Model calls' })
  const rows = within(table)
    .getAllByRole('row')
    .map((row) => within(row).queryAllByRole('cell').map((cell) => cell.textContent))
    .filter((cells) => cells.length > 0)
  expect(rows).toEqual([
    ['claude-haiku-4-5', '5', '11,825', '285', '$0.0133'],
    ['jev-latest', '8', '1,696', '64', '$0.0001'],
  ])
})

test('a run that called no model cost nothing, which differs from not recorded', async () => {
  serveRuns({ [`GET ${RUNS}`]: month({ runs: [run(1, { model_cost_usd: '0', models: [] })] }) })
  await openRuns()

  const card = runCard()
  expect(figure(card, 'Model cost')).toBe('$0')
  expect(card).toHaveTextContent('No model was called.')
  expect(within(card).queryByRole('table', { name: 'Model calls' })).toBeNull()
})

test('a cost that is not known says so and still lists the models called', async () => {
  const unpriced: ModelCost = { ...JEV, model: 'jev-2-preview', cost_usd: null }
  serveRuns({
    [`GET ${RUNS}`]: month({ runs: [run(1, { model_cost_usd: null, models: [HAIKU, unpriced] })] }),
  })
  await openRuns()

  const card = runCard()
  expect(figure(card, 'Model cost')).toBe('Unknown')
  const table = within(card).getByRole('table', { name: 'Model calls' })
  expect(within(table).getByRole('row', { name: /jev-2-preview/ })).toHaveTextContent(
    'Not known',
  )
})

test('how each run was started, and by whom from the dashboard, is shown', async () => {
  serveRuns({
    [`GET ${RUNS}`]: month({
      runs: [
        run(3, { started_by: 'dashboard', person: FINANCE, started_at: '2026-09-05T10:00:00+00:00' }),
        run(2, { started_by: 'command_line', started_at: '2026-09-04T10:00:00+00:00' }),
        run(1),
      ],
    }),
  })
  await openRuns()

  expect(runCard('Run started 5 Sep 2026 at 10:00 UTC')).toHaveTextContent(
    `From the dashboard by ${FINANCE}`,
  )
  expect(runCard('Run started 4 Sep 2026 at 10:00 UTC')).toHaveTextContent(
    'From the command line',
  )
  expect(runCard()).toHaveTextContent('On the schedule')
})

test('a failed source account shows why it failed', async () => {
  serveRuns()
  await openRuns()

  const failed = within(runCard()).getByRole('list', { name: 'Source accounts not read' })
  expect(failed).toHaveTextContent(`Could not read ${OPS}: the sign-in has expired`)
  expect(runCard()).toHaveTextContent(`Read: ${ENGINEERING}`)
})

test('a month is run again, and the screen follows the run until it finishes', async () => {
  let answers = 0
  const running = month({
    running: {
      started_by: 'dashboard',
      person: FINANCE,
      only_source_account: null,
      requested_at: '2026-09-29T10:30:00+00:00',
    },
    runs: [
      run(2, {
        started_by: 'dashboard',
        person: FINANCE,
        state: 'running',
        started_at: '2026-09-29T10:30:00+00:00',
        finished_at: null,
        duration_seconds: null,
        emails_found: null,
        collected: null,
        needs_review: null,
        skipped: null,
        failed: null,
      }),
      run(1),
    ],
    cannot_start: `A run of 2026-08 is already going on, started by ${FINANCE}.`,
  })
  const finished = month({
    runs: [
      run(2, { started_by: 'dashboard', person: FINANCE, started_at: '2026-09-29T10:30:00+00:00' }),
      run(1),
    ],
  })
  const calls = serveRuns({
    [`GET ${RUNS}`]: () => {
      answers += 1
      if (answers === 1) return month()
      return answers < 4 ? running : finished
    },
    [`POST ${RUNS}`]: { month: '2026-08', source_account: null },
  })
  await openRuns()

  await userEvent.click(screen.getByRole('button', { name: 'Run August 2026 again' }))

  expect(calls).toContainEqual({ method: 'POST', path: RUNS, body: { source_account: null } })
  expect(await screen.findByText(/Started a run of August 2026/)).toBeVisible()
  expect(await screen.findByText(/A run of August 2026 is going on/)).toHaveTextContent(
    `started by ${FINANCE}`,
  )
  const going = runCard('Run started 29 Sep 2026 at 10:30 UTC')
  expect(going).toHaveTextContent('Running')
  expect(figure(going, 'Collected')).toBe('Not yet known')
  expect(screen.getByRole('button', { name: 'Run August 2026 again' })).toBeDisabled()

  await waitFor(() => expect(screen.queryByText(/is going on/)).not.toBeInTheDocument())
  expect(runCard('Run started 29 Sep 2026 at 10:30 UTC')).toHaveTextContent('Finished')
  expect(screen.getByRole('button', { name: 'Run August 2026 again' })).toBeEnabled()
})

test('a second run while one is going on is refused with the reason', async () => {
  const reason = `A run of 2026-08 is already going on, started by ${OPS}.`
  serveRuns({ [`POST ${RUNS}`]: new Reply(409, { detail: reason }) })
  await openRuns()

  await userEvent.click(screen.getByRole('button', { name: 'Run August 2026 again' }))

  expect(await screen.findByRole('alert')).toHaveTextContent(reason)
})

test('a run that cannot start says why, and its button is off', async () => {
  serveRuns({
    [`GET ${RUNS}`]: month({
      cannot_start: 'No source account is connected. Connect one on the Source accounts screen first.',
    }),
  })
  await openRuns()

  expect(screen.getByRole('button', { name: 'Run August 2026 again' })).toBeDisabled()
  expect(screen.getByText(/No source account is connected/)).toBeVisible()
})

test('a single failed source account is run again alone', async () => {
  const calls = serveRuns({ [`POST ${RUNS}`]: { month: '2026-08', source_account: OPS } })
  await openRuns()

  await userEvent.click(within(runCard()).getByRole('button', { name: `Run ${OPS} again` }))

  expect(calls).toContainEqual({ method: 'POST', path: RUNS, body: { source_account: OPS } })
  expect(await screen.findByText(new RegExp(`Started running ${OPS} again`))).toBeVisible()
})

test('a failed source account read since is not offered to run again', async () => {
  serveRuns({
    [`GET ${RUNS}`]: month({
      runs: [
        run(1, {
          source_accounts: [
            { source_account: OPS, read: false, reason: 'the sign-in has expired', can_run_again: false },
          ],
        }),
      ],
    }),
  })
  await openRuns()

  expect(runCard()).toHaveTextContent(`Could not read ${OPS}: the sign-in has expired`)
  expect(within(runCard()).queryByRole('button', { name: `Run ${OPS} again` })).not.toBeInTheDocument()
})

test('a run for one source account says it read only that one', async () => {
  serveRuns({
    [`GET ${RUNS}`]: month({
      runs: [run(1, { started_by: 'dashboard', person: FINANCE, only_source_account: OPS })],
    }),
  })
  await openRuns()

  expect(runCard()).toHaveTextContent(`Read ${OPS} again, and no other`)
})

test('a stopped run and a run started elsewhere that has not finished are told apart', async () => {
  serveRuns({
    [`GET ${RUNS}`]: month({
      runs: [
        run(2, {
          started_by: 'dashboard',
          person: FINANCE,
          state: 'stopped',
          started_at: '2026-09-04T10:00:00+00:00',
          finished_at: null,
          problem: 'the disk is full',
        }),
        run(1, { state: 'unfinished', finished_at: null }),
      ],
    }),
  })
  await openRuns()

  const stopped = runCard('Run started 4 Sep 2026 at 10:00 UTC')
  expect(stopped).toHaveTextContent('Stopped')
  expect(stopped).toHaveTextContent('It stopped before finishing: the disk is full')
  expect(runCard()).toHaveTextContent('Not finished')
  expect(runCard()).toHaveTextContent(/cannot tell whether it is still running/)
})

test('a run that did not start is listed with why', async () => {
  serveRuns({
    [`GET ${RUNS}`]: month({
      not_started: [
        {
          person: FINANCE,
          only_source_account: null,
          requested_at: '2026-09-28T09:00:00+00:00',
          problem: 'the owner account is not signed in',
        },
      ],
    }),
  })
  await openRuns()

  const section = screen.getByRole('region', { name: 'Runs that did not start' })
  expect(section).toHaveTextContent(
    `Asked for by ${FINANCE} on 28 Sep 2026 at 09:00 UTC: the owner account is not signed in`,
  )
})

test('with no month yet, the month that has just ended is chosen and can be run', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date(2026, 8, 30) })
  const calls = serve(
    signedIn({
      'GET /api/months': { months: [] },
      'POST /api/months/2026-08/runs': { month: '2026-08', source_account: null },
      [`GET ${RUNS}`]: month({ runs: [] }),
    }),
  )
  openDashboard('/runs')

  expect(await screen.findByText('No run of August 2026 has been recorded yet.')).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Run August 2026' }))

  expect(calls).toContainEqual({ method: 'POST', path: RUNS, body: { source_account: null } })
  vi.useRealTimers()
})

test('a scheduled run going on in the runner says the schedule started it', async () => {
  serveRuns({
    [`GET ${RUNS}`]: month({
      running: {
        started_by: 'schedule',
        person: null,
        only_source_account: null,
        requested_at: '2026-09-03T00:30:00+00:00',
      },
      runs: [run(1, { state: 'running', finished_at: null, duration_seconds: null })],
      cannot_start: 'A run of 2026-08 is already going on, started by the schedule.',
    }),
  })
  await openRuns()

  expect(screen.getByText(/A run of August 2026 is going on/)).toHaveTextContent(
    'started by the schedule on 3 Sep 2026 at 00:30 UTC',
  )
  expect(runCard()).toHaveTextContent('Running')
  expect(screen.getByRole('button', { name: 'Run August 2026 again' })).toBeDisabled()
})

test('a runner that cannot be reached is said plainly, and its runs are not called stopped', async () => {
  const problem =
    'The runner cannot be reached at http://runner:8001 (connection refused). Check that the runner service is running.'
  serveRuns({
    [`GET ${RUNS}`]: month({
      runs: [run(1, { started_by: 'dashboard', person: FINANCE, state: 'unfinished', finished_at: null })],
      cannot_start: problem,
      runner_problem: problem,
    }),
  })
  await openRuns()

  expect(screen.getByRole('alert')).toHaveTextContent(problem)
  expect(screen.getByRole('button', { name: 'Run August 2026 again' })).toBeDisabled()
  expect(runCard()).toHaveTextContent('Not finished')
  expect(runCard()).toHaveTextContent(
    'The runner cannot be reached, so whether it is still running cannot be told.',
  )
})
