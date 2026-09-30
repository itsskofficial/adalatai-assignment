import type { ComponentProps, ReactNode } from 'react'
import { cn } from '@/lib/utils'

/** The content area of a screen: the page and its sections, one column, with room to breathe. */
export function Screen({ className, ...props }: ComponentProps<'main'>) {
  return (
    <main
      className={cn('mx-auto flex w-full max-w-7xl flex-1 flex-col gap-8 px-4 py-6 md:px-8', className)}
      {...props}
    />
  )
}

/** The head of a screen: its title, one line saying what it is, and the actions that matter most. */
export function PageHeader({
  title,
  description,
  actions,
  children,
}: {
  title: ReactNode
  description?: ReactNode
  /** Buttons, on the right. */
  actions?: ReactNode
  /** Anything under the title line, such as a notice. */
  children?: ReactNode
}) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="flex min-w-0 flex-col gap-1">
          <h1 className="text-2xl font-semibold tracking-tight text-balance">{title}</h1>
          {description && <p className="max-w-3xl text-sm text-muted-foreground">{description}</p>}
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {children}
    </div>
  )
}

/** A titled part of a screen. The title carries a count when there is one. */
export function Section({
  id,
  title,
  count,
  description,
  actions,
  className,
  children,
  ...props
}: Omit<ComponentProps<'section'>, 'title'> & {
  /** The id of the heading, given when the section is labelled by it. */
  id?: string
  title: ReactNode
  count?: number
  description?: ReactNode
  actions?: ReactNode
}) {
  return (
    <section className={cn('flex flex-col gap-3', className)} {...props}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 className="flex items-center gap-2 text-base font-semibold tracking-tight">
            <span id={id}>{title}</span>
            {count !== undefined && (
              <small className="rounded-full bg-muted px-2 py-0.5 text-xs font-medium tabular text-muted-foreground">
                {count}
              </small>
            )}
          </h2>
          {description && <p className="max-w-3xl text-sm text-muted-foreground">{description}</p>}
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {children}
    </section>
  )
}
