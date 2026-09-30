import { ReceiptTextIcon } from 'lucide-react'
import { useSearchParams } from 'react-router'
import { SIGN_IN_PATH } from './api'
import { Problem } from './components/Notice'
import { ThemeToggle } from './components/ThemeToggle'
import { Button } from './components/ui/button'
import { TooltipProvider } from './components/ui/tooltip'

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
    <TooltipProvider>
      <main className="relative flex min-h-svh flex-1 items-center justify-center bg-background p-6">
        <div className="absolute top-4 right-4">
          <ThemeToggle />
        </div>
        <div className="flex w-full max-w-sm flex-col gap-6 rounded-2xl border bg-card p-8 shadow-sm">
          <div className="flex flex-col items-start gap-4">
            <span className="flex size-10 items-center justify-center rounded-xl bg-primary text-primary-foreground">
              <ReceiptTextIcon aria-hidden="true" className="size-5" />
            </span>
            <div className="flex flex-col gap-1.5">
              <h1 className="text-xl font-semibold tracking-tight">Invoice Collection</h1>
              <p className="text-sm text-muted-foreground">
                Sign in to see what was collected and what needs a person.
              </p>
            </div>
          </div>
          {outcome && <Problem>{outcome}</Problem>}
          <form action={SIGN_IN_PATH} method="get">
            <Button type="submit" size="lg" className="w-full">
              Sign in with Google
            </Button>
          </form>
        </div>
      </main>
    </TooltipProvider>
  )
}
