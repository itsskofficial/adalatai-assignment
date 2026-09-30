import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'
import type { Answer } from './api'
import { openDashboard, Reply, serve, signedIn } from './test/dashboard'

const AWS_SUMMER: Answer = {
  question: 'How much did we spend on AWS this summer?',
  answered: true,
  answer: 'Total spend on AWS from June 2026 to August 2026 was ₹1,31,130.00 in 2 charges.',
  reason: null,
  query: {
    name: 'total_spend',
    parameters: {
      vendor: 'AWS',
      source_account: null,
      from_month: '2026-06',
      to_month: '2026-08',
    },
    description: 'Total spend on AWS, June 2026 to August 2026',
  },
  columns: [
    { key: 'inr_total', label: 'Total in rupees', kind: 'amount' },
    { key: 'charges', label: 'Charges', kind: 'count' },
  ],
  rows: [{ inr_total: '131130.00', charges: '2' }],
  documents: [
    {
      vendor: 'AWS',
      document_type: 'invoice',
      date: '2026-06-02',
      amount: '1400.00',
      currency: 'USD',
      amount_inr: '118380.00',
      source_account: 'engineering@nyayalabs.example',
      file_name: '2026-06_AWS_1400.00-USD.pdf',
      file_url: '/api/months/2026-06/billing-documents/2026-06_AWS_1400.00-USD.pdf',
    },
    {
      vendor: 'AWS',
      document_type: 'credit_note',
      date: '2026-08-02',
      amount: '150.00',
      currency: 'USD',
      amount_inr: '12750.00',
      source_account: 'engineering@nyayalabs.example',
      file_name: '2026-08_AWS_150.00-USD.pdf',
      file_url: 'https://drive.google.example/file/d/aws/view',
    },
  ],
}

const FORECAST: Answer = {
  question: 'What will we spend next year?',
  answered: false,
  answer: "I can't answer that yet.",
  reason: 'Forecasts are not among the fixed queries.',
  query: null,
  columns: [],
  rows: [],
  documents: [],
}

async function openQuestions(answer: unknown = AWS_SUMMER) {
  serve(signedIn({ 'GET /api/months': { months: ['2026-08'] }, 'POST /api/questions': answer }))
  openDashboard('/questions')
  await screen.findByRole('heading', { name: 'Questions' })
}

async function ask(text: string) {
  const box = screen.getByRole('textbox', { name: 'Question' })
  await userEvent.clear(box)
  await userEvent.type(box, text)
  await userEvent.click(screen.getByRole('button', { name: 'Ask' }))
}

function cellsOfRow(row: HTMLElement): string[] {
  return within(row)
    .getAllByRole('cell')
    .map((cell) => cell.textContent ?? '')
}

test('the Questions navigation entry opens the Questions screen', async () => {
  await openQuestions()

  expect(screen.getByRole('link', { name: 'Questions' })).toHaveClass('is-active')
  expect(screen.queryByText('Not built yet.')).not.toBeInTheDocument()
})

test('an example question fills the question box', async () => {
  await openQuestions()

  await userEvent.click(
    screen.getByRole('button', { name: 'How much did we spend on AWS last quarter?' }),
  )

  expect(screen.getByRole('textbox', { name: 'Question' })).toHaveValue(
    'How much did we spend on AWS last quarter?',
  )
})

test('an answer shows its sentence, the query in plain words and its result', async () => {
  await openQuestions()

  await ask('How much did we spend on AWS this summer?')

  const answer = await screen.findByRole('article', {
    name: 'How much did we spend on AWS this summer?',
  })
  const entry = within(answer)
  expect(
    entry.getByText(
      'Total spend on AWS from June 2026 to August 2026 was ₹1,31,130.00 in 2 charges.',
    ),
  ).toBeInTheDocument()
  expect(entry.getByText('Total spend on AWS, June 2026 to August 2026')).toBeInTheDocument()
  const result = entry.getByRole('table', { name: 'Result' })
  const [, row] = within(result).getAllByRole('row')
  expect(cellsOfRow(row!)).toEqual(['₹1,31,130.00', '2'])
})

test('an answer lists the billing documents behind it with links to the files', async () => {
  await openQuestions()

  await ask('How much did we spend on AWS this summer?')

  const documents = await screen.findByRole('table', { name: 'Billing documents behind this answer' })
  const [, ...rows] = within(documents).getAllByRole('row')
  expect(rows.map(cellsOfRow)).toEqual([
    [
      'AWS',
      'Invoice',
      '2 Jun 2026',
      '1,400.00',
      'USD',
      '₹1,18,380.00',
      'engineering@nyayalabs.example',
      '2026-06_AWS_1400.00-USD.pdf',
    ],
    [
      'AWS',
      'Credit note',
      '2 Aug 2026',
      '150.00',
      'USD',
      '₹12,750.00',
      'engineering@nyayalabs.example',
      '2026-08_AWS_150.00-USD.pdf',
    ],
  ])
  expect(within(documents).getByRole('link', { name: '2026-06_AWS_1400.00-USD.pdf' })).toHaveAttribute(
    'href',
    '/api/months/2026-06/billing-documents/2026-06_AWS_1400.00-USD.pdf',
  )
})

