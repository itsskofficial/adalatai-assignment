// The charts drawn with Recharts, loaded only once a chart has a width to be drawn at, so
// the rest of the dashboard does not carry the charting library.

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
} from 'recharts'
import { AXIS, shortRupees } from './charts'
import { ChartTooltip } from './components/Chart'
import { monthName } from './format'

export type MonthPoint = { month: string; total: number; label: string }
export type VendorPoint = { vendor: string; total: number; label: string }

/** "Jun ’26", short enough for an axis. */
function axisMonth(month: string): string {
  const [name = '', year = ''] = monthName(month).split(' ')
  return `${name.slice(0, 3)} ’${year.slice(2)}`
}

function MonthTooltip({ active, payload }: Partial<TooltipContentProps<number, string>>) {
  const point = payload?.[0]?.payload as MonthPoint | undefined
  if (!point) return null
  return (
    <ChartTooltip
      active={active}
      title={monthName(point.month)}
      lines={[{ label: 'Spend', value: point.label, color: 'var(--chart-1)' }]}
    />
  )
}

/** Spend per collection month, one bar each. */
export function MonthBars({
  points,
  width,
  height,
}: {
  points: MonthPoint[]
  width: number
  height: number
}) {
  return (
    <BarChart
      width={width}
      height={height}
      data={points}
      margin={{ top: 8, right: 8, bottom: 0, left: 8 }}
      barCategoryGap="30%"
    >
      <CartesianGrid vertical={false} {...AXIS.grid} />
      <XAxis
        dataKey="month"
        tickFormatter={axisMonth}
        tick={AXIS.tick}
        axisLine={AXIS.line}
        tickLine={false}
        tickMargin={8}
      />
      <YAxis
        tickFormatter={shortRupees}
        tick={AXIS.tick}
        axisLine={false}
        tickLine={false}
        width={56}
      />
      <Tooltip
        cursor={{ fill: 'var(--muted)', opacity: 0.6 }}
        content={<MonthTooltip />}
        isAnimationActive={false}
      />
      <Bar dataKey="total" fill="var(--chart-1)" radius={[4, 4, 0, 0]} maxBarSize={64} isAnimationActive={false}>
        {points.map((point) => (
          <Cell key={point.month} fill={point.total < 0 ? 'var(--destructive)' : 'var(--chart-1)'} />
        ))}
      </Bar>
    </BarChart>
  )
}

function VendorTooltip({ active, payload }: Partial<TooltipContentProps<number, string>>) {
  const point = payload?.[0]?.payload as VendorPoint | undefined
  if (!point) return null
  return (
    <ChartTooltip
      active={active}
      title={point.vendor}
      lines={[
        {
          label: 'Spend',
          value: point.label,
          color: point.total < 0 ? 'var(--destructive)' : 'var(--chart-1)',
        },
      ]}
    />
  )
}

/** Spend per vendor, largest first, one bar each across the width. */
export function VendorBars({
  points,
  width,
  height,
}: {
  points: VendorPoint[]
  width: number
  height: number
}) {
  const labelWidth = Math.min(140, Math.max(60, ...points.map((p) => p.vendor.length * 6.5)))
  return (
    <BarChart
      layout="vertical"
      width={width}
      height={height}
      data={points}
      margin={{ top: 4, right: 16, bottom: 4, left: 8 }}
      barCategoryGap="28%"
    >
      <CartesianGrid horizontal={false} {...AXIS.grid} />
      <XAxis
        type="number"
        tickFormatter={shortRupees}
        tick={AXIS.tick}
        axisLine={AXIS.line}
        tickLine={false}
      />
      <YAxis
        type="category"
        dataKey="vendor"
        tick={AXIS.tick}
        axisLine={false}
        tickLine={false}
        width={labelWidth}
      />
      <Tooltip
        cursor={{ fill: 'var(--muted)', opacity: 0.6 }}
        content={<VendorTooltip />}
        isAnimationActive={false}
      />
      <Bar dataKey="total" radius={[0, 4, 4, 0]} maxBarSize={22} isAnimationActive={false}>
        {points.map((point) => (
          <Cell key={point.vendor} fill={point.total < 0 ? 'var(--destructive)' : 'var(--chart-1)'} />
        ))}
      </Bar>
    </BarChart>
  )
}

/** The shape of spend over the months, small enough for a figure card. No axes; the card says the number. */
export function Sparkline({
  points,
  width,
  height,
}: {
  points: MonthPoint[]
  width: number
  height: number
}) {
  return (
    <AreaChart width={width} height={height} data={points} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
      <defs>
        <linearGradient id="sparkline-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--chart-1)" stopOpacity={0.35} />
          <stop offset="100%" stopColor="var(--chart-1)" stopOpacity={0} />
        </linearGradient>
      </defs>
      <Tooltip content={<MonthTooltip />} cursor={{ stroke: 'var(--border)' }} isAnimationActive={false} />
      <Area
        type="monotone"
        dataKey="total"
        stroke="var(--chart-1)"
        strokeWidth={2}
        fill="url(#sparkline-fill)"
        dot={false}
        activeDot={{ r: 3, strokeWidth: 0 }}
        isAnimationActive={false}
      />
    </AreaChart>
  )
}
