import { useEffect, useId, useState, type FormEvent } from 'react'
import {
  acceptVendor,
  addVendor,
  editVendor,
  ignoreVendor,
  NotSignedIn,
  removeVendor,
  restoreVendor,
  vendorList,
  type BillingCycle,
  type Vendor,
  type VendorFields,
  type VendorList,
} from './api'
import { formatAmount, monthName, MONTHS } from './format'
import { useShell } from './shell'
import type { Loaded } from './useLoaded'

/** Makes one change; answers with the reason it was refused, or null once it is made. */
type Change = (action: () => Promise<void>) => Promise<string | null>

const NEW_VENDOR: VendorFields = {
  vendor: '',
  source_account: null,
  billing_cycle: 'monthly',
  renewal_month: null,
  usual_amount: null,
  currency: null,
}

function fieldsOf(vendor: Vendor): VendorFields {
  return {
    vendor: vendor.vendor,
    source_account: vendor.source_account,
    billing_cycle: vendor.billing_cycle,
    renewal_month: vendor.renewal_month,
    usual_amount: vendor.usual_amount,
    currency: vendor.currency,
  }
}

function amountIn(amount: string | null, currency: string | null): string | null {
  if (amount === null) return null
  return currency ? `${formatAmount(amount)} ${currency}` : formatAmount(amount)
}

function billingCycle(vendor: Vendor): string {
  if (vendor.billing_cycle === 'annual' && vendor.renewal_month !== null) {
    return `Annual, renews in ${MONTHS[vendor.renewal_month - 1]}`
  }
  return vendor.billing_cycle === 'annual' ? 'Annual' : 'Monthly'
}

function listOfMonths(months: string[]): string {
  const names = months.map(monthName)
  if (names.length <= 1) return names.join('')
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

export function VendorsScreen() {
  const { onSignedOut } = useShell()
  const [list, setList] = useState<Loaded<VendorList>>({ status: 'loading' })
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)

  // Each change counts up, and the list is read again from the server after it.
  const [changes, setChanges] = useState(0)

  useEffect(() => {
    let current = true
    vendorList().then(
      (value) => {
        if (!current) return
        setList({ status: 'ready', value })
        setBusy(false)
      },
      (problem: unknown) => {
        if (!current) return
        setBusy(false)
        if (problem instanceof NotSignedIn) {
          onSignedOut()
          return
        }
        setList({
          status: 'problem',
          message: problem instanceof Error ? problem.message : String(problem),
        })
      },
    )
    return () => {
      current = false
    }
  }, [changes, onSignedOut])

  // Whatever happened, the list is read again rather than guessed.
  const change: Change = async (action) => {
    setBusy(true)
    setNotice(null)
    try {
      await action()
      return null
    } catch (problem) {
      if (problem instanceof NotSignedIn) {
        onSignedOut()
        return null
      }
      return problem instanceof Error ? problem.message : String(problem)
    } finally {
      // Buttons stay disabled until the list has been read again.
      setChanges((count) => count + 1)
    }
  }

  async function decide(action: () => Promise<void>) {
    setNotice(await change(action))
  }

  return (
    <main className="screen">
      <h1>Vendors</h1>
      <p className="lede">
        Expected vendors are checked for gaps in each run. Suggested vendors have billed the
        company but are not on the list yet. Changes take effect on the next run.
      </p>
      {notice && (
        <p className="reasons" role="alert">
          {notice}
        </p>
      )}
      {list.status === 'loading' && <p className="empty">Loading…</p>}
      {list.status === 'problem' && (
        <p className="reasons" role="alert">
          {list.message}
        </p>
      )}
      {list.status === 'ready' && (
        <>
          {list.value.suggested.length > 0 && (
            <SuggestedVendors
              vendors={list.value.suggested}
              busy={busy}
              change={change}
              decide={decide}
            />
          )}
          <ExpectedVendors
            list={list.value}
            busy={busy}
            change={change}
            decide={decide}
            adding={adding}
            onAdding={setAdding}
          />
          {list.value.ignored.length > 0 && (
            <IgnoredVendors vendors={list.value.ignored} busy={busy} decide={decide} />
          )}
        </>
      )}
    </main>
  )
}

