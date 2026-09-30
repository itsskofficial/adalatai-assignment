// Whether the sidebar shows icons alone, kept in this browser.

import { useCallback, useSyncExternalStore } from 'react'

const COLLAPSED_KEY = 'sidebar'
const listeners = new Set<() => void>()

function isCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === 'collapsed'
  } catch {
    return false
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/** Whether the sidebar shows icons alone. The choice is kept in this browser. */
export function useSidebarCollapsed(): [boolean, (collapsed: boolean) => void] {
  const collapsed = useSyncExternalStore(subscribe, isCollapsed, () => false)
  const set = useCallback((next: boolean) => {
    try {
      localStorage.setItem(COLLAPSED_KEY, next ? 'collapsed' : 'open')
    } catch {
      // Without storage the sidebar still folds until the page is left.
    }
    for (const listener of listeners) listener()
  }, [])
  return [collapsed, set]
}
