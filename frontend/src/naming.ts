// How the file of a billing document is named, as the tool names it (naming.py).

import type { DocumentFields, DocumentType } from './api'

/** The total as it is filed: two decimals, and negative for a credit note. */
function filedTotal(total: string, documentType: DocumentType): string {
  const text = total.trim()
  if (!/^[-+]?(\d+\.?\d*|\.\d+)$/.test(text)) return text
  const value = Number(text)
  return (documentType === 'credit_note' ? -Math.abs(value) : value).toFixed(2)
}

/** The name the file will be saved under, as the tool names it. */
export function fileNameFor(fields: DocumentFields): string {
  const month = /^\d{4}-\d{2}/.test(fields.invoice_date.trim())
    ? fields.invoice_date.trim().slice(0, 7)
    : 'YYYY-MM'
  const vendor = fields.vendor.replace(/[^A-Za-z0-9]+/g, '')
  const total = filedTotal(fields.total, fields.document_type)
  return `${month}_${vendor}_${total}-${fields.currency.trim().toUpperCase()}.pdf`
}
