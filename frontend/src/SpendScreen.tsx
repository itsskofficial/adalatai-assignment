import {
  ArrowDownRightIcon,
  ArrowUpRightIcon,
  Building2Icon,
  ChartColumnIcon,
  InboxIcon,
  MinusIcon,
  TriangleAlertIcon,
  WalletIcon,
  type LucideIcon,
} from 'lucide-react'
import { lazy, Suspense, useEffect, useId, type ReactNode } from 'react'
import { spendInRupees, type Spend, type Total, type VendorChange } from './api'
import { Rupees } from './components/Amount'
import { ChartFrame } from './components/Chart'
import { EmptyState } from './components/EmptyState'
import { CardsSkeleton, Loading, TableSkeleton } from './components/Loading'
import { Hint, Problem } from './components/Notice'
import { PageHeader, Screen, Section } from './components/Screen'
import type { MonthPoint, VendorPoint } from './SpendCharts'
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
import { formatAmount, formatRupees, isNegative, monthName } from './format'
import { cn } from './lib/utils'
import { useShell } from './shell'
import { useLoaded } from './useLoaded'

// The charting library is fetched the first time a chart has a width to be drawn at.
const MonthBars = lazy(() => import('./SpendCharts').then((m) => ({ default: m.MonthBars })))
const VendorBars = lazy(() => import('./SpendCharts').then((m) => ({ default: m.VendorBars })))

export function SpendScreen() {
  const { onSignedOut } = useShell()
  const spend = useLoaded('spend', spendInRupees)

  const notSignedIn = spend.status === 'not-signed-in'
  useEffect(() => {
    if (notSignedIn) onSignedOut()
  }, [notSignedIn, onSignedOut])

  return (
    <Screen>
      <PageHeader
        title="Spend"
        description="What the company spent, in rupees at each invoice date's rate, by month, vendor and source account."
      />
      {spend.status === 'loading' && (
        <Loading>
          <CardsSkeleton />
          <TableSkeleton rows={3} columns={3} />
        </Loading>
      )}
      {spend.status === 'problem' && <Problem>{spend.message}</Problem>}
      {spend.status === 'ready' && <SpendSections spend={spend.value} />}
    </Screen>
  )
}

function rangeOf(spend: Spend): string {
  return spend.from_month === spend.to_month
    ? monthName(spend.to_month ?? '')
    : `${monthName(spend.from_month ?? '')} to ${monthName(spend.to_month ?? '')}`
}

function SpendSections({ spend }: { spend: Spend }) {
  if (spend.months.length === 0) {
    return <EmptyState icon={WalletIcon}>No charges have been collected yet.</EmptyState>
  }
  const range = rangeOf(spend)
  const largest = spend.vendors[0]
  return (
    <>
      <dl className="m-0 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          label="Total spend"
          value={<Rupees amount={spend.inr_total} />}
          detail={`${range}, at each invoice date's rate`}
          icon={WalletIcon}
        />
        <Stat
          label="Months"
          value={spend.months.length}
          detail={
            spend.months.length === 1 ? 'One collection month' : 'Collection months with charges'
          }
          icon={ChartColumnIcon}
        />
        <Stat
          label="Vendors"
          value={spend.vendors.length}
          detail={largest ? `Largest: ${largest.vendor}` : 'None with a rupee amount'}
          icon={Building2Icon}
        />
        <Stat
          label="Source accounts"
          value={spend.source_accounts.length}
          detail={
            spend.shared_charges === 0
              ? 'No charge was found in more than one'
              : spend.shared_charges === 1
                ? '1 charge found in several'
                : `${spend.shared_charges} charges found in several`
          }
          icon={InboxIcon}
        />
      </dl>
      <WithoutRupeesNote count={spend.without_rupees.charges} totals={spend.without_rupees.totals} />
      <LabelledSection name="Spend by month">
        <MonthChart months={spend.months} range={range} />
      </LabelledSection>
      <LabelledSection name="Spend by vendor">
        <VendorChart vendors={spend.vendors} />
      </LabelledSection>
      <LabelledSection name="Spend by source account">
        <Bars
          items={spend.source_accounts.map((each) => ({
            label: each.source_account,
            amount: each.inr_total,
          }))}
        />
        {spend.shared_charges > 0 && (
          <Hint>
            {spend.shared_charges === 1
              ? '1 charge was found in several source accounts and is counted under the first.'
              : `${spend.shared_charges} charges were found in several source accounts and are counted under the first.`}
          </Hint>
        )}
      </LabelledSection>
      {spend.changes && (
        <LabelledSection
          name="Changes since last month"
          description={`${monthName(spend.changes.month)} compared with ${monthName(spend.changes.previous_month)}, largest change first.`}
        >
          <ChangesTable
            month={spend.changes.month}
            previousMonth={spend.changes.previous_month}
            vendors={spend.changes.vendors}
          />
        </LabelledSection>
      )}
    </>
  )
}

