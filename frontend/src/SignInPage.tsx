import { useSearchParams } from 'react-router'
import { SIGN_IN_PATH } from './api'

const OUTCOMES: Record<string, string> = {
  refused:
    'That address is not allowed to use the dashboard. An administrator can add it on the ' +
    'People screen; ask one to, then sign in again.',
  failed: 'Sign-in did not complete. Try again.',
}

export function SignInPage() {
  const [search] = useSearchParams()
  const outcome = OUTCOMES[search.get('sign_in') ?? '']

  return (
    <main className="sign-in">
      <div className="sign-in-card">
        <h1>Invoice Collection</h1>
        <p>Sign in to see what was collected and what needs a person.</p>
        {outcome && (
          <p className="reasons" role="alert">
            {outcome}
          </p>
        )}
        <form action={SIGN_IN_PATH} method="get">
          <button type="submit" className="primary">
            Sign in with Google
          </button>
        </form>
      </div>
    </main>
  )
}
