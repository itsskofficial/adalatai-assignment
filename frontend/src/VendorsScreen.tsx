import {
  Building2Icon,
  CheckIcon,
  ChevronRightIcon,
  PencilIcon,
  PlusIcon,
  SparklesIcon,
  Trash2Icon,
} from 'lucide-react'
import { useEffect, useId, useState, type FormEvent } from 'react'
import { toast } from 'sonner'
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
import { NotAvailable } from './components/Amount'
import { ConfirmDialog } from './components/ConfirmDialog'
import { EmptyState } from './components/EmptyState'
import { Loading, TableSkeleton } from './components/Loading'
import { Hint, Problem } from './components/Notice'
import { PageHeader, Screen, Section } from './components/Screen'
import { Badge } from './components/ui/badge'
import { Button } from './components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from './components/ui/dialog'
import { Input } from './components/ui/input'
import { Label } from './components/ui/label'
import { Select } from './components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableRowHead,
} from './components/ui/table'
import { formatAmount, monthName, MONTHS } from './format'
import { cn } from './lib/utils'
import { useShell } from './shell'
import type { Loaded } from './useLoaded'

/** Makes one change; answers with the reason it was refused, or null once it is made. */
type Change = (action: () => Promise<void>, done?: string) => Promise<string | null>

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
  const change: Change = async (action, done) => {
    setBusy(true)
    setNotice(null)
    try {
      await action()
      if (done) toast.success(done)
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

  async function decide(action: () => Promise<void>, done?: string) {
    setNotice(await change(action, done))
  }

  return (
    <Screen>
      <PageHeader
        title="Vendors"
        description="Expected vendors are checked for gaps in each run. Suggested vendors have billed the company but are not on the list yet. Changes take effect on the next run."
        actions={
          <Button disabled={busy || list.status !== 'ready'} onClick={() => setAdding(true)}>
            <PlusIcon />
            Add vendor
          </Button>
        }
      >
        {notice && <Problem>{notice}</Problem>}
      </PageHeader>
      <VendorDialog
        open={adding}
        onOpenChange={setAdding}
        label="Add vendor"
        description="A vendor expected to bill the company. Its gaps are checked from the next run."
        initial={NEW_VENDOR}
        submitLabel="Add"
        busy={busy}
        onSubmit={(fields) => change(() => addVendor(fields), `Added ${fields.vendor}`)}
      />
      {list.status === 'loading' && (
        <Loading>
          <TableSkeleton columns={6} />
        </Loading>
      )}
      {list.status === 'problem' && <Problem>{list.message}</Problem>}
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
          <ExpectedVendors list={list.value} busy={busy} change={change} decide={decide} />
          {list.value.ignored.length > 0 && (
            <IgnoredVendors vendors={list.value.ignored} busy={busy} decide={decide} />
          )}
        </>
      )}
    </Screen>
  )
}

