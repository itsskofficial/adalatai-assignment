import { useEffect, useId, useState, type FormEvent } from 'react'
import {
  addPerson,
  changeRole,
  NotSignedIn,
  peopleList,
  refusedSignIns,
  removePerson,
  type PersonOnList,
  type RefusedSignIn,
  type Role,
} from './api'
import { formatDate } from './format'
import { useShell } from './shell'
import type { Loaded } from './useLoaded'

type People = { people: PersonOnList[]; refused: RefusedSignIn[] }

/** Makes one change; answers with the reason it was refused, or null once it is made. */
type Change = (action: () => Promise<void>) => Promise<string | null>

const ROLE_NAMES: Record<Role, string> = { member: 'Member', administrator: 'Administrator' }

/** The time a sign-in was refused, to the minute, read as written so it never shifts. */
function formatTime(iso: string): string {
  return `${formatDate(iso)}, ${iso.slice(11, 16)}`
}

export function PeopleScreen({ administrator }: { administrator: boolean }) {
  if (!administrator) {
    return (
      <main className="screen">
        <h1>People</h1>
        <p className="empty">
          This screen is for administrators. Ask an administrator to make a change.
        </p>
      </main>
    )
  }
  return <ManagePeople />
}

function ManagePeople() {
  const { onSignedOut } = useShell()
  const [loaded, setLoaded] = useState<Loaded<People>>({ status: 'loading' })
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)

  // Each change counts up, and the list is read again from the server after it.
  const [changes, setChanges] = useState(0)

  useEffect(() => {
    let current = true
    Promise.all([peopleList(), refusedSignIns()]).then(
      ([people, refused]) => {
        if (!current) return
        setLoaded({ status: 'ready', value: { people, refused } })
        setBusy(false)
      },
      (problem: unknown) => {
        if (!current) return
        setBusy(false)
        if (problem instanceof NotSignedIn) {
          onSignedOut()
          return
        }
        setLoaded({
          status: 'problem',
          message: problem instanceof Error ? problem.message : String(problem),
        })
      },
    )
    return () => {
      current = false
    }
  }, [changes, onSignedOut])

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
      <h1>People</h1>
      <p className="lede">
        Everyone here may sign in to the dashboard. Administrators may also change this list;
        members may do everything else. Administrators set by the installation are named in the
        INVOICE_COLLECTOR_ALLOWLIST setting and cannot be changed here.
      </p>
      {notice && (
        <p className="reasons" role="alert">
          {notice}
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
          <section aria-label="People who may sign in">
            <h2>
              People who may sign in <small>{loaded.value.people.length}</small>
            </h2>
            <AddPerson busy={busy} change={change} />
            <table aria-label="People who may sign in">
              <thead>
                <tr>
                  <th scope="col">Email address</th>
                  <th scope="col">Role</th>
                  <th scope="col">Added</th>
                  <th scope="col">Last signed in</th>
                  <th scope="col">
                    <span className="visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {loaded.value.people.map((person) => (
                  <PersonRows
                    key={person.address}
                    person={person}
                    busy={busy}
                    removing={removing === person.address}
                    onRemoving={(on) => setRemoving(on ? person.address : null)}
                    decide={decide}
                  />
                ))}
              </tbody>
            </table>
          </section>
          <RefusedSignIns
            refused={loaded.value.refused}
            listed={new Set(loaded.value.people.map((person) => person.address))}
            busy={busy}
            decide={decide}
          />
        </>
      )}
    </main>
  )
}

