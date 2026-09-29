import { useEffect, useId, type ReactNode } from 'react'
import { Link } from 'react-router'
import {
  monthSummary,
  type BillingSignal,
  type DocumentType,
  type EmailNeedingReview,
  type EmailWithReason,
  type FailedSourceAccount,
  type Gap,
  type MonthSummary,
  type SignalKind,
  type SummaryRow,
  type UpcomingCharge,
} from './api'
import {
  formatAmount,
  formatDate,
  isCollectionMonth,
  isNegative,
  isSafeLink,
  monthName,
} from './format'
import { useShell } from './shell'
import { useLoaded } from './useLoaded'

const DOCUMENT_TYPES: Record<DocumentType, string> = {
  invoice: 'Invoice',
  receipt: 'Receipt',
  credit_note: 'Credit note',
}

const SIGNAL_KINDS: Record<SignalKind, string> = {
  payment_failed: 'Payment failed',
  renewal_reminder: 'Renewal reminder',
}

export function SummaryScreen() {
  const { month, monthsLoading } = useShell()

  if (month === null) {
    return (
      <main className="screen">
        <h1>Summary</h1>
        <p className="empty">
          {monthsLoading ? 'Loading…' : 'No run has been recorded yet.'}
        </p>
      </main>
    )
  }
  if (!isCollectionMonth(month)) {
    return (
      <main className="screen">
        <h1>Summary</h1>
        <p className="reasons" role="alert">
          {month} is not a collection month. Choose one from the list above.
        </p>
      </main>
    )
  }
  return <SummaryOfMonth month={month} />
}

function SummaryOfMonth({ month }: { month: string }) {
  const { onSignedOut } = useShell()
  const summary = useLoaded(`summary-${month}`, (signal) => monthSummary(month, signal))

  const notSignedIn = summary.status === 'not-signed-in'
  useEffect(() => {
    if (notSignedIn) onSignedOut()
  }, [notSignedIn, onSignedOut])

  return (
    <main className="screen">
      <h1>Summary for {monthName(month)}</h1>
      {summary.status === 'loading' && <p className="empty">Loading…</p>}
      {summary.status === 'problem' && (
        <p className="reasons" role="alert">
          {summary.message}
        </p>
      )}
      {summary.status === 'ready' && <Sections summary={summary.value} />}
    </main>
  )
}

function Sections({ summary }: { summary: MonthSummary }) {
  return (
    <>
      <Headline summary={summary} />
      {summary.failed_source_accounts.length > 0 && (
        <Section
          name="Source accounts that could not be read"
          count={summary.failed_source_accounts.length}
        >
          <UnreadAccounts accounts={summary.failed_source_accounts} />
        </Section>
      )}
      <Section name="Gaps" count={summary.gaps.length}>
        {summary.gaps.length === 0 ? (
          <p className="empty">Every expected vendor sent a billing document.</p>
        ) : (
          <GapTable gaps={summary.gaps} />
        )}
      </Section>
      {summary.upcoming.length > 0 && (
        <Section name="Upcoming charges" count={summary.upcoming.length}>
          <UpcomingTable upcoming={summary.upcoming} />
        </Section>
      )}
      <Section name="Billing documents" count={summary.rows.length}>
        {summary.rows.length === 0 ? (
          <p className="empty">No billing documents were collected.</p>
        ) : (
          <SummaryTable month={summary.month} rows={summary.rows} />
        )}
      </Section>
      <Section name="Emails needing review" count={summary.needs_review.length}>
        {summary.needs_review.length === 0 ? (
          <p className="empty">No emails need review.</p>
        ) : (
          <EmailTable emails={summary.needs_review} withPortalLink />
        )}
      </Section>
      <Section name="Skipped emails" count={summary.skipped.length}>
        {summary.skipped.length === 0 ? (
          <p className="empty">No emails were skipped.</p>
        ) : (
          <EmailTable emails={summary.skipped} />
        )}
      </Section>
      <Section name="Failed emails" count={summary.failed.length}>
        {summary.failed.length === 0 ? (
          <p className="empty">No emails failed.</p>
        ) : (
          <EmailTable emails={summary.failed} />
        )}
      </Section>
      <Section name="Billing signals" count={summary.billing_signals.length}>
        {summary.billing_signals.length === 0 ? (
          <p className="empty">No billing signals were found.</p>
        ) : (
          <SignalTable signals={summary.billing_signals} />
        )}
      </Section>
    </>
  )
}

