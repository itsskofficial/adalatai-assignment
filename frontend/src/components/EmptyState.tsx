import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

/** Nothing to show here yet: an icon, a sentence, and what to do next when there is something. */
export function EmptyState({
  icon: Icon,
  children,
  action,
  className,
  compact = false,
}: {
  icon?: LucideIcon
  /** The sentence. Kept as the text tests and people read. */
  children: ReactNode
  action?: ReactNode
  className?: string
  /** A smaller version, for inside a card or a pane. */
  compact?: boolean
}) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed bg-card/60 text-center',
        compact ? 'px-4 py-6' : 'px-6 py-12',
        className,
      )}
    >
      {Icon && (
        <span
          className={cn(
            'flex items-center justify-center rounded-full bg-muted text-muted-foreground',
            compact ? 'size-8' : 'size-11',
          )}
        >
          <Icon aria-hidden="true" className={compact ? 'size-4' : 'size-5'} />
        </span>
      )}
      <p className="m-0 max-w-md text-sm text-muted-foreground">{children}</p>
      {action && <div className="flex flex-wrap justify-center gap-2">{action}</div>}
    </div>
  )
}
