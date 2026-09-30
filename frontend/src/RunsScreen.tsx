import {
  CpuIcon,
  LoaderCircleIcon,
  PlayIcon,
  RefreshCwIcon,
  type LucideIcon,
} from 'lucide-react'
import { useEffect, useId, useState, type FormEvent, type ReactNode } from 'react'
import { useSearchParams } from 'react-router'
import { NotSignedIn } from './api'
import { NotAvailable } from './components/Amount'
import { EmptyState } from './components/EmptyState'
import { CardsSkeleton, Loading } from './components/Loading'
import { Hint, Problem, Status } from './components/Notice'
import { PageHeader, Screen, Section } from './components/Screen'
import { Badge } from './components/ui/badge'
import { Button } from './components/ui/button'
import { Input } from './components/ui/input'
import { Label } from './components/ui/label'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from './components/ui/table'
import { formatDate, formatDollars, isCollectionMonth, monthName } from './format'
import { cn } from './lib/utils'
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

const STATE_VARIANTS: Record<RunView['state'], 'info' | 'success' | 'warning'> = {
  running: 'info',
  finished: 'success',
  stopped: 'warning',
  unfinished: 'warning',
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
      <Screen>
        <PageHeader
          title="Runs"
          description="Every run: how it was started, what it found, and each source account it could not read."
        />
        {monthsLoading ? (
          <Loading>
            <CardsSkeleton count={2} />
          </Loading>
        ) : (
          <FirstRun />
        )}
      </Screen>
    )
  }
  if (!isCollectionMonth(month)) {
    return (
      <Screen>
        <PageHeader title="Runs" />
        <Problem>{month} is not a collection month. Choose one from the list above.</Problem>
      </Screen>
    )
  }
  return <RunsOfOneMonth key={month} month={month} />
}

/** No month has been run yet: the person chooses the first. */
function FirstRun() {
  return (
    <MonthRunForm label="Run a month">
      <EmptyState icon={PlayIcon} compact>
        No run has been recorded yet.
      </EmptyState>
    </MonthRunForm>
  )
}

/**
 * Chooses a month and runs it: the first month, or one not yet in the list above. The
 * list holds only months with a run, so this is how a new month is started.
 */
function MonthRunForm({ label, children }: { label: string; children?: ReactNode }) {
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
    <form
      className="flex max-w-xl flex-col gap-4 rounded-xl border bg-card p-5 shadow-xs"
      aria-label={label}
      onSubmit={submit}
    >
      {children}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <div className="flex flex-1 flex-col gap-1.5">
          <Label htmlFor={id}>Collection month</Label>
          <Input
            id={id}
            type="month"
            required
            value={chosen}
            onChange={(event) => setChosen(event.target.value)}
          />
        </div>
        <Button type="submit" disabled={busy || !isCollectionMonth(chosen)}>
          <PlayIcon />
          Run
        </Button>
      </div>
      {refused && <Problem>{refused}</Problem>}
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
    <Screen>
      <PageHeader
        title="Runs"
        description={`Every run of ${monthName(month)}: how it was started, what it found, and each source account it could not read. A run started here goes on in the background, and this page follows it.`}
        actions={
          runs && (
            <Button disabled={busy || cannotStart !== null} onClick={() => start(null)}>
              {runs.running ? <LoaderCircleIcon className="animate-spin" /> : <PlayIcon />}
              {runs.runs.length > 0 ? `Run ${monthName(month)} again` : `Run ${monthName(month)}`}
            </Button>
          )
        }
      >
        {runs && cannotStart && !runs.running && !runs.runner_problem && <Hint>{cannotStart}</Hint>}
        {notice?.kind === 'status' && <Status tone="success">{notice.text}</Status>}
        {notice?.kind === 'alert' && <Problem>{notice.text}</Problem>}
      </PageHeader>
      {loaded.status === 'loading' && (
        <Loading>
          <CardsSkeleton count={2} />
        </Loading>
      )}
      {loaded.status === 'problem' && <Problem>{loaded.message}</Problem>}
      {runs && (
        <>
          {runs.runner_problem && (
            <Problem tone="destructive">
              {runs.runner_problem} Runs cannot be started, and whether a run is still going on
              cannot be told, until it can be reached.
            </Problem>
          )}
          {runs.running && <GoingOn month={month} going={runs.running} />}
          {runs.not_started.length > 0 && <NotStarted requests={runs.not_started} />}
          <Section aria-label="Runs of the month" title="Runs" count={runs.runs.length}>
            {runs.runs.length === 0 ? (
              <EmptyState icon={PlayIcon}>
                No run of {monthName(month)} has been recorded yet.
              </EmptyState>
            ) : (
              <div className="grid gap-4">
                {runs.runs.map((run) => (
                  <RunCard
                    key={run.id}
                    run={run}
                    runnerUnreachable={Boolean(runs.runner_problem)}
                    canStart={!busy && cannotStart === null}
                    onRunAgain={start}
                  />
                ))}
              </div>
            )}
          </Section>
          <Section
            aria-label="Run another month"
            title="Run another month"
            description="A month not in the list above has not been run yet. Choose it here; once it has run, it joins the list."
          >
            <MonthRunForm label="Run another month" />
          </Section>
        </>
      )}
    </Screen>
  )
}