function Stat({
  label,
  value,
  detail,
  icon: Icon,
}: {
  label: string
  value: ReactNode
  detail?: ReactNode
  icon: LucideIcon
}) {
  return (
    <div className="flex flex-col gap-2 rounded-xl border bg-card p-4 shadow-xs">
      <dt className="flex items-center justify-between gap-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {label}
        <Icon aria-hidden="true" className="size-4 text-muted-foreground/70" />
      </dt>
      <dd className="m-0 text-2xl font-semibold tabular">
        {value}
        {detail && (
          <small className="mt-1 block truncate text-xs font-normal text-muted-foreground">
            {detail}
          </small>
        )}
      </dd>
    </div>
  )
}

function LabelledSection({
  name,
  description,
  children,
}: {
  name: string
  description?: ReactNode
  children: ReactNode
}) {
  const id = useId()
  return (
    <Section id={id} aria-labelledby={id} title={name} description={description}>
      {children}
    </Section>
  )
}

export function WithoutRupeesNote({ count, totals }: { count: number; totals: Total[] }) {
  if (count === 0) return null
  const amounts = totals.map((total) => `${total.currency} ${formatAmount(total.amount)}`)
  return (
    <p
      role="note"
      className="m-0 flex items-start gap-3 rounded-lg border border-warning/30 bg-warning-soft px-3.5 py-3 text-sm text-warning"
    >
      <TriangleAlertIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
      <span>
        {count === 1 ? '1 charge has' : `${count} charges have`} no rupee amount and{' '}
        {count === 1 ? 'is' : 'are'} left out of these totals: {amounts.join(', ')}.
      </span>
    </p>
  )
}

function shortMonth(month: string): string {
  const [name = '', year = ''] = monthName(month).split(' ')
  return `${name.slice(0, 3)} ${year}`
}

