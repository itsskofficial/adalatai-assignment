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
  /** Identifies the billing document, for its history. */
  content_hash?: string | null
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

export type Role = 'member' | 'administrator'

/** The person signed in, and what they may do. */
export type Person = { email: string; role: Role }

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

/** Whether a source account's stored sign-in works. */
export type SignInState = 'works' | 'expired' | 'missing' | 'unknown'

export type SourceAccount = {
  address: string
  is_owner: boolean
  connected_by: string
  connected_at: string
  sign_in: SignInState
  sign_in_problem: string | null
  /** When the sign-in stops working, when that can be known. */
  sign_in_ends_at: string | null
  expiring_soon: boolean
  /** The owner account's sign-in does not yet reach the Drive files the tool creates. */
  needs_drive_access: boolean
  /** The latest collection month in which it was read. */
  last_read_month: string | null
  latest_run: {
    month: string
    read: boolean
    reason: string | null
    billing_documents: number
  } | null
}

export type SourceAccountList = {
  source_accounts: SourceAccount[]
  /** Signed in from the command line on this machine, but not connected. */
  found_on_this_machine: string[]
  sign_in_lifetime_days: number | null
}

/** How the latest connection through Google ended. */
export type ConnectionResult = {
  outcome: 'connected' | 'renewed' | 'wrong_address' | 'failed' | null
  address: string | null
  signed_in_address: string | null
  reason: string | null
}

export type OwnerChanged = { address: string; needs_renewal: boolean; message: string }

export type SourceAccountChange = {
  address: string
  action: 'connected' | 'renewed' | 'added' | 'removed' | 'made_owner'
  person: string
  changed_at: string
}

/** The API refused a change to the source accounts, and said why in plain words. */
export class SourceAccountChangeRefused extends Error {
  constructor(detail: string) {
    super(detail)
    this.name = 'SourceAccountChangeRefused'
  }
}

function sourceAccountPath(address: string): string {
  return `/api/source-accounts/${encodeURIComponent(address)}`
}

async function changeSourceAccounts(path: string, method: string, body?: unknown): Promise<unknown> {
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
  if ([404, 409, 422, 503].includes(response.status)) {
    const answer = (await response.json().catch(() => ({}))) as { detail?: unknown }
    if (typeof answer.detail === 'string') throw new SourceAccountChangeRefused(answer.detail)
  }
  if (!response.ok) throw new ApiUnavailable(`${path} answered ${response.status}`)
  return response.status === 204 ? null : ((await response.json()) as unknown)
}

export function sourceAccounts(signal?: AbortSignal): Promise<SourceAccountList> {
  return get<SourceAccountList>('/api/source-accounts', signal)
}

export function connectionResult(signal?: AbortSignal): Promise<ConnectionResult> {
  return get<ConnectionResult>('/api/source-accounts/connection-result', signal)
}

export function sourceAccountHistory(signal?: AbortSignal): Promise<SourceAccountChange[]> {
  return get<SourceAccountChange[]>('/api/source-accounts/history', signal)
}

/** Starts connecting a source account; answers with where to send the person at Google. */
export async function connectSourceAccount(address: string, owner: boolean): Promise<string> {
  const answer = (await changeSourceAccounts('/api/source-accounts/connect', 'POST', {
    address,
    owner,
  })) as { authorization_url: string }
  return answer.authorization_url
}

/** Starts renewing a source account's sign-in; answers with where to send the person. */
export async function renewSourceAccount(address: string): Promise<string> {
  const answer = (await changeSourceAccounts(`${sourceAccountPath(address)}/renew`, 'POST')) as {
    authorization_url: string
  }
  return answer.authorization_url
}

export async function removeSourceAccount(address: string): Promise<void> {
  await changeSourceAccounts(sourceAccountPath(address), 'DELETE')
}

export async function makeOwnerAccount(address: string): Promise<OwnerChanged> {
  return (await changeSourceAccounts(
    `${sourceAccountPath(address)}/make-owner`,
    'POST',
  )) as OwnerChanged
}

/** Connects a source account already signed in from the command line on this machine. */
export async function addFoundSourceAccount(address: string): Promise<void> {
  await changeSourceAccounts(`${sourceAccountPath(address)}/add`, 'POST')
}

export type Field = 'vendor' | 'invoice_date' | 'total' | 'currency' | 'document_type'

/** A reason not to trust what was read. The field is the one to look at, if one is. */
export type Doubt = { field: Field | null; reason: string }

/** The fields of a billing document that a person confirms. */
export type DocumentFields = {
  vendor: string
  invoice_date: string
  total: string
  currency: string
  document_type: DocumentType
}

/** A billing document held for a person to confirm. */
export type HeldDocument = DocumentFields & {
  content_hash: string
  doubts: Doubt[]
  /** Whether a stronger model read the document after the first reading was doubted. */
  read_again: boolean
  /** What read it last: a model's name, or 'rules' when no model could. Null when unknown. */
  read_by: string | null
  file_name: string
  file_url: string
  usual_amount: string | null
  usual_currency: string | null
  /** Every source account the document was found in. */
  source_accounts: string[]
}

