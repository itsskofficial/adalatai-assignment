import { useEffect, useId, useState, type FormEvent } from 'react'
import { NotSignedIn } from './api'
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
    <main className="screen">
      <h1>Settings</h1>
      <p className="lede">
        When the tool collects by itself, and where in the owner account&apos;s Drive it files.
        Keys, sign-in clients and other secrets are not set here: whoever deploys the tool sets
        them.
      </p>
      {loaded.status === 'loading' && <p className="empty">Loading…</p>}
      {loaded.status === 'problem' && (
        <p className="reasons" role="alert">
          {loaded.message}
        </p>
      )}
      {loaded.status === 'ready' && (
        <SettingsForm
          settings={loaded.value}
          onSaved={(value) => setLoaded({ status: 'ready', value })}
        />
      )}
    </main>
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
        <p className="hint">Only an administrator can change these settings. You can see them.</p>
      )}
      <form className="settings" aria-label="Settings" onSubmit={submit}>
        <section className="panel" aria-labelledby={`${ids.enabled}-heading`}>
          <h2 id={`${ids.enabled}-heading`}>Schedule</h2>
          <p className="hint">
            On the chosen day of each month the runner collects the month that has just ended,
            since its invoices have arrived by then.
          </p>
          <label className="settings-switch" htmlFor={ids.enabled}>
            <input
              id={ids.enabled}
              type="checkbox"
              checked={schedule.enabled}
              disabled={locked}
              onChange={(event) => changeSchedule({ enabled: event.target.checked })}
            />
            Collect on a schedule
          </label>
          <div className="settings-fields">
            <label htmlFor={ids.day}>Day of the month</label>
            <input
              id={ids.day}
              type="number"
              min={1}
              max={LAST_DAY}
              required
              value={schedule.day}
              disabled={locked}
              onChange={(event) => changeSchedule({ day: Number(event.target.value) })}
            />
            <label htmlFor={ids.time}>Time of day</label>
            <input
              id={ids.time}
              type="time"
              required
              value={schedule.time}
              disabled={locked}
              onChange={(event) => changeSchedule({ time: event.target.value })}
            />
            <label htmlFor={ids.zone}>Time zone</label>
            <input
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
          <p className="hint">The day runs from 1 to {LAST_DAY}, so it falls in every month.</p>
          <NextScheduledRun next={settings.next_run} timeZone={settings.schedule.time_zone} />
          {!settings.runner && (
            <p className="hint">
              No runner service is set up for this dashboard, so nothing runs on the schedule
              until one is started with this ledger.
            </p>
          )}
        </section>
        <section className="panel" aria-labelledby={`${ids.folder}-heading`}>
          <h2 id={`${ids.folder}-heading`}>Drive folder</h2>
          <div className="settings-fields">
            <label htmlFor={ids.folder}>Folder in the owner account&apos;s Drive</label>
            <input
              id={ids.folder}
              type="text"
              required
              maxLength={100}
              value={folder}
              disabled={locked}
              onChange={(event) => setFolder(event.target.value)}
            />
          </div>
          <p className="hint">
            PDFs and summary sheets go into this folder, at the top of My Drive, one folder per
            month inside it. Changing it affects later runs and approvals; nothing already filed
            is moved.
          </p>
        </section>
        {settings.can_change && (
          <div className="actions">
            <button type="submit" className="primary" disabled={busy}>
              Save settings
            </button>
          </div>
        )}
      </form>
      {saved && (
        <output className="notice settings-saved">
          {saved.map((line) => (
            <span key={line}>{line}</span>
          ))}
        </output>
      )}
      {refused && (
        <p className="reasons" role="alert">
          {refused}
        </p>
      )}
      <Changes changes={settings.changes} />
    </>
  )
}

function NextScheduledRun({ next, timeZone }: { next: NextRun | null; timeZone: string }) {
  if (next === null) {
    return (
      <p className="settings-next" aria-label="Next scheduled run">
        The schedule is off. Nothing runs by itself.
      </p>
    )
  }
  return (
    <p className="settings-next" aria-label="Next scheduled run">
      Next run: <strong>{formatDue(next.due_at)}</strong> ({timeZone}), collecting{' '}
      <strong>{monthName(next.collection_month)}</strong>.
    </p>
  )
}

function Changes({ changes }: { changes: SettingChange[] }) {
  return (
    <section aria-label="Changes to the settings">
      <h2>Changes</h2>
      {changes.length === 0 ? (
        <p className="empty">No setting has been changed yet. Each is at its default.</p>
      ) : (
        <ul className="changes">
          {changes.map((change, index) => (
            <li key={`${change.changed_at}-${change.name}-${index}`}>
              {change.person} changed {NAMES[change.name] ?? change.name} from{' '}
              {valueText(change.name, change.value_before)} to{' '}
              {valueText(change.name, change.value_after)}{' '}
              <small>on {formatDue(change.changed_at)} UTC</small>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
