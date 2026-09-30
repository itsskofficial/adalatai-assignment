import { useCallback, useEffect, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router'
import { me, signOut } from './api'
import { AppShell } from './AppShell'
import { DocumentHistoryScreen } from './DocumentHistoryScreen'
import { NotBuiltYet } from './NotBuiltYet'
import { PeopleScreen } from './PeopleScreen'
import { QuestionsScreen } from './QuestionsScreen'
import { ReviewScreen } from './ReviewScreen'
import { RunsScreen } from './RunsScreen'
import { SettingsScreen } from './SettingsScreen'
import { forgetSession } from './session'
import { SCREENS } from './shell'
import { SignInPage } from './SignInPage'
import { SourceAccountsScreen } from './SourceAccountsScreen'
import { SpendScreen } from './SpendScreen'
import { SummaryScreen } from './SummaryScreen'
import { useLoaded } from './useLoaded'
import { VendorsScreen } from './VendorsScreen'

const BUILT = new Set<string>([
  '/summary',
  '/review',
  '/vendors',
  '/spend',
  '/questions',
  '/source-accounts',
  '/runs',
  '/settings',
])

export default function App() {
  // Signing in leaves the page for Google and loads it afresh, so this never needs to go back.
  const [signedOut, setSignedOut] = useState(false)
  const person = useLoaded('me', me)

  const onSignedOut = useCallback(() => {
    forgetSession()
    setSignedOut(true)
  }, [])

  // A session that had ended before the page was opened leaves nothing behind either.
  const nobody = person.status === 'not-signed-in'
  useEffect(() => {
    if (nobody) forgetSession()
  }, [nobody])

  async function leave() {
    try {
      await signOut()
    } catch {
      // The sign-in page is shown either way; a session that survived is found on next load.
    }
    onSignedOut()
  }

  if (signedOut || person.status === 'not-signed-in') return <SignInPage />
  if (person.status === 'loading') return <p className="page-note">Loading…</p>
  if (person.status === 'problem') {
    return (
      <p className="page-note problem" role="alert">
        {person.message}
      </p>
    )
  }

  return (
    <Routes>
      <Route
        element={
          <AppShell
            email={person.value.email}
            role={person.value.role}
            onSignOut={leave}
            onSignedOut={onSignedOut}
          />
        }
      >
        <Route index element={<Navigate to="/summary" replace />} />
        <Route path="summary" element={<SummaryScreen />} />
        <Route path="review" element={<ReviewScreen />} />
        <Route path="vendors" element={<VendorsScreen />} />
        <Route path="spend" element={<SpendScreen />} />
        <Route path="questions" element={<QuestionsScreen />} />
        <Route
          path="people"
          element={<PeopleScreen administrator={person.value.role === 'administrator'} />}
        />
        <Route path="source-accounts" element={<SourceAccountsScreen />} />
        <Route path="documents/:contentHash" element={<DocumentHistoryScreen />} />
        <Route path="runs" element={<RunsScreen />} />
        <Route path="settings" element={<SettingsScreen />} />
        {SCREENS.filter((screen) => !BUILT.has(screen.path)).map((screen) => (
          <Route
            key={screen.path}
            path={screen.path}
            element={<NotBuiltYet name={screen.name} />}
          />
        ))}
        <Route path="*" element={<Navigate to="/summary" replace />} />
      </Route>
    </Routes>
  )
}
