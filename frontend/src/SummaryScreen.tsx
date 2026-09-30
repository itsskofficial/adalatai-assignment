import {
  BellIcon,
  CircleCheckIcon,
  FileTextIcon,
  HistoryIcon,
  MailWarningIcon,
  PlayIcon,
  SearchXIcon,
  type LucideIcon,
} from 'lucide-react'
import { lazy, Suspense, useEffect, useId, useState, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { NotSignedIn } from './api'
import { Button } from './components/ui/button'
import { runAgain } from './runsApi'
import {
  monthSummary,
  spendInRupees,
  type BillingSignal,
  type DocumentType,
  type EmailNeedingReview,
  type EmailWithReason,
  type FailedSourceAccount,
  type Gap,
  type GapStatus,
  type MonthSummary,
  type SignalKind,
  type SummaryRow,
  type UpcomingCharge,
} from './api'
import { Money, NotAvailable, Rupees } from './components/Amount'
import { ChartFrame } from './components/Chart'
import { EmptyState } from './components/EmptyState'
import { CardsSkeleton, Loading, TableSkeleton } from './components/Loading'
import { Problem } from './components/Notice'
import { PageHeader, Screen, Section } from './components/Screen'
import type { MonthPoint } from './SpendCharts'
import { Badge } from './components/ui/badge'
import { Skeleton } from './components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from './components/ui/table'
import { formatDate, formatRupees, isCollectionMonth, isSafeLink, monthName } from './format'
import { cn } from './lib/utils'
import { useShell } from './shell'
import { useLoaded } from './useLoaded'

// The charting library is fetched the first time the sparkline has a width to be drawn at.
const Sparkline = lazy(() => import('./SpendCharts').then((m) => ({ default: m.Sparkline })))

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
  const { month } = useShell()

  if (month === null) {
    return (
      <Screen>
        <PageHeader title="Summary" description="What each run found, one row per charge." />
        <Loading>
          <CardsSkeleton />
          <TableSkeleton />
        </Loading>
      </Screen>
    )
  }
  if (!isCollectionMonth(month)) {
    return (
      <Screen>
        <PageHeader title="Summary" />
        <Problem>{month} is not a collection month. Choose one from the list above.</Problem>
      </Screen>
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
    <Screen>
      <PageHeader
        title={`Summary for ${monthName(month)}`}
        description="The billing documents collected, the gaps, and the emails that need a person."
      />
      {summary.status === 'loading' && (
        <Loading>
          <CardsSkeleton />
          <TableSkeleton />
        </Loading>
      )}
      {summary.status === 'problem' && <Problem>{summary.message}</Problem>}
      {summary.status === 'ready' && <Sections summary={summary.value} />}
    </Screen>
  )
}

/** Runs the month from here, so a month chosen in the list needs no other screen. */
function RunButton({ month }: { month: string }) {
  const { onSignedOut } = useShell()
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true)
    try {
      await runAgain(month, null)
      toast.success(`Started running ${monthName(month)}.`)
      // The Runs screen follows the run; this one would keep showing what it had loaded.
      void navigate({ pathname: '/runs', search: `?month=${encodeURIComponent(month)}` })
    } catch (problem) {
      if (problem instanceof NotSignedIn) {
        onSignedOut()
        return
      }
      toast.error(problem instanceof Error ? problem.message : String(problem))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Button type="button" disabled={busy} onClick={() => void run()}>
      <PlayIcon />
      Run {monthName(month)}
    </Button>
  )
}

