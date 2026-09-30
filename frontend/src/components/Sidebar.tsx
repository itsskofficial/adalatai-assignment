import { PanelLeftIcon, ReceiptTextIcon } from 'lucide-react'
import { NavLink } from 'react-router'
import type { Role } from '@/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { PEOPLE_SCREEN, SCREENS, type Screen } from '@/shell'

const ROLE_NAMES: Record<Role, string> = { member: 'Member', administrator: 'Administrator' }

export type SidebarProps = {
  email: string
  role: Role
  /** The search part of each screen's address, carrying the chosen collection month. */
  query: string
  collapsed?: boolean
  onCollapse?: (collapsed: boolean) => void
  /** Called when a screen is chosen, so a sheet holding the sidebar can close. */
  onNavigate?: () => void
  className?: string
}

/**
 * The screens, with the person signed in at the foot. Shown at the side on a wide screen and
 * in a sheet on a narrow one; the same content either way.
 */
export function Sidebar({
  email,
  role,
  query,
  collapsed = false,
  onCollapse,
  onNavigate,
  className,
}: SidebarProps) {
  // Only administrators manage who may sign in, so only they are shown the way there.
  const screens: readonly Screen[] = [
    ...SCREENS,
    ...(role === 'administrator' ? [PEOPLE_SCREEN] : []),
  ]
  return (
    <div
      data-collapsed={collapsed ? 'true' : undefined}
      className={cn(
        'flex h-full flex-col bg-sidebar text-sidebar-foreground',
        collapsed ? 'w-16' : 'w-64',
        className,
      )}
    >
      <div
        className={cn(
          'flex h-14 shrink-0 items-center gap-2 border-b border-sidebar-border px-3',
          collapsed && 'justify-center px-0',
        )}
      >
        <span
          className={cn(
            'flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground',
          )}
        >
          <ReceiptTextIcon aria-hidden="true" className="size-4" />
        </span>
        {!collapsed && (
          <strong className="min-w-0 flex-1 truncate text-sm font-semibold tracking-tight">
            Invoice Collection
          </strong>
        )}
        {onCollapse && !collapsed && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Collapse the sidebar"
                className="text-muted-foreground"
                onClick={() => onCollapse(true)}
              >
                <PanelLeftIcon />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="right">Collapse the sidebar</TooltipContent>
          </Tooltip>
        )}
      </div>

      {onCollapse && collapsed && (
        <div className="flex justify-center border-b border-sidebar-border py-2">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Expand the sidebar"
                className="text-muted-foreground"
                onClick={() => onCollapse(false)}
              >
                <PanelLeftIcon />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="right">Expand the sidebar</TooltipContent>
          </Tooltip>
        </div>
      )}

      <nav aria-label="Screens" className="flex flex-1 flex-col gap-0.5 overflow-y-auto p-2">
        {screens.map((screen) => {
          const link = (
            <NavLink
              key={screen.path}
              to={{ pathname: screen.path, search: query }}
              onClick={onNavigate}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm font-medium text-muted-foreground transition-colors outline-none hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-[3px] focus-visible:ring-sidebar-ring/40',
                  collapsed && 'justify-center px-0',
                  isActive &&
                    'is-active bg-sidebar-accent text-sidebar-accent-foreground shadow-[inset_2px_0_0_var(--sidebar-primary)]',
                )
              }
            >
              <screen.icon aria-hidden="true" className="size-4 shrink-0" />
              <span className={cn('truncate', collapsed && 'sr-only')}>{screen.name}</span>
            </NavLink>
          )
          if (!collapsed) return link
          return (
            <Tooltip key={screen.path}>
              <TooltipTrigger asChild>{link}</TooltipTrigger>
              <TooltipContent side="right">{screen.name}</TooltipContent>
            </Tooltip>
          )
        })}
      </nav>

      <div
        className={cn(
          'flex items-center gap-2.5 border-t border-sidebar-border p-3',
          collapsed && 'justify-center p-2',
        )}
      >
        <span
          aria-hidden="true"
          className="flex size-8 shrink-0 items-center justify-center rounded-full bg-muted text-xs font-semibold uppercase text-muted-foreground"
        >
          {email.charAt(0)}
        </span>
        <div className={cn('flex min-w-0 flex-col gap-0.5', collapsed && 'sr-only')}>
          <span className="truncate text-xs font-medium" title={email}>
            {email}
          </span>
          <Badge variant={role === 'administrator' ? 'info' : 'muted'} className="w-fit">
            {ROLE_NAMES[role]}
          </Badge>
        </div>
      </div>
    </div>
  )
}
