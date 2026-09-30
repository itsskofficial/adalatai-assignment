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

export type Screen = { name: string; path: string; icon: LucideIcon }

export const SCREENS = [
  { name: 'Summary', path: '/summary', icon: LayoutDashboardIcon },
  { name: 'Review', path: '/review', icon: ClipboardCheckIcon },
  { name: 'Vendors', path: '/vendors', icon: Building2Icon },
  { name: 'Spend', path: '/spend', icon: ChartColumnIcon },
  { name: 'Questions', path: '/questions', icon: MessageCircleQuestionMarkIcon },
  { name: 'Source accounts', path: '/source-accounts', icon: InboxIcon },
  { name: 'Runs', path: '/runs', icon: PlayIcon },
  { name: 'Settings', path: '/settings', icon: SettingsIcon },
] as const satisfies readonly Screen[]

/** Shown only to administrators. */
export const PEOPLE_SCREEN = { name: 'People', path: '/people', icon: UsersIcon } as const

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
