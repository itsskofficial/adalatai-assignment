import {
  CrownIcon,
  HistoryIcon,
  InboxIcon,
  KeyRoundIcon,
  MailIcon,
  PlusIcon,
  RefreshCwIcon,
  Trash2Icon,
} from 'lucide-react'
import { useEffect, useId, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router'
import {
  addFoundSourceAccount,
  connectionResult,
  connectSourceAccount,
  fillWithSampleMail,
  makeOwnerAccount,
  NotSignedIn,
  removeSourceAccount,
  renewSourceAccount,
  sampleMailOffer,
  sampleMailResult,
  sourceAccountHistory,
  sourceAccounts,
  type ConnectionResult,
  type OwnerAccount,
  type SampleMailOffer,
  type SampleMailResult,
  type SourceAccount,
  type SourceAccountChange,
  type SourceAccountList,
} from './api'
import { browser } from './browser'
import { ConfirmDialog } from './components/ConfirmDialog'
import { EmptyState } from './components/EmptyState'
import { CardsSkeleton, Loading } from './components/Loading'
import { Hint, Problem, Status } from './components/Notice'
import { PageHeader, Screen, Section } from './components/Screen'
import { Badge } from './components/ui/badge'
import { Button } from './components/ui/button'
import { Checkbox } from './components/ui/checkbox'
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
import { Tooltip, TooltipContent, TooltipTrigger } from './components/ui/tooltip'
import { formatDate, monthName } from './format'
import { cn } from './lib/utils'
import { useShell } from './shell'
import type { Loaded } from './useLoaded'

type Notice = { kind: 'status' | 'alert'; text: string }

/** Makes one change; answers with the reason it failed, or null once it is made. */
type Act = (action: () => Promise<Notice | null | void>) => Promise<string | null>

/** What the screen reads from the server: the source accounts and who changed them. */
type Accounts = { list: SourceAccountList; history: SourceAccountChange[] }

const ACTIONS: Record<SourceAccountChange['action'], string> = {
  connected: 'Connected',
  renewed: 'Renewed',
  added: 'Added',
  removed: 'Removed',
  made_owner: 'Made owner',
  filled_with_sample_mail: 'Put sample mail into',
}

const HISTORY_SHOWN = 20

function problemText(problem: unknown): string {
  return problem instanceof Error ? problem.message : String(problem)
}

export function SourceAccountsScreen() {
  const { onSignedOut } = useShell()
  const [search] = useSearchParams()
  const returnedFromGoogle = search.has('connect')
  const returnedWithLeaveToInsert = search.has('sample-mail')
  const [loaded, setLoaded] = useState<Loaded<Accounts>>({ status: 'loading' })
  const [result, setResult] = useState<ConnectionResult | null>(null)
  const [offer, setOffer] = useState<SampleMailOffer | null>(null)
  const [filled, setFilled] = useState<SampleMailResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<Notice | null>(null)

  // Each change counts up, and the list is read again from the server after it.
  const [changes, setChanges] = useState(0)

  useEffect(() => {
    let current = true
    Promise.all([sourceAccounts(), sourceAccountHistory()]).then(
      ([list, history]) => {
        if (!current) return
        setLoaded({ status: 'ready', value: { list, history } })
        setBusy(false)
      },
      (problem: unknown) => {
        if (!current) return
        setBusy(false)
        if (problem instanceof NotSignedIn) {
          onSignedOut()
          return
        }
        setLoaded({ status: 'problem', message: problemText(problem) })
      },
    )
    return () => {
      current = false
    }
  }, [changes, onSignedOut])

  // Whether sample mail can be put into a mailbox. Without it the rest of the screen works.
  useEffect(() => {
    let current = true
    sampleMailOffer().then(
      (value) => {
        if (current) setOffer(value)
      },
      () => {
        if (current) setOffer(null)
      },
    )
    return () => {
      current = false
    }
  }, [])

  useEffect(() => {
    if (!returnedWithLeaveToInsert) return
    let current = true
    sampleMailResult().then(
      (value) => {
        if (current) setFilled(value)
      },
      (problem: unknown) => {
        if (current && problem instanceof NotSignedIn) onSignedOut()
      },
    )
    return () => {
      current = false
    }
  }, [returnedWithLeaveToInsert, onSignedOut])

  useEffect(() => {
    if (!returnedFromGoogle) return
    let current = true
    connectionResult().then(
      (value) => {
        if (current) setResult(value)
      },
      (problem: unknown) => {
        if (!current) return
        if (problem instanceof NotSignedIn) onSignedOut()
      },
    )
    return () => {
      current = false
    }
  }, [returnedFromGoogle, onSignedOut])

  const act: Act = async (action) => {
    setBusy(true)
    setNotice(null)
    try {
      const said = await action()
      if (said) setNotice(said)
      setChanges((count) => count + 1)
      return null
    } catch (problem) {
      setBusy(false)
      if (problem instanceof NotSignedIn) {
        onSignedOut()
        return null
      }
      return problemText(problem)
    }
  }

  /** Sends the person to Google; the screen stays busy until the browser leaves. */
  async function goToGoogle(start: () => Promise<string>): Promise<string | null> {
    setBusy(true)
    setNotice(null)
    try {
      browser.goTo(await start())
      return null
    } catch (problem) {
      setBusy(false)
      if (problem instanceof NotSignedIn) {
        onSignedOut()
        return null
      }
      return problemText(problem)
    }
  }

  async function decide(action: () => Promise<Notice | null | void>) {
    const failed = await act(action)
    if (failed) setNotice({ kind: 'alert', text: failed })
  }

  const list = loaded.status === 'ready' ? loaded.value.list : null
  return (
    <Screen>
      <PageHeader
        title="Source accounts"
        description="The tool reads each source account with read-only access to Gmail, so it cannot send, change or delete mail. The owner account also lets the tool use the Drive files it creates itself, and nothing else in that Drive."
      >
        {list?.sign_in_lifetime_days && (
          <Hint>
            While the OAuth app is in testing, Google ends each sign-in{' '}
            {list.sign_in_lifetime_days} days after it is made. Renew a sign-in here before it
            ends.
          </Hint>
        )}
        {result && <Outcome result={result} />}
        {filled && <SampleMailOutcome result={filled} />}
        {notice?.kind === 'status' && <Status tone="success">{notice.text}</Status>}
        {notice?.kind === 'alert' && <Problem>{notice.text}</Problem>}
      </PageHeader>
      {loaded.status === 'loading' && (
        <Loading>
          <CardsSkeleton count={3} className="xl:grid-cols-3" />
        </Loading>
      )}
      {loaded.status === 'problem' && <Problem>{loaded.message}</Problem>}
      {loaded.status === 'ready' && (
        <>
          {loaded.value.list.owner && <OwnerNotice owner={loaded.value.list.owner} />}
          <ConnectedAccounts
            accounts={loaded.value.list.source_accounts}
            busy={busy}
            decide={decide}
            renew={(address) => goToGoogle(() => renewSourceAccount(address))}
            offer={offer?.available && offer.can_fill ? offer : null}
            fill={async (address, mailbox, vendors) => {
              setFilled(null)
              let started: string | null = null
              const failed = await act(async () => {
                const answer = await fillWithSampleMail(address, mailbox, vendors)
                if (answer.authorization_url) started = answer.authorization_url
                else if (answer.result) setFilled(answer.result)
              })
              if (started) browser.goTo(started)
              return failed
            }}
          />
          <ConnectForm
            busy={busy}
            connect={(address, owner) => goToGoogle(() => connectSourceAccount(address, owner))}
          />
          {loaded.value.list.found_on_this_machine.length > 0 && (
            <FoundOnThisMachine
              addresses={loaded.value.list.found_on_this_machine}
              busy={busy}
              decide={decide}
            />
          )}
          {loaded.value.history.length > 0 && <Changes history={loaded.value.history} />}
        </>
      )}
    </Screen>
  )
}

/** Which account is the owner account, and whether runs and approvals can reach its Drive. */
function OwnerNotice({ owner }: { owner: OwnerAccount }) {
  return (
    <section className="flex flex-col gap-2" aria-label="Owner account">
      {owner.problem ? (
        <Problem tone="destructive">{owner.problem}</Problem>
      ) : (
        <p className="m-0 flex items-start gap-3 rounded-lg border bg-muted/60 px-3.5 py-3 text-sm text-muted-foreground">
          <CrownIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
          <span>
            {owner.address} is the owner account
            {owner.chosen_on === 'setting' ? ', named by the INVOICE_COLLECTOR_GOOGLE_OWNER setting' : ''}
            . Runs, and documents approved or uploaded on the Review screen, are filed to its Drive.
          </span>
        </p>
      )}
      {owner.set_aside && <Hint>{owner.set_aside}</Hint>}
    </section>
  )
}

/** How the connection through Google ended, shown after the person comes back. */
function Outcome({ result }: { result: ConnectionResult }) {
  const address = result.address ?? 'The source account'
  switch (result.outcome) {
    case 'connected':
      return (
        <Status tone="success">Connected {result.address}. The next run reads it.</Status>
      )
    case 'renewed':
      return <Status tone="success">Renewed the sign-in for {result.address}.</Status>
    case 'wrong_address':
      return (
        <Problem>
          {address} was not connected. Expected {result.address}, but {result.signed_in_address}{' '}
          signed in. Nothing was stored. Try again, and choose {result.address} at Google.
        </Problem>
      )
    case 'failed':
      return (
        <Problem>
          {address} was not connected: {result.reason ?? 'the sign-in did not complete'}
        </Problem>
      )
    default:
      return null
  }
}

type Decide = (action: () => Promise<Notice | null | void>) => Promise<void>

/** Puts a sample mailbox into a source account; answers with why it failed, or null. */
type Fill = (address: string, mailbox: string, vendors: boolean) => Promise<string | null>

function ConnectedAccounts({
  accounts,
  busy,
  decide,
  renew,
  offer,
  fill,
}: {
  accounts: SourceAccount[]
  busy: boolean
  decide: Decide
  renew: (address: string) => Promise<string | null>
  offer: SampleMailOffer | null
  fill: Fill
}) {
  const [removing, setRemoving] = useState<string | null>(null)
  return (
    <Section
      aria-label="Connected source accounts"
      title="Connected source accounts"
      count={accounts.length}
    >
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => {
          if (!open) setRemoving(null)
        }}
        title={`Remove ${removing ?? ''}?`}
        description="Its stored sign-in is deleted. What was collected from it stays."
        confirmLabel={`Yes, remove ${removing ?? ''}`}
        cancelLabel="Keep"
        busy={busy}
        onConfirm={() => {
          const address = removing
          if (address) void decide(() => removeSourceAccount(address))
        }}
      />
      {accounts.length === 0 ? (
        <EmptyState icon={InboxIcon}>
          No source account is connected yet. Connect one below.
        </EmptyState>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {accounts.map((account) => (
            <AccountCard
              key={account.address}
              account={account}
              busy={busy}
              onRemove={() => setRemoving(account.address)}
              decide={decide}
              renew={renew}
              offer={account.sign_in === 'works' ? offer : null}
              fill={fill}
            />
          ))}
        </div>
      )}
    </Section>
  )
}

