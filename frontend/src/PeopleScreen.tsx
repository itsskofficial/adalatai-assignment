import { ShieldCheckIcon, Trash2Icon, UserPlusIcon, UsersIcon } from 'lucide-react'
import { useEffect, useId, useState, type FormEvent } from 'react'
import { toast } from 'sonner'
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
import { NotAvailable } from './components/Amount'
import { ConfirmDialog } from './components/ConfirmDialog'
import { EmptyState } from './components/EmptyState'
import { Loading, TableSkeleton } from './components/Loading'
import { Problem } from './components/Notice'
import { PageHeader, Screen, Section } from './components/Screen'
import { Badge } from './components/ui/badge'
import { Button } from './components/ui/button'
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
import { formatDate } from './format'
import { useShell } from './shell'
import type { Loaded } from './useLoaded'

type People = { people: PersonOnList[]; refused: RefusedSignIn[] }

/** Makes one change; answers with the reason it was refused, or null once it is made. */
type Change = (action: () => Promise<void>, done?: string) => Promise<string | null>

const ROLE_NAMES: Record<Role, string> = { member: 'Member', administrator: 'Administrator' }

/** The time a sign-in was refused, to the minute, read as written so it never shifts. */
function formatTime(iso: string): string {
  return `${formatDate(iso)}, ${iso.slice(11, 16)}`
}

