import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ScheduleEditor } from './ScheduleEditor'
const base = { timezone: 'UTC', enabled: true }
function renderWith(start: string, stop: string, onSave = vi.fn()) {
  render(
    <ScheduleEditor
      initial={{ ...base, rules: [{ days_of_week: ['mon'], start_time: start, stop_time: stop }] }}
      onSave={onSave}
      saving={false}
    />,
  )
  return onSave
}
describe('ScheduleEditor overnight rules', () => {
  it('allows saving a rule whose stop time is earlier than its start (stops the next day)', () => {
    const onSave = renderWith('22:00', '06:00')
    expect(screen.getByTestId('overnight-hint')).toHaveTextContent('stops the next day')
    const save = screen.getByRole('button', { name: 'Save schedule' })
    expect(save).toBeEnabled()
    fireEvent.click(save)
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        rules: [{ days_of_week: ['mon'], start_time: '22:00', stop_time: '06:00' }],
      }),
    )
  })
  it('shows no overnight hint for an ordinary same-day rule', () => {
    renderWith('09:00', '18:00')
    expect(screen.queryByTestId('overnight-hint')).toBeNull()
    expect(screen.getByRole('button', { name: 'Save schedule' })).toBeEnabled()
  })
  it('refuses equal start and stop times', () => {
    renderWith('09:00', '09:00')
    expect(screen.getByRole('button', { name: 'Save schedule' })).toBeDisabled()
    expect(screen.getByText(/must both be set, and different/)).toBeInTheDocument()
  })
})
