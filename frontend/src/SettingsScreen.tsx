import { CalendarClockIcon, FolderIcon, HistoryIcon, LockIcon } from 'lucide-react'
import { useEffect, useId, useState, type FormEvent } from 'react'
import { NotSignedIn } from './api'
import { EmptyState } from './components/EmptyState'
import { LinesSkeleton, Loading } from './components/Loading'
import { Hint, Problem, Status } from './components/Notice'
import { PageHeader, Screen, Section } from './components/Screen'
import { Button } from './components/ui/button'
import { Input } from './components/ui/input'
import { Label } from './components/ui/label'
import { Switch } from './components/ui/switch'
import { formatDate, monthName } from './format'
import {
  changeSettings,
  collectionSettings,
  type CollectionSettings,
  type NextRun,
  type ScheduleSettings,
  type SettingChange,
} from './settingsApi'
import { useShell } from './shell'
import type { Loaded } from './useLoaded'

const LAST_DAY = 28
const NAMES: Record<string, string> = {
  'schedule.enabled': 'the schedule',
  'schedule.day': 'the day of the month',
  'schedule.time': 'the time of day',
  'schedule.time_zone': 'the time zone',
  'drive.folder': 'the Drive folder',
}

/** Every time zone the browser knows, to choose from; the API checks the one given. */
function timeZones(): string[] {
  try {
    return Intl.supportedValuesOf('timeZone')
  } catch {
    return []
  }
}

/** "5 Sep 2026 at 06:00", read as written, in the zone it was written in. */
function formatDue(iso: string): string {
  return `${formatDate(iso)} at ${iso.slice(11, 16)}`
}

function valueText(name: string, value: string | null): string {
  if (value === null) return 'not set'
  if (name === 'schedule.enabled') return value === 'on' ? 'on' : 'off'
  return value
}

function problemText(problem: unknown): string {
  return problem instanceof Error ? problem.message : String(problem)
}

