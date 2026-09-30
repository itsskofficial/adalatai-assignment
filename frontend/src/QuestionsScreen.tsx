import { CircleHelpIcon, FileTextIcon, LoaderCircleIcon, SendIcon } from 'lucide-react'
import { useEffect, useId, useState, type FormEvent } from 'react'
import {
  askQuestion,
  NotSignedIn,
  type Answer,
  type AnswerColumn,
  type DocumentBehind,
  type DocumentType,
} from './api'
import { Money, NotAvailable, Rupees } from './components/Amount'
import { EmptyState } from './components/EmptyState'
import { Hint, Problem } from './components/Notice'
import { PageHeader, Screen } from './components/Screen'
import { Badge } from './components/ui/badge'
import { Button } from './components/ui/button'
import { Input } from './components/ui/input'
import { Label } from './components/ui/label'
import { Skeleton } from './components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from './components/ui/table'
import { formatDate, isSafeLink } from './format'
import { cn } from './lib/utils'
import { useShell } from './shell'

const EXAMPLES = [
  'How much did we spend on AWS last quarter?',
  'Which vendors cost the most last month?',
  'Which vendors are new this month?',
  'What were our five largest charges this year?',
]

const DOCUMENT_TYPES: Record<DocumentType, string> = {
  invoice: 'Invoice',
  receipt: 'Receipt',
  credit_note: 'Credit note',
}

type Entry =
  | { id: number; question: string; status: 'asking' }
  | { id: number; question: string; status: 'answered'; answer: Answer }
  | { id: number; question: string; status: 'problem'; message: string }

const STORED = 'questions-asked'

/** Questions asked in this browser session, kept while the person moves between screens. */
function storedEntries(): Entry[] {
  try {
    const stored = sessionStorage.getItem(STORED)
    const entries = stored ? (JSON.parse(stored) as Entry[]) : []
    return entries.filter((entry) => entry.status !== 'asking')
  } catch {
    return []
  }
}

function store(entries: Entry[]) {
  try {
    sessionStorage.setItem(STORED, JSON.stringify(entries))
  } catch {
    // Keeping the questions is a convenience; the screen works without it.
  }
}

export function QuestionsScreen() {
  const { onSignedOut } = useShell()
  const [draft, setDraft] = useState('')
  const [entries, setEntries] = useState<Entry[]>(storedEntries)
  const boxId = useId()
  // One question at a time: the box and the button wait until the answer is back.
  const asking = entries.some((entry) => entry.status === 'asking')

  useEffect(() => store(entries), [entries])

  async function submit(event: FormEvent) {
    event.preventDefault()
    const question = draft.trim()
    if (!question || asking) return
    const id = Date.now() + Math.random()
    setEntries((earlier) => [{ id, question, status: 'asking' }, ...earlier])
    setDraft('')
    let settled: Entry
    try {
      settled = { id, question, status: 'answered', answer: await askQuestion(question) }
    } catch (problem) {
      if (problem instanceof NotSignedIn) {
        onSignedOut()
        return
      }
      settled = {
        id,
        question,
        status: 'problem',
        message: problem instanceof Error ? problem.message : String(problem),
      }
    }
    setEntries((earlier) => earlier.map((entry) => (entry.id === id ? settled : entry)))
  }

  return (
    <Screen>
      <PageHeader
        title="Questions"
        description="Ask about spend by vendor, month, source account or period. Each answer comes from one of a fixed set of queries and lists the billing documents behind it."
      />
      <form
        className="flex flex-col gap-3 rounded-xl border bg-card p-4 shadow-xs"
        onSubmit={submit}
        aria-busy={asking || undefined}
      >
        <Label htmlFor={boxId}>Question</Label>
        <div className="flex gap-2">
          <Input
            id={boxId}
            type="text"
            value={draft}
            maxLength={500}
            disabled={asking}
            placeholder="How much did we spend on AWS last quarter?"
            onChange={(event) => setDraft(event.target.value)}
          />
          <Button type="submit" disabled={asking || !draft.trim()}>
            {asking ? <LoaderCircleIcon className="animate-spin" /> : <SendIcon />}
            Ask
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-muted-foreground">Try:</span>
          {EXAMPLES.map((example) => (
            <Button
              key={example}
              type="button"
              variant="outline"
              size="xs"
              className="rounded-full font-normal text-muted-foreground"
              disabled={asking}
              onClick={() => setDraft(example)}
            >
              {example}
            </Button>
          ))}
        </div>
      </form>
      {entries.length === 0 && (
        <EmptyState icon={CircleHelpIcon}>
          Nothing has been asked yet. Try one of the questions above.
        </EmptyState>
      )}
      <div className="flex flex-col gap-4">
        {entries.map((entry) => (
          <AskedQuestion key={entry.id} entry={entry} />
        ))}
      </div>
    </Screen>
  )
}

