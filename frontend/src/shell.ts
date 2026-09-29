import { useOutletContext } from 'react-router'

export const SCREENS = [
  { name: 'Summary', path: '/summary' },
  { name: 'Review', path: '/review' },
  { name: 'Vendors', path: '/vendors' },
  { name: 'Spend', path: '/spend' },
  { name: 'Questions', path: '/questions' },
  { name: 'Runs', path: '/runs' },
] as const

/** Shown only to administrators. */
export const PEOPLE_SCREEN = { name: 'People', path: '/people' } as const

/** What every screen is told by the shell around it. */
export type ShellContext = {
  /** The chosen collection month, or null when the ledger holds none. */
  month: string | null
  /** True until the collection months have been read from the ledger. */
  monthsLoading: boolean
  /** Called by a screen when the API says the person is no longer signed in. */
  onSignedOut: () => void
}

export function useShell(): ShellContext {
  return useOutletContext<ShellContext>()
}
