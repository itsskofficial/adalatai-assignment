import { useEffect, type ReactNode } from 'react'
import { Link, useParams } from 'react-router'
import { documentTrail, type DocumentTrail, type TrailEntry } from './api'
import { formatAmount, formatDate, formatMoment, isSafeLink, monthName } from './format'
import { useShell } from './shell'
import { useLoaded } from './useLoaded'

// Everything here is shown as text. Sender, subject and the reader's note come from email,
// which is not trusted (ADR 0012), so nothing is ever set as HTML.

const FIELD_LABELS: Record<string, string> = {
  vendor: 'Vendor',
  invoice_date: 'Invoice date',
  total: 'Total',
  currency: 'Currency',
  document_type: 'Document type',
}

const FIELD_ORDER = ['vendor', 'invoice_date', 'total', 'currency', 'document_type']

const KINDS_OF_EMAIL: Record<string, string> = {
  invoice: 'an invoice',
  receipt: 'a receipt',
  credit_note: 'a credit note',
  payment_failed: 'a payment-failed notice',
  renewal_reminder: 'a renewal reminder',
  not_billing: 'not about billing',
}

const DOCUMENT_TYPES: Record<string, string> = {
  invoice: 'Invoice',
  receipt: 'Receipt',
  credit_note: 'Credit note',
}

const FORMATS: Record<string, string> = {
  attachment: 'PDF attachment',
  body: 'Email body',
  portal_link: 'Portal link',
}

const UPLOAD_OUTCOMES: Record<string, string> = {
  collected: 'Filed and added to the summary.',
  held: 'Held for review.',
  already_collected: 'The same document was collected before, so this email was linked to it.',
}

const STATES: Record<string, string> = {
  collected: 'Collected',
  needs_review: 'Needs review',
  rejected: 'Judged not a billing document',
  not_collected: 'Not collected',
}

type Doubt = { field: string | null; reason: string }
type Check = { check: string; passed: boolean; doubts: Doubt[] }
type Change = { field: string; before: string; after: string }

function text(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value)
}

function list<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : []
}

/** A file's size in bytes, in the unit a person reads it in. */
function fileSize(value: unknown): string {
  if (typeof value !== 'number') return 'size not recorded'
  if (value < 1024) return `${value} bytes`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

function sentence(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1)
}

/** "left_for_month" becomes "Left for month": how a kind the dashboard does not know is named. */
function kindName(kind: string): string {
  return sentence(kind.replace(/_/g, ' '))
}

function label(field: string): string {
  return FIELD_LABELS[field] ?? kindName(field)
}

export function DocumentHistoryScreen() {
  const { contentHash = '' } = useParams()
  const { onSignedOut, month } = useShell()
  const trail = useLoaded(`trail-${contentHash}`, (signal) => documentTrail(contentHash, signal))

  const notSignedIn = trail.status === 'not-signed-in'
  useEffect(() => {
    if (notSignedIn) onSignedOut()
  }, [notSignedIn, onSignedOut])

  const query = month ? `?month=${encodeURIComponent(month)}` : ''
  return (
    <main className="screen document-history">
      <p className="hint">
        <Link to={`/summary${query}`}>Back to the summary</Link>
      </p>
      {trail.status === 'loading' && (
        <>
          <h1>History of a billing document</h1>
          <p className="empty">Loading…</p>
        </>
      )}
      {trail.status === 'problem' && (
        <>
          <h1>History of a billing document</h1>
          <p className="reasons" role="alert">
            {trail.message}
          </p>
        </>
      )}
      {trail.status === 'ready' && <History trail={trail.value} />}
    </main>
  )
}

function title(trail: DocumentTrail): string {
  const fields = trail.fields
  if (fields === null) return 'History of a billing document'
  const type = DOCUMENT_TYPES[fields.document_type] ?? fields.document_type
  const amount = `${formatAmount(fields.total)} ${fields.currency}`
  return `History of ${fields.vendor} ${type.toLowerCase()}, ${amount}`
}

