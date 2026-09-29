// How amounts, dates and collection months are shown.

const MONTHS = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
]

export function isCollectionMonth(text: string): boolean {
  return /^\d{4}-(0[1-9]|1[0-2])$/.test(text)
}

/** "2026-08" becomes "August 2026". */
export function monthName(month: string): string {
  const [year, number] = month.split('-')
  return `${MONTHS[Number(number) - 1]} ${year}`
}

/** The date part of an ISO date or time, read as written so it never shifts by time zone. */
export function formatDate(iso: string): string {
  const [year, month, day] = iso.slice(0, 10).split('-')
  return `${Number(day)} ${MONTHS[Number(month) - 1]?.slice(0, 3)} ${year}`
}

/**
 * Two decimals and grouped thousands. Works on the digits as text,
 * so an amount is never rounded by being turned into a floating point number.
 */
export function formatAmount(amount: string): string {
  const sign = amount.startsWith('-') ? '-' : ''
  const [whole = '0', fraction = ''] = amount.replace(/^[-+]/, '').split('.')
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${sign}${grouped}.${fraction.padEnd(2, '0').slice(0, 2)}`
}

export function isNegative(amount: string): boolean {
  return amount.startsWith('-') && /[1-9]/.test(amount)
}

/** True for addresses a browser may safely open: the web, or this dashboard's own API. */
export function isSafeLink(link: string): boolean {
  return /^https?:\/\//i.test(link) || /^\/api\//.test(link)
}
