import type { ReactNode } from 'react'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

/**
 * The shape of what is coming, while it loads. The word is a polite status, so a screen
 * reader says it; the grey shapes are marked busy and hidden, since they say nothing.
 */
export function Loading({
  children,
  className,
  label = 'Loading…',
}: {
  children?: ReactNode
  className?: string
  label?: string
}) {
  return (
    <div className={cn('flex flex-col gap-4', className)}>
      <output className="sr-only">
        {label}
      </output>
      <div aria-busy="true" aria-hidden="true" className="contents">
        {children}
      </div>
    </div>
  )
}

/** A table's worth of grey lines. */
export function TableSkeleton({ rows = 4, columns = 5 }: { rows?: number; columns?: number }) {
  return (
    <div className="overflow-hidden rounded-xl border bg-card">
      <div className="flex gap-6 border-b px-4 py-3">
        {Array.from({ length: columns }, (_, column) => (
          <Skeleton key={column} className="h-3 flex-1" />
        ))}
      </div>
      {Array.from({ length: rows }, (_, row) => (
        <div key={row} className="flex gap-6 border-b px-4 py-3.5 last:border-0">
          {Array.from({ length: columns }, (_, column) => (
            <Skeleton key={column} className={cn('h-3.5 flex-1', column === 0 && 'max-w-40')} />
          ))}
        </div>
      ))}
    </div>
  )
}

/** A row of figure cards, greyed. */
export function CardsSkeleton({ count = 4, className }: { count?: number; className?: string }) {
  return (
    <div className={cn('grid gap-4 sm:grid-cols-2 xl:grid-cols-4', className)}>
      {Array.from({ length: count }, (_, index) => (
        <div key={index} className="flex flex-col gap-3 rounded-xl border bg-card p-4">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-7 w-32" />
          <Skeleton className="h-3 w-40" />
        </div>
      ))}
    </div>
  )
}

/** Lines of text, greyed. */
export function LinesSkeleton({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div className={cn('flex flex-col gap-2.5', className)}>
      {Array.from({ length: lines }, (_, index) => (
        <Skeleton key={index} className={cn('h-3.5', index % 3 === 2 ? 'w-2/3' : 'w-full')} />
      ))}
    </div>
  )
}
