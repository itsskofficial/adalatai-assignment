import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { toast } from 'sonner'
import { afterEach, vi } from 'vitest'

// jsdom has no ResizeObserver, which the charts and the floating menus watch sizes with.
// Nothing changes size in a test, so one that never reports is enough. It is set plainly,
// not stubbed, so unstubbing the globals after each test leaves it in place.
if (typeof globalThis.ResizeObserver === 'undefined') {
  class QuietResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  globalThis.ResizeObserver = QuietResizeObserver as unknown as typeof ResizeObserver
}

afterEach(() => {
  cleanup()
  // Toasts are kept outside React, so one raised by a test would show in the next.
  toast.dismiss()
  sessionStorage.clear()
  localStorage.clear()
  vi.unstubAllGlobals()
})
