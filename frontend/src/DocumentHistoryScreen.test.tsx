import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'
import type { DocumentTrail, TrailEntry } from './api'
import { AUGUST, openDashboard, Reply, serve, signedIn } from './test/dashboard'

const ENGINEERING = 'engineering@nyayalabs.example'
const OPS = 'ops@nyayalabs.example'
const FINANCE = 'finance@nyayalabs.example'
const HASH = 'a'.repeat(64)
const TRAIL = `/api/billing-documents/${HASH}/trail`
const RUN = '2026-09-03T06:00:00+00:00'
const DECIDED = '2026-09-29T10:30:00+00:00'

function step(kind: string, details: Record<string, unknown>, changes: Partial<TrailEntry> = {}) {
  return {
    kind,
    at: RUN,
    source_account: ENGINEERING,
    actor: 'run',
    details,
    ...changes,
  } satisfies TrailEntry
}

const FIELDS = {
  vendor: 'Slack',
  invoice_date: '2026-08-03',
  total: '652.50',
  currency: 'USD',
  document_type: 'invoice',
}

const SLACK_TRAIL: DocumentTrail = {
  content_hash: HASH,
  fields: { ...FIELDS, total: '625.50', document_type: 'invoice' },
  state: 'collected',
  collection_month: '2026-08',
  invoice_format: 'attachment',
  source_accounts: [ENGINEERING, OPS],
  file_name: '2026-08_Slack_625.50-USD.pdf',
  file_url: '/api/months/2026-08/billing-documents/2026-08_Slack_625.50-USD.pdf',
  emails: [],
  recorded_before_trail: false,
  entries: [
    step(
      'received',
      {
        sender: 'Slack <feedback@slack.example>',
        subject: '<img src=x onerror=alert(1)> Your Slack invoice',
        message_id: 'm-slack',
      },
      { at: '2026-08-03T09:00:00+00:00', actor: null },
    ),
    step(
      'received',
      { sender: 'Slack <feedback@slack.example>', subject: 'Fwd: Your Slack invoice' },
      { at: '2026-08-04T09:00:00+00:00', actor: null, source_account: OPS },
    ),
    step(
      'classified',
      { kind: 'invoice', vendor: 'Slack', confidence: 'high', probability: 0.97 },
      { actor: 'jev-latest' },
    ),
    step('found', { invoice_format: 'attachment', attachment: 'invoice.pdf' }),
    step(
      'read',
      { fields: FIELDS, confidence: 'low', reader_note: 'the total is smudged' },
      { actor: 'claude-haiku-4-5' },
    ),
    step('checked', {
      stage: 'reading',
      checks: [
        {
          check: "the reader's own confidence",
          passed: false,
          doubts: [{ field: null, reason: 'the reader was unsure: the total is smudged' }],
        },
        { check: 'the total the email states', passed: true, doubts: [] },
      ],
    }),
    step(
      'read_again',
      {
        fields: FIELDS,
        confidence: 'high',
        reader_note: '',
        changes: [],
      },
      { actor: 'claude-sonnet-5-5' },
    ),
    step('checked', {
      stage: 'history',
      checks: [
        {
          check: "the vendor's usual amount",
          passed: false,
          doubts: [{ field: 'total', reason: 'the total is 40% above the usual 466.00 USD' }],
        },
      ],
    }),
    step('filed', {
      file_name: '2026-08_Slack_652.50-USD.pdf',
      pending: true,
      current: false,
      web_link: null,
    }),
    step('held', {
      doubts: [{ field: 'total', reason: 'the total is 40% above the usual 466.00 USD' }],
      waits_with_email: false,
    }),
    step(
      'corrected',
      { field: 'total', before: '652.50', after: '625.50' },
      { at: DECIDED, actor: FINANCE },
    ),
    step('approved', { changed_fields: ['total'] }, { at: DECIDED, actor: FINANCE }),
    step(
      'filed',
      {
        file_name: '2026-08_Slack_625.50-USD.pdf',
        pending: false,
        current: true,
        web_link: 'https://drive.example/2026-08/2026-08_Slack_625.50-USD.pdf',
      },
      { at: DECIDED, actor: FINANCE },
    ),
    step(
      'converted',
      { currency: 'USD', rate: '95.34', rate_date: '2026-08-03' },
      { at: DECIDED, actor: FINANCE },
    ),
  ],
}

function serveHistory(trail: unknown) {
  return serve(
    signedIn({
      'GET /api/months': { months: ['2026-08'] },
      'GET /api/months/2026-08/summary': {
        ...AUGUST,
        rows: AUGUST.rows.map((row, index) => ({
          ...row,
          content_hash: index === 0 ? HASH : null,
        })),
      },
      [`GET ${TRAIL}`]: trail,
    }),
  )
}

async function openHistory(trail: unknown = SLACK_TRAIL) {
  serveHistory(trail)
  openDashboard(`/documents/${HASH}?month=2026-08`)
  return screen.findByRole('list', { name: 'History' })
}

function steps(history: HTMLElement): HTMLElement[] {
  return Array.from(history.children) as HTMLElement[]
}

function stepHeadings(history: HTMLElement): string[] {
  return steps(history).map((item) => within(item).getByRole('heading').textContent ?? '')
}

