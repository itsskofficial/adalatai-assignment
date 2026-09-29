// PROTOTYPE, throwaway. Floating bar that cycles between variants.

import { useEffect } from 'react'

type Props = {
  variants: { key: string; name: string }[]
  current: string
  onChange: (key: string) => void
}

export function PrototypeSwitcher({ variants, current, onChange }: Props) {
  const index = Math.max(0, variants.findIndex((v) => v.key === current))
  const step = (by: number) => onChange(variants[(index + by + variants.length) % variants.length].key)

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.target as HTMLElement).matches('input, textarea, [contenteditable]')) return
      if (e.key === 'ArrowLeft') step(-1)
      if (e.key === 'ArrowRight') step(1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  if (import.meta.env.PROD) return null

  return (
    <div className="switcher">
      <button aria-label="Previous variant" onClick={() => step(-1)}>
        ←
      </button>
      <span>
        {variants[index].key} ({variants[index].name})
      </span>
      <button aria-label="Next variant" onClick={() => step(1)}>
        →
      </button>
    </div>
  )
}
