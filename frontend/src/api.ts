// The dashboard's API, as the screens see it.

export type DocumentType = 'invoice' | 'receipt' | 'credit_note'
export type SignalKind = 'payment_failed' | 'renewal_reminder'

export type SummaryRow = {
  vendor: string
  document_type: DocumentType
  date: string
  amount: string
  currency: string
  source_account: string
  file_name: string
  file_url: string
}

export type Total = { currency: string; amount: string }

export type EmailWithReason = {
  source_account: string
  message_id: string
  subject: string
  reason: string | null
}

export type EmailNeedingReview = EmailWithReason & { portal_link: string | null }

export type BillingSignal = {
  kind: SignalKind
  vendor: string | null
  source_account: string
  message_id: string
  subject: string
  received_at: string
}

export type MonthSummary = {
  month: string
  rows: SummaryRow[]
  totals: Total[]
  counts: { collected: number; needs_review: number; skipped: number; failed: number }
  needs_review: EmailNeedingReview[]
  skipped: EmailWithReason[]
  failed: EmailWithReason[]
  billing_signals: BillingSignal[]
}

export type Person = { email: string }

/** The API answered 401: nobody is signed in, or the person is no longer on the allowlist. */
export class NotSignedIn extends Error {
  constructor() {
    super('Not signed in')
    this.name = 'NotSignedIn'
  }
}

export class ApiUnavailable extends Error {
  constructor(detail: string) {
    super(`The dashboard could not reach the API: ${detail}`)
    this.name = 'ApiUnavailable'
  }
}

async function call(path: string, init?: RequestInit): Promise<Response> {
  let response: Response
  try {
    response = await fetch(path, { credentials: 'include', ...init })
  } catch (problem) {
    throw new ApiUnavailable(problem instanceof Error ? problem.message : String(problem))
  }
  if (response.status === 401) throw new NotSignedIn()
  if (!response.ok) throw new ApiUnavailable(`${path} answered ${response.status}`)
  return response
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await call(path, { signal })
  return (await response.json()) as T
}

export function me(signal?: AbortSignal): Promise<Person> {
  return get<Person>('/api/me', signal)
}

export async function collectionMonths(signal?: AbortSignal): Promise<string[]> {
  return (await get<{ months: string[] }>('/api/months', signal)).months
}

export function monthSummary(month: string, signal?: AbortSignal): Promise<MonthSummary> {
  return get<MonthSummary>(`/api/months/${month}/summary`, signal)
}

export async function signOut(): Promise<void> {
  await call('/auth/logout', { method: 'POST' })
}

export const SIGN_IN_PATH = '/auth/login'
