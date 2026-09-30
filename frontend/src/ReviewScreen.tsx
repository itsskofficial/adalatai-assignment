import {
  CheckIcon,
  CircleCheckIcon,
  ExternalLinkIcon,
  FileSearchIcon,
  HistoryIcon,
  InboxIcon,
  UploadIcon,
  XIcon,
} from 'lucide-react'
import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
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
  type AssistedDownload,
  uploadDownload,
} from './api'
import { ConfirmDialog } from './components/ConfirmDialog'
import { EmptyState } from './components/EmptyState'
import { LinesSkeleton, Loading } from './components/Loading'
import { Hint, Problem, Status } from './components/Notice'
import { PageHeader, Screen } from './components/Screen'
import { Button } from './components/ui/button'
import { Input } from './components/ui/input'
import { Label } from './components/ui/label'
import { Select } from './components/ui/select'
import { Tabs, TabsList, TabsTrigger } from './components/ui/tabs'
import { formatAmount, formatDate, isCollectionMonth, isSafeLink, monthName } from './format'
import { cn } from './lib/utils'
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
type Decide = (item: ReviewItem, action: () => Promise<string[]>) => Promise<ReviewRefused | null>

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
      <Screen>
        <PageHeader
          title="Review"
          description="Held documents, each with its PDF and what was read from it, for a person to confirm."
        />
        {monthsLoading ? (
          <Loading>
            <LinesSkeleton lines={4} className="max-w-md" />
          </Loading>
        ) : (
          <EmptyState icon={InboxIcon}>No run has been recorded yet.</EmptyState>
        )}
      </Screen>
    )
  }
  if (!isCollectionMonth(month)) {
    return (
      <Screen>
        <PageHeader title="Review" />
        <Problem>{month} is not a collection month. Choose one from the list above.</Problem>
      </Screen>
    )
  }
  return <ReviewOfMonth key={month} month={month} />
}

function ReviewOfMonth({ month }: { month: string }) {
  const { onSignedOut } = useShell()
  const [search, setSearch] = useSearchParams()
  const [queue, setQueue] = useState<Loaded<ReviewQueue>>({ status: 'loading' })
  const [busy, setBusy] = useState(false)
  // What the last decision could not tidy up. The decision itself stands.
  const [warnings, setWarnings] = useState<string[]>([])
  // What became of the latest upload, said once the queue has been read again.
  const [uploaded, setUploaded] = useState<AssistedDownload | null>(null)
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
    setUploaded(null)
    setBusy(true)
    setWarnings([])
    try {
      setWarnings(await action())
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

  function afterUpload(result: AssistedDownload) {
    setWarnings(result.warnings)
    setUploaded(result)
    // A held upload waits in the queue like any other held document, so it is opened.
    if (result.outcome === 'held' && result.collection_month === month) {
      setSearch({ month, email: result.message_id }, { replace: true })
    }
    setReloads((count) => count + 1)
  }

  return (
    <main
      className={cn(
        'flex flex-1 flex-col lg:grid lg:h-[calc(100svh-3.5rem)] lg:min-h-0',
        selected === null
          ? 'lg:grid-cols-[20rem_minmax(0,1fr)]'
          : 'lg:grid-cols-[20rem_minmax(0,1fr)_24rem]',
      )}
    >
      <aside
        className="flex flex-col gap-3 border-b bg-card p-4 lg:overflow-y-auto lg:border-r lg:border-b-0"
        aria-label="Review queue"
      >
        <div className="flex flex-col gap-0.5">
          <h1 className="flex items-center gap-2 text-base font-semibold tracking-tight">
            Needs review{' '}
            <small className="rounded-full bg-muted px-2 py-0.5 text-xs font-medium tabular text-muted-foreground">
              {reviewable.length}
            </small>
          </h1>
          <Hint>{monthName(month)}</Hint>
        </div>
        {warnings.length > 0 && <Status tone="warning">{warnings.join(' ')}</Status>}
        {uploaded !== null && <UploadNotice month={month} result={uploaded} />}
        {queue.status === 'loading' && (
          <Loading>
            <LinesSkeleton lines={6} />
          </Loading>
        )}
        {queue.status === 'problem' && <Problem>{queue.message}</Problem>}
        {queue.status === 'ready' && reviewable.length === 0 && (
          <EmptyState icon={CircleCheckIcon} compact>
            Nothing needs review for {monthName(month)}.
          </EmptyState>
        )}
        {reviewable.length > 0 && (
          <ul className="m-0 flex list-none flex-col gap-1.5 p-0" aria-label="Emails needing review">
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
                    className={cn(
                      'grid w-full cursor-pointer grid-cols-[1fr_auto] gap-x-2 gap-y-0.5 rounded-lg border bg-card px-3 py-2.5 text-left text-sm transition-colors outline-none hover:bg-accent focus-visible:ring-[3px] focus-visible:ring-ring/40',
                      isSelected && 'is-selected border-primary bg-primary/5 ring-1 ring-primary',
                    )}
                    aria-current={isSelected ? 'true' : undefined}
                    onClick={() => choose(item)}
                    onKeyDown={move}
                  >
                    <strong className="truncate font-semibold">{first?.vendor}</strong>
                    <span className="number tabular whitespace-nowrap">
                      {first ? `${formatAmount(first.total)} ${first.currency}` : ''}
                    </span>
                    <small className="col-span-2 text-xs text-warning">{firstReason(item)}</small>
                    {item.documents.length > 1 && (
                      <small className="col-span-2 text-xs text-muted-foreground">
                        and {item.documents.length - 1} more in this email
                      </small>
                    )}
                  </button>
                </li>
              )
            })}
          </ul>
        )}
        {manual.length > 0 && (
          <ManualDownloads
            month={month}
            items={manual}
            onUploaded={afterUpload}
            onSignedOut={onSignedOut}
          />
        )}
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
      {selected === null && queue.status === 'ready' && (
        <section className="hidden items-center justify-center p-8 lg:flex" aria-label="Document">
          <EmptyState icon={FileSearchIcon} className="w-full max-w-md border-none bg-transparent">
            {manual.length > 0
              ? 'Every held document is decided. The emails on the left still need a download by hand.'
              : 'Every held document of this month is decided.'}
          </EmptyState>
        </section>
      )}
    </main>
  )
}