type Decide = (action: () => Promise<void>, done?: string) => Promise<void>

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
    <Section
      aria-label="Suggested vendors"
      title="Suggested vendors"
      count={vendors.length}
      description="These vendors have billed the company. Accept one to expect it each billing cycle, or ignore it so it is not suggested again."
    >
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
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
    </Section>
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
    <article
      className="flex flex-col gap-3 rounded-xl border border-l-4 border-l-primary bg-card p-4 shadow-xs"
      aria-labelledby={id}
    >
      <div className="flex items-start justify-between gap-2">
        <h3 id={id} className="text-base font-semibold">
          {vendor.vendor}
        </h3>
        <Badge variant="info">
          <SparklesIcon aria-hidden="true" />
          Suggested
        </Badge>
      </div>
      <ul className="m-0 flex list-none flex-col gap-1 p-0 text-sm text-muted-foreground">
        <li>
          Billed in{' '}
          {vendor.months_billed.length > 0 ? listOfMonths(vendor.months_billed) : 'no month yet'}
        </li>
        <li>
          Latest amount{' '}
          {latest ? (
            <span className="tabular text-foreground">{latest}</span>
          ) : (
            <NotAvailable>none</NotAvailable>
          )}
        </li>
        <li>Source account {vendor.source_account ?? <NotAvailable>any</NotAvailable>}</li>
      </ul>
      {withChanges ? (
        <VendorForm
          label={`Accept ${vendor.vendor}`}
          initial={fieldsOf(vendor)}
          submitLabel="Accept"
          busy={busy}
          onSubmit={(fields) =>
            change(() => acceptVendor(vendor.vendor, fields), `Accepted ${vendor.vendor}`)
          }
          onDone={() => setWithChanges(false)}
        />
      ) : (
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            disabled={busy}
            onClick={() => decide(() => acceptVendor(vendor.vendor), `Accepted ${vendor.vendor}`)}
          >
            <CheckIcon />
            Accept
          </Button>
          <Button variant="outline" size="sm" disabled={busy} onClick={() => setWithChanges(true)}>
            Accept with changes
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={busy}
            onClick={() => decide(() => ignoreVendor(vendor.vendor), `Ignored ${vendor.vendor}`)}
          >
            Ignore
          </Button>
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
}: {
  list: VendorList
  busy: boolean
  change: Change
  decide: Decide
}) {
  const [editing, setEditing] = useState<Vendor | null>(null)
  const [removing, setRemoving] = useState<Vendor | null>(null)
  const latestMonth = list.latest_month ? monthName(list.latest_month) : null
  return (
    <Section aria-label="Expected vendors" title="Expected vendors" count={list.expected.length}>
      {editing && (
        <VendorDialog
          open
          onOpenChange={(open) => {
            if (!open) setEditing(null)
          }}
          label={`Edit ${editing.vendor}`}
          description="What is expected of this vendor. The next run checks it as changed here."
          initial={fieldsOf(editing)}
          submitLabel="Save"
          busy={busy}
          onSubmit={(fields) =>
            change(() => editVendor(editing.vendor, fields), `Saved ${editing.vendor}`)
          }
        />
      )}
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => {
          if (!open) setRemoving(null)
        }}
        title={`Remove ${removing?.vendor ?? ''} from the vendor list?`}
        description="It is no longer checked for gaps. What was collected from it stays."
        confirmLabel={`Yes, remove ${removing?.vendor ?? ''}`}
        cancelLabel="Keep"
        busy={busy}
        onConfirm={() => {
          const vendor = removing
          if (vendor) void decide(() => removeVendor(vendor.vendor), `Removed ${vendor.vendor}`)
        }}
      />
      {list.expected.length === 0 ? (
        <EmptyState icon={Building2Icon}>No vendor is expected yet.</EmptyState>
      ) : (
        <Table aria-label="Expected vendors">
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Vendor</TableHead>
              <TableHead scope="col">Source account</TableHead>
              <TableHead scope="col">Billing cycle</TableHead>
              <TableHead scope="col" className="amount">
                Usual amount
              </TableHead>
              <TableHead scope="col" className="amount">
                Latest amount
              </TableHead>
              <TableHead scope="col">{latestMonth ?? 'Latest month'}</TableHead>
              <TableHead scope="col" className="text-right">
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {list.expected.map((vendor) => (
              <ExpectedVendorRow
                key={vendor.vendor}
                vendor={vendor}
                latestMonth={latestMonth}
                busy={busy}
                onEdit={() => setEditing(vendor)}
                onRemove={() => setRemoving(vendor)}
              />
            ))}
          </TableBody>
        </Table>
      )}
    </Section>
  )
}

function GapMark({ vendor, latestMonth }: { vendor: Vendor; latestMonth: string | null }) {
  if (vendor.gap === null || latestMonth === null) return null
  if (vendor.gap === 'unknown') {
    return (
      <Badge
        variant="muted"
        className="tag tag-gap-unknown border-dashed"
        title="A source account could not be read"
      >
        Gap unknown in {latestMonth}
      </Badge>
    )
  }
  return (
    <Badge variant="warning" className="tag tag-gap" title="No billing document was collected">
      Gap in {latestMonth}
    </Badge>
  )
}

