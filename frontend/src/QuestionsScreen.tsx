import { useEffect, useId, useState, type FormEvent } from 'react'
import {
  askQuestion,
  NotSignedIn,
  type Answer,
  type AnswerColumn,
  type DocumentBehind,
  type DocumentType,
} from './api'
import { formatAmount, formatDate, formatRupees, isNegative, isSafeLink } from './format'
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

  useEffect(() => store(entries), [entries])

  async function submit(event: FormEvent) {
    event.preventDefault()
    const question = draft.trim()
    if (!question) return
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
    <main className="screen">
      <h1>Questions</h1>
      <p className="lede">
        Ask about spend by vendor, month, source account or period. Each answer comes from one of
        a fixed set of queries and lists the billing documents behind it.
      </p>
      <form className="ask" onSubmit={submit}>
        <label htmlFor={boxId}>Question</label>
        <div className="ask-row">
          <input
            id={boxId}
            type="text"
            value={draft}
            maxLength={500}
            placeholder="How much did we spend on AWS last quarter?"
            onChange={(event) => setDraft(event.target.value)}
          />
          <button type="submit" className="primary" disabled={!draft.trim()}>
            Ask
          </button>
        </div>
        <div className="examples">
          <span>Try:</span>
          {EXAMPLES.map((example) => (
            <button key={example} type="button" onClick={() => setDraft(example)}>
              {example}
            </button>
          ))}
        </div>
      </form>
      {entries.map((entry) => (
        <AskedQuestion key={entry.id} entry={entry} />
      ))}
    </main>
  )
}

function AskedQuestion({ entry }: { entry: Entry }) {
  const id = useId()
  return (
    <article className="asked" aria-labelledby={id}>
      <h2 id={id}>{entry.question}</h2>
      {entry.status === 'asking' && <p className="empty">Working it out…</p>}
      {entry.status === 'problem' && (
        <p className="reasons" role="alert">
          {entry.message}
        </p>
      )}
      {entry.status === 'answered' && <AnswerShown answer={entry.answer} />}
    </article>
  )
}

function AnswerShown({ answer }: { answer: Answer }) {
  if (!answer.answered) {
    return (
      <div className="cannot-answer">
        <p className="answer">{answer.answer}</p>
        {answer.reason && <p>{answer.reason}</p>}
        <p className="hint">
          Questions like this are noted so the set of questions it can answer can grow.
        </p>
      </div>
    )
  }
  return (
    <>
      <p className="answer">{answer.answer}</p>
      {answer.query && (
        <p className="hint">
          Query used: <span className="query">{answer.query.description}</span>
        </p>
      )}
      {answer.rows.length > 0 && <ResultTable columns={answer.columns} rows={answer.rows} />}
      {answer.documents.length > 0 ? (
        <DocumentsTable documents={answer.documents} />
      ) : (
        <p className="empty">No billing documents are behind this answer.</p>
      )}
    </>
  )
}

function Rupees({ amount }: { amount: string }) {
  return (
    <span className={isNegative(amount) ? 'number is-negative' : 'number'}>
      {formatRupees(amount)}
    </span>
  )
}

function Cell({ column, value }: { column: AnswerColumn; value: string | undefined }) {
  if (column.kind === 'amount') {
    return (
      <td className="amount">
        {value ? <Rupees amount={value} /> : <span className="not-available">—</span>}
      </td>
    )
  }
  return <td className={column.kind === 'count' ? 'amount' : undefined}>{value ?? ''}</td>
}

function ResultTable({
  columns,
  rows,
}: {
  columns: AnswerColumn[]
  rows: Record<string, string>[]
}) {
  return (
    <table aria-label="Result">
      <thead>
        <tr>
          {columns.map((column) => (
            <th
              key={column.key}
              scope="col"
              className={column.kind === 'text' ? undefined : 'amount'}
            >
              {column.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={index}>
            {columns.map((column) => (
              <Cell key={column.key} column={column} value={row[column.key]} />
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function DocumentsTable({ documents }: { documents: DocumentBehind[] }) {
  return (
    <table aria-label="Billing documents behind this answer" className="documents-behind">
      <thead>
        <tr>
          <th scope="col">Vendor</th>
          <th scope="col">Document type</th>
          <th scope="col">Invoice date</th>
          <th scope="col" className="amount">
            Amount
          </th>
          <th scope="col">Currency</th>
          <th scope="col" className="amount">
            Amount in rupees
          </th>
          <th scope="col">Source account</th>
          <th scope="col">File</th>
        </tr>
      </thead>
      <tbody>
        {documents.map((document) => (
          <tr
            key={`${document.file_url} ${document.date}`}
            className={document.document_type === 'credit_note' ? 'is-credit-note' : undefined}
          >
            <td>{document.vendor}</td>
            <td>{DOCUMENT_TYPES[document.document_type] ?? document.document_type}</td>
            <td>{formatDate(document.date)}</td>
            <td className="amount">
              <span className={isNegative(document.amount) ? 'number is-negative' : 'number'}>
                {formatAmount(document.amount)}
              </span>
            </td>
            <td>{document.currency}</td>
            <td className="amount">
              {document.amount_inr === null ? (
                <span className="not-available">None</span>
              ) : (
                <Rupees amount={document.amount_inr} />
              )}
            </td>
            <td>{document.source_account}</td>
            <td className="file">
              {isSafeLink(document.file_url) ? (
                <a href={document.file_url} target="_blank" rel="noopener noreferrer">
                  {document.file_name}
                </a>
              ) : (
                document.file_name
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
