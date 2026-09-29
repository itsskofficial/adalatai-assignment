// What the dashboard keeps in the browser for the person signed in.

/**
 * Forgets everything kept for the person who was signed in, such as the questions they
 * asked and the answers, so the next person at this browser does not see it.
 */
export function forgetSession() {
  try {
    sessionStorage.clear()
  } catch {
    // Storage that cannot be reached holds nothing to forget, and signing out goes on.
  }
}
