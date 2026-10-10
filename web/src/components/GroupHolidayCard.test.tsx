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
import { groupSkipsOn } from './GroupHolidayCard'
describe('groupSkipsOn', () => {
  const holidays = [
    { date: '2026-12-25', scope: 'all' as const, target: null, note: null, created_at: 'x' },
    { date: '2026-12-26', scope: 'group' as const, target: 'prod', note: null, created_at: 'x' },
    { date: '2026-12-27', scope: 'group' as const, target: 'other', note: null, created_at: 'x' },
  ]
  it('applies account-wide holidays only while the group follows them', () => {
    expect(groupSkipsOn('2026-12-25', holidays, 'prod', false)).toBe(true)
    expect(groupSkipsOn('2026-12-25', holidays, 'prod', true)).toBe(false)
  })
  it('always applies the group own holidays, never another group', () => {
    expect(groupSkipsOn('2026-12-26', holidays, 'prod', true)).toBe(true)
    expect(groupSkipsOn('2026-12-27', holidays, 'prod', false)).toBe(false)
  })
})