function UploadNotice({ month, result }: { month: string; result: AssistedDownload }) {
  const where =
    result.collection_month === month ? '' : ` under ${monthName(result.collection_month)}`
  const reason = result.document.doubts[0]?.reason
  const said =
    result.outcome === 'collected'
      ? `Filed ${result.file_name}${where} and added to the summary.`
      : result.outcome === 'held'
        ? `Held ${result.file_name} for review${where}: ${reason ?? 'a check raised a doubt'}.`
        : `Already collected as ${result.file_name}${where}. This email is now linked to it.`
  return <Status tone={result.outcome === 'held' ? 'warning' : 'success'}>{said}</Status>
}

function ManualDownloads({
  month,
  items,
  onUploaded,
  onSignedOut,
}: {
  month: string
  items: ReviewItem[]
  onUploaded: (result: AssistedDownload) => void
  onSignedOut: () => void
}) {
  return (
    <section className="mt-2 flex flex-col gap-2 border-t pt-4" aria-label="Manual download needed">
      <h2 className="flex items-center gap-2 text-sm font-semibold">
        Manual download needed{' '}
        <small className="rounded-full bg-muted px-2 py-0.5 text-xs font-medium tabular text-muted-foreground">
          {items.length}
        </small>
      </h2>
      <Hint className="text-xs">
        These portal links need a sign-in. Open each link, sign in, download the PDF, and upload it
        here. It is read, checked and filed like any other billing document.
      </Hint>
      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        {items.map((item) => (
          <ManualDownload
            key={`${item.source_account}/${item.message_id}`}
            month={month}
            item={item}
            onUploaded={onUploaded}
            onSignedOut={onSignedOut}
          />
        ))}
      </ul>
    </section>
  )
}

