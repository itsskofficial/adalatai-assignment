import { expect, test } from 'vitest'
import {
  formatAmount,
  formatDate,
  formatIndianAmount,
  formatRupees,
  isCollectionMonth,
  isNegative,
  monthName,
} from './format'

test.each([
  ['652.50', '652.50'],
  ['1652.50', '1,652.50'],
  ['1234567.00', '1,234,567.00'],
  ['-40.00', '-40.00'],
  ['-1040.00', '-1,040.00'],
  ['12', '12.00'],
  ['12.5', '12.50'],
  ['0.00', '0.00'],
])('amount %s is shown as %s', (amount, shown) => {
  expect(formatAmount(amount)).toBe(shown)
})

test.each([
  ['123456.00', '1,23,456.00'],
  ['12345678.5', '1,23,45,678.50'],
  ['1000', '1,000.00'],
  ['999.99', '999.99'],
  ['-850.00', '-850.00'],
  ['-123456.00', '-1,23,456.00'],
  ['0', '0.00'],
])('amount %s in Indian digit grouping is %s', (amount, shown) => {
  expect(formatIndianAmount(amount)).toBe(shown)
})

test('an amount in rupees carries the rupee sign before the digits', () => {
  expect(formatRupees('123456.00')).toBe('₹1,23,456.00')
  expect(formatRupees('-850.00')).toBe('-₹850.00')
})

test('only an amount below zero is negative', () => {
  expect(isNegative('-40.00')).toBe(true)
  expect(isNegative('40.00')).toBe(false)
  expect(isNegative('-0.00')).toBe(false)
})

test('a collection month is named in words', () => {
  expect(monthName('2026-08')).toBe('August 2026')
  expect(monthName('2026-12')).toBe('December 2026')
})

test('a date is shown the same whatever the time zone of the reader', () => {
  expect(formatDate('2026-08-03')).toBe('3 Aug 2026')
  expect(formatDate('2026-08-16T23:30:00+00:00')).toBe('16 Aug 2026')
})

test.each([
  ['2026-08', true],
  ['2026-13', false],
  ['2026-8', false],
  ['august', false],
])('%s is a collection month: %s', (text, expected) => {
  expect(isCollectionMonth(text)).toBe(expected)
})