type Decide = (action: () => Promise<void>) => Promise<void>

function SuggestedVendors({
  vendors,
  busy,
  change,
  decide,
}: {
  vendors: Vendor[]
  busy: boolean
  change: Change
  decide: Decide
}) {
  return (
    <section aria-label="Suggested vendors">
      <h2>
        Suggested vendors <small>{vendors.length}</small>
      </h2>
      <p className="hint">
        These vendors have billed the company. Accept one to expect it each billing cycle, or
        ignore it so it is not suggested again.
      </p>
      <div className="suggestions">
        {vendors.map((vendor) => (
          <Suggestion
            key={vendor.vendor}
            vendor={vendor}
            busy={busy}
            change={change}
            decide={decide}
          />
        ))}
      </div>
    </section>
  )
}

function Suggestion({
  vendor,
  busy,
  change,
  decide,
}: {
  vendor: Vendor
  busy: boolean
  change: Change
  decide: Decide
}) {
  const id = useId()
  const [withChanges, setWithChanges] = useState(false)
  const latest = amountIn(vendor.latest_amount, vendor.latest_currency)
  return (
    <article className="suggestion" aria-labelledby={id}>
      <h3 id={id}>{vendor.vendor}</h3>
      <ul className="facts">
        <li>
          Billed in{' '}
          {vendor.months_billed.length > 0 ? listOfMonths(vendor.months_billed) : 'no month yet'}
        </li>
        <li>Latest amount {latest ?? <span className="not-available">none</span>}</li>
        <li>Source account {vendor.source_account ?? <span className="not-available">any</span>}</li>
      </ul>
      {withChanges ? (
        <VendorForm
          label={`Accept ${vendor.vendor}`}
          initial={fieldsOf(vendor)}
          submitLabel="Accept"
          busy={busy}
          onSubmit={(fields) => change(() => acceptVendor(vendor.vendor, fields))}
          onDone={() => setWithChanges(false)}
        />
      ) : (
        <div className="actions">
          <button
            type="button"
            className="primary"
            disabled={busy}
            onClick={() => decide(() => acceptVendor(vendor.vendor))}
          >
            Accept
          </button>
          <button type="button" disabled={busy} onClick={() => setWithChanges(true)}>
            Accept with changes
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => decide(() => ignoreVendor(vendor.vendor))}
          >
            Ignore
          </button>
        </div>
      )}
    </article>
  )
}

