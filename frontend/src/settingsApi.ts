// The Settings screen's part of the dashboard's API.

import { ApiUnavailable, NotSignedIn } from './api'

export type ScheduleSettings = {
  enabled: boolean
  /** 1 to 28, so it falls in every month. */
  day: number
  /** HH:MM, in the time zone. */
  time: string
  time_zone: string
}

export type NextRun = {
  /** When it is due, written in the schedule's time zone. */
  due_at: string
  collection_month: string
}

export type SettingChange = {
  name: string
  value_before: string | null
  value_after: string
  person: string
  changed_at: string
}

export type CollectionSettings = {
  schedule: ScheduleSettings
  drive_folder: string
  /** Null while the schedule is off. */
  next_run: NextRun | null
  /** Administrators may change them; members see them. */
  can_change: boolean
  /** Whether a runner service keeps the schedule for this dashboard. */
  runner: boolean
  changes: SettingChange[]
}

export type SettingsSaved = CollectionSettings & {
  /** What became of telling the runner, when the schedule changed. */
  runner_notice: string | null
}

/** The API refused the change, and said why in plain words. */
export class SettingsRefused extends Error {
  constructor(detail: string) {
    super(detail)
    this.name = 'SettingsRefused'
  }
}

const PATH = '/api/settings'

async function answered(init: RequestInit): Promise<Response> {
  let response: Response
  try {
    response = await fetch(PATH, { credentials: 'include', ...init })
  } catch (problem) {
    throw new ApiUnavailable(problem instanceof Error ? problem.message : String(problem))
  }
  if (response.status === 401) throw new NotSignedIn()
  if ([403, 422].includes(response.status)) {
    const answer = (await response.json().catch(() => ({}))) as { detail?: unknown }
    if (typeof answer.detail === 'string') throw new SettingsRefused(answer.detail)
    throw new SettingsRefused('The settings could not be kept as given.')
  }
  if (!response.ok) throw new ApiUnavailable(`${PATH} answered ${response.status}`)
  return response
}

export async function collectionSettings(signal?: AbortSignal): Promise<CollectionSettings> {
  const response = await answered({ signal })
  return (await response.json()) as CollectionSettings
}

export async function changeSettings(
  schedule: ScheduleSettings,
  driveFolder: string,
): Promise<SettingsSaved> {
  const response = await answered({
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schedule, drive_folder: driveFolder }),
  })
  return (await response.json()) as SettingsSaved
}
