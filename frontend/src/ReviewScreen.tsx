import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { useSearchParams } from 'react-router'
import {
  approveItem,
  NotSignedIn,
  rejectItem,
  reviewQueue,
  ReviewRefused,
  type DocumentFields,
  type DocumentType,
  type Field,
  type FieldProblem,
  type HeldDocument,
  type ReviewItem,
  type ReviewQueue,
} from './api'
import { formatAmount, formatDate, isCollectionMonth, isSafeLink, monthName } from './format'
import { fileNameFor } from './naming'
import { useShell } from './shell'
import type { Loaded } from './useLoaded'

const FIELD_LABELS: Record<Field, string> = {
  vendor: 'Vendor',
  invoice_date: 'Invoice date',
  total: 'Total',
  currency: 'Currency',
  document_type: 'Document type',
}

const FIELDS = Object.keys(FIELD_LABELS) as Field[]

const DOCUMENT_TYPES: Record<DocumentType, string> = {
  invoice: 'Invoice',
  receipt: 'Receipt',
  credit_note: 'Credit note',
}

/** Makes one decision; answers with why it was refused, or null once it is made. */
type Decide = (item: ReviewItem, action: () => Promise<void>) => Promise<ReviewRefused | null>

function sentence(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}

function fieldsOf(document: HeldDocument): DocumentFields {
  return {
    vendor: document.vendor,
    invoice_date: document.invoice_date,
    total: document.total,
    currency: document.currency,
    document_type: document.document_type,
  }
}

function firstReason(item: ReviewItem): string {
  const doubt = item.documents.flatMap((document) => document.doubts)[0]
  return sentence(doubt?.reason ?? item.reason ?? 'Waiting for a person to confirm')
}

export function ReviewScreen() {
  const { month, monthsLoading } = useShell()

  if (month === null) {
    return (
      <main className="screen">
        <h1>Review</h1>
        <p className="empty">{monthsLoading ? 'Loading…' : 'No run has been recorded yet.'}</p>
      </main>
    )
  }
  if (!isCollectionMonth(month)) {
    return (
      <main className="screen">
        <h1>Review</h1>
        <p className="reasons" role="alert">
          {month} is not a collection month. Choose one from the list above.
        </p>
      </main>
    )
  }
  return <ReviewOfMonth key={month} month={month} />
}

function ReviewOfMonth({ month }: { month: string }) {
  const { onSignedOut } = useShell()
  const [search, setSearch] = useSearchParams()
  const [queue, setQueue] = useState<Loaded<ReviewQueue>>({ status: 'loading' })
  const [busy, setBusy] = useState(false)
  // Each decision counts up, and the queue is read again from the server after it.
  const [reloads, setReloads] = useState(0)
  const entries = useRef(new Map<string, HTMLButtonElement>())

  useEffect(() => {
    const abort = new AbortController()
    reviewQueue(month, abort.signal).then(
      (value) => {
        if (abort.signal.aborted) return
        setQueue({ status: 'ready', value })
        setBusy(false)
      },
      (problem: unknown) => {
        if (abort.signal.aborted) return
        setBusy(false)
        if (problem instanceof NotSignedIn) {
          onSignedOut()
          return
        }
        setQueue({
          status: 'problem',
          message: problem instanceof Error ? problem.message : String(problem),
        })
      },
    )
    return () => abort.abort()
  }, [month, reloads, onSignedOut])

  const items = queue.status === 'ready' ? queue.value.items : []
  const reviewable = items.filter((item) => !item.needs_manual_download)
  const manual = items.filter((item) => item.needs_manual_download)
  const wanted = search.get('email')
  const selected =
    reviewable.find((item) => wanted !== null && item.message_ids.includes(wanted)) ??
    reviewable[0] ??
    null

  function choose(item: ReviewItem | null) {
    setSearch(item ? { month, email: item.message_id } : { month }, { replace: true })
  }

  function move(event: KeyboardEvent) {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    event.preventDefault()
    if (selected === null) return
    const index = reviewable.indexOf(selected) + (event.key === 'ArrowDown' ? 1 : -1)
    const next = reviewable[index]
    if (next === undefined) return
    choose(next)
    entries.current.get(next.message_id)?.focus()
  }

  const decide: Decide = async (item, action) => {
    const index = reviewable.indexOf(item)
    const next = reviewable[index + 1] ?? reviewable[index - 1] ?? null
    setBusy(true)
    try {
      await action()
    } catch (problem) {
      setBusy(false)
      if (problem instanceof NotSignedIn) {
        onSignedOut()
        return null
      }
      if (problem instanceof ReviewRefused) return problem
      return new ReviewRefused(problem instanceof Error ? problem.message : String(problem))
    }
    // The next item is chosen now; the buttons stay disabled until the queue is read again.
    choose(next)
    setReloads((count) => count + 1)
    return null
  }

  return (
    <main className={`review ${selected === null ? 'is-empty' : ''}`}>
      <aside className="review-queue" aria-label="Review queue">
        <h1>
          Needs review <small>{reviewable.length}</small>
        </h1>
        <p className="hint">{monthName(month)}</p>
        {queue.status === 'loading' && <p className="empty">Loading…</p>}
        {queue.status === 'problem' && (
          <p className="reasons" role="alert">
            {queue.message}
          </p>
        )}
        {queue.status === 'ready' && reviewable.length === 0 && (
          <p className="empty">Nothing needs review for {monthName(month)}.</p>
        )}
        {reviewable.length > 0 && (
          <ul className="queue" aria-label="Emails needing review">
            {reviewable.map((item) => {
              const [first] = item.documents
              const isSelected = item === selected
              return (
                <li key={`${item.source_account}/${item.message_id}`}>
                  <button
                    type="button"
                    ref={(button) => {
                      if (button) entries.current.set(item.message_id, button)
                      else entries.current.delete(item.message_id)
                    }}
                    className={`queue-entry ${isSelected ? 'is-selected' : ''}`}
                    aria-current={isSelected ? 'true' : undefined}
                    onClick={() => choose(item)}
                    onKeyDown={move}
                  >
                    <strong>{first?.vendor}</strong>
                    <span className="number">
                      {first ? `${formatAmount(first.total)} ${first.currency}` : ''}
                    </span>
                    <small>{firstReason(item)}</small>
                    {item.documents.length > 1 && (
                      <small>and {item.documents.length - 1} more in this email</small>
                    )}
                  </button>
                </li>
              )
            })}
          </ul>
        )}
        {manual.length > 0 && <ManualDownloads items={manual} />}
      </aside>
      {selected !== null && (
        <ItemReview
          key={`${selected.source_account}/${selected.message_id}`}
          month={month}
          item={selected}
          busy={busy}
          decide={decide}
        />
      )}
    </main>
  )
}