function ExpectedVendors({
  list,
  busy,
  change,
  decide,
  adding,
  onAdding,
}: {
  list: VendorList
  busy: boolean
  change: Change
  decide: Decide
  adding: boolean
  onAdding: (adding: boolean) => void
}) {
  const [editing, setEditing] = useState<string | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const latestMonth = list.latest_month ? monthName(list.latest_month) : null
  return (
    <section aria-label="Expected vendors">
      <div className="section-head">
        <h2>
          Expected vendors <small>{list.expected.length}</small>
        </h2>
        {!adding && (
          <button type="button" disabled={busy} onClick={() => onAdding(true)}>
            Add vendor
          </button>
        )}
      </div>
      {adding && (
        <div className="panel">
          <VendorForm
            label="Add vendor"
            initial={NEW_VENDOR}
            submitLabel="Add"
            busy={busy}
            onSubmit={(fields) => change(() => addVendor(fields))}
            onDone={() => onAdding(false)}
          />
        </div>
      )}
      {list.expected.length === 0 ? (
        <p className="empty">No vendor is expected yet.</p>
      ) : (
        <table aria-label="Expected vendors">
          <thead>
            <tr>
              <th scope="col">Vendor</th>
              <th scope="col">Source account</th>
              <th scope="col">Billing cycle</th>
              <th scope="col" className="amount">
                Usual amount
              </th>
              <th scope="col" className="amount">
                Latest amount
              </th>
              <th scope="col">{latestMonth ?? 'Latest month'}</th>
              <th scope="col">
                <span className="visually-hidden">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {list.expected.map((vendor) => (
              <ExpectedVendorRows
                key={vendor.vendor}
                vendor={vendor}
                latestMonth={latestMonth}
                busy={busy}
                editing={editing === vendor.vendor}
                removing={removing === vendor.vendor}
                onEditing={(on) => setEditing(on ? vendor.vendor : null)}
                onRemoving={(on) => setRemoving(on ? vendor.vendor : null)}
                change={change}
                decide={decide}
              />
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

function GapMark({ vendor, latestMonth }: { vendor: Vendor; latestMonth: string | null }) {
  if (vendor.gap === null || latestMonth === null) return null
  if (vendor.gap === 'unknown') {
    return (
      <span className="tag tag-gap-unknown" title="A source account could not be read">
        Gap unknown in {latestMonth}
      </span>
    )
  }
  return (
    <span className="tag tag-gap" title="No billing document was collected">
      Gap in {latestMonth}
    </span>
  )
}

function ExpectedVendorRows({
  vendor,
  latestMonth,
  busy,
  editing,
  removing,
  onEditing,
  onRemoving,
  change,
  decide,
}: {
  vendor: Vendor
  latestMonth: string | null
  busy: boolean
  editing: boolean
  removing: boolean
  onEditing: (on: boolean) => void
  onRemoving: (on: boolean) => void
  change: Change
  decide: Decide
}) {
  const usual = amountIn(vendor.usual_amount, vendor.currency)
  const latest = amountIn(vendor.latest_amount, vendor.latest_currency)
  return (
    <>
      <tr className={vendor.gap ? 'has-gap' : undefined}>
        <th scope="row">{vendor.vendor}</th>
        <td>{vendor.source_account ?? <span className="not-available">Any</span>}</td>
        <td>{billingCycle(vendor)}</td>
        <td className="amount">
          {usual ? <span className="number">{usual}</span> : <span className="not-available">—</span>}
        </td>
        <td className="amount">
          {latest ? (
            <span className="number">{latest}</span>
          ) : (
            <span className="not-available">—</span>
          )}
        </td>
        <td>
          <GapMark vendor={vendor} latestMonth={latestMonth} />
        </td>
        <td className="actions">
          <button
            type="button"
            disabled={busy || editing}
            onClick={() => {
              onRemoving(false)
              onEditing(true)
            }}
          >
            Edit
          </button>
          <button
            type="button"
            disabled={busy || removing}
            onClick={() => {
              onEditing(false)
              onRemoving(true)
            }}
          >
            Remove
          </button>
        </td>
      </tr>
      {removing && (
        <tr className="confirming">
          <td colSpan={7}>
            <div className="confirm">
              <span>Remove {vendor.vendor} from the vendor list?</span>
              <button
                type="button"
                className="danger"
                aria-label={`Yes, remove ${vendor.vendor}`}
                disabled={busy}
                onClick={async () => {
                  await decide(() => removeVendor(vendor.vendor))
                  onRemoving(false)
                }}
              >
                Yes, remove {vendor.vendor}
              </button>
              <button type="button" disabled={busy} onClick={() => onRemoving(false)}>
                Keep
              </button>
            </div>
          </td>
        </tr>
      )}
      {editing && (
        <tr className="editing">
          <td colSpan={7}>
            <VendorForm
              label={`Edit ${vendor.vendor}`}
              initial={fieldsOf(vendor)}
              submitLabel="Save"
              busy={busy}
              onSubmit={(fields) => change(() => editVendor(vendor.vendor, fields))}
              onDone={() => onEditing(false)}
            />
          </td>
        </tr>
      )}
    </>
  )
}

function IgnoredVendors({
  vendors,
  busy,
  decide,
}: {
  vendors: Vendor[]
  busy: boolean
  decide: Decide
}) {
  return (
    <section>
      <details className="ignored">
        <summary>Ignored vendors ({vendors.length})</summary>
        <p className="hint">
          Ignored vendors are not suggested again. Restore one to decide on it afresh.
        </p>
        <ul>
          {vendors.map((vendor) => (
            <li key={vendor.vendor}>
              <span>
                <strong>{vendor.vendor}</strong>
                {vendor.months_billed.length > 0 && (
                  <small> billed in {listOfMonths(vendor.months_billed)}</small>
                )}
              </span>
              <button
                type="button"
                disabled={busy}
                onClick={() => decide(() => restoreVendor(vendor.vendor))}
              >
                Restore
              </button>
            </li>
          ))}
        </ul>
      </details>
    </section>
  )
}

type Draft = {
  vendor: string
  source_account: string
  billing_cycle: BillingCycle
  renewal_month: string
  usual_amount: string
  currency: string
}

function draftOf(fields: VendorFields): Draft {
  return {
    vendor: fields.vendor,
    source_account: fields.source_account ?? '',
    billing_cycle: fields.billing_cycle,
    renewal_month: fields.renewal_month === null ? '' : String(fields.renewal_month),
    usual_amount: fields.usual_amount ?? '',
    currency: fields.currency ?? '',
  }
}

function fieldsFrom(draft: Draft): VendorFields {
  const blankIsNull = (text: string) => (text.trim() ? text.trim() : null)
  return {
    vendor: draft.vendor,
    source_account: blankIsNull(draft.source_account),
    billing_cycle: draft.billing_cycle,
    renewal_month:
      draft.billing_cycle === 'annual' && draft.renewal_month ? Number(draft.renewal_month) : null,
    usual_amount: blankIsNull(draft.usual_amount),
    currency: blankIsNull(draft.currency),
  }
}

/** A vendor's fields. The server checks them; its reason for refusing is shown here. */
function VendorForm({
  label,
  initial,
  submitLabel,
  busy,
  onSubmit,
  onDone,
}: {
  label: string
  initial: VendorFields
  submitLabel: string
  busy: boolean
  onSubmit: (fields: VendorFields) => Promise<string | null>
  onDone: () => void
}) {
  const id = useId()
  const [draft, setDraft] = useState<Draft>(() => draftOf(initial))
  const [refused, setRefused] = useState<string | null>(null)

  function set<K extends keyof Draft>(key: K, value: Draft[K]) {
    setDraft((earlier) => ({ ...earlier, [key]: value }))
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    const reason = await onSubmit(fieldsFrom(draft))
    if (reason === null) onDone()
    else setRefused(reason)
  }

  return (
    <form className="vendor-form" aria-label={label} onSubmit={submit}>
      <div className="fields">
        <label htmlFor={`${id}-vendor`}>Vendor</label>
        <input
          id={`${id}-vendor`}
          type="text"
          value={draft.vendor}
          onChange={(event) => set('vendor', event.target.value)}
        />
        <label htmlFor={`${id}-account`}>Source account</label>
        <input
          id={`${id}-account`}
          type="text"
          value={draft.source_account}
          placeholder="Any"
          onChange={(event) => set('source_account', event.target.value)}
        />
        <label htmlFor={`${id}-cycle`}>Billing cycle</label>
        <select
          id={`${id}-cycle`}
          value={draft.billing_cycle}
          onChange={(event) => set('billing_cycle', event.target.value as BillingCycle)}
        >
          <option value="monthly">Monthly</option>
          <option value="annual">Annual</option>
        </select>
        {draft.billing_cycle === 'annual' && (
          <>
            <label htmlFor={`${id}-renewal`}>Renewal month</label>
            <select
              id={`${id}-renewal`}
              value={draft.renewal_month}
              onChange={(event) => set('renewal_month', event.target.value)}
            >
              <option value="">Choose a month</option>
              {MONTHS.map((name, index) => (
                <option key={name} value={String(index + 1)}>
                  {name}
                </option>
              ))}
            </select>
          </>
        )}
        <label htmlFor={`${id}-amount`}>Usual amount</label>
        <input
          id={`${id}-amount`}
          type="text"
          inputMode="decimal"
          value={draft.usual_amount}
          onChange={(event) => set('usual_amount', event.target.value)}
        />
        <label htmlFor={`${id}-currency`}>Currency</label>
        <input
          id={`${id}-currency`}
          type="text"
          className="currency"
          value={draft.currency}
          placeholder="USD"
          onChange={(event) => set('currency', event.target.value)}
        />
      </div>
      {refused && (
        <p className="reasons" role="alert">
          {refused}
        </p>
      )}
      <div className="actions">
        <button type="submit" className="primary" disabled={busy}>
          {submitLabel}
        </button>
        <button type="button" disabled={busy} onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  )
}