function Sections({ summary }: { summary: MonthSummary }) {
  return (
    <>
      <Headline summary={summary} />
      {summary.failed_source_accounts.length > 0 && (
        <LabelledSection
          name="Source accounts that could not be read"
          count={summary.failed_source_accounts.length}
        >
          <UnreadAccounts accounts={summary.failed_source_accounts} />
        </LabelledSection>
      )}
      <LabelledSection name="Gaps" count={summary.gaps.length}>
        {summary.gaps.length === 0 ? (
          <EmptyState icon={CircleCheckIcon} compact>
            Every expected vendor sent a billing document.
          </EmptyState>
        ) : (
          <GapTable gaps={summary.gaps} />
        )}
      </LabelledSection>
      {summary.upcoming.length > 0 && (
        <LabelledSection name="Upcoming charges" count={summary.upcoming.length}>
          <UpcomingTable upcoming={summary.upcoming} />
        </LabelledSection>
      )}
      <LabelledSection name="Billing documents" count={summary.rows.length}>
        {summary.rows.length === 0 ? (
          <EmptyState icon={FileTextIcon} action={<RunButton month={summary.month} />}>
            No billing documents were collected.
          </EmptyState>
        ) : (
          <SummaryTable month={summary.month} rows={summary.rows} />
        )}
      </LabelledSection>
      <LabelledSection name="Emails needing review" count={summary.needs_review.length}>
        {summary.needs_review.length === 0 ? (
          <EmptyState icon={CircleCheckIcon} compact>
            No emails need review.
          </EmptyState>
        ) : (
          <EmailTable emails={summary.needs_review} withPortalLink />
        )}
      </LabelledSection>
      <LabelledSection name="Skipped emails" count={summary.skipped.length}>
        {summary.skipped.length === 0 ? (
          <EmptyState icon={SearchXIcon} compact>
            No emails were skipped.
          </EmptyState>
        ) : (
          <EmailTable emails={summary.skipped} />
        )}
      </LabelledSection>
      <LabelledSection name="Failed emails" count={summary.failed.length}>
        {summary.failed.length === 0 ? (
          <EmptyState icon={CircleCheckIcon} compact>
            No emails failed.
          </EmptyState>
        ) : (
          <EmailTable emails={summary.failed} />
        )}
      </LabelledSection>
      <LabelledSection name="Billing signals" count={summary.billing_signals.length}>
        {summary.billing_signals.length === 0 ? (
          <EmptyState icon={BellIcon} compact>
            No billing signals were found.
          </EmptyState>
        ) : (
          <SignalTable signals={summary.billing_signals} />
        )}
      </LabelledSection>
    </>
  )
}

function LabelledSection({
  name,
  count,
  children,
}: {
  name: string
  count: number
  children: ReactNode
}) {
  const id = useId()
  return (
    <Section id={id} aria-labelledby={id} title={name} count={count}>
      {children}
    </Section>
  )
}

/** One figure that matters this month, in a card. */
function Figure({
  label,
  value,
  detail,
  icon: Icon,
  flagged = false,
}: {
  label: ReactNode
  value: ReactNode
  detail?: ReactNode
  icon: LucideIcon
  flagged?: boolean
}) {
  return (
    <div
      className={cn(
        'figure flex flex-col gap-2 rounded-xl border bg-card p-4 shadow-xs',
        flagged && 'is-flagged border-warning/40 bg-warning-soft/60',
      )}
    >
      <dt className="flex items-center justify-between gap-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {label}
        <Icon
          aria-hidden="true"
          className={cn('size-4', flagged ? 'text-warning' : 'text-muted-foreground/70')}
        />
      </dt>
      <dd className={cn('m-0 text-2xl font-semibold tabular', flagged && 'text-warning')}>
        {value}
        {detail && <small className="mt-1 block text-xs font-normal">{detail}</small>}
      </dd>
    </div>
  )
}

/**
 * The rupee spend of every month, for the shape beside this month's total. Read apart from
 * the summary and never in its way: without it the figure stands alone.
 */
function useSpendShape(): MonthPoint[] {
  const spend = useLoaded('spend-shape', spendInRupees)
  if (spend.status !== 'ready' || spend.value.months.length < 2) return []
  return spend.value.months.map((each) => ({
    month: each.month,
    total: Number(each.inr_total),
    label: formatRupees(each.inr_total),
  }))
}