function Section({ name, count, children }: { name: string; count: number; children: ReactNode }) {
  const id = useId()
  return (
    <section aria-labelledby={id}>
      <h2>
        <span id={id}>{name}</span> <small>{count}</small>
      </h2>
      {children}
    </section>
  )
}

function Headline({ summary }: { summary: MonthSummary }) {
  const id = useId()
  const totalsId = useId()
  return (
    <section aria-labelledby={id} className="headline">
      <h2 id={id} className="visually-hidden">
        Headline numbers
      </h2>
      <dl>
        <div className="figure">
          <dt>Billing documents collected</dt>
          <dd>{summary.rows.length}</dd>
        </div>
        <div className={summary.counts.needs_review > 0 ? 'figure is-flagged' : 'figure'}>
          <dt>Needing review</dt>
          <dd>{summary.counts.needs_review}</dd>
        </div>
        <div className={summary.gaps.length > 0 ? 'figure is-flagged' : 'figure'}>
          <dt>Gaps</dt>
          <dd>{summary.gaps.length}</dd>
        </div>
        <div className="figure">
          <dt>Total in rupees</dt>
          <dd>
            {summary.rows.length === 0 ? (
              <span className="not-available">Nothing collected</span>
            ) : (
              <>
                <span className="currency">INR</span> <Amount amount={summary.total_inr} />
                {summary.rows_without_rupees > 0 && (
                  <small className="is-flagged">
                    {summary.rows_without_rupees === 1
                      ? '1 row has no rupee amount and is left out'
                      : `${summary.rows_without_rupees} rows have no rupee amount and are left out`}
                  </small>
                )}
              </>
            )}
          </dd>
        </div>
        <div className="figure">
          <dt id={totalsId}>Total per currency</dt>
          <dd>
            {summary.totals.length === 0 ? (
              <span className="not-available">Nothing collected</span>
            ) : (
              <ul aria-labelledby={totalsId} className="totals">
                {summary.totals.map((total) => (
                  <li key={total.currency}>
                    <span className="currency">{total.currency}</span>{' '}
                    <Amount amount={total.amount} />
                  </li>
                ))}
              </ul>
            )}
          </dd>
        </div>
      </dl>
    </section>
  )
}

function Amount({ amount }: { amount: string }) {
  return (
    <span className={isNegative(amount) ? 'number is-negative' : 'number'}>
      {formatAmount(amount)}
    </span>
  )
}

function SafeLink({ to, children }: { to: string; children: ReactNode }) {
  if (!isSafeLink(to)) return <>{to}</>
  return (
    <a href={to} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  )
}

function rateOf(row: SummaryRow): string | undefined {
  if (row.inr_rate === null || row.currency === 'INR') return undefined
  return `1 ${row.currency} = ${row.inr_rate} INR on the invoice date`
}