export function PeopleScreen({ administrator }: { administrator: boolean }) {
  if (!administrator) {
    return (
      <Screen>
        <PageHeader title="People" />
        <EmptyState icon={ShieldCheckIcon}>
          This screen is for administrators. Ask an administrator to make a change.
        </EmptyState>
      </Screen>
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
        title="People"
        description="Everyone here may sign in to the dashboard. Administrators may also change this list; members may do everything else. Administrators set by the installation are named in the INVOICE_COLLECTOR_ALLOWLIST setting and cannot be changed here."
      >
        {notice && <Problem>{notice}</Problem>}
      </PageHeader>
      {loaded.status === 'loading' && (
        <Loading>
          <TableSkeleton columns={4} />
        </Loading>
      )}
      {loaded.status === 'problem' && <Problem>{loaded.message}</Problem>}
      {loaded.status === 'ready' && (
        <>
          <Section
            aria-label="People who may sign in"
            title="People who may sign in"
            count={loaded.value.people.length}
          >
            <AddPerson busy={busy} change={change} />
            <ConfirmDialog
              open={removing !== null}
              onOpenChange={(open) => {
                if (!open) setRemoving(null)
              }}
              title={`Remove ${removing ?? ''}? They will be signed out at once.`}
              confirmLabel={`Yes, remove ${removing ?? ''}`}
              cancelLabel="Keep"
              busy={busy}
              onConfirm={() => {
                const address = removing
                if (address) void decide(() => removePerson(address), `Removed ${address}`)
              }}
            />
            <Table aria-label="People who may sign in">
              <TableHeader>
                <TableRow>
                  <TableHead scope="col">Email address</TableHead>
                  <TableHead scope="col">Role</TableHead>
                  <TableHead scope="col">Added</TableHead>
                  <TableHead scope="col">Last signed in</TableHead>
                  <TableHead scope="col" className="text-right">
                    <span className="sr-only">Actions</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {loaded.value.people.map((person) => (
                  <PersonRow
                    key={person.address}
                    person={person}
                    busy={busy}
                    onRemove={() => setRemoving(person.address)}
                    decide={decide}
                  />
                ))}
              </TableBody>
            </Table>
          </Section>
          <RefusedSignIns
            refused={loaded.value.refused}
            listed={new Set(loaded.value.people.map((person) => person.address))}
            busy={busy}
            decide={decide}
          />
        </>
      )}
    </Screen>
  )
}

function PersonRow({
  person,
  busy,
  onRemove,
  decide,
}: {
  person: PersonOnList
  busy: boolean
  onRemove: () => void
  decide: (action: () => Promise<void>, done?: string) => Promise<void>
}) {
  return (
    <TableRow>
      <TableRowHead>{person.address}</TableRowHead>
      <TableCell>
        {person.set_by_installation ? (
          <Badge variant={person.role === 'administrator' ? 'info' : 'muted'}>
            {ROLE_NAMES[person.role]}
          </Badge>
        ) : (
          <Select
            aria-label={`Role of ${person.address}`}
            wrapperClassName="w-40"
            value={person.role}
            disabled={busy}
            onChange={(event) => {
              const role = event.target.value as Role
              void decide(() => changeRole(person.address, role), `${person.address} is now ${ROLE_NAMES[role].toLowerCase()}`)
            }}
          >
            <option value="member">{ROLE_NAMES.member}</option>
            <option value="administrator">{ROLE_NAMES.administrator}</option>
          </Select>
        )}
      </TableCell>
      <TableCell className="text-muted-foreground">
        {person.set_by_installation ? (
          <Badge variant="outline" className="tag" title="Named in the INVOICE_COLLECTOR_ALLOWLIST setting">
            Set by the installation
          </Badge>
        ) : person.added_by && person.added_at ? (
          `${person.added_by} on ${formatDate(person.added_at)}`
        ) : (
          <NotAvailable>—</NotAvailable>
        )}
      </TableCell>
      <TableCell className="whitespace-nowrap text-muted-foreground">
        {person.last_signed_in_at ? (
          formatDate(person.last_signed_in_at)
        ) : (
          <NotAvailable>Never</NotAvailable>
        )}
      </TableCell>
      <TableCell className="actions whitespace-nowrap">
        {!person.set_by_installation && (
          <div className="flex justify-end">
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
        )}
      </TableCell>
    </TableRow>
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
    const reason = await change(() => addPerson(address, role), `Added ${address}`)
    setRefused(reason)
    if (reason === null) {
      setAddress('')
      setRole('member')
    }
  }

  return (
    <form
      className="flex flex-col gap-3 rounded-xl border bg-card p-4 shadow-xs"
      aria-label="Add a person"
      onSubmit={submit}
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <div className="flex flex-1 flex-col gap-1.5">
          <Label htmlFor={`${id}-address`}>Email address</Label>
          <Input
            id={`${id}-address`}
            type="text"
            inputMode="email"
            autoComplete="off"
            value={address}
            placeholder="name@example.com"
            onChange={(event) => setAddress(event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5 sm:w-44">
          <Label htmlFor={`${id}-role`}>Role</Label>
          <Select
            id={`${id}-role`}
            value={role}
            onChange={(event) => setRole(event.target.value as Role)}
          >
            <option value="member">{ROLE_NAMES.member}</option>
            <option value="administrator">{ROLE_NAMES.administrator}</option>
          </Select>
        </div>
        <Button type="submit" disabled={busy}>
          <UserPlusIcon />
          Add
        </Button>
      </div>
      {refused && <Problem>{refused}</Problem>}
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
  decide: (action: () => Promise<void>, done?: string) => Promise<void>
}) {
  return (
    <Section
      aria-label="Refused sign-ins"
      title="Refused sign-ins"
      count={refused.length}
      description="The latest attempts to sign in with an address that may not. Add one as a member to let that person in."
    >
      {refused.length === 0 ? (
        <EmptyState icon={UsersIcon} compact>
          No sign-in has been refused.
        </EmptyState>
      ) : (
        <ul className="m-0 flex list-none flex-col divide-y rounded-xl border bg-card px-4 shadow-xs">
          {refused.map((attempt, index) => (
            <li
              key={`${index}-${attempt.address}`}
              className="flex flex-wrap items-center justify-between gap-3 py-3"
            >
              <span className="flex min-w-0 flex-col gap-0.5 text-sm">
                <strong className="truncate">{attempt.address}</strong>
                <small className="text-muted-foreground">{formatTime(attempt.attempted_at)}</small>
              </span>
              {listed.has(attempt.address) ? (
                <NotAvailable>Now on the list</NotAvailable>
              ) : (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={busy}
                  aria-label={`Add ${attempt.address} as a member`}
                  onClick={() =>
                    decide(() => addPerson(attempt.address, 'member'), `Added ${attempt.address}`)
                  }
                >
                  <UserPlusIcon />
                  Add as a member
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}
