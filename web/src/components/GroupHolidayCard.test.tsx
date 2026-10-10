import { describe, expect, it } from 'vitest'
import { tomorrowIn } from './GroupHolidayCard'
describe('tomorrowIn', () => {
  it('is tomorrow in the given timezone, not the browser or UTC date', () => {
    const now = new Date('2026-10-10T20:00:00Z')
    expect(tomorrowIn('Asia/Kolkata', now)).toBe('2026-10-12')
    expect(tomorrowIn('UTC', now)).toBe('2026-10-11')
    expect(tomorrowIn('America/Los_Angeles', now)).toBe('2026-10-11')
  })
})