function Headline({ summary }: { summary: MonthSummary }) {
  const id = useId()
  const totalsId = useId()
  const shape = useSpendShape()
  const gapsFlagged = summary.gaps.length > 0
  const reviewFlagged = summary.counts.needs_review > 0
  return (
    <section aria-labelledby={id} className="headline">
      <h2 id={id} className="sr-only">
        Headline numbers
      </h2>
      <dl className="m-0 grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <Figure
          label="Billing documents collected"
          value={summary.rows.length}
          icon={FileTextIcon}
        />
        <Figure
          label="Needing review"
          value={summary.counts.needs_review}
          icon={MailWarningIcon}
          flagged={reviewFlagged}
        />
        <Figure label="Gaps" value={summary.gaps.length} icon={SearchXIcon} flagged={gapsFlagged} />
        <div className="figure relative flex flex-col gap-2 overflow-hidden rounded-xl border bg-card p-4 shadow-xs">
          <dt className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
            Total in rupees
          </dt>
          <dd className="m-0 text-2xl font-semibold tabular">
            {summary.rows.length === 0 ? (
              <span className="not-available text-sm font-normal text-muted-foreground">
                Nothing collected
              </span>
            ) : (
              <>
                <Rupees amount={summary.total_inr} />
                {summary.rows_without_rupees > 0 && (
                  <small className="is-flagged mt-1 block text-xs font-normal text-warning">
                    {summary.rows_without_rupees === 1
                      ? '1 row has no rupee amount and is left out'
                      : `${summary.rows_without_rupees} rows have no rupee amount and are left out`}
                  </small>
                )}
              </>
            )}
          </dd>
          {shape.length > 0 && (
            <ChartFrame height={36} className="mt-1">
              {(width) => (
                <Suspense fallback={<Skeleton className="h-full w-full" />}>
                  <Sparkline points={shape} width={width} height={36} />
                </Suspense>
              )}
            </ChartFrame>
          )}
        </div>
        <div className="figure flex flex-col gap-2 rounded-xl border bg-card p-4 shadow-xs">
          <dt id={totalsId} className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
            Total per currency
          </dt>
          <dd className="m-0">
            {summary.totals.length === 0 ? (
              <span className="not-available text-sm text-muted-foreground">
                Nothing collected
              </span>
            ) : (
              <ul aria-labelledby={totalsId} className="totals m-0 flex list-none flex-col gap-1 p-0">
                {summary.totals.map((total) => (
                  <li key={total.currency} className="flex items-baseline justify-between gap-3">
                    <span className="currency text-sm font-medium text-muted-foreground">
                      {total.currency}
                    </span>{' '}
                    <Money amount={total.amount} className="text-base font-semibold" />
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

function SafeLink({ to, children }: { to: string; children: ReactNode }) {
  if (!isSafeLink(to)) return <>{to}</>
  return (
    <a
      href={to}
      target="_blank"
      rel="noopener noreferrer"
      className="text-primary underline-offset-4 hover:underline"
    >
      {children}
    </a>
  )
}

function rateOf(row: SummaryRow): string | undefined {
  if (row.inr_rate === null || row.currency === 'INR') return undefined
  return `1 ${row.currency} = ${row.inr_rate} INR on the invoice date`
}

function DocumentTypeBadge({ type }: { type: DocumentType }) {
  return (
    <Badge
      variant={type === 'credit_note' ? 'destructive' : type === 'receipt' ? 'muted' : 'outline'}
      className={`tag tag-${type}`}
    >
      {DOCUMENT_TYPES[type] ?? type}
    </Badge>
  )
}

function SummaryTable({ month, rows }: { month: string; rows: SummaryRow[] }) {
  return (
    <Table>
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
          <TableHead scope="col">Notes</TableHead>
          <TableHead scope="col">History</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow
            key={`${row.source_account} ${row.file_url}`}
            className={cn(row.document_type === 'credit_note' && 'is-credit-note bg-destructive-soft/40')}
          >
            <TableCell className="font-medium">{row.vendor}</TableCell>
            <TableCell>
              <DocumentTypeBadge type={row.document_type} />
            </TableCell>
            <TableCell className="whitespace-nowrap">{formatDate(row.date)}</TableCell>
            <TableCell className="amount">
              <Money amount={row.amount} />
            </TableCell>
            <TableCell>{row.currency}</TableCell>
            <TableCell className="amount" title={rateOf(row)}>
              {row.amount_inr === null ? (
                <NotAvailable>No rate</NotAvailable>
              ) : (
                <Rupees amount={row.amount_inr} />
              )}
            </TableCell>
            <TableCell className="text-muted-foreground">{row.source_account}</TableCell>
            <TableCell className="file font-mono text-xs whitespace-nowrap">
              {isSafeLink(row.file_url) ? (
                <SafeLink to={row.file_url}>{row.file_name}</SafeLink>
              ) : (
                row.file_name
              )}
            </TableCell>
            <TableCell className="reason max-w-xs text-xs text-muted-foreground">{row.notes}</TableCell>
            <TableCell>
              {row.content_hash ? (
                <Link
                  to={`/documents/${row.content_hash}?month=${encodeURIComponent(month)}`}
                  aria-label={`History of ${row.file_name}`}
                  className="inline-flex items-center gap-1 text-primary underline-offset-4 hover:underline"
                >
                  <HistoryIcon aria-hidden="true" className="size-3.5" />
                  History
                </Link>
              ) : (
                <NotAvailable>Not recorded</NotAvailable>
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function UnreadAccounts({ accounts }: { accounts: FailedSourceAccount[] }) {
  return (
    <Problem tone="destructive">
      <ul className="m-0 flex list-none flex-col gap-1 p-0">
        {accounts.map((account) => (
          <li key={account.source_account}>
            <strong>{account.source_account}</strong>:{' '}
            {account.reason ?? 'no reason was recorded'}. Whether its vendors billed is not known.
          </li>
        ))}
      </ul>
    </Problem>
  )
}

// What stands in the way of each gap's billing document, and how loudly to say it.
type GapTone = 'warning' | 'destructive' | 'muted'

const GAP_STATUSES: Record<GapStatus, { label: string; tone: GapTone }> = {
  held_for_review: { label: 'Held for review', tone: 'warning' },
  manual_download: { label: 'Awaiting manual download', tone: 'warning' },
  email_failed: { label: 'An email failed', tone: 'destructive' },
  payment_failed: { label: 'Payment failed', tone: 'warning' },
  mailbox_unread: { label: 'Mailbox not read', tone: 'muted' },
  not_received: { label: 'Not received', tone: 'muted' },
}

function GapBadge({ status }: { status: GapStatus }) {
  const shown = GAP_STATUSES[status] ?? { label: status, tone: 'muted' }
  return (
    <Badge variant={shown.tone} className={`tag tag-gap-${status}`}>
      {shown.label}
    </Badge>
  )
}

function GapTable({ gaps }: { gaps: Gap[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Expected vendor</TableHead>
          <TableHead scope="col">Gap</TableHead>
          <TableHead scope="col">Source account</TableHead>
          <TableHead scope="col">Explanation</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {gaps.map((gap) => (
          <TableRow key={`${gap.vendor} ${gap.source_account}`}>
            <TableCell className="font-medium">{gap.vendor}</TableCell>
            <TableCell>
              <GapBadge status={gap.status} />
            </TableCell>
            <TableCell className="text-muted-foreground">
              {gap.source_account ?? <NotAvailable>Any</NotAvailable>}
            </TableCell>
            <TableCell className="reason text-warning">
              {gap.explanation ?? <NotAvailable>None found</NotAvailable>}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function UpcomingTable({ upcoming }: { upcoming: UpcomingCharge[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Vendor</TableHead>
          <TableHead scope="col">Source account</TableHead>
          <TableHead scope="col">What is coming</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {upcoming.map((charge) => (
          <TableRow key={`${charge.vendor} ${charge.source_account} ${charge.note}`}>
            <TableCell className="font-medium">{charge.vendor}</TableCell>
            <TableCell className="text-muted-foreground">{charge.source_account}</TableCell>
            <TableCell className="reason">{charge.note}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

type EmailTableProps =
  | { emails: EmailNeedingReview[]; withPortalLink: true }
  | { emails: EmailWithReason[]; withPortalLink?: false }

function EmailTable(props: EmailTableProps) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Subject</TableHead>
          <TableHead scope="col">Source account</TableHead>
          <TableHead scope="col">Reason</TableHead>
          {props.withPortalLink && <TableHead scope="col">Portal link</TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {props.withPortalLink
          ? props.emails.map((email) => (
              <EmailRow key={`${email.source_account} ${email.message_id}`} email={email}>
                <TableCell className="file text-xs whitespace-nowrap">
                  {email.portal_link ? (
                    <SafeLink to={email.portal_link}>Open portal link</SafeLink>
                  ) : (
                    <NotAvailable>None</NotAvailable>
                  )}
                </TableCell>
              </EmailRow>
            ))
          : props.emails.map((email) => (
              <EmailRow key={`${email.source_account} ${email.message_id}`} email={email} />
            ))}
      </TableBody>
    </Table>
  )
}

function EmailRow({ email, children }: { email: EmailWithReason; children?: ReactNode }) {
  return (
    <TableRow>
      <TableCell className="font-medium">{email.subject}</TableCell>
      <TableCell className="text-muted-foreground">{email.source_account}</TableCell>
      <TableCell className="reason text-warning">
        {email.reason ?? <NotAvailable>No reason recorded</NotAvailable>}
      </TableCell>
      {children}
    </TableRow>
  )
}

function SignalTable({ signals }: { signals: BillingSignal[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Kind</TableHead>
          <TableHead scope="col">Vendor</TableHead>
          <TableHead scope="col">Subject</TableHead>
          <TableHead scope="col">Source account</TableHead>
          <TableHead scope="col">Received</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {signals.map((signal) => (
          <TableRow key={`${signal.source_account} ${signal.message_id}`}>
            <TableCell>
              <Badge variant="warning" className="tag tag-signal">
                {SIGNAL_KINDS[signal.kind] ?? signal.kind}
              </Badge>
            </TableCell>
            <TableCell className="font-medium">
              {signal.vendor ?? <NotAvailable>Unknown</NotAvailable>}
            </TableCell>
            <TableCell>{signal.subject}</TableCell>
            <TableCell className="text-muted-foreground">{signal.source_account}</TableCell>
            <TableCell className="whitespace-nowrap">{formatDate(signal.received_at)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