function ManualDownloads({ items }: { items: ReviewItem[] }) {
  return (
    <section className="manual" aria-label="Manual download needed">
      <h2>
        Manual download needed <small>{items.length}</small>
      </h2>
      <p className="hint">
        These portal links need a sign-in. Download each PDF from the portal, then upload it on
        the Assisted Download screen, which is not built yet.
      </p>
      <ul>
        {items.map((item) => (
          <li key={`${item.source_account}/${item.message_id}`}>
            <strong>{item.subject}</strong>
            <small>{item.source_account}</small>
            {item.portal_link && isSafeLink(item.portal_link) ? (
              <a href={item.portal_link} target="_blank" rel="noreferrer">
                Open the portal link
              </a>
            ) : (
              <small>{item.portal_link ?? 'No portal link was found'}</small>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

function ItemReview({
  month,
  item,
  busy,
  decide,
}: {
  month: string
  item: ReviewItem
  busy: boolean
  decide: Decide
}) {
  const [edits, setEdits] = useState<Record<string, DocumentFields>>(() =>
    Object.fromEntries(item.documents.map((d) => [d.content_hash, fieldsOf(d)])),
  )
  const [active, setActive] = useState(0)
  const [problems, setProblems] = useState<FieldProblem[]>([])
  const [notice, setNotice] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const document = item.documents[active] ?? item.documents[0]
  if (document === undefined) return null
  const fields = edits[document.content_hash] ?? fieldsOf(document)

  function edit(field: Field, value: string) {
    if (document === undefined) return
    setEdits((current) => ({
      ...current,
      [document.content_hash]: { ...fields, [field]: value },
    }))
  }

  function refused(refusal: ReviewRefused) {
    setProblems(refusal.problems)
    setNotice(refusal.message)
    const at = item.documents.findIndex((d) =>
      refusal.problems.some((p) => p.content_hash === d.content_hash && p.field !== null),
    )
    if (at >= 0) setActive(at)
  }

  async function approve() {
    setProblems([])
    setNotice(null)
    const confirmed = item.documents.map((d) => ({
      content_hash: d.content_hash,
      ...(edits[d.content_hash] ?? fieldsOf(d)),
    }))
    const refusal = await decide(item, () => approveItem(month, item, confirmed))
    if (refusal) refused(refusal)
  }

  async function reject() {
    setProblems([])
    setNotice(null)
    const refusal = await decide(item, () => rejectItem(month, item))
    setConfirming(false)
    if (refusal) refused(refusal)
  }

  const general = problems.filter((p) => p.field === null)

  return (
    <>
      <section className="review-document" aria-label="Document">
        <DocumentFrame document={document} />
      </section>
      <aside className="review-fields" aria-label="Extracted fields">
        <h2>Extracted fields</h2>
        {item.documents.length > 1 && (
          <div className="tabs" role="tablist" aria-label="Billing documents in this email">
            {item.documents.map((each, index) => (
              <button
                key={each.content_hash}
                type="button"
                role="tab"
                aria-selected={index === active}
                className={index === active ? 'is-active' : undefined}
                onClick={() => setActive(index)}
              >
                {each.vendor} {formatAmount(each.total)} {each.currency}
                {problems.some((p) => p.content_hash === each.content_hash) && (
                  <span className="tab-problem"> (check)</span>
                )}
              </button>
            ))}
          </div>
        )}
        {document.doubts.length > 0 ? (
          <ul className="reasons" aria-label="Reasons for review">
            {document.doubts.map((doubt) => (
              <li key={`${doubt.field}-${doubt.reason}`}>{sentence(doubt.reason)}</li>
            ))}
          </ul>
        ) : (
          <p className="hint">
            Nothing was doubted in this document. It waits with the other documents of the same
            email, and is approved with them.
          </p>
        )}
        {document.read_again && (
          <p className="hint">
            A stronger model read this document again after the first reading was doubted.
          </p>
        )}
        <form
          className="review-form"
          onSubmit={(event) => {
            event.preventDefault()
            void approve()
          }}
        >
          {FIELDS.map((field) => (
            <FieldInput
              key={`${document.content_hash}-${field}`}
              field={field}
              value={fields[field]}
              doubted={document.doubts.some((d) => d.field === field)}
              problems={problems.filter(
                (p) => p.content_hash === document.content_hash && p.field === field,
              )}
              usual={
                field === 'total'
                  ? document.usual_amount !== null
                    ? `Usually ${formatAmount(document.usual_amount)} ${document.usual_currency ?? ''}`.trim()
                    : 'No usual amount known for this vendor'
                  : null
              }
              disabled={busy}
              onChange={(value) => edit(field, value)}
            />
          ))}
          <dl className="details">
            <dt>Will be saved as</dt>
            <dd className="file">{fileNameFor(fields)}</dd>
            <dt>Found in</dt>
            <dd>{document.source_accounts.join(', ')}</dd>
            <dt>Source email</dt>
            <dd>
              {item.sender}
              <br />
              {item.subject}
              <br />
              <small>Arrived {formatDate(item.received_at)}</small>
            </dd>
          </dl>
          {(notice || general.length > 0) && (
            <div className="reasons" role="alert">
              {notice && <p>{notice}</p>}
              {general.map((p) => (
                <p key={`${p.content_hash}-${p.reason}`}>{p.reason}</p>
              ))}
            </div>
          )}
          {item.documents.length > 1 && (
            <p className="hint">
              Approve confirms all {item.documents.length} billing documents of this email
              together.
            </p>
          )}
          {confirming ? (
            <fieldset className="confirm">
              <legend>Skip this email and delete its PDF?</legend>
              <button
                type="button"
                className="danger"
                disabled={busy}
                onClick={() => void reject()}
              >
                Yes, not a billing document
              </button>
              <button type="button" disabled={busy} onClick={() => setConfirming(false)}>
                Cancel
              </button>
            </fieldset>
          ) : (
            <div className="actions">
              <button type="submit" className="primary" disabled={busy}>
                Approve
              </button>
              <button type="button" disabled={busy} onClick={() => setConfirming(true)}>
                Not a billing document
              </button>
            </div>
          )}
        </form>
      </aside>
    </>
  )
}

function DocumentFrame({ document }: { document: HeldDocument }) {
  if (!isSafeLink(document.file_url)) {
    return <p className="empty">The PDF of this document cannot be opened from here.</p>
  }
  return (
    <object
      className="pdf-frame"
      data={document.file_url}
      type="application/pdf"
      title={`PDF of ${document.file_name}`}
    >
      <p className="empty">
        This browser cannot show the PDF here.{' '}
        <a href={document.file_url} target="_blank" rel="noreferrer">
          Open {document.file_name}
        </a>
      </p>
    </object>
  )
}

function FieldInput({
  field,
  value,
  doubted,
  problems,
  usual,
  disabled,
  onChange,
}: {
  field: Field
  value: string
  doubted: boolean
  problems: FieldProblem[]
  usual: string | null
  disabled: boolean
  onChange: (value: string) => void
}) {
  const id = useId()
  const described = [
    doubted ? `${id}-doubted` : null,
    usual !== null ? `${id}-usual` : null,
    problems.length > 0 ? `${id}-problem` : null,
  ]
    .filter((each) => each !== null)
    .join(' ')
  const common = {
    id,
    disabled,
    'aria-describedby': described || undefined,
    'aria-invalid': problems.length > 0 ? true : undefined,
  }
  return (
    <div
      className={`review-field ${doubted ? 'is-flagged' : ''} ${problems.length > 0 ? 'has-problem' : ''}`}
    >
      <label htmlFor={id}>{FIELD_LABELS[field]}</label>
      {field === 'document_type' ? (
        <select {...common} value={value} onChange={(event) => onChange(event.target.value)}>
          {Object.entries(DOCUMENT_TYPES).map(([type, name]) => (
            <option key={type} value={type}>
              {name}
            </option>
          ))}
        </select>
      ) : (
        <input
          {...common}
          value={value}
          inputMode={field === 'total' ? 'decimal' : undefined}
          placeholder={field === 'invoice_date' ? 'YYYY-MM-DD' : undefined}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
      {doubted && (
        <small id={`${id}-doubted`} className="doubted">
          Doubted: check this field
        </small>
      )}
      {usual !== null && <small id={`${id}-usual`}>{usual}</small>}
      {problems.length > 0 && (
        <small id={`${id}-problem`} className="field-problem">
          {problems.map((p) => p.reason).join('. ')}
        </small>
      )}
    </div>
  )
}
