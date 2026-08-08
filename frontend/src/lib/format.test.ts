import { describe, expect, it } from 'vitest'
import { formatBytes, formatDuration, formatTimecode } from './format'

describe('formatDuration', () => {
  it('formats minutes and seconds', () => {
    expect(formatDuration(0)).toBe('0:00')
    expect(formatDuration(9)).toBe('0:09')
    expect(formatDuration(75)).toBe('1:15')
  })

  it('survives nonsense rather than rendering NaN', () => {
    expect(formatDuration(Number.NaN)).toBe('0:00')
    expect(formatDuration(-5)).toBe('0:00')
  })
})

describe('formatTimecode', () => {
  it('includes the frame number', () => {
    expect(formatTimecode(1.5, 24)).toBe('0:01.12')
    expect(formatTimecode(61.0, 24)).toBe('1:01.00')
  })
})

describe('formatBytes', () => {
  it('scales units', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(2048)).toBe('2.0 KB')
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB')
  })
})
