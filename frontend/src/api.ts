// The dashboard's API, as the screens see it.

export type DocumentType = 'invoice' | 'receipt' | 'credit_note'
export type SignalKind = 'payment_failed' | 'renewal_reminder'

export type SummaryRow = {
  vendor: string
  document_type: DocumentType
  date: string
  amount: string
  currency: string
  /** Null when no exchange rate was found for the invoice date. */
  amount_inr: string | null
  inr_rate: string | null
  source_account: string
  file_name: string
  file_url: string
  notes: string
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

/** An expected vendor with no billing document in the collection month. */
export type Gap = {
  vendor: string
  /** Unknown when the source account it bills could not be read, so nothing can be said. */
  kind: 'missing' | 'unknown'
  source_account: string | null
  explanation: string | null
}

/** A charge that a billing signal says is coming. */
export type UpcomingCharge = { vendor: string; source_account: string; note: string }

export type FailedSourceAccount = { source_account: string; reason: string | null }

export type MonthSummary = {
  month: string
  rows: SummaryRow[]
  totals: Total[]
  /** The rupee amounts of the rows that have one, added up. */
  total_inr: string
  rows_without_rupees: number
  counts: { collected: number; needs_review: number; skipped: number; failed: number }
  needs_review: EmailNeedingReview[]
  skipped: EmailWithReason[]
  failed: EmailWithReason[]
  billing_signals: BillingSignal[]
  gaps: Gap[]
  upcoming: UpcomingCharge[]
  failed_source_accounts: FailedSourceAccount[]
}

export type Person = { email: string }

export type Spend = {
  from_month: string | null
  to_month: string | null
  months: { month: string; inr_total: string }[]
  vendors: { vendor: string; inr_total: string }[]
  source_accounts: { source_account: string; inr_total: string }[]
  shared_charges: number
  inr_total: string
  without_rupees: { charges: number; totals: Total[] }
  changes: {
    month: string
    previous_month: string
    vendors: VendorChange[]
  } | null
}

export type AnswerColumn = { key: string; label: string; kind: 'text' | 'amount' | 'count' }

export type DocumentBehind = {
  vendor: string
  document_type: DocumentType
  date: string
  amount: string
  currency: string
  amount_inr: string | null
  source_account: string
  file_name: string
  file_url: string
}

export type Answer = {
  question: string
  answered: boolean
  answer: string
  reason: string | null
  query: {
    name: string
    parameters: Record<string, string | number | null>
    description: string
  } | null
  columns: AnswerColumn[]
  rows: Record<string, string>[]
  documents: DocumentBehind[]
}

export type VendorChange = {
  vendor: string
  previous_inr: string
  current_inr: string
  change_inr: string
  change_percent: string | null
}

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

/** Ask your invoices is unavailable just now: no API key, or the model could not answer. */
export class QuestionsUnavailable extends Error {
  constructor(detail: string) {
    super(detail)
    this.name = 'QuestionsUnavailable'
  }
}

export async function askQuestion(question: string): Promise<Answer> {
  let response: Response
  try {
    response = await fetch('/api/questions', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    })
  } catch (problem) {
    throw new ApiUnavailable(problem instanceof Error ? problem.message : String(problem))
  }
  if (response.status === 401) throw new NotSignedIn()
  if (response.status === 503) {
    const body = (await response.json().catch(() => ({}))) as { detail?: unknown }
    throw new QuestionsUnavailable(
      typeof body.detail === 'string' ? body.detail : 'Ask your invoices is unavailable just now.',
    )
  }
  if (!response.ok) throw new ApiUnavailable(`/api/questions answered ${response.status}`)
  return (await response.json()) as Answer
}

/** Spend in rupees over the latest six collection months. */
export function spendInRupees(signal?: AbortSignal): Promise<Spend> {
  return get<Spend>('/api/spend', signal)
}

export type BillingCycle = 'monthly' | 'annual'
export type VendorStatus = 'expected' | 'suggested' | 'ignored'
export type GapKind = 'missing' | 'unknown'

/** What a person sets on a vendor list entry. */
export type VendorFields = {
  vendor: string
  source_account: string | null
  billing_cycle: BillingCycle
  renewal_month: number | null
  usual_amount: string | null
  currency: string | null
}

/** A vendor on the list, with what the ledger says about its billing. */
export type Vendor = VendorFields & {
  status: VendorStatus
  /** The latest six collection months in which it billed, oldest first. */
  months_billed: string[]
  latest_amount: string | null
  latest_currency: string | null
  /** A gap in the latest collection month that was run, for an expected vendor. */
  gap: GapKind | null
}

export type VendorList = {
  latest_month: string | null
  expected: Vendor[]
  suggested: Vendor[]
  ignored: Vendor[]
}

/** The API refused a change to the vendor list, and said why in plain words. */
export class VendorChangeRefused extends Error {
  constructor(detail: string) {
    super(detail)
    this.name = 'VendorChangeRefused'
  }
}

function vendorPath(vendor: string): string {
  // A name may hold slashes, spaces and other characters, so it is encoded whole.
  return `/api/vendors/${encodeURIComponent(vendor)}`
}

async function changeVendors(path: string, method: string, body?: unknown): Promise<void> {
  let response: Response
  try {
    response = await fetch(path, {
      method,
      credentials: 'include',
      ...(body === undefined
        ? {}
        : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
    })
  } catch (problem) {
    throw new ApiUnavailable(problem instanceof Error ? problem.message : String(problem))
  }
  if (response.status === 401) throw new NotSignedIn()
  if ([404, 409, 422].includes(response.status)) {
    const answer = (await response.json().catch(() => ({}))) as { detail?: unknown }
    if (typeof answer.detail === 'string') throw new VendorChangeRefused(answer.detail)
  }
  if (!response.ok) throw new ApiUnavailable(`${path} answered ${response.status}`)
}

export function vendorList(signal?: AbortSignal): Promise<VendorList> {
  return get<VendorList>('/api/vendors', signal)
}

export function addVendor(fields: VendorFields): Promise<void> {
  return changeVendors('/api/vendors', 'POST', fields)
}

export function editVendor(vendor: string, fields: VendorFields): Promise<void> {
  return changeVendors(vendorPath(vendor), 'PUT', fields)
}

export function removeVendor(vendor: string): Promise<void> {
  return changeVendors(vendorPath(vendor), 'DELETE')
}

/** Makes a suggested vendor expected, applying any changes given at the same time. */
export function acceptVendor(vendor: string, fields?: VendorFields): Promise<void> {
  return changeVendors(`${vendorPath(vendor)}/accept`, 'POST', fields)
}

export function ignoreVendor(vendor: string): Promise<void> {
  return changeVendors(`${vendorPath(vendor)}/ignore`, 'POST')
}

export function restoreVendor(vendor: string): Promise<void> {
  return changeVendors(`${vendorPath(vendor)}/restore`, 'POST')
}

export async function signOut(): Promise<void> {
  await call('/auth/logout', { method: 'POST' })
}

export const SIGN_IN_PATH = '/auth/login'