function SignInMark({ account }: { account: SourceAccount }) {
  switch (account.sign_in) {
    case 'works':
      if (account.expiring_soon && account.sign_in_ends_at) {
        return (
          <Badge variant="warning" className="tag tag-expiring">
            Sign-in expires soon, on {formatDate(account.sign_in_ends_at)}
          </Badge>
        )
      }
      return (
        <Badge variant="success" className="tag tag-works">
          Sign-in works
          {account.sign_in_ends_at && <> until {formatDate(account.sign_in_ends_at)}</>}
        </Badge>
      )
    case 'expired':
      return (
        <Badge variant="destructive" className="tag tag-expired">
          Sign-in expired
        </Badge>
      )
    case 'missing':
      return (
        <Badge variant="destructive" className="tag tag-expired">
          Not signed in
        </Badge>
      )
    default:
      return (
        <Badge
          variant="warning"
          className="tag tag-expiring"
          title={account.sign_in_problem ?? undefined}
        >
          Sign-in could not be checked
        </Badge>
      )
  }
}

function documentCount(count: number): string {
  return count === 1 ? '1 billing document' : `${count} billing documents`
}

function AccountCard({
  account,
  busy,
  onRemove,
  decide,
  renew,
  offer,
  fill,
}: {
  account: SourceAccount
  busy: boolean
  onRemove: () => void
  decide: Decide
  renew: (address: string) => Promise<string | null>
  offer: SampleMailOffer | null
  fill: Fill
}) {
  const id = useId()
  const [renewFailed, setRenewFailed] = useState<string | null>(null)
  const [filling, setFilling] = useState(false)
  const run = account.latest_run
  const attention = !(account.sign_in === 'works' && !account.expiring_soon)
  const renewMatters = account.sign_in !== 'works' || account.needs_drive_access
  return (
    <article
      className={cn(
        'flex flex-col gap-3 rounded-xl border border-l-4 bg-card p-4 shadow-xs',
        attention ? 'border-l-warning' : 'border-l-success',
      )}
      aria-labelledby={id}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 id={id} className="min-w-0 text-base font-semibold break-all">
          {account.address}
        </h3>
        {account.is_owner && (
          <Badge variant="info" className="tag tag-owner">
            <CrownIcon aria-hidden="true" />
            Owner account
          </Badge>
        )}
      </div>
      <p className="m-0">
        <SignInMark account={account} />
      </p>
      <ul className="m-0 flex list-none flex-col gap-1 p-0 text-sm text-muted-foreground">
        <li>
          {account.last_read_month
            ? `Last read for ${monthName(account.last_read_month)}`
            : 'Not read yet'}
        </li>
        {run &&
          (run.read ? (
            <li>
              {documentCount(run.billing_documents)} collected in {monthName(run.month)}
            </li>
          ) : (
            <li className="text-destructive">
              Could not be read in {monthName(run.month)}: {run.reason ?? 'no reason was recorded'}
            </li>
          ))}
        <li>
          Connected by {account.connected_by} on {formatDate(account.connected_at)}
        </li>
      </ul>
      {account.needs_drive_access && (
        <Hint>
          Renew to give it access to Drive files the tool creates, so a run can archive there
          and write the summary.
        </Hint>
      )}
      {renewFailed && <Problem>{renewFailed}</Problem>}
      {offer && (
        <SampleMailDialog
          open={filling}
          onOpenChange={setFilling}
          address={account.address}
          offer={offer}
          busy={busy}
          fill={fill}
        />
      )}
      <div className="mt-auto flex flex-wrap gap-2 pt-1">
        <Button
          variant={renewMatters ? 'default' : 'outline'}
          size="sm"
          disabled={busy}
          onClick={async () => setRenewFailed(await renew(account.address))}
        >
          <RefreshCwIcon />
          Renew
        </Button>
        {!account.is_owner && (
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() =>
              decide(async () => {
                const changed = await makeOwnerAccount(account.address)
                return { kind: 'status', text: changed.message }
              })
            }
          >
            <CrownIcon />
            Make owner
          </Button>
        )}
        {offer && (
          <Button variant="outline" size="sm" disabled={busy} onClick={() => setFilling(true)}>
            <MailIcon />
            Fill with sample mail
          </Button>
        )}
        <RemoveButton
          disabled={busy || account.is_owner}
          reason={
            account.is_owner
              ? 'Make another source account the owner before removing this one'
              : undefined
          }
          onClick={onRemove}
        />
      </div>
    </article>
  )
}