test('a row of the summary leads to the history of its billing document', async () => {
  serveHistory(SLACK_TRAIL)
  openDashboard('/summary?month=2026-08')

  const link = await screen.findByRole('link', {
    name: 'History of 2026-08_Slack_1652.50-USD.pdf',
  })
  await userEvent.click(link)

  expect(
    await screen.findByRole('heading', { name: 'History of Slack invoice, 625.50 USD', level: 1 }),
  ).toBeVisible()
})

test('the history is a timeline from the email to the rupee rate', async () => {
  const history = await openHistory()

  expect(stepHeadings(history)).toEqual([
    `Email received in ${ENGINEERING}`,
    `Email received in ${OPS}`,
    'Classified as an invoice',
    'Billing document found: PDF attachment',
    'Read',
    'Checked the reading',
    'Read again by the stronger model',
    "Checked against the vendor's history",
    'Filed in the pending folder',
    'Held for review',
    'Total corrected',
    'Approved',
    'Filed',
    'Converted to rupees',
  ])
})

test('the source email, source accounts and invoice format are shown', async () => {
  const history = await openHistory()

  const facts = screen.getByRole('region', { name: 'About this document' })
  expect(within(facts).getByText(`${ENGINEERING}, ${OPS}`)).toBeVisible()
  expect(within(facts).getByText('PDF attachment')).toBeVisible()
  const [received] = steps(history)
  expect(within(received!).getByText('Slack <feedback@slack.example>')).toBeVisible()
  expect(within(received!).getByText('3 Aug 2026, 09:00')).toBeVisible()
})

test('text from an email is shown as text, never as HTML', async () => {
  const history = await openHistory()

  expect(
    within(history).getByText('<img src=x onerror=alert(1)> Your Slack invoice'),
  ).toBeVisible()
  expect(history.querySelector('img')).toBeNull()
})

test('the model, what it read and the checks that ran are shown', async () => {
  const history = await openHistory()
  const [, , classified, , read, checked, readAgain] = steps(history)

  expect(within(classified!).getByText('By jev-latest')).toBeVisible()
  expect(within(classified!).getByText(/Confidence high \(97%\)/)).toBeVisible()
  expect(within(read!).getByText('By claude-haiku-4-5')).toBeVisible()
  expect(within(read!).getByText('652.50')).toBeVisible()
  expect(within(read!).getByText(/Its note: the total is smudged/)).toBeVisible()
  expect(within(checked!).getByText(/The reader's own confidence: doubted/)).toBeVisible()
  expect(within(checked!).getByText('The reader was unsure: the total is smudged')).toBeVisible()
  expect(within(checked!).getByText(/The total the email states: passed/)).toBeVisible()
  expect(within(readAgain!).getByText('By claude-sonnet-5-5')).toBeVisible()
  expect(within(readAgain!).getByText('It read every field the same.')).toBeVisible()
})

test('each correction shows who made it, when, and the value before and after', async () => {
  const history = await openHistory()
  const corrected = steps(history)[10]!

  expect(within(corrected).getByText(`By ${FINANCE}`)).toBeVisible()
  expect(within(corrected).getByText('29 Sep 2026, 10:30')).toBeVisible()
  expect(within(corrected).getByText('652.50')).toBeVisible()
  expect(within(corrected).getByText('625.50')).toBeVisible()
})

test('the filed copy links to its file and the rate names its date', async () => {
  const history = await openHistory()
  const all = steps(history)
  const filed = all[12]!
  const converted = all[13]!

  expect(within(filed).getByRole('link', { name: '2026-08_Slack_625.50-USD.pdf' })).toHaveAttribute(
    'href',
    'https://drive.example/2026-08/2026-08_Slack_625.50-USD.pdf',
  )
  expect(within(converted).getByText('1 USD = ₹95.34, the rate on 3 Aug 2026')).toBeVisible()
})

test('a kind of step the dashboard does not know is shown by its name and details', async () => {
  const history = await openHistory({
    ...SLACK_TRAIL,
    entries: [
      step(
        'matched_to_vendor',
        { stated_vendor: 'Slack Technologies Ltd', expected_vendor: 'Slack' },
        { actor: 'rules' },
      ),
    ],
  })

  const [matched] = steps(history)
  expect(within(matched!).getByRole('heading', { name: 'Matched to vendor' })).toBeVisible()
  expect(within(matched!).getByText('Slack Technologies Ltd')).toBeVisible()
  expect(within(matched!).getByText('Stated vendor')).toBeVisible()
})

test('a document read before the history was kept says its history is short', async () => {
  const history = await openHistory({
    ...SLACK_TRAIL,
    recorded_before_trail: true,
    entries: [
      SLACK_TRAIL.entries[0],
      step(
        'filed',
        {
          file_name: '2026-08_Slack_652.50-USD.pdf',
          pending: false,
          current: true,
          web_link: null,
        },
        { at: null, actor: null },
      ),
    ],
  })

  expect(screen.getByRole('note')).toHaveTextContent(/before its history was recorded/)
  expect(within(steps(history)[1]!).getByText('Time not recorded')).toBeVisible()
})

test('an unknown document is reported plainly', async () => {
  serveHistory(new Reply(404, { detail: 'No such billing document' }))
  openDashboard(`/documents/${HASH}?month=2026-08`)

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'The ledger holds no billing document with this identity.',
  )
})