function ManualDownload({
  month,
  item,
  onUploaded,
  onSignedOut,
}: {
  month: string
  item: ReviewItem
  onUploaded: (result: AssistedDownload) => void
  onSignedOut: () => void
}) {
  const id = useId()
  const [file, setFile] = useState<File | null>(null)
  const [sending, setSending] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)

  async function send() {
    if (file === null) return
    setSending(true)
    setProblem(null)
    try {
      const result = await uploadDownload(month, item, file)
      onUploaded(result)
    } catch (refused) {
      setSending(false)
      if (refused instanceof NotSignedIn) {
        onSignedOut()
        return
      }
      // An UploadRefused says in plain words why nothing was changed.
      setProblem(refused instanceof Error ? refused.message : String(refused))
    }
  }

  return (
    <li className="flex flex-col gap-1.5 rounded-lg border border-dashed p-3 text-sm">
      <strong className="break-words">{item.sender}</strong>
      <span className="break-words">{item.subject}</span>
      <small className="text-xs text-muted-foreground">
        Arrived {formatDate(item.received_at)} in {item.source_account}
      </small>
      {item.portal_link && isSafeLink(item.portal_link) ? (
        <a
          href={item.portal_link}
          target="_blank"
          rel="noreferrer noopener"
          className="inline-flex items-center gap-1 text-primary underline-offset-4 hover:underline"
        >
          Open the portal link
          <ExternalLinkIcon aria-hidden="true" className="size-3.5" />
        </a>
      ) : (
        <small className="text-xs break-all text-muted-foreground">
          {item.portal_link ?? 'No portal link was found'}
        </small>
      )}
      <form
        className="mt-1 flex flex-col gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          void send()
        }}
      >
        <Label htmlFor={id} className="text-xs text-muted-foreground">
          PDF for {item.subject}
        </Label>
        <div className="flex flex-wrap items-center gap-2">
          <Input
            id={id}
            type="file"
            accept="application/pdf,.pdf"
            disabled={sending}
            className="h-auto flex-1 py-1.5 text-xs"
            onChange={(event) => {
              setProblem(null)
              setFile(event.target.files?.[0] ?? null)
            }}
          />
          <Button type="submit" size="sm" disabled={sending || file === null}>
            <UploadIcon />
            {sending ? 'Uploading…' : 'Upload'}
          </Button>
        </div>
      </form>
      {problem && <Problem>{problem}</Problem>}
    </li>
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
      <section
        className="flex min-h-[60vh] flex-col bg-muted/40 p-4 lg:min-h-0"
        aria-label="Document"
      >
        <DocumentFrame document={document} />
      </section>
      <aside
        className="flex flex-col gap-4 border-t bg-card p-4 lg:overflow-y-auto lg:border-t-0 lg:border-l"
        aria-label="Extracted fields"
      >
        <h2 className="text-base font-semibold tracking-tight">Extracted fields</h2>
        {item.documents.length > 1 && (
          <Tabs value={String(active)} onValueChange={(value) => setActive(Number(value))}>
            <TabsList aria-label="Billing documents in this email" className="h-auto">
              {item.documents.map((each, index) => (
                <TabsTrigger key={each.content_hash} value={String(index)} className="text-xs">
                  {each.vendor} {formatAmount(each.total)} {each.currency}
                  {problems.some((p) => p.content_hash === each.content_hash) && (
                    <span className="text-destructive"> (check)</span>
                  )}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        )}
        {document.doubts.length > 0 ? (
          <ul
            className="m-0 flex list-disc flex-col gap-1 rounded-lg border border-warning/30 bg-warning-soft py-2.5 pr-3 pl-7 text-sm text-warning"
            aria-label="Reasons for review"
          >
            {document.doubts.map((doubt) => (
              <li key={`${doubt.field}-${doubt.reason}`}>{sentence(doubt.reason)}</li>
            ))}
          </ul>
        ) : (
          <Hint>
            Nothing was doubted in this document. It waits with the other documents of the same
            email, and is approved with them.
          </Hint>
        )}
        {document.read_by === 'rules' && (
          <p role="note" className="m-0 rounded-lg border bg-muted/60 px-3 py-2 text-sm text-muted-foreground">
            No model read this document. Rules read it, because no model key is set or the model
            could not read it, and what rules read is never filed without a person. Check every
            field against the PDF before approving it.
          </p>
        )}
        {document.read_again && (
          <Hint>A stronger model read this document again after the first reading was doubted.</Hint>
        )}
        <form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault()
            void approve()
          }}
        >
          <div className="flex flex-col gap-3">
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
          </div>
          <dl className="m-0 flex flex-col gap-2.5 rounded-lg border bg-muted/40 p-3 text-xs">
            <div>
              <dt className="text-muted-foreground">Will be saved as</dt>
              <dd className="m-0 mt-0.5 font-mono break-all">{fileNameFor(fields)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Found in</dt>
              <dd className="m-0 mt-0.5 break-all">{document.source_accounts.join(', ')}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Source email</dt>
              <dd className="m-0 mt-0.5 break-words">
                {item.sender}
                <br />
                {item.subject}
                <br />
                <small className="text-muted-foreground">Arrived {formatDate(item.received_at)}</small>
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">History</dt>
              <dd className="m-0 mt-0.5">
                <Link
                  to={`/documents/${document.content_hash}?month=${encodeURIComponent(month)}`}
                  className="inline-flex items-center gap-1 text-primary underline-offset-4 hover:underline"
                >
                  <HistoryIcon aria-hidden="true" className="size-3.5" />
                  How this document was found, read and checked
                </Link>
              </dd>
            </div>
          </dl>
          {(notice || general.length > 0) && (
            <Problem>
              {notice && <p>{notice}</p>}
              {general.map((p) => (
                <p key={`${p.content_hash}-${p.reason}`}>{p.reason}</p>
              ))}
            </Problem>
          )}
          {item.documents.length > 1 && (
            <Hint>
              Approve confirms all {item.documents.length} billing documents of this email
              together.
            </Hint>
          )}
          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={busy}>
              <CheckIcon />
              Approve
            </Button>
            <Button type="button" variant="outline" disabled={busy} onClick={() => setConfirming(true)}>
              <XIcon />
              Not a billing document
            </Button>
          </div>
          <ConfirmDialog
            open={confirming}
            onOpenChange={setConfirming}
            title="Skip this email and delete its PDF?"
            description="The email is judged not a billing document, and its pending copy is removed. What was read from it stays in its history."
            confirmLabel="Yes, not a billing document"
            busy={busy}
            onConfirm={() => void reject()}
          />
        </form>
      </aside>
    </>
  )
}