/** The remove button, with the reason it cannot be used when it cannot. */
function RemoveButton({
  disabled,
  reason,
  onClick,
}: {
  disabled: boolean
  reason: string | undefined
  onClick: () => void
}) {
  const button = (
    <Button
      variant="ghost"
      size="sm"
      className="text-destructive hover:text-destructive"
      disabled={disabled}
      title={reason}
      onClick={onClick}
    >
      <Trash2Icon />
      Remove
    </Button>
  )
  if (!reason) return button
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex rounded-md">{button}</span>
      </TooltipTrigger>
      <TooltipContent>{reason}</TooltipContent>
    </Tooltip>
  )
}

/** How putting sample mail into a mailbox ended, shown after it is done. */
function SampleMailOutcome({ result }: { result: SampleMailResult }) {
  if (result.outcome === 'failed') {
    return (
      <Problem>
        No sample mail was put into {result.address ?? 'the mailbox'}:{' '}
        {result.reason ?? 'it did not complete'}
      </Problem>
    )
  }
  if (result.outcome !== 'filled') return null
  const emails = (count: number) => (count === 1 ? '1 sample email' : `${count} sample emails`)
  return (
    <Status tone="success">
      Put {emails(result.inserted)} from {result.sample_mailbox} into {result.address}
      {result.already_there > 0 && <>; {result.already_there} were already there</>}.
      {result.vendors_added.length > 0 && (
        <> Added to the expected vendor list: {result.vendors_added.join(', ')}.</>
      )}
      {result.vendors_already_listed.length > 0 && (
        <> Already on the list and left as they are: {result.vendors_already_listed.join(', ')}.</>
      )}{' '}
      Run the month on the Runs screen to collect them.
    </Status>
  )
}