export function SettingsScreen() {
  const { onSignedOut } = useShell()
  const [loaded, setLoaded] = useState<Loaded<CollectionSettings>>({ status: 'loading' })

  useEffect(() => {
    const abort = new AbortController()
    collectionSettings(abort.signal).then(
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
  }, [onSignedOut])

  return (
    <Screen>
      <PageHeader
        title="Settings"
        description="When the tool collects by itself, and where in the owner account's Drive it files. Keys, sign-in clients and other secrets are not set here: whoever deploys the tool sets them."
      />
      {loaded.status === 'loading' && (
        <Loading>
          <div className="max-w-3xl rounded-xl border bg-card p-4">
            <LinesSkeleton lines={5} />
          </div>
        </Loading>
      )}
      {loaded.status === 'problem' && <Problem>{loaded.message}</Problem>}
      {loaded.status === 'ready' && (
        <SettingsForm
          settings={loaded.value}
          onSaved={(value) => setLoaded({ status: 'ready', value })}
        />
      )}
    </Screen>
  )
}

function SettingsForm({
  settings,
  onSaved,
}: {
  settings: CollectionSettings
  onSaved: (settings: CollectionSettings) => void
}) {
  const { onSignedOut } = useShell()
  const [schedule, setSchedule] = useState<ScheduleSettings>(settings.schedule)
  const [folder, setFolder] = useState(settings.drive_folder)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState<string[] | null>(null)
  const [refused, setRefused] = useState<string | null>(null)
  const ids = {
    enabled: useId(),
    day: useId(),
    time: useId(),
    zone: useId(),
    zones: useId(),
    folder: useId(),
  }
  const locked = !settings.can_change || busy

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setSaved(null)
    setRefused(null)
    try {
      const answer = await changeSettings(schedule, folder)
      setSchedule(answer.schedule)
      setFolder(answer.drive_folder)
      onSaved(answer)
      setSaved(['The settings are saved.', ...(answer.runner_notice ? [answer.runner_notice] : [])])
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

  function changeSchedule(changes: Partial<ScheduleSettings>) {
    setSchedule((current) => ({ ...current, ...changes }))
  }

  return (
    <>
      {!settings.can_change && (
        <Status tone="muted">
          <LockIcon aria-hidden="true" className="mr-1.5 inline size-3.5 align-text-bottom" />
          Only an administrator can change these settings. You can see them.
        </Status>
      )}
      <form className="flex max-w-3xl flex-col gap-6" aria-label="Settings" onSubmit={submit}>
        <section
          className="flex flex-col gap-4 rounded-xl border bg-card p-5 shadow-xs"
          aria-labelledby={`${ids.enabled}-heading`}
        >
          <div className="flex flex-col gap-1">
            <h2 id={`${ids.enabled}-heading`} className="flex items-center gap-2 text-base font-semibold">
              <CalendarClockIcon aria-hidden="true" className="size-4 text-muted-foreground" />
              Schedule
            </h2>
            <Hint>
              On the chosen day of each month the runner collects the month that has just ended,
              since its invoices have arrived by then.
            </Hint>
          </div>
          <div className="flex items-center gap-3">
            <Switch
              id={ids.enabled}
              checked={schedule.enabled}
              disabled={locked}
              onCheckedChange={(enabled) => changeSchedule({ enabled })}
            />
            <Label htmlFor={ids.enabled} className="font-semibold">
              Collect on a schedule
            </Label>
          </div>
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={ids.day}>Day of the month</Label>
              <Input
                id={ids.day}
                type="number"
                min={1}
                max={LAST_DAY}
                required
                className="tabular"
                value={schedule.day}
                disabled={locked}
                onChange={(event) => changeSchedule({ day: Number(event.target.value) })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={ids.time}>Time of day</Label>
              <Input
                id={ids.time}
                type="time"
                required
                className="tabular"
                value={schedule.time}
                disabled={locked}
                onChange={(event) => changeSchedule({ time: event.target.value })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={ids.zone}>Time zone</Label>
              <Input
                id={ids.zone}
                type="text"
                required
                list={ids.zones}
                value={schedule.time_zone}
                disabled={locked}
                onChange={(event) => changeSchedule({ time_zone: event.target.value })}
              />
              <datalist id={ids.zones}>
                {timeZones().map((zone) => (
                  <option key={zone} value={zone}>
                    {zone}
                  </option>
                ))}
              </datalist>
            </div>
          </div>
          <Hint>The day runs from 1 to {LAST_DAY}, so it falls in every month.</Hint>
          <NextScheduledRun next={settings.next_run} timeZone={settings.schedule.time_zone} />
          {!settings.runner && (
            <Hint>
              No runner service is set up for this dashboard, so nothing runs on the schedule
              until one is started with this ledger.
            </Hint>
          )}
        </section>
        <section
          className="flex flex-col gap-4 rounded-xl border bg-card p-5 shadow-xs"
          aria-labelledby={`${ids.folder}-heading`}
        >
          <h2 id={`${ids.folder}-heading`} className="flex items-center gap-2 text-base font-semibold">
            <FolderIcon aria-hidden="true" className="size-4 text-muted-foreground" />
            Drive folder
          </h2>
          <div className="flex max-w-md flex-col gap-1.5">
            <Label htmlFor={ids.folder}>Folder in the owner account&apos;s Drive</Label>
            <Input
              id={ids.folder}
              type="text"
              required
              maxLength={100}
              value={folder}
              disabled={locked}
              onChange={(event) => setFolder(event.target.value)}
            />
          </div>
          <Hint>
            PDFs and summary sheets go into this folder, at the top of My Drive, one folder per
            month inside it. Changing it affects later runs and approvals; nothing already filed
            is moved.
          </Hint>
        </section>
        {settings.can_change && (
          <div className="flex justify-end">
            <Button type="submit" disabled={busy}>
              Save settings
            </Button>
          </div>
        )}
      </form>
      {saved && (
        <Status tone="success" className="max-w-3xl">
          <span className="flex flex-col gap-0.5">
            {saved.map((line) => (
              <span key={line}>{line}</span>
            ))}
          </span>
        </Status>
      )}
      {refused && <Problem className="max-w-3xl">{refused}</Problem>}
      <Changes changes={settings.changes} />
    </>
  )
}

function NextScheduledRun({ next, timeZone }: { next: NextRun | null; timeZone: string }) {
  if (next === null) {
    return (
      <p className="m-0 rounded-lg bg-muted/60 px-3 py-2 text-sm" aria-label="Next scheduled run">
        The schedule is off. Nothing runs by itself.
      </p>
    )
  }
  return (
    <p className="m-0 rounded-lg bg-info-soft px-3 py-2 text-sm" aria-label="Next scheduled run">
      Next run: <strong>{formatDue(next.due_at)}</strong> ({timeZone}), collecting{' '}
      <strong>{monthName(next.collection_month)}</strong>.
    </p>
  )
}

function Changes({ changes }: { changes: SettingChange[] }) {
  return (
    <Section aria-label="Changes to the settings" title="Changes" className="max-w-3xl">
      {changes.length === 0 ? (
        <EmptyState icon={HistoryIcon} compact>
          No setting has been changed yet. Each is at its default.
        </EmptyState>
      ) : (
        <ul className="m-0 flex list-none flex-col divide-y rounded-xl border bg-card px-4 shadow-xs">
          {changes.map((change, index) => (
            <li key={`${change.changed_at}-${change.name}-${index}`} className="py-2.5 text-sm">
              {change.person} changed {NAMES[change.name] ?? change.name} from{' '}
              <strong>{valueText(change.name, change.value_before)}</strong> to{' '}
              <strong>{valueText(change.name, change.value_after)}</strong>{' '}
              <small className="text-muted-foreground">on {formatDue(change.changed_at)} UTC</small>
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}
