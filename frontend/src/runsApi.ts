// The Runs screen's part of the dashboard's API.

import { ApiUnavailable, NotSignedIn } from './api'

/** How often the Runs screen reads the runs again while one is going on. Tests shorten it. */
export const polling = { everyMs: 2000 }

export type StartedBy = 'schedule' | 'dashboard' | 'command_line'

/**
 * Running: this service is performing it now. Stopped: started here, and no longer
 * performed, so it will not finish. Unfinished: started elsewhere and not finished; the
 * dashboard cannot tell whether it still goes on there.
 */
export type RunState = 'running' | 'finished' | 'stopped' | 'unfinished'

export type SourceAccountRead = {
  source_account: string
  read: boolean
  /** Why it could not be read. */
  reason: string | null
  /** Its latest read for the month failed too, and it is connected. */
  can_run_again: boolean
}

/** The calls a run made to one model, the tokens they took, and what they cost. */
export type ModelCost = {
  model: string
  calls: number
  input_tokens: number
  output_tokens: number
  /** Null when the model's price is not known. */
  cost_usd: string | null
}

export type RunView = {
  id: number
  started_by: StartedBy
  /** Who started it, for a run started from the dashboard. */
  person: string | null
  /** The one source account it read again, when it was run for one account only. */
  only_source_account: string | null
  state: RunState
  started_at: string
  finished_at: string | null
  duration_seconds: number | null
  /** Null until the run finishes. */
  emails_found: number | null
  collected: number | null
  needs_review: number | null
  skipped: number | null
  failed: number | null
  /**
   * Null when not recorded, which is not the same as nothing, or, with models listed,
   * when the cost of one of them is not known.
   */
  model_cost_usd: string | null
  /** Each model the run called, when it metered its calls. */
  models: ModelCost[]
  source_accounts: SourceAccountRead[]
  /** Why it stopped, when it was started here and stopped with a reason. */
  problem: string | null
}

export type RunNotStarted = {
  person: string
  only_source_account: string | null
  requested_at: string
  problem: string | null
}

export type RunGoingOn = {
  person: string
  only_source_account: string | null
  requested_at: string
}

export type RunsOfMonth = {
  month: string
  running: RunGoingOn | null
  runs: RunView[]
  not_started: RunNotStarted[]
  connected_source_accounts: number
  /** Why a run of the month cannot be started just now, if it cannot. */
  cannot_start: string | null
}

/** The API refused to start a run, and said why in plain words. */
export class RunRefused extends Error {
  constructor(detail: string) {
    super(detail)
    this.name = 'RunRefused'
  }
}

function runsPath(month: string): string {
  return `/api/months/${encodeURIComponent(month)}/runs`
}

async function answered(path: string, init: RequestInit): Promise<Response> {
  let response: Response
  try {
    response = await fetch(path, { credentials: 'include', ...init })
  } catch (problem) {
    throw new ApiUnavailable(problem instanceof Error ? problem.message : String(problem))
  }
  if (response.status === 401) throw new NotSignedIn()
  if ([409, 422, 503].includes(response.status)) {
    const answer = (await response.json().catch(() => ({}))) as { detail?: unknown }
    if (typeof answer.detail === 'string') throw new RunRefused(answer.detail)
  }
  if (!response.ok) throw new ApiUnavailable(`${path} answered ${response.status}`)
  return response
}

export async function runsOfMonth(month: string, signal?: AbortSignal): Promise<RunsOfMonth> {
  const response = await answered(runsPath(month), { signal })
  return (await response.json()) as RunsOfMonth
}

/** Starts a run of the month, or of one failed source account; answers once it has started. */
export async function runAgain(month: string, sourceAccount: string | null): Promise<void> {
  await answered(runsPath(month), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_account: sourceAccount }),
  })
}
