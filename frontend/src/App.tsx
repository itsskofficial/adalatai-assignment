// PROTOTYPE, throwaway.
// Three variants of the review screen, switchable via ?variant=A|B|C.
// Question: how should a person review a billing document that needs review?

import { useState } from 'react'
import './prototype/prototype.css'
import { PrototypeSwitcher } from './prototype/PrototypeSwitcher'
import { useQueue } from './prototype/useQueue'
import { VariantA, name as nameA } from './prototype/VariantA'
import { VariantB, name as nameB } from './prototype/VariantB'
import { VariantC, name as nameC } from './prototype/VariantC'

const VARIANTS = [
  { key: 'A', name: nameA },
  { key: 'B', name: nameB },
  { key: 'C', name: nameC },
]

function App() {
  const [variant, setVariant] = useState(() => new URLSearchParams(location.search).get('variant') ?? 'A')
  const q = useQueue()

  function change(key: string) {
    const url = new URL(location.href)
    url.searchParams.set('variant', key)
    history.replaceState(null, '', url)
    setVariant(key)
  }

  return (
    <>
      <header className="app-bar">
        <strong>Invoice Collection</strong>
        <span>August 2026</span>
        <nav>
          <a>Summary</a>
          <a className="is-active">Review</a>
          <a>Vendors</a>
          <a>Spend</a>
        </nav>
        <button onClick={q.reset}>Reset prototype</button>
      </header>

      {variant === 'A' && <VariantA q={q} />}
      {variant === 'B' && <VariantB q={q} />}
      {variant === 'C' && <VariantC q={q} />}

      <PrototypeSwitcher variants={VARIANTS} current={variant} onChange={change} />
    </>
  )
}

export default App