function SampleMailDialog({
  open,
  onOpenChange,
  address,
  offer,
  busy,
  fill,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  address: string
  offer: SampleMailOffer
  busy: boolean
  fill: Fill
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Fill {address} with sample mail</DialogTitle>
          <DialogDescription>
            Sample emails from the tool's own set, for trying a run on a test mailbox.
          </DialogDescription>
        </DialogHeader>
        {open && (
          <SampleMailForm
            address={address}
            offer={offer}
            busy={busy}
            fill={fill}
            onClose={() => onOpenChange(false)}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function SampleMailForm({
  address,
  offer,
  busy,
  fill,
  onClose,
}: {
  address: string
  offer: SampleMailOffer
  busy: boolean
  fill: Fill
  onClose: () => void
}) {
  const id = useId()
  const [mailbox, setMailbox] = useState(offer.mailboxes[0]?.name ?? '')
  const [vendors, setVendors] = useState(false)
  const [refused, setRefused] = useState<string | null>(null)
  const chosen = offer.mailboxes.find((each) => each.name === mailbox)

  async function submit(event: FormEvent) {
    event.preventDefault()
    const failed = await fill(address, mailbox, vendors)
    setRefused(failed)
    if (!failed) onClose()
  }

  return (
    <form
      className="flex flex-col gap-4"
      aria-label={`Fill ${address} with sample mail`}
      onSubmit={submit}
    >
      <p
        role="note"
        className="m-0 rounded-lg border border-warning/30 bg-warning-soft px-3 py-2 text-sm text-warning"
      >
        This puts sample emails into the mailbox {address}. It is meant for test mailboxes
        only: the emails stay there until someone deletes them in Gmail. Google is asked for
        leave to insert mail into this mailbox, which is kept apart from the read-only sign-in.
        Doing it again inserts nothing twice.
      </p>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${id}-mailbox`}>Sample mailbox</Label>
        <Select
          id={`${id}-mailbox`}
          value={mailbox}
          onChange={(event) => setMailbox(event.target.value)}
        >
          {offer.mailboxes.map((each) => (
            <option key={each.name} value={each.name}>
              {each.name} ({each.emails} emails)
            </option>
          ))}
        </Select>
      </div>
      {chosen && chosen.vendors.length > 0 && (
        <div className="flex items-start gap-2 text-sm">
          <Checkbox
            id={`${id}-vendors`}
            className="mt-0.5"
            checked={vendors}
            onChange={(event) => setVendors(event.target.checked)}
          />
          <label htmlFor={`${id}-vendors`}>
            Also add the {chosen.vendors.length} vendors that bill this sample mailbox to the
            expected vendor list, as billed to {address}: {chosen.vendors.join(', ')}
          </label>
        </div>
      )}
      <Hint>
        {offer.portal_url
          ? `Portal links in these emails will lead to ${offer.portal_url}, the sample portal a run can reach.`
          : 'No sample portal is set, so a run refuses the portal links in these emails, and those emails fail with that reason.'}
      </Hint>
      {refused && <Problem>{refused}</Problem>}
      <div className="flex flex-wrap justify-end gap-2">
        <Button type="button" variant="outline" disabled={busy} onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" disabled={busy || !mailbox}>
          Put sample mail into {address}
        </Button>
      </div>
    </form>
  )
}

function ConnectForm({
  busy,
  connect,
}: {
  busy: boolean
  connect: (address: string, owner: boolean) => Promise<string | null>
}) {
  const id = useId()
  const [address, setAddress] = useState('')
  const [owner, setOwner] = useState(false)
  const [refused, setRefused] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setRefused(await connect(address.trim(), owner))
  }

  return (
    <Section title="Connect a source account">
      <form
        className="flex max-w-2xl flex-col gap-4 rounded-xl border bg-card p-5 shadow-xs"
        aria-label="Connect a source account"
        onSubmit={submit}
      >
        <Hint>
          Google opens and asks you to sign in as this address and allow read-only access to its
          mail. You come back here when it is done.
        </Hint>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="flex flex-1 flex-col gap-1.5">
            <Label htmlFor={`${id}-address`}>Address</Label>
            <Input
              id={`${id}-address`}
              type="email"
              required
              value={address}
              placeholder="billing@example.com"
              onChange={(event) => setAddress(event.target.value)}
            />
          </div>
          <Button type="submit" disabled={busy || !address.trim()}>
            <KeyRoundIcon />
            Connect
          </Button>
        </div>
        <div className="flex items-start gap-2 text-sm">
          <Checkbox
            id={`${id}-owner`}
            className="mt-0.5"
            checked={owner}
            onChange={(event) => setOwner(event.target.checked)}
          />
          <label htmlFor={`${id}-owner`}>
            This is the owner account: also allow access to the Drive files the tool creates
          </label>
        </div>
        {refused && <Problem>{refused}</Problem>}
      </form>
    </Section>
  )
}

function FoundOnThisMachine({
  addresses,
  busy,
  decide,
}: {
  addresses: string[]
  busy: boolean
  decide: Decide
}) {
  return (
    <Section
      aria-label="Found on this machine"
      title="Found on this machine"
      count={addresses.length}
      description="These were signed in from the command line on this machine but are not connected. Add one so that runs read it."
    >
      <ul className="m-0 flex list-none flex-col divide-y rounded-xl border bg-card px-4 shadow-xs">
        {addresses.map((address) => (
          <li key={address} className="flex items-center justify-between gap-3 py-2.5 text-sm">
            <span className="font-medium break-all">{address}</span>
            <Button
              variant="outline"
              size="sm"
              aria-label={`Add ${address}`}
              disabled={busy}
              onClick={() => decide(() => addFoundSourceAccount(address))}
            >
              <PlusIcon />
              Add
            </Button>
          </li>
        ))}
      </ul>
    </Section>
  )
}

function Changes({ history }: { history: SourceAccountChange[] }) {
  return (
    <Section aria-label="Changes" title="Changes">
      <ul className="m-0 flex list-none flex-col divide-y rounded-xl border bg-card px-4 shadow-xs">
        {history.slice(0, HISTORY_SHOWN).map((change, index) => (
          <li
            key={`${change.changed_at}-${change.address}-${change.action}-${index}`}
            className="flex items-center gap-3 py-2.5 text-sm"
          >
            <HistoryIcon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
            <span>
              <strong>
                {ACTIONS[change.action]} {change.address}
              </strong>{' '}
              <small className="text-muted-foreground">
                by {change.person} on {formatDate(change.changed_at)}
              </small>
            </span>
          </li>
        ))}
      </ul>
    </Section>
  )
}
