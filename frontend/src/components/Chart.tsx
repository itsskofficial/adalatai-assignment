// What every chart shares: a frame that waits for its width, and a tooltip in the
// dashboard's own colours. The axes' look and the short rupee format are in src/charts.ts.

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

/**
 * Draws its chart once it knows how wide it is, so the chart is never drawn at a guessed
 * width and then again at the real one. Until then it shows the chart's outline.
 */
export function ChartFrame({
  height,
  className,
  children,
}: {
  height: number
  className?: string
  children: (width: number) => ReactNode
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)

  useEffect(() => {
    const element = ref.current
    if (element === null || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver((entries) => {
      const measured = entries[0]?.contentRect.width ?? 0
      setWidth(Math.floor(measured))
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  return (
    <div ref={ref} aria-hidden="true" className={cn('w-full', className)} style={{ height }}>
      {width > 0 ? children(width) : <Skeleton className="h-full w-full" />}
    </div>
  )
}

/** What a tooltip says: a title and one line per value. */
export function ChartTooltip({
  active,
  title,
  lines,
}: {
  active?: boolean
  title?: ReactNode
  lines: { label: ReactNode; value: ReactNode; color?: string }[]
}) {
  if (!active || lines.length === 0) return null
  return (
    <div className="min-w-32 rounded-lg border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-md">
      {title && <div className="mb-1 font-medium">{title}</div>}
      {lines.map((line, index) => (
        <div key={index} className="flex items-center justify-between gap-4">
          <span className="flex items-center gap-1.5 text-muted-foreground">
            {line.color && (
              <span
                aria-hidden="true"
                className="inline-block size-2 rounded-[2px]"
                style={{ background: line.color }}
              />
            )}
            {line.label}
          </span>
          <span className="font-medium tabular">{line.value}</span>
        </div>
      ))}
    </div>
  )
}
