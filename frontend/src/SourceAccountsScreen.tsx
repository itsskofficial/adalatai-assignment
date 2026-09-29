import { useEffect, useId, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router'
import {
  addFoundSourceAccount,
  connectionResult,
  connectSourceAccount,
  makeOwnerAccount,
  NotSignedIn,
  removeSourceAccount,
  renewSourceAccount,
  sourceAccountHistory,
  sourceAccounts,
  type ConnectionResult,
  type SourceAccount,
  type SourceAccountChange,
  type SourceAccountList,
} from './api'
import { browser } from './browser'
import { formatDate, monthName } from './format'
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
}

const HISTORY_SHOWN = 20

function problemText(problem: unknown): string {
  return problem instanceof Error ? problem.message : String(problem)
}

export function SourceAccountsScreen() {
  const { onSignedOut } = useShell()
  const [search] = useSearchParams()
  const returnedFromGoogle = search.has('connect')
  const [loaded, setLoaded] = useState<Loaded<Accounts>>({ status: 'loading' })
  const [result, setResult] = useState<ConnectionResult | null>(null)
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
    <main className="screen">
      <h1>Source accounts</h1>
      <p className="lede">
        The tool reads each source account with read-only access to Gmail, so it cannot send,
        change or delete mail. The owner account also lets the tool use the Drive files it
        creates itself, and nothing else in that Drive.
      </p>
      {list?.sign_in_lifetime_days && (
        <p className="hint">
          While the OAuth app is in testing, Google ends each sign-in{' '}
          {list.sign_in_lifetime_days} days after it is made. Renew a sign-in here before it
          ends.
        </p>
      )}
      {result && <Outcome result={result} />}
      {notice?.kind === 'status' && <output className="notice">{notice.text}</output>}
      {notice?.kind === 'alert' && (
        <p className="reasons" role="alert">
          {notice.text}
        </p>
      )}
      {loaded.status === 'loading' && <p className="empty">Loading…</p>}
      {loaded.status === 'problem' && (
        <p className="reasons" role="alert">
          {loaded.message}
        </p>
      )}
      {loaded.status === 'ready' && (
        <>
          <ConnectedAccounts
            accounts={loaded.value.list.source_accounts}
            busy={busy}
            decide={decide}
            renew={(address) => goToGoogle(() => renewSourceAccount(address))}
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
    </main>
  )
}

/** How the connection through Google ended, shown after the person comes back. */
function Outcome({ result }: { result: ConnectionResult }) {
  const address = result.address ?? 'The source account'
  switch (result.outcome) {
    case 'connected':
      return (
        <output className="notice">Connected {result.address}. The next run reads it.</output>
      )
    case 'renewed':
      return (
        <output className="notice">Renewed the sign-in for {result.address}.</output>
      )
    case 'wrong_address':
      return (
        <p className="reasons" role="alert">
          {address} was not connected. Expected {result.address}, but {result.signed_in_address}{' '}
          signed in. Nothing was stored. Try again, and choose {result.address} at Google.
        </p>
      )
    case 'failed':
      return (
        <p className="reasons" role="alert">
          {address} was not connected: {result.reason ?? 'the sign-in did not complete'}
        </p>
      )
    default:
      return null
  }
}

type Decide = (action: () => Promise<Notice | null | void>) => Promise<void>

function ConnectedAccounts({
  accounts,
  busy,
  decide,
  renew,
}: {
  accounts: SourceAccount[]
  busy: boolean
  decide: Decide
  renew: (address: string) => Promise<string | null>
}) {
  const [removing, setRemoving] = useState<string | null>(null)
  return (
    <section aria-label="Connected source accounts">
      <h2>
        Connected source accounts <small>{accounts.length}</small>
      </h2>
      {accounts.length === 0 ? (
        <p className="empty">No source account is connected yet. Connect one below.</p>
      ) : (
        <div className="source-accounts">
          {accounts.map((account) => (
            <AccountCard
              key={account.address}
              account={account}
              busy={busy}
              removing={removing === account.address}
              onRemoving={(on) => setRemoving(on ? account.address : null)}
              decide={decide}
              renew={renew}
            />
          ))}
        </div>
      )}
    </section>
  )
}

function SignInMark({ account }: { account: SourceAccount }) {
  switch (account.sign_in) {
    case 'works':
      if (account.expiring_soon && account.sign_in_ends_at) {
        return (
          <span className="tag tag-expiring">
            Sign-in expires soon, on {formatDate(account.sign_in_ends_at)}
          </span>
        )
      }
      return (
        <span className="tag tag-works">
          Sign-in works
          {account.sign_in_ends_at && <> until {formatDate(account.sign_in_ends_at)}</>}
        </span>
      )
    case 'expired':
      return <span className="tag tag-expired">Sign-in expired</span>
    case 'missing':
      return <span className="tag tag-expired">Not signed in</span>
    default:
      return (
        <span className="tag tag-expiring" title={account.sign_in_problem ?? undefined}>
          Sign-in could not be checked
        </span>
      )
  }
}

function documentCount(count: number): string {
  return count === 1 ? '1 billing document' : `${count} billing documents`
}

function AccountCard({
  account,
  busy,
  removing,
  onRemoving,
  decide,
  renew,
}: {
  account: SourceAccount
  busy: boolean
  removing: boolean
  onRemoving: (on: boolean) => void
  decide: Decide
  renew: (address: string) => Promise<string | null>
}) {
  const id = useId()
  const [renewFailed, setRenewFailed] = useState<string | null>(null)
  const run = account.latest_run
  return (
    <article
      className={`source-account${account.sign_in === 'works' && !account.expiring_soon ? '' : ' needs-attention'}`}
      aria-labelledby={id}
    >
      <div className="source-account-head">
        <h3 id={id}>{account.address}</h3>
        {account.is_owner && <span className="tag tag-owner">Owner account</span>}
      </div>
      <p className="sign-in-state">
        <SignInMark account={account} />
      </p>
      <ul className="facts">
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
            <li className="could-not-read">
              Could not be read in {monthName(run.month)}: {run.reason ?? 'no reason was recorded'}
            </li>
          ))}
        <li>
          Connected by {account.connected_by} on {formatDate(account.connected_at)}
        </li>
      </ul>
      {account.needs_drive_access && (
        <p className="hint">
          Renew to give it access to Drive files the tool creates, so a run can archive there
          and write the summary.
        </p>
      )}
      {renewFailed && (
        <p className="reasons" role="alert">
          {renewFailed}
        </p>
      )}
      {removing ? (
        <div className="confirm">
          <span>
            Remove {account.address}? Its stored sign-in is deleted. What was collected from it
            stays.
          </span>
          <button
            type="button"
            className="danger"
            disabled={busy}
            onClick={async () => {
              await decide(() => removeSourceAccount(account.address))
              onRemoving(false)
            }}
          >
            Yes, remove {account.address}
          </button>
          <button type="button" disabled={busy} onClick={() => onRemoving(false)}>
            Keep
          </button>
        </div>
      ) : (
        <div className="actions">
          <button
            type="button"
            className={account.sign_in === 'works' && !account.needs_drive_access ? undefined : 'primary'}
            disabled={busy}
            onClick={async () => setRenewFailed(await renew(account.address))}
          >
            Renew
          </button>
          {!account.is_owner && (
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                decide(async () => {
                  const changed = await makeOwnerAccount(account.address)
                  return { kind: 'status', text: changed.message }
                })
              }
            >
              Make owner
            </button>
          )}
          <button
            type="button"
            disabled={busy || account.is_owner}
            title={
              account.is_owner
                ? 'Make another source account the owner before removing this one'
                : undefined
            }
            onClick={() => onRemoving(true)}
          >
            Remove
          </button>
        </div>
      )}
    </article>
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
    <section>
      <h2>Connect a source account</h2>
      <form className="connect-form panel" aria-label="Connect a source account" onSubmit={submit}>
        <p className="hint">
          Google opens and asks you to sign in as this address and allow read-only access to its
          mail. You come back here when it is done.
        </p>
        <div className="connect-row">
          <label htmlFor={`${id}-address`}>Address</label>
          <input
            id={`${id}-address`}
            type="email"
            required
            value={address}
            placeholder="billing@example.com"
            onChange={(event) => setAddress(event.target.value)}
          />
          <button type="submit" className="primary" disabled={busy || !address.trim()}>
            Connect
          </button>
        </div>
        <label className="owner-choice">
          <input
            type="checkbox"
            checked={owner}
            onChange={(event) => setOwner(event.target.checked)}
          />
          This is the owner account: also allow access to the Drive files the tool creates
        </label>
        {refused && (
          <p className="reasons" role="alert">
            {refused}
          </p>
        )}
      </form>
    </section>
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
    <section aria-label="Found on this machine">
      <h2>
        Found on this machine <small>{addresses.length}</small>
      </h2>
      <p className="hint">
        These were signed in from the command line on this machine but are not connected. Add
        one so that runs read it.
      </p>
      <ul className="found">
        {addresses.map((address) => (
          <li key={address}>
            <span>{address}</span>
            <button
              type="button"
              aria-label={`Add ${address}`}
              disabled={busy}
              onClick={() => decide(() => addFoundSourceAccount(address))}
            >
              Add
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}

function Changes({ history }: { history: SourceAccountChange[] }) {
  return (
    <section aria-label="Changes">
      <h2>Changes</h2>
      <ul className="changes">
        {history.slice(0, HISTORY_SHOWN).map((change, index) => (
          <li key={`${change.changed_at}-${change.address}-${change.action}-${index}`}>
            <strong>
              {ACTIONS[change.action]} {change.address}
            </strong>{' '}
            <small>
              by {change.person} on {formatDate(change.changed_at)}
            </small>
          </li>
        ))}
      </ul>
    </section>
  )
}
