import { useEffect, useState } from 'react'
import { NotSignedIn } from './api'

export type Loaded<T> =
  | { status: 'loading' }
  | { status: 'ready'; value: T }
  | { status: 'not-signed-in' }
  | { status: 'problem'; message: string }

type Settled<T> = { key: string; outcome: Exclude<Loaded<T>, { status: 'loading' }> }

/**
 * Loads a value from the API, again whenever the key changes.
 * An answer that arrives after the key has changed is dropped.
 */
export function useLoaded<T>(key: string, load: (signal: AbortSignal) => Promise<T>): Loaded<T> {
  const [settled, setSettled] = useState<Settled<T> | null>(null)

  useEffect(() => {
    const abort = new AbortController()
    load(abort.signal).then(
      (value) => {
        if (!abort.signal.aborted) setSettled({ key, outcome: { status: 'ready', value } })
      },
      (problem: unknown) => {
        if (abort.signal.aborted) return
        const outcome: Settled<T>['outcome'] =
          problem instanceof NotSignedIn
            ? { status: 'not-signed-in' }
            : {
                status: 'problem',
                message: problem instanceof Error ? problem.message : String(problem),
              }
        setSettled({ key, outcome })
      },
    )
    return () => abort.abort()
    // The key stands for everything the load depends on.
    // oxlint-disable-next-line react/exhaustive-deps
  }, [key])

  return settled?.key === key ? settled.outcome : { status: 'loading' }
}
