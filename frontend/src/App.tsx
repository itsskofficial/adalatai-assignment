import { useCallback, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router'
import { me, signOut } from './api'
import { AppShell } from './AppShell'
import { NotBuiltYet } from './NotBuiltYet'
import { QuestionsScreen } from './QuestionsScreen'
import { SCREENS } from './shell'
import { SignInPage } from './SignInPage'
import { SpendScreen } from './SpendScreen'
import { SummaryScreen } from './SummaryScreen'
import { useLoaded } from './useLoaded'

const BUILT = new Set<string>(['/summary', '/spend', '/questions'])

export default function App() {
  // Signing in leaves the page for Google and loads it afresh, so this never needs to go back.
  const [signedOut, setSignedOut] = useState(false)
  const person = useLoaded('me', me)

  const onSignedOut = useCallback(() => setSignedOut(true), [])

  async function leave() {
    try {
      await signOut()
    } catch {
      // The sign-in page is shown either way; a session that survived is found on next load.
    }
    setSignedOut(true)
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
          <AppShell email={person.value.email} onSignOut={leave} onSignedOut={onSignedOut} />
        }
      >
        <Route index element={<Navigate to="/summary" replace />} />
        <Route path="summary" element={<SummaryScreen />} />
        <Route path="spend" element={<SpendScreen />} />
        <Route path="questions" element={<QuestionsScreen />} />
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