function GoingOn({ month, going }: { month: string; going: RunGoingOn }) {
  return (
    <Status tone="info" className="run-going-on">
      <span className="flex items-center gap-2">
        <LoaderCircleIcon aria-hidden="true" className="size-4 shrink-0 animate-spin text-info" />
        <span>
          {going.only_source_account
            ? `${going.only_source_account} is being read again for ${monthName(month)}`
            : `A run of ${monthName(month)} is going on`}
          , started {going.person ? `by ${going.person}` : 'by the schedule'} on{' '}
          {formatMoment(going.requested_at)}. This page updates by itself.
        </span>
      </span>
    </Status>
  )
}

function NotStarted({ requests }: { requests: RunNotStarted[] }) {
  return (
    <Section aria-label="Runs that did not start" title="Did not start">
      <ul className="m-0 flex list-none flex-col divide-y rounded-xl border border-destructive/30 bg-card px-4 shadow-xs">
        {requests.map((request) => (
          <li key={request.requested_at} className="py-2.5 text-sm text-destructive">
            <strong>
              Asked for by {request.person} on {formatMoment(request.requested_at)}
              {request.only_source_account && <> for {request.only_source_account} only</>}
            </strong>
            : {request.problem ?? 'no reason was recorded'}
          </li>
        ))}
      </ul>
    </Section>
  )
}