test('a question outside the fixed set says it cannot be answered yet, and why', async () => {
  await openQuestions(FORECAST)

  await ask('What will we spend next year?')

  const answer = await screen.findByRole('article', { name: 'What will we spend next year?' })
  expect(within(answer).getByText("I can't answer that yet.")).toBeInTheDocument()
  expect(within(answer).getByText('Forecasts are not among the fixed queries.')).toBeInTheDocument()
  expect(within(answer).queryByRole('table')).not.toBeInTheDocument()
})

test('a problem with the model is shown as an error', async () => {
  await openQuestions(
    new Reply(503, {
      detail: 'The model could not answer just now (HTTP 529). Try again in a minute.',
    }),
  )

  await ask('How much did we spend on AWS?')

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'The model could not answer just now (HTTP 529). Try again in a minute.',
  )
})

test('earlier questions and answers stay on screen', async () => {
  await openQuestions()

  await ask('How much did we spend on AWS this summer?')
  await screen.findByRole('article', { name: 'How much did we spend on AWS this summer?' })
  await ask('And AWS again?')

  await screen.findByRole('article', { name: 'And AWS again?' })
  expect(screen.getAllByRole('article')).toHaveLength(2)
  expect(screen.getByRole('textbox', { name: 'Question' })).toHaveValue('')
})

test('while a question is being answered the box waits, and earlier answers stay', async () => {
  let answer: (value: Answer) => void = () => {}
  const held = new Promise<Answer>((resolve) => {
    answer = resolve
  })
  let asked = 0
  serve(
    signedIn({
      'GET /api/months': { months: ['2026-08'] },
      // The first question is answered at once; the second waits until the test lets it go.
      'POST /api/questions': () => (asked++ === 0 ? AWS_SUMMER : held),
    }),
  )
  openDashboard('/questions')
  await screen.findByRole('heading', { name: 'Questions' })
  await ask('How much did we spend on AWS this summer?')
  await screen.findByText(AWS_SUMMER.answer)

  await ask('And AWS again?')

  const pending = screen.getByRole('article', { name: 'And AWS again?' })
  expect(within(pending).getByRole('status')).toHaveTextContent('Asking…')
  expect(screen.getByRole('textbox', { name: 'Question' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled()
  expect(screen.getByText(AWS_SUMMER.answer)).toBeVisible()

  answer({ ...AWS_SUMMER, question: 'And AWS again?' })

  await within(pending).findByText(AWS_SUMMER.answer)
  expect(within(pending).queryByRole('status')).toBeNull()
  expect(screen.getByRole('textbox', { name: 'Question' })).toBeEnabled()
})

test('an empty question is not sent', async () => {
  const calls = serve(signedIn({ 'GET /api/months': { months: [] } }))
  openDashboard('/questions')
  await screen.findByRole('heading', { name: 'Questions' })

  expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled()
  expect(calls.filter((call) => call.method === 'POST')).toEqual([])
})

test('questions and answers are forgotten when the person signs out', async () => {
  serve(
    signedIn({
      'GET /api/months': { months: [] },
      'POST /api/questions': AWS_SUMMER,
      'POST /auth/logout': null,
    }),
  )
  openDashboard('/questions')
  await userEvent.type(await screen.findByLabelText('Question'), AWS_SUMMER.question)
  await userEvent.click(screen.getByRole('button', { name: 'Ask' }))
  await screen.findByText(AWS_SUMMER.answer)
  expect(sessionStorage.getItem('questions-asked')).toContain('AWS')

  await userEvent.click(screen.getByRole('button', { name: 'Sign out' }))

  await screen.findByRole('button', { name: 'Sign in with Google' })
  expect(sessionStorage.getItem('questions-asked')).toBeNull()
})

test('questions and answers are forgotten when the session ends', async () => {
  const answers = signedIn({
    'GET /api/months': { months: [] },
    'POST /api/questions': AWS_SUMMER,
  })
  serve(answers)
  openDashboard('/questions')
  await userEvent.type(await screen.findByLabelText('Question'), AWS_SUMMER.question)
  await userEvent.click(screen.getByRole('button', { name: 'Ask' }))
  await screen.findByText(AWS_SUMMER.answer)

  delete answers['POST /api/questions']
  await userEvent.type(screen.getByLabelText('Question'), 'And on Slack?')
  await userEvent.click(screen.getByRole('button', { name: 'Ask' }))

  await screen.findByRole('button', { name: 'Sign in with Google' })
  expect(sessionStorage.getItem('questions-asked')).toBeNull()
})