/** An email that needs review, with its held billing documents. */
export type ReviewItem = {
  source_account: string
  message_id: string
  sender: string
  subject: string
  received_at: string
  invoice_format: 'attachment' | 'body' | 'portal_link' | null
  portal_link: string | null
  reason: string | null
  /** A login-gated portal link: a person downloads the PDF, and it cannot be approved here. */
  needs_manual_download: boolean
  documents: HeldDocument[]
  /** Every email the item stands for, when the same documents came to several accounts. */
  message_ids: string[]
}

export type ReviewQueue = { month: string; items: ReviewItem[] }

export type FieldProblem = { content_hash: string | null; field: string | null; reason: string }

/** The API refused a decision on a held email, and said why. Nothing was changed. */
export class ReviewRefused extends Error {
  readonly problems: FieldProblem[]

  constructor(detail: string, problems: FieldProblem[] = []) {
    super(detail)
    this.name = 'ReviewRefused'
    this.problems = problems
  }
}

export function reviewQueue(month: string, signal?: AbortSignal): Promise<ReviewQueue> {
  return get<ReviewQueue>(`/api/months/${month}/review`, signal)
}

function reviewPath(month: string, item: ReviewItem, action: 'approve' | 'reject'): string {
  const account = encodeURIComponent(item.source_account)
  return `/api/months/${month}/review/${account}/${encodeURIComponent(item.message_id)}/${action}`
}

/**
 * Sends a decision on a held email. Answers with what could not be tidied up afterwards,
 * such as a pending copy left in the archive; the decision stands all the same.
 */