function ModelCalls({ models }: { models: ModelCost[] }) {
  return (
    <Table aria-label="Model calls" className="w-auto min-w-[min(100%,32rem)]">
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Model</TableHead>
          <TableHead scope="col" className="amount">
            Calls
          </TableHead>
          <TableHead scope="col" className="amount">
            Input tokens
          </TableHead>
          <TableHead scope="col" className="amount">
            Output tokens
          </TableHead>
          <TableHead scope="col" className="amount">
            Cost
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {models.map((each) => (
          <TableRow key={each.model}>
            <TableCell className="font-mono text-xs">{each.model}</TableCell>
            <TableCell className="amount">{each.calls.toLocaleString('en-US')}</TableCell>
            <TableCell className="amount">{each.input_tokens.toLocaleString('en-US')}</TableCell>
            <TableCell className="amount">{each.output_tokens.toLocaleString('en-US')}</TableCell>
            <TableCell className="amount">
              {each.cost_usd === null ? (
                <NotAvailable>Not known</NotAvailable>
              ) : (
                formatDollars(each.cost_usd)
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function Figure({
  label,
  value,
  notYet = 'Not yet known',
}: {
  label: string
  value: ReactNode | null
  notYet?: string
}) {
  return (
    <div className="flex flex-col gap-1 rounded-lg border bg-background/60 px-3 py-2.5">
      <dt className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {label}
      </dt>
      {value === null ? (
        <dd className="not-available m-0 text-sm text-muted-foreground">{notYet}</dd>
      ) : (
        <dd className="m-0 text-lg font-semibold tabular">{value}</dd>
      )}
    </div>
  )
}

function whyNotFinished(run: RunView, runnerUnreachable: boolean): string | null {
  if (run.state === 'stopped') {
    return run.problem
      ? `It stopped before finishing: ${run.problem}`
      : 'It stopped before finishing: the service performing it stopped while it ran.'
  }
  if (run.state === 'unfinished' && runnerUnreachable && run.started_by !== 'command_line') {
    return 'It has not finished. The runner cannot be reached, so whether it is still running cannot be told.'
  }
  if (run.state === 'unfinished') {
    return 'It has not finished. It was started outside the dashboard, which cannot tell whether it is still running there or stopped.'
  }
  if (run.problem) return `It finished, then reported a problem: ${run.problem}`
  return null
}

const STATE_ICONS: Partial<Record<RunView['state'], LucideIcon>> = {
  running: LoaderCircleIcon,
}

function RunCard({
  run,
  runnerUnreachable,
  canStart,
  onRunAgain,
}: {
  run: RunView
  runnerUnreachable: boolean
  canStart: boolean
  onRunAgain: (sourceAccount: string) => void
}) {
  const id = useId()
  const why = whyNotFinished(run, runnerUnreachable)
  const failed = run.source_accounts.filter((each) => !each.read)
  const read = run.source_accounts.filter((each) => each.read)
  const StateIcon = STATE_ICONS[run.state]
  return (
    <article
      className={cn(
        'flex flex-col gap-4 rounded-xl border border-l-4 bg-card p-5 shadow-xs',
        run.state === 'finished' && 'border-l-success',
        run.state === 'running' && 'border-l-info',
        (run.state === 'stopped' || run.state === 'unfinished') && 'border-l-warning',
      )}
      aria-labelledby={id}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h3 id={id} className="text-base font-semibold">
            Run started {formatMoment(run.started_at)}
          </h3>
          <ul className="m-0 flex list-none flex-wrap gap-x-4 gap-y-1 p-0 text-sm text-muted-foreground">
            <li>{howStarted(run.started_by, run.person)}</li>
            {run.only_source_account && <li>Read {run.only_source_account} again, and no other</li>}
            {run.finished_at && <li>Finished {formatMoment(run.finished_at)}</li>}
          </ul>
        </div>
        <Badge variant={STATE_VARIANTS[run.state]} className={`tag tag-run-${run.state}`}>
          {StateIcon && <StateIcon aria-hidden="true" className="animate-spin" />}
          {STATES[run.state]}
        </Badge>
      </div>
      {why &&
        (run.state === 'unfinished' ? (
          <Hint>{why}</Hint>
        ) : (
          <Problem tone={run.state === 'stopped' ? 'warning' : 'destructive'}>{why}</Problem>
        ))}
      <dl className="m-0 grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-7">
        <Figure label="Emails found" value={run.emails_found} />
        <Figure label="Collected" value={run.collected} />
        <Figure label="Needs review" value={run.needs_review} />
        <Figure label="Skipped" value={run.skipped} />
        <Figure label="Failed" value={run.failed} />
        <Figure
          label="Duration"
          value={run.duration_seconds === null ? null : formatDuration(run.duration_seconds)}
        />
        <Figure
          label="Model cost"
          value={run.model_cost_usd !== null ? formatDollars(run.model_cost_usd) : null}
          notYet={run.models.length > 0 ? 'Unknown' : 'Not recorded'}
        />
      </dl>
      {run.model_cost_usd !== null && run.models.length === 0 && (
        <Hint className="flex items-center gap-1.5">
          <CpuIcon aria-hidden="true" className="size-3.5" />
          No model was called.
        </Hint>
      )}
      {run.models.length > 0 && (
        <div className="overflow-x-auto">
          <ModelCalls models={run.models} />
        </div>
      )}
      {failed.length > 0 && (
        <ul
          className="m-0 flex list-none flex-col gap-2 p-0"
          aria-label="Source accounts not read"
        >
          {failed.map((each) => (
            <li
              key={each.source_account}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-destructive/30 bg-destructive-soft px-3 py-2 text-sm text-destructive"
            >
              <span>
                Could not read {each.source_account}: {each.reason ?? 'no reason was recorded'}
              </span>
              {each.can_run_again && (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!canStart}
                  onClick={() => onRunAgain(each.source_account)}
                >
                  <RefreshCwIcon />
                  Run {each.source_account} again
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {read.length > 0 && <Hint>Read: {read.map((each) => each.source_account).join(', ')}</Hint>}
    </article>
  )
}
