import { formatAmount, formatRupees, isNegative } from '@/format'
import { cn } from '@/lib/utils'

/** An amount as text, in tabular figures, set apart when it is negative. */
export function Money({ amount, className }: { amount: string; className?: string }) {
  const negative = isNegative(amount)
  return (
    <span
      className={cn(
        'number tabular whitespace-nowrap',
        negative && 'is-negative font-semibold text-destructive',
        className,
      )}
    >
      {formatAmount(amount)}
    </span>
  )
}

/** An amount in rupees with the sign and Indian grouping, set apart when negative. */
export function Rupees({ amount, className }: { amount: string; className?: string }) {
  const negative = isNegative(amount)
  return (
    <span
      className={cn(
        'number tabular whitespace-nowrap',
        negative && 'is-negative font-semibold text-destructive',
        className,
      )}
    >
      {formatRupees(amount)}
    </span>
  )
}

/** A value the ledger does not have, said quietly. */
export function NotAvailable({ children }: { children: string }) {
  return <span className="not-available text-muted-foreground">{children}</span>
}
