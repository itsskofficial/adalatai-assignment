import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

/**
 * A native checkbox in the theme's colours. Native so that a form submits it and tests
 * check it as a person does.
 */
function Checkbox({ className, ...props }: ComponentProps<'input'>) {
  return (
    <input
      type="checkbox"
      data-slot="checkbox"
      className={cn(
        'size-4 shrink-0 cursor-pointer rounded-[4px] border border-input accent-primary shadow-xs outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40 disabled:cursor-not-allowed disabled:opacity-50',
        className,
      )}
      {...props}
    />
  )
}

export { Checkbox }
