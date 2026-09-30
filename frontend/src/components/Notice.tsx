import { cva, type VariantProps } from 'class-variance-authority'
import {
  CircleAlertIcon,
  CircleCheckIcon,
  InfoIcon,
  TriangleAlertIcon,
  type LucideIcon,
} from 'lucide-react'
import type { ComponentProps, ReactNode } from 'react'
import { cn } from '@/lib/utils'

const noticeVariants = cva(
  'flex w-full items-start gap-3 rounded-lg border px-3.5 py-3 text-sm [&_a]:font-medium [&_a]:underline [&_a]:underline-offset-4',
  {
    variants: {
      tone: {
        warning: 'border-warning/30 bg-warning-soft text-warning [&_strong]:text-warning',
        destructive:
          'border-destructive/30 bg-destructive-soft text-destructive [&_strong]:text-destructive',
        info: 'border-info/30 bg-info-soft text-foreground',
        success: 'border-success/30 bg-success-soft text-foreground',
        muted: 'border-border bg-muted/60 text-muted-foreground',
      },
    },
    defaultVariants: { tone: 'warning' },
  },
)

const ICONS: Record<NonNullable<VariantProps<typeof noticeVariants>['tone']>, LucideIcon> = {
  warning: TriangleAlertIcon,
  destructive: CircleAlertIcon,
  info: InfoIcon,
  success: CircleCheckIcon,
  muted: InfoIcon,
}

type NoticeProps = VariantProps<typeof noticeVariants> & {
  className?: string
  children: ReactNode
}

/**
 * Something that went wrong, or a reason a change was refused, said where it happened.
 * Announced at once.
 */
export function Problem({
  tone = 'warning',
  className,
  children,
  ...props
}: NoticeProps & Omit<ComponentProps<'div'>, 'children'>) {
  const Icon = ICONS[tone ?? 'warning']
  return (
    <div role="alert" className={cn(noticeVariants({ tone }), className)} {...props}>
      <Icon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
      <div className="min-w-0 flex-1 [&>p]:m-0 [&>p+p]:mt-1">{children}</div>
    </div>
  )
}

/** How an action ended, said in place and announced politely. */
export function Status({
  tone = 'info',
  className,
  children,
  ...props
}: NoticeProps & Omit<ComponentProps<'output'>, 'children'>) {
  const Icon = ICONS[tone ?? 'info']
  return (
    <output className={cn(noticeVariants({ tone }), className)} {...props}>
      <Icon
        aria-hidden="true"
        className={cn('mt-0.5 size-4 shrink-0', tone === 'success' && 'text-success', tone === 'info' && 'text-info')}
      />
      <span className="min-w-0 flex-1">{children}</span>
    </output>
  )
}

/** A quiet line of guidance under a title or a field. */
export function Hint({ className, ...props }: ComponentProps<'p'>) {
  return <p className={cn('text-sm text-muted-foreground', className)} {...props} />
}