function SummaryTable({ month, rows }: { month: string; rows: SummaryRow[] }) {
  return (
    <table>
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
          <th scope="col">Notes</th>
          <th scope="col">History</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr
            key={`${row.source_account} ${row.file_url}`}
            className={row.document_type === 'credit_note' ? 'is-credit-note' : undefined}
          >
            <td>{row.vendor}</td>
            <td>
              <span className={`tag tag-${row.document_type}`}>
                {DOCUMENT_TYPES[row.document_type] ?? row.document_type}
              </span>
            </td>
            <td>{formatDate(row.date)}</td>
            <td className="amount">
              <Amount amount={row.amount} />
            </td>
            <td>{row.currency}</td>
            <td className="amount" title={rateOf(row)}>
              {row.amount_inr === null ? (
                <span className="not-available">No rate</span>
              ) : (
                <Amount amount={row.amount_inr} />
              )}
            </td>
            <td>{row.source_account}</td>
            <td className="file">
              {isSafeLink(row.file_url) ? (
                <SafeLink to={row.file_url}>{row.file_name}</SafeLink>
              ) : (
                row.file_name
              )}
            </td>
            <td className="reason">{row.notes}</td>
            <td>
              {row.content_hash ? (
                <Link
                  to={`/documents/${row.content_hash}?month=${encodeURIComponent(month)}`}
                  aria-label={`History of ${row.file_name}`}
                >
                  History
                </Link>
              ) : (
                <span className="not-available">Not recorded</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function UnreadAccounts({ accounts }: { accounts: FailedSourceAccount[] }) {
  return (
    <ul className="reasons" role="alert">
      {accounts.map((account) => (
        <li key={account.source_account}>
          <strong>{account.source_account}</strong>:{' '}
          {account.reason ?? 'no reason was recorded'}. Whether its vendors billed is not known.
        </li>
      ))}
    </ul>
  )
}

const GAP_KINDS: Record<Gap['kind'], string> = {
  missing: 'No billing document',
  unknown: 'Not known',
}

function GapTable({ gaps }: { gaps: Gap[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th scope="col">Expected vendor</th>
          <th scope="col">Gap</th>
          <th scope="col">Source account</th>
          <th scope="col">Explanation</th>
        </tr>
      </thead>
      <tbody>
        {gaps.map((gap) => (
          <tr key={`${gap.vendor} ${gap.source_account}`}>
            <td>{gap.vendor}</td>
            <td>
              <span className={`tag tag-gap-${gap.kind}`}>{GAP_KINDS[gap.kind] ?? gap.kind}</span>
            </td>
            <td>{gap.source_account ?? <span className="not-available">Any</span>}</td>
            <td className="reason">
              {gap.explanation ?? <span className="not-available">None found</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function UpcomingTable({ upcoming }: { upcoming: UpcomingCharge[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th scope="col">Vendor</th>
          <th scope="col">Source account</th>
          <th scope="col">What is coming</th>
        </tr>
      </thead>
      <tbody>
        {upcoming.map((charge) => (
          <tr key={`${charge.vendor} ${charge.source_account} ${charge.note}`}>
            <td>{charge.vendor}</td>
            <td>{charge.source_account}</td>
            <td className="reason">{charge.note}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

type EmailTableProps =
  | { emails: EmailNeedingReview[]; withPortalLink: true }
  | { emails: EmailWithReason[]; withPortalLink?: false }

function EmailTable(props: EmailTableProps) {
  return (
    <table>
      <thead>
        <tr>
          <th scope="col">Subject</th>
          <th scope="col">Source account</th>
          <th scope="col">Reason</th>
          {props.withPortalLink && <th scope="col">Portal link</th>}
        </tr>
      </thead>
      <tbody>
        {props.withPortalLink
          ? props.emails.map((email) => (
              <EmailRow key={`${email.source_account} ${email.message_id}`} email={email}>
                <td className="file">
                  {email.portal_link ? (
                    <SafeLink to={email.portal_link}>Open portal link</SafeLink>
                  ) : (
                    <span className="not-available">None</span>
                  )}
                </td>
              </EmailRow>
            ))
          : props.emails.map((email) => (
              <EmailRow key={`${email.source_account} ${email.message_id}`} email={email} />
            ))}
      </tbody>
    </table>
  )
}

function EmailRow({ email, children }: { email: EmailWithReason; children?: ReactNode }) {
  return (
    <tr>
      <td>{email.subject}</td>
      <td>{email.source_account}</td>
      <td className="reason">
        {email.reason ?? <span className="not-available">No reason recorded</span>}
      </td>
      {children}
    </tr>
  )
}

function SignalTable({ signals }: { signals: BillingSignal[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th scope="col">Kind</th>
          <th scope="col">Vendor</th>
          <th scope="col">Subject</th>
          <th scope="col">Source account</th>
          <th scope="col">Received</th>
        </tr>
      </thead>
      <tbody>
        {signals.map((signal) => (
          <tr key={`${signal.source_account} ${signal.message_id}`}>
            <td>
              <span className="tag tag-signal">{SIGNAL_KINDS[signal.kind] ?? signal.kind}</span>
            </td>
            <td>{signal.vendor ?? <span className="not-available">Unknown</span>}</td>
            <td>{signal.subject}</td>
            <td>{signal.source_account}</td>
            <td>{formatDate(signal.received_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
