// Leaving the dashboard for another site, kept apart so tests can watch where it goes.

export const browser = {
  /** Sends the person to another address, such as Google's sign-in page. */
  goTo(url: string): void {
    window.location.assign(url)
  },
}
