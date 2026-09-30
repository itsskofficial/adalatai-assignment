// The light or dark theme: the system's unless the person chose one, kept in this browser.
//
// index.html sets the class on <html> before anything is drawn, from the same key, so the
// page never flashes light before turning dark.

import { useCallback, useEffect, useSyncExternalStore } from 'react'

export type Theme = 'light' | 'dark'

/** Where the person's choice is kept. index.html reads the same key. */
export const THEME_KEY = 'theme'

const listeners = new Set<() => void>()

function darkPreferred(): boolean {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches
  } catch {
    return false
  }
}

/** The theme the person chose in this browser, or null to follow the system. */
export function chosenTheme(): Theme | null {
  try {
    const stored = localStorage.getItem(THEME_KEY)
    return stored === 'light' || stored === 'dark' ? stored : null
  } catch {
    return null
  }
}

/** The theme in use: the chosen one, else the system's. */
export function currentTheme(): Theme {
  return chosenTheme() ?? (darkPreferred() ? 'dark' : 'light')
}

/** Puts the theme on <html>, where the stylesheet reads it. */
export function applyTheme(theme: Theme) {
  const root = document.documentElement
  root.classList.toggle('dark', theme === 'dark')
  root.style.colorScheme = theme
}

function announce() {
  for (const listener of listeners) listener()
}

/** Keeps the person's choice and applies it at once. */
export function chooseTheme(theme: Theme) {
  try {
    localStorage.setItem(THEME_KEY, theme)
  } catch {
    // A browser that keeps nothing still shows the theme until the page is left.
  }
  applyTheme(theme)
  announce()
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/** The theme in use, and a way to switch it. Follows the system until a choice is made. */
export function useTheme(): [Theme, (theme: Theme) => void] {
  const theme = useSyncExternalStore(subscribe, currentTheme, () => 'light' as const)

  // Until the person chooses, a change of the system's preference is followed.
  useEffect(() => {
    let media: MediaQueryList
    try {
      media = window.matchMedia('(prefers-color-scheme: dark)')
    } catch {
      return
    }
    const follow = () => {
      if (chosenTheme() === null) {
        applyTheme(currentTheme())
        announce()
      }
    }
    media.addEventListener('change', follow)
    return () => media.removeEventListener('change', follow)
  }, [])

  return [theme, useCallback((next: Theme) => chooseTheme(next), [])]
}
