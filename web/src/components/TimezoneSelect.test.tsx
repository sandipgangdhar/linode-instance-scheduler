import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { TimezoneSelect } from './TimezoneSelect'
async function waitForDebounce() {
  await new Promise((resolve) => setTimeout(resolve, 160))
}
describe('TimezoneSelect', () => {
  it('shows the curated common-zone list on focus, before typing anything', () => {
    render(<TimezoneSelect value="UTC" onChange={() => {}} />)
    fireEvent.focus(screen.getByRole('textbox'))
    expect(screen.getByText(/Common zones/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /UTC/i })).toBeInTheDocument()
  })
  it('finds a bare city name with no region prefix', async () => {
    render(<TimezoneSelect value="UTC" onChange={() => {}} />)
    const input = screen.getByRole('textbox')
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'kolkata' } })
    await waitForDebounce()
    const match = screen.queryByRole('button', { name: /kolkata|calcutta/i })
    expect(match).not.toBeNull()
  })
  it('finds a "Region/City" query typed with a space instead of an underscore', async () => {
    render(<TimezoneSelect value="UTC" onChange={() => {}} />)
    const input = screen.getByRole('textbox')
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'america/new york' } })
    await waitForDebounce()
    expect(screen.getByRole('button', { name: /new york/i })).toBeInTheDocument()
  })
  it('finds a hyphenated bare city name typed with spaces (e.g. Port-au-Prince)', async () => {
    render(<TimezoneSelect value="UTC" onChange={() => {}} />)
    const input = screen.getByRole('textbox')
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'port au prince' } })
    await waitForDebounce()
    expect(screen.getByRole('button', { name: /port.au.prince/i })).toBeInTheDocument()
  })
  it('selecting a suggestion calls onChange with the resolved zone and closes the dropdown', () => {
    const onChange = vi.fn()
    render(<TimezoneSelect value="UTC" onChange={onChange} />)
    const input = screen.getByRole('textbox')
    fireEvent.focus(input)
    fireEvent.click(screen.getByRole('button', { name: /UTC/i }))
    expect(onChange).toHaveBeenCalledWith('UTC')
    expect(screen.queryByText(/Common zones/i)).not.toBeInTheDocument()
  })
  it('Enter commits the LIVE typed value even if it fires before the 120ms debounce settles', () => {
    const onChange = vi.fn()
    render(<TimezoneSelect value="UTC" onChange={onChange} />)
    const input = screen.getByRole('textbox')
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'america/new_york' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('America/New_York')
  })
  it('Enter does nothing when the field is merely focused with nothing typed', () => {
    const onChange = vi.fn()
    render(<TimezoneSelect value="Asia/Tokyo" onChange={onChange} />)
    const input = screen.getByRole('textbox')
    fireEvent.focus(input)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).not.toHaveBeenCalled()
  })
  it('lets an unrecognized-but-typed name through directly via Enter, matching the empty-state promise', async () => {
    const onChange = vi.fn()
    render(<TimezoneSelect value="UTC" onChange={onChange} />)
    const input = screen.getByRole('textbox')
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'Not/A/Real/Zone' } })
    await waitForDebounce()
    expect(screen.getByText(/No match in the known list/i)).toBeInTheDocument()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('Not/A/Real/Zone')
  })
  it('the empty-state "use directly" button commits the exact typed text', async () => {
    const onChange = vi.fn()
    render(<TimezoneSelect value="UTC" onChange={onChange} />)
    const input = screen.getByRole('textbox')
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'Not/A/Real/Zone' } })
    await waitForDebounce()
    fireEvent.click(screen.getByRole('button', { name: /use.*not\/a\/real\/zone.*directly/i }))
    expect(onChange).toHaveBeenCalledWith('Not/A/Real/Zone')
  })
  it('Escape reverts the visible text to the current value without calling onChange', () => {
    const onChange = vi.fn()
    render(<TimezoneSelect value="Asia/Tokyo" onChange={onChange} />)
    const input = screen.getByRole('textbox')
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'some garbage' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(input).toHaveValue('Asia/Tokyo')
    expect(onChange).not.toHaveBeenCalled()
  })
  it('syncs the displayed text when `value` changes from outside (e.g. an async-loaded schedule)', () => {
    const { rerender } = render(<TimezoneSelect value="UTC" onChange={() => {}} />)
    expect(screen.getByRole('textbox')).toHaveValue('UTC')
    rerender(<TimezoneSelect value="Asia/Tokyo" onChange={() => {}} />)
    expect(screen.getByRole('textbox')).toHaveValue('Asia/Tokyo')
  })
  it('highlights the currently-selected zone in the common list', () => {
    render(<TimezoneSelect value="Asia/Tokyo" onChange={() => {}} />)
    fireEvent.focus(screen.getByRole('textbox'))
    const row = screen.getByRole('button', { name: /Asia\/Tokyo/i })
    expect(row.className).toMatch(/bg-indigo-50/)
  })
  it('is disabled when the disabled prop is set', () => {
    render(<TimezoneSelect value="UTC" onChange={() => {}} disabled />)
    expect(screen.getByRole('textbox')).toBeDisabled()
  })
  it('closing without selecting (click-away) does not fire onChange', () => {
    const onChange = vi.fn()
    render(
      <div>
        <TimezoneSelect value="UTC" onChange={onChange} />
        <div data-testid="outside">elsewhere</div>
      </div>,
    )
    fireEvent.focus(screen.getByRole('textbox'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'typed but not picked' } })
    fireEvent.mouseDown(screen.getByTestId('outside'))
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox')).toHaveValue('UTC')
  })
  it('renders a per-zone offset label next to each common-zone row', () => {
    render(<TimezoneSelect value="UTC" onChange={() => {}} />)
    fireEvent.focus(screen.getByRole('textbox'))
    const row = screen.getByRole('button', { name: /Asia\/Tokyo/i })
    expect(within(row).getByText(/GMT|UTC/i)).toBeInTheDocument()
  })
})
