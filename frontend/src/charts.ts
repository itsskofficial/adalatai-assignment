// What the charts share: the look of the axes, and amounts short enough for an axis.

/** The colours and sizes the axes and grid share, from the theme. */
export const AXIS = {
  tick: { fill: 'var(--muted-foreground)', fontSize: 11 },
  line: { stroke: 'var(--border)' },
  grid: { stroke: 'var(--border)', strokeDasharray: '3 3' },
} as const

/** ₹1,23,456 shown short on an axis: ₹1.23L, ₹12.6k, ₹1.5Cr. */
export function shortRupees(value: number): string {
  const sign = value < 0 ? '-' : ''
  const amount = Math.abs(value)
  if (amount >= 1e7) return `${sign}₹${trim(amount / 1e7)}Cr`
  if (amount >= 1e5) return `${sign}₹${trim(amount / 1e5)}L`
  if (amount >= 1e3) return `${sign}₹${trim(amount / 1e3)}k`
  return `${sign}₹${trim(amount)}`
}

function trim(value: number): string {
  return value >= 100 ? value.toFixed(0) : value >= 10 ? value.toFixed(1) : value.toFixed(2).replace(/\.?0+$/, '')
}