/** The PDF of a held document. One filed to Google Drive is shown from its copy on this
 * machine, since Drive cannot be shown inside the page, with a link to open it in Drive. */
function DocumentFrame({ document }: { document: HeldDocument }) {
  const driveUrl =
    document.drive_url !== null && isSafeLink(document.drive_url) ? document.drive_url : null
  const inDrive = driveUrl && (
    <a
      href={driveUrl}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1 text-primary underline-offset-4 hover:underline"
    >
      Open in Google Drive
      <ExternalLinkIcon aria-hidden="true" className="size-3.5" />
    </a>
  )
  if (!isSafeLink(document.file_url)) {
    return (
      <EmptyState icon={FileSearchIcon} className="flex-1">
        The PDF of this document cannot be opened from here. {inDrive}
      </EmptyState>
    )
  }
  return (
    <>
      <object
        className="min-h-[60vh] w-full flex-1 rounded-xl border bg-white shadow-xs lg:min-h-0"
        data={document.file_url}
        type="application/pdf"
        title={`PDF of ${document.file_name}`}
      >
        <EmptyState icon={FileSearchIcon} className="h-full">
          This browser cannot show the PDF here.{' '}
          <a
            href={document.file_url}
            target="_blank"
            rel="noreferrer"
            className="text-primary underline-offset-4 hover:underline"
          >
            Open {document.file_name}
          </a>
          {driveUrl && driveUrl !== document.file_url && <> or {inDrive}</>}
        </EmptyState>
      </object>
      {driveUrl && <p className="m-0 mt-2 text-sm">{inDrive}</p>}
    </>
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
  const flagged = doubted
    ? 'is-flagged border-warning bg-warning-soft focus-visible:border-warning focus-visible:ring-warning/30'
    : undefined
  return (
    <div
      className={cn(
        'flex flex-col gap-1.5',
        doubted && 'is-flagged',
        problems.length > 0 && 'has-problem',
      )}
    >
      <Label htmlFor={id} className={cn(doubted && 'text-warning')}>
        {FIELD_LABELS[field]}
      </Label>
      {field === 'document_type' ? (
        <Select
          {...common}
          value={value}
          className={flagged}
          onChange={(event) => onChange(event.target.value)}
        >
          {Object.entries(DOCUMENT_TYPES).map(([type, name]) => (
            <option key={type} value={type}>
              {name}
            </option>
          ))}
        </Select>
      ) : (
        <Input
          {...common}
          value={value}
          className={cn(flagged, field === 'total' && 'tabular')}
          inputMode={field === 'total' ? 'decimal' : undefined}
          placeholder={field === 'invoice_date' ? 'YYYY-MM-DD' : undefined}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
      {doubted && (
        <small id={`${id}-doubted`} className="text-xs text-warning">
          Doubted: check this field
        </small>
      )}
      {usual !== null && (
        <small id={`${id}-usual`} className="text-xs text-muted-foreground">
          {usual}
        </small>
      )}
      {problems.length > 0 && (
        <small id={`${id}-problem`} className="text-xs text-destructive">
          {problems.map((p) => p.reason).join('. ')}
        </small>
      )}
    </div>
  )
}
