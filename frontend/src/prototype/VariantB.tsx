// PROTOTYPE, throwaway. Variant B: an editable table. The document opens in a drawer.

import { useState } from 'react'
import { FIELDS, FIELD_LABELS, filename } from './data'
import type { Queue } from './useQueue'

export const name = 'Table with drawer'

export function VariantB({ q }: { q: Queue }) {
  const [openId, setOpenId] = useState<string | null>(null)
  const [checked, setChecked] = useState<Record<string, boolean>>({})
  const open = q.items.find((i) => i.id === openId)
  const chosen = q.pending.filter((i) => checked[i.id])

  return (
    <div className="b-root">
      <header className="b-head">
        <h2>Needs review ({q.pending.length})</h2>
        <button className="primary" disabled={chosen.length === 0} onClick={() => chosen.forEach((i) => q.decide(i.id, 'approved'))}>
          Approve selected ({chosen.length})
        </button>
      </header>

      <table className="b-table">
        <thead>
          <tr>
            <th />
            {FIELDS.map((f) => (
              <th key={f}>{FIELD_LABELS[f]}</th>
            ))}
            <th>Why it was flagged</th>
            <th>Found in</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {q.items.map((i) => {
            const done = q.decisions[i.id]
            return (
              <tr key={i.id} className={done ? 'is-done' : ''}>
                <td>
                  <input
                    type="checkbox"
                    disabled={!!done}
                    checked={!!checked[i.id]}
                    onChange={(e) => setChecked({ ...checked, [i.id]: e.target.checked })}
                  />
                </td>
                {FIELDS.map((f) => (
                  <td key={f} className={i.flagged.includes(f) ? 'is-flagged' : ''}>
                    <input value={i.fields[f]} disabled={!!done} onChange={(e) => q.edit(i.id, f, e.target.value)} />
                  </td>
                ))}
                <td className="b-reason">{done ?? i.reasons.join('. ')}</td>
                <td>{i.sourceAccounts.length} account{i.sourceAccounts.length > 1 ? 's' : ''}</td>
                <td>
                  <button onClick={() => setOpenId(i.id)}>View</button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>

      {open && (
        <div className="b-drawer">
          <div className="b-drawer-head">
            <strong>{filename(open.fields)}</strong>
            <button onClick={() => setOpenId(null)}>Close</button>
          </div>
          <iframe title="Billing document" src={`${open.pdf}#toolbar=0&view=FitH`} />
          <div className="actions">
            <button className="primary" disabled={!!q.decisions[open.id]} onClick={() => q.decide(open.id, 'approved')}>
              Approve
            </button>
            <button disabled={!!q.decisions[open.id]} onClick={() => q.decide(open.id, 'rejected')}>
              Not a billing document
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
