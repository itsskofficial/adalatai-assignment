// Helpers for tests: the dashboard in a page, talking to a pretend API through fetch.

import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { vi } from 'vitest'
import App from '../App'
import type { MonthSummary } from '../api'

export type Call = { method: string; path: string }

const NOT_SIGNED_IN = { status: 401, body: { detail: 'Sign in to use the dashboard' } }

/**
 * Answers fetch from a table of "METHOD /path" to JSON body.
 * Anything not in the table answers 401, as the API does for a person not signed in.
 */
export function serve(answers: Record<string, unknown>): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = typeof input === 'string' ? input : input.toString()
      const method = init?.method ?? 'GET'
      calls.push({ method, path })
      const key = `${method} ${path}`
      if (!(key in answers)) {
        return Response.json(NOT_SIGNED_IN.body, { status: NOT_SIGNED_IN.status })
      }
      const body = answers[key]
      return body === null ? new Response(null, { status: 204 }) : Response.json(body)
    }),
  )
  return calls
}

export function signedIn(answers: Record<string, unknown> = {}): Record<string, unknown> {
  return { 'GET /api/me': { email: 'finance@nyayalabs.example' }, ...answers }
}

export function openDashboard(at = '/') {
  return render(
    <MemoryRouter initialEntries={[at]}>
      <App />
    </MemoryRouter>,
  )
}

export function emptySummary(month: string): MonthSummary {
  return {
    month,
    rows: [],
    totals: [],
    total_inr: '0.00',
    rows_without_rupees: 0,
    counts: { collected: 0, needs_review: 0, skipped: 0, failed: 0 },
    needs_review: [],
    skipped: [],
    failed: [],
    billing_signals: [],
    gaps: [],
    upcoming: [],
    failed_source_accounts: [],
  }
}

export const AUGUST: MonthSummary = {
  month: '2026-08',
  rows: [
    {
      vendor: 'Slack',
      document_type: 'invoice',
      date: '2026-08-03',
      amount: '1652.50',
      currency: 'USD',
      amount_inr: '157549.35',
      inr_rate: '95.34',
      source_account: 'engineering@nyayalabs.example',
      file_name: '2026-08_Slack_1652.50-USD.pdf',
      file_url: '/api/months/2026-08/billing-documents/2026-08_Slack_1652.50-USD.pdf',
      notes: 'receipt also received: 2026-08_Slack_1652.50-USD_2.pdf',
    },
    {
      vendor: 'Notion',
      document_type: 'receipt',
      date: '2026-08-09',
      amount: '221.40',
      currency: 'EUR',
      amount_inr: null,
      inr_rate: null,
      source_account: 'design@nyayalabs.example',
      file_name: '2026-08_Notion_221.40-EUR.pdf',
      file_url: 'https://drive.google.example/file/d/abc/view',
      notes: '',
    },
    {
      vendor: 'Figma',
      document_type: 'credit_note',
      date: '2026-08-21',
      amount: '-40.00',
      currency: 'USD',
      amount_inr: '-3828.00',
      inr_rate: '95.7',
      source_account: 'design@nyayalabs.example',
      file_name: '2026-08_Figma_-40.00-USD.pdf',
      file_url: '/api/months/2026-08/billing-documents/2026-08_Figma_-40.00-USD.pdf',
      notes: '',
    },
  ],
  totals: [
    { currency: 'EUR', amount: '221.40' },
    { currency: 'USD', amount: '1612.50' },
  ],
  total_inr: '153721.35',
  rows_without_rupees: 1,
  counts: { collected: 3, needs_review: 1, skipped: 2, failed: 1 },
  needs_review: [
    {
      source_account: 'engineering@nyayalabs.example',
      message_id: 'm-zoom',
      subject: 'Your Zoom invoice is ready',
      reason: 'manual download needed',
      portal_link: 'https://zoom.example/billing/invoices/889',
    },
  ],
  skipped: [
    {
      source_account: 'engineering@nyayalabs.example',
      message_id: 'm-news',
      subject: 'What is new in Slack',
      reason: 'not a billing email',
    },
    {
      source_account: 'engineering@nyayalabs.example',
      message_id: 'm-linear',
      subject: 'Payment failed for Linear',
      reason: 'billing signal: payment failed',
    },
  ],
  failed: [
    {
      source_account: 'engineering@nyayalabs.example',
      message_id: 'm-github',
      subject: 'Your GitHub invoice',
      reason: 'the PDF could not be read',
    },
  ],
  billing_signals: [
    {
      kind: 'payment_failed',
      vendor: 'Linear',
      source_account: 'engineering@nyayalabs.example',
      message_id: 'm-linear',
      subject: 'Payment failed for Linear',
      received_at: '2026-08-16T09:00:00+00:00',
    },
  ],
  gaps: [
    {
      vendor: 'Linear',
      kind: 'missing',
      source_account: 'engineering@nyayalabs.example',
      explanation: 'payment failed on 16 August',
    },
    { vendor: 'Zoho', kind: 'unknown', source_account: 'ops@nyayalabs.example', explanation: null },
  ],
  upcoming: [
    {
      vendor: '1Password',
      source_account: 'engineering@nyayalabs.example',
      note: 'Your 1Password subscription renews on September 24, 2026',
    },
  ],
  failed_source_accounts: [
    { source_account: 'ops@nyayalabs.example', reason: 'the sign-in no longer works' },
  ],
}
