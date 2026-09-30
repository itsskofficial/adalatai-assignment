// Whether the sidebar shows icons alone, kept in this browser.

import { useCallback, useSyncExternalStore } from 'react'

const COLLAPSED_KEY = 'sidebar'
const listeners = new Set<() => void>()
// The choice made on this page, for a browser that refuses to keep it.
let remembered: boolean | null = null

function isCollapsed(): boolean {
  if (remembered !== null) return remembered
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
    remembered = next
    try {
      localStorage.setItem(COLLAPSED_KEY, next ? 'collapsed' : 'open')
    } catch {
      // Without storage the sidebar still folds until the page is left.
    }
    for (const listener of listeners) listener()
  }, [])
  return [collapsed, set]
}
