// PROTOTYPE, throwaway. Variant C: one item at a time, only the doubtful fields, keyboard first.

import { useEffect } from 'react'
import { FIELDS, FIELD_LABELS, filename } from './data'
import type { Queue } from './useQueue'

export const name = 'One at a time'

export function VariantC({ q }: { q: Queue }) {
  const item = q.pending[0]
  const total = q.items.length
  const doneCount = total - q.pending.length

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement
      if (!item || t.matches('input, textarea, [contenteditable]')) return
      if (e.key === 'Enter') q.decide(item.id, 'approved')
      if (e.key === 'x') q.decide(item.id, 'rejected')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  if (!item) {
    return (
      <div className="c-root c-empty">
        <h2>Nothing left to review</h2>
        <p>{total} items decided. They will appear in the summary on the next run.</p>
        <button onClick={q.reset}>Start again</button>
      </div>
    )
  }

  const confident = FIELDS.filter((f) => !item.flagged.includes(f))

  return (
    <div className="c-root">
      <div className="c-progress">
        <div style={{ width: `${(doneCount / total) * 100}%` }} />
      </div>
      <p className="c-count">
        {doneCount + 1} of {total}
      </p>

      <div className="c-stage">
        <iframe title="Billing document" src={`${item.pdf}#toolbar=0&view=FitH`} />

        <section className="c-card">
          <h2>
            {item.fields.vendor}, {item.fields.total} {item.fields.currency}
          </h2>

          {item.reasons.map((r) => (
            <p key={r} className="c-reason">
              {r}
            </p>
          ))}

          <h3>Please check</h3>
          {item.flagged.map((f) => (
            <label key={f} className="is-flagged">
              {FIELD_LABELS[f]}
              <input autoFocus value={item.fields[f]} onChange={(e) => q.edit(item.id, f, e.target.value)} />
              {f === 'total' && item.usual && <small>{item.usual}</small>}
            </label>
          ))}

          <details>
            <summary>{confident.length} other fields look right</summary>
            {confident.map((f) => (
              <label key={f}>
                {FIELD_LABELS[f]}
                <input value={item.fields[f]} onChange={(e) => q.edit(item.id, f, e.target.value)} />
              </label>
            ))}
          </details>

          <p className="c-file">{filename(item.fields)}</p>

          <div className="actions">
            <button className="primary" onClick={() => q.decide(item.id, 'approved')}>
              Approve <kbd>Enter</kbd>
            </button>
            <button onClick={() => q.decide(item.id, 'rejected')}>
              Not a billing document <kbd>X</kbd>
            </button>
          </div>
        </section>
      </div>
    </div>
  )
}
