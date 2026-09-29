// PROTOTYPE, throwaway. Variant A: three panes. Queue, document, fields.

import { FIELDS, FIELD_LABELS, filename } from './data'
import type { Queue } from './useQueue'

export const name = 'Three panes'

export function VariantA({ q }: { q: Queue }) {
  const item = q.selected
  const done = q.decisions[item.id]

  return (
    <div className="a-root">
      <aside className="a-queue">
        <h2>Needs review ({q.pending.length})</h2>
        {q.items.map((i) => (
          <button
            key={i.id}
            className={`a-row ${i.id === item.id ? 'is-selected' : ''} ${q.decisions[i.id] ? 'is-done' : ''}`}
            onClick={() => q.setSelectedId(i.id)}
          >
            <strong>{i.fields.vendor}</strong>
            <span>
              {i.fields.total} {i.fields.currency}
            </span>
            <small>{q.decisions[i.id] ?? i.reasons[0]}</small>
          </button>
        ))}
      </aside>

      <main className="a-doc">
        <iframe title="Billing document" src={`${item.pdf}#toolbar=0&view=FitH`} />
      </main>

      <aside className="a-fields">
        <h2>Extracted fields</h2>
        <ul className="reasons">
          {item.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
        {FIELDS.map((f) => (
          <label key={f} className={item.flagged.includes(f) ? 'is-flagged' : ''}>
            {FIELD_LABELS[f]}
            <input value={item.fields[f]} onChange={(e) => q.edit(item.id, f, e.target.value)} />
            {f === 'total' && item.usual && <small>{item.usual}</small>}
          </label>
        ))}
        <dl>
          <dt>Will be saved as</dt>
          <dd>{filename(item.fields)}</dd>
          <dt>Found in</dt>
          <dd>{item.sourceAccounts.join(', ')}</dd>
          <dt>Source email</dt>
          <dd>
            {item.from}
            <br />
            {item.subject}
          </dd>
        </dl>
        <div className="actions">
          <button className="primary" disabled={!!done} onClick={() => q.decide(item.id, 'approved')}>
            Approve
          </button>
          <button disabled={!!done} onClick={() => q.decide(item.id, 'rejected')}>
            Not a billing document
          </button>
        </div>
      </aside>
    </div>
  )
}
