import { useEffect } from 'react'
import { NavLink, Outlet, useSearchParams } from 'react-router'
import { collectionMonths, type Role } from './api'
import { isCollectionMonth, monthName } from './format'
import { PEOPLE_SCREEN, SCREENS, type ShellContext } from './shell'
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
    <>
      <header className="app-bar">
        <strong>Invoice Collection</strong>
        <label className="month-picker">
          <span>Collection month</span>
          <select
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
          </select>
        </label>
        <nav aria-label="Screens">
          {/* Only administrators manage who may sign in, so only they are shown the way there. */}
          {[...SCREENS, ...(role === 'administrator' ? [PEOPLE_SCREEN] : [])].map((screen) => (
            <NavLink
              key={screen.path}
              to={{ pathname: screen.path, search: query }}
              className={({ isActive }) => (isActive ? 'is-active' : undefined)}
            >
              {screen.name}
            </NavLink>
          ))}
        </nav>
        <span className="person">{email}</span>
        <button type="button" onClick={onSignOut}>
          Sign out
        </button>
      </header>

      {months.status === 'problem' && (
        <p className="page-note problem" role="alert">
          {months.message}
        </p>
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
    </>
  )
}