function ExpectedVendorRow({
  vendor,
  latestMonth,
  busy,
  onEdit,
  onRemove,
}: {
  vendor: Vendor
  latestMonth: string | null
  busy: boolean
  onEdit: () => void
  onRemove: () => void
}) {
  const usual = amountIn(vendor.usual_amount, vendor.currency)
  const latest = amountIn(vendor.latest_amount, vendor.latest_currency)
  return (
    <TableRow className={cn(vendor.gap && 'has-gap [&>th]:shadow-[inset_3px_0_0_var(--warning)]')}>
      <TableRowHead>{vendor.vendor}</TableRowHead>
      <TableCell className="text-muted-foreground">
        {vendor.source_account ?? <NotAvailable>Any</NotAvailable>}
      </TableCell>
      <TableCell>{billingCycle(vendor)}</TableCell>
      <TableCell className="amount">
        {usual ? <span className="number">{usual}</span> : <NotAvailable>—</NotAvailable>}
      </TableCell>
      <TableCell className="amount">
        {latest ? <span className="number">{latest}</span> : <NotAvailable>—</NotAvailable>}
      </TableCell>
      <TableCell>
        <GapMark vendor={vendor} latestMonth={latestMonth} />
      </TableCell>
      <TableCell className="actions whitespace-nowrap">
        <div className="flex justify-end gap-1">
          <Button variant="ghost" size="sm" disabled={busy} onClick={onEdit}>
            <PencilIcon />
            Edit
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="text-destructive hover:text-destructive"
            disabled={busy}
            onClick={onRemove}
          >
            <Trash2Icon />
            Remove
          </Button>
        </div>
      </TableCell>
    </TableRow>
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
      <details className="group rounded-xl border bg-card px-4 py-3 shadow-xs">
        <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-semibold [&::-webkit-details-marker]:hidden">
          <ChevronRightIcon
            aria-hidden="true"
            className="size-4 text-muted-foreground transition-transform group-open:rotate-90"
          />
          Ignored vendors ({vendors.length})
        </summary>
        <Hint className="mt-2">
          Ignored vendors are not suggested again. Restore one to decide on it afresh.
        </Hint>
        <ul className="m-0 mt-2 flex list-none flex-col divide-y p-0">
          {vendors.map((vendor) => (
            <li key={vendor.vendor} className="flex items-center justify-between gap-3 py-2.5">
              <span className="text-sm">
                <strong>{vendor.vendor}</strong>
                {vendor.months_billed.length > 0 && (
                  <small className="text-muted-foreground">
                    {' '}
                    billed in {listOfMonths(vendor.months_billed)}
                  </small>
                )}
              </span>
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => decide(() => restoreVendor(vendor.vendor), `Restored ${vendor.vendor}`)}
              >
                Restore
              </Button>
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

/** A vendor's fields in a dialog, for adding one or editing one. */
function VendorDialog({
  open,
  onOpenChange,
  label,
  description,
  initial,
  submitLabel,
  busy,
  onSubmit,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  label: string
  description: string
  initial: VendorFields
  submitLabel: string
  busy: boolean
  onSubmit: (fields: VendorFields) => Promise<string | null>
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{label}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {open && (
          <VendorForm
            label={label}
            initial={initial}
            submitLabel={submitLabel}
            busy={busy}
            onSubmit={onSubmit}
            onDone={() => onOpenChange(false)}
          />
        )}
      </DialogContent>
    </Dialog>
  )
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
    <form className="flex flex-col gap-4" aria-label={label} onSubmit={submit}>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5 sm:col-span-2">
          <Label htmlFor={`${id}-vendor`}>Vendor</Label>
          <Input
            id={`${id}-vendor`}
            type="text"
            value={draft.vendor}
            onChange={(event) => set('vendor', event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5 sm:col-span-2">
          <Label htmlFor={`${id}-account`}>Source account</Label>
          <Input
            id={`${id}-account`}
            type="text"
            value={draft.source_account}
            placeholder="Any"
            onChange={(event) => set('source_account', event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${id}-cycle`}>Billing cycle</Label>
          <Select
            id={`${id}-cycle`}
            value={draft.billing_cycle}
            onChange={(event) => set('billing_cycle', event.target.value as BillingCycle)}
          >
            <option value="monthly">Monthly</option>
            <option value="annual">Annual</option>
          </Select>
        </div>
        {draft.billing_cycle === 'annual' && (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${id}-renewal`}>Renewal month</Label>
            <Select
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
            </Select>
          </div>
        )}
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${id}-amount`}>Usual amount</Label>
          <Input
            id={`${id}-amount`}
            type="text"
            inputMode="decimal"
            className="tabular"
            value={draft.usual_amount}
            onChange={(event) => set('usual_amount', event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${id}-currency`}>Currency</Label>
          <Input
            id={`${id}-currency`}
            type="text"
            className="uppercase"
            value={draft.currency}
            placeholder="USD"
            onChange={(event) => set('currency', event.target.value)}
          />
        </div>
      </div>
      {refused && <Problem>{refused}</Problem>}
      <div className="flex flex-wrap justify-end gap-2">
        <Button type="button" variant="outline" disabled={busy} onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" disabled={busy}>
          {submitLabel}
        </Button>
      </div>
    </form>
  )
}
