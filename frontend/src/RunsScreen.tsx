import { useEffect, useId, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router'
import { NotSignedIn } from './api'
import { formatDate, formatDollars, isCollectionMonth, monthName } from './format'
import {
  polling,
  runAgain,
  runsOfMonth,
  type ModelCost,
  type RunGoingOn,
  type RunNotStarted,
  type RunsOfMonth,
  type RunView,
  type StartedBy,
} from './runsApi'
import { useShell } from './shell'
import type { Loaded } from './useLoaded'

const STATES: Record<RunView['state'], string> = {
  running: 'Running',
  finished: 'Finished',
  stopped: 'Stopped',
  unfinished: 'Not finished',
}

/** "3 Sep 2026 at 00:30 UTC", read as written so it never shifts by time zone. */
function formatMoment(iso: string): string {
  const time = iso.slice(11, 16)
  const utc = /(\+00:00|Z)$/.test(iso) ? ' UTC' : ''
  return time ? `${formatDate(iso)} at ${time}${utc}` : formatDate(iso)
}

function formatDuration(seconds: number): string {
  const whole = Math.round(seconds)
  if (whole < 1) return 'under a second'
  const hours = Math.floor(whole / 3600)
  const minutes = Math.floor((whole % 3600) / 60)
  const rest = whole % 60
  return [hours && `${hours} h`, minutes && `${minutes} min`, rest && `${rest} s`]
    .filter(Boolean)
    .join(' ')
}

function howStarted(startedBy: StartedBy, person: string | null): string {
  switch (startedBy) {
    case 'schedule':
      return 'On the schedule'
    case 'command_line':
      return 'From the command line'
    default:
      return person ? `From the dashboard by ${person}` : 'From the dashboard'
  }
}

function problemText(problem: unknown): string {
  return problem instanceof Error ? problem.message : String(problem)
}

export function RunsScreen() {
  const { month, monthsLoading } = useShell()

  if (month === null) {
    return (
      <main className="screen">
        <h1>Runs</h1>
        {monthsLoading ? <p className="empty">Loading…</p> : <FirstRun />}
      </main>
    )
  }
  if (!isCollectionMonth(month)) {
    return (
      <main className="screen">
        <h1>Runs</h1>
        <p className="reasons" role="alert">
          {month} is not a collection month. Choose one from the list above.
        </p>
      </main>
    )
  }
  return <RunsOfOneMonth key={month} month={month} />
}

/** No month has been run yet: the person chooses the first. */
function FirstRun() {
  const { onSignedOut } = useShell()
  const [, setSearch] = useSearchParams()
  const id = useId()
  const [chosen, setChosen] = useState('')
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setRefused(null)
    try {
      await runAgain(chosen, null)
      setSearch({ month: chosen })
    } catch (problem) {
      if (problem instanceof NotSignedIn) {
        onSignedOut()
        return
      }
      setRefused(problemText(problem))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="panel run-first" aria-label="Run a month" onSubmit={submit}>
      <p className="empty">No run has been recorded yet.</p>
      <div className="connect-row">
        <label htmlFor={id}>Collection month</label>
        <input
          id={id}
          type="month"
          required
          value={chosen}
          onChange={(event) => setChosen(event.target.value)}
        />
        <button type="submit" className="primary" disabled={busy || !isCollectionMonth(chosen)}>
          Run
        </button>
      </div>
      {refused && (
        <p className="reasons" role="alert">
          {refused}
        </p>
      )}
    </form>
  )
}

function RunsOfOneMonth({ month }: { month: string }) {
  const { onSignedOut } = useShell()
  const [loaded, setLoaded] = useState<Loaded<RunsOfMonth>>({ status: 'loading' })
  // Counts up to read the runs again: after a run is started, and while one goes on.
  const [reads, setReads] = useState(0)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ kind: 'status' | 'alert'; text: string } | null>(null)

  useEffect(() => {
    const abort = new AbortController()
    runsOfMonth(month, abort.signal).then(
      (value) => {
        if (!abort.signal.aborted) setLoaded({ status: 'ready', value })
      },
      (problem: unknown) => {
        if (abort.signal.aborted) return
        if (problem instanceof NotSignedIn) {
          onSignedOut()
          return
        }
        setLoaded({ status: 'problem', message: problemText(problem) })
      },
    )
    return () => abort.abort()
  }, [month, reads, onSignedOut])

  const going = loaded.status === 'ready' && loaded.value.running !== null
  useEffect(() => {
    if (!going) return
    const timer = setTimeout(() => setReads((count) => count + 1), polling.everyMs)
    return () => clearTimeout(timer)
  }, [going, reads])

  async function start(sourceAccount: string | null) {
    setBusy(true)
    setNotice(null)
    try {
      await runAgain(month, sourceAccount)
      setNotice({
        kind: 'status',
        text: sourceAccount
          ? `Started running ${sourceAccount} again for ${monthName(month)}. It goes on in the background.`
          : `Started a run of ${monthName(month)}. It goes on in the background.`,
      })
    } catch (problem) {
      if (problem instanceof NotSignedIn) {
        onSignedOut()
        return
      }
      setNotice({ kind: 'alert', text: problemText(problem) })
    } finally {
      setBusy(false)
      setReads((count) => count + 1)
    }
  }

  const runs = loaded.status === 'ready' ? loaded.value : null
  const cannotStart = runs?.cannot_start ?? null
  return (
    <main className="screen">
      <h1>Runs</h1>
      <p className="lede">
        Every run of {monthName(month)}: how it was started, what it found, and each source
        account it could not read. A run started here goes on in the background, and this page
        follows it.
      </p>
      {notice?.kind === 'status' && <output className="notice">{notice.text}</output>}
      {notice?.kind === 'alert' && (
        <p className="reasons" role="alert">
          {notice.text}
        </p>
      )}
      {loaded.status === 'loading' && <p className="empty">Loading…</p>}
      {loaded.status === 'problem' && (
        <p className="reasons" role="alert">
          {loaded.message}
        </p>
      )}
      {runs && (
        <>
          <div className="actions run-month">
            <button
              type="button"
              className="primary"
              disabled={busy || cannotStart !== null}
              onClick={() => start(null)}
            >
              {runs.runs.length > 0 ? `Run ${monthName(month)} again` : `Run ${monthName(month)}`}
            </button>
            {cannotStart && !runs.running && <span className="hint">{cannotStart}</span>}
          </div>
          {runs.running && <GoingOn month={month} going={runs.running} />}
          {runs.not_started.length > 0 && <NotStarted requests={runs.not_started} />}
          <section aria-label="Runs of the month">
            <h2>
              Runs <small>{runs.runs.length}</small>
            </h2>
            {runs.runs.length === 0 ? (
              <p className="empty">No run of {monthName(month)} has been recorded yet.</p>
            ) : (
              <div className="runs">
                {runs.runs.map((run) => (
                  <RunCard
                    key={run.id}
                    run={run}
                    canStart={!busy && cannotStart === null}
                    onRunAgain={start}
                  />
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </main>
  )
}

function GoingOn({ month, going }: { month: string; going: RunGoingOn }) {
  return (
    <output className="notice run-going-on">
      {going.only_source_account
        ? `${going.only_source_account} is being read again for ${monthName(month)}`
        : `A run of ${monthName(month)} is going on`}
      , started by {going.person} on {formatMoment(going.requested_at)}. This page updates by
      itself.
    </output>
  )
}

function NotStarted({ requests }: { requests: RunNotStarted[] }) {
  return (
    <section aria-label="Runs that did not start">
      <h2>Did not start</h2>
      <ul className="changes">
        {requests.map((request) => (
          <li key={request.requested_at} className="could-not-read">
            <strong>
              Asked for by {request.person} on {formatMoment(request.requested_at)}
              {request.only_source_account && <> for {request.only_source_account} only</>}
            </strong>
            : {request.problem ?? 'no reason was recorded'}
          </li>
        ))}
      </ul>
    </section>
  )
}

function ModelCalls({ models }: { models: ModelCost[] }) {
  return (
    <div className="model-calls">
      <table aria-label="Model calls">
        <thead>
          <tr>
            <th scope="col">Model</th>
            <th scope="col" className="amount">
              Calls
            </th>
            <th scope="col" className="amount">
              Input tokens
            </th>
            <th scope="col" className="amount">
              Output tokens
            </th>
            <th scope="col" className="amount">
              Cost
            </th>
          </tr>
        </thead>
        <tbody>
          {models.map((each) => (
            <tr key={each.model}>
              <td>{each.model}</td>
              <td className="amount">{each.calls.toLocaleString('en-US')}</td>
              <td className="amount">{each.input_tokens.toLocaleString('en-US')}</td>
              <td className="amount">{each.output_tokens.toLocaleString('en-US')}</td>
              <td className="amount">
                {each.cost_usd === null ? (
                  <span className="not-available">Not known</span>
                ) : (
                  formatDollars(each.cost_usd)
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Figure({ label, value }: { label: string; value: string | number | null }) {
  return (
    <div className="figure">
      <dt>{label}</dt>
      {value === null ? (
        <dd className="not-available">Not yet known</dd>
      ) : (
        <dd>{value}</dd>
      )}
    </div>
  )
}

function whyNotFinished(run: RunView): string | null {
  if (run.state === 'stopped') {
    return run.problem
      ? `It stopped before finishing: ${run.problem}`
      : 'It stopped before finishing: the dashboard service stopped while it ran.'
  }
  if (run.state === 'unfinished') {
    return 'It has not finished. It was started outside the dashboard, which cannot tell whether it is still running there or stopped.'
  }
  if (run.problem) return `It finished, then reported a problem: ${run.problem}`
  return null
}

function RunCard({
  run,
  canStart,
  onRunAgain,
}: {
  run: RunView
  canStart: boolean
  onRunAgain: (sourceAccount: string) => void
}) {
  const id = useId()
  const why = whyNotFinished(run)
  const failed = run.source_accounts.filter((each) => !each.read)
  const read = run.source_accounts.filter((each) => each.read)
  return (
    <article className={`run run-${run.state}`} aria-labelledby={id}>
      <div className="run-head">
        <h3 id={id}>Run started {formatMoment(run.started_at)}</h3>
        <span className={`tag tag-run-${run.state}`}>{STATES[run.state]}</span>
      </div>
      <ul className="facts">
        <li>{howStarted(run.started_by, run.person)}</li>
        {run.only_source_account && <li>Read {run.only_source_account} again, and no other</li>}
        {run.finished_at && <li>Finished {formatMoment(run.finished_at)}</li>}
      </ul>
      {why && <p className={run.state === 'unfinished' ? 'hint' : 'reasons'}>{why}</p>}
      <dl className="run-figures">
        <Figure label="Emails found" value={run.emails_found} />
        <Figure label="Collected" value={run.collected} />
        <Figure label="Needs review" value={run.needs_review} />
        <Figure label="Skipped" value={run.skipped} />
        <Figure label="Failed" value={run.failed} />
        <Figure
          label="Duration"
          value={run.duration_seconds === null ? null : formatDuration(run.duration_seconds)}
        />
        <div className="figure">
          <dt>Model cost</dt>
          {run.model_cost_usd !== null ? (
            <dd>{formatDollars(run.model_cost_usd)}</dd>
          ) : run.models.length > 0 ? (
            <dd className="not-available">Unknown</dd>
          ) : (
            <dd className="not-available">Not recorded</dd>
          )}
        </div>
      </dl>
      {run.model_cost_usd !== null && run.models.length === 0 && (
        <p className="hint">No model was called.</p>
      )}
      {run.models.length > 0 && <ModelCalls models={run.models} />}
      {failed.length > 0 && (
        <ul className="facts run-failed-accounts" aria-label="Source accounts not read">
          {failed.map((each) => (
            <li key={each.source_account} className="could-not-read">
              <span>
                Could not read {each.source_account}: {each.reason ?? 'no reason was recorded'}
              </span>
              {each.can_run_again && (
                <button
                  type="button"
                  disabled={!canStart}
                  onClick={() => onRunAgain(each.source_account)}
                >
                  Run {each.source_account} again
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {read.length > 0 && (
        <p className="hint">Read: {read.map((each) => each.source_account).join(', ')}</p>
      )}
    </article>
  )
}
