import {
  Building2Icon,
  ChartColumnIcon,
  ClipboardCheckIcon,
  InboxIcon,
  LayoutDashboardIcon,
  MessageCircleQuestionMarkIcon,
  PlayIcon,
  SettingsIcon,
  UsersIcon,
  type LucideIcon,
} from 'lucide-react'
import { useOutletContext } from 'react-router'

export type Screen = { name: string; path: string; icon: LucideIcon; monthly: boolean }

/** Screens marked monthly show one collection month, chosen in the top bar; the rest
 * show the company as a whole and have no month. */
export const SCREENS = [
  { name: 'Summary', path: '/summary', icon: LayoutDashboardIcon, monthly: true },
  { name: 'Review', path: '/review', icon: ClipboardCheckIcon, monthly: true },
  { name: 'Vendors', path: '/vendors', icon: Building2Icon, monthly: false },
  { name: 'Spend', path: '/spend', icon: ChartColumnIcon, monthly: false },
  { name: 'Questions', path: '/questions', icon: MessageCircleQuestionMarkIcon, monthly: false },
  { name: 'Source accounts', path: '/source-accounts', icon: InboxIcon, monthly: false },
  { name: 'Runs', path: '/runs', icon: PlayIcon, monthly: true },
  { name: 'Settings', path: '/settings', icon: SettingsIcon, monthly: false },
] as const satisfies readonly Screen[]

/** Whether the screen at a path shows one collection month. */
export function isMonthly(pathname: string): boolean {
  return SCREENS.some((screen) => screen.monthly && pathname.startsWith(screen.path))
}

/** Shown only to administrators. */
export const PEOPLE_SCREEN = {
  name: 'People',
  path: '/people',
  icon: UsersIcon,
  monthly: false,
} as const

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