function History({ trail }: { trail: DocumentTrail }) {
  return (
    <>
      <h1>{title(trail)}</h1>
      <section aria-label="About this document">
        <dl className="details trail-facts">
          <dt>State</dt>
          <dd>{STATES[trail.state] ?? kindName(trail.state)}</dd>
          {trail.collection_month && (
            <>
              <dt>Collection month</dt>
              <dd>{monthName(trail.collection_month)}</dd>
            </>
          )}
          <dt>Invoice format</dt>
          <dd>
            {trail.invoice_format
              ? (FORMATS[trail.invoice_format] ?? trail.invoice_format)
              : 'Not recorded'}
          </dd>
          <dt>Source accounts</dt>
          <dd>{trail.source_accounts.join(', ') || 'None recorded'}</dd>
          {trail.file_name && (
            <>
              <dt>File</dt>
              <dd className="file">
                {trail.file_url && isSafeLink(trail.file_url) ? (
                  <a href={trail.file_url} target="_blank" rel="noopener noreferrer">
                    {trail.file_name}
                  </a>
                ) : (
                  trail.file_name
                )}
              </dd>
            </>
          )}
        </dl>
      </section>
      {trail.recorded_before_trail && (
        <p className="hint" role="note">
          This document was read before its history was recorded, so some steps are missing and
          the time of those shown without one is not known.
        </p>
      )}
      <ol className="trail" aria-label="History">
        {trail.entries.map((entry, index) => (
          <Step key={`${index}-${entry.kind}`} entry={entry} />
        ))}
      </ol>
    </>
  )
}

function Step({ entry }: { entry: TrailEntry }) {
  const { heading, body } = describe(entry)
  return (
    <li className={`trail-step trail-${entry.kind.replace(/[^a-z_]/g, '')}`}>
      <p className="trail-when">
        {entry.at ? (
          <time dateTime={entry.at}>{formatMoment(entry.at)}</time>
        ) : (
          <span className="not-available">Time not recorded</span>
        )}
      </p>
      <h2 className="trail-heading">{heading}</h2>
      {entry.actor && entry.actor !== 'run' && <p className="trail-actor">By {entry.actor}</p>}
      {entry.source_account && <p className="trail-account">{entry.source_account}</p>}
      {body}
    </li>
  )
}

function Fields({ fields }: { fields: Record<string, unknown> }) {
  const names = [
    ...FIELD_ORDER.filter((f) => f in fields),
    ...Object.keys(fields).filter((f) => !FIELD_ORDER.includes(f)),
  ]
  return (
    <dl className="trail-fields">
      {names.map((name) => (
        <div key={name}>
          <dt>{label(name)}</dt>
          <dd>{text(fields[name])}</dd>
        </div>
      ))}
    </dl>
  )
}

function Doubts({ doubts }: { doubts: Doubt[] }) {
  return (
    <ul className="reasons">
      {doubts.map((doubt) => (
        <li key={`${doubt.field}-${doubt.reason}`}>{sentence(doubt.reason)}</li>
      ))}
    </ul>
  )
}