async function decide(path: string, body?: unknown): Promise<string[]> {
  let response: Response
  try {
    response = await fetch(path, {
      method: 'POST',
      credentials: 'include',
      ...(body === undefined
        ? {}
        : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
    })
  } catch (problem) {
    throw new ApiUnavailable(problem instanceof Error ? problem.message : String(problem))
  }
  if (response.status === 401) throw new NotSignedIn()
  // 502: the archive could not be reached, and nothing was changed.
  if ([404, 409, 422, 502].includes(response.status)) {
    const answer = (await response.json().catch(() => ({}))) as { detail?: unknown }
    const detail = answer.detail
    if (typeof detail === 'string') throw new ReviewRefused(detail)
    if (detail !== null && typeof detail === 'object' && 'problems' in detail) {
      const refused = detail as { message?: unknown; problems: FieldProblem[] }
      throw new ReviewRefused(
        typeof refused.message === 'string' ? refused.message : 'Nothing was changed.',
        refused.problems,
      )
    }
  }
  if (!response.ok) throw new ApiUnavailable(`${path} answered ${response.status}`)
  const decision = (await response.json().catch(() => ({}))) as { warnings?: unknown }
  return Array.isArray(decision.warnings)
    ? decision.warnings.filter((warning): warning is string => typeof warning === 'string')
    : []
}

/** Approves every held billing document of the email, with the fields as confirmed. */
export function approveItem(
  month: string,
  item: ReviewItem,
  documents: (DocumentFields & { content_hash: string })[],
): Promise<string[]> {
  return decide(reviewPath(month, item, 'approve'), { documents })
}

/** Records that the email holds no billing document. Its pending PDF is deleted. */
export function rejectItem(month: string, item: ReviewItem): Promise<string[]> {
  return decide(reviewPath(month, item, 'reject'))
}

export async function signOut(): Promise<void> {
  await call('/auth/logout', { method: 'POST' })
}

/** Someone who may sign in to the dashboard. */
export type PersonOnList = {
  address: string
  role: Role
  /** Named in the INVOICE_COLLECTOR_ALLOWLIST setting: always an administrator, never changed here. */
  set_by_installation: boolean
  added_by: string | null
  added_at: string | null
  last_signed_in_at: string | null
}

/** A sign-in refused because the address may not sign in. */
export type RefusedSignIn = { address: string; attempted_at: string }

/** The API refused a change to the people list, and said why in plain words. */
export class PeopleChangeRefused extends Error {
  constructor(detail: string) {
    super(detail)
    this.name = 'PeopleChangeRefused'
  }
}

async function changePeople(path: string, method: string, body?: unknown): Promise<void> {
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
  if ([403, 404, 409, 422].includes(response.status)) {
    const answer = (await response.json().catch(() => ({}))) as { detail?: unknown }
    if (typeof answer.detail === 'string') throw new PeopleChangeRefused(answer.detail)
  }
  if (!response.ok) throw new ApiUnavailable(`${path} answered ${response.status}`)
}

function personPath(address: string): string {
  return `/api/people/${encodeURIComponent(address)}`
}

export function peopleList(signal?: AbortSignal): Promise<PersonOnList[]> {
  return get<PersonOnList[]>('/api/people', signal)
}

export function refusedSignIns(signal?: AbortSignal): Promise<RefusedSignIn[]> {
  return get<RefusedSignIn[]>('/api/people/refused', signal)
}

export function addPerson(address: string, role: Role): Promise<void> {
  return changePeople('/api/people', 'POST', { address, role })
}

export function changeRole(address: string, role: Role): Promise<void> {
  return changePeople(personPath(address), 'PUT', { role })
}

export function removePerson(address: string): Promise<void> {
  return changePeople(personPath(address), 'DELETE')
}

export const SIGN_IN_PATH = '/auth/login'

/** One step in the history of a billing document. */
export type TrailEntry = {
  /** Open: a kind the dashboard does not know is shown by its name and details. */
  kind: string
  /** Null for a step recorded before the history was kept, whose time is not known. */
  at: string | null
  source_account: string | null
  /** Who or what did it: a model's name, "rules", a person's address, or "run". */
  actor: string | null
  details: Record<string, unknown>
}

/** An email a billing document was found in: facts about it, never its body. */
export type TrailEmail = {
  source_account: string
  message_id: string
  sender: string
  subject: string
  received_at: string
  collection_month: string
  state: 'collected' | 'needs_review' | 'skipped' | 'failed'
  reason: string | null
}

/** The history of one billing document, in order of time. */
export type DocumentTrail = {
  content_hash: string
  fields: DocumentFields | null
  state: 'collected' | 'needs_review' | 'rejected' | 'not_collected' | string
  collection_month: string | null
  invoice_format: 'attachment' | 'body' | 'portal_link' | null
  source_accounts: string[]
  file_name: string | null
  file_url: string | null
  emails: TrailEmail[]
  entries: TrailEntry[]
  /** Read before the history was kept, so some steps are missing. */
  recorded_before_trail: boolean
}

/** The ledger holds no billing document with the identity asked for. */
export class NoSuchDocument extends Error {
  constructor() {
    super('The ledger holds no billing document with this identity.')
    this.name = 'NoSuchDocument'
  }
}

export async function documentTrail(
  contentHash: string,
  signal?: AbortSignal,
): Promise<DocumentTrail> {
  const path = `/api/billing-documents/${encodeURIComponent(contentHash)}/trail`
  let response: Response
  try {
    response = await fetch(path, { credentials: 'include', signal })
  } catch (problem) {
    throw new ApiUnavailable(problem instanceof Error ? problem.message : String(problem))
  }
  if (response.status === 401) throw new NotSignedIn()
  // 422: not the form of a content hash, so no document can have it.
  if (response.status === 404 || response.status === 422) throw new NoSuchDocument()
  if (!response.ok) throw new ApiUnavailable(`${path} answered ${response.status}`)
  return (await response.json()) as DocumentTrail
}

/** What became of a PDF uploaded for an email whose portal link needs a sign-in. */
export type UploadOutcome = 'collected' | 'held' | 'already_collected'

export type AssistedDownload = {
  outcome: UploadOutcome
  /** The collection month it was filed or held under: the month of its invoice date. */
  collection_month: string
  source_account: string
  message_id: string
  subject: string
  portal_link: string | null
  file_name: string
  size: number
  document: DocumentFields & { doubts: Doubt[] }
  person: string
  uploaded_at: string
}

/** The API refused an upload, and said why in plain words. Nothing was changed. */
export class UploadRefused extends Error {
  constructor(detail: string) {
    super(detail)
    this.name = 'UploadRefused'
  }
}

/** Hands the tool a PDF downloaded by hand from the email's portal link. */
export async function uploadDownload(
  month: string,
  item: ReviewItem,
  file: Blob,
): Promise<AssistedDownload> {
  const account = encodeURIComponent(item.source_account)
  const path = `/api/months/${month}/review/${account}/${encodeURIComponent(item.message_id)}/upload`
  let response: Response
  try {
    response = await fetch(path, {
      method: 'POST',
      credentials: 'include',
      // The PDF is sent as it is. The server judges it by its content, not by this type.
      headers: { 'Content-Type': 'application/pdf' },
      body: file,
    })
  } catch (problem) {
    throw new ApiUnavailable(problem instanceof Error ? problem.message : String(problem))
  }
  if (response.status === 401) throw new NotSignedIn()
  if ([404, 409, 413, 422, 502, 503].includes(response.status)) {
    const answer = (await response.json().catch(() => ({}))) as { detail?: unknown }
    if (typeof answer.detail === 'string') throw new UploadRefused(answer.detail)
  }
  if (!response.ok) throw new ApiUnavailable(`${path} answered ${response.status}`)
  return (await response.json()) as AssistedDownload
}

/** Every upload filed or held under the month, newest first. */
export function assistedDownloads(month: string, signal?: AbortSignal): Promise<AssistedDownload[]> {
  return get<AssistedDownload[]>(`/api/months/${month}/review/uploads`, signal)
}