function AskedQuestion({ entry }: { entry: Entry }) {
  const id = useId()
  return (
    <article
      className={cn(
        'flex flex-col gap-3 rounded-xl border bg-card p-4 shadow-xs',
        entry.status === 'asking' && 'border-dashed',
      )}
      aria-labelledby={id}
    >
      <h2 id={id} className="text-sm font-medium text-muted-foreground">
        {entry.question}
      </h2>
      {entry.status === 'asking' && <Asking />}
      {entry.status === 'problem' && <Problem>{entry.message}</Problem>}
      {entry.status === 'answered' && <AnswerShown answer={entry.answer} />}
    </article>
  )
}

/** The answer's outline while the model picks a query and the server runs it. */
function Asking() {
  return (
    <div className="flex flex-col gap-3">
      <output className="flex items-center gap-2 text-sm text-muted-foreground">
        <LoaderCircleIcon aria-hidden="true" className="size-4 animate-spin" />
        Asking…
      </output>
      <div aria-busy="true" aria-hidden="true" className="contents">
        <Skeleton className="h-6 w-2/3 max-w-md" />
        <Skeleton className="h-3.5 w-1/3 max-w-xs" />
        <div className="flex flex-col gap-2 rounded-lg border p-3">
          <Skeleton className="h-3 w-full" />
          <Skeleton className="h-3 w-5/6" />
          <Skeleton className="h-3 w-2/3" />
        </div>
      </div>
    </div>
  )
}

function AnswerShown({ answer }: { answer: Answer }) {
  if (!answer.answered) {
    return (
      <div className="flex flex-col gap-1.5">
        <p className="m-0 text-base font-semibold text-warning">{answer.answer}</p>
        {answer.reason && <p className="m-0 text-sm">{answer.reason}</p>}
        <Hint>Questions like this are noted so the set of questions it can answer can grow.</Hint>
      </div>
    )
  }
  return (
    <>
      <p className="m-0 text-lg font-semibold tabular">{answer.answer}</p>
      {answer.query && (
        <Hint>
          Query used: <span className="font-medium text-foreground">{answer.query.description}</span>
        </Hint>
      )}
      {answer.rows.length > 0 && <ResultTable columns={answer.columns} rows={answer.rows} />}
      {answer.documents.length > 0 ? (
        <DocumentsTable documents={answer.documents} />
      ) : (
        <EmptyState icon={FileTextIcon} compact>
          No billing documents are behind this answer.
        </EmptyState>
      )}
    </>
  )
}

function Cell({ column, value }: { column: AnswerColumn; value: string | undefined }) {
  if (column.kind === 'amount') {
    return (
      <TableCell className="amount">
        {value ? <Rupees amount={value} /> : <NotAvailable>—</NotAvailable>}
      </TableCell>
    )
  }
  return <TableCell className={column.kind === 'count' ? 'amount' : undefined}>{value ?? ''}</TableCell>
}

function ResultTable({
  columns,
  rows,
}: {
  columns: AnswerColumn[]
  rows: Record<string, string>[]
}) {
  return (
    <Table aria-label="Result">
      <TableHeader>
        <TableRow>
          {columns.map((column) => (
            <TableHead
              key={column.key}
              scope="col"
              className={column.kind === 'text' ? undefined : 'amount'}
            >
              {column.label}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row, index) => (
          <TableRow key={index}>
            {columns.map((column) => (
              <Cell key={column.key} column={column} value={row[column.key]} />
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function DocumentsTable({ documents }: { documents: DocumentBehind[] }) {
  return (
    <Table aria-label="Billing documents behind this answer" className="documents-behind">
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Vendor</TableHead>
          <TableHead scope="col">Document type</TableHead>
          <TableHead scope="col">Invoice date</TableHead>
          <TableHead scope="col" className="amount">
            Amount
          </TableHead>
          <TableHead scope="col">Currency</TableHead>
          <TableHead scope="col" className="amount">
            Amount in rupees
          </TableHead>
          <TableHead scope="col">Source account</TableHead>
          <TableHead scope="col">File</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {documents.map((document) => (
          <TableRow
            key={`${document.file_url} ${document.date}`}
            className={cn(
              document.document_type === 'credit_note' && 'is-credit-note bg-destructive-soft/40',
            )}
          >
            <TableCell className="font-medium">{document.vendor}</TableCell>
            <TableCell>
              <Badge
                variant={document.document_type === 'credit_note' ? 'destructive' : 'outline'}
                className={`tag tag-${document.document_type}`}
              >
                {DOCUMENT_TYPES[document.document_type] ?? document.document_type}
              </Badge>
            </TableCell>
            <TableCell className="whitespace-nowrap">{formatDate(document.date)}</TableCell>
            <TableCell className="amount">
              <Money amount={document.amount} />
            </TableCell>
            <TableCell>{document.currency}</TableCell>
            <TableCell className="amount">
              {document.amount_inr === null ? (
                <NotAvailable>None</NotAvailable>
              ) : (
                <Rupees amount={document.amount_inr} />
              )}
            </TableCell>
            <TableCell className="text-muted-foreground">{document.source_account}</TableCell>
            <TableCell className="file font-mono text-xs break-all">
              {isSafeLink(document.file_url) ? (
                <a
                  href={document.file_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-primary underline-offset-4 hover:underline"
                >
                  {document.file_name}
                </a>
              ) : (
                document.file_name
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