/** The heading and body of a step. A kind not named here is shown by its name and details. */
function describe(entry: TrailEntry): { heading: string; body: ReactNode } {
  const d = entry.details
  switch (entry.kind) {
    case 'received':
      return {
        heading: `Email received in ${entry.source_account ?? 'a source account'}`,
        body: (
          <dl className="trail-fields">
            <div>
              <dt>From</dt>
              <dd>{text(d.sender)}</dd>
            </div>
            <div>
              <dt>Subject</dt>
              <dd>{text(d.subject)}</dd>
            </div>
          </dl>
        ),
      }
    case 'classified': {
      const kind = text(d.kind)
      const probability =
        typeof d.probability === 'number' ? ` (${Math.round(d.probability * 100)}%)` : ''
      return {
        heading: `Classified as ${KINDS_OF_EMAIL[kind] ?? kind}`,
        body: (
          <p>
            Confidence {text(d.confidence)}
            {probability}
            {d.vendor ? `. Vendor: ${text(d.vendor)}` : ''}
          </p>
        ),
      }
    }
    case 'found': {
      const format = text(d.invoice_format)
      const where = d.attachment
        ? `Attached as ${text(d.attachment)}`
        : d.portal_host
          ? `Behind a portal link at ${text(d.portal_host)}`
          : null
      return {
        heading: `Billing document found: ${FORMATS[format] ?? format}`,
        body: where ? <p>{where}</p> : null,
      }
    }
    case 'read':
      return {
        heading: 'Read',
        body: (
          <>
            <Fields fields={(d.fields as Record<string, unknown>) ?? {}} />
            <p>
              The reader's confidence: {text(d.confidence)}
              {d.reader_note ? `. Its note: ${text(d.reader_note)}` : ''}
            </p>
          </>
        ),
      }
    case 'checked': {
      const checks = list<Check>(d.checks)
      return {
        heading:
          d.stage === 'history'
            ? "Checked against the vendor's history"
            : d.stage === 'reading'
              ? 'Checked the reading'
              : 'Checked',
        body:
          checks.length === 0 ? (
            <p>No check applied to this document.</p>
          ) : (
            <ul className="trail-checks">
              {checks.map((check) => (
                <li key={check.check} className={check.passed ? 'is-passed' : 'is-doubted'}>
                  {sentence(check.check)}: {check.passed ? 'passed' : 'doubted'}
                  {!check.passed && <Doubts doubts={check.doubts} />}
                </li>
              ))}
            </ul>
          ),
      }
    }
    case 'read_again': {
      const changes = list<Change>(d.changes)
      return {
        heading: 'Read again by the stronger model',
        body: (
          <>
            {changes.length === 0 ? (
              <p>It read every field the same.</p>
            ) : (
              <ul className="trail-changes">
                {changes.map((change) => (
                  <li key={change.field}>
                    {label(change.field)}: {change.before} → {change.after}
                  </li>
                ))}
              </ul>
            )}
            <p>The reader's confidence: {text(d.confidence)}</p>
          </>
        ),
      }
    }
    case 'read_again_failed':
      return {
        heading: 'The stronger model could not read it again',
        body: <p>{sentence(text(d.reason))}. The first reading stands.</p>,
      }
    case 'held': {
      const doubts = list<Doubt>(d.doubts)
      return {
        heading: 'Held for review',
        body:
          doubts.length > 0 ? (
            <Doubts doubts={doubts} />
          ) : (
            <p>Nothing was doubted in it. It waits with a doubted document of the same email.</p>
          ),
      }
    }
    case 'collected':
      return { heading: 'Collected', body: null }
    case 'left_for_month':
      return {
        heading: `Left for collection month ${text(d.month)}`,
        body: <p>Its invoice date falls in that month.</p>,
      }
    case 'filed': {
      const name = text(d.file_name)
      const link = typeof d.web_link === 'string' && isSafeLink(d.web_link) ? d.web_link : null
      return {
        heading: d.pending ? 'Filed in the pending folder' : 'Filed',
        body: (
          <p className="file">
            {link ? (
              <a href={link} target="_blank" rel="noopener noreferrer">
                {name}
              </a>
            ) : (
              name
            )}
          </p>
        ),
      }
    }
    case 'converted':
      return {
        heading: 'Converted to rupees',
        body:
          d.rate === null || d.rate === undefined ? (
            <p>
              No rate from {text(d.currency)} to rupees was known for{' '}
              {formatDate(text(d.rate_date))}.
            </p>
          ) : (
            <p>
              1 {text(d.currency)} = ₹{text(d.rate)}, the rate on {formatDate(text(d.rate_date))}
            </p>
          ),
      }
    case 'corrected':
      return {
        heading: `${label(text(d.field))} corrected`,
        body: (
          <p>
            <span className="trail-before">{text(d.before)}</span> →{' '}
            <span className="trail-after">{text(d.after)}</span>
          </p>
        ),
      }
    case 'approved': {
      const changed = list<string>(d.changed_fields)
      return {
        heading: 'Approved',
        body: (
          <p>
            {changed.length === 0
              ? 'Confirmed as read.'
              : `Confirmed with ${changed.map(label).join(', ').toLowerCase()} corrected.`}
          </p>
        ),
      }
    }
    case 'rejected':
      return { heading: 'Judged not a billing document', body: null }
    case 'matched':
      return {
        heading: `Matched to the expected vendor ${text(d.expected_vendor)}`,
        body: (
          <p>
            The document named the vendor {text(d.as_read)}. It is filed and summarised as{' '}
            {text(d.expected_vendor)}.
          </p>
        ),
      }
    case 'retried':
      return {
        heading: `Tried again, attempt ${text(d.attempt)}`,
        body: <p>The attempt before it failed: {text(d.after)}</p>,
      }
    case 'unopened':
      return {
        heading: 'The PDF could not be opened, so it was saved as it is',
        body: (
          <p>
            {sentence(text(d.problem))}. Nothing was read from it; a person enters its fields from
            the document.
          </p>
        ),
      }
    case 'pending_copy_removed':
      return {
        heading: 'Pending copy removed',
        body: <p className="file">{text(d.file_name)}</p>,
      }
    case 'pending_copy_not_removed':
      return {
        heading: 'Pending copy left in place',
        body: <p>{text(d.reason)}</p>,
      }
    case 'uploaded':
      return {
        heading: 'Uploaded by hand from the portal link',
        body: (
          <p>
            {text(d.file_name)}, {fileSize(d.size)}.{' '}
            {UPLOAD_OUTCOMES[text(d.outcome)] ?? text(d.outcome)}
          </p>
        ),
      }
    default:
      return {
        heading: kindName(entry.kind),
        body:
          Object.keys(d).length === 0 ? null : (
            <dl className="trail-fields">
              {Object.entries(d).map(([name, value]) => (
                <div key={name}>
                  <dt>{kindName(name)}</dt>
                  <dd>{text(value)}</dd>
                </div>
              ))}
            </dl>
          ),
      }
  }
}
