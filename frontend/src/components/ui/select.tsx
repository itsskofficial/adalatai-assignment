import { ChevronDownIcon } from 'lucide-react'
import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

/**
 * A native select, styled like the other fields. Native so that it works with the keyboard
 * and on phones as the browser does, and so tests choose an option as a person does.
 */
function Select({
  className,
  wrapperClassName,
  children,
  ...props
}: ComponentProps<'select'> & { wrapperClassName?: string }) {
  return (
    <span data-slot="select" className={cn('relative inline-flex w-full', wrapperClassName)}>
      <select
        className={cn(
          'h-9 w-full min-w-0 cursor-pointer appearance-none rounded-md border border-input bg-card py-1 pr-8 pl-3 text-sm shadow-xs transition-[color,box-shadow] outline-none disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30 dark:[&>option]:bg-popover',
          'focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/40',
          'aria-invalid:border-destructive aria-invalid:ring-destructive/20',
          className,
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDownIcon
        aria-hidden="true"
        className="pointer-events-none absolute top-1/2 right-2.5 size-4 -translate-y-1/2 text-muted-foreground"
      />
    </span>
  )
}

export { Select }