function PersonRows({
  person,
  busy,
  removing,
  onRemoving,
  decide,
}: {
  person: PersonOnList
  busy: boolean
  removing: boolean
  onRemoving: (on: boolean) => void
  decide: (action: () => Promise<void>) => Promise<void>
}) {
  return (
    <>
      <tr>
        <th scope="row">{person.address}</th>
        <td>
          {person.set_by_installation ? (
            ROLE_NAMES[person.role]
          ) : (
            <select
              aria-label={`Role of ${person.address}`}
              value={person.role}
              disabled={busy}
              onChange={(event) =>
                decide(() => changeRole(person.address, event.target.value as Role))
              }
            >
              <option value="member">{ROLE_NAMES.member}</option>
              <option value="administrator">{ROLE_NAMES.administrator}</option>
            </select>
          )}
        </td>
        <td>
          {person.set_by_installation ? (
            <span className="tag" title="Named in the INVOICE_COLLECTOR_ALLOWLIST setting">
              Set by the installation
            </span>
          ) : person.added_by && person.added_at ? (
            `${person.added_by} on ${formatDate(person.added_at)}`
          ) : (
            <span className="not-available">—</span>
          )}
        </td>
        <td>
          {person.last_signed_in_at ? (
            formatDate(person.last_signed_in_at)
          ) : (
            <span className="not-available">Never</span>
          )}
        </td>
        <td className="actions">
          {!person.set_by_installation && (
            <button type="button" disabled={busy || removing} onClick={() => onRemoving(true)}>
              Remove
            </button>
          )}
        </td>
      </tr>
      {removing && (
        <tr className="confirming">
          <td colSpan={5}>
            <div className="confirm">
              <span>Remove {person.address}? They will be signed out at once.</span>
              <button
                type="button"
                className="danger"
                aria-label={`Yes, remove ${person.address}`}
                disabled={busy}
                onClick={async () => {
                  await decide(() => removePerson(person.address))
                  onRemoving(false)
                }}
              >
                Yes, remove {person.address}
              </button>
              <button type="button" disabled={busy} onClick={() => onRemoving(false)}>
                Keep
              </button>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

/** An address and a role. The server checks them; its reason for refusing is shown here. */
function AddPerson({ busy, change }: { busy: boolean; change: Change }) {
  const id = useId()
  const [address, setAddress] = useState('')
  const [role, setRole] = useState<Role>('member')
  const [refused, setRefused] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    const reason = await change(() => addPerson(address, role))
    setRefused(reason)
    if (reason === null) {
      setAddress('')
      setRole('member')
    }
  }

  return (
    <form className="vendor-form panel" aria-label="Add a person" onSubmit={submit}>
      <div className="fields">
        <label htmlFor={`${id}-address`}>Email address</label>
        <input
          id={`${id}-address`}
          type="text"
          inputMode="email"
          autoComplete="off"
          value={address}
          placeholder="name@example.com"
          onChange={(event) => setAddress(event.target.value)}
        />
        <label htmlFor={`${id}-role`}>Role</label>
        <select
          id={`${id}-role`}
          value={role}
          onChange={(event) => setRole(event.target.value as Role)}
        >
          <option value="member">{ROLE_NAMES.member}</option>
          <option value="administrator">{ROLE_NAMES.administrator}</option>
        </select>
      </div>
      {refused && (
        <p className="reasons" role="alert">
          {refused}
        </p>
      )}
      <div className="actions">
        <button type="submit" className="primary" disabled={busy}>
          Add
        </button>
      </div>
    </form>
  )
}

function RefusedSignIns({
  refused,
  listed,
  busy,
  decide,
}: {
  refused: RefusedSignIn[]
  listed: Set<string>
  busy: boolean
  decide: (action: () => Promise<void>) => Promise<void>
}) {
  return (
    <section aria-label="Refused sign-ins">
      <h2>
        Refused sign-ins <small>{refused.length}</small>
      </h2>
      <p className="hint">
        The latest attempts to sign in with an address that may not. Add one as a member to let
        that person in.
      </p>
      {refused.length === 0 ? (
        <p className="empty">No sign-in has been refused.</p>
      ) : (
        <ul className="refused">
          {refused.map((attempt, index) => (
            <li key={`${index}-${attempt.address}`}>
              <span>
                <strong>{attempt.address}</strong> <small>{formatTime(attempt.attempted_at)}</small>
              </span>
              {listed.has(attempt.address) ? (
                <span className="not-available">Now on the list</span>
              ) : (
                <button
                  type="button"
                  disabled={busy}
                  aria-label={`Add ${attempt.address} as a member`}
                  onClick={() => decide(() => addPerson(attempt.address, 'member'))}
                >
                  Add as a member
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
