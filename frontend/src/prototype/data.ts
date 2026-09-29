// PROTOTYPE, throwaway. Mock review queue. Nothing here is saved.

export type Field = 'vendor' | 'invoiceDate' | 'total' | 'currency' | 'documentType'

export type PendingItem = {
  id: string
  pdf: string
  sourceAccounts: string[]
  from: string
  subject: string
  format: 'Attachment' | 'Email body' | 'Portal link'
  fields: Record<Field, string>
  flagged: Field[]
  reasons: string[]
  usual?: string
}

export const FIELD_LABELS: Record<Field, string> = {
  vendor: 'Vendor',
  invoiceDate: 'Invoice date',
  total: 'Total',
  currency: 'Currency',
  documentType: 'Document type',
}

export const FIELDS = Object.keys(FIELD_LABELS) as Field[]

export const QUEUE: PendingItem[] = [
  {
    id: 'a1',
    pdf: '/prototype/2026-08_Slack_652.50-USD.pdf',
    sourceAccounts: ['engineering@nyayalabs.example'],
    from: 'Slack <feedback@slack.com>',
    subject: 'Your Slack invoice is available',
    format: 'Attachment',
    fields: { vendor: 'Slack', invoiceDate: '2026-08-03', total: '652.50', currency: 'USD', documentType: 'Invoice' },
    flagged: ['total'],
    reasons: ['Total is 38% above the usual amount for Slack'],
    usual: 'Usually about 472.50 USD',
  },
  {
    id: 'a2',
    pdf: '/prototype/2026-08_Notion_221.40-EUR.pdf',
    sourceAccounts: ['ops@nyayalabs.example', 'finance@nyayalabs.example'],
    from: 'Notion <team@mail.notion.so>',
    subject: 'Your receipt from Notion #2391-7745',
    format: 'Email body',
    fields: { vendor: 'Notion', invoiceDate: '2026-08-14', total: '180.00', currency: 'EUR', documentType: 'Receipt' },
    flagged: ['total', 'currency'],
    reasons: [
      'Total in the email text (221.40) differs from the extracted total (180.00)',
      'Currency is EUR; Notion usually bills in USD',
    ],
    usual: 'Usually about 190.00 USD',
  },
  {
    id: 'a3',
    pdf: '/prototype/2026-08_Figma_190.00-USD.pdf',
    sourceAccounts: ['engineering@nyayalabs.example'],
    from: 'Figma <billing@figma.com>',
    subject: 'Your Figma invoice is ready',
    format: 'Portal link',
    fields: { vendor: 'Figma', invoiceDate: '2026-08-21', total: '190.00', currency: 'USD', documentType: 'Invoice' },
    flagged: ['invoiceDate'],
    reasons: ['Second invoice from Figma this month'],
    usual: 'Usually about 190.00 USD',
  },
  {
    id: 'a4',
    pdf: '/prototype/2026-08_Slack_652.50-USD.pdf',
    sourceAccounts: ['ops@nyayalabs.example'],
    from: 'Zoom <billing@zoom.us>',
    subject: 'Zoom payment confirmation',
    format: 'Attachment',
    fields: { vendor: 'Zoom Video Communications', invoiceDate: '2026-08-09', total: '149.90', currency: 'USD', documentType: 'Receipt' },
    flagged: ['vendor'],
    reasons: ['Vendor name does not match any expected vendor'],
  },
  {
    id: 'a5',
    pdf: '/prototype/2026-08_Figma_190.00-USD.pdf',
    sourceAccounts: ['finance@nyayalabs.example'],
    from: 'Linear <billing@linear.app>',
    subject: 'Receipt for Linear',
    format: 'Email body',
    fields: { vendor: 'Linear', invoiceDate: '2026-08-27', total: '96.00', currency: 'USD', documentType: 'Receipt' },
    flagged: ['documentType'],
    reasons: ['Could not tell whether this is a receipt or a renewal reminder'],
    usual: 'Usually about 96.00 USD',
  },
]

export function filename(f: Record<Field, string>): string {
  const vendor = f.vendor.replace(/[^A-Za-z0-9]+/g, '')
  return `${f.invoiceDate.slice(0, 7)}_${vendor}_${f.total}-${f.currency}.pdf`
}
