// PROTOTYPE, throwaway. In-memory queue state shared by every variant.

import { useState } from 'react'
import { QUEUE, type Field, type PendingItem } from './data'

export type Decision = 'approved' | 'rejected'

export function useQueue() {
  const [items, setItems] = useState<PendingItem[]>(QUEUE)
  const [decisions, setDecisions] = useState<Record<string, Decision>>({})
  const [selectedId, setSelectedId] = useState<string>(QUEUE[0].id)

  const pending = items.filter((i) => !decisions[i.id])
  const selected = items.find((i) => i.id === selectedId) ?? pending[0] ?? items[0]

  function edit(id: string, field: Field, value: string) {
    setItems((prev) => prev.map((i) => (i.id === id ? { ...i, fields: { ...i.fields, [field]: value } } : i)))
  }

  function decide(id: string, decision: Decision) {
    setDecisions((prev) => ({ ...prev, [id]: decision }))
    const next = pending.find((i) => i.id !== id)
    if (next) setSelectedId(next.id)
  }

  function reset() {
    setItems(QUEUE)
    setDecisions({})
    setSelectedId(QUEUE[0].id)
  }

  return { items, pending, decisions, selected, setSelectedId, edit, decide, reset }
}

export type Queue = ReturnType<typeof useQueue>