function MonthChart({ months, range }: { months: Spend['months']; range: string }) {
  const points: MonthPoint[] = months.map((each) => ({
    month: each.month,
    total: Number(each.inr_total),
    label: formatRupees(each.inr_total),
  }))
  return (
    <figure
      className="m-0 flex flex-col gap-4 rounded-xl border bg-card p-4 shadow-xs"
      aria-label={`Spend by month, ${range}`}
    >
      <ChartFrame height={260}>
        {(width) => (
          <Suspense fallback={<Skeleton className="h-full w-full" />}>
            <MonthBars points={points} width={width} height={260} />
          </Suspense>
        )}
      </ChartFrame>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead scope="col">Month</TableHead>
            <TableHead scope="col" className="amount">
              Spend in rupees
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {points.map((point) => (
            <TableRow key={point.month}>
              <TableCell className="font-medium">{shortMonth(point.month)}</TableCell>
              <TableCell className="amount">
                <Rupees amount={months.find((each) => each.month === point.month)?.inr_total ?? '0'} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </figure>
  )
}

const VENDORS_CHARTED = 12

function VendorChart({ vendors }: { vendors: Spend['vendors'] }) {
  if (vendors.length === 0) {
    return (
      <EmptyState icon={Building2Icon} compact>
        No charges with a rupee amount.
      </EmptyState>
    )
  }
  const points: VendorPoint[] = vendors.slice(0, VENDORS_CHARTED).map((each) => ({
    vendor: each.vendor,
    total: Number(each.inr_total),
    label: formatRupees(each.inr_total),
  }))
  const height = Math.max(160, 28 * points.length + 40)
  return (
    <div className="grid gap-4 lg:grid-cols-5">
      <figure
        className="m-0 rounded-xl border bg-card p-4 shadow-xs lg:col-span-3"
        aria-label={
          vendors.length > VENDORS_CHARTED
            ? `Spend by vendor, the ${VENDORS_CHARTED} largest`
            : 'Spend by vendor'
        }
      >
        <ChartFrame height={height}>
          {(width) => (
            <Suspense fallback={<Skeleton className="h-full w-full" />}>
              <VendorBars points={points} width={width} height={height} />
            </Suspense>
          )}
        </ChartFrame>
      </figure>
      <div className="lg:col-span-2">
        <Bars
          items={vendors.map((each) => ({ label: each.vendor, amount: each.inr_total }))}
          withTrack={false}
        />
      </div>
    </div>
  )
}

/** A ranked list, each with its share of the largest as a bar. */
function Bars({
  items,
  withTrack = true,
}: {
  items: { label: string; amount: string }[]
  withTrack?: boolean
}) {
  if (items.length === 0) {
    return (
      <EmptyState icon={InboxIcon} compact>
        No charges with a rupee amount.
      </EmptyState>
    )
  }
  const largest = Math.max(1, ...items.map((item) => Math.abs(Number(item.amount))))
  return (
    <ol className="m-0 flex list-none flex-col divide-y rounded-xl border bg-card px-4 py-1 shadow-xs [counter-reset:rank]">
      {items.map((item) => (
        <li
          key={item.label}
          className={cn(
            'grid items-center gap-x-3 gap-y-1 py-2.5',
            withTrack
              ? 'grid-cols-[minmax(0,1.2fr)_2fr_auto]'
              : // The rank is drawn, not written, so the list reads as name and amount alone.
                'grid-cols-[1.25rem_minmax(0,1fr)_auto] [counter-increment:rank] before:text-right before:text-xs before:tabular before:text-muted-foreground before:content-[counter(rank)]',
          )}
        >
          <span className="truncate text-sm font-medium" title={item.label}>
            {item.label}
          </span>
          {withTrack && (
            <span
              aria-hidden="true"
              className="block h-2 overflow-hidden rounded-full bg-muted"
            >
              <span
                className={cn(
                  'block h-full rounded-full',
                  isNegative(item.amount) ? 'bg-destructive' : 'bg-chart-1',
                )}
                style={{ width: `${(Math.abs(Number(item.amount)) / largest) * 100}%` }}
              />
            </span>
          )}
          <Rupees amount={item.amount} className="text-sm" />
        </li>
      ))}
    </ol>
  )
}

function direction(change: VendorChange): 'increase' | 'decrease' | 'same' {
  if (isNegative(change.change_inr)) return 'decrease'
  return /[1-9]/.test(change.change_inr) ? 'increase' : 'same'
}

function percent(change: VendorChange): string {
  if (change.change_percent !== null) {
    const sign = change.change_percent.startsWith('-') ? '' : '+'
    return `${sign}${change.change_percent}%`
  }
  return direction(change) === 'increase' ? 'New' : '—'
}

function ChangeBadge({ way }: { way: 'increase' | 'decrease' | 'same' }) {
  const Icon = way === 'increase' ? ArrowUpRightIcon : way === 'decrease' ? ArrowDownRightIcon : MinusIcon
  return (
    <Badge
      variant={way === 'increase' ? 'warning' : way === 'decrease' ? 'success' : 'muted'}
      className={`change change-${way}`}
    >
      <Icon aria-hidden="true" />
      {way === 'increase' ? 'Up' : way === 'decrease' ? 'Down' : 'Same'}
    </Badge>
  )
}

function ChangesTable({
  month,
  previousMonth,
  vendors,
}: {
  month: string
  previousMonth: string
  vendors: VendorChange[]
}) {
  if (vendors.length === 0) {
    return (
      <EmptyState icon={ChartColumnIcon} compact>
        No charges with a rupee amount in either month.
      </EmptyState>
    )
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Vendor</TableHead>
          <TableHead scope="col" className="amount">
            {monthName(previousMonth)}
          </TableHead>
          <TableHead scope="col" className="amount">
            {monthName(month)}
          </TableHead>
          <TableHead scope="col" className="amount">
            Change
          </TableHead>
          <TableHead scope="col" className="amount">
            Change %
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {vendors.map((change) => {
          const way = direction(change)
          return (
            <TableRow key={change.vendor} className={`is-${way}`}>
              <TableCell className="font-medium">{change.vendor}</TableCell>
              <TableCell className="amount">
                <Rupees amount={change.previous_inr} />
              </TableCell>
              <TableCell className="amount">
                <Rupees amount={change.current_inr} />
              </TableCell>
              <TableCell className="amount">
                <ChangeBadge way={way} />{' '}
                <span className="number tabular">
                  {way === 'increase' ? '+' : ''}
                  {formatRupees(change.change_inr)}
                </span>
              </TableCell>
              <TableCell className="amount">{percent(change)}</TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}
