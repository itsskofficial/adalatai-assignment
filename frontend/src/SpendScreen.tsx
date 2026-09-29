import { useEffect, useId, type ReactNode } from 'react'
import { spendInRupees, type Spend, type Total, type VendorChange } from './api'
import { formatAmount, formatRupees, isNegative, monthName } from './format'
import { useShell } from './shell'
import { useLoaded } from './useLoaded'

export function SpendScreen() {
  const { onSignedOut } = useShell()
  const spend = useLoaded('spend', spendInRupees)

  const notSignedIn = spend.status === 'not-signed-in'
  useEffect(() => {
    if (notSignedIn) onSignedOut()
  }, [notSignedIn, onSignedOut])

  return (
    <main className="screen">
      <h1>Spend</h1>
      {spend.status === 'loading' && <p className="empty">Loading…</p>}
      {spend.status === 'problem' && (
        <p className="reasons" role="alert">
          {spend.message}
        </p>
      )}
      {spend.status === 'ready' && <SpendSections spend={spend.value} />}
    </main>
  )
}

function SpendSections({ spend }: { spend: Spend }) {
  if (spend.months.length === 0) {
    return <p className="empty">No charges have been collected yet.</p>
  }
  const range =
    spend.from_month === spend.to_month
      ? monthName(spend.to_month ?? '')
      : `${monthName(spend.from_month ?? '')} to ${monthName(spend.to_month ?? '')}`
  return (
    <>
      <p className="lede">
        {range}, in rupees at each invoice date's rate. Total{' '}
        <strong>
          <Rupees amount={spend.inr_total} />
        </strong>
        .
      </p>
      <WithoutRupeesNote count={spend.without_rupees.charges} totals={spend.without_rupees.totals} />
      <Section name="Spend by month">
        <MonthChart months={spend.months} range={range} />
      </Section>
      <div className="spend-columns">
        <Section name="Spend by vendor">
          <Bars
            items={spend.vendors.map((each) => ({ label: each.vendor, amount: each.inr_total }))}
          />
        </Section>
        <Section name="Spend by source account">
          <Bars
            items={spend.source_accounts.map((each) => ({
              label: each.source_account,
              amount: each.inr_total,
            }))}
          />
          {spend.shared_charges > 0 && (
            <p className="hint">
              {spend.shared_charges === 1
                ? '1 charge was found in several source accounts and is counted under the first.'
                : `${spend.shared_charges} charges were found in several source accounts and are counted under the first.`}
            </p>
          )}
        </Section>
      </div>
      {spend.changes && (
        <Section name="Changes since last month">
          <p className="hint">
            {monthName(spend.changes.month)} compared with{' '}
            {monthName(spend.changes.previous_month)}, largest change first.
          </p>
          <ChangesTable
            month={spend.changes.month}
            previousMonth={spend.changes.previous_month}
            vendors={spend.changes.vendors}
          />
        </Section>
      )}
    </>
  )
}

function Section({ name, children }: { name: string; children: ReactNode }) {
  const id = useId()
  return (
    <section aria-labelledby={id}>
      <h2 id={id}>{name}</h2>
      {children}
    </section>
  )
}

export function Rupees({ amount }: { amount: string }) {
  return (
    <span className={isNegative(amount) ? 'number is-negative' : 'number'}>
      {formatRupees(amount)}
    </span>
  )
}

export function WithoutRupeesNote({ count, totals }: { count: number; totals: Total[] }) {
  if (count === 0) return null
  const amounts = totals.map((total) => `${total.currency} ${formatAmount(total.amount)}`)
  return (
    <p className="reasons" role="note">
      {count === 1 ? '1 charge has' : `${count} charges have`} no rupee amount and{' '}
      {count === 1 ? 'is' : 'are'} left out of these totals: {amounts.join(', ')}.
    </p>
  )
}

function shortMonth(month: string): string {
  const [name = '', year = ''] = monthName(month).split(' ')
  return `${name.slice(0, 3)} ${year}`
}

const CHART = { width: 640, height: 240, top: 28, bottom: 28, gap: 16 }

function MonthChart({ months, range }: { months: Spend['months']; range: string }) {
  const largest = Math.max(1, ...months.map((each) => Number(each.inr_total)))
  const plot = CHART.height - CHART.top - CHART.bottom
  const slot = CHART.width / months.length
  const barWidth = Math.min(72, slot - CHART.gap)
  return (
    <figure className="chart" aria-label={`Spend by month, ${range}`}>
      <svg
        viewBox={`0 0 ${CHART.width} ${CHART.height}`}
        preserveAspectRatio="xMidYMid meet"
      >
        <line
          x1={0}
          x2={CHART.width}
          y1={CHART.top + plot}
          y2={CHART.top + plot}
          className="axis"
        />
        {months.map((each, index) => {
          const height = (Math.max(0, Number(each.inr_total)) / largest) * plot
          const x = index * slot + (slot - barWidth) / 2
          const y = CHART.top + plot - height
          return (
            <g key={each.month}>
              <title>{`${monthName(each.month)}: ${formatRupees(each.inr_total)}`}</title>
              <rect x={x} y={y} width={barWidth} height={height} rx={3} className="bar" />
              <text x={x + barWidth / 2} y={y - 8} textAnchor="middle" className="bar-value">
                {formatRupees(each.inr_total)}
              </text>
              <text
                x={x + barWidth / 2}
                y={CHART.height - 8}
                textAnchor="middle"
                className="bar-label"
              >
                {shortMonth(each.month)}
              </text>
            </g>
          )
        })}
      </svg>
    </figure>
  )
}

function Bars({ items }: { items: { label: string; amount: string }[] }) {
  if (items.length === 0) return <p className="empty">No charges with a rupee amount.</p>
  const largest = Math.max(1, ...items.map((item) => Math.abs(Number(item.amount))))
  return (
    <ol className="bars">
      {items.map((item) => (
        <li key={item.label}>
          <span className="bar-name">{item.label}</span>
          <span className="bar-track" aria-hidden="true">
            <span
              className={isNegative(item.amount) ? 'bar-fill is-negative' : 'bar-fill'}
              style={{ width: `${(Math.abs(Number(item.amount)) / largest) * 100}%` }}
            />
          </span>
          <Rupees amount={item.amount} />
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
    return <p className="empty">No charges with a rupee amount in either month.</p>
  }
  return (
    <table>
      <thead>
        <tr>
          <th scope="col">Vendor</th>
          <th scope="col" className="amount">
            {monthName(previousMonth)}
          </th>
          <th scope="col" className="amount">
            {monthName(month)}
          </th>
          <th scope="col" className="amount">
            Change
          </th>
          <th scope="col" className="amount">
            Change %
          </th>
        </tr>
      </thead>
      <tbody>
        {vendors.map((change) => {
          const way = direction(change)
          return (
            <tr key={change.vendor} className={`is-${way}`}>
              <td>{change.vendor}</td>
              <td className="amount">
                <Rupees amount={change.previous_inr} />
              </td>
              <td className="amount">
                <Rupees amount={change.current_inr} />
              </td>
              <td className="amount">
                <span className={`change change-${way}`}>
                  {way === 'increase' ? 'Up' : way === 'decrease' ? 'Down' : 'Same'}
                </span>{' '}
                <span className="number">
                  {way === 'increase' ? '+' : ''}
                  {formatRupees(change.change_inr)}
                </span>
              </td>
              <td className="amount">{percent(change)}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
