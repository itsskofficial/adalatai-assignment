import { CalendarIcon, LogOutIcon, MenuIcon } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { Outlet, useSearchParams } from 'react-router'
import { collectionMonths, type Role } from './api'
import { Problem } from './components/Notice'
import { Sidebar } from './components/Sidebar'
import { useSidebarCollapsed } from './sidebarState'
import { ThemeToggle } from './components/ThemeToggle'
import { Button } from './components/ui/button'
import { Select } from './components/ui/select'
import { Sheet, SheetContent, SheetDescription, SheetTitle, SheetTrigger } from './components/ui/sheet'
import { Toaster } from './components/ui/sonner'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from './components/ui/tooltip'
import { isCollectionMonth, monthName } from './format'
import { cn } from './lib/utils'
import { type ShellContext } from './shell'
import { useLoaded } from './useLoaded'

type Props = {
  email: string
  role: Role
  onSignOut: () => void
  onSignedOut: () => void
}

export function AppShell({ email, role, onSignOut, onSignedOut }: Props) {
  const [search, setSearch] = useSearchParams()
  const months = useLoaded('months', collectionMonths)
  const [collapsed, setCollapsed] = useSidebarCollapsed()
  const [menuOpen, setMenuOpen] = useState(false)
  const monthId = useId()

  const notSignedIn = months.status === 'not-signed-in'
  useEffect(() => {
    if (notSignedIn) onSignedOut()
  }, [notSignedIn, onSignedOut])

  const known = months.status === 'ready' ? months.value : []
  const month = search.get('month') ?? known[0] ?? null
  // A month asked for by address is offered even when the ledger holds nothing for it.
  const offered = month !== null && !known.includes(month) ? [month, ...known] : known
  const query = search.has('month') ? `?month=${encodeURIComponent(month ?? '')}` : ''

  return (
    <TooltipProvider>
      <div className="flex min-h-svh w-full">
        <aside
          className={cn(
            'sticky top-0 hidden h-svh shrink-0 border-r border-sidebar-border md:block',
            collapsed ? 'w-16' : 'w-64',
          )}
        >
          <Sidebar
            email={email}
            role={role}
            query={query}
            collapsed={collapsed}
            onCollapse={setCollapsed}
          />
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-20 flex h-14 shrink-0 items-center gap-2 border-b bg-background/85 px-3 backdrop-blur md:px-6">
            <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
              <SheetTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  className="md:hidden"
                  aria-label="Open the menu"
                >
                  <MenuIcon />
                </Button>
              </SheetTrigger>
              <SheetContent side="left" className="w-72 gap-0 p-0" showCloseButton>
                <SheetTitle className="sr-only">Menu</SheetTitle>
                <SheetDescription className="sr-only">The screens of the dashboard.</SheetDescription>
                <Sidebar
                  email={email}
                  role={role}
                  query={query}
                  onNavigate={() => setMenuOpen(false)}
                  className="w-full"
                />
              </SheetContent>
            </Sheet>

            <label
              htmlFor={monthId}
              className="flex min-w-0 items-center gap-2 text-sm"
            >
              <CalendarIcon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
              <span className="sr-only text-xs text-muted-foreground sm:not-sr-only">
                Collection month
              </span>
              <Select
                id={monthId}
                wrapperClassName="w-auto"
                value={month ?? ''}
                disabled={offered.length === 0}
                onChange={(event) => setSearch({ month: event.target.value })}
              >
                {offered.length === 0 && <option value="">No months yet</option>}
                {offered.map((each) => (
                  <option key={each} value={each}>
                    {isCollectionMonth(each) ? monthName(each) : each}
                  </option>
                ))}
              </Select>
            </label>

            <div className="ml-auto flex items-center gap-1">
              <ThemeToggle />
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button variant="outline" size="sm" aria-label="Sign out" onClick={onSignOut}>
                    <LogOutIcon />
                    <span className="hidden sm:inline">Sign out</span>
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Sign out of the dashboard</TooltipContent>
              </Tooltip>
            </div>
          </header>

          {months.status === 'problem' && (
            <div className="px-4 pt-4 md:px-8">
              <Problem>{months.message}</Problem>
            </div>
          )}
          <Outlet
            context={
              {
                month,
                monthsLoading: months.status === 'loading',
                onSignedOut,
              } satisfies ShellContext
            }
          />
        </div>
      </div>
      <Toaster />
    </TooltipProvider>
  )
}
